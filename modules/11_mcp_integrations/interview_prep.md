# Senior FDE System Design Crucible

**Prompt:** Design the integration layer that lets AI agents at a global manufacturer read from SAP (SOAP/RFC), an Oracle reporting DB and Jira, and create Jira tickets and ERP change requests, for 15,000 employees. Legacy ERP capacity is 40 concurrent users and must not degrade month-end close.

**Requirements**
- Standard tool interface (MCP) usable by multiple agents and vendors; per-user permissions propagate to backends.
- Protect backends: cap load, degrade gracefully, never write without approval.
- Auditable: who (user + agent) invoked which tool with what arguments, and what changed.

**Sizing**
- Users: 15,000; assume 12% weekly active using agents = 1,800; peak concurrent agent sessions = 5% of those = 90; each session issues a tool call every ~20 s => **4.5 tool calls/s**; with agent fan-out (3 parallel calls) peak **~14 calls/s** across all backends.
- Backend split: 55% Oracle reporting (read replica), 30% Jira, 15% SAP SOAP. SAP load = 14 x 0.15 = **2.1 calls/s**; at ~600 ms per call that is 1.3 concurrent calls, well within the 40-user capacity; but an unbounded agent loop could reach 50+/s, so enforce a **hard cap of 8 calls/s and 6 concurrent** with a token bucket and a bulkhead semaphore, plus a **cache** (60 s TTL) with expected 45% hit rate => ~1.2 backend calls/s.
- Oracle: read replica connection pool of 30; `statement_timeout` 5 s; `FETCH FIRST 50 ROWS` cap. Average query 80 ms => 14 x 0.55 x 0.08 = 0.6 concurrent. Fine.
- Jira Cloud: rate limit ~100 requests/s per app but per-user quotas lower; use a single service app with per-user attribution, 429 handling, and cache of issue reads (30 s).
- Audit log: 14 calls/s x 86,400 x 0.3 duty (business hours) = ~360k calls/day x 2 KB = **~720 MB/day**, 1 year = 260 GB; ship to the SIEM.
- MCP server pods: stateless, 3 replicas (N+1 across 2 zones), 500 req/s each is far above need, sized for availability not throughput.

**Component breakdown**
1. MCP gateway: OAuth2 token validation (Module 12), scope-to-tool mapping, tool registry with versioned JSON Schemas.
2. Tool servers per domain (ERP, Data, Work-management) deployed independently; each owns its adapters and breakers.
3. Adapters: SOAP (envelope builder/parser, fault mapping, WS-Security through a vault-injected secret), SQL (read-only role, allowlisted views, bound params), Jira REST (idempotency keys on create).
4. Write path: tools that mutate return a **proposal** (diff + preview); a separate `commit_change` tool requires human approval token (Module 10).
5. Resilience: per-backend token bucket, bulkhead, circuit breaker, jittered retries honoring `Retry-After`, response cache.
6. Observability: per-tool latency/error metrics, backend saturation dashboards, audit trail to SIEM.

**Failure modes**
- Agent loop hammers a backend: per-backend and per-user quotas, loop detection on repeated identical calls, breaker.
- Backend outage: open breaker returns `retryable: true` with `retry_after_s`; agents switch to cached or tell the user plainly.
- Schema drift in SOAP response: contract tests on recorded payloads, strict output validation, alert on parse failures.
- Confused-deputy: tool executes with the *user's* delegated identity, not a god-mode service account; row-level security in Oracle.
- Prompt injection from a Jira description: tool outputs flagged as untrusted content in the prompt; no tool may be invoked solely on instructions found in tool output without user confirmation.

# Client Simulation Challenge

**Scenario.** The client's Head of ERP Operations says: *"You want to put an AI in front of our SAP system. Last time a vendor's integration locked up month-end close for six hours. Give me one reason I should say yes."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "That incident is exactly the risk I'd want protected against, and I wouldn't ask you to say yes on trust. The failure pattern is predictable: a client that doesn't know your system's limits sends traffic as fast as it can. AI agents are worse at that than most, because a confused agent can loop."

*Impact.* "If this lock-up happened again during close, the cost isn't only six hours of your team's time, it's finance reporting deadlines and the credibility of the whole AI program."

*Remedy.* "So the design starts from your system's limits, not the AI's appetite. First, every call goes through a gateway that caps SAP traffic at eight calls a second and six at a time, numbers your Basis team sets and can change, far below your tested capacity. Second, if SAP slows or errors, a circuit breaker stops sending traffic within seconds and the agent tells the user it's unavailable, so we back off rather than pile on. Third, there's a month-end freeze switch your team controls that turns the integration read-only from cache. And everything is read-only for the first quarter: the agent can look things up, but any change is a proposal a person approves. I'll run a load test against your quality system with a deliberately misbehaving agent, with your Basis team watching their dashboards, before we touch production."


# Extension Practice

**1. Webhook dedupe sizing.** Your Slack app receives 50 events/s at peak and you keep event ids for 1 hour. How much memory, and why not longer? *Answer:* 50 x 3,600 = 180,000 ids x ~100 B (id + timestamp + dict overhead) = **~18 MB**, trivial in memory; use Redis `SET NX` with a TTL if you run several replicas. Retries arrive within minutes, so an hour is ample, and a longer TTL only adds memory. Pair it with a 5-minute timestamp window so a captured request cannot be replayed later.

**2. Paging a Jira project.** 12,500 issues, `maxResults=100`, and you self-limit to 5 requests/s. How long does a full read take, and what do you do on a 429? *Answer:* 125 requests / 5 per second = **25 s** minimum. On a 429, sleep for the `Retry-After` value (not your own guess), then continue from the same `startAt`; add jitter and a hard page cap so a bad `total` cannot loop forever.

**3. Text-to-SQL success math.** First-try success is 70%, and each repair fixes half of the remaining failures. A reviewed template covers 60% of the questions that still fail. What are the outcomes and the average model calls? *Answer:* failures after two repairs = 0.30 x 0.5 x 0.5 = 7.5%, so **92.5% answered by the model**; templates rescue 0.075 x 0.6 = 4.5%; **3.0% refused**. Average calls = 1 + 0.30 + 0.15 = **1.45**. Track repair rate and refusal rate: a rising repair rate usually means schema drift or missing column descriptions.

**4. XML bomb arithmetic.** Ten nested entities each referencing the previous one ten times: how large is the expansion and what is the cheapest defense? *Answer:* 10^9 repetitions of a 3-byte string = **~3 GB** from a file of a few hundred bytes. Cheapest defense: reject any DOCTYPE or ENTITY before parsing (SOAP never needs one), plus a size cap (1 MB) and element and depth caps; use `defusedxml` as a second layer.

**5. Schema context budget.** 40 tables x 12 columns x ~8 tokens is ~3,800 tokens before descriptions. Your budget is 1,500. What do you cut, in order? *Answer:* drop sample values first, then column descriptions, then rank tables by keyword overlap with the question and keep the best until the budget is full. Never cut the foreign keys of kept tables (joins need them) and never include hidden PII columns to make room look better.
