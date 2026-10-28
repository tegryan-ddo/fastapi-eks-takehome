"""Apply the versioned schema in app/migrations, in order, once each.

    python -m app.migrate

Runs as a controlled release step before the application deploys, never from
API pods or health endpoints. Repeating it is safe: applied versions are
recorded in schema_migrations and skipped. Concurrent runs wait on an advisory
lock, so no two runs apply the same migration.
"""

import asyncio
import sys
from pathlib import Path

from app.db import DATABASE_ERRORS, ConfigurationError, Settings, error_summary
from app.logs import log_event

MIGRATIONS = Path(__file__).with_name("migrations")
LOCK_KEY = 20261028  # any constant; every migrate run takes the same session lock
LOCK_WAIT_SECONDS = 30
MIGRATION_TIMEOUT_SECONDS = 60


async def apply_pending(connection):
    """Apply every migration file not yet recorded. Returns the versions applied."""
    applied = []
    await connection.execute("SELECT pg_advisory_lock($1)", LOCK_KEY, timeout=LOCK_WAIT_SECONDS)
    try:
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row["version"] for row in await connection.fetch("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            if path.stem in done:
                continue
            # One transaction per migration: it is applied and recorded together, or not at all.
            async with connection.transaction():
                await connection.execute(path.read_text(), timeout=MIGRATION_TIMEOUT_SECONDS)
                await connection.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
            log_event("migration_applied", version=path.stem)
            applied.append(path.stem)
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
    return applied


async def migrate(settings):
    connection = await settings.connect()
    try:
        return await apply_pending(connection)
    finally:
        await connection.close()


def main():
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        log_event("configuration_invalid", level="error", reason=str(error))
        return 1
    try:
        applied = asyncio.run(migrate(settings))
    except DATABASE_ERRORS as error:
        log_event("migration_failed", level="error", error=error_summary(error))
        return 1
    log_event("migrations_complete", applied=applied)
    return 0


if __name__ == "__main__":
    sys.exit(main())
