# asset-api

FastAPI service for content-addressed project and asset storage.
Part of the gpmidi Reference-Guided MIDI Restoration pipeline.

## Overview

Manages persistent Projects with content-addressed assets (SHA-256 dedup),
Guitar Pro revisions, upload tickets, and deterministic manifests.

## Quick Start (local development)

```bash
cd services/asset_api
pip install -r requirements.txt
ASSET_DATA_ROOT=/tmp/asset-api-dev python -m uvicorn asset_api.main:app --reload --port 8000
```

## API

Base path: `/asset-api` (when behind ingress)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness |
| GET | `/readyz` | Readiness (SQLite + PVC check) |
| POST | `/v1/projects` | Create project |
| GET | `/v1/projects` | List projects |
| GET | `/v1/projects/{id}` | Get project detail |
| PATCH | `/v1/projects/{id}` | Update (optimistic lock) |
| DELETE | `/v1/projects/{id}` | Delete project |
| POST | `/v1/projects/{id}/upload-tickets` | Create upload ticket |
| PUT | `/v1/uploads/{ticket}` | Stream upload |
| GET | `/v1/projects/{id}/assets` | List assets |
| GET | `/v1/projects/{id}/assets/{link}/download` | Download |
| DELETE | `/v1/projects/{id}/assets/{link}` | Delete link |
| GET | `/v1/projects/{id}/manifest` | Deterministic manifest |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ASSET_DATA_ROOT` | `/var/lib/gpmidi` | Root for DB + blobs + temp |
| `CORS_ORIGINS` | (empty) | Comma-separated allowed origins |
| `MAX_AUDIO_BYTES` | 1 GiB | Max upload size for audio |
| `MAX_MIDI_BYTES` | 100 MiB | Max upload size for MIDI |
| `MAX_GP_BYTES` | 100 MiB | Max upload size for Guitar Pro |
| `MAX_TEXT_BYTES` | 5 MiB | Max upload size for text |
| `TICKET_TTL_SECONDS` | 900 | Upload ticket lifetime |

## Tests

```bash
cd services/asset_api
python -m pytest tests/ -v
```

## Docker

```bash
docker build -t asset-api:local .
docker run -p 8000:8000 -e ASSET_DATA_ROOT=/var/lib/gpmidi \
  -v /tmp/asset-data:/var/lib/gpmidi asset-api:local
```

## Deployment

Production images are built and deployed by the exact-SHA Tekton pipeline in
`infra/ailab/tekton/`. Use `infra/ailab/scripts/run-pipeline.sh` only for the
documented controlled manual exact-SHA path. The old
`build-import-asset-api.sh` flow is emergency recovery only and is not accepted
delivery evidence. See `../../docs/CODEX_PROJECT_GUIDE.md`.
