"""Deployment smoke-test harness for the mobile-api Helm chart.

This is not the production service. It exists so the container image and the
chart can be exercised end to end before the real application is supplied.

/health/ready reports only that this demo has finished starting and can serve
requests. It does not check Postgres, payment processing, or anything else the
real application depends on.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

SERVICE_NAME = "demo-api"
SERVICE_VERSION = os.environ.get("APP_VERSION", "dev")

# Set once lifespan startup finishes. Uvicorn does not accept connections
# until then, so here it mainly documents the contract the real service must
# honour: report ready only after initialization is complete.
ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ready
    ready = True
    yield


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": SERVICE_NAME, "version": SERVICE_VERSION}


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready_check(response: Response):
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting"}
    return {"status": "ready"}
