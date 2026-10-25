"""Module 02 lab: per-tenant token-bucket admission control and SSE streaming with backpressure.

The core (TokenBucketLimiter, StreamSession, sse_format) is dependency-free beyond Pydantic so the demo
runs offline: `python3 lab.py`. `create_app()` wires the same core into FastAPI (SSE + WebSocket) when
fastapi is installed: `uvicorn lab:app --port 8000` (after `pip install fastapi uvicorn`).
"""
from __future__ import annotations

import asyncio
import dis
import faulthandler
import inspect
import io
import json
import logging
import random
import re
import sys
import sysconfig
import tempfile
import time
import trace
import types
import unittest
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, TypeVar
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from pydantic import (BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator,
                      model_validator)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": round(record.created, 3), "level": record.levelname, "msg": record.getMessage()}
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


log = get_logger("lab02")


class ApiError(Exception):
    status_code = 500


class RateLimitedError(ApiError):
    status_code = 429

    def __init__(self, tenant_id: str, retry_after_s: float) -> None:
        super().__init__(f"tenant {tenant_id} rate limited")
        self.retry_after_s = retry_after_s


class SlowConsumerError(ApiError):
    status_code = 408


class UpstreamError(ApiError):
    status_code = 502


class BucketConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    capacity: float = Field(gt=0, description="max burst in tokens")
    refill_per_s: float = Field(gt=0)


class ChatRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=8000)
    max_tokens: int = Field(default=64, ge=1, le=512)

    @property
    def cost(self) -> float:
        """Cost-weighted admission: long prompts and big completions consume more budget."""
        return 1.0 + len(self.prompt) / 1000 + self.max_tokens / 128


class TokenBucketLimiter:
    """Lazy-refill token bucket per key. Single-process; swap `_state` for Redis+Lua when sharded."""

    def __init__(self, default: BucketConfig, overrides: dict[str, BucketConfig] | None = None,
                 clock: Any = time.monotonic) -> None:
        self._default = default
        self._overrides = overrides or {}
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    def _cfg(self, key: str) -> BucketConfig:
        return self._overrides.get(key, self._default)

    async def acquire(self, key: str, cost: float = 1.0) -> None:
        cfg = self._cfg(key)
        if cost > cfg.capacity:
            raise RateLimitedError(key, retry_after_s=float("inf"))
        async with self._lock:
            now = self._clock()
            tokens, last = self._state.get(key, (cfg.capacity, now))
            tokens = min(cfg.capacity, tokens + (now - last) * cfg.refill_per_s)
            if tokens < cost:
                self._state[key] = (tokens, now)
                raise RateLimitedError(key, retry_after_s=round((cost - tokens) / cfg.refill_per_s, 3))
            self._state[key] = (tokens - cost, now)


def sse_format(event: str, data: BaseModel | dict[str, Any], event_id: int | None = None) -> str:
    body = data.model_dump_json() if isinstance(data, BaseModel) else json.dumps(data)
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event}\ndata: {body}\n\n"


class Outcome(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    CLIENT_SLOW = "client_slow"
    UPSTREAM_ERROR = "upstream_error"


class TokenEvent(BaseModel):
    index: int
    text: str


class FakeLLM:
    """Streams tokens with jitter; fails mid-stream when prompt contains 'boom'."""

    async def stream(self, prompt: str, max_tokens: int) -> AsyncIterator[str]:
        words = (prompt.split() or ["ok"]) * max_tokens
        for i, w in enumerate(words[:max_tokens]):
            await asyncio.sleep(random.uniform(0.001, 0.01))
            if "boom" in prompt and i == 3:
                raise UpstreamError("provider reset connection")
            yield w


class StreamSession:
    """Producer/consumer pair joined by a bounded queue; slow consumers are cut off and spend is cancelled."""

    def __init__(self, llm: FakeLLM, *, queue_size: int = 8, slow_after_s: float = 0.5,
                 heartbeat_s: float = 15.0, attempts: int = 3) -> None:
        self._llm, self._queue_size = llm, queue_size
        self._slow_after_s, self._heartbeat_s, self._attempts = slow_after_s, heartbeat_s, attempts
        self.outcome: Outcome | None = None

    async def _produce(self, req: ChatRequest, q: asyncio.Queue[TokenEvent | Exception | None]) -> None:
        delay = 0.05
        for attempt in range(1, self._attempts + 1):
            emitted = 0
            try:
                async for tok in self._llm.stream(req.prompt, req.max_tokens):
                    await asyncio.wait_for(q.put(TokenEvent(index=emitted, text=tok)), self._slow_after_s)
                    emitted += 1
                await q.put(None)
                return
            except asyncio.TimeoutError:
                await _put_nowait_safe(q, SlowConsumerError("client not reading"))
                return
            except UpstreamError as exc:
                if emitted > 0 or attempt == self._attempts:  # cannot replay a partially delivered stream
                    await _put_nowait_safe(q, exc)
                    return
                log.warning("upstream retry", extra={"ctx": {"attempt": attempt}})
                await asyncio.sleep(random.uniform(0, delay))
                delay *= 2

    async def run(self, req: ChatRequest) -> AsyncIterator[str]:
        q: asyncio.Queue[TokenEvent | Exception | None] = asyncio.Queue(maxsize=self._queue_size)
        producer = asyncio.create_task(self._produce(req, q))
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), self._heartbeat_s)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    self.outcome = Outcome.COMPLETED
                    yield sse_format("done", {"ok": True})
                    return
                if isinstance(item, SlowConsumerError):
                    self.outcome = Outcome.CLIENT_SLOW
                    yield sse_format("error", {"code": item.status_code, "message": str(item)})
                    return
                if isinstance(item, Exception):
                    self.outcome = Outcome.UPSTREAM_ERROR
                    yield sse_format("error", {"code": 502, "message": str(item)})
                    return
                yield sse_format("token", item, event_id=item.index)
        except asyncio.CancelledError:
            self.outcome = Outcome.CANCELLED  # client disconnected
            raise
        finally:
            producer.cancel()  # stops upstream spend immediately
            await asyncio.gather(producer, return_exceptions=True)
            log.info("stream closed", extra={"ctx": {"tenant": req.tenant_id, "outcome": self.outcome}})


async def _put_nowait_safe(q: asyncio.Queue[Any], item: Any) -> None:
    while q.full():
        q.get_nowait()
    q.put_nowait(item)


def create_app() -> Any:
    """FastAPI wiring. Imported lazily so the core above has no web-framework dependency."""
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="Module 02 streaming API")
    limiter = TokenBucketLimiter(BucketConfig(capacity=20, refill_per_s=5),
                                 {"enterprise": BucketConfig(capacity=200, refill_per_s=50)})
    llm = FakeLLM()

    @app.exception_handler(RateLimitedError)
    async def _rl(_: Any, exc: RateLimitedError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=429, headers={"Retry-After": str(max(1, int(exc.retry_after_s)))})

    @app.post("/v1/chat/stream")
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        await limiter.acquire(req.tenant_id, req.cost)
        session = StreamSession(llm)
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
        return StreamingResponse(session.run(req), media_type="text/event-stream", headers=headers)

    async def chat_ws(ws: WebSocket) -> None:
        await ws.accept()
        try:
            req = ChatRequest.model_validate(await ws.receive_json())
            await limiter.acquire(req.tenant_id, req.cost)
        except RateLimitedError as exc:
            await ws.send_json({"type": "error", "retry_after_s": exc.retry_after_s})
            await ws.close(code=1013)
            return
        session = StreamSession(llm)

        async def pump() -> None:
            async for frame in session.run(req):
                await ws.send_text(frame)

        pump_task = asyncio.create_task(pump())
        try:
            while not pump_task.done():
                msg = await asyncio.wait_for(ws.receive_json(), timeout=60)
                if msg.get("type") == "cancel":
                    pump_task.cancel()
                    break
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pump_task.cancel()
        finally:
            await asyncio.gather(pump_task, return_exceptions=True)

    # `WebSocket` is imported inside this function, so under postponed annotations FastAPI cannot resolve the
    # string "WebSocket" from module globals and would treat `ws` as a query parameter (handshake rejected).
    chat_ws.__annotations__["ws"] = WebSocket
    app.websocket("/v1/chat/ws")(chat_ws)

    install_extension_routes(app)
    return app


async def demo() -> None:
    limiter = TokenBucketLimiter(BucketConfig(capacity=5, refill_per_s=2))
    admitted = rejected = 0
    for i in range(12):
        try:
            await limiter.acquire("tenant-a", 1.0)
            admitted += 1
        except RateLimitedError as exc:
            rejected += 1
            if rejected == 1:
                log.info("first rejection", extra={"ctx": {"retry_after_s": exc.retry_after_s}})
    log.info("burst result", extra={"ctx": {"admitted": admitted, "rejected": rejected}})

    ok = ChatRequest(tenant_id="tenant-a", prompt="hello streaming world", max_tokens=6)
    s1 = StreamSession(FakeLLM())
    frames = [f async for f in s1.run(ok)]
    log.info("fast consumer", extra={"ctx": {"frames": len(frames), "outcome": s1.outcome}})

    s2 = StreamSession(FakeLLM(), queue_size=2, slow_after_s=0.05)
    async for _ in s2.run(ChatRequest(tenant_id="tenant-a", prompt="slow client", max_tokens=200)):
        await asyncio.sleep(0.2)  # reader far slower than producer
    log.info("slow consumer", extra={"ctx": {"outcome": s2.outcome}})

    s3 = StreamSession(FakeLLM())
    _ = [f async for f in s3.run(ChatRequest(tenant_id="tenant-a", prompt="boom now", max_tokens=10))]
    log.info("upstream failure", extra={"ctx": {"outcome": s3.outcome}})


# ---- Syllabus extension: shared errors, retries, HTTP primitives and mini router (M2 #1 path and query parameters) ---
class NotFoundError(ApiError):
    status_code = 404


class ConflictError(ApiError):
    status_code = 409


class UnprocessableError(ApiError):
    """Request failed Pydantic validation; `errors` is a JSON-safe list like FastAPI's 422 body."""
    status_code = 422

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class UnsupportedVersionError(ApiError):
    status_code = 400


class GraphQLError(ApiError):
    status_code = 400


class DependencyResolutionError(ApiError):
    status_code = 500


class CorsDeniedError(ApiError):
    status_code = 403


class TransientStoreError(ApiError):
    """Retryable storage fault (connection reset, deadlock victim)."""
    status_code = 503


class MissingDependencyError(ApiError):
    """Optional third-party library not installed; message carries the install hint."""
    status_code = 501


class SelfTestFailure(ApiError):
    pass


T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)


async def retry_async(fn: Callable[[], Awaitable[T]], *, op: str, attempts: int = 3,
                      base_delay_s: float = 0.01, max_delay_s: float = 0.1) -> T:
    """Exponential backoff with full jitter around any I/O; only TransientStoreError is retried."""
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except TransientStoreError:
            if attempt == attempts:
                raise
            delay = random.uniform(0, min(max_delay_s, base_delay_s * 2 ** (attempt - 1)))
            log.warning("retrying", extra={"ctx": {"op": op, "attempt": attempt, "delay_s": round(delay, 4)}})
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class HttpRequest(BaseModel):
    """Transport-neutral request so routers, CORS, versioning and tests need no web framework."""
    method: str
    path: str
    query: dict[str, list[str]] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None

    @field_validator("method")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("headers")
    @classmethod
    def _lower_keys(cls, v: dict[str, str]) -> dict[str, str]:
        return {k.lower(): val for k, val in v.items()}  # header names are case-insensitive

    @classmethod
    def from_url(cls, method: str, url: str, headers: dict[str, str] | None = None,
                 body: dict[str, Any] | None = None) -> "HttpRequest":
        parts = urlsplit(url)
        return cls(method=method, path=parts.path, query=parse_qs(parts.query), headers=headers or {}, body=body)


class HttpResponse(BaseModel):
    status: int = 200
    body: Any = None
    headers: dict[str, str] = Field(default_factory=dict)


RouteHandler = Callable[[HttpRequest, dict[str, Any]], Awaitable[HttpResponse]]
AppCallable = Callable[[HttpRequest], Awaitable[HttpResponse]]


def validate_model(model: type[M], data: Any) -> M:
    """Pydantic v2 validation that raises our typed 422 instead of leaking ValidationError."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        raise UnprocessableError(f"{model.__name__} failed validation", errors) from exc


def error_response(exc: ApiError) -> HttpResponse:
    body: dict[str, Any] = {"error": type(exc).__name__, "detail": str(exc)}
    headers: dict[str, str] = {}
    if isinstance(exc, UnprocessableError):
        body["errors"] = exc.errors
    if isinstance(exc, RateLimitedError) and exc.retry_after_s != float("inf"):
        headers["Retry-After"] = str(max(1, int(exc.retry_after_s)))
    return HttpResponse(status=exc.status_code, body=body, headers=headers)


_PATH_PARAM = re.compile(r"\{(\w+)(?::(int|str))?\}")


class MiniRouter:
    """Pattern routing with typed path parameters: '/items/{item_id:int}' -> {'item_id': 7} (like FastAPI)."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], dict[str, type], RouteHandler]] = []

    def add(self, method: str, pattern: str, handler: RouteHandler) -> None:
        converters: dict[str, type] = {}

        def sub(m: re.Match[str]) -> str:
            converters[m.group(1)] = int if m.group(2) == "int" else str
            return rf"(?P<{m.group(1)}>\d+|-\d+)" if m.group(2) == "int" else rf"(?P<{m.group(1)}>[^/]+)"
        regex = re.compile(_PATH_PARAM.sub(sub, pattern))
        self._routes.append((method.upper(), regex, converters, handler))

    async def dispatch(self, req: HttpRequest) -> HttpResponse:
        path_matched = False
        for method, regex, converters, handler in self._routes:
            m = regex.fullmatch(req.path)
            if m is None:
                continue
            path_matched = True
            if method != req.method:
                continue
            params = {name: converters[name](value) for name, value in m.groupdict().items()}
            try:
                return await handler(req, params)
            except ApiError as exc:
                return error_response(exc)
        if path_matched:
            return HttpResponse(status=405, body={"error": "MethodNotAllowed", "detail": req.method})
        return HttpResponse(status=404, body={"error": "NotFoundError", "detail": req.path})


class ListQuery(BaseModel):
    """Query-string parameters: strings from the URL are coerced and bounded by Pydantic."""
    limit: int = Field(10, ge=1, le=100)
    offset: int = Field(0, ge=0)
    q: str | None = Field(None, max_length=50)

    @classmethod
    def from_request(cls, req: HttpRequest) -> "ListQuery":
        return validate_model(cls, {k: v[-1] for k, v in req.query.items() if v})  # last value wins


# ---- Syllabus extension: Pydantic data validation and CRUD service (M2 #2, #5) ---
class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    sku: str = Field(pattern=r"^[A-Z]{3}-\d{3}$", description="e.g. ABC-123")
    name: str = Field(min_length=1, max_length=80)
    price_cents: int = Field(ge=0, le=10_000_000)
    tags: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("tags")
    @classmethod
    def _normalise_tags(cls, tags: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(t.strip().lower() for t in tags))  # de-duplicate, keep order
        if any(not t or len(t) > 20 for t in cleaned):
            raise ValueError("tags must be 1-20 characters")
        return cleaned


class ItemPatch(BaseModel):
    """Partial update (PATCH semantics): only fields the client sent are changed."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(None, min_length=1, max_length=80)
    price_cents: int | None = Field(None, ge=0, le=10_000_000)
    tags: list[str] | None = Field(None, max_length=5)

    @model_validator(mode="after")
    def _not_empty(self) -> "ItemPatch":
        if not self.model_fields_set:
            raise ValueError("patch must change at least one field")
        return self


class Item(ItemCreate):
    id: int
    version: int = 1


class Page(BaseModel):
    items: list[Item]
    total: int
    limit: int
    offset: int
    next_offset: int | None


class ItemRepository:
    """In-memory store with a lock (single-writer semantics) and optional fault injection for retry tests."""

    def __init__(self) -> None:
        self._items: dict[int, Item] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()
        self.fail_next = 0

    async def _io(self) -> None:
        await asyncio.sleep(0)  # stands in for a network hop
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TransientStoreError("connection reset by peer")

    async def create(self, data: ItemCreate) -> Item:
        await self._io()
        async with self._lock:
            if any(i.sku == data.sku for i in self._items.values()):
                raise ConflictError(f"sku {data.sku} already exists")
            item = Item(id=self._next_id, **data.model_dump())
            self._items[item.id] = item
            self._next_id += 1
            return item

    async def get(self, item_id: int) -> Item:
        await self._io()
        try:
            return self._items[item_id]
        except KeyError:
            raise NotFoundError(f"item {item_id} not found") from None

    async def update(self, item_id: int, patch: ItemPatch) -> Item:
        await self._io()
        async with self._lock:
            current = await self.get(item_id)
            changed = current.model_copy(update={**patch.model_dump(exclude_unset=True), "version": current.version + 1})
            self._items[item_id] = changed
            return changed

    async def delete(self, item_id: int) -> None:
        await self._io()
        async with self._lock:
            if self._items.pop(item_id, None) is None:
                raise NotFoundError(f"item {item_id} not found")

    async def list(self, query: ListQuery) -> Page:
        await self._io()
        rows = sorted(self._items.values(), key=lambda i: i.id)
        if query.q:
            rows = [i for i in rows if query.q.lower() in i.name.lower()]
        window = rows[query.offset:query.offset + query.limit]
        nxt = query.offset + query.limit
        return Page(items=window, total=len(rows), limit=query.limit, offset=query.offset,
                    next_offset=nxt if nxt < len(rows) else None)


class ItemService:
    """Business layer: wraps every repository call in backoff retries; handlers depend on this, not the repo."""

    def __init__(self, repo: ItemRepository) -> None:
        self._repo = repo

    async def create(self, data: ItemCreate) -> Item:
        return await retry_async(lambda: self._repo.create(data), op="item.create")

    async def get(self, item_id: int) -> Item:
        return await retry_async(lambda: self._repo.get(item_id), op="item.get")

    async def update(self, item_id: int, patch: ItemPatch) -> Item:
        return await retry_async(lambda: self._repo.update(item_id, patch), op="item.update")

    async def delete(self, item_id: int) -> None:
        await retry_async(lambda: self._repo.delete(item_id), op="item.delete")

    async def list(self, query: ListQuery) -> Page:
        return await retry_async(lambda: self._repo.list(query), op="item.list")


# ---- Syllabus extension: dependency injection with test overrides (M2 #3) ---
class Depends:
    """Marker used as a parameter default, mirroring fastapi.Depends."""

    def __init__(self, dependency: Callable[..., Any], *, use_cache: bool = True) -> None:
        self.dependency, self.use_cache = dependency, use_cache


class DependencyContainer:
    """`overrides` mirrors FastAPI's `app.dependency_overrides`: swap a provider for tests without touching handlers."""

    def __init__(self) -> None:
        self.overrides: dict[Callable[..., Any], Callable[..., Any]] = {}

    @asynccontextmanager
    async def request_scope(self) -> AsyncIterator["DependencyScope"]:
        async with AsyncExitStack() as stack:  # generator dependencies are finalised here, in reverse order
            yield DependencyScope(self, stack)


class DependencyScope:
    """One request: per-request cache (a dependency used twice is built once) and guaranteed cleanup."""

    def __init__(self, container: DependencyContainer, stack: AsyncExitStack) -> None:
        self._container, self._stack = container, stack
        self._cache: dict[Callable[..., Any], Any] = {}
        self._resolving: list[Callable[..., Any]] = []

    async def solve(self, fn: Callable[..., Any], **explicit: Any) -> Any:
        target = self._container.overrides.get(fn, fn)
        kwargs = dict(explicit)
        for name, param in inspect.signature(target).parameters.items():
            if name not in kwargs and isinstance(param.default, Depends):
                kwargs[name] = await self._resolve(param.default)
        return await self._invoke(target, kwargs)

    async def _resolve(self, dep: Depends) -> Any:
        key = dep.dependency
        if key in self._resolving:
            raise DependencyResolutionError(f"dependency cycle at {getattr(key, '__name__', key)}")
        if dep.use_cache and key in self._cache:
            return self._cache[key]
        self._resolving.append(key)
        try:
            value = await self.solve(key)
        finally:
            self._resolving.pop()
        if dep.use_cache:
            self._cache[key] = value
        return value

    async def _invoke(self, target: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        if inspect.isasyncgenfunction(target):  # `yield` dependency: setup before yield, teardown after
            gen = target(**kwargs)
            value = await gen.__anext__()

            async def finish() -> None:
                try:
                    await gen.__anext__()
                except StopAsyncIteration:
                    return
                raise DependencyResolutionError("generator dependencies must yield exactly once")
            self._stack.push_async_callback(finish)
            return value
        if inspect.iscoroutinefunction(target):
            return await target(**kwargs)
        return target(**kwargs)


REPO_SESSIONS = {"opened": 0, "closed": 0}
_PRODUCTION_REPO = ItemRepository()


async def get_repo() -> AsyncIterator[ItemRepository]:
    """Provider with setup/teardown (think: DB session). Tests override it with a fresh repository."""
    REPO_SESSIONS["opened"] += 1
    try:
        yield _PRODUCTION_REPO
    finally:
        REPO_SESSIONS["closed"] += 1


def get_service(repo: ItemRepository = Depends(get_repo)) -> ItemService:
    return ItemService(repo)


# ---- Syllabus extension: REST handlers over the CRUD service (M2 #1, #5) ---
async def h_list_items(req: HttpRequest, params: dict[str, Any], service: ItemService = Depends(get_service)) -> HttpResponse:
    return HttpResponse(body=(await service.list(ListQuery.from_request(req))).model_dump())


async def h_create_item(req: HttpRequest, params: dict[str, Any], service: ItemService = Depends(get_service)) -> HttpResponse:
    item = await service.create(validate_model(ItemCreate, req.body or {}))
    return HttpResponse(status=201, body=item.model_dump(), headers={"Location": f"/v2/items/{item.id}"})


async def h_get_item(req: HttpRequest, params: dict[str, Any], service: ItemService = Depends(get_service)) -> HttpResponse:
    return HttpResponse(body=(await service.get(params["item_id"])).model_dump())


async def h_patch_item(req: HttpRequest, params: dict[str, Any], service: ItemService = Depends(get_service)) -> HttpResponse:
    item = await service.update(params["item_id"], validate_model(ItemPatch, req.body or {}))
    return HttpResponse(body=item.model_dump())


async def h_delete_item(req: HttpRequest, params: dict[str, Any], service: ItemService = Depends(get_service)) -> HttpResponse:
    await service.delete(params["item_id"])
    return HttpResponse(status=204)


async def h_get_item_v1(req: HttpRequest, params: dict[str, Any], service: ItemService = Depends(get_service)) -> HttpResponse:
    """Legacy v1 contract: price as a float in dollars, no version field."""
    item = await service.get(params["item_id"])
    return HttpResponse(body={"id": item.id, "sku": item.sku, "name": item.name, "price": item.price_cents / 100})


def _bind(container: DependencyContainer, handler: Callable[..., Awaitable[HttpResponse]]) -> RouteHandler:
    async def bound(req: HttpRequest, params: dict[str, Any]) -> HttpResponse:
        async with container.request_scope() as scope:  # new scope per request: cache + cleanup
            return await scope.solve(handler, req=req, params=params)
    return bound


def build_v2_router(container: DependencyContainer) -> MiniRouter:
    r = MiniRouter()
    r.add("GET", "/items", _bind(container, h_list_items))
    r.add("POST", "/items", _bind(container, h_create_item))
    r.add("GET", "/items/{item_id:int}", _bind(container, h_get_item))
    r.add("PATCH", "/items/{item_id:int}", _bind(container, h_patch_item))
    r.add("DELETE", "/items/{item_id:int}", _bind(container, h_delete_item))
    return r


def build_v1_router(container: DependencyContainer) -> MiniRouter:
    r = MiniRouter()
    r.add("GET", "/items/{item_id:int}", _bind(container, h_get_item_v1))
    return r


# ---- Syllabus extension: CORS policy evaluator and middleware (M2 #4) ---
SAFELISTED_REQUEST_HEADERS = {"accept", "accept-language", "content-language"}


class CorsPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    allow_origins: tuple[str, ...] = ()
    allow_methods: tuple[str, ...] = ("GET",)
    allow_headers: tuple[str, ...] = ()
    allow_credentials: bool = False
    expose_headers: tuple[str, ...] = ()
    max_age_s: int = Field(600, ge=0)


class CorsDecision(BaseModel):
    allowed: bool
    is_preflight: bool
    reason: str
    headers: dict[str, str] = Field(default_factory=dict)


def evaluate_cors(policy: CorsPolicy, method: str, headers: Mapping[str, str]) -> CorsDecision:
    """Decide like a browser-facing server: simple request vs preflight (OPTIONS + Access-Control-Request-Method)."""
    h = {k.lower(): v for k, v in headers.items()}
    origin = h.get("origin")
    if origin is None:
        return CorsDecision(allowed=True, is_preflight=False, reason="not a CORS request")
    is_preflight = method.upper() == "OPTIONS" and "access-control-request-method" in h
    wildcard = "*" in policy.allow_origins
    if not wildcard and origin not in policy.allow_origins:
        return CorsDecision(allowed=False, is_preflight=is_preflight, reason="origin_not_allowed")
    out: dict[str, str] = {}
    if wildcard and not policy.allow_credentials:
        out["Access-Control-Allow-Origin"] = "*"
    else:  # credentials forbid '*': echo the specific origin and tell caches the answer varies by Origin
        out["Access-Control-Allow-Origin"] = origin
        out["Vary"] = "Origin"
    if policy.allow_credentials:
        out["Access-Control-Allow-Credentials"] = "true"
    if not is_preflight:
        if policy.expose_headers:
            out["Access-Control-Expose-Headers"] = ", ".join(policy.expose_headers)
        return CorsDecision(allowed=True, is_preflight=False, reason="simple request allowed", headers=out)
    wanted_method = h["access-control-request-method"].upper()
    if wanted_method not in {m.upper() for m in policy.allow_methods}:
        return CorsDecision(allowed=False, is_preflight=True, reason="method_not_allowed:" + wanted_method)
    wanted_headers = [x.strip().lower() for x in h.get("access-control-request-headers", "").split(",") if x.strip()]
    allowed_headers = {x.lower() for x in policy.allow_headers}
    if "*" not in allowed_headers:
        missing = [x for x in wanted_headers if x not in allowed_headers and x not in SAFELISTED_REQUEST_HEADERS]
        if missing:
            return CorsDecision(allowed=False, is_preflight=True, reason="header_not_allowed:" + ",".join(missing))
    out["Access-Control-Allow-Methods"] = ", ".join(policy.allow_methods)
    if wanted_headers:
        out["Access-Control-Allow-Headers"] = ", ".join(wanted_headers)
    out["Access-Control-Max-Age"] = str(policy.max_age_s)
    return CorsDecision(allowed=True, is_preflight=True, reason="preflight allowed", headers=out)


DEFAULT_CORS = CorsPolicy(allow_origins=("https://app.example.com",), allow_methods=("GET", "POST", "PATCH", "DELETE"),
                          allow_headers=("Content-Type", "Authorization", "X-API-Version"), allow_credentials=True,
                          expose_headers=("Location", "Deprecation", "Sunset"), max_age_s=600)


def cors_middleware(app: AppCallable, policy: CorsPolicy) -> AppCallable:
    """Wrap any app callable: answer preflights directly (never reaching handlers), decorate real responses."""
    async def wrapped(req: HttpRequest) -> HttpResponse:
        decision = evaluate_cors(policy, req.method, req.headers)
        if decision.is_preflight:
            return HttpResponse(status=204 if decision.allowed else 403, headers=decision.headers,
                                body=None if decision.allowed else {"error": "CorsDeniedError", "detail": decision.reason})
        resp = await app(req)
        if decision.allowed:
            return resp.model_copy(update={"headers": {**resp.headers, **decision.headers}})
        return resp  # no CORS headers: the browser blocks the page from reading it
    return wrapped


# ---- Syllabus extension: URL-based vs header-based API versioning (M2 #10) ---
class VersionStrategy(str, Enum):
    URL = "url"
    HEADER = "header"
    DEFAULT = "default"


_URL_VERSION = re.compile(r"^/v(\d+)(/.*)?$")
_ACCEPT_VERSION = re.compile(r"application/vnd\.lab\.v(\d+)\+json")


class VersionedApi:
    """URL wins over headers. Header forms: `X-API-Version: 2` or `Accept: application/vnd.lab.v2+json`.
    No version at all -> pinned default (the oldest, so existing clients never break silently)."""

    def __init__(self, routers: Mapping[int, MiniRouter], *, default_version: int = 1,
                 deprecated: Mapping[int, str] | None = None) -> None:
        self._routers, self._default, self._deprecated = dict(routers), default_version, dict(deprecated or {})

    def resolve(self, req: HttpRequest) -> tuple[int, str, VersionStrategy]:
        m = _URL_VERSION.match(req.path)
        if m:
            return int(m.group(1)), m.group(2) or "/", VersionStrategy.URL
        if "x-api-version" in req.headers:
            try:
                return int(req.headers["x-api-version"]), req.path, VersionStrategy.HEADER
            except ValueError:
                raise UnsupportedVersionError("X-API-Version must be an integer") from None
        am = _ACCEPT_VERSION.search(req.headers.get("accept", ""))
        if am:
            return int(am.group(1)), req.path, VersionStrategy.HEADER
        return self._default, req.path, VersionStrategy.DEFAULT

    async def __call__(self, req: HttpRequest) -> HttpResponse:
        try:
            version, path, strategy = self.resolve(req)
            router = self._routers.get(version)
            if router is None:
                raise UnsupportedVersionError(f"version {version} not supported; use one of {sorted(self._routers)}")
        except UnsupportedVersionError as exc:
            return error_response(exc)
        resp = await router.dispatch(req.model_copy(update={"path": path}))
        headers = {**resp.headers, "X-API-Version-Used": f"{version} ({strategy.value})"}
        if version in self._deprecated:
            headers["Deprecation"] = "true"
            headers["Sunset"] = self._deprecated[version]  # HTTP-date after which the version may be removed
        return resp.model_copy(update={"headers": headers})


def build_api(container: DependencyContainer) -> AppCallable:
    """Full offline stack: CORS -> version router -> typed routes -> DI -> service -> repository."""
    versioned = VersionedApi({1: build_v1_router(container), 2: build_v2_router(container)}, default_version=1,
                             deprecated={1: "Thu, 31 Dec 2026 23:59:59 GMT"})
    return cors_middleware(versioned, DEFAULT_CORS)


# ---- Syllabus extension: GraphQL schema, executor, resolvers, DataLoader (M2 #6, #7, #8, #9) ---
K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class DataLoader(Generic[K, V]):
    """Collects every `load(key)` issued in the same event-loop tick and calls `batch_fn` ONCE with all keys.
    Results are memoised per loader instance, so create one loader per request (never share across users)."""

    def __init__(self, batch_fn: Callable[[list[K]], Awaitable[Mapping[K, V]]], *, max_batch: int = 100) -> None:
        self._batch_fn, self._max_batch = batch_fn, max_batch
        self._cache: dict[K, asyncio.Future[V | None]] = {}
        self._queue: list[K] = []
        self._scheduled = False
        self._task: asyncio.Task[None] | None = None
        self.batches = 0

    async def load(self, key: K) -> V | None:
        fut = self._cache.get(key)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._cache[key] = fut
            self._queue.append(key)
            if not self._scheduled:
                self._scheduled = True
                loop.call_soon(self._schedule_dispatch)
        return await fut

    def _schedule_dispatch(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._dispatch())

    async def _dispatch(self) -> None:
        await asyncio.sleep(0)  # let sibling resolvers enqueue their keys first
        keys, self._queue, self._scheduled = self._queue, [], False
        for start in range(0, len(keys), self._max_batch):
            chunk = keys[start:start + self._max_batch]
            self.batches += 1
            try:
                found = await self._batch_fn(chunk)
            except BaseException as exc:
                for k in chunk:
                    fut = self._cache.pop(k)  # forget failures so a later request can retry
                    if fut.done():
                        continue
                    fut.set_exception(exc) if isinstance(exc, Exception) else fut.cancel()
                if not isinstance(exc, Exception):
                    raise
                continue
            for k in chunk:
                self._cache[k].set_result(found.get(k))


@dataclass(frozen=True)
class GField:
    type: str  # GraphQL type reference, e.g. "[Book!]!"
    resolver: Callable[..., Awaitable[Any]] | None = None
    args: Mapping[str, str] = dc_field(default_factory=dict)  # argument name -> type, e.g. {"authorId": "Int!"}


@dataclass(frozen=True)
class GObject:
    name: str
    fields: Mapping[str, GField]


@dataclass(frozen=True)
class GraphQLSchema:
    query: GObject
    mutation: GObject | None
    types: Mapping[str, GObject]

    def sdl(self) -> str:
        def render(obj: GObject) -> str:
            lines = []
            for name, f in obj.fields.items():
                args = "(" + ", ".join(f"{a}: {t}" for a, t in f.args.items()) + ")" if f.args else ""
                lines.append(f"  {name}{args}: {f.type}")
            return "type " + obj.name + " {\n" + "\n".join(lines) + "\n}"
        objs = [self.query, *([self.mutation] if self.mutation else []), *self.types.values()]
        return "\n\n".join(render(o) for o in objs)


@dataclass
class FieldNode:
    name: str
    alias: str | None = None
    args: dict[str, Any] = dc_field(default_factory=dict)
    selections: list["FieldNode"] = dc_field(default_factory=list)

    @property
    def key(self) -> str:
        return self.alias or self.name


@dataclass
class Operation:
    kind: str
    selections: list[FieldNode]


_GQL_TOKEN = re.compile(r'\s+|\#[^\n]*|,|(?P<punct>[{}():!\[\]=])|(?P<num>-?\d+(?:\.\d+)?)|(?P<str>"(?:[^"\\]|\\.)*")|(?P<name>[_A-Za-z]\w*)')


def parse_graphql(source: str) -> Operation:
    """Parser for the executable subset: operation keyword, nested selections, aliases, literal arguments."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(source):
        m = _GQL_TOKEN.match(source, pos)
        if m is None:
            raise GraphQLError(f"syntax error at offset {pos}")
        if m.lastgroup:
            tokens.append((m.lastgroup, m.group()))
        pos = m.end()
    idx = 0

    def peek() -> tuple[str, str]:
        return tokens[idx] if idx < len(tokens) else ("eof", "")

    def take(kind: str, text: str | None = None) -> str:
        nonlocal idx
        k, t = peek()
        if k != kind or (text is not None and t != text):
            raise GraphQLError(f"expected {text or kind} but found {t or 'end of query'}")
        idx += 1
        return t

    def value() -> Any:
        nonlocal idx
        k, t = peek()
        idx += 1
        if k == "num":
            return float(t) if "." in t else int(t)
        if k == "str":
            return json.loads(t)
        if k == "name" and t in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[t]
        raise GraphQLError(f"unsupported value {t!r}")

    def field() -> FieldNode:
        name = take("name")
        alias = None
        if peek() == ("punct", ":"):
            take("punct", ":")
            alias, name = name, take("name")
        node = FieldNode(name=name, alias=alias)
        if peek() == ("punct", "("):
            take("punct", "(")
            while peek() != ("punct", ")"):
                arg = take("name")
                take("punct", ":")
                node.args[arg] = value()
            take("punct", ")")
        if peek() == ("punct", "{"):
            node.selections = selection_set()
        return node

    def selection_set() -> list[FieldNode]:
        take("punct", "{")
        out: list[FieldNode] = []
        while peek() != ("punct", "}"):
            out.append(field())
        take("punct", "}")
        if not out:
            raise GraphQLError("empty selection set")
        return out

    kind = "query"
    if peek()[0] == "name" and peek()[1] in ("query", "mutation"):
        kind = take("name")
        if peek()[0] == "name":
            take("name")  # operation name, ignored
    ops = selection_set()
    if idx != len(tokens):
        raise GraphQLError("unexpected content after the operation")
    return Operation(kind=kind, selections=ops)


_SCALARS = ("Int", "String", "Boolean", "Float")


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _coerce_scalar(type_name: str, raw: Any, arg: str) -> Any:
    ok = ((type_name == "Int" and isinstance(raw, int) and not isinstance(raw, bool))
          or (type_name == "String" and isinstance(raw, str))
          or (type_name == "Boolean" and isinstance(raw, bool))
          or (type_name == "Float" and isinstance(raw, (int, float)) and not isinstance(raw, bool)))
    if not ok:
        raise GraphQLError(f"argument {arg!r} must be {type_name}")
    return raw


def _coerce_args(f: GField, node: FieldNode) -> dict[str, Any]:
    unknown = set(node.args) - set(f.args)
    if unknown:
        raise GraphQLError(f"unknown argument(s) {sorted(unknown)} on field {node.name!r}")
    out: dict[str, Any] = {}
    for arg, type_ref in f.args.items():
        required = type_ref.endswith("!")
        if arg not in node.args or node.args[arg] is None:
            if required:
                raise GraphQLError(f"argument {arg!r} is required")
            continue
        out[_snake(arg)] = _coerce_scalar(type_ref.rstrip("!"), node.args[arg], arg)
    return out


async def _complete(schema: GraphQLSchema, type_ref: str, raw: Any, node: FieldNode, ctx: dict[str, Any],
                    errors: list[dict[str, Any]], path: list[str]) -> Any:
    if type_ref.endswith("!"):
        if raw is None:
            raise GraphQLError(f"cannot return null for non-null field {'.'.join(path)}")
        type_ref = type_ref[:-1]
    if raw is None:
        return None
    if type_ref.startswith("["):
        inner = type_ref[1:-1]
        # children are completed concurrently: this is what lets a DataLoader see all N keys in one tick
        return list(await asyncio.gather(*(_complete(schema, inner, item, node, ctx, errors, path + [str(i)])
                                           for i, item in enumerate(raw))))
    if type_ref in _SCALARS:
        return raw
    obj = schema.types[type_ref]
    if not node.selections:
        raise GraphQLError(f"field {node.name!r} of type {type_ref} needs a selection of subfields")
    values = await asyncio.gather(*(_resolve_field(schema, obj, raw, sel, ctx, errors, path) for sel in node.selections))
    return {sel.key: v for sel, v in zip(node.selections, values)}


async def _resolve_field(schema: GraphQLSchema, parent_type: GObject, parent: Any, node: FieldNode,
                         ctx: dict[str, Any], errors: list[dict[str, Any]], path: list[str]) -> Any:
    here = path + [node.key]
    try:
        gfield = parent_type.fields.get(node.name)
        if gfield is None:
            raise GraphQLError(f"cannot query field {node.name!r} on type {parent_type.name}")
        args = _coerce_args(gfield, node)
        if gfield.resolver is not None:
            raw = await gfield.resolver(parent, ctx, **args)
        else:
            raw = parent.get(node.name) if isinstance(parent, dict) else getattr(parent, node.name)  # default resolver
        return await _complete(schema, gfield.type, raw, node, ctx, errors, here)
    except ApiError as exc:  # field errors do not abort siblings; they are reported in `errors`
        errors.append({"message": str(exc), "path": here})
        return None


async def execute_graphql(schema: GraphQLSchema, source: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Queries resolve top-level fields concurrently; mutations run strictly one after another (spec rule)."""
    errors: list[dict[str, Any]] = []
    try:
        op = parse_graphql(source)
        root = schema.query if op.kind == "query" else schema.mutation
        if root is None:
            raise GraphQLError("schema has no mutation type")
    except GraphQLError as exc:
        return {"data": None, "errors": [{"message": str(exc)}]}
    if op.kind == "query":
        values = await asyncio.gather(*(_resolve_field(schema, root, None, s, ctx, errors, []) for s in op.selections))
    else:
        values = [await _resolve_field(schema, root, None, s, ctx, errors, []) for s in op.selections]
    result: dict[str, Any] = {"data": {s.key: v for s, v in zip(op.selections, values)}}
    if errors:
        result["errors"] = errors
    return result


class BookStore:
    """Fake database that COUNTS queries, so N+1 shows up as a number rather than a feeling."""

    def __init__(self, n_authors: int = 8, books_per_author: int = 3) -> None:
        self.authors = {i: {"id": i, "name": f"Author {i}"} for i in range(1, n_authors + 1)}
        self.books: dict[int, dict[str, Any]] = {}
        for a in self.authors:
            for b in range(books_per_author):
                bid = len(self.books) + 1
                self.books[bid] = {"id": bid, "author_id": a, "title": f"Book {a}.{b}"}
        self.query_log: list[str] = []
        self.fail_next = 0

    async def _query(self, sql: str) -> None:
        self.query_log.append(sql)
        await asyncio.sleep(0.001)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TransientStoreError("deadlock victim")

    async def list_authors(self) -> list[dict[str, Any]]:
        await self._query("SELECT * FROM authors")
        return list(self.authors.values())

    async def authors_by_ids(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        await self._query(f"SELECT * FROM authors WHERE id IN ({len(ids)} ids)")
        return {i: self.authors[i] for i in ids if i in self.authors}

    async def books_for_author(self, author_id: int) -> list[dict[str, Any]]:
        await self._query(f"SELECT * FROM books WHERE author_id = {author_id}")
        return [b for b in self.books.values() if b["author_id"] == author_id]

    async def books_for_authors(self, ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        await self._query(f"SELECT * FROM books WHERE author_id IN ({len(ids)} ids)")
        out: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
        for b in self.books.values():
            if b["author_id"] in out:
                out[b["author_id"]].append(b)
        return out

    async def add_book(self, author_id: int, title: str) -> dict[str, Any]:
        await self._query("INSERT INTO books")
        if author_id not in self.authors:
            raise NotFoundError(f"author {author_id} not found")
        book = {"id": len(self.books) + 1, "author_id": author_id, "title": title}
        self.books[book["id"]] = book
        return book


def make_graphql_context(store: BookStore) -> dict[str, Any]:
    """Per-request context: the store plus fresh DataLoaders (batch functions are retried on transient faults)."""
    async def load_books(ids: list[int]) -> Mapping[int, list[dict[str, Any]]]:
        return await retry_async(lambda: store.books_for_authors(ids), op="books_for_authors")

    async def load_authors(ids: list[int]) -> Mapping[int, dict[str, Any]]:
        return await retry_async(lambda: store.authors_by_ids(ids), op="authors_by_ids")
    return {"store": store, "loaders": {"books": DataLoader(load_books), "authors": DataLoader(load_authors)}}


async def _r_authors(_p: Any, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    return await retry_async(ctx["store"].list_authors, op="list_authors")


async def _r_author(_p: Any, ctx: dict[str, Any], id: int) -> dict[str, Any] | None:
    return (await ctx["store"].authors_by_ids([id])).get(id)


async def _r_books_naive(author: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    return await ctx["store"].books_for_author(author["id"])  # one query PER author: the N+1 trap


async def _r_books_batched(author: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    return await ctx["loaders"]["books"].load(author["id"]) or []


async def _r_book_author_naive(book: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    return (await ctx["store"].authors_by_ids([book["author_id"]])).get(book["author_id"])


async def _r_book_author_batched(book: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
    return await ctx["loaders"]["authors"].load(book["author_id"])


async def _r_add_book(_p: Any, ctx: dict[str, Any], author_id: int, title: str) -> dict[str, Any]:
    return await ctx["store"].add_book(author_id, title)


def build_library_schema(*, use_loaders: bool) -> GraphQLSchema:
    """Object types Author/Book plus Query and Mutation roots. `use_loaders=False` builds the N+1 version."""
    book = GObject("Book", {
        "id": GField("Int!"), "title": GField("String!"),
        "author": GField("Author", _r_book_author_batched if use_loaders else _r_book_author_naive)})
    author = GObject("Author", {
        "id": GField("Int!"), "name": GField("String!"),
        "books": GField("[Book!]!", _r_books_batched if use_loaders else _r_books_naive)})
    query = GObject("Query", {"authors": GField("[Author!]!", _r_authors),
                              "author": GField("Author", _r_author, {"id": "Int!"})})
    mutation = GObject("Mutation", {"addBook": GField("Book!", _r_add_book, {"authorId": "Int!", "title": "String!"})})
    return GraphQLSchema(query=query, mutation=mutation, types={"Author": author, "Book": book})


def build_strawberry_schema(store: BookStore) -> Any:
    """Adapter: the same model with Strawberry (`pip install strawberry-graphql`). Imported lazily."""
    try:
        import strawberry
    except ImportError as exc:
        raise MissingDependencyError("strawberry not installed: pip install strawberry-graphql") from exc

    # This file uses postponed annotations, and Strawberry resolves annotation strings against MODULE globals, so
    # types built inside a function need real (non-string) annotations: we set them explicitly.
    Book = strawberry.type(type("Book", (), {"__annotations__": {"id": int, "title": str}}))

    async def author_books(self: Any, info: Any) -> Any:  # resolver using the DataLoader from the request context
        rows = await info.context["loaders"]["books"].load(self.id) or []
        return [Book(id=r["id"], title=r["title"]) for r in rows]
    author_books.__annotations__ = {"info": strawberry.Info, "return": list[Book]}
    Author = strawberry.type(type("Author", (), {"__annotations__": {"id": int, "name": str},
                                                 "books": strawberry.field(resolver=author_books)}))

    async def authors(self: Any) -> Any:
        return [Author(id=a["id"], name=a["name"]) for a in await store.list_authors()]
    authors.__annotations__ = {"return": list[Author]}
    Query = strawberry.type(type("Query", (), {"authors": strawberry.field(resolver=authors)}))

    async def add_book(self: Any, author_id: int, title: str) -> Any:
        row = await store.add_book(author_id, title)
        return Book(id=row["id"], title=row["title"])
    add_book.__annotations__ = {"author_id": int, "title": str, "return": Book}
    Mutation = strawberry.type(type("Mutation", (), {"add_book": strawberry.mutation(resolver=add_book)}))

    return strawberry.Schema(query=Query, mutation=Mutation)


def build_graphene_schema(store: BookStore) -> Any:
    """Adapter: the same model with Graphene 3 (`pip install graphene`). Imported lazily."""
    try:
        import graphene
    except ImportError as exc:
        raise MissingDependencyError("graphene not installed: pip install graphene") from exc

    class Book(graphene.ObjectType):
        id = graphene.Int(required=True)
        title = graphene.String(required=True)

    class Author(graphene.ObjectType):
        id = graphene.Int(required=True)
        name = graphene.String(required=True)
        books = graphene.List(graphene.NonNull(Book), required=True)

        @staticmethod
        async def resolve_books(parent: dict[str, Any], info: Any) -> list[dict[str, Any]]:
            return await info.context["loaders"]["books"].load(parent["id"]) or []

    class Query(graphene.ObjectType):
        authors = graphene.List(graphene.NonNull(Author), required=True)

        @staticmethod
        async def resolve_authors(parent: Any, info: Any) -> list[dict[str, Any]]:
            return await store.list_authors()

    class AddBook(graphene.Mutation):
        class Arguments:
            author_id = graphene.Int(required=True)
            title = graphene.String(required=True)

        Output = Book

        @staticmethod
        async def mutate(parent: Any, info: Any, author_id: int, title: str) -> dict[str, Any]:
            return await store.add_book(author_id, title)

    class Mutation(graphene.ObjectType):
        add_book = AddBook.Field()

    return graphene.Schema(query=Query, mutation=Mutation)


# ---- Syllabus extension: FastAPI wiring for the new endpoints (lazy import; offline stack above is what the demo runs) ---
def install_extension_routes(app: Any) -> None:
    """Same behaviour as the offline stack, expressed with FastAPI primitives."""
    from fastapi import Depends as FDepends
    from fastapi import Header, Path as FPath, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, Response

    app.add_middleware(CORSMiddleware, allow_origins=list(DEFAULT_CORS.allow_origins),
                       allow_methods=list(DEFAULT_CORS.allow_methods), allow_headers=list(DEFAULT_CORS.allow_headers),
                       allow_credentials=DEFAULT_CORS.allow_credentials, expose_headers=list(DEFAULT_CORS.expose_headers),
                       max_age=DEFAULT_CORS.max_age_s)

    @app.exception_handler(ApiError)
    async def _api_error(_: Any, exc: ApiError) -> JSONResponse:
        resp = error_response(exc)
        return JSONResponse(resp.body, status_code=resp.status, headers=resp.headers)

    async def fastapi_service() -> AsyncIterator[ItemService]:  # FastAPI's own Depends; override via app.dependency_overrides
        async for repo in get_repo():
            yield ItemService(repo)

    @app.get("/v2/items")
    async def list_items(limit: int = Query(10, ge=1, le=100), offset: int = Query(0, ge=0),
                         q: str | None = Query(None, max_length=50),
                         svc: ItemService = FDepends(fastapi_service)) -> Page:
        return await svc.list(ListQuery(limit=limit, offset=offset, q=q))

    # NOTE: annotations may only name module-level types (this file uses postponed annotations), so handlers that
    # need custom headers return a Response object instead of declaring a `Response` parameter.
    @app.post("/v2/items", status_code=201, response_model=Item)
    async def create_item(body: ItemCreate, svc: ItemService = FDepends(fastapi_service)) -> Any:
        item = await svc.create(body)
        return JSONResponse(item.model_dump(), status_code=201, headers={"Location": f"/v2/items/{item.id}"})

    @app.get("/v2/items/{item_id}")
    async def get_item(item_id: int = FPath(ge=1), svc: ItemService = FDepends(fastapi_service)) -> Item:
        return await svc.get(item_id)

    @app.patch("/v2/items/{item_id}")
    async def patch_item(item_id: int, body: ItemPatch, svc: ItemService = FDepends(fastapi_service)) -> Item:
        return await svc.update(item_id, body)

    @app.delete("/v2/items/{item_id}", status_code=204, response_class=Response)
    async def delete_item(item_id: int, svc: ItemService = FDepends(fastapi_service)):  # no return annotation: 204 has no body
        await svc.delete(item_id)
        return Response(status_code=204)

    @app.get("/items/{item_id}")  # header-based versioning: one URL, behaviour chosen by X-API-Version
    async def get_item_by_header(item_id: int, x_api_version: int = Header(1),
                                 svc: ItemService = FDepends(fastapi_service)) -> Any:
        item = await svc.get(item_id)
        headers = {"X-API-Version-Used": str(x_api_version)}
        if x_api_version == 2:
            return JSONResponse(item.model_dump(), headers=headers)
        if x_api_version == 1:
            headers["Deprecation"] = "true"
            return JSONResponse({"id": item.id, "sku": item.sku, "name": item.name, "price": item.price_cents / 100},
                                headers=headers)
        raise UnsupportedVersionError(f"version {x_api_version} not supported")

    graphql_schema = build_library_schema(use_loaders=True)
    graphql_store = BookStore()

    @app.post("/graphql")
    async def graphql_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
        return await execute_graphql(graphql_schema, str(payload.get("query", "")), make_graphql_context(graphql_store))


# ---- Syllabus extension: unit testing, fixtures and mocking (M2 #11, #12) ---
class ItemApiTests(unittest.IsolatedAsyncioTestCase):
    """Fixtures: asyncSetUp builds a fresh repo + DI override per test; addCleanup undoes global state."""

    async def asyncSetUp(self) -> None:
        self.repo = ItemRepository()
        self.container = DependencyContainer()

        async def fake_repo() -> AsyncIterator[ItemRepository]:
            yield self.repo
        self.container.overrides[get_repo] = fake_repo  # test double replaces the production provider
        self.app = build_api(self.container)
        self.addCleanup(self.container.overrides.clear)

    async def call(self, method: str, url: str, body: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> HttpResponse:
        return await self.app(HttpRequest.from_url(method, url, headers, body))

    async def seed(self, n: int) -> None:
        for i in range(n):
            r = await self.call("POST", "/v2/items", {"sku": f"ABC-{100 + i}", "name": f"Widget {i}", "price_cents": 1250})
            self.assertEqual(r.status, 201)

    async def test_create_then_get(self) -> None:
        created = await self.call("POST", "/v2/items", {"sku": "ABC-123", "name": "  Bolt ", "price_cents": 500, "tags": ["Metal", "metal"]})
        self.assertEqual(created.status, 201)
        self.assertEqual(created.headers["Location"], "/v2/items/1")
        got = await self.call("GET", "/v2/items/1")
        self.assertEqual((got.body["name"], got.body["tags"]), ("Bolt", ["metal"]))  # stripped and de-duplicated

    async def test_validation_errors_are_422(self) -> None:
        cases = {"bad sku": {"sku": "nope", "name": "x", "price_cents": 1},
                 "negative price": {"sku": "ABC-123", "name": "x", "price_cents": -1},
                 "extra field": {"sku": "ABC-123", "name": "x", "price_cents": 1, "color": "red"}}
        for label, body in cases.items():
            with self.subTest(label):
                r = await self.call("POST", "/v2/items", body)
                self.assertEqual(r.status, 422)
                self.assertTrue(r.body["errors"])

    async def test_duplicate_sku_is_409_and_missing_is_404(self) -> None:
        await self.seed(1)
        dup = await self.call("POST", "/v2/items", {"sku": "ABC-100", "name": "again", "price_cents": 1})
        self.assertEqual(dup.status, 409)
        self.assertEqual((await self.call("GET", "/v2/items/99")).status, 404)
        self.assertEqual((await self.call("DELETE", "/v2/items/99")).status, 404)
        self.assertEqual((await self.call("PUT", "/v2/items/1")).status, 405)

    async def test_pagination_and_query_params(self) -> None:
        await self.seed(5)
        page = (await self.call("GET", "/v2/items?limit=2&offset=2")).body
        self.assertEqual([i["id"] for i in page["items"]], [3, 4])
        self.assertEqual((page["total"], page["next_offset"]), (5, 4))
        self.assertIsNone((await self.call("GET", "/v2/items?limit=2&offset=4")).body["next_offset"])
        self.assertEqual((await self.call("GET", "/v2/items?limit=0")).status, 422)
        self.assertEqual((await self.call("GET", "/v2/items?limit=abc")).status, 422)
        self.assertEqual((await self.call("GET", "/v2/items/abc")).status, 404)  # {item_id:int} does not match 'abc'

    async def test_patch_bumps_version_and_rejects_empty(self) -> None:
        await self.seed(1)
        r = await self.call("PATCH", "/v2/items/1", {"price_cents": 999})
        self.assertEqual((r.body["price_cents"], r.body["version"]), (999, 2))
        self.assertEqual((await self.call("PATCH", "/v2/items/1", {})).status, 422)

    async def test_versioning_url_vs_header(self) -> None:
        await self.seed(1)
        v1 = await self.call("GET", "/v1/items/1")
        self.assertEqual(v1.body["price"], 12.5)
        self.assertEqual(v1.headers["Deprecation"], "true")
        v2_url = await self.call("GET", "/v2/items/1")
        v2_hdr = await self.call("GET", "/items/1", headers={"X-API-Version": "2"})
        v2_accept = await self.call("GET", "/items/1", headers={"Accept": "application/vnd.lab.v2+json"})
        self.assertEqual(v2_url.body, v2_hdr.body)
        self.assertEqual(v2_hdr.body, v2_accept.body)
        self.assertIn("price_cents", v2_hdr.body)
        self.assertEqual((await self.call("GET", "/items/1")).body["price"], 12.5)  # no version -> pinned default v1
        self.assertEqual((await self.call("GET", "/v9/items/1")).status, 400)
        self.assertEqual((await self.call("GET", "/items/1", headers={"X-API-Version": "two"})).status, 400)

    async def test_cors_preflight(self) -> None:
        origin = {"Origin": "https://app.example.com"}
        ok = await self.call("OPTIONS", "/v2/items", headers={**origin, "Access-Control-Request-Method": "POST",
                                                             "Access-Control-Request-Headers": "content-type, authorization"})
        self.assertEqual(ok.status, 204)
        self.assertEqual(ok.headers["Access-Control-Allow-Origin"], "https://app.example.com")
        self.assertEqual(ok.headers["Access-Control-Allow-Credentials"], "true")
        evil = await self.call("OPTIONS", "/v2/items", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        self.assertEqual(evil.status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", evil.headers)
        bad_method = await self.call("OPTIONS", "/v2/items", headers={**origin, "Access-Control-Request-Method": "PUT"})
        self.assertEqual(bad_method.status, 403)
        bad_header = await self.call("OPTIONS", "/v2/items", headers={**origin, "Access-Control-Request-Method": "GET",
                                                                     "Access-Control-Request-Headers": "x-secret"})
        self.assertEqual(bad_header.status, 403)
        actual = await self.call("GET", "/v2/items", headers=origin)
        self.assertEqual(actual.headers["Access-Control-Expose-Headers"], "Location, Deprecation, Sunset")
        star = evaluate_cors(CorsPolicy(allow_origins=("*",)), "GET", {"origin": "https://x.example"})
        self.assertEqual(star.headers["Access-Control-Allow-Origin"], "*")

    async def test_di_override_cleanup_and_cache(self) -> None:
        events: list[str] = []

        async def conn() -> AsyncIterator[str]:
            events.append("open")
            try:
                yield "conn"
            finally:
                events.append("close")

        def svc_a(c: str = Depends(conn)) -> str:
            return "a:" + c

        def svc_b(c: str = Depends(conn)) -> str:
            return "b:" + c

        def top(a: str = Depends(svc_a), b: str = Depends(svc_b), extra: int = 0) -> tuple[str, str, int]:
            return a, b, extra
        async with self.container.request_scope() as scope:
            self.assertEqual(await scope.solve(top, extra=7), ("a:conn", "b:conn", 7))
            self.assertEqual(events, ["open"])  # shared dependency built once per request
        self.assertEqual(events, ["open", "close"])
        self.container.overrides[conn] = lambda: "fake-conn"
        async with self.container.request_scope() as scope:
            self.assertEqual((await scope.solve(top))[0], "a:fake-conn")
        self.assertEqual(events, ["open", "close"])  # the real provider never ran

    async def test_di_cycle_is_detected(self) -> None:
        def a(x: Any = None) -> Any:
            return x

        def b(y: Any = Depends(a)) -> Any:
            return y
        a.__defaults__ = (Depends(b),)
        async with self.container.request_scope() as scope:
            with self.assertRaises(DependencyResolutionError):
                await scope.solve(a)

    async def test_retry_recovers_with_mock(self) -> None:
        flaky = mock.AsyncMock(side_effect=[TransientStoreError("blip"), TransientStoreError("blip"), "ok"])
        with mock.patch.object(random, "uniform", return_value=0.0):  # no real sleeping in unit tests
            self.assertEqual(await retry_async(flaky, op="t"), "ok")
        self.assertEqual(flaky.await_count, 3)
        dead = mock.AsyncMock(side_effect=TransientStoreError("down"))
        with mock.patch.object(random, "uniform", return_value=0.0), self.assertRaises(TransientStoreError):
            await retry_async(dead, op="t", attempts=2)
        self.assertEqual(dead.await_count, 2)
        self.repo.fail_next = 2
        with mock.patch.object(random, "uniform", return_value=0.0):
            self.assertEqual((await ItemService(self.repo).list(ListQuery())).total, 0)  # service retried through 2 faults

    async def test_dataloader_batches_and_dedupes(self) -> None:
        batch = mock.AsyncMock(return_value={1: "a", 2: "b"})
        loader: DataLoader[int, str] = DataLoader(batch)
        results = await asyncio.gather(loader.load(1), loader.load(2), loader.load(1), loader.load(3))
        self.assertEqual(results, ["a", "b", "a", None])
        batch.assert_awaited_once_with([1, 2, 3])
        await loader.load(1)  # served from the per-request cache
        self.assertEqual(batch.await_count, 1)

    async def test_graphql_n_plus_one_and_mutation(self) -> None:
        q = "{ authors { name books { title } } }"
        naive_store, fast_store = BookStore(), BookStore()
        naive = await execute_graphql(build_library_schema(use_loaders=False), q, make_graphql_context(naive_store))
        fast = await execute_graphql(build_library_schema(use_loaders=True), q, make_graphql_context(fast_store))
        self.assertEqual(naive, fast)
        self.assertEqual((len(naive_store.query_log), len(fast_store.query_log)), (9, 2))
        m = await execute_graphql(build_library_schema(use_loaders=True),
                                  'mutation { addBook(authorId: 1, title: "New") { id title author { name } } }',
                                  make_graphql_context(fast_store))
        self.assertEqual(m["data"]["addBook"]["author"]["name"], "Author 1")
        self.assertEqual(len(fast_store.books), 25)
        bad = await execute_graphql(build_library_schema(use_loaders=True), "{ authors { nope } }", make_graphql_context(fast_store))
        self.assertIn("errors", bad)
        self.assertIn("errors", await execute_graphql(build_library_schema(use_loaders=True), "{ authors ", make_graphql_context(fast_store)))
        missing = await execute_graphql(build_library_schema(use_loaders=True), 'mutation { addBook(authorId: 99, title: "x") { id } }',
                                        make_graphql_context(fast_store))
        self.assertIsNone(missing["data"]["addBook"])

    async def test_optional_dependencies_give_install_hint(self) -> None:
        with mock.patch.dict(sys.modules, {"strawberry": None, "graphene": None, "debugpy": None}):
            for builder in (build_strawberry_schema, build_graphene_schema):
                with self.assertRaises(MissingDependencyError) as ctx:
                    builder(BookStore())
                self.assertIn("pip install", str(ctx.exception))
            with self.assertRaises(MissingDependencyError):
                enable_debug_hooks({"LAB_DEBUG_PORT": "5678"})


def run_self_tests() -> dict[str, Any]:
    """Run the suite programmatically (what `python -m unittest` or CI would do) and fail loudly on any red test."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ItemApiTests)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SelfTestFailure(stream.getvalue()[-2000:])
    return {"ran": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}


# ---- Syllabus extension: test coverage analysis (M2 #13) ---
def _function_lines(path: Path) -> dict[str, set[int]]:
    """Executable line numbers per function, from the compiled code objects (stdlib `dis`, no coverage.py)."""
    found: dict[str, set[int]] = {}

    def walk(code: types.CodeType, prefix: str) -> None:
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                name = prefix + const.co_name
                if const.co_flags & inspect.CO_NEWLOCALS:  # real function bodies (class bodies run at import)
                    lines = {ln for _, ln in dis.findlinestarts(const) if ln is not None and ln != const.co_firstlineno}
                    if lines:
                        found[name + "@" + str(const.co_firstlineno)] = lines
                walk(const, name + ".")
    walk(compile(path.read_text(encoding="utf-8"), str(path), "exec"), "")
    return found


def measure_self_test_coverage() -> dict[str, Any]:
    """Line coverage of this file while `run_self_tests` executes, via stdlib `trace`. Approximate by design:
    use `coverage run --branch` or `pytest --cov` in CI (see README) for branch coverage and reports."""
    path = Path(__file__).resolve()
    ignore = sorted({sys.prefix, sys.exec_prefix, sys.base_prefix, sysconfig.get_path("stdlib"),
                     sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    tracer = trace.Trace(count=True, trace=False, ignoredirs=ignore)
    tracer.runfunc(run_self_tests)
    hit = {ln for (fn, ln), _ in tracer.results().counts.items() if Path(fn).resolve() == path}
    per_function = _function_lines(path)
    total = sum(len(v) for v in per_function.values())
    covered = sum(len(v & hit) for v in per_function.values())
    worst = sorted(((len(v & hit) / len(v), name.split("@")[0], len(v - hit)) for name, v in per_function.items()
                    if v - hit), key=lambda t: (t[0], -t[2]))[:4]
    return {"function_lines": total, "covered": covered, "percent": round(100 * covered / max(total, 1), 1),
            "least_covered": [{"function": n, "percent": round(100 * p), "missed_lines": m} for p, n, m in worst]}


# ---- Syllabus extension: debugging configurations and IDE extensions (M2 #14, #15) ---
LAUNCH_JSON = """{
  "version": "0.2.0",
  "configurations": [
    {"name": "Lab: run module", "type": "debugpy", "request": "launch",
     "program": "${workspaceFolder}/modules/02_api_backend/lab.py", "console": "integratedTerminal", "justMyCode": true},
    {"name": "API: uvicorn lab:app", "type": "debugpy", "request": "launch", "module": "uvicorn",
     "args": ["lab:app", "--port", "8000"], "cwd": "${workspaceFolder}/modules/02_api_backend",
     "env": {"LAB_LOG_LEVEL": "DEBUG"}, "justMyCode": false},
    {"name": "Tests: current file (unittest)", "type": "debugpy", "request": "launch", "module": "unittest",
     "args": ["-v", "${file}"], "console": "integratedTerminal"},
    {"name": "Attach: debugpy on 5678", "type": "debugpy", "request": "attach",
     "connect": {"host": "localhost", "port": 5678}}
  ]
}
"""

EXTENSIONS_JSON = """{
  "recommendations": [
    "ms-python.python", "ms-python.vscode-pylance", "ms-python.debugpy", "charliermarsh.ruff",
    "ms-python.mypy-type-checker", "GraphQL.vscode-graphql", "humao.rest-client",
    "ryanluker.vscode-coverage-gutters", "eamodio.gitlens", "ms-azuretools.vscode-docker", "redhat.vscode-yaml"
  ]
}
"""


class LaunchConfig(BaseModel):
    """Schema for the subset of launch.json fields we use; extra='forbid' catches typos such as 'justmycode'."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    type: Literal["debugpy", "python"]
    request: Literal["launch", "attach"]
    program: str | None = None
    module: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    console: str | None = None
    justMyCode: bool = True
    connect: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _has_target(self) -> "LaunchConfig":
        if self.request == "launch" and not (self.program or self.module):
            raise ValueError("launch needs program or module")
        if self.request == "attach" and not self.connect:
            raise ValueError("attach needs connect.host/port")
        return self


class LaunchFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    configurations: list[LaunchConfig]


class ExtensionsFile(BaseModel):
    recommendations: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]+\.[A-Za-z0-9-]+$")]]


def enable_debug_hooks(env: Mapping[str, str]) -> str:
    """LAB_DEBUG_PORT=5678 attaches debugpy (VS Code 'Attach' config); otherwise only enable faulthandler,
    which dumps Python tracebacks on fatal signals. `breakpoint()` honours PYTHONBREAKPOINT."""
    port = env.get("LAB_DEBUG_PORT")
    if not port:
        faulthandler.enable()
        faulthandler.disable()
        return "faulthandler available; no debugger requested"
    try:
        import debugpy
    except ImportError as exc:
        raise MissingDependencyError("debugpy not installed: pip install debugpy") from exc
    debugpy.listen(("127.0.0.1", int(port)))
    if env.get("LAB_DEBUG_WAIT") == "1":
        debugpy.wait_for_client()
    return f"debugpy listening on 127.0.0.1:{port}"


async def demo_debug_and_ide(workdir: Path) -> dict[str, Any]:
    vscode = workdir / ".vscode"
    vscode.mkdir()
    await asyncio.to_thread((vscode / "launch.json").write_text, LAUNCH_JSON, encoding="utf-8")
    await asyncio.to_thread((vscode / "extensions.json").write_text, EXTENSIONS_JSON, encoding="utf-8")
    launch = LaunchFile.model_validate_json(await asyncio.to_thread((vscode / "launch.json").read_text, encoding="utf-8"))
    exts = ExtensionsFile.model_validate_json(await asyncio.to_thread((vscode / "extensions.json").read_text, encoding="utf-8"))
    try:
        LaunchFile.model_validate({"version": "0.2.0", "configurations": [{"name": "x", "type": "debugpy", "request": "attach"}]})
        broken = "accepted"
    except ValidationError:
        broken = "rejected (attach without connect)"
    return {"launch_configs": [c.name for c in launch.configurations], "extensions": len(exts.recommendations),
            "broken_config": broken, "debug_hooks": enable_debug_hooks({})}


# ---- Syllabus extension: end-to-end demo ---
def _flat(resp: HttpResponse) -> dict[str, Any]:
    return {"status": resp.status, "headers": resp.headers, "body": resp.body}


async def extension_demo() -> None:
    """Runs every syllabus-extension demo offline; each step logs the evidence for what it teaches."""
    container = DependencyContainer()
    app_call = build_api(container)

    async def call(method: str, url: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> HttpResponse:
        return await app_call(HttpRequest.from_url(method, url, headers, body))

    # path/query params, Pydantic validation, CRUD, pagination, DI (real get_repo provider with session counters)
    for i in range(5):
        await call("POST", "/v2/items", {"sku": f"WID-{100 + i}", "name": f"Widget {i}", "price_cents": 1000 + i})
    page = await call("GET", "/v2/items?limit=2&offset=2&q=widget")
    log.info("crud pagination", extra={"ctx": {"ids": [i["id"] for i in page.body["items"]], "total": page.body["total"],
                                               "next_offset": page.body["next_offset"], "sessions": dict(REPO_SESSIONS)}})
    bad = await call("POST", "/v2/items", {"sku": "bad", "name": "", "price_cents": -5})
    log.info("validation 422", extra={"ctx": {"status": bad.status, "fields": sorted({e["loc"][0] for e in bad.body["errors"]})}})
    log.info("path param typing", extra={"ctx": {"abc_status": (await call("GET", "/v2/items/abc")).status,
                                                 "missing_status": (await call("GET", "/v2/items/999")).status}})
    # DI override: same handlers, different provider
    stub = ItemRepository()

    async def stub_provider() -> AsyncIterator[ItemRepository]:
        yield stub
    container.overrides[get_repo] = stub_provider
    after_override = await call("GET", "/v2/items")
    container.overrides.clear()
    log.info("di override", extra={"ctx": {"items_visible_with_override": after_override.body["total"],
                                           "items_visible_in_prod_repo": (await call("GET", "/v2/items")).body["total"]}})
    # CORS
    pre = await call("OPTIONS", "/v2/items", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Headers": "content-type"})
    deny = await call("OPTIONS", "/v2/items", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    log.info("cors preflight", extra={"ctx": {"allowed": _flat(pre), "denied_status": deny.status}})
    # versioning
    v_url = await call("GET", "/v1/items/1")
    v_hdr = await call("GET", "/items/1", headers={"X-API-Version": "2"})
    log.info("versioning", extra={"ctx": {"url_v1": v_url.body, "url_v1_headers": v_url.headers,
                                          "header_v2": v_hdr.body, "header_v2_used": v_hdr.headers["X-API-Version-Used"]}})
    # GraphQL: N+1 before/after, queries vs mutations
    q = "{ authors { name books { title } } }"
    naive_store, fast_store = BookStore(), BookStore()
    naive = await execute_graphql(build_library_schema(use_loaders=False), q, make_graphql_context(naive_store))
    fast_ctx = make_graphql_context(fast_store)
    fast = await execute_graphql(build_library_schema(use_loaders=True), q, fast_ctx)
    log.info("n+1 problem", extra={"ctx": {"authors": len(fast["data"]["authors"]), "queries_before": len(naive_store.query_log),
                                           "queries_after": len(fast_store.query_log), "same_result": naive == fast,
                                           "loader_batches": fast_ctx["loaders"]["books"].batches}})
    mut = await execute_graphql(build_library_schema(use_loaders=True),
                                'mutation { addBook(authorId: 2, title: "Fresh") { id title author { name } } }', fast_ctx)
    log.info("mutation", extra={"ctx": {"result": mut["data"], "queries_issued_by_mutation": fast_store.query_log[2:]}})
    log.info("sdl", extra={"ctx": {"schema": build_library_schema(use_loaders=True).sdl().splitlines()[:8]}})
    # optional adapters: run for real when installed, otherwise report the typed install hint
    for name, builder in (("strawberry", build_strawberry_schema), ("graphene", build_graphene_schema)):
        store = BookStore()
        try:
            schema = builder(store)
        except MissingDependencyError as exc:
            log.info("adapter", extra={"ctx": {"adapter": name, "status": "skipped", "hint": str(exc)}})
            continue
        ctx = make_graphql_context(store)
        res = await (schema.execute(q, context_value=ctx) if name == "strawberry" else schema.execute_async(q, context_value=ctx))
        log.info("adapter", extra={"ctx": {"adapter": name, "status": "ran", "errors": bool(res.errors), "queries": len(store.query_log)}})
    # tests (sync unittest runs its own loop, so use a worker thread) and coverage
    tests = await asyncio.to_thread(run_self_tests)
    log.info("self tests", extra={"ctx": tests})
    log.info("coverage", extra={"ctx": await asyncio.to_thread(measure_self_test_coverage)})
    with tempfile.TemporaryDirectory(prefix="lab02-") as tmp:
        log.info("debug and ide config", extra={"ctx": await demo_debug_and_ide(Path(tmp))})


async def main() -> None:
    await demo()
    await extension_demo()


try:  # allow `uvicorn lab:app`
    app = create_app()
except ImportError:  # offline demo mode
    app = None


if __name__ == "__main__":
    asyncio.run(main())
