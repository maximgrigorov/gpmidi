# Headlamp Dashboard + Routing Verification

**Date:** 2026-08-01
**Scope:** install a cluster dashboard and give the 443 routing model
non-conflicting addresses. No application image was rebuilt; no data-bearing
workload was touched.

## Discovered before changing anything

| Item | Value |
|---|---|
| k3s / Kubernetes | `v1.36.2+k3s1` (server and client) |
| Node | `ailab` Ready, control-plane, containerd `2.3.2-k3s2` |
| Traefik | `3.7.4`, chart `traefik-40.1.3_up40.1.0` |
| Traefik entrypoints | `web :8000` (**not exposed**), `websecure :8443` → host 443, `traefik :8080`, `metrics :9100` |
| Helm | **absent** — installed as part of this task |
| Namespaces | default, gpmidi-ml, kube-node-lease, kube-public, kube-system, tekton-pipelines, tekton-pipelines-resolvers |
| Host port 80 | `nginx` (pid 1337), `<title>Server Dashboard</title>` |
| Host port 443 | klipper-lb `svclb-traefik` hostPort → DNAT to Traefik. No userspace listener, so `ss -tlnp` shows nothing on 443 — expected |
| TLS secrets | `gpmidi-ml/ailab-tls`, `kube-system/k3s-serving` |
| `ailab-tls` SANs (before) | `IP:192.168.30.2, DNS:ailab.local, DNS:gpmidi-ml.ailab.local` |
| metrics-server | present in kube-system |
| GPU/LLM profile | `llama-server` active, `/health` 200, 14630/16311 MiB used |

### Routing before

Two Ingresses claimed the hostless `/` route:

```
gpmidi-ml/smoke-app    <HOSTLESS>  /  -> smoke-app:8080      (live)
gpmidi-ml/gpmidi-web   <HOSTLESS>  /  -> gpmidi-web:8080     (committed in 8d8d18d, never applied)
```

`https://192.168.30.2/` therefore answered with the Phase 0 smoke page. Two
hostless `PathPrefix(/)` routers are the same rule length, so Traefik's
longest-rule-wins ordering could not break the tie deterministically.

## Installed

| Item | Value |
|---|---|
| Helm client | `v3.21.3` (checksum-verified) at `/usr/local/bin/helm` |
| Chart | `headlamp/headlamp` **0.44.0**, appVersion **0.44.0** |
| Chart repo | `https://kubernetes-sigs.github.io/headlamp/` (official kubernetes-sigs) |
| Chart tarball sha256 | `02ea3f0131cc8ec02d647dff53802eaf8f7e8d9428cf7b5c4648486352d57f85` — matches the published index digest |
| Image | `ghcr.io/headlamp-k8s/headlamp@sha256:b491653c1a0d380b70b67a4024f1204591f9c84a68ee567778d53d28b1856168` |
| Namespace | `headlamp`, 1 replica |
| Resources | requests 50m / 128Mi, limits 500m / 256Mi |
| Actual usage | **3m CPU, 15Mi memory** |
| Pod | `headlamp-77c58d8c5b-f6nkn` Ready, **restartCount 0** |

Rendered release contains exactly three objects — Deployment, Service,
ServiceAccount. No PVC, no HA, no cert-manager, no monitoring stack, no Rancher.

## Authentication

The chart default `clusterRoleBinding.create: true` /
`clusterRoleName: cluster-admin` would have granted the **pod's** ServiceAccount
cluster-admin. It is switched off. Verified:

```
can-i list nodes       as system:serviceaccount:headlamp:headlamp -> no
can-i list pods        as system:serviceaccount:headlamp:headlamp -> no
can-i get  secrets     as system:serviceaccount:headlamp:headlamp -> no
can-i list namespaces  as system:serviceaccount:headlamp:headlamp -> no
can-i create pods      as system:serviceaccount:headlamp:headlamp -> no
can-i '*' '*'          as system:serviceaccount:headlamp:headlamp -> no
ClusterRoleBindings/RoleBindings referencing that SA                -> none
can-i '*' '*'          as system:serviceaccount:headlamp:headlamp-admin -> yes
```

`config.unsafeUseServiceAccountToken` is `false`. Unauthenticated access to the
Kubernetes proxy is refused:

```
/clusters/ailab/api/v1/nodes         -> 401
/clusters/ailab/api/v1/namespaces    -> 401
/clusters/ailab/api/v1/pods          -> 401
/clusters/ailab/api/v1/secrets       -> 401
/clusters/ailab/apis/apps/v1/deployments -> 401
Authorization: Bearer not-a-real-token   -> 401
```

`/api/v1/nodes` returns 200, but its body is byte-identical (sha256
`279717a8…`) to `/` — it is the single-page-app fallback, not cluster data.

With a `headlamp-admin` token minted via the TokenRequest API
(`kubectl create token`, 24h, never stored):

| Resource | HTTP | items |
|---|---|---|
| nodes | 200 | 1 |
| namespaces | 200 | 8 |
| pods | 200 | 92 |
| deployments | 200 | 17 |
| statefulsets | 200 | 0 |
| jobs | 200 | 6 |
| cronjobs | 200 | 1 |
| ingresses | 200 | 5 |
| persistentvolumeclaims | 200 | 14 |
| events | 200 | 137 |

Pod logs through the Headlamp proxy, for a gpmidi pod:

```
GET /clusters/ailab/api/v1/namespaces/gpmidi-ml/pods/gpmidi-web-57c7656c5c-4xxvk/log?tailLines=4
-> 200
   10.42.0.1 - - [31/Jul/2026:20:21:59 +0000] "GET /healthz HTTP/1.1" 200 46 "-" "kube-probe/1.36"
```

## Verified routing matrix

Exactly **1** hostless `/` rule exists cluster-wide, owned by Headlamp.

| Request | HTTP | Content-Type | Identifying body | Backend |
|---|---|---|---|---|
| `http://192.168.30.2/` (port 80) | 200 | `text/html` | `<title>Server Dashboard</title>` | host nginx, untouched |
| `https://192.168.30.2/` | 200 | `text/html; charset=utf-8` | `<title>Headlamp</title>` | headlamp |
| `https://k8s.ailab.home.arpa/` | 200 | `text/html; charset=utf-8` | `<title>Headlamp</title>` | headlamp |
| `https://gpmidi.ailab.home.arpa/` | 200 | `text/html; charset=utf-8` | `<title>конвертер midi</title>` | gpmidi-web |
| `https://gpmidi.ailab.local/` (legacy) | 200 | `text/html; charset=utf-8` | `<title>конвертер midi</title>` | gpmidi-web |
| `https://192.168.30.2/asset-api/healthz` | 200 | `application/json` | `{"status":"ok"}` | asset-api |
| `https://192.168.30.2/reference-time/healthz` | 200 | `application/json` | `{"status":"ok","version":"0.4.0"}` | reference-time |
| `https://ailab.local/` | 200 | `text/html` | `AILab gpmidi-ml smoke deployment` | smoke-app |

All of the above also pass under `curl -kfsS` (fail-on-error).

## TLS

`ailab-tls` reissued and replicated into `gpmidi-ml` and `headlamp`:

```
subject=CN = ailab.home.arpa, O = ailab
notBefore=Jul 31 20:20:04 2026 GMT   notAfter=Nov  2 20:20:04 2028 GMT
X509v3 Subject Alternative Name:
    IP Address:192.168.30.2, DNS:ailab.home.arpa, DNS:k8s.ailab.home.arpa,
    DNS:gpmidi.ailab.home.arpa, DNS:ailab.local, DNS:gpmidi-ml.ailab.local,
    DNS:gpmidi.ailab.local
```

Hostname verification now succeeds for both `home.arpa` names; only the
self-signed chain remains untrusted, which is accepted for this LAN-only PoC. The
private key was generated on AILab and is not in the repository.

## Unchanged by this task (before → after)

| Item | Before | After |
|---|---|---|
| `asset-api` imageID | `…@sha256:f61d50d0…fc80` | identical |
| `gpmidi-web` imageID | `…@sha256:a82d05dc…fc22` | identical |
| `reference-time` imageID | `…@sha256:20b5ced4…4def` | identical |
| Pod names | `asset-api-6849789cb8-lqlgc`, `gpmidi-web-57c7656c5c-4xxvk`, `reference-time-f949d449d-62pjd` | identical |
| Restart counts | 1, 1, 1 (from a host reboot ~20 min before this task) | 1, 1, 1 |
| `project-assets` PVC UID | `385cd0c7-4682-4944-be90-2a35a3a07c43` | identical |
| `reference-time-data` PVC UID | `ee6ceb47-a582-4d0c-b27f-bb1c0ca9a884` | identical |
| PVC count | 14 | 14 |
| llama.cpp `/health` | 200 | 200 |
| Active GPU profile | `llama-server` | `llama-server` |
| GPU memory | 14630/16311 MiB | 14630/16311 MiB |

No workload was restarted, rescheduled or rolled out. The GPU profile switch
lifecycle in the top-level README was not needed: Headlamp is CPU-only and
requests no `nvidia.com/gpu`.

### Quota

`gpmidi-ml` quota after the change — nothing violated:

```
limits.cpu 5700m/16   limits.memory 3200Mi/24Gi   nvidia.com/gpu 0/1
pvc 14/20   pods 5/30   requests.cpu 450m/8   requests.memory 704Mi/16Gi
services 5/10
```

Headlamp lives in the `headlamp` namespace, which has no ResourceQuota, so it
consumes none of the `gpmidi-ml` budget.

**Observed, pre-existing, not caused by this task:** the `services` counter read
6 at the start and 5 afterwards. The Tekton EventListener controller is thrashing
its own Service in `gpmidi-ml` — at 20:01 UTC, before this task's baseline
snapshot, it had already driven the counter to `services=10` and hit
`exceeded quota: gpmidi-ml-quota`, and its `Service` condition is still
`False: services "el-gitea-listener" already exists`. This task created no
Service in `gpmidi-ml`.

## Gates

| Gate | Tool | Result |
|---|---|---|
| shellcheck | ShellCheck 0.11.0 (pinned CI image) | clean over all `infra/ailab/scripts/*.sh` |
| bash syntax | `bash -n` | clean |
| Helm lint | helm v3.21.3 + pinned chart 0.44.0 + our values | `1 chart(s) linted, 0 chart(s) failed` |
| Helm template | rendered 3 resources; asserted 0 ClusterRoleBindings, no unsafe token flag | pass |
| Schema (changed files) | kubeconform 0.7.0 `-strict`, k8s 1.36.2 | 6/6 valid |
| Schema (rendered root) | kubeconform 0.7.0 `-strict` | 50 valid, 0 invalid, 19 CRD-schema skips |
| Kustomize root | `kubectl kustomize infra/ailab` | 69 resources render |
| Manifest policy | `scripts/check_manifests.py` | all checks passed (4 workloads, 1 cronjob, 9 policies) |
| Secrets | gitleaks 8.24.3 | no leaks |
| Whitespace | `git diff --check` | clean |

## Idempotence

`install-helm.sh` and `install-headlamp.sh` were both run twice.

- `install-helm.sh` second run: `helm v3.21.3 already installed … Nothing to do.`
- `ensure-ailab-tls.sh` second run: `already covers every required name` / `already current` for both namespaces — no reissue.
- `install-headlamp.sh` second run: Helm revision 2, but the same pod
  `headlamp-77c58d8c5b-f6nkn` at the same age with restartCount 0 — no rollout.

## Known limitations

1. The certificate is self-signed, so browsers warn on first visit. Accepted for
   a LAN-only PoC; no cert-manager was installed.
2. The `headlamp` namespace has no NetworkPolicy. `gpmidi-ml` is default-deny, but
   adding an equivalent policy here would need an `ipBlock` egress rule for the
   host-IP API server endpoint — a real footgun for a dashboard-only task, and
   out of scope for a usability change.
3. `headlamp-admin` is bound to `cluster-admin` because the stated goal is to
   administer, not only inspect. On any shared cluster this is the first binding
   to narrow.
4. The pre-existing Tekton EventListener Service reconcile failure above is
   untouched and still `Ready: False`.
