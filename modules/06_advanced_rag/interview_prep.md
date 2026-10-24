# Senior FDE System Design Crucible

**Prompt:** Design an air-gapped RAG system for a bank's 50,000 concurrent tellers searching 2 million policy and procedure documents, with a 200 ms retrieval SLA (excluding LLM generation) and strict document-level entitlements.

**Requirements**
- Retrieval p95 <= 200 ms at peak; answers cite sources; entitlement filtering per teller role.
- Air-gapped: no external API calls, models self-hosted.
- Freshness: policy changes searchable within 15 minutes.

**Sizing**
- Concurrency: 50,000 tellers, but only ~4% active at once; each issues a query every ~90 s when active: 50,000 x 0.04 / 90 s = **~22 QPS** on average. Opening-of-business rushes run ~10x, so **~220 QPS peak**; use a 300 QPS design target for headroom.
- Corpus: 2 M docs x 12 chunks = **24 M chunks**. Vectors at 768 dim, float32: 24 M x 768 x 4 B = **73.7 GB**; with int8 scalar quantization = **18.4 GB**; HNSW graph overhead (~M=32 neighbors x 4 B x 24M) = 3 GB. Plan 2 replicas x 32 GB RAM nodes, sharded 3 ways if a single node is not enough.
- BM25 inverted index: ~24 M chunks x ~120 tokens; postings ~ 1.2x raw text = ~7 GB. Text store: 2 M docs x 40 KB = 80 GB on SSD.
- Latency budget (200 ms): query embedding 15 ms (CPU ONNX, batch 1) + parallel dense ANN 25 ms / BM25 15 ms + RRF 1 ms + entitlement filter in-index (0 ms extra) + rerank top-30 with a distilled cross-encoder on GPU 70 ms + network 20 ms = **~131 ms**, leaving 70 ms slack.
- Rerank GPU: 300 QPS x 30 pairs = 9,000 pairs/s. A small cross-encoder on one L4-class GPU does ~3,000 pairs/s, so need **~4 GPUs** (N+1 = 5).
- Embedding at ingest: 24 M chunks, one-time at 1,500 chunks/s/GPU = 4.4 hours on one GPU; incremental ~2,000 chunks/day trivial.

**Component breakdown**
1. Ingest: parse -> structure-aware chunker -> embed + tokenize -> write to both indices with `acl_groups` and `doc_version` metadata.
2. Query service: parallel dense/sparse with per-leg timeouts (degraded mode: if one leg fails, return the other and flag it), RRF, rerank.
3. Entitlement filter pushed down into the index query (pre-filter), never post-filter (post-filter leaks counts and wastes top-k).
4. Blue/green index aliasing for re-embedding; freshness via a 5-minute micro-batch from the change feed.
5. Eval harness: 1,000 labeled queries, recall@50 and nDCG@5 gate every index or model change.

**Failure modes**
- GPU reranker saturated: shed to RRF-only ranking when queue wait > 30 ms (graceful degradation), alert.
- Pre-filter too selective makes ANN recall collapse: use filtered-HNSW with fallback to brute force on tiny allowed sets.
- Stale ACLs: ACL changes propagate through the same change feed with a 2-minute SLO; deny on lookup failure.
- Index corruption on node restart: snapshots plus replica promote; alias swap is atomic.
- Embedding model upgrade: dual-write new index, compare recall on the golden set, then flip the alias.

# Client Simulation Challenge

**Scenario.** The client's CISO says: *"I will not allow our policy documents to be sent to a cloud embedding API. Your demo used one. I need to know whether this project is dead."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "You are right to stop us there. The demo used a hosted embedding service for speed of setup, and I should have flagged that it was a demo-only choice. Your policy documents have not been sent anywhere. The demo used public sample text, and I can show you the network logs from the session."

*Impact.* "If real policies had left your environment, that would have been a data-handling violation and a breach of the trust this project depends on. So I understand why you're asking whether the project is dead. It isn't, but it has to be built the way you require."

*Remedy.* "The production design is fully air-gapped. We deploy an open-weight embedding model and a reranker inside your VPC, on your GPU nodes, with no outbound internet route. The model weights are delivered once, through your approved transfer process, with checksums your team verifies. Retrieval quality in our tests with open models is within two points of the hosted service on recall at fifty, and I'll prove it on a sample of your own documents, inside your environment, before you commit to anything further. I'd also suggest your security team review the egress rules directly: a deny-all outbound policy is the control that makes the guarantee real, rather than my promise."
