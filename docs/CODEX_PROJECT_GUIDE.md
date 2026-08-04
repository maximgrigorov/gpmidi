# gpmidi: complete Codex project and deployment guide

This is the operational source of truth for a fresh Codex session. It explains
what the project does, which files are authoritative, how to develop and test it,
how AILab delivery works, where credentials originate, and how to verify or roll
back a release. It intentionally contains **no secret values**.

## 1. Start here

Repository:

```text
http://192.168.30.2:3300/mgrigorov/gpmidi.git
```

Code-of-record branch: `main`.

Read these files before changing anything, in this order:

1. `CODEX.md` — short agent entry point and hard boundaries.
2. `AGENTS.md` — project invariants and accepted musical behaviour.
3. `LESSONS.md` — measured findings; mandatory before timing, dynamics,
   articulation, bend, vibrato or humanization work.
4. `docs/PROJECT_STATUS.md` — current product phase and accepted evidence.
5. This guide.
6. The relevant implementation report or ADR under `docs/`.

Never infer current production state from an old handoff document, old image tag
or filesystem timestamp. Use Git `main`, the latest successful PipelineRun, the
Deployment image digest and the live pod `imageID`.

## 2. Product and architecture

The repository contains three delivered applications plus historical research
code:

- **gpmidi-web** — Flask UI and Guitar Pro to MIDI converter. The conversion
  source of truth is `gp_to_shreddage.py`; `app.py` is the web wrapper.
- **asset-api** — FastAPI persistent project/content-addressed asset service in
  `services/asset_api/`.
- **reference-time** — FastAPI source-MIDI/Guitar-Pro alignment and report
  service in `services/reference_time/`.
- **transcription spike** — evidence-only model experiments in
  `services/transcription_spike/`; not a product delivery path.

AILab topology:

```text
Developer/Codex
  -> Gitea Git + OCI registry             http://192.168.30.2:3300
  -> Tekton Pipeline/Triggers             k3s namespace gpmidi-ml
  -> Traefik HTTPS                        https://192.168.30.2
       /asset-api/*       -> asset-api
       /reference-time/*  -> reference-time
       Host gpmidi.ailab.home.arpa or legacy gpmidi.ailab.local -> gpmidi-web
       hostless /         -> Headlamp
```

Persistent data:

- `project-assets` PVC — project DB and content-addressed blobs.
- `reference-time-data` PVC — analyses and reports.
- `tekton-evidence` PVC — retained pipeline evidence bundles.
- gpmidi-web sessions use bounded `emptyDir`; uploads are not permanent there.

The OCI registry is Gitea at `192.168.30.2:3300`. Delivered images are built on
AILab for `linux/amd64`, pushed with a full 40-character commit SHA tag, resolved
to a digest, audited/scanned, and deployed by digest.

## 3. Hard project boundaries

These rules are not optional:

- Musical conversion behaviour is stable. Do not change mapping, timing,
  keyswitches, bend/vibrato/slide behaviour, track classification or opt-in
  defaults without explicit user approval.
- Read `LESSONS.md` before touching musical behaviour.
- Humanization and ghost notes are opt-in. Ghost notes are drums-only.
- The accepted Type-1 `_ALL.mid` evidence, not per-track timestamps, is the
  forward baseline. See `docs/evidence/` and `docs/PROJECT_STATUS.md`.
- Never grid-snap or enrich the accepted Solo baseline unless the user explicitly
  approves that experiment.
- Never commit credentials, kubeconfigs, cookies, TLS keys, `.env`, databases,
  uploads, generated GP/MIDI/audio artifacts, model files or `/data` contents.
- Do not build production images on a workstation. AILab Tekton is the accepted
  Linux/amd64 build and deployment path.
- Deploy exact commits only. Never deploy `latest` or another mutable app tag.
- Do not delete/recreate the persistent PVCs during deployment or rollback.
- OpenAI arrangement calls cost money. A configured token is not permission to
  make a paid request; obtain explicit authorization for the exact live call.
- GPU experiments must preserve and restore the previously active AILab GPU
  profile. Normal app delivery does not need the GPU and must not change it.

## 4. Repository map

```text
app.py                              Flask web application
gp_to_shreddage.py                 converter source of truth
humanize.py                         opt-in instrument humanization
verify_midi.py                      artifact invariant verifier
config/articulation_maps/           versioned instrument mappings
config/humanize_profiles/           versioned humanization profiles
services/asset_api/                 persistent Project/Asset API
services/reference_time/            alignment/report API
services/transcription_spike/       historical/evidence-only model spike
e2e/live_acceptance.py              deployed live acceptance suite
infra/ailab/                        declarative k3s/Tekton/application state
infra/ailab/tekton/                 delivery pipeline, gates, RBAC, retention
infra/ailab/scripts/run-pipeline.sh exact-SHA manual pipeline launcher
docs/PROJECT_STATUS.md              current milestone and evidence index
docs/adr/                           accepted architecture decisions
docs/evidence/                      frozen evidence identities/fingerprints
```

## 5. Clone and Git authentication

### 5.1 Get a Gitea token

1. Open `http://192.168.30.2:3300` on the trusted LAN/VPN.
2. Sign in as the authorized Gitea user.
3. Open **Settings -> Applications -> Manage Access Tokens**.
4. Create a dedicated token for the workstation/Codex environment.
5. Grant only repository read/write access needed for clone/push. Add package
   read/write scope only if that workstation must inspect/manage OCI packages;
   normal delivery through Tekton does not require a local registry login.
6. Store the token in the OS credential manager. Do not put it in the clone URL,
   a prompt, repository file, shell history or Codex transcript.

Clone:

```bash
git clone http://192.168.30.2:3300/mgrigorov/gpmidi.git
cd gpmidi
git switch main
git pull --ff-only origin main
```

When Git asks for credentials, use Gitea username `mgrigorov` and the access token
as the password. Prefer Git Credential Manager or the operating-system keychain;
do not enable plaintext `credential.helper store`.

Before work:

```bash
git status --short --branch
git fetch origin
test "$(git rev-parse main)" = "$(git rev-parse origin/main)"
```

Unless the user explicitly requests a direct `main` edit, create a focused branch
from current `origin/main`, make small commits, push it and provide the exact
branch/SHA for review.

## 6. Local development

Use Python 3.11. The root converter and the two services require incompatible
NumPy versions, so use separate virtual environments rather than installing all
requirements into one environment.

### 6.1 Converter and web UI

```bash
python3.11 -m venv .venv-root
. .venv-root/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env          # optional; keep .env untracked
set -a; . ./.env; set +a
python -m pytest -q
python -m flask --app app run --host 127.0.0.1 --port 8080
```

Use a real, untracked `.gp`/`.gp5` input before claiming conversion works. Verify
both parsed track summary and downloadable per-track/Type-1 MIDI artifacts.

CLI conversion follows the help printed by:

```bash
python gp_to_shreddage.py --help
```

Do not commit source GP files or generated MIDI/ZIP/PDF artifacts.

### 6.2 Asset API

```bash
python3.11 -m venv .venv-asset
. .venv-asset/bin/activate
python -m pip install --upgrade pip
python -m pip install -r services/asset_api/requirements.txt
cd services/asset_api
ASSET_DATA_ROOT=/tmp/gpmidi-asset-api-dev \
  python -m uvicorn asset_api.main:app --reload --port 8001
```

Tests:

```bash
cd services/asset_api
python -m pytest -q
```

### 6.3 Reference-Time API

```bash
python3.11 -m venv .venv-reference
. .venv-reference/bin/activate
python -m pip install --upgrade pip
python -m pip install -r services/reference_time/requirements.txt
python -m pip install -r services/reference_time/requirements-test.txt
cd services/reference_time
RT_DATA_ROOT=/tmp/gpmidi-reference-time-dev \
ASSET_API_URL=http://127.0.0.1:8001 \
  python -m uvicorn reference_time.api:app --reload --port 8002
```

Tests:

```bash
cd services/reference_time
python -m pytest -q
```

## 7. Required checks before push

The authoritative full gate is Tekton, but run the relevant local checks first:

```bash
# Root environment
python -m pytest -q
python -m ruff check .
git diff --check

# Service environments
( cd services/asset_api && python -m pytest -q )
( cd services/reference_time && python -m pytest -q )

# Shell and manifests when infra changed
bash -n infra/ailab/scripts/*.sh
shellcheck infra/ailab/scripts/*.sh
kubectl kustomize infra/ailab > /tmp/gpmidi-rendered.yaml
python infra/ailab/scripts/check_manifests.py /tmp/gpmidi-rendered.yaml
```

The CI image pins the exact production gate toolchain in
`infra/ailab/tekton/Dockerfile.ci`. Tekton additionally performs strict
Kubeconform validation, full-history gitleaks scan, dependency/license review,
per-image audit, Trivy scan, rollout and live E2E.

## 8. Cluster access and Kubernetes token

### 8.1 Normal Codex delivery needs no kubeconfig

An accepted push to `main` is intended to enter through the signed Gitea webhook.
Therefore a Codex workstation normally needs only Git credentials. Cluster
credentials are needed to monitor, manually launch an exact-SHA run, inspect
logs, provision Secrets or perform rollback.

### 8.2 Operator kubeconfig for Tekton monitoring/manual runs

The installed least-privilege operator identity is the namespaced ServiceAccount
`hermes-operator` in namespace `gpmidi-ml`. It can inspect the application and
Tekton resources, create a PipelineRun and perform bounded operational actions;
it is not a cluster-admin identity.

On the AILab host or console, from a trusted checkout, mint a short-lived
kubeconfig:

```bash
cd /path/to/gpmidi
bash infra/ailab/scripts/generate-operator-kubeconfig.sh 24h
```

The script writes `~/.kube/gpmidi-operator.kubeconfig` with mode `0600`. Transfer
it only through an approved secure channel to the development workstation, keep
mode `0600`, and delete it when no longer needed. It contains a bearer token.
Never commit or paste it into chat.

Use it:

```bash
export KUBECONFIG="$HOME/.kube/gpmidi-operator.kubeconfig"
kubectl config current-context
kubectl auth can-i create pipelineruns.tekton.dev -n gpmidi-ml
kubectl -n gpmidi-ml get pods
```

The older `generate-deployer-kubeconfig.sh` creates the separate
`gpmidi-deployer` identity for application-resource administration. It is not the
documented token for starting Tekton PipelineRuns.

If the ServiceAccount/RBAC is missing on a rebuilt cluster, an AILab administrator
must first apply `infra/ailab/base/rbac.yaml`. Do not grant Codex cluster-admin.

## 9. Credential and Secret inventory

No value in this section belongs in Git.

### 9.1 Gitea repository PAT

- Source: Gitea user settings at
  `http://192.168.30.2:3300/user/settings/applications`.
- Consumer: local Git credential manager.
- Minimum purpose: repository clone/fetch/push.
- Rotation: create a replacement token, update the credential manager, verify
  clone/push, revoke the old token.

### 9.2 Registry credential (`gitea-registry-auth`)

- Source: a dedicated Gitea token with package read/write permission.
- Consumer: Tekton build/audit tasks through a Docker config Secret.
- Kubernetes object: `gpmidi-ml/gitea-registry-auth`, type
  `kubernetes.io/dockerconfigjson`.
- Normal Codex work does not need to read this Secret or know its token.

Create/rotate on AILab as an administrator without placing the token in command
arguments:

```bash
set +o history
read -rsp 'Gitea registry token: ' GITEA_REGISTRY_TOKEN; printf '\n'
TMP_DOCKER_CONFIG=$(mktemp -d)
trap 'unset GITEA_REGISTRY_TOKEN; rm -rf "$TMP_DOCKER_CONFIG"' EXIT
printf '%s' "$GITEA_REGISTRY_TOKEN" | \
  DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker login 192.168.30.2:3300 \
    --username mgrigorov --password-stdin
sudo k3s kubectl -n gpmidi-ml create secret generic gitea-registry-auth \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$TMP_DOCKER_CONFIG/config.json" \
  --dry-run=client -o yaml | sudo k3s kubectl apply -f - >/dev/null
unset GITEA_REGISTRY_TOKEN
set -o history
```

Verify metadata/key presence only; do not decode it:

```bash
sudo k3s kubectl -n gpmidi-ml get secret gitea-registry-auth \
  -o jsonpath='{.metadata.name}{" type="}{.type}{" keys="}{range $k,$v := .data}{$k}{" "}{end}{"\n"}'
```

### 9.3 Gitea webhook shared secret (`gitea-webhook-secret`)

- Source: random high-entropy value generated by the operator.
- Consumers: Gitea repository webhook and Tekton's GitHub signature interceptor.
- Kubernetes object/key: `gpmidi-ml/gitea-webhook-secret`, key `secret`.
- Gitea webhook target: the current ClusterIP of the EventListener-owned Service
  `gpmidi-ml/el-gitea-listener`, port 8080.
- Events: push; branch accepted by the CEL filter is exactly `refs/heads/main`.

Generate once, store it in a password manager, apply it to Kubernetes and enter
the same value into the Gitea repository webhook UI. Never put it in a manifest.
After rotation, update both sides before expecting pushes to trigger.

Tekton Triggers owns and may recreate `Service/el-gitea-listener`; its ClusterIP
can therefore change. Gitea runs on the AILab host and can route to that Service
CIDR, but a stale URL silently stops push delivery. After installing/reconciling
the EventListener, or whenever a push produces no PipelineRun, run on AILab:

```bash
bash infra/ailab/scripts/sync-gitea-webhook.sh 1
```

The script resolves the live ClusterIP, reads the shared webhook Secret without
printing it, securely prompts for a Gitea repository token (or reads the path in
`GITEA_TOKEN_FILE`), and patches webhook ID 1. Then make/test a real accepted
`main` push and confirm a `gitea-push-main` PipelineRun appears. Never hard-code a
copied ClusterIP in documentation or automation.

### 9.4 Flask signing secret (`gpmidi-web-secret`)

- Source: `openssl rand -hex 32`.
- Consumer: gpmidi-web `SECRET_KEY`.
- Kubernetes object/key: `gpmidi-ml/gpmidi-web-secret`, key `SECRET_KEY`.

Safe creation on AILab:

```bash
TMP_ENV=$(mktemp)
trap 'rm -f "$TMP_ENV"' EXIT
umask 077
printf 'SECRET_KEY=%s\n' "$(openssl rand -hex 32)" > "$TMP_ENV"
sudo k3s kubectl -n gpmidi-ml create secret generic gpmidi-web-secret \
  --from-env-file="$TMP_ENV" --dry-run=client -o yaml \
  | sudo k3s kubectl apply -f - >/dev/null
rm -f "$TMP_ENV"
```

Changing it invalidates existing Flask sessions.

### 9.5 OpenAI token (`gpmidi-openai-secret`, optional)

- Source: `https://platform.openai.com/api-keys` under the authorized OpenAI
  project/account.
- Consumer: optional gpmidi-web arrangement draft feature.
- Kubernetes object/key: `gpmidi-ml/gpmidi-openai-secret`, key `OPENAI_TOKEN`.
- Local variable: `OPENAI_TOKEN`.
- The token is optional; baseline conversion must work without it.

Provision from an operator-controlled private terminal using a temporary mode-0600
file or stdin. Do not paste the token into Codex, Git, YAML, reports, process
arguments or shell history. Verify only that the Secret/key exists. A health check
may report configuration state but must not reveal the token or make a paid call.

### 9.6 Headlamp login token

This is a short-lived Kubernetes TokenRequest, not a stored static password:

```bash
sudo k3s kubectl -n headlamp create token headlamp-admin --duration=24h
```

Enter it only into Headlamp at `https://192.168.30.2/`. Mint a new one after
expiry; never save it in Git.

### 9.7 TLS key

`infra/ailab/scripts/ensure-ailab-tls.sh` generates/replicates the self-signed LAN
certificate Secret `ailab-tls`. The private key exists only in Kubernetes Secrets
and temporary host files cleaned by the script. Never export it into the repo.

## 10. Delivery paths

### 10.1 Preferred accepted-main path: signed Gitea webhook

1. Update and verify the code.
2. Commit and push the accepted commit to `main`.
3. Record the full SHA:

   ```bash
   SHA=$(git rev-parse HEAD)
   test "$SHA" = "$(git ls-remote origin refs/heads/main | cut -f1)"
   printf '%s\n' "$SHA"
   ```

4. Verify that the webhook created a new PipelineRun labelled
   `gpmidi.ailab/trigger=gitea-push-main`:

   ```bash
   kubectl -n gpmidi-ml get pipelineruns.tekton.dev \
     --sort-by=.metadata.creationTimestamp \
     -o custom-columns='NAME:.metadata.name,STATUS:.status.conditions[0].status,REASON:.status.conditions[0].reason,TRIGGER:.metadata.labels.gpmidi\.ailab/trigger'
   ```

5. Confirm that the run's `revision` and `expected-sha` are the exact pushed SHA.

Do not assume that a push triggered delivery. If no run appears, inspect the
EventListener and Gitea webhook delivery result/signature configuration. Do not
silently substitute an unrecorded workstation build.

### 10.2 Controlled manual exact-SHA path

Use this when explicitly validating/deploying an exact commit or when webhook
operation is being repaired. It labels evidence `manual-candidate`; it must never
be described as a webhook main-push run.

Prerequisites:

- exact 40-character commit SHA already present in Gitea;
- operator kubeconfig from section 8;
- current pinned CI image, normally
  `192.168.30.2:3300/mgrigorov/gpmidi-ci:pinned`.

Run from the repository checkout:

```bash
export KUBECONFIG="$HOME/.kube/gpmidi-operator.kubeconfig"
SHA=$(git rev-parse HEAD)
test ${#SHA} -eq 40
KUBECTL=kubectl bash infra/ailab/scripts/run-pipeline.sh ci \
  "$SHA" 192.168.30.2:3300/mgrigorov/gpmidi-ci:pinned
```

The script prints the generated PipelineRun name. Save it as `RUN`.

### 10.3 What the pipeline does

The `gpmidi-ci` pipeline:

1. clones the requested revision and fails unless checkout equals `expected-sha`;
2. records the pinned toolchain;
3. runs root, asset-api and reference-time tests;
4. runs Ruff, whitespace, shell, schema and manifest policy gates;
5. runs gitleaks and dependency/license review;
6. builds three Linux/amd64 images with Kaniko using exact-SHA tags;
7. resolves and audits each immutable digest;
8. scans each image with Trivy and blocks fixable CRITICAL findings;
9. deploys each application by digest, waits for rollout and compares pod
   `imageID` to the built digest;
10. runs live E2E and persistence/PVC checks;
11. archives evidence plus `SHA256SUMS` on `tekton-evidence`.

The pipeline is fail-closed: build/deploy stages depend on successful gates, and
the negative pipeline structurally has no deployment task.

## 11. Monitor and diagnose a PipelineRun

Status:

```bash
kubectl -n gpmidi-ml get "pipelinerun/$RUN" \
  -o custom-columns='NAME:.metadata.name,STATUS:.status.conditions[0].status,REASON:.status.conditions[0].reason,START:.status.startTime,END:.status.completionTime'
```

Task status:

```bash
kubectl -n gpmidi-ml get taskruns.tekton.dev \
  -l "tekton.dev/pipelineRun=$RUN" \
  -o custom-columns='NAME:.metadata.name,STATUS:.status.conditions[0].status,REASON:.status.conditions[0].reason'
```

Results after success:

```bash
kubectl -n gpmidi-ml get "pipelinerun/$RUN" \
  -o jsonpath='{range .status.results[*]}{.name}={.value}{"\n"}{end}'
```

For a failed TaskRun, identify its pod and list valid container names before
reading logs:

```bash
TASKRUN=<failed-taskrun>
POD=$(kubectl -n gpmidi-ml get pods -l "tekton.dev/taskRun=$TASKRUN" \
  -o jsonpath='{.items[0].metadata.name}')
kubectl -n gpmidi-ml get pod "$POD" \
  -o jsonpath='{.spec.initContainers[*].name}{"\n"}{.spec.containers[*].name}{"\n"}'
kubectl -n gpmidi-ml logs "$POD" -c <step-container>
```

If a run remains `PipelineRunPending`, inspect ResourceQuota, PVC count, pod
scheduling events and previous retained workspace PVCs. Do not delete all PVCs;
protect `project-assets`, `reference-time-data`, `tekton-evidence` and all other
known durable data. Retention is implemented in `tekton/pruning.yaml`.

## 12. Mandatory live verification

A green PipelineRun is necessary but still verify the facts directly:

```bash
export KUBECONFIG="$HOME/.kube/gpmidi-operator.kubeconfig"
NS=gpmidi-ml

for d in asset-api reference-time gpmidi-web; do
  kubectl -n "$NS" rollout status "deployment/$d" --timeout=300s
  kubectl -n "$NS" get "deployment/$d" \
    -o jsonpath='{.metadata.name}{" image="}{.spec.template.spec.containers[0].image}{"\n"}'
  kubectl -n "$NS" get pod -l "app=$d" \
    -o jsonpath='{.items[0].metadata.name}{" imageID="}{.items[0].status.containerStatuses[0].imageID}{" ready="}{.items[0].status.containerStatuses[0].ready}{"\n"}'
done

curl -skS --fail https://192.168.30.2/asset-api/healthz
curl -skS --fail https://192.168.30.2/reference-time/healthz
curl -skS --fail -H 'Host: gpmidi.ailab.local' https://192.168.30.2/healthz
```

For every application, the live pod `imageID` digest must equal the corresponding
PipelineRun result. A tag, rollout success or source environment variable alone
is not provenance proof.

For converter changes, also perform a real live conversion using an authorized,
untracked GP input and confirm the requested output path. Do not replace the
user's musical listening test with a synthetic metric.

## 13. Rollback

Authoritative rollback detail is in `infra/ailab/tekton/ROLLBACK.md`.

Use the PipelineRun's `*-previous-image` result as the first rollback candidate.
Set the Deployment back to the exact recorded digest, wait for rollout, then prove
the live pod `imageID` and service health. Example:

```bash
NS=gpmidi-ml
SERVICE=gpmidi-web
REPO=192.168.30.2:3300/mgrigorov/gpmidi-web
DIGEST=sha256:<recorded-last-known-good>

kubectl -n "$NS" set image "deployment/$SERVICE" \
  "$SERVICE=$REPO@$DIGEST"
kubectl -n "$NS" rollout status "deployment/$SERVICE" --timeout=300s
kubectl -n "$NS" get pod -l "app=$SERVICE" \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

Rollback never deletes/recreates the persistent PVCs, registry, evidence archive,
Gitea or GPU service. After emergency rollback, fix the source and run normal
exact-SHA delivery; do not leave production on an undocumented manual state.

## 14. CI image maintenance

The gate image is an internal tool image. Rebuild it on AILab only:

```bash
export KUBECONFIG="$HOME/.kube/gpmidi-operator.kubeconfig"
SHA=$(git rev-parse HEAD)
KUBECTL=kubectl bash infra/ailab/scripts/run-pipeline.sh ci-image "$SHA"
```

After successful review, an authorized registry operator may move the deliberate
`pinned` alias to that exact CI-image build. The floating alias is allowed only
for this internal gate image, never for delivered applications.

## 15. Rebuilding AILab from repository state

The durable desired state is rooted at `infra/ailab/kustomization.yaml`. Pinned
versions are in `infra/ailab/versions.env`. Installation scripts are idempotent
where documented; read each script before executing it.

High-level order on AILab:

1. k3s/storage/GPU baseline and namespace resources;
2. TLS and Traefik configuration (Traefik must bind 443 only; port 80 belongs to
   the host dashboard);
3. Tekton Pipelines/Triggers and `configure-tekton.sh`;
4. Gitea registry credential and signed webhook secret;
5. application Secrets;
6. Headlamp;
7. exact-SHA delivery through Tekton.

Do not run `kubectl apply -k infra/ailab` blindly against an unknown cluster.
First render, validate and diff the resources; application Deployments in Git may
contain an older reviewed digest while Tekton owns release-time image patches.
Never allow a broad apply to roll a live application backward unintentionally.

## 16. Current-state and documentation discipline

When a behaviour, dependency, deployment contract or credential location changes,
update documentation in the same change:

- `AGENTS.md` and `LESSONS.md` for durable musical/technical invariants;
- `docs/PROJECT_STATUS.md` for current accepted state and evidence;
- this guide for setup, operations, credentials and deployment;
- component README for local API/config changes;
- `infra/ailab/README.md` for cluster topology/version changes;
- rollback instructions for any release-mechanism change.

Never copy live secret values into a status report. Record object names, key names,
source/rotation procedure and verification method only.

## 17. Codex completion checklist

Before reporting completion:

- [ ] Read `CODEX.md`, `AGENTS.md`, `LESSONS.md` and relevant ADR/evidence.
- [ ] Work from current `origin/main`; state branch and exact SHA.
- [ ] Do not change musical semantics without explicit approval.
- [ ] Add/update tests first for behavioural defects.
- [ ] Run relevant local tests and static checks.
- [ ] Review `git diff --check` and scan for secrets/generated artifacts.
- [ ] Push the exact reviewed commit to Gitea.
- [ ] Deploy only through the exact-SHA Tekton path.
- [ ] Record PipelineRun name/status/results.
- [ ] Prove Deployment digest equals pod `imageID`.
- [ ] Verify live health and the feature's real path.
- [ ] Preserve PVCs, unrelated services and GPU profile.
- [ ] Update documentation so `main` remains sufficient for the next session.
- [ ] Return go/no-go and an exact digest rollback target.

## 18. Canonical references

- `AGENTS.md` — invariants and open musical issues.
- `LESSONS.md` — measured converter/humanization lessons.
- `docs/PROJECT_STATUS.md` — current milestone and evidence.
- `docs/Phase_2_Reference_Time_Vertical_Slice.md` — service/delivery design.
- `infra/ailab/README.md` — cluster topology and access.
- `infra/ailab/tekton/pipeline.yaml` — actual delivery graph.
- `infra/ailab/tekton/tasks.yaml` — actual gate/build/deploy commands.
- `infra/ailab/tekton/triggers.yaml` — webhook validation contract.
- `infra/ailab/tekton/ROLLBACK.md` — rollback/recovery.
- `infra/ailab/versions.env` — pinned infrastructure versions.
