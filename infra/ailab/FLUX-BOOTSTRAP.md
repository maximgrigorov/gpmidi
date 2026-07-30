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
  --path=infra/ailab/clusters/ailab \
  --username=mgrigorov \
  --password=<GITEA_TOKEN> \
  --token-auth=true \
  --components-extra=image-reflector-controller,image-automation-controller
```

## Directory structure

```
infra/ailab/
├── clusters/ailab/      # Flux entrypoint — Kustomization root
│   └── kustomization.yaml
└── apps/gpmidi-ml/      # Application manifests
    └── kustomization.yaml
```

## Notes

- The `clusters/ailab/kustomization.yaml` aggregates base, storage, gpu,
  and smoke-app resources. Flux will reconcile them as a single Kustomization.
- For production, replace the password-based auth with an SSH deploy key
  stored as a Kubernetes Secret.
- Gitea webhook for push-based reconciliation can be configured at
  `http://192.168.30.2:3300/mgrigorov/gpmidi/settings/hooks`.
