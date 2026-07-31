# AILab k3s GPU Cluster

Single-node Kubernetes (k3s) cluster on AILab with NVIDIA GPU support
for the Reference-Guided MIDI Restoration pipeline.

## Quick reference

| Component | Version |
|-----------|---------|
| k3s | v1.36.2+k3s1 |
| containerd | 2.3.2-k3s2 |
| NVIDIA driver | 590.48.01 |
| CUDA (driver) | 13.1 |
| NVIDIA device plugin | v0.19.3 |
| Node | ailab (192.168.30.2) |
| Namespace | gpmidi-ml |
| Tekton Pipelines | v1.14.1 |
| Tekton Triggers | v0.36.0 |
| Traefik | 3.7.4 (chart traefik-40.1.3_up40.1.0) |
| Helm client | v3.21.3 |
| Headlamp | chart 0.44.0 / app 0.44.0 |
| Registry | Gitea OCI, `192.168.30.2:3300` |

## Directory structure

```
infra/ailab/
├── README.md              # This file
├── versions.env           # Pinned versions
├── ROLLBACK.md            # How to remove everything
├── FLUX-BOOTSTRAP.md      # Flux CD setup (placeholder)
├── kustomization.yaml     # Buildable durable reconciliation root
├── traefik-config.yaml    # HelmChartConfig: 443 only
├── scripts/               # Idempotent host-side scripts
├── base/                  # Namespace, RBAC, quotas, policies
├── storage/               # StorageClass config, PVCs, smoke
├── gpu/                   # Device plugin, GPU smoke jobs
├── smoke-app/             # Test deployment (Phase 0)
├── tekton/                # Delivery pipeline, tasks, triggers, RBAC, retention
├── apps/asset-api/        # Phase 1 asset storage service
├── apps/reference-time/   # Phase 2 reference-time analysis service
├── apps/gpmidi-web/       # Flask converter + Projects UI (Phase 2)
├── apps/headlamp/         # Headlamp cluster dashboard (Helm release + repo Ingress/RBAC)
├── clusters/ailab/        # Flux-generated files after bootstrap
├── apps/gpmidi-ml/        # Future application layer
└── reports/               # Verification reports
```

## Access

```bash
# From AILab host
sudo k3s kubectl get pods -n gpmidi-ml

# HTTPS smoke
curl -k https://192.168.30.2/healthz

# Scoped kubeconfig (generate first)
bash scripts/generate-deployer-kubeconfig.sh
KUBECONFIG=~/.kube/gpmidi-deployer.kubeconfig kubectl get pods -n gpmidi-ml
```

## Storage

All k3s data is on `/data` (separate NVMe, 544G free):

- `/data/k3s` — k3s data directory (etcd, containerd images)
- `/data/k3s-storage` — local-path-provisioner PVC data

Existing `/data` contents are not touched.

## GPU workload lifecycle

Docker/systemd GPU profiles and Kubernetes GPU pods share one physical GPU and
must be treated as mutually exclusive. The currently active profile is dynamic:
normally it is `llama-cpp` serving `qwen3-coder-next`, but it may be ComfyUI or
another model. Never hard-code the restore target.

Before a GPU-heavy Kubernetes job:

1. Read `http://192.168.30.2/api/stats` and record the active profile.
2. Through the homepage on port 80, or its documented API, switch to `none`.
3. Wait until the profile switch completes and GPU VRAM is released.
4. Run the Kubernetes GPU workload.
5. On success, failure, timeout, or cancellation, restore the profile recorded
   in step 1 and verify its health endpoint.

Manual API example (the response is an SSE stream):

```bash
curl -N -X POST http://192.168.30.2/api/profiles/switch \
  -H 'Content-Type: application/json' \
  -d '{"profile":"none"}'
# Run the GPU workload, then restore the profile that was active before it.
```

Do not start a Kubernetes GPU pod while a profile still owns VRAM. A previous
smoke run stopped llama.cpp because this lifecycle was not followed.

## Port map

| Port | Service | Notes |
|------|---------|-------|
| 80 | nginx homepage | unchanged — host/server management page |
| 443 | k3s Traefik HTTPS | new |
| 3300 | Gitea | unchanged |
| 6443 | k3s API | new |
| 8080 | llama.cpp | unchanged |

Port 80 belongs to the host's nginx machine dashboard and Kubernetes must never
take it: `traefik-config.yaml` sets `ports.web.expose.default: false`, so Traefik
binds 443 only. Traefik reaches 443 through the klipper-lb `svclb-traefik` pod's
hostPort, which is a DNAT rule rather than a userspace listener — so `ss -tlnp`
shows nothing on 443 even though it is served. That is expected.

## Routing

One entrypoint (`websecure`, 443) serves the dashboard, the PoC UI and the two
APIs. Traefik orders overlapping routers by rule length, longest first, which is
what lets a hostless `/` coexist with hostless longer path prefixes.

| Request | Goes to | Rule owner |
|---|---|---|
| `https://192.168.30.2/` (no DNS needed) | Headlamp | `apps/headlamp/ingress.yaml` |
| `https://k8s.ailab.home.arpa/` | Headlamp | `apps/headlamp/ingress.yaml` |
| `https://gpmidi.ailab.home.arpa/` | gpmidi-web | `apps/gpmidi-web/ingress.yaml` |
| `https://gpmidi.ailab.local/` | gpmidi-web | legacy alias, same file |
| `<any host>/asset-api/…` | asset-api | `apps/asset-api/ingress.yaml` |
| `<any host>/reference-time/…` | reference-time | `apps/reference-time/ingress.yaml` |
| `https://ailab.local/`, `https://gpmidi-ml.ailab.local/` | smoke-app | `smoke-app/ingress.yaml` |

**There must be exactly one hostless `/` rule in the cluster**, and it belongs to
Headlamp. Both the Phase 0 smoke-app and a short-lived gpmidi-web PoC fallback
previously claimed it; two hostless `PathPrefix(/)` routers of equal length are an
unresolvable tie in Traefik, so whichever won was luck. Check with:

```bash
sudo k3s kubectl get ingress -A -o json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(1 for i in d["items"] for r in i["spec"].get("rules",[]) if not r.get("host") for p in r["http"]["paths"] if p["path"]=="/"))'
# must print 1
```

### DNS

Static A records on the MikroTik — no wildcard needed:

```
k8s.ailab.home.arpa     -> 192.168.30.2
gpmidi.ailab.home.arpa  -> 192.168.30.2
```

`home.arpa` (RFC 8375) is the documented primary domain. The `*.ailab.local`
names are legacy aliases kept so existing bookmarks keep working: `.local` is
reserved for mDNS by RFC 6762 and must not be used as a primary name. Do not add
new `.local` hosts.

### TLS

All hosts share the self-signed `ailab-tls` certificate, reissued and replicated
into every namespace that needs it by `scripts/ensure-ailab-tls.sh` (an Ingress
can only reference a Secret in its own namespace). Browsers warn on first visit;
that is accepted for this LAN-only PoC. Private keys are generated on AILab and
are never committed.

## Cluster dashboard

Headlamp (kubernetes-sigs), one replica in the `headlamp` namespace, installed
from the official chart pinned in `versions.env`. Login is by Kubernetes bearer
token — the pod's own ServiceAccount deliberately has no permissions at all. See
[`apps/headlamp/README.md`](apps/headlamp/README.md).

```bash
bash infra/ailab/scripts/install-helm.sh        # once
bash infra/ailab/scripts/install-headlamp.sh    # idempotent
sudo k3s kubectl -n headlamp create token headlamp-admin --duration=24h
```


## Delivery

`infra/ailab/tekton/` holds the whole delivery path. Images are built on AILab by
kaniko for `linux/amd64`, published under an **immutable exact-SHA tag only**, and
deployed **by digest**; a deploy fails unless the live pod's `imageID` equals the
published digest. Nothing is built before every gate passes and nothing is
deployed before the image has been audited and scanned.

Every gate tool is pinned inside `tekton/Dockerfile.ci` — kubectl, kubeconform,
ShellCheck, gitleaks, trivy, crane, ruff — plus two Python environments, because
the converter and the services pin different numpy versions. That image is itself
built on AILab by the `gpmidi-ci-image` pipeline.

```bash
# Reviewed candidate validation (labelled manual-candidate, never "a main push")
bash infra/ailab/scripts/run-pipeline.sh ci        <full-sha> <ci-image>

# Rebuild the gate image
bash infra/ailab/scripts/run-pipeline.sh ci-image  <full-sha>

# Prove a failing gate cannot deploy
bash infra/ailab/scripts/run-pipeline.sh negative  <full-sha> <ci-image> [gate]
```

Production trigger: a Gitea webhook on `main`, authenticated by shared-secret
signature and validated for repository, ref and commit shape.

One-time cluster configuration:

```bash
bash infra/ailab/scripts/configure-tekton.sh   # disables co-scheduling
```

Namespace limits are real: `LimitRange` caps a pod at 10 CPU, `ResourceQuota` caps
namespace `limits.cpu` at 12, and the application pods hold about 5.7. Gate groups
and per-image build/audit pairs are therefore chained, not parallel.

Evidence for every run is archived on the `tekton-evidence` PVC with `SHA256SUMS`,
ten bundles retained. Rollback: `infra/ailab/tekton/ROLLBACK.md`.

## Secrets (never committed)

```bash
K="sudo k3s kubectl -n gpmidi-ml"
$K create secret generic gpmidi-web-secret --from-literal=SECRET_KEY="$(openssl rand -hex 32)"
$K create secret generic gitea-webhook-secret --from-literal=secret="$(openssl rand -hex 24)"
# gitea-registry-auth holds a docker config.json for 192.168.30.2:3300
```
