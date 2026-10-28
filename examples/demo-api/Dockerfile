FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Fixed numeric IDs so Kubernetes runAsNonRoot can verify the user is not root.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies before source: this layer is reused until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

USER 10001:10001
EXPOSE 8000

# Exec form makes Uvicorn PID 1, so it receives SIGTERM directly.
# Graceful shutdown is capped at 20s; the chart's terminationGracePeriodSeconds
# (30s) covers this plus the 5s preStop sleep.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "20"]
