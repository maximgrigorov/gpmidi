# Ingress / HTTPS Smoke Test Results

**Date:** 2026-07-30

## TLS Certificate

```
Subject: CN = ailab.local, O = gpmidi-ml
X509v3 Subject Alternative Name:
    IP Address:192.168.30.2, DNS:ailab.local, DNS:gpmidi-ml.ailab.local
```

Self-signed, valid 365 days. Private key stored only in Kubernetes Secret
`ailab-tls` in namespace `gpmidi-ml`.

## Endpoint Tests

### From AILab host

```
$ curl -kfsS https://192.168.30.2/healthz
{"status": "ok", "version": "0.1.0", "hostname": "smoke-app-c5957ddc5-sxvwp"}

$ curl -kfsS https://192.168.30.2/readyz
{"ready":true}

$ curl -kfsS https://192.168.30.2/storage
{"marker_sha256": "449270121a504970c812aa6110e68cda592088429525482d36ee18feb0e5076c", "status": "ok"}

$ curl -kfsS https://192.168.30.2/
<h1>AILab gpmidi-ml smoke deployment</h1><p>Version 0.1.0 on ...</p>
```

### From Cursor machine (LAN)

```
$ curl -kfsS https://192.168.30.2/healthz
{"status": "ok", "version": "0.1.0", "hostname": "smoke-app-c5957ddc5-sxvwp"}
```

### After rollout restart

New pod created, healthz responds with new hostname — PASS.

### After pod deletion and recreation

Deployment recreated pod automatically, healthz responds — PASS.

## Ingress Configuration

```
NAME        CLASS     HOSTS                               ADDRESS        PORTS
smoke-app   traefik   ailab.local,gpmidi-ml.ailab.local   192.168.30.2   80, 443
```

Catch-all rule (no host) included — responds to IP-based requests.

## Port 80

Port 80 remains with nginx (homepage). k3s Traefik HelmChartConfig disables
port 80 exposure (`ports.web.expose.default: false`). HTTP→HTTPS redirect
is not configured since port 80 serves a different service (homepage dashboard).
