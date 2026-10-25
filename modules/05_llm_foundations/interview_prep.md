# Senior FDE System Design Crucible

**Prompt:** Design a contract-intelligence extraction service for a law firm's client: 120,000 contracts (average 45 pages) backfill plus 1,500 new contracts/day, extract 30 typed fields with >= 97% field-level accuracy, every output auditable and reproducible.

**Requirements**
- Typed output validated against a schema; invalid output never reaches the matter-management system.
- Reproducible: same document + same prompt version -> same result; versioned prompts and models.
- Backfill within 14 days; daily SLA of 15 minutes per new contract.

**Sizing**
- Tokens per contract: 45 pages x ~500 words x 1.33 tokens/word = **~30,000 tokens**. Fits a 128k window, but quality improves with section routing; plan two passes (locate clauses, then extract) averaging 14k input tokens each + 2k output = **~30k input, 2k output** per contract.
- Backfill: 120,000 x 30k = **3.6 B input tokens**, 240 M output tokens. At $3/M input and $15/M output = $10.8k + $3.6k = **~$14.4k**; with prompt caching of the shared instructions (2k of each call) and batch API at 50% discount: **~$7k**.
- Throughput: 14 days = 1.21 M seconds; 120,000 contracts / 1.21 M s = ~0.1 contracts/s average. Plan for 10x headroom (retries, uneven batches): 1 contract/s x 30k tokens = **30k input tokens/s**, so request a ~2M tokens-per-minute tier (33k/s). Run async with concurrency ~20 (each call takes ~15-20 s).
- Daily: 1,500 x 32k = 48 M tokens/day, trivial ($150-$200/day).
- Storage: raw PDFs 120k x 2 MB = 240 GB; extracted JSON 120k x 6 KB = 720 MB; audit log of prompts/responses 120k x 70 KB = **8.4 GB**.
- Repair budget: expect 4% of docs need one repair (+4% cost); cap at 2.

**Component breakdown**
1. Ingestion + OCR/layout parse (Module 08), section splitter.
2. Context budgeter: system prompt + clause-specific few-shots + retrieved sections; refuses to send when over budget.
3. Extractor: JSON schema from Pydantic, temperature 0, Instructor-style validate/repair (max 2), model + prompt version pinned in a registry.
4. Validators: type, cross-field (end date after start date), source-grounding (every value must quote a span found in the text, verified by substring match).
5. Review queue for low-confidence or failed validation; reviewer corrections become the eval set.
6. Audit store: input hash, prompt version, model id, raw output, validated output.

**Failure modes**
- Silent truncation or lost-in-the-middle: token counting plus needle tests; section-scoped prompts.
- Model upgrade drift: pinned model ids, golden set of 500 contracts, block rollout if field accuracy drops > 1 point.
- Hallucinated values: grounding check rejects values lacking a source quote.
- Provider outage/rate limits: queue-based backfill, backoff with jitter, secondary provider with identical schema tests.
- Prompt injection inside a contract ("ignore previous instructions"): treat document text as data, delimit it, and validate outputs against schema only (see Module 13).

# Client Simulation Challenge

**Scenario.** The client's lead architect says: *"Last quarter's model update changed how you extract 'governing law' for about 8% of our contracts. Nobody told us. How do I trust a system whose behaviour changes under me?"*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "You're describing a real failure, and the cause is on our side. The provider updated the model behind the alias we were calling, and our regression tests didn't cover the governing-law field with enough variety. So behaviour changed without a release on our end and without notice to you."

*Impact.* "Eight percent of your contracts carried a different governing-law value for several weeks. Anyone who relied on that field for risk routing could have made a decision on incorrect data, and that is exactly the kind of silent change an architect should not have to tolerate."

*Remedy.* "We're making three commitments. We now pin an exact model version, so nothing changes unless we change it. Every model or prompt change must pass a golden set of 500 of your own contracts, field by field, and we block the release if any field drops by more than one point. And you will see a change log before any change reaches your tenant, with the evaluation results attached. For the 8% already affected, we'll re-run them under the pinned model this week and send you a diff report so your team can decide which downstream records to correct."


# Extension Practice

**1. Price a request.** 10,000 input tokens, 500 output tokens, illustrative prices of $3 and $15 per million tokens. What does it cost, and what if 9,000 input tokens are served from a cache priced at $0.30 per million?
*Answer:* uncached = 10,000 x 3/1e6 + 500 x 15/1e6 = $0.03 + $0.0075 = **$0.0375**. Cached = 1,000 x 3/1e6 + 9,000 x 0.30/1e6 + $0.0075 = $0.003 + $0.0027 + $0.0075 = **$0.0132**, about 65% cheaper. Say the prices are placeholders and name where you would read real ones.

**2. Size a sliding window.** Budget 8,000 tokens, 1,000 reserved for output, system prompt 500, average turn (user + assistant) 700. How many turns fit, and what must never be split?
*Answer:* (8,000 - 1,000 - 500) / 700 = 9.3, so **9 turns**. Never split an assistant tool call from its tool result, and never drop the system prompt. If the oldest turns matter, add a running summary (compression) instead of widening the window.

**3. Parallel tools and timeouts.** Three independent tools take 400 ms, 900 ms and 250 ms. Latency sequential vs parallel, and with a 500 ms per-tool timeout?
*Answer:* sequential 1,550 ms; parallel `asyncio.gather` ~**900 ms** (the slowest); with a 500 ms timeout the 900 ms tool returns an error result and total latency is ~**500 ms**. Results must keep their call ids because completion order is not request order.

**4. The model calls `get_weathr`.** What do you do and why must you still answer that call id?
*Answer:* do not execute; return an error tool result ("Unknown tool, available: ..., did you mean get_weather?") with the same id. Providers reject a follow-up request when a requested id has no result. Cap tool rounds (say 5), reject identical repeated calls, and alert on cap hits.

**5. Why a discriminator?** A union of three action models receives `{"kind": "fax", ...}`. Compare the error with and without `Field(discriminator="kind")`.
*Answer:* without it the model tries all three members and returns up to three error groups, mostly noise that bloats the repair prompt. With it you get one error ("tag 'fax' does not match expected tags 'email', 'call', 'meeting'"), the repair call is shorter and succeeds more often, and valid payloads validate faster because only one branch is tried.
