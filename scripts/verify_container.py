"""Build the demo image and check it under the chart's runtime restrictions,
against a throwaway PostgreSQL container.

    python scripts/verify_container.py [--engine docker|podman] [--version 0.1.0]

Every container runs as the image's non-root user with a read-only root
filesystem, a tmpfs /tmp, no capabilities, and no-new-privileges. The checks:
  1. start PostgreSQL and apply the schema with `python -m app.migrate` twice;
     the second run must apply nothing;
  2. start the API and smoke-test the HTTP endpoints (scripts/smoke_test.py,
     including the demo flag in GET /);
  3. the user, the read-only root, and the writable /tmp;
  4. write a record through the API, replace the API container with a new one,
     and read the record back;
  5. run the batch twice for one business date: one summary row, run_count 1
     then 2;
  6. stop PostgreSQL: liveness stays 200, readiness turns 503 within a bound,
     each readiness check stays bounded, and the API container keeps running;
     start PostgreSQL again and readiness returns to 200 within a bound;
  7. the host-side integration tests (tests/integration) against the same
     database;
  8. SIGTERM: a clean exit, logged lifespan shutdown, and completion within
     Uvicorn's graceful-shutdown allowance;
  9. failure paths: an invalid batch date exits 2; the API, migrate, and batch
     exit non-zero with a clear event when DATABASE_URL is missing.

This is local container verification. Step 4 replaces a container, not a
Kubernetes pod. Nothing here covers Kubernetes, ALB routing, DNS, TLS to RDS,
or rollout behaviour.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "demo-api:verify"
POSTGRES_IMAGE = "public.ecr.aws/docker/library/postgres:17.11-alpine"
NETWORK = "demo-api-verify"
API = "demo-api-verify-api"
DB = "demo-api-verify-db"
PORT = 18000
DB_PORT = 15432
RESTRICTIONS = ["--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]

# Obviously dummy credentials for the throwaway database only.
DB_USER, DB_PASSWORD, DB_NAME = "demo", "dummy-local-password", "demo"
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB}:5432/{DB_NAME}?sslmode=disable"
HOST_DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@127.0.0.1:{DB_PORT}/{DB_NAME}?sslmode=disable"
BUSINESS_DATE = "1999-12-31"

SHUTDOWN_ALLOWANCE_SECONDS = 20  # Uvicorn --timeout-graceful-shutdown in the Dockerfile
READINESS_PROBE_TIMEOUT_SECONDS = 2  # readinessProbe.timeoutSeconds in the chart
OUTAGE_DETECTED_WITHIN_SECONDS = 15
RECOVERED_WITHIN_SECONDS = 60

results = []


def record(passed, name, detail=""):
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""), flush=True)


def run(*cmd, check=True, timeout=None):
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and result.returncode != 0:
        shown = " ".join(cmd).replace(DB_PASSWORD, "***")
        sys.exit(f"Command failed ({result.returncode}): {shown}\n{result.stdout}{result.stderr}")
    return result


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def wait_until(condition, bound_seconds, interval=0.5):
    """Seconds until condition() is true, or None if it is not within the bound."""
    started = time.monotonic()
    while True:
        if condition():
            return time.monotonic() - started
        if time.monotonic() - started > bound_seconds:
            return None
        time.sleep(interval)


def http(method, path, body=None, timeout=3):
    """(status, JSON body) from the API container; raises OSError when unreachable."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def ready_status():
    try:
        return http("GET", "/health/ready")[0]
    except OSError:
        return None


def pick_engine(requested):
    for engine in [requested] if requested else ["docker", "podman"]:
        if shutil.which(engine) and run(engine, "info", check=False).returncode == 0:
            return engine
    sys.exit("No working container engine: start Docker or Podman, or pass --engine")


def one_off(engine, *command, database_url=DATABASE_URL):
    """Run a command from the demo image on the test network and return the result."""
    env = ["-e", f"DATABASE_URL={database_url}"] if database_url else []
    return run(engine, "run", "--rm", "--network", NETWORK, *RESTRICTIONS, *env, IMAGE, *command, check=False)


def start_database(engine):
    run(engine, "run", "-d", "--name", DB, "--network", NETWORK, "-p", f"127.0.0.1:{DB_PORT}:5432",
        "-e", f"POSTGRES_USER={DB_USER}", "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", "-e", f"POSTGRES_DB={DB_NAME}",
        POSTGRES_IMAGE)
    def accepting():
        # Over TCP, so the image's temporary initialization server (Unix socket only) does not count.
        return run(engine, "exec", DB, "pg_isready", "-h", "127.0.0.1", "-U", DB_USER, "-d", DB_NAME,
                   check=False).returncode == 0

    waited = wait_until(accepting, 60)
    if waited is None:
        sys.exit(f"PostgreSQL ({POSTGRES_IMAGE}) did not accept connections within 60s")
    print(f"PostgreSQL ready after {waited:.1f}s ({POSTGRES_IMAGE})", flush=True)


def start_api(engine):
    run(engine, "run", "-d", "--name", API, "--network", NETWORK, "-p", f"127.0.0.1:{PORT}:8000",
        *RESTRICTIONS, "-e", f"DATABASE_URL={DATABASE_URL}", IMAGE)


def check_migrations(engine):
    first = one_off(engine, "python", "-m", "app.migrate")
    applied = [e["version"] for e in events(first.stdout) if e["event"] == "migration_applied"]
    record(first.returncode == 0 and applied == ["0001_initial"], "schema migration applies 0001_initial",
           f"exit {first.returncode}, applied {applied}")

    second = one_off(engine, "python", "-m", "app.migrate")
    complete = [e for e in events(second.stdout) if e["event"] == "migrations_complete"]
    record(second.returncode == 0 and complete and complete[0]["applied"] == [], "repeated migration applies nothing",
           f"exit {second.returncode}")


def check_http_server(engine, version):
    smoke = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_test.py"), f"http://127.0.0.1:{PORT}",
                            "--expect-version", version, "--expect-demo"])
    record(smoke.returncode == 0, "HTTP endpoints (smoke test above)")

    uid = run(engine, "exec", API, "id", "-u").stdout.strip()
    record(uid == "10001", "runs as UID 10001", f"uid={uid}")
    root_write = run(engine, "exec", API, "touch", "/srv/probe", check=False)
    record(root_write.returncode != 0, "root filesystem is read-only", root_write.stderr.strip())
    tmp_write = run(engine, "exec", API, "touch", "/tmp/probe", check=False)
    record(tmp_write.returncode == 0, "/tmp is writable")


def check_persistence_across_container_replacement(engine):
    note = f"verify-container {uuid.uuid4().hex}"
    status, created = http("POST", "/demo/records", {"note": note})
    record(status == 201 and created.get("note") == note, "API writes a record to PostgreSQL", f"id={created.get('id')}")

    run(engine, "rm", "-f", API)
    start_api(engine)
    ready_after = wait_until(lambda: ready_status() == 200, 30)
    record(ready_after is not None, "replacement API container becomes ready",
           f"{ready_after:.1f}s" if ready_after is not None else "not within 30s")

    status, fetched = http("GET", f"/demo/records/{created.get('id')}")
    record(status == 200 and fetched == created, "record read back after container replacement",
           "new API container, same database")


def check_batch_replay(engine):
    def completed(result):
        return next((e for e in events(result.stdout) if e["event"] == "batch_completed"), {})

    first = completed(one_off(engine, "python", "-m", "app.batch", "--business-date", BUSINESS_DATE))
    record(first.get("run_count") == 1 and first.get("replayed") is False, "batch run writes the summary row",
           f"record_count={first.get('record_count')}, run_count={first.get('run_count')}")

    second = completed(one_off(engine, "python", "-m", "app.batch", "--business-date", BUSINESS_DATE))
    record(second.get("run_count") == 2 and second.get("replayed") is True, "batch replay refreshes the same row",
           f"run_count={second.get('run_count')}, replayed={second.get('replayed')}")

    status, summary = http("GET", f"/demo/batch-runs/{BUSINESS_DATE}")
    record(status == 200 and summary.get("run_count") == 2 and summary.get("record_count") == second.get("record_count"),
           "API reads the batch summary: one row, run_count 2", f"{summary}")


def check_outage_and_recovery(engine):
    run(engine, "stop", "-t", "5", DB)
    detected_after = wait_until(lambda: ready_status() == 503, OUTAGE_DETECTED_WITHIN_SECONDS)
    record(detected_after is not None, "readiness fails while the database is down",
           f"503 after {detected_after:.1f}s" if detected_after is not None else f"not within {OUTAGE_DETECTED_WITHIN_SECONDS}s")

    status, body = http("GET", "/health/ready")
    record(body == {"status": "not ready", "reason": "database unavailable"}, "readiness names the reason", f"{status} {body}")
    record(http("GET", "/health/live")[0] == 200, "liveness stays 200 during the outage")

    started = time.monotonic()
    ready_status()
    elapsed = time.monotonic() - started
    record(elapsed < READINESS_PROBE_TIMEOUT_SECONDS, "each readiness check stays within the probe timeout",
           f"{elapsed:.2f}s < {READINESS_PROBE_TIMEOUT_SECONDS}s")

    state = run(engine, "inspect", "--format", "{{.State.Running}} restarts={{.RestartCount}}", API).stdout.strip()
    record(state == "true restarts=0", "API container keeps running, no exit or restart", state)

    run(engine, "start", DB)
    recovered_after = wait_until(lambda: ready_status() == 200, RECOVERED_WITHIN_SECONDS)
    record(recovered_after is not None, "readiness recovers after the database returns",
           f"200 after {recovered_after:.1f}s" if recovered_after is not None else f"not within {RECOVERED_WITHIN_SECONDS}s")


def check_integration_tests():
    result = subprocess.run([sys.executable, "-m", "pytest", "tests/integration", "-q", "-p", "no:cacheprovider"],
                            cwd=ROOT, env={**os.environ, "DATABASE_URL": HOST_DATABASE_URL})
    record(result.returncode == 0, "host integration tests against the container database (pytest output above)")


def check_shutdown(engine):
    started = time.monotonic()
    run(engine, "stop", "-t", "30", API)
    elapsed = time.monotonic() - started
    exit_code = run(engine, "inspect", "--format", "{{.State.ExitCode}}", API).stdout.strip()
    logged = [e["event"] for e in events(run(engine, "logs", API).stdout)]
    record(elapsed < SHUTDOWN_ALLOWANCE_SECONDS, "SIGTERM shutdown within allowance",
           f"{elapsed:.1f}s of {SHUTDOWN_ALLOWANCE_SECONDS}s")
    record(exit_code == "0", "clean exit after SIGTERM", f"exit code {exit_code}")
    record("shutdown_complete" in logged, "lifespan shutdown logged", f"events: {logged}")


def check_failure_paths(engine):
    invalid = one_off(engine, "python", "-m", "app.batch", "--business-date", "2026-02-30")
    rejected = [e["event"] for e in events(invalid.stderr)]
    record(invalid.returncode == 2 and rejected == ["batch_rejected"] and not invalid.stdout.strip(),
           "batch rejects an invalid date", f"exit {invalid.returncode}, events {rejected}")

    for name, command in (("batch", ["python", "-m", "app.batch"]), ("migrate", ["python", "-m", "app.migrate"])):
        result = one_off(engine, *command, database_url=None)
        logged = [e["event"] for e in events(result.stderr)]
        record(result.returncode == 1 and logged == ["configuration_invalid"], f"{name} without DATABASE_URL exits 1",
               f"exit {result.returncode}, events {logged}")

    try:
        api = run(engine, "run", "--rm", *RESTRICTIONS, IMAGE, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        record(False, "API without DATABASE_URL exits", "still running after 30s")
        return
    logged = [e["event"] for e in events(api.stderr)]
    record(api.returncode != 0 and "configuration_invalid" in logged, "API without DATABASE_URL exits non-zero",
           f"exit {api.returncode}, events {logged}")


def cleanup(engine):
    run(engine, "rm", "-f", API, DB, check=False)
    run(engine, "network", "rm", NETWORK, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", choices=["docker", "podman"], help="default: docker if running, else podman")
    parser.add_argument("--version", default="0.1.0", help="APP_VERSION baked into the test image")
    args = parser.parse_args()

    engine = pick_engine(args.engine)
    print(f"Engine: {engine}", flush=True)
    run(engine, "build", "--build-arg", f"APP_VERSION={args.version}", "-t", IMAGE, str(ROOT / "examples" / "demo-api"))
    cleanup(engine)
    try:
        run(engine, "network", "create", NETWORK)
        start_database(engine)
        check_migrations(engine)
        start_api(engine)
        check_http_server(engine, args.version)
        check_persistence_across_container_replacement(engine)
        check_batch_replay(engine)
        check_outage_and_recovery(engine)
        check_integration_tests()
        check_shutdown(engine)
        check_failure_paths(engine)
    finally:
        cleanup(engine)

    passed = all(results)
    print(f"{'PASSED' if passed else 'FAILED'}: {sum(results)}/{len(results)} container checks")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
