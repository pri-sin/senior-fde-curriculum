# Senior FDE System Design Crucible

**Prompt:** Design a durable agent platform for a telecom's customer-care automation: 30,000 conversations/day, each averaging 9 agent steps, some requiring human approval for credits above $50, runs can pause for up to 3 days, and no action may be applied twice.

**Requirements**
- Resume after crashes/deploys at the last completed step; exactly-once-effective side effects.
- Human approval gates with SLA tracking; audit log of every transition.
- Cost cap per conversation; no infinite loops.

**Sizing**
- Steps: 30,000 x 9 = **270,000 steps/day** = 3.1 steps/s average; peak 10x = **31 steps/s**.
- LLM tokens: per step ~1,800 input + 250 output; 270,000 x 2,050 = **553 M tokens/day**. At blended $4/M = **~$2.2k/day**; with a small model for routing/critique (60% of steps at 1/10 the price): ~$1.0k/day.
- Checkpoint writes: one per step = 270,000/day; state ~12 KB (references, not documents) = **3.2 GB/day**; keep latest + milestone checkpoints: 90 days = ~60 GB after pruning. Postgres on one primary handles 31 writes/s trivially; JSONB per thread row keyed (thread_id, step).
- Concurrency: step latency ~2.5 s (LLM-dominated) => 31 x 2.5 = ~78 concurrent steps at peak. Run 8 async workers x 20 concurrent = 160 slots (2x headroom).
- Paused runs: 8% need approval = 2,400/day; avg wait 4 h => ~400 parked threads at any time, zero compute while parked (state is in the DB).
- Budget guard: max 25 steps and 40k tokens per conversation; p99 conversation uses 14 steps, so limits trigger only on genuine loops.

**Component breakdown**
1. API: create/resume run endpoints returning run id; SSE for progress.
2. Worker pool: pulls `(thread_id, next_node)` from a queue (visibility timeout), loads checkpoint, executes one node, writes checkpoint transactionally with the next queue message (outbox).
3. Checkpointer: Postgres, state schema-versioned with migration functions.
4. Tool gateway: idempotency keys `(thread_id, step, tool)`, circuit breakers, per-tool timeouts and retries with jitter.
5. Approval service: interrupt creates a task in the agent-ops UI; resume via signed callback; SLA timers escalate at 4 h.
6. Observability: step traces (Module 14), loop detector (repeated identical critic feedback), cost meter.

**Failure modes**
- Worker dies mid-node: queue visibility timeout redelivers; node re-runs; idempotency keys protect effects.
- Deploy changes state schema: `schema_version` + upcasters; old threads continue on compatible nodes, incompatible ones are drained.
- Approval never arrives: SLA escalation, then auto-cancel with customer message after 3 days.
- Checkpoint DB outage: workers stop pulling (fail closed); no step runs without durable state.
- Poison conversation (crashes every attempt): max 3 redeliveries, then dead-letter with the last checkpoint for engineers to replay.

# Client Simulation Challenge

**Scenario.** The client's VP of Engineering says: *"Your agent got stuck last Tuesday and kept calling the language model for six hours on one ticket. We got a $4,000 bill for a single customer. I'm told this is normal for agents. It isn't acceptable."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "You're right that it's not acceptable, and it isn't normal for a well-built agent. On that ticket the drafting step and the review step disagreed with each other indefinitely: the reviewer kept finding something small to change. We had no ceiling on the number of loops and no budget attached to the conversation, so nothing stopped it."

*Impact.* "Four thousand dollars is the visible cost. The less visible one is that the customer's ticket was never answered, so you paid and got nothing."

*Remedy.* "Three controls, all already designed into the graph model we're moving to. First, a hard limit of twenty-five steps per conversation, after which the case goes to a human with everything the agent did so far. Second, a token budget per conversation, so a runaway is capped at about two dollars, not four thousand. Third, a stall detector: if the reviewer gives the same feedback twice, the loop stops and escalates, because repeating the same instruction is not progress. I'll show you the replay of last Tuesday's ticket under the new limits in the next review, so you can see it stop at step twelve."


# Extension Practice

**1. Two parallel branches both write `summary`. What should the framework do, and what do you change?**
Raise, do not pick a winner: last-finisher-wins makes results depend on timing. Fix by giving the field a reducer (append and merge later), or by making one node the single writer (a synthesize node after the join). In the lab, `StateConflictError` names the field and the writers.

**2. Fan-out of 3 branches at 2.0 s, 2.5 s and 3.0 s each. Latency serial versus concurrent? What if the third has a 2.8 s timeout?**
Serial = 7.5 s; concurrent = max = 3.0 s. With a 2.8 s timeout the run fails at 2.8 s (the slowest branch is cut off), so decide per branch whether a timeout is fatal or whether you merge partial results and mark the field degraded.

**3. Map-reduce over 1,000 documents, 40 ms per summary, concurrency limit 20. Wall time and what is the risk of raising the limit to 200?**
Serial = 1,000 x 40 ms = 40 s. At limit 20: 1,000 / 20 = 50 waves x 40 ms = about 2 s; at limit 200: about 0.2 s. The limit is protecting the provider: 200 concurrent calls can hit rate limits (429s), which retries then amplify. Raise it only with backoff and a provider quota that supports it.

**4. ReAct versus Plan-and-Execute for a 6-step data pull where steps are known in advance. Which, and roughly why?**
Plan-and-Execute: 1 planning call + 6 cheap tool executions, versus ReAct re-reading a growing transcript on all 6 steps (transcript cost grows roughly quadratically in steps). Keep a replanner for failures. Choose ReAct when each step depends on what the previous one revealed.

**5. Your compiled graph passes, yet a prod run spins until the recursion limit. What did compile-time validation miss?**
Static checks prove a path to END exists, not that the router ever takes it. Add a runtime guard (recursion limit, token budget, repeated-state detection) and test the router on adversarial states.
