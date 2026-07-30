#!/usr/bin/env bash
set -Eeuo pipefail

# Configure NVIDIA container runtime for k3s containerd.
# NOTE: k3s v1.36.2+ auto-detects nvidia-container-runtime and adds
# the nvidia runtime to containerd config automatically. This script
# verifies the configuration rather than creating it.
# Usage: ssh mgrigorov@192.168.30.2 'bash -s' < configure-nvidia-runtime.sh

K3S_DATA_DIR="${K3S_DATA_DIR:-/data/k3s}"

if ! command -v nvidia-container-runtime &>/dev/null; then
    echo "ERROR: nvidia-container-runtime not found. Install nvidia-container-toolkit." >&2
    exit 1
fi

if ! command -v k3s &>/dev/null; then
    echo "ERROR: k3s not installed." >&2
    exit 1
fi

CONFIG="$K3S_DATA_DIR/agent/etc/containerd/config.toml"
if [ -f "$CONFIG" ]; then
    if grep -q 'nvidia-container-runtime' "$CONFIG"; then
        echo "NVIDIA runtime already configured in containerd (auto-detected by k3s)."
        grep -A2 'nvidia' "$CONFIG"
        exit 0
    fi
fi

echo "NVIDIA runtime not found in generated config."
echo "k3s v1.36.2+ should auto-detect nvidia-container-runtime."
echo "Verify nvidia-container-toolkit is installed: dpkg -l nvidia-container-toolkit"
echo "Then restart k3s: sudo systemctl restart k3s"
exit 1
