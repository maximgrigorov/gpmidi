FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends cups-client fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# tuttut declares obsolete GUI dependencies. Its logic path still imports
# matplotlib, so install a current wheel and then tuttut without dependencies.
RUN sed '/^tuttut==/d' requirements.txt > /tmp/requirements-headless.txt \
    && pip install --no-cache-dir --default-timeout=120 --retries=5 -r /tmp/requirements-headless.txt \
    && pip install --no-cache-dir --default-timeout=120 --retries=5 'matplotlib==3.7.5' \
    && pip install --no-cache-dir --default-timeout=120 --retries=5 --no-deps tuttut==0.0.6

COPY . .
RUN mkdir -p /app/data/sessions

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "app:app"]
