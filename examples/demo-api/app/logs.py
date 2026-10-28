"""Structured event logs: one JSON object per line.

Info events go to stdout, errors to stderr. Callers pass only non-sensitive
fields: never credentials, environment dumps, or request bodies.
"""

import json
import sys
from datetime import datetime, timezone

from app import SERVICE_NAME, SERVICE_VERSION


def log_event(event, level="info", **fields):
    record = {
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "event": event,
        **fields,
    }
    stream = sys.stderr if level == "error" else sys.stdout
    print(json.dumps(record), file=stream, flush=True)
