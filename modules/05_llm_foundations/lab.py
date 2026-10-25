"""Module 05 lab: context-window budgeting and a from-scratch Instructor-style structured-output loop.

Syllabus extension sections (prompting, tokens, windows, compression, schemas, tools) run from extension_demo().

- ContextBudgeter packs prompt sections by priority within a token budget and reports what was dropped.
- StructuredExtractor asks an LLM for JSON, validates with Pydantic v2, and feeds validation errors back
  (bounded repairs). A scripted fake LLM makes the demo deterministic. Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import copy
import difflib
import json
import logging
import math
import random
import re
import sys
import time
from collections import Counter
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Awaitable, Callable, Generic, Literal, Protocol, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator


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


# ---- Syllabus extension: shared helpers (retry with exponential backoff) ---
T = TypeVar("T")


async def retry_with_backoff(fn: Callable[[], Awaitable[T]], *, attempts: int = 3, base_delay: float = 0.01,
                             retry_on: tuple[type[BaseException], ...] = (RateLimitError,)) -> T:
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


class MissingAnswerError(LLMError):
    """Chain-of-thought output had no parseable final answer."""


class ToolArgumentError(LLMError):
    """Tool-call arguments were not valid JSON or failed schema validation."""


class ToolTransientError(LLMError):
    """A tool failed in a retryable way (upstream 503, connection reset)."""


class ToolLoopExceeded(LLMError):
    """The model kept calling tools past the round budget."""


# ---- Syllabus extension: zero-shot, few-shot and chain-of-thought prompting ---
Messages = list[dict[str, Any]]


def build_zero_shot(task: str, user_input: str) -> Messages:
    """Zero-shot: instructions only, no examples. Cheapest; fine when the task is common and the format is simple."""
    return [{"role": "system", "content": task}, {"role": "user", "content": user_input}]


def build_few_shot(task: str, examples: list[tuple[str, str]], user_input: str) -> Messages:
    """Few-shot: worked examples as alternating user/assistant turns. The model copies format AND label distribution,
    so keep examples balanced, representative, and in the exact output format you want back."""
    msgs: Messages = [{"role": "system", "content": task}]
    for question, answer in examples:
        msgs += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    return msgs + [{"role": "user", "content": user_input}]


COT_INSTRUCTION = ("Think step by step inside <reasoning> tags, then give only the final result inside <answer> tags. "
                   "Nothing after </answer>.")


def build_chain_of_thought(task: str, user_input: str) -> Messages:
    """Chain of thought: ask for intermediate steps before the answer. Helps multi-step arithmetic/logic; costs output tokens."""
    return [{"role": "system", "content": f"{task}\n{COT_INSTRUCTION}"}, {"role": "user", "content": user_input}]


class ReasonedAnswer(BaseModel):
    reasoning: str
    answer: str


_REASONING = re.compile(r"<reasoning>(.*?)</reasoning>", re.S)
_ANSWER = re.compile(r"<answer>(.*?)</answer>", re.S)


def parse_reasoned_output(raw: str) -> ReasonedAnswer:
    """Separate scratch-work from the result. Downstream code consumes ONLY `answer`.

    Why we never log or depend on `reasoning`: it can contain customer PII copied from the prompt, it is not a faithful
    record of why the model answered (it can be wrong while the answer is right, and vice versa), and providers may
    hide or summarise it. For audit, log the answer plus verifiable evidence (cited source spans, tool results).
    """
    a = _ANSWER.search(raw)
    if not a or not a.group(1).strip():
        raise MissingAnswerError("no <answer> block in model output")
    r = _REASONING.search(raw)
    return ReasonedAnswer(reasoning=r.group(1).strip() if r else "", answer=a.group(1).strip())


# ---- Syllabus extension: token calculation (byte-pair-ish tokenizer and cost) ---
_PRETOKEN = re.compile(r"[A-Za-z]+|\d|[^\sA-Za-z\d]")  # words, single digits, single punctuation marks


class MiniBPE:
    """Tiny byte-pair-encoding trainer/encoder. Real tokenizers (tiktoken, provider counters) are trained on huge
    corpora with byte-level vocabularies; this shows the mechanism: start from characters, repeatedly merge the most
    frequent adjacent pair. Counts are an approximation, so budget with a margin and use the provider counter in prod."""

    END = "</w>"

    def __init__(self, merges: list[tuple[str, str]]) -> None:
        self.merges = merges
        self._rank = {pair: i for i, pair in enumerate(merges)}

    @staticmethod
    def _symbols(word: str) -> list[str]:
        s = list(word)
        s[-1] += MiniBPE.END
        return s

    @classmethod
    def train(cls, corpus: str, num_merges: int) -> "MiniBPE":
        vocab: Counter[tuple[str, ...]] = Counter()
        for w, n in Counter(_PRETOKEN.findall(corpus.lower())).items():
            if w.isalpha():
                vocab[tuple(cls._symbols(w))] += n
        merges: list[tuple[str, str]] = []
        for _ in range(num_merges):
            pairs: Counter[tuple[str, str]] = Counter()
            for sym, n in vocab.items():
                for pair in zip(sym, sym[1:]):
                    pairs[pair] += n
            if not pairs:
                break
            best = max(pairs.items(), key=lambda kv: (kv[1], kv[0]))[0]  # deterministic tie-break
            merges.append(best)
            merged: Counter[tuple[str, ...]] = Counter()
            for sym, n in vocab.items():
                merged[tuple(cls._apply(list(sym), best))] += n
            vocab = merged
        return cls(merges)

    @staticmethod
    def _apply(sym: list[str], pair: tuple[str, str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(sym):
            if i < len(sym) - 1 and (sym[i], sym[i + 1]) == pair:
                out.append(sym[i] + sym[i + 1])
                i += 2
            else:
                out.append(sym[i])
                i += 1
        return out

    def encode_word(self, word: str) -> list[str]:
        sym = self._symbols(word)
        while len(sym) > 1:
            ranked = [(self._rank[p], p) for p in zip(sym, sym[1:]) if p in self._rank]
            if not ranked:
                break
            sym = self._apply(sym, min(ranked)[1])
        return sym

    def count(self, text: str) -> int:
        total = 0
        for piece in _PRETOKEN.findall(text.lower()):
            if not piece.isascii():
                total += len(piece.encode("utf-8"))  # unseen scripts fall back to one token per byte
            elif piece.isalpha():
                total += len(self.encode_word(piece))
            else:
                total += 1
        return total


class ModelPrice(BaseModel):
    """USD per million tokens. Values are supplied by the caller; check your provider's current price page."""
    name: str
    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cached_input_per_mtok: Decimal | None = Field(default=None, ge=0)


def chat_tokens(messages: Messages, counter: Callable[[str], int], per_message_overhead: int = 4) -> int:
    """Message framing (role markers etc.) adds a few tokens per message; the exact number is provider specific."""
    total = 0
    for m in messages:
        content = m.get("content")
        text = content if isinstance(content, str) else json.dumps(content, default=str)
        total += counter(text) + per_message_overhead
    return total


def request_cost(price: ModelPrice, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> Decimal:
    if cached_input_tokens > input_tokens:
        raise ValueError("cached_input_tokens cannot exceed input_tokens")
    cached_rate = price.cached_input_per_mtok if price.cached_input_per_mtok is not None else price.input_per_mtok
    total = ((input_tokens - cached_input_tokens) * price.input_per_mtok + cached_input_tokens * cached_rate
             + output_tokens * price.output_per_mtok) / Decimal(1_000_000)
    return total.quantize(Decimal("0.000001"))


# ---- Syllabus extension: sliding window over history and over long text ---
class SlidingWindowHistory:
    """Keep system messages plus the newest whole turns that fit the token budget. A turn starts at a plain-text user
    message and includes the assistant/tool messages that follow, so a tool result is never separated from the
    assistant tool call that produced it (providers reject orphaned tool results)."""

    def __init__(self, budget_tokens: int, counter: Callable[[str], int]) -> None:
        self.budget, self.counter = budget_tokens, counter

    def trim(self, messages: Messages) -> tuple[Messages, int]:
        system = [m for m in messages if m["role"] == "system"]
        turns: list[Messages] = []
        for m in (m for m in messages if m["role"] != "system"):
            if m["role"] == "user" and isinstance(m.get("content"), str) or not turns:
                turns.append([])
            turns[-1].append(m)
        used = chat_tokens(system, self.counter)
        kept: list[Messages] = []
        for turn in reversed(turns):
            need = chat_tokens(turn, self.counter)
            if used + need > self.budget:
                break  # stop at the first turn that does not fit: never leave a gap in the middle of history
            kept.append(turn)
            used += need
        if not kept and turns:
            raise ContextOverflowError(f"newest turn alone exceeds budget of {self.budget} tokens")
        return system + [m for turn in reversed(kept) for m in turn], len(turns) - len(kept)


def sliding_windows(items: list[str], size: int, stride: int) -> list[tuple[int, list[str]]]:
    """Overlapping windows over a long token/word list (overlap = size - stride) for map-reduce summarisation."""
    if size <= 0 or not 0 < stride <= size:
        raise ValueError("need size > 0 and 0 < stride <= size")
    out = [(s, items[s:s + size]) for s in range(0, max(1, len(items) - size + stride), stride)]
    return [w for w in out if w[1]]


# ---- Syllabus extension: context compression (extractive, budgeted) ---
_STOP = frozenset("the a an of to in and or is are was were be for on with as by that this it at from has have had".split())


class CompressedContext(BaseModel):
    text: str
    original_tokens: int
    compressed_tokens: int
    kept_sentences: list[int]

    @property
    def ratio(self) -> float:
        return round(self.compressed_tokens / max(1, self.original_tokens), 3)


def compress_context(text: str, budget_tokens: int, counter: Callable[[str], int], query: str = "") -> CompressedContext:
    """Extractive summarisation: score sentences by corpus term frequency, query overlap and lead position, then keep
    the best ones that fit the budget, in original order. Cheap and deterministic; an LLM summariser is the abstractive
    alternative (better prose, costs a call, can hallucinate)."""
    if budget_tokens < 1:
        raise ValueError("budget_tokens must be >= 1")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    content = [[w for w in tokenize_words(s) if w not in _STOP] for s in sentences]
    freq = Counter(w for ws in content for w in ws)
    top = max(freq.values(), default=1)
    q = {w for w in tokenize_words(query) if w not in _STOP}
    scores = []
    for i, ws in enumerate(content):
        tf = sum(freq[w] / top for w in ws) / (len(ws) or 1)
        scores.append(tf + 2.0 * (len(q & set(ws)) / (len(q) or 1)) + (0.25 if i == 0 else 0.0))
    kept: list[int] = []
    used = 0
    for i in sorted(range(len(sentences)), key=lambda i: -scores[i]):
        need = counter(sentences[i])
        if used + need <= budget_tokens:
            kept.append(i)
            used += need
    kept.sort()
    return CompressedContext(text=" ".join(sentences[i] for i in kept), original_tokens=counter(text),
                             compressed_tokens=used, kept_sentences=kept)


def tokenize_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


# ---- Syllabus extension: strict JSON schemas, nested models, type-hint validation, discriminated unions ---
class StrictModel(BaseModel):
    """extra=forbid: an invented field is a validation error (and `additionalProperties: false` in the JSON Schema)."""
    model_config = ConfigDict(extra="forbid")


class Address(StrictModel):
    street: str
    city: str
    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2")


class LineItem(StrictModel):
    sku: str
    quantity: int = Field(ge=1)
    unit_price: Decimal = Field(ge=0, decimal_places=2)


class Party(StrictModel):
    name: str
    address: Address
    contacts: list[str] = Field(default_factory=list)


class PurchaseOrder(StrictModel):
    """Three levels deep: order -> parties -> address, plus a keyed mapping of lists of models."""
    po_number: str
    buyer: Party
    seller: Party
    items: list[LineItem] = Field(min_length=1)
    shipments: dict[str, list[LineItem]] = Field(default_factory=dict, description="warehouse -> items")


class EmailAction(StrictModel):
    kind: Literal["email"]
    to: str
    subject: str


class CallAction(StrictModel):
    kind: Literal["call"]
    phone: str
    minutes: int = Field(ge=1, le=120)


class MeetingAction(StrictModel):
    kind: Literal["meeting"]
    attendees: list[str] = Field(min_length=1)
    duration_minutes: int = Field(ge=5)


# The discriminator makes validation pick ONE branch by `kind` and report errors for that branch only,
# instead of "none of the 3 union members matched" noise that confuses repair prompts.
NextAction = Annotated[Union[EmailAction, CallAction, MeetingAction], Field(discriminator="kind")]


class ActionPlan(StrictModel):
    account: str
    actions: list[NextAction] = Field(min_length=1)


def to_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Shape a Pydantic JSON Schema for provider-side strict structured outputs: every object closed
    (additionalProperties false) and every property listed in `required`. Defaults become required fields, so model
    optionals as nullable types. Providers support only a subset of JSON Schema: check the current docs."""
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            if out.get("type") == "object" and isinstance(out.get("properties"), dict):
                out["additionalProperties"] = False
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(copy.deepcopy(schema))  # type: ignore[no-any-return]


def openai_response_format(model: type[BaseModel]) -> dict[str, Any]:
    """OpenAI-style `response_format` for json_schema structured outputs."""
    return {"type": "json_schema", "json_schema": {"name": model.__name__, "strict": True,
                                                   "schema": to_strict_schema(model.model_json_schema())}}


def anthropic_forced_tool(model: type[BaseModel]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Anthropic-style schema enforcement: expose the schema as a tool and force it with tool_choice."""
    tool = {"name": model.__name__, "description": f"Return a {model.__name__}.", "input_schema": model.model_json_schema()}
    return tool, {"type": "tool", "name": model.__name__}


def validate_against_hint(hint: Any, raw_json: str, *, strict: bool = False) -> Any:
    """Validate raw model output against ANY type hint (list[int], dict[str, Contract], a union...) with no wrapper
    model. strict=True refuses silent coercion such as the string "3" becoming the int 3."""
    return TypeAdapter(hint).validate_json(raw_json, strict=strict)


# ---- Syllabus extension: function/tool schemas, argument validation, parallel execution ---
ToolFn = Callable[[Any], Awaitable[Any]]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: Union[str, dict[str, Any]]  # OpenAI sends a JSON string; Anthropic sends an already-parsed object


class ToolResult(BaseModel):
    call_id: str
    name: str
    content: str
    is_error: bool = False


class AssistantTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class RegisteredTool:
    def __init__(self, name: str, description: str, args_model: type[BaseModel], fn: ToolFn) -> None:
        self.name, self.description, self.args_model, self.fn = name, description, args_model, fn

    def openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.args_model.model_json_schema()}}

    def anthropic_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.args_model.model_json_schema()}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, name: str, description: str, args_model: type[BaseModel], fn: ToolFn) -> None:
        if name in self._tools:
            raise ValueError(f"duplicate tool name {name}")
        self._tools[name] = RegisteredTool(name, description, args_model, fn)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, style: str) -> list[dict[str, Any]]:
        return [t.openai_schema() if style == "openai" else t.anthropic_schema() for t in self._tools.values()]


def parse_tool_arguments(tool: RegisteredTool, raw: Union[str, dict[str, Any]]) -> BaseModel:
    """Never call a tool with unvalidated model output: parse JSON, then validate against the tool's args model."""
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise ToolArgumentError(f"arguments are not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ToolArgumentError("arguments must be a JSON object")
    try:
        return tool.args_model.model_validate(data)
    except ValidationError as exc:
        raise ToolArgumentError(StructuredExtractor._format_errors(exc)) from exc


async def execute_tool_call(registry: ToolRegistry, call: ToolCall, *, timeout_s: float = 1.0, attempts: int = 3) -> ToolResult:
    """Run one call and ALWAYS return a ToolResult carrying the call id (errors become is_error results the model can
    read). Transient errors are retried with backoff; timeouts are not retried because the tool may not be idempotent."""
    def fail(msg: str) -> ToolResult:
        return ToolResult(call_id=call.id, name=call.name, content=msg, is_error=True)

    tool = registry.get(call.name)
    if tool is None:
        return fail(f"unknown tool {call.name}")
    try:
        args = parse_tool_arguments(tool, call.arguments)
    except ToolArgumentError as exc:
        return fail(f"invalid arguments: {exc}")
    try:
        value = await retry_with_backoff(lambda: asyncio.wait_for(tool.fn(args), timeout_s),
                                         attempts=attempts, retry_on=(ToolTransientError,))
    except asyncio.TimeoutError:
        return fail(f"tool timed out after {timeout_s}s")
    except ToolTransientError as exc:
        return fail(f"tool unavailable after {attempts} attempts: {exc}")
    except Exception as exc:  # noqa: BLE001 - one crashing tool must not sink its siblings
        log.error("tool crashed", extra={"ctx": {"tool": call.name, "error": type(exc).__name__}})
        return fail(f"tool raised {type(exc).__name__}")
    return ToolResult(call_id=call.id, name=call.name, content=value if isinstance(value, str) else json.dumps(value, default=str))


async def execute_tool_calls(registry: ToolRegistry, calls: list[ToolCall], *, timeout_s: float = 1.0) -> list[ToolResult]:
    """Parallel fan-out. gather() returns results in input order, so ids line up even if tools finish out of order."""
    return list(await asyncio.gather(*(execute_tool_call(registry, c, timeout_s=timeout_s) for c in calls)))


# ---- Syllabus extension: tool results in chat history, hallucinated tool calls, tool loop ---
def assistant_tool_message(turn: AssistantTurn, style: str) -> dict[str, Any]:
    """The assistant message that REQUESTED the tools must be in history before any result is."""
    if style == "openai":
        return {"role": "assistant", "content": turn.text or None, "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.arguments if isinstance(c.arguments, str) else json.dumps(c.arguments)}}
            for c in turn.tool_calls]}
    blocks: list[dict[str, Any]] = [{"type": "text", "text": turn.text}] if turn.text else []
    for c in turn.tool_calls:
        if isinstance(c.arguments, dict):
            parsed: Any = c.arguments
        else:
            try:
                parsed = json.loads(c.arguments)
            except json.JSONDecodeError:
                parsed = {"_unparsed_arguments": c.arguments}
        blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": parsed})
    return {"role": "assistant", "content": blocks}


def tool_result_messages(results: list[ToolResult], style: str, max_chars: int = 2000) -> Messages:
    """OpenAI style: one role=tool message per call id. Anthropic style: ONE user message holding a tool_result block per
    tool_use id. Every requested id needs a result, including failed and hallucinated calls. Long results are clipped."""
    def clip(s: str) -> str:
        return s if len(s) <= max_chars else s[:max_chars] + " …[clipped]"
    if style == "openai":
        return [{"role": "tool", "tool_call_id": r.call_id, "content": clip(r.content)} for r in results]
    return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": r.call_id, "content": clip(r.content),
                                          "is_error": r.is_error} for r in results]}]


class ToolCallGuard:
    """Pre-flight checks for hallucinated or runaway tool calls. Returns an error ToolResult for the model to read
    (it usually self-corrects when told the valid names or the exact argument errors), or None if the call is fine."""

    def __init__(self, registry: ToolRegistry, max_identical_calls: int = 1, max_calls_per_turn: int = 8) -> None:
        self.registry, self.max_identical, self.max_per_turn = registry, max_identical_calls, max_calls_per_turn
        self._seen_ids: set[str] = set()
        self._seen_calls: Counter[str] = Counter()

    def check(self, call: ToolCall, index_in_turn: int) -> ToolResult | None:
        def fail(msg: str) -> ToolResult:
            return ToolResult(call_id=call.id, name=call.name, content=msg, is_error=True)

        if not call.id or call.id in self._seen_ids:
            return fail("tool call id missing or reused; ids must be unique")
        self._seen_ids.add(call.id)
        if index_in_turn >= self.max_per_turn:
            return fail(f"too many tool calls in one turn (max {self.max_per_turn}); call fewer tools")
        tool = self.registry.get(call.name)
        if tool is None:
            close = difflib.get_close_matches(call.name, self.registry.names(), n=2)
            hint = f" Did you mean {' or '.join(close)}?" if close else ""
            return fail(f"Unknown tool '{call.name}'. Available tools: {', '.join(self.registry.names())}.{hint}")
        try:
            args = parse_tool_arguments(tool, call.arguments)
        except ToolArgumentError as exc:
            return fail(f"Invalid arguments for {call.name}: {exc}. Fix them and call again.")
        key = call.name + json.dumps(args.model_dump(mode="json"), sort_keys=True)
        self._seen_calls[key] += 1
        if self._seen_calls[key] > self.max_identical:
            return fail(f"You already called {call.name} with identical arguments. Reuse the earlier result or change the arguments.")
        return None


class ToolLLM(Protocol):
    async def complete(self, messages: Messages, tools: list[dict[str, Any]]) -> AssistantTurn: ...


class ScriptedToolLLM:
    """Deterministic stand-in for a tool-calling model."""

    def __init__(self, turns: list[AssistantTurn | Exception]) -> None:
        self._turns, self.calls = list(turns), 0

    async def complete(self, messages: Messages, tools: list[dict[str, Any]]) -> AssistantTurn:
        self.calls += 1
        await asyncio.sleep(0.002)
        item = self._turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def run_tool_loop(llm: ToolLLM, registry: ToolRegistry, messages: Messages, *, style: str = "openai",
                        max_rounds: int = 5, timeout_s: float = 0.2) -> str:
    """Model -> (guard -> parallel execute) -> results appended -> model, until it answers in plain text."""
    guard = ToolCallGuard(registry)
    schemas = registry.schemas(style)
    for round_no in range(1, max_rounds + 1):
        turn = await retry_with_backoff(lambda: llm.complete(messages, schemas), retry_on=(RateLimitError,))
        if not turn.tool_calls:
            messages.append({"role": "assistant", "content": turn.text})
            return turn.text
        rejected = {c.id: r for i, c in enumerate(turn.tool_calls) if (r := guard.check(c, i)) is not None}
        runnable = [c for c in turn.tool_calls if c.id not in rejected]
        executed = {r.call_id: r for r in await execute_tool_calls(registry, runnable, timeout_s=timeout_s)}
        results = [rejected.get(c.id) or executed[c.id] for c in turn.tool_calls]  # original call order
        log.info("tool round", extra={"ctx": {"round": round_no, "calls": [c.name for c in turn.tool_calls],
                                              "errors": [r.name for r in results if r.is_error]}})
        messages.append(assistant_tool_message(turn, style))
        messages.extend(tool_result_messages(results, style))
    raise ToolLoopExceeded(f"no final answer after {max_rounds} rounds")


class WeatherArgs(StrictModel):
    city: str
    unit: Literal["c", "f"] = "c"


class TickerArgs(StrictModel):
    ticker: str = Field(pattern=r"^[A-Z]{1,5}$")


class NoArgs(StrictModel):
    pass


def build_demo_registry() -> ToolRegistry:
    state = {"flaky_calls": 0}

    async def get_weather(a: WeatherArgs) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        return {"city": a.city, "temp": 21 if a.unit == "c" else 70, "unit": a.unit}

    async def get_stock_price(a: TickerArgs) -> dict[str, Any]:
        state["flaky_calls"] += 1
        if state["flaky_calls"] == 1:
            raise ToolTransientError("quote service 503")
        await asyncio.sleep(0.005)
        return {"ticker": a.ticker, "price": 101.5}

    async def slow_report(a: NoArgs) -> str:
        await asyncio.sleep(1.0)
        return "never delivered"

    reg = ToolRegistry()
    reg.register("get_weather", "Current weather for a city.", WeatherArgs, get_weather)
    reg.register("get_stock_price", "Latest price for a stock ticker.", TickerArgs, get_stock_price)
    reg.register("slow_report", "Generate a long report.", NoArgs, slow_report)
    return reg


EXAMPLES_SENTIMENT = [("The renewal went smoothly, thanks!", "positive"), ("Still waiting on a reply after 3 weeks.", "negative")]
SAMPLE_DOC = ("Quarterly review notes. The onboarding team shipped the new import wizard in March. Customers using the import "
              "wizard reduced setup time by half. The cafeteria menu changed again. Support tickets about import errors "
              "dropped sharply after the wizard launched. Parking validation is now handled by the front desk. "
              "The wizard still lacks support for large spreadsheet files, which remains the top open request.")
BPE_CORPUS = ("the quick extraction pipeline validates the response and retries the request when the response fails "
              "validation. customers expect the pipeline to retry the failed requests and to report the validation errors.")

PLAN_BAD_KIND = '{"account":"Acme","actions":[{"kind":"fax","number":"555"}]}'
PLAN_GOOD = ('{"account":"Acme","actions":[{"kind":"email","to":"cfo@acme.test","subject":"Renewal"},'
             '{"kind":"call","phone":"+15550100","minutes":15}]}')
PO_JSON = ('{"po_number":"PO-1","buyer":{"name":"Acme","address":{"street":"1 Main","city":"Austin","country":"US"}},'
           '"seller":{"name":"Globex","address":{"street":"9 High","city":"Leeds","country":"GB"},"contacts":["a@g.test"]},'
           '"items":[{"sku":"A1","quantity":2,"unit_price":"9.99"}],'
           '"shipments":{"wh-east":[{"sku":"A1","quantity":2,"unit_price":"9.99"}]}}')


async def extension_demo() -> None:
    # 1-2. zero-shot, few-shot, chain of thought (scripted model; the point is the message shapes and the parser)
    zs = build_zero_shot("Classify sentiment as positive or negative.", "Great support team")
    fs = build_few_shot("Classify sentiment as positive or negative.", EXAMPLES_SENTIMENT, "Great support team")
    cot = build_chain_of_thought("Solve the word problem.", "A seat costs $12 and we buy 7 with a $9 discount. Total?")
    log.info("prompt shapes", extra={"ctx": {"zero_shot_msgs": len(zs), "few_shot_msgs": len(fs), "cot_system": cot[0]["content"][-60:]}})
    llm = ScriptedLLM(["positive", "<reasoning>7 x 12 = 84; 84 - 9 = 75</reasoning><answer>$75</answer>"])
    label = await llm.complete(fs)
    parsed = parse_reasoned_output(await llm.complete(cot))
    log.info("cot parsed", extra={"ctx": {"label": label, "answer": parsed.answer, "reasoning_chars": len(parsed.reasoning)}})  # never log the reasoning text
    try:
        parse_reasoned_output("<reasoning>thinking...</reasoning>")
    except MissingAnswerError as exc:
        log.info("missing answer caught", extra={"ctx": {"error": str(exc)}})

    # 3. token calculation
    bpe = MiniBPE.train(BPE_CORPUS, num_merges=60)
    counter = bpe.count
    sample = "Customers expect the pipeline to retry failed validation requests, 3 times."
    price = ModelPrice(name="illustrative", input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"), cached_input_per_mtok=Decimal("0.3"))
    prompt_tokens = chat_tokens(fs, counter)
    log.info("token calc", extra={"ctx": {"bpe_tokens": counter(sample), "chars_div_4": estimate_tokens(sample), "word_pieces": bpe.encode_word("validation"),
                                          "few_shot_prompt_tokens": prompt_tokens,
                                          "cost_usd_uncached": str(request_cost(price, 10_000, 500)),
                                          "cost_usd_90pct_cached": str(request_cost(price, 10_000, 500, cached_input_tokens=9_000))}})

    # 4. sliding windows
    history: Messages = [{"role": "system", "content": "You are a helpful analyst."}]
    for i in range(6):
        history += [{"role": "user", "content": f"question number {i} " * 6}, {"role": "assistant", "content": f"answer number {i} " * 6}]
    trimmed, dropped = SlidingWindowHistory(400, counter).trim(history)
    wins = sliding_windows(SAMPLE_DOC.split(), size=20, stride=15)
    log.info("sliding window", extra={"ctx": {"before": len(history), "after": len(trimmed), "dropped_turns": dropped,
                                              "first_kept": trimmed[1]["content"][:18], "doc_windows": [(s, len(w)) for s, w in wins]}})

    # 5. context compression
    comp = compress_context(SAMPLE_DOC, budget_tokens=110, counter=counter, query="import wizard")
    log.info("compressed", extra={"ctx": {"ratio": comp.ratio, "kept": comp.kept_sentences, "text": comp.text}})

    # 6-8. strict schemas, nested models, type-hint validation
    fmt = openai_response_format(ActionPlan)
    tool, choice = anthropic_forced_tool(ActionPlan)
    log.info("schema enforcement", extra={"ctx": {"openai_format_keys": sorted(fmt["json_schema"]), "strict": fmt["json_schema"]["strict"],
                                                  "additional_properties_allowed": fmt["json_schema"]["schema"]["additionalProperties"],
                                                  "anthropic_tool_choice": choice, "anthropic_tool": tool["name"]}})
    po = PurchaseOrder.model_validate_json(PO_JSON)
    try:
        PurchaseOrder.model_validate_json(PO_JSON.replace('"po_number":"PO-1"', '"po_number":"PO-1","surprise":true'))
    except ValidationError as exc:
        log.info("extra field rejected", extra={"ctx": {"error": exc.errors()[0]["type"]}})
    log.info("nested model", extra={"ctx": {"buyer_country": po.buyer.address.country, "shipment_items": len(po.shipments["wh-east"])}})
    lax = validate_against_hint(dict[str, list[int]], '{"a":["1","2"]}')
    try:
        validate_against_hint(dict[str, list[int]], '{"a":["1","2"]}', strict=True)
    except ValidationError as exc:
        log.info("type hints", extra={"ctx": {"lax": lax, "strict_error": exc.errors()[0]["type"]}})

    # 9-10. repair loop on a discriminated union (reuses StructuredExtractor)
    union_llm = ScriptedLLM([PLAN_BAD_KIND, PLAN_GOOD])
    plan, repairs = await StructuredExtractor(union_llm, ActionPlan).extract("Follow up with Acme about renewal")
    round_trip = ActionPlan.model_validate_json(plan.model_dump_json())
    log.info("discriminated union", extra={"ctx": {"repairs": repairs, "kinds": [type(a).__name__ for a in round_trip.actions], "equal": round_trip == plan}})
    try:
        ActionPlan.model_validate_json(PLAN_BAD_KIND)
    except ValidationError as exc:
        e = exc.errors()[0]
        log.info("discriminator error", extra={"ctx": {"type": e["type"], "loc": list(map(str, e["loc"]))}})

    # 11. function schemas in both vendor shapes
    registry = build_demo_registry()
    log.info("tool schemas", extra={"ctx": {"openai": registry.schemas("openai")[0]["function"]["parameters"]["properties"],
                                            "anthropic_keys": sorted(registry.schemas("anthropic")[0])}})

    # 12-13. argument validation and parallel execution with per-tool timeout, preserving call ids
    calls = [ToolCall(id="c1", name="get_weather", arguments='{"city":"Paris"}'),
             ToolCall(id="c2", name="get_stock_price", arguments={"ticker": "ACME"}),
             ToolCall(id="c3", name="slow_report", arguments="{}"),
             ToolCall(id="c4", name="get_weather", arguments='{"city":"Rome","unit":"kelvin"}')]
    started = time.perf_counter()
    results = await execute_tool_calls(registry, calls, timeout_s=0.1)
    log.info("parallel tools", extra={"ctx": {"elapsed_under_slowest": time.perf_counter() - started < 0.5,
                                              "results": [(r.call_id, r.is_error, r.content[:48]) for r in results]}})

    # 14. results into history, both shapes
    turn = AssistantTurn(tool_calls=calls[:2])
    for style in ("openai", "anthropic"):
        msgs = [assistant_tool_message(turn, style)] + tool_result_messages(results[:2], style)
        log.info("history shape", extra={"ctx": {"style": style, "roles": [m["role"] for m in msgs], "ids": [r.call_id for r in results[:2]]}})

    # 15. hallucinated tool calls end to end
    registry = build_demo_registry()
    scripted = ScriptedToolLLM([
        RateLimitError("429"),
        AssistantTurn(tool_calls=[ToolCall(id="a1", name="get_weather", arguments='{"city":"Oslo"}'),
                                  ToolCall(id="a2", name="get_weathr", arguments='{"city":"Oslo"}'),
                                  ToolCall(id="a3", name="get_stock_price", arguments='{"ticker":"acme!"}')]),
        AssistantTurn(tool_calls=[ToolCall(id="b1", name="get_weather", arguments='{"city":"Oslo"}'),
                                  ToolCall(id="b2", name="get_stock_price", arguments='{"ticker":"ACME"}')]),
        AssistantTurn(text="Oslo is 21C and ACME trades at 101.5.")])
    chat: Messages = [{"role": "user", "content": "Weather in Oslo and the ACME price?"}]
    final = await run_tool_loop(scripted, registry, chat)
    errors = [m["content"][:60] for m in chat if m["role"] == "tool" and m["content"].startswith(("Unknown", "Invalid", "You already"))]
    log.info("tool loop", extra={"ctx": {"final": final, "llm_calls": scripted.calls, "messages": len(chat), "error_results": errors}})
    looping = ScriptedToolLLM([AssistantTurn(tool_calls=[ToolCall(id=f"r{i}", name="get_weather", arguments='{"city":"X"}')]) for i in range(4)])
    try:
        await run_tool_loop(looping, build_demo_registry(), [{"role": "user", "content": "loop"}], max_rounds=3)
    except ToolLoopExceeded as exc:
        log.info("runaway loop stopped", extra={"ctx": {"error": str(exc)}})


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
    await extension_demo()


if __name__ == "__main__":
    asyncio.run(main())
