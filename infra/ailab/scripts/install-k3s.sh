#!/usr/bin/env bash
set -Eeuo pipefail

# Install k3s single-node on AILab.
# Prerequisites: sudo NOPASSWD, port 443 free, /data mounted.
# Usage: ssh mgrigorov@192.168.30.2 'bash -s' < install-k3s.sh

K3S_VERSION="${K3S_VERSION:-v1.36.2+k3s1}"
K3S_DATA_DIR="/data/k3s"

if command -v k3s &>/dev/null; then
    INSTALLED="$(k3s --version 2>/dev/null | head -1)"
    echo "k3s already installed: $INSTALLED"
    echo "Skipping installation. Remove k3s first if upgrade is intended."
    exit 0
fi

if [ ! -d /data ]; then
    echo "ERROR: /data not mounted" >&2
    exit 1
fi

if ss -tlnp | grep -q ':6443 '; then
    echo "ERROR: port 6443 already in use" >&2
    exit 1
fi

sudo mkdir -p "$K3S_DATA_DIR"

echo "Installing k3s $K3S_VERSION with data-dir=$K3S_DATA_DIR ..."

curl -sfL https://get.k3s.io | \
    INSTALL_K3S_VERSION="$K3S_VERSION" \
    sh -s - server \
    --data-dir "$K3S_DATA_DIR" \
    --tls-san "192.168.30.2" \
    --tls-san "ailab.local" \
    --write-kubeconfig-mode "0600"

echo "Waiting for k3s to be ready ..."
sleep 5

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

for i in $(seq 1 30); do
    if sudo k3s kubectl get --raw=/readyz &>/dev/null; then
        echo "k3s API ready after ${i}s"
        break
    fi
    sleep 2
done

sudo k3s kubectl get nodes -o wide
echo "k3s $K3S_VERSION installed successfully."
