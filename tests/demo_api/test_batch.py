"""Demo batch entry point: business-date handling, exit codes, and events."""

import json
from datetime import datetime, timezone

import pytest

from app import batch


def fixed_now():
    return datetime(2026, 10, 28, 23, 59, tzinfo=timezone.utc)


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_default_business_date_is_the_current_utc_date(capsys):
    assert batch.main([], now=fixed_now) == 0
    started, completed = events(capsys.readouterr().out)
    assert (started["event"], completed["event"]) == ("batch_started", "batch_completed")
    assert started["business_date"] == completed["business_date"] == "2026-10-28"
    assert started["business_date_source"] == "default-current-utc-date"
    assert started["demo"] is completed["demo"] is True


def test_explicit_business_date_is_used(capsys):
    assert batch.main(["--business-date", "2024-02-29"], now=fixed_now) == 0
    started, completed = events(capsys.readouterr().out)
    assert started["business_date"] == completed["business_date"] == "2024-02-29"
    assert started["business_date_source"] == "argument"


@pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "20261028", "2026-1-5", "2026-10-28T00:00", "yesterday", ""])
def test_invalid_business_date_fails_before_starting(value, capsys):
    assert batch.main(["--business-date", value], now=fixed_now) == 2
    output = capsys.readouterr()
    assert output.out == "", "no batch_started event may be logged for invalid input"
    (rejected,) = events(output.err)
    assert (rejected["event"], rejected["level"]) == ("batch_rejected", "error")


def test_unknown_option_exits_with_usage_error():
    with pytest.raises(SystemExit) as stopped:
        batch.main(["--date", "2026-10-28"])
    assert stopped.value.code == 2
