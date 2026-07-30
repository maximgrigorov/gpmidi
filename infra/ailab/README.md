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
├── apps/asset-api/        # Phase 1 asset storage service
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
