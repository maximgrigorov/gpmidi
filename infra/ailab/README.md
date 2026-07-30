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
├── traefik-config.yaml    # HelmChartConfig: 443 only
├── scripts/               # Idempotent host-side scripts
├── base/                  # Namespace, RBAC, quotas, policies
├── storage/               # StorageClass config, PVCs, smoke
├── gpu/                   # Device plugin, GPU smoke jobs
├── smoke-app/             # Test deployment
├── clusters/ailab/        # Flux entrypoint (Kustomize root)
├── apps/gpmidi-ml/        # Application layer
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

## GPU coexistence

The GPU is shared between Docker services (llama.cpp) and k3s pods.
Before running GPU-heavy ML jobs in k3s, stop the Docker GPU service:

```bash
sudo ~/homepage/scripts/gpu-switch.sh none
```

After ML work, restore:

```bash
sudo ~/homepage/scripts/gpu-switch.sh llama-cpp
```

## Port map

| Port | Service | Notes |
|------|---------|-------|
| 80 | nginx homepage | unchanged |
| 443 | k3s Traefik HTTPS | new |
| 3300 | Gitea | unchanged |
| 6443 | k3s API | new |
| 8080 | llama.cpp | unchanged |
