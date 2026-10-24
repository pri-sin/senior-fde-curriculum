"""Module 10 lab: hierarchical multi-agent orchestration with consensus voting, fallbacks and risk-tiered
human-in-the-loop governance.

Supervisor fans work out to workers in parallel under a shared budget/deadline; workers degrade through
retry -> secondary -> rules-only; a consensus panel produces a verdict with an agreement ratio; the policy
engine decides auto / approval / blocked; an approval queue models human review. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
from collections import Counter
from enum import Enum
from typing import Awaitable, Callable

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


if __name__ == "__main__":
    asyncio.run(main())
