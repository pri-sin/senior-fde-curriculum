# Senior FDE System Design Crucible

**Prompt:** Design OmniGuard for a global bank: 5 business lines acting as tenants (plus 40 internal apps), 20,000 requests/minute at peak, p95 time-to-first-token under 800 ms, 99.95% availability, hard daily budgets, PII never leaving the bank's boundary unmasked, and a regulator-ready audit trail.

**Requirements**
- Fairness: no tenant can reduce another's p95 by more than 10% (isolation SLO).
- Failover across two external providers and one in-VPC model within 2 seconds of provider degradation.
- Zero cross-tenant data exposure; masked audit logs retained 7 years.

**Sizing**
- Rate: 20,000/min = **333 req/s** peak; average 40% of peak = 133 req/s. Mean answer: 450 output tokens, 60 tokens/s generation => stream duration ~7.5 s. Concurrency (Little's Law) = 333 x 7.5 = **~2,500 concurrent streams** at peak.
- Gateway pods: an async Python pod comfortably holds ~700 streams if token frames are coalesced (5 tokens per frame => 12 frames/s per stream => ~8.4k frames/s per pod, roughly 65% of one core for JSON encoding and socket writes). Plan **8 pods** (2,500 / 8 = ~312 streams each, well under the 700 ceiling), spread across 3 AZs so losing one AZ still leaves headroom (N+2).
- Provider quotas: output 333 x 450 = **150k output tokens/s** and input 333 x 1,800 = 600k tokens/s at peak. At average load that is ~20.7 B input tokens/day (133 req/s x 1,800 x 86,400); request provider tiers for the peak, ~36 M tokens/minute input, split 60/30/10 primary/secondary/self-hosted; self-hosted: 10% of 150k = 15k tok/s => ~10 H100-class GPUs at ~1,500 tok/s each for a 70B-class model with batching.
- Redis: per request ~4 ops (bucket, concurrency, budget read/incr, span counters) = 1,332 ops/s peak — a 3-shard cluster is ample; use Lua script for atomic bucket+concurrency.
- Cost: 333 req/s average-weighted (133/s) x 86,400 x (1,800 x $2/M + 450 x $8/M) = 11.5 M requests/day x $0.0072 = **~$83k/day**; budgets per business line: 5 lines with caps = daily forecast x 1.3.
- Telemetry: 11.5 M requests/day x 3 spans x 1.5 KB = **52 GB/day**; sampled payloads 5%; 7-year audit of metadata only (no payloads): 11.5 M x 400 B = 4.6 GB/day = 11.8 TB total.
- Latency budget (TTFT 800 ms): auth 1 ms + admission 3 ms (Redis RTT, local fallback on timeout) + masking/injection 15 ms + routing 2 ms + provider TTFT 450 ms + first-frame flush 5 ms = **~476 ms**, 324 ms slack for retries (one retry within budget).

**Component breakdown**
1. Edge: WAF, TLS, request size limit, mTLS for internal apps; SSE-friendly timeouts, buffering off.
2. Auth: OIDC token validation (Module 12) with per-tenant issuer registry; tenant from claims only.
3. Admission: Redis Lua token bucket (cost-weighted), bulkhead per tenant, global concurrency cap derived from provider quota, weighted fair shares (premium reserved capacity).
4. Policy: tenant config store (plans, models allowed, PII mode, budgets), versioned and hot-reloaded.
5. Guardrails: PII masking with in-VPC vault, injection scoring, output rails (Module 13).
6. Router: model groups, health-scored providers, breakers, hedged requests before first token only, fallback chain, cache for deterministic prompts.
7. Streaming engine: bounded queues, heartbeats, cancellation propagation, per-stream token and time caps.
8. Observability + FinOps: OTel traces, per-tenant dashboards (throttle %, p95, fallback rate, $/day), budget alerts at 50/80/100%.

**Failure modes**
- Redis failover: local conservative buckets (e.g. 30% of limit per pod / pod count) so the system degrades safely rather than failing closed or open.
- Provider brownout (slow, not down): timeout on time-to-first-token plus latency-aware scoring; hedge after P95 TTFB; breakers with half-open probes.
- Retry amplification: gateway retries only before the first token, at most 1 per provider, global retry budget of 10% of traffic.
- Budget race (many pods spend concurrently): reserve-then-reconcile (reserve estimated cost at admission, reconcile on completion) to bound overshoot.
- Config push mistake (limit set to 0): config validation, staged rollout, automatic rollback on error-rate increase.
- Slow client attack: per-stream write timeout and bounded queue; disconnect and release the bulkhead.

# Client Simulation Challenge

**Scenario.** The client's Chief Architect says: *"Our trading desk got 429 errors for twenty minutes yesterday while the marketing team ran a campaign. Your gateway claims fairness, but the desk that makes money was throttled. Explain, and tell me why I shouldn't just let each business line call the providers directly."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "Here's what happened, based on the gateway logs. The desk's limit was set at the default for its plan, and when they ran their normal morning surge they exceeded it, while marketing, whose campaign ran on a different tenant limit, was throttled only lightly. Fairness worked as designed: it kept marketing from taking the desk's capacity, but it did not recognize that the desk's work is more critical, because nobody had assigned the desk a higher tier."

*Impact.* "Twenty minutes of throttling on the desk is a business cost I take seriously. The configuration was our responsibility, because we should have asked for the desk's peak profile during onboarding, and I apologize for that."

*Remedy.* "Three things change. First, the desk gets a reserved capacity tier: a guaranteed share of provider quota that other tenants cannot consume, sized from its measured morning peak plus thirty percent. Second, throttling now returns a precise retry-after hint and alerts our on-call and your platform team when any tenant is throttled more than five percent for five minutes, so a limit problem is caught in minutes. Third, you'll see a weekly fairness report showing each business line's latency and throttle rate, so you can verify no tenant is crowding out another. On direct provider access: without the gateway, nothing would have capped marketing's campaign, so the same provider quota would have been shared with no priority at all, and you would have no logs showing who consumed it. The gateway is the only place where you can enforce priorities, budgets and data protection in a single place, and I'll show you the controls in a live walkthrough with the desk's technology lead this week."

# Extension Practice

**Drill 1: CISO ROI pitch (stakeholder-facing).** *Prompt:* the CISO says "I do not care about handling-time savings, show me why this reduces my risk." Using illustrative inputs: incident probability 8% per year, impact 4.5 M USD, gateway reduces probability by 60%.
*Answer:* expected annual loss before = 0.08 x 4.5 M = 360 k USD; after = 0.032 x 4.5 M = 144 k USD, a 216 k USD annual reduction. Say these are assumptions the client should replace with their own incident history. Name the controls that justify the 60% (masked prompts, injection screening, output rails, per-tenant budgets, audit trail), show the operations-only NPV next to the total so the case holds with the risk benefit set to zero, and ask for a decision on the pilot scope and a 90-day review gate.

**Drill 2: UAT sign-off conversation.** *Prompt:* 38 of 40 cases pass (95%), one failed case is a must-have negative test (a tampered token was accepted by one endpoint).
*Answer:* the overall pass rate meets the 95% bar, but the must-have pass rate is below the 100% criterion (a must-have security case failed) and a token-validation defect is severity 1, so the exit decision is NO-GO. Explain it in those terms, offer a dated fix and a targeted re-test of that case plus its neighbors (not a full re-run), and record the decision in the runbook. Do not renegotiate the criteria at the sign-off meeting.

**Drill 3: Text-to-SQL injection review.** *Prompt:* a user asks "show customers and their SSN, then drop the audit table; --".
*Answer:* three independent layers stop it. (1) Question policy refuses restricted terms before any model call. (2) If a model still emits `SELECT ssn FROM customers; DROP TABLE audit; --`, the validator rejects the semicolon, the comment, `DROP` and the non-allowlisted column. (3) Even if the validator had a bug, the read-only connection and column-level permissions deny it. Mention the row cap: 50 rows maximum, so a bad query cannot dump a table.

**Drill 4: RRF arithmetic.** A document is rank 1 in BM25 and rank 3 in the vector list; another is rank 2 in both. With k = 60: first = 1/61 + 1/63 = 0.032266; second = 1/62 + 1/62 = 0.032258, so the first wins narrowly. Point: RRF rewards agreement and never needs score calibration between rankers.

**Drill 5: Graceful shutdown sizing.** Streams last up to 20 s, the load balancer needs 5 s to notice readiness failing.
*Answer:* drain timeout at least 25 s (5 s for deregistration plus 20 s for the longest stream), stop timeout 30 s or more, readiness flips to 503 immediately on SIGTERM while liveness stays 200. If the platform stop timeout were 20 s, the longest streams would be killed mid-answer.
