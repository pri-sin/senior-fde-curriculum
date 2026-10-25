"""Module 07 lab: ontology-constrained triple extraction, entity resolution, a guarded Cypher planner and
an in-memory graph store behind a Neo4j-shaped async interface. The "Syllabus extension" sections add a typed property
graph, taxonomy vs ontology, relational-to-graph loading, a Cypher-subset pattern engine, join ordering, logistics and
interaction models, chatbot graph retrieval, and Neo4j/AuraDB and Neptune adapters.

The GraphStore protocol mirrors what an async Neo4j driver session offers (execute a parameterized read
query); InMemoryGraph interprets the planner's multi-hop pattern so the lab runs offline. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import math
import os
import random
import re
import sqlite3
import sys
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol, TypeVar

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


LEGAL_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|llp|lp|plc|gmbh|ag|sa|bv|nv|pty)\b\.?", re.I)


def normalize(name: str) -> str:
    n = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", LEGAL_SUFFIX.sub(" ", n)).strip()


class EntityResolver:
    """Alias table + normalization. Real systems add blocking and fuzzy scoring with human review."""

    def __init__(self, tenant: str, aliases: dict[str, str] | None = None) -> None:
        if not tenant:
            raise GraphError("tenant is required: node ids are tenant-scoped")
        self.tenant = tenant
        self._alias = {normalize(k): v for k, v in (aliases or {}).items()}
        self._seen: dict[str, str] = {}

    def resolve(self, e: Entity) -> str:
        key = normalize(e.name)
        canon = self._alias.get(key, key)
        node_id = f"{self.tenant}|{e.type}:{canon}"  # tenant prefix: the same name under two tenants is two nodes
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
        if not seed.startswith(f"{params['tenant']}|"):  # fail closed: a seed outside the caller's tenant is never traversed
            raise GraphError("seed is outside the caller's tenant")
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
                  "RETURN e.id AS end, e.name AS end_name, [r IN relationships(p) | type(r)] AS rels, "
                  "[r IN relationships(p) | r.evidence] AS evidence LIMIT $limit")
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
    resolver = EntityResolver(tenant)
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


# ---- Syllabus extension: typed property graph (nodes, relationships, properties) ---------------------
# A labelled property graph: every node has one label and a property map, every relationship has a type,
# a direction and its own property map. Constraints and indexes mirror what Neo4j enforces server-side.
PropValue = str | int | float | bool
T = TypeVar("T")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # labels and types cannot be Cypher parameters, so they are validated


class ConstraintViolation(GraphError):
    pass


class PatternSyntaxError(GraphError):
    pass


class TransformError(GraphError):
    pass


class MissingDependencyError(GraphError):
    """An optional third-party adapter was used without its package installed."""


class MissingCredentialsError(GraphError):
    pass


class GNode(BaseModel):
    id: str
    label: str
    props: dict[str, PropValue] = Field(default_factory=dict)


class GRel(BaseModel):
    id: int
    type: str
    src: str
    dst: str
    props: dict[str, PropValue] = Field(default_factory=dict)


class LabelSchema(BaseModel):
    required: list[str] = Field(default_factory=list)
    unique: list[str] = Field(default_factory=list)


def _check_ident(name: str) -> str:
    if not IDENT.match(name):
        raise ConstraintViolation(f"{name!r} is not a safe label, type or property name")
    return name


class PropertyGraph:
    """In-memory labelled property graph with adjacency lists, label sets, property indexes and constraints."""

    def __init__(self) -> None:
        self.nodes: dict[str, GNode] = {}
        self.rels: dict[int, GRel] = {}
        self._out: dict[str, list[int]] = defaultdict(list)
        self._in: dict[str, list[int]] = defaultdict(list)
        self.by_label: dict[str, set[str]] = defaultdict(set)
        self.schema: dict[str, LabelSchema] = {}
        self._indexes: dict[tuple[str, str], dict[PropValue, set[str]]] = {}
        self._unique: dict[tuple[str, str], dict[PropValue, str]] = {}
        self._rel_key: dict[tuple[str, str, str], int] = {}
        self.rel_stats: Counter[tuple[str, str, str]] = Counter()  # (type, src label, dst label) -> count: the planner's catalog
        self.touched = 0  # rows touched by scans and expansions: the cost metric of the join demo

    # -- schema -------------------------------------------------------------------------------------
    def create_index(self, label: str, prop: str) -> None:
        _check_ident(label), _check_ident(prop)
        idx: dict[PropValue, set[str]] = {}
        for nid in self.by_label.get(label, ()):
            if prop in self.nodes[nid].props:
                idx.setdefault(self.nodes[nid].props[prop], set()).add(nid)
        self._indexes[(label, prop)] = idx

    def add_unique_constraint(self, label: str, prop: str) -> None:
        _check_ident(label), _check_ident(prop)
        owners: dict[PropValue, str] = {}
        for nid in self.by_label.get(label, ()):
            v = self.nodes[nid].props.get(prop)
            if v is None:
                continue
            if v in owners:
                raise ConstraintViolation(f"existing data violates uniqueness of {label}.{prop}: {v!r}")
            owners[v] = nid
        self._unique[(label, prop)] = owners
        sch = self.schema.setdefault(label, LabelSchema())
        if prop not in sch.unique:
            sch.unique.append(prop)
        self.create_index(label, prop)  # a uniqueness constraint is backed by an index, as in Neo4j

    def require_properties(self, label: str, *props: str) -> None:
        sch = self.schema.setdefault(_check_ident(label), LabelSchema())
        sch.required.extend(p for p in props if p not in sch.required)

    def _validate(self, label: str, nid: str, props: dict[str, PropValue]) -> None:
        sch = self.schema.get(label)
        if sch is None:
            return
        for p in sch.required:
            if props.get(p) is None:
                raise ConstraintViolation(f"{label}.{p} is required")
        for p in sch.unique:
            v = props.get(p)
            owner = self._unique[(label, p)].get(v) if v is not None else None
            if owner not in (None, nid):
                raise ConstraintViolation(f"{label}.{p}={v!r} already belongs to {owner}")

    def _reindex(self, node: GNode, add: bool) -> None:
        for (label, prop), idx in self._indexes.items():
            if label != node.label or prop not in node.props:
                continue
            v = node.props[prop]
            if add:
                idx.setdefault(v, set()).add(node.id)
            else:
                bucket = idx.get(v)
                if bucket is not None:
                    bucket.discard(node.id)
                    if not bucket:
                        idx.pop(v)
        for (label, prop), owners in self._unique.items():
            if label == node.label and prop in node.props:
                if add:
                    owners[node.props[prop]] = node.id
                elif owners.get(node.props[prop]) == node.id:
                    owners.pop(node.props[prop])

    # -- writes -------------------------------------------------------------------------------------
    def add_node(self, label: str, node_id: str, props: dict[str, PropValue] | None = None) -> GNode:
        _check_ident(label)
        if node_id in self.nodes:
            raise ConstraintViolation(f"node id {node_id!r} already exists")
        props = dict(props or {})
        self._validate(label, node_id, props)
        node = GNode(id=node_id, label=label, props=props)
        self.nodes[node_id] = node
        self.by_label[label].add(node_id)
        self._reindex(node, add=True)
        return node

    def merge_node(self, label: str, node_id: str, props: dict[str, PropValue] | None = None) -> GNode:
        """Cypher `MERGE (n:Label {id: $id}) SET n += $props`: create if missing, otherwise update properties."""
        node = self.nodes.get(node_id)
        if node is None:
            return self.add_node(label, node_id, props)
        if node.label != label:
            raise ConstraintViolation(f"{node_id!r} exists with label {node.label}, not {label}")
        merged = {**node.props, **(props or {})}
        self._validate(label, node_id, merged)
        self._reindex(node, add=False)
        node.props = merged
        self._reindex(node, add=True)
        return node

    def add_rel(self, rtype: str, src: str, dst: str, props: dict[str, PropValue] | None = None) -> GRel:
        _check_ident(rtype)
        if src not in self.nodes or dst not in self.nodes:
            raise ConstraintViolation(f"both endpoints must exist: {src!r} -> {dst!r}")
        rel = GRel(id=len(self.rels) + 1, type=rtype, src=src, dst=dst, props=dict(props or {}))
        self.rels[rel.id] = rel
        self._out[src].append(rel.id)
        self._in[dst].append(rel.id)
        self._rel_key[(rtype, src, dst)] = rel.id
        self.rel_stats[(rtype, self.nodes[src].label, self.nodes[dst].label)] += 1
        return rel

    def merge_rel(self, rtype: str, src: str, dst: str, props: dict[str, PropValue] | None = None) -> GRel:
        rid = self._rel_key.get((rtype, src, dst))
        if rid is None:
            return self.add_rel(rtype, src, dst, props)
        self.rels[rid].props.update(props or {})
        return self.rels[rid]

    # -- reads --------------------------------------------------------------------------------------
    def find(self, label: str | None, props: dict[str, Any] | None = None) -> list[str]:
        """Index lookup when an indexed property is given, otherwise a label scan. Every inspected node counts as touched."""
        props = props or {}
        pool: set[str] | None = None
        if label:
            for p, v in props.items():
                idx = self._indexes.get((label, p))
                if idx is not None:
                    bucket = idx.get(v, set())
                    if pool is None or len(bucket) < len(pool):
                        pool = bucket
            if pool is None:
                pool = self.by_label.get(label, set())
        else:
            pool = set(self.nodes)
        out: list[str] = []
        for nid in sorted(pool):
            self.touched += 1
            if all(self.nodes[nid].props.get(k) == v for k, v in props.items()):
                out.append(nid)
        return out

    def neighbors(self, node_id: str, types: list[str] | None = None, direction: str = "out") -> list[tuple[GRel, str]]:
        res: list[tuple[GRel, str]] = []
        if direction in ("out", "both"):
            for rid in self._out.get(node_id, []):
                self.touched += 1
                r = self.rels[rid]
                if not types or r.type in types:
                    res.append((r, r.dst))
        if direction in ("in", "both"):
            for rid in self._in.get(node_id, []):
                self.touched += 1
                r = self.rels[rid]
                if not types or r.type in types:
                    res.append((r, r.src))
        return res

    def degree(self, node_id: str) -> int:
        return len(self._out.get(node_id, [])) + len(self._in.get(node_id, []))

    # -- statistics used by the join-order estimator -------------------------------------------------
    def estimate_candidates(self, label: str | None, props: dict[str, Any]) -> float:
        if not label:
            return float(len(self.nodes))
        est = float(len(self.by_label.get(label, ())))
        for p, v in props.items():
            idx = self._indexes.get((label, p))
            est = min(est, float(len(idx.get(v, ())))) if idx is not None else est * 0.1  # unindexed equality: assume 10 percent
        return est

    def fanout(self, label: str | None, types: list[str], direction: str) -> float:
        n = len(self.by_label.get(label, ())) if label else len(self.nodes)
        if n == 0:
            return 0.0
        total = 0
        for (t, src_label, dst_label), c in self.rel_stats.items():
            if types and t not in types:
                continue
            if label:
                total += c if direction in ("out", "both") and src_label == label else 0
                total += c if direction in ("in", "both") and dst_label == label else 0
            else:
                total += c * (2 if direction == "both" else 1)
        return total / n


def build_demo_property_graph() -> PropertyGraph:
    """Teaching graph for nodes, relationships and properties: constraints reject bad writes."""
    g = PropertyGraph()
    g.add_unique_constraint("Person", "email")
    g.require_properties("Person", "email", "name")
    g.add_node("Person", "p1", {"name": "Ada", "email": "ada@example.com", "age": 36})
    g.add_node("Company", "c1", {"name": "Acme", "founded": 1999})
    g.add_rel("WORKS_AT", "p1", "c1", {"since": 2019, "role": "engineer"})  # properties live on the relationship too
    return g


# ---- Syllabus extension: taxonomy vs ontology --------------------------------------------------------
class Taxonomy:
    """A taxonomy is a single-parent is-a tree: one place per term, only subsumption queries."""

    def __init__(self) -> None:
        self.parent: dict[str, str | None] = {}

    def add(self, term: str, parent: str | None = None) -> None:
        if term in self.parent:
            raise OntologyViolation(f"{term!r} already has a place in the tree: a taxonomy allows one parent")
        if parent is not None and parent not in self.parent:
            raise OntologyViolation(f"unknown parent {parent!r}")
        self.parent[term] = parent

    def ancestors(self, term: str) -> list[str]:
        out: list[str] = []
        cur = self.parent[term]
        while cur is not None:
            out.append(cur)
            cur = self.parent[cur]
        return out

    def descendants(self, term: str) -> list[str]:
        return sorted(t for t in self.parent if term in self.ancestors(t))

    def is_a(self, term: str, other: str) -> bool:
        return term == other or other in self.ancestors(term)

    def lowest_common_ancestor(self, a: str, b: str) -> str | None:
        chain_a = [a] + self.ancestors(a)
        return next((x for x in [b] + self.ancestors(b) if x in chain_a), None)


class RelationDef(BaseModel):
    name: str
    domain: str
    range: str
    transitive: bool = False
    symmetric: bool = False
    inverse_of: str | None = None


class Ontology:
    """Typed classes (multiple inheritance allowed), relations with domain and range, and a small reasoner."""

    def __init__(self) -> None:
        self.parents: dict[str, set[str]] = {}
        self.relations: dict[str, RelationDef] = {}
        self.instances: dict[str, str] = {}
        self.facts: set[tuple[str, str, str]] = set()

    def add_class(self, name: str, parents: tuple[str, ...] = ()) -> None:
        for p in parents:
            if p not in self.parents:
                raise OntologyViolation(f"unknown superclass {p!r}")
        self.parents[name] = set(parents)

    def add_relation(self, rel: RelationDef) -> None:
        for c in (rel.domain, rel.range):
            if c not in self.parents:
                raise OntologyViolation(f"relation {rel.name} uses unknown class {c!r}")
        if rel.transitive and rel.domain != rel.range:
            raise OntologyViolation("a transitive relation needs the same domain and range")
        self.relations[rel.name] = rel
        if rel.inverse_of:  # declare the inverse automatically, with swapped domain and range
            self.relations.setdefault(rel.inverse_of, RelationDef(name=rel.inverse_of, domain=rel.range, range=rel.domain, inverse_of=rel.name))

    def superclasses(self, cls: str) -> set[str]:
        out, stack = {cls}, [cls]
        while stack:
            for p in self.parents[stack.pop()]:
                if p not in out:
                    out.add(p)
                    stack.append(p)
        return out

    def add_instance(self, ident: str, cls: str) -> None:
        if cls not in self.parents:
            raise OntologyViolation(f"unknown class {cls!r}")
        self.instances[ident] = cls

    def check_fact(self, s: str, p: str, o: str) -> None:
        rel = self.relations.get(p)
        if rel is None:
            raise OntologyViolation(f"unknown relation {p!r}")
        if s not in self.instances or o not in self.instances:
            raise OntologyViolation(f"unknown instance in ({s}, {p}, {o})")
        if rel.domain not in self.superclasses(self.instances[s]):
            raise OntologyViolation(f"{p} domain is {rel.domain}, but {s} is a {self.instances[s]}")
        if rel.range not in self.superclasses(self.instances[o]):
            raise OntologyViolation(f"{p} range is {rel.range}, but {o} is a {self.instances[o]}")

    def assert_fact(self, s: str, p: str, o: str) -> None:
        self.check_fact(s, p, o)
        self.facts.add((s, p, o))

    def validate(self) -> list[str]:
        """Instance-data validation: report every fact that breaks domain or range (the SHACL-style audit)."""
        problems: list[str] = []
        for s, p, o in sorted(self.facts):
            try:
                self.check_fact(s, p, o)
            except OntologyViolation as exc:
                problems.append(str(exc))
        return problems

    def infer(self) -> set[tuple[str, str, str]]:
        """Forward-chain inverse, symmetric and transitive rules to a fixpoint; returns only the new facts."""
        known = set(self.facts)
        while True:
            new: set[tuple[str, str, str]] = set()
            for s, p, o in known:
                rel = self.relations[p]
                if rel.inverse_of:
                    new.add((o, rel.inverse_of, s))
                if rel.symmetric:
                    new.add((o, p, s))
                if rel.transitive:
                    new |= {(s, p, o2) for s2, p2, o2 in known if p2 == p and s2 == o}
            if new <= known:
                return known - self.facts
            known |= new


def build_logistics_ontology() -> Ontology:
    onto = Ontology()
    for name, parents in (("Thing", ()), ("Organization", ("Thing",)), ("Company", ("Organization",)), ("Facility", ("Thing",)),
                          ("Hub", ("Facility",)), ("TemperatureControlled", ("Thing",)), ("ColdChainHub", ("Hub", "TemperatureControlled")),
                          ("Region", ("Thing",))):
        onto.add_class(name, parents)
    onto.add_relation(RelationDef(name="PART_OF", domain="Region", range="Region", transitive=True))
    onto.add_relation(RelationDef(name="OWNS", domain="Organization", range="Facility", inverse_of="OWNED_BY"))
    onto.add_relation(RelationDef(name="PARTNER_OF", domain="Organization", range="Organization", symmetric=True))
    for ident, cls in (("Acme", "Company"), ("Borealis", "Company"), ("FRA", "Hub"), ("RTM", "ColdChainHub"),
                       ("Hesse", "Region"), ("Germany", "Region"), ("EU", "Region")):
        onto.add_instance(ident, cls)
    for fact in (("Hesse", "PART_OF", "Germany"), ("Germany", "PART_OF", "EU"), ("Acme", "OWNS", "FRA"), ("Acme", "PARTNER_OF", "Borealis"),
                 ("Borealis", "OWNS", "RTM")):
        onto.assert_fact(*fact)
    return onto


# ---- Syllabus extension: relational tables to a graph (sqlite3 source, idempotent Cypher MERGE) --------
class CypherStmt(BaseModel):
    text: str
    params: dict[str, Any] = Field(default_factory=dict)


class TableInfo(BaseModel):
    name: str
    columns: list[str]
    pk: list[str]
    fks: list[tuple[str, str, str]]  # (column, referenced table, referenced column)


def introspect(conn: sqlite3.Connection) -> dict[str, TableInfo]:
    tables: dict[str, TableInfo] = {}
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for name in names:
        info = conn.execute(f"PRAGMA table_info({_check_ident(name)})").fetchall()  # (cid, name, type, notnull, default, pk position)
        pk = [r[1] for r in sorted((r for r in info if r[5] > 0), key=lambda r: r[5])]
        tables[name] = TableInfo(name=name, columns=[r[1] for r in info], pk=pk, fks=[])
    for name in names:
        for row in conn.execute(f"PRAGMA foreign_key_list({name})").fetchall():  # (id, seq, table, from, to, ...)
            ref_table, col, ref_col = row[2], row[3], row[4]
            if ref_col is None:
                ref_col = tables[ref_table].pk[0]
            tables[name].fks.append((col, ref_table, ref_col))
    return tables


def label_for(table: str) -> str:
    """customers -> Customer, order_items -> OrderItem (naive singularization is enough for a schema-driven demo)."""
    words = []
    for w in table.split("_"):
        singular = w[:-3] + "y" if w.endswith("ies") else (w[:-1] if w.endswith("s") and not w.endswith("ss") else w)
        words.append(singular.capitalize())
    return "".join(words)


def is_join_table(t: TableInfo) -> bool:
    """A table whose whole primary key is made of exactly two foreign keys is a many-to-many relationship, not an entity."""
    fk_cols = {c for c, _, _ in t.fks}
    return len(t.fks) == 2 and bool(t.pk) and set(t.pk) <= fk_cols


def relational_to_cypher(conn: sqlite3.Connection, rel_names: dict[str, str] | None = None) -> list[CypherStmt]:
    """Rules: entity table -> node label; row -> node {id, non-FK columns}; FK column -> relationship child to parent;
    join table -> relationship between the two referenced tables, extra columns become relationship properties."""
    rel_names = rel_names or {}
    tables = introspect(conn)
    stmts: list[CypherStmt] = []
    entities = {n: t for n, t in tables.items() if not is_join_table(t)}
    for name in entities:
        lab = _check_ident(label_for(name))
        stmts.append(CypherStmt(text=f"CREATE CONSTRAINT {lab.lower()}_id IF NOT EXISTS FOR (n:{lab}) REQUIRE n.id IS UNIQUE"))
    for name, t in entities.items():
        if len(t.pk) != 1:
            raise TransformError(f"entity table {name} needs a single-column primary key")
        lab = label_for(name)
        fk_cols = {c for c, _, _ in t.fks}
        cols = ", ".join(_check_ident(c) for c in t.columns)
        for row in conn.execute(f"SELECT {cols} FROM {name} ORDER BY {t.pk[0]}"):
            rec = dict(zip(t.columns, row))
            props = {k: v for k, v in rec.items() if k not in fk_cols and k != t.pk[0] and v is not None}  # FKs become relationships
            stmts.append(CypherStmt(text=f"MERGE (n:{lab} {{id: $id}}) SET n += $props", params={"id": str(rec[t.pk[0]]), "props": props}))

    def rel_stmt(rtype: str, a_label: str, b_label: str, src: object, dst: object, props: dict[str, Any]) -> CypherStmt:
        text = (f"MATCH (a:{a_label} {{id: $src}}), (b:{b_label} {{id: $dst}}) "
                f"MERGE (a)-[r:{_check_ident(rtype)}]->(b) SET r += $props")
        return CypherStmt(text=text, params={"src": str(src), "dst": str(dst), "props": props})

    for name, t in entities.items():
        lab = label_for(name)
        for col, ref_table, _ in t.fks:
            rtype = rel_names.get(f"{name}.{col}", "HAS_" + re.sub(r"_id$", "", col).upper())
            for pk, fk in conn.execute(f"SELECT {t.pk[0]}, {col} FROM {name} WHERE {col} IS NOT NULL ORDER BY {t.pk[0]}"):
                stmts.append(rel_stmt(rtype, lab, label_for(ref_table), pk, fk, {}))
    for name, t in tables.items():
        if not is_join_table(t):
            continue
        (c1, rt1, _), (c2, rt2, _) = sorted(t.fks, key=lambda fk: t.columns.index(fk[0]))  # PRAGMA lists FKs in reverse; restore column order
        extra = [c for c in t.columns if c not in (c1, c2)]
        rtype = rel_names.get(name, f"{label_for(rt1).upper()}_{label_for(rt2).upper()}")
        sel = ", ".join(_check_ident(c) for c in [c1, c2, *extra])
        for row in conn.execute(f"SELECT {sel} FROM {name} ORDER BY {c1}, {c2}"):
            props = {k: v for k, v in zip(extra, row[2:]) if v is not None}
            stmts.append(rel_stmt(rtype, label_for(rt1), label_for(rt2), row[0], row[1], props))
    return stmts


CONSTRAINT_RE = re.compile(r"^CREATE CONSTRAINT \w+ IF NOT EXISTS FOR \(n:(\w+)\) REQUIRE n\.(\w+) IS UNIQUE$")
NODE_MERGE_RE = re.compile(r"^MERGE \(n:(\w+) \{id: \$id\}\) SET n \+= \$props$")
REL_MERGE_RE = re.compile(r"^MATCH \(a:(\w+) \{id: \$src\}\), \(b:(\w+) \{id: \$dst\}\) MERGE \(a\)-\[r:(\w+)\]->\(b\) SET r \+= \$props$")


def load_cypher(graph: PropertyGraph, stmts: list[CypherStmt]) -> dict[str, int]:
    """Executes the three statement shapes the transformer emits. A Neo4j driver would run the same text unchanged."""
    done = {"constraints": 0, "nodes": 0, "rels": 0}
    for s in stmts:
        if m := CONSTRAINT_RE.match(s.text):
            graph.add_unique_constraint(m.group(1), m.group(2))
            done["constraints"] += 1
        elif m := NODE_MERGE_RE.match(s.text):
            nid = str(s.params["id"])
            graph.merge_node(m.group(1), f"{m.group(1)}:{nid}", {"id": nid, **s.params["props"]})
            done["nodes"] += 1
        elif m := REL_MERGE_RE.match(s.text):
            graph.merge_rel(m.group(3), f"{m.group(1)}:{s.params['src']}", f"{m.group(2)}:{s.params['dst']}", s.params["props"])
            done["rels"] += 1
        else:
            raise TransformError(f"unsupported statement: {s.text[:60]}")
    return done


def make_sales_db(customers: int = 6, orders: int = 12, products: int = 5, seed: int = 11) -> sqlite3.Connection:
    rnd = random.Random(seed)
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, tier TEXT);
        CREATE TABLE products(id INTEGER PRIMARY KEY, sku TEXT NOT NULL, name TEXT, unit_price REAL);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), placed_at TEXT, total REAL);
        CREATE TABLE order_items(order_id INTEGER REFERENCES orders(id), product_id INTEGER REFERENCES products(id), qty INTEGER,
                                 PRIMARY KEY (order_id, product_id));
    """)
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?)", [(i, f"Customer {i}", rnd.choice(["gold", "silver"])) for i in range(1, customers + 1)])
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", [(i, f"SKU-{i:03d}", f"Product {i}", 10.0 + i) for i in range(1, products + 1)])
    for oid in range(1, orders + 1):
        conn.execute("INSERT INTO orders VALUES (?, ?, ?, ?)", (oid, rnd.randint(1, customers), f"2024-{rnd.randint(1, 12):02d}-01", 0.0))
        for pid in rnd.sample(range(1, products + 1), rnd.randint(1, min(3, products))):
            conn.execute("INSERT INTO order_items VALUES (?, ?, ?)", (oid, pid, rnd.randint(1, 5)))
    return conn


# ---- Syllabus extension: pattern matching and path traversal (Cypher-like subset) -----------------------
MAX_VARLEN = 6
NODE_RE = re.compile(r"\(\s*(?P<var>\w+)?\s*(?::\s*(?P<label>\w+))?\s*(?P<props>\{[^}]*\})?\s*\)")
REL_RE = re.compile(
    r"(?P<l><)?-\s*(?:\[\s*(?P<var>\w+)?\s*(?::\s*(?P<types>\w+(?:\|\w+)*))?\s*"
    r"(?P<star>\*\s*(?P<min>\d+)?\s*(?:(?P<dots>\.\.)\s*(?P<max>\d+)?)?)?\s*(?P<props>\{[^}]*\})?\s*\])?\s*-(?P<r>>)?")
PROP_RE = re.compile(r'''(\w+)\s*:\s*('[^']*'|"[^"]*"|\$\w+|-?\d+(?:\.\d+)?|true|false)''', re.I)
COND_RE = re.compile(r"^\s*(\w+)\.(\w+)\s*(<>|<=|>=|=|<|>|CONTAINS)\s*(.+?)\s*$", re.I | re.S)
RET_RE = re.compile(r"^\s*(?:length\(\s*(\w+)\s*\)|(\w+)(?:\.(\w+))?)\s*(?:AS\s+(\w+))?\s*$", re.I)
QUERY_RE = re.compile(
    r"^\s*MATCH\s+(?:(?P<pv>\w+)\s*=\s*)?(?P<pat>.+?)(?:\s+WHERE\s+(?P<where>.+?))?\s+RETURN\s+(?P<ret>.+?)"
    r"(?:\s+LIMIT\s+(?P<lim>\d+|\$\w+))?\s*$", re.I | re.S)


class NodePat(BaseModel):
    var: str | None = None
    label: str | None = None
    props: dict[str, Any] = Field(default_factory=dict)


class RelPat(BaseModel):
    var: str | None = None
    types: list[str] = Field(default_factory=list)
    direction: Literal["out", "in", "both"] = "out"
    min_hops: int = 1
    max_hops: int = 1
    variable: bool = False
    props: dict[str, Any] = Field(default_factory=dict)


class Condition(BaseModel):
    var: str
    prop: str
    op: str
    value: Any


class ReturnItem(BaseModel):
    kind: Literal["var", "prop", "length"]
    var: str
    prop: str | None = None
    name: str


class ParsedQuery(BaseModel):
    path_var: str | None = None
    nodes: list[NodePat]
    rels: list[RelPat]
    where: list[Condition] = Field(default_factory=list)
    returns: list[ReturnItem]
    limit: int | None = None


def _literal(tok: str, params: dict[str, Any]) -> Any:
    t = tok.strip()
    if t.startswith("$"):
        if t[1:] not in params:
            raise PatternSyntaxError(f"missing parameter {t}")
        return params[t[1:]]
    if t[0] in "'\"":
        return t[1:-1]
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    return float(t) if "." in t else int(t)


def _parse_props(text: str | None, params: dict[str, Any]) -> dict[str, Any]:
    if not text:
        return {}
    inner = text.strip()[1:-1]
    if PROP_RE.sub("", inner).replace(",", "").strip():
        raise PatternSyntaxError(f"cannot parse property map {text!r}")
    return {m.group(1): _literal(m.group(2), params) for m in PROP_RE.finditer(inner)}


def _rel_pat(m: re.Match[str], params: dict[str, Any]) -> RelPat:
    if m.group("l") and m.group("r"):
        raise PatternSyntaxError("a relationship cannot point both ways: use -- for undirected")
    direction: Literal["out", "in", "both"] = "in" if m.group("l") else ("out" if m.group("r") else "both")
    lo = hi = 1
    if m.group("star"):
        mn = int(m.group("min")) if m.group("min") else None
        if m.group("dots"):
            lo, hi = (mn if mn is not None else 1), (int(m.group("max")) if m.group("max") else 0)
        elif mn is not None:
            lo = hi = mn
        else:
            lo, hi = 1, 0
        if hi == 0:
            raise PatternSyntaxError("variable-length patterns need an explicit upper bound (e.g. *1..3)")
        if hi > MAX_VARLEN or lo > hi:
            raise PatternSyntaxError(f"hop range {lo}..{hi} is invalid or above the maximum of {MAX_VARLEN}")
    return RelPat(var=m.group("var"), types=(m.group("types") or "").split("|") if m.group("types") else [], direction=direction,
                  min_hops=lo, max_hops=hi, variable=bool(m.group("star")), props=_parse_props(m.group("props"), params))


def parse_query(text: str, params: dict[str, Any] | None = None) -> ParsedQuery:
    params = params or {}
    qm = QUERY_RE.match(text)
    if not qm:
        raise PatternSyntaxError("expected: MATCH <pattern> [WHERE ...] RETURN ... [LIMIT n]")
    pat = qm.group("pat").strip()
    nodes: list[NodePat] = []
    rels: list[RelPat] = []
    nm = NODE_RE.match(pat)
    if not nm:
        raise PatternSyntaxError(f"pattern must start with a node: {pat[:30]!r}")
    nodes.append(NodePat(var=nm.group("var"), label=nm.group("label"), props=_parse_props(nm.group("props"), params)))
    pos = nm.end()
    while pos < len(pat):
        rm = REL_RE.match(pat, pos)
        if not rm:
            raise PatternSyntaxError(f"cannot parse relationship at {pat[pos:pos + 20]!r}")
        nm = NODE_RE.match(pat, rm.end())
        if not nm:
            raise PatternSyntaxError(f"relationship must end in a node at {pat[rm.end():rm.end() + 20]!r}")
        rels.append(_rel_pat(rm, params))
        nodes.append(NodePat(var=nm.group("var"), label=nm.group("label"), props=_parse_props(nm.group("props"), params)))
        pos = nm.end()
    where: list[Condition] = []
    if qm.group("where"):
        for part in re.split(r"\s+AND\s+", qm.group("where"), flags=re.I):
            cm = COND_RE.match(part)
            if not cm:
                raise PatternSyntaxError(f"cannot parse condition {part!r} (only AND of var.prop <op> value is supported)")
            where.append(Condition(var=cm.group(1), prop=cm.group(2), op=cm.group(3).upper(), value=_literal(cm.group(4), params)))
    returns: list[ReturnItem] = []
    for part in qm.group("ret").split(","):
        rt = RET_RE.match(part)
        if not rt:
            raise PatternSyntaxError(f"cannot parse RETURN item {part!r}")
        if rt.group(1):
            returns.append(ReturnItem(kind="length", var=rt.group(1), name=rt.group(4) or f"length({rt.group(1)})"))
        elif rt.group(3):
            returns.append(ReturnItem(kind="prop", var=rt.group(2), prop=rt.group(3), name=rt.group(4) or f"{rt.group(2)}.{rt.group(3)}"))
        else:
            returns.append(ReturnItem(kind="var", var=rt.group(2), name=rt.group(4) or rt.group(2)))
    declared = {n.var for n in nodes if n.var} | {r.var for r in rels if r.var} | ({qm.group("pv")} if qm.group("pv") else set())
    for used in [c.var for c in where] + [r.var for r in returns]:
        if used not in declared:
            raise PatternSyntaxError(f"variable {used!r} is not declared in the MATCH pattern")
    lim = qm.group("lim")
    limit = None if lim is None else int(_literal(lim, params))
    return ParsedQuery(path_var=qm.group("pv"), nodes=nodes, rels=rels, where=where, returns=returns, limit=limit)


class QueryPlan(BaseModel):
    anchor: Literal["left", "right"]
    anchor_var: str | None
    estimated_cost: float
    alternatives: dict[str, float]


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    plan: QueryPlan
    touched: int


class PatternEngine:
    """Executes MATCH patterns by anchoring on one end, then expanding hop by hop with trail semantics
    (a relationship is used at most once per path). The anchor is chosen by a cardinality cost estimate."""

    def __init__(self, graph: PropertyGraph) -> None:
        self.graph = graph

    @staticmethod
    def _reverse(nodes: list[NodePat], rels: list[RelPat]) -> tuple[list[NodePat], list[RelPat]]:
        flip = {"out": "in", "in": "out", "both": "both"}
        return nodes[::-1], [r.model_copy(update={"direction": flip[r.direction]}) for r in rels[::-1]]

    def _cost(self, nodes: list[NodePat], rels: list[RelPat]) -> float:
        rows = self.graph.estimate_candidates(nodes[0].label, nodes[0].props)
        total = rows
        for i, rp in enumerate(rels):
            f = self.graph.fanout(nodes[i].label, rp.types, rp.direction)
            rows *= sum(f ** h for h in range(rp.min_hops, rp.max_hops + 1)) if rp.variable else f
            nxt = nodes[i + 1]
            pop = self.graph.estimate_candidates(nxt.label, nxt.props)
            base = float(len(self.graph.by_label.get(nxt.label, ()))) if nxt.label else float(len(self.graph.nodes))
            rows *= (pop / base) if base else 0.0  # selectivity of the next node filter
            total += rows
        return total

    def plan(self, q: ParsedQuery, anchor: str = "auto") -> QueryPlan:
        left = self._cost(q.nodes, q.rels)
        right = self._cost(*self._reverse(q.nodes, q.rels))
        pick = anchor if anchor in ("left", "right") else ("right" if right < left else "left")
        start = q.nodes[-1] if pick == "right" else q.nodes[0]
        return QueryPlan(anchor=pick, anchor_var=start.var, estimated_cost=round(right if pick == "right" else left, 1),
                         alternatives={"left": round(left, 1), "right": round(right, 1)})  # type: ignore[arg-type]

    def run(self, text: str, params: dict[str, Any] | None = None, anchor: str = "auto") -> QueryResult:
        q = parse_query(text, params)
        plan = self.plan(q, anchor)
        nodes, rels = (self._reverse(q.nodes, q.rels) if plan.anchor == "right" else (q.nodes, q.rels))
        before = self.graph.touched
        bindings = self._match(q, nodes, rels, reversed_=plan.anchor == "right")
        rows = [{it.name: self._project(it, b) for it in q.returns} for b in bindings]
        return QueryResult(columns=[it.name for it in q.returns], rows=rows, plan=plan, touched=self.graph.touched - before)

    def _node_ok(self, pat: NodePat, nid: str) -> bool:
        n = self.graph.nodes[nid]
        return (pat.label is None or n.label == pat.label) and all(n.props.get(k) == v for k, v in pat.props.items())

    def _expand(self, start: str, rp: RelPat, used: frozenset[int]) -> list[tuple[str, list[GRel], list[str]]]:
        res: list[tuple[str, list[GRel], list[str]]] = []

        def dfs(cur: str, chain: list[GRel], mids: list[str], seen: frozenset[int]) -> None:
            depth = len(chain)
            if depth >= rp.min_hops:
                res.append((cur, list(chain), list(mids)))
            if depth >= rp.max_hops:
                return
            for r, other in self.graph.neighbors(cur, rp.types, rp.direction):
                if r.id in seen or any(r.props.get(k) != v for k, v in rp.props.items()):
                    continue
                chain.append(r)
                dfs(other, chain, mids + ([cur] if depth > 0 else []), seen | {r.id})
                chain.pop()

        dfs(start, [], [], used)
        return res

    def _match(self, q: ParsedQuery, nodes: list[NodePat], rels: list[RelPat], reversed_: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def bind(binding: dict[str, Any], pat: NodePat, nid: str) -> dict[str, Any] | None:
            if pat.var is None:
                return binding
            if pat.var in binding and binding[pat.var] != nid:
                return None  # a repeated variable must refer to the same node
            return {**binding, pat.var: nid}

        def extend(i: int, nid: str, binding: dict[str, Any], path: list[str], used: frozenset[int]) -> None:
            if q.limit is not None and len(out) >= q.limit:
                return
            if i == len(rels):
                full = dict(binding)
                if q.path_var:
                    full[q.path_var] = path[::-1] if reversed_ else path
                if all(self._holds(c, full) for c in q.where):
                    out.append(full)
                return
            rp, npat = rels[i], nodes[i + 1]
            for end, chain, mids in self._expand(nid, rp, used):
                if not self._node_ok(npat, end):
                    continue
                nb = bind(binding, npat, end)
                if nb is None:
                    continue
                if rp.var:
                    nb = {**nb, rp.var: chain if rp.variable else chain[0]}
                extend(i + 1, end, nb, path + mids + [end], used | {r.id for r in chain})

        for sid in self.graph.find(nodes[0].label, nodes[0].props):
            nb = bind({}, nodes[0], sid)
            if nb is not None:
                extend(0, sid, nb, [sid], frozenset())
        return out

    def _props_of(self, obj: Any) -> dict[str, Any] | None:
        if isinstance(obj, str):
            return self.graph.nodes[obj].props
        if isinstance(obj, GRel):
            return obj.props
        return None

    def _holds(self, c: Condition, binding: dict[str, Any]) -> bool:
        props = self._props_of(binding.get(c.var))
        val = None if props is None else props.get(c.prop)
        if val is None:
            return False  # null never satisfies a predicate, as in Cypher
        try:
            return {"=": val == c.value, "<>": val != c.value, "<": val < c.value, "<=": val <= c.value, ">": val > c.value,
                    ">=": val >= c.value, "CONTAINS": isinstance(val, str) and str(c.value) in val}[c.op]
        except TypeError:
            return False

    def _project(self, it: ReturnItem, b: dict[str, Any]) -> Any:
        obj = b.get(it.var)
        if it.kind == "length":
            if isinstance(obj, list):
                return len(obj) - 1 if obj and isinstance(obj[0], str) else len(obj)  # a path counts hops, a rel list counts rels
            return 0
        if it.kind == "prop":
            props = self._props_of(obj)
            return None if props is None else props.get(it.prop or "")
        if isinstance(obj, GRel):
            return obj.type
        if isinstance(obj, list):
            return [x.type if isinstance(x, GRel) else x for x in obj]
        return obj


class PathResult(BaseModel):
    nodes: list[str]
    rel_ids: list[int]
    cost: float
    score: float = 0.0


def shortest_path(g: PropertyGraph, src: str, dst: str, rel_types: list[str] | None = None, blocked: frozenset[str] = frozenset(),
                  weight_prop: str | None = None, direction: str = "out") -> PathResult | None:
    """Dijkstra (BFS when weight_prop is None). Blocked nodes are never entered: 'route around the closed hub'."""
    if src in blocked or dst in blocked or src not in g.nodes or dst not in g.nodes:
        return None
    tie = 0
    heap: list[tuple[float, int, str, list[str], list[int]]] = [(0.0, tie, src, [src], [])]
    best: dict[str, float] = {}
    while heap:
        cost, _, cur, nodes, rids = heapq.heappop(heap)
        if cur == dst:
            return PathResult(nodes=nodes, rel_ids=rids, cost=cost)
        if best.get(cur, float("inf")) <= cost:
            continue
        best[cur] = cost
        for r, other in g.neighbors(cur, rel_types, direction):
            if other in blocked:
                continue
            w = float(r.props.get(weight_prop, 1.0)) if weight_prop else 1.0
            tie += 1
            heapq.heappush(heap, (cost + w, tie, other, nodes + [other], rids + [r.id]))
    return None


def score_path(g: PropertyGraph, p: PathResult, decay: float = 0.8) -> float:
    """Path relevance for GraphRAG: product of edge confidences, damped per hop so shorter, better-evidenced paths rank first."""
    conf = 1.0
    for rid in p.rel_ids:
        conf *= float(g.rels[rid].props.get("confidence", 1.0)) * decay
    return round(conf, 4)


def k_paths(g: PropertyGraph, src: str, dst: str, max_hops: int, rel_types: list[str] | None = None, direction: str = "out",
            weight_prop: str | None = None, k: int = 3) -> list[PathResult]:
    """All simple paths up to max_hops, ranked by score_path then by cost."""
    found: list[PathResult] = []

    def dfs(cur: str, nodes: list[str], rids: list[int], cost: float) -> None:
        if cur == dst:
            found.append(PathResult(nodes=list(nodes), rel_ids=list(rids), cost=cost))
            return
        if len(rids) >= max_hops:
            return
        for r, other in g.neighbors(cur, rel_types, direction):
            if other in nodes:
                continue
            nodes.append(other)
            rids.append(r.id)
            dfs(other, nodes, rids, cost + (float(r.props.get(weight_prop, 1.0)) if weight_prop else 1.0))
            nodes.pop()
            rids.pop()

    dfs(src, [src], [], 0.0)
    for p in found:
        p.score = score_path(g, p)
    return sorted(found, key=lambda p: (-p.score, p.cost))[:k]


# ---- Syllabus extension: retry helper and Neo4j / AuraDB driver adapter -------------------------------
async def retry_async(fn: Callable[[], Awaitable[T]], retryable: tuple[type[BaseException], ...], attempts: int = 4,
                      base: float = 0.005) -> T:
    """Exponential backoff with full jitter around any I/O call: sleep is uniform(0, base * 2**attempt)."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except retryable:
            if i == attempts:
                raise
            await asyncio.sleep(random.uniform(0, base * 2 ** i))
    raise AssertionError("unreachable")


class Neo4jAdapter:
    """Async driver adapter for Neo4j / AuraDB. The offline engine above answers the lab; this class sends the same
    parameterized, guarded Cypher to a real server. The `neo4j` package is imported lazily and credentials come only
    from environment variables (NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, optional NEO4J_DATABASE)."""

    ENCRYPTED = ("neo4j+s://", "neo4j+ssc://", "bolt+s://", "bolt+ssc://")

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j") -> None:
        self.uri = self.validate_uri(uri)
        self._user, self._password, self._database = user, password, database
        self._driver: Any = None

    @classmethod
    def validate_uri(cls, uri: str) -> str:
        """AuraDB URIs look like neo4j+s://<instance-id>.databases.neo4j.io; plaintext schemes are allowed only on localhost."""
        local = uri.startswith(("neo4j://localhost", "bolt://localhost", "neo4j://127.0.0.1", "bolt://127.0.0.1"))
        if not (uri.startswith(cls.ENCRYPTED) or local):
            raise GraphError("use an encrypted scheme such as neo4j+s:// for any non-local database")
        return uri

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Neo4jAdapter":
        env = os.environ if env is None else env
        missing = [k for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD") if not env.get(k)]
        if missing:
            raise MissingCredentialsError("set environment variables: " + ", ".join(missing))
        return cls(env["NEO4J_URI"], env["NEO4J_USERNAME"], env["NEO4J_PASSWORD"], env.get("NEO4J_DATABASE", "neo4j"))

    @staticmethod
    def _import_driver() -> Any:
        try:
            import neo4j  # lazy: the offline lab never needs it
        except ImportError as exc:
            raise MissingDependencyError("the neo4j package is not installed: pip install neo4j") from exc
        return neo4j

    def _retryable(self) -> tuple[type[BaseException], ...]:
        mod = self._import_driver()
        return (GraphTimeout, mod.exceptions.ServiceUnavailable, mod.exceptions.SessionExpired, mod.exceptions.TransientError)

    def _connect(self) -> Any:
        if self._driver is None:
            self._driver = self._import_driver().AsyncGraphDatabase.driver(self.uri, auth=(self._user, self._password))
        return self._driver

    async def read(self, cypher: str, params: dict[str, object]) -> list[dict[str, object]]:
        guard_cypher(cypher)  # same static guard as the offline path; the read-only DB role remains the real control
        driver, retryable = self._connect(), self._retryable()

        async def once() -> list[dict[str, object]]:
            async def work(tx: Any) -> list[dict[str, object]]:
                result = await tx.run(cypher, params)
                return [rec.data() async for rec in result]

            async with driver.session(database=self._database) as session:
                return await session.execute_read(work)

        return await retry_async(once, retryable)

    async def write(self, stmts: list[CypherStmt]) -> int:
        """Runs the transformer's idempotent MERGE statements in one write transaction."""
        driver, retryable = self._connect(), self._retryable()

        async def once() -> int:
            async def work(tx: Any) -> int:
                for s in stmts:
                    await (await tx.run(s.text, s.params)).consume()
                return len(stmts)

            async with driver.session(database=self._database) as session:
                return await session.execute_write(work)

        return await retry_async(once, retryable)

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None


# ---- Syllabus extension: Amazon Neptune query builder (same traversal as openCypher and Gremlin) --------
class TraversalSpec(BaseModel):
    start_label: str
    key: str = "code"
    start_value: str
    rel_types: list[str] = Field(min_length=1)
    max_hops: int = Field(ge=1, le=MAX_VARLEN)
    direction: Literal["out", "in", "both"] = "out"
    limit: int = Field(default=25, ge=1, le=100)


def to_open_cypher(spec: TraversalSpec) -> tuple[str, dict[str, object]]:
    """Neptune speaks openCypher on the same bolt-style semantics; the start value travels as a parameter."""
    for ident in [spec.start_label, spec.key, *spec.rel_types]:
        _check_ident(ident)
    left, right = {"out": ("-", "->"), "in": ("<-", "-"), "both": ("-", "-")}[spec.direction]
    types = "|".join(spec.rel_types)
    text = (f"MATCH p = (s:{spec.start_label} {{{spec.key}: $start}}){left}[:{types}*1..{spec.max_hops}]{right}(e) "
            f"RETURN [x IN nodes(p) | x.{spec.key}] AS path LIMIT $limit")
    return text, {"start": spec.start_value, "limit": spec.limit}


def to_gremlin(spec: TraversalSpec) -> str:
    """Gremlin has no parameters in plain text submission, so values are escaped and identifiers whitelisted."""
    for ident in [spec.start_label, spec.key, *spec.rel_types]:
        _check_ident(ident)
    value = spec.start_value.replace("\\", "\\\\").replace("'", "\\'")
    labels = ", ".join("'" + t + "'" for t in spec.rel_types)
    step = {"out": "out", "in": "in", "both": "both"}[spec.direction]
    return (f"g.V().has('{spec.start_label}', '{spec.key}', '{value}')"
            f".repeat({step}({labels}).simplePath()).times({spec.max_hops}).emit().path().by('{spec.key}').limit({spec.limit})")


# ---- Syllabus extension: logistics network modeling ----------------------------------------------------
class ExposedShipment(BaseModel):
    ref: str
    value_usd: float
    status: Literal["rerouted", "stranded", "endpoint_down"]
    baseline_hours: float | None
    rerouted_hours: float | None
    delay_hours: float | None
    sla_breached: bool | None


def build_logistics_graph() -> PropertyGraph:
    """Hubs, directed ROUTE edges with transit_hours, and shipments linked to their planned hubs by ROUTED_VIA {seq}."""
    g = PropertyGraph()
    g.add_unique_constraint("Hub", "code")
    g.add_unique_constraint("Shipment", "ref")
    for code, city in (("RTM", "Rotterdam"), ("FRA", "Frankfurt"), ("WAW", "Warsaw"), ("MIL", "Milan"), ("MAD", "Madrid"), ("OSL", "Oslo")):
        g.add_node("Hub", f"Hub:{code}", {"code": code, "name": city, "region": "EU"})
    routes = (("RTM", "FRA", 6, "truck"), ("FRA", "WAW", 9, "truck"), ("RTM", "WAW", 20, "rail"), ("FRA", "MIL", 8, "truck"),
              ("MAD", "MIL", 14, "truck"), ("MAD", "FRA", 15, "truck"), ("MIL", "WAW", 16, "rail"), ("RTM", "MAD", 22, "truck"),
              ("OSL", "RTM", 30, "ferry"))
    for a, b, hours, mode in routes:
        for src, dst in ((a, b), (b, a)):  # lanes are usable both ways
            g.add_rel("ROUTE", f"Hub:{src}", f"Hub:{dst}", {"transit_hours": hours, "mode": mode, "confidence": 0.95, "source": "tms:route-table-2024q3"})
    shipments = (("S-100", "RTM", "WAW", 120000, 24, ["RTM", "FRA", "WAW"]), ("S-101", "RTM", "MIL", 80000, 20, ["RTM", "FRA", "MIL"]),
                 ("S-102", "MAD", "WAW", 45000, 30, ["MAD", "FRA", "WAW"]), ("S-103", "MAD", "MIL", 30000, 16, ["MAD", "MIL"]),
                 ("S-104", "OSL", "WAW", 200000, 60, ["OSL", "RTM", "FRA", "WAW"]))
    for ref, origin, dest, value, sla, itinerary in shipments:
        g.add_node("Shipment", f"Shipment:{ref}", {"ref": ref, "origin": origin, "dest": dest, "value_usd": value, "sla_hours": sla})
        for seq, hub in enumerate(itinerary, start=1):
            g.add_rel("ROUTED_VIA", f"Shipment:{ref}", f"Hub:{hub}", {"seq": seq, "source": "tms:shipment-plan"})
    return g


def fastest_route(g: PropertyGraph, origin: str, dest: str, closed: frozenset[str] = frozenset()) -> PathResult | None:
    """Dijkstra over ROUTE by transit_hours; closed hubs are removed from the search space."""
    return shortest_path(g, f"Hub:{origin}", f"Hub:{dest}", ["ROUTE"], frozenset(f"Hub:{c}" for c in closed), "transit_hours")


def reachable(g: PropertyGraph, start: str, blocked: frozenset[str]) -> set[str]:
    seen, queue = {start}, deque([start])
    while queue:
        for _, other in g.neighbors(queue.popleft(), ["ROUTE"], "out"):
            if other not in seen and other not in blocked:
                seen.add(other)
                queue.append(other)
    return seen


def exposure_if_hub_fails(g: PropertyGraph, code: str) -> tuple[list[ExposedShipment], list[str]]:
    """Blast radius: shipments planned through the hub (found by an incoming ROUTED_VIA traversal), what each costs to
    reroute, and which hubs become unreachable from the rest of the network."""
    hub = f"Hub:{code}"
    exposed: list[ExposedShipment] = []
    for _, ship_id in g.neighbors(hub, ["ROUTED_VIA"], "in"):
        s = g.nodes[ship_id].props
        origin, dest = str(s["origin"]), str(s["dest"])
        base = fastest_route(g, origin, dest)
        alt = None if code in (origin, dest) else fastest_route(g, origin, dest, frozenset({code}))
        status: Literal["rerouted", "stranded", "endpoint_down"] = "endpoint_down" if code in (origin, dest) else ("rerouted" if alt else "stranded")
        delay = None if alt is None or base is None else alt.cost - base.cost
        exposed.append(ExposedShipment(ref=str(s["ref"]), value_usd=float(s["value_usd"]), status=status,
                                       baseline_hours=base.cost if base else None, rerouted_hours=alt.cost if alt else None, delay_hours=delay,
                                       sla_breached=None if alt is None else alt.cost > float(s["sla_hours"])))
    survivors = [h for h in sorted(g.by_label["Hub"]) if h != hub]
    reach = reachable(g, survivors[0], frozenset({hub})) if survivors else set()
    return sorted(exposed, key=lambda e: -e.value_usd), sorted(h for h in survivors if h not in reach)


# ---- Syllabus extension: modeling user interactions (events as weighted, timestamped edges) -------------
EVENT_WEIGHTS = {"VIEWED": 1.0, "ADDED_TO_CART": 3.0, "PURCHASED": 5.0}
DAY = 86400


class Recommendation(BaseModel):
    item: str
    score: float
    because: list[str]


def build_interaction_graph(now: int) -> PropertyGraph:
    g = PropertyGraph()
    g.add_unique_constraint("User", "uid")
    g.add_unique_constraint("Item", "name")
    for uid in ("u1", "u2", "u3", "u4", "u5"):
        g.add_node("User", f"User:{uid}", {"uid": uid})
    for item in ("laptop", "dock", "monitor", "mouse", "keyboard", "webcam"):
        g.add_node("Item", f"Item:{item}", {"name": item})
    events = (("u1", "PURCHASED", "laptop", 30), ("u1", "PURCHASED", "dock", 28), ("u1", "VIEWED", "monitor", 2),
              ("u2", "PURCHASED", "laptop", 10), ("u2", "PURCHASED", "dock", 9), ("u2", "PURCHASED", "monitor", 8), ("u2", "VIEWED", "webcam", 1),
              ("u3", "PURCHASED", "laptop", 400), ("u3", "PURCHASED", "keyboard", 399),
              ("u4", "VIEWED", "mouse", 1), ("u4", "ADDED_TO_CART", "keyboard", 1),
              ("u5", "PURCHASED", "dock", 5), ("u5", "PURCHASED", "monitor", 4), ("u5", "ADDED_TO_CART", "webcam", 3))
    for uid, etype, item, days_ago in events:
        g.add_rel(etype, f"User:{uid}", f"Item:{item}", {"ts": now - days_ago * DAY})  # one edge per event keeps the history
    return g


def user_vector(g: PropertyGraph, user: str, now: int, half_life_days: float = 30.0) -> dict[str, float]:
    """Edge weight = event weight x 0.5 ** (age / half-life): an event half-life old counts half as much."""
    vec: dict[str, float] = defaultdict(float)
    for r, item in g.neighbors(user, list(EVENT_WEIGHTS), "out"):
        age_days = max(0.0, (now - float(r.props["ts"])) / DAY)
        vec[item] += EVENT_WEIGHTS[r.type] * 0.5 ** (age_days / half_life_days)
    return dict(vec)


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot_ab = sum(v * b[k] for k, v in a.items() if k in b)
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot_ab / (na * nb) if na and nb else 0.0


def recommend(g: PropertyGraph, user: str, now: int, half_life_days: float = 30.0, k: int = 3) -> list[Recommendation]:
    """'Users who interacted with what you did also interacted with ...': user -> items -> other users -> their items,
    weighted by similarity of the two users' decayed interaction vectors."""
    mine = user_vector(g, user, now, half_life_days)
    peers = {other for item in mine for _, other in g.neighbors(item, list(EVENT_WEIGHTS), "in") if other != user}
    scores: dict[str, float] = defaultdict(float)
    why: dict[str, list[str]] = defaultdict(list)
    for peer in sorted(peers):
        theirs = user_vector(g, peer, now, half_life_days)
        sim = cosine(mine, theirs)
        for item, w in theirs.items():
            if item not in mine:
                scores[item] += sim * w
                why[item].append(g.nodes[peer].props["uid"])  # type: ignore[arg-type]
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
    return [Recommendation(item=str(g.nodes[i].props["name"]), score=round(s, 3), because=why[i]) for i, s in ranked]


def co_occurrence(g: PropertyGraph, item: str) -> Counter[str]:
    """Plain 'users who bought X also bought Y' counts (distinct users, no decay)."""
    users = {u for _, u in g.neighbors(item, ["PURCHASED"], "in")}
    c: Counter[str] = Counter()
    for u in users:
        for other in {i for _, i in g.neighbors(u, ["PURCHASED"], "out") if i != item}:
            c[str(g.nodes[other].props["name"])] += 1
    return c


# ---- Syllabus extension: graph-based retrieval logic for an AI chatbot ---------------------------------
class CitedFact(BaseModel):
    ref: int
    text: str
    source: str
    hop: int


class GraphContext(BaseModel):
    question: str
    seeds: list[str]
    facts: list[CitedFact]
    truncated: bool
    prompt: str


def _plain(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


class GraphRetriever:
    """Link entities in the question, expand a bounded subgraph, and emit numbered, sourced facts for the LLM.
    Bounds: max_hops, max_facts and degree_cap (a node above the cap is shown but never expanded: supernode guard)."""

    def __init__(self, g: PropertyGraph, name_props: tuple[str, ...] = ("code", "name", "ref", "uid"), max_hops: int = 2,
                 max_facts: int = 12, degree_cap: int = 25) -> None:
        self.g, self.max_hops, self.max_facts, self.degree_cap = g, max_hops, max_facts, degree_cap
        self.alias: dict[str, str] = {}
        for nid, node in g.nodes.items():
            for p in name_props:
                v = node.props.get(p)
                if v is not None and len(_plain(str(v))) >= 3:
                    self.alias.setdefault(_plain(str(v)), nid)

    def link_entities(self, question: str) -> list[str]:
        text, seeds = _plain(question), []
        for key in sorted(self.alias, key=len, reverse=True):  # longest alias first so 'new york city' beats 'new york'
            m = re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", text)
            if m:
                seeds.append(self.alias[key])
                text = text[:m.start()] + "#" * (m.end() - m.start()) + text[m.end():]  # mask so aliases cannot overlap
        return list(dict.fromkeys(seeds))

    def display(self, nid: str) -> str:
        n = self.g.nodes[nid]
        return f"{n.label} {n.props.get('code') or n.props.get('ref') or n.props.get('name') or n.id}"

    def expand(self, seeds: list[str]) -> tuple[list[tuple[GRel, int]], bool]:
        seen_nodes, seen_rels = set(seeds), set()
        found: list[tuple[GRel, int]] = []
        frontier = list(seeds)
        for hop in range(1, self.max_hops + 1):
            nxt: list[str] = []
            for nid in frontier:
                if self.g.degree(nid) > self.degree_cap:
                    continue
                for r, other in self.g.neighbors(nid, None, "both"):
                    if r.id not in seen_rels:
                        seen_rels.add(r.id)
                        found.append((r, hop))
                    if other not in seen_nodes:
                        seen_nodes.add(other)
                        nxt.append(other)
            frontier = nxt
        found.sort(key=lambda x: (x[1], x[0].id))
        return found[:self.max_facts], len(found) > self.max_facts

    def build(self, question: str) -> GraphContext:
        seeds = self.link_entities(question)
        found, truncated = self.expand(seeds)
        facts: list[CitedFact] = []
        for i, (r, hop) in enumerate(found, start=1):
            extras = ", ".join(f"{k}={v}" for k, v in r.props.items() if k not in ("source", "confidence"))
            facts.append(CitedFact(ref=i, hop=hop, source=str(r.props.get("source", "graph")),
                                   text=f"{self.display(r.src)} -[{r.type}{' ' + extras if extras else ''}]-> {self.display(r.dst)}"))
        lines = [f"[{f.ref}] {f.text} (source: {f.source})" for f in facts]
        prompt = ("Answer using only the numbered facts below and cite them like [1]. If they are insufficient, say you do not know.\n"
                  + "\n".join(lines) + f"\nQuestion: {question}")
        return GraphContext(question=question, seeds=seeds, facts=facts, truncated=truncated, prompt=prompt)


def unresolvable_citations(answer: str, ctx: GraphContext) -> list[int]:
    """Citation check: every [n] in the answer must point at a fact that was actually in the context."""
    valid = {f.ref for f in ctx.facts}
    return sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)} - valid)


# ---- Syllabus extension: demo ----------------------------------------------------------------------------
class FlakyReader:
    """Simulated remote graph read that times out twice, to exercise retry_async."""

    def __init__(self) -> None:
        self.calls = 0

    async def read(self) -> str:
        self.calls += 1
        await asyncio.sleep(0.001)
        if self.calls <= 2:
            raise GraphTimeout("transaction timed out")
        return "ok"


async def extension_demo() -> None:
    # 1. nodes, relationships, properties, constraints
    pg = build_demo_property_graph()
    for label, props in (("duplicate email", {"name": "Bob", "email": "ada@example.com"}), ("missing required", {"name": "NoEmail"})):
        try:
            pg.add_node("Person", "px", props)
        except ConstraintViolation as exc:
            log.info("constraint blocked write", extra={"ctx": {"case": label, "reason": str(exc)}})
    log.info("property graph", extra={"ctx": {"nodes": len(pg.nodes), "rels": len(pg.rels), "rel_props": pg.rels[1].props}})

    # 2. taxonomy (single-parent tree) vs ontology (typed classes, domain/range, inference)
    tax = Taxonomy()
    for term, parent in (("Product", None), ("Electronics", "Product"), ("Phones", "Electronics"), ("Smartphone", "Phones")):
        tax.add(term, parent)
    try:
        tax.add("Smartphone", "Electronics")
    except OntologyViolation as exc:
        log.info("taxonomy rejects second parent", extra={"ctx": {"reason": str(exc)}})
    log.info("taxonomy", extra={"ctx": {"ancestors": tax.ancestors("Smartphone"), "descendants": tax.descendants("Electronics")}})
    onto = build_logistics_ontology()
    inferred = sorted(onto.infer())
    try:
        onto.assert_fact("Hesse", "OWNS", "FRA")
    except OntologyViolation as exc:
        log.info("ontology rejects bad instance data", extra={"ctx": {"reason": str(exc)}})
    onto.facts.add(("Hesse", "OWNS", "FRA"))  # bypass assert_fact to show the audit finding the same problem in loaded data
    log.info("ontology", extra={"ctx": {"inferred": inferred, "multiple_inheritance": "TemperatureControlled" in onto.superclasses("ColdChainHub"),
                                        "audit": onto.validate()}})

    # 3. relational tables -> graph, idempotent
    conn = make_sales_db()
    stmts = relational_to_cypher(conn, {"orders.customer_id": "PLACED_BY", "order_items": "CONTAINS"})
    sales = PropertyGraph()
    first = load_cypher(sales, stmts)
    counts = (len(sales.nodes), len(sales.rels))
    load_cypher(sales, stmts)
    log.info("relational to graph", extra={"ctx": {"statements": len(stmts), "loaded": first, "idempotent": counts == (len(sales.nodes), len(sales.rels)),
                                                   "sample": stmts[len(stmts) // 2].text, "rel_stats": {"|".join(k): v for k, v in sales.rel_stats.items()}}})

    # 4. pattern matching and path traversal
    logistics = build_logistics_graph()
    engine = PatternEngine(logistics)
    q1 = engine.run("MATCH p = (a:Hub {code: 'RTM'})-[:ROUTE*1..2]->(b:Hub) WHERE b.code <> 'RTM' RETURN b.code AS hub, length(p) AS hops", anchor="left")
    q2 = engine.run("MATCH (s:Shipment)-[v:ROUTED_VIA]->(h:Hub {code: $hub}) WHERE s.value_usd >= 100000 RETURN s.ref, v.seq", {"hub": "FRA"})
    log.info("pattern match", extra={"ctx": {"hubs_within_2_hops": sorted({(r["hub"], r["hops"]) for r in q1.rows}), "high_value_via_FRA": q2.rows}})
    for bad in ("MATCH (a)-[:ROUTE*]->(b) RETURN b", "MATCH (a)<-[:ROUTE]->(b) RETURN b", "MATCH (a) RETURN z"):
        try:
            engine.run(bad)
        except PatternSyntaxError as exc:
            log.info("pattern rejected", extra={"ctx": {"query": bad, "reason": str(exc)}})
    sp = shortest_path(logistics, "Hub:OSL", "Hub:MIL", ["ROUTE"], weight_prop="transit_hours")
    ranked = k_paths(logistics, "Hub:RTM", "Hub:MIL", 3, ["ROUTE"], weight_prop="transit_hours", k=3)
    log.info("path queries", extra={"ctx": {"fastest_OSL_to_MIL": [sp.nodes, sp.cost] if sp else None,
                                            "top_RTM_to_MIL": [[p.nodes, p.cost, p.score] for p in ranked]}})

    # 5. join optimization: anchor on the most selective end, index lookups, cost estimate vs rows touched
    big = PropertyGraph()
    load_cypher(big, relational_to_cypher(make_sales_db(300, 1500, 50), {"orders.customer_id": "PLACED_BY", "order_items": "CONTAINS"}))
    big_engine = PatternEngine(big)
    join_q = "MATCH (c:Customer)<-[:PLACED_BY]-(o:Order)-[:CONTAINS]->(p:Product {sku: $sku}) RETURN c.id AS cust, o.id AS ord"
    runs = {a: big_engine.run(join_q, {"sku": "SKU-007"}, anchor=a) for a in ("left", "right", "auto")}
    same = len({json.dumps(sorted((r["cust"], r["ord"]) for r in res.rows)) for res in runs.values()}) == 1
    big.create_index("Product", "sku")
    indexed = big_engine.run(join_q, {"sku": "SKU-007"}, anchor="auto")
    log.info("join order", extra={"ctx": {"same_rows": same, "result_rows": len(runs["auto"].rows),
                                          "touched": {a: r.touched for a, r in runs.items()}, "estimates": runs["auto"].plan.alternatives,
                                          "auto_anchor": runs["auto"].plan.anchor, "touched_with_index": indexed.touched,
                                          "reduction_x": round(runs["left"].touched / max(1, indexed.touched), 1)}})

    # 6. logistics: fastest route avoiding a closed hub, blast radius of a hub failure
    base = fastest_route(logistics, "RTM", "WAW")
    detour = fastest_route(logistics, "RTM", "WAW", frozenset({"FRA"}))
    exposed, isolated = exposure_if_hub_fails(logistics, "FRA")
    log.info("logistics", extra={"ctx": {"RTM_to_WAW": [base.nodes, base.cost] if base else None, "avoiding_FRA": [detour.nodes, detour.cost] if detour else None,
                                         "FRA_exposed": [e.model_dump() for e in exposed], "FRA_isolated_hubs": isolated}})
    rtm_exposed, rtm_isolated = exposure_if_hub_fails(logistics, "RTM")
    log.info("logistics hub RTM fails", extra={"ctx": {"statuses": {e.ref: e.status for e in rtm_exposed}, "isolated_hubs": rtm_isolated}})

    # 7. user interactions with recency decay
    now = 1_730_000_000
    inter = build_interaction_graph(now)
    log.info("interactions", extra={"ctx": {"recommend_u1": [r.model_dump() for r in recommend(inter, "User:u1", now)],
                                            "no_decay_flat_counts": dict(co_occurrence(inter, "Item:laptop")),
                                            "u3_weight_now": round(sum(user_vector(inter, "User:u3", now).values()), 6)}})

    # 8. chatbot graph retrieval with citations
    retriever = GraphRetriever(logistics, max_facts=10)
    ctx = retriever.build("Which shipments are affected if Frankfurt closes, and is S-100 at risk?")
    answer = "S-100 is planned through Frankfurt [1] and the lane is sourced from the route table [2]. Unsupported claim [99]."
    log.info("graph context", extra={"ctx": {"seeds": ctx.seeds, "facts": len(ctx.facts), "truncated": ctx.truncated, "first_fact": ctx.facts[0].text,
                                             "unresolvable_citations": unresolvable_citations(answer, ctx)}})

    # 9. Neo4j / AuraDB adapter (credentials from env only) and Neptune query text
    for uri in ("neo4j+s://abcd1234.databases.neo4j.io", "bolt://prod.example.com:7687"):
        try:
            log.info("uri accepted", extra={"ctx": {"uri": Neo4jAdapter.validate_uri(uri)}})
        except GraphError as exc:
            log.info("uri rejected", extra={"ctx": {"uri": uri, "reason": str(exc)}})
    try:
        Neo4jAdapter.from_env({})
    except MissingCredentialsError as exc:
        log.info("adapter needs env credentials", extra={"ctx": {"reason": str(exc)}})
    try:
        Neo4jAdapter._import_driver()
        log.info("neo4j package available", extra={"ctx": {}})
    except MissingDependencyError as exc:
        log.info("neo4j package missing (adapter unused offline)", extra={"ctx": {"hint": str(exc)}})
    spec = TraversalSpec(start_label="Hub", start_value="FRA", rel_types=["ROUTE"], max_hops=3)
    cy, cy_params = to_open_cypher(spec)
    log.info("neptune queries", extra={"ctx": {"openCypher": cy, "params": cy_params, "gremlin": to_gremlin(spec)}})
    flaky = FlakyReader()
    log.info("retry with backoff", extra={"ctx": {"result": await retry_async(flaky.read, (GraphTimeout,)), "attempts": flaky.calls}})


async def main() -> None:
    store = InMemoryGraph(fail_first_reads=2)
    stats = await ingest("(document text)", store, tenant="t1")
    log.info("ingested", extra={"ctx": {**stats, "nodes": len(store.nodes)}})

    planner = CypherPlanner()
    cypher, params = planner.plan("t1|Company:acme", ["OWNS", "SUPPLIES", "SANCTIONED_BY"], depth=5, limit=500, tenant="t1")
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

    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
