"""Demonstration application for the mobile-api deployment.

Only the assignment brief was supplied, so this package stands in for the real
service. It exercises container startup, health checks, shutdown, PostgreSQL
reads and writes, schema migration, and batch execution, with synthetic data.
It does not integrate a payment provider.
"""

import os

SERVICE_NAME = "demo-api"
SERVICE_VERSION = os.environ.get("APP_VERSION", "dev")
