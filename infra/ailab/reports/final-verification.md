# Final Verification Report

**Date:** 2026-07-30

## Cluster State

```
NAME    STATUS   ROLES           AGE   VERSION        INTERNAL-IP    EXTERNAL-IP
ailab   Ready    control-plane   24m   v1.36.2+k3s1   192.168.30.2   <none>
```

Container runtime: containerd://2.3.2-k3s2

## GPU

| Metric | Value |
|--------|-------|
| capacity | 1 |
| allocatable | 1 |
| nvidia-smi Job | Completed (PASS) |
| PyTorch Job | Completed (PASS) |

## Storage

| PVC | Status | Capacity |
|-----|--------|----------|
| smoke-pvc | Bound | 1Gi |
| project-assets | Pending (WaitForFirstConsumer) | 50Gi |

Persistence verified: writer → delete pod → reader (SHA-256 match).

## HTTPS Ingress

| Test | Result |
|------|--------|
| curl -k https://192.168.30.2/healthz | 200 OK |
| curl -k https://192.168.30.2/storage | 200 OK |
| openssl s_client SAN | IP:192.168.30.2, DNS:ailab.local, DNS:gpmidi-ml.ailab.local |
| From LAN (Cursor machine) | 200 OK |
| After rollout restart | 200 OK |
| After pod delete/recreate | 200 OK |

## RBAC

| Check | Expected | Actual |
|-------|----------|--------|
| create deployments in gpmidi-ml | yes | yes |
| get nodes (cluster-scoped) | no | no |
| list secrets in gpmidi-ml | no | no |
| exec in kube-system | no | no |

## Existing Services

| Service | Port | Status |
|---------|------|--------|
| Gitea | 3300 | 200 OK |
| llama.cpp | 8080 | OK (restarted after k3s GPU job) |
| nginx (homepage) | 80 | running |
| SearXNG | 8888 | running |

**Note:** llama-server (Docker) stopped during initial k3s GPU smoke job
execution (GPU resource contention). Restarted via `systemctl restart
llama-cpp.service` — back to healthy.

## /data Integrity

All pre-existing directories preserved:
```
backups  docker-volumes  lost+found  models  music  Music
open-webui2  photos-cache  virtio-win.iso  vms
```

New directories added: `k3s`, `k3s-storage`.

## Port Matrix

| Port | Service | Accessible from |
|------|---------|----------------|
| 22/tcp | SSH | LAN |
| 80/tcp | nginx homepage | LAN |
| 443/tcp | k3s Traefik (HTTPS) | LAN |
| 2222/tcp | Gitea SSH | LAN |
| 3300/tcp | Gitea web | LAN |
| 6443/tcp | k3s API | LAN (no additional firewall rules added) |
| 8080/tcp | llama.cpp | LAN |
| 8081/tcp | antiloop-proxy | LAN |
| 8765/tcp | claude-consultant | LAN |
| 8888/tcp | SearXNG | LAN |
| 10250 | kubelet | localhost+cluster |
| 10257-10259 | k8s control plane | localhost |

## Reboot Persistence (VERIFIED)

Server rebooted at 2026-07-30 22:37 UTC+5. All services recovered:

| Check | Result |
|-------|--------|
| nvidia-smi | RTX 5060 Ti, 590.48.01 |
| k3s service | active |
| Node | Ready |
| Device plugin | Running (1 restart) |
| nvidia.com/gpu | 1 allocatable |
| PVC smoke-pvc | Bound (data preserved) |
| Smoke app | Running (1 restart) |
| HTTPS healthz | 200 OK |
| Gitea 3300 | 200 OK |
| LLM health 8080 | OK |

## Still Not Verified

- **Flux**: controllers not installed (Gitea credentials not provisioned)
- **Port 6443 firewall**: no restriction added; accessible from LAN
- **GPU workload lifecycle**: before every k3s GPU job, record the currently
  active homepage profile, switch to `none`, wait for VRAM release, and restore
  that same profile in a finally/cleanup path. Concurrent Docker/k3s GPU use is
  unsupported on this host.
