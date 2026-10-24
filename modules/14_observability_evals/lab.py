"""Module 14 lab: LLM gateway with fallbacks and budgets, trace/span recording with cost accounting, and a
RAGAS-style evaluation harness with regression gates.

- Gateway: model group with ordered providers, per-attempt timeout, jittered retries, circuit-breaker-lite,
  per-tenant daily budget (hard stop), response cache.
- Tracer: Langfuse-like in-memory traces with parent/child spans and percentile summaries.
- Evals: lexical implementations of faithfulness, answer relevancy and context precision (deterministic,
  offline); swap the judge for an LLM in production. Run: python3 lab.py
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
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Protocol

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


class Gateway:
    def __init__(self, providers: list[SimProvider], tracer: Tracer, daily_budget_usd: dict[str, float], attempt_timeout_s: float = 0.2,
                 breaker_threshold: int = 3, breaker_cooldown_s: float = 0.5) -> None:
        self.providers, self.tracer = providers, tracer
        self.budget, self.spend = daily_budget_usd, {k: 0.0 for k in daily_budget_usd}
        self.timeout, self.cache = attempt_timeout_s, {}
        self._fails = {p.name: 0 for p in providers}
        self._open_until = {p.name: 0.0 for p in providers}
        self._threshold, self._cooldown = breaker_threshold, breaker_cooldown_s
        self.fallbacks = 0
        self.requests = 0

    @staticmethod
    def _cost(p: SimProvider, u: Usage) -> float:
        return (u.input_tokens * p.in_price + u.output_tokens * p.out_price) / 1_000_000

    async def chat(self, tenant: str, prompt: str) -> Completion:
        self.requests += 1
        if self.spend.get(tenant, 0.0) >= self.budget.get(tenant, 0.0):
            raise BudgetExceeded(f"tenant {tenant} reached daily budget ${self.budget.get(tenant, 0.0):.4f}")
        key = hashlib.sha256(f"{tenant}:{prompt}".encode()).hexdigest()
        async with self.tracer.span("gateway.chat", tenant=tenant) as root:
            if key in self.cache:
                root.attrs.update(cached=True, cost_usd=0.0)
                return self.cache[key].model_copy(update={"cached": True, "cost_usd": 0.0, "latency_ms": 0.1})
            for rank, p in enumerate(self.providers):
                if time.monotonic() < self._open_until[p.name]:
                    continue  # breaker open: skip provider entirely
                for attempt in range(1, 3):
                    t0 = time.perf_counter()
                    try:
                        async with self.tracer.span("llm.call", provider=p.name, attempt=attempt) as sp:
                            text, usage = await asyncio.wait_for(p.complete(prompt), self.timeout)
                            cost = self._cost(p, usage)
                            sp.attrs.update(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cost_usd=round(cost, 8))
                        self._fails[p.name] = 0
                        self.spend[tenant] += cost
                        comp = Completion(text=text, usage=usage, provider=p.name, cost_usd=cost,
                                          latency_ms=(time.perf_counter() - t0) * 1000, fallback_used=rank > 0)
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


if __name__ == "__main__":
    asyncio.run(main())
