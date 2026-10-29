"""Create the application's database role from the master account.

Runs in the mobile-api-db-admin CodeBuild project (buildspecs/db-bootstrap.yml).
CodeBuild fills these environment variables from Secrets Manager:

    PGHOST PGPORT PGDATABASE PGMASTER_USER PGMASTER_PASSWORD   rds-master secret
    APP_DATABASE_URL                                            runtime secret

The role name and password come from APP_DATABASE_URL, which the operator
stored with scripts/load_secrets.py. Nothing secret is printed, and errors are
reported without their original messages.
"""

import asyncio
import os
import ssl
import sys
import urllib.parse
from pathlib import Path

import asyncpg

APP_ROLE = "mobile_api_app"
SQL = Path(__file__).with_name("create_app_user.sql").read_text()


def required(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Error: {name} is not set")
    return value


def app_credentials(master_host, master_database):
    url = urllib.parse.urlsplit(required("APP_DATABASE_URL"))
    user = urllib.parse.unquote(url.username or "")
    password = urllib.parse.unquote(url.password or "")
    if user != APP_ROLE:
        sys.exit(f"Error: APP_DATABASE_URL must use the role {APP_ROLE}")
    if not password:
        sys.exit("Error: APP_DATABASE_URL has no password")
    if (url.hostname, url.path.lstrip("/")) != (master_host, master_database):
        sys.exit("Error: APP_DATABASE_URL does not point at this instance and database")
    return password


async def main():
    host, database = required("PGHOST"), required("PGDATABASE")
    password = app_credentials(host, database)
    tls = ssl.create_default_context(cafile=required("DATABASE_CA_BUNDLE"))
    connection = await asyncpg.connect(
        host=host, port=int(required("PGPORT")), database=database,
        user=required("PGMASTER_USER"), password=required("PGMASTER_PASSWORD"),
        ssl=tls, timeout=10, command_timeout=30,
    )
    try:
        async with connection.transaction():
            await connection.execute("SELECT set_config('app.password', $1, true)", password)
            await connection.execute(SQL)
    finally:
        await connection.close()
    print(f"Role {APP_ROLE} is ready on {host}/{database}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as error:
        # Driver messages can echo connection details; report only the class.
        sys.exit(f"Error: database bootstrap failed ({type(error).__name__})")
