# Tekton delivery: rollback and recovery

Delivery is by **immutable OCI digest**. A rollback therefore means pointing a
Deployment back at a digest that is already in the registry, and proving the
running pod really is that digest. Nothing about a rollback touches
PersistentVolumeClaims: the `tekton-deploy` Role has no `delete` verb at all, so
neither a deploy nor a rollback can remove Project or analysis data.

## 1. Find the rollback target

Every `deploy-by-digest` task records the image it replaced as the task result
`previous-image`, and writes it into the evidence bundle:

```bash
sudo k3s kubectl -n gpmidi-ml get pipelinerun <RUN> \
  -o jsonpath='{range .status.results[*]}{.name}={.value}{"\n"}{end}'
```

The archived bundle holds the same facts per service:

```bash
sudo find /data/k3s-storage -path '*tekton-evidence*' -name 'deploy-*.txt' | head
```

List what the durable registry actually holds — a rollback target that is not in
the registry is not a rollback target:

```bash
TOKEN=$(cat ~/.gitea-token)
curl -s -u "mgrigorov:$TOKEN" \
  http://192.168.30.2:3300/v2/mgrigorov/reference-time/tags/list | jq .
```

Resolve a tag to its digest before using it:

```bash
crane digest --insecure 192.168.30.2:3300/mgrigorov/reference-time:<SHA>
```

## 2. Roll back by digest

```bash
NS=gpmidi-ml
REPO=192.168.30.2:3300/mgrigorov/reference-time
DIGEST=sha256:...            # the recorded last-known-good digest

sudo k3s kubectl -n $NS set image deployment/reference-time \
  reference-time=$REPO@$DIGEST
sudo k3s kubectl -n $NS rollout status deployment/reference-time --timeout=300s
```

## 3. Prove the rollback

Readiness is not proof. Compare the live pod's `imageID` with the digest you
asked for:

```bash
sudo k3s kubectl -n $NS get pod -l app=reference-time \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

Then confirm the data survived and the service works:

```bash
# PVC identities must be unchanged (same UID, same PersistentVolume)
sudo k3s kubectl -n $NS get pvc project-assets reference-time-data \
  -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,VOL:.spec.volumeName,PHASE:.status.phase

# Project and analysis data still readable
curl -sk https://192.168.30.2/asset-api/v1/projects | jq '.projects | length'
curl -sk https://192.168.30.2/reference-time/readyz

# A previously published analysis is still retrievable
curl -sk "https://192.168.30.2/reference-time/v1/projects/<PROJECT>/analyses" \
  | jq '.analyses | length'
```

## 4. `kubectl rollout undo`

Kubernetes keeps the previous ReplicaSet, so this also works and is faster:

```bash
sudo k3s kubectl -n gpmidi-ml rollout undo deployment/reference-time
sudo k3s kubectl -n gpmidi-ml rollout status deployment/reference-time
```

It is less explicit than setting the digest, because the target is "whatever the
previous ReplicaSet used" rather than a digest you named and recorded. Prefer
step 2 when you have the digest.

## 5. Restore the reviewed candidate

Rolling forward again is the same operation with the newer digest, followed by
the same imageID proof.

## What a rollback never does

- delete or recreate the `project-assets` (50 GiB) or `reference-time-data`
  (5 GiB) PVCs — the deploy identity cannot delete a PVC;
- touch Gitea, its OCI registry contents, or the evidence archive;
- change the GPU profile or any LLM service, which live outside `gpmidi-ml`.

## Interrupted analyses after a restart

A rollback restarts the analyzer pod. On startup the service terminally recovers
every orphaned `queued` and `running` job as `interrupted` with
`error_code=process_restart`, and never touches published results. Re-requesting
the same inputs is therefore a cache hit; re-requesting an analysis that had not
finished creates a new job.

## Emergency only: manual image build

If both Tekton and the registry are unavailable:

```bash
# On the AILab host only. NOT a delivery path.
cd /home/mgrigorov/phase2-work
bash infra/ailab/scripts/build-import-reference-time.sh
```

This uses `docker build` plus `k3s ctr images import` and produces an image with
no pushed digest, no provenance and no scan, so nothing built this way is
acceptable delivery evidence. Recover the pipeline and rebuild.

## Rebuilding the CI gate image

The gate image is built on AILab by its own pipeline, never on a workstation:

```bash
cd /home/mgrigorov/phase2-work
bash infra/ailab/scripts/run-pipeline.sh ci-image <full-sha>

# Move the `pinned` alias onto the reviewed build. `pinned` is a deliberate
# floating alias for an internal *tool* image, never for a delivered artifact.
crane tag --insecure \
  192.168.30.2:3300/mgrigorov/gpmidi-ci:<full-sha> pinned
```
