"""Demonstration daily batch job.

    python -m app.batch [--business-date YYYY-MM-DD]

In one transaction it counts the demo records and writes a run summary for the
business date. Replaying a date never adds a row: the existing summary is
refreshed and its run_count incremented. That is this demo's replay behaviour,
not evidence about the real batch job.

Without --business-date it uses the current UTC date. That is a demo default;
the real job's business-date semantics are still to be agreed.

Exit codes: 0 completed, 1 configuration or database failure, 2 invalid input.
"""

import argparse
import asyncio
import re
import sys
import time
from datetime import date, datetime, timezone

from app.db import DATABASE_ERRORS, ConfigurationError, Settings, error_summary
from app.logs import log_event

OPERATION = "demo-daily-batch"


def parse_business_date(text):
    """Return the date for a strict YYYY-MM-DD string, or raise ValueError."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("--business-date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("--business-date is not a valid calendar date") from None


async def run(settings, business_date):
    """Count the records and upsert the business date's summary, in one transaction."""
    connection = await settings.connect()
    try:
        async with connection.transaction():
            count = await connection.fetchval("SELECT count(*) FROM demo_records")
            row = await connection.fetchrow(
                "INSERT INTO batch_runs (business_date, record_count) VALUES ($1, $2)"
                " ON CONFLICT (business_date) DO UPDATE"
                " SET record_count = EXCLUDED.record_count,"
                "     run_count = batch_runs.run_count + 1,"
                "     last_run_at = now()"
                " RETURNING record_count, run_count",
                business_date,
                count,
            )
        return dict(row)
    finally:
        await connection.close()


def main(argv=None, now=lambda: datetime.now(timezone.utc)):
    parser = argparse.ArgumentParser(prog="python -m app.batch", description=__doc__.split("\n\n")[0])
    parser.add_argument("--business-date", help="YYYY-MM-DD; default: current UTC date (demo default)")
    args = parser.parse_args(argv)

    if args.business_date is None:
        business_date, source = now().date(), "default-current-utc-date"
    else:
        try:
            business_date, source = parse_business_date(args.business_date), "argument"
        except ValueError as error:
            log_event("batch_rejected", level="error", operation=OPERATION, demo=True, reason=str(error))
            return 2

    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        log_event("configuration_invalid", level="error", operation=OPERATION, reason=str(error))
        return 1

    started = time.monotonic()
    log_event("batch_started", operation=OPERATION, demo=True,
              business_date=business_date.isoformat(), business_date_source=source)
    try:
        summary = asyncio.run(run(settings, business_date))
    except DATABASE_ERRORS as error:
        log_event("batch_failed", level="error", operation=OPERATION, demo=True,
                  business_date=business_date.isoformat(), error=error_summary(error))
        return 1
    log_event("batch_completed", operation=OPERATION, demo=True, business_date=business_date.isoformat(),
              record_count=summary["record_count"], run_count=summary["run_count"],
              replayed=summary["run_count"] > 1, duration_seconds=round(time.monotonic() - started, 3))
    return 0


if __name__ == "__main__":
    sys.exit(main())
