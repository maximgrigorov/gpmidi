# GPU Smoke Test Results

**Date:** 2026-07-30

## nvidia-smi Job (`gpu-nvidia-smi-smoke`)

**Status:** Completed (PASS)

```
Thu Jul 30 17:11:53 2026
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 590.48.01              Driver Version: 590.48.01      CUDA Version: 13.1     |
+-----------------------------------------+------------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|=========================================+========================+======================|
|   0  NVIDIA GeForce RTX 5060 Ti     Off |   00000000:01:00.0 Off |                  N/A |
|  0%   34C    P8              8W /  180W |       2MiB /  16311MiB |      0%      Default |
+-----------------------------------------+------------------------+----------------------+
```

- GPU: NVIDIA GeForce RTX 5060 Ti
- VRAM: 16311 MiB
- Driver: 590.48.01, CUDA: 13.1
- Image: `nvidia/cuda:13.1.0-base-ubuntu24.04`
- runtimeClassName: nvidia

## PyTorch CUDA Job (`gpu-pytorch-smoke`)

**Status:** Completed (PASS)

```
PyTorch version: 2.7.1+cu128
CUDA available: True
Device: NVIDIA GeForce RTX 5060 Ti
CUDA version: 12.8
Max matmul diff: 9.92e-05
Peak VRAM: 20.1 MB
PYTORCH CUDA SMOKE: PASS
```

- Matrix operation: 1024x1024 float32 matmul verified
- Max numerical difference: 9.92e-05 (threshold: 1e-2)
- Peak VRAM: 20.1 MB
- Image: `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime`
- CUDA 12.8 container on driver 590 (CUDA 13.1) — forward-compatible

## Node GPU Resources

```
capacity.nvidia.com/gpu: 1
allocatable.nvidia.com/gpu: 1
```
