"""Against a real PostgreSQL: migrations, the demo records API, readiness, and
batch replay. These run only when DATABASE_URL is set; scripts/verify_container.py
provides a throwaway database. They create only synthetic rows and delete
exactly those rows afterwards.
"""

import asyncio
import json
import os
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import batch, main, migrate
from app.db import Settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set; these tests need a PostgreSQL database"
)

NOTE_PREFIX = "integration-test "


def sql(query, *args):
    async def run():
        connection = await Settings.from_env().connect()
        try:
            return await connection.fetch(query, *args)
        finally:
            await connection.close()

    return asyncio.run(run())


@pytest.fixture(scope="module", autouse=True)
def migrated():
    assert migrate.main() == 0
    assert migrate.main() == 0, "a second migrate run must succeed and apply nothing"


@pytest.fixture
def client():
    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def created_ids():
    ids = []
    yield ids
    if ids:
        sql("DELETE FROM demo_records WHERE id = ANY($1::bigint[])", ids)


@pytest.fixture
def business_date():
    """A synthetic date far in the past, unlikely to collide; its summary row is removed afterwards."""
    chosen = date(1900, 1, 1) + timedelta(days=uuid.uuid4().int % 36500)
    yield chosen
    sql("DELETE FROM batch_runs WHERE business_date = $1", chosen)


def test_readiness_is_ready_with_a_reachable_database(client):
    response = client.get("/health/ready")
    assert (response.status_code, response.json()) == (200, {"status": "ready"})


def test_record_written_through_the_api_is_read_back_from_postgres(client, created_ids):
    note = NOTE_PREFIX + uuid.uuid4().hex
    created = client.post("/demo/records", json={"note": note})
    assert created.status_code == 201
    record = created.json()
    created_ids.append(record["id"])

    assert client.get(f"/demo/records/{record['id']}").json() == record
    assert record["id"] in [r["id"] for r in client.get("/demo/records?limit=100").json()]
    # The row is in PostgreSQL, not only in the process that wrote it.
    assert [row["note"] for row in sql("SELECT note FROM demo_records WHERE id = $1", record["id"])] == [note]


def test_record_survives_an_application_restart(created_ids):
    note = NOTE_PREFIX + uuid.uuid4().hex
    with TestClient(main.app) as first:
        record = first.post("/demo/records", json={"note": note}).json()
    created_ids.append(record["id"])
    # A new lifespan and a new pool stand in for a replacement process.
    with TestClient(main.app) as second:
        assert second.get(f"/demo/records/{record['id']}").json() == record


def test_unknown_record_and_batch_run_are_not_found(client):
    assert client.get("/demo/records/9223372036854775807").status_code == 404
    assert client.get("/demo/batch-runs/1899-12-31").status_code == 404


def test_note_validation(client):
    assert client.post("/demo/records", json={"note": ""}).status_code == 422
    assert client.post("/demo/records", json={"note": "x" * 201}).status_code == 422


def test_batch_replay_refreshes_one_row_per_business_date(client, created_ids, business_date, capsys):
    created_ids.append(client.post("/demo/records", json={"note": NOTE_PREFIX + "batch"}).json()["id"])
    expected_count = sql("SELECT count(*) AS n FROM demo_records")[0]["n"]
    arguments = ["--business-date", business_date.isoformat()]

    assert batch.main(arguments) == 0
    first = client.get(f"/demo/batch-runs/{business_date.isoformat()}").json()
    assert (first["record_count"], first["run_count"]) == (expected_count, 1)

    assert batch.main(arguments) == 0
    second = client.get(f"/demo/batch-runs/{business_date.isoformat()}").json()
    assert (second["record_count"], second["run_count"]) == (expected_count, 2)
    assert second["first_run_at"] == first["first_run_at"]
    assert second["last_run_at"] >= first["last_run_at"]
    assert sql("SELECT count(*) AS n FROM batch_runs WHERE business_date = $1", business_date)[0]["n"] == 1

    completed = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"batch_completed"' in line]
    assert [event["replayed"] for event in completed] == [False, True]
