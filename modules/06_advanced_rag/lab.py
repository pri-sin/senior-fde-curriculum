"""Module 06 lab: hybrid retrieval pipeline in pure Python.

Components: recursive chunker with overlap, BM25 index, deterministic hashed-n-gram "embedding" with cosine
search (stand-in for a real embedding model + HNSW), RRF fusion, and a pluggable reranker protocol with a
lexical cross-encoder stand-in. Dense and sparse retrievers run concurrently with timeouts and backoff retries.
Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import sys
from collections import Counter, defaultdict
from typing import Protocol

from pydantic import BaseModel, Field


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


log = get_logger("lab06")


class RetrievalError(Exception):
    pass


class RetrieverTimeout(RetrievalError):
    """Retryable."""


class IndexEmptyError(RetrievalError):
    pass


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    position: int = Field(ge=0)


class Hit(BaseModel):
    chunk_id: str
    score: float
    source: str


class Result(BaseModel):
    chunk: Chunk
    fused_score: float
    rerank_score: float | None = None
    sources: list[str]


def recursive_chunk(doc_id: str, text: str, max_chars: int = 220, overlap: float = 0.15) -> list[Chunk]:
    """Split on paragraph, then sentence, then word boundaries; add trailing overlap between chunks."""
    def split(t: str, seps: list[str]) -> list[str]:
        if len(t) <= max_chars or not seps:
            return [t]
        parts = [p for p in re.split(seps[0], t) if p.strip()]
        if len(parts) == 1:
            return split(t, seps[1:])
        out: list[str] = []
        for p in parts:
            out.extend(split(p, seps[1:]) if len(p) > max_chars else [p])
        return out

    pieces = split(text, [r"\n\s*\n", r"(?<=[.!?])\s+", r"\s+"])
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) + 1 > max_chars:
            chunks.append(cur)
            tail = cur[-int(max_chars * overlap):]
            cur = (tail.split(" ", 1)[-1] + " " + p).strip() if overlap else p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return [Chunk(chunk_id=f"{doc_id}#{i}", doc_id=doc_id, text=c, position=i) for i, c in enumerate(chunks)]


TOKEN = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text)]  # underscores/digits kept so ERR_4471 stays one token


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        if not chunks:
            raise IndexEmptyError("cannot build BM25 over zero chunks")
        self.k1, self.b = k1, b
        self.docs = {c.chunk_id: Counter(tokenize(c.text)) for c in chunks}
        self.len = {cid: sum(tf.values()) for cid, tf in self.docs.items()}
        self.avg = sum(self.len.values()) / len(self.len)
        df: Counter[str] = Counter()
        for tf in self.docs.values():
            df.update(tf.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, k: int) -> list[Hit]:
        q = tokenize(query)
        scored = []
        for cid, tf in self.docs.items():
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[cid] / self.avg))
            if s > 0:
                scored.append(Hit(chunk_id=cid, score=s, source="bm25"))
        return sorted(scored, key=lambda h: -h.score)[:k]


DIM = 256


def embed(text: str) -> list[float]:
    """Deterministic hashed char-trigram + word embedding. Placeholder for a real model; same interface."""
    v = [0.0] * DIM
    toks = tokenize(text)
    feats = toks + [w[i:i + 3] for w in toks for i in range(max(1, len(w) - 2))]
    for f in feats:
        h = int(hashlib.md5(f.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 8) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class VectorIndex:
    def __init__(self, chunks: list[Chunk]) -> None:
        if not chunks:
            raise IndexEmptyError("cannot build vector index over zero chunks")
        self.vecs = {c.chunk_id: embed(c.text) for c in chunks}

    def search(self, query: str, k: int) -> list[Hit]:
        q = embed(query)
        hits = [Hit(chunk_id=cid, score=sum(a * b for a, b in zip(q, v)), source="dense") for cid, v in self.vecs.items()]
        return sorted(hits, key=lambda h: -h.score)[:k]


def rrf(rankings: list[list[Hit]], k: int = 60, top: int = 50) -> list[tuple[str, float, list[str]]]:
    """score(d) = sum over rankers of 1/(k + rank). Rank-based, so no score calibration is needed."""
    score: dict[str, float] = defaultdict(float)
    srcs: dict[str, list[str]] = defaultdict(list)
    for ranking in rankings:
        for rank, h in enumerate(ranking, 1):
            score[h.chunk_id] += 1.0 / (k + rank)
            srcs[h.chunk_id].append(h.source)
    return [(cid, s, srcs[cid]) for cid, s in sorted(score.items(), key=lambda x: -x[1])[:top]]


class Reranker(Protocol):
    async def score(self, query: str, passages: list[str]) -> list[float]: ...


class LexicalCrossEncoder:
    """Stand-in for a cross-encoder: scores query-passage *pairs* jointly (coverage + proximity bonus)."""

    async def score(self, query: str, passages: list[str]) -> list[float]:
        await asyncio.sleep(0.002)
        q = set(tokenize(query))
        out = []
        for p in passages:
            toks = tokenize(p)
            cover = len(q & set(toks)) / (len(q) or 1)
            pos = [i for i, t in enumerate(toks) if t in q]
            span = (max(pos) - min(pos) + 1) if pos else len(toks) + 1
            out.append(cover + len(pos) / span * 0.5)
        return out


async def with_timeout_retry(fn, *, timeout: float, attempts: int = 3):  # type: ignore[no-untyped-def]
    for i in range(1, attempts + 1):
        try:
            return await asyncio.wait_for(fn(), timeout)
        except (asyncio.TimeoutError, RetrieverTimeout):
            if i == attempts:
                raise RetrieverTimeout(f"retriever failed after {attempts} attempts") from None
            await asyncio.sleep(random.uniform(0, 0.01 * 2 ** i))


class HybridRetriever:
    def __init__(self, chunks: list[Chunk], reranker: Reranker, flaky_dense: bool = False) -> None:
        self.chunks = {c.chunk_id: c for c in chunks}
        self.bm25, self.vec, self.reranker = BM25Index(chunks), VectorIndex(chunks), reranker
        self._flaky, self._dense_calls = flaky_dense, 0

    async def _dense(self, q: str, k: int) -> list[Hit]:
        self._dense_calls += 1
        if self._flaky and self._dense_calls == 1:
            raise RetrieverTimeout("ANN node timeout")
        await asyncio.sleep(0.001)
        return self.vec.search(q, k)

    async def _sparse(self, q: str, k: int) -> list[Hit]:
        await asyncio.sleep(0.001)
        return self.bm25.search(q, k)

    async def retrieve(self, query: str, k: int = 3, candidates: int = 20) -> list[Result]:
        dense, sparse = await asyncio.gather(
            with_timeout_retry(lambda: self._dense(query, candidates), timeout=0.2),
            with_timeout_retry(lambda: self._sparse(query, candidates), timeout=0.2))
        fused = rrf([dense, sparse])
        scores = await self.reranker.score(query, [self.chunks[cid].text for cid, _, _ in fused])
        results = [Result(chunk=self.chunks[cid], fused_score=fs, rerank_score=rs, sources=src)
                   for (cid, fs, src), rs in zip(fused, scores)]
        return sorted(results, key=lambda r: -(r.rerank_score or 0))[:k]


CORPUS = {
    "kb-tls": "Troubleshooting TLS handshakes. If the client reports ERR_4471_TLS, the server certificate chain is "
              "missing an intermediate. Reinstall the full chain and restart the gateway.\n\nGeneral TLS tips: keep "
              "certificates rotated every ninety days and monitor expiry dates.",
    "policy-refund": "Refund policy. Customers may request a refund within 30 days of purchase. Enterprise contracts "
                     "are excluded and follow the terms of the master services agreement instead.",
    "policy-leave": "Employee leave policy. Staff receive twenty five days of paid vacation per year. Unused days "
                    "carry over for one quarter. Parental leave is covered in a separate document.",
    "kb-sso": "Single sign on setup. Configure the identity provider with the SAML metadata URL and map the email "
              "claim. Users who cannot log in should check clock skew between the provider and the service.",
}


async def main() -> None:
    chunks = [c for doc_id, t in CORPUS.items() for c in recursive_chunk(doc_id, t)]
    log.info("indexed", extra={"ctx": {"docs": len(CORPUS), "chunks": len(chunks)}})
    retriever = HybridRetriever(chunks, LexicalCrossEncoder(), flaky_dense=True)
    for q in ("ERR_4471_TLS", "can I get my money back", "why can't users log in with SAML"):
        res = await retriever.retrieve(q, k=2)
        log.info("query", extra={"ctx": {"q": q, "top": [{"id": r.chunk.chunk_id, "fused": round(r.fused_score, 4),
                                                           "rerank": round(r.rerank_score or 0, 3), "via": r.sources} for r in res]}})
    try:
        BM25Index([])
    except IndexEmptyError as exc:
        log.info("empty index guarded", extra={"ctx": {"error": str(exc)}})


if __name__ == "__main__":
    asyncio.run(main())
