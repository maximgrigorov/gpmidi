# Phase 2 — Reference-Time Vertical Slice

**Branch:** `feat/reference-time-vertical-slice`
**Status:** implementation and delivery hardening complete on the branch.
**Do not merge to `main`. Phase 3 not started.**

This document records what is actually implemented and what was actually
verified. Nothing here is marked complete on the strength of a manifest, a source
tag or an HTTP 200. Run-specific evidence lives in section 15.

---

## 1. Architecture

```
                          Traefik (443, TLS)
                                 │
   Host: *            ┌──────────┴──────────┐    Host: gpmidi.ailab.local
   /asset-api/*       │  asset-api:8000     │    /  ──►  gpmidi-web:8080
   /reference-time/*  │  reference-time:8000│                 │
                      └──────────┬──────────┘                 ├─► asset-api
                                 │                            └─► reference-time
   reference-time ──HTTP (streamed, bounded)──► asset-api
        │
        ├─ SQLite  /var/lib/reference-time/db/     (reference-time-data PVC, 5 GiB)
        └─ scopes  /var/lib/reference-time/tmp/    (same PVC, purged at startup)

   asset-api  ── blobs /var/lib/asset-api/blobs/   (project-assets PVC, 50 GiB)
```

Three Deployments in namespace `gpmidi-ml` on the single-node k3s cluster
(v1.36.2+k3s1), one replica each, CPU only, no GPU request anywhere.

| Component | Role |
|---|---|
| `asset-api` | Phase 1 owner of Projects, asset links, blobs, manifests, GP revisions |
| `reference-time` | Phase 2 analyzer: source-time map, consensus, mapping, reports |
| `gpmidi-web` | Flask converter and Projects / Reference-Time UI |

`gpmidi-web` is **new in this phase**. The Flask UI existed in the repository but
was not deployed anywhere, which is why the previous acceptance run exercised it
with an in-process `test_client`. Acceptance scenario 12 requires the real HTTP
path, so the UI is now a cluster workload on its own hostname — a catch-all `/`
rule on the `*` host would shadow `/asset-api` and `/reference-time`.

### Deliberate deviations

- **Two CI Python environments.** `requirements.txt` pins numpy 1.23.2, which
  matplotlib then lifts to 1.26.4 — exactly what the deployed converter image
  does — while `services/reference_time` pins numpy 2.2.6. One interpreter cannot
  hold both, so the gate image provides `/opt/venv-root` and `/opt/venv-svc` and
  the three test suites are three separate gates. `pytest.ini` therefore collects
  only the root suites.
- **The registry is Gitea's OCI registry** at `192.168.30.2:3300`. Its package
  data lives under `/home/mgrigorov/gitea/data` on the 937 GiB root NVMe rather
  than on `/data`. Durable persistent host storage, just not the second NVMe;
  moving Gitea's data directory was judged out of scope.
- **Phase 2 is analysis-only.** No GP or MIDI is modified, no restoration patch is
  produced, no transcription model is used, and the converter's musical behaviour
  is untouched.

---

## 2. Source and GP time grids

The two grids are never merged. The source timeline is absolute seconds derived
from MIDI tempo evidence; the GP timeline is musical ticks in an even destination
grid. GP tempo is destination-only evidence and never overwrites source timing.

### Source side — `midi_tempo.py`

`extract_tempo_evidence` parses type 0 and type 1 MIDI with `mido`, merges meta
events from all tracks by absolute tick, and produces `SourceTempoEvidence`:
asset link id and digest, parser name and version, PPQ, ordered time-signature
events, ordered tempo events, first musical event tick and seconds, duration in
ticks and seconds, MIDI type, warnings and validation errors.

Tick→second conversion is piecewise across tempo regions using `Fraction`
arithmetic, so a long file accumulates no floating-point drift. SMPTE division and
zero PPQ are rejected. A missing tempo or time signature falls back to the MIDI
default **with an explicit warning** (`default_tempo`, `default_time_sig`).
Pre-roll, empty tracks and suspicious durations are warned, never silently
assumed.

`build_source_measures` emits `SourceMeasure` records with tick and second bounds,
time signature, effective BPM, note density, optional audio downbeat evidence,
confidence and warnings. A time-signature change on a measure boundary is handled
exactly; a **mid-measure** change truncates that measure and marks it
`mid_measure_ts_change` with reduced confidence, rather than shifting everything
after it.

### GP side — `gp_grid.py`

`extract_gp_grid` reads `song.measureHeaders`, which are shared across tracks, so
multiple tracks do not multiply the measure count. Each `GPMeasure` carries the GP
revision digest, index and 1-based number, tick bounds at GP PPQ 960, time
signature, marker and section text, repeat-open/close plus close count, alternate
ending flags and numbers, an `is_empty` flag, optional tempo, and warnings.

### Consensus — `consensus.py`

Sources are ordered by a documented deterministic key — duration descending, then
digest ascending — so selection cannot depend on upload order, SQLite row order,
hash-map order or thread scheduling. The reference is compared against every other
source on:

- **PPQ/time-normalized tempo trajectory** resampled onto a common grid, so
  differing raw tempo-event counts are irrelevant: a file carrying a redundant
  identical `set_tempo` per bar still agrees;
- **tempo-change boundary alignment**;
- **measure/downbeat boundaries** over the shared prefix, derived from tempo and
  time-signature evidence;
- **time-signature sequences**;
- **duration**;
- **pre-roll / first-event offset**.

Disagreement beyond tolerance produces `decision=conflict` plus explicit
`conflict_regions`. A localized tempo conflict carries a `start_seconds` /
`end_seconds` region; whole-file conflicts (time signature, duration, pre-roll,
measure grid) are not localizable and cover the whole timeline.

Tolerances: 1.0 BPM, 2.0 s duration, 0.5 s boundary, 100 trajectory samples.

---

## 3. Alignment — `mapping.py`

A dynamic-programming alignment of source measures onto GP measures.

### State and transitions

`dp[i][j]` is the best score after consuming source measures `0..i-1` with `j` GP
measures consumed. Transitions into `(i, j)`:

| Op | From | Score |
|---|---|---|
| `match` | `(i-1, k)`, `k < j` | `cell(i,j) + gap_penalty·(j-1-k)` |
| `repeat` | `(i-1, j)`, only if `gp[j-1]` is repeat-eligible | `cell(i,j) + repeat_transition_penalty` |
| `source_gap` | `(i-1, j)` | `gap_penalty` |
| `anchor` | `(i-1, k)`, only `j = anchor+1` | `user_anchor_score + gap_penalty·(anchor-k)` |

**Each cell records its operation, not only its predecessor.** This is the fix for
the audited backtrace defect: a `source_gap` into `(i, j)` keeps `j > 0` because
the last consumed GP measure does not change, so reconstructing from `(i, j)`
alone reinterpreted it as a second match against `gp[j-1]` and emitted a bogus
`repeat`.

A GP measure is **repeat-eligible** only when it carries a repeat marker or an
alternate ending, or lies inside an open/close repeat span. Without such evidence
no GP measure is ever mapped twice.

Anchors are hard: an anchored source measure may only land on its GP measure, and
an anchor set that no monotonic alignment satisfies raises
`structure_anchor_unsatisfiable`.

### Tie-breaking

Candidates are evaluated in a fixed order — match with the smallest skipped-GP
prefix, then justified repeat, then source gap — and only a strictly greater score
displaces the incumbent. The final column is scanned in ascending `j`. Alignment
is a pure function of its inputs; a determinism test asserts two runs produce
identical `model_dump()` output.

### Cell score components

| Component | Contribution |
|---|---|
| time signature | `+10.0` match / `-5.0` mismatch |
| relative duration | `±2.0`, graded inside a 0.30 deviation tolerance |
| note density | `+1.0 × min(1, density / 4.0)` |
| repeat markers | `+2.0`; `+1.0` for an alternate ending |
| audio corroboration | `+3.0 × downbeat_evidence` |
| marker / section text | `+8.0` |
| user anchor | `+100.0` |
| skipped GP measure | `-3.0` each |
| justified repeat | `-1.0` |

### Confidence

Calibrated from the same evidence, each component graded rather than thresholded:
time signature 0.25, duration 0.25, marker 0.12, monotonicity 0.15, density 0.10,
audio 0.10, justified repeat 0.03. Ambiguous mappings are halved. A mapping whose
source interval overlaps a consensus conflict region is halved again and gains the
reason code `consensus_conflict_region`. Gaps get a fixed 0.10.

Global confidence is the mean of matched mappings multiplied by coverage
(matched ÷ total), then by 0.7 when consensus conflicts — so gaps and conflict
pull it down instead of being averaged away.

Sensitivity tests assert that changing duration, density, audio, marker, repeat or
conflict inputs changes the output. No `gap_count >= 0`-style assertion exists
anywhere in the suite.

### Mapping records

Every `MeasureMapping` carries the source index, the GP index and 1-based number
or an explicit gap, the mapping type (`one_to_one`, `source_gap`, `gp_gap`,
`repeat`, `ambiguous`), the source seconds interval, the destination tick
interval, the normalized `[0,1]` intra-timeline position, confidence, evidence
strings, stable reason codes, warnings, and bounded deterministically-ordered
alternatives for near ties. Unmatched GP measures get their own explicit `gp_gap`
records.

---

## 4. Structure and anchors — `structure.py`

Strict versioned parsing (`version: "1.0"`), fail-closed, one stable code per
malformation:

| Code | Cause |
|---|---|
| `structure_malformed_json` | not JSON, or not UTF-8 |
| `structure_root_not_object` | root is not an object |
| `structure_unsupported_version` | missing or unknown version |
| `structure_entry_not_object` | `sections` / `anchors` not arrays of objects |
| `structure_field_missing` | anchor without a source or GP field |
| `structure_index_not_integer` | non-integer index, or non-string label |
| `structure_source_index_out_of_range` | source index or timestamp outside the timeline |
| `structure_gp_index_out_of_range` | GP index outside the grid |
| `structure_conflicting_anchor` | one source to two GP measures, or vice versa |
| `structure_non_monotonic_anchors` | anchors not strictly increasing |
| `structure_anchor_unsatisfiable` | no monotonic alignment satisfies the anchors |

An anchor addresses the source side either by `source_measure` (integer) or by
`source_seconds` (a timestamp, converted to the measure containing that instant).
Sections may derive anchors, and section labels plus GP markers appear in both the
canonical JSON and the HTML report.

---

## 5. Bounded input handling — `assets.py`

`AssetClient.download` streams to a file and enforces two bounds *while* reading,
so an oversized or slow asset is abandoned rather than buffered:

| Role | Byte ceiling | Transfer deadline |
|---|---|---|
| `guitar-pro` | 32 MiB | 60 s |
| `suno-midi.*` | 8 MiB | 30 s |
| `mix`, `stem.*` | 512 MiB | 120 s |
| `structure` | 1 MiB | 15 s |

Connect 5 s, read 30 s, write 30 s, pool 10 s. A declared `Content-Length` over
the ceiling is refused before a byte is stored, and the partial file is unlinked
before any error propagates.

A `DownloadScope` owns a per-analysis directory under
`/var/lib/reference-time/tmp` and removes its whole subtree on success, decode
failure, timeout and cancellation. `purge_stale_scopes` reclaims directories
orphaned by a process restart at startup, so at most one run's directory can
survive a crash. The analyzer never sees or constructs a physical blob path.

Audio is read from that already-bounded file, capped at 600 s of decode, with
persisted onset and downbeat lists truncated to 512 and 256 entries.

---

## 6. Trusted identity and cache contract

### Validation — `validation.py`

Nothing the caller sends is authoritative. `resolve_analysis_inputs` checks every
requested link against the project's own asset listing and its registered GP
revisions:

| Slot | Eligible roles | Eligible types |
|---|---|---|
| GP input | `guitar-pro` | `.gp .gp3 .gp4 .gp5 .gpx` |
| source MIDI | `suno-midi.{mix,drums,bass,guitar,other}` | `.mid .midi` |
| audio | `mix`, `stem.{drums,bass,guitar,vocals,other}` | `.wav .flac` |
| structure | `structure` | `.json .txt .md` |

Failure codes: `no_source_midi`, `asset_link_missing`, `asset_not_in_project`,
`asset_role_invalid`, `asset_type_invalid`, `asset_missing_sha`,
`gp_revision_not_found`, `gp_revision_mismatch`. Each returns HTTP 422 with a
request id **before** any job or cache entry exists. An unreachable or malformed
Asset API is a 502 with its own code, not a generic 500.

`gp_revision_sha256` is optional and treated as an assertion to be checked. A
mismatch is `gp_revision_mismatch`; the trusted digest from Asset API metadata is
the only value that ever reaches a cache key. Duplicate links are de-duplicated
deterministically and inputs are ordered by digest, so identity does not depend on
request ordering.

### Cache identity — `cache.py`

Two project-scoped identities, both prefixed with `CACHE_IDENTITY_VERSION = "2"`
so results computed under the old scheme can never be served:

- **analysis key** — project id, GP revision digest and resolved revision number,
  sorted source MIDI digests, sorted audio digests, structure digest, processor
  versions, canonical parameters;
- **source-evidence key** — project id, sorted source MIDI digests, sorted audio
  digests, processor versions, and only the source-relevant parameters. It
  deliberately excludes the GP revision.

Project scoping fixes the audited isolation defect: two projects holding
byte-identical assets no longer collide, so project B can no longer be handed an
`analysis_id` owned by project A and then fail authorization on retrieval.

`processor_versions()` reports `reference_time`, `mido`, `pydantic`,
`pyguitarpro`, `soundfile` and the alignment parameter version, and all of it
participates in the key. The service version is `0.4.0`; the analysis schema
version is `2.0.0`.

### Invalidation behaviour

| Change | Effect |
|---|---|
| new GP revision only | source evidence **reused**; GP extraction and mapping recomputed |
| source MIDI changed | new source-evidence identity; everything recomputed |
| audio added or changed | new source-evidence identity; everything recomputed |
| structure changed | source evidence reused; mapping recomputed |
| processor or parameter version changed | both identities change |
| HTML template only | canonical JSON unchanged; nothing rerun |
| failed / interrupted / timed out | never returned as a cache hit |

---

## 7. Persisted source evidence

Schema version 2 adds a project-scoped `source_evidence` table. One canonical
bundle holds the parsed MIDI evidence, the bounded audio evidence, the derived
source measures and the consensus decision — everything that does not depend on
the GP side.

On a GP-only revision change the bundle is loaded and **the source MIDI is
neither downloaded nor parsed again**. `provenance` records the key, whether it
was reused, when the row was first written, and the per-run extraction counts
(`source_midi_extractions`, `audio_extractions`, `gp_extractions`), so reuse is
observable and testable by call counts rather than by comparing two hash functions
to each other. Reads are additionally filtered by `project_id`, a second barrier
on top of the project-scoped key. Writes are first-writer-wins, so concurrent
requests cannot corrupt the identity.

---

## 8. Job and cache lifecycle — `database.py`

Invariants, each one a fossilised defect:

- **Terminal states are immutable.** Every status write carries
  `WHERE status NOT IN ('succeeded','failed','cancelled','interrupted')` and
  reports whether it applied, so a late worker cannot overwrite `failed/timeout`
  with `succeeded`.
- **`started_at` is written once** (`COALESCE`). A progress update can no longer
  slide the timeout deadline forward.
- **Results are published atomically against the owning job.** `store_result`
  re-reads the job inside one `BEGIN IMMEDIATE` transaction and refuses to insert
  unless it is still queued or running, so a timed-out job cannot publish.
- **Admission is atomic.** Result lookup, active-job lookup, queue count and
  insert happen in the same immediate transaction, so concurrent cold-cache
  requests admit exactly one computation and every follower is idempotent.
- **No `INSERT OR REPLACE`** on any identity column: `analysis_results` and
  `source_evidence` both use `ON CONFLICT DO NOTHING`.
- **Recovery is terminal and documented.** At startup every orphaned `queued`
  *and* `running` job becomes `interrupted` with `error_code=process_restart`.
  There is no automatic retry: published results are never touched, so
  re-requesting the same inputs is a cache hit while re-requesting an unfinished
  analysis creates a new job.
- **The timeout watchdog runs continuously** for the life of the process, not only
  at startup — `TIMEOUT_WATCHDOG_INTERVAL_SECONDS` (5 s) against
  `JOB_TIMEOUT_SECONDS` (300 s).
- **Ordering is deterministic**, with an explicit tie-breaker on every listing.
- **Shutdown is bounded**: the watchdog is joined with a grace period and the
  executor is shut down with `cancel_futures`.
- Connections are per-operation with a process-wide write lock, on WAL.

Schema migrations: v1 jobs and results, v2 `source_evidence`, v3 `summary_json`
on `analysis_results`.

---

## 9. API and UI

`/reference-time/v1`:

| Method | Path |
|---|---|
| POST | `/projects/{project_id}/analyses` |
| GET | `/projects/{project_id}/analyses` |
| GET | `/projects/{project_id}/analyses/{analysis_id}` |
| GET | `/projects/{project_id}/analyses/{analysis_id}/report.json` |
| GET | `/projects/{project_id}/analyses/{analysis_id}/report.html` |
| GET | `/jobs/{job_id}` |
| GET | `/healthz`, `/readyz` |

Request body: `gp_asset_link_id`, `source_midi_link_ids`, optional
`gp_revision_sha256` (checked, never authoritative), `audio_link_ids`,
`structure_link_id`. Response: `job_id`, `analysis_id`, `status`, `cache_hit`,
`request_id`.

Job phases: `starting → midi_extract → consensus → measures → audio → gp_parse →
structure → alignment → done`. When source evidence is reused the source-side
phases are skipped entirely.

The Project page offers only GP assets that are registered GP revisions, with
their real revision number; source MIDI, audio and structure selection; job rows
that poll the UI's own status endpoint every 5 s for a bounded 240 ticks — one
short request per active job, so no Flask worker is pinned and no browser loops
forever; and a results table showing global confidence, source→GP measure counts,
the mapping-type breakdown, consensus decision, warning codes, whether source
evidence was reused, the structure version, the anchored measures, and every
trusted input digest. A compact `summary_json` is persisted next to each result so
no client parses a full report to render that.

The HTML report adds provenance, audio evidence with exact identities, and
structure/marker sections. All user-provided text is escaped; no physical path,
ticket, token, stack trace or credential appears in either report.

---

## 10. Kubernetes resources and security

| Deployment | Image reference | Replicas | Limits |
|---|---|---|---|
| `asset-api` | deployed by digest | 1 | 1 CPU / 512 MiB |
| `reference-time` | deployed by digest | 1 | 2 CPU / 1 GiB / 1 GiB ephemeral |
| `gpmidi-web` | deployed by digest | 1 | 2 CPU / 1 GiB / 2 GiB ephemeral |

| PVC | Capacity | Use |
|---|---|---|
| `project-assets` | 50 GiB | Asset API blobs, projects, metadata |
| `reference-time-data` | 5 GiB | analysis SQLite plus bounded download scopes |
| `tekton-evidence` | 5 GiB | archived CI evidence bundles, 10 kept |

`gpmidi-web` has **no** PVC: converter sessions live on an `emptyDir`, so nothing
uploaded through the Projects UI is ever persisted there.

`gpmidi-ml` enforces PSS `baseline` and audits/warns `restricted`. Every
application workload runs non-root with a read-only root filesystem, all
capabilities dropped, `RuntimeDefault` seccomp, no privilege escalation, no host
namespaces, no hostPath, no ServiceAccount token, bounded CPU and memory, and a
NetworkPolicy that names its sources. `check_manifests.py` enforces all of that as
a gate — including on CronJobs — and rejects `:latest` on an application
Deployment.

Delivery identities are separated:

| ServiceAccount | May |
|---|---|
| `tekton-build` | clone and build; holds the registry credential; **no** Kubernetes rights |
| `tekton-deploy` | patch Deployments, read pods and PVCs; **no delete verb at all** |
| `tekton-e2e` | delete a pod and exec into one; read Deployments; cannot patch them |
| `tekton-prune` | delete PipelineRuns and orphaned workspace PVCs, from the CronJob only |
| `tekton-triggers-el` | create PipelineRuns from validated webhooks |

Because `tekton-deploy` cannot delete anything, neither a deploy nor a rollback
can remove Project or analysis data.

---

## 11. Delivery pipeline

Pinned versions: `infra/ailab/versions.env` and
`infra/ailab/tekton/Dockerfile.ci`.

| Component | Version |
|---|---|
| Tekton Pipelines | v1.14.1 |
| Tekton Triggers | v0.36.0 |
| kaniko | v1.23.2 |
| kubectl | v1.32.4 |
| kubeconform | v0.7.0 |
| ShellCheck | v0.11.0 |
| gitleaks | 8.24.3 |
| trivy | 0.72.0 |
| crane | v0.20.3 |
| ruff | 0.15.6 |
| Gitea | 1.25.5 |

Fail-closed ordering:

```
clone (exact SHA, verified against the announced SHA)
  └─ source-gates ── root pytest │ reference-time pytest │ asset-api pytest
                     ruff │ git diff --check │ bash -n │ shellcheck
                     kubectl kustomize │ kubeconform -strict
                     manifest policy │ gitleaks │ dependency + license review
  └─ pvc-before (record data-volume identities)
  └─ build × 3 (kaniko, linux/amd64, immutable exact-SHA tag only)
  └─ audit × 3 (tag==digest, arch, non-root uid, content, trivy)
  └─ deploy × 3 (patch by digest, rollout wait, pod imageID == digest)
  └─ pvc-after (identities unchanged)
  └─ smoke × 3
  └─ live-e2e (fourteen fail-closed scenarios)
finally: publish-evidence (archive + SHA256SUMS, keep 10 bundles)
```

Nothing is built until every gate passes, nothing is deployed until the image has
been audited and scanned, and nothing is called live until the running pod's
`imageID` has been proven equal to the published digest. `publish-evidence` is a
`finally` task, so a failed run stays diagnosable.

Vulnerability policy: trivy records the full report and **fails on a CRITICAL that
has a fix available**. Unfixed base-image findings are recorded but cannot be
actioned in this repository.

Co-scheduling is disabled (`configure-tekton.sh`): with the default
`coschedule=workspaces` the affinity assistant refuses a TaskRun binding both the
shared source workspace and the durable evidence workspace, and on a single-node
cluster co-scheduling buys nothing.

### Webhook

The EventListener authenticates the payload with the `github` ClusterInterceptor
against `gitea-webhook-secret` — Gitea signs the body and sends
`X-Hub-Signature-256` — then a CEL interceptor requires
`repository.full_name == 'mgrigorov/gpmidi'`, `ref == 'refs/heads/main'`, a
well-formed non-zero 40-hex commit SHA, and rejects `chore: image tag` and
`[skip ci]` commits. The PipelineRun receives `expected-sha` equal to the
announced SHA, so a branch that moved between webhook and clone fails the run.

There is no delivery commit at all — image tags come from the commit SHA, not from
committed manifests — so there is no commit for delivery to loop on.

### Manual candidate validation

`infra/ailab/scripts/run-pipeline.sh` launches the same Pipeline labelled
`gpmidi.ailab/trigger=manual-candidate`, so evidence never misrepresents a manual
run as a main push. Modes: `ci`, `ci-image`, `negative`.

### Negative proof

`gpmidi-ci-negative` injects a deliberate failure into the **ephemeral workspace
only** — a failing test, a lint violation, a schema-invalid manifest, or a
committed secret — and has no build or deploy task at all, so it cannot mutate a
Deployment. Nothing broken is ever committed to the branch.

Rollback: `infra/ailab/tekton/ROLLBACK.md`.

---

## 12. Test organization

| Gate | Command |
|---|---|
| root suites | `python -m pytest -q` |
| reference-time | `cd services/reference_time && python -m pytest -q` |
| asset-api | `cd services/asset_api && python -m pytest -q` |
| lint | `python -m ruff check .` |

The root suites are hermetic: no live cluster, no SSH, no permanent remote data,
no undeclared fixture. `e2e/live_acceptance.py` is an executable module, not a
`test_*.py`, and `pytest.ini` additionally excludes `e2e/`, so the live runner can
never be collected by the pre-deploy gate again.

`test_e2e_runner_contract.py` pins the runner's fail-closed contract in the
ordinary root gate without any I/O: a recorded failure without a raise still
yields `fail`, a scenario that asserted nothing fails, an unexpected exception
fails, the evidence document reflects failures, all fourteen scenarios are
registered, the source contains no skip, and the generated fixtures really have
the properties the scenarios depend on.

---

## 13. Live acceptance scenarios

`e2e/live_acceptance.py` runs after rollout in its own Tekton task under
`tekton-e2e`. Every observation goes through `Check.require`, which records it and
raises; a scenario passes only if it neither raised nor holds a failed
observation, and a scenario with no observations fails. The evidence document is
generated from those records, so it cannot report PASS while an assertion failed.
There is no skip: an unmet prerequisite is a failure.

Fixtures are generated per run and tagged with a unique run id, so scenario 8
starts from a genuinely cold cache identity.

| # | Scenario |
|---|---|
| 1 | GP parses to the intended multi-measure grid with notes, marker and repeat metadata |
| 2 | Two agreeing MIDIs produce deterministic consensus despite different raw tempo-event counts |
| 3 | Conflicting MIDI produces a localized regional conflict and lower global *and* per-mapping confidence |
| 4 | In-phase clicks corroborate more downbeats and score higher than clicks shifted half a bar |
| 5 | A valid anchor locks the selected mapping, not merely the cache key; timestamp anchors resolve; sections and markers appear in JSON and HTML |
| 6 | Conflicting anchors fail with a stable error code and publish no report; a wrong claimed digest and a wrong role are rejected before admission |
| 7 | Source-gap and GP-gap cases produce explicit records, and a source gap is never reported as a repeat |
| 8 | Concurrent requests on a cold unique identity admit exactly one computation with idempotent followers, then a warm hit |
| 9 | GP-only revision change reuses persisted source evidence (zero re-extractions, same row) and recomputes the GP mapping |
| 10 | Pod restart preserves published results byte-identically and terminally recovers both an observed queued and an observed running job |
| 11 | Canonical JSON/HTML determinism with the documented volatile fields excluded; no NaN/Infinity; no path or trace leakage |
| 12 | The deployed Flask Project page completes the real HTTP workflow end to end |
| 13 | Every source asset still hashes to its upload digest, and no audio/MIDI/GP copy or temporary scope remains on UI or analyzer storage |
| 14 | Asset API, Project data, converter, homepage, Gitea, GPU profile set and the active LLM are unchanged |

Evidence is written to `e2e-evidence.json` in the run's evidence bundle and
archived with SHA256SUMS.

---

## 14. Known boundaries

- **The webhook accept-and-deploy path is not exercised end to end**, because
  doing so requires a push to `main` and this phase must not merge. Payload
  authentication and the identity filters are configured and verifiable; the
  PipelineRun spec the trigger produces is the same one the manual candidate run
  uses.
- **Gitea's registry data is on the root NVMe**, not `/data`. Durable, but not the
  second disk.
- The CI gate image is referenced by the floating `pinned` alias. It is an
  internal *tool* image, republished only by `gpmidi-ci-image`; delivered
  artifacts are always exact-SHA tags deployed by digest.
- Audio evidence is bounded onset/downbeat phase validation only — no
  transcription, no labels, no GPU, as the phase specifies.
- Three root tests in `test_playable_tabs.py` fail on the ARM64 workstation
  because `tuttut` is not installed there. They pass in the CI image, which is the
  authoritative environment.

---

## 15. Verified delivery evidence

See `docs/PROJECT_STATUS.md` for the accepted-state summary. The authoritative
per-run evidence for this branch is recorded below and archived on the
`tekton-evidence` PVC.

<!-- EVIDENCE-START -->
### Authoritative PipelineRun

| | |
|---|---|
| PipelineRun | `gpmidi-ci-manual-bmc96` — **Succeeded**, 20/20 tasks |
| Source commit | `9ebe854f353b16b7d37e7f246f49dff17e443dc7` (checkout verified against the requested SHA) |
| Trigger label | `gpmidi.ailab/trigger=manual-candidate` — a controlled candidate validation, not a main push |
| CI gate image | `gpmidi-ci@sha256:e9055e085be25635dd1ea88d753fa1eee38f78d7ee2e6581182c710d30f03363` (built by `gpmidi-ci-image-t2l9b`) |

TaskRuns, in execution order, all `True/Succeeded`:

`clone`, `pvc-before`, `test-gates`, `static-gates`, `security-gates`,
`build-asset-api`, `audit-asset-api`, `deploy-asset-api`, `build-reference-time`,
`audit-reference-time`, `deploy-reference-time`, `build-gpmidi-web`,
`audit-gpmidi-web`, `deploy-gpmidi-web`, `pvc-after`, `smoke-asset-api`,
`smoke-reference-time`, `smoke-gpmidi-web`, `live-e2e`, `publish-evidence`.

### Gate output

| Gate | Result |
|---|---|
| `python -m pytest -q` (root) | **112 passed, 27 skipped** |
| `services/reference_time` | **198 passed** |
| `services/asset_api` | **81 passed** |
| `python -m ruff check .` | **All checks passed!** |
| `git diff --check origin/main...HEAD` | clean |
| `bash -n infra/ailab/scripts/*.sh` | 9 scripts, clean |
| ShellCheck 0.11.0 | clean |
| `kubectl kustomize infra/ailab` | rendered |
| Kubeconform 0.7.0 `-strict` | **69 resources — Valid 51, Invalid 0, Errors 0, Skipped 18** (unpublished CRD schemas) |
| Manifest policy | 4 workloads, 1 CronJob, 9 NetworkPolicies — all pass |
| gitleaks 8.24.3 | 84 commits scanned, **no leaks found** |
| Dependency + license review | **72 dependencies**, all permissive or explicitly excepted |
| trivy 0.72.0 | full report archived per image; **no fixable CRITICAL** |
| PVC persistence | 2 claims — UID, PersistentVolume and capacity unchanged |

The 27 skips are `test_regression.py` cases needing private `.gp` samples that are
not in the repository. The root count is 112 in CI versus 109 on the ARM64
workstation: the three `tuttut` tests pass in the CI image and fail locally only
because tuttut is not installed there.

### Images, digests and live pods

| Service | Immutable tag | Pushed digest | Live pod `imageID` |
|---|---|---|---|
| asset-api | `asset-api:9ebe854f…` | `sha256:f61d50d0156fce087ee9d0a57397cbcc60bee839b7da9212f3d37cabb4e2fc80` | identical |
| reference-time | `reference-time:9ebe854f…` | `sha256:20b5ced49d6e586ec7e611f8031aa94ab8fb92c999528aa5a48e17792c174def` | identical |
| gpmidi-web | `gpmidi-web:9ebe854f…` | `sha256:a82d05dc6ec25ad5add00a52ab142e811f0a90c18de2506a9aa8cf0b6855fc22` | identical |

Every `deploy-by-digest` task verified `imageID` against the published digest and
would have failed otherwise. Deployment generation/revision after the run:
asset-api 26/22, reference-time 28/24, gpmidi-web 14/10, each annotated with
`gpmidi.ailab/source-commit` and `gpmidi.ailab/image-digest`.

Per-image audit: tag digest equals build digest; `linux/amd64`; effective user
`appuser` (services) and `gpmidi` (converter), never root; no VCS or credential
material; no test or infrastructure content under `app/`.

Provenance and full reports are archived at
`/data/k3s-storage/pvc-ff222482-…_tekton-evidence/manual-candidate-9ebe854f…/`
with `SHA256SUMS`, alongside `provenance-<service>.txt`, `image-<service>/`
(config, manifest, file list, trivy JSON and table), every gate log, `pvc-*.json`
and `e2e-evidence.json`.

### Live acceptance — 14/14, 206 recorded observations

Project `8fc8e47c-483a-4ac1-b744-8a7ec2328b01`, run id `9d5589d13c`.

| # | Decisive evidence |
|---|---|
| 1 | 8 GP measures, all 4/4 with notes, marker `Chorus` at index 4, repeat-open at 2, repeat-close ×2 at 5 |
| 2 | `agreed` on two sources whose raw tempo-event counts differ; measure boundaries and pre-roll aligned; reversed request order is an idempotent hit on the same analysis |
| 3 | `conflict` with **4 conflict regions**, at least one time-localized; global confidence **0.125 vs 0.451**; per-mapping confidence reduced with `consensus_conflict_region` |
| 4 | in-phase clicks corroborate **7** source measures, clicks shifted half a bar corroborate **0**; confidence **0.4925 vs 0.4513** |
| 5 | source 1 locked to GP 5 and the section-derived anchor 0→0 honoured, `anchored_source_indices=[0,1]`; unanchored mapping differs; `source_seconds=5.0` resolves to measure 2; sections and markers present in JSON and HTML |
| 6 | `structure_non_monotonic_anchors`, no report published; wrong claimed digest → `gp_revision_mismatch`; wrong role → `asset_role_invalid`, both before admission |
| 7 | **5 source-gap** records (8 source vs 3 GP) and **6 gp-gap** records (2 source vs 8 GP), no repeat emitted without repeat evidence |
| 8 | 8 simultaneous requests on a fresh identity → **1 admitted job**, all 8 resolved to one analysis (2 followers legitimately hit the warm result), exactly one new result row |
| 9 | identical `source_evidence_key` `b111df4347242ea1…` across cache keys `bb9e58275b4d…` and `1886c77aa610…`; **0** source-MIDI and **0** audio re-extractions, 1 GP extraction; source measures byte-identical, mapping recomputed, 6-measure grid |
| 10 | 2 queued and 2 running jobs at the instant of deletion; **all 4** recovered `interrupted`/`process_restart`, none finished first; new pod `reference-time-f949d449d-lkqfq`; a previously published report survived byte-identically |
| 11 | canonical content `sha256:432a00cf2b1f97309c09c056c95a9e05…`, HTML digest recorded in the bundle excluding its render timestamp; no NaN/Infinity, no path or trace leakage |
| 12 | `https://192.168.30.2/` with `Host: gpmidi.ailab.local` — health, home, Projects list, Project page rendered directly, real form submit, UI status endpoint, both proxied reports; analysis `47ab0ce1-7692-487c-b1f0-bea6db26f7af` |
| 13 | every source asset re-downloaded and re-hashed to its upload digest; no audio/MIDI/GP file and no leftover download scope on UI or analyzer storage |
| 14 | active GPU profile `['llama-server']` before and after; llama.cpp `/health` 200; homepage 200; Gitea 200; 14 projects intact |

### Negative-gate proof

Run twice, breaking a different gate each time. Both proved the same thing.

| Run | Broken gate | Failed at | Skipped after |
|---|---|---|---|
| `gpmidi-ci-negative-h4h2l` | injected failing root test | `test-gates` — `AssertionError: deliberate failure injected by inject-gate-failure` (`1 failed, 112 passed, 27 skipped`) | `static-gates`, `security-gates` |
| `gpmidi-ci-negative-9sgqd` | injected lint violation | `static-gates` (`test-gates` passed first) | `security-gates` |

The pipeline contains no build or deploy task at all, so there is nothing after the
gate chain that could publish or deploy.

State before and after each run is byte-identical: Deployment generation, revision
and digest unchanged, pod UIDs unchanged, PVC UIDs `385cd0c7…`, `ee6ceb47…` and
`ff222482…` unchanged, project count unchanged. The injected failure existed only in
the ephemeral workspace and was never committed to the branch.

### Rollback

Rollback target `reference-time:1485ed92…` — the `previous-image` the deploy task
recorded — confirmed present in the durable registry and resolved to
`sha256:9bb4347eea5a69473d64a6b8834d1bf4458e1fcd72494b8045920131b038cf2b`.

1. Rolled back by digest → live pod `imageID` equal to the rollback digest,
   `readyz` 200, **16 projects and 12 analyses intact**.
2. Restored the candidate digest
   `sha256:20b5ced49d6e586ec7e611f8031aa94ab8fb92c999528aa5a48e17792c174def` →
   live pod `imageID` equal to it, 16 projects and 12 analyses still intact.

The registry holds every SHA-tagged build, so any of them is a usable rollback
target: `09081b1f`, `0d57fa4e`, `1485ed92`, `18f20be8`, `30138db3`, `65fcc60c`,
`7eebd1a5`, `8ab9124d`, `9ebe854f`, `b4bc7854`.

PVC UIDs and PersistentVolumes were unchanged throughout. The `tekton-deploy` Role
has no delete verb, so no rollback can remove Project or analysis data.

### Declared state reproduces the live state

`infra/ailab/apps/*/deployment.yaml` now pin the three reviewed digests, so
`kubectl apply -k infra/ailab` reproduces exactly the verified deployment.

<!-- EVIDENCE-END -->
