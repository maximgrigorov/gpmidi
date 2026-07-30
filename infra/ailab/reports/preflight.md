# AILab Preflight Report

**Date:** 2026-07-30

## System

| Parameter | Value |
|-----------|-------|
| Hostname | ailab |
| IP | 192.168.30.2 |
| OS | Ubuntu 24.04.4 LTS (Noble Numbat) |
| Kernel | 6.8.0-117-generic |
| CPU | Intel i5-13400F, 10 cores / 16 threads |
| RAM | 32 GB DDR5 |
| Swap | 8 GB (file) |
| cgroup | v2 (cgroup2fs) |
| AppArmor | loaded, 123 profiles |
| SELinux | not installed |

## GPU

| Parameter | Value |
|-----------|-------|
| GPU | NVIDIA GeForce RTX 5060 Ti |
| VRAM | 16311 MiB (16 GB) |
| Driver | 590.48.01 |
| CUDA (driver) | 13.1 |
| CUDA toolkit (nvcc) | 12.0 (host-installed, not used by k3s) |
| nvidia-container-toolkit | 1.19.0-1 (apt) |
| nvidia-container-runtime | /usr/bin/nvidia-container-runtime |

## Filesystems

| Mount | Type | Size | Used | Free | Use% |
|-------|------|------|------|------|------|
| / | ext4 (NVMe) | 937G | 318G | 572G | 36% |
| /data | ext4 (NVMe, separate disk) | 938G | 347G | 544G | 39% |

**Decision:** k3s data placed on `/data/k3s` (544G free on separate NVMe).

## /data top-level contents (preserved)

```
backups/  docker-volumes/  lost+found/  models/  music/  Music/
open-webui2/  photos-cache/  virtio-win.iso  vms/
```

## Container runtimes

| Runtime | Version | Status |
|---------|---------|--------|
| Docker | 29.3.1 | running |
| containerd (Docker) | 2.2.2 | running |
| k3s | not installed | — |
| kubectl | not installed | — |
| helm | not installed | — |

## Listening ports (pre-install)

| Port | Service |
|------|---------|
| 22 | SSH |
| 80 | nginx (homepage) |
| 443 | nginx (OpenClaw proxy — **removed**, OpenClaw no longer running) |
| 2222 | Gitea SSH |
| 3080 | Homepage API (localhost only) |
| 3300 | Gitea web |
| 8000 | python3 (unknown) |
| 8080 | llama.cpp (Docker) |
| 8081 | antiloop-proxy |
| 8765 | claude-consultant |
| 8888 | SearXNG (Docker) |
| 9090 | Prometheus/metrics (unknown) |

## Port conflicts resolved

- **443**: nginx openclaw.conf removed (user confirmed OpenClaw not running)
- **80**: remains nginx homepage; k3s Traefik configured to NOT bind port 80
- **6443**: was free, used by k3s API

## Firewall

- No ufw installed
- iptables: INPUT policy ACCEPT, FORWARD policy DROP (Docker managed)
- nftables: mangle/raw tables with libvirt rules only
- No firewalld

## Docker containers (pre-install)

| Container | Status |
|-----------|--------|
| llama-server | Up (healthy) |
| searxng | Up |
| gitea | Up |
| ollama | Exited |
| open-webui | Exited |
