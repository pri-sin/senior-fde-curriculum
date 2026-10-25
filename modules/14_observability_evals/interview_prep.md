# Senior FDE System Design Crucible

**Prompt:** Build the LLM gateway and evaluation platform for an enterprise with 60 internal AI applications, 25M LLM calls/month, three model providers plus a self-hosted model, a $180k/month AI budget, and a p95 latency SLO of 3 s for chat. Finance wants chargeback by team; security wants prompts masked in logs.

**Requirements**
- Single OpenAI-compatible endpoint; per-team keys, budgets and rate limits; provider fallback.
- Traces for every call with token/cost; searchable for 30 days; masked payloads.
- Automated evals gate prompt/model changes; online quality sampling.

**Sizing**
- Traffic: 25 M calls/month = 25,000,000 / 2,592,000 s = **9.6 calls/s** average; peak factor 6x = **~58 calls/s**. Each call ~1.8k input / 350 output tokens: monthly tokens = 45 B input + 8.75 B output.
- Cost sanity check: blended at $2/M input and $8/M output = $90k + $70k = **$160k/month**, within the $180k budget but only 11% headroom, so plan caching and routing: exact+semantic cache hit rate 18% saves ~$29k; routing 40% of easy queries to a small model at 1/8 price saves ~$24k. Target **~$107k/month**.
- Gateway capacity: 58 calls/s with ~3 ms overhead; a single proxy pod handles ~500 req/s. Run 4 pods (N+2 over 2 zones) with Redis for budgets/rate limits (Redis ops ~58 x 4 = 240/s, trivial) and Postgres for keys/spend.
- Streaming holds connections: 58 calls/s x 6 s avg duration = **~350 concurrent streams**, ~90 per pod.
- Trace storage: 25 M calls x 3 spans x 2 KB (masked, truncated payloads) = **150 GB/month**; ClickHouse with 30-day hot retention = ~150 GB; sampled 10% payload retention to 1 year = 15 GB/month.
- Eval compute: golden sets: 60 apps x 300 cases = 18,000 cases; an LLM-judge pass with 3 judge calls per case at 2.5k tokens = 135 M tokens = **~$600 per full run**; run full nightly on changed apps only (~10 apps) = ~$100/night; plus CI subsets of 50 cases per PR.
- Latency budget: gateway 5 ms + cache 2 ms + provider TTFB 600 ms + generation 350 tokens at 80 tok/s = 4.4 s total for non-streamed (violates the 3 s SLO), so the SLO is defined on **time to first token < 1.2 s** and **full completion < 6 s**; for the 3 s full-answer SLO route to a faster model or cap output at 200 tokens.

**Component breakdown**
1. Gateway (LiteLLM proxy or equivalent): auth, budget (hard stop), rate limit, router with ordered fallbacks, per-provider circuit breakers, cache.
2. Telemetry: OpenTelemetry GenAI spans -> Langfuse/ClickHouse; masking at the gateway; cost computed from a versioned price table.
3. Chargeback: nightly rollup of spend per team/app/model to the finance warehouse; alerts at 50/80/100% of budget.
4. Eval service: golden-set store, runners producing faithfulness/relevancy/context-precision, regression gates integrated with CI, results attached to the release.
5. Online evaluation: 2% sample of traces judged by a stronger model + user feedback signals; drift dashboards.
6. Release control: model aliases (`chat-prod`) so rollback is a config flip; canary 5% with side-by-side judge scoring.

**Failure modes**
- Provider outage: fallback with circuit breakers and concurrency caps; alert on fallback rate > 5%; degrade (smaller model, shorter output) instead of cascading.
- Budget bypass via many keys: budgets are enforced per team, not per key; keys map to teams.
- Trace backend lag/outage: async batch export with bounded queue and drop policy; never block the request path.
- Judge bias/drift: periodic human-labeled calibration set; track judge agreement; version judge prompts.
- Price table stale: reconcile gateway-computed cost with provider invoices monthly; alert if variance > 3%.

# Client Simulation Challenge

**Scenario.** The client's CFO says: *"Our AI bill rose from $60k to $140k in two months, and when I ask what we're getting for it, I get slides. Show me where the money goes and why I shouldn't cut the program."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "That growth is real, and you're right that slides don't answer it. Here is what the data shows. Usage grew about 40 percent, which was expected as more teams launched. But average prompt size roughly tripled in the same period, because one application started attaching full conversation history to every request, and we weren't measuring cost per request, so nobody saw it."

*Impact.* "That one change accounts for roughly 60 of the 80 thousand dollar increase, and it didn't improve answer quality, our tests show the same accuracy with a quarter of the history. So most of the added spend bought you nothing, and I understand why that reads as a program that isn't under control."

*Remedy.* "We're changing three things, and you will see them in your own reports. Every team gets a monthly budget enforced at the gateway, so a runaway change stops at the cap instead of reaching your invoice. Every request carries its cost, so your finance team can see spend by team and by application each week, and an alert fires when any application's cost per request jumps by more than twenty percent. Finally, every prompt or model change must pass our quality test set and shows its cost impact before release, so you always know what you pay for each point of accuracy. With caching and routing easy questions to a smaller model, I expect to bring the bill under 90 thousand dollars a month without reducing quality, and I'll bring the measured result to our next review rather than a forecast."


# Extension Practice

**1. A provider recovers from a 1-second outage and 300 clients retry at once. What backoff do you choose and why?**
Full or decorrelated jitter with a cap, plus a max attempt count and deadline. In the lab simulation (300 clients, service admits 20 calls per 0.1 s = 200/s after recovery, so the best possible drain time is 300 / 200 = 1.5 s), capped exponential without jitter peaks at 140 attempts per 0.1 s bucket after recovery, averages 8.0 attempts per client, and leaves 13% of clients failed after ten attempts with p95 completion 9.2 s. Full jitter peaks at 56, averages 7.1 attempts, 100% succeed, p95 2.7 s. Jitter works because it desynchronizes clients, not because it waits longer.

**2. Size the idempotency store for write tools at 50 requests/s with a 24 h TTL, and explain what happens if the response is lost.**
50 x 86,400 = 4.32 M keys; at roughly 300 B each (key, payload hash, stored result) that is about 1.3 GB, comfortable in Redis. If the response is lost after the side effect, the client retries with the same key: the store replays the stored result, and the downstream call carries the same key so it does not repeat the action. A reused key with a different payload must fail loudly, never replay. Failed executions are not cached, so a retry can run again.

**3. Which rate limiter would you pick for a per-tenant limit of 600 requests/minute across 10,000 tenants?**
Sliding window counter or token bucket. A sliding log stores up to 600 timestamps per tenant: 10,000 x 600 x 8 B = 48 MB, exact but heavy. A counter needs two numbers per tenant, about 160 KB. A fixed window admits up to 2x the limit across a boundary (the lab shows 20 admitted for a limit of 10). Use the token bucket when bursts are acceptable and for tokens-per-minute limits (`cost` = token count); add a concurrency cap for streaming.

**4. Your LLM judge prefers whichever answer comes first. How do you measure and mitigate it, and what does it cost?**
Judge each pair in both orders. In the lab, a judge with a first-position bonus drops kappa against human labels from 0.66 to 0.42, and only 50% of verdicts survive the swap; on the surviving pairs kappa is 0.75, and the rest go to a human or a third judge. Cost: swapping doubles judge calls. For 18,000 cases at 2.5k tokens per call, one pairwise comparison with swap is 18,000 x 2 x 2,500 = 90 M tokens, about $400 at the ~$4.4 per million blended rate implied by the crucible ($600 for 135 M tokens).

**5. How do you decide whether prompt v2 really regressed on a 17-case benchmark, and how large should the benchmark be?**
Use a paired bootstrap on per-case differences. The lab's v2 shows faithfulness 1.00 to 0.735 (delta -0.265, 95% interval -0.38 to -0.15): a real regression, so block the release. Another run with answer-relevancy delta -0.059 and interval -0.18 to 0.00 is "no significant change" despite a visible drop, which is what small samples do to you. To detect a 0.05 shift when per-case standard deviation of the difference is about 0.3, you need roughly (1.96 x 0.3 / 0.05)^2 = 138 paired cases, so grow the benchmark with synthetic questions plus sampled production traces.
