# Senior FDE System Design Crucible

**Prompt:** Design AuditMesh for a multinational insurer facing DORA, GDPR and SOC 2 audits simultaneously: 1,400 atomic requirements, 90,000 evidence artifacts (policies, IaC configs, tickets, access reviews), quarterly re-audit in under 48 hours, defensible to external auditors, and a human compliance team of 12.

**Requirements**
- Per-requirement verdict with verbatim, offset-anchored evidence; reproducible (replayable) runs.
- Tamper-evident audit trail; evidence immutable by content hash.
- <= 15% of requirements routed to humans; zero fabricated citations; false "compliant" rate < 1% (measured against expert-labeled sample).

**Sizing**
- Work units: 1,400 requirements. Retrieval top-5 evidence per requirement from 90,000 artifacts (avg 6 KB => 540 MB text, ~270k chunks at 2 KB). Hybrid index small: 270k x 768 x 4 B = **830 MB** vectors; trivial on one node (replicated x2).
- Verifier calls: per requirement 3 verifiers x ~3k tokens (requirement + 5 evidence + instructions) + 400 output = 3 x 3.4k = 10.2k tokens. Total = 1,400 x 10.2k = **14.3 M tokens per full audit**; at $5/M blended = **~$72 per full pass**. Include extractor and second-pass calls for disputed items (20%): ~**$100-150 per quarter**. LLM cost is negligible next to human review.
- Latency: serial per requirement ~6 s (retrieve 0.3 s + 3 parallel verifiers ~3 s + overhead); 1,400 x 6 s = 8,400 s serial = 2.3 h; with concurrency 16 => **~9 minutes**. Re-audit in 48 h leaves ample slack for evidence refresh and human review.
- Human load: target 15% routed = 210 requirements; at 12 minutes each = 42 reviewer-hours, i.e. **3.5 hours per reviewer** across 12 people; the critical/non-compliant routing rule (every critical requirement signs off) may push this to ~25% (350 reqs x 12 min = 70 h, ~6 h each), still feasible; tune by trusting corroborated COMPLIANT findings on low/high severity.
- Audit chain: ~12 entries per requirement x 1,400 = 16,800 entries/run x 1.5 KB = **25 MB per run**; anchor the head hash in an external timestamping/WORM store after each run; retention 7 years = ~700 MB for 28 runs.
- Evidence snapshots: 540 MB per quarter content-addressed (dedup across quarters ~85%) => ~80 MB new per quarter.
- Quality measurement: expert-labeled 300 requirements; weekly regression; track precision of NON_COMPLIANT and miss rate of COMPLIANT separately (errors are asymmetric).

**Component breakdown**
1. Regulation ingestion: extractor agent proposes atomic requirements with check types; compliance lead signs off; versioned.
2. Evidence connectors: Confluence/SharePoint, Git (IaC), Jira, IAM exports; snapshot + SHA-256 + metadata (owner, date, system).
3. Retrieval: hybrid (Module 06) with metadata filters (business unit, jurisdiction, validity dates).
4. Grounding service: verbatim-quote check with offsets; rejects ungrounded claims; invariant "no grounded evidence => never COMPLIANT".
5. Verifier panel: presence, quantitative (numeric/units parsing in code, not by LLM), skeptic, plus configuration verifiers that evaluate IaC directly (e.g. `storage_encrypted == true`); model diversity.
6. Governance engine (versioned code): routing by severity, verdict, agreement, and evidence age.
7. Review workbench: side-by-side, override with justification, creates remediation tickets, labels feed evals (Module 14).
8. Audit store: hash-chained log, WORM storage, replay tool (re-run with pinned model/prompt/requirements versions and compare).
9. Orchestration: checkpointed workflow (Module 09) with idempotent steps; gateway (Module 15) for model access; tenant isolation (Module 12).

**Failure modes**
- Hallucinated evidence: blocked by grounding; sampled audits of 5% of COMPLIANT findings against sources.
- Systematic misread of a threshold type (e.g. business days vs hours): quantitative parsing unit tests from real policy phrases; disagreement-triggered review.
- Stale evidence presented as current: evidence validity dates; requirements specify maximum evidence age; stale => INSUFFICIENT.
- Reviewer rubber-stamping: show reason for routing, require justification, measure reviewer override rate and time-per-item; blind duplicates to calibrate.
- Provider outage mid-run: checkpoint per requirement; resume; fall back to alternate provider; abstain rather than guess.
- Policy conflict (two documents disagree): surface both quotes, verdict INSUFFICIENT with "conflicting evidence" tag, route to owner.

# Client Simulation Challenge

**Scenario.** The client's external auditor lead and the Chief Compliance Officer are both on the call. The auditor says: *"I've seen AI tools that cite policies that don't exist. If your engine marks a control as compliant and I find the quote isn't in the document, that's a finding against the company. Why would I accept any of this as audit evidence?"*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "You're describing a failure mode that is real, and the design of this system starts from it. A language model on its own can invent a citation. So the model's output is never accepted as evidence in this engine. Evidence is accepted only if the exact sentence is found, character for character, at the recorded location in a document snapshot we hold by content hash."

*Impact.* "If a compliant finding rested on an invented quote, the cost to the company would be a formal audit finding and a loss of your confidence in everything else we produce. That's why the rule is absolute: no verified quote, no 'compliant' verdict. In that case the finding is marked as an evidence gap and goes to the control owner."

*Remedy.* "Here is what you can inspect. For every requirement, the report shows the quote, the document, the version hash and the offset, and your team can click through to the source snapshot. Every step of the run is recorded in an append-only log where each entry includes the hash of the one before it, and we publish the final hash to an external timestamp store, so after the fact nobody, including us, can change a finding without breaking the chain. Critical controls and any negative or contested finding are confirmed by a named human before they enter the report. And I'd welcome you sampling it: pick any twenty compliant findings, and my team will walk you from the report to the source for each one, live, and also show you the cases the system refused to mark compliant, because those are where the design earns your trust."
