"""Module 14 lab: LLM gateway with fallbacks and budgets, trace/span recording with cost accounting, and a
RAGAS-style evaluation harness with regression gates.

- Gateway: model group with ordered providers, per-attempt timeout, jittered retries, circuit-breaker-lite,
  per-tenant daily budget (hard stop), response cache.
- Tracer: Langfuse-like in-memory traces with parent/child spans and percentile summaries.
- Evals: lexical implementations of faithfulness, answer relevancy and context precision (deterministic,
  offline); swap the judge for an LLM in production.

Syllabus extension (see "Syllabus extension" banners): idempotent tool execution, backoff strategies and a retry-storm
simulator, LiteLLM-style config and virtual keys, rate limiters and routing policies wired into the Gateway, an
LLM-as-judge pipeline, full RAGAS-style metrics, synthetic benchmarks, run registry with bootstrap CIs, GenAI-style span
attributes with cost/latency reports, an agent-trace debugger, session metrics and trace export. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import logging
import math
import os
import random
import re
import sys
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from enum import Enum
from typing import AsyncIterator, Awaitable, Callable, Literal, Mapping, Protocol, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


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


log = get_logger("lab14")


class GatewayError(Exception):
    pass


class BudgetExceeded(GatewayError):
    pass


class ProviderError(GatewayError):
    """Retryable provider failure (429/5xx/timeouts)."""


class AllProvidersFailed(GatewayError):
    pass


class EvalRegression(GatewayError):
    pass


class RateLimited(GatewayError):
    """Raised before any provider call when a tenant exceeds its rate or concurrency limit."""


class Usage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class Completion(BaseModel):
    text: str
    usage: Usage
    provider: str
    cost_usd: float
    latency_ms: float
    fallback_used: bool = False
    cached: bool = False


class Span(BaseModel):
    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    start: float
    end: float = 0.0
    attrs: dict[str, object] = Field(default_factory=dict)

    @property
    def ms(self) -> float:
        return (self.end - self.start) * 1000


_current_span: ContextVar[Span | None] = ContextVar("current_span", default=None)  # per-task, safe under asyncio.gather


class Tracer:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    @asynccontextmanager
    async def span(self, name: str, trace_id: str | None = None, **attrs: object) -> AsyncIterator[Span]:
        parent = _current_span.get()
        sp = Span(span_id=uuid.uuid4().hex[:8], trace_id=trace_id or (parent.trace_id if parent else uuid.uuid4().hex[:8]),
                  parent_id=parent.span_id if parent else None, name=name, start=time.perf_counter(), attrs=dict(attrs))
        token = _current_span.set(sp)
        try:
            yield sp
        except Exception as exc:
            sp.attrs["error"] = type(exc).__name__
            raise
        finally:
            sp.end = time.perf_counter()
            _current_span.reset(token)
            self.spans.append(sp)

    def percentile(self, name: str, p: float) -> float:
        xs = sorted(s.ms for s in self.spans if s.name == name)
        return xs[min(len(xs) - 1, math.ceil(p / 100 * len(xs)) - 1)] if xs else 0.0


class Provider(Protocol):
    name: str
    in_price: float   # USD per 1M input tokens
    out_price: float

    async def complete(self, prompt: str) -> tuple[str, Usage]: ...


class SimProvider:
    def __init__(self, name: str, in_price: float, out_price: float, latency_s: float, fail_rate: float = 0.0, seed: int = 1) -> None:
        self.name, self.in_price, self.out_price = name, in_price, out_price
        self.latency_s, self.fail_rate, self._rng = latency_s, fail_rate, random.Random(seed)

    async def complete(self, prompt: str) -> tuple[str, Usage]:
        await asyncio.sleep(self.latency_s)
        if self._rng.random() < self.fail_rate:
            raise ProviderError(f"{self.name}: 503")
        text = f"[{self.name}] answer: " + " ".join(prompt.split()[:8])
        return text, Usage(input_tokens=max(1, len(prompt) // 4), output_tokens=max(1, len(text) // 4))


class ProviderStats:
    """Live per-provider numbers that routing policies read: latency EMA, in-flight calls, spend, call count."""

    def __init__(self) -> None:
        self.latency_ema_ms, self.inflight, self.calls, self.cost_usd = 0.0, 0, 0, 0.0

    def record(self, latency_ms: float, cost: float) -> None:
        self.latency_ema_ms = latency_ms if self.calls == 0 else 0.8 * self.latency_ema_ms + 0.2 * latency_ms
        self.calls += 1
        self.cost_usd += cost


class Gateway:
    def __init__(self, providers: list[SimProvider], tracer: Tracer, daily_budget_usd: dict[str, float], attempt_timeout_s: float = 0.2,
                 breaker_threshold: int = 3, breaker_cooldown_s: float = 0.5, limiter: RateLimiter | None = None,
                 concurrency: ConcurrencyLimiter | None = None, policy: RoutingPolicy | None = None, attempts_per_provider: int = 2) -> None:
        self.providers, self.tracer = providers, tracer
        self.budget, self.spend = daily_budget_usd, {k: 0.0 for k in daily_budget_usd}
        self.timeout, self.cache = attempt_timeout_s, {}
        self._fails = {p.name: 0 for p in providers}
        self._open_until = {p.name: 0.0 for p in providers}
        self._threshold, self._cooldown = breaker_threshold, breaker_cooldown_s
        self.fallbacks = 0
        self.requests = 0
        self.limiter, self.concurrency, self.policy, self.attempts = limiter, concurrency, policy, attempts_per_provider
        self.stats = {p.name: ProviderStats() for p in providers}

    @staticmethod
    def _cost(p: SimProvider, u: Usage) -> float:
        return (u.input_tokens * p.in_price + u.output_tokens * p.out_price) / 1_000_000

    async def chat(self, tenant: str, prompt: str) -> Completion:
        self.requests += 1
        if self.spend.get(tenant, 0.0) >= self.budget.get(tenant, 0.0):
            raise BudgetExceeded(f"tenant {tenant} reached daily budget ${self.budget.get(tenant, 0.0):.4f}")
        if self.limiter is not None and not self.limiter.allow(tenant, time.monotonic()):
            raise RateLimited(f"tenant {tenant} exceeded its rate limit")
        if self.concurrency is not None and not self.concurrency.try_acquire(tenant):
            raise RateLimited(f"tenant {tenant} exceeded its concurrency limit")
        try:
            return await self._serve(tenant, prompt)
        finally:
            if self.concurrency is not None:
                self.concurrency.release(tenant)

    async def _serve(self, tenant: str, prompt: str) -> Completion:
        key = hashlib.sha256(f"{tenant}:{prompt}".encode()).hexdigest()
        async with self.tracer.span("gateway.chat", tenant=tenant) as root:
            if key in self.cache:
                root.attrs.update(cached=True, cost_usd=0.0)
                return self.cache[key].model_copy(update={"cached": True, "cost_usd": 0.0, "latency_ms": 0.1})
            ordered = self.policy.order(self.providers, self.stats) if self.policy else self.providers
            for rank, p in enumerate(ordered):
                if time.monotonic() < self._open_until[p.name]:
                    continue  # breaker open: skip provider entirely
                for attempt in range(1, self.attempts + 1):
                    t0 = time.perf_counter()
                    self.stats[p.name].inflight += 1
                    try:
                        async with self.tracer.span("llm.call", provider=p.name, attempt=attempt) as sp:
                            text, usage = await asyncio.wait_for(p.complete(prompt), self.timeout)
                            cost = self._cost(p, usage)
                            sp.attrs.update(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cost_usd=round(cost, 8))
                        self._fails[p.name] = 0
                        self.spend[tenant] += cost
                        comp = Completion(text=text, usage=usage, provider=p.name, cost_usd=cost,
                                          latency_ms=(time.perf_counter() - t0) * 1000, fallback_used=rank > 0)
                        self.stats[p.name].record(comp.latency_ms, cost)
                        self.fallbacks += rank > 0
                        root.attrs.update(provider=p.name, fallback=rank > 0, cost_usd=round(cost, 8))
                        self.cache[key] = comp
                        return comp
                    except (ProviderError, asyncio.TimeoutError):
                        self._fails[p.name] += 1
                        if self._fails[p.name] >= self._threshold:
                            self._open_until[p.name] = time.monotonic() + self._cooldown
                            log.warning("breaker opened", extra={"ctx": {"provider": p.name}})
                            break
                        await asyncio.sleep(random.uniform(0, 0.005 * 2 ** attempt))
                    finally:
                        self.stats[p.name].inflight -= 1
            raise AllProvidersFailed("no provider could serve the request")


# ---- RAGAS-style metrics (lexical, deterministic) ------------------------------------------------------
STOP = set("the a an of to in is are was were and or for on with by as at from that this it be".split())


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def faithfulness(answer: str, contexts: list[str]) -> float:
    """Fraction of answer sentences whose content words are mostly covered by the retrieved context."""
    ctx = content_words(" ".join(contexts))
    sents = sentences(answer)
    if not sents:
        return 0.0
    supported = sum(1 for s in sents if (cw := content_words(s)) and len(cw & ctx) / len(cw) >= 0.7)
    return supported / len(sents)


def answer_relevancy(question: str, answer: str) -> float:
    q, a = content_words(question), content_words(answer)
    return len(q & a) / len(q) if q else 0.0


def context_precision(question: str, contexts: list[str], relevant_ids: set[int]) -> float:
    """Rank-aware precision@k average over positions of relevant chunks (higher when relevant chunks come first)."""
    hits, score = 0, 0.0
    for i, _ in enumerate(contexts, 1):
        if (i - 1) in relevant_ids:
            hits += 1
            score += hits / i
    return score / len(relevant_ids) if relevant_ids else 0.0


class EvalCase(BaseModel):
    question: str
    contexts: list[str]
    relevant_ids: set[int]
    answer: str


class EvalReport(BaseModel):
    faithfulness: float
    answer_relevancy: float
    context_precision: float


def run_eval(cases: list[EvalCase]) -> EvalReport:
    n = len(cases)
    return EvalReport(faithfulness=round(sum(faithfulness(c.answer, c.contexts) for c in cases) / n, 3),
                      answer_relevancy=round(sum(answer_relevancy(c.question, c.answer) for c in cases) / n, 3),
                      context_precision=round(sum(context_precision(c.question, c.contexts, c.relevant_ids) for c in cases) / n, 3))


def gate(candidate: EvalReport, baseline: EvalReport, max_drop: float = 0.03) -> None:
    for metric in EvalReport.model_fields:
        drop = getattr(baseline, metric) - getattr(candidate, metric)
        if drop > max_drop:
            raise EvalRegression(f"{metric} dropped {drop:.3f} (> {max_drop}) vs baseline")


# ---- Syllabus extension: shared utilities ---------------------------------------------------------------
T = TypeVar("T")


class ConfigError(GatewayError):
    pass


class AuthError(GatewayError):
    pass


class IdempotencyConflict(GatewayError):
    """Same idempotency key re-used with a different payload: a client bug, never silently replayed."""


class ToolTransientError(GatewayError):
    """Retryable tool failure, e.g. a lost acknowledgement."""


class JudgeFormatError(GatewayError):
    pass


class OptionalDependencyMissing(GatewayError):
    def __init__(self, package: str, hint: str) -> None:
        super().__init__(f"optional dependency '{package}' is not installed: {hint}")
        self.package = package


class ExporterUnsupported(GatewayError):
    pass


async def with_backoff(op: Callable[[], Awaitable[T]], retryable: tuple[type[BaseException], ...], attempts: int = 4,
                       base_s: float = 0.002, cap_s: float = 0.05) -> T:
    """Exponential backoff with full jitter around any I/O call."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await op()
        except retryable as exc:
            last = exc
            if i < attempts - 1:
                await asyncio.sleep(random.uniform(0, min(cap_s, base_s * 2 ** i)))
    raise last if last is not None else RuntimeError("with_backoff called with attempts < 1")


def percentile(values: list[float], p: float) -> float:
    xs = sorted(values)
    return xs[min(len(xs) - 1, max(0, math.ceil(p / 100 * len(xs)) - 1))] if xs else 0.0


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class HashedEmbedder:
    """Offline stand-in for an embedding model: hashed word unigrams plus char trigrams, L2-normalised."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        toks = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP]
        joined = " ".join(toks)
        vec = [0.0] * self.dim
        for g, weight in [(w, 2.0) for w in toks] + [(joined[i:i + 3], 1.0) for i in range(max(0, len(joined) - 2))]:
            vec[int(hashlib.md5(g.encode()).hexdigest()[:8], 16) % self.dim] += weight
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ---- Syllabus extension: idempotency keys for safe tool execution ----------------------------------------
def derive_idempotency_key(scope: str, tool: str, args: dict[str, object]) -> str:
    """Deterministic key from (who/what scope, tool, canonical args). Use when the caller cannot supply a key; prefer a client-chosen key per user intent."""
    return hashlib.sha256(f"{scope}|{tool}|{canonical_json(args)}".encode()).hexdigest()[:32]


class IdempotencyState(str, Enum):
    IN_FLIGHT = "in_flight"
    DONE = "done"


class IdempotencyRecord(BaseModel):
    key: str
    payload_hash: str
    state: IdempotencyState
    result: object | None = None
    expires_at: float


class IdempotencyStore:
    """Per-key exactly-once-ish execution: replay stored results, join in-flight duplicates, reject payload mismatches, expire with TTL.
    Failed executions are forgotten so a retry with the same key can run again. In production this is Redis SET NX with a TTL."""

    def __init__(self, ttl_s: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s, self.clock = ttl_s, clock
        self._records: dict[str, IdempotencyRecord] = {}
        self._waiters: dict[str, asyncio.Future[object]] = {}

    async def execute(self, key: str, payload: dict[str, object], fn: Callable[[], Awaitable[T]]) -> tuple[T, str]:
        now = self.clock()
        for k in [k for k, r in self._records.items() if r.state is IdempotencyState.DONE and r.expires_at <= now]:
            del self._records[k]
        phash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        rec = self._records.get(key)
        if rec is not None:
            if rec.payload_hash != phash:
                raise IdempotencyConflict(f"key {key[:8]} was first used with a different payload")
            if rec.state is IdempotencyState.DONE:
                return rec.result, "replayed"  # type: ignore[return-value]
            return await self._waiters[key], "joined"  # type: ignore[return-value]
        self._records[key] = IdempotencyRecord(key=key, payload_hash=phash, state=IdempotencyState.IN_FLIGHT, expires_at=now + self.ttl_s)
        fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._waiters[key] = fut
        try:
            result = await fn()
        except BaseException as exc:
            self._records.pop(key, None)
            self._waiters.pop(key, None)
            if isinstance(exc, Exception):
                fut.set_exception(exc)
                fut.exception()  # mark retrieved so un-joined failures do not warn
            else:
                fut.cancel()
            raise
        self._records[key] = IdempotencyRecord(key=key, payload_hash=phash, state=IdempotencyState.DONE, result=result, expires_at=self.clock() + self.ttl_s)
        self._waiters.pop(key, None)
        fut.set_result(result)
        return result, "executed"


class FakeEmailService:
    """Downstream with a lost-acknowledgement fault: it performs the send, then the first reply times out.
    Honours an Idempotency-Key the way payment and messaging APIs do, so a retry with the same key does not send twice."""

    def __init__(self, honour_keys: bool) -> None:
        self.honour_keys, self.sent, self._by_key, self._lost_once = honour_keys, [], {}, False

    async def send(self, to: str, idempotency_key: str | None) -> str:
        await asyncio.sleep(0.001)
        if self.honour_keys and idempotency_key in self._by_key:
            msg_id = self._by_key[idempotency_key]
        else:
            msg_id = f"msg-{len(self.sent) + 1}"
            self.sent.append((to, msg_id))
            if idempotency_key:
                self._by_key[idempotency_key] = msg_id
        if not self._lost_once:
            self._lost_once = True
            raise ToolTransientError("timeout after send (acknowledgement lost)")
        return msg_id


# ---- Syllabus extension: exponential backoff strategies and a retry-storm simulator -----------------------
class BackoffStrategy(Protocol):
    name: str

    def next_delay(self, attempt: int, prev_delay: float, rng: random.Random) -> float: ...


class ExponentialCapped:
    name = "capped-no-jitter"

    def __init__(self, base: float = 0.05, cap: float = 2.0) -> None:
        self.base, self.cap = base, cap

    def next_delay(self, attempt: int, prev_delay: float, rng: random.Random) -> float:
        return min(self.cap, self.base * 2 ** attempt)


class FullJitter(ExponentialCapped):
    name = "full-jitter"

    def next_delay(self, attempt: int, prev_delay: float, rng: random.Random) -> float:
        return rng.uniform(0, super().next_delay(attempt, prev_delay, rng))


class EqualJitter(ExponentialCapped):
    name = "equal-jitter"

    def next_delay(self, attempt: int, prev_delay: float, rng: random.Random) -> float:
        half = super().next_delay(attempt, prev_delay, rng) / 2
        return half + rng.uniform(0, half)


class DecorrelatedJitter(ExponentialCapped):
    name = "decorrelated-jitter"

    def next_delay(self, attempt: int, prev_delay: float, rng: random.Random) -> float:
        return min(self.cap, rng.uniform(self.base, max(self.base, prev_delay * 3)))


async def retry_with_strategy(op: Callable[[], Awaitable[T]], strategy: BackoffStrategy, retryable: tuple[type[BaseException], ...], max_attempts: int = 5,
                              rng: random.Random | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> T:
    rng, prev = rng or random.Random(), 0.0
    for attempt in range(max_attempts):
        try:
            return await op()
        except retryable:
            if attempt == max_attempts - 1:
                raise
            prev = strategy.next_delay(attempt, prev, rng)
            await sleep(prev)
    raise RuntimeError("max_attempts must be >= 1")


class StormResult(BaseModel):
    strategy: str
    total_attempts: int
    attempts_per_client: float
    peak_post_outage_attempts_per_bucket: int
    success_rate: float
    p95_completion_s: float


def simulate_retry_storm(strategy: BackoffStrategy, clients: int = 300, outage_s: float = 1.0, capacity_per_bucket: int = 20, bucket_s: float = 0.1,
                         max_attempts: int = 10, seed: int = 7) -> StormResult:
    """Discrete-event simulation (no real sleeping): clients hit a service that is down for outage_s, then admits capacity_per_bucket calls per bucket."""
    rng = random.Random(seed)
    events: list[tuple[float, int, int, float]] = [(rng.uniform(0, 0.2), c, 0, 0.0) for c in range(clients)]
    heapq.heapify(events)
    starts = {c: t for t, c, _, _ in events}
    loads: dict[int, int] = defaultdict(int)
    done: dict[int, float] = {}
    attempts = 0
    while events:
        t, c, attempt, prev = heapq.heappop(events)
        attempts += 1
        bucket = int(t / bucket_s)
        loads[bucket] += 1
        if t >= outage_s and loads[bucket] <= capacity_per_bucket:
            done[c] = t - starts[c]
            continue
        if attempt + 1 < max_attempts:
            delay = strategy.next_delay(attempt, prev, rng)
            heapq.heappush(events, (t + delay, c, attempt + 1, delay))
    return StormResult(strategy=strategy.name, total_attempts=attempts, attempts_per_client=round(attempts / clients, 2), peak_post_outage_attempts_per_bucket=max((v for b, v in loads.items() if b * bucket_s >= outage_s), default=0),
                       success_rate=round(len(done) / clients, 3), p95_completion_s=round(percentile(list(done.values()), 95), 2))


# ---- Syllabus extension: LiteLLM-style gateway config, virtual keys, and a lazy litellm adapter ----------
def _scalar(tok: str) -> object:
    tok = tok.strip()
    if tok[:1] in "[{":
        try:
            return json.loads(tok)
        except ValueError as exc:
            raise ConfigError(f"flow value must be valid JSON (double quotes): {tok}") from exc
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    low = tok.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    for cast in (int, float):
        try:
            return cast(tok)
        except ValueError:
            continue
    return tok


_KEY_LINE = re.compile(r"^([\w.\-]+):(?:\s+(.*))?$")


def parse_yamlish(text: str) -> object:
    """Parses the small YAML subset gateway configs use: nested maps, '- ' lists (of maps or scalars), scalars, inline JSON [..] / {..}.
    Use PyYAML for anything richer; this keeps the lab dependency-free."""
    lines = [[len(raw) - len(raw.lstrip(" ")), raw.strip()] for raw in text.splitlines() if raw.strip() and not raw.strip().startswith("#")]
    pos = 0

    def block(indent: int) -> object:
        return seq(indent) if lines[pos][1].startswith("- ") else mapping(indent)

    def seq(indent: int) -> list[object]:
        nonlocal pos
        out: list[object] = []
        while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("- "):
            item = lines[pos][1][2:].strip()
            if _KEY_LINE.match(item):
                lines[pos] = [indent + 2, item]  # re-read the item as the first line of a nested map
                out.append(mapping(indent + 2))
            else:
                out.append(_scalar(item))
                pos += 1
        return out

    def mapping(indent: int) -> dict[str, object]:
        nonlocal pos
        out: dict[str, object] = {}
        while pos < len(lines) and lines[pos][0] == indent and not lines[pos][1].startswith("- "):
            m = _KEY_LINE.match(lines[pos][1])
            if not m:
                raise ConfigError(f"cannot parse line: {lines[pos][1]}")
            key, rest = m.group(1), (m.group(2) or "").strip()
            pos += 1
            if rest:
                out[key] = _scalar(rest)
            elif pos < len(lines) and lines[pos][0] > indent:
                out[key] = block(lines[pos][0])
            else:
                out[key] = None
        return out

    if not lines:
        return {}
    result = block(lines[0][0])
    if pos != len(lines):
        raise ConfigError(f"unexpected indentation near: {lines[pos][1]}")
    return result


class LitellmParams(BaseModel):
    model: str
    api_base: str | None = None
    api_key: str | None = None  # a reference like os.environ/OPENAI_API_KEY, never the secret itself
    rpm: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)


class ModelEntry(BaseModel):
    model_name: str  # the alias applications call
    litellm_params: LitellmParams


class RouterSettings(BaseModel):
    routing_strategy: Literal["simple-shuffle", "least-busy", "latency-based-routing", "usage-based-routing"] = "simple-shuffle"
    num_retries: int = Field(default=2, ge=0)
    timeout: float = Field(default=30.0, gt=0)
    fallbacks: list[dict[str, list[str]]] = Field(default_factory=list)


class GatewayConfig(BaseModel):
    model_list: list[ModelEntry]
    router_settings: RouterSettings = Field(default_factory=RouterSettings)

    @model_validator(mode="after")
    def _fallbacks_reference_known_aliases(self) -> GatewayConfig:
        aliases = {m.model_name for m in self.model_list}
        for fb in self.router_settings.fallbacks:
            for src, targets in fb.items():
                missing = ({src} | set(targets)) - aliases
                if missing:
                    raise ValueError(f"fallback references unknown alias {sorted(missing)}")
        return self


def resolve_secret(ref: str | None, env: Mapping[str, str]) -> str | None:
    """Resolve 'os.environ/NAME' against env. Inline secrets are rejected so keys never live in config files or git."""
    if ref is None:
        return None
    if not ref.startswith("os.environ/"):
        raise ConfigError("api_key must be an os.environ/NAME reference, not an inline secret")
    name = ref.split("/", 1)[1]
    if name not in env:
        raise ConfigError(f"environment variable {name} is not set")
    return env[name]


def load_gateway_config(text: str, env: Mapping[str, str] | None = None) -> GatewayConfig:
    raw = parse_yamlish(text)
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    try:
        cfg = GatewayConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid gateway config: {exc.error_count()} errors; first: {exc.errors()[0]['msg']}") from exc
    for m in cfg.model_list:
        resolve_secret(m.litellm_params.api_key, env if env is not None else os.environ)  # fail at load time, not at the first request
    return cfg


GATEWAY_CONFIG_TEXT = """
model_list:
  - model_name: chat-prod
    litellm_params:
      model: vendor-a/large-model
      api_key: os.environ/VENDOR_A_API_KEY
      rpm: 600
  - model_name: chat-prod
    litellm_params:
      model: vendor-b/large-model
      api_base: https://eu.vendor-b.example/v1
      api_key: os.environ/VENDOR_B_API_KEY
      rpm: 300
  - model_name: chat-backup
    litellm_params:
      model: selfhost/small-model
      api_base: http://vllm.internal:8000/v1
router_settings:
  routing_strategy: latency-based-routing
  num_retries: 1
  timeout: 10
  fallbacks: [{"chat-prod": ["chat-backup"]}]
"""


class VirtualKey(BaseModel):
    key_id: str
    team: str
    allowed_models: set[str]
    rpm: int
    token_sha256: str
    revoked: bool = False


class VirtualKeyStore:
    """Portkey/LiteLLM-style virtual keys: apps hold a revocable team token; provider keys stay server-side (env refs) and never reach apps."""

    def __init__(self) -> None:
        self._keys: dict[str, VirtualKey] = {}

    def issue(self, team: str, allowed_models: set[str], rpm: int) -> str:
        token = "sk-vk-" + uuid.uuid4().hex
        key_id = uuid.uuid4().hex[:8]
        self._keys[hashlib.sha256(token.encode()).hexdigest()] = VirtualKey(key_id=key_id, team=team, allowed_models=allowed_models, rpm=rpm,
                                                                             token_sha256=hashlib.sha256(token.encode()).hexdigest())
        return token  # shown once; only the hash is stored

    def authorize(self, token: str, model: str) -> VirtualKey:
        vk = self._keys.get(hashlib.sha256(token.encode()).hexdigest())
        if vk is None or vk.revoked:
            raise AuthError("unknown or revoked key")
        if model not in vk.allowed_models:
            raise AuthError(f"team {vk.team} may not call {model}")
        return vk

    def revoke(self, key_id: str) -> None:
        for vk in self._keys.values():
            if vk.key_id == key_id:
                vk.revoked = True


def lazy_litellm_router(cfg: GatewayConfig) -> object:
    """Build a real litellm.Router from the validated config when litellm is installed (the offline AliasRouter below is what the demo runs)."""
    try:
        from litellm import Router
    except ImportError as exc:
        raise OptionalDependencyMissing("litellm", "pip install litellm (verify Router arguments against the current LiteLLM docs)") from exc
    model_list = [{"model_name": m.model_name, "litellm_params": m.litellm_params.model_dump(exclude_none=True)} for m in cfg.model_list]
    rs = cfg.router_settings
    return Router(model_list=model_list, routing_strategy=rs.routing_strategy, num_retries=rs.num_retries, timeout=rs.timeout, fallbacks=rs.fallbacks)


async def litellm_complete(router: object, alias: str, prompt: str) -> str:
    async def op() -> str:
        res = await router.acompletion(model=alias, messages=[{"role": "user", "content": prompt}])  # type: ignore[attr-defined]
        return str(res.choices[0].message.content)

    return await with_backoff(op, (ConnectionError, TimeoutError, OSError))


# ---- Syllabus extension: rate-limiting algorithms ---------------------------------------------------------
class RateLimiter(Protocol):
    def allow(self, key: str, now: float, cost: float = 1.0) -> bool: ...


class TokenBucket:
    """Allows bursts up to capacity and a steady rate_per_s afterwards; cost lets one bucket meter tokens-per-minute instead of requests."""

    def __init__(self, rate_per_s: float, capacity: float) -> None:
        self.rate, self.capacity = rate_per_s, capacity
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, now: float, cost: float = 1.0) -> bool:
        tokens, last = self._state.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        ok = tokens >= cost
        self._state[key] = (tokens - cost if ok else tokens, now)
        return ok


class FixedWindow:
    """Cheapest, but lets 2x the limit through across a window boundary (shown for comparison)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._counts: dict[tuple[str, int], float] = defaultdict(float)

    def allow(self, key: str, now: float, cost: float = 1.0) -> bool:
        slot = (key, int(now // self.window_s))
        if self._counts[slot] + cost > self.limit:
            return False
        self._counts[slot] += cost
        return True


class SlidingWindowLog:
    """Exact: stores every admitted timestamp. Memory is O(limit) per key."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._log: dict[str, deque[tuple[float, float]]] = defaultdict(deque)

    def allow(self, key: str, now: float, cost: float = 1.0) -> bool:
        q = self._log[key]
        while q and q[0][0] <= now - self.window_s:
            q.popleft()
        if sum(c for _, c in q) + cost > self.limit:
            return False
        q.append((now, cost))
        return True


class SlidingWindowCounter:
    """Approximation: weights the previous fixed window by how much of it still overlaps the sliding window. O(1) memory per key."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._counts: dict[str, dict[int, float]] = defaultdict(dict)

    def allow(self, key: str, now: float, cost: float = 1.0) -> bool:
        slot, frac = int(now // self.window_s), (now % self.window_s) / self.window_s
        c = self._counts[key]
        for old in [s for s in c if s < slot - 1]:
            del c[old]
        estimate = c.get(slot - 1, 0.0) * (1 - frac) + c.get(slot, 0.0)
        if estimate + cost > self.limit:
            return False
        c[slot] = c.get(slot, 0.0) + cost
        return True


class ConcurrencyLimiter:
    """Caps in-flight requests per key (protects slow providers and streaming connections); pair try_acquire with release in a finally block."""

    def __init__(self, max_in_flight: int) -> None:
        self.max_in_flight = max_in_flight
        self._in_flight: dict[str, int] = defaultdict(int)

    def try_acquire(self, key: str) -> bool:
        if self._in_flight[key] >= self.max_in_flight:
            return False
        self._in_flight[key] += 1
        return True

    def release(self, key: str) -> None:
        self._in_flight[key] = max(0, self._in_flight[key] - 1)


def compare_limiters(timestamps: list[float], limit: int = 10, window_s: float = 1.0) -> dict[str, int]:
    makers: dict[str, RateLimiter] = {"fixed-window": FixedWindow(limit, window_s), "sliding-log": SlidingWindowLog(limit, window_s),
                                      "sliding-counter": SlidingWindowCounter(limit, window_s), "token-bucket": TokenBucket(limit / window_s, limit)}
    return {name: sum(lim.allow("k", t) for t in timestamps) for name, lim in makers.items()}


# ---- Syllabus extension: fallback routing policies (plug into Gateway) -----------------------------------
class RoutingPolicy(Protocol):
    name: str

    def order(self, providers: list[SimProvider], stats: dict[str, ProviderStats]) -> list[SimProvider]: ...


class PriorityPolicy:
    name = "priority"

    def order(self, providers: list[SimProvider], stats: dict[str, ProviderStats]) -> list[SimProvider]:
        return list(providers)


class LatencyPolicy:
    """Fastest recent provider first; unmeasured providers sort first so they get explored."""
    name = "latency-based"

    def order(self, providers: list[SimProvider], stats: dict[str, ProviderStats]) -> list[SimProvider]:
        return sorted(providers, key=lambda p: stats[p.name].latency_ema_ms)


class CostPolicy:
    """Cheapest blended price first (80% input / 20% output weighting)."""
    name = "cost-based"

    def order(self, providers: list[SimProvider], stats: dict[str, ProviderStats]) -> list[SimProvider]:
        return sorted(providers, key=lambda p: 0.8 * p.in_price + 0.2 * p.out_price)


class LeastBusyPolicy:
    name = "least-busy"

    def order(self, providers: list[SimProvider], stats: dict[str, ProviderStats]) -> list[SimProvider]:
        return sorted(providers, key=lambda p: (stats[p.name].inflight, stats[p.name].calls))


class LimitedProvider:
    """Wraps a provider with a per-deployment rpm bucket; an exhausted bucket behaves like a provider 429 so the gateway falls back."""

    def __init__(self, inner: SimProvider, rpm: int | None) -> None:
        self.inner, self.name, self.in_price, self.out_price = inner, inner.name, inner.in_price, inner.out_price
        self._bucket = TokenBucket(rpm / 60.0, max(1.0, rpm / 60.0 * 5)) if rpm else None

    async def complete(self, prompt: str) -> tuple[str, Usage]:
        if self._bucket is not None and not self._bucket.allow("deployment", time.monotonic()):
            raise ProviderError(f"{self.name}: 429 rpm limit")
        return await self.inner.complete(prompt)


class AliasRouter:
    """Offline LiteLLM-Router-like: aliases map to deployments, routing strategy orders them, rpm limits become 429s, fallbacks hop aliases."""

    POLICIES: dict[str, RoutingPolicy] = {"simple-shuffle": PriorityPolicy(), "least-busy": LeastBusyPolicy(), "latency-based-routing": LatencyPolicy(),
                                          "usage-based-routing": CostPolicy()}

    def __init__(self, cfg: GatewayConfig, tracer: Tracer, sim: dict[str, tuple[float, float]], prices: dict[str, tuple[float, float]]) -> None:
        self.cfg, self.fallbacks = cfg, {src: targets for fb in cfg.router_settings.fallbacks for src, targets in fb.items()}
        self.gateways: dict[str, Gateway] = {}
        for alias in {m.model_name for m in cfg.model_list}:
            provs = []
            for m in [e for e in cfg.model_list if e.model_name == alias]:
                latency, fail = sim.get(m.litellm_params.model, (0.01, 0.0))
                pin, pout = prices.get(m.litellm_params.model, (1.0, 3.0))
                provs.append(LimitedProvider(SimProvider(m.litellm_params.model, pin, pout, latency, fail, seed=len(provs) + 5), m.litellm_params.rpm))
            self.gateways[alias] = Gateway(provs, tracer, {"*": 1e9}, attempt_timeout_s=cfg.router_settings.timeout, policy=self.POLICIES[cfg.router_settings.routing_strategy],
                                           attempts_per_provider=cfg.router_settings.num_retries + 1)  # type: ignore[arg-type]

    async def chat(self, alias: str, prompt: str, tenant: str = "*") -> Completion:
        if alias not in self.gateways:
            raise ConfigError(f"unknown model alias {alias}")
        for hop in [alias, *self.fallbacks.get(alias, [])]:
            try:
                comp = await self.gateways[hop].chat(tenant, prompt)
                return comp.model_copy(update={"fallback_used": comp.fallback_used or hop != alias})
            except AllProvidersFailed:
                log.info("alias exhausted, trying fallback", extra={"ctx": {"alias": hop}})
        raise AllProvidersFailed(f"alias {alias} and its fallbacks all failed")


# ---- Syllabus extension: LLM-as-judge scoring pipeline -----------------------------------------------------
class Criterion(BaseModel):
    name: str
    description: str
    anchors: dict[int, str]


class Rubric(BaseModel):
    name: str
    criteria: list[Criterion]


QA_RUBRIC = Rubric(name="qa-v1", criteria=[
    Criterion(name="groundedness", description="Every claim is supported by the context.",
              anchors={1: "mostly unsupported or contradicts the context", 3: "some claims unsupported", 5: "every claim supported"}),
    Criterion(name="relevance", description="The answer addresses the question asked.",
              anchors={1: "off topic", 3: "partly answers", 5: "directly and fully answers"})])


def render_pointwise_prompt(rubric: Rubric, question: str, context: str, answer: str) -> str:
    crit = "\n".join(f"- {c.name}: {c.description} Anchors: " + "; ".join(f"{k}={v}" for k, v in sorted(c.anchors.items())) for c in rubric.criteria)
    keys = ", ".join(f'"{c.name}": <1-5>' for c in rubric.criteria)
    return (f"You are a strict evaluator. Rubric {rubric.name}:\n{crit}\n\nQUESTION: {question}\nCONTEXT: {context}\nANSWER: {answer}\n\n"
            f'Reply with JSON only: {{"scores": {{{keys}}}, "rationale": "<one sentence>"}}')


def render_pairwise_prompt(rubric: Rubric, question: str, context: str, answer_a: str, answer_b: str) -> str:
    crit = "; ".join(c.name + ": " + c.description for c in rubric.criteria)
    return (f"You are a strict evaluator. Decide which answer is better on: {crit}. Ignore answer order and length.\n\nQUESTION: {question}\nCONTEXT: {context}\n"
            f'ANSWER_A: {answer_a}\nANSWER_B: {answer_b}\n\nReply with JSON only: {{"winner": "A"|"B"|"tie", "rationale": "<one sentence>"}}')


class PointwiseVerdict(BaseModel):
    scores: dict[str, int]
    rationale: str = ""

    @field_validator("scores")
    @classmethod
    def _in_range(cls, v: dict[str, int]) -> dict[str, int]:
        if not v or any(not 1 <= s <= 5 for s in v.values()):
            raise ValueError("scores must be integers in 1..5")
        return v

    @property
    def mean(self) -> float:
        return sum(self.scores.values()) / len(self.scores)


class PairwiseVerdict(BaseModel):
    winner: Literal["A", "B", "tie"]
    rationale: str = ""


def parse_json_verdict(raw: str, model: type[T]) -> T:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise JudgeFormatError("no JSON object in judge output")
    try:
        return model.model_validate(json.loads(raw[start:end + 1]))  # type: ignore[attr-defined,no-any-return]
    except (ValueError, ValidationError) as exc:
        raise JudgeFormatError(f"invalid verdict: {exc}") from exc


class Judge(Protocol):
    async def complete(self, prompt: str) -> str: ...


class DeterministicJudge:
    """Offline judge: groundedness from context coverage, relevance from question overlap. position_bias adds a bonus to answer A to mimic a real bias."""

    def __init__(self, position_bias: float = 0.0, margin: float = 0.25) -> None:
        self.position_bias, self.margin = position_bias, margin

    @staticmethod
    def _field(prompt: str, name: str) -> str:
        m = re.search(rf"^{name}: (.*)$", prompt, re.M)
        return m.group(1) if m else ""

    def _score(self, q: str, ctx: str, ans: str) -> dict[str, int]:
        cw = content_words(ans)
        grounded = len(cw & content_words(ctx)) / len(cw) if cw else 0.0
        qw = content_words(q)
        relevant = len(qw & cw) / len(qw) if qw else 0.0
        return {"groundedness": 1 + round(4 * grounded), "relevance": 1 + round(4 * relevant)}

    async def complete(self, prompt: str) -> str:
        await asyncio.sleep(0)
        q, ctx = self._field(prompt, "QUESTION"), self._field(prompt, "CONTEXT")
        if "ANSWER_A:" in prompt:
            ga, gb = self._ground(q, ctx, self._field(prompt, "ANSWER_A")) + self.position_bias, self._ground(q, ctx, self._field(prompt, "ANSWER_B"))
            winner = "A" if ga - gb > self.margin else ("B" if gb - ga > self.margin else "tie")
            return json.dumps({"winner": winner, "rationale": f"A={ga:.2f} B={gb:.2f}"})
        scores = self._score(q, ctx, self._field(prompt, "ANSWER"))
        return json.dumps({"scores": scores, "rationale": "lexical coverage"})

    def _ground(self, q: str, ctx: str, ans: str) -> float:
        """Unrounded overall score for pairwise comparison; groundedness is weighted double."""
        cw = content_words(ans)
        grounded = len(cw & content_words(ctx)) / len(cw) if cw else 0.0
        qw = content_words(q)
        relevant = len(qw & cw) / len(qw) if qw else 0.0
        return 2 * (1 + 4 * grounded) + (1 + 4 * relevant)


class FormatFlakyJudge:
    """Wraps a judge and returns prose instead of JSON for the first n calls, to exercise the validate-and-retry path."""

    def __init__(self, inner: Judge, fail_first: int = 1) -> None:
        self.inner, self._left = inner, fail_first

    async def complete(self, prompt: str) -> str:
        if self._left > 0:
            self._left -= 1
            return "Sure! I think the answer is quite good overall."
        return await self.inner.complete(prompt)


class AnthropicJudge:
    """Lazy adapter for a real LLM judge via the anthropic SDK; the caller supplies the model id. Never runs in the offline demo."""

    def __init__(self, model: str, max_tokens: int = 300) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise OptionalDependencyMissing("anthropic", "pip install anthropic and set ANTHROPIC_API_KEY") from exc
        self._sdk, self._client, self.model, self.max_tokens = anthropic, anthropic.AsyncAnthropic(), model, max_tokens

    async def complete(self, prompt: str) -> str:
        async def op() -> str:
            msg = await self._client.messages.create(model=self.model, max_tokens=self.max_tokens, messages=[{"role": "user", "content": prompt}])
            return "".join(getattr(b, "text", "") for b in msg.content)

        return await with_backoff(op, (self._sdk.RateLimitError, self._sdk.APIConnectionError, self._sdk.InternalServerError))


async def _judged(judge: Judge, prompt: str, model: type[T], attempts: int = 3) -> T:
    for i in range(attempts):
        raw = await judge.complete(prompt if i == 0 else prompt + "\n\nYour previous reply was not valid JSON. Reply with the JSON object only.")
        try:
            return parse_json_verdict(raw, model)
        except JudgeFormatError:
            if i == attempts - 1:
                raise
            await asyncio.sleep(random.uniform(0, 0.002 * 2 ** i))
    raise JudgeFormatError("unreachable")


async def judge_pointwise(judge: Judge, question: str, context: str, answer: str, rubric: Rubric = QA_RUBRIC) -> PointwiseVerdict:
    return await _judged(judge, render_pointwise_prompt(rubric, question, context, answer), PointwiseVerdict)


class PairwiseResult(BaseModel):
    winner: Literal["A", "B", "tie"]
    consistent: bool
    raw: tuple[str, str]


async def judge_pairwise(judge: Judge, question: str, context: str, a: str, b: str, swap_check: bool = True, rubric: Rubric = QA_RUBRIC) -> PairwiseResult:
    """With swap_check, judge both orders and keep only verdicts that survive the swap; disagreement becomes a tie (position-bias mitigation)."""
    first = await _judged(judge, render_pairwise_prompt(rubric, question, context, a, b), PairwiseVerdict)
    if not swap_check:
        return PairwiseResult(winner=first.winner, consistent=True, raw=(first.winner, first.winner))
    second = await _judged(judge, render_pairwise_prompt(rubric, question, context, b, a), PairwiseVerdict)
    unswapped = {"A": "B", "B": "A", "tie": "tie"}[second.winner]
    agree = unswapped == first.winner
    return PairwiseResult(winner=first.winner if agree else "tie", consistent=agree, raw=(first.winner, unswapped))


def cohen_kappa(a: list[str], b: list[str]) -> float:
    """Chance-corrected agreement between two labelers: 1 is perfect, 0 is chance level."""
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in set(a) | set(b))
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


JUDGE_ITEMS = [
    ("What is the refund window for standard plans?", "Refunds are available within 30 days of purchase for standard plans.",
     "Standard plans can be refunded within 30 days of purchase.", "Standard plans can be refunded within 60 days of purchase."),
    ("How many seats does the Team plan include?", "The Team plan includes 25 seats and one admin.", "The Team plan includes 25 seats.", "The Team plan includes 50 seats."),
    ("Which plan offers single sign-on?", "Single sign-on is available on the Enterprise plan only.", "Single sign-on is available on the Enterprise plan only.",
     "Single sign-on is available on every plan."),
    ("What is the uptime commitment?", "The service level agreement commits to 99.9 percent monthly uptime.", "The agreement commits to 99.9 percent monthly uptime.",
     "The agreement commits to 99.99 percent monthly uptime."),
    ("How long are call recordings retained?", "Call recordings are retained for 90 days unless the customer requests deletion.",
     "Call recordings are retained for 90 days unless deletion is requested.", "Call recordings are retained for 365 days unless deletion is requested."),
    ("Which regions host customer data?", "Customer data is hosted in the US and EU regions.", "Customer data is hosted in the US and EU regions.",
     "Customer data is hosted in the US and APAC regions."),
]
JUDGE_TIES = [("What is the refund window for standard plans?", "Refunds are available within 30 days of purchase for standard plans.",
               "Standard plans can be refunded within 30 days of purchase.", "Refunds for standard plans are available within 30 days of purchase."),
              ("How many seats does the Team plan include?", "The Team plan includes 25 seats and one admin.", "The Team plan includes 25 seats.", "Team plan: 25 seats and one admin.")]


def build_judge_pairs() -> list[tuple[str, str, str, str, str]]:
    """(question, context, answer_a, answer_b, human_label): each item appears with the good answer in both positions, plus human-labelled ties."""
    pairs = []
    for q, ctx, good, bad in JUDGE_ITEMS:
        pairs += [(q, ctx, good, bad, "A"), (q, ctx, bad, good, "B")]
    return pairs + [(q, ctx, a, b, "tie") for q, ctx, a, b in JUDGE_TIES]


# ---- Syllabus extension: RAGAS-style metrics, fully defined (faithfulness, answer relevancy, context precision/recall) ----
# Real RAGAS uses an LLM to (a) split the answer into statements and judge each against the context (faithfulness), (b) generate
# questions from the answer and compare embeddings to the real question (answer relevancy), (c) judge each retrieved chunk's usefulness
# against the reference (context precision) and (d) attribute each reference sentence to the context (context recall). The lexical
# versions below keep the same definitions with deterministic stand-ins for the LLM calls. Verify details against the current RAGAS docs.
NONCOMMITTAL = re.compile(r"\b(i don't know|i do not know|not sure|cannot (find|answer)|no information|the documents do not)\b", re.I)
_NUM = re.compile(r"\d+(?:\.\d+)?")


def extract_claims(answer: str) -> list[str]:
    """Statement decomposition: sentences, then split on semicolons and ', and ' so one sentence can hold several checkable claims."""
    claims: list[str] = []
    for sent in sentences(answer):
        if NONCOMMITTAL.search(sent):
            continue  # an abstention makes no factual claim
        claims += [c.strip() for c in re.split(r";|, and ", sent) if len(content_words(c)) >= 2]
    return claims


def claim_supported(claim: str, contexts: list[str], min_cover: float = 0.75) -> bool:
    ctx_words, cw = content_words(" ".join(contexts)), content_words(claim)
    numbers_ok = set(_NUM.findall(claim)) <= set(_NUM.findall(" ".join(contexts)))  # an invented number is never supported
    return bool(cw) and numbers_ok and len(cw & ctx_words) / len(cw) >= min_cover


def ragas_faithfulness(answer: str, contexts: list[str]) -> float:
    """supported claims / total claims (1.0 when the answer makes no claims, e.g. an abstention)."""
    claims = extract_claims(answer)
    return sum(claim_supported(c, contexts) for c in claims) / len(claims) if claims else 1.0


class QuestionGenerator(Protocol):
    def generate(self, answer: str, n: int) -> list[str]: ...


class TemplateQuestionGenerator:
    """Deterministic stand-in for the LLM that writes questions the answer would be a good reply to."""

    def generate(self, answer: str, n: int = 3) -> list[str]:
        out = []
        for sent in sentences(answer)[:n]:
            terms = sorted(content_words(sent), key=lambda w: (-len(w), w))[:5]
            out.append("What is stated about " + " ".join(terms) + "?")
        return out


def ragas_answer_relevancy(question: str, answer: str, emb: HashedEmbedder, gen: QuestionGenerator | None = None, n: int = 3) -> float:
    """mean cosine(question, generated_question_i); noncommittal answers score 0 (RAGAS also zeroes them)."""
    if NONCOMMITTAL.search(answer):
        return 0.0
    qs = (gen or TemplateQuestionGenerator()).generate(answer, n)
    qv = emb.embed(question)
    return round(sum(max(0.0, cosine(qv, emb.embed(g))) for g in qs) / len(qs), 3) if qs else 0.0


def chunk_relevant(chunk: str, ground_truth: str, min_cover: float = 0.4) -> bool:
    gt = content_words(ground_truth)
    return bool(gt) and len(gt & content_words(chunk)) / len(gt) >= min_cover


def ragas_context_precision(contexts: list[str], ground_truth: str) -> float:
    """Average precision over ranks: mean of precision@k at every rank k holding a relevant chunk (relevant chunks ranked first score higher)."""
    hits, total = 0, 0.0
    for k, chunk in enumerate(contexts, 1):
        if chunk_relevant(chunk, ground_truth):
            hits += 1
            total += hits / k
    return round(total / hits, 3) if hits else 0.0


def ragas_context_recall(contexts: list[str], ground_truth: str) -> float:
    """Fraction of ground-truth sentences that can be attributed to the retrieved context."""
    gts = sentences(ground_truth)
    return sum(claim_supported(s, contexts, 0.6) for s in gts) / len(gts) if gts else 0.0


class RagasScores(BaseModel):
    faithfulness: float
    answer_relevancy: float
    context_precision: float
    context_recall: float


def ragas_scores(question: str, answer: str, contexts: list[str], ground_truth: str, emb: HashedEmbedder) -> RagasScores:
    return RagasScores(faithfulness=round(ragas_faithfulness(answer, contexts), 3), answer_relevancy=ragas_answer_relevancy(question, answer, emb),
                       context_precision=ragas_context_precision(contexts, ground_truth), context_recall=round(ragas_context_recall(contexts, ground_truth), 3))


# ---- Syllabus extension: synthetic benchmark datasets from source documents ---------------------------------
class SourceDoc(BaseModel):
    doc_id: str
    title: str
    text: str


class SyntheticCase(BaseModel):
    case_id: str
    question: str
    ground_truth: str
    qtype: Literal["factoid", "multi_hop", "negative", "unanswerable"]
    difficulty: Literal["easy", "medium", "hard"]
    gold_spans: list[tuple[str, int, int]]  # (doc_id, start, end) character offsets of the supporting sentences


SOURCE_DOCS = [
    SourceDoc(doc_id="refunds", title="Refund Policy", text="Refunds are available within 30 days of purchase for standard plans. Enterprise contracts follow the master services agreement. "
              "Approved refunds are issued to the original payment method within 10 business days. Annual plans are refunded pro rata after the first 30 days."),
    SourceDoc(doc_id="plans", title="Plans and Seats", text="The Team plan includes 25 seats and one admin. The Enterprise plan includes unlimited seats and single sign-on. "
              "Additional seats on the Team plan cost 12 USD per seat each month. Seats can be reassigned to another user at any time."),
    SourceDoc(doc_id="security", title="Security Overview", text="Customer data is hosted in the US and EU regions. All data is encrypted at rest with AES-256. "
              "Single sign-on is available on the Enterprise plan only. Penetration tests are performed twice per year by an external firm."),
    SourceDoc(doc_id="retention", title="Data Retention", text="Call recordings are retained for 90 days unless the customer requests deletion. Email activity logs are retained for 13 months. "
              "Deleted workspaces are purged from backups within 35 days. Customers can export their data at any time."),
]
UNRELATED_TOPICS = ["helicopter pilot licensing", "volcano insurance", "quantum teleportation pricing", "moon base catering"]


def split_sentences_with_spans(doc: SourceDoc) -> list[tuple[str, int, int]]:
    return [(m.group().strip(), m.start(), m.end()) for m in re.finditer(r"[^.!?]+[.!?]", doc.text)]


_VALUE = re.compile(r"\b\d+(?:\.\d+)?(?:-\w+)?(?:\s(?:days|months|seats|USD|business days|times|regions|percent))?|\bAES-256\b|\bunlimited\b|\btwice per year\b")


def generate_synthetic_dataset(docs: list[SourceDoc], seed: int = 11, max_per_type: int = 8, dup_threshold: float = 0.92) -> list[SyntheticCase]:
    """Template-based generator: cloze factoids (mask a value), multi-hop (two docs sharing a term), negative (false premise to refute),
    unanswerable (topic absent from the corpus). Near-duplicate questions are dropped by embedding similarity. An LLM generator would
    produce more natural wording; the structure (span-grounded truth, type, difficulty) stays the same."""
    rng, emb = random.Random(seed), HashedEmbedder()
    sents = [(d, s, a, b) for d in docs for s, a, b in split_sentences_with_spans(d)]
    cases: list[SyntheticCase] = []
    counter = 0

    def add(q: str, truth: str, qtype: str, diff: str, spans: list[tuple[str, int, int]]) -> None:
        nonlocal counter
        counter += 1
        cases.append(SyntheticCase(case_id=f"syn-{counter:03d}", question=q, ground_truth=truth, qtype=qtype, difficulty=diff, gold_spans=spans))  # type: ignore[arg-type]

    factoid = [(d, s, a, b, _VALUE.search(s)) for d, s, a, b in sents]
    for d, s, a, b, m in rng.sample([f for f in factoid if f[4]], k=min(max_per_type, sum(1 for f in factoid if f[4]))):
        masked = s[:m.start()] + "____" + s[m.end():]
        add(f"According to the {d.title}, what fills the blank: {masked}", s, "factoid", "easy" if len(s.split()) < 12 else "medium", [(d.doc_id, a, b)])
    term_docs: dict[str, list[tuple[SourceDoc, str, int, int]]] = defaultdict(list)
    for d, s, a, b in sents:
        for w in content_words(s):
            if w in {"enterprise", "plan", "plans", "data", "single", "seats"}:
                term_docs[w].append((d, s, a, b))
    hops = 0
    for term, items in sorted(term_docs.items()):
        for (d1, s1, a1, b1), (d2, s2, a2, b2) in zip(items, items[1:]):
            if d1.doc_id != d2.doc_id and hops < max_per_type:
                add(f"What do the {d1.title} and the {d2.title} each say about {term}?", f"{s1} {s2}", "multi_hop", "hard", [(d1.doc_id, a1, b1), (d2.doc_id, a2, b2)])
                hops += 1
                break
    for d, s, a, b, m in rng.sample([f for f in factoid if f[4] and _NUM.search(f[4].group())], k=min(3, sum(1 for f in factoid if f[4] and _NUM.search(f[4].group())))):
        n = float(_NUM.search(m.group()).group())  # type: ignore[union-attr]
        wrong = s[:m.start()] + m.group().replace(_NUM.search(m.group()).group(), str(int(n * 2 + 1))) + s[m.end():]  # type: ignore[union-attr]
        add(f"Is it true that: {wrong}", f"No. {s}", "negative", "hard", [(d.doc_id, a, b)])
    for topic in UNRELATED_TOPICS[:3]:
        add(f"What does the documentation say about {topic}?", "The documents do not say.", "unanswerable", "medium", [])
    unique: list[SyntheticCase] = []
    vecs: list[list[float]] = []
    for c in cases:
        v = emb.embed(c.question)
        if all(cosine(v, u) < dup_threshold for u in vecs):
            unique.append(c)
            vecs.append(v)
    return unique


class NaiveRagSystem:
    """Extractive RAG over sentence chunks: top-k by embedding; answer = best sentence, abstain when similarity is low. hallucination_rate injects an
    unsupported sentence to emulate a worse prompt/model version."""

    def __init__(self, docs: list[SourceDoc], top_k: int = 3, hallucination_rate: float = 0.0, abstain_below: float = 0.3, seed: int = 0) -> None:
        self.emb, self.top_k, self.h_rate, self.abstain_below, self.seed = HashedEmbedder(), top_k, hallucination_rate, abstain_below, seed
        self.chunks = [s for d in docs for s, _, _ in split_sentences_with_spans(d)]
        self._vecs = [self.emb.embed(c) for c in self.chunks]

    def answer(self, question: str) -> tuple[str, list[str], bool]:
        qv = self.emb.embed(question)
        ranked = sorted(range(len(self.chunks)), key=lambda i: -cosine(qv, self._vecs[i]))[:self.top_k]
        ctxs = [self.chunks[i] for i in ranked]
        if cosine(qv, self._vecs[ranked[0]]) < self.abstain_below:
            return "I don't know based on the documents.", ctxs, True
        ans = ctxs[0]
        if random.Random(f"{self.seed}:{question}").random() < self.h_rate:
            ans += " This also includes free shipping on all orders."
        return ans, ctxs, False


class RunMeta(BaseModel):
    run_id: str
    git_sha: str
    prompt_version: str
    model: str
    dataset_version: str


class EvalRun(BaseModel):
    meta: RunMeta
    per_case: dict[str, dict[str, float]]

    def mean(self, metric: str) -> float:
        xs = [m[metric] for m in self.per_case.values() if metric in m]
        return sum(xs) / len(xs) if xs else 0.0


def evaluate_system(system: NaiveRagSystem, cases: list[SyntheticCase], meta: RunMeta) -> EvalRun:
    per_case: dict[str, dict[str, float]] = {}
    for c in cases:
        ans, ctxs, abstained = system.answer(c.question)
        sc = ragas_scores(c.question, ans, ctxs, c.ground_truth if c.qtype != "unanswerable" else "", system.emb)
        row = sc.model_dump()
        row["behavior_ok"] = float(abstained == (c.qtype == "unanswerable"))
        if c.qtype == "unanswerable":
            row["context_precision"] = row["context_recall"] = row["answer_relevancy"] = row["behavior_ok"]  # no reference to retrieve: score only the abstention
        per_case[c.case_id] = row
    return EvalRun(meta=meta, per_case=per_case)


# ---- Syllabus extension: tracking evaluation scores across deployment runs ----------------------------------
class RunRegistry:
    """Append-only registry of eval runs (metric vector per case, tagged with git sha, prompt version, model). to_jsonl/from_jsonl give a storage format."""

    def __init__(self) -> None:
        self.runs: dict[str, EvalRun] = {}

    def add(self, run: EvalRun) -> None:
        if run.meta.run_id in self.runs:
            raise GatewayError(f"run {run.meta.run_id} already recorded (runs are immutable)")
        self.runs[run.meta.run_id] = run

    def to_jsonl(self) -> str:
        return "\n".join(r.model_dump_json() for r in self.runs.values())

    @classmethod
    def from_jsonl(cls, text: str) -> RunRegistry:
        reg = cls()
        for line in text.splitlines():
            reg.add(EvalRun.model_validate_json(line))
        return reg

    def trend(self, metric: str) -> list[tuple[str, str, str, float]]:
        return [(r.meta.run_id, r.meta.git_sha, r.meta.prompt_version, round(r.mean(metric), 3)) for r in self.runs.values()]


def bootstrap_ci(deltas: list[float], iters: int = 2000, alpha: float = 0.05, seed: int = 3) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the mean of paired per-case differences."""
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return round(means[int(alpha / 2 * iters)], 4), round(means[int((1 - alpha / 2) * iters) - 1], 4)


class MetricComparison(BaseModel):
    metric: str
    baseline: float
    candidate: float
    delta: float
    ci_low: float
    ci_high: float
    verdict: Literal["REGRESSION", "improved", "no significant change"]


def compare_runs(registry: RunRegistry, baseline_id: str, candidate_id: str, metrics: list[str], min_effect: float = 0.02) -> list[MetricComparison]:
    a, b = registry.runs[baseline_id], registry.runs[candidate_id]
    common = sorted(set(a.per_case) & set(b.per_case))
    out = []
    for m in metrics:
        deltas = [b.per_case[c][m] - a.per_case[c][m] for c in common if m in a.per_case[c] and m in b.per_case[c]]
        if not deltas:
            continue
        lo, hi = bootstrap_ci(deltas)
        mean = sum(deltas) / len(deltas)
        verdict = "REGRESSION" if hi < 0 and mean < -min_effect else ("improved" if lo > 0 and mean > min_effect else "no significant change")
        out.append(MetricComparison(metric=m, baseline=round(a.mean(m), 3), candidate=round(b.mean(m), 3), delta=round(mean, 3), ci_low=lo, ci_high=hi, verdict=verdict))  # type: ignore[arg-type]
    return out


# ---- Syllabus extension: deep span-level traces, token/cost accounting, latency histograms -------------------
# Attribute names follow the OpenTelemetry GenAI semantic conventions as I know them (gen_ai.operation.name, gen_ai.request.model,
# gen_ai.usage.input_tokens, gen_ai.usage.output_tokens, gen_ai.tool.name). That spec is still evolving: verify names against the current
# OpenTelemetry docs. Anything application-specific uses an app.* prefix on purpose.
GEN_AI_OP, REQ_MODEL, RESP_MODEL = "gen_ai.operation.name", "gen_ai.request.model", "gen_ai.response.model"
IN_TOK, OUT_TOK, TOOL_NAME = "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.tool.name"
PRICE_PER_MTOK = {"small-model": (0.5, 1.5), "large-model": (3.0, 15.0)}  # illustrative USD per 1M tokens: use your provider's invoice price table


def token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICE_PER_MTOK[model]
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def llm_attrs(model: str, input_tokens: int, output_tokens: int, endpoint: str, **extra: object) -> dict[str, object]:
    return {GEN_AI_OP: "chat", REQ_MODEL: model, RESP_MODEL: model, IN_TOK: input_tokens, OUT_TOK: output_tokens, "app.endpoint": endpoint,
            "app.cost_usd": round(token_cost(model, input_tokens, output_tokens), 8), **extra}


class SpanFactory:
    """Writes spans with explicit virtual timestamps (reproducible traffic) into a Tracer's span list."""

    def __init__(self, tracer: Tracer) -> None:
        self.tracer, self._n = tracer, 0

    def new_id(self) -> str:
        self._n += 1
        return f"{self._n:08x}"

    def add(self, name: str, trace_id: str, parent_id: str | None, start_s: float, ms: float, attrs: dict[str, object]) -> Span:
        sp = Span(span_id=self.new_id(), trace_id=trace_id, parent_id=parent_id, name=name, start=start_s, end=start_s + ms / 1000, attrs=attrs)
        self.tracer.spans.append(sp)
        return sp


TRAFFIC_INPUTS = ["How do I reset my password?", "Summarize the call with jane.doe@acme.com from yesterday", "Why did my sync fail? Call me on 415-555-0199",
                  "Refund policy for annual plans?", "Card 4111 1111 1111 1111 was charged twice, please check", "How many seats are on the Team plan?"]


def generate_traffic(tracer: Tracer, n: int = 300, seed: int = 5, input_scale: float = 1.0) -> SpanFactory:
    """Synthetic request traffic: http.request root -> retrieval -> llm.call, with a lognormal latency tail, errors, feedback and judge scores."""
    rng, sf = random.Random(seed), SpanFactory(tracer)
    cfg = {"/chat": ("large-model", 1800, 300, 0.9), "/summarize": ("large-model", 3500, 450, 1.1), "/search": ("small-model", 700, 120, 0.5)}
    t = 0.0
    for i in range(n):
        endpoint = rng.choices(list(cfg), weights=[5, 3, 4])[0]
        model, in_mean, out_mean, lat_scale = cfg[endpoint]
        tin, tout = int(rng.gauss(in_mean * input_scale, in_mean * 0.15)), max(20, int(rng.gauss(out_mean, out_mean * 0.2)))
        llm_ms = lat_scale * 1000 * rng.lognormvariate(-0.1, 0.55) * (1 + tout / 600)
        ret_ms = rng.uniform(15, 60)
        err = rng.random() < 0.02
        trace_id = f"t{i:06d}"
        user = f"u{rng.randrange(40)}"
        text = rng.choice(TRAFFIC_INPUTS)
        root_attrs: dict[str, object] = {"app.endpoint": endpoint, "app.user.id": user, "app.session.id": f"s{rng.randrange(80)}", "app.input": text,
                                         "app.output": "Here is the answer for: " + text, "app.judge_score": round(rng.uniform(2.5, 5.0), 1)}
        if err:
            root_attrs["error"] = "ProviderError"
        if rng.random() < 0.3:
            root_attrs["app.feedback"] = "up" if rng.random() < 0.8 else "down"
        total_ms = ret_ms + llm_ms + 5
        root = sf.add("http.request", trace_id, None, t, total_ms, root_attrs)
        sf.add("retrieval", trace_id, root.span_id, t + 0.002, ret_ms, {"app.endpoint": endpoint, "app.retrieval.k": 5})
        sf.add("llm.call", trace_id, root.span_id, t + 0.002 + ret_ms / 1000, llm_ms, llm_attrs(model, tin, tout, endpoint, **({"error": "ProviderError"} if err else {})))
        t += rng.expovariate(20)
    return sf


class LatencyHistogram:
    BOUNDS_MS = [50, 100, 250, 500, 1000, 2500, 5000]

    def __init__(self) -> None:
        self.counts = [0] * (len(self.BOUNDS_MS) + 1)

    def observe(self, ms: float) -> None:
        self.counts[next((i for i, b in enumerate(self.BOUNDS_MS) if ms <= b), len(self.BOUNDS_MS))] += 1

    def render(self) -> dict[str, int]:
        labels = [f"<={b}ms" for b in self.BOUNDS_MS] + [f">{self.BOUNDS_MS[-1]}ms"]
        return dict(zip(labels, self.counts))


class EndpointReport(BaseModel):
    endpoint: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_per_call: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    histogram: dict[str, int]


def endpoint_report(spans: list[Span]) -> list[EndpointReport]:
    roots: dict[str, list[Span]] = defaultdict(list)
    llm: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        if s.name == "http.request":
            roots[str(s.attrs["app.endpoint"])].append(s)
        elif s.name == "llm.call":
            llm[str(s.attrs["app.endpoint"])].append(s)
    out = []
    for ep, rs in sorted(roots.items()):
        hist = LatencyHistogram()
        for r in rs:
            hist.observe(r.ms)
        cost = sum(float(s.attrs["app.cost_usd"]) for s in llm[ep])
        lat = [r.ms for r in rs]
        out.append(EndpointReport(endpoint=ep, calls=len(rs), errors=sum("error" in r.attrs for r in rs), input_tokens=sum(int(s.attrs[IN_TOK]) for s in llm[ep]),
                                  output_tokens=sum(int(s.attrs[OUT_TOK]) for s in llm[ep]), cost_usd=round(cost, 4), cost_per_call=round(cost / len(rs), 6),
                                  p50_ms=round(percentile(lat, 50), 1), p95_ms=round(percentile(lat, 95), 1), p99_ms=round(percentile(lat, 99), 1), histogram=hist.render()))
    return out


def cost_by_model(spans: list[Span]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for s in spans:
        if s.name == "llm.call":
            out[str(s.attrs[REQ_MODEL])] += float(s.attrs["app.cost_usd"])
    return {k: round(v, 4) for k, v in out.items()}


def detect_cost_anomaly(prev: list[EndpointReport], curr: list[EndpointReport], threshold: float = 0.2) -> list[str]:
    """Alert when an endpoint's cost per call rises more than threshold versus the previous window (the 'bill tripled overnight' guard)."""
    before = {r.endpoint: r.cost_per_call for r in prev}
    return [f"{r.endpoint}: cost/call {before[r.endpoint]:.5f} -> {r.cost_per_call:.5f} (+{(r.cost_per_call / before[r.endpoint] - 1) * 100:.0f}%)"
            for r in curr if r.endpoint in before and before[r.endpoint] > 0 and r.cost_per_call > before[r.endpoint] * (1 + threshold)]


# ---- Syllabus extension: debugging multi-step agent reasoning and tool inputs -----------------------------
class AgentScriptStep(BaseModel):
    thought: str
    tool: str | None
    tool_input: dict[str, object] = Field(default_factory=dict)
    tool_output: str | None = None
    error: str | None = None
    input_tokens: int
    output_tokens: int
    llm_ms: float = 400.0
    tool_ms: float = 120.0


def build_agent_trace(sf: SpanFactory, trace_id: str, script: list[AgentScriptStep], model: str = "large-model") -> None:
    """Record one agent run: agent.run -> per step an llm.call (with the thought) and a tool.call (name, input, output, error)."""
    t = 1000.0
    root = sf.add("agent.run", trace_id, None, t, sum(s.llm_ms + s.tool_ms for s in script), {"app.endpoint": "/agent", "app.input": "Where is order A-1 and what is the refund policy?"})
    for i, st in enumerate(script):
        sf.add("llm.call", trace_id, root.span_id, t, st.llm_ms, llm_attrs(model, st.input_tokens, st.output_tokens, "/agent", **{"app.agent.step": i, "app.agent.thought": st.thought}))
        t += st.llm_ms / 1000
        if st.tool:
            attrs: dict[str, object] = {GEN_AI_OP: "execute_tool", TOOL_NAME: st.tool, "app.agent.step": i, "app.tool.input": canonical_json(st.tool_input), "app.tool.output": st.tool_output or ""}
            if st.error:
                attrs["error"] = st.error
            sf.add("tool.call", trace_id, root.span_id, t, st.tool_ms, attrs)
            t += st.tool_ms / 1000


class AgentStep(BaseModel):
    index: int
    thought: str
    tool: str | None
    tool_input: str
    tool_output: str
    error: str | None
    input_tokens: int
    output_tokens: int
    ms: float


class Anomaly(BaseModel):
    kind: Literal["repeated_call", "tool_error", "ballooning_context", "slow_step", "too_many_steps"]
    step: int
    detail: str


def reconstruct_agent_run(spans: list[Span], trace_id: str, max_steps: int = 8) -> tuple[list[AgentStep], list[Anomaly]]:
    mine = [s for s in spans if s.trace_id == trace_id]
    llm = {int(s.attrs["app.agent.step"]): s for s in mine if s.name == "llm.call"}
    tools = {int(s.attrs["app.agent.step"]): s for s in mine if s.name == "tool.call"}
    steps = []
    for i in sorted(llm):
        lc, tc = llm[i], tools.get(i)
        steps.append(AgentStep(index=i, thought=str(lc.attrs.get("app.agent.thought", "")), tool=str(tc.attrs[TOOL_NAME]) if tc else None,
                               tool_input=str(tc.attrs.get("app.tool.input", "")) if tc else "", tool_output=str(tc.attrs.get("app.tool.output", "")) if tc else "",
                               error=str(tc.attrs["error"]) if tc and "error" in tc.attrs else None, input_tokens=int(lc.attrs[IN_TOK]),
                               output_tokens=int(lc.attrs[OUT_TOK]), ms=round(lc.ms + (tc.ms if tc else 0.0), 1)))
    anomalies: list[Anomaly] = []
    seen: dict[tuple[str, str], int] = {}
    for st in steps:
        if st.tool:
            sig = (st.tool, st.tool_input)
            if sig in seen:
                anomalies.append(Anomaly(kind="repeated_call", step=st.index, detail=f"{st.tool}{st.tool_input} already called at step {seen[sig]}"))
            seen.setdefault(sig, st.index)
        if st.error:
            anomalies.append(Anomaly(kind="tool_error", step=st.index, detail=f"{st.tool}{st.tool_input} -> {st.error}"))
    growth = 0
    for prev, cur in zip(steps, steps[1:]):
        growth = growth + 1 if cur.input_tokens > 1.5 * prev.input_tokens else 0
        if growth >= 2 or (steps and cur.input_tokens > 4 * steps[0].input_tokens):
            anomalies.append(Anomaly(kind="ballooning_context", step=cur.index, detail=f"input tokens {prev.input_tokens} -> {cur.input_tokens}"))
            break
    med = sorted(s.ms for s in steps)[len(steps) // 2] if steps else 0.0
    anomalies += [Anomaly(kind="slow_step", step=s.index, detail=f"{s.ms}ms vs median {med}ms") for s in steps if len(steps) >= 3 and s.ms > 3 * med]
    if len(steps) > max_steps:
        anomalies.append(Anomaly(kind="too_many_steps", step=len(steps) - 1, detail=f"{len(steps)} steps > {max_steps}"))
    return steps, anomalies


AGENT_SCRIPT = [
    AgentScriptStep(thought="Need the order status first.", tool="lookup_order", tool_input={"order_id": "A-1"}, tool_output='{"status": "shipped"}', input_tokens=900, output_tokens=60),
    AgentScriptStep(thought="Now find the refund policy.", tool="search_docs", tool_input={"query": "refund policy"}, tool_output="Refunds within 30 days.", input_tokens=1300, output_tokens=70),
    AgentScriptStep(thought="Let me search again to be sure.", tool="search_docs", tool_input={"query": "refund policy"}, tool_output="Refunds within 30 days.", input_tokens=2300, output_tokens=65),
    AgentScriptStep(thought="Check the customer record.", tool="lookup_customer", tool_input={"id": ""}, error="ValidationError: id must be non-empty", input_tokens=4200, output_tokens=80, tool_ms=2400),
    AgentScriptStep(thought="Answer with what I have.", tool=None, input_tokens=4300, output_tokens=140),
]


# ---- Syllabus extension: custom user-session tracking metrics -------------------------------------------------
class SessionEvent(BaseModel):
    session_id: str
    user_id: str
    ts: float  # seconds since epoch (day index = ts // 86400)
    kind: Literal["turn_start", "first_token", "turn_end", "feedback"]
    value: float = 0.0  # ttft ms for first_token, cost USD for turn_end
    label: str = ""  # "up" | "down" for feedback


class SessionMetrics(BaseModel):
    session_id: str
    user_id: str
    turns: int
    completed_turns: int
    abandoned: bool
    ttft_p50_ms: float
    thumbs_up: int
    thumbs_down: int
    cost_usd: float
    duration_s: float


def summarize_sessions(events: list[SessionEvent]) -> list[SessionMetrics]:
    by: dict[str, list[SessionEvent]] = defaultdict(list)
    for e in sorted(events, key=lambda e: e.ts):
        by[e.session_id].append(e)
    out = []
    for sid, evs in by.items():
        starts = [e for e in evs if e.kind == "turn_start"]
        ends = [e for e in evs if e.kind == "turn_end"]
        out.append(SessionMetrics(session_id=sid, user_id=evs[0].user_id, turns=len(starts), completed_turns=len(ends), abandoned=len(ends) < len(starts),
                                  ttft_p50_ms=round(percentile([e.value for e in evs if e.kind == "first_token"], 50), 1), thumbs_up=sum(e.label == "up" for e in evs),
                                  thumbs_down=sum(e.label == "down" for e in evs), cost_usd=round(sum(e.value for e in ends), 5), duration_s=round(evs[-1].ts - evs[0].ts, 1)))
    return out


def aggregate_sessions(ms: list[SessionMetrics]) -> dict[str, float]:
    rated = sum(m.thumbs_up + m.thumbs_down for m in ms)
    return {"sessions": len(ms), "avg_turns": round(sum(m.turns for m in ms) / len(ms), 2), "abandonment_rate": round(sum(m.abandoned for m in ms) / len(ms), 3),
            "thumbs_up_share": round(sum(m.thumbs_up for m in ms) / rated, 3) if rated else 0.0, "ttft_p50_ms": round(percentile([m.ttft_p50_ms for m in ms], 50), 1),
            "cost_per_session_p50": round(percentile([m.cost_usd for m in ms], 50), 5), "cost_per_session_p95": round(percentile([m.cost_usd for m in ms], 95), 5)}


def retention_buckets(events: list[SessionEvent], days: tuple[int, ...] = (1, 3, 7)) -> dict[str, float]:
    """Share of each user's first-day cohort that is active again k days later (D1/D3/D7)."""
    active: dict[str, set[int]] = defaultdict(set)
    for e in events:
        active[e.user_id].add(int(e.ts // 86400))
    cohort = [sorted(d) for d in active.values()]
    return {f"D{k}": round(sum((d[0] + k) in set(d) for d in cohort) / len(cohort), 3) for k in days}


def generate_session_events(users: int = 40, seed: int = 9) -> list[SessionEvent]:
    rng = random.Random(seed)
    events: list[SessionEvent] = []
    n = 0
    for u in range(users):
        first = rng.randrange(0, 3)
        for day in range(first, 10):
            if day != first and rng.random() > 0.45 * 0.9 ** (day - first):
                continue
            n += 1
            sid, t, turns = f"s{n}", day * 86400 + rng.uniform(3600, 70000), rng.randint(1, 6)
            for k in range(turns):
                t += rng.uniform(20, 90)
                events.append(SessionEvent(session_id=sid, user_id=f"u{u}", ts=t, kind="turn_start"))
                events.append(SessionEvent(session_id=sid, user_id=f"u{u}", ts=t + 0.6, kind="first_token", value=rng.lognormvariate(6.2, 0.35)))
                if k == turns - 1 and rng.random() < 0.12:
                    break  # user left before the answer finished
                events.append(SessionEvent(session_id=sid, user_id=f"u{u}", ts=t + 4, kind="turn_end", value=rng.uniform(0.004, 0.02)))
                if rng.random() < 0.25:
                    events.append(SessionEvent(session_id=sid, user_id=f"u{u}", ts=t + 6, kind="feedback", label="up" if rng.random() < 0.75 else "down"))
    return events


# ---- Syllabus extension: exporting trace data for continuous improvement ----------------------------------
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,2}[ .-])?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    nums = [int(c) for c in digits][::-1]
    return len(nums) >= 13 and sum(n if i % 2 == 0 else (n * 2 - 9 if n * 2 > 9 else n * 2) for i, n in enumerate(nums)) % 10 == 0


def mask_pii(text: str) -> str:
    """Miniature of Module 13's analyzers: mask before any trace leaves the boundary."""
    text = _CARD.sub(lambda m: "[CARD]" if _luhn(re.sub(r"\D", "", m.group())) else m.group(), text)
    return _PHONE.sub("[PHONE]", _SSN.sub("[SSN]", _EMAIL.sub("[EMAIL]", text)))


def export_jsonl(spans: list[Span], mask: bool = True) -> str:
    lines = []
    for s in spans:
        attrs = {k: (mask_pii(v) if mask and isinstance(v, str) else v) for k, v in s.attrs.items()}
        lines.append(json.dumps({"trace_id": s.trace_id, "span_id": s.span_id, "parent_id": s.parent_id, "name": s.name, "start": s.start, "duration_ms": round(s.ms, 3), "attrs": attrs}, default=str))
    return "\n".join(lines)


def _otlp_value(v: object) -> dict[str, object]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}  # OTLP JSON encodes 64-bit integers as strings
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def to_otlp_json(spans: list[Span], service_name: str = "lab14", mask: bool = True) -> dict[str, object]:
    """OTLP/JSON-shaped export (resourceSpans -> scopeSpans -> spans). Ids are hex strings, times are unix nanoseconds as strings.
    Spans carry monotonic or virtual clocks, so an epoch offset is added to make the timestamps plausible wall-clock values."""
    offset = time.time() - max((s.end for s in spans), default=0.0)
    out_spans = []
    for s in spans:
        out_spans.append({"traceId": hashlib.sha256(s.trace_id.encode()).hexdigest()[:32], "spanId": hashlib.sha256(f"{s.trace_id}{s.span_id}".encode()).hexdigest()[:16],
                          **({"parentSpanId": hashlib.sha256(f"{s.trace_id}{s.parent_id}".encode()).hexdigest()[:16]} if s.parent_id else {}),
                          "name": s.name, "kind": 1, "startTimeUnixNano": str(int((s.start + offset) * 1e9)), "endTimeUnixNano": str(int((s.end + offset) * 1e9)),
                          "attributes": [{"key": k, "value": _otlp_value(mask_pii(v) if mask and isinstance(v, str) else v)} for k, v in s.attrs.items()],
                          "status": {"code": 2 if "error" in s.attrs else 1}})
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
                               "scopeSpans": [{"scope": {"name": "lab14"}, "spans": out_spans}]}]}


class DatasetRecord(BaseModel):
    messages: list[dict[str, str]]
    metadata: dict[str, object]


def build_dataset(spans: list[Span], min_judge: float = 4.0) -> tuple[list[DatasetRecord], dict[str, int]]:
    """Turn production traces into a fine-tuning/eval dataset: keep clean, well-rated, de-duplicated traces; mask PII; count why others were dropped."""
    drops: dict[str, int] = defaultdict(int)
    seen: set[str] = set()
    kept: list[DatasetRecord] = []
    errored = {s.trace_id for s in spans if "error" in s.attrs}
    for s in spans:
        if s.name != "http.request":
            continue
        inp, out = str(s.attrs.get("app.input", "")), str(s.attrs.get("app.output", ""))
        if s.trace_id in errored:
            drops["error in trace"] += 1
        elif s.attrs.get("app.feedback") == "down":
            drops["thumbs down"] += 1
        elif float(s.attrs.get("app.judge_score", 0.0)) < min_judge and s.attrs.get("app.feedback") != "up":
            drops["low judge score"] += 1
        elif len(out) < 20:
            drops["output too short"] += 1
        elif (key := hashlib.sha256(inp.lower().encode()).hexdigest()) in seen:
            drops["duplicate input"] += 1
        else:
            seen.add(key)
            kept.append(DatasetRecord(messages=[{"role": "user", "content": mask_pii(inp)}, {"role": "assistant", "content": mask_pii(out)}],
                                      metadata={"trace_id": s.trace_id, "endpoint": s.attrs.get("app.endpoint"), "judge_score": s.attrs.get("app.judge_score")}))
    return kept, dict(drops)


class InMemoryLangfuse:
    """Offline stand-in with the method shapes the exporter uses (trace/span/generation/flush); flush fails once to exercise backoff."""

    class _Trace:
        def __init__(self, owner: InMemoryLangfuse, tid: str) -> None:
            self.owner, self.tid = owner, tid

        def span(self, **kw: object) -> None:
            self.owner.calls.append(("span", self.tid, kw.get("name")))

        def generation(self, **kw: object) -> None:
            self.owner.calls.append(("generation", self.tid, kw.get("model")))

    def __init__(self, fail_flush_once: bool = True) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self._fail = fail_flush_once

    def trace(self, **kw: object) -> InMemoryLangfuse._Trace:
        self.calls.append(("trace", str(kw.get("id")), kw.get("name")))
        return InMemoryLangfuse._Trace(self, str(kw.get("id")))

    def flush(self) -> None:
        if self._fail:
            self._fail = False
            raise ConnectionError("collector unreachable")


class LangfuseShapeExporter:
    """Exports span trees through a Langfuse-style client (trace -> span/generation, then flush). The v2-style Python SDK exposes client.trace();
    newer SDK majors are OpenTelemetry-based and differ, so verify against the current Langfuse docs or use the OTLP export instead."""

    def __init__(self, client: object) -> None:
        if not hasattr(client, "trace"):
            raise ExporterUnsupported("this Langfuse SDK has no client.trace(); export OTLP JSON (to_otlp_json) to its OpenTelemetry endpoint instead")
        self.client = client

    @classmethod
    def from_env(cls) -> LangfuseShapeExporter:
        try:
            from langfuse import Langfuse
        except ImportError as exc:
            raise OptionalDependencyMissing("langfuse", "pip install langfuse and set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST") from exc
        return cls(Langfuse())

    async def export(self, spans: list[Span]) -> int:
        by_trace: dict[str, list[Span]] = defaultdict(list)
        for s in spans:
            by_trace[s.trace_id].append(s)
        for tid, group in by_trace.items():
            root = next((s for s in group if s.parent_id is None), group[0])
            tr = self.client.trace(id=tid, name=root.name, input=mask_pii(str(root.attrs.get("app.input", ""))), output=mask_pii(str(root.attrs.get("app.output", ""))))  # type: ignore[attr-defined]
            for s in group:
                if s.parent_id is None:
                    continue
                if REQ_MODEL in s.attrs:
                    tr.generation(name=s.name, model=s.attrs[REQ_MODEL], usage={"input": s.attrs[IN_TOK], "output": s.attrs[OUT_TOK]}, metadata={"cost_usd": s.attrs.get("app.cost_usd")})
                else:
                    tr.span(name=s.name, metadata={k: v for k, v in s.attrs.items() if not k.startswith("app.input")})

        async def flush() -> None:
            await asyncio.to_thread(self.client.flush)  # type: ignore[attr-defined]

        await with_backoff(flush, (ConnectionError, TimeoutError, OSError))
        return len(by_trace)


async def extension_demo() -> None:
    log.info("=== Syllabus extension demo: Module 14 ===")
    # 1. Idempotency keys
    clock = [0.0]
    store = IdempotencyStore(ttl_s=60.0, clock=lambda: clock[0])
    sends: list[str] = []

    async def send_once() -> str:
        await asyncio.sleep(0.005)
        sends.append("sent")
        return "msg-1"

    key = derive_idempotency_key("user-7", "send_email", {"to": "cfo@salesloft.com", "body": "hi"})
    results = await asyncio.gather(*(store.execute(key, {"to": "cfo@salesloft.com"}, send_once) for _ in range(5)))
    log.info("in-flight dedupe", extra={"ctx": {"executions": len(sends), "statuses": sorted(s for _, s in results)}})
    _, status = await store.execute(key, {"to": "cfo@salesloft.com"}, send_once)
    log.info("replay", extra={"ctx": {"status": status, "executions": len(sends)}})
    try:
        await store.execute(key, {"to": "someone-else@salesloft.com"}, send_once)
    except IdempotencyConflict as exc:
        log.info("conflict", extra={"ctx": {"error": str(exc)}})
    clock[0] = 61.0
    _, status = await store.execute(key, {"to": "cfo@salesloft.com"}, send_once)
    log.info("after ttl expiry", extra={"ctx": {"status": status, "executions": len(sends)}})
    for honour in (False, True):
        svc = FakeEmailService(honour_keys=honour)
        k = "key-123" if honour else None
        await with_backoff(lambda: svc.send("a@b.test", k), (ToolTransientError,))
        log.info("lost-ack retry", extra={"ctx": {"idempotency_key_sent": honour, "emails_actually_sent": len(svc.sent)}})
    # 2. Backoff strategies and retry storms
    for strat in (ExponentialCapped(), FullJitter(), EqualJitter(), DecorrelatedJitter()):
        log.info("retry storm", extra={"ctx": simulate_retry_storm(strat).model_dump()})
    # 3. Config, virtual keys, adapters
    env = {"VENDOR_A_API_KEY": "demo-placeholder-a", "VENDOR_B_API_KEY": "demo-placeholder-b"}
    cfg = load_gateway_config(GATEWAY_CONFIG_TEXT, env)
    log.info("config loaded", extra={"ctx": {"aliases": sorted({m.model_name for m in cfg.model_list}), "strategy": cfg.router_settings.routing_strategy,
                                              "key_refs": [m.litellm_params.api_key for m in cfg.model_list], "fallbacks": cfg.router_settings.fallbacks}})
    for bad, why in [("model_list:\n  - model_name: x\n    litellm_params:\n      model: v/m\n      api_key: sk-live-inline-secret", "inline secret"),
                     ("model_list:\n  - model_name: x\n    litellm_params:\n      model: v/m\n      api_key: os.environ/MISSING_KEY", "unset env var"),
                     ('model_list:\n  - model_name: x\n    litellm_params:\n      model: v/m\nrouter_settings:\n  fallbacks: [{"x": ["nope"]}]', "unknown fallback alias")]:
        try:
            load_gateway_config(bad, env)
        except ConfigError as exc:
            log.info("config rejected", extra={"ctx": {"case": why, "error": str(exc)[:90]}})
    vks = VirtualKeyStore()
    token = vks.issue("team-search", {"chat-prod"}, rpm=60)
    vk = vks.authorize(token, "chat-prod")
    try:
        vks.authorize(token, "chat-backup")
    except AuthError as exc:
        log.info("virtual key scope enforced", extra={"ctx": {"team": vk.team, "error": str(exc)}})
    vks.revoke(vk.key_id)
    try:
        vks.authorize(token, "chat-prod")
    except AuthError as exc:
        log.info("virtual key revoked", extra={"ctx": {"error": str(exc)}})
    tracer = Tracer()
    router = AliasRouter(cfg, tracer, sim={"vendor-a/large-model": (0.01, 1.0), "vendor-b/large-model": (0.01, 1.0), "selfhost/small-model": (0.01, 0.0)},
                         prices={"vendor-a/large-model": (3.0, 15.0), "vendor-b/large-model": (3.0, 15.0), "selfhost/small-model": (0.2, 0.2)})
    comp = await router.chat("chat-prod", "Summarize the renewal terms for the customer in two sentences")
    log.info("alias fallback", extra={"ctx": {"provider": comp.provider, "fallback_used": comp.fallback_used}})
    try:
        lazy_litellm_router(cfg)
    except OptionalDependencyMissing as exc:
        log.info("litellm adapter skipped", extra={"ctx": {"reason": str(exc)}})
    # 4. Rate limiting and routing policies
    burst = [0.95 + i * 0.005 for i in range(10)] + [1.01 + i * 0.005 for i in range(10)]
    log.info("limiters on a window-boundary burst (limit 10/s, 20 requests in 0.1 s)", extra={"ctx": compare_limiters(burst)})
    tpm = TokenBucket(rate_per_s=1000 / 60, capacity=1000)
    log.info("token bucket as tokens-per-minute meter", extra={"ctx": {"600_tok": tpm.allow("t", 0.0, 600), "600_tok_again": tpm.allow("t", 0.1, 600), "after_30s": tpm.allow("t", 30.0, 600)}})
    cl = ConcurrencyLimiter(2)
    log.info("concurrency limiter", extra={"ctx": {"acquires": [cl.try_acquire("k") for _ in range(3)], "after_release": (cl.release("k"), cl.try_acquire("k"))[1]}})
    provs = [SimProvider("slow-cheap", 0.5, 1.5, 0.04), SimProvider("fast-pricey", 5.0, 20.0, 0.005), SimProvider("mid", 2.0, 6.0, 0.02)]
    for policy in (PriorityPolicy(), LatencyPolicy(), CostPolicy(), LeastBusyPolicy()):
        gw = Gateway(provs, Tracer(), {"t": 10.0}, policy=policy, limiter=TokenBucket(5, 5))
        picks = [(await gw.chat("t", f"warmup {i}")).provider for i in range(3)]
        later = (await gw.chat("t", "after warmup")).provider
        log.info("routing policy", extra={"ctx": {"policy": policy.name, "first_three": picks, "after_warmup": later}})
    gw = Gateway(provs, Tracer(), {"t": 10.0}, limiter=TokenBucket(2, 2))
    limited = 0
    for i in range(5):
        try:
            await gw.chat("t", f"burst {i}")
        except RateLimited:
            limited += 1
    log.info("gateway rate limit", extra={"ctx": {"served": 5 - limited, "rate_limited": limited}})
    # 5. LLM-as-judge
    judge: Judge = FormatFlakyJudge(DeterministicJudge(), fail_first=1)
    q0, c0, good, bad = JUDGE_ITEMS[0]
    pv_good, pv_bad = await judge_pointwise(judge, q0, c0, good), await judge_pointwise(judge, q0, c0, bad)
    log.info("pointwise judge (first reply was not JSON, retried)", extra={"ctx": {"good": pv_good.scores, "bad": pv_bad.scores}})
    pairs = build_judge_pairs()
    human = [p[4] for p in pairs]
    biased = DeterministicJudge(position_bias=1.0)  # a judge that favours whatever is shown first
    single = [(await judge_pairwise(biased, q, c, a, b, swap_check=False)).winner for q, c, a, b, _ in pairs]
    swap_runs = [await judge_pairwise(biased, q, c, a, b, swap_check=True) for q, c, a, b, _ in pairs]
    keep = [i for i, r in enumerate(swap_runs) if r.consistent]
    clean = [(await judge_pairwise(DeterministicJudge(), q, c, a, b)).winner for q, c, a, b, _ in pairs]
    log.info("judge vs human agreement", extra={"ctx": {"pairs": len(pairs), "kappa_unbiased_judge": cohen_kappa(human, clean), "kappa_biased_single_order": cohen_kappa(human, single),
                                                          "swap_consistency_rate": round(len(keep) / len(pairs), 3),
                                                          "kappa_biased_on_swap_consistent_pairs": cohen_kappa([human[i] for i in keep], [swap_runs[i].winner for i in keep]),
                                                          "note": "inconsistent verdicts reveal position bias: send them to a human or a third judge"}})
    try:
        AnthropicJudge("model-id-supplied-by-caller")
    except OptionalDependencyMissing as exc:
        log.info("real judge adapter skipped", extra={"ctx": {"reason": str(exc)}})
    # 6. RAGAS-style metrics
    emb = HashedEmbedder()
    ctx = ["Refunds are available within 30 days of purchase for standard plans.", "Enterprise contracts follow the master services agreement."]
    gt = "Standard plans can be refunded within 30 days of purchase."
    q = "What is the refund window for standard plans?"
    for name, ans, contexts in [("grounded", gt, ctx), ("hallucinated number", "Standard plans can be refunded within 90 days of purchase.", ctx),
                                ("retrieval miss (relevant chunk ranked last)", gt, list(reversed(ctx))), ("abstention", "I don't know based on the documents.", ctx)]:
        log.info("ragas", extra={"ctx": {"case": name, **ragas_scores(q, ans, contexts, gt, emb).model_dump()}})
    # 8. Synthetic benchmark
    cases = generate_synthetic_dataset(SOURCE_DOCS)
    by_type: dict[str, int] = defaultdict(int)
    for c in cases:
        by_type[c.qtype] += 1
    log.info("synthetic dataset", extra={"ctx": {"cases": len(cases), "by_type": dict(by_type), "example": cases[0].model_dump()}})
    # 7 + 9. Context precision/recall on the benchmark and tracking across runs
    registry = RunRegistry()
    variants = [("run-001", "a1b2c3d", "prompt-v1", NaiveRagSystem(SOURCE_DOCS, top_k=3, hallucination_rate=0.0)),
                ("run-002", "d4e5f6a", "prompt-v2", NaiveRagSystem(SOURCE_DOCS, top_k=3, hallucination_rate=0.5, seed=1)),
                ("run-003", "9f8e7d6", "prompt-v3", NaiveRagSystem(SOURCE_DOCS, top_k=5, hallucination_rate=0.0, abstain_below=0.2))]
    for run_id, sha, pv, system in variants:
        registry.add(evaluate_system(system, cases, RunMeta(run_id=run_id, git_sha=sha, prompt_version=pv, model="large-model", dataset_version="syn-v1")))
    metrics = ["faithfulness", "answer_relevancy", "context_precision", "context_recall", "behavior_ok"]
    for m in metrics:
        log.info("trend across runs", extra={"ctx": {"metric": m, "runs": registry.trend(m)}})
    for cand in ("run-002", "run-003"):
        for row in compare_runs(registry, "run-001", cand, metrics):
            log.info("comparison vs run-001", extra={"ctx": {"candidate": cand, **row.model_dump()}})
    restored = RunRegistry.from_jsonl(registry.to_jsonl())
    log.info("registry round trip", extra={"ctx": {"runs": list(restored.runs), "jsonl_bytes": len(registry.to_jsonl())}})
    # 10-11. Span traces, token costs, latency
    tr = Tracer()
    generate_traffic(tr, n=300)
    reports = endpoint_report(tr.spans)
    for r in reports:
        log.info("endpoint report", extra={"ctx": r.model_dump()})
    log.info("cost by model", extra={"ctx": cost_by_model(tr.spans)})
    heavy = Tracer()
    generate_traffic(heavy, n=300, input_scale=3.0)
    log.info("cost anomaly alerts", extra={"ctx": {"alerts": detect_cost_anomaly(reports, endpoint_report(heavy.spans))}})
    # 12. Agent debugger
    at = Tracer()
    build_agent_trace(SpanFactory(at), "trace-agent-1", AGENT_SCRIPT)
    steps, anomalies = reconstruct_agent_run(at.spans, "trace-agent-1")
    for st in steps:
        log.info("agent step", extra={"ctx": {"i": st.index, "thought": st.thought, "tool": st.tool, "input": st.tool_input, "output": st.tool_output[:30], "error": st.error,
                                              "in_tokens": st.input_tokens, "ms": st.ms}})
    for an in anomalies:
        log.info("agent anomaly", extra={"ctx": an.model_dump()})
    # 13. Sessions
    events = generate_session_events()
    sm = summarize_sessions(events)
    log.info("session metrics", extra={"ctx": aggregate_sessions(sm)})
    log.info("retention", extra={"ctx": retention_buckets(events)})
    # 14. Export
    jl = export_jsonl(tr.spans[:6])
    log.info("jsonl export (masked)", extra={"ctx": {"first_line": jl.splitlines()[0][:160], "pii_present": "@" in jl or "4111" in jl}})
    otlp = to_otlp_json(tr.spans[:6])
    sp0 = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]  # type: ignore[index]
    log.info("otlp json export", extra={"ctx": {"trace_id_len": len(sp0["traceId"]), "span_id_len": len(sp0["spanId"]), "name": sp0["name"], "attrs": len(sp0["attributes"])}})
    dataset, dropped = build_dataset(tr.spans)
    log.info("dataset from traces", extra={"ctx": {"kept": len(dataset), "dropped": dropped, "sample": dataset[0].model_dump() if dataset else None}})
    fake = InMemoryLangfuse()
    exported = await LangfuseShapeExporter(fake).export(tr.spans[:12])
    log.info("langfuse-shape export (flush retried)", extra={"ctx": {"traces": exported, "calls": len(fake.calls)}})
    try:
        LangfuseShapeExporter.from_env()
    except OptionalDependencyMissing as exc:
        log.info("langfuse adapter skipped", extra={"ctx": {"reason": str(exc)}})


async def main() -> None:
    tracer = Tracer()
    primary = SimProvider("provider-a", in_price=3.0, out_price=15.0, latency_s=0.02, fail_rate=0.5, seed=3)
    backup = SimProvider("provider-b", in_price=0.5, out_price=1.5, latency_s=0.03)
    gw = Gateway([primary, backup], tracer, daily_budget_usd={"team-search": 0.0012, "team-legal": 1.0})

    for i in range(6):
        try:
            c = await gw.chat("team-search", f"Summarize clause {i} of the master agreement for the customer in two sentences please")
            log.info("completion", extra={"ctx": {"i": i, "provider": c.provider, "fallback": c.fallback_used, "cost_usd": round(c.cost_usd, 8), "ms": round(c.latency_ms, 1)}})
        except BudgetExceeded as exc:
            log.info("budget stop", extra={"ctx": {"i": i, "error": str(exc)}})
    again = await gw.chat("team-legal", "What is our refund policy?")
    cached = await gw.chat("team-legal", "What is our refund policy?")
    log.info("cache", extra={"ctx": {"first_cost": round(again.cost_usd, 8), "second_cached": cached.cached, "second_cost": cached.cost_usd}})
    log.info("slo", extra={"ctx": {"p50_ms": round(tracer.percentile("llm.call", 50), 1), "p95_ms": round(tracer.percentile("llm.call", 95), 1),
                                    "fallback_rate": round(gw.fallbacks / gw.requests, 3), "spend": {k: round(v, 8) for k, v in gw.spend.items()}}})
    root = next(s for s in tracer.spans if s.name == "gateway.chat")
    log.info("trace tree", extra={"ctx": {"trace": root.trace_id, "children": [(s.name, s.attrs.get("provider"), round(s.ms, 1)) for s in tracer.spans if s.parent_id == root.span_id]}})

    ctx = ["Refunds are available within 30 days of purchase for standard plans.", "Enterprise contracts follow the master services agreement."]
    good = EvalCase(question="What is the refund window for standard plans?", contexts=ctx, relevant_ids={0},
                    answer="Standard plans can be refunded within 30 days of purchase.")
    bad = EvalCase(question="What is the refund window for standard plans?", contexts=list(reversed(ctx)), relevant_ids={1},
                   answer="Refunds are granted within 90 days and include shipping costs.")
    baseline, candidate = run_eval([good]), run_eval([bad])
    log.info("eval", extra={"ctx": {"baseline": baseline.model_dump(), "candidate": candidate.model_dump()}})
    try:
        gate(candidate, baseline)
    except EvalRegression as exc:
        log.info("release blocked", extra={"ctx": {"reason": str(exc)}})
    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
