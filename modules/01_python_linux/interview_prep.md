# Senior FDE System Design Crucible

**Prompt:** Design the ingestion worker tier for a bank that must enrich 40 million documents per night through a rate-limited third-party KYC API (600 req/s contractual cap) in an 6-hour window, on Kubernetes, with exactly-once-effective semantics and clean rollouts.

**Requirements**
- Throughput: finish 40M docs in <= 6 h; API cap 600 req/s shared across all pods.
- Reliability: no lost documents; duplicates tolerated only if the downstream write is idempotent.
- Operability: SIGTERM during a rolling deploy must lose zero in-flight work; p99 loop lag < 250 ms.

**Sizing**
- Needed rate: 40,000,000 / 21,600 s = **1,852 docs/s**. One doc = 1 API call, so the cap (600/s) is the bottleneck: 40M / 600 = 66,667 s = **18.5 h**. The design cannot meet the SLA as stated. Present this immediately: options are a negotiated cap of >= 2,000 req/s, batching endpoints (10 docs/call needs only 185 calls/s), or caching on content hash (a 70% duplicate rate cuts calls to 12M = 5.5 h at 600/s).
- With batching at 10: 185 req/s, mean latency 120 ms, so Little's Law gives concurrency = 185 x 0.12 = **~22 in flight**. One asyncio pod with Semaphore(32) suffices; run 4 pods for availability, each with a leased rate share of 150 req/s (Redis token bucket).
- Queue: 40M x 2 KB metadata = 80 GB in Kafka at RF3 = 240 GB; 12 partitions per consumer group, 4 pods x 3 partitions.
- Memory: 22 in flight x 50 KB + queue buffer 1,000 x 2 KB = tiny; budget 512 MiB per pod, mostly interpreter and libraries.

**Component breakdown**
1. Kafka topic `docs.pending` (partitioned by tenant) with a DLQ `docs.failed`.
2. Worker pod: consumer -> bounded `asyncio.Queue(1000)` -> N workers -> semaphore -> batcher -> API. Offsets commit only after the idempotent write (key = content hash + version).
3. Distributed token bucket in Redis (Lua script) so pod count can change without breaching the cap.
4. Shutdown: preStop hook sleeps 5 s (endpoints deregister), SIGTERM sets the stop event, consumer pauses, drain with timeout = `terminationGracePeriodSeconds - 10`, uncommitted messages are simply re-read.
5. Observability: loop-lag gauge, queue depth, retry ratio, fd count, per-attempt latency histogram.

**Failure modes**
- Upstream brownout: circuit breaker opens after 50% errors over 20 s; consumers pause rather than burn retry budget; backoff with full jitter.
- Poison document: after 3 permanent failures route to the DLQ with the error payload; never block the partition.
- Pod OOMKilled: bounded queue plus bounded batch size; offsets uncommitted so work is redelivered.
- Duplicate processing after rebalance: idempotent upsert key prevents double effects.
- Clock/rate drift between pods: leases expire in 1 s so a crashed pod's share returns quickly.

# Client Simulation Challenge

**Scenario.** The client's VP of Platform says: *"Your async service passed the pilot, but last week it froze during a rolling deploy and we dropped 3,000 records. Python can't do this. Our Java team says we should rewrite it."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "You're right to be frustrated, and I'd rather give you the facts than a defence. On the night of the deploy, Kubernetes sent a termination signal, our worker stopped taking new work, but the drain window we'd configured was 5 seconds while a batch of enrichment calls takes up to 30. The pod was killed with about 3,000 records in memory. That was a configuration gap on our side, not a language limit."

*Impact.* "The business impact is concrete: 3,000 records were not enriched on time, which delayed the morning compliance report for those accounts. The more important point is the risk it exposed, that any deploy could repeat it, and I understand why that makes a rewrite sound attractive."

*Remedy.* "Three changes, all in place by Friday, with evidence you can inspect. First, we commit offsets only after the write succeeds, so a killed pod means redelivery, never loss. Second, the drain window is now 45 seconds, matched to the worst-case call, and we test it in your staging cluster by killing pods mid-batch during a load test. Third, we've added loop-lag and in-flight gauges to your Grafana so you can see health yourself. A rewrite would cost roughly two quarters and would not by itself change any of those three controls; the same gap could appear in Java. If after the load test you still want to evaluate Java, I'll support a time-boxed comparison on the same acceptance criteria."
