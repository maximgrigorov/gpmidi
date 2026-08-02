FROM python:3.10.19-slim-bookworm@sha256:23f63358922e79a794f71be8f3723c84e5ccca9638af3f74456dc73d6184499e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

ARG IDM_COMMIT=456656868538205ef756912c7cf5b0fd936de8af
ARG IDM_SOURCE_SHA256=d209125b1053dfa1dea19deba0e3bc908761326c5a7253f77b525e34e7cf7827
ARG IDM_CHECKPOINT_SHA256=5856a9bee7c6d503842795756d238dc8470f6f3e010e9e4f33ede0362850cb4c

RUN curl -fsSL -o /tmp/idm.tar.gz \
      "https://github.com/bernardo-torres/inverse-drum-machine/archive/${IDM_COMMIT}.tar.gz" \
    && echo "${IDM_SOURCE_SHA256}  /tmp/idm.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/idm \
    && tar -xzf /tmp/idm.tar.gz --strip-components=1 -C /opt/idm \
    && rm /tmp/idm.tar.gz \
    && echo "${IDM_CHECKPOINT_SHA256}  /opt/idm/pretrained/idm-44-train-kits/checkpoints/val-epoch=518-global_step=0.ckpt" | sha256sum -c -

RUN python -m pip install \
      --index-url https://download.pytorch.org/whl/cpu \
      torch==2.1.1+cpu torchaudio==2.1.1+cpu \
    && python -m pip install /opt/idm

WORKDIR /app
COPY run.py /app/run.py
RUN chmod -R a=rX /opt/idm /app

USER 65532:65532
ENTRYPOINT ["python", "/app/run.py"]
