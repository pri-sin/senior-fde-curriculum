"""Module 06 lab: hybrid retrieval pipeline in pure Python.

Components: recursive chunker with overlap, BM25 index, deterministic hashed-n-gram "embedding" with cosine
search (stand-in for a real embedding model + HNSW), RRF fusion, and a pluggable reranker protocol with a
lexical cross-encoder stand-in. Dense and sparse retrievers run concurrently with timeouts and backoff retries.
Syllabus extension sections (chunking, vectors, indexes, vector DB adapters, metrics, rerank API) run from extension_demo().
Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import math
import os
import random
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeVar, Union

from pydantic import BaseModel, Field, ValidationError


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


# ---- Syllabus extension: shared helpers (retry with exponential backoff) ---
T = TypeVar("T")


async def retry_with_backoff(fn: Callable[[], Awaitable[T]], *, attempts: int = 4, base_delay: float = 0.01,
                             retry_on: tuple[type[BaseException], ...] = (RetrieverTimeout,)) -> T:
    """Full-jitter exponential backoff around any awaitable I/O call. Only errors in retry_on are retried."""
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:
            if i == attempts:
                raise
            delay = random.uniform(0, base_delay * 2 ** i)
            log.warning("retrying", extra={"ctx": {"attempt": i, "error": type(exc).__name__, "sleep_s": round(delay, 4)}})
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class VectorDBError(RetrievalError):
    pass


class VectorDBTransient(VectorDBError):
    """Connection reset, timeout, 429/5xx: safe to retry."""


class VectorDBDependencyError(VectorDBError):
    def __init__(self, package: str) -> None:
        super().__init__(f"optional dependency '{package}' is not installed; run: pip install {package}")
        self.package = package


class RerankError(RetrievalError):
    pass


class RerankRetryable(RerankError):
    """429, 5xx or network failure from the rerank API."""


class RerankDependencyError(RerankError):
    def __init__(self, package: str) -> None:
        super().__init__(f"optional dependency '{package}' is not installed; run: pip install {package}")


# ---- Syllabus extension: fixed-size vs semantic chunking and overlap optimisation ---
def fixed_size_chunk(doc_id: str, text: str, size_words: int, overlap_words: int = 0) -> list[Chunk]:
    """Fixed-size windows over words. Predictable and cheap, but blind to meaning: it will cut sentences in half."""
    if size_words <= 0 or not 0 <= overlap_words < size_words:
        raise ValueError("need size_words > 0 and 0 <= overlap_words < size_words")
    words = text.split()
    step = size_words - overlap_words
    out = []
    for i, start in enumerate(range(0, max(1, len(words) - overlap_words), step)):
        out.append(Chunk(chunk_id=f"{doc_id}#{i}", doc_id=doc_id, text=" ".join(words[start:start + size_words]), position=i))
    return out


def cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def semantic_chunk(doc_id: str, text: str, *, percentile: float = 30.0, min_sentences: int = 2) -> tuple[list[Chunk], list[float]]:
    """Semantic chunking: embed each sentence, then cut where similarity to the NEXT sentence falls into the lowest
    `percentile` of all adjacent similarities (a topic shift). Returns chunks and the adjacent similarities.
    Costs one embedding per sentence at ingest; with a real embedding model this tracks meaning, with the hashed
    embedder used here it tracks shared vocabulary, which is enough to show the mechanism."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    if len(sentences) < 2:
        return [Chunk(chunk_id=f"{doc_id}#0", doc_id=doc_id, text=text.strip(), position=0)], []
    vecs = [embed(s) for s in sentences]
    sims = [cosine(vecs[i], vecs[i + 1]) for i in range(len(vecs) - 1)]
    ranked = sorted(sims)
    cut_at = ranked[min(len(ranked) - 1, int(len(ranked) * percentile / 100))]
    groups: list[list[str]] = [[sentences[0]]]
    for i, sim in enumerate(sims):
        if sim < cut_at and len(groups[-1]) >= min_sentences:
            groups.append([])
        groups[-1].append(sentences[i + 1])
    return [Chunk(chunk_id=f"{doc_id}#{i}", doc_id=doc_id, text=" ".join(g), position=i) for i, g in enumerate(groups)], sims


MIXED_DOC = ("Troubleshooting TLS handshakes. If the client reports ERR_4471_TLS the certificate chain is missing an intermediate. "
             "Reinstall the full certificate chain and restart the gateway. Rotate every certificate before expiry. "
             "Refund policy. Customers may request a refund within thirty days of purchase. "
             "Enterprise contracts are excluded from the refund window. Refund requests go to the billing team.")

# (query, doc_id, gold phrase that must sit inside ONE chunk for the answer to be retrievable)
LABELED_QA = [
    ("how long do refunds take to request", "policy-refund", "refund within 30 days of purchase"),
    ("which contracts are excluded from refunds", "policy-refund", "enterprise contracts are excluded"),
    ("what does ERR_4471_TLS mean", "kb-tls", "missing an intermediate"),
    ("how do I fix the certificate chain", "kb-tls", "reinstall the full chain and restart the gateway"),
    ("how many vacation days per year", "policy-leave", "twenty five days of paid vacation"),
    ("do unused vacation days carry over", "policy-leave", "unused days carry over for one quarter"),
    ("login fails with SAML", "kb-sso", "check clock skew between the provider and the service"),
]


def overlap_sweep(corpus: dict[str, str], size_words: int, overlaps: list[int]) -> list[dict[str, float]]:
    """Measure what overlap buys. coverage = share of gold phrases fully inside some chunk (boundary-split protection);
    recall@1 = BM25 top chunk contains the gold phrase; bloat = chunk words / source words (index size and cost)."""
    rows = []
    source_words = sum(len(t.split()) for t in corpus.values())
    for ov in overlaps:
        chunks = [c for d, t in corpus.items() for c in fixed_size_chunk(d, t, size_words, ov)]
        index = BM25Index(chunks)
        by_id = {c.chunk_id: c for c in chunks}
        norm = lambda s: " ".join(tokenize(s))  # noqa: E731
        covered = hit1 = 0
        for query, doc_id, gold in LABELED_QA:
            if any(c.doc_id == doc_id and norm(gold) in norm(c.text) for c in chunks):
                covered += 1
            top = index.search(query, 1)
            if top and norm(gold) in norm(by_id[top[0].chunk_id].text):
                hit1 += 1
        rows.append({"overlap_words": ov, "chunks": len(chunks), "coverage": round(covered / len(LABELED_QA), 3),
                     "recall_at_1": round(hit1 / len(LABELED_QA), 3),
                     "bloat": round(sum(len(c.text.split()) for c in chunks) / source_words, 2)})
    return rows


# ---- Syllabus extension: vector representations, dimensionality trade-offs, quantisation ---
CONCEPTS = {"car": "VEHICLE", "automobile": "VEHICLE", "vehicle": "VEHICLE", "money": "CASH", "refund": "CASH", "cash": "CASH",
            "back": "RETURN", "return": "RETURN", "vacation": "LEAVE", "holiday": "LEAVE", "leave": "LEAVE"}


def toy_semantic_embed(text: str, dim: int = 64) -> list[float]:
    """Hand-made lexicon (synonyms share a concept id) hashed into `dim` dims. A real model LEARNS such structure from
    data; this toy only shows why dense vectors match 'car' with 'automobile' while sparse term vectors cannot."""
    v = [0.0] * dim
    for t in tokenize(text):
        h = _term_hash(CONCEPTS.get(t, t))
        v[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def sparse_vector(text: str) -> dict[str, float]:
    """Sparse representation: one dimension per vocabulary term, almost all zero, stored as a dict of non-zeros."""
    return {t: float(c) for t, c in Counter(tokenize(text)).items()}


def sparse_cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(w * b.get(t, 0.0) for t, w in a.items())
    na, nb = math.sqrt(sum(w * w for w in a.values())), math.sqrt(sum(w * w for w in b.values()))
    return dot / (na * nb) if na and nb else 0.0


_HASH_CACHE: dict[str, int] = {}


def _term_hash(term: str) -> int:
    if term not in _HASH_CACHE:
        _HASH_CACHE[term] = int(hashlib.md5(term.encode()).hexdigest(), 16)
    return _HASH_CACHE[term]


def hash_embed_tokens(tokens: list[str], dim: int) -> list[float]:
    v = [0.0] * dim
    for t in tokens:
        h = _term_hash(t)
        v[h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def synthetic_corpus(n_docs: int = 120, vocab: int = 600, topics: int = 12, words_per_doc: int = 40, seed: int = 7) -> list[list[str]]:
    rng = random.Random(seed)
    per_topic = vocab // topics
    docs = []
    for i in range(n_docs):
        base = (i % topics) * per_topic
        docs.append([f"w{base + rng.randrange(per_topic)}" if rng.random() < 0.8 else f"w{rng.randrange(vocab)}" for _ in range(words_per_doc)])
    return docs


def top_ids(scores: list[float], k: int) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:k]


def dimension_sweep(dims: list[int], k: int = 5, n_queries: int = 15, seed: int = 7) -> list[dict[str, float]]:
    """Hashing words into `dim` buckets: fewer dims = less memory but more collisions = lower recall against the exact
    sparse ranking (ground truth). This is the same trade-off as picking a 256 vs 1536-dim embedding model."""
    docs = synthetic_corpus(seed=seed)
    rng = random.Random(seed + 1)
    queries = [rng.sample(docs[i], 8) for i in rng.sample(range(len(docs)), n_queries)]
    sparse_docs = [sparse_vector(" ".join(d)) for d in docs]
    truth = [top_ids([sparse_cosine(sparse_vector(" ".join(q)), d) for d in sparse_docs], k) for q in queries]
    vocab_terms = sorted({t for d in docs for t in d})
    rows = []
    for dim in dims:
        dvecs = [hash_embed_tokens(d, dim) for d in docs]
        recall = 0.0
        for q, gt in zip(queries, truth):
            qv = hash_embed_tokens(q, dim)
            got = top_ids([sum(a * b for a, b in zip(qv, dv)) for dv in dvecs], k)
            recall += len(set(got) & set(gt)) / k
        buckets = Counter(_term_hash(t) % dim for t in vocab_terms)
        shared = sum(1 for t in vocab_terms if buckets[_term_hash(t) % dim] > 1)
        rows.append({"dim": dim, "recall_at_5": round(recall / len(queries), 3),
                     "terms_in_collision": round(shared / len(vocab_terms), 3), "bytes_per_vec_f32": dim * 4})
    return rows


def quantize_int8(vec: list[float]) -> tuple[list[int], float]:
    """Symmetric scalar quantisation: x ~ q * scale with q in [-127, 127]. 4x smaller than float32."""
    scale = (max(abs(x) for x in vec) / 127.0) or 1.0
    return [max(-127, min(127, round(x / scale))) for x in vec], scale


def dequantize_int8(q: list[int], scale: float) -> list[float]:
    return [x * scale for x in q]


def vector_memory_bytes(n_vectors: int, dim: int, bytes_per_value: int, per_vector_overhead: int = 0) -> int:
    return n_vectors * (dim * bytes_per_value + per_vector_overhead)


# ---- Syllabus extension: BM25 as an explicit sparse matrix (CSR) and keyword search mechanisms ---
class SparseBM25Matrix:
    """BM25 weights stored as a CSR matrix with TERMS as rows and DOCUMENTS as columns: row t lists (doc, weight) pairs.
    That is exactly an inverted index. Scoring a query is a sparse matrix-vector product that touches only the rows of
    the query terms, so cost scales with postings length, not corpus size. Weight = idf * tf*(k1+1) / (tf + k1*(1-b+b*len/avg))."""

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        if not chunks:
            raise IndexEmptyError("cannot build BM25 matrix over zero chunks")
        self.doc_ids = [c.chunk_id for c in chunks]
        tfs = [Counter(tokenize(c.text)) for c in chunks]
        lens = [sum(tf.values()) for tf in tfs]
        avg = sum(lens) / len(lens)
        df: Counter[str] = Counter(t for tf in tfs for t in tf)
        n = len(chunks)
        self.terms = sorted(df)
        self.term_row = {t: i for i, t in enumerate(self.terms)}
        rows: list[list[tuple[int, float]]] = [[] for _ in self.terms]
        for d, tf in enumerate(tfs):
            for t, f in tf.items():
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                rows[self.term_row[t]].append((d, idf * f * (k1 + 1) / (f + k1 * (1 - b + b * lens[d] / avg))))
        self.indptr, self.indices, self.data = [0], [], []
        for row in rows:
            for d, w in row:
                self.indices.append(d)
                self.data.append(w)
            self.indptr.append(len(self.indices))
        self.n_docs = n

    @property
    def sparsity(self) -> float:
        return 1.0 - len(self.data) / (len(self.terms) * self.n_docs)

    def search(self, query: str, k: int) -> list[Hit]:
        scores: dict[int, float] = defaultdict(float)
        for term, qtf in Counter(tokenize(query)).items():  # repeated query terms count repeatedly, as in BM25Index
            row = self.term_row.get(term)
            if row is None:
                continue
            for j in range(self.indptr[row], self.indptr[row + 1]):
                scores[self.indices[j]] += self.data[j] * qtf
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [Hit(chunk_id=self.doc_ids[d], score=s, source="bm25-csr") for d, s in ranked]


class PositionalIndex:
    """Classic keyword mechanics: postings with token positions support boolean AND/OR/NOT and exact phrase queries."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.postings: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.all_ids = {c.chunk_id for c in chunks}
        for c in chunks:
            for pos, t in enumerate(tokenize(c.text)):
                self.postings[t][c.chunk_id].append(pos)

    def docs_with(self, term: str) -> set[str]:
        return set(self.postings.get(term.lower(), {}))

    def boolean(self, must: list[str], any_of: list[str] | None = None, must_not: list[str] | None = None) -> set[str]:
        result = set(self.all_ids)
        for t in must:
            result &= self.docs_with(t)
        if any_of:
            result &= set().union(*(self.docs_with(t) for t in any_of))
        for t in must_not or []:
            result -= self.docs_with(t)
        return result

    def phrase(self, phrase: str) -> set[str]:
        terms = tokenize(phrase)
        if not terms:
            return set()
        hits = set()
        for cid in self.docs_with(terms[0]):
            if any(all(cid in self.postings.get(t, {}) and p + i in self.postings[t][cid] for i, t in enumerate(terms))
                   for p in self.postings[terms[0]][cid]):
                hits.add(cid)
        return hits


# ---- Syllabus extension: distance metrics (cosine, euclidean, dot) ---
def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def euclidean(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def l2_normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def metric_equivalence_demo(n: int = 200, dim: int = 16, seed: int = 11) -> dict[str, object]:
    """Unit-length vectors: ||a-b||^2 = 2 - 2cos(a,b) and dot == cos, so all three metrics rank identically.
    Raw vectors of different lengths: dot rewards magnitude and euclid punishes it, so rankings diverge."""
    rng = random.Random(seed)
    raw = [[rng.gauss(0, 1) * rng.uniform(0.5, 3.0) for _ in range(dim)] for _ in range(n)]
    q = [rng.gauss(0, 1) for _ in range(dim)]

    def rankings(vs: list[list[float]], qv: list[float]) -> dict[str, list[int]]:
        return {"cosine": top_ids([cosine(qv, v) for v in vs], 5), "euclid": top_ids([-euclidean(qv, v) for v in vs], 5),
                "dot": top_ids([dot(qv, v) for v in vs], 5)}

    unit = [l2_normalize(v) for v in raw]
    qn = l2_normalize(q)
    ident_err = max(abs(euclidean(qn, v) ** 2 - (2 - 2 * cosine(qn, v))) for v in unit)
    r_raw, r_unit = rankings(raw, q), rankings(unit, qn)
    return {"raw_top1": {m: r[0] for m, r in r_raw.items()}, "raw_all_agree": len({tuple(r) for r in r_raw.values()}) == 1,
            "unit_all_agree": len({tuple(r) for r in r_unit.values()}) == 1, "max_identity_error": ident_err}


# ---- Syllabus extension: index creation (flat vs IVF with probe count) ---
def clustered_vectors(n: int, dim: int, centers: int, noise: float, seed: int) -> list[list[float]]:
    rng = random.Random(seed)
    cs = [[rng.gauss(0, 1) for _ in range(dim)] for _ in range(centers)]
    return [l2_normalize([c + rng.gauss(0, noise) for c in rng.choice(cs)]) for _ in range(n)]


class FlatIndex:
    """Exact brute-force scan: 100% recall, O(n*d) per query. The baseline every ANN index is measured against."""

    def __init__(self, vectors: list[list[float]]) -> None:
        if not vectors:
            raise IndexEmptyError("cannot build flat index over zero vectors")
        self.vectors = vectors

    def search(self, q: list[float], k: int) -> list[int]:
        return top_ids([dot(q, v) for v in self.vectors], k)


class IVFIndex:
    """Inverted-file index: k-means partitions vectors into `nlist` buckets; a query scans only the `nprobe` buckets whose
    centroids are closest. More probes = higher recall, more vectors scanned (latency). HNSW trades differently:
    graph hops instead of buckets, higher RAM, usually better recall per millisecond."""

    def __init__(self, vectors: list[list[float]], nlist: int, iters: int = 6, seed: int = 5) -> None:
        if not vectors or nlist < 1 or nlist > len(vectors):
            raise IndexEmptyError("need 1 <= nlist <= number of vectors")
        self.vectors = vectors
        rng = random.Random(seed)
        self.centroids = [list(vectors[i]) for i in rng.sample(range(len(vectors)), nlist)]
        for _ in range(iters):
            members = self._assign()
            for c, ids in enumerate(members):
                if ids:
                    dim = len(vectors[0])
                    self.centroids[c] = [sum(vectors[i][d] for i in ids) / len(ids) for d in range(dim)]
        self.lists = self._assign()
        self.last_scanned = 0

    def _assign(self) -> list[list[int]]:
        members: list[list[int]] = [[] for _ in self.centroids]
        for i, v in enumerate(self.vectors):
            members[min(range(len(self.centroids)), key=lambda c: euclidean(v, self.centroids[c]))].append(i)
        return members

    def search(self, q: list[float], k: int, nprobe: int = 1) -> list[int]:
        probe = sorted(range(len(self.centroids)), key=lambda c: euclidean(q, self.centroids[c]))[:nprobe]
        cand = [i for c in probe for i in self.lists[c]]
        self.last_scanned = len(cand)
        return sorted(cand, key=lambda i: (-dot(q, self.vectors[i]), i))[:k]


def ivf_probe_sweep(nprobes: list[int], n: int = 1200, dim: int = 24, nlist: int = 16, k: int = 10, n_queries: int = 30) -> list[dict[str, float]]:
    vecs = clustered_vectors(n, dim, centers=nlist, noise=0.45, seed=3)
    rng = random.Random(9)
    queries = [l2_normalize([x + rng.gauss(0, 0.2) for x in vecs[rng.randrange(n)]]) for _ in range(n_queries)]
    flat, ivf = FlatIndex(vecs), IVFIndex(vecs, nlist)
    truth = [set(flat.search(q, k)) for q in queries]
    rows = []
    for p in nprobes:
        rec = scanned = 0.0
        for q, gt in zip(queries, truth):
            rec += len(set(ivf.search(q, k, p)) & gt) / k
            scanned += ivf.last_scanned / n
        rows.append({"nprobe": p, "recall_at_10": round(rec / n_queries, 3), "fraction_scanned": round(scanned / n_queries, 3)})
    return rows


# ---- Syllabus extension: relevance metrics (precision, recall, MRR, nDCG) ---
def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return sum(1 for d in ranked[:k] if d in relevant) / k


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return sum(1 for d in ranked[:k] if d in relevant) / len(relevant) if relevant else 0.0


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    return next((1.0 / i for i, d in enumerate(ranked, 1) if d in relevant), 0.0)


def ndcg_at_k(ranked: list[str], gains: dict[str, float], k: int) -> float:
    """Graded relevance with log discount: DCG = sum gain_i / log2(i + 1); normalised by the ideal ordering."""
    dcg = sum(gains.get(d, 0.0) / math.log2(i + 1) for i, d in enumerate(ranked[:k], 1))
    ideal = sum(g / math.log2(i + 1) for i, g in enumerate(sorted(gains.values(), reverse=True)[:k], 1))
    return dcg / ideal if ideal else 0.0


EVAL_EXTRA = {
    "policy-expense": "Expense policy. Submit receipts within fourteen days. Meals above fifty dollars need manager approval before reimbursement.",
    "kb-vpn": "VPN troubleshooting. If the tunnel drops, reconnect and check the gateway certificate. Split tunnel mode routes only corporate traffic.",
    "kb-backup": "Backup runbook. Nightly snapshots are retained for thirty days. Restore requests need a ticket and a recovery point objective.",
    "policy-security": "Security policy. Passwords rotate every ninety days. Report lost laptops to the security team within one hour.",
}
EVAL_QUERIES = [  # query -> graded relevance by doc id (2 = answers it, 1 = related)
    ("ERR_4471_TLS", {"kb-tls": 2, "kb-vpn": 1}),
    ("can I get my money back", {"policy-refund": 2}),
    ("how many vacation days do I get", {"policy-leave": 2}),
    ("users cannot log in SAML clock skew", {"kb-sso": 2}),
    ("enterprise contract refund exclusion", {"policy-refund": 2}),
    ("certificate expiry monitoring rotation", {"kb-tls": 2, "kb-vpn": 1, "policy-security": 1}),
]


def doc_ranking(chunk_ranking: list[str], chunks: dict[str, Chunk]) -> list[str]:
    seen: list[str] = []
    for cid in chunk_ranking:
        d = chunks[cid].doc_id
        if d not in seen:
            seen.append(d)
    return seen


def score_system(name: str, ranker: Callable[[str], list[str]], k: int = 3) -> dict[str, float | str]:
    p = r = mrr = ndcg = 0.0
    for query, gains in EVAL_QUERIES:
        ranked, rel = ranker(query), {d for d, g in gains.items() if g > 0}
        p, r = p + precision_at_k(ranked, rel, k), r + recall_at_k(ranked, rel, k)
        mrr, ndcg = mrr + reciprocal_rank(ranked, rel), ndcg + ndcg_at_k(ranked, gains, k)
    n = len(EVAL_QUERIES)
    return {"system": name, "precision_at_3": round(p / n, 3), "recall_at_3": round(r / n, 3), "mrr": round(mrr / n, 3), "ndcg_at_3": round(ndcg / n, 3)}


# ---- Syllabus extension: vector DB client Protocol, metadata filtering, in-memory + Pinecone/Qdrant adapters ---
Scalar = Union[str, int, float, bool]
MetricName = Literal["cosine", "euclidean", "dot"]


class VectorRecord(BaseModel):
    id: str
    vector: list[float]
    metadata: dict[str, Scalar] = Field(default_factory=dict)


class QueryMatch(BaseModel):
    id: str
    score: float  # higher is better for every metric (euclidean is returned as negative distance)
    metadata: dict[str, Scalar] = Field(default_factory=dict)


class IndexSpec(BaseModel):
    """Desired state of a cloud index. Provisioning is declarative and idempotent: same spec twice = one index."""
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,44}$", description="lowercase letters, digits, hyphens; max 45 chars")
    dim: int = Field(gt=0, le=20000)
    metric: MetricName = "cosine"


class VectorDB(Protocol):
    async def describe_index(self, name: str) -> IndexSpec | None: ...
    async def create_index(self, spec: IndexSpec) -> None: ...
    async def upsert(self, index: str, records: list[VectorRecord]) -> int: ...
    async def query(self, index: str, vector: list[float], top_k: int, filter: dict[str, Any] | None = None) -> list[QueryMatch]: ...
    async def delete_index(self, name: str) -> None: ...


# Canonical filter: {"field": value} means equals; {"field": {"$gte": 2024}} uses operators $eq $ne $gt $gte $lt $lte $in.
# Several managed vector databases (e.g. Pinecone) use this Mongo-style shape natively; others need translation.
def metadata_matches(metadata: dict[str, Scalar], flt: dict[str, Any] | None) -> bool:
    for field, cond in (flt or {}).items():
        value = metadata.get(field)
        ops = cond if isinstance(cond, dict) else {"$eq": cond}
        for op, arg in ops.items():
            if value is None:
                return False
            ok = {"$eq": lambda: value == arg, "$ne": lambda: value != arg, "$gt": lambda: value > arg,  # type: ignore[operator]
                  "$gte": lambda: value >= arg, "$lt": lambda: value < arg, "$lte": lambda: value <= arg,  # type: ignore[operator]
                  "$in": lambda: value in arg}.get(op)
            if ok is None:
                raise VectorDBError(f"unsupported filter operator {op}")
            if not ok():
                return False
    return True


class InMemoryVectorDB:
    """Offline reference implementation. Filtering happens BEFORE scoring (pre-filter), so top_k is always filled when
    enough allowed vectors exist. `transient_failures` simulates network blips for retry demos."""

    def __init__(self, transient_failures: int = 0) -> None:
        self._indexes: dict[str, tuple[IndexSpec, dict[str, VectorRecord]]] = {}
        self.transient_failures = transient_failures

    async def _maybe_fail(self) -> None:
        await asyncio.sleep(0.001)
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise VectorDBTransient("simulated connection reset")

    async def describe_index(self, name: str) -> IndexSpec | None:
        await self._maybe_fail()
        return self._indexes[name][0] if name in self._indexes else None

    async def create_index(self, spec: IndexSpec) -> None:
        await self._maybe_fail()
        if spec.name in self._indexes:
            raise VectorDBError(f"index {spec.name} already exists")
        self._indexes[spec.name] = (spec, {})

    async def upsert(self, index: str, records: list[VectorRecord]) -> int:
        await self._maybe_fail()
        spec, store = self._get(index)
        for r in records:
            if len(r.vector) != spec.dim:
                raise VectorDBError(f"record {r.id} has dim {len(r.vector)}, index expects {spec.dim}")
            store[r.id] = r
        return len(records)

    async def query(self, index: str, vector: list[float], top_k: int, filter: dict[str, Any] | None = None) -> list[QueryMatch]:
        await self._maybe_fail()
        spec, store = self._get(index)
        if len(vector) != spec.dim:
            raise VectorDBError(f"query has dim {len(vector)}, index expects {spec.dim}")
        score = {"cosine": cosine, "dot": dot, "euclidean": lambda a, b: -euclidean(a, b)}[spec.metric]
        allowed = [r for r in store.values() if metadata_matches(r.metadata, filter)]
        ranked = sorted(allowed, key=lambda r: (-score(vector, r.vector), r.id))[:top_k]
        return [QueryMatch(id=r.id, score=score(vector, r.vector), metadata=r.metadata) for r in ranked]

    async def delete_index(self, name: str) -> None:
        await self._maybe_fail()
        self._indexes.pop(name, None)

    def _get(self, name: str) -> tuple[IndexSpec, dict[str, VectorRecord]]:
        if name not in self._indexes:
            raise VectorDBError(f"unknown index {name}")
        return self._indexes[name]


class ResilientVectorDB:
    """Wraps any VectorDB: every call retried with exponential backoff on VectorDBTransient only."""

    def __init__(self, inner: VectorDB, attempts: int = 4) -> None:
        self._inner, self._attempts = inner, attempts

    async def _retry(self, fn: Callable[[], Awaitable[T]]) -> T:
        return await retry_with_backoff(fn, attempts=self._attempts, retry_on=(VectorDBTransient,))

    async def describe_index(self, name: str) -> IndexSpec | None:
        return await self._retry(lambda: self._inner.describe_index(name))

    async def create_index(self, spec: IndexSpec) -> None:
        await self._retry(lambda: self._inner.create_index(spec))

    async def upsert(self, index: str, records: list[VectorRecord]) -> int:
        return await self._retry(lambda: self._inner.upsert(index, records))

    async def query(self, index: str, vector: list[float], top_k: int, filter: dict[str, Any] | None = None) -> list[QueryMatch]:
        return await self._retry(lambda: self._inner.query(index, vector, top_k, filter))

    async def delete_index(self, name: str) -> None:
        await self._retry(lambda: self._inner.delete_index(name))


async def provision_index(db: VectorDB, spec: IndexSpec) -> bool:
    """Idempotent provisioning: create if missing, no-op if identical, fail loudly on drift (wrong dim or metric).
    Returns True when an index was created. Run this from CI/CD or a bootstrap job, not from request handlers."""
    existing = await db.describe_index(spec.name)
    if existing is None:
        await db.create_index(spec)
        return True
    if (existing.dim, existing.metric) != (spec.dim, spec.metric):
        raise VectorDBError(f"index {spec.name} exists with dim={existing.dim} metric={existing.metric}; refusing to reuse it for {spec.dim}/{spec.metric}")
    return False


def _batched(items: list[T], size: int) -> list[list[T]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _sdk_call_errors(exc: Exception) -> VectorDBError:
    """Map SDK exceptions: network-level problems are retryable, everything else is a hard error."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return VectorDBTransient(str(exc))
    return VectorDBError(f"{type(exc).__name__}: {exc}")


class PineconeVectorDB:
    """Adapter for the Pinecone Python SDK (`pip install pinecone`, serverless indexes). The SDK is imported lazily, so
    this module runs without it. The API key is passed in by the caller (read it from an environment variable such as
    PINECONE_API_KEY; never hard-code it). Verify call signatures against the SDK version you pin."""

    METRICS = {"cosine": "cosine", "euclidean": "euclidean", "dot": "dotproduct"}

    def __init__(self, api_key: str, *, cloud: str, region: str, namespace: str = "", batch_size: int = 100) -> None:
        self._api_key, self._cloud, self._region = api_key, cloud, region
        self._namespace, self._batch = namespace, batch_size
        self._pc: Any = None

    @staticmethod
    def check_dependency() -> None:
        try:
            import pinecone  # noqa: F401
        except ImportError as exc:
            raise VectorDBDependencyError("pinecone") from exc

    def _client(self) -> Any:
        self.check_dependency()
        if self._pc is None:
            from pinecone import Pinecone
            self._pc = Pinecone(api_key=self._api_key)
        return self._pc

    async def _run(self, fn: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(fn)
        except VectorDBError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate vendor errors into our typed errors
            raise _sdk_call_errors(exc) from exc

    async def describe_index(self, name: str) -> IndexSpec | None:
        pc = self._client()
        if name not in await self._run(lambda: pc.list_indexes().names()):
            return None
        info = await self._run(lambda: pc.describe_index(name))
        back = {v: k for k, v in self.METRICS.items()}
        return IndexSpec(name=name, dim=info.dimension, metric=back[info.metric])

    async def create_index(self, spec: IndexSpec) -> None:
        pc = self._client()
        from pinecone import ServerlessSpec
        await self._run(lambda: pc.create_index(name=spec.name, dimension=spec.dim, metric=self.METRICS[spec.metric],
                                                spec=ServerlessSpec(cloud=self._cloud, region=self._region)))

    async def upsert(self, index: str, records: list[VectorRecord]) -> int:
        idx = self._client().Index(index)
        for batch in _batched(records, self._batch):
            payload = [{"id": r.id, "values": r.vector, "metadata": r.metadata} for r in batch]
            await self._run(lambda p=payload: idx.upsert(vectors=p, namespace=self._namespace))
        return len(records)

    async def query(self, index: str, vector: list[float], top_k: int, filter: dict[str, Any] | None = None) -> list[QueryMatch]:
        idx = self._client().Index(index)
        resp = await self._run(lambda: idx.query(vector=vector, top_k=top_k, filter=filter, include_metadata=True, namespace=self._namespace))
        return [QueryMatch(id=m.id, score=float(m.score), metadata=dict(m.metadata or {})) for m in resp.matches]

    async def delete_index(self, name: str) -> None:
        pc = self._client()
        await self._run(lambda: pc.delete_index(name))


class QdrantVectorDB:
    """Adapter for qdrant-client (`pip install qdrant-client`). Works against a local container
    (`docker run -p 6333:6333 qdrant/qdrant`, url http://localhost:6333) or Qdrant Cloud (url + api_key). Qdrant point ids must
    be unsigned ints or UUIDs, so string ids are mapped with uuid5 and the original id is kept in the payload as `_id`.
    Uses query_points (qdrant-client >= 1.10); verify against the version you pin."""

    DISTANCE = {"cosine": "COSINE", "euclidean": "EUCLID", "dot": "DOT"}

    def __init__(self, url: str, api_key: str | None = None, batch_size: int = 128) -> None:
        self._url, self._api_key, self._batch = url, api_key, batch_size
        self._client_obj: Any = None

    @staticmethod
    def check_dependency() -> None:
        try:
            import qdrant_client  # noqa: F401
        except ImportError as exc:
            raise VectorDBDependencyError("qdrant-client") from exc

    def _client(self) -> Any:
        self.check_dependency()
        if self._client_obj is None:
            from qdrant_client import QdrantClient
            self._client_obj = QdrantClient(url=self._url, api_key=self._api_key)
        return self._client_obj

    @staticmethod
    def point_id(record_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, record_id))

    @staticmethod
    def to_qdrant_filter(flt: dict[str, Any] | None, models: Any) -> Any:
        """Translate the canonical filter into qdrant `Filter(must=[FieldCondition...])`. $ne/$gt-style ops map to
        Range or must_not; unsupported operators fail loudly rather than silently widening results."""
        if not flt:
            return None
        must, must_not = [], []
        for field, cond in flt.items():
            ops = cond if isinstance(cond, dict) else {"$eq": cond}
            rng: dict[str, Any] = {}
            for op, arg in ops.items():
                if op == "$eq":
                    must.append(models.FieldCondition(key=field, match=models.MatchValue(value=arg)))
                elif op == "$in":
                    must.append(models.FieldCondition(key=field, match=models.MatchAny(any=list(arg))))
                elif op == "$ne":
                    must_not.append(models.FieldCondition(key=field, match=models.MatchValue(value=arg)))
                elif op in ("$gt", "$gte", "$lt", "$lte"):
                    rng[op[1:]] = arg
                else:
                    raise VectorDBError(f"unsupported filter operator {op}")
            if rng:
                must.append(models.FieldCondition(key=field, range=models.Range(**rng)))
        return models.Filter(must=must or None, must_not=must_not or None)

    async def _run(self, fn: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(fn)
        except VectorDBError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate vendor errors into our typed errors
            raise _sdk_call_errors(exc) from exc

    async def describe_index(self, name: str) -> IndexSpec | None:
        c = self._client()
        if not await self._run(lambda: c.collection_exists(name)):
            return None
        params = (await self._run(lambda: c.get_collection(name))).config.params.vectors
        back = {"cosine": "cosine", "euclid": "euclidean", "dot": "dot"}
        return IndexSpec(name=name, dim=params.size, metric=back[str(getattr(params.distance, "value", params.distance)).lower()])

    async def create_index(self, spec: IndexSpec) -> None:
        c = self._client()
        from qdrant_client import models
        cfg = models.VectorParams(size=spec.dim, distance=getattr(models.Distance, self.DISTANCE[spec.metric]))
        await self._run(lambda: c.create_collection(collection_name=spec.name, vectors_config=cfg))

    async def upsert(self, index: str, records: list[VectorRecord]) -> int:
        c = self._client()
        from qdrant_client import models
        for batch in _batched(records, self._batch):
            points = [models.PointStruct(id=self.point_id(r.id), vector=r.vector, payload={**r.metadata, "_id": r.id}) for r in batch]
            await self._run(lambda p=points: c.upsert(collection_name=index, points=p))
        return len(records)

    async def query(self, index: str, vector: list[float], top_k: int, filter: dict[str, Any] | None = None) -> list[QueryMatch]:
        c = self._client()
        from qdrant_client import models
        qf = self.to_qdrant_filter(filter, models)
        resp = await self._run(lambda: c.query_points(collection_name=index, query=vector, limit=top_k, query_filter=qf, with_payload=True))
        out = []
        for p in resp.points:
            payload = dict(p.payload or {})
            out.append(QueryMatch(id=str(payload.pop("_id", p.id)), score=float(p.score), metadata=payload))
        return out

    async def delete_index(self, name: str) -> None:
        c = self._client()
        await self._run(lambda: c.delete_collection(name))


# ---- Syllabus extension: end-to-end basic retrieval over the vector DB protocol ---
TENANTS = {"kb-tls": ("acme", 2025), "kb-sso": ("acme", 2024), "policy-refund": ("acme", 2023), "policy-leave": ("globex", 2025),
           "policy-expense": ("globex", 2024), "kb-vpn": ("acme", 2025), "kb-backup": ("globex", 2022), "policy-security": ("acme", 2024)}


async def build_basic_index(db: VectorDB, spec: IndexSpec, chunks: list[Chunk]) -> int:
    """chunk -> embed -> upsert with filterable metadata. The same pipeline works against any VectorDB implementation."""
    await provision_index(db, spec)
    records = [VectorRecord(id=c.chunk_id, vector=embed(c.text),
                            metadata={"doc_id": c.doc_id, "tenant": TENANTS[c.doc_id][0], "year": TENANTS[c.doc_id][1], "position": c.position})
               for c in chunks]
    return await db.upsert(spec.name, records)


async def basic_retrieve(db: VectorDB, index: str, query: str, k: int = 3, flt: dict[str, Any] | None = None) -> list[QueryMatch]:
    return await db.query(index, embed(query), k, flt)


# ---- Syllabus extension: combining dense and sparse signals (weighted fusion vs RRF) ---
def min_max(scores: dict[str, float]) -> dict[str, float]:
    lo, hi = min(scores.values(), default=0.0), max(scores.values(), default=0.0)
    return {k: (v - lo) / (hi - lo) if hi > lo else 1.0 for k, v in scores.items()}


def weighted_fusion(dense: list[Hit], sparse: list[Hit], alpha: float = 0.5, top: int = 50) -> list[tuple[str, float]]:
    """Score-based fusion: alpha*dense + (1-alpha)*bm25 AFTER min-max normalisation, because raw BM25 (unbounded) and
    cosine ([-1,1]) are not comparable. Needs alpha tuning per corpus; RRF needs only k."""
    d, s = min_max({h.chunk_id: h.score for h in dense}), min_max({h.chunk_id: h.score for h in sparse})
    fused = {cid: alpha * d.get(cid, 0.0) + (1 - alpha) * s.get(cid, 0.0) for cid in set(d) | set(s)}
    return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def rrf_explain(rankings: list[list[str]], k: int = 60) -> dict[str, list[str]]:
    """Per-document audit trail of RRF: shows each 1/(k+rank) term, handy when a stakeholder asks why a result won."""
    out: dict[str, list[str]] = defaultdict(list)
    for ranking in rankings:
        for rank, cid in enumerate(ranking, 1):
            out[cid].append(f"1/({k}+{rank})={1 / (k + rank):.5f}")
    return dict(out)


# ---- Syllabus extension: cross-encoder rerankers and API integration ---
class HTTPTransport(Protocol):
    async def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]: ...


class RerankHTTPError(RerankError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"rerank API returned {status}: {message}")
        self.status = status  # 0 means the request never got an HTTP response (network/timeout)


class UrllibTransport:
    """Real stdlib transport (no third-party HTTP client). Blocking urllib runs in a worker thread."""

    async def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        def call() -> dict[str, Any]:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - https URL supplied by operator config
                    return json.loads(resp.read().decode())  # type: ignore[no-any-return]
            except urllib.error.HTTPError as exc:
                raise RerankHTTPError(exc.code, exc.reason) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RerankHTTPError(0, str(exc)) from exc
        return await asyncio.to_thread(call)


class RerankItem(BaseModel):
    index: int = Field(ge=0)
    relevance_score: float


class RerankResponse(BaseModel):
    results: list[RerankItem]


RETRYABLE_STATUS = {0, 429, 500, 502, 503, 504}


class HTTPReranker:
    """Hosted cross-encoder behind a typical rerank API. Request: {"model", "query", "documents": [str], "top_n"}.
    Response: {"results": [{"index": i, "relevance_score": s}, ...]} sorted best-first, where `index` points into the
    documents we sent. Field names vary a little by vendor (check yours); the Pydantic response models are the one
    place to adapt. We map results back to the input order so it satisfies the Reranker protocol."""

    RETRY_STATUS = RETRYABLE_STATUS

    def __init__(self, transport: HTTPTransport, url: str, api_key: str, model: str, *, attempts: int = 4,
                 timeout_s: float = 2.0, max_documents: int = 1000) -> None:
        self._t, self._url, self._key, self._model = transport, url, api_key, model
        self._attempts, self._timeout, self._max_docs = attempts, timeout_s, max_documents

    async def _once(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._t.post_json(self._url, {"Authorization": f"Bearer {self._key}"}, body, self._timeout)
        except RerankHTTPError as exc:
            if exc.status in RETRYABLE_STATUS:
                raise RerankRetryable(str(exc)) from exc
            raise

    async def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        if len(passages) > self._max_docs:
            raise RerankError(f"{len(passages)} documents exceeds max_documents={self._max_docs}; rerank a shorter shortlist")
        body = {"model": self._model, "query": query, "documents": passages, "top_n": len(passages)}
        raw = await retry_with_backoff(lambda: self._once(body), attempts=self._attempts, retry_on=(RerankRetryable,))
        try:
            parsed = RerankResponse.model_validate(raw)
        except ValidationError as exc:
            raise RerankError(f"malformed rerank response: {exc.errors()[0]['msg']}") from exc
        scores: list[float | None] = [None] * len(passages)
        for item in parsed.results:
            if item.index >= len(passages):
                raise RerankError(f"rerank response index {item.index} out of range")
            scores[item.index] = item.relevance_score
        if any(s is None for s in scores):
            raise RerankError("rerank response omitted some documents")
        return [float(s) for s in scores if s is not None]


class FakeRerankTransport:
    """In-process stand-in for a rerank server: validates the request shape, can fail first with given statuses, then
    scores with the lexical cross-encoder and answers best-first like a real API."""

    def __init__(self, fail_first: list[int] | None = None) -> None:
        self.fail_first, self.calls, self.last_body = list(fail_first or []), 0, {}

    async def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(0.001)
        if self.fail_first:
            raise RerankHTTPError(self.fail_first.pop(0), "simulated")
        if not headers.get("Authorization", "").startswith("Bearer ") or not {"model", "query", "documents", "top_n"} <= set(body):
            raise RerankHTTPError(400, "bad request shape")
        self.last_body = body
        scores = await LexicalCrossEncoder().score(body["query"], body["documents"])
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[:body["top_n"]]
        return {"results": [{"index": i, "relevance_score": scores[i]} for i in order]}


class FallbackReranker:
    """Graceful degradation: if the primary reranker fails, log and use the fallback instead of failing the query."""

    def __init__(self, primary: Reranker, fallback: Reranker) -> None:
        self._p, self._f = primary, fallback

    async def score(self, query: str, passages: list[str]) -> list[float]:
        try:
            return await self._p.score(query, passages)
        except RerankError as exc:
            log.warning("reranker failed, using fallback", extra={"ctx": {"error": str(exc)}})
            return await self._f.score(query, passages)


class SentenceTransformersCrossEncoder:
    """Self-hosted cross-encoder via sentence-transformers (`pip install sentence-transformers`); imported lazily.
    Pass a model name of your choice, e.g. "cross-encoder/ms-marco-MiniLM-L-6-v2" (downloads weights unless cached)."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: Any = None

    async def score(self, query: str, passages: list[str]) -> list[float]:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RerankDependencyError("sentence-transformers") from exc
        if self._model is None:
            self._model = await asyncio.to_thread(CrossEncoder, self.model_name)
        scores = await asyncio.to_thread(self._model.predict, [(query, p) for p in passages])
        return [float(s) for s in scores]


async def extension_demo() -> None:
    # 1-2. fixed-size vs semantic chunking, overlap sweep
    fixed = fixed_size_chunk("mixed", MIXED_DOC, size_words=20, overlap_words=0)
    sem, sims = semantic_chunk("mixed", MIXED_DOC)
    log.info("chunking", extra={"ctx": {"fixed_chunks": len(fixed), "semantic_chunks": len(sem), "adjacent_sims": [round(s, 2) for s in sims],
                                        "semantic_first_words": [c.text.split()[0] + " " + c.text.split()[1] for c in sem]}})
    log.info("overlap sweep", extra={"ctx": {"rows": overlap_sweep(CORPUS, size_words=14, overlaps=[0, 2, 4, 6, 8])}})

    # 3-4. representations and dimensionality
    sp = sparse_cosine(sparse_vector("car refund"), sparse_vector("automobile cash back"))
    de = cosine(toy_semantic_embed("car refund"), toy_semantic_embed("automobile cash back"))
    log.info("sparse vs dense", extra={"ctx": {"sparse_cosine": round(sp, 3), "toy_dense_cosine": round(de, 3),
                                               "sparse_nonzeros": len(sparse_vector("car refund")), "dense_dims": 64}})
    log.info("dimension sweep", extra={"ctx": {"rows": dimension_sweep([8, 32, 128, 512, 2048])}})
    v = embed("refund within thirty days")
    q8, scale = quantize_int8(v)
    err = cosine(v, dequantize_int8(q8, scale))
    n_chunks, dim = 24_000_000, 768
    log.info("int8 quantisation", extra={"ctx": {"cosine_after_roundtrip": round(err, 5),
                                                 "float32_gb": round(vector_memory_bytes(n_chunks, dim, 4) / 1e9, 1),
                                                 "int8_gb": round(vector_memory_bytes(n_chunks, dim, 1, per_vector_overhead=4) / 1e9, 1)}})

    # 5. BM25 as a sparse matrix, 11. keyword mechanics
    chunks = [c for doc_id, t in {**CORPUS, **EVAL_EXTRA}.items() for c in recursive_chunk(doc_id, t)]
    by_id = {c.chunk_id: c for c in chunks}
    matrix, bm25 = SparseBM25Matrix(chunks), BM25Index(chunks)
    a, b = matrix.search("refund enterprise contracts", 3), bm25.search("refund enterprise contracts", 3)
    same = [h.chunk_id for h in a] == [h.chunk_id for h in b] and all(abs(x.score - y.score) < 1e-9 for x, y in zip(a, b))
    log.info("bm25 csr", extra={"ctx": {"terms": len(matrix.terms), "docs": matrix.n_docs, "nnz": len(matrix.data),
                                        "sparsity": round(matrix.sparsity, 3), "matches_BM25Index": same}})
    pos = PositionalIndex(chunks)
    log.info("keyword search", extra={"ctx": {"AND": sorted(pos.boolean(["certificate", "chain"])),
                                              "OR_NOT": sorted(pos.boolean([], any_of=["refund", "vacation"], must_not=["enterprise"])),
                                              "phrase_hit": sorted(pos.phrase("clock skew between")), "phrase_miss": sorted(pos.phrase("skew clock"))}})

    # 6. index creation: flat vs IVF
    log.info("ivf probe sweep", extra={"ctx": {"rows": ivf_probe_sweep([1, 2, 4, 8, 16])}})

    # 7. distance metrics
    log.info("metric equivalence", extra={"ctx": metric_equivalence_demo()})

    # 8-10, 9. vector DB protocol, provisioning, metadata filtering, end-to-end retrieval
    flaky = InMemoryVectorDB(transient_failures=2)
    db: VectorDB = ResilientVectorDB(flaky)
    all_chunks = chunks
    spec = IndexSpec(name="support-kb", dim=DIM, metric="cosine")
    created = await provision_index(db, spec)
    created_again = await provision_index(db, spec)
    try:
        await provision_index(db, IndexSpec(name="support-kb", dim=128, metric="cosine"))
    except VectorDBError as exc:
        log.info("provision drift guarded", extra={"ctx": {"error": str(exc)[:70]}})
    await db.delete_index("support-kb")
    n = await build_basic_index(db, spec, all_chunks)
    top = await basic_retrieve(db, "support-kb", "how many vacation days do I get", 2)
    acme = await basic_retrieve(db, "support-kb", "how many vacation days do I get", 3, {"tenant": "acme", "year": {"$gte": 2024}})
    log.info("basic retrieval", extra={"ctx": {"created": created, "created_again": created_again, "upserted": n,
                                               "unfiltered_top": [m.id for m in top],
                                               "acme_2024_plus": [(m.id, m.metadata["tenant"], m.metadata["year"]) for m in acme]}})
    qvec = embed("how many vacation days do I get")
    post = [m for m in await db.query("support-kb", qvec, 3) if m.metadata["tenant"] == "acme"]
    pre = await db.query("support-kb", qvec, 3, {"tenant": "acme"})
    log.info("pre vs post filter", extra={"ctx": {"post_filter_returned": len(post), "pre_filter_returned": len(pre)}})
    for name, adapter in (("pinecone", PineconeVectorDB), ("qdrant", QdrantVectorDB)):
        try:
            adapter.check_dependency()
            log.info("adapter available", extra={"ctx": {"adapter": name, "note": "not exercised offline"}})
        except VectorDBDependencyError as exc:
            log.info("adapter skipped", extra={"ctx": {"adapter": name, "hint": str(exc)}})

    # 12-13. dense + sparse combination, RRF
    vindex = VectorIndex(chunks)
    q = "ERR_4471_TLS certificate chain"
    dense_hits, sparse_hits = vindex.search(q, 10), bm25.search(q, 10)
    log.info("fusion", extra={"ctx": {"weighted_alpha_0.5": [cid for cid, _ in weighted_fusion(dense_hits, sparse_hits, 0.5)[:3]],
                                      "weighted_alpha_0.9": [cid for cid, _ in weighted_fusion(dense_hits, sparse_hits, 0.9)[:3]],
                                      "rrf_k60": [cid for cid, _, _ in rrf([dense_hits, sparse_hits])[:3]],
                                      "rrf_k1": [cid for cid, _, _ in rrf([dense_hits, sparse_hits], k=1)[:3]]}})
    top_cid = rrf([dense_hits, sparse_hits])[0][0]
    log.info("rrf explained", extra={"ctx": {"winner": top_cid, "terms": rrf_explain([[h.chunk_id for h in dense_hits], [h.chunk_id for h in sparse_hits]])[top_cid]}})

    # 14. precision / recall / MRR / nDCG over systems
    fake = FakeRerankTransport(fail_first=[429, 503])
    http_reranker = HTTPReranker(fake, "https://rerank.example.test/v1/rerank", os.environ.get("RERANK_API_KEY", "offline-demo"), "demo-rerank-model")
    retriever = HybridRetriever(chunks, http_reranker)
    ranked_map: dict[str, list[str]] = {}
    for query, _ in EVAL_QUERIES:
        ranked_map[query] = [r.chunk.chunk_id for r in await retriever.retrieve(query, k=6)]
    systems = [
        ("bm25", lambda qq: doc_ranking([h.chunk_id for h in bm25.search(qq, 10)], by_id)),
        ("dense", lambda qq: doc_ranking([h.chunk_id for h in vindex.search(qq, 10)], by_id)),
        ("hybrid_rrf", lambda qq: doc_ranking([cid for cid, _, _ in rrf([bm25.search(qq, 10), vindex.search(qq, 10)])], by_id)),
        ("hybrid_rrf+rerank_api", lambda qq: doc_ranking(ranked_map[qq], by_id)),
    ]
    log.info("retrieval metrics", extra={"ctx": {"rows": [score_system(name, fn) for name, fn in systems]}})
    log.info("worked example", extra={"ctx": {"ranked": ["a", "x", "b"], "relevant": ["a", "b"], "P@3": round(precision_at_k(["a", "x", "b"], {"a", "b"}, 3), 3),
                                              "R@3": recall_at_k(["a", "x", "b"], {"a", "b"}, 3), "MRR": reciprocal_rank(["x", "a"], {"a"}),
                                              "nDCG@3": round(ndcg_at_k(["x", "a", "b"], {"a": 2, "b": 1}, 3), 3)}})

    # 15-16. cross-encoder reranking through the HTTP adapter (fake transport), retries, fallback
    passages = [by_id[c].text for c in ("kb-tls#0", "policy-refund#0", "kb-sso#0")]
    scores = await http_reranker.score("ERR_4471_TLS", passages)
    log.info("http rerank", extra={"ctx": {"scores": [round(s, 3) for s in scores], "transport_calls": fake.calls,
                                           "request_keys": sorted(fake.last_body), "top_n": fake.last_body["top_n"]}})
    down = HTTPReranker(FakeRerankTransport(fail_first=[503] * 10), "https://rerank.example.test/v1/rerank", "offline-demo", "demo-rerank-model", attempts=3)
    try:
        await down.score("x", passages)
    except RerankRetryable as exc:
        log.info("rerank retries exhausted", extra={"ctx": {"error": str(exc)}})
    safe = await FallbackReranker(down, LexicalCrossEncoder()).score("ERR_4471_TLS", passages)
    bad_key = HTTPReranker(FakeRerankTransport(fail_first=[401]), "https://rerank.example.test/v1/rerank", "offline-demo", "demo-rerank-model")
    try:
        await bad_key.score("x", passages)
    except RerankHTTPError as exc:
        log.info("non-retryable not retried", extra={"ctx": {"status": exc.status}})
    log.info("fallback used", extra={"ctx": {"scores": [round(s, 3) for s in safe]}})
    if importlib.util.find_spec("sentence_transformers") is None:  # never trigger a model download in the offline demo
        try:
            await SentenceTransformersCrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2").score("x", ["y"])
        except RerankDependencyError as exc:
            log.info("local cross-encoder skipped", extra={"ctx": {"hint": str(exc)}})
    else:
        log.info("local cross-encoder available", extra={"ctx": {"note": "not exercised offline"}})


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
    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
