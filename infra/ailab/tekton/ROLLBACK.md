# Rollback Instructions

## Quick rollback to a previous image

Every successful pipeline run pushes both a SHA-tagged and `:latest` image.
To roll back a service to a known-good commit SHA:

```bash
# Find available image tags in the Gitea OCI registry
curl -s -u "$USER:$TOKEN" \
  http://192.168.30.2:3300/v2/mgrigorov/reference-time/tags/list | jq .

# Roll back to a specific SHA tag (does NOT touch PVC data)
kubectl -n gpmidi-ml set image deployment/reference-time \
  reference-time=192.168.30.2:3300/mgrigorov/reference-time:<GOOD_SHA>
kubectl -n gpmidi-ml rollout status deployment/reference-time --timeout=120s

# Same for asset-api
kubectl -n gpmidi-ml set image deployment/asset-api \
  asset-api=192.168.30.2:3300/mgrigorov/asset-api:<GOOD_SHA>
kubectl -n gpmidi-ml rollout status deployment/asset-api --timeout=120s
```

## Using kubectl rollout undo

Kubernetes keeps the previous ReplicaSet by default:

```bash
# Undo the last deployment update
kubectl -n gpmidi-ml rollout undo deployment/reference-time
kubectl -n gpmidi-ml rollout undo deployment/asset-api

# Verify
kubectl -n gpmidi-ml rollout status deployment/reference-time
kubectl -n gpmidi-ml get pods -l app=reference-time -o wide
```

## What rollback does NOT touch

- `project-assets` PVC (Asset API uploads, projects, metadata)
- `reference-time-data` PVC (SQLite DB with analysis jobs/results)
- Gitea repository and OCI registry contents
- GPU/LLM services (separate namespace and deployment)

## Verifying rollback

```bash
# Check the running image
kubectl -n gpmidi-ml get pod -l app=reference-time \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'

# Health check
curl -s http://192.168.30.2:8000/reference-time/healthz
curl -s http://192.168.30.2:8000/asset-api/healthz
```

## Emergency: manual image build (deprecated path)

If the Tekton pipeline and Gitea OCI registry are both unavailable:

```bash
# On AILab host only
cd /home/mgrigorov/gpmidi
bash infra/ailab/scripts/build-import-reference-time.sh
kubectl apply -k infra/ailab/apps/reference-time/
```

This uses `docker build` + `k3s ctr images import` and is a last resort.
