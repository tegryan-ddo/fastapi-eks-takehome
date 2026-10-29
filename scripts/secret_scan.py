"""Scan the repository for secrets with the pinned gitleaks. Findings are redacted.

    python scripts/secret_scan.py

Coverage:
  history  Every commit reachable from any local branch or tag (gitleaks git).
           Reported as skipped when there is no .git directory, as in a
           CodePipeline source archive.
  files    The current working tree, limited to files Git would track: tracked
           files plus untracked files not excluded by .gitignore. Without Git,
           every file except tool and cache directories.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import tools

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".tools", ".venv", "venv", "reports", "__pycache__", ".pytest_cache"}
LEAKS_FOUND = 99  # gitleaks exit code for findings, distinct from its error exits


def gitleaks(*args, cwd):
    cmd = [tools.path("gitleaks"), *args, "--redact", "--no-banner", "--verbose", f"--exit-code={LEAKS_FOUND}"]
    return subprocess.run(cmd, cwd=cwd).returncode


def outcome(name, code):
    if code == 0:
        print(f"{name}: clean")
    elif code == LEAKS_FOUND:
        print(f"{name}: SECRETS FOUND (details above, values redacted)")
    else:
        print(f"{name}: gitleaks failed with exit code {code}; this scan is incomplete")
    return code == 0


def has_commits():
    return (ROOT / ".git").exists() and subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=ROOT, capture_output=True
    ).returncode == 0


def working_tree_files():
    if (ROOT / ".git").exists():
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        return [f for f in listed.split("\0") if f and (ROOT / f).is_file()]
    files = []
    for directory, subdirectories, names in os.walk(ROOT):
        subdirectories[:] = [d for d in subdirectories if d not in SKIP_DIRS]
        files += [os.path.relpath(os.path.join(directory, n), ROOT) for n in names]
    return files


def scan_working_tree():
    files = working_tree_files()
    print(f"\n== files: {len(files)} working-tree files", flush=True)
    with tempfile.TemporaryDirectory() as copy:
        for relative in files:
            destination = Path(copy, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        return outcome("files", gitleaks("dir", ".", cwd=copy))


def scan_history():
    if not has_commits():
        print("\n== history: SKIPPED (no Git history available); only working-tree files were scanned", flush=True)
        return True
    print("\n== history: all commits reachable from local refs", flush=True)
    return outcome("history", gitleaks("git", "--log-opts=--all", ".", cwd=ROOT))


if __name__ == "__main__":
    results = [scan_history(), scan_working_tree()]
    sys.exit(0 if all(results) else 1)
