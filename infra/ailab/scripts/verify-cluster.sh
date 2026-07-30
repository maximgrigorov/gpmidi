#!/usr/bin/env bash
set -Eeuo pipefail

# Final verification of AILab k3s cluster.
# Usage: ssh mgrigorov@192.168.30.2 'bash -s' < verify-cluster.sh

echo "=== NODES ==="
sudo k3s kubectl get nodes -o wide

echo "=== ALL PODS ==="
sudo k3s kubectl get pods -A -o wide

echo "=== GPU CAPACITY ==="
sudo k3s kubectl get node -o jsonpath='{.items[0].status.capacity.nvidia\.com/gpu}'
echo ""

echo "=== GPU ALLOCATABLE ==="
sudo k3s kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'
echo ""

echo "=== STORAGE ==="
sudo k3s kubectl get sc,pv,pvc -A -o wide

echo "=== INGRESS ==="
sudo k3s kubectl get ingress -A

echo "=== RBAC: deployer can create deployments ==="
sudo k3s kubectl auth can-i --as=system:serviceaccount:gpmidi-ml:gpmidi-deployer create deployments -n gpmidi-ml

echo "=== RBAC: deployer cannot get nodes ==="
sudo k3s kubectl auth can-i --as=system:serviceaccount:gpmidi-ml:gpmidi-deployer get nodes || true

echo "=== GPU SMOKE LOGS ==="
sudo k3s kubectl logs job/gpu-nvidia-smi-smoke -n gpmidi-ml 2>/dev/null || echo "job not found"

echo "=== PYTORCH SMOKE LOGS ==="
sudo k3s kubectl logs job/gpu-pytorch-smoke -n gpmidi-ml 2>/dev/null || echo "job not found"

echo "=== HTTPS HEALTHZ ==="
curl -kfsS https://192.168.30.2/healthz 2>/dev/null || echo "HTTPS healthz failed"

echo "=== HTTPS STORAGE ==="
curl -kfsS https://192.168.30.2/storage 2>/dev/null || echo "HTTPS storage failed"

echo "=== EXISTING SERVICES ==="
echo "Gitea 3300:"
curl -fsS --connect-timeout 5 http://localhost:3300/ -o /dev/null -w '%{http_code}' 2>/dev/null || echo "FAIL"
echo ""
echo "LLM health 8080:"
curl -fsS --connect-timeout 5 http://localhost:8080/health 2>/dev/null || echo "FAIL"
echo ""

echo "=== VERIFICATION COMPLETE ==="
