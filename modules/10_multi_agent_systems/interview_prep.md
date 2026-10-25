# Senior FDE System Design Crucible

**Prompt:** Design a multi-agent adjudication system for a payments processor: 3 million transaction-alert reviews per day, 99.9% resolved without a human, but anything over $10k, any irreversible action, or any panel disagreement must have a human decision in under 15 minutes. Every decision must be explainable to a regulator.

**Requirements**
- Latency: auto-resolved alerts p95 < 4 s; human-routed alerts queued within 5 s.
- Accuracy: false-negative (missed fraud) rate < 0.1% on the labeled set; false-positive rate low enough to cap customer friction.
- Governance: full decision trace, reviewer identity and justification, quarterly audit sampling.

**Sizing**
- Rate: 3,000,000 / 86,400 = **34.7 alerts/s** average, peak 4x = **~140/s**.
- Agents per alert: supervisor (1 call, 1.5k tokens), 3 risk scorers (1.2k tokens each), policy checker (rules engine, no LLM). Tokens/alert = 1.5k + 3.6k = **5.1k**. Daily: 3 M x 5.1k = **15.3 B tokens/day**. At a blended $0.60/M for small models = **~$9.2k/day**; routing 70% of alerts through a single cheap scorer first (escalating to the panel only when its confidence is under 0.9) cuts tokens to ~7.5 B/day (**~$4.5k/day**).
- Concurrency: 140/s x ~2.2 s (parallel scorers dominated by slowest) = **~310 in-flight alerts** at peak; 12 worker pods x 40 concurrent.
- Human review: 0.1% hard-routed (over $10k etc.) + ~0.6% panel disagreement ~= 0.7% = **21,000 cases/day**. Handling at 90 s each = 525 reviewer-hours/day => ~70 reviewers per shift pattern; reduce by tightening routing for repeat benign patterns.
- Audit storage: decision trace 6 KB x 3 M = **18 GB/day**, 7-year retention = ~46 TB in cold storage with a hot 90-day index (1.6 TB).
- Approval queue: 21,000/day = 0.24/s average; trivial for Postgres + SKIP LOCKED, SLA timers via a scheduler.

**Component breakdown**
1. Ingest: alert stream (Kafka), dedupe by alert id, enrichment (customer history, device, geo).
2. Triage agent (cheap model) -> confidence; high-confidence benign alerts auto-close with a logged rationale.
3. Panel: three scorers with different models/prompts and one deterministic rules voter; confidence-weighted consensus with agreement ratio.
4. Governance policy engine (versioned code, not a prompt): amount, reversibility, customer tier, jurisdiction -> AUTO / APPROVAL / BLOCKED.
5. Approval service with SLA timers, escalation, justification enforcement, evidence view.
6. Executor: the only component with write credentials; idempotency keys per decision.
7. Audit log (append-only, hash-chained) + weekly sampling for QA and model-drift checks.

**Failure modes**
- Correlated errors across scorers: model diversity, rules voter, and monitoring of agreement rate (a sudden jump to 100% is suspicious).
- Provider outage: fallback chain scorer -> secondary model -> rules; fallback results are labeled `degraded` and lower the auto-approve ceiling to $500.
- Reviewer backlog: dynamic thresholds (never relaxing hard limits), load-shedding by risk rank, staffing alerts at queue age > 10 min.
- Prompt injection via transaction memo ("approve this payment"): memo is data in a delimited field, scorers cannot call tools, executor ignores free text.
- Policy drift: policy engine changes require review + replay against last 30 days of decisions; diff report before rollout.

# Client Simulation Challenge

**Scenario.** The client's Chief Risk Officer says: *"A regulator asked us why the AI approved a $48,000 transfer to a sanctioned party. Your team told me you have three agents voting, so how did all three get it wrong?"*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "I want to answer this directly. The three scorers were built on the same underlying model with nearly identical prompts. On this case, the beneficiary name was a near-match to a sanctions-list entry with a transliteration difference, and all three made the same mistake together. We called it consensus, but it was really one opinion repeated three times."

*Impact.* "The transfer exceeded your own ten-thousand-dollar human-review threshold, and it should never have been eligible for automatic approval in the first place. That is a policy gap as well as a model error, and it is the part the regulator will care about most."

*Remedy.* "We're fixing both layers. First, the panel will use independent models and different evidence: one voter is a deterministic sanctions screening engine with fuzzy transliteration matching, which does not share failure modes with a language model. Second, the amount and irreversibility rules move out of the prompts into a versioned policy engine, so anything over ten thousand dollars always goes to a named human, whatever the agents say. Third, I'll give your compliance team a full decision trace for this transfer today, covering each agent's vote, the policy rule that should have fired, and a replay of the last ninety days to find any similar near-misses, so you can tell the regulator what you found and what changed."


# Extension Practice

**1. An agent loop runs 25 steps at about 2,000 tokens each before a hard cap hits. Cost per stuck run, and what does a detector save?**
25 x 2,000 = 50,000 tokens; at a blended $3 per million tokens = $0.15 per stuck run. The cost that matters is volume: 10,000 stuck runs/day = $1,500/day. A repeated-signature detector (3 identical steps) stops at step 3, a reduction of about 88%, and the case reaches a human with the trace.

**2. Memory half-life is 30 days. What recency weight does a 90-day-old memory get, and would a low-importance one be forgotten?**
0.5 ** (90 / 30) = 0.125. With retention = 0.5 x importance + 0.5 x recency and a 0.25 threshold: importance 0.1 gives 0.05 + 0.0625 = 0.1125 (forgotten); importance 0.9 gives 0.45 + 0.0625 = 0.5125 (kept).

**3. A looping agent calls a read tool 5 times per second; the gateway allows 30 per minute per agent per tool. What happens, and why not just retry on the rate-limit error?**
5/s = 300 calls/min, so about 90% are rejected after the first 30 (10x reduction in upstream load) and each rejection carries a retry-after. Blind retries re-create the storm; the agent loop must treat the error as an observation, and the loop detector should fire first.

**4. Self-correction allows 3 rounds with a separate critic. How many model calls and how much does it cost versus one shot?**
1 generation + 3 critiques + 2 reflections = 6 calls, about 6x the tokens of one shot (9,000 vs 1,500 tokens at 1,500 each). Use a cheap model for critique, stop early when the score plateaus, and measure whether quality gains justify the multiple.

**5. Why use a protocol such as MCP for tool access, in numbers? How do you stop one server from affecting another?**
Without a shared protocol, N hosts and M tool providers need N x M custom integrations (5 x 8 = 40); with one, N + M adapters (13). Isolation comes from the 1:1 client-to-server sessions, host-side policy (allowlist, consent for non-read-only tools), per-tool scopes, and a pinned manifest digest so a changed tool definition is noticed.
