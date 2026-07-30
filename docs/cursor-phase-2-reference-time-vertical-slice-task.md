# Cursor task: Phase 2 — Reference-time vertical slice

> Start this in a **clean Cursor session**. Execute the task completely: prerequisite Phase 1 hotfix rollout, TDD implementation, immutable image(s), AILab deployment, real E2E tests, and a factual report. Do not stop at a plan, stub, local-only tests, or YAML.

## 1. Goal

Build the first read-only analysis vertical slice that reconciles the floating time of Suno/reference material with the user's evenly aligned Guitar Pro grid.

Inputs already stored by Phase 1:

- a Guitar Pro revision;
- full mix WAV/FLAC;
- optional drums and bass stems;
- one or more recognized Suno MIDI files;
- optional structure JSON/text with timestamp anchors.

Outputs:

1. a deterministic, versioned **source-time map**;
2. comparison/consensus evidence when several Suno MIDI tempo maps exist;
3. a monotonic mapping from source measures to GP measures;
4. confidence, warnings, and provenance for each mapped measure;
5. deterministic JSON and human-readable HTML reports available from the Project UI/API.

This phase is **analysis-only**:

- do not modify GP or MIDI;
- do not generate restoration patches;
- do not run drum/bass transcription models;
- do not add enrichment or humanization;
- do not change existing Shreddage converter semantics;
- do not use GPU unless a separately justified, measured dependency unexpectedly requires it. The intended implementation is CPU-only.

## 2. Mandatory reading and git rules

Before coding, read fully:

- `AGENTS.md`;
- `docs/PROJECT_STATUS.md`;
- `docs/reference-guided-midi-restoration-plan.md`;
- `docs/Phase_0_AILab_bootstrap.md`;
- `docs/Phase_1_Project_Asset_Storage.md`;
- `docs/cursor-phase-1-project-asset-storage-task.md`;
- `services/asset_api/README.md`;
- `infra/ailab/README.md`;
- `infra/ailab/FLUX-BOOTSTRAP.md`.

Then:

1. fetch and checkout current `main`;
2. verify it contains `c9dbcebb454aa6772d8ed33dbaf1cb84f163d27f` or a descendant;
3. create `feat/reference-time-vertical-slice` from current `main`;
4. never implement or commit directly on `main`;
5. make small logical commits and push the feature branch;
6. do not merge to `main`;
7. at first SSH connection to AILab, read `/home/mgrigorov/AGENTS.md` fully before any other remote modification.

Do not commit credentials, cookies, kubeconfig, TLS keys, SQLite DBs, blobs, generated user reports, large audio/MIDI/GP fixtures, container archives, model files, or `/data` content.

## 3. Prerequisite: finish the accepted Phase 1 rollout

The source includes Hermes hardening commit `c9dbceb`, while the last verified live image was `asset-api:2adc75b`.

Before Phase 2 code:

1. record current AILab state:
   - `GET http://192.168.30.2/api/stats`;
   - active profile and model on `:8080` if enabled;
   - homepage, Gitea, smoke ingress, k3s node/pods/PVC;
   - current `asset-api` Deployment image and rollout state;
2. build `services/asset_api` from the accepted source at the exact current source commit;
3. tag it immutably with a git SHA, never `latest`;
4. import/push using the existing documented Phase 1 delivery path;
5. update the manifest to that exact tag;
6. run `kubectl diff`, apply, rollout status, pod image-ID check, logs and probes;
7. verify live:
   - health and readiness;
   - Project list and one persisted Project;
   - upload/download of a tiny generated WAV and SHA-256 equality;
   - interrupted-upload ticket recovery with a controlled test or an integration test plus startup log evidence;
   - generic 500 response is sanitized and has matching body/header request ID;
   - existing Flask Projects page still works;
8. preserve `asset-api:2adc75b` as documented rollback target;
9. commit the image-tag update before beginning Phase 2 implementation.

If this rollout fails, fix or report the blocker and **do not proceed to Phase 2**.

## 4. Core architecture

Implement an additive, independently testable package/service for reference-time analysis. Keep transport, domain algorithm, persistence, and HTML rendering separate.

A reasonable layout is:

```text
services/reference_time/
  reference_time/
    api.py
    models.py
    midi_tempo.py
    gp_grid.py
    audio_evidence.py
    consensus.py
    mapping.py
    report.py
    cache.py
  tests/
  Dockerfile
  requirements*.txt
infra/ailab/apps/reference-time/
```

You may choose a different layout if it is simpler and documented. Do not bury the algorithm in Flask views or extend `asset_api/main.py` into a monolith.

Preferred runtime boundaries:

- `asset-api` remains owner of Projects, asset links, blobs, manifests, and GP revisions;
- a separate `reference-time` API/worker performs CPU analysis;
- the analyzer receives stable asset identity and streams/reads authorized assets through a narrow interface;
- cached analysis metadata/results are linked to the Project through an explicit, versioned contract;
- one replica is enough for this phase;
- no Kubernetes API access from the application pod;
- no hostPath and no direct browsing of unrelated `/data` paths.

If the existing Asset API needs a small backward-compatible endpoint or analysis-result role, add it with migration, tests, and documentation. Do not expose physical blob paths over HTTP.

## 5. Data contracts

Define Pydantic/dataclass models and JSON Schema fixtures for at least:

### `SourceTempoEvidence`

- source asset link ID and SHA-256;
- parser/processor name and semantic version;
- MIDI PPQ;
- ordered time-signature events;
- ordered tempo events;
- first musical event time/tick;
- source duration/tick range;
- warnings and validation errors.

### `SourceMeasure`

- stable index/ID;
- source tick start/end;
- source absolute seconds start/end;
- numerator/denominator;
- effective tempo evidence;
- optional audio downbeat/onset evidence;
- confidence and warning codes.

### `GPMeasure`

- GP revision/link/SHA identity;
- measure index and number;
- start/end in destination GP ticks;
- time signature;
- marker/section text if present;
- repeat/alternate-ending metadata where available;
- musical content summary only as needed for alignment;
- no absolute host paths.

### `MeasureMapping`

- source measure ID/index;
- GP measure index/number or explicit `unmapped`;
- mapping type (`one_to_one`, `source_gap`, `gp_gap`, `repeat`, `ambiguous`);
- source interval in seconds;
- destination interval in GP ticks/beats;
- normalized position mapping `[0,1] -> [0,1]`;
- confidence in `[0,1]`;
- evidence/reason codes;
- warnings;
- optional alternatives with scores.

### `ReferenceTimeAnalysis`

- schema version;
- analysis ID;
- project ID;
- exact input identities/hashes;
- selected GP revision;
- processor versions and parameters;
- source MIDI evidence list;
- MIDI consensus decision or explicit conflict;
- source measures;
- GP measures;
- mappings;
- global confidence and warnings;
- deterministic cache key;
- timestamps that are excluded from canonical result equality if needed.

No NaN/Infinity. List ordering must be stable. Use UTC ISO 8601. Distinguish human display timestamps from deterministic canonical analysis content.

## 6. Source MIDI tempo-map extraction

Use a tested library such as `mido`; pin direct dependencies.

Requirements:

- parse type 0 and type 1 MIDI;
- merge tracks deterministically for meta timing events;
- preserve PPQ and convert ticks to seconds piecewise across tempo changes;
- support multiple tempo and time-signature changes;
- default tempo/time signature only when the MIDI standard permits and emit an explicit warning;
- reject SMPTE division unless deliberately implemented and tested;
- reject malformed/non-monotonic/unsupported files with stable error codes;
- avoid floating cumulative drift: keep integer ticks and rational/controlled decimal calculations until serialization;
- detect pre-roll, pickup/first-event offset, trailing silence, empty musical tracks, and suspiciously short/long maps;
- never infer that the first note is automatically bar 1 without recording the assumption.

Build measure boundaries from tempo/time-signature evidence. Handle a time-signature change that occurs on a boundary. For a mid-measure change, either implement a documented deterministic split policy or mark the affected measure ambiguous; never silently shift everything after it.

## 7. Multiple Suno MIDI consensus

When several `suno-midi.*` assets exist, do not silently choose the first.

Compare at least:

- PPQ-normalized tempo event trajectories;
- duration;
- time signatures;
- bar/downbeat boundaries;
- first-event/pre-roll offsets.

Produce:

- agreement metrics and per-source quality warnings;
- a deterministic selected primary when sources agree within documented tolerances;
- consensus boundaries where safe;
- explicit `tempo_map_conflict` when disagreement exceeds tolerance;
- no automatic high-confidence mapping in conflict regions.

Selection must not depend on SQLite row order, upload order, filesystem order, hash-map order, or thread scheduling. Tie-break with stable documented keys.

## 8. Audio evidence — bounded Phase 2 scope

Audio in this phase validates timing; it is not transcription.

Implement lightweight, CPU-bounded evidence from mix/drums stem when present:

- read metadata/duration/sample rate safely;
- derive mono low-cost onset/transient envelope or downbeat candidates using a pinned permissive dependency;
- use bounded resampling/windowing and documented resource limits;
- estimate/validate initial offset and phase against MIDI-derived boundaries;
- record scores and warnings, not invented drum labels;
- if audio is absent or decoding fails, continue from MIDI with reduced confidence and explicit warning;
- do not load a 1 GiB asset fully into RAM;
- do not write decoded full WAV copies to the gpmidi host;
- use temporary files only inside bounded pod storage and clean them on success, failure, timeout and restart.

Do not introduce Essentia without a written license review. Do not install Beat This!, All-In-One, ADTOF, Basic Pitch, Demucs, or any GPU/model workload in Phase 2.

## 9. GP grid extraction

Use the existing Guitar Pro parser dependency without changing `gp_to_shreddage.py` semantics.

Extract a destination grid that handles/documentedly represents:

- measure headers and time signatures;
- tempo metadata as destination-only evidence, not WAV timing truth;
- pickup/anacrusis if represented;
- repeats, alternate endings, markers and section text;
- multiple tracks without multiplying measure count;
- empty measures;
- malformed/inconsistent measure duration.

Important: destination GP tempo must not overwrite the source absolute timeline. GP positions remain musical ticks/beats in the even destination grid.

## 10. Source-to-GP mapping

Implement a deterministic sequence-alignment algorithm with explicit scoring, not ad-hoc index zip.

Use available features such as:

- measure/time-signature compatibility;
- marker/section anchors;
- optional user timestamp anchors;
- duration only as source-side evidence;
- coarse musical density/onset summaries;
- repeated-section structure;
- monotonicity constraints.

Requirements:

- mapping is monotonic except explicitly represented repeats;
- supports source and GP gaps;
- supports different measure counts;
- confidence is calibrated from documented score components;
- ambiguity produces alternatives/warnings rather than false precision;
- user timestamp anchors are hard constraints and conflicting anchors fail validation;
- map each event position through normalized intra-measure position, not by copying absolute Suno seconds into GP output;
- no MIDI/GP mutation in Phase 2.

A dynamic-programming sequence alignment is acceptable. Keep scoring constants in a versioned parameters object included in the cache key.

## 11. Persistence, jobs and cache invalidation

Analysis can take longer than an HTTP request. Provide a minimal durable job lifecycle:

- `queued`, `running`, `succeeded`, `failed`, `cancelled`;
- stable job ID and analysis ID;
- created/started/finished timestamps;
- progress phase/message;
- sanitized stable error code;
- no raw stack trace/path in API responses;
- idempotent request with same cache key returns/reuses existing succeeded analysis;
- bounded concurrency and queue length;
- timeout and cleanup;
- after process restart, jobs left `running` become a documented recoverable terminal state or are safely re-queued.

Cache key must include exact hashes/IDs for:

- selected GP revision;
- all source MIDI inputs;
- audio evidence inputs used;
- structure/anchor input;
- processor semantic versions;
- dependency/checkpoint identity where applicable;
- canonical parameters.

Critical invalidation behavior:

- upload only a new GP revision: reuse source MIDI/audio time-map evidence; recompute GP parse and source-to-GP mapping;
- change a source MIDI: recompute consensus/source-time map and downstream mapping;
- change only HTML template: do not rerun source analysis if canonical JSON is unchanged;
- failed/incomplete analyses are never returned as successful cache hits.

Use migrations/versioning. Do not mutate Phase 1 blobs.

## 12. API and UI

Versioned API under a documented ingress prefix, for example `/reference-time/v1`:

- `POST /projects/{project_id}/analyses`;
- `GET /projects/{project_id}/analyses`;
- `GET /projects/{project_id}/analyses/{analysis_id}`;
- `GET /projects/{project_id}/analyses/{analysis_id}/report.json`;
- `GET /projects/{project_id}/analyses/{analysis_id}/report.html`;
- `GET /jobs/{job_id}`;
- optional `POST /jobs/{job_id}/cancel` if cancellation is genuinely implemented;
- `/healthz`, `/readyz`.

Validate that all requested asset links belong to the given Project and match expected roles/types. Never accept an arbitrary server path or bare SHA as authorization.

Extend the existing Flask Project page additively:

- select GP revision and available source MIDI/audio/structure assets;
- start Reference-time analysis;
- display job progress/failure;
- list prior analyses and input hashes;
- show summary confidence/warnings;
- link/embed HTML report;
- existing converter and Phase 1 upload UI remain unchanged.

HTML report minimum:

- project and exact input identities;
- consensus summary and conflicts;
- source and GP measure counts;
- per-measure source seconds, GP measure/ticks, mapping type, confidence, evidence and warnings;
- filters for ambiguous/unmapped/low-confidence rows;
- no physical paths, tickets, tokens, stack traces or credentials;
- escaped user-provided names/markers to prevent stored XSS;
- deterministic row ordering.

## 13. Security and operations

Kubernetes workload:

- namespace `gpmidi-ml`;
- PSS restricted;
- non-root, read-only root filesystem, drop ALL capabilities, RuntimeDefault seccomp;
- ServiceAccount token automount disabled;
- no privileged, hostPID, hostNetwork or hostPath;
- bounded CPU/RAM, temp storage and concurrency;
- liveness/readiness/startup probes where appropriate;
- restrictive NetworkPolicy that still permits intended Traefik/API traffic;
- no GPU request for this phase;
- one replica unless durable coordination is implemented;
- immutable exact-SHA image tag or digest, never `latest`.

Before and after deployment, prove homepage, Gitea, smoke-app, Asset API, Flask UI, PVC data, and active LLM profile/model are not harmed. Phase 2 must not switch GPU profile for CPU-only work.

## 14. Required TDD fixtures

Keep fixtures tiny, generated or clearly redistributable:

- MIDI type 0: constant tempo 4/4;
- MIDI type 1: tempo changes;
- 3/4 to 4/4 boundary change;
- pickup/pre-roll;
- conflicting MIDI tempo maps;
- empty MIDI and malformed MIDI;
- GP with matching measures;
- GP with extra/missing measure;
- GP with repeat/marker if existing parser fixture supports it;
- tiny synthetic WAV with known click/onset times;
- structure JSON with valid and conflicting anchors;
- user-controlled marker containing HTML/script characters.

No copyrighted Suno audio in git.

## 15. Required tests

### Unit

- exact tick-to-second conversion across tempo changes;
- measure boundary generation and time-signature changes;
- default-meta warnings;
- SMPTE/malformed rejection;
- pre-roll/pickup representation;
- deterministic MIDI consensus and conflict detection;
- audio duration/onset extraction with bounded reads;
- GP grid extraction including empty and differing measures;
- monotonic alignment, gaps, repeat representation and anchor constraints;
- confidence bounds and reason codes;
- deterministic canonical JSON/cache key;
- stable ordering and no NaN/Infinity;
- HTML escaping and no secret/path leakage.

### Integration

- Project asset-link ownership validation;
- create job -> run -> succeeded -> JSON/HTML retrieval;
- duplicate request -> cache reuse;
- failed analysis -> stable sanitized error;
- restart recovery for running job;
- new GP revision reuses source evidence but creates a new mapping;
- changed source MIDI invalidates source map;
- missing audio yields warning, not false failure;
- simultaneous requests respect queue/concurrency limits;
- existing Asset API and Flask UI regression tests remain green.

### Deployment/E2E

Use a dedicated synthetic Project and prove:

1. upload tiny GP, mix/drums click WAV, and at least two tiny Suno MIDI assets;
2. run a matching-map analysis;
3. retrieve JSON and HTML;
4. verify monotonic mappings and expected confidence;
5. run a deliberately conflicting MIDI-map case and verify explicit conflict/low confidence;
6. delete/recreate analyzer pod and retrieve persisted analysis;
7. request the same analysis and prove cache hit/no duplicate heavy work;
8. upload only GP revision 2 and prove source evidence identity is reused while mapping identity changes;
9. download reports and verify content hashes where deterministic;
10. verify no MIDI/GP output was modified or created;
11. verify no large source file was persisted on the gpmidi host;
12. verify existing Phase 1 data and converter still work.

## 16. Quality gates

Run and record actual output:

```bash
python -m pytest -q
python -m ruff check ...
git diff --check origin/main...HEAD
bash -n infra/ailab/scripts/*.sh
shellcheck infra/ailab/scripts/*.sh
kubectl kustomize infra/ailab
kubeconform -strict -summary rendered.yaml
kubectl diff -k ...
kubectl apply -k ...
kubectl rollout status ...
curl/API E2E checks
```

Also run:

- dependency/license review for every new direct dependency;
- secret scan;
- image vulnerability scan if the existing environment provides one;
- image inspection proving exact code/tag and no embedded fixtures/secrets;
- pod `securityContext`, resources, mounted volumes, image ID and effective NetworkPolicy checks;
- temp-file cleanup checks;
- regression proving GPU profile/model unchanged.

Do not call tests “pre-existing failures” without checking current accepted `main`. The accepted baseline passed **183 tests with 1 expected xfail** in Hermes' environment. Explain environment-dependent skips only with exact collection/failure evidence.

## 17. Required documentation

Create/update:

- `docs/Phase_2_Reference_Time_Vertical_Slice.md`;
- service README/API schema docs;
- `infra/ailab/README.md` and app-specific README;
- `docs/PROJECT_STATUS.md` only to reflect factual feature-branch status, not acceptance;
- migrations and rollback instructions.

Final report must contain:

1. architecture and deviations;
2. source/GP time-grid model;
3. schemas and cache-key contract;
4. exact mapping/scoring algorithm and tolerances;
5. dependency versions/licenses;
6. API/UI endpoints;
7. Kubernetes resources/security;
8. exact test counts and commands;
9. real E2E evidence including conflict and cache invalidation;
10. persistence/restart evidence;
11. before/after GPU/LLM and existing service state;
12. image tag/digest and rollback;
13. known boundaries and concrete Phase 3 handoff.

## 18. Stop conditions and final response

Stop and report rather than guessing if:

- AILab is unavailable after wake/readiness wait;
- Phase 1 hotfix image cannot be built/deployed/rolled back safely;
- required asset ownership cannot be enforced without exposing paths;
- source formats are unsupported by pinned permissive dependencies;
- real E2E evidence contradicts the design;
- deployment would require deleting/recreating existing Project data/PVC;
- credentials or cluster-wide privilege are required but not already approved.

When complete, respond with:

- feature branch;
- ordered commits;
- deployed immutable images;
- exact test/gate results;
- live URLs and concise E2E evidence;
- migrations/rollback;
- GPU/profile before/after;
- known limitations;
- report paths;
- explicit statement: **do not merge to main; Phase 3 not started**.
