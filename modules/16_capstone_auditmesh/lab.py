"""Module 16 capstone: AuditMesh, an autonomous multi-agent regulatory-compliance verification engine.

Flow per requirement: retrieve evidence -> verbatim grounding -> panel of independent verifiers (presence,
quantitative, skeptic) -> confidence-weighted consensus -> governance routing to human review -> hash-chained
audit log + checkpoint. Crash/resume is demonstrated. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import sys
import tempfile
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

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


log = get_logger("auditmesh")


class AuditMeshError(Exception):
    pass


class VerifierTransientError(AuditMeshError):
    """Retryable verifier/model failure."""


class AuditChainBroken(AuditMeshError):
    pass


class SimulatedCrash(AuditMeshError):
    pass


class Verdict(str, Enum):
    COMPLIANT = "COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    INSUFFICIENT = "INSUFFICIENT_EVIDENCE"


class Severity(str, Enum):
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


class Check(BaseModel):
    type: Literal["presence", "max_hours", "max_months", "min_frequency_months"]
    param: float | None = None


class Requirement(BaseModel):
    req_id: str
    text: str
    severity: Severity
    check: Check
    keywords: list[str] = Field(min_length=1)


class Evidence(BaseModel):
    doc_id: str
    quote: str
    start: int


class Vote(BaseModel):
    verifier: str
    verdict: Verdict | None  # None = abstain
    confidence: float = Field(ge=0, le=1)
    rationale: str


class Finding(BaseModel):
    req_id: str
    verdict: Verdict
    agreement: float
    evidence: list[Evidence]
    votes: list[Vote]
    needs_human: bool
    reason: str


# ---------------- retrieval + grounding -------------------------------------------------------------------
def split_sentences(doc_id: str, text: str) -> list[Evidence]:
    return [Evidence(doc_id=doc_id, quote=m.group().strip(), start=m.start()) for m in re.finditer(r"[^.!?\n]+[.!?]?", text) if m.group().strip()]


def keyword_hits(sentence: str, keywords: list[str]) -> int:
    words = re.findall(r"[a-z0-9]+", sentence.lower())
    return sum(1 for k in keywords if any(w.startswith(k.lower()) for w in words))


def retrieve(req: Requirement, corpus: dict[str, str], k: int = 3) -> list[Evidence]:
    scored = [(keyword_hits(e.quote, req.keywords), e) for d, t in corpus.items() for e in split_sentences(d, t)]
    ranked = sorted((x for x in scored if x[0] > 0), key=lambda x: -x[0])
    return [e for _, e in ranked[:k]]


def ground(evidence: list[Evidence], corpus: dict[str, str]) -> list[Evidence]:
    """Keep only quotes that appear verbatim at the claimed offset. Defends against fabricated citations."""
    return [e for e in evidence if corpus.get(e.doc_id, "")[e.start:e.start + len(e.quote) + 2].strip().startswith(e.quote)]


# ---------------- verifiers -------------------------------------------------------------------------------
class Verifier(Protocol):
    name: str

    async def verify(self, req: Requirement, evidence: list[Evidence]) -> Vote: ...


NEGATION = re.compile(r"\b(not|never|no|without|except)\b", re.I)
HEDGE = re.compile(r"\b(should|may|where possible|generally|aim to|best effort|typically)\b", re.I)
FREQ_MONTHS = {"monthly": 1, "quarterly": 3, "semi-annually": 6, "semiannually": 6, "annually": 12, "yearly": 12}


def coverage(req: Requirement, sentence: str) -> float:
    return keyword_hits(sentence, req.keywords) / len(req.keywords)


class PresenceVerifier:
    name = "presence"

    def __init__(self, flaky_first: int = 0) -> None:
        self._fail = flaky_first

    async def verify(self, req: Requirement, evidence: list[Evidence]) -> Vote:
        await asyncio.sleep(0.001)
        if self._fail > 0:
            self._fail -= 1
            raise VerifierTransientError("model overloaded")
        if not evidence:
            return Vote(verifier=self.name, verdict=Verdict.INSUFFICIENT, confidence=0.9, rationale="no grounded evidence")
        best = max(evidence, key=lambda e: coverage(req, e.quote))
        if NEGATION.search(best.quote):
            return Vote(verifier=self.name, verdict=Verdict.NON_COMPLIANT, confidence=0.8, rationale=f"negation in evidence: {best.quote!r}")
        cov = coverage(req, best.quote)
        return Vote(verifier=self.name, verdict=Verdict.COMPLIANT if cov >= 0.6 else Verdict.INSUFFICIENT, confidence=round(0.5 + 0.4 * cov, 2),
                    rationale=f"keyword coverage {cov:.2f}")


class QuantitativeVerifier:
    name = "quantitative"

    async def verify(self, req: Requirement, evidence: list[Evidence]) -> Vote:
        await asyncio.sleep(0.001)
        if req.check.type == "presence" or not evidence:
            return Vote(verifier=self.name, verdict=None, confidence=0.0, rationale="not a quantitative requirement or no evidence")
        for e in evidence:
            q = e.quote.lower()
            if req.check.type == "max_hours" and (m := re.search(r"(\d+)\s*hours?", q)):
                ok = int(m.group(1)) <= req.check.param  # type: ignore[operator]
                return Vote(verifier=self.name, verdict=Verdict.COMPLIANT if ok else Verdict.NON_COMPLIANT, confidence=0.95,
                            rationale=f"{m.group(1)}h vs limit {req.check.param}h")
            if req.check.type == "max_months" and (m := re.search(r"(\d+)\s*months?", q)):
                ok = int(m.group(1)) <= req.check.param  # type: ignore[operator]
                return Vote(verifier=self.name, verdict=Verdict.COMPLIANT if ok else Verdict.NON_COMPLIANT, confidence=0.95,
                            rationale=f"{m.group(1)}mo vs limit {req.check.param}mo")
            if req.check.type == "min_frequency_months":
                for word, months in FREQ_MONTHS.items():
                    if word in q:
                        ok = months <= req.check.param  # type: ignore[operator]
                        return Vote(verifier=self.name, verdict=Verdict.COMPLIANT if ok else Verdict.NON_COMPLIANT, confidence=0.95,
                                    rationale=f"every {months}mo vs required every {req.check.param}mo")
        return Vote(verifier=self.name, verdict=Verdict.INSUFFICIENT, confidence=0.6, rationale="no parsable quantity in evidence")


class SkepticVerifier:
    name = "skeptic"

    async def verify(self, req: Requirement, evidence: list[Evidence]) -> Vote:
        await asyncio.sleep(0.001)
        if not evidence:
            return Vote(verifier=self.name, verdict=Verdict.INSUFFICIENT, confidence=0.9, rationale="nothing to verify")
        best = max(evidence, key=lambda e: coverage(req, e.quote))
        if NEGATION.search(best.quote):
            return Vote(verifier=self.name, verdict=Verdict.NON_COMPLIANT, confidence=0.7, rationale=f"negated control: {best.quote!r}")
        if HEDGE.search(best.quote):
            return Vote(verifier=self.name, verdict=Verdict.NON_COMPLIANT, confidence=0.7, rationale=f"hedged language is not a mandatory control: {best.quote!r}")
        if coverage(req, best.quote) < 0.8:
            return Vote(verifier=self.name, verdict=Verdict.INSUFFICIENT, confidence=0.6, rationale="weak keyword coverage")
        return Vote(verifier=self.name, verdict=Verdict.COMPLIANT, confidence=0.7, rationale="strong coverage, no hedging")


VETO_VERIFIERS = {"quantitative"}  # deterministic, code-based checks: their NON_COMPLIANT cannot be outvoted by lexical votes


def consensus(votes: list[Vote]) -> tuple[Verdict, float]:
    live = [v for v in votes if v.verdict is not None]
    if not live:
        return Verdict.INSUFFICIENT, 0.0
    vetoes = [v for v in live if v.verifier in VETO_VERIFIERS and v.verdict is Verdict.NON_COMPLIANT]
    if vetoes:
        return Verdict.NON_COMPLIANT, 1.0
    weight: Counter[Verdict] = Counter()
    for v in live:
        weight[v.verdict] += v.confidence  # type: ignore[index]
    verdict, w = weight.most_common(1)[0]
    return verdict, round(w / sum(weight.values()), 3)


# ---------------- audit chain -----------------------------------------------------------------------------
class AuditLog:
    """Append-only, hash-chained. Each entry commits to the previous entry's hash."""

    GENESIS = "0" * 64

    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    @property
    def head(self) -> str:
        return str(self.entries[-1]["hash"]) if self.entries else self.GENESIS

    def append(self, event: str, data: dict[str, object]) -> None:
        body = {"seq": len(self.entries), "event": event, "data": data, "prev": self.head}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        self.entries.append({**body, "hash": digest})

    def verify(self) -> None:
        prev = self.GENESIS
        for e in self.entries:
            body = {k: e[k] for k in ("seq", "event", "data", "prev")}
            if e["prev"] != prev or hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest() != e["hash"]:
                raise AuditChainBroken(f"chain broken at seq {e['seq']}")
            prev = str(e["hash"])


# ---------------- orchestrator ----------------------------------------------------------------------------
class AuditEngine:
    def __init__(self, corpus: dict[str, str], verifiers: list[Verifier], checkpoint: Path, concurrency: int = 4,
                 crash_after: int | None = None) -> None:
        self.corpus, self.verifiers, self.ckpt = corpus, verifiers, checkpoint
        self.sem, self.audit, self.crash_after = asyncio.Semaphore(concurrency), AuditLog(), crash_after
        self.verifier_calls = 0
        self.review_queue: list[str] = []
        self._done: dict[str, Finding] = {}
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text())
            self._done = {k: Finding.model_validate(v) for k, v in saved["findings"].items()}
            self.audit.entries = saved["audit"]
            self.audit.verify()  # refuse to resume from a tampered chain
            self.audit.append("resumed", {"findings_restored": len(self._done)})
            self.review_queue = [k for k, f in self._done.items() if f.needs_human]

    async def _ask(self, v: Verifier, req: Requirement, ev: list[Evidence]) -> Vote:
        for attempt in range(1, 4):
            self.verifier_calls += 1
            try:
                return await asyncio.wait_for(v.verify(req, ev), 0.5)
            except (VerifierTransientError, asyncio.TimeoutError):
                if attempt == 3:
                    return Vote(verifier=v.name, verdict=None, confidence=0.0, rationale="verifier unavailable (abstained)")
                await asyncio.sleep(random.uniform(0, 0.004 * 2 ** attempt))
        raise AssertionError("unreachable")

    async def audit_requirement(self, req: Requirement) -> Finding:
        async with self.sem:
            raw = retrieve(req, self.corpus)
            grounded = ground(raw, self.corpus)
            self.audit.append("retrieved", {"req": req.req_id, "candidates": len(raw), "grounded": len(grounded),
                                            "evidence_sha": [hashlib.sha256(e.quote.encode()).hexdigest()[:12] for e in grounded]})
            votes = list(await asyncio.gather(*(self._ask(v, req, grounded) for v in self.verifiers)))
            verdict, agreement = consensus(votes)
            if not grounded and verdict is Verdict.COMPLIANT:
                verdict = Verdict.INSUFFICIENT  # invariant: no grounded evidence can never yield COMPLIANT
            needs_human, reason = False, "auto-accepted"
            if agreement < 0.67:
                needs_human, reason = True, f"panel disagreement ({agreement})"
                if verdict is Verdict.COMPLIANT:
                    verdict = Verdict.INSUFFICIENT  # asymmetric risk: never leave an unconfirmed COMPLIANT on a contested finding
            elif verdict is Verdict.NON_COMPLIANT:
                needs_human, reason = True, "non-compliance finding requires confirmation"
            elif req.severity is Severity.CRITICAL:
                needs_human, reason = True, "critical requirement requires sign-off"
            elif verdict is Verdict.INSUFFICIENT:
                needs_human, reason = True, "evidence gap: request artifacts from control owner"
            finding = Finding(req_id=req.req_id, verdict=verdict, agreement=agreement, evidence=grounded, votes=votes, needs_human=needs_human, reason=reason)
            self.audit.append("finding", {"req": req.req_id, "verdict": verdict, "agreement": agreement, "needs_human": needs_human,
                                          "votes": [(v.verifier, v.verdict, v.confidence) for v in votes]})
            if needs_human:
                self.review_queue.append(req.req_id)
            return finding

    async def run(self, reqs: list[Requirement]) -> dict[str, Finding]:
        todo = [r for r in reqs if r.req_id not in self._done]
        log.info("run start", extra={"ctx": {"total": len(reqs), "resumed": len(reqs) - len(todo)}})

        async def one(r: Requirement) -> None:
            self._done[r.req_id] = await self.audit_requirement(r)
            self.ckpt.write_text(json.dumps({"findings": {k: v.model_dump(mode="json") for k, v in self._done.items()},
                                             "audit": self.audit.entries}, default=str))  # durable after each finding
            if self.crash_after is not None and len(self._done) >= self.crash_after:
                raise SimulatedCrash(f"process died after {len(self._done)} findings")

        try:
            async with asyncio.TaskGroup() as tg:  # first failure cancels in-flight siblings, like a dying process
                for r in todo:
                    tg.create_task(one(r))
        except ExceptionGroup as eg:
            crash = next((e for e in eg.exceptions if isinstance(e, SimulatedCrash)), None)
            raise crash or eg.exceptions[0] from None
        return self._done


POLICIES = {
    "infosec-policy": ("All personal data is encrypted at rest using AES-256. Backups are not encrypted at present. "
                       "Privileged access reviews are performed annually. Security incidents are logged in the ticketing system."),
    "privacy-policy": ("The company notifies the supervisory authority of a personal data breach within 96 hours. "
                       "Customer records are retained for 18 months after contract end. "
                       "Employees should complete privacy training where possible."),
}

REQS = [
    Requirement(req_id="R1", text="Personal data must be encrypted at rest", severity=Severity.CRITICAL, check=Check(type="presence"), keywords=["personal", "encrypt", "rest"]),
    Requirement(req_id="R2", text="Breaches must be notified within 72 hours", severity=Severity.CRITICAL, check=Check(type="max_hours", param=72), keywords=["notif", "breach"]),
    Requirement(req_id="R3", text="Privileged access reviewed at least quarterly", severity=Severity.HIGH, check=Check(type="min_frequency_months", param=3), keywords=["privileged", "access", "review"]),
    Requirement(req_id="R4", text="Vendors must sign data processing agreements", severity=Severity.HIGH, check=Check(type="presence"), keywords=["vendor", "processing", "agreement"]),
    Requirement(req_id="R5", text="Retention must not exceed 24 months", severity=Severity.LOW, check=Check(type="max_months", param=24), keywords=["retain", "records"]),
    Requirement(req_id="R6", text="Backups must be encrypted", severity=Severity.HIGH, check=Check(type="presence"), keywords=["backup", "encrypt"]),
    Requirement(req_id="R7", text="Staff must complete privacy training", severity=Severity.LOW, check=Check(type="presence"), keywords=["employee", "privacy", "training"]),
]


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="auditmesh-"))
    ckpt = tmp / "run.json"
    verifiers: list[Verifier] = [PresenceVerifier(flaky_first=2), QuantitativeVerifier(), SkepticVerifier()]

    engine = AuditEngine(POLICIES, verifiers, ckpt, concurrency=2, crash_after=3)
    try:
        await engine.run(REQS)
    except SimulatedCrash as exc:
        log.warning("crash", extra={"ctx": {"error": str(exc), "checkpointed": len(json.loads(ckpt.read_text())["findings"])}})

    engine2 = AuditEngine(POLICIES, [PresenceVerifier(), QuantitativeVerifier(), SkepticVerifier()], ckpt, concurrency=4)
    findings = await engine2.run(REQS)
    for req in REQS:
        f = findings[req.req_id]
        log.info("finding", extra={"ctx": {"req": req.req_id, "verdict": f.verdict.value, "agreement": f.agreement, "human": f.needs_human, "reason": f.reason,
                                            "evidence": [e.quote[:50] for e in f.evidence[:1]], "rationales": [v.rationale[:40] for v in f.votes]}})
    engine2.audit.verify()
    summary = Counter(f.verdict.value for f in findings.values())
    log.info("report", extra={"ctx": {"summary": dict(summary), "review_queue": engine2.review_queue, "chain_entries": len(engine2.audit.entries),
                                       "chain_head": engine2.audit.head[:16], "verifier_calls_after_resume": engine2.verifier_calls}})

    forged = Evidence(doc_id="infosec-policy", quote="Backups are encrypted nightly with AES-256.", start=0)
    log.info("fabricated citation grounded?", extra={"ctx": {"kept": len(ground([forged], POLICIES))}})
    engine2.audit.entries[2]["data"]["verdict"] = "COMPLIANT"  # type: ignore[index]
    try:
        engine2.audit.verify()
    except AuditChainBroken as exc:
        log.info("tamper detected", extra={"ctx": {"error": str(exc)}})


if __name__ == "__main__":
    asyncio.run(main())
