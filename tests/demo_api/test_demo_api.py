"""Demo API contract without a database: identity, readiness states, lifecycle
logs, and configuration errors. Database behaviour is in tests/integration."""

import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import ConfigurationError

# A closed local port: connection attempts are refused at once, so tests stay fast.
UNREACHABLE_URL = "postgresql://demo:dummy-password@127.0.0.1:1/demo?sslmode=disable"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_URL)


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_root_identifies_the_service_as_a_demo(configured):
    with TestClient(main.app) as client:
        assert client.get("/").json() == {"service": "demo-api", "version": main.SERVICE_VERSION, "demo": True}


def test_readiness_reports_starting_before_startup(configured):
    response = TestClient(main.app).get("/health/ready")
    assert (response.status_code, response.json()) == (503, {"status": "not ready", "reason": "starting"})


def test_readiness_requires_a_reachable_database(configured, capsys):
    with TestClient(main.app) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        response = client.get("/health/ready")
    assert (response.status_code, response.json()) == (503, {"status": "not ready", "reason": "database unavailable"})
    output = capsys.readouterr()
    assert "database_unavailable" in [e["event"] for e in events(output.err)]
    assert "dummy-password" not in output.out + output.err


async def always_ready():
    return True


def test_readiness_is_ready_when_the_database_check_passes(configured, monkeypatch):
    monkeypatch.setattr(main.database, "check", always_ready)
    with TestClient(main.app) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}


def test_demo_endpoints_return_503_when_the_database_is_unreachable(configured, capsys):
    with TestClient(main.app) as client:
        response = client.post("/demo/records", json={"note": "unit test"})
    assert (response.status_code, response.json()) == (503, {"detail": "database unavailable"})
    output = capsys.readouterr()
    assert "database_error" in [e["event"] for e in events(output.err)]
    assert "dummy-password" not in output.out + output.err


def test_startup_fails_clearly_without_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigurationError):
        with TestClient(main.app):
            pass
    (event,) = [e for e in events(capsys.readouterr().err) if e["event"] == "configuration_invalid"]
    assert "DATABASE_URL" in event["reason"]


def test_lifecycle_events_are_logged_without_environment_values(configured, capsys):
    with TestClient(main.app):
        pass
    output = capsys.readouterr()
    assert [e["event"] for e in events(output.out)] == [
        "startup_started", "startup_complete", "shutdown_started", "shutdown_complete",
    ]
    assert "dummy-password" not in output.out + output.err
