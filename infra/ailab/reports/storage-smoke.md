# Storage Smoke Test Results

**Date:** 2026-07-30

## Storage Configuration

| Parameter | Value |
|-----------|-------|
| StorageClass | `local-path` (default) |
| Provisioner | `rancher.io/local-path` |
| VolumeBindingMode | WaitForFirstConsumer |
| ReclaimPolicy | Delete |
| Physical path | `/data/k3s-storage` |

## PVC Writer Job (`pvc-writer`)

**Status:** Completed (PASS)

```
WRITTEN: smoke-720e09be-537e-4a82-af35-049912cd5027
SHA256:  449270121a504970c812aa6110e68cda592088429525482d36ee18feb0e5076c
```

## PVC Reader Job (`pvc-reader`)

**Status:** Completed (PASS)

```
READ:     smoke-720e09be-537e-4a82-af35-049912cd5027
EXPECTED: 449270121a504970c812aa6110e68cda592088429525482d36ee18feb0e5076c
ACTUAL:   449270121a504970c812aa6110e68cda592088429525482d36ee18feb0e5076c
PERSISTENCE SMOKE: PASS
```

Writer pod wrote data, was deleted; Reader pod mounted same PVC and verified
SHA-256 integrity. Data persists across pod lifecycle.

## PVC Status

```
NAME             STATUS   VOLUME                                     CAPACITY   ACCESS MODES
smoke-pvc        Bound    pvc-bc63aff4-419e-4372-8a02-5ad17fb49306   1Gi        RWO
project-assets   Pending  (WaitForFirstConsumer — correct, no pod yet)  50Gi     RWO
```

## Reclaim policy rationale

`Delete` is appropriate for a single-node dev/ML cluster where PVC lifecycle
is managed by Flux/kubectl. For production data, individual PVCs can override
with `persistentVolumeReclaimPolicy: Retain`.
