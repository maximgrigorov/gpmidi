#!/usr/bin/env bash
set -Eeuo pipefail

# Install the pinned Helm 3 client on AILab.
#
# k3s ships its own HelmChart/HelmChartConfig controller, but that controller is
# reserved for the components k3s itself bundles (Traefik). Charts this project
# installs are driven by a real Helm client so the release is inspectable with
# `helm list` / `helm get values` and upgradeable in place.
#
# The archive is verified against the checksum published next to it before
# anything is unpacked. Re-running with the pinned version already present is a
# no-op, so this is safe in a loop.
#
# Usage: ssh mgrigorov@192.168.30.2 'bash -s' < install-helm.sh

HELM_VERSION="${HELM_VERSION:-v3.21.3}"
ARCH="linux-amd64"
INSTALL_DIR="/usr/local/bin"
TARBALL="helm-${HELM_VERSION}-${ARCH}.tar.gz"
BASE_URL="https://get.helm.sh"

if command -v helm >/dev/null 2>&1; then
    current="$(helm version --short 2>/dev/null | sed 's/+.*//')"
    if [ "$current" = "$HELM_VERSION" ]; then
        echo "helm ${HELM_VERSION} already installed at $(command -v helm). Nothing to do."
        exit 0
    fi
    echo "helm ${current:-unknown} present; replacing with ${HELM_VERSION}."
fi

WORK_DIR="$(mktemp -d /tmp/helm-install-XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Downloading ${TARBALL} ..."
curl -fsSL -o "${WORK_DIR}/${TARBALL}" "${BASE_URL}/${TARBALL}"
curl -fsSL -o "${WORK_DIR}/${TARBALL}.sha256sum" "${BASE_URL}/${TARBALL}.sha256sum"

echo "Verifying checksum ..."
( cd "$WORK_DIR" && sha256sum -c "${TARBALL}.sha256sum" )

echo "Unpacking ..."
tar -xzf "${WORK_DIR}/${TARBALL}" -C "$WORK_DIR"
sudo install -m 0755 "${WORK_DIR}/${ARCH}/helm" "${INSTALL_DIR}/helm"

echo "Installed: $("${INSTALL_DIR}/helm" version --short)"
