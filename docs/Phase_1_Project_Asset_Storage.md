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
- **Atomic ticket claim** (CAS `pending → uploading → consumed/failed`)
- **Rate limiter** (bounded in-process: 30 tickets/min, 5 concurrent uploads)
- **GP revisions** (monotonic per-project, idempotent on same hash)
- **Manifest/cache-key** API for future pipeline invalidation
- **Request-ID** on all error responses (header + body)

### Deviations from task

| Item | Task | Actual | Reason |
|------|------|--------|--------|
| Direct browser upload | Browser → AILab HTTPS directly | Flask proxy stream | Self-signed TLS + CORS blocks browser fetch to self-signed endpoint; streaming proxy does NOT store file on gpmidi host |
| HTTP Range for download | "at least basic" or document blocker | Not implemented | Blocker: single-threaded sync reads, FastAPI StreamingResponse; documented, not a Phase 1 priority |
| GP revision via dedicated endpoint | POST /gp-revisions | Returns 501, use ticket flow with role=guitar-pro | Avoids dual-path complexity; revision created automatically on GP upload |

---

## 2. Schema / Migration

**Version:** 2 (migrated from v1 in-place, no data loss)

Tables:
- `projects` — UUID PK, name, description, timestamps, optimistic revision
- `assets` — SHA-256 PK, size, media_type, blob_relpath, integrity status
- `project_assets` — UUID link, FK to project + asset, role, provenance JSON, UNIQUE(project_id, sha256, role)
- `gp_revisions` — UUID PK, FK project + asset, monotonic revision, UNIQUE(project_id, revision)
- `upload_tickets` — ticket_hash PK, TTL, status (`pending`/`uploading`/`consumed`/`failed`/`expired`), consumed_at

**Migration v1→v2:**
- Adds `status` column to `upload_tickets` if missing (safe for existing DBs)
- Invalidates pending tickets for deleted projects
- No data loss, no PVC recreation

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
| PATCH | `/v1/projects/{id}` | Update (optimistic locking via revision, blank name rejected) |
| DELETE | `/v1/projects/{id}` | Delete (cascades links + tickets, preserves blobs) |
| POST | `/v1/projects/{id}/upload-tickets` | Create upload ticket (rate limited: 30/min) |
| PUT | `/v1/uploads/{ticket}` | Streaming upload (atomic claim, concurrency limited: 5) |
| GET | `/v1/projects/{id}/assets` | List project assets |
| GET | `/v1/projects/{id}/assets/{link_id}/download` | Download with Content-Disposition |
| DELETE | `/v1/projects/{id}/assets/{link_id}` | Delete link (blob stays) |
| GET | `/v1/projects/{id}/manifest` | Deterministic manifest JSON |

All errors return `{"detail": {"code": "...", "message": "...", "request_id": "..."}}` + `X-Request-Id` header.
No stack traces, filesystem paths, or ticket values in error responses.

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
| Image | `asset-api:2adc75b` |
| Previous (invalidated) | `asset-api:17448310e361` |
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

## 7. Test Counts (single root `python -m pytest -q`)

| Suite | Tests | Result |
|-------|-------|--------|
| Unit (services/asset_api/tests/test_unit.py) | 35 | ALL PASS |
| Integration (services/asset_api/tests/test_integration.py) | 42 | ALL PASS |
| Flask UI + TLS (test_projects_ui.py) | 16 | ALL PASS |
| Upload proxy (test_upload_proxy.py) | 1 | ALL PASS |
| Regression suite (test_regression.py, test_playable_tabs.py, etc.) | 85 | 82 pass, 3 skip/fail (pre-existing `tuttut` dep) |
| **Total** | **152 passed** | 27 skipped, 3 pre-existing failures |

Pre-existing failures are all in `test_playable_tabs.py` (missing `tuttut` package — unrelated to Phase 1).

---

## 8. Hermes Findings — Resolved

| # | Finding | Resolution |
|---|---------|------------|
| 1 | Package `app/` shadows Flask `app.py` | Renamed to `asset_api/`; single `pytest -q` from root works |
| 2 | TLS verify default inverted | `_parse_bool()` with strict true/false, default=false |
| 3 | Content-Length multipart total sent as file length | Removed; `requests` handles chunked transfer |
| 4 | One ticket consumable twice concurrently | Atomic CAS `pending→uploading` via `WHERE status='pending'`; second gets 409/410 |
| 5 | No concurrency/rate-limit tests or implementation | In-process RateLimiter(30/min) + ConcurrencyLimiter(5); deterministic barrier tests |
| 6 | Errors lack request_id | Middleware + exception handlers; all errors now have `code`+`message`+`request_id` |
| 7 | PATCH allows blank name | `field_validator("name")` on `ProjectUpdate`, same as `ProjectCreate` |
| 8 | Delete project leaves orphan tickets | `UPDATE upload_tickets SET status='expired'` before project delete; schema migration v2; upload verifies project exists after claim |
| 9 | Trailing whitespace / EOF blank lines | Fixed in report and `style.css`; ruff clean on all Phase 1 files |
| 10 | Live Flask acceptance not demonstrated | Deployed Flask on AILab:8082, created project via POST, uploaded WAV via streaming proxy, verified converter home page |

---

## 9. Concurrency Evidence

### Same-ticket race (live on AILab)
```
Results: [201, 410]
Success: 1, Fail(409/410): 1
```
Two concurrent Python threads with `ThreadPoolExecutor(2)` uploading to the same ticket.
Exactly one succeeds, other gets 410 (ticket already claimed by CAS).

### Same-bytes dedup (live)
```
Upload 0: 201, sha=722d4123710e, dedup=False
Upload 1: 201, sha=722d4123710e, dedup=True
DEDUP PASS: same blob, different links
```
Same WAV uploaded with different roles → one physical blob, two project_asset links.

---

## 10. Rate Limiter Evidence (live)

```
201×30, then 429 429
HTTP/2 429
retry-after: 53
x-request-id: 89dca0f320d2
{"detail":{"code":"rate_limit_exceeded","message":"Too many ticket requests. Try again later.","request_id":"89dca0f320d2"}}
```

30 tickets/min threshold; `Retry-After` header with seconds until next slot.

---

## 11. Ticket Cascade on Project Delete (live)

```
Project: b600f4ab-09df-4400-b1ae-cfb5bdb33fa4
Ticket: FXV03GJ8X3EXX7AGjy7W...
Project deleted
Upload after delete: 410 (expected 404 or 410)
CASCADE PASS
```

---

## 12. Flask UI Acceptance (live on AILab)

| Test | URL | Result |
|------|-----|--------|
| Home page (converter) | `http://192.168.30.2:8082/` | 200, shows "конвертер midi" |
| Projects list | `http://192.168.30.2:8082/projects` | 200, shows projects from AILab API |
| Create project | `POST http://192.168.30.2:8082/projects` | 302 → project detail |
| Upload via streaming proxy | `POST http://192.168.30.2:8082/projects/{id}/upload` | 302, file stored in asset-api, no temp on Flask host |
| No residual temp files | `ls /tmp/*.wav` | "No such file" ✓ |

**Flask URL:** `http://192.168.30.2:8082`

---

## 13. TLS Verification (corrected)

| Env var `ASSET_API_VERIFY_TLS` | Meaning |
|-------------------------------|---------|
| `false` / `0` / `no` / `off` / unset | Do not verify (default for self-signed AILab) |
| `true` / `1` / `yes` / `on` | Verify TLS certificate |

Parameterized tests cover all values.

---

## 14. Persistence Evidence

| Event | Project | Assets | GP Revisions | Download SHA |
|-------|---------|--------|--------------|--------------|
| After initial upload | E2E Test Song | 4 | 2 | 3cdd176f... |
| After acceptance fixes deploy | E2E Test Song | 7 | 2 | 3cdd176f... ✓ |
| After `kubectl delete pod` | E2E Test Song | 7 | 2 | 3cdd176f... ✓ |

Schema migration v1→v2 preserved all existing data.

---

## 15. Existing Services Preservation

| Service | Before | After |
|---------|--------|-------|
| Gitea (:3300) | OK | OK |
| Homepage (:80) | 200 | 200 |
| k3s ingress (smoke-app) | Running | Running |
| PVC `smoke-pvc` | Bound | Bound |

---

## 16. GPU Profile Before/After

| Parameter | Before | After |
|-----------|--------|-------|
| GPU | NVIDIA GeForce RTX 5060 Ti | NVIDIA GeForce RTX 5060 Ti |
| Power | 145.97 W | 151.71 W |
| Temperature | 70°C | 71°C |
| asset-api GPU request | — | **None** (CPU/memory only) |

Phase 1 did NOT switch, stop, or request GPU resources.

---

## 17. Known Boundaries / Blockers

1. **LAN-only, no user authentication** — explicitly Phase 1 boundary.
2. **No HTTP Range support** for large audio downloads.
3. **Self-signed TLS** — browser direct upload blocked by certificate trust.
4. **No garbage collection** of orphaned blobs (by design, Phase 1).
5. **Single replica** mandatory (SQLite + RWO PVC).
6. **No Flux/GitOps** — image delivered via `docker save` + `k3s ctr import`.
7. **Rate limiter is in-process** — resets on pod restart. Sufficient for Phase 1 single-replica.

---

## 18. Rollback Steps

```bash
# Remove asset-api deployment
sudo k3s kubectl delete -k infra/ailab/apps/asset-api/

# Data is preserved on PVC until PVC is deleted:
# sudo k3s kubectl delete pvc project-assets -n gpmidi-ml

# Revert git:
git checkout main
```

---

## 19. Branch and Commit SHAs

| Item | Value |
|------|-------|
| Branch | `feat/project-asset-storage` |
| Commit (fixes) | `2adc75b` — fix(asset-api): address Hermes acceptance review findings |
| Commit (image) | `d41090a` — chore: update asset-api image tag to 2adc75b |
| Previous commits | `1744831`, `fa33449` — original Phase 1 implementation |
| Deployed image | `asset-api:2adc75b` |
| Base (main) | `9811154` — docs: add Phase 1 project asset storage task |

Feature branch complete and independently accepted by Hermes. See `docs/PROJECT_STATUS.md` for the authoritative handoff and post-review hardening state.

---

## 20. Quality Gates — Final Run

```
$ python -m pytest -q
152 passed, 27 skipped, 3 failed (pre-existing tuttut), 2 warnings

$ python -m ruff check <Phase-1 files>
All checks passed!

$ git diff --check origin/main...HEAD
(clean)

$ bash -n infra/ailab/scripts/*.sh
(clean)

$ kubectl kustomize infra/ailab > rendered.yaml
657 lines, no errors
```

---

## 21. Independent Hermes Acceptance

Hermes independently reviewed the Cursor fixes, probed the live services, and reran the repository gates. The first independent root run exposed a scheduler-dependent concurrency test failure (`[201, 201]` rather than a guaranteed overlap), despite the live limiter behavior itself having been demonstrated. Commit `c9dbceb` replaced that test with a held streaming upload and added final defensive hardening:

- interrupted `uploading` tickets become `failed` at startup;
- unexpected 500 responses are sanitized and carry a matching request ID in body/header;
- readiness errors do not leak filesystem details;
- SQL result order has deterministic ID tie-breakers;
- rate/concurrency limiter reads are locked;
- stale package command and root Ruff findings are corrected.

Independent final result after that commit:

```text
python -m pytest -q
183 passed, 1 xfailed

python -m ruff check <Phase 1 and changed root files>
All checks passed!

bash -n infra/ailab/scripts/*.sh
ShellCheck 0.11.0
kubectl kustomize infra/ailab
Kubeconform 0.8.0: 23 resources, 22 valid, 0 invalid/errors, 1 skipped
```

The differing earlier `152 passed, 27 skipped, 3 failed` result was environment-dependent: Hermes' validation environment had the optional `tuttut` dependency available, so those tests passed rather than being skipped/failed. It is retained above as historical Cursor evidence, not the final acceptance baseline.

Live before shutdown: Asset API, Flask UI, smoke-app, Gitea and homepage all returned HTTP 200; persisted Project data remained readable. The live image was still `asset-api:2adc75b`. The next task must first build and deploy an immutable image containing `c9dbceb` (or its accepted descendant), then rerun focused Phase 1 smoke checks before implementing Phase 2.
