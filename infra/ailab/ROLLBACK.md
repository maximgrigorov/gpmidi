# AILab k3s rollback

## What was installed

1. **k3s** `v1.36.2+k3s1` — single-node server, systemd service `k3s.service`
2. **NVIDIA device plugin** `v0.19.3` — DaemonSet in `kube-system`
3. **containerd config** — NVIDIA runtime template at `/data/k3s/agent/etc/containerd/config.toml.tmpl`
4. **Namespace `gpmidi-ml`** with RBAC, ResourceQuota, LimitRange, NetworkPolicy
5. **TLS secret** `ailab-tls` in `gpmidi-ml` (self-signed certificate)
6. **Smoke app deployment** in `gpmidi-ml`
7. **PVCs** in `gpmidi-ml`: `project-assets`, `smoke-pvc`
8. **Traefik HelmChartConfig** — disables port 80, keeps 443 only
9. **nginx `openclaw.conf`** removed (port 443 freed for k3s Traefik)

## Directories created

- `/data/k3s` — k3s data directory (images, containerd, etcd)
- `/data/k3s-storage` — local-path-provisioner storage

## How to rollback

### 1. Stop and uninstall k3s

```bash
sudo /usr/local/bin/k3s-uninstall.sh
```

This removes the k3s binary, systemd service, and all Kubernetes state.
It does NOT remove `/data/k3s` or `/data/k3s-storage`.

### 2. Remove data directories (optional)

```bash
sudo rm -rf /data/k3s
sudo rm -rf /data/k3s-storage
```

### 3. Restore containerd config

k3s-uninstall.sh removes everything under the k3s data dir.
Docker's containerd (`/etc/containerd/`) is not affected.

### 4. Restore nginx 443 (if needed)

If OpenClaw or another service needs port 443 via nginx:

```bash
# Re-create /etc/nginx/conf.d/openclaw.conf with the desired upstream
sudo nginx -t && sudo systemctl reload nginx
```

### 5. Preserved data

The following are NOT touched by rollback:

- `/data/models/` — ML model weights
- `/data/Music/`, `/data/music/` — audio data
- `/data/docker-volumes/` — Docker volume data
- `/data/backups/` — backup data
- All Docker containers (gitea, searxng, etc.)
- All systemd services (nginx, homepage-api, llama-cpp, etc.)
- NVIDIA host driver (590.48.01)
- nvidia-container-toolkit package
