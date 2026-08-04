# reference-time

Analysis service for aligning source MIDI tempo/time-grid to Guitar Pro
measure structure. Part of the Reference-Guided MIDI Restoration pipeline.

## What it does

1. Parses one or more Suno MIDI files → tempo maps, time signatures, measure boundaries
2. Builds consensus across multiple MIDI sources (picks longest, detects conflicts)
3. Parses a Guitar Pro file → measure grid with markers, repeats, time signatures
4. Aligns source measures to GP measures via dynamic programming
5. Produces JSON and HTML reports with per-measure confidence scores

## API

Base path: `/reference-time` (behind Traefik ingress).

| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Liveness |
| GET | `/readyz` | Readiness (DB check) |
| POST | `/v1/projects/{pid}/analyses` | Start analysis |
| GET | `/v1/projects/{pid}/analyses` | List analyses |
| GET | `/v1/projects/{pid}/analyses/{aid}/report.json` | JSON report |
| GET | `/v1/projects/{pid}/analyses/{aid}/report.html` | HTML report |
| GET | `/v1/jobs/{jid}` | Poll job status |

### Start analysis

```bash
curl -k -X POST https://192.168.30.2/reference-time/v1/projects/$PID/analyses \
  -H 'Content-Type: application/json' \
  -d '{
    "gp_revision_sha256": "...",
    "gp_asset_link_id": "uuid",
    "source_midi_link_ids": ["uuid1", "uuid2"],
    "audio_link_ids": ["uuid3"]
  }'
```

Returns `{"job_id": "...", "analysis_id": "...", "status": "queued", "cache_hit": false}`.
Poll `/v1/jobs/{job_id}` until `status` is `succeeded` or `failed`.

## Module structure

```
reference_time/
├── __init__.py          # version
├── api.py               # FastAPI app and endpoints
├── config.py            # environment-based configuration
├── models.py            # Pydantic data contracts
├── midi_tempo.py        # MIDI tempo-map extraction
├── gp_grid.py           # Guitar Pro grid extraction
├── consensus.py         # Multi-MIDI consensus
├── audio_evidence.py    # Onset/downbeat extraction
├── mapping.py           # DP alignment algorithm
├── cache.py             # Cache-key computation
├── report.py            # JSON/HTML report generation
├── database.py          # SQLite persistence (WAL mode)
```

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `RT_DATA_ROOT` | `/var/lib/reference-time` | SQLite DB and artifacts |
| `ASSET_API_URL` | `http://asset-api.gpmidi-ml.svc.cluster.local:8000` | Asset API base URL |
| `MAX_CONCURRENT_JOBS` | `2` | ThreadPoolExecutor workers |
| `MAX_QUEUE_LENGTH` | `10` | Max queued jobs before 429 |
| `JOB_TIMEOUT_SECONDS` | `300` | Job timeout |
| `CORS_ORIGINS` | (empty) | Comma-separated CORS origins |

## Development

```bash
pip install -r requirements.txt
pip install -r requirements-test.txt

# Run tests
python -m pytest tests/ -v

# Run locally
uvicorn reference_time.api:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t reference-time:dev .
docker run -p 8000:8000 -e ASSET_API_URL=http://host:8000 reference-time:dev
```

## Deployment

Production images are built and deployed by the exact-SHA Tekton pipeline in
`infra/ailab/tekton/`. Use `infra/ailab/scripts/run-pipeline.sh` only for the
documented controlled manual exact-SHA path. The old manual build/import script
is emergency recovery only and is not accepted delivery evidence. See
`../../docs/CODEX_PROJECT_GUIDE.md`.
