# Cursor prompt: Phase 2 clean-context audit, repair, and AILab CI/CD

Copy everything below this line into a new Cursor context.

---

You are taking over Phase 2 of the gpmidi project in a completely fresh context.

Your task is to independently audit, repair, test, deploy, and produce reproducible acceptance evidence for the Phase 2 reference-time vertical slice.

Do not trust previous completion claims, test counts, generated evidence files, deployed image labels, or documentation. Treat all existing work as an untrusted candidate implementation that must be independently verified.

Do not merge anything into main. Do not start Phase 3.

## 1. Hard environment and execution constraints

Your local Cursor workstation is ARM64 and has very little free disk space.

Therefore:

- NEVER build container images on Cursor.
- NEVER pull/cache/export/import application images on Cursor.
- NEVER use local Docker Desktop, Podman, BuildKit, buildx, `docker save`, or image tarballs.
- NEVER transfer container images from Cursor to AILab.
- Do not run large local dependency caches if the same gate can run in Tekton.
- Local Cursor work is limited to Git operations, source/manifests editing, lightweight targeted tests where practical, documentation, and inspecting remote PipelineRuns and HTTP endpoints.
- ALL Linux amd64 image builds, image scanning, publication, deployment, rollout checks, and authoritative clean-checkout gates must run on AILab through Tekton.
- AILab/k3s is the CI/CD and image-build authority.
- Use a new clean SSH session when performing AILab-only operations.
- Before changing AILab, read `/home/mgrigorov/AGENTS.md`.
- Preserve all existing PVC data and unrelated AILab services.
- Preserve the active GPU/LLM deployment and record its before/after state.
- Do not mount the host Docker socket into Tekton build pods.
- Do not replace Tekton with ad-hoc SSH build scripts.
- Do not claim success based only on manifests, source tags, or HTTP 200 responses.

If a required prerequisite is genuinely impossible, stop and report the exact blocker with real command output. Do not silently weaken acceptance criteria.

## 2. Repository and branch

Repository: `gpmidi`

Remote feature branch:

    feat/reference-time-vertical-slice

The last independently fetched remote feature tip was:

    42782de5c9efd9bb69aa32082b0ea0b91ba46464

This SHA is context only. Fetch the remote and determine the current real tip yourself.

Begin with a clean checkout. Do not reuse an old dirty worktree.

Required initial procedure:

1. Locate the repository.
2. Run:
   - `git status --short --branch`
   - `git remote -v`
   - `git fetch --all --prune`
   - `git rev-parse origin/main`
   - `git rev-parse origin/feat/reference-time-vertical-slice`
   - `git merge-base origin/main origin/feat/reference-time-vertical-slice`
3. Create a clean local branch/worktree from the current remote feature tip.
4. Do not rewrite already-pushed history.
5. Do not force-push.
6. Incorporate current `origin/main` into the feature branch with a normal merge if it is not already incorporated.
7. Keep commits small, ordered, and reviewable.
8. Push only to `feat/reference-time-vertical-slice`.
9. Never merge the feature branch into `main`.

Do not copy any uncommitted Hermes work. Reimplement fixes cleanly from verified tests and requirements.

## 3. Mandatory documents

Read all of these fully before editing:

- `AGENTS.md`
- `docs/PROJECT_STATUS.md` from accepted `main`
- `docs/cursor-phase-2-reference-time-vertical-slice-task.md`
- `docs/Phase_2_Reference_Time_Vertical_Slice.md`
- `docs/Phase_1_Project_Asset_Storage.md`
- `docs/cursor-phase-2-acceptance-fixes.md`
- `infra/ailab/README.md`
- `/home/mgrigorov/AGENTS.md` on AILab

The requirements in `docs/cursor-phase-2-acceptance-fixes.md` remain binding. Do not mark Phase 2 complete by deleting or weakening those requirements.

## 4. Known independent audit results

The following defects were independently observed at or around feature tip `42782de5...`. Reproduce them before fixing where practical. Do not assume this list is exhaustive.

### A. Current tip did not reproduce the claimed root gate

A clean Python 3.11 review environment produced approximately:

    283 passed, 2 failed, 14 errors

The exact current count may change; report what you really obtain.

Known reasons:

- `tests/test_e2e_live.py` is accidentally collected by ordinary root pytest.
- It defines tests requiring fixtures such as `project_id`, `links`, and analysis IDs that do not exist.
- Its `fail()` helper records a failure but does not raise an assertion when invoked by pytest.
- Therefore pytest can report a scenario as passed even when the scenario internally recorded FAIL.
- Two root tests can fail because `pkg_resources/setuptools` is not available in the reproducible CI environment.
- Existing evidence claiming `258 passed, 27 skipped` may refer to a different commit or environment and must not be reused.

### B. Cache authorization/isolation is unsafe

- The full cache identity was not scoped to `project_id`.
- Two projects with byte-identical assets could collide.
- Project B could receive an `analysis_id` belonging to project A and then fail authorization on retrieval.
- Cache identity must be based on trusted Asset API metadata and authorization scope.

### C. Caller-supplied GP SHA handling is incomplete

- The request supplies `gp_revision_sha256`.
- The service must resolve the actual GP asset/revision through Asset API metadata.
- A mismatching claimed SHA must fail with a stable error such as `gp_revision_mismatch`.
- The untrusted caller value must never become authoritative cache identity.

### D. Asset roles/types are not sufficiently validated

Validate every requested input against project ownership and expected role/type:

- GP input: `guitar-pro`
- source MIDI inputs: eligible `suno-midi.*` roles
- audio inputs: `mix` or eligible `stem.*` audio roles
- structure input: `structure`

Wrong-project, missing, wrong-role, wrong-type, and missing-SHA inputs must fail closed with stable request-ID-bearing API errors.

### E. Downloads are not safely streamed

- Existing code used `resp.content`.
- Stream Asset API downloads incrementally.
- Enforce hard byte and network time limits while reading.
- Use separate configured limits for GP, MIDI, audio, and structure assets.
- Do not first materialize an unbounded response.
- Clean temporary files on success, decode failure, timeout, cancellation, and process restart.

### F. Alignment backtrace can misclassify source gaps

- A DP transition representing `source_gap` can retain `j > 0`.
- Reconstructing only from `(i, j)` can misinterpret that transition as another match against `gp[j - 1]`.
- With hard anchors, a source gap was observed to become `repeat`.
- Backtrace must preserve the transition operation explicitly.
- A repeated mapping is allowed only when repeat evidence/semantics justify it.
- Add a regression test with a hard anchor and more source measures than GP measures.

### G. Timeout handling is not real

- Timeout enforcement was effectively called only during startup.
- Progress updates could reset `started_at`, extending the deadline.
- A late worker could overwrite `failed/timeout` with `succeeded`.
- A late worker could store a successful cache result after the job had timed out.
- Terminal job states must be immutable.
- `started_at` must be set once.
- Timeout enforcement must run continuously while the service is alive.
- Result insertion must atomically verify that the owning job is still active/running.
- A timeout must prevent later result/cache publication.
- Temporary resources must be cleaned.
- Add tests that reproduce late-worker completion and result publication races.

### H. Source evidence reuse is mostly a claim, not a proven implementation

- Computing a `source_evidence_cache_key` alone does not prove reuse.
- Persist parsed source MIDI/audio evidence separately from full mappings.
- Key it by exact trusted source identities plus processor/dependency identity and project authorization scope where required.
- On GP-only revision changes, load and reuse persisted source evidence but recompute GP extraction/mapping.
- Expose deterministic provenance showing whether source evidence was reused.
- Test actual extractor call counts or persisted evidence reads, not just equality of hash functions.

### I. Existing live E2E evidence is insufficient

Known weak scenarios:

- Anchor scenario compared only cache keys rather than mappings.
- Concurrent cache scenario used an already warm cache, so it did not prove atomic concurrent admission.
- GP revision scenario did not prove source evidence reuse.
- Flask scenario used an in-process `test_client`, not the deployed browser/HTTP path.
- Restart scenario did not fully prove queued and running recovery.
- Source-gap assertions were too weak.
- Some health checks depended on SSH from a location where SSH was unavailable.

Do not reuse `tests/e2e_evidence.json` as acceptance proof.

### J. Image/deployment proof must be digest-based

- Building an immutable tag is not enough.
- Capture the pushed OCI digest.
- Deploy the exact digest, or produce an equivalent fail-closed immutable deployment contract.
- Verify live pod `imageID` matches the published digest and recorded provenance.
- A source commit label without digest verification is not proof that the commit is live.

## 5. Required implementation method

Use strict test-driven development for application defects.

For every behavior change:

1. Write a focused failing test.
2. Run that exact test and record the expected failure.
3. Implement the minimal correct fix.
4. Run the focused test until green.
5. Run the related suite.
6. Refactor only while tests stay green.
7. Commit the coherent change.

Do not “fix” gates by:

- removing meaningful assertions;
- converting failures to skips;
- excluding required tests without creating the correct replacement execution stage;
- accepting generic success merely because an endpoint returns 200;
- mocking the behavior acceptance explicitly requires to be live;
- editing evidence JSON manually;
- hard-coding pass results;
- deleting requirements from documentation.

## 6. Application workstream

### 6.1 Asset and request validation

Implement and test:

- project ownership validation;
- exact eligible roles/types;
- duplicate link handling;
- trusted SHA retrieval;
- mismatch rejection for the claimed GP revision;
- stable structured API errors;
- request IDs in error responses;
- no cache/job creation after validation failure.

### 6.2 Bounded download and cleanup

Implement and test:

- streaming HTTP reads;
- per-role byte limits;
- connect/read/write/pool timeouts;
- oversized response rejection during streaming;
- decode-failure cleanup;
- timeout cleanup;
- no permanent source copy on Flask host;
- no modification of source assets.

### 6.3 Structure and anchor contract

Implement strict versioned parsing and stable errors for:

- malformed JSON;
- unsupported version;
- non-object roots;
- non-integer indices;
- missing fields;
- out-of-range source indices;
- out-of-range GP indices;
- duplicate anchors;
- conflicting anchors;
- non-monotonic anchors;
- section-derived anchors;
- timestamp-to-measure conversion if required by the Phase 2 specification.

Use explicit error classes/codes rather than mapping every failure to one generic message.

Prove:

- a valid hard anchor changes or locks the selected mapping;
- a conflict fails deterministically;
- section and marker evidence appears in canonical JSON/HTML output.

### 6.4 Alignment correctness

Audit the complete alignment implementation, not only the known backtrace issue.

Required behavior:

- explicit `source_gap` records;
- explicit `gp_gap` records;
- explicit repeat records only when justified;
- deterministic monotonic alignment;
- hard anchors;
- duration-sensitive scoring;
- density-sensitive scoring;
- audio/onset-sensitive scoring;
- marker-sensitive scoring;
- repeat/alternate-ending-sensitive scoring;
- calibrated confidence from real score components;
- conflict regions lower mapping/global confidence;
- stable warnings/reason codes;
- alternatives for near ties;
- normalized intra-measure mapping values;
- deterministic tie-breaking.

Add mutation/sensitivity tests that would fail if duration, density, audio, repeat, marker, or conflict inputs stopped influencing output.

Do not accept assertions such as `gap_count >= 0`.

### 6.5 Consensus

Implement the specified normalized comparison:

- PPQ/time-normalized tempo trajectory;
- measure/downbeat boundaries;
- durations;
- time signatures;
- pre-roll/downbeat offset;
- deterministic primary selection;
- explicit regional conflict output;
- lowered confidence where evidence conflicts.

Do not require equal raw tempo-event counts.

### 6.6 Job/cache lifecycle

Implement and test:

- startup recovery for both queued and running orphaned jobs;
- documented retry-or-terminal policy;
- continuous timeout watchdog;
- one-time `started_at`;
- terminal-state immutability;
- atomic queue count plus insert;
- atomic idempotent admission by cache identity;
- no duplicate active jobs/results under concurrent cold-cache requests;
- no `INSERT OR REPLACE` identity corruption;
- per-operation/per-thread SQLite connections or properly serialized transactions;
- stable deterministic list ordering with tie-breakers;
- bounded worker shutdown;
- no failed/interrupted/timed-out job returned as a cache hit;
- no result stored after timeout;
- cleanup on every terminal path.

### 6.7 Persisted source evidence

Implement a real persisted source-evidence layer.

It must:

- store canonical parsed source MIDI evidence;
- store bounded audio evidence and exact identities;
- include processor/dependency versions;
- avoid cross-project authorization leaks;
- be reusable across GP-only revision changes;
- invalidate on MIDI/audio/version changes;
- make reuse observable in provenance;
- be race-safe under concurrent requests.

### 6.8 Flask Project UI

Implement the real deployed Flask workflow:

- eligible GP revision selection;
- source MIDI selection;
- optional audio selection;
- optional structure selection;
- create analysis;
- queued/running/succeeded/failed/interrupted status;
- polling without blocking Flask workers indefinitely;
- prior analyses;
- exact trusted input hashes;
- confidence and warnings;
- links or embeds for JSON/HTML reports;
- preserve Phase 1 upload behavior;
- preserve existing converter behavior.

Exercise it through the deployed HTTP/browser path. An in-process Flask `test_client` is useful for unit tests but is not live acceptance evidence.

## 7. Test organization

Ordinary root unit/integration tests must be hermetic and must not:

- require a live cluster;
- restart pods;
- require SSH;
- create permanent remote data;
- depend on undeclared pytest fixtures;
- silently record internal FAIL while returning pytest PASS.

Move or redesign the current live E2E runner so it is not accidentally collected by the pre-deploy root pytest stage.

Recommended separation:

- Hermetic tests: normal root pytest and service-specific pytest.
- Post-deploy live E2E: explicit executable module/script invoked by a dedicated Tekton task after rollout; exits non-zero on any scenario failure; emits machine-readable evidence generated from actual assertions; cannot report PASS when an internal assertion failed.

Do not simply rename the live file and stop. Wire the replacement into the authoritative post-deploy Pipeline.

Ensure the CI image contains all declared test dependencies. Resolve the `pkg_resources/setuptools` issue explicitly and reproducibly; do not rely on an undeclared transitive package.

## 8. Live E2E requirements

Create fresh, uniquely identified, generated, redistributable fixtures for every run.

The post-deploy E2E must prove all fourteen required scenarios from the acceptance document:

1. GP parses to the intended multi-measure grid with notes, marker, repeat metadata.
2. Two agreeing MIDIs produce deterministic consensus.
3. Conflicting MIDI produces explicit regional conflict and lower confidence.
4. Audio clicks/onsets measurably corroborate or contradict MIDI phase.
5. A valid structure anchor changes or locks mapping, not merely cache identity.
6. Conflicting anchors fail with a stable error code.
7. Both source-gap and GP-gap cases produce explicit records.
8. Concurrent requests begin from a cold unique cache key and result in one admitted computation with idempotent followers.
9. GP-only revision change reuses persisted source evidence but recomputes GP mapping.
10. Pod restart preserves results and correctly terminally recovers both queued and running jobs.
11. Canonical JSON/HTML determinism is checked according to the documented identity rules.
12. The deployed Flask Project page completes the real HTTP workflow.
13. MIDI/GP hashes remain unchanged and no permanent source copy remains on Flask storage.
14. Existing Asset API, Project data, converter, homepage, Gitea, GPU, and active LLM remain healthy/unchanged.

For scenario 10, run cluster operations inside an appropriately permissioned, least-privilege Tekton task or controlled AILab SSH session. Do not make the generic E2E client require an unavailable SSH route.

Every scenario must use assertions that terminate the runner with non-zero exit status on failure.

## 9. AILab-native Tekton CI/CD

Audit the existing Tekton implementation from first principles.

It must provide:

- pinned Tekton Pipelines version;
- pinned Tekton Triggers version;
- durable OCI registry;
- persistent registry storage on `/data` where applicable;
- registry authentication;
- secret-authenticated Gitea webhook;
- exact repository validation;
- exact ref validation;
- exact commit SHA validation;
- trigger-loop prevention;
- accepted `main` push as the production webhook path;
- controlled manual validation of the feature candidate without pretending it was a main push;
- exact-SHA clean checkout;
- fail-closed ordering;
- no deploy before all required gates pass;
- Linux amd64 builds on AILab;
- no host Docker socket;
- immutable image tags;
- pushed digest capture;
- provenance capture;
- deployment by immutable digest;
- rollout wait;
- post-deploy smoke and live E2E;
- least-privilege service accounts;
- separated clone/build/push/deploy credentials;
- bounded CPU/memory/ephemeral storage;
- workspace cleanup;
- image/cache/log/result retention appropriate for finite `/data`;
- rollback by recorded digest;
- no PVC deletion during deploy or rollback.

Do not use a manual `docker build` / `docker save` / `ctr images import` path as normal delivery. Delete it or mark it explicitly emergency-only/deprecated.

Do not trigger recursive cron or pipeline creation.

## 10. Required authoritative pipeline gates

Run these from a clean exact-SHA Tekton checkout, not from a dirty Cursor worktree:

- `python -m pytest -q`
- focused reference-time service suite
- focused Asset API suite if separate
- `python -m ruff check .`
- `git diff --check origin/main...HEAD`
- `bash -n infra/ailab/scripts/*.sh`
- `shellcheck infra/ailab/scripts/*.sh`
- `kubectl kustomize infra/ailab > rendered.yaml`
- strict schema validation with `kubeconform`
- secret scanning
- dependency review
- license review
- image vulnerability scanning
- image content inspection
- manifest/network-policy checks
- container runs as intended user
- no unwanted tools/secrets in final image
- PVC persistence verification
- focused post-rollout smoke
- all live E2E scenarios

All tools and versions must be provisioned/pinned in the CI task/image. “Not installed on Cursor” is irrelevant.

Record exact real outputs and counts. Never predict counts in advance.

## 11. Negative pipeline proof

Create a deliberate, isolated failing-gate PipelineRun.

Before running it, record:

- Deployment generation;
- Deployment revision;
- deployed image digest;
- pod UID/imageID;
- relevant PVC identities.

The negative run must:

- fail at a deliberate test/lint/schema gate;
- not execute image publication/deployment tasks that require green gates;
- not mutate the current Deployment;
- not change the last-known-good digest;
- not delete or alter Project/analysis PVC data.

Afterward, record the same state and prove it is unchanged.

Do not weaken or leave the deliberately failing code on the feature branch.

## 12. Deployment and digest proof

For every deployed service:

1. Record exact source commit SHA.
2. Record immutable registry repository/tag.
3. Record pushed OCI digest.
4. Record provenance linking source SHA, build task, image, and digest.
5. Show the Deployment uses that exact digest or equivalent immutable resolved reference.
6. Wait for rollout success.
7. Query live pod `status.containerStatuses[].imageID`.
8. Prove the pod imageID equals the published digest.
9. Verify readiness and focused smoke.
10. Verify declared manifests/deployment contract can reproduce the same state.

Do not claim “commit X is live” merely because an image tag, manifest, environment variable, or endpoint contains X or returns 200.

## 13. Rollback

Before rollout, record the last-known-good digest and verify that it exists in the durable registry.

Perform and document a real rollback test:

- deploy or restore the recorded prior digest;
- wait for rollout;
- verify pod imageID;
- verify Project and analysis data remains;
- verify service health;
- restore the reviewed candidate digest if appropriate;
- verify it again.

Never delete Project or analysis PVC data to make rollback pass.

## 14. Documentation

Update documentation only after real verification.

Required updates include:

- actual implemented architecture;
- exact pinned Tekton/Trigger/tool versions;
- registry path and authentication model;
- webhook validation model;
- exact pipeline stages;
- evidence locations and retention;
- immutable image/digest deployment model;
- emergency-only manual procedure, if retained;
- rollback procedure;
- job recovery/timeout policy;
- source evidence cache behavior;
- live E2E execution;
- known remaining boundaries.

Do not mark Phase 2 complete while a binding P0 requirement remains unimplemented.

Remove stale counts, stale image tags, stale PipelineRun names, and fabricated/obsolete evidence.

## 15. Commit strategy

Use small commits in a logical order, for example:

1. `test: reproduce phase 2 acceptance defects`
2. `fix: validate trusted analysis asset identities`
3. `fix: stream bounded analysis inputs`
4. `fix: make alignment gaps and anchors deterministic`
5. `fix: harden job timeout and cache lifecycle`
6. `feat: persist reusable source evidence`
7. `fix: make live e2e fail closed`
8. `feat: complete deployed project analysis UI`
9. `ci: harden AILab Tekton delivery`
10. `test: add authoritative live acceptance coverage`
11. `docs: record verified phase 2 delivery`

These names are illustrative. Do not combine unrelated fixes into one giant commit.

After each commit:

- run the relevant focused tests;
- inspect `git diff --check`;
- push to the feature branch;
- verify the remote SHA.

Do not force-push.

## 16. Stop conditions

Do not merge to main.

Do not begin Phase 3.

Do not declare Phase 2 accepted if any of these remain:

- root tests fail/error;
- live E2E can internally fail while exiting zero;
- source evidence reuse is not real;
- timeout can publish a late result;
- cold-cache concurrency can duplicate computation;
- wrong-role or wrong-project assets are accepted;
- deployment is tag-only without digest proof;
- negative PipelineRun mutates Deployment;
- rollback is untested;
- Flask UI is only mock-tested;
- active GPU/LLM state is not checked;
- PVC persistence is not checked;
- authoritative gates were run only on Cursor;
- image was built on Cursor.

If blocked, stop with:

- exact failed command;
- exact output;
- current source SHA;
- current PipelineRun/TaskRun;
- current cluster state;
- what is needed to continue.

## 17. Required final response

Your final response must be factual and contain:

- remote feature branch;
- ordered commit list;
- final exact source SHA;
- merge-base with current main;
- clean `git status`;
- pinned Tekton Pipelines version;
- pinned Tekton Triggers version;
- registry implementation and durable storage path;
- webhook EventListener/Trigger names and validation rules;
- successful PipelineRun name/status;
- all relevant TaskRun names/statuses;
- deliberately failing PipelineRun name/status;
- proof the failed run did not mutate Deployment;
- exact root/focused test counts;
- exact lint/render/schema/security/scanning outputs;
- immutable image tags;
- exact OCI digests;
- pod imageIDs;
- proof digest equals live imageID;
- provenance locations/hashes;
- generated E2E project/analysis/job IDs;
- concise evidence for every required E2E scenario;
- source evidence reuse proof;
- timeout/late-worker proof;
- concurrent cold-cache proof;
- deployed Flask UI URL and workflow proof;
- rollback target and tested rollback proof;
- PVC identities before/after;
- GPU/LLM state before/after;
- remaining boundaries, only if they do not violate Phase 2 acceptance.

Attach or point to raw logs/evidence, but summarize the decisive facts directly.

Start now from repository discovery and the mandatory document review. Do not ask for broad confirmation. Ask only if a truly unrecoverable credential or authorization prerequisite is missing.
