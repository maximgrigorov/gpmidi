FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --target=/deps -r requirements.txt

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

WORKDIR /app
COPY --from=builder /deps /deps
ENV PYTHONPATH=/deps
COPY reference_time/ ./reference_time/

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "reference_time.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
