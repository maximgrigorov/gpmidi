# Flux CD Bootstrap for AILab

## Status

Flux controllers are **not installed** in this initial bootstrap.
Reason: Gitea repository credentials must be provisioned securely,
and the exact registry token/SSH key configuration was not part of
this initial scope.

## Prerequisites

1. k3s cluster running and healthy
2. Gitea personal access token with repo read access
3. `flux` CLI installed locally

## Bootstrap command (when ready)

```bash
flux bootstrap git \
  --url=http://192.168.30.2:3300/mgrigorov/gpmidi.git \
  --branch=main \
  --path=infra/ailab \
  --username=mgrigorov \
  --password=<GITEA_TOKEN> \
  --token-auth=true \
  --components-extra=image-reflector-controller,image-automation-controller
```

## Directory structure

```
infra/ailab/
├── kustomization.yaml   # Buildable reconciliation root
├── clusters/ailab/      # Flux-generated flux-system files after bootstrap
└── apps/gpmidi-ml/      # Phase 1 application layer
```

## Notes

- `infra/ailab/kustomization.yaml` is the durable reconciliation root:
  Traefik configuration, local-path configuration, NVIDIA device plugin, and
  the application layer. One-shot GPU/PVC smoke Jobs stay in git but are not
  reconciled continuously by Flux.
- The TLS secret is generated out-of-band and must exist before the smoke-app
  Ingress becomes ready; its private key is never committed.
- For production, replace the password-based auth with an SSH deploy key
  stored as a Kubernetes Secret.
- Gitea webhook for push-based reconciliation can be configured at
  `http://192.168.30.2:3300/mgrigorov/gpmidi/settings/hooks`.
