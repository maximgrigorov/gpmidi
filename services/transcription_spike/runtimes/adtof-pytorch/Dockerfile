FROM python:3.11.13-slim-bookworm@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

ARG ADTOF_COMMIT=85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9
ARG ADTOF_SOURCE_SHA256=28602a3bd89836240d519396b566966c52b6439e2f3cda61d8a674433b350b56
ARG ADTOF_CHECKPOINT_SHA256=1bc986e596ec47ba0b44916f87cd4a39f0b2bec23596df3fb5d0e87749217320

RUN python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/xavriley/ADTOF-pytorch/archive/${ADTOF_COMMIT}.tar.gz', '/tmp/adtof-pytorch.tar.gz')" \
    && echo "${ADTOF_SOURCE_SHA256}  /tmp/adtof-pytorch.tar.gz" | sha256sum -c -

RUN python -m pip install \
      --index-url https://download.pytorch.org/whl/cpu \
      torch==2.7.1+cpu \
    && python -m pip install \
      audioread==3.1.0 \
      certifi==2026.7.22 \
      cffi==2.1.0 \
      charset-normalizer==3.4.9 \
      decorator==5.3.1 \
      idna==3.18 \
      importlib-resources==7.1.0 \
      joblib==1.5.3 \
      lazy-loader==0.5 \
      librosa==0.11.0 \
      llvmlite==0.48.0 \
      mido==1.3.3 \
      msgpack==1.2.1 \
      narwhals==2.24.0 \
      numba==0.66.0 \
      numpy==2.4.6 \
      packaging==26.2 \
      platformdirs==4.11.0 \
      pooch==1.9.0 \
      pretty-midi==0.2.11.post0 \
      pycparser==3.0 \
      requests==2.34.2 \
      scikit-learn==1.9.0 \
      scipy==1.17.1 \
      six==1.17.0 \
      soundfile==0.14.0 \
      soxr==1.1.0 \
      threadpoolctl==3.6.0 \
      urllib3==2.7.0 \
    && python -m pip install --no-deps /tmp/adtof-pytorch.tar.gz \
    && rm /tmp/adtof-pytorch.tar.gz \
    && echo "${ADTOF_CHECKPOINT_SHA256}  /usr/local/lib/python3.11/site-packages/adtof_pytorch/data/adtof_frame_rnn_pytorch_weights.pth" | sha256sum -c -

COPY run.py /app/run.py
RUN chmod -R a=rX /app /usr/local/lib/python3.11/site-packages/adtof_pytorch

USER 65532:65532
ENTRYPOINT ["python", "/app/run.py"]
