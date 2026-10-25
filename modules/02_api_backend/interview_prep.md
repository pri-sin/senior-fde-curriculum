# Senior FDE System Design Crucible

**Prompt:** Design the streaming chat API for an insurer's claims-assistant: 8,000 concurrent adjusters, 2,500 requests/minute at peak, time-to-first-token (TTFT) p95 <= 800 ms, per-business-unit fairness, behind a corporate proxy that you do not control.

**Requirements**
- Streaming answers (avg 350 output tokens, 45 tokens/s); user can cancel; reconnect resumes without duplication.
- Fairness: 12 business units with plan-based quotas; no unit can starve another.
- Budget: LLM spend capped per unit per day.

**Sizing**
- Peak arrival rate: 2,500/min = **41.7 req/s**. Average stream duration = 350 / 45 = **7.8 s**. Little's Law: concurrent streams = 41.7 x 7.8 = **~325 active streams** (the 8,000 are connected users, mostly idle).
- Connection memory: 325 streams x (64-item queue x ~200 B + ~50 KB task/socket state) = ~17 MB. Memory is not the constraint; the **LLM quota** is. Tokens/s from upstream = 325 x 45 = **14,600 tokens/s**; at 350 tokens per answer that is ~42 answers/s.
- Pod count: one uvicorn worker comfortably handles ~1,000 SSE streams; plan 4 pods (N+2) for rolling deploys across 2 zones.
- Rate limits: capacity = 2 minutes of the unit's steady share. Unit with 15% share: refill 6.2 req/s, burst 750 tokens of cost. Daily budget: 41.7 x 86,400 x 0.3 duty cycle x $0.01 = ~$10.8k/day total; enforce in a Redis counter.
- Storage: store usage records 1 KB x 3.6M/day (0.3 duty) = ~3.6 GB/day in a warehouse, not the hot path.

**Component breakdown**
1. L7 gateway: TLS, JWT validation, `proxy_buffering off`, idle timeout >= 300 s.
2. API pods (FastAPI): token-bucket admission using Redis Lua (atomic refill + take), cost-weighted by prompt and max tokens.
3. Stream session: producer/consumer with bounded queue; 15 s heartbeats; slow-consumer cutoff cancels upstream.
4. Resume: SSE `id:` equals token index; client sends `Last-Event-ID`; completed text is cached 60 s keyed by request id so reconnect replays missed tokens instead of regenerating.
5. LLM router: two providers, hedged on connection failure only before first token.

**Failure modes**
- Corporate proxy buffers: detect via synthetic TTFB probe; fall back to WebSocket on a different port when probe fails twice.
- Redis outage: limiter fails *closed* to a local conservative per-pod bucket (capacity/4) rather than open, which would risk budget overrun.
- Thundering reconnect after deploy: jittered `retry:` field in SSE and connection draining (preStop 15 s).
- Upstream mid-stream failure: cannot transparently retry (partial output already shown), so emit an `error` event with a resumable request id.

# Client Simulation Challenge

**Scenario.** The client's lead architect says: *"We tested your streaming demo from our office and the answer appeared all at once after fifteen seconds. Your streaming doesn't work behind our network. I want a signed statement it will work before we proceed."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "Thank you for testing it from your real network; that is exactly the test that matters. What you saw is almost certainly your outbound proxy buffering the response until it completes. The service was producing tokens from the first second, but the proxy held them. That behaviour is common in networks with TLS-inspecting proxies, and I should have asked about it earlier."

*Impact.* "For your adjusters, a 15-second blank screen is the difference between a tool they trust and one they abandon, so this is a launch-blocking issue, and I'm treating it that way."

*Remedy.* "I won't sign a blanket statement, because I can't verify your proxy from here, but I can commit to something stronger. Within three days we'll run a probe from inside your network that measures time-to-first-byte through the real proxy and reports pass or fail. If the proxy buffers, we have two tested options: ask your network team to exempt `text/event-stream` on this hostname, which is a one-line change they've made for other vendors, or switch your tenant to our WebSocket transport, which proxies handle differently. Either way the acceptance test is the same: first token under 800 milliseconds at the 95th percentile, measured from an adjuster's desk. I'll put that criterion in writing as the exit gate for the pilot."


# Extension Practice

**1. A GraphQL query `authors { books { title } }` returns 200 authors and the DB shows 201 queries. Diagnose, fix, and quantify.**
Classic N+1: one query for authors plus one per author. Batch with a per-request DataLoader: 1 + 1 = **2 queries**. At 3 ms per query sequential that is 603 ms vs about 6 ms of database time (plus one `IN (...)` list of 200 ids; cap batches at about 100-1,000 keys). The lab shows 9 vs 2 for 8 authors.

**2. A browser reports a CORS error on `PATCH /items/1` but `curl` works. Why, and what must the server return?**
`curl` ignores CORS; the browser sent a preflight `OPTIONS` because PATCH and JSON headers are not "simple". The server must answer it with `Access-Control-Allow-Origin` (the exact origin when cookies are used, plus `Vary: Origin`), `Access-Control-Allow-Methods` including PATCH and `Access-Control-Allow-Headers` including `content-type`. `Access-Control-Max-Age: 600` lets the browser skip the preflight for 10 minutes, cutting extra requests by up to 50 percent for chatty clients.

**3. You must ship a breaking response change to 400 integrations. URL or header versioning, and how do you retire v1?**
Prefer URL versioning for a public API with many unknown clients (visible, cacheable, easy to route and log). Header versioning suits a small controlled client set. Either way: keep v1 working, answer with `Deprecation: true` and a `Sunset` date, publish the migration guide, measure v1 traffic per client, contact the remaining callers, and only then return 410. Default unversioned calls to the oldest version so nobody breaks silently.

**4. Coverage is 92 percent and an outage still happened in the retry path. How?**
Coverage measures executed lines, not asserted behaviour or branches. Run with `--branch`, check the "missing" lines of the error paths, and write a test with `AsyncMock(side_effect=[TransientError, TransientError, "ok"])` asserting `await_count == 3`. Gate CI on a threshold for changed code rather than a single global number.

**5. How do you test a handler that depends on a database without running one?**
Use dependency overrides: replace the provider (`overrides[get_repo] = fake_repo`) with an in-memory fake or an `AsyncMock`, keep a few contract tests that run the same assertions against the real database in CI, and verify cleanup (a `yield` dependency's teardown ran) so connection leaks show up as test failures.
