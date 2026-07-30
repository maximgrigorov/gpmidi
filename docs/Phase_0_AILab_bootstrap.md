# Phase 0: AILab k3s GPU Bootstrap — Отчёт

**Дата:** 2026-07-30
**Ветка:** `infra/ailab-k3s-bootstrap`
**Оператор:** Cursor Agent (Opus 4.6)

---

## 1. Что обнаружено до установки

### Система

| Параметр | Значение |
|----------|----------|
| ОС | Ubuntu 24.04.4 LTS (Noble Numbat) |
| Ядро | 6.8.0-117-generic |
| CPU | Intel i5-13400F, 10 ядер / 16 потоков |
| RAM | 32 GB DDR5 |
| Swap | 8 GB (file) |
| cgroup | v2 |
| AppArmor | loaded (123 profiles) |

### GPU

| Параметр | Значение |
|----------|----------|
| GPU | NVIDIA GeForce RTX 5060 Ti (16 GB VRAM) |
| Driver | 590.48.01 |
| CUDA (driver) | 13.1 |
| nvidia-container-toolkit | 1.19.0-1 (уже установлен) |

### Диски

| Точка | FS | Размер | Свободно |
|-------|----|--------|----------|
| / | ext4 (NVMe) | 937G | 572G |
| /data | ext4 (NVMe, отдельный диск) | 938G | 544G |

**Решение:** k3s data (`/data/k3s`) и storage provisioner (`/data/k3s-storage`)
размещены на `/data` — отдельный NVMe с 544G свободного места. Существующие
каталоги (`models/`, `Music/`, `docker-volumes/` и др.) не затронуты.

### Порты и конфликты

- **Port 80** — nginx (homepage dashboard) → **оставлен за nginx**, k3s Traefik
  не биндит port 80 (HelmChartConfig `ports.web.expose.default: false`)
- **Port 443** — nginx (OpenClaw proxy, `server_name 192.168.50.22`) →
  **удалён** по согласованию с пользователем (OpenClaw не работает).
  Port 443 передан k3s Traefik
- **Port 6443** — свободен → k3s API
- **Port 3300** — Gitea (Docker) → не затронут
- **Port 8080** — llama.cpp (Docker) → не затронут

### Удалённый AGENTS.md

Прочитан полностью. Обнаружены правила:
- GPU-сервисы взаимоисключающие (gpu-switch.sh профили)
- Gitea на 3300, llama.cpp на 8080
- nginx на 80/443 (OpenClaw прокси убран)
- Противоречий с заданием не выявлено

### Firewall

- ufw не установлен
- iptables: INPUT ACCEPT, FORWARD DROP (Docker managed)
- nftables: только libvirt mangle rules
- **Дополнительные правила не добавлялись** — не было необходимости,
  INPUT policy ACCEPT

---

## 2. Что установлено и какими версиями

| Компонент | Версия | Способ |
|-----------|--------|--------|
| k3s | v1.36.2+k3s1 | `curl -sfL https://get.k3s.io` с INSTALL_K3S_VERSION |
| containerd (k3s embedded) | 2.3.2-k3s2 | входит в k3s |
| Traefik (k3s bundled) | v40.x (Helm chart) | входит в k3s |
| NVIDIA device plugin | v0.19.3 | DaemonSet manifest |
| NVIDIA container runtime | auto-detected | k3s auto-config при наличии nvidia-container-toolkit |

### Host-side изменения

1. `/etc/nginx/conf.d/openclaw.conf` → переименован в `.bak`, nginx reloaded
2. `/data/k3s/` — каталог данных k3s (etcd, containerd images)
3. `/data/k3s-storage/` — каталог для local-path-provisioner PVC
4. k3s.service (systemd, enabled) — автостарт после reboot
5. `/usr/local/bin/k3s`, `/usr/local/bin/kubectl` (symlink)

### Kubernetes ресурсы

- Namespace `gpmidi-ml` с Pod Security Standards `restricted`
- ResourceQuota: 8 CPU req, 16Gi mem req, 1 GPU, 20 pods
- LimitRange: default 500m/512Mi, max 8/16Gi per container
- NetworkPolicy: default-deny + allow DNS/ingress/registries
- ServiceAccount `gpmidi-app`, `gpmidi-deployer`
- Role/RoleBinding `gpmidi-deployer` (namespace-scoped)
- StorageClass `local-path` переконфигурирован на `/data/k3s-storage`
- PVC `smoke-pvc` (1Gi, Bound), `project-assets` (50Gi, Pending/WaitForFirstConsumer)
- TLS Secret `ailab-tls` (self-signed, 365 days)
- Ingress `smoke-app` на 443 с TLS
- Deployment `smoke-app` (Python HTTP server via ConfigMap)

---

## 3. Какие файлы/commits созданы

Все воспроизводимые файлы в `infra/ailab/`:

```
infra/ailab/
├── README.md
├── versions.env
├── ROLLBACK.md
├── FLUX-BOOTSTRAP.md
├── traefik-config.yaml
├── scripts/
│   ├── preflight.sh
│   ├── install-k3s.sh
│   ├── configure-nvidia-runtime.sh
│   ├── generate-selfsigned-cert.sh
│   ├── verify-cluster.sh
│   └── generate-deployer-kubeconfig.sh
├── base/
│   ├── namespace.yaml
│   ├── resource-quota.yaml
│   ├── limit-range.yaml
│   ├── network-policy.yaml
│   ├── rbac.yaml
│   └── kustomization.yaml
├── storage/
│   ├── local-path-config.yaml
│   ├── project-assets-pvc.yaml
│   ├── persistence-smoke.yaml
│   └── kustomization.yaml
├── gpu/
│   ├── device-plugin.yaml
│   ├── nvidia-smi-job.yaml
│   ├── pytorch-smoke-job.yaml
│   └── kustomization.yaml
├── smoke-app/
│   ├── deployment.yaml  (includes ConfigMap + Deployment)
│   ├── service.yaml
│   ├── ingress.yaml
│   └── kustomization.yaml
├── clusters/ailab/
│   └── kustomization.yaml
├── apps/gpmidi-ml/
│   └── kustomization.yaml
└── reports/
    ├── preflight.md
    ├── gpu-smoke.md
    ├── storage-smoke.md
    ├── ingress-smoke.md
    └── final-verification.md
```

Ветка: `infra/ailab-k3s-bootstrap` (не merge в main).

---

## 4. Фактические результаты smoke тестов

### GPU / nvidia-smi

```
NVIDIA GeForce RTX 5060 Ti | Driver 590.48.01 | CUDA 13.1
VRAM: 16311 MiB | Status: Completed (PASS)
```

### PyTorch CUDA

```
PyTorch version: 2.7.1+cu128
CUDA available: True
Device: NVIDIA GeForce RTX 5060 Ti
Max matmul diff: 9.92e-05
Peak VRAM: 20.1 MB
PYTORCH CUDA SMOKE: PASS
```

### PVC Persistence

```
Writer → marker smoke-720e09be-537e-4a82-af35-049912cd5027
         SHA256: 449270121a504970c812aa6110e68cda592088429525482d36ee18feb0e5076c
Reader → SHA256 match: PERSISTENCE SMOKE: PASS
```

### HTTPS Ingress

```
curl -kfsS https://192.168.30.2/healthz
→ {"status": "ok", "version": "0.1.0", "hostname": "smoke-app-..."}

curl -kfsS https://192.168.30.2/storage
→ {"marker_sha256": "449270...", "status": "ok"}

openssl s_client SAN:
→ IP:192.168.30.2, DNS:ailab.local, DNS:gpmidi-ml.ailab.local
```

Протестировано: с AILab host, с Cursor машины (LAN), после rollout restart,
после delete/recreate pod.

### RBAC

| Проверка | Ожидание | Факт |
|----------|----------|------|
| create deployments -n gpmidi-ml | yes | **yes** |
| get nodes | no | **no** |
| list secrets -n gpmidi-ml | no | **no** |
| exec pods -n kube-system | no | **no** |

---

## 5. Какие порты доступны и откуда

| Port | Сервис | Доступ |
|------|--------|--------|
| 22/tcp | SSH | LAN |
| 80/tcp | nginx (homepage) | LAN |
| 443/tcp | k3s Traefik (HTTPS ingress) | LAN |
| 2222/tcp | Gitea SSH | LAN |
| 3300/tcp | Gitea web | LAN |
| 6443/tcp | k3s API | LAN (no explicit restriction) |
| 8080/tcp | llama.cpp | LAN |
| 8081/tcp | antiloop-proxy | LAN |
| 8765/tcp | claude-consultant | LAN |
| 8888/tcp | SearXNG | LAN |
| 10250/tcp | kubelet | localhost + cluster |
| 10257-10259 | k8s control plane | localhost |

Firewall rules **не менялись**. INPUT policy ACCEPT, что означает все порты
доступны из LAN. k3s API (6443) доступен по LAN; для ограничения потребуется
добавить iptables/ufw правила.

---

## 6. Подтверждение сохранности данных и сервисов

### /data

Все существующие каталоги сохранены:
```
backups  docker-volumes  lost+found  models  music  Music
open-webui2  photos-cache  virtio-win.iso  vms
```

Добавлены: `k3s/`, `k3s-storage/`. chown/chmod рекурсивно не выполнялись.

### Gitea (3300)

```
curl -fsS http://localhost:3300/ → HTTP 200
```

### LLM API (8080)

```
curl -fsS http://localhost:8080/health → {"status":"ok"}
```

**Замечание:** llama-server (Docker) остановился во время первого запуска
GPU smoke job (ресурсное конкурирование за GPU). Перезапущен через
`sudo systemctl restart llama-cpp.service` — работает штатно.

---

## 7. Что осталось непроверенным или заблокированным

### Reboot Persistence (ПРОВЕРЕНО)

Сервер перезагружен, все сервисы восстановились автоматически:

| Проверка | Результат |
|----------|-----------|
| nvidia-smi | RTX 5060 Ti, driver 590.48.01 |
| k3s service | active |
| Node | Ready |
| NVIDIA device plugin | Running (1 restart, нормально) |
| nvidia.com/gpu allocatable | 1 |
| PVC smoke-pvc | Bound (данные сохранились) |
| Smoke app | Running (1 restart, нормально) |
| HTTPS healthz | 200 OK |
| Gitea 3300 | 200 OK |
| LLM health 8080 | OK |

### Оставшиеся непроверенные пункты

1. **Port 6443 firewall** — k3s API доступен всему LAN, ограничение не
   добавлено (INPUT policy ACCEPT, нет ufw)
2. **Flux reconciliation** — контроллеры не установлены, нет Gitea token

### Заблокировано

1. **Flux bootstrap** — требует Gitea personal access token. Manifests
   подготовлены в `infra/ailab/clusters/ailab/` и `infra/ailab/apps/gpmidi-ml/`.
   Bootstrap command в `FLUX-BOOTSTRAP.md`
2. **Custom smoke-app image** — Gitea container registry не настроен для push.
   Используется `python:3.12-slim` с ConfigMap-mounted Python script
3. **GPU workload lifecycle** — перед каждым GPU-heavy workload нужно через
   homepage/API запомнить активный профиль, переключить его на `none`, дождаться
   освобождения VRAM и обязательно восстановить тот же профиль в cleanup-path.

### Известные ограничения RBAC

- Kubernetes RBAC не может ограничить hostPath в pod spec на уровне Role —
  это контролируется Pod Security Standards (`restricted` enforcement на
  namespace). PSS `restricted` запрещает hostPath volumes
- `gpmidi-deployer` не может читать Secrets (по design), но может создавать
  Deployments/Jobs. Kubernetes RBAC и PSS не запрещают такому workload
  смонтировать уже известный Secret из того же namespace. Поэтому namespace
  нельзя считать изолированным от самого deployer: в `gpmidi-ml` допустимы
  только те Secrets, доступ к которым разрешён владельцу deployer credential.
  Для более строгой границы нужен отдельный namespace и/или admission policy.

---

## 8. Как откатить изменения

### Полное удаление k3s

```bash
sudo /usr/local/bin/k3s-uninstall.sh
```

### Удаление данных (опционально)

```bash
sudo rm -rf /data/k3s /data/k3s-storage
```

### Восстановление nginx 443 (если нужно)

```bash
sudo mv /etc/nginx/conf.d/openclaw.conf.bak /etc/nginx/conf.d/openclaw.conf
sudo nginx -t && sudo systemctl reload nginx
```

### Что сохраняется при rollback

- Все данные в `/data` (кроме `/data/k3s` и `/data/k3s-storage`)
- Docker контейнеры (gitea, searxng, etc.)
- Все systemd сервисы
- NVIDIA driver и nvidia-container-toolkit
- nginx homepage

Подробнее: `infra/ailab/ROLLBACK.md`

---

## 9. Branch / commit SHA

- **Ветка:** `infra/ailab-k3s-bootstrap`
- **Не merge в main** — готова для code review
- **Секреты в git:** отсутствуют (kubeconfig, TLS keys, tokens не коммитятся)

---

## Приложение: Обнаруженные и решённые проблемы

### 1. Port 443 конфликт

**Проблема:** nginx слушал на 443 (OpenClaw proxy, `server_name 192.168.50.22`).

**Решение:** По согласованию с пользователем — OpenClaw не работает, nginx
конфиг удалён (`openclaw.conf` → `openclaw.conf.bak`), port 443 передан
k3s Traefik.

### 2. containerd config template (v2 incompatibility)

**Проблема:** Первоначальный containerd config template использовал v1 plugin
paths (`io.containerd.grpc.v1.cri`), несовместимые с containerd v2.3.2 в
k3s v1.36.2. k3s не запускался.

**Решение:** k3s v1.36.2 **автоматически обнаруживает** nvidia-container-runtime
и добавляет nvidia runtime в containerd config. Кастомный template не нужен.
Генерированный config использует `io.containerd.cri.v1.runtime` (v3 format).

### 3. Pod Security Standards и GPU jobs

**Проблема:** Namespace `gpmidi-ml` с PSS `restricted` enforcement блокировал
GPU jobs без security context.

**Решение:** Добавлены `runAsNonRoot`, `seccompProfile: RuntimeDefault`,
`allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]` ко всем
job templates. GPU доступ работает через device plugin volumes, не требует
привилегий.

### 4. LimitRange и PyTorch memory

**Проблема:** LimitRange default limit 512Mi, PyTorch job request 2Gi >
default limit → pod creation rejected.

**Решение:** Добавлены explicit limits (4Gi memory, 4 CPU) к PyTorch job.

### 5. llama-server GPU contention

**Проблема:** Docker контейнер llama-server (14.7 GB VRAM) остановился
при запуске k3s GPU smoke job.

**Решение:** Перезапущен через `systemctl restart llama-cpp.service`.
Документировано: перед ML workloads рекомендуется `gpu-switch.sh none`.
