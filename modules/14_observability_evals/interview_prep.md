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
