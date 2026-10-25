# Senior FDE System Design Crucible

**Prompt:** Design tenant isolation for a multi-tenant document-QA SaaS with 4,000 enterprise tenants (sizes from 1k to 40M chunks), SSO via customer IdPs (Okta, Entra, PingFederate), document-level ACLs synchronized from SharePoint, 300 QPS peak search, and a contractual guarantee of zero cross-tenant exposure. Two tenants require dedicated infrastructure and customer-managed keys.

**Requirements**
- Authenticate against many IdPs; map each to a tenant; short-lived tokens, SCIM deprovisioning within 5 minutes.
- Document-level ACL enforcement at retrieval; p95 search < 250 ms.
- Provable isolation: tests, canaries, audit trails; tenant offboarding with verifiable deletion.

**Sizing**
- Data: long-tail tenant sizes: 3,900 small/medium tenants x avg 400k chunks = **1.56 B chunks**, plus 100 large tenants x 20 M = **2.0 B**; total **~3.6 B chunks**. Vectors 768 dim int8 = 768 B: 3.6 B x 768 B = **2.8 TB** (+30% graph/overhead = 3.6 TB). Sharded over 24 nodes at 192 GB usable RAM-equivalent with disk-backed HNSW (~150 GB RAM-resident hot set per node).
- Isolation strategy by tier: small/medium tenants (3,900) share collections partitioned by hash(tenant_id) into 64 shards with payload index on `tenant_id`; the 100 large tenants get dedicated collections; the 2 regulated tenants get dedicated clusters + CMK.
- Search load: 300 QPS peak x top-50 candidates; filtered ANN on selective filters (tenant = 0.03% of index) needs a payload-indexed pre-filter; verify recall@10 stays >= 0.95 against brute-force on a nightly sample.
- ACL sync: SharePoint change feed ~2 M permission changes/day = 23/s average; store ACLs as `acl_groups` arrays on chunks (denormalized) with group-membership expansion at query time from a cache (5 min TTL): token groups claim (<=200 groups) intersect `acl_groups`. If group count > 200 use the "groups overage" lookup via Graph API cached 5 min.
- Token validation: 300 QPS x ~0.1 ms (cached JWKS, local verify) = negligible; JWKS refresh on unknown `kid` rate-limited to 1 per 30 s per issuer.
- Audit: 300 QPS x 86,400 x 0.4 duty x 1.5 KB = **~15.5 GB/day** to SIEM/object storage.
- Offboarding: deleting 20M chunks from a shared collection: delete-by-filter ~15 min plus compaction; with per-tenant CMK, destroying the key gives crypto-shredding immediately for the dedicated tenants.

**Component breakdown**
1. Identity: per-tenant IdP config (issuer, audience, JWKS URL) in a registry; token validation selects config by `iss` after allowlist lookup; `tid` is derived from registry mapping (not a trusted claim alone).
2. Gateway: builds `RequestContext`; the retrieval SDK only accepts a context, so unfiltered queries are unrepresentable.
3. Retrieval: pre-filter on `tenant_id` and `acl_groups`; hybrid index (Module 06) with the same filter in BM25.
4. Postgres metadata with row-level security keyed on a session variable; DB role has no BYPASSRLS.
5. Canary service: continuously issues queries as synthetic tenants with seeded "poison" documents and asserts they never appear elsewhere.
6. Key management: per-tenant data keys wrapped by KMS CMKs; rotation and revocation runbooks.

**Failure modes**
- Mis-mapped tenant in IdP registry: registry changes need two-person review and a synthetic login test.
- Filter dropped by a refactor: contract test enumerates all retrieval call sites; CI fails if any call lacks a context; runtime assert on every response.
- Stale ACLs after a user is removed: SCIM + short token life + cache TTL 5 min bound exposure, documented; critical removals trigger cache bust.
- Noisy neighbor: per-tenant rate limits (Module 02) and shard-level concurrency caps.
- Embedding inversion risk: vectors treated as sensitive as the text; same encryption and access controls.

# Client Simulation Challenge

**Scenario.** The client's CISO says: *"Your platform stores all customers in one vector index. A single bug and my contracts show up in a competitor's answers. Convince me this is safe, or I walk."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "You've described the right fear, and I won't tell you a shared index is risk-free. One shared index with one filter is a single point of failure, and a bug in that filter is the scenario you're worried about."

*Impact.* "If your contracts reached a competitor, the harm is a confidentiality breach, possible legal exposure, and the loss of your customers' trust. So the question is whether isolation depends on one filter being perfect, and it shouldn't."

*Remedy.* "It doesn't, because there are four independent layers. First, the tenant identity comes only from your signed login token, never from the request, so a user can't ask for another tenant. Second, our search library cannot be called without that identity, so an unfiltered query can't be written by mistake. Third, every response is checked on the way out, and a result from any other tenant stops the request and pages our on-call. Fourth, a canary system runs all day, planting marker documents in test tenants and alerting if one appears anywhere else; I'll show you its log. And because you're a regulated customer, you also have an option I'd recommend: a dedicated index in its own account, encrypted with your own key, so destroying that key makes the data unreadable immediately. You can have your red team attempt cross-tenant access in a staging copy before you sign."


# Extension Practice

**1. Post-filter versus pre-filter arithmetic.** One tenant owns 0.03% of a shared index. You need 10 visible results. How many candidates must a post-filtering retriever fetch, and what is the fix? *Answer:* 10 / 0.0003 = **~33,000 candidates** on average, which is slow and still unreliable (some queries return fewer than 10). Fix: push the tenant and ACL filter into the index query (payload index on `tenant_id`) so ranking only ever sees authorized chunks. The lab shows a user for whom post-filtering returns 0 results while pre-filtering returns the 2 they may read.

**2. Device flow load.** 10,000 TVs are waiting on users with the default 5 s polling interval. What is the worst-case token endpoint load and how do you protect it? *Answer:* 10,000 / 5 = **2,000 requests/s** if every device is pending. Return `slow_down` (add 5 s) to clients that poll early, cap device code lifetime (600 s means at most 120 polls per device), and rate limit per `device_code`.

**3. Groups overage cost.** 4,000 users, 5% of them over the group limit, Graph lookup takes 3 calls (100 groups per page, ~250 groups), cached 5 minutes. Average Graph load? *Answer:* 200 users x 3 calls / 300 s = **2 calls/s** average if each is active continuously; far lower in practice. Without the cache it scales with request rate, so cache by user object id with a short TTL and a bounded retry that honors `Retry-After`.

**4. SAML replay cache sizing.** 1 login per second, assertions valid for 5 minutes plus 60 s skew. How big is the replay cache? *Answer:* keep ids for the validity window (use 1 hour to be safe): 3,600 x ~64 B = **~230 KB**. Reject an id seen twice. This does not replace signature verification; say plainly that unsigned or unverified assertions are untrusted.

**5. Why not just decode the JWT?** A teammate reads `roles` and `tid` from the payload "because it is already decoded by the gateway". What is wrong? *Answer:* decoding is public and free for an attacker; any claim can be edited and still decode. Verify the signature (pinned algorithm, key by `kid`), `iss`, `aud`, `exp`/`nbf` first, then build the request context from the verified claims only, and derive the tenant from that context, never from the request body.
