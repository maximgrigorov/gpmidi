#!/usr/bin/env bash
set -euo pipefail

# DEPRECATED: This manual build-import workflow is superseded by the Tekton
# CI/CD pipeline (infra/ailab/tekton/). Images are now built by Kaniko inside
# the pipeline and pushed to the Gitea OCI registry. Use this script only for
# emergency offline recovery when the pipeline is unavailable.
#
# Original purpose: build the reference-time container image and import it
# into k3s containerd. Run on AILab host.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SERVICE_DIR="$REPO_ROOT/services/reference_time"

GIT_SHA=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
IMAGE_NAME="reference-time"
IMAGE_TAG="${IMAGE_NAME}:${GIT_SHA}"

echo "=== Building $IMAGE_TAG ==="
docker build -t "$IMAGE_TAG" "$SERVICE_DIR"

echo "=== Exporting image ==="
TMPTAR="/tmp/${IMAGE_NAME}-${GIT_SHA}.tar"
docker save "$IMAGE_TAG" -o "$TMPTAR"

echo "=== Importing into k3s containerd ==="
sudo k3s ctr images import "$TMPTAR"
rm -f "$TMPTAR"

echo "=== Updating deployment image ==="
DEPLOY_YAML="$REPO_ROOT/infra/ailab/apps/reference-time/deployment.yaml"
sed -i "s|image: ${IMAGE_NAME}:.*|image: ${IMAGE_TAG}|" "$DEPLOY_YAML"

echo "=== Done: $IMAGE_TAG ==="
echo "Next: kubectl apply -k infra/ailab/apps/reference-time/"
