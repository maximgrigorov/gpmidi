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
*Pending: filled from the authoritative PipelineRun, the negative run and the
rollback test.*
<!-- EVIDENCE-END -->
