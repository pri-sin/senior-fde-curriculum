"""Module 01 lab: a bounded async worker pool with backpressure, retries, executor offload,
graceful shutdown, event-loop lag monitoring and Linux /proc introspection.

Run: python3 lab.py
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import heapq
import json
import logging
import os
import random
import signal
import sys
import stat
import tempfile
import threading
import time
import tracemalloc
import weakref
from abc import ABC, abstractmethod
from collections import Counter, deque
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

try:  # POSIX-only user/group databases
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]

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
    """Exponential backoff with full jitter; only TransientUpstreamError/timeouts are retried.

    `fn` owns its per-attempt timeout, so time spent queueing for a semaphore is never charged to the upstream."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
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
                async with self._sem:  # wait for a slot outside the clock
                    return await asyncio.wait_for(self._upstream.call(job.payload), self._policy.attempt_timeout_s)

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
                try:
                    self.results.append(await self._process(job))
                except asyncio.CancelledError:  # drain deadline hit while this job was in flight
                    self.results.append(JobResult(job_id=job.job_id, status=JobStatus.DROPPED))
                    raise
                except Exception as exc:  # one poison job must not kill the worker
                    log.error("job crashed", extra={"ctx": {"job_id": job.job_id, "error": repr(exc)}})
                    self.results.append(JobResult(job_id=job.job_id, status=JobStatus.FAILED, attempts_note=type(exc).__name__))
            finally:
                self._queue.task_done()

    async def drain(self, timeout_s: float) -> int:
        """Stop intake, finish work within ONE deadline (queued, in-flight, worker exit); the rest is DROPPED. Returns dropped count."""
        self._closing = True
        dropped = 0
        deadline = time.monotonic() + timeout_s
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
            self._queue.put_nowait(None)
        _, pending = await asyncio.wait(self._tasks, timeout=max(0.0, deadline - time.monotonic()))
        for task in pending:  # in-flight past the deadline: cancel, the worker records DROPPED
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return dropped + len(pending)


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


# ---- Syllabus extension: shared helpers ---
class TransientIOError(TransientUpstreamError):
    """Retryable local I/O or subprocess failure; subclass so `retry_async` treats it like any transient fault."""


class ConfigError(LabError):
    """Environment configuration is missing or malformed (never contains secret values)."""


class PermissionPolicyError(LabError):
    """A chmod/permission request violated the safety policy."""


class CommandTimeoutError(LabError):
    """A subprocess exceeded its deadline and was killed."""


IO_POLICY = RetryPolicy(max_attempts=3, base_delay_s=0.01, max_delay_s=0.1)


def _time_it(fn: Callable[[], object], number: int) -> float:
    """Return total seconds for `number` calls (perf_counter, no GC tweaks: good enough to show Big-O gaps)."""
    t0 = time.perf_counter()
    for _ in range(number):
        fn()
    return time.perf_counter() - t0


# ---- Syllabus extension: Python core data structures (M1 #1) ---
def demo_data_structures(n: int = 20_000) -> dict[str, object]:
    """Complexity you can feel: list.insert(0) is O(n), deque.appendleft is O(1); `in` on list O(n) vs set O(1)."""
    lst = list(range(n))
    dq: deque[int] = deque(range(n))
    st = set(lst)
    t_list_front = _time_it(lambda: lst.insert(0, -1), 2000)
    t_deque_front = _time_it(lambda: dq.appendleft(-1), 2000)
    t_list_in = _time_it(lambda: (n - 1) in lst, 200)
    t_set_in = _time_it(lambda: (n - 1) in st, 200)
    scores = [(random.Random(3).randint(0, 10**6) + i * 7) % 10_007 for i in range(n)]
    top3 = heapq.nlargest(3, scores)  # O(n log k) beats sorting everything when k is small
    heap: list[int] = []
    for s in scores[:10]:
        heapq.heappush(heap, s)
    smallest = heapq.heappop(heap)  # priority-queue primitive: always the minimum
    ordered = {"b": 1, "a": 2}
    ordered["c"] = 3
    ordered["b"] = 99  # updating a key keeps its original position
    counts = Counter("mississippi")
    return {
        "front_insert_ratio_list_vs_deque": round(t_list_front / max(t_deque_front, 1e-9), 1),
        "membership_ratio_list_vs_set": round(t_list_in / max(t_set_in, 1e-9), 1),
        "top3": top3, "heap_min": smallest, "dict_order": list(ordered), "most_common": counts.most_common(2),
    }


# ---- Syllabus extension: memory management fundamentals (M1 #2) ---
class _PlainPoint:
    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y


class _SlotPoint:
    __slots__ = ("x", "y")  # no per-instance __dict__

    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y


class _CycleNode:
    def __init__(self) -> None:
        self.other: _CycleNode | None = None


def demo_memory() -> dict[str, object]:
    """Refcounts, the cyclic GC, weak references and tracemalloc, each with a measurable result."""
    plain, slotted = _PlainPoint(1, 2), _SlotPoint(1, 2)
    plain_bytes = sys.getsizeof(plain) + sys.getsizeof(plain.__dict__)
    slot_bytes = sys.getsizeof(slotted)
    sizes = {"list_1000": sys.getsizeof(list(range(1000))), "tuple_1000": sys.getsizeof(tuple(range(1000)))}
    shared: list[int] = []
    refs_before = sys.getrefcount(shared)
    alias = shared
    refs_after = sys.getrefcount(shared)  # one more name -> one more reference
    del alias
    a, b = _CycleNode(), _CycleNode()
    a.other, b.other = b, a  # reference cycle: refcounting alone can never free these
    probe = weakref.ref(a)
    del a, b
    alive_before_gc = probe() is not None
    collected = gc.collect()
    cache: weakref.WeakValueDictionary[str, _PlainPoint] = weakref.WeakValueDictionary()
    cache["p"] = plain
    del plain
    cache_after_del = len(cache)  # entry vanished with its last strong reference
    tracemalloc.start()
    blob = [bytes(100) for _ in range(5000)]
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del blob
    return {"plain_obj_bytes": plain_bytes, "slots_obj_bytes": slot_bytes, **sizes,
            "refcount_delta": refs_after - refs_before, "cycle_alive_before_gc": alive_before_gc,
            "cycle_freed_after_gc": probe() is None, "gc_collected_objects": collected,
            "gc_thresholds": gc.get_threshold(), "weak_cache_len_after_del": cache_after_del,
            "tracemalloc_current_kb": current // 1024, "tracemalloc_peak_kb": peak // 1024}


# ---- Syllabus extension: object-oriented programming basics (M1 #3) ---
@dataclass(frozen=True, order=True, slots=True)
class Invoice:
    """Dataclass: generated __init__/__repr__/__eq__/ordering; frozen makes it hashable and safe to share."""
    amount_cents: int
    customer: str = field(compare=False)
    tags: tuple[str, ...] = ()


class Notifier(ABC):
    """Abstract base class: subclasses MUST implement `send`; template method `notify` is shared."""

    @abstractmethod
    def send(self, message: str) -> str: ...

    def notify(self, invoice: Invoice) -> str:
        return self.send(f"invoice {invoice.amount_cents} for {invoice.customer}")


class EmailNotifier(Notifier):
    def send(self, message: str) -> str:
        return "email:" + message


class SmsNotifier(Notifier):
    def send(self, message: str) -> str:
        return "sms:" + message[:20]  # polymorphism: same call, different behaviour


@runtime_checkable
class SupportsClose(Protocol):
    """Structural typing: anything with close() qualifies, no inheritance needed (duck typing, checked)."""

    def close(self) -> None: ...


class _Handle:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def demo_oop() -> dict[str, object]:
    inv_small, inv_big = Invoice(100, "acme"), Invoice(900, "zeta", ("vip",))
    try:
        Notifier()  # type: ignore[abstract]
        abstract_blocked = False
    except TypeError:
        abstract_blocked = True
    handle = _Handle()
    return {"sorted_by_amount": [i.customer for i in sorted([inv_big, inv_small])],
            "abstract_instantiation_blocked": abstract_blocked,
            "polymorphic": [n.notify(inv_small) for n in (EmailNotifier(), SmsNotifier())],
            "protocol_match": isinstance(handle, SupportsClose), "protocol_miss": isinstance(object(), SupportsClose),
            "frozen_hashable": hash(inv_small) == hash(Invoice(100, "other"))}


# ---- Syllabus extension: exception handling (M1 #4) ---
def _parse_amount(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise PermanentJobError(f"bad amount {raw!r}") from exc  # explicit chain: __cause__ is kept for debugging


async def demo_exceptions() -> dict[str, object]:
    chained: dict[str, object] = {}
    try:
        _parse_amount("12x")
    except PermanentJobError as exc:
        chained = {"type": type(exc).__name__, "cause": type(exc.__cause__).__name__}
    order: list[str] = []
    try:
        order.append("try")
    except Exception:
        order.append("except")
    else:
        order.append("else")  # runs only when no exception was raised
    finally:
        order.append("finally")  # always runs, even on return/raise
    handled: list[str] = []
    try:
        async with asyncio.TaskGroup() as tg:  # Python 3.11: failures surface together as an ExceptionGroup
            async def fail(kind: type[Exception]) -> None:
                await asyncio.sleep(0.001)
                raise kind("boom")
            tg.create_task(fail(ValueError))
            tg.create_task(fail(KeyError))
    except* ValueError as eg:
        handled.append(f"ValueError x{len(eg.exceptions)}")
    except* KeyError as eg:
        handled.append(f"KeyError x{len(eg.exceptions)}")
    return {"chain": chained, "order": order, "exception_group_handlers": handled}


# ---- Syllabus extension: file I/O operations (M1 #5) ---
def atomic_write_text(path: Path, text: str) -> None:
    """Write to a temp file in the SAME directory, fsync, then os.replace: readers see old or new, never half."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)  # atomic on POSIX within one filesystem
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


async def read_text_retrying(path: Path) -> str:
    """Async file read: blocking open() runs in a worker thread (asyncio.to_thread), wrapped in backoff retries."""
    async def attempt() -> str:
        try:
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            raise  # permanent: retrying will not create the file
        except OSError as exc:
            raise TransientIOError(f"read {path.name}: {exc}") from exc
    return await retry_async(attempt, IO_POLICY, op=f"read-{path.name}")


async def demo_file_io(workdir: Path) -> dict[str, object]:
    target = workdir / "state.json"
    await asyncio.to_thread(atomic_write_text, target, json.dumps({"v": 1}))
    await asyncio.to_thread(atomic_write_text, target, json.dumps({"v": 2}))
    data = json.loads(await read_text_retrying(target))
    lines_path = workdir / "lines.txt"
    await asyncio.to_thread(lines_path.write_text, "a\nb\nc\n", encoding="utf-8")
    with lines_path.open("r", encoding="utf-8") as fh:  # streaming: constant memory regardless of file size
        line_count = sum(1 for _ in fh)
    with (workdir / "blob.bin").open("wb") as fh:
        fh.write(bytes(range(16)))
    binary_len = len((workdir / "blob.bin").read_bytes())
    try:
        await read_text_retrying(workdir / "missing.txt")
        missing = "no error"
    except FileNotFoundError:
        missing = "FileNotFoundError (not retried)"
    leftovers = sorted(p.name for p in workdir.glob("*.tmp"))
    return {"state": data, "lines": line_count, "binary_len": binary_len, "missing": missing, "tmp_leftovers": leftovers}


# ---- Syllabus extension: async context managers (M1 #8) ---
class PooledConnection:
    """Class-based async context manager: __aenter__ acquires, __aexit__ ALWAYS releases (even on error/cancel)."""
    open_count = 0

    def __init__(self, name: str) -> None:
        self.name = name

    async def __aenter__(self) -> "PooledConnection":
        await asyncio.sleep(0)
        PooledConnection.open_count += 1
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        PooledConnection.open_count -= 1
        return False  # do not swallow exceptions


@asynccontextmanager
async def timed_section(label: str) -> AsyncIterator[dict[str, float]]:
    """Generator-based async context manager: code before `yield` is enter, `finally` is exit."""
    box: dict[str, float] = {}
    t0 = time.perf_counter()
    try:
        yield box
    finally:
        box["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        log.info("section", extra={"ctx": {"label": label, **box}})


async def demo_async_context_managers() -> dict[str, object]:
    async with PooledConnection("db") as conn, timed_section("query") as box:
        await asyncio.sleep(0.01)
        inside = PooledConnection.open_count
    released_after_error = False
    try:
        async with PooledConnection("db"):
            raise PermanentJobError("query failed")
    except PermanentJobError:
        released_after_error = PooledConnection.open_count == 0
    return {"conn": conn.name, "open_inside": inside, "open_after": PooledConnection.open_count,
            "released_after_error": released_after_error, "timed": box["elapsed_ms"] >= 10}


# ---- Syllabus extension: concurrency vs parallelism (M1 #9) ---
def _sleep_io(seconds: float) -> float:
    time.sleep(seconds)  # stands in for a blocking network call (releases the GIL)
    return seconds


async def demo_concurrency_vs_parallelism() -> dict[str, float | str]:
    """I/O-bound: threads overlap waiting. CPU-bound: threads share one GIL (no speedup), processes use cores."""
    loop = asyncio.get_running_loop()
    n_tasks, wait_s = 6, 0.05
    t0 = time.perf_counter()
    for _ in range(n_tasks):
        _sleep_io(wait_s)
    io_seq = time.perf_counter() - t0
    with ThreadPoolExecutor(max_workers=n_tasks) as tp:
        t0 = time.perf_counter()
        await asyncio.gather(*(loop.run_in_executor(tp, _sleep_io, wait_s) for _ in range(n_tasks)))
        io_threads = time.perf_counter() - t0
    rounds = 500_000
    t0 = time.perf_counter()
    for i in range(4):
        await loop.run_in_executor(None, cpu_digest, f"s{i}", rounds)
    cpu_seq = time.perf_counter() - t0
    with ThreadPoolExecutor(max_workers=4) as tp:
        t0 = time.perf_counter()
        await asyncio.gather(*(loop.run_in_executor(tp, cpu_digest, f"t{i}", rounds) for i in range(4)))
        cpu_threads = time.perf_counter() - t0
    with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pp:
        await loop.run_in_executor(pp, cpu_digest, "warm", 1000)  # exclude process start-up from timing
        t0 = time.perf_counter()
        await asyncio.gather(*(loop.run_in_executor(pp, cpu_digest, f"p{i}", rounds) for i in range(4)))
        cpu_procs = time.perf_counter() - t0
    return {"io_sequential_s": round(io_seq, 3), "io_threads_s": round(io_threads, 3),
            "cpu_sequential_s": round(cpu_seq, 3), "cpu_threads_s": round(cpu_threads, 3),
            "cpu_processes_s": round(cpu_procs, 3), "cpus": os.cpu_count() or 1,
            "takeaway": "threads help waiting, processes help computing"}


# ---- Syllabus extension: ThreadPoolExecutor integration (M1 #10) ---
class BlockingClient:
    """Pretend vendor SDK with a synchronous API; tracks peak concurrent callers to prove the pool bound."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def lookup(self, key: int) -> str:
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            time.sleep(0.02)
            if key == 13:
                raise ValueError("unlucky key")  # permanent failure, must not be retried
            return f"value-{key}"
        finally:
            with self._lock:
                self._active -= 1


class ThreadBridge:
    """Bridges async code to a blocking SDK: bounded executor + semaphore + per-call timeout + retries."""

    def __init__(self, client: BlockingClient, workers: int = 4) -> None:
        self._client = client
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lab01-io")
        self._sem = asyncio.Semaphore(workers * 2)

    async def lookup(self, key: int) -> str:
        loop = asyncio.get_running_loop()

        async def attempt() -> str:
            async with self._sem:
                return await asyncio.wait_for(loop.run_in_executor(self._pool, self._client.lookup, key), 2.0)
        return await retry_async(attempt, IO_POLICY, op=f"lookup-{key}")

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


async def demo_thread_pool_integration() -> dict[str, object]:
    client = BlockingClient()
    bridge = ThreadBridge(client, workers=4)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1  # keeps counting only if the loop is NOT blocked

    tick_task = asyncio.create_task(ticker())
    try:
        results = await asyncio.gather(*(bridge.lookup(k) for k in range(16)), return_exceptions=True)
    finally:
        tick_task.cancel()
        await asyncio.gather(tick_task, return_exceptions=True)
        bridge.close()
    failures = [r for r in results if isinstance(r, Exception)]
    return {"ok": sum(1 for r in results if isinstance(r, str)), "failed": [repr(f) for f in failures],
            "peak_threads_in_use": client.peak, "loop_ticks_during_run": ticks}


# ---- Syllabus extension: navigating the file system (M1 #11) ---
def build_sample_tree(root: Path) -> None:
    for rel in ("src/app/main.py", "src/app/util.py", "src/tests/test_main.py", "docs/readme.md", "docs/img/logo.png"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel, encoding="utf-8")


def walk_tree(root: Path, pattern: str = "*.py") -> dict[str, object]:
    """pathlib equivalents of `pwd`, `cd`, `ls`, `find -name`, `du`, `realpath`."""
    py_files = sorted(str(p.relative_to(root)) for p in root.rglob(pattern))
    top_level = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir())
    total_bytes = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    walked = [(Path(d).relative_to(root).as_posix(), len(files)) for d, _, files in os.walk(root)]
    return {"cwd_is_abs": Path.cwd().is_absolute(), "top_level": top_level, "py_files": py_files,
            "total_bytes": total_bytes, "dirs_with_file_counts": sorted(walked),
            "resolved_matches": (root / "src" / ".." / "docs").resolve() == (root / "docs").resolve()}


# ---- Syllabus extension: permission and user management (M1 #12) ---
def safe_chmod(path: Path, mode: int, allowed_root: Path) -> str:
    """chmod only inside `allowed_root` and never grant world-write; returns the `ls -l` style string."""
    resolved = path.resolve()
    try:
        resolved.relative_to(allowed_root.resolve())
    except ValueError as exc:
        raise PermissionPolicyError(f"{path} is outside {allowed_root}") from exc
    if mode & stat.S_IWOTH or mode & ~0o777:
        raise PermissionPolicyError(f"mode {oct(mode)} grants world-write or exceeds 0o777")
    os.chmod(resolved, mode)
    return stat.filemode(resolved.stat().st_mode)


def describe_user() -> dict[str, object]:
    if pwd is None or grp is None:  # Windows
        return {"supported": False}
    entry = pwd.getpwuid(os.getuid())
    group_names = sorted({grp.getgrgid(g).gr_name for g in os.getgroups() if _gid_known(g)})
    return {"user": entry.pw_name, "uid": os.getuid(), "primary_group": grp.getgrgid(entry.pw_gid).gr_name,
            "n_groups": len(group_names), "is_root": os.geteuid() == 0}


def _gid_known(gid: int) -> bool:
    try:
        grp.getgrgid(gid)  # type: ignore[union-attr]
        return True
    except KeyError:
        return False


def demo_permissions(workdir: Path) -> dict[str, object]:
    f = workdir / "secret.txt"
    f.write_text("s3", encoding="utf-8")
    after_600 = safe_chmod(f, 0o600, workdir)
    after_640 = safe_chmod(f, 0o640, workdir)
    mode_bits = stat.S_IMODE(f.stat().st_mode)
    rejected: list[str] = []
    for bad_path, bad_mode in ((f, 0o666), (Path(tempfile.gettempdir()), 0o700)):
        try:
            safe_chmod(bad_path, bad_mode, workdir)
        except PermissionPolicyError as exc:
            rejected.append(str(exc)[:40])
    return {"mode_600": after_600, "mode_640": after_640, "octal": oct(mode_bits),
            "owner_can_write": bool(mode_bits & stat.S_IWUSR), "other_can_read": bool(mode_bits & stat.S_IROTH),
            "os_access_read": os.access(f, os.R_OK), "rejected": rejected, "user": describe_user()}


# ---- Syllabus extension: process monitoring (M1 #13) ---
async def run_command(argv: list[str], *, timeout_s: float = 5.0, env: Mapping[str, str] | None = None,
                      policy: RetryPolicy = IO_POLICY) -> tuple[int, str, str]:
    """Run a subprocess without a shell; kill it on timeout; retry only if it could not be spawned."""
    async def attempt() -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE,
                                                        env=dict(env) if env is not None else None)
        except OSError as exc:
            raise TransientIOError(f"spawn {argv[0]}: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise CommandTimeoutError(f"{argv[0]} exceeded {timeout_s}s") from None
        return proc.returncode if proc.returncode is not None else -1, out.decode(), err.decode()
    return await retry_async(attempt, policy, op=f"run-{Path(argv[0]).name}")


async def process_info(pid: int) -> dict[str, object]:
    """/proc on Linux; portable `ps` fallback (macOS/BSD). Equivalent of `ps -o pid,rss,stat,etime -p PID`."""
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
        return {"source": "/proc", "pid": pid, "state": fields["State"].strip(),
                "rss_kb": int(fields.get("VmRSS", "0 kB").split()[0]), "threads": int(fields["Threads"])}
    rc, out, _ = await run_command(["ps", "-o", "pid=,rss=,stat=,etime=", "-p", str(pid)], timeout_s=3.0)
    parts = out.split()
    if rc != 0 or len(parts) < 4:
        raise LabError(f"process {pid} not found")
    return {"source": "ps", "pid": int(parts[0]), "rss_kb": int(parts[1]), "state": parts[2], "etime": parts[3]}


async def demo_process_monitoring() -> dict[str, object]:
    me = await process_info(os.getpid())
    child = await asyncio.create_subprocess_exec("sleep", "30")
    try:
        child_info = await process_info(child.pid)
        child.terminate()  # SIGTERM first (graceful), as an orchestrator would
        try:
            await asyncio.wait_for(child.wait(), 2.0)
        except asyncio.TimeoutError:
            child.kill()
            await child.wait()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    return {"self": me, "child_source": child_info["source"], "child_state_when_sampled": child_info["state"],
            "child_exit_code": child.returncode, "sigterm_exit_code_is_minus_15": child.returncode == -15}


# ---- Syllabus extension: shell scripting basics (M1 #14) ---
SHELL_SCRIPT = """#!/usr/bin/env bash
# Count files by extension; demonstrates strict mode, args, functions, loops, traps and env vars.
set -euo pipefail

usage() { echo "usage: $0 <dir> <ext>" >&2; exit 2; }
[[ $# -eq 2 ]] || usage
dir=$1
ext=$2
[[ -d "$dir" ]] || { echo "not a directory: $dir" >&2; exit 3; }

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT        # cleanup runs on success, error and SIGTERM

count=0
for f in "$dir"/*."$ext"; do
  [[ -e "$f" ]] || continue          # glob matched nothing
  count=$((count + 1))
  basename "$f" >> "$scratch/names"
done
echo "app=${LAB_APP_NAME:-unset} ext=$ext count=$count"
[[ -f "$scratch/names" ]] && sort "$scratch/names" | paste -sd, - || true
"""


async def demo_shell_script(workdir: Path) -> dict[str, object]:
    script = workdir / "count_files.sh"
    script.write_text(SHELL_SCRIPT, encoding="utf-8")
    safe_chmod(script, 0o750, workdir)  # executable by owner/group, as `chmod 750` would
    data_dir = workdir / "data"
    data_dir.mkdir()
    for name in ("a.txt", "b.txt", "c.log"):
        (data_dir / name).write_text(name, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LAB_APP_NAME": "lab01"}
    rc_ok, out_ok, _ = await run_command(["bash", str(script), str(data_dir), "txt"], env=env, timeout_s=5.0)
    rc_usage, _, err_usage = await run_command(["bash", str(script)], env=env, timeout_s=5.0)
    rc_dir, _, err_dir = await run_command(["bash", str(script), str(workdir / "nope"), "txt"], env=env, timeout_s=5.0)
    try:
        await run_command(["bash", "-c", "sleep 5"], timeout_s=0.2, policy=RetryPolicy(max_attempts=1))
        timeout_result = "no timeout"
    except (CommandTimeoutError, TransientUpstreamError):
        timeout_result = "killed after deadline"
    return {"ok_exit": rc_ok, "ok_output": out_ok.strip().splitlines(), "usage_exit": rc_usage,
            "usage_msg": err_usage.strip()[:30], "bad_dir_exit": rc_dir, "bad_dir_msg": err_dir.strip()[:30],
            "timeout": timeout_result}


# ---- Syllabus extension: environment variable configuration (M1 #15) ---
class AppConfig(BaseModel):
    """Twelve-factor config: every value arrives as a string in the environment; Pydantic coerces and validates."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    app_name: str = Field(min_length=1)
    api_key: SecretStr  # required; repr/log show ********
    port: int = Field(8080, ge=1, le=65535)
    debug: bool = False
    timeout_s: float = Field(2.5, gt=0)
    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [h.strip() for h in value.split(",") if h.strip()]
        return value


def load_config(env: Mapping[str, str], prefix: str = "LAB_") -> AppConfig:
    """Pick PREFIX_* keys, strip the prefix, validate. Errors name fields but never echo values (secrets)."""
    raw = {k[len(prefix):].lower(): v for k, v in env.items() if k.startswith(prefix)}
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        problems = sorted({".".join(str(p) for p in e["loc"]) + ":" + e["type"] for e in exc.errors()})
        raise ConfigError("invalid configuration: " + ", ".join(problems)) from None


def demo_env_config() -> dict[str, object]:
    good = {"LAB_APP_NAME": "billing", "LAB_API_KEY": "demo-not-a-real-key", "LAB_PORT": "9000",
            "LAB_DEBUG": "true", "LAB_ALLOWED_HOSTS": "a.example.com, b.example.com", "UNRELATED": "ignored"}
    cfg = load_config(good)
    errors: dict[str, str] = {}
    for label, env in (("missing_required", {"LAB_APP_NAME": "x"}),
                       ("bad_types", {**good, "LAB_PORT": "http", "LAB_TIMEOUT_S": "-1"}),
                       ("unknown_key", {**good, "LAB_PORTT": "1"})):
        try:
            load_config(env)
            errors[label] = "accepted"
        except ConfigError as exc:
            errors[label] = str(exc)
    return {"port_type": type(cfg.port).__name__, "port": cfg.port, "debug": cfg.debug, "hosts": cfg.allowed_hosts,
            "default_timeout_s": cfg.timeout_s, "secret_repr": repr(cfg.api_key),
            "secret_not_in_dump_str": "demo-not-a-real-key" not in str(cfg),
            "secret_value_len": len(cfg.api_key.get_secret_value()), "errors": errors}


async def extension_demo() -> None:
    """Runs every syllabus-extension demo once; all are offline and bounded to a few seconds."""
    log.info("data_structures", extra={"ctx": demo_data_structures()})
    log.info("memory", extra={"ctx": demo_memory()})
    log.info("oop", extra={"ctx": demo_oop()})
    log.info("exceptions", extra={"ctx": await demo_exceptions()})
    log.info("async_context_managers", extra={"ctx": await demo_async_context_managers()})
    log.info("concurrency_vs_parallelism", extra={"ctx": await demo_concurrency_vs_parallelism()})
    log.info("thread_pool_integration", extra={"ctx": await demo_thread_pool_integration()})
    log.info("process_monitoring", extra={"ctx": await demo_process_monitoring()})
    log.info("env_config", extra={"ctx": demo_env_config()})
    with tempfile.TemporaryDirectory(prefix="lab01-") as tmp:
        root = Path(tmp)
        log.info("file_io", extra={"ctx": await demo_file_io(root)})
        tree = root / "tree"
        tree.mkdir()
        build_sample_tree(tree)
        log.info("filesystem", extra={"ctx": walk_tree(tree)})
        log.info("permissions", extra={"ctx": demo_permissions(root)})
        log.info("shell_script", extra={"ctx": await demo_shell_script(root)})


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
    await extension_demo()
    by_status: dict[str, int] = {}
    for r in pool.results:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
    log.info("finished", extra={"ctx": {"by_status": by_status, "dropped": dropped, "upstream_calls": upstream.calls,
                                         "max_loop_lag_ms": round(monitor.max_lag_ms, 2), **proc_snapshot()}})


if __name__ == "__main__":
    asyncio.run(main())
