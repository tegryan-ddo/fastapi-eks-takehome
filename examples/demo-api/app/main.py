"""HTTP API of the demonstration application.

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

GET /health/ready reports exactly two things: lifespan startup has completed,
and a pooled connection answered SELECT 1 within the readiness budget. It makes
no other checks. GET /health/live never touches the database.

The /demo endpoints show persistence through PostgreSQL with synthetic data.
They are not the real service's API.
"""

from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import SERVICE_NAME, SERVICE_VERSION
from app.db import DATABASE_ERRORS, ConfigurationError, Database, Settings, error_summary
from app.logs import log_event

database = Database()

# True between the end of lifespan startup and the start of lifespan shutdown.
started = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global started
    log_event("startup_started", demo=True)
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        # Uvicorn reports the failed startup and exits non-zero.
        log_event("configuration_invalid", level="error", reason=str(error))
        raise
    # Opens no connections, so startup does not depend on the database.
    await database.open(settings)
    started = True
    log_event("startup_complete", demo=True)
    yield
    # Uvicorn reaches this point after it stops accepting connections and
    # in-flight requests finish, or its graceful-shutdown timeout expires.
    started = False
    log_event("shutdown_started")
    await database.close()
    log_event("shutdown_complete")


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)


async def database_error(request, error):
    log_event("database_error", level="error", path=request.url.path, error=error_summary(error))
    return JSONResponse({"detail": "database unavailable"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


for error_class in DATABASE_ERRORS:
    app.add_exception_handler(error_class, database_error)


class NewRecord(BaseModel):
    note: str = Field(min_length=1, max_length=200)


@app.get("/")
async def root():
    return {"service": SERVICE_NAME, "version": SERVICE_VERSION, "demo": True}


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready_check(response: Response):
    if not started:
        reason = "starting"
    elif not await database.check():
        reason = "database unavailable"
    else:
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not ready", "reason": reason}


@app.post("/demo/records", status_code=status.HTTP_201_CREATED)
async def create_record(body: NewRecord):
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "INSERT INTO demo_records (note) VALUES ($1) RETURNING id, note, created_at", body.note
        )
    return dict(row)


@app.get("/demo/records")
async def list_records(limit: int = Query(20, ge=1, le=100)):
    async with database.acquire() as connection:
        rows = await connection.fetch(
            "SELECT id, note, created_at FROM demo_records ORDER BY id DESC LIMIT $1", limit
        )
    return [dict(row) for row in rows]


@app.get("/demo/records/{record_id}")
async def get_record(record_id: int):
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT id, note, created_at FROM demo_records WHERE id = $1", record_id
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "record not found")
    return dict(row)


@app.get("/demo/batch-runs/{business_date}")
async def get_batch_run(business_date: date):
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT business_date, record_count, run_count, first_run_at, last_run_at"
            " FROM batch_runs WHERE business_date = $1",
            business_date,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no batch run for that business date")
    return dict(row)
