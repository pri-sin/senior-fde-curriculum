# Senior FDE System Design Crucible

**Prompt:** Design the guardrail layer for a bank's employee-facing GenAI assistant (35,000 employees, 60 QPS peak) that summarizes customer emails and documents. Requirements: no customer PII or card data may reach an external LLM API, indirect prompt injection must not trigger actions, and added latency must stay under 120 ms p95.

**Requirements**
- PII recall >= 98% on card/SSN/account numbers, precision >= 95% (avoid blocking part numbers).
- Indirect injection resilience: assistant has an email-send tool and ticket-update tool.
- Full audit with masked content only; reversible detokenization for authorized staff.

**Sizing**
- Traffic: 60 QPS peak; documents average 3,000 characters (~750 tokens); inputs per day: 35,000 employees x 12 requests/day = 420,000 requests = 4.9 QPS average; peak factor 12x = **~60 QPS**.
- Rail compute: regex+Luhn+context analyzers ~1 ms per 3 KB; spaCy NER (small model) ~12 ms per 3 KB on CPU. 60 QPS x 13 ms = 0.78 CPU-seconds/s => **~1 core busy**; run 4 vCPU across 3 pods for headroom (N+1).
- Injection classifier (small distilled model, 20 ms CPU/GPU-batched): 60 QPS => two replicas on one small GPU or 4 CPU pods; adds ~20 ms. Total input rail p95 = 1 + 12 + 20 + parsing 8 = **~41 ms**, inside the 120 ms budget, leaving 79 ms for output rails (secret/PII re-scan ~3 ms, schema check ~1 ms, canary check ~0).
- Token vault: 420,000 requests/day x avg 6 entities = 2.5 M mappings/day x 120 B = **~300 MB/day**, TTL 24 h in Redis (in-VPC, encrypted), keyed by request id. Peak memory 300 MB x 1.5 = ~450 MB.
- Audit logs: 420,000 x 4 KB masked = **1.7 GB/day**; retention 7 years in cold storage ~4.3 TB.
- Cost: extra LLM guard calls avoided by local rails; for a managed moderation API at $1 per 1,000 calls the same coverage would add **$420/day**, the reason to prefer in-process rails for PII and use API checks only on the 3% flagged as borderline.

**Component breakdown**
1. Gateway: size/encoding normalization (NFKC, strip zero-width/bidi), per-user rate limit (Module 02).
2. PII analyzer (Presidio engine + custom recognizers for account numbers/IBAN/internal IDs), anonymizer producing typed placeholders; vault in the bank's VPC.
3. Injection scorer for user input AND retrieved/uploaded content; scores above warn threshold remove write-capable tools for the turn; above block threshold refuse.
4. Prompt assembly with spotlighting, canary token in system prompt, tool schemas minimal.
5. Tool policy: `send_email` requires explicit human confirmation showing recipient and body; recipient domain allowlist; `update_ticket` limited to the user's own tickets.
6. Output rails: canary/system-prompt leak, secret scanner, PII re-scan, groundedness check on citations; one regeneration then safe refusal.
7. Observability: block rate, false-positive feedback button, weekly red-team replay set (Module 14).

**Failure modes**
- Novel injection phrasing evades patterns: assume it happens; least-privilege tools and human confirmation contain impact.
- PII in unusual format (spaces, unicode digits): normalization before scan; recall tracked on a labeled set with synthetic perturbations.
- Detokenization abuse: restore only for requests whose user has the entitlement, only for the originating request id.
- Vault outage: fail closed for requests with PII; requests without PII proceed.
- Over-blocking: per-entity thresholds, monitored block rate (alert > 3% over baseline), user-visible reason and appeal path.

# Client Simulation Challenge

**Scenario.** The client's CISO says: *"Last week a red-team intern got your assistant to ignore its instructions with a trick in a PDF. Your vendor said guardrails make this impossible. Obviously not. Why shouldn't I shut this down?"*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "Your red team did exactly what they should. And I want to correct one thing: nobody can honestly promise prompt injection is impossible to trigger, and if anyone told you guardrails make it impossible, that statement was wrong. What matters is what the attack could accomplish once it works."

*Impact.* "In this test the hidden instruction changed the assistant's summary, which is a real integrity problem. The reason I'm not alarmed about data loss is that the assistant in your deployment has no tool that can send data out. But I understand that 'it only changed a summary' is a thin comfort if employees rely on those summaries."

*Remedy.* "So we're strengthening the design in three ways, and you'll be able to verify each. Content from documents is now marked as data and scanned for hostile instructions before the model sees it, and anything suspicious is shown to the employee with a warning. The assistant's powers stay minimal: it can read, and anything that writes or sends goes through a confirmation screen showing exactly what will happen. And your red team's PDF, along with the fifty variants we'll generate from it, becomes a permanent regression test that runs on every release. I'd invite your team to keep attacking it. I'd rather find the next bypass in a test than in production, and I'll report the block rate and every bypass to you each week."
