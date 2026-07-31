# AILab k3s GPU Cluster

Single-node Kubernetes (k3s) cluster on AILab with NVIDIA GPU support
for the Reference-Guided MIDI Restoration pipeline.

## Quick reference

| Component | Version |
|-----------|---------|
| k3s | v1.36.2+k3s1 |
| containerd | 2.3.2-k3s2 |
| NVIDIA driver | 590.48.01 |
| CUDA (driver) | 13.1 |
| NVIDIA device plugin | v0.19.3 |
| Node | ailab (192.168.30.2) |
| Namespace | gpmidi-ml |
| Tekton Pipelines | v1.14.1 |
| Tekton Triggers | v0.36.0 |
| Registry | Gitea OCI, `192.168.30.2:3300` |

## Directory structure

```
infra/ailab/
├── README.md              # This file
├── versions.env           # Pinned versions
├── ROLLBACK.md            # How to remove everything
├── FLUX-BOOTSTRAP.md      # Flux CD setup (placeholder)
├── kustomization.yaml     # Buildable durable reconciliation root
├── traefik-config.yaml    # HelmChartConfig: 443 only
├── scripts/               # Idempotent host-side scripts
├── base/                  # Namespace, RBAC, quotas, policies
├── storage/               # StorageClass config, PVCs, smoke
├── gpu/                   # Device plugin, GPU smoke jobs
├── smoke-app/             # Test deployment (Phase 0)
├── tekton/                # Delivery pipeline, tasks, triggers, RBAC, retention
├── apps/asset-api/        # Phase 1 asset storage service
├── apps/reference-time/   # Phase 2 reference-time analysis service
├── apps/gpmidi-web/       # Flask converter + Projects UI (Phase 2)
├── clusters/ailab/        # Flux-generated files after bootstrap
├── apps/gpmidi-ml/        # Future application layer
└── reports/               # Verification reports
```

## Access

```bash
# From AILab host
sudo k3s kubectl get pods -n gpmidi-ml

# HTTPS smoke
curl -k https://192.168.30.2/healthz

# Scoped kubeconfig (generate first)
bash scripts/generate-deployer-kubeconfig.sh
KUBECONFIG=~/.kube/gpmidi-deployer.kubeconfig kubectl get pods -n gpmidi-ml
```

## Storage

All k3s data is on `/data` (separate NVMe, 544G free):

- `/data/k3s` — k3s data directory (etcd, containerd images)
- `/data/k3s-storage` — local-path-provisioner PVC data

Existing `/data` contents are not touched.

## GPU workload lifecycle

Docker/systemd GPU profiles and Kubernetes GPU pods share one physical GPU and
must be treated as mutually exclusive. The currently active profile is dynamic:
normally it is `llama-cpp` serving `qwen3-coder-next`, but it may be ComfyUI or
another model. Never hard-code the restore target.

Before a GPU-heavy Kubernetes job:

1. Read `http://192.168.30.2/api/stats` and record the active profile.
2. Through the homepage on port 80, or its documented API, switch to `none`.
3. Wait until the profile switch completes and GPU VRAM is released.
4. Run the Kubernetes GPU workload.
5. On success, failure, timeout, or cancellation, restore the profile recorded
   in step 1 and verify its health endpoint.

Manual API example (the response is an SSE stream):

```bash
curl -N -X POST http://192.168.30.2/api/profiles/switch \
  -H 'Content-Type: application/json' \
  -d '{"profile":"none"}'
# Run the GPU workload, then restore the profile that was active before it.
```

Do not start a Kubernetes GPU pod while a profile still owns VRAM. A previous
smoke run stopped llama.cpp because this lifecycle was not followed.

## Port map

| Port | Service | Notes |
|------|---------|-------|
| 80 | nginx homepage | unchanged |
| 443 | k3s Traefik HTTPS | new |
| 3300 | Gitea | unchanged |
| 6443 | k3s API | new |
| 8080 | llama.cpp | unchanged |


## Delivery

`infra/ailab/tekton/` holds the whole delivery path. Images are built on AILab by
kaniko for `linux/amd64`, published under an **immutable exact-SHA tag only**, and
deployed **by digest**; a deploy fails unless the live pod's `imageID` equals the
published digest. Nothing is built before every gate passes and nothing is
deployed before the image has been audited and scanned.

Every gate tool is pinned inside `tekton/Dockerfile.ci` — kubectl, kubeconform,
ShellCheck, gitleaks, trivy, crane, ruff — plus two Python environments, because
the converter and the services pin different numpy versions. That image is itself
built on AILab by the `gpmidi-ci-image` pipeline.

```bash
# Reviewed candidate validation (labelled manual-candidate, never "a main push")
bash infra/ailab/scripts/run-pipeline.sh ci        <full-sha> <ci-image>

# Rebuild the gate image
bash infra/ailab/scripts/run-pipeline.sh ci-image  <full-sha>

# Prove a failing gate cannot deploy
bash infra/ailab/scripts/run-pipeline.sh negative  <full-sha> <ci-image> [gate]
```

Production trigger: a Gitea webhook on `main`, authenticated by shared-secret
signature and validated for repository, ref and commit shape.

One-time cluster configuration:

```bash
bash infra/ailab/scripts/configure-tekton.sh   # disables co-scheduling
```

Namespace limits are real: `LimitRange` caps a pod at 10 CPU, `ResourceQuota` caps
namespace `limits.cpu` at 12, and the application pods hold about 5.7. Gate groups
and per-image build/audit pairs are therefore chained, not parallel.

Evidence for every run is archived on the `tekton-evidence` PVC with `SHA256SUMS`,
ten bundles retained. Rollback: `infra/ailab/tekton/ROLLBACK.md`.

## Secrets (never committed)

```bash
K="sudo k3s kubectl -n gpmidi-ml"
$K create secret generic gpmidi-web-secret --from-literal=SECRET_KEY="$(openssl rand -hex 32)"
$K create secret generic gitea-webhook-secret --from-literal=secret="$(openssl rand -hex 24)"
# gitea-registry-auth holds a docker config.json for 192.168.30.2:3300
```
