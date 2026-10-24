# Senior FDE System Design Crucible

**Prompt:** A healthcare network wants our AI assistant to read from three hospital data VPCs (each owned by a different subsidiary) and call a vendor-hosted model gateway. Design the connectivity, IAM, and isolation model. 14 hospital VPCs in year 2, HIPAA scope, PHI must never traverse the public internet.

**Requirements**
- Private-only paths; no public IPs on data stores or model endpoint.
- Subsidiaries must not see each other's data or route to each other.
- Auditable access (who called what, from which VPC endpoint).
- Added hospital onboarding in < 2 days.

**Sizing**
- Peering mesh for N=14 data VPCs + 1 AI VPC if full-mesh: 15 x 14 / 2 = **105 peerings**, each with 2 route entries to maintain: 210 routes. Hub-and-spoke with a transit gateway: **15 attachments**, one route table per trust domain. Clear win from N ~ 5.
- Traffic: 14 hospitals x 40 queries/s x 200 KB retrieval payload = 112 MB/s = **~0.9 Gbps** peak. TGW handles 50 Gbps per attachment, so capacity is not binding. Data processing fee at $0.02/GB x 112 MB/s x 86,400 x 0.3 duty = ~$58/day.
- IP plan: reserve a /16 per hospital out of 10.64.0.0/10 (64 /16 blocks available) and a /16 for the AI plane. Check against subsidiary on-prem ranges first: overlap makes peering impossible, so use PrivateLink for any collision.
- IAM: one role per hospital for the retrieval service with a permissions boundary; session duration 1 h; ~15 roles x 3 policies = 45 documents to review per quarter.

**Component breakdown**
1. Transit gateway with separate route tables: `ai-plane` can route to each hospital; hospital tables do **not** have routes to one another (isolation by absence of routes).
2. PrivateLink for the vendor model gateway: no peering, overlap-safe, one-way exposure.
3. Security groups reference SG ids, not CIDRs, so scaling pods does not require rule edits.
4. IAM: IRSA/OIDC pod identity -> `AssumeRole` with `aws:SourceVpce` conditions; explicit Deny on cross-hospital resource ARNs.
5. VPC Flow Logs + CloudTrail data events to an immutable log account; Reachability Analyzer in CI for every route change.

**Failure modes**
- Route table drift (someone adds `0.0.0.0/0` to a TGW table): detect with AWS Config rule and an automated analyzer test asserting hospital-A to hospital-B is unreachable.
- Overlapping CIDR on onboarding: pre-flight overlap check blocks the pipeline; fall back to PrivateLink.
- Role compromise: short sessions, SourceVpce condition, anomaly alert on cross-hospital ARN access attempts.
- DNS: private hosted zones must be associated with every VPC; failure shows as intermittent timeouts, so include a DNS probe in the readiness test.

# Client Simulation Challenge

**Scenario.** The client's CISO says: *"Peering the vendor's network to ours is a non-starter. I don't want another company's VPC with a route into my hospitals. Show me why this isn't a breach waiting to happen."*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "You're right about the shape of the risk. A traditional peering gives two networks a mutual route, and if anything on the vendor side were compromised, an attacker could probe whatever the route table exposes. So I'm not going to ask you to accept that design."

*Impact.* "If we got this wrong the exposure would be patient data, a reportable HIPAA incident, and loss of trust across all of your subsidiaries. That is the standard we are designing to, and it's why isolation has to be true by construction, not by promises."

*Remedy.* "Here is what we'll do instead. The vendor model endpoint is exposed to you through PrivateLink: one-way, a single network interface in your VPC, no route table entries into the vendor's network and none from it into yours. The vendor cannot initiate a connection to you at all. Your hospitals stay on separate route tables with no path between them, and I'll prove that with an automated reachability test that fails the deployment if any hospital can reach another. Credentials are short-lived and only valid from your endpoint ID, so a stolen token is useless elsewhere. I'll walk your network team through the route tables and the test output live this week, and your CISO's team can run the analyzer themselves."
