"""Module 07 lab: ontology-constrained triple extraction, entity resolution, a guarded Cypher planner and
an in-memory graph store behind a Neo4j-shaped async interface.

The GraphStore protocol mirrors what an async Neo4j driver session offers (execute a parameterized read
query); InMemoryGraph interprets the planner's multi-hop pattern so the lab runs offline. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
from collections import defaultdict, deque
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "msg": record.getMessage()}
        payload.update(getattr(record, "ctx", {}))
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


log = get_logger("lab07")


class GraphError(Exception):
    pass


class OntologyViolation(GraphError):
    pass


class UnsafeCypherError(GraphError):
    pass


class GraphTimeout(GraphError):
    """Retryable read timeout."""


NodeType = Literal["Company", "Person", "Product", "Sanction"]
RelType = Literal["OWNS", "SUPPLIES", "DIRECTS", "SANCTIONED_BY"]
ALLOWED_PATTERNS: dict[str, tuple[str, str]] = {
    "OWNS": ("Company", "Company"), "SUPPLIES": ("Company", "Product"),
    "DIRECTS": ("Person", "Company"), "SANCTIONED_BY": ("Company", "Sanction"),
}


class Entity(BaseModel):
    name: str = Field(min_length=1)
    type: NodeType


class Triple(BaseModel):
    head: Entity
    rel: RelType
    tail: Entity
    evidence: str = Field(min_length=5)
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


def validate_ontology(t: Triple) -> Triple:
    want = ALLOWED_PATTERNS[t.rel]
    if (t.head.type, t.tail.type) != want:
        raise OntologyViolation(f"{t.rel} requires {want[0]}->{want[1]}, got {t.head.type}->{t.tail.type}")
    return t


LEGAL_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|ltd|llc|plc|gmbh|sa)\b\.?", re.I)


def normalize(name: str) -> str:
    n = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", LEGAL_SUFFIX.sub(" ", n)).strip()


class EntityResolver:
    """Alias table + normalization. Real systems add blocking and fuzzy scoring with human review."""

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._alias = {normalize(k): v for k, v in (aliases or {}).items()}
        self._seen: dict[str, str] = {}

    def resolve(self, e: Entity) -> str:
        key = normalize(e.name)
        canon = self._alias.get(key, key)
        node_id = f"{e.type}:{canon}"
        self._seen.setdefault(node_id, e.name)
        return node_id


class Edge(BaseModel):
    src: str
    rel: str
    dst: str
    evidence: str


class GraphStore(Protocol):
    async def upsert(self, nodes: dict[str, str], edges: list[Edge]) -> None: ...
    async def read(self, cypher: str, params: dict[str, object]) -> list[dict[str, object]]: ...


class InMemoryGraph:
    """Interprets exactly the planner's pattern; a real Neo4j adapter would send `cypher` unchanged."""

    def __init__(self, fail_first_reads: int = 0) -> None:
        self.nodes: dict[str, str] = {}
        self.adj: dict[str, list[Edge]] = defaultdict(list)
        self._fails = fail_first_reads

    async def upsert(self, nodes: dict[str, str], edges: list[Edge]) -> None:
        self.nodes.update(nodes)  # MERGE semantics: idempotent
        for e in edges:
            if not any(x.dst == e.dst and x.rel == e.rel for x in self.adj[e.src]):
                self.adj[e.src].append(e)

    async def read(self, cypher: str, params: dict[str, object]) -> list[dict[str, object]]:
        await asyncio.sleep(0.001)
        if self._fails > 0:
            self._fails -= 1
            raise GraphTimeout("transaction timed out")
        seed, depth, limit = str(params["seed"]), int(params["depth"]), int(params["limit"])  # type: ignore[call-overload]
        out: list[dict[str, object]] = []
        queue: deque[tuple[str, list[Edge]]] = deque([(seed, [])])
        seen = {seed}
        while queue and len(out) < limit:
            node, path = queue.popleft()
            if len(path) >= depth:
                continue
            for e in self.adj.get(node, []):
                if e.dst in seen:
                    continue
                seen.add(e.dst)
                newp = path + [e]
                out.append({"end": e.dst, "end_name": self.nodes.get(e.dst), "path": [f"{p.src}-[{p.rel}]->{p.dst}" for p in newp],
                            "evidence": [p.evidence for p in newp]})
                queue.append((e.dst, newp))
        return out


WRITE_KEYWORDS = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|CALL)\b", re.I)


class CypherPlanner:
    """Template planner (the safe default). Values always travel as parameters, never string-interpolated."""

    MAX_DEPTH = 3
    MAX_LIMIT = 50

    def plan(self, seed_id: str, rel_types: list[str], depth: int, limit: int, tenant: str) -> tuple[str, dict[str, object]]:
        for r in rel_types:
            if r not in ALLOWED_PATTERNS:
                raise UnsafeCypherError(f"relationship {r!r} not in ontology")
        depth, limit = min(depth, self.MAX_DEPTH), min(limit, self.MAX_LIMIT)
        rels = "|".join(rel_types)
        cypher = (f"MATCH p = (s {{id: $seed, tenant: $tenant}})-[:{rels}*1..{depth}]->(e) "
                  "RETURN e.id AS end, e.name AS end_name, [r IN relationships(p) | type(r)] AS rels LIMIT $limit")
        return cypher, {"seed": seed_id, "tenant": tenant, "depth": depth, "limit": limit}


def guard_cypher(cypher: str) -> str:
    """Static defense-in-depth for any (LLM-generated) query. The real control is a read-only DB role."""
    if WRITE_KEYWORDS.search(cypher):
        raise UnsafeCypherError("write or procedure clause rejected")
    if not re.search(r"\bLIMIT\b", cypher, re.I):
        raise UnsafeCypherError("query must be bounded with LIMIT")
    for m in re.finditer(r"\*(\d*)\.\.(\d*)", cypher):
        if not m.group(2) or int(m.group(2)) > CypherPlanner.MAX_DEPTH:
            raise UnsafeCypherError("variable-length path must have an upper bound <= 3")
    if re.search(r"\*\s*(\]|\))", cypher):
        raise UnsafeCypherError("unbounded variable-length pattern")
    return cypher


async def read_with_retry(store: GraphStore, cypher: str, params: dict[str, object], attempts: int = 4) -> list[dict[str, object]]:
    for i in range(1, attempts + 1):
        try:
            return await store.read(guard_cypher(cypher), params)
        except GraphTimeout:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, 0.005 * 2 ** i))
    raise AssertionError("unreachable")


class ScriptedExtractor:
    """Stand-in for the LLM extractor (Module 05 pattern). Includes one ontology violation and one low-confidence triple."""

    def extract(self, doc: str) -> list[dict[str, object]]:
        return [
            {"head": {"name": "Acme Corp.", "type": "Company"}, "rel": "OWNS", "tail": {"name": "Borealis Ltd", "type": "Company"},
             "evidence": "Acme Corp. completed its acquisition of Borealis Ltd in 2021.", "confidence": 0.95},
            {"head": {"name": "Borealis", "type": "Company"}, "rel": "SUPPLIES", "tail": {"name": "Widget X", "type": "Product"},
             "evidence": "Borealis supplies the casing for Widget X.", "confidence": 0.9},
            {"head": {"name": "Acme Corporation", "type": "Company"}, "rel": "SANCTIONED_BY", "tail": {"name": "OFAC-2024-17", "type": "Sanction"},
             "evidence": "Acme Corporation was listed under OFAC-2024-17 last spring.", "confidence": 0.4},
            {"head": {"name": "Widget X", "type": "Product"}, "rel": "OWNS", "tail": {"name": "Borealis", "type": "Company"},
             "evidence": "The product owns the supplier (extraction error).", "confidence": 0.9},
            {"head": {"name": "Borealis Limited", "type": "Company"}, "rel": "SANCTIONED_BY", "tail": {"name": "OFAC-2024-17", "type": "Sanction"},
             "evidence": "Borealis Limited appears on sanctions list OFAC-2024-17.", "confidence": 0.92},
        ]


async def ingest(doc: str, store: GraphStore, tenant: str, min_conf: float = 0.6) -> dict[str, int]:
    resolver = EntityResolver()
    nodes: dict[str, str] = {}
    edges: list[Edge] = []
    stats = {"accepted": 0, "low_confidence": 0, "ontology_violations": 0}
    for raw in ScriptedExtractor().extract(doc):
        t = Triple.model_validate(raw)
        if t.confidence < min_conf:
            stats["low_confidence"] += 1
            continue
        try:
            validate_ontology(t)
        except OntologyViolation as exc:
            stats["ontology_violations"] += 1
            log.warning("rejected triple", extra={"ctx": {"reason": str(exc)}})
            continue
        h, tl = resolver.resolve(t.head), resolver.resolve(t.tail)
        nodes[h], nodes[tl] = t.head.name, t.tail.name
        edges.append(Edge(src=h, rel=t.rel, dst=tl, evidence=t.evidence))
        stats["accepted"] += 1
    await store.upsert(nodes, edges)
    return stats


async def main() -> None:
    store = InMemoryGraph(fail_first_reads=2)
    stats = await ingest("(document text)", store, tenant="t1")
    log.info("ingested", extra={"ctx": {**stats, "nodes": len(store.nodes)}})

    planner = CypherPlanner()
    cypher, params = planner.plan("Company:acme", ["OWNS", "SUPPLIES", "SANCTIONED_BY"], depth=5, limit=500, tenant="t1")
    log.info("planned", extra={"ctx": {"cypher": cypher, "params": params}})
    rows = await read_with_retry(store, cypher, params)
    for r in rows:
        log.info("path", extra={"ctx": {"end": r["end"], "hops": len(r["path"]), "path": r["path"]}})  # type: ignore[arg-type]

    for bad in ("MATCH (n) DETACH DELETE n", "MATCH (a)-[*]->(b) RETURN b LIMIT 5", "MATCH (a)-[:OWNS*1..9]->(b) RETURN b LIMIT 5",
                "CALL apoc.cypher.runMany('x') YIELD row RETURN row LIMIT 1"):
        try:
            guard_cypher(bad)
        except UnsafeCypherError as exc:
            log.info("guard blocked", extra={"ctx": {"query": bad, "reason": str(exc)}})


if __name__ == "__main__":
    asyncio.run(main())
