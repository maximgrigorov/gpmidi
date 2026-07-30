# Cursor / Opus 4.6 task: bootstrap AILab as an isolated k3s GPU worker

Этот файл можно целиком передать Cursor Agent с моделью Opus 4.6.

---

## Задание

Ты работаешь в репозитории `gpmidi`. Нужно подготовить и реально выполнить воспроизводимый bootstrap удалённой машины AILab как single-node Kubernetes GPU worker для будущего Reference-Guided MIDI Restoration pipeline.

### Машина

- Host: `192.168.30.2`
- SSH user: `mgrigorov`
- Authentication: стандартный/default SSH key текущего пользователя; не проси создавать новый ключ и не записывай private keys в репозиторий.
- Hardware: RTX 5060 Ti 16 GB, 32 GB RAM, Intel i5-13400F (10 cores / 16 threads).
- На сервере уже работают другие сервисы, включая Gitea на `:3300` и LLM/API health на `:8080`. Их нельзя ломать или занимать их порты.
- `/data` уже содержит модели и другие ценные данные. Нельзя форматировать filesystem, менять владельца/права рекурсивно, удалять или перемещать существующие данные.

### Критически важный первый шаг

Самое первое удалённое действие после проверки SSH-соединения — прочитать `/home/mgrigorov/AGENTS.md` полностью:

```bash
ssh mgrigorov@192.168.30.2 'cat /home/mgrigorov/AGENTS.md'
```

Сначала кратко зафиксируй обнаруженные там обязательные правила и только затем продолжай. Если `AGENTS.md` противоречит этому заданию или сообщает о специальных ограничениях сервера, остановись и запроси решение пользователя. Не начинай установку до чтения файла.

Используй `BatchMode=yes` для диагностических SSH-команд, чтобы не зависать на password prompts. Перед изменениями проверь `sudo -n true`; если sudo требует пароль или подтверждение, не пытайся обходить это — попроси пользователя выполнить необходимое подтверждение.

## Результат

Нужен не план и не набор непроверенных YAML, а реально работающий single-node k3s с:

1. Kubernetes API и здоровым node;
2. рабочей NVIDIA GPU интеграцией внутри pod;
3. persistent storage под отдельными каталогами `/data`;
4. namespace `gpmidi-ml`;
5. тестовым HTTPS ingress на `443` с self-signed сертификатом;
6. тестовым web deployment;
7. GPU smoke Job;
8. PyTorch CUDA smoke Job;
9. PVC persistence smoke;
10. ограниченным RBAC для будущего gpmidi deployment;
11. воспроизводимыми scripts/manifests в git;
12. итоговым отчётом с фактическими командами и их выводом.

## Правила работы с git

1. Проверь `git status`, текущую ветку и remote до изменений.
2. Не работай напрямую в `main`.
3. Создай feature branch:

```text
infra/ailab-k3s-bootstrap
```

4. Не включай в commit:
   - kubeconfig;
   - ServiceAccount tokens;
   - TLS private keys;
   - registry credentials;
   - `.env`;
   - вывод с секретами;
   - загруженные container images или model weights.
5. Все воспроизводимые материалы положи под:

```text
infra/ailab/
```

6. Не меняй `gp_to_shreddage.py`, музыкальные configs, humanization, timing, bend, articulation или существующую семантику экспортера.
7. Сделай небольшие логические commits. Не push/merge в `main`; подготовь ветку для review.

## Phase 1 — безопасное discovery до установки

Собери и сохрани redacted baseline в `infra/ailab/reports/preflight.md`:

- OS/distribution/version/kernel;
- hostname/IP/routes/DNS;
- CPU/RAM;
- filesystem type, mount options, total/free space для `/`, `/var`, `/data`;
- существует ли `/data`, список только верхнего уровня без чтения содержимого моделей;
- `nvidia-smi`, версия driver, GPU name/VRAM;
- установлен ли CUDA toolkit;
- установлен ли `nvidia-container-toolkit`;
- Docker/Podman/containerd/k3s/minikube/kubectl/helm/flux;
- занятые listening ports;
- firewall implementation and current rules;
- AppArmor/SELinux;
- swap status;
- cgroup version;
- существующие systemd services/container workloads;
- уже существующий Kubernetes state.

Не переустанавливай работающий NVIDIA driver без доказанной необходимости. Если драйвер не поддерживает RTX 5060 Ti/CUDA runtime или `nvidia-smi` не работает, остановись и отдельно опиши blocker — не делай рискованный blind driver upgrade.

Перед выбором каталогов измерь `/data`. Создавай только новые изолированные каталоги, например:

```text
/data/k3s
/data/k3s-storage
/data/gpmidi-projects
```

Не делай `chown -R /data` и аналогичных операций.

Проверь, свободны ли `80`, `443` и `6443`. Если `443` занят, не убивай процесс и не переназначай существующий сервис: зафиксируй конфликт и предложи безопасную интеграцию с существующим reverse proxy. Продолжать с альтернативным портом можно только после явного согласования.

## Phase 2 — воспроизводимая установка k3s

Установи pinned stable k3s single-node. Версию зафиксируй в `infra/ailab/versions.env`; не используй неприкреплённый `latest`.

Требования:

- k3s запускается как systemd service;
- API certificate содержит TLS SAN `192.168.30.2`;
- kubeconfig имеет безопасные права;
- API health проверен через `kubectl get --raw=/readyz`;
- node находится в `Ready`;
- после reboot сервис должен стартовать автоматически;
- существующие сервисы на `3300` и `8080` остаются доступны;
- если `/data` подходит по filesystem и capacity, k3s data/container image storage размещается в отдельном `/data/k3s`; решение и причина фиксируются в отчёте;
- ingress controller — штатный Traefik k3s, если preflight не выявил конфликтов или причины выбрать другой вариант;
- не отключай security features ради удобства.

Все host-side действия должны быть представлены идемпотентными scripts или Ansible-подобными шагами под `infra/ailab/scripts/`. Скрипты должны использовать `set -Eeuo pipefail`, проверять prerequisites и не разрушать существующую установку при повторном запуске.

Добавь `infra/ailab/ROLLBACK.md`: что именно установлено, как остановить/удалить только созданные компоненты, какие каталоги при этом сохраняются. Не запускай rollback.

## Phase 3 — NVIDIA GPU в k3s

Настрой GPU по официально поддерживаемой схеме для фактически обнаруженных OS, k3s/containerd и driver:

- host NVIDIA driver сохраняется;
- устанавливается/настраивается NVIDIA Container Toolkit, если отсутствует;
- k3s containerd получает NVIDIA runtime;
- устанавливается pinned NVIDIA Kubernetes device plugin;
- не используй GPU Operator, если для single-node host-driver setup достаточно device plugin;
- не устанавливай второй конфликтующий container runtime.

Acceptance:

```text
kubectl get node -o json
```

должен показывать allocatable/capacity `nvidia.com/gpu: 1`.

Создай namespace-scoped Job `gpu-nvidia-smi-smoke`, который:

- явно запрашивает `nvidia.com/gpu: 1`;
- использует pinned CUDA image, совместимый с driver;
- выполняет `nvidia-smi`;
- в логах показывает RTX 5060 Ti и ожидаемый объём VRAM;
- успешно завершается.

Создай второй Job `gpu-pytorch-smoke`, который на pinned image:

- импортирует torch;
- проверяет `torch.cuda.is_available()`;
- печатает `torch.cuda.get_device_name(0)`;
- создаёт tensors на GPU и выполняет небольшую матричную операцию;
- завершает `torch.cuda.synchronize()`;
- проверяет конечный численный результат;
- печатает peak allocated VRAM;
- успешно завершается.

Одного `nvidia-smi` недостаточно для приёмки.

Сохрани redacted логи обоих Jobs в `infra/ailab/reports/`.

## Phase 4 — storage на `/data`

Настрой отдельный StorageClass, использующий только выделенный подкаталог `/data/k3s-storage`, либо безопасно перенастрой встроенный local-path provisioner на этот каталог. Предпочтительное имя:

```text
ailab-local
```

Требования:

- динамическое создание PVC;
- `volumeBindingMode: WaitForFirstConsumer`, если поддерживается выбранным provisioner;
- reclaim policy должна быть выбрана осознанно и записана в документации;
- не создавать PV поверх корня `/data`;
- не давать pod произвольный hostPath к существующим model directories;
- namespace `gpmidi-ml` получает PVC для будущих project assets;
- application path внутри pod, например `/var/lib/gpmidi`;
- физический путь остаётся под `/data/k3s-storage` или отдельным `/data/gpmidi-projects`.

PVC smoke test:

1. создать PVC;
2. pod A записывает уникальную строку и SHA-256 в volume;
3. pod A удаляется;
4. pod B монтирует тот же PVC и проверяет содержимое и SHA-256;
5. pod B успешно завершается;
6. удалить test pods, но не удалять рабочий project-assets PVC.

Зафиксируй `kubectl get sc,pv,pvc -o wide` и логи проверки.

## Phase 5 — namespace и security boundaries

Создай namespace:

```text
gpmidi-ml
```

Применить:

- Pod Security labels уровня `restricted` для application workloads, если GPU/CNI компоненты могут оставаться в своих системных namespaces;
- ResourceQuota;
- LimitRange;
- default-deny ingress/egress NetworkPolicy с минимальными разрешениями для DNS, ingress controller и необходимых image/model endpoints;
- ServiceAccount для приложения без cluster-admin;
- ServiceAccount/Role/RoleBinding `gpmidi-deployer` только в `gpmidi-ml`.

`gpmidi-deployer` должен иметь только необходимые namespace-scoped права для:

- Deployments/ReplicaSets;
- Services;
- ConfigMaps;
- Jobs/CronJobs;
- Pods list/get/watch и pod logs;
- PVC list/get/create/update/delete;
- Ingress list/get/create/update/delete;
- Events read.

Не давать:

- ClusterRoleBinding;
- nodes access;
- namespaces management;
- persistentvolumes management;
- privileged pod/hostPID/hostNetwork;
- arbitrary hostPath;
- secrets read/list;
- exec в системные pods;
- изменение `kube-system`.

Если стандартный Kubernetes RBAC не может ограничить опасное действие достаточно тонко, документируй границу вместо создания иллюзии безопасности.

Подготовь локальный script генерации ограниченного kubeconfig через `kubectl create token --duration=...`, но не коммить сгенерированный kubeconfig/token. Сохранять его на сервере можно только в `/home/mgrigorov/.kube/` с mode `0600` и после явного подтверждения пользователя.

## Phase 6 — HTTPS ingress на 443

Создай self-signed сертификат без cert-manager, чтобы не добавлять лишний controller. Сертификат должен иметь SAN:

- IP: `192.168.30.2`;
- DNS: `ailab.local`;
- DNS: `gpmidi-ml.ailab.local`.

Требования:

- private key никогда не попадает в git или отчёт;
- в git хранится только идемпотентный script генерации/apply secret;
- Kubernetes TLS Secret находится в `gpmidi-ml`;
- ingress слушает `443`;
- HTTP `80`, если свободен, перенаправляется на HTTPS;
- должен работать catch-all/IP request, а не только DNS hostname;
- `curl -k https://192.168.30.2/healthz` возвращает HTTP 200;
- `openssl s_client` подтверждает SAN и self-signed certificate;
- не выключай TLS verification глобально — `-k` используется только для smoke самоподписанного сертификата.

## Phase 7 — тестовый deployment

Разверни минимальный pinned test application в `gpmidi-ml` с:

- non-root user;
- read-only root filesystem, если image позволяет;
- dropped Linux capabilities;
- seccomp RuntimeDefault;
- requests/limits;
- liveness/readiness probes;
- ClusterIP Service;
- Ingress;
- монтированным test/project PVC только в требуемый path.

Endpoints:

- `/healthz` → 200 + JSON с version/hostname;
- `/readyz` → 200;
- `/storage` → читает заранее созданный harmless marker из PVC и возвращает его hash, без directory listing;
- `/` → краткая страница `AILab gpmidi-ml smoke deployment`.

Если готового доверенного pinned image нет, добавь в `infra/ailab/smoke-app/` небольшой исходник и Dockerfile, собери image воспроизводимо, push в существующий локальный Gitea registry только после обнаружения его фактического URL/auth и без вывода credentials. Если registry не настроен, не изобретай insecure registry молча: используй временный pinned public image для ingress smoke, а custom app оставь готовым к build и зафиксируй blocker.

Проверь endpoint:

- изнутри cluster;
- с AILab host;
- с машины, где работает Cursor, если сетевой маршрут доступен;
- повторно после rollout restart;
- повторно после удаления/recreation application pod.

## Phase 8 — ports и firewall

Документируй фактическую матрицу:

- `443/tcp` — HTTPS application ingress, доступен из локальной сети;
- `80/tcp` — только redirect на HTTPS, если используется;
- `6443/tcp` — Kubernetes API, не открывать всему LAN без необходимости;
- `3300/tcp` и `8080/tcp` — существующие сервисы, остаются неизменными;
- SSH `22/tcp` — остаётся без изменений;
- внутренние k3s/CNI/kubelet ports не публиковать шире необходимого для single-node.

Не сбрасывай firewall и не меняй default policy. Делай только минимальные additive rules после определения ufw/firewalld/nftables. Kubernetes API должен быть доступен локально; внешний доступ разрешай только доверенному management source, если его адрес достоверно известен и пользователь подтвердил. Если внешний API не нужен благодаря GitOps, не открывай `6443` всему LAN.

После изменений повторно проверь существующие endpoints Gitea `:3300` и LLM health `http://192.168.30.2:8080/health`.

## Phase 9 — Flux preparation

Установи pinned Flux controllers только если это не требует угадывать repository credentials и preflight не выявил конфликтов. Не создавай и не печатай Gitea token.

В любом случае подготовь:

```text
infra/ailab/clusters/ailab/
infra/ailab/apps/gpmidi-ml/
```

с Kustomize-compatible manifests. Если credentials безопасно доступны через существующий approved mechanism, подключи Flux к feature-branch path и проверь reconciliation. Если нет — оставь контроллеры неустановленными либо установленными без Git source, а точный bootstrap command сохрани в `infra/ailab/FLUX-BOOTSTRAP.md` с placeholders. Это не должно блокировать k3s/GPU/storage/ingress acceptance.

## Phase 10 — reboot verification

После всех первичных проверок запроси у пользователя отдельное подтверждение на reboot. Не перезагружай сервер молча.

После разрешённого reboot проверь:

- host вернулся;
- `nvidia-smi` работает;
- k3s active;
- node Ready;
- device plugin Ready;
- `nvidia.com/gpu: 1` allocatable;
- PVC Bound и marker сохранился;
- test deployment Ready;
- HTTPS 443 отвечает;
- Gitea 3300 отвечает;
- LLM health 8080 отвечает.

Если пользователь не разрешил reboot, честно пометь reboot persistence как непроверенную границу.

## Обязательные файлы результата

```text
infra/ailab/
├── README.md
├── versions.env
├── ROLLBACK.md
├── FLUX-BOOTSTRAP.md
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
│   ├── storage-class-or-provisioner.yaml
│   ├── project-assets-pvc.yaml
│   ├── persistence-smoke.yaml
│   └── kustomization.yaml
├── gpu/
│   ├── device-plugin.yaml-or-helm-values.yaml
│   ├── nvidia-smi-job.yaml
│   ├── pytorch-smoke-job.yaml
│   └── kustomization.yaml
├── smoke-app/
│   ├── source and Dockerfile if needed
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   └── kustomization.yaml
├── clusters/ailab/
├── apps/gpmidi-ml/
└── reports/
    ├── preflight.md
    ├── gpu-smoke.md
    ├── storage-smoke.md
    ├── ingress-smoke.md
    └── final-verification.md
```

Допускается изменить структуру, если итог проще и все требования покрыты. Объясни отклонения.

## Приёмочные проверки

Не объявляй задачу выполненной, пока реальные команды не подтвердили:

```bash
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl get node -o jsonpath='{.items[0].status.capacity.nvidia\.com/gpu}'
kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'
kubectl get sc,pv,pvc -A -o wide
kubectl get ingress -A
kubectl auth can-i --as=system:serviceaccount:gpmidi-ml:gpmidi-deployer create deployments -n gpmidi-ml
kubectl auth can-i --as=system:serviceaccount:gpmidi-ml:gpmidi-deployer get nodes
kubectl logs job/gpu-nvidia-smi-smoke -n gpmidi-ml
kubectl logs job/gpu-pytorch-smoke -n gpmidi-ml
curl -kfsS https://192.168.30.2/healthz
curl -kfsS https://192.168.30.2/storage
curl -fsS http://192.168.30.2:8080/health
```

Ожидаемое RBAC:

- create deployment в `gpmidi-ml`: `yes`;
- get nodes: `no`.

Также проверь:

- generated private key и kubeconfig отсутствуют в `git status` и `git diff`;
- `git diff --check` проходит;
- scripts проходят `bash -n` и shellcheck, если shellcheck можно безопасно установить/он уже есть;
- Kustomize manifests проходят build;
- YAML проходит parser/schema validation доступным инструментом;
- существующие gpmidi tests не затронуты; если изменялся только `infra/`, минимум запусти быстрый regression/import smoke и зафиксируй результат.

## Формат финального ответа

Верни:

1. **Что обнаружено до установки.**
2. **Что установлено и какими pinned версиями.**
3. **Какие файлы/commits созданы.**
4. **Фактические результаты GPU/PyTorch/PVC/HTTPS/RBAC smoke.**
5. **Какие порты доступны и откуда.**
6. **Подтверждение, что `/data` существующие данные, Gitea и LLM API не повреждены.**
7. **Что осталось непроверенным или заблокированным.**
8. **Как откатить изменения.**
9. **Branch/commit SHA для последующего code review; не merge в main.**

Не подменяй реальные результаты ожидаемыми. Если network, sudo, driver, registry, firewall или reboot блокируют часть проверки, остановись на безопасном состоянии и сообщи точный blocker.
