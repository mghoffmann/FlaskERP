"""db.py -- token-optimized database lifecycle helper for agents.

Why this exists: agents need to bring up/reset Postgres without memorizing
docker compose incantations or psql syntax. All SQL runs inside the `db`
container via `docker compose exec`; `flask db upgrade` / `flask seed` run
on the host using the current Python environment. One line of output per
action, no prose, exit codes matter.

Usage:
    .venv/Scripts/python.exe scripts/db.py -u   # compose up db, wait until ready (60s timeout)
    .venv/Scripts/python.exe scripts/db.py -t   # drop+recreate empty erp_test
    .venv/Scripts/python.exe scripts/db.py -r   # reset dev db `erp` schema + migrate + seed
    .venv/Scripts/python.exe scripts/db.py -u -t -r   # combine; always runs in -u, -t, -r order

Flags are combinable and always execute in the order -u, -t, -r regardless
of the order given on the command line.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

DEV_DB_URL = "postgresql+psycopg://erp:erp@localhost:5432/erp"
READY_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 2


def run(cmd, cwd=None, env=None):
    """Run a subprocess with an explicit arg list (no shell=True) and return CompletedProcess."""
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def cmd_up(repo_root: Path) -> int:
    """`docker compose up -d db`, then poll pg_isready inside the container until ready."""
    up = run(["docker", "compose", "up", "-d", "db"], cwd=repo_root)
    if up.returncode != 0:
        print("db timeout")
        sys.stderr.write(up.stderr)
        return 1

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        check = run(
            ["docker", "compose", "exec", "-T", "db", "pg_isready", "-U", "erp", "-d", "erp"],
            cwd=repo_root,
        )
        if check.returncode == 0:
            print("db ready")
            return 0
        time.sleep(POLL_INTERVAL_SECONDS)

    print("db timeout")
    return 1


def cmd_reset_test(repo_root: Path) -> int:
    """Drop and recreate an empty erp_test database (two statements, pg16 DROP...WITH (FORCE))."""
    sql = "DROP DATABASE IF EXISTS erp_test WITH (FORCE); CREATE DATABASE erp_test;"
    result = run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "erp", "-d", "erp", "-c", sql],
        cwd=repo_root,
    )
    if result.returncode != 0:
        print("erp_test reset failed")
        sys.stderr.write(result.stderr)
        return 1
    print("erp_test reset")
    return 0


def cmd_reset_dev(repo_root: Path) -> int:
    """Terminate connections to erp, drop+recreate its public schema, then migrate + seed on host."""
    sql = (
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = 'erp' AND pid <> pg_backend_pid(); "
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    )
    result = run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "erp", "-d", "erp", "-c", sql],
        cwd=repo_root,
    )
    if result.returncode != 0:
        print("erp reset failed")
        sys.stderr.write(result.stderr)
        return 1

    migrations_dir = repo_root / "migrations"
    if not migrations_dir.is_dir():
        print("no migrations")
        return 0

    env = os.environ.copy()
    env.setdefault("DATABASE_URL", DEV_DB_URL)
    env.setdefault("FLASK_APP", "app")

    upgrade = run([sys.executable, "-m", "flask", "db", "upgrade"], cwd=repo_root, env=env)
    if upgrade.returncode != 0:
        print("flask db upgrade failed")
        sys.stderr.write(upgrade.stderr)
        return 1

    seed = run([sys.executable, "-m", "flask", "seed"], cwd=repo_root, env=env)
    if seed.returncode != 0:
        print("flask seed failed")
        sys.stderr.write(seed.stderr)
        return 1

    print("erp reset+seeded")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="db.py",
        description="Database lifecycle helper: bring up db, reset erp_test, reset+seed dev erp.",
    )
    parser.add_argument("-u", dest="up", action="store_true", help="compose up db, wait until ready")
    parser.add_argument("-t", dest="reset_test", action="store_true", help="drop+recreate empty erp_test")
    parser.add_argument("-r", dest="reset_dev", action="store_true", help="reset dev erp schema + migrate + seed")
    args = parser.parse_args()

    if not (args.up or args.reset_test or args.reset_dev):
        parser.print_help()
        return 0

    repo_root = Path(__file__).resolve().parent.parent

    if args.up:
        rc = cmd_up(repo_root)
        if rc != 0:
            return rc
    if args.reset_test:
        rc = cmd_reset_test(repo_root)
        if rc != 0:
            return rc
    if args.reset_dev:
        rc = cmd_reset_dev(repo_root)
        if rc != 0:
            return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
