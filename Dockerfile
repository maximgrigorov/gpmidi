FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    GPMIDI_DATA_ROOT=/var/lib/gpmidi

RUN apt-get update \
    && apt-get install -y --no-install-recommends cups-client fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# tuttut declares obsolete GUI dependencies. Its logic path still imports
# matplotlib, so install a current wheel and then tuttut without dependencies.
# `setuptools` from requirements.txt supplies the `pkg_resources` tuttut imports
# at runtime, which recent Python no longer bundles.
RUN sed '/^tuttut==/d' requirements.txt > /tmp/requirements-headless.txt \
    && pip install --no-cache-dir --default-timeout=120 --retries=5 -r /tmp/requirements-headless.txt \
    && pip install --no-cache-dir --default-timeout=120 --retries=5 'matplotlib==3.7.5' \
    && pip install --no-cache-dir --default-timeout=120 --retries=5 --no-deps tuttut==0.0.6

COPY . .

# Run as a non-root account so the Kubernetes deployment can satisfy the
# `restricted` Pod Security Standard with a read-only root filesystem. Converter
# session data goes to GPMIDI_DATA_ROOT, which the deployment mounts writable.
RUN groupadd --gid 10001 gpmidi \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin gpmidi \
    && mkdir -p /var/lib/gpmidi/sessions \
    && chown -R 10001:10001 /var/lib/gpmidi

USER 10001:10001

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "120", "--access-logfile", "-", "app:app"]
