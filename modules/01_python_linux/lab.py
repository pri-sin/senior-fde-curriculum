"""Module 01 lab: a bounded async worker pool with backpressure, retries, executor offload,
graceful shutdown, event-loop lag monitoring and Linux /proc introspection.

Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

T = TypeVar("T")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": round(record.created, 3), "level": record.levelname,
                   "logger": record.name, "msg": record.getMessage()}
        payload.update(getattr(record, "ctx", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


log = get_logger("lab01")


class LabError(Exception):
    """Base class for all lab errors."""


class TransientUpstreamError(LabError):
    """Retryable failure (timeouts, 5xx)."""


class PermanentJobError(LabError):
    """Non-retryable failure (validation, 4xx)."""


class PoolClosedError(LabError):
    """Submission attempted after shutdown began."""


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_attempts: int = Field(4, ge=1, le=10)
    base_delay_s: float = Field(0.05, gt=0)
    max_delay_s: float = Field(2.0, gt=0)
    attempt_timeout_s: float = Field(1.0, gt=0)


async def retry_async(fn: Callable[[], Awaitable[T]], policy: RetryPolicy, *, op: str) -> T:
    """Exponential backoff with full jitter; only TransientUpstreamError/timeouts are retried."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await asyncio.wait_for(fn(), timeout=policy.attempt_timeout_s)
        except (TransientUpstreamError, asyncio.TimeoutError) as exc:
            if attempt == policy.max_attempts:
                raise TransientUpstreamError(f"{op}: exhausted {attempt} attempts") from exc
            cap = min(policy.max_delay_s, policy.base_delay_s * 2 ** (attempt - 1))
            delay = random.uniform(0, cap)
            log.warning("retrying", extra={"ctx": {"op": op, "attempt": attempt, "delay_s": round(delay, 3)}})
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


class JobStatus(str, Enum):
    DONE = "done"
    FAILED = "failed"
    DROPPED = "dropped"


class Job(BaseModel):
    job_id: int = Field(ge=0)
    payload: str = Field(min_length=1, max_length=4096)
    cpu_heavy: bool = False


class JobResult(BaseModel):
    job_id: int
    status: JobStatus
    digest: str | None = None
    attempts_note: str | None = None
    elapsed_ms: float = 0.0


def cpu_digest(payload: str, rounds: int = 200_000) -> str:
    """CPU-bound work: runs in a process pool so it cannot stall the event loop."""
    h = payload.encode()
    for _ in range(rounds // 1000):
        for _ in range(1000):
            h = hashlib.sha256(h).digest()
    return h.hex()[:16]


class FlakyUpstream:
    """Deterministic stand-in for a remote API that fails transiently some of the time."""

    def __init__(self, failure_rate: float, seed: int = 7) -> None:
        self._rng = random.Random(seed)
        self._failure_rate = failure_rate
        self.calls = 0

    async def call(self, payload: str) -> str:
        self.calls += 1
        await asyncio.sleep(self._rng.uniform(0.005, 0.03))
        roll = self._rng.random()
        if roll < self._failure_rate:
            raise TransientUpstreamError("503 from upstream")
        if roll > 0.995:
            await asyncio.sleep(5)  # hung connection, killed by attempt timeout
        return f"echo:{payload}"


class LoopLagMonitor:
    """Measures how late a 100 ms sleep wakes up; large drift means something blocked the loop."""

    def __init__(self, interval_s: float = 0.1) -> None:
        self.interval_s = interval_s
        self.max_lag_ms = 0.0
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            start = time.perf_counter()
            await asyncio.sleep(self.interval_s)
            lag = (time.perf_counter() - start - self.interval_s) * 1000
            self.max_lag_ms = max(self.max_lag_ms, lag)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="loop-lag")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def proc_snapshot() -> dict[str, int | None]:
    """Read RSS and open-fd count from /proc (Linux); returns None values elsewhere (e.g. macOS)."""
    rss_kb: int | None = None
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
    fd_dir = Path("/proc/self/fd")
    open_fds = len(list(fd_dir.iterdir())) if fd_dir.exists() else None
    return {"rss_kb": rss_kb, "open_fds": open_fds, "pid": os.getpid()}


class WorkerPool:
    def __init__(self, upstream: FlakyUpstream, *, workers: int = 8, queue_size: int = 32,
                 upstream_concurrency: int = 4, policy: RetryPolicy | None = None,
                 proc_pool: ProcessPoolExecutor | None = None) -> None:
        self._upstream = upstream
        self._queue: asyncio.Queue[Job | None] = asyncio.Queue(maxsize=queue_size)
        self._sem = asyncio.Semaphore(upstream_concurrency)
        self._policy = policy or RetryPolicy()
        self._proc_pool = proc_pool
        self._n_workers = workers
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self.results: list[JobResult] = []

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker(i), name=f"worker-{i}") for i in range(self._n_workers)]

    async def submit(self, job: Job) -> None:
        if self._closing:
            raise PoolClosedError("pool is draining")
        await self._queue.put(job)  # blocks when full: this is the backpressure

    async def _process(self, job: Job) -> JobResult:
        t0 = time.perf_counter()
        try:
            async def attempt() -> str:
                async with self._sem:
                    return await self._upstream.call(job.payload)

            reply = await retry_async(attempt, self._policy, op=f"job-{job.job_id}")
            digest: str | None = None
            if job.cpu_heavy and self._proc_pool is not None:
                digest = await asyncio.get_running_loop().run_in_executor(self._proc_pool, cpu_digest, reply)
            return JobResult(job_id=job.job_id, status=JobStatus.DONE, digest=digest,
                             elapsed_ms=round((time.perf_counter() - t0) * 1000, 2))
        except LabError as exc:
            return JobResult(job_id=job.job_id, status=JobStatus.FAILED, attempts_note=str(exc),
                             elapsed_ms=round((time.perf_counter() - t0) * 1000, 2))

    async def _worker(self, idx: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                self.results.append(await self._process(job))
            finally:
                self._queue.task_done()

    async def drain(self, timeout_s: float) -> int:
        """Stop intake, finish queued work within timeout, mark leftovers DROPPED. Returns dropped count."""
        self._closing = True
        dropped = 0
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout_s)
        except asyncio.TimeoutError:
            while not self._queue.empty():
                job = self._queue.get_nowait()
                self._queue.task_done()
                if job is not None:
                    dropped += 1
                    self.results.append(JobResult(job_id=job.job_id, status=JobStatus.DROPPED))
        for _ in self._tasks:
            await self._queue.put(None)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        return dropped


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def main() -> None:
    stop = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop)
    monitor = LoopLagMonitor()
    monitor.start()
    upstream = FlakyUpstream(failure_rate=0.25)
    with ProcessPoolExecutor(max_workers=2) as procs:
        pool = WorkerPool(upstream, proc_pool=procs)
        pool.start()
        log.info("started", extra={"ctx": proc_snapshot()})
        for i in range(120):
            if stop.is_set():
                break
            try:
                job = Job(job_id=i, payload=f"doc-{i}", cpu_heavy=(i % 15 == 0))
            except ValidationError as exc:  # pragma: no cover - payloads above are valid
                raise PermanentJobError(str(exc)) from exc
            await pool.submit(job)
        dropped = await pool.drain(timeout_s=10)
    await monitor.stop()
    by_status: dict[str, int] = {}
    for r in pool.results:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
    log.info("finished", extra={"ctx": {"by_status": by_status, "dropped": dropped, "upstream_calls": upstream.calls,
                                         "max_loop_lag_ms": round(monitor.max_lag_ms, 2), **proc_snapshot()}})


if __name__ == "__main__":
    asyncio.run(main())
