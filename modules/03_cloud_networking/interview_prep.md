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

# Extension Practice

**1. Lifecycle arithmetic.** Rule on `logs/`: `STANDARD_IA` at day 30, `GLACIER` at day 90, expire at day 365. Where are objects aged 10, 45, 120 and 400 days? Would expiring at day 100 instead be a good idea?
*Answer:* 10 days stays `STANDARD`, 45 is `STANDARD_IA`, 120 is `GLACIER`, 400 is expired. Expiring at 100 leaves only 10 days in Glacier against a 90-day billed minimum, so you pay for 80 days you did not use (early-deletion charge). Expire at 180 or later, or skip the Glacier step.

**2. Subnet sizing and NAT count.** A VPC spans 3 AZs with public, app and data tiers. How many NAT gateways for AZ-resilient egress, and how many usable IPs in a `/24` and a `/28`?
*Answer:* 3 NAT gateways, one per AZ, each in that AZ's public subnet, with each app subnet routing to its own AZ's NAT. A `/24` has 256 - 5 = 251 usable addresses; a `/28` has 16 - 5 = 11. The data tier has no default route, so it is isolated.

**3. Why can't the private instance reach the internet?** Its route table has `0.0.0.0/0` to `nat-a`, but `nat-a` was created in `app-a`. Diagnose it.
*Answer:* A NAT gateway must sit in a public subnet (one whose route table sends `0.0.0.0/0` to an internet gateway). In a private subnet the translated packets have nowhere to go. Move the NAT to `public-a`, point the private route table at it, and confirm with a path check (`egress_path` in the lab).

**4. Budget alerts.** Limit $1,000. Spend is $323 on day 4, $565 on day 7, $808 on day 10, $1,050 on day 13 of a 31-day month. Which alerts fire, and when?
*Answer:* Day 4: forecast is 323 / 4 x 31 = $2,503, above 100% of the limit, so the FORECASTED 100% alert fires first. Day 7: actual passes 50%. Day 10: actual passes 80%. Day 13: actual passes 100%. Each alert fires once. The forecast alert is the early warning; the actual alerts are confirmation.

**5. Tighten a role.** A role is granted `s3:*`, `sqs:*` and `iam:PassRole` on `*`, but CloudTrail shows 3 distinct calls in 90 days (`s3:GetObject`, `s3:ListBucket`, `sqs:SendMessage`). How much of the grant is unused, and what must you check before swapping the policy?
*Answer:* The catalog expansion grants 13 actions and 3 are used: 10 unused, a 77% reduction. Check that every observed call still succeeds under the new policy (regression), that nothing new opens up (escalation), that rare jobs (quarterly, disaster recovery) are not missing from a 90-day window, and keep the explicit Deny guardrails. Remove `iam:PassRole` on `*` regardless of usage.

**6. Cross-account failure triage.** Account B's role calls `sts:AssumeRole` on account A's role and gets AccessDenied. Name the four things to check, in order.
*Answer:* (1) A's trust policy names B's account or role as principal. (2) Trust conditions: external ID, MFA, source IP, organization ID all satisfied. (3) B's caller identity policy allows `sts:AssumeRole` on A's role ARN (cross-account needs both sides). (4) Session limits: requested duration is within the role's maximum, and at most 3,600 seconds when role-chained.
