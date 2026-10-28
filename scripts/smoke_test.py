"""Smoke-test a running service at an explicit base URL.

    python scripts/smoke_test.py http://localhost:8000
    python scripts/smoke_test.py https://staging.example.com --expect-version 0.1.0

Waits up to --ready-deadline seconds for the readiness endpoint, then checks
the liveness endpoint and the service identity returned by GET /. HTTPS
certificates are verified.

A pass shows only that the service is reachable, ready, and is the expected
build. The demo's health endpoints do not exercise Postgres, payment
processing, or the batch job.
"""

import argparse
import http.client
import json
import sys
import time
import urllib.parse

LIVE_PATH = "/health/live"
READY_PATH = "/health/ready"


def get(base_url, path, connect_timeout, response_timeout):
    """GET base_url + path. Returns (status, body); raises OSError on network failure."""
    url = urllib.parse.urlsplit(base_url.rstrip("/") + path)
    connection_class = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(url.hostname, url.port, timeout=connect_timeout)
    try:
        connection.connect()
        connection.sock.settimeout(response_timeout)
        connection.request("GET", url.path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def wait_until_ready(args):
    deadline = time.monotonic() + args.ready_deadline
    attempt = 0
    while True:
        attempt += 1
        try:
            status, body = get(args.base_url, READY_PATH, args.connect_timeout, args.response_timeout)
            if status == 200:
                print(f"PASS  GET {READY_PATH} -> 200 (attempt {attempt})")
                return True
            outcome = f"{status} {body[:200]!r}"
        except OSError as error:
            outcome = f"{type(error).__name__}: {error}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"FAIL  GET {READY_PATH} not ready after {attempt} attempts in {args.ready_deadline}s; last: {outcome}")
            return False
        print(f"      GET {READY_PATH} attempt {attempt} -> {outcome}; retrying")
        time.sleep(min(args.retry_interval, remaining))


def check(args, path, expect):
    """One attempt: GET path, then expect(status, body) returns an error string or None."""
    try:
        status, body = get(args.base_url, path, args.connect_timeout, args.response_timeout)
        problem = expect(status, body)
    except OSError as error:
        problem = f"{type(error).__name__}: {error}"
    print(f"{'FAIL' if problem else 'PASS'}  GET {path}" + (f" -> {problem}" if problem else ""))
    return problem is None


def expect_identity(args):
    def expect(status, body):
        if status != 200:
            return f"status {status}, expected 200"
        try:
            data = json.loads(body)
        except ValueError:
            return f"response is not JSON: {body[:200]!r}"
        if data.get("service") != args.expect_service:
            return f"service {data.get('service')!r}, expected {args.expect_service!r}"
        if args.expect_version and data.get("version") != args.expect_version:
            return f"version {data.get('version')!r}, expected {args.expect_version!r}"
        if args.expect_demo and data.get("demo") is not True:
            return f"demo {data.get('demo')!r}, expected true"
        return None

    return expect


def expect_ok(status, body):
    return None if status == 200 else f"status {status}, expected 200"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url", help="e.g. http://localhost:8000 or https://staging.example.com")
    parser.add_argument("--expect-service", default="demo-api", help="expected 'service' in GET / (default: demo-api)")
    parser.add_argument("--expect-version", help="expected 'version' in GET /; not checked if omitted")
    parser.add_argument("--expect-demo", action="store_true", help="require 'demo': true in GET / (demo image only)")
    parser.add_argument("--connect-timeout", type=float, default=3, help="seconds to connect, including TLS (default: 3)")
    parser.add_argument("--response-timeout", type=float, default=5, help="seconds to wait for response data (default: 5)")
    parser.add_argument("--ready-deadline", type=float, default=60, help="seconds to wait for readiness (default: 60)")
    parser.add_argument("--retry-interval", type=float, default=2, help="seconds between readiness attempts (default: 2)")
    args = parser.parse_args()

    if urllib.parse.urlsplit(args.base_url).scheme not in ("http", "https"):
        parser.error("base_url must start with http:// or https://")

    print(f"Smoke test: {args.base_url}")
    passed = wait_until_ready(args) and all([
        check(args, LIVE_PATH, expect_ok),
        check(args, "/", expect_identity(args)),
    ])
    print(f"{'PASSED' if passed else 'FAILED'}: {args.base_url} (reachability and identity only; "
          "no database, payment, or batch checks)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
