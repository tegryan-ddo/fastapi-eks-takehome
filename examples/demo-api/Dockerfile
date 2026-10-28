FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Fixed numeric IDs so Kubernetes runAsNonRoot can verify the user is not root.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /srv

# Dependencies before source: this layer is reused until requirements.txt changes.
# pip check fails the build on inconsistent pins. pip is needed only to build,
# so it is then removed with the vendored libraries it carries, which image
# scanners otherwise report against the runtime image.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip check \
    && pip uninstall --yes pip

COPY app/ ./app/

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

USER 10001:10001
EXPOSE 8000

# Exec form makes Uvicorn PID 1, so it receives SIGTERM directly and runs the
# FastAPI lifespan shutdown. Graceful shutdown is capped at 20s; the chart's
# terminationGracePeriodSeconds (30s) covers this plus the 5s preStop sleep.
# The batch CronJob overrides this with: python -m app.batch
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "20"]
