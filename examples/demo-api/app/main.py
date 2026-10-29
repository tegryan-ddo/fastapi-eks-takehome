"""HTTP API of the demonstration application.

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

GET /health/ready reports exactly one thing: whether this process has finished
its lifespan startup and has not begun lifespan shutdown. It makes no network
calls and does not check Postgres, payment processing, or any other dependency.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from app import SERVICE_NAME, SERVICE_VERSION
from app.logs import log_event

# True between the end of lifespan startup and the start of lifespan shutdown.
# Uvicorn accepts connections only after startup, so in practice readiness is
# 200 whenever the server answers; the flag states the contract the real
# service must keep (ready only once initialization is complete).
ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ready
    log_event("startup_started", demo=True)
    # A real service would open its database pool and load configuration here.
    ready = True
    log_event("startup_complete", demo=True)
    yield
    # Uvicorn reaches this point after it stops accepting connections and
    # in-flight requests finish, or its graceful-shutdown timeout expires.
    ready = False
    log_event("shutdown_started")
    log_event("shutdown_complete")


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": SERVICE_NAME, "version": SERVICE_VERSION, "demo": True}


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready_check(response: Response):
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not ready"}
    return {"status": "ready"}
