"""Build the demo image and check it under the chart's runtime restrictions.

    python scripts/verify_container.py [--engine docker|podman] [--version 0.1.0]

Runs the container as the image's non-root user with a read-only root
filesystem, a tmpfs /tmp, no capabilities, and no-new-privileges, then:
  1. smoke-tests the HTTP endpoints (scripts/smoke_test.py, including the
     demo flag in GET /);
  2. checks the user, the read-only root, and the writable /tmp;
  3. sends SIGTERM and requires a clean exit, logged lifespan shutdown, and
     completion within Uvicorn's graceful-shutdown allowance;
  4. runs the batch command with the default date, an explicit date, and an
     invalid date.

This is local container verification. It says nothing about Kubernetes,
ALB routing, DNS, TLS, or rollout behaviour.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "demo-api:verify"
CONTAINER = "demo-api-verify"
PORT = 18000
SHUTDOWN_ALLOWANCE_SECONDS = 20  # Uvicorn --timeout-graceful-shutdown in the Dockerfile
RESTRICTIONS = ["--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]

results = []


def record(passed, name, detail=""):
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""), flush=True)


def run(*cmd, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        sys.exit(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stdout}{result.stderr}")
    return result


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def pick_engine(requested):
    for engine in [requested] if requested else ["docker", "podman"]:
        if shutil.which(engine) and run(engine, "info", check=False).returncode == 0:
            return engine
    sys.exit("No working container engine: start Docker or Podman, or pass --engine")


def check_http_server(engine, version):
    run(engine, "run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{PORT}:8000", *RESTRICTIONS, IMAGE)
    smoke = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_test.py"), f"http://127.0.0.1:{PORT}",
                            "--expect-version", version, "--expect-demo"])
    record(smoke.returncode == 0, "HTTP endpoints (smoke test above)")

    uid = run(engine, "exec", CONTAINER, "id", "-u").stdout.strip()
    record(uid == "10001", "runs as UID 10001", f"uid={uid}")
    root_write = run(engine, "exec", CONTAINER, "touch", "/srv/probe", check=False)
    record(root_write.returncode != 0, "root filesystem is read-only", root_write.stderr.strip())
    tmp_write = run(engine, "exec", CONTAINER, "touch", "/tmp/probe", check=False)
    record(tmp_write.returncode == 0, "/tmp is writable")

    started = time.monotonic()
    run(engine, "stop", "-t", "30", CONTAINER)
    elapsed = time.monotonic() - started
    exit_code = run(engine, "inspect", "--format", "{{.State.ExitCode}}", CONTAINER).stdout.strip()
    logs = run(engine, "logs", CONTAINER)
    logged = [e["event"] for e in events(logs.stdout)]
    record(elapsed < SHUTDOWN_ALLOWANCE_SECONDS, "SIGTERM shutdown within allowance",
           f"{elapsed:.1f}s of {SHUTDOWN_ALLOWANCE_SECONDS}s")
    record(exit_code == "0", "clean exit after SIGTERM", f"exit code {exit_code}")
    record("shutdown_complete" in logged, "lifespan shutdown logged", f"events: {logged}")


def check_batch(engine):
    def batch(*args):
        return run(engine, "run", "--rm", *RESTRICTIONS, IMAGE, "python", "-m", "app.batch", *args, check=False)

    today = datetime.now(timezone.utc).date().isoformat()
    default = batch()
    logged = events(default.stdout)
    record(default.returncode == 0 and [e["event"] for e in logged] == ["batch_started", "batch_completed"]
           and logged[0]["business_date"] == today, "batch with default date", f"exit {default.returncode}, {today}")

    explicit = batch("--business-date", "2026-10-27")
    logged = events(explicit.stdout)
    record(explicit.returncode == 0 and logged and logged[-1].get("business_date") == "2026-10-27",
           "batch with explicit date", f"exit {explicit.returncode}")

    invalid = batch("--business-date", "2026-02-30")
    rejected = [e["event"] for e in events(invalid.stderr)]
    record(invalid.returncode == 2 and rejected == ["batch_rejected"] and not invalid.stdout.strip(),
           "batch rejects an invalid date", f"exit {invalid.returncode}, events {rejected}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", choices=["docker", "podman"], help="default: docker if running, else podman")
    parser.add_argument("--version", default="0.1.0", help="APP_VERSION baked into the test image")
    args = parser.parse_args()

    engine = pick_engine(args.engine)
    print(f"Engine: {engine}", flush=True)
    run(engine, "build", "--build-arg", f"APP_VERSION={args.version}", "-t", IMAGE, str(ROOT / "examples" / "demo-api"))
    run(engine, "rm", "-f", CONTAINER, check=False)
    try:
        check_http_server(engine, args.version)
        check_batch(engine)
    finally:
        run(engine, "rm", "-f", CONTAINER, check=False)

    passed = all(results)
    print(f"{'PASSED' if passed else 'FAILED'}: {sum(results)}/{len(results)} container checks")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
