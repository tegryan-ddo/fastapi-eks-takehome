"""Demonstration application for the mobile-api deployment.

Only the assignment brief was supplied, so this package stands in for the real
service. It exercises container startup, health checks, shutdown, and batch
execution. It does not touch Postgres or a payment provider.
"""

import os

SERVICE_NAME = "demo-api"
SERVICE_VERSION = os.environ.get("APP_VERSION", "dev")
