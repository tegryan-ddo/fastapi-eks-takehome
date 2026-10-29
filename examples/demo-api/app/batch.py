"""Demonstration daily batch job.

    python -m app.batch [--business-date YYYY-MM-DD]

Logs a start and a completion event and does nothing else: no database, no
network calls, no file writes. It exists to exercise the image as a
Kubernetes Job; real business processing is an integration requirement.

Without --business-date it uses the current UTC date. That is a demo default;
the real job's business-date semantics are still to be agreed.
"""

import argparse
import re
import sys
import time
from datetime import date, datetime, timezone

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

    started = time.monotonic()
    log_event("batch_started", operation=OPERATION, demo=True,
              business_date=business_date.isoformat(), business_date_source=source)
    # Demo: no processing and no external side effects.
    log_event("batch_completed", operation=OPERATION, demo=True, business_date=business_date.isoformat(),
              records_processed=0, duration_seconds=round(time.monotonic() - started, 3))
    return 0


if __name__ == "__main__":
    sys.exit(main())
