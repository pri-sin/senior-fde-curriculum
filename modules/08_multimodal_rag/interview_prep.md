# Senior FDE System Design Crucible

**Prompt:** Design a document-intelligence pipeline for an insurer processing 2 million pages/day of claims (60% born-digital PDF, 30% scans, 10% photos of forms), extracting 25 fields plus damage-estimate tables, with 99% accuracy on monetary fields and p95 end-to-end under 90 seconds per claim packet (average 12 pages).

**Requirements**
- Monetary fields: >= 99% accuracy; everything else >= 95%. Every value traceable to page and bounding box.
- Handle skewed photos, handwriting on forms, multi-column letters, and tables spanning pages.
- Human review for low-confidence values only; reviewer throughput targets 5% of fields.

**Sizing**
- Throughput: 2,000,000 pages/day / 86,400 = **23 pages/s** average; peak (business hours, 3x) = **70 pages/s**.
- Compute per page: layout model + OCR ~600 ms on GPU (batched), table structure 300 ms. 70 pages/s x 0.9 s = **63 GPU-seconds/s**, so ~63 concurrent slots; with batching at ~8 pages/GPU: **~8 GPUs** (12 with N+4 headroom).
- Storage: originals 2 M x 400 KB = **800 GB/day** (~290 TB/yr; lifecycle to cold storage after 90 days); parsed JSON 2 M x 25 KB = 50 GB/day; page images at 150 dpi WebP 2 M x 150 KB = 300 GB/day only for the retrieval tier (keep 30 days).
- Page-image index (ColPali): ~1,000 patch vectors x 128 dim x 1 byte (binary/int8 quantized) = 128 KB/page; 30 days x 2 M = 60 M pages = **7.7 TB** of vectors; costly, so restrict to document classes that need it (slides, diagrams ~8% = 0.6 TB).
- VLM answering for visual pages only: 8% of 2 M = 160k pages/day x 1,500 image tokens = 240 M tokens/day (~$720/day at $3/M).
- Review load: 2 M pages x ~6 fields = 12 M fields/day; 5% to review = 600k fields; at 8 s each = 1,333 reviewer-hours/day, which is too many, so lower the review band to monetary fields below 0.9 confidence plus random 1% audit = ~90k fields = **200 reviewer-hours/day (~25 reviewers)**.

**Component breakdown**
1. Router: born-digital (text layer) vs scan vs photo; photos get deskew/denoise/perspective correction.
2. Parsing: layout detection -> reading order -> element stream with bbox/confidence; table structure recognition; cross-page table stitching by header match.
3. Field extraction: key-value models for forms; LLM extraction from table-records with Pydantic validation (Module 05).
4. Validators: arithmetic (line items sum to total), format (dates, currency), cross-document (claim number consistency).
5. Review UI showing the crop of the source region next to the value; corrections stored as labeled data.
6. Retrieval tier: hybrid text index for all; page-image multi-vector index for visual classes.

**Failure modes**
- Accounting-format misreads ((1,204) vs 1,204): parser rules plus total-consistency validation.
- OCR service brownout: queue with retries and backoff; degrade to born-digital text layer where available.
- Table split across pages loses header: stitch by repeated header or column-count/alignment heuristic; flag if ambiguous.
- Model/parser upgrade regresses a field: golden set of 2,000 annotated pages gating releases, field-level accuracy dashboards.
- Prompt injection in uploaded document ("approve this claim"): extraction output is validated data only; no agentic actions from document text.

# Client Simulation Challenge

**Scenario.** The client's head of claims operations says: *"Your extraction misread a repair estimate: $1,204 credit became a $1,204 charge. We paid a claim wrong. My team now says OCR can't be trusted and wants to go back to manual keying."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "Thank you for flagging it precisely, because it lets me be precise about what went wrong. The estimate showed the credit in parentheses, the accounting convention for a negative number. Our pipeline treated the parentheses as punctuation and read the value as positive. That was a defect in our parsing rules and in our validation, not an inevitable limit of automation."

*Impact.* "One claim was overpaid by twice the credit amount, $2,408, and the incident undermined your team's confidence, which is the harder thing to repair. I'd rather you keep a healthy skepticism than trust us blindly."

*Remedy.* "Three controls, effective this week. First, the parser now treats parenthesized values as negatives, with a regression test built from this exact document and thirty other format variants your adjusters have seen. Second, every estimate table has to reconcile: line items must sum to the stated total, and any mismatch blocks the field and routes it to a person, with the source image crop beside the value. That check would have caught this claim. Third, we'll measure monetary-field accuracy weekly against a sample your team keys by hand and report it to you; if it falls below ninety-nine percent we pause automatic payment recommendations for that document type. Manual keying isn't free of error either, so I'd like us to compare error rates on the same hundred claims before deciding."


# Extension Practice

**1. Patch-index storage.** A ColPali-style index keeps 1,024 patch vectors of 128 dimensions per page, int8 (1 byte per dimension). How big is 1 million pages, and how do you cut it?
*Answer.* 1,024 x 128 = 131,072 bytes = 128 KiB per page, so 1 M pages is about 131 GB of vectors before index overhead. Cuts: index only visual document classes (slides, diagrams), keep a text index for the rest, binarize vectors (1 bit per dimension is 16 KiB per page, 8x smaller), and merge or prune near-blank patches (margins and whitespace are often a third of a page).

**2. InfoNCE sanity check.** Batch of 256 pairs, untrained model. What loss do you expect, and what does a larger batch change?
*Answer.* Chance level is ln 256 = 5.55, because each image must pick its caption among 256 candidates. A batch of 32,768 has chance level ln 32768 = 10.4: the task is harder (more negatives), so each step gives a stronger training signal. If the loss at step 0 is far below ln N, suspect leakage (for example the same caption used for many images in a batch); if it never gets below ln N, suspect misaligned pairs.

**3. Image token budget.** A 3000 x 2000 photo is sent at full size, and a 12-page packet is sent as page images at 1275 x 1650. Estimate tokens using the lab's heuristic.
*Answer.* The photo is downscaled so the long edge is 1,568 px, giving 1568 x 1045, and 1568 x 1045 / 750 = about 2,184 tokens, so uploading the 6 MP original only adds bytes and latency, not tokens or detail. Each page is about 2,533 tokens, so the packet is about 30,400 tokens. If the budget is 8,000 image tokens, select 3 pages (relevance plus neighbours) instead of sending all 12.

**4. Region assignment metric.** A layout model predicts a table box (0, 0, 10, 10); the true box is (5, 0, 15, 10). Compute IoU and containment, and say which one decides word assignment.
*Answer.* Intersection = 5 x 10 = 50, union = 100 + 100 - 50 = 150, so IoU = 0.33. Containment of the predicted box in the truth is 50 / 100 = 0.5. IoU is the right metric for scoring the layout model (it penalizes size errors both ways); containment is the right test for deciding whether a given word belongs to a region (word area inside region / word area).

**5. Why deskew matters.** A 1,200 px wide scan is skewed by 2 degrees. What happens to line grouping with a 6 px tolerance?
*Answer.* The right edge sits 1200 x tan(2 degrees) = about 42 px lower than the left edge, which is several line heights, so words from one printed line land on different "lines" and table cells shift into neighbouring rows. That is the war-story failure mode in this module. Fix upstream: estimate skew (projection profile, usually within 0.5 degrees), rotate, then group lines; flag pages with skew above about 5 degrees for review.
