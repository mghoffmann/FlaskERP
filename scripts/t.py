"""t.py -- token-optimized pytest runner for agents.

Why this exists: agents pay per token, so this wraps pytest with terse,
deterministic flags (-q --tb=line --no-header) instead of the verbose
default output. It also supplies a sane default TEST_DATABASE_URL so
agents don't need to know the local Postgres credentials, and no-ops
cleanly (exit 0) when there is no tests/ directory yet (e.g. early in
the build before any tests exist).

Usage:
    .venv/Scripts/python.exe scripts/t.py                # run full suite
    .venv/Scripts/python.exe scripts/t.py -k "stock"      # filter by expr
    .venv/Scripts/python.exe scripts/t.py -x              # stop on first failure
    .venv/Scripts/python.exe scripts/t.py -k "stock" -x   # combine

Output is pytest's own passthrough (already terse via -q --tb=line).
Exit code is pytest's exit code.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_TEST_DB_URL = "postgresql+psycopg://erp:erp@localhost:5432/erp_test"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="t.py",
        description="Run the pytest suite with terse, agent-friendly output.",
    )
    parser.add_argument("-k", dest="expr", metavar="EXPR", help="only run tests matching EXPR")
    parser.add_argument("-x", dest="stop_first", action="store_true", help="stop on first failure")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        print("no tests")
        return 0

    env = os.environ.copy()
    env.setdefault("TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-q",
        "--tb=line",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    if args.expr:
        cmd.extend(["-k", args.expr])
    if args.stop_first:
        cmd.append("-x")

    result = subprocess.run(cmd, cwd=repo_root, env=env)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
