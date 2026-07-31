# Project status — Reference-Guided MIDI Restoration

**Last independent review:** 2026-07-31
**Current milestone:** Phase 1 accepted. Phase 2 implemented and re-verified on its feature branch after a clean-context audit; **not merged**. Phase 3 not started.

## Completed

### Phase 0 — AILab bootstrap

Accepted and merged. AILab has the reproducible k3s/GPU/storage/ingress baseline in `infra/ailab`.

### Phase 1 — Project and Asset Storage

Accepted after Cursor implementation, Cursor acceptance fixes, and an independent Hermes review.

Implemented:

- Project CRUD and optimistic revisions;
- content-addressed SHA-256 blob storage on the `project-assets` 50 GiB PVC;
- upload tickets, streaming upload, type/size validation, deduplication;
- Guitar Pro revisions, manifests, provenance and cache-key utility;
- Flask Projects UI and streaming proxy;
- restricted Kubernetes deployment and HTTPS ingress;
- persistence and live E2E evidence in `docs/Phase_1_Project_Asset_Storage.md`.

Relevant source commits:

- `2adc75b` — Cursor acceptance fixes;
- `d41090a` — deployed image tag update;
- `c6b0156` — Cursor acceptance report update;
- `c9dbceb` — Hermes final hardening after independent review.

Independent final gates after `c9dbceb`:

- root `python -m pytest -q`: **183 passed, 1 xfailed**;
- Ruff: pass;
- `git diff --check`: pass;
- `bash -n`: pass;
- ShellCheck 0.11.0: pass;
- `kubectl kustomize infra/ailab`: pass;
- Kubeconform 0.8.0: 23 resources, 22 valid, 0 invalid/errors, 1 skipped custom schema.

Live checks before shutdown:

- Asset API health/readiness/projects: HTTP 200;
- Flask home and Projects UI: HTTP 200;
- smoke-app, Gitea and homepage: HTTP 200;
- persisted Phase 1 data remained readable;
- live image at that moment: `asset-api:2adc75b`.

## Important deployment handoff

`c9dbceb` adds source hardening newer than the last live image:

- deterministic active-upload-limit test instead of a scheduler-dependent flaky test;
- recovery of tickets left `uploading` after process termination;
- sanitized request-ID-bearing unexpected 500 responses;
- non-leaking readiness errors;
- deterministic SQL tie-break ordering;
- limiter locking, stale README command, and root Ruff cleanup.

Therefore the **first prerequisite in the next Cursor task** is to establish AILab-native Tekton CI/CD with a durable OCI registry and a Gitea-triggered, fail-closed pipeline. Tests, Linux/amd64 image builds, publication and deployment must run on AILab from the exact accepted `main` commit; the ARM64 Cursor workstation must not build, cache or transfer images. The pipeline must then build/deploy the hardened `asset-api`, pin the exact tag/digest, and rerun Phase 1 focused live smoke checks. Do not start Phase 2 implementation until this rollout succeeds. The previous `asset-api:2adc75b` deployment remains the rollback target.

### Phase 2 — Reference-Time Vertical Slice (feature branch)

**Branch:** `feat/reference-time-vertical-slice`
**Status:** implemented and independently re-verified on the branch after a
clean-context audit. **Not merged to `main`. Phase 3 not started.**

The previous branch state was audited and rejected. Every defect below was
reproduced before being fixed, and the fixes are pinned by tests rather than by
claims:

| Audit item | What was wrong | Now |
|---|---|---|
| A | the live E2E runner was collected by root pytest (13 fixture errors) and its `fail()` helper recorded a failure without raising, so a scenario could report PASS while failing | runner moved to `e2e/live_acceptance.py`, excluded by `pytest.ini`, rebuilt fail-closed; `test_e2e_runner_contract.py` pins that property in the root gate |
| B | cache identity was not scoped to `project_id`, so two projects with identical assets collided | both identities are project-scoped and version-prefixed |
| C | a caller-supplied `gp_revision_sha256` could become authoritative | the claim is checked against Asset API metadata (`gp_revision_mismatch`); only the trusted digest reaches a key |
| D | no role or type validation at all | every slot validates ownership, role, type and digest with distinct stable codes, before any job exists |
| E | downloads used `resp.content` | streamed with per-role byte and time bounds enforced while reading, and scopes cleaned on every path |
| F | a `source_gap` DP transition with `j > 0` was reconstructed as a repeat | each cell records its operation; repeats need repeat evidence |
| G | timeouts ran only at startup, progress reset `started_at`, a late worker could overwrite a terminal state and publish a result | continuous watchdog, one-time `started_at`, immutable terminal states, guarded atomic publication |
| H | "source evidence reuse" was a computed key with nothing behind it | a persisted project-scoped bundle; reuse proven by extraction counts |
| I | weak live evidence: cache-key-only anchor check, warm-cache concurrency, no reuse proof, in-process Flask client, partial restart proof | all fourteen scenarios rebuilt; the Flask UI is now genuinely deployed |
| J | deployment proved only by tag | deployed by digest, with the live pod `imageID` compared against the published digest |

Also delivered:

- `gpmidi-web`: the Flask converter and Projects UI, now an in-cluster workload,
  because scenario 12 requires the real HTTP path and the UI was not deployed
  anywhere before;
- the AILab Tekton path rebuilt around exact-SHA checkout, a full gate set with
  every tool pinned in the CI image, digest-based deployment, image audit and
  vulnerability scanning, PVC persistence verification, a post-deploy live
  acceptance stage, least-privilege separated identities, evidence archiving with
  retention, and a negative pipeline that structurally cannot deploy.

Full detail, including the pinned toolchain, scoring model and cache contract:
`docs/Phase_2_Reference_Time_Vertical_Slice.md`.

<!-- PHASE2-EVIDENCE-START -->
**Authoritative run:** PipelineRun `gpmidi-ci-manual-6svjg`, **Succeeded**, 20/20
tasks, from commit `1485ed928d1363ada70b0e5c3f9067d638475dea` with the checkout
verified against the requested SHA.

| Gate | Result |
|---|---|
| root `python -m pytest -q` | 112 passed, 27 skipped |
| `services/reference_time` | 198 passed |
| `services/asset_api` | 81 passed |
| `python -m ruff check .` | All checks passed |
| `git diff --check origin/main...HEAD` | clean |
| `bash -n` + ShellCheck 0.11.0 | clean |
| Kubeconform 0.7.0 `-strict` | 69 resources, 51 valid, 0 invalid, 0 errors, 18 skipped |
| Manifest policy | 4 workloads, 1 CronJob, 9 NetworkPolicies — pass |
| gitleaks 8.24.3 | 84 commits, no leaks |
| Dependency + license review | 72 dependencies, all permissive or declared |
| trivy 0.72.0 | no fixable CRITICAL in any image |
| Live acceptance | **14/14 scenarios, 206 recorded observations** |

Deployed by immutable digest, each verified equal to the live pod `imageID`:

- `asset-api@sha256:9e7c10a66c11e1996d9e94d2146df77e36b3b308bee65ef75351596736cd8127`
- `reference-time@sha256:9bb4347eea5a69473d64a6b8834d1bf4458e1fcd72494b8045920131b038cf2b`
- `gpmidi-web@sha256:a33b33b69491ca59998d0b1fe79d9e300dd0fae20217cb3ed665b0c3773f9002`

**Negative proof:** `gpmidi-ci-negative-h4h2l` failed at `test-gates`; downstream
gates skipped; Deployment generation, revision, digest, pod UIDs, PVC identities
and project count all unchanged.

**Rollback:** rolled `reference-time` back to
`sha256:ce2398f506b8bc8e94ab716dc7ae3ad08a732cd4db9a843a3a6c5dbacb8bdefe` and
forward again, each time proving the live pod `imageID` and that 14 projects and 12
analyses survived. PVC UIDs unchanged.

**GPU/LLM:** active profile `['llama-server']` before and after; llama.cpp
`/health` 200; homepage and Gitea 200.

Full evidence, including per-scenario observations and archived gate logs:
`docs/Phase_2_Reference_Time_Vertical_Slice.md` section 15, and the
`tekton-evidence` PVC bundle `manual-candidate-1485ed92…` with `SHA256SUMS`.
<!-- PHASE2-EVIDENCE-END -->

## Next

Phase 2 acceptance review. Phase 3 (MIDI restoration using the measure mappings) not started.

## Operational state

AILab is running. The Tekton delivery path is installed and active in namespace
`gpmidi-ml`. A push to `main` on Gitea is authenticated by shared-secret signature
and validated for repository, ref and commit shape before a PipelineRun is created;
a feature candidate is validated by `infra/ailab/scripts/run-pipeline.sh`, which
labels the run `manual-candidate` so evidence never misrepresents it as a main
push.

Installed versions (verified on the cluster, and pinned in
`infra/ailab/versions.env`):

| Component | Version |
|---|---|
| k3s | v1.36.2+k3s1 |
| Tekton Pipelines | v1.14.1 |
| Tekton Triggers | v0.36.0 |
| kaniko | v1.23.2 |
| Gitea (registry + git) | 1.25.5 |
| kubectl / kubeconform / ShellCheck | v1.32.4 / v0.7.0 / v0.11.0 |
| gitleaks / trivy / crane / ruff | 8.24.3 / 0.72.0 / v0.20.3 / 0.15.6 |

Registry: Gitea's OCI registry at `192.168.30.2:3300`, package data under
`/home/mgrigorov/gitea/data` on the root NVMe.

Tekton co-scheduling is disabled (`infra/ailab/scripts/configure-tekton.sh`): the
default affinity assistant refuses a TaskRun that binds both the shared source
workspace and the durable evidence workspace, and on a single-node cluster
co-scheduling buys nothing.

Namespace limits are real constraints, not decoration: `LimitRange` caps a pod at
10 CPU and `ResourceQuota` caps `limits.cpu` at 12 for the whole namespace, of
which the application pods hold about 5.7. Gate groups and per-image build/audit
pairs are therefore chained rather than run in parallel.
