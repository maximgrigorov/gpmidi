# gpmidi-web (Flask Projects UI)

The Guitar Pro converter and the Projects / Reference-Time UI, served in-cluster
so the Phase 2 acceptance workflow can be exercised over the real HTTP path
instead of an in-process Flask `test_client`.

| Property | Value |
|----------|-------|
| Image | `192.168.30.2:3300/mgrigorov/gpmidi-web` (deployed by digest) |
| Service | `gpmidi-web.gpmidi-ml.svc.cluster.local:8080` |
| Ingress | `https://192.168.30.2/` with `Host: gpmidi.ailab.local` |
| Storage | `emptyDir` only — nothing uploaded through the UI is persisted here |

## Why a dedicated host

`asset-api` and `reference-time` are served on the `*` host under the path
prefixes `/asset-api` and `/reference-time`. A catch-all `/` rule on `*` would
shadow them, so the UI gets its own hostname.

## Secret

`SECRET_KEY` comes from the `gpmidi-web-secret` Secret and is never committed:

```bash
sudo k3s kubectl -n gpmidi-ml create secret generic gpmidi-web-secret \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)"
```

## Upstream API base URLs

In-cluster the two APIs are separate Services. `REFERENCE_TIME_BASE` is therefore
set explicitly; deriving it from `ASSET_API_BASE` by string replacement (the
previous behaviour, kept only as a fallback for the shared-ingress layout) would
send reference-time requests to asset-api.

## No permanent source copies

Project uploads are streamed straight through to the Asset API
(`stream_proxy_upload`), and converter sessions live under
`GPMIDI_DATA_ROOT=/var/lib/gpmidi`, an `emptyDir`. Live E2E scenario 13 asserts
that no project asset ever appears under that path.
