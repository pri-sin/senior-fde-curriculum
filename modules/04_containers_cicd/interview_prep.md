# Senior FDE System Design Crucible

**Prompt:** Design the build and release system for an AI platform deployed into 30 customer-controlled Kubernetes clusters (some air-gapped). 40 engineers, ~120 deploys per week, SOC 2 and FedRAMP-moderate customers, rollback in under 5 minutes.

**Requirements**
- Every image traceable to a commit, signed, scanned, with an SBOM.
- Air-gapped customers receive an offline bundle; no direct registry access.
- Zero-downtime upgrades; automatic rollback on SLO regression.

**Sizing**
- Deploys: 120/week = ~17/day, ~1 per 85 minutes of business time. Each deploy = 3 images (api, worker, migrator). Builds: 17 x 3 = **51 image builds/day** plus PR builds (~5 per engineer per day = 200). Total ~250 builds/day x 6 min = 1,500 runner-minutes/day; with 8-way concurrency and peak at 3x average, plan **12 ephemeral runners**.
- Registry storage: 51 images/day x 150 MB (slim) = 7.6 GB/day, with layer dedup (~80% shared) ~1.5 GB/day net = **~550 GB/year**; retention policy keeps 90 days of non-release tags -> ~140 GB hot.
- Pull storm: a 30-cluster fleet x 20 nodes x 150 MB = 90 GB per release if all nodes pull from origin; use per-cluster pull-through caches, reducing origin egress to 30 x 150 MB = 4.5 GB.
- Rollback budget: image already cached on nodes (previous ReplicaSet), so rollback = new rollout of N pods at maxSurge 25%: ~90 s for 40 pods. Meets 5 min.
- Offline bundle: tarball of images + SBOMs + cosign bundle ~1.2 GB per release; signed manifest listing digests.

**Component breakdown**
1. CI with OIDC federation to the registry (no stored credentials); BuildKit with remote cache; dockerfile policy gate (this lab) and secret scan.
2. SBOM (CycloneDX) generated per image and attached as an OCI artifact; vulnerability scan with an allowlist that expires.
3. Cosign keyless signatures; Kyverno/Gatekeeper admission verifies signature and rejects tags (digest only).
4. GitOps (Argo CD): environment repo holds `image@digest`; promotion = PR; Argo Rollouts canary 5% -> 25% -> 100% with Prometheus analysis (error rate, p95).
5. Air-gap path: `fde-bundle export` -> customer's transfer process -> import into their registry -> same admission verification with a customer-held trust root.

**Failure modes**
- Scanner outage blocks all releases: retry with backoff; documented break-glass with two-person approval and a 24 h follow-up ticket.
- Compromised CI token: tokens are per-job OIDC (minutes of lifetime); registry accepts pushes only from the pipeline identity.
- Bad migration with no rollback: expand/contract schema discipline, migrator separate from app, canary reads only.
- Base image CVE disclosure: scheduled rebuilds nightly, automated digest-bump PRs, fleet-wide report from SBOM index in < 1 hour.

# Client Simulation Challenge

**Scenario.** The client's head of compliance says: *"Your platform's last release contained a critical OpenSSL vulnerability for nine days before anyone noticed. Why should we trust your supply chain?"*

**Word-for-word executive response (Situation-Impact-Remedy):**

*Situation.* "You're right, and I'll be specific. The vulnerability was disclosed on a Tuesday. Our scan runs at build time, and no build ran that week for that service, so the old image kept running with a known flaw. Our pipeline was strong at the gate and blind between gates."

*Impact.* "For nine days, your environment ran software we could have patched in an hour. We found no sign of exploitation, and I can share the log review, but the risk was real and the detection delay is what your audit team is right to challenge."

*Remedy.* "Three changes, each with evidence. First, we rebuild and rescan every base image nightly, so the gap is hours, not days. Second, every image carries an SBOM, so when a new CVE is published we can answer 'which of your clusters run the vulnerable package' within an hour from an index rather than by hand; I'll demonstrate that query on your fleet during our next call. Third, admission control in your cluster only accepts signed images by digest, so nothing unscanned or modified can run. I'll send you the pipeline policy as code, and your auditors can run the same checks against any image we ship you."

# Extension Practice

**1. Cache-friendly Dockerfile.** A 90-second `pip install` re-runs on every one-line code change. Why, and what is the fix?
*Answer:* `COPY . .` comes before `pip install`, so any file change invalidates that layer and every layer after it. Copy `requirements.txt` alone, run `pip install`, then copy the app code. A code edit then re-runs 0 install steps (about 0 s instead of 90 s); only a change to `requirements.txt` pays the cost. Add `.dockerignore` so edits to ignored files do not invalidate `COPY .`.

**2. Fargate sizing.** A service needs 1 vCPU and 3 GiB. Which task size, and would 0.5 vCPU / 3 GiB be valid?
*Answer:* 1 vCPU is 1024 CPU units, valid memory 2048 to 8192 MiB in 1 GiB steps, so 1024 / 3072 is the smallest fit. 0.5 vCPU (512 units) allows only 1024 to 4096 MiB, so 512 / 3072 is also valid and cheaper if the app is not CPU-bound; 256 units with 3 GiB is invalid (max 2048 MiB).

**3. Rollout arithmetic.** Desired count 4. What happens with `maximumPercent` 100 and `minimumHealthyPercent` 100? With 125 and 100? With 200 and 50?
*Answer:* 100/100: at most 4 tasks, all must stay healthy, so no new task can start and the deployment is stuck (the lab raises an error). 125/100: ceiling of 5 tasks, so one extra new task at a time, 4 rounds of start one then stop one (8 steps, slowest, safest capacity). 200/50: up to 8 tasks and a floor of 2 healthy, so all 4 new tasks start at once and the old ones stop; one round, fastest.

**4. Health check timing.** Target group interval 10 s, unhealthy threshold 2, healthy threshold 2, deregistration delay 30 s. How long until a dead task stops receiving traffic, how long until a new task receives traffic, and what must the ECS health check grace period cover?
*Answer:* Detection takes 10 x 2 = 20 s of failed probes. A new task needs 10 x 2 = 20 s of passing probes. The grace period must cover container start plus start period plus that 20 s, for example 10 s start period + 20 s = 30 s minimum, so 60 s is a safe setting. If every target is unhealthy the load balancer fails open and sends to all of them, so alert on healthy-host count rather than assuming errors will appear.

**5. Autoscaling.** 4 tasks at 90% CPU, target 60%, min 2, max 10. What is the desired count? Why use a short scale-out cooldown and a long scale-in cooldown?
*Answer:* ceil(4 x 90 / 60) = 6 tasks. Short scale-out reacts to spikes before users see saturation; long scale-in avoids removing capacity during a brief dip and then scaling out again (flapping). A policy where scale-out is longer than scale-in is the wrong way round.

**6. Workflow triggers and secrets.** A workflow has `on: push: branches: [main], paths: ["app/**"]`. Does a push to `main` that only edits `docs/a.md` run it? A tag push `v1.0`? Separately: a step runs `echo "token is $TOKEN"` where `TOKEN` comes from a secret. Is that safe?
*Answer:* The docs-only push does not run (no changed file matches `paths`). The tag push does not run (only a branch filter is defined). The echo prints `***` because GitHub masks registered secret values, but this is still a defect: it relies on masking, which does not catch base64, substrings or reversed copies; never print secrets. Prefer OIDC role assumption so there are no long-lived AWS secrets to leak.
