"""Module 05 lab: context-window budgeting and a from-scratch Instructor-style structured-output loop.

- ContextBudgeter packs prompt sections by priority within a token budget and reports what was dropped.
- StructuredExtractor asks an LLM for JSON, validates with Pydantic v2, and feeds validation errors back
  (bounded repairs). A scripted fake LLM makes the demo deterministic. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import sys
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel, Field, ValidationError, model_validator


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


log = get_logger("lab05")


class LLMError(Exception):
    pass


class RateLimitError(LLMError):
    """429/503: retryable."""


class ContextOverflowError(LLMError):
    """Mandatory sections alone exceed the budget."""


class ExtractionFailed(LLMError):
    def __init__(self, attempts: int, last_errors: str) -> None:
        super().__init__(f"extraction failed after {attempts} attempts: {last_errors}")
        self.attempts, self.last_errors = attempts, last_errors


def estimate_tokens(text: str) -> int:
    """Cheap approximation (~4 chars/token for English). Replace with tiktoken / provider counter in prod."""
    return max(1, math.ceil(len(text) / 4))


class Section(BaseModel):
    name: str
    text: str
    priority: int = Field(ge=0, description="higher is kept first")
    mandatory: bool = False
    truncatable: bool = False


class PackedContext(BaseModel):
    text: str
    tokens: int
    dropped: list[str]
    truncated: list[str]


class ContextBudgeter:
    def __init__(self, model_context: int, reserved_output: int, safety_margin: int = 64) -> None:
        if reserved_output + safety_margin >= model_context:
            raise ValueError("reserved_output + safety_margin must be < model_context")
        self.limit = model_context - reserved_output - safety_margin

    def pack(self, sections: list[Section]) -> PackedContext:
        mandatory = [s for s in sections if s.mandatory]
        used = sum(estimate_tokens(s.text) for s in mandatory)
        if used > self.limit:
            raise ContextOverflowError(f"mandatory sections need {used} tokens, budget is {self.limit}")
        keep = {s.name: s.text for s in mandatory}
        dropped, truncated = [], []
        for s in sorted((s for s in sections if not s.mandatory), key=lambda s: -s.priority):
            need = estimate_tokens(s.text)
            if used + need <= self.limit:
                keep[s.name], used = s.text, used + need
            elif s.truncatable and self.limit - used > 20:
                marker = " …[truncated]"
                room_chars = max(0, (self.limit - used) * 4 - len(marker) - 4)  # stay strictly inside the budget
                keep[s.name] = s.text[:room_chars].rsplit(" ", 1)[0] + marker
                used, _ = used + estimate_tokens(keep[s.name]), truncated.append(s.name)
            else:
                dropped.append(s.name)
        ordered = [f"## {s.name}\n{keep[s.name]}" for s in sections if s.name in keep]  # original order, not priority
        return PackedContext(text="\n\n".join(ordered), tokens=used, dropped=dropped, truncated=truncated)


class Currency(str, Enum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


class Invoice(BaseModel):
    vendor: str = Field(min_length=2)
    issue_date: date
    due_date: date
    currency: Currency
    total: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    line_items: list[Decimal] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistency(self) -> "Invoice":
        if self.due_date < self.issue_date:
            raise ValueError("due_date must be on or after issue_date")
        if sum(self.line_items) != self.total:
            raise ValueError(f"line_items sum to {sum(self.line_items)} but total is {self.total}")
        return self


M = TypeVar("M", bound=BaseModel)


class LLM(Protocol):
    async def complete(self, messages: list[dict[str, str]]) -> str: ...


class ScriptedLLM:
    """Returns pre-scripted outputs, simulating malformed JSON, a bad date and a rate limit."""

    def __init__(self, outputs: list[str | Exception]) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        await asyncio.sleep(0.005)
        item = self._outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(raw: str) -> str:
    m = _FENCE.search(raw)
    text = m.group(1) if m else raw
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    return text[start:end + 1]


class StructuredExtractor(Generic[M]):
    def __init__(self, llm: LLM, schema: type[M], *, max_repairs: int = 2, max_transport_retries: int = 4) -> None:
        self._llm, self._schema = llm, schema
        self._max_repairs, self._max_retries = max_repairs, max_transport_retries

    def _system(self) -> str:
        return ("Extract data as a single JSON object matching this JSON Schema. Output JSON only.\n"
                + json.dumps(self._schema.model_json_schema()))

    async def _call(self, messages: list[dict[str, str]]) -> str:
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._llm.complete(messages)
            except RateLimitError:
                if attempt == self._max_retries:
                    raise
                delay = random.uniform(0, 0.01 * 2 ** attempt)
                log.warning("rate limited", extra={"ctx": {"attempt": attempt, "sleep_s": round(delay, 4)}})
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def extract(self, document: str) -> tuple[M, int]:
        messages = [{"role": "system", "content": self._system()}, {"role": "user", "content": document}]
        last_errors = ""
        for repair in range(self._max_repairs + 1):
            raw = await self._call(messages)
            try:
                return self._schema.model_validate_json(extract_json(raw)), repair
            except (ValidationError, ValueError) as exc:
                last_errors = self._format_errors(exc)
                log.info("validation failed", extra={"ctx": {"repair": repair, "errors": last_errors}})
                # send only the latest bad output + errors, not the growing history (cost control)
                messages = messages[:2] + [{"role": "assistant", "content": raw},
                                           {"role": "user", "content": f"Invalid. Fix these errors and return JSON only:\n{last_errors}"}]
        raise ExtractionFailed(self._max_repairs + 1, last_errors)

    @staticmethod
    def _format_errors(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in exc.errors())
        return str(exc)


GOOD = ('{"vendor":"Acme Corp","issue_date":"2025-03-01","due_date":"2025-03-31","currency":"USD",'
        '"total":"150.00","line_items":["100.00","50.00"]}')
BAD_DATE = GOOD.replace("2025-03-31", "2025-02-30")
BAD_SUM = GOOD.replace('"total":"150.00"', '"total":"160.00"')


async def main() -> None:
    packer = ContextBudgeter(model_context=512, reserved_output=128)
    packed = packer.pack([
        Section(name="system", text="You are a contracts analyst. Cite clause numbers.", priority=100, mandatory=True),
        Section(name="question", text="What is the termination notice period?", priority=100, mandatory=True),
        Section(name="retrieved-1", text="Clause 14.2 Either party may terminate on 60 days notice. " * 12, priority=90),
        Section(name="retrieved-2", text="Clause 9 governs liability caps and indemnities in detail. " * 25, priority=50, truncatable=True),
        Section(name="history", text="User asked about renewal terms earlier in the chat. " * 30, priority=10),
    ])
    log.info("context packed", extra={"ctx": {"tokens": packed.tokens, "limit": packer.limit, "dropped": packed.dropped, "truncated": packed.truncated}})
    try:
        packer.pack([Section(name="system", text="x" * 4000, priority=1, mandatory=True)])
    except ContextOverflowError as exc:
        log.info("overflow caught", extra={"ctx": {"error": str(exc)}})

    llm = ScriptedLLM([RateLimitError("429"), "Sure! ```json\n" + BAD_DATE + "\n```", BAD_SUM, GOOD])
    invoice, repairs = await StructuredExtractor(llm, Invoice).extract("Invoice from Acme Corp ... total $150")
    log.info("extracted", extra={"ctx": {"invoice": json.loads(invoice.model_dump_json()), "repairs": repairs, "llm_calls": llm.calls}})

    always_bad = ScriptedLLM(["not json at all"] * 3)
    try:
        await StructuredExtractor(always_bad, Invoice).extract("garbage")
    except ExtractionFailed as exc:
        log.info("routed to review", extra={"ctx": {"attempts": exc.attempts, "last_errors": exc.last_errors}})


if __name__ == "__main__":
    asyncio.run(main())
