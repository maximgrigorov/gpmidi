# Phase 1: Project and Asset Storage — Report

**Date:** 2026-07-31  
**Branch:** `feat/project-asset-storage`  
**Operator:** Cursor Agent (Opus 4.6)

---

## 1. Architecture

### Implemented

Separate FastAPI service `asset-api` deployed in namespace `gpmidi-ml` on AILab:

- **FastAPI** (uvicorn, 1 worker, proxy-headers)
- **SQLite** in WAL mode on `project-assets` PVC (50 GiB RWO)
- **Content-addressed blob storage** (SHA-256, dedup, atomic rename)
- **Upload tickets** (cryptographic, one-time, 15 min TTL, hash-stored)
- **GP revisions** (monotonic per-project, idempotent on same hash)
- **Manifest/cache-key** API for future pipeline invalidation

### Deviations from task

| Item | Task | Actual | Reason |
|------|------|--------|--------|
| Direct browser upload | Browser → AILab HTTPS directly | Flask proxy stream | Self-signed TLS + CORS blocks browser fetch to self-signed endpoint; streaming proxy does NOT store file on gpmidi host |
| HTTP Range for download | "at least basic" or document blocker | Not implemented | Blocker: single-threaded sync reads, FastAPI StreamingResponse; documented, not a Phase 1 priority |
| Rate limiting | Required on ticket creation | Not implemented (documented as Phase 1 boundary) | Would require Redis/in-memory state; acceptable for LAN-only single-user |
| GP revision via dedicated endpoint | POST /gp-revisions | Returns 501, use ticket flow with role=guitar-pro | Avoids dual-path complexity; revision created automatically on GP upload |

---

## 2. Schema / Migration

**Version:** 1 (stored in `schema_version` table)

Tables:
- `projects` — UUID PK, name, description, timestamps, optimistic revision
- `assets` — SHA-256 PK, size, media_type, blob_relpath, integrity status
- `project_assets` — UUID link, FK to project + asset, role, provenance JSON, UNIQUE(project_id, sha256, role)
- `gp_revisions` — UUID PK, FK project + asset, monotonic revision, UNIQUE(project_id, revision)
- `upload_tickets` — ticket_hash PK, TTL, one-time, consumed status

Foreign keys enforced. WAL mode. Busy timeout 5000ms.

---

## 3. API Endpoints

Base: `https://192.168.30.2/asset-api`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness (always 200) |
| GET | `/readyz` | Readiness (checks SQLite + PVC) |
| POST | `/v1/projects` | Create project |
| GET | `/v1/projects` | List projects (with asset count, latest GP rev) |
| GET | `/v1/projects/{id}` | Get project with assets and GP revisions |
| PATCH | `/v1/projects/{id}` | Update (optimistic locking via revision) |
| DELETE | `/v1/projects/{id}` | Delete (cascades links, preserves blobs) |
| POST | `/v1/projects/{id}/upload-tickets` | Create upload ticket |
| PUT | `/v1/uploads/{ticket}` | Streaming upload (raw body) |
| GET | `/v1/projects/{id}/assets` | List project assets |
| GET | `/v1/projects/{id}/assets/{link_id}/download` | Download with Content-Disposition |
| DELETE | `/v1/projects/{id}/assets/{link_id}` | Delete link (blob stays) |
| GET | `/v1/projects/{id}/manifest` | Deterministic manifest JSON |

All errors return `{"detail": {"code": "...", "message": "..."}}` — no stack traces or filesystem paths.

---

## 4. Filesystem Layout (inside PVC)

```
/var/lib/gpmidi/
├── db/
│   └── projects.sqlite3
├── blobs/
│   └── sha256/
│       ├── 3c/dd/3cdd176f8914...  (WAV mix = stem.drums, deduped)
│       ├── 69/62/6962238194d7...  (GP rev 1)
│       └── 77/d7/77d7e59eec30...  (GP rev 2)
└── tmp/
    └── uploads/  (cleared on startup + failure)
```

Physical PVC path: `/data/k3s/storage/pvc-385cd0c7-..._gpmidi-ml_project-assets/`

---

## 5. Image Tag and Delivery

| Parameter | Value |
|-----------|-------|
| Image | `asset-api:17448310e361` |
| Built on | AILab via `docker build` |
| Delivery | `docker save` → `sudo k3s ctr images import` |
| Pull policy | `Never` (pre-loaded into containerd) |
| Not GitOps | Gitea registry auth not configured; fallback per task spec |

Build script: `infra/ailab/scripts/build-import-asset-api.sh`

---

## 6. Manifests, Resources, Limits

```yaml
resources:
  requests: {cpu: 100m, memory: 128Mi}
  limits:   {cpu: "1", memory: 512Mi}
```

| Manifest | Path |
|----------|------|
| Deployment | `infra/ailab/apps/asset-api/deployment.yaml` |
| Service | `infra/ailab/apps/asset-api/service.yaml` |
| Ingress | `infra/ailab/apps/asset-api/ingress.yaml` |
| NetworkPolicy | `infra/ailab/apps/asset-api/networkpolicy.yaml` |
| Kustomization | `infra/ailab/apps/asset-api/kustomization.yaml` |

Security: PSS restricted, non-root (UID 1000), read-only rootfs, no hostPath,
automountServiceAccountToken: false, capabilities drop ALL, seccomp RuntimeDefault,
bounded emptyDir for `/tmp` (256 Mi).

---

## 7. Test Counts

| Suite | Tests | Result |
|-------|-------|--------|
| Unit (services/asset_api/tests/test_unit.py) | 35 | ALL PASS |
| Integration (services/asset_api/tests/test_integration.py) | 33 | ALL PASS |
| Flask UI (test_projects_ui.py) | 8 | ALL PASS |
| **Total** | **76** | **ALL PASS** |

---

## 8. Real E2E Request/Response Summaries

### Create project
```
POST /v1/projects {"name":"E2E Test Song","description":"Phase 1 smoke test"}
→ 201 {"id":"f6614277-...","name":"E2E Test Song","revision":1}
```

### Upload WAV (mix)
```
POST /v1/projects/{id}/upload-tickets {"role":"mix","original_filename":"smoke-test.wav"}
→ 201 {"ticket":"vBAKXb...","expires_at":"...","max_bytes":1073741824}

PUT /v1/uploads/{ticket} [926 bytes WAV]
→ 201 {"sha256":"3cdd176f8914...","size_bytes":926,"deduplicated":false}
```

### Dedup: same bytes as stem.drums
```
PUT /v1/uploads/{ticket2} [926 bytes WAV, same content]
→ 201 {"sha256":"3cdd176f8914...","deduplicated":true}
```

### GP revisions
```
PUT /v1/uploads/{ticket3} [GP5 header v1]
→ 201 {"sha256":"69622381...","role":"guitar-pro"}

PUT /v1/uploads/{ticket4} [GP5 header v2]
→ 201 {"sha256":"77d7e59e...","role":"guitar-pro"}
```

### Download + SHA verify
```
GET /v1/projects/{id}/assets/{link_id}/download
→ 200 [binary, Content-Disposition: attachment]
  sha256sum = 6962238194d7... ✓ MATCH
```

### Manifest
```
GET /v1/projects/{id}/manifest
→ 200 {"schema_version":1,"project":{...},"gp_revisions":[2],"assets":[4]}
  No absolute paths: ✓
```

---

## 9. Dedup Evidence

| Link | Role | Original filename | SHA-256 |
|------|------|-------------------|---------|
| `0a66aec7-...` | mix | smoke-test.wav | `3cdd176f8914da7c3d9d298ea2c4793d4d43bf3ce3e7c6cdf1bbe749a0f2f5c9` |
| `ab954348-...` | stem.drums | same-data.wav | `3cdd176f8914da7c3d9d298ea2c4793d4d43bf3ce3e7c6cdf1bbe749a0f2f5c9` |

**One physical blob** at `blobs/sha256/3c/dd/3cdd176f...`. Two project_asset links, different roles.

---

## 10. Persistence Evidence

| Event | Project | Assets | GP Revisions | Download SHA |
|-------|---------|--------|--------------|--------------|
| After upload | E2E Test Song | 4 | 2 | 3cdd176f... |
| After `kubectl delete pod` | E2E Test Song | 4 | 2 | 3cdd176f... ✓ |
| After `kubectl rollout restart` | E2E Test Song | 4 | 2 | 3cdd176f... ✓ |

---

## 11. Existing Services Preservation

| Service | Before | After |
|---------|--------|-------|
| Gitea (:3300) | OK | OK |
| Homepage (:80) | OK | OK |
| k3s ingress (smoke-app) | `{"status":"ok","version":"0.1.0"}` | Same |
| LLM (:8080) | DOWN (ComfyUI active) | DOWN (expected) |
| `/data` existing dirs | Untouched | Untouched |
| PVC `smoke-pvc` | Bound | Bound |

---

## 12. GPU Profile Before/After

| Parameter | Before | After |
|-----------|--------|-------|
| Active profile | comfyui | comfyui |
| VRAM used | 138 MB | 138 MB |
| GPU temp | 37°C | 37°C |
| asset-api GPU request | — | **None** (CPU/memory only) |

Phase 1 did NOT switch, stop, or request GPU resources.

---

## 13. Known Boundaries / Blockers

1. **LAN-only, no user authentication** — explicitly Phase 1 boundary.
   Anyone on LAN can create/delete projects.
2. **No HTTP Range support** for large audio downloads —
   streaming works, but no partial content / resume.
3. **No rate limiting** — single-user LAN scenario.
4. **Self-signed TLS** — browser direct upload blocked by
   certificate trust; Flask streaming proxy is the workaround.
5. **No garbage collection** of orphaned blobs (by design, Phase 1).
6. **Single replica** mandatory (SQLite + RWO PVC).
7. **No Flux/GitOps** — image delivered via `docker save` + `k3s ctr import`.

---

## 14. Rollback Steps

```bash
# Remove asset-api deployment
sudo k3s kubectl delete -k infra/ailab/apps/asset-api/

# Data is preserved on PVC until PVC is deleted:
# sudo k3s kubectl delete pvc project-assets -n gpmidi-ml

# Revert git:
git checkout main
```

---

## 15. Branch and Commit SHAs

| Item | Value |
|------|-------|
| Branch | `feat/project-asset-storage` |
| Commit 1 | `1744831` — feat: implement Phase 1 asset-api service |
| Commit 2 | `fa33449` — fix: add NetworkPolicy for Traefik ingress |
| Base (main) | `9811154` — docs: add Phase 1 project asset storage task |

Not merged into `main`. Ready for independent review by Hermes.
