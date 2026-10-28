"""Demo API contract: identity response, readiness tied to the lifespan, and
lifecycle logs that carry no environment data."""

import json

from fastapi.testclient import TestClient

from app import main


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_root_identifies_the_service_as_a_demo():
    with TestClient(main.app) as client:
        assert client.get("/").json() == {"service": "demo-api", "version": main.SERVICE_VERSION, "demo": True}


def test_readiness_is_true_only_between_startup_and_shutdown():
    client = TestClient(main.app)
    assert client.get("/health/ready").status_code == 503, "ready before lifespan startup ran"
    with client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 503, "still ready after lifespan shutdown"


def test_lifecycle_events_are_logged_without_environment_values(capsys, monkeypatch):
    sentinel = "DUMMY_DATABASE_URL_SENTINEL"
    monkeypatch.setenv("DATABASE_URL", sentinel)
    with TestClient(main.app):
        pass
    output = capsys.readouterr()
    assert [e["event"] for e in events(output.out)] == [
        "startup_started", "startup_complete", "shutdown_started", "shutdown_complete",
    ]
    assert sentinel not in output.out + output.err
