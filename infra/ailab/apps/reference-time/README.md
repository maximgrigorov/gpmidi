# reference-time — AILab deployment

Kubernetes manifests for the reference-time analysis service.

## Resources

| File | Kind | Description |
|---|---|---|
| `deployment.yaml` | Deployment | Single replica, non-root, PVC-backed |
| `service.yaml` | Service | ClusterIP on port 8000 |
| `ingress.yaml` | Ingress | Traefik path prefix `/reference-time` |
| `networkpolicy.yaml` | NetworkPolicy | Ingress from Traefik, egress to asset-api |
| `pvc.yaml` | PVC | 5Gi `reference-time-data` for SQLite DB |
| `kustomization.yaml` | Kustomize | Resource list |

## Build and deploy

```bash
ssh mgrigorov@192.168.30.2
cd /tmp/gpmidi-build
git pull origin feat/reference-time-vertical-slice

cd services/reference_time
IMAGE_TAG=$(git rev-parse --short=12 HEAD)
sudo docker build -t reference-time:$IMAGE_TAG .
sudo docker save reference-time:$IMAGE_TAG | sudo k3s ctr images import -

sudo k3s kubectl -n gpmidi-ml set image deployment/reference-time \
  reference-time=reference-time:$IMAGE_TAG
sudo k3s kubectl -n gpmidi-ml rollout status deployment/reference-time
```

Or use the convenience script:

```bash
bash infra/ailab/scripts/build-import-reference-time.sh
```

## Rollback

```bash
# To a specific previous image
sudo k3s kubectl -n gpmidi-ml set image deployment/reference-time \
  reference-time=reference-time:<previous-tag>

# Complete removal
sudo k3s kubectl -n gpmidi-ml delete -f infra/ailab/apps/reference-time/
```

## Verify

```bash
curl -k https://192.168.30.2/reference-time/healthz
curl -k https://192.168.30.2/reference-time/readyz
```
