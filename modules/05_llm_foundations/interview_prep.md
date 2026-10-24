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
