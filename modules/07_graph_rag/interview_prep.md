# Senior FDE System Design Crucible

**Prompt:** Design a supply-chain risk graph for a manufacturer: 400,000 suppliers across 6 tiers, 3 M parts, updates from ERP, news and sanctions feeds; analysts ask "which of our products are exposed to supplier X's failure?" with answers in under 3 seconds and an audit trail for each edge.

**Requirements**
- Multi-hop (up to 6) exposure queries; each answer cites source documents.
- Entity resolution across ERP IDs, DUNS numbers and free-text news mentions.
- Daily bulk load plus streaming updates within 10 minutes; analysts read only their business unit's data.

**Sizing**
- Nodes: 400k suppliers + 3 M parts + 60k products + 500k "legal entities/people" = **~4 M nodes**. Edges: parts-to-supplier 6 M, bill-of-materials (part-to-part, product-to-part) 15 M, ownership 1.2 M, news-derived 4 M = **~26 M edges**.
- Memory: Neo4j rule of thumb ~ 75 B/node + 135 B/relationship + properties. 4 M x 75 B = 300 MB; 26 M x 135 B = 3.5 GB; properties (avg 300 B per node) 1.2 GB; indexes 1 GB. Page cache target **~8 GB**; run 32 GB nodes for headroom (3-node cluster: 1 primary + 2 read replicas).
- Query load: 200 analysts x 1 query/20 s peak = 10 QPS graph reads, each 3-6 hops. Typical 6-hop expansion from a mid-degree node touches ~50k edges, ~20 ms warm. Fits comfortably; supernode guard needed.
- Extraction from news: 50,000 articles/day x 1,200 tokens x 2 calls = 120 M tokens/day = ~$360/day at $3/M; use a small model for first pass and a larger one only for ontology-ambiguous items, cutting to ~$120/day.
- Provenance store: 26 M edges x 300 B evidence pointers = 7.8 GB in object/columnar storage, referenced by edge `evidence_id`.

**Component breakdown**
1. Source connectors: ERP (batch CDC), sanctions lists (daily), news (stream).
2. Extraction service with ontology-constrained JSON (Module 05), confidence thresholds, human review queue below 0.6.
3. Entity resolution: deterministic keys first (DUNS, LEI), then blocking + fuzzy name scoring, merges below 0.9 go to review; merge log is reversible.
4. Graph writer: batched `UNWIND ... MERGE` through an async driver, idempotent via stable edge keys, single writer role.
5. Query layer: template Cypher for common questions, LLM text-to-Cypher for long tail through the guard, read replica, 2 s timeout.
6. Answer builder: path + evidence snippets + source links to the LLM, which must cite edge IDs.

**Failure modes**
- Entity-resolution false merge links two unrelated suppliers: reversible merge log, merge-quality audit sample weekly, ambiguous merges never auto-applied.
- Supernode (e.g. a distributor with 80k edges): degree cap with explicit opt-in, precomputed aggregates.
- Stale edges (supplier relationship ended): edges carry `valid_from/valid_to`, queries filter on `as_of`.
- Extraction hallucinated relation: require evidence span verified by substring match; low confidence is shown as "unverified".
- Write-path outage: reads continue from replicas; queue buffers updates; analysts see "data as of" timestamp.

# Client Simulation Challenge

**Scenario.** The client's lead architect says: *"We already have a vector search for our knowledge base. Why do we need to stand up Neo4j and hire people to maintain an ontology? It sounds like a science project."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "That's a fair challenge, and for many of your use cases you're right: if someone asks what a policy says, vector search is faster and cheaper, and I'd keep it. The gap shows up in a specific kind of question. Last month your risk team asked which of your products depend, directly or through a subsidiary, on a supplier that was just sanctioned. Vector search returned paragraphs mentioning the supplier, but nothing connecting the supplier to the products."

*Impact.* "Answering that took your analysts about two days by hand, across three systems. If the answer is late or incomplete, you either halt shipments unnecessarily or miss real exposure, and both have a measurable cost."

*Remedy.* "So I'd propose a narrow scope rather than a big platform. We model only four entity types and four relationships, the ones needed for that question. We load them from data you already have, supplier master, bill of materials and the sanctions feed, and keep vector search for everything else. Success has a hard test: the exposure question answered in under three seconds, with a source for every link, on a hundred historical cases your analysts already solved by hand. If we don't match them, we stop after six weeks and you've spent a fraction of what a platform build would cost. If we do, you'll have seen the value before committing to the ontology work."
