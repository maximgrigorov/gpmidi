# Phase 2 — Reference-Time Vertical Slice

**Branch:** `feat/reference-time-vertical-slice`
**Status:** Acceptance fixes applied on branch; **do not merge to main; Phase 3 not started.**

## 1. Architecture and deviations

### Deployed architecture

```
Client → Traefik (443/TLS) → Ingress
         ├── /asset-api/*  → asset-api:8000
         └── /reference-time/* → reference-time:8000
                                    │
                                    ├── HTTP ← asset-api (download assets)
                                    └── SQLite ← /var/lib/reference-time/db/
```

Both services run in the `gpmidi-ml` namespace on AILab (single node, k3s
v1.36.2+k3s1). The reference-time service is an additive, independently
testable microservice with no changes to existing conversion logic.

### Implementation status

| Requirement | Status | Notes |
|---|---|---|
| Tekton CI/CD pipeline | **Implemented** | Tekton Pipelines v0.76.1 + Triggers v0.30.1; fail-closed 9-task pipeline (clone, test-lint, kustomize-validate, build×2, deploy×2, smoke×2). |
| Durable OCI registry | **Implemented** | Gitea OCI registry at `192.168.30.2:3300`; images tagged by commit SHA + `:latest`. |
| Gitea webhook trigger | **Implemented** | EventListener with CEL interceptor filtering `refs/heads/main` pushes. |
| Negative gate proof | **Verified** | `negative-gate-xlk95`: test-lint failed → 6 downstream tasks skipped. |
| ARM64 prohibition | **Complied** | All images built by Kaniko on AILab (linux/amd64). |
| Audio evidence module | **Implemented** | Onset/downbeat extraction via spectral flux; integrated into alignment scoring. |
| Structure JSON input | **Implemented** | Anchor constraints with monotonicity enforcement, integrated into DP alignment. |
| Note density scoring | **Implemented** | Per-measure note density computed and used in alignment confidence. |
| Flask Project UI | **Implemented** | GP/MIDI/audio/structure selection, analysis launch, job/result display, HTML/JSON report links. |
| Rollback instructions | **Documented** | `infra/ailab/tekton/ROLLBACK.md` with SHA-tagged registry images. |
| Workspace pruning | **Implemented** | CronJob (`tekton-prune`) prunes old PipelineRuns and orphaned PVCs daily. |

The deprecated manual build-import scripts (`infra/ailab/scripts/build-import-*.sh`) are retained only for emergency offline recovery.

## 2. Source/GP time-grid model

### Source time grid

A Suno MIDI file produces `SourceTempoEvidence`:

- **Tempo events**: `(tick, bpm, seconds)` — piecewise-linear tempo map
- **Time-signature events**: `(tick, numerator, denominator, seconds)`
- **Duration**: total seconds from first musical event to last
- **Preroll detection**: ticks before first musical note flagged as warning

From evidence, `build_source_measures` constructs `SourceMeasure` objects:

- `measure_index`, `start_tick`, `end_tick`
- `start_seconds`, `end_seconds`, `duration_seconds`
- `tempo_bpm`, `time_signature` (numerator/denominator)
- `note_density` (notes per second, 0 if no notes found)

### GP grid

`extract_gp_grid` parses a Guitar Pro file via PyGuitarPro and yields
`GPMeasure` objects:

- `measure_index`, `start_tick`, `end_tick`
- `tempo_bpm`, `time_signature`
- `has_notes` (any track has notes in this measure)
- `repeat_open`, `repeat_close`, `repeat_count`
- `marker_name` (section markers like "Verse", "Chorus")

Tick-to-second conversion uses the same piecewise-constant tempo map as MIDI.

### Consensus (multiple Suno MIDIs)

`build_consensus` compares N source evidence objects:

- Selects the one with longest duration as primary
- Compares tempo trajectories (normalized 100-point interpolation) and time
  signatures against each reference
- Reports conflicts (`tempo_trajectory_divergence`, `time_signature_mismatch`)
  and drops conflicting sources from the consensus set
- Produces `MidiConsensus` with `primary_sha256`, `agreed_sha256s`, warnings

## 3. Schemas and cache-key contract

### Cache key

`compute_cache_key` produces a deterministic SHA-256 hash over:

```python
{
    "gp_revision_sha256": str,
    "source_midi_sha256s": sorted(list[str]),  # sorted for order-independence
    "audio_sha256s": sorted(list[str]),
    "structure_sha256": Optional[str],
    "processor_versions": dict,
    "parameters": dict
}
```

Canonical JSON serialization (`json.dumps(sort_keys=True)`) ensures
determinism. Any change to input data, processor version, or algorithm
parameters produces a different cache key.

### Key models (Pydantic v2)

- `FiniteFloat`: validator rejecting NaN/Infinity
- `SourceTempoEvidence`: full MIDI parse output
- `SourceMeasure`: measure with timing, tempo, note density
- `GPMeasure`: measure from Guitar Pro with markers and repeats
- `MeasureMapping`: source → GP mapping with type, score, confidence, alternatives
- `MidiConsensus`: multi-MIDI agreement result
- `ReferenceTimeAnalysis`: top-level analysis with all evidence

## 4. Mapping/scoring algorithm and tolerances

Dynamic programming alignment (`align_measures`) with scoring:

| Parameter | Value | Description |
|---|---|---|
| `ts_match_score` | 10.0 | Bonus for matching time signatures |
| `ts_mismatch_penalty` | −5.0 | Penalty for time-signature mismatch |
| `marker_anchor_bonus` | 8.0 | Bonus when GP markers align with source structure |
| `user_anchor_score` | 100.0 | Score for user-provided anchor constraints |
| `gap_penalty` | −3.0 | Penalty for leaving a measure unmapped |
| `duration_weight` | 2.0 | Weight for duration similarity |
| `duration_tolerance_ratio` | 0.3 | Max acceptable ratio difference (30%) |
| `density_weight` | 1.0 | Weight for note-density similarity |
| `repeat_bonus` | 2.0 | Bonus for GP repeat boundaries |

Confidence components (sum to 1.0):

| Component | Weight |
|---|---|
| Time-signature match | 0.25 |
| Duration similarity | 0.25 |
| Marker alignment | 0.15 |
| Monotonicity | 0.15 |
| Note density | 0.10 |
| Audio corroboration | 0.10 |

Mapping types: `one_to_one`, `source_gap`, `gp_gap`, `repeat`, `ambiguous`.

Monotonicity is enforced: if source measure `i` maps to GP measure `j`, then
source measure `i+1` maps to GP measure `≥ j`. User anchors override DP scoring.

## 5. Dependency versions and licenses

| Package | Version | License |
|---|---|---|
| fastapi | 0.115.12 | MIT |
| uvicorn | 0.34.2 | BSD-3-Clause |
| pydantic | 2.11.4 | MIT |
| mido | 1.3.3 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| soundfile | 0.13.1 | BSD-3-Clause |
| numpy | 2.2.6 | BSD-3-Clause |
| PyGuitarPro | 0.11 | LGPL-3.0-only |

PyGuitarPro is LGPL-3.0 — used as a library dependency (imported, not
modified), which is compatible with the project's usage model.

All dependencies are permissively licensed or LGPL (library-use only).

## 6. API endpoints

Base path: `/reference-time`

| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Liveness probe |
| GET | `/readyz` | Readiness probe (DB check) |
| POST | `/v1/projects/{id}/analyses` | Create analysis job |
| GET | `/v1/projects/{id}/analyses` | List analyses and jobs |
| GET | `/v1/projects/{id}/analyses/{aid}` | Get analysis metadata |
| GET | `/v1/projects/{id}/analyses/{aid}/report.json` | JSON report |
| GET | `/v1/projects/{id}/analyses/{aid}/report.html` | HTML report |
| GET | `/v1/jobs/{jid}` | Poll job status |

### Create analysis request

```json
{
  "gp_revision_sha256": "c59eff2c...",
  "gp_asset_link_id": "uuid",
  "source_midi_link_ids": ["uuid", "uuid"],
  "audio_link_ids": ["uuid"],
  "structure_link_id": null
}
```

### Create analysis response

```json
{
  "job_id": "uuid",
  "analysis_id": "uuid",
  "status": "queued",
  "cache_hit": false,
  "request_id": "hex12"
}
```

Job phases: `validating → gp_parse → midi_extract → consensus → measures → alignment → done`

## 7. Kubernetes resources and security

### Deployments

| Deployment | Image | Replicas | Resources |
|---|---|---|---|
| asset-api | `asset-api:a87e8fc914ef` | 1 | (default) |
| reference-time | `reference-time:86f46b5a5f18` | 1 | (default) |

### NetworkPolicies

- `default-deny-all`: deny all ingress/egress by default
- `allow-dns`: egress to kube-dns
- `allow-egress-registries`: egress to registries
- `allow-ingress-asset-api`: ingress from Traefik + reference-time pods
- `allow-ingress-controller`: ingress from Traefik to smoke-app
- `reference-time`: ingress from Traefik, egress to asset-api

### PVCs

| PVC | Capacity | Use |
|---|---|---|
| `project-assets` | 50Gi | Asset API blob storage |
| `reference-time-data` | 5Gi | SQLite DB + analysis artifacts |

### Security context

- Non-root user (`appuser`) in both images
- Read-only root filesystem where applicable
- No GPU required (CPU-only service)

## 8. Test counts and commands

```bash
cd services/reference_time
python3 -m pytest tests/ -v
```

**54 tests passed, 0 failed, 0 skipped** (0.54s)

Breakdown:
- Unit tests (`test_unit.py`): 43 tests
  - `TestTickToSeconds`: 5
  - `TestExtractTempoEvidence`: 7
  - `TestBuildSourceMeasures`: 3
  - `TestConsensus`: 6
  - `TestAlignment`: 10
  - `TestCacheKey`: 6
  - `TestReport`: 3
  - `TestModels`: 3
- Integration tests (`test_integration.py`): 11 tests
  - `TestDatabase`: 8
  - `TestCacheInvalidation`: 3

## 9. E2E evidence

### Live E2E test (2026-07-31)

Full end-to-end test against live AILab deployment:

1. **GP fixture created**: 1422 bytes GP5 via PyGuitarPro, 8 measures
2. **Uploaded to asset-api**: via ticket flow, `link_id` returned
3. **Assets resolved**: 2 MIDI + 1 audio existing in project `4c0b0ad0-...`
4. **Analysis created**: `job_id` returned, `cache_hit=false`
5. **Job polled**: succeeded on first poll (< 2s total processing)
6. **JSON report fetched**: 9203 chars
   - `global_confidence`: 0.26
   - `gp_measures`: 1 (default Song has minimal structure)
   - `source_measures`: 8
   - `mappings`: 8
   - `source_evidence`: 2
7. **HTML report fetched**: 10439 chars, XSS-safe
8. **Cache hit confirmed**: second identical request returned `cache_hit=true`

### Cache invalidation

Tested via integration tests:
- Changed MIDI SHA → new cache key → no cache hit
- Changed GP SHA but same MIDIs → source evidence reuse possible
- HTML report re-render does not re-run analysis

## 10. Persistence and restart evidence

- SQLite WAL mode with `check_same_thread=False` for cross-thread access
- `recover_interrupted_jobs()` runs on startup: marks orphaned `running` jobs as
  `interrupted` with `error_code='process_restart'`
- Failed/interrupted analyses are never returned as cache hits
- Pod restart tested implicitly: rollout replaces the pod cleanly, and readiness
  probe confirms DB init succeeds

## 11. Before/after GPU/LLM and existing service state

### GPU state (unchanged)

```
NVIDIA GeForce RTX 5060 Ti, 16311 MiB, 14630 MiB used, 0% utilization
```

The reference-time service does **not** use the GPU. GPU memory usage is from
the existing llama.cpp profile — unchanged before and after deployment.

### Existing services (verified after deployment)

| Service | Status |
|---|---|
| asset-api | Running (1/1), image `asset-api:a87e8fc914ef` |
| smoke-app | Running (1/1) |
| GPU smoke jobs | Completed |
| PVC smoke jobs | Completed |
| Homepage (port 80) | Accessible |
| Gitea (port 3300) | Accessible |

No existing service was disrupted by the Phase 2 deployment.

## 12. Image tag/digest and rollback

### Current images (from PipelineRun `ruff-fix-t5r6w`)

| Service | Digest | Commit |
|---|---|---|
| asset-api | `sha256:07d5f1f943c1…` | `30138db` — ruff BLE001/I001 fix |
| reference-time | `sha256:ce2398f506b8…` | `30138db` — ruff BLE001/I001 fix |

Pod image IDs match pipeline-produced digests exactly.

### Rollback

```bash
# Rollback reference-time to previous version
sudo k3s kubectl -n gpmidi-ml set image deployment/reference-time \
  reference-time=reference-time:d3bda2d3cc24

# Complete removal of reference-time
sudo k3s kubectl -n gpmidi-ml delete -f infra/ailab/apps/reference-time/

# Rollback asset-api to pre-Phase-2
sudo k3s kubectl -n gpmidi-ml set image deployment/asset-api \
  asset-api=asset-api:49ee7ef0b4ba
# Or the original Phase 1 image:
#  asset-api=asset-api:2adc75b
```

Removing reference-time has no effect on asset-api or any other service.

## 13. Known boundaries and Phase 3 handoff

### Known boundaries

1. **Audio evidence is lightweight**: onset-based downbeat estimation only, no
   ML, no beat tracking model. Sufficient for corroborating tempo estimates.
2. **Single-threaded SQLite**: WAL mode with per-call connections and a write
   lock. Sufficient for current concurrency but not for high-throughput.
3. **No authentication/authorization**: the API is open within the cluster;
   Traefik provides HTTPS but no auth.
4. **Cross-project cache**: cache key does not include project_id. Identical
   inputs in different projects return the same cached result. The cached
   analysis_id may not match the requesting project, causing 404 on report
   fetch unless the caller uses the original project_id.
5. **HTML report timestamp**: `generate_html_report` embeds `datetime.now()`
   on each render. JSON is stored deterministically; HTML is re-rendered.

### Phase 3 handoff

Phase 3 would be the actual MIDI restoration — using the measure mappings
produced by Phase 2 to time-stretch, re-map, and insert Suno MIDI notes into
the GP grid with confidence-gated automation. Prerequisites:

- Validate Phase 2 mappings with real production GP + Suno MIDI pairs
- Tune scoring parameters based on real-world alignment quality
- Fix cross-project cache identity (include project_id or scope results)
- Consider authentication if the service is exposed beyond the local network

## Live E2E evidence

All 14 scenarios passed against deployed services on AILab. Evidence written
to `tests/e2e_evidence.json`. Project ID: see evidence file.

| # | Scenario | Result | Detail |
|---|----------|--------|--------|
| 1 | GP multi-measure grid | **PASS** | 8 measures, marker=True, repeat=True, notes=True |
| 2 | Agreeing MIDIs consensus | **PASS** | decision=agreed, deterministic primary SHA |
| 3 | Conflicting MIDI | **PASS** | decision=conflict, 3 regional conflict regions |
| 4 | Audio corroboration | **PASS** | audio_downbeat_evidence populated, no decode errors |
| 5 | Structure anchor | **PASS** | Different cache keys confirm anchor affects result |
| 6 | Conflicting anchors | **PASS** | error_code=structure_parse_failed |
| 7 | Source/GP gaps | **PASS** | mapping_types={one_to_one, gp_gap} |
| 8 | Cache concurrency | **PASS** | 3 concurrent requests → 1 analysis_id, 3 cache hits |
| 9 | GP revision recompute | **PASS** | Different GP SHA, same source evidence SHA |
| 10 | Pod restart | **PASS** | Pre/post restart: 8=8 GP measures preserved |
| 11 | JSON/HTML deterministic | **PASS** | JSON byte-identical; HTML identical (timestamp excluded) |
| 12 | Flask project page | **PASS** | status=200, analysis UI present, assets visible |
| 13 | No source modified | **PASS** | GP and MIDI SHA match after full analysis workflow |
| 14 | Services healthy | **PASS** | asset-api, reference-time, Flask, Gitea, GPU all ok |

## Ordered commits

See `git log --oneline origin/main..feat/reference-time-vertical-slice` for
the full list. Key commits:

```
30138db fix: ruff BLE001 and I001 in midi_tempo and test_api
571ccbd fix: phase 2 acceptance fixes — CI/CD, scoring, tests, docs
65fcc60 fix: ruff 0.16.1 linting across all service files
```

## Live URLs

| URL | Purpose |
|---|---|
| `https://192.168.30.2/reference-time/healthz` | Health check |
| `https://192.168.30.2/reference-time/readyz` | Readiness check |
| `https://192.168.30.2/reference-time/v1/projects/{id}/analyses` | API |
| `https://192.168.30.2/asset-api/v1/projects` | Asset API |

**Do not merge to main; Phase 3 not started.**
