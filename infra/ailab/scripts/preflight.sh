#!/usr/bin/env bash
set -Eeuo pipefail

# Preflight discovery for AILab k3s bootstrap.
# Collects system info and checks prerequisites.
# Usage: ssh mgrigorov@192.168.30.2 'bash -s' < preflight.sh

echo "=== OS ==="
cat /etc/os-release
echo "=== KERNEL ==="
uname -r
echo "=== HOSTNAME ==="
hostname
echo "=== CPU ==="
lscpu | head -20
echo "=== RAM ==="
free -h
echo "=== SWAP ==="
swapon --show
echo "=== CGROUP ==="
stat -fc %T /sys/fs/cgroup 2>/dev/null || true
echo "=== NVIDIA-SMI ==="
nvidia-smi
echo "=== NVIDIA DRIVER ==="
cat /proc/driver/nvidia/version 2>/dev/null || true
echo "=== CUDA TOOLKIT ==="
nvcc --version 2>/dev/null || echo "nvcc not in PATH"
echo "=== NVIDIA CONTAINER TOOLKIT ==="
dpkg -l nvidia-container-toolkit 2>/dev/null || echo "not installed"
echo "=== CONTAINERD ==="
containerd --version 2>/dev/null || echo "not found"
echo "=== DOCKER ==="
docker --version 2>/dev/null || echo "not found"
echo "=== K3S ==="
k3s --version 2>/dev/null || echo "not installed"
echo "=== KUBECTL ==="
kubectl version --client 2>/dev/null || echo "not installed"
echo "=== HELM ==="
helm version 2>/dev/null || echo "not installed"
echo "=== FILESYSTEMS ==="
df -hT / /var /data 2>/dev/null || true
echo "=== MOUNT OPTIONS ==="
mount | grep -E "on / |on /var |on /data " || true
echo "=== /data TOP LEVEL ==="
python3 - <<'PY'
from pathlib import Path

for path in sorted(Path("/data").iterdir(), key=lambda item: item.name)[:30]:
    print(path.name)
PY
echo "=== LISTENING PORTS ==="
ss -tlnp 2>/dev/null || true
echo "=== FIREWALL (iptables INPUT) ==="
sudo -n iptables -L INPUT -n 2>/dev/null | head -20 || echo "requires password"
echo "=== APPARMOR ==="
sudo -n aa-status 2>/dev/null | head -10 || echo "requires password"
echo "=== SELINUX ==="
getenforce 2>/dev/null || echo "not installed"
echo "=== ROUTES ==="
ip route show
echo "=== DNS ==="
resolvectl status 2>/dev/null | head -20 || cat /etc/resolv.conf
echo "=== DOCKER CONTAINERS ==="
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true
echo "=== PREFLIGHT COMPLETE ==="
