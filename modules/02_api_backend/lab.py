"""Module 02 lab: per-tenant token-bucket admission control and SSE streaming with backpressure.

The core (TokenBucketLimiter, StreamSession, sse_format) is dependency-free beyond Pydantic so the demo
runs offline: `python3 lab.py`. `create_app()` wires the same core into FastAPI (SSE + WebSocket) when
fastapi is installed: `uvicorn lab:app --port 8000` (after `pip install fastapi uvicorn`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import time
from collections.abc import AsyncIterator
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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

    @app.websocket("/v1/chat/ws")
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

    return app


try:  # allow `uvicorn lab:app`
    app = create_app()
except ImportError:  # offline demo mode
    app = None


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


if __name__ == "__main__":
    asyncio.run(demo())
