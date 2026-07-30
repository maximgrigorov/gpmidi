#!/usr/bin/env bash
set -euo pipefail

# Build the asset-api container image and import it into k3s containerd.
# Run on AILab host. Uses git SHA as tag; no registry push needed.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SERVICE_DIR="$REPO_ROOT/services/asset_api"

GIT_SHA=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
IMAGE_NAME="asset-api"
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
DEPLOY_YAML="$REPO_ROOT/infra/ailab/apps/asset-api/deployment.yaml"
sed -i "s|image: ${IMAGE_NAME}:.*|image: ${IMAGE_TAG}|" "$DEPLOY_YAML"

echo "=== Done: $IMAGE_TAG ==="
echo "Next: kubectl apply -k infra/ailab/apps/asset-api/"
