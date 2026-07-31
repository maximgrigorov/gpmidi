# Project status — Reference-Guided MIDI Restoration

**Last independent review:** 2026-07-31
**Current milestone:** Phase 1 accepted; Phase 2 is the next implementation phase.

## Completed

### Phase 0 — AILab bootstrap

Accepted and merged. AILab has the reproducible k3s/GPU/storage/ingress baseline in `infra/ailab`.

### Phase 1 — Project and Asset Storage

Accepted after Cursor implementation, Cursor acceptance fixes, and an independent Hermes review.

Implemented:

- Project CRUD and optimistic revisions;
- content-addressed SHA-256 blob storage on the `project-assets` 50 GiB PVC;
- upload tickets, streaming upload, type/size validation, deduplication;
- Guitar Pro revisions, manifests, provenance and cache-key utility;
- Flask Projects UI and streaming proxy;
- restricted Kubernetes deployment and HTTPS ingress;
- persistence and live E2E evidence in `docs/Phase_1_Project_Asset_Storage.md`.

Relevant source commits:

- `2adc75b` — Cursor acceptance fixes;
- `d41090a` — deployed image tag update;
- `c6b0156` — Cursor acceptance report update;
- `c9dbceb` — Hermes final hardening after independent review.

Independent final gates after `c9dbceb`:

- root `python -m pytest -q`: **183 passed, 1 xfailed**;
- Ruff: pass;
- `git diff --check`: pass;
- `bash -n`: pass;
- ShellCheck 0.11.0: pass;
- `kubectl kustomize infra/ailab`: pass;
- Kubeconform 0.8.0: 23 resources, 22 valid, 0 invalid/errors, 1 skipped custom schema.

Live checks before shutdown:

- Asset API health/readiness/projects: HTTP 200;
- Flask home and Projects UI: HTTP 200;
- smoke-app, Gitea and homepage: HTTP 200;
- persisted Phase 1 data remained readable;
- live image at that moment: `asset-api:2adc75b`.

## Important deployment handoff

`c9dbceb` adds source hardening newer than the last live image:

- deterministic active-upload-limit test instead of a scheduler-dependent flaky test;
- recovery of tickets left `uploading` after process termination;
- sanitized request-ID-bearing unexpected 500 responses;
- non-leaking readiness errors;
- deterministic SQL tie-break ordering;
- limiter locking, stale README command, and root Ruff cleanup.

Therefore the **first prerequisite in the next Cursor task** is to establish AILab-native Tekton CI/CD with a durable OCI registry and a Gitea-triggered, fail-closed pipeline. Tests, Linux/amd64 image builds, publication and deployment must run on AILab from the exact accepted `main` commit; the ARM64 Cursor workstation must not build, cache or transfer images. The pipeline must then build/deploy the hardened `asset-api`, pin the exact tag/digest, and rerun Phase 1 focused live smoke checks. Do not start Phase 2 implementation until this rollout succeeds. The previous `asset-api:2adc75b` deployment remains the rollback target.

### Phase 2 — Reference-Time Vertical Slice (feature branch)

**Branch:** `feat/reference-time-vertical-slice`
**Status:** Acceptance fixes applied on branch; not merged to main.

Implemented on branch:

- reference-time analysis service (`services/reference_time/`, version 0.3.0);
- MIDI tempo-map extraction with note-density computation per measure;
- multi-MIDI PPQ/time-normalized consensus with regional conflict detection;
- GP grid extraction with markers, repeats, alternate endings;
- DP measure alignment with duration, density, audio, repeat, marker, anchor scoring;
- audio evidence extraction (onset/downbeat via spectral flux);
- structure JSON parsing with monotonic anchor constraints;
- JSON and HTML report generation (XSS-safe);
- SQLite persistence with WAL mode, thread-safe writes, cache-key invalidation;
- FastAPI with background job processing (ThreadPoolExecutor);
- startup recovery for orphaned queued/running jobs;
- Flask Project UI: GP/MIDI/audio/structure selection, analyze, job/result display;
- K8s deployment, service, ingress, NetworkPolicy, PVC;
- Tekton CI/CD: 9-task pipeline (clone, test-lint, kustomize-validate, build×2, deploy×2, smoke×2);
- Gitea OCI registry for durable image storage;
- Gitea webhook → EventListener with CEL interceptor;
- fail-closed negative gate proof (negative-gate-xlk95);
- workspace pruning CronJob;
- rollback instructions with SHA-tagged registry images;
- 99 service tests + 255 root tests passing; ruff clean.

Deployed images (from Tekton pipeline, SHA `65fcc60c`):
- `asset-api@sha256:dc3343e7ea3cd15d74cea5b3f90da5c4398e62686440a40586cd5f2a6a62ff85`
- `reference-time@sha256:67e3fcc8388919f19673814469ec6f40032df8ac3062bb97866fd7e8af68ed96`

Report: `docs/Phase_2_Reference_Time_Vertical_Slice.md`

## Next

Phase 2 acceptance review pending. Phase 3 (MIDI restoration using measure mappings) not started.

## Operational state

AILab services are running. Tekton CI/CD pipeline is active; pushes to `main` on Gitea trigger automated build/test/deploy.

Tekton versions:
- Tekton Pipelines: v0.76.1
- Tekton Triggers: v0.30.1
- Kaniko: v1.23.2
