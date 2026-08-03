# Cursor / Claude Opus 4.6 handoff — recover Tekton PVC capacity and deploy OpenAI draft UI

You are operating as an infrastructure recovery engineer. Use **Claude Opus 4.6** in Cursor. Work conservatively: inspect first, mutate second, verify every side effect. Do not invent output.

## Repository contract

- Repository URL: `http://192.168.30.2:3300/mgrigorov/gpmidi.git`
- Branch: `feat/logic-arrangement-workflow`
- Baseline commit at handoff creation: `6e4edb2fea1a811ce6f57ed33cda53a1b9e9b987`
- Always `git fetch` and use the **latest remote tip** of this branch; the OpenAI draft feature may add commits after the baseline above.
- Start investigation at: `infra/ailab/tekton/pruning.yaml`
- Also inspect:
  - `infra/ailab/scripts/check_manifests.py`
  - `infra/ailab/base/resource-quota.yaml`
  - `infra/ailab/tekton/rbac.yaml`
  - `infra/ailab/tekton/pipeline.yaml`
  - `infra/ailab/apps/gpmidi-web/deployment.yaml`
  - `infra/ailab/apps/gpmidi-web/networkpolicy.yaml`
  - `infra/ailab/README.md`
- Full path of this prompt in the repository: `docs/handoffs/CURSOR_OPUS_4_6_AILAB_PVC_AND_OPENAI_DEPLOY.md`

## Known live state (verify; do not assume it is unchanged)

- AILab k3s endpoint/host: `192.168.30.2`.
- Namespace: `gpmidi-ml`.
- ResourceQuota is exhausted: `persistentvolumeclaims 20/20`.
- The live `tekton-prune` CronJob is stale: schedule `0 3 * * *`, deleting only runs older than seven days.
- Git already contains the intended durable policy:
  - schedule `*/15 * * * *`;
  - retain at most four completed PipelineRuns;
  - delete orphaned PipelineRun-owned workspace PVCs;
  - manifest policy checks enforce this.
- Four data PVCs are persistent application/evidence state and MUST NOT be deleted:
  - `smoke-pvc`
  - `project-assets`
  - `reference-time-data`
  - `tekton-evidence`
- The other currently observed claims were generated `pvc-*` claims owned by terminal PipelineRuns. Re-query ownership before deletion.
- Two stale pending runs were observed with no TaskRuns because PVC admission could not start:
  - `gpmidi-ci-manual-plnkp`
  - `gpmidi-ci-manual-mrzlb`
  Verify their age/state/workspaces first. Delete stale pending runs before freeing quota so they cannot unexpectedly start and consume the newly released claims.
- Hermes RBAC can read this state but cannot delete PVCs, patch CronJobs, or create/patch Secrets. That is why this task requires your external SSH/admin path.

## Goals

1. Restore PVC headroom safely without deleting persistent data.
2. Make the Git prune policy live so quota exhaustion does not recur during a normal acceptance session.
3. Provision the OpenAI token as a Kubernetes Secret without writing it to Git, logs, Cursor chat, shell history, process listings, or generated artifacts.
4. Deploy the latest exact branch SHA through the repository's Tekton delivery path.
5. Verify live health, exact image provenance, baseline export, opt-in OpenAI draft, usage/cost reporting, and explicit-only apply.

## Hard safety rules

- Do **not** raise the PVC quota as the primary fix. The bug is stale retention, not insufficient intended capacity.
- Do **not** run blanket commands such as `kubectl delete pvc --all`, delete-by-prefix without owner checks, or remove the four protected PVCs.
- Do **not** delete a nonterminal PipelineRun unless it is demonstrably stale and blocked, and record the evidence first.
- Do **not** copy `OPENAI_TOKEN` into any repository file, YAML manifest, prompt, report, command transcript, or `kubectl get ... -o yaml` output.
- Do **not** print or base64-decode Secret data during verification.
- Do **not** run a paid OpenAI request until the user explicitly authorizes that exact live request. Health/config verification must not call the provider.
- Do **not** bypass repository tests or deploy a mutable app tag. The live `imageID` must match the digest built from the deployed Git SHA.
- Keep the improved prune policy even if the application rollback is needed. Do not roll back to the unsafe nightly seven-day policy.

## Procedure

### 1. Establish access and capture a read-only baseline

Use the SSH host/user already configured by the operator. If no SSH target is configured, ask only for the SSH alias/user; do not guess credentials.

On AILab, use the administrative k3s kubeconfig (`sudo k3s kubectl ...` or the host's documented kubeconfig). Capture:

```bash
kubectl -n gpmidi-ml get resourcequota
kubectl -n gpmidi-ml get pvc -o custom-columns='NAME:.metadata.name,CREATED:.metadata.creationTimestamp,OWNER_KIND:.metadata.ownerReferences[0].kind,OWNER:.metadata.ownerReferences[0].name,STATUS:.status.phase'
kubectl -n gpmidi-ml get pipelineruns.tekton.dev --sort-by=.metadata.creationTimestamp
kubectl -n gpmidi-ml get taskruns.tekton.dev --sort-by=.metadata.creationTimestamp
kubectl -n gpmidi-ml get cronjob tekton-prune -o jsonpath='schedule={.spec.schedule}{"\n"}'
```

For each generated `pvc-*`, prove its owner name/kind and the owner's terminal status. Produce a deletion candidate list and a protected list before mutation.

### 2. Update the prune controller before cleanup

From a clean checkout of the latest remote branch, validate manifests first:

```bash
python infra/ailab/scripts/check_manifests.py infra/ailab
kubectl kustomize infra/ailab >/tmp/gpmidi-rendered.yaml
```

Apply only the committed prune/RBAC resources needed to activate the fixed policy, using the repository's documented kustomize/apply path. Re-read the live CronJob and prove:

- `schedule=*/15 * * * *`;
- `RETAIN_COMPLETED_RUNS=4` exists in the live job script;
- the service account can delete terminal PipelineRuns and orphaned owned PVCs.

Do not paste Secret-bearing rendered manifests into chat or logs.

### 3. Neutralize stale pending runs and release quota

Inspect the two known pending runs and any newer pending runs. For each stale run, confirm:

- condition is `Unknown/PipelineRunPending` or equivalent;
- no active TaskRun/pod exists;
- it has been blocked by PVC quota;
- it is not a currently intended deployment.

Delete only confirmed stale pending PipelineRuns. Then create a one-shot Job from the updated CronJob, rather than manually deleting arbitrary PVCs:

```bash
job="tekton-prune-manual-$(date +%s)"
kubectl -n gpmidi-ml create job --from=cronjob/tekton-prune "$job"
kubectl -n gpmidi-ml wait --for=condition=complete "job/$job" --timeout=180s
kubectl -n gpmidi-ml logs "job/$job"
```

Verify that:

- newest four completed PipelineRuns are retained;
- old terminal PipelineRuns are deleted;
- their owner-referenced workspace PVCs are garbage-collected, or the prune job removes only proven orphans;
- all four protected PVCs remain Bound;
- quota has enough headroom for one normal two-workspace PipelineRun, preferably `PVC_USED <= 12/20` after retaining four runs;
- no stuck terminating PVC remains. If one does, inspect finalizers and storage state; do not remove finalizers blindly.

### 4. Provision the OpenAI Secret safely

Inspect the latest committed `infra/ailab/apps/gpmidi-web/deployment.yaml` to get the exact Secret name/key expected by `OPENAI_TOKEN`. Prefer a dedicated Secret if that is what the manifest expects.

Have the operator place the token into an environment variable in a private terminal/session. Never ask them to paste the value into Cursor chat. Create/apply the Secret from stdin with shell history disabled and output redirected; avoid `--from-literal=...` in a logged command. One acceptable pattern is a temporary `0600` env file outside the repository, with a trap that shreds/removes it immediately after `kubectl create secret --from-env-file ... --dry-run=client -o yaml | kubectl apply -f -`. Adapt to the local security tooling.

Verify only metadata and key presence, never value:

```bash
kubectl -n gpmidi-ml get secret <expected-name> -o jsonpath='{.metadata.name}{" keys="}{range $k,$v := .data}{$k}{" "}{end}{"\n"}'
```

Expected key: `OPENAI_TOKEN`. Preserve any existing `SECRET_KEY` if the application reuses `gpmidi-web-secret`; never recreate that Secret from scratch with only one key.

### 5. Deploy the latest exact SHA through Tekton

- Fetch and record the latest branch SHA.
- Ensure local worktree is clean and tests/manifests pass.
- Trigger the repository's normal manual PipelineRun for that exact SHA; do not hand-edit the Deployment image.
- Watch PipelineRun/TaskRuns to terminal state.
- Require all test, scan, license, manifest, build, deploy, and live E2E gates to pass.
- Record the immutable built digest and verify live:

```bash
kubectl -n gpmidi-ml get deploy gpmidi-web -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n gpmidi-ml get pod -l app=gpmidi-web -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

The digest in the Deployment and pod `imageID` must correspond to the pipeline result for the exact Git SHA.

### 6. Live verification without accidental spend

Before any paid request:

- `/healthz` is 200 and reports provider configured without exposing a token;
- baseline upload with OpenAI unchecked succeeds and does not call the provider;
- UI offers OpenAI draft only when configured;
- source HTML, responses, logs, manifests, downloadable artifacts, and pod environment dumps in the report contain no token;
- NetworkPolicy allows public TCP 443 but excludes private/link-local CIDRs as committed;
- no MIDI enrichment is applied before the explicit approval POST;
- a repeated approval POST is idempotent or safely rejected;
- rollback baseline artifacts are retained.

Only after the user explicitly authorizes one paid live draft, run exactly one Spring Melody request and record model, response ID, token usage, estimated cost, and generated plan artifact. Do not expose the token.

## Rollback

If the new app fails after a successful build:

1. Roll back `gpmidi-web` to the previously recorded immutable digest using the repository's documented rollback procedure.
2. Verify pod `imageID`, `/healthz`, baseline upload, and download paths.
3. Keep the fixed `*/15` prune policy active.
4. Keep the Secret unless compromise is suspected; if removing it, remove only `OPENAI_TOKEN` while preserving unrelated Secret keys.
5. Do not restore deleted terminal PipelineRuns/PVCs; `tekton-evidence` and its checksummed bundles are the retained audit source.

## Required final report

Return concise evidence, not assertions:

- SSH target alias used (no credentials);
- starting and deployed Git SHA;
- PVC used/hard before and after;
- protected PVC status after cleanup;
- deleted PipelineRun names and why each was safe;
- live prune schedule and retained-run setting;
- Secret name plus key names only;
- PipelineRun name/result;
- built digest and live pod imageID;
- test/gate counts;
- health/config result;
- whether a paid call was made (default: no) and, if explicitly authorized, its exact usage/estimated cost;
- go/no-go and application rollback digest.
