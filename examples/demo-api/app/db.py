"""Database access shared by the API, the migration command, and the batch.

Settings come from the environment; DATABASE_URL is required. Nothing here
logs the URL or any part of it: failures are reported by exception class, or
by the server's message for PostgreSQL errors, which never carries credentials.
"""

import asyncio
import os
import ssl
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import asyncpg

from app.logs import log_event

DEFAULT_CA_BUNDLE = "/srv/certs/rds-global-bundle.pem"
SSL_MODES = ("verify-full", "disable")

# What a connection attempt or query raises when the database is unreachable,
# refuses the connection, times out, or rejects the statement.
DATABASE_ERRORS = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError)

# Readiness budget. Together these stay under the probe's 2s timeoutSeconds.
READY_ACQUIRE_SECONDS = 1.0
READY_QUERY_SECONDS = 0.5

CLOSE_TIMEOUT_SECONDS = 5


class ConfigurationError(Exception):
    """Missing or invalid database configuration. Messages contain no secrets."""


def error_summary(error):
    """Loggable description: the server message for PostgreSQL errors, else only the class."""
    if isinstance(error, asyncpg.PostgresError):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


@dataclass(frozen=True)
class Settings:
    dsn: str
    tls: ssl.SSLContext | bool
    pool_min: int
    pool_max: int
    connect_timeout: float
    acquire_timeout: float
    query_timeout: float

    @classmethod
    def from_env(cls, env=os.environ):
        dsn = env.get("DATABASE_URL", "")
        try:
            parts = urlsplit(dsn)
            valid = parts.scheme in ("postgresql", "postgres") and parts.hostname and len(parts.path) > 1
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError(
                "DATABASE_URL must be set to postgresql://user:password@host:5432/database[?sslmode=verify-full]"
            )
        mode = parse_qs(parts.query).get("sslmode", ["verify-full"])[-1]
        if mode not in SSL_MODES:
            raise ConfigurationError(f"DATABASE_URL sslmode must be one of: {', '.join(SSL_MODES)}")

        pool_min = _number(env, "DATABASE_POOL_MIN", 0, int)
        pool_max = _number(env, "DATABASE_POOL_MAX", 5, int)
        if pool_max < 1 or pool_min > pool_max:
            raise ConfigurationError("DATABASE_POOL_MAX must be at least 1 and not below DATABASE_POOL_MIN")
        return cls(
            dsn=dsn,
            tls=_tls_context(env.get("DATABASE_CA_BUNDLE", DEFAULT_CA_BUNDLE)) if mode == "verify-full" else False,
            pool_min=pool_min,
            pool_max=pool_max,
            connect_timeout=_number(env, "DATABASE_CONNECT_TIMEOUT", 3, float),
            acquire_timeout=_number(env, "DATABASE_ACQUIRE_TIMEOUT", 2, float),
            query_timeout=_number(env, "DATABASE_QUERY_TIMEOUT", 5, float),
        )

    async def connect(self):
        """A single connection, for the migration and batch commands."""
        return await asyncpg.connect(
            self.dsn, ssl=self.tls, timeout=self.connect_timeout, command_timeout=self.query_timeout
        )


def _tls_context(bundle):
    """Verify the server certificate against the RDS CA bundle, hostname included."""
    try:
        return ssl.create_default_context(cafile=bundle)
    except (OSError, ssl.SSLError) as error:
        raise ConfigurationError(f"DATABASE_CA_BUNDLE {bundle} cannot be loaded ({type(error).__name__})") from None


def _number(env, name, default, kind):
    try:
        value = kind(env.get(name, default))
    except ValueError:
        raise ConfigurationError(f"{name} must be a number") from None
    if value < 0:
        raise ConfigurationError(f"{name} must not be negative")
    return value


class Database:
    """The API's bounded connection pool.

    Opening the pool connects to nothing: connections are made on first use, so
    the process starts while the database is unreachable, and readiness
    recovers by itself once it is reachable again.
    """

    def __init__(self):
        self.settings = None
        self.pool = None

    async def open(self, settings):
        self.settings = settings
        self.pool = await asyncpg.create_pool(
            settings.dsn,
            ssl=settings.tls,
            min_size=settings.pool_min,
            max_size=settings.pool_max,
            timeout=settings.connect_timeout,
            command_timeout=settings.query_timeout,
        )

    async def close(self):
        if self.pool is None:
            return
        try:
            await asyncio.wait_for(self.pool.close(), CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            self.pool.terminate()
        self.pool = None

    def acquire(self):
        """Context manager for one pooled connection, bounded by the acquire timeout."""
        return self.pool.acquire(timeout=self.settings.acquire_timeout)

    async def check(self):
        """True when a pooled connection answers SELECT 1 within the readiness budget."""
        try:
            async with self.pool.acquire(timeout=READY_ACQUIRE_SECONDS) as connection:
                await connection.fetchval("SELECT 1", timeout=READY_QUERY_SECONDS)
        except DATABASE_ERRORS as error:
            log_event("database_unavailable", level="error", error=error_summary(error))
            return False
        return True
