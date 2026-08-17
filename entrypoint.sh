#!/bin/sh
# entrypoint.sh — container startup sequence for the app image.
#
# Runs as PID 1 inside the container (this is what `ENTRYPOINT ["./entrypoint.sh"]`
# in the Dockerfile invokes). Three steps, in order:
#   1. Wait for Postgres to accept connections.
#   2. Run pending Alembic migrations (`flask db upgrade`).
#   3. Hand off to gunicorn as the real long-running process.
#
# Seeding is deliberately NOT done here — see requirements/11-deployment.md:
# auto-seeding a production container on every boot is unsafe. Run
# `docker compose exec app flask seed` explicitly, once.

set -e

# --- 1. Wait for the database ------------------------------------------------
# `depends_on: condition: service_healthy` (docker-compose.yml) already waits
# for Postgres's own healthcheck, but that only proves the *container* is up —
# not that Postgres has finished recovery/initialization and is accepting
# application connections yet. This short retry loop closes that gap using
# psycopg (already a dependency, so no extra tooling needed) instead of
# installing a separate wait-for-it script.
echo "entrypoint: waiting for database..."
python -c "
import os
import sys
import time

import psycopg

url = os.environ['DATABASE_URL']
# flask/SQLAlchemy use the 'postgresql+psycopg://' dialect prefix; psycopg's
# own connect() doesn't understand the '+psycopg' driver tag, so strip it.
url = url.replace('postgresql+psycopg://', 'postgresql://')

for attempt in range(30):
    try:
        psycopg.connect(url, connect_timeout=3).close()
        print('entrypoint: database is accepting connections')
        sys.exit(0)
    except psycopg.OperationalError as exc:
        print(f'entrypoint: db not ready yet (attempt {attempt + 1}/30): {exc}')
        time.sleep(1)

print('entrypoint: database never became ready, giving up')
sys.exit(1)
"

# --- 2. Run migrations --------------------------------------------------------
# Running `flask db upgrade` here (rather than as a separate deploy step)
# means the schema is always in sync with whatever image version is running,
# for both dev (`docker compose up`) and prod (`docker compose -f
# docker-compose.prod.yml up -d --build`) — one code path, no manual migration
# step to forget.
echo "entrypoint: running migrations..."
flask db upgrade

# --- 3. Start the app server --------------------------------------------------
# Run whatever command the container was given as its final argument list
# (Docker CMD, or docker-compose's `command:` override) rather than a
# hardcoded gunicorn invocation. The Dockerfile's CMD defaults to the
# gunicorn command below for prod; docker-compose.yml overrides it in dev to
# `flask run --debug ...` for live reload. Either way, "$@" is what carries
# that choice through to here — without it, this script would always run
# gunicorn and the dev `command:` override in docker-compose.yml would be
# silently ignored.
#
# `exec` replaces this shell process with "$@" instead of running it as a
# child process. That matters because this script is PID 1 in the container:
# without `exec`, signals like SIGTERM (sent by `docker stop`) go to the shell,
# which by default does not forward them to children, so the container would
# hang until Docker's kill timeout forcibly SIGKILLs it. With `exec`, the app
# server *becomes* PID 1 and receives signals directly, enabling fast, clean
# shutdown.
echo "entrypoint: starting app server..."
exec "$@"
