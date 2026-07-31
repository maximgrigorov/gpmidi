#!/usr/bin/env bash
# Launch a Tekton PipelineRun on AILab from an exact commit SHA.
#
# The production path is a Gitea webhook on an accepted `main` push. This script
# exists for the *controlled manual validation of a feature candidate*, which must
# not pretend to be a main push: the resulting PipelineRun is labelled
# `gpmidi.ailab/trigger=manual-candidate` so evidence never misrepresents how it
# started.
#
# Usage:
#   run-pipeline.sh ci        <commit-sha> <ci-image>   # full delivery pipeline
#   run-pipeline.sh ci-image  <commit-sha>              # build the CI gate image
#   run-pipeline.sh negative  <commit-sha> <ci-image> [gate]
#
# Run on the AILab host. Requires sudo k3s kubectl.
set -euo pipefail

NAMESPACE=gpmidi-ml
REPO_URL=http://192.168.30.2:3300/mgrigorov/gpmidi.git
REGISTRY=192.168.30.2:3300
OWNER=mgrigorov

KUBECTL=${KUBECTL:-"sudo k3s kubectl"}

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

require_sha() {
  local sha=$1
  if ! printf '%s' "$sha" | grep -Eq '^[0-9a-f]{40}$'; then
    echo "ERROR: '$sha' is not a full 40-character commit SHA" >&2
    exit 1
  fi
}

submit() {
  local manifest=$1
  local name
  name=$($KUBECTL create -f "$manifest" -o name)
  echo "created: $name"
  printf '%s\n' "${name#*/}"
}

mode=${1:-}
case "$mode" in
  ci)
    sha=${2:-}; ci_image=${3:-}
    [ -n "$sha" ] && [ -n "$ci_image" ] || usage
    require_sha "$sha"
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    cat > "$tmp" <<EOF
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: gpmidi-ci-manual-
  namespace: ${NAMESPACE}
  labels:
    gpmidi.ailab/trigger: manual-candidate
spec:
  pipelineRef:
    name: gpmidi-ci
  params:
  - name: repo-url
    value: ${REPO_URL}
  - name: revision
    value: ${sha}
  - name: expected-sha
    value: ${sha}
  - name: registry
    value: ${REGISTRY}
  - name: registry-owner
    value: ${OWNER}
  - name: ci-image
    value: ${ci_image}
  - name: run-name
    value: manual-candidate
  taskRunTemplate:
    serviceAccountName: tekton-build
    podTemplate:
      hostAliases:
      - ip: 192.168.30.2
        hostnames: [server]
  taskRunSpecs:
  - pipelineTaskName: pvc-before
    serviceAccountName: tekton-deploy
  - pipelineTaskName: pvc-after
    serviceAccountName: tekton-deploy
  - pipelineTaskName: deploy-asset-api
    serviceAccountName: tekton-deploy
  - pipelineTaskName: deploy-reference-time
    serviceAccountName: tekton-deploy
  - pipelineTaskName: deploy-gpmidi-web
    serviceAccountName: tekton-deploy
  - pipelineTaskName: live-e2e
    serviceAccountName: tekton-e2e
  workspaces:
  - name: shared-workspace
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 4Gi
  - name: evidence
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 4Gi
  - name: evidence-archive
    persistentVolumeClaim:
      claimName: tekton-evidence
  - name: docker-config
    secret:
      secretName: gitea-registry-auth
  timeouts:
    pipeline: 2h
    tasks: 1h30m
EOF
    submit "$tmp"
    ;;

  ci-image)
    sha=${2:-}
    [ -n "$sha" ] || usage
    require_sha "$sha"
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    cat > "$tmp" <<EOF
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: gpmidi-ci-image-
  namespace: ${NAMESPACE}
  labels:
    gpmidi.ailab/trigger: manual-candidate
spec:
  pipelineRef:
    name: gpmidi-ci-image
  params:
  - name: repo-url
    value: ${REPO_URL}
  - name: revision
    value: ${sha}
  - name: registry
    value: ${REGISTRY}
  - name: registry-owner
    value: ${OWNER}
  taskRunTemplate:
    serviceAccountName: tekton-build
    podTemplate:
      hostAliases:
      - ip: 192.168.30.2
        hostnames: [server]
  workspaces:
  - name: shared-workspace
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 4Gi
  - name: docker-config
    secret:
      secretName: gitea-registry-auth
  timeouts:
    pipeline: 1h
    tasks: 50m
EOF
    submit "$tmp"
    ;;

  negative)
    sha=${2:-}; ci_image=${3:-}; gate=${4:-pytest}
    [ -n "$sha" ] && [ -n "$ci_image" ] || usage
    require_sha "$sha"
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    cat > "$tmp" <<EOF
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: gpmidi-ci-negative-
  namespace: ${NAMESPACE}
  labels:
    gpmidi.ailab/trigger: negative-proof
spec:
  pipelineRef:
    name: gpmidi-ci-negative
  params:
  - name: repo-url
    value: ${REPO_URL}
  - name: revision
    value: ${sha}
  - name: ci-image
    value: ${ci_image}
  - name: fail-gate
    value: ${gate}
  taskRunTemplate:
    serviceAccountName: tekton-build
    podTemplate:
      hostAliases:
      - ip: 192.168.30.2
        hostnames: [server]
  workspaces:
  - name: shared-workspace
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 4Gi
  - name: evidence
    volumeClaimTemplate:
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 2Gi
  timeouts:
    pipeline: 45m
    tasks: 40m
EOF
    submit "$tmp"
    ;;

  *)
    usage
    ;;
esac
