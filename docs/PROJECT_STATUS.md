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

## Next

Phase 2 — Reference-time vertical slice:

- ingest/use GP + mix + drums/bass stems + one or more Suno MIDI assets already held by Phase 1;
- construct and validate a source-time map;
- preserve separate source-time and destination GP grids;
- map source measures to GP measures with confidence and explicit ambiguity;
- generate deterministic per-measure JSON and HTML reports;
- do **not** modify MIDI and do not run transcription models.

The self-contained clean-session task is:

`docs/cursor-phase-2-reference-time-vertical-slice-task.md`

## Operational state

AILab was intentionally powered off after all checks so it would not make noise overnight. Wake it before the next live task. Never assume SSH or services are ready immediately after Wake-on-LAN; wait for homepage, Gitea, k3s ingress, and required API health.
