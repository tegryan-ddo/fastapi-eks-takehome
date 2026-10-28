"""Demo batch entry point: argument validation, configuration errors, events,
and exit codes. The database path is covered in tests/integration."""

import json
from datetime import datetime, timezone

import pytest

from app import batch

# A closed local port: connection attempts are refused at once.
UNREACHABLE_URL = "postgresql://demo:dummy-password@127.0.0.1:1/demo?sslmode=disable"


def fixed_now():
    return datetime(2026, 10, 28, 23, 59, tzinfo=timezone.utc)


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_URL)


@pytest.fixture
def fake_database(monkeypatch):
    """Replace the database step with the summary a first run would produce."""
    dates = []

    async def run(settings, business_date):
        dates.append(business_date)
        return {"record_count": 3, "run_count": 1}

    monkeypatch.setattr(batch, "run", run)
    return dates


def test_default_business_date_is_the_current_utc_date(configured, fake_database, capsys):
    assert batch.main([], now=fixed_now) == 0
    started, completed = events(capsys.readouterr().out)
    assert (started["event"], completed["event"]) == ("batch_started", "batch_completed")
    assert started["business_date"] == completed["business_date"] == "2026-10-28"
    assert started["business_date_source"] == "default-current-utc-date"
    assert [d.isoformat() for d in fake_database] == ["2026-10-28"]
    assert (completed["record_count"], completed["run_count"], completed["replayed"]) == (3, 1, False)
    assert started["demo"] is completed["demo"] is True


def test_explicit_business_date_is_used(configured, fake_database, capsys):
    assert batch.main(["--business-date", "2024-02-29"], now=fixed_now) == 0
    started, completed = events(capsys.readouterr().out)
    assert started["business_date"] == completed["business_date"] == "2024-02-29"
    assert started["business_date_source"] == "argument"


@pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "20261028", "2026-1-5", "2026-10-28T00:00", "yesterday", ""])
def test_invalid_business_date_fails_before_configuration_is_read(value, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert batch.main(["--business-date", value], now=fixed_now) == 2
    output = capsys.readouterr()
    assert output.out == "", "no batch_started event may be logged for invalid input"
    (rejected,) = events(output.err)
    assert (rejected["event"], rejected["level"]) == ("batch_rejected", "error")


def test_missing_database_url_fails_before_starting(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert batch.main(["--business-date", "2026-10-27"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    (event,) = events(output.err)
    assert event["event"] == "configuration_invalid" and "DATABASE_URL" in event["reason"]


def test_unreachable_database_fails_after_starting(configured, capsys):
    assert batch.main(["--business-date", "2026-10-27"]) == 1
    output = capsys.readouterr()
    assert [e["event"] for e in events(output.out)] == ["batch_started"]
    (failed,) = events(output.err)
    assert (failed["event"], failed["level"]) == ("batch_failed", "error")
    assert "dummy-password" not in output.out + output.err


def test_unknown_option_exits_with_usage_error():
    with pytest.raises(SystemExit) as stopped:
        batch.main(["--date", "2026-10-28"])
    assert stopped.value.code == 2
