# Cursor task: Phase 2 acceptance fixes — CI/CD and real vertical slice

> Continue on `feat/reference-time-vertical-slice`. Do not merge to `main` and do not start Phase 3. The reported tip `a743f09373095e0f29ed0dff50e1294f7164ed61` is **not accepted**. Fix every blocker below, use AILab-native Tekton CI/CD as the authoritative delivery path, then provide independently reproducible evidence.

## 1. Mandatory context

Read fully before changing anything:

- `AGENTS.md`;
- `docs/PROJECT_STATUS.md` from accepted `main`;
- `docs/cursor-phase-2-reference-time-vertical-slice-task.md`;
- `docs/Phase_2_Reference_Time_Vertical_Slice.md`;
- `docs/Phase_1_Project_Asset_Storage.md`;
- `infra/ailab/README.md` and `/home/mgrigorov/AGENTS.md` on AILab.

Fetch current `main`, verify it contains the present task, then rebase or merge it into the feature branch without rewriting already-pushed history. Preserve all existing PVC data and unrelated AILab services. Do not merge the feature branch.

## 2. Independent Hermes evidence at `a743f09`

Hermes fetched the remote branch and reproduced:

```text
origin/main    49ee7ef0b4ba43c2062c3221eee866a99e0b4094
origin/feature a743f09373095e0f29ed0dff50e1294f7164ed61
merge-base     49ee7ef0b4ba43c2062c3221eee866a99e0b4094

python -m pytest -q
239 passed, 1 xfailed

cd services/reference_time && python -m pytest tests/ -q
54 passed

python -m ruff check services/reference_time services/asset_api
22 errors

git diff --check origin/main...HEAD
3 trailing-whitespace errors
```

Live endpoints returned 200, and the persisted Phase 2 analysis/report exists. That proves the service is reachable, not that the required vertical slice or delivery architecture is complete.

## 3. P0 — establish the required AILab-native CI/CD

The statement “AILab does not have Tekton installed” is not an acceptable deviation: installing and proving it was the explicit prerequisite. The manual `docker build` / `docker save` / `k3s ctr images import` path must not remain the accepted delivery path.

Implement and version:

1. pinned Tekton Pipelines and Tekton Triggers on AILab/k3s;
2. a durable OCI registry reachable by Tekton and k3s/containerd — prefer proven Gitea OCI support, otherwise deploy a small TLS/authenticated registry with persistent storage on `/data`;
3. secret-authenticated Gitea webhook handling for accepted `main` pushes, with repository/ref/SHA validation and trigger-loop prevention;
4. a fail-closed Pipeline that checks out the exact SHA, runs root tests/lint/render/schema/security gates, builds Linux `amd64` images on AILab without mounting the host Docker socket, pushes immutable tags, captures digests/provenance, deploys only after green gates, waits for rollout, and runs focused smoke/E2E;
5. least-privilege build/deploy ServiceAccounts, separate credentials, bounded workspaces/resources/timeouts, log/result retention and image/workspace pruning suitable for finite `/data` capacity;
6. a negative PipelineRun proving a deliberately failing gate cannot deploy or mutate the last-known-good Deployment;
7. rollback instructions and a tested rollback that does not delete Project or analysis PVC data.

Cursor may edit and push code and observe PipelineRuns. It must not build/cache/transfer images on its ARM64 workstation. The final image build and deployment evidence must come from AILab Tekton.

Delete or explicitly deprecate the manual `build-import-reference-time.sh` workflow so documentation does not present it as the normal path.

## 4. P0 — make the vertical slice real, not a set of disconnected modules

### 4.1 Audio evidence is currently dead code

`audio_link_ids` affect only the cache key. `_run_analysis` never downloads audio and never calls `extract_audio_evidence`; the live report has no audio evidence.

Fix the full path:

- validate expected asset roles/types;
- stream authorized audio from Asset API with hard byte/time limits — do not first materialize the entire asset with `resp.content`;
- extract bounded metadata/onset/phase evidence;
- integrate it into source measures/alignment confidence and warnings;
- continue with explicit reduced-confidence warnings when audio is absent or decode fails;
- serialize exact audio identities and evidence in canonical JSON/HTML;
- clean temporary files on success, failure, timeout and restart.

Add API-level and live tests proving audio changes evidence and cache identity, while missing/bad audio degrades safely.

### 4.2 Structure/anchor input is currently ignored

`structure_link_id` affects only validation/cache identity. The structure fixture is unused and the request never produces `AnchorConstraint` objects.

Implement:

- strict versioned structure JSON parsing;
- timestamp/measure anchor conversion;
- hard monotonic constraints;
- stable errors for malformed, out-of-range, duplicate and conflicting anchors;
- marker/section evidence in result/report;
- API and live tests showing an anchor changes the selected mapping and conflicting anchors fail.

### 4.3 Mapping scoring is presently non-functional

At `a743f09`:

- `_compute_cell_score` uses only time signature and any GP marker;
- `_duration_score` is dead and internally tautological (`for m in []`, then compares `gp_frac` with itself);
- density, audio, repeats and alternate endings are not scored;
- every non-marker 4/4 one-to-one match receives the same `0.7` confidence regardless of timing evidence;
- consensus conflict does not lower regional/global mapping confidence;
- `alternatives` are always empty;
- GP gaps are only a global warning, not explicit `MeasureMapping` records;
- normalized mapping fields remain default `0.0 -> 1.0` for every row.

Replace this with a deterministic, tested alignment that genuinely uses the evidence required by the original task. Requirements:

- explicit source and GP gaps;
- monotonicity with explicitly represented repeats;
- calibrated confidence derived from actual score components;
- conflict regions reduce confidence and emit stable warning/reason codes;
- alternatives for near-ties/ambiguity;
- normalized intra-measure mapping values with tests;
- hard user anchors;
- deterministic tie-breaking;
- tests that fail if duration/audio/density/repeat/marker inputs are changed but output scoring remains unchanged.

Do not merely remove dead parameters to make Ruff green; implement the promised semantics or document and obtain explicit scope reduction before proceeding.

### 4.4 Consensus comparison is too shallow

The current implementation requires equal raw tempo-event counts and compares events pairwise. Implement the specified PPQ/time-normalized trajectory, boundary, duration, time-signature and pre-roll/downbeat comparison with deterministic tie-breaks and explicit conflict regions. A conflict must not silently feed the selected primary into a normal-confidence mapping.

## 5. P0 — durable and race-safe job/cache lifecycle

A live persisted job is stuck forever in `queued`:

```text
job_id: 74de9d2c-edf7-40a6-aa2f-0a81e85de898
status: queued
created_at: 2026-07-31T09:04:58.728518+00:00
```

`recover_interrupted_jobs()` only handles `running`. Also, `JOB_TIMEOUT_SECONDS` is imported but unused, and one SQLite connection is shared across worker/request threads with `check_same_thread=False` but without transaction/connection synchronization.

Fix and test:

- startup recovery for both orphaned `queued` and `running` jobs using a documented retry-or-terminal policy;
- real timeout enforcement and cleanup;
- race-safe queue admission (count + insert must be atomic);
- idempotent concurrent requests for the same cache key: no duplicate jobs/results and no `INSERT OR REPLACE` identity corruption;
- safe SQLite access via per-thread connections or explicit serialized transactions/locking;
- bounded worker shutdown behavior;
- deterministic ordering with tie-breakers;
- no failed/incomplete/interrupted job returned as a cache hit.

Clean up or terminally recover the existing stuck live job without deleting successful results.

## 6. P0 — cache identity and authorization correctness

The request supplies `gp_revision_sha256`, and the cache key is computed from that untrusted value before the actual asset SHA is used. A caller can create duplicate or incorrect cache identities by lying about the GP SHA.

Fix:

- derive all cache identities from Asset API metadata after project ownership and role/type validation;
- resolve and validate the selected GP revision contract rather than accepting a bare caller SHA as authority;
- include exact code/algorithm/dependency identity. `__version__` remained `0.1.0` across behavior-changing fixes, so old results can otherwise remain falsely valid;
- persist and actually reuse separate source MIDI/audio evidence when only the GP revision changes;
- add API tests for wrong claimed SHA, wrong project, wrong role/type, changed MIDI, changed audio, changed structure, changed processor version and concurrent duplicate requests.

## 7. P0 — implement the required Flask Project UI

No Flask template/static/application files changed in the feature diff. Implement the additive Project-page integration required by the original task:

- select GP revision and eligible source MIDI/audio/structure assets;
- start analysis;
- display queued/running/succeeded/failed/interrupted progress;
- list prior analyses and exact input hashes;
- display confidence/warnings;
- link or embed JSON/HTML reports;
- preserve Phase 1 upload and existing converter behavior.

Exercise the real browser/HTTP UI-to-live-backend path. Mock-only tests are insufficient. Verify no permanent source asset copy is left on the Flask host.

## 8. P0 — make deployment declarative and reproducible

The branch manifests do not match the reported/live images:

```text
manifest asset-api:      49ee7ef0b4ba
reported/live asset-api: a87e8fc914ef
manifest reference-time: 9bcfb71dbca7
reported/live ref-time:  86f46b5a5f18
```

The live state was apparently changed imperatively. Fix manifests/GitOps or the declared Tekton deployment contract so a clean reconciliation deploys the exact reviewed digests. Do not claim a source commit is live unless pod image ID/digest and provenance prove it.

Preserve rollback targets, but verify every referenced rollback image actually exists in the durable registry.

## 9. Required tests and gates

Add tests before fixes. At minimum include:

- FastAPI TestClient/ASGI integration tests for create -> run -> result/report;
- role/project/SHA authorization tests;
- API error shape/request-ID tests;
- audio success/missing/decode-failure/bounds tests;
- structure anchor success/conflict tests;
- alignment sensitivity and alternatives tests;
- consensus regional-conflict tests;
- atomic queue/cache/concurrency tests;
- restart recovery for queued and running jobs;
- timeout and temporary-file cleanup;
- new GP revision reuses persisted source evidence but recomputes mapping;
- Flask live integration tests;
- manifest-to-image/provenance tests where practical.

The current file named `test_integration.py` tests only the database/cache helper layer; do not count it as API integration coverage.

Run in the authoritative Tekton clean checkout and record exact output:

```bash
python -m pytest -q
python -m ruff check .
git diff --check origin/main...HEAD
bash -n infra/ailab/scripts/*.sh
shellcheck infra/ailab/scripts/*.sh
kubectl kustomize infra/ailab > rendered.yaml
kubeconform -strict -summary rendered.yaml
```

Also run secret scanning, dependency/license review, image vulnerability scanning, image content inspection, NetworkPolicy verification, PVC persistence/restart tests, and proof that the active GPU/LLM profile remains unchanged.

No gate may be omitted merely because the tool is not installed on Cursor; provision it in the AILab Tekton build image/task.

## 10. Required live E2E

The existing E2E is not acceptance evidence for mapping quality: it claimed an 8-measure GP fixture, but the live report contains only **1 GP measure**, 8 source measures, 7 `source_gap` rows and one generic 4/4 match. It did not exercise audio or structure evidence.

Create tiny, generated, redistributable fixtures and prove live:

1. GP really parses to the intended multi-measure grid with notes, marker and repeat metadata;
2. two agreeing MIDIs yield deterministic consensus;
3. a conflicting MIDI case emits explicit regional conflict and lower confidence;
4. audio clicks/onsets measurably corroborate or contradict MIDI phase;
5. a valid structure anchor changes/locks mapping;
6. conflicting anchors fail with stable error code;
7. source and GP gap cases create explicit mappings;
8. cache hit is idempotent under concurrent duplicate requests;
9. GP-only revision change reuses source evidence and recomputes mapping;
10. pod restart preserves results and correctly recovers queued/running jobs;
11. exact JSON/HTML hashes are deterministic where specified;
12. the real Flask Project page completes the workflow;
13. no MIDI/GP is modified and no source asset is copied permanently to the Flask host;
14. existing Asset API, Project data, converter, homepage, Gitea and active LLM remain healthy.

## 11. Documentation and final response

Correct `docs/Phase_2_Reference_Time_Vertical_Slice.md` and `docs/PROJECT_STATUS.md`; do not retain “complete” claims for unimplemented requirements. Remove trailing whitespace and make all reported commands/counts factual.

Final response must include:

- feature branch and ordered commits;
- exact source SHA;
- Tekton/registry/webhook versions and declarative paths;
- successful and deliberately failing PipelineRun/TaskRun names/statuses;
- root and focused test/gate counts;
- immutable registry tags and digests;
- exact Deployment pod image IDs matching manifests/provenance;
- live E2E IDs/URLs and concise evidence for audio, structure, conflict, gaps, cache, restart and UI;
- rollback proof;
- GPU/LLM before/after;
- remaining boundaries only if they do not violate Phase 2 acceptance requirements.

Do not merge to `main` and do not begin Phase 3. Stop and report a concrete blocker rather than silently dropping a required prerequisite.
