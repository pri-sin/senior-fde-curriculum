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
