# asset-api Kubernetes Manifests

Deployment manifests for the `asset-api` service in namespace `gpmidi-ml`.

## Components

| File | Resource |
|------|----------|
| `deployment.yaml` | Deployment (1 replica, PSS restricted, read-only rootfs) |
| `service.yaml` | ClusterIP Service on port 8000 |
| `ingress.yaml` | HTTPS ingress at `/asset-api` via Traefik |
| `networkpolicy.yaml` | Allow ingress from kube-system (Traefik) |
| `kustomization.yaml` | Kustomize overlay |

## Deploy

```bash
# On AILab host:
bash infra/ailab/scripts/build-import-asset-api.sh
sudo k3s kubectl apply -k infra/ailab/apps/asset-api/
sudo k3s kubectl rollout status deployment/asset-api -n gpmidi-ml
```

## Verify

```bash
curl -k https://192.168.30.2/asset-api/healthz
curl -k https://192.168.30.2/asset-api/readyz
```

## Security

- Non-root (UID 1000)
- Read-only root filesystem
- No hostPath, no privileged, no hostNetwork
- automountServiceAccountToken: false
- Capabilities: drop ALL
- Seccomp: RuntimeDefault
- No GPU request
- NetworkPolicy: only Traefik (kube-system) can reach port 8000

## Storage

Uses PVC `project-assets` (50 GiB, RWO) mounted at `/var/lib/gpmidi`.
Single replica required while using SQLite + RWO PVC.

## Image

Image tag includes git SHA: `asset-api:<short-sha>`.
Delivered via `docker save` + `k3s ctr images import` (no registry push).
`imagePullPolicy: Never`.
