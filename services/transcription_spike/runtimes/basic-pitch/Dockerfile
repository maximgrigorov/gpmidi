FROM python:3.10.19-slim-bookworm@sha256:23f63358922e79a794f71be8f3723c84e5ccca9638af3f74456dc73d6184499e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgnutls30=3.7.9-2+deb12u7 \
        libssl3=3.0.20-1~deb12u2 \
        openssl=3.0.20-1~deb12u2 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --require-hashes -r /app/requirements.lock

COPY run.py /app/run.py
RUN python - <<'PY'
import hashlib
from pathlib import Path
from basic_pitch import FilenameSuffix, build_icassp_2022_model_path
from run import MODEL_SHA256

model = Path(build_icassp_2022_model_path(FilenameSuffix["tflite"]))
actual = hashlib.sha256(model.read_bytes()).hexdigest()
assert actual == MODEL_SHA256, (actual, MODEL_SHA256)
PY

USER 65532:65532
ENTRYPOINT ["python", "/app/run.py"]
