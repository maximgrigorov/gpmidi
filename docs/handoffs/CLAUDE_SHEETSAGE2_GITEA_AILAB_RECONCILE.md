# Claude Code task: reconcile and prove the SheetSage2 on-demand service on AILab

## Role and objective

You are the authoring-and-privileged-operations agent for the `gpmidi` SheetSage2 candidate. Work from the Gitea feature branch supplied in the launch instructions, and use the explicitly authorized SSH path `mgrigorov@ailab` for AILab host operations.

The objective is to obtain a **working Gitea-based candidate instance** and prove the real vertical slice:

`browser-facing gpmidi-web route -> SheetSage2 API -> one bounded Kubernetes GPU Job -> persisted result -> downloadable archive/MIDI/report`.

Do not publish anything to GitHub. Do not merge to `main`. Do not open, modify, review, or close the existing Gitea pull request.

## Repository coordinates

- Authoritative forge: `http://192.168.30.2:3300/mgrigorov/gpmidi.git`
- Branch: `feat/sheetsage2-on-demand-tool`
- Starting implementation commit before this handoff document: `f03b733272f5fd615b7627e462c0ffa9abfd2f2f`
- Exact handoff HEAD: use the 40-character SHA supplied alongside this prompt by the operator; verify the remote branch resolves to it before doing any work.
- First instruction file: `AGENTS.md`
- Deployment guide: `docs/CODEX_PROJECT_GUIDE.md`
- This prompt: `docs/handoffs/CLAUDE_SHEETSAGE2_GITEA_AILAB_RECONCILE.md`

## Known state and confirmed root cause

A previous exact-SHA run, `gpmidi-ci-manual-bhl4l`, successfully completed 25/25 Tekton tasks and the generic live suite passed 14/14 at commit `db2980d2cba4477919fd1a136e7404e9dc1d16ca`.

A separate real on-demand upload then returned HTTP 503. From the deployed SheetSage2 controller pod, the Kubernetes Python client failed to reach `https://10.43.0.1:443`; k3s translates that Service to control-plane endpoint `192.168.30.2:6443`, and the live CNI enforces egress after DNAT. RBAC is not the blocker: `system:serviceaccount:gpmidi-ml:sheetsage2-controller` is authorized to create Jobs.

Commit `f03b733272f5fd615b7627e462c0ffa9abfd2f2f` already contains the source fix:

- `infra/ailab/apps/sheetsage2-service/networkpolicy.yaml` allows only `192.168.30.2/32:6443` in addition to `10.43.0.1/32:443`;
- the API has a fail-closed `/readyz` that calls the Kubernetes API;
- the Deployment readiness probe uses `/readyz`;
- regression tests cover the endpoint and readiness behavior.

Hermes could not apply the protected NetworkPolicy because `hermes-operator` is intentionally denied. You are explicitly authorized to do that minimum privileged reconciliation over SSH as `mgrigorov@ailab` with `sudo k3s kubectl`.

## Safety and ownership boundaries

1. Work only on `feat/sheetsage2-on-demand-tool` and AILab namespace `gpmidi-ml`.
2. No GitHub push, no `main` merge, no force-push, no Gitea PR operation.
3. Never print, copy, commit, or include secret values, registry credentials, kubeconfig contents, cookies, owner tokens, or connection strings in the report. Metadata and key names are sufficient.
4. Do not disable or weaken admission/RBAC guardrails. Do not create privileged helper pods or impersonate service accounts.
5. The only planned protected mutation is applying the reviewed repository-owned SheetSage2 NetworkPolicy with the host administrator path.
6. Do not alter or restart unrelated workloads. Preserve pre-existing projects/PVCs and capture their identities before and after.
7. Do not build application images interactively on the workstation or via ad-hoc Docker commands on AILab. The authoritative build/deploy path is Tekton.
8. Do not power off or reboot AILab.
9. Generated audio, MIDI, reports, archives, cookies, and evidence are temporary acceptance artifacts and must not be committed.
10. If a source defect is found, use RED -> narrow fix -> focused GREEN -> full gate, commit normally, push to the same Gitea branch, read back the new remote SHA, and use that exact SHA for the next PipelineRun. Do not amend or force-push.

## Phase 1 — establish exact source state

On the workstation:

1. Clone or fetch the Gitea repository into a clean directory.
2. Checkout `feat/sheetsage2-on-demand-tool` at the exact handoff SHA supplied by the operator.
3. Read `AGENTS.md`, `docs/CODEX_PROJECT_GUIDE.md`, this prompt, and the SheetSage2 manifests/service code.
4. Verify:
   - `git status --porcelain` is empty;
   - `git rev-parse HEAD` equals the supplied handoff SHA;
   - `git ls-remote origin refs/heads/feat/sheetsage2-on-demand-tool` resolves to the same SHA;
   - `git merge-base --is-ancestor 14c52f18e521d3945f5acc02eae80f6c0d880af5 HEAD` succeeds.
5. Do not contact GitHub except for a read-only comparison if genuinely required; no write operation is permitted.

## Phase 2 — pre-change AILab baseline

SSH to `mgrigorov@ailab`. Use a fresh clean clone/worktree of the Gitea branch; do not edit an unknown or dirty checkout.

Before mutation, capture without exposing secrets:

- current namespace PSS label;
- current `sheetsage2-service`, `gpmidi-web`, `asset-api`, and `reference-time` Deployment images/digests and Ready/Available counts;
- exact UIDs and bound volume names for `project-assets`, `reference-time-data`, `sheetsage2-data`, `sheetsage2-models`, and `tekton-evidence`;
- current SheetSage2 NetworkPolicy egress destinations/ports;
- current active GPU profile set from the existing AILab stats API;
- current ready/non-terminating controller pod and its imageID;
- latest PipelineRuns in chronological order.

Use `sudo k3s kubectl` for privileged host-side Kubernetes operations. Do not read Secret values.

## Phase 3 — minimum privileged reconciliation

From the clean checkout at the exact candidate SHA:

1. Validate the intended manifest locally:

```bash
python -m pytest -q test_pipeline_contract.py
sudo k3s kubectl apply --dry-run=server \
  -f infra/ailab/apps/sheetsage2-service/networkpolicy.yaml
```

2. Apply only the reviewed SheetSage2 NetworkPolicy:

```bash
sudo k3s kubectl apply \
  -f infra/ailab/apps/sheetsage2-service/networkpolicy.yaml
```

3. Read it back and prove both rules exist:
   - `10.43.0.1/32`, TCP 443;
   - `192.168.30.2/32`, TCP 6443.

4. From the existing controller pod, use the production Kubernetes Python client path to perform a **server-side dry-run** `create_namespaced_job(..., dry_run="All")` with `build_worker_job`. It must return the dry-run Job name. Do not create a real Job in this step.

5. If the dry-run still fails, inspect effective policies, Service ClusterIP/endpoints, and live CNI/DNAT behavior. Do not broaden egress to `0.0.0.0/0`. Any further durable policy change must be a narrow repository change with a regression test, commit, push, and exact-SHA readback before apply.

## Phase 4 — current candidate gates and exact-SHA release

Run the actual repository gates before release. At minimum:

```bash
python -m pytest -q
(cd services/reference_time && python -m pytest -q)
(cd services/asset_api && python -m pytest -q)
(cd services/sheetsage2_service && PYTHONPATH=. python -m pytest -q)
ruff check .
bash -n infra/ailab/scripts/*.sh
shellcheck infra/ailab/scripts/*.sh
```

Render Kustomize output and run `infra/ailab/scripts/check_manifests.py` against the rendered temporary file. Do not use global `ruff format --check .` as a release gate: the repository currently contains many historical formatting differences; format only files you actually modify when appropriate.

On AILab, apply the candidate Tekton Task/Pipeline definitions from the exact same checkout if their live definitions differ:

```bash
sudo k3s kubectl apply -f infra/ailab/tekton/tasks.yaml
sudo k3s kubectl apply -f infra/ailab/tekton/pipeline.yaml
```

Launch the controlled manual candidate using the full remote-verified SHA:

```bash
./infra/ailab/scripts/run-pipeline.sh ci \
  "$CANDIDATE_SHA" \
  192.168.30.2:3300/mgrigorov/gpmidi-ci:pinned
```

Record the generated PipelineRun name. Wait for authoritative terminal state. Do not infer success from the watcher process exit code alone.

Acceptance for the release pipeline:

- `revision` and `expected-sha` both equal `CANDIDATE_SHA`;
- PipelineRun condition is `Succeeded=True`;
- all TaskRuns are successful;
- built images are immutable `image@sha256:digest` values;
- current pods are Running, Ready, non-terminating, and their `imageID` values equal the deployed digests;
- `/readyz` from `gpmidi-web` to `http://sheetsage2-service:8000/readyz` returns HTTP 200 with JSON containing `status=ready` and `worker_control_plane=true`;
- generic live E2E remains 14/14;
- PVC UIDs/volume names and pre-existing project data remain intact;
- the active GPU profile set is unchanged after the run.

If the PipelineRun fails, inspect the exact failed TaskRun/pod logs. Fix source only when evidence identifies a source defect. Preserve every failed run and its evidence; do not delete or relabel it.

## Phase 5 — real UI -> API -> GPU Job -> persisted download proof

Use the browser-facing host `gpmidi.ailab.home.arpa`, resolving it to `192.168.30.2` if local DNS is unavailable. Validate that `GET /audio-to-midi` returns the actual gpmidi HTML, not the dashboard SPA.

Create a deterministic, non-copyrighted synthetic WAV of sufficient duration (for example 60–90 seconds of a simple changing triad progression with beat transients). Record its SHA-256 and byte count.

Exercise the **gpmidi-web route**, not a direct controller shortcut:

1. Start a fresh cookie jar with `GET https://gpmidi.ailab.home.arpa/audio-to-midi`.
2. Submit multipart form field `file` to `POST /audio-to-midi/upload` with the same cookie jar.
3. Capture the redirect location and Job ID without printing the session cookie.
4. Prove a Kubernetes Job named `sheetsage2-<job-id>` appears with:
   - label `app=sheetsage2-worker`;
   - exact digest-pinned worker image matching the deployed SheetSage2 service image;
   - request and limit `nvidia.com/gpu: 1`;
   - `sheetsage2-data` mounted read-write at `/data`;
   - `sheetsage2-models` mounted read-only at `/models`;
   - no service-account token mount and no privilege escalation.
5. Observe it to terminal completion. If it is Pending, distinguish normal GPU contention from a policy/PVC/image defect. Do not wait beyond the declared Job deadline without diagnosis.
6. Poll `/audio-to-midi/jobs/<job-id>/status` through gpmidi-web with the same cookie jar until `succeeded` or a normalized terminal failure.
7. On success, download through gpmidi-web:
   - `/audio-to-midi/jobs/<job-id>/download/archive`;
   - `/audio-to-midi/jobs/<job-id>/download/midi`;
   - `/audio-to-midi/jobs/<job-id>/download/report`.
8. Verify:
   - each download is non-empty and has a stable SHA-256;
   - MIDI starts with `MThd`, parses as Type 1, contains note-bearing tracks, and includes the intended track names;
   - archive is a valid ZIP containing `*_ALL_TRACKS.mid`, `report.html`, `report.json`, `manifest.json`, and raw model outputs;
   - `report.json` contains input hash, model revision, track/note counts, elapsed time, and `musical_summary` fields for estimated BPM, beats/downbeats, chords, keys, and sections;
   - report HTML begins with an HTML doctype and presents the musical summary;
   - persisted downloads remain retrievable after deleting/restarting only the SheetSage2 controller pod and waiting for the replacement to become Ready.
9. Delete the acceptance job through the user/API path after all persistence checks, or let the worker Job TTL clean up the Kubernetes Job. Remove temporary cookie/audio/download files securely. Do not delete the persisted service PVC or unrelated user jobs.

If the synthetic input is rejected for a legitimate model-quality/input-domain reason after the infrastructure path is proven, generate a better deterministic musical fixture and retry once. Do not use private user audio or copyrighted downloaded media without explicit permission.

## Phase 6 — regression and rollback decision

After the real slice:

- confirm `asset-api`, `reference-time`, `gpmidi-web`, Gitea, homepage, and AILab stats remain healthy;
- confirm no pre-existing Project disappeared and representative pre-existing project asset counts are unchanged;
- confirm no temporary download scope or stray permanent source copy remains on Flask storage;
- confirm no unexpected active GPU Job/profile remains;
- compare final PVC identities with the baseline.

If the candidate causes a regression, stop new submissions and roll the affected Deployment back to the exact pre-change digest captured in Phase 2. Do not roll back unrelated services. Keep the NetworkPolicy rule unless evidence shows it caused the regression; it is required for the controller's intended API path.

## Source-change policy

You may make narrow source fixes on the same feature branch only when live evidence requires them. For each fix:

1. add or identify a focused failing regression test;
2. demonstrate RED;
3. make the minimum patch;
4. demonstrate focused GREEN and the full advertised gates;
5. commit with a descriptive message;
6. push normally to Gitea branch `feat/sheetsage2-on-demand-tool`;
7. read back the remote SHA;
8. restart release validation from that new exact SHA.

Do not rewrite history or silently mix unrelated cleanup.

## Required return handoff

Return a concise report containing:

1. **Verdict:** GO or NO-GO for a working Gitea/AIlab SheetSage2 instance.
2. **Git:** repository URL, branch, starting SHA, final pushed SHA, and whether any source commits were added.
3. **Privileged reconciliation:** exact NetworkPolicy object changed and readback of destinations/ports, without secrets.
4. **Pipeline:** PipelineRun name, terminal condition, all failed/skipped counts, exact candidate SHA, and immutable image digests.
5. **Real vertical slice:** UI URL, sanitized Job ID, GPU Job terminal state, controller restart/persistence proof, downloaded filenames/sizes/SHA-256, MIDI/ZIP/report validation, and musical summary in plain language.
6. **Regression checks:** neighboring service health, project/PVC preservation, GPU profile baseline/final state.
7. **Changes:** changed-file summary, test outputs, and any deviation from this task.
8. **Open risks/blockers:** exact evidence and smallest next action.
9. **Cleanup:** confirmation that temporary cookies/audio/downloads and any non-TTL test objects were removed.

Do not claim GO unless the exact final SHA passed Tekton and the real gpmidi-web -> API -> GPU Job -> persisted-download path completed successfully.
