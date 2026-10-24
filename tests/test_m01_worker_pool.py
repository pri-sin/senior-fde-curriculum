"""Module 01 regressions: timeout scope, worker supervision, single drain deadline."""
import asyncio
import time

from _labs import load

m = load("01_python_linux")


def _run(coro):
    return asyncio.run(coro)


def test_semaphore_wait_is_not_charged_to_the_upstream_timeout():
    class Slow(m.FlakyUpstream):
        async def call(self, payload):
            self.calls += 1
            await asyncio.sleep(0.4)
            return f"echo:{payload}"

    async def go():
        up = Slow(0.0)
        pool = m.WorkerPool(up, workers=8, queue_size=32, upstream_concurrency=1,
                            policy=m.RetryPolicy(max_attempts=4, attempt_timeout_s=1.0))
        pool.start()
        for i in range(8):
            await pool.submit(m.Job(job_id=i, payload=f"d{i}"))
        await pool.drain(30)
        return up.calls, {r.status for r in pool.results}

    calls, statuses = _run(go())
    assert calls == 8  # was 16: queueing time was misread as upstream failure
    assert statuses == {m.JobStatus.DONE}


def test_unexpected_exception_fails_the_job_but_keeps_the_worker_alive():
    class Boom(m.FlakyUpstream):
        async def call(self, payload):
            raise RuntimeError("unexpected")

    async def go():
        pool = m.WorkerPool(Boom(0.0), workers=2, queue_size=8, upstream_concurrency=2)
        pool.start()
        for i in range(4):
            await pool.submit(m.Job(job_id=i, payload="x"))
        await pool.drain(5)
        return pool.results

    results = _run(go())
    assert len(results) == 4  # was 0: both workers died on the first job
    assert {r.status for r in results} == {m.JobStatus.FAILED}


def test_drain_honours_one_deadline_and_marks_in_flight_work_dropped():
    class Hang(m.FlakyUpstream):
        async def call(self, payload):
            await asyncio.sleep(30)

    async def go():
        pool = m.WorkerPool(Hang(0.0), workers=2, queue_size=8, upstream_concurrency=2,
                            policy=m.RetryPolicy(max_attempts=10, attempt_timeout_s=5.0))
        pool.start()
        for i in range(6):
            await pool.submit(m.Job(job_id=i, payload="x"))
        t0 = time.monotonic()
        dropped = await pool.drain(1.0)
        return time.monotonic() - t0, dropped, pool.results

    elapsed, dropped, results = _run(go())
    assert elapsed < 2.0
    assert dropped == 6
    assert {r.status for r in results} == {m.JobStatus.DROPPED}
    assert sorted(r.job_id for r in results) == list(range(6))  # nothing silently lost
