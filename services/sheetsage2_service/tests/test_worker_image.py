from __future__ import annotations

import re
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]


def test_worker_torch_wheels_have_kernels_for_the_ailab_gpu():
    # The AILab GPU is an RTX 5060 Ti (compute capability 12.0). torch 2.8.0
    # cu126 wheels are compiled for sm_50..sm_90 only, with no PTX fallback, so
    # every CUDA kernel fails there. Blackwell support starts with cu128.
    dockerfile = (SERVICE / "Dockerfile.worker").read_text(encoding="utf-8")
    index = re.search(r"--index-url https://download\.pytorch\.org/whl/cu(\d+)", dockerfile)
    assert index is not None
    assert int(index.group(1)) >= 128

    pins = dict(
        line.split("==", 1)
        for line in (SERVICE / "requirements-worker.txt").read_text(encoding="utf-8").splitlines()
        if "==" in line
    )
    assert f"torch=={pins['torch']}" in dockerfile
    assert f"torchaudio=={pins['torchaudio']}" in dockerfile
