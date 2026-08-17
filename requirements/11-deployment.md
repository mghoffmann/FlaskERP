# 11 — Docker & Digital Ocean Deployment

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md).

## Goals

A reviewer must be able to run the whole system with **one command** locally, and the author must be able to deploy to a fresh Digital Ocean droplet in **under 30 minutes** by following the repo README's deploy section (which this doc specifies).

## Dockerfile (app image)

- `FROM python:3.12-slim`; non-root user; `pip install -r requirements.txt` as its own cached layer; copy the app; `EXPOSE 8000`.
- Entrypoint `entrypoint.sh`: wait for the DB to accept connections (short psycopg retry loop), `flask db upgrade`, then `exec gunicorn -w 2 -b 0.0.0.0:8000 "app:create_app()"`.
- Seeding is **not** automatic in the entrypoint: run `docker compose exec app flask seed` once, explicitly (deliberate — auto-seeding a production container is the kind of thing the job posting's "verify before deploy" bullet is about).

## docker-compose.yml (dev)

- `db`: `postgres:16`, env `POSTGRES_USER/PASSWORD/DB` = `erp`, named volume, healthcheck `pg_isready`, port 5432 published to localhost (for psql/pytest).
- `app`: `build: .`, port 8000:8000, env from `.env`, `depends_on: db: condition: service_healthy`, bind-mount the source and run `flask run --debug --host 0.0.0.0` instead of gunicorn (override `command:`) for live reload.
- Bring-up: `cp .env.example .env`, `docker compose up`, `docker compose exec app flask seed` → login at `http://localhost:8000` as `admin`.

## docker-compose.prod.yml

- `db`: as above, but **no published port** (reachable only on the compose network) and a distinct volume.
- `app`: image built on the droplet (`build: .`), gunicorn entrypoint (no bind mounts, no debug), no published port, `restart: unless-stopped`, env from `.env` (real `SECRET_KEY`, strong `POSTGRES_PASSWORD`, strong seed passwords).
- `caddy`: `caddy:2`, ports 80/443, `restart: unless-stopped`, volumes for `Caddyfile` + cert storage. Caddyfile:
  ```
  {$DOMAIN}
  reverse_proxy app:8000
  ```
  With a real domain/subdomain pointed at the droplet, Caddy obtains Let's Encrypt certificates automatically — zero TLS configuration. **Fallback without a domain**: set the Caddyfile site to `http://` on port 80 and serve plain HTTP at the droplet IP (acceptable for a short-lived demo; prefer the domain — HTTPS on a demo you built in hours is a strong signal).

## Digital Ocean runbook (goes in the repo README)

1. Create the cheapest droplet from the **Docker on Ubuntu** marketplace image; add your SSH key. Enable the DO cloud firewall: inbound 22/80/443 only.
2. DNS: add an A record (e.g. `erp.yourdomain.com`) → droplet IP.
3. SSH in; `git clone` the repo; `cp .env.example .env` and fill in: `DOMAIN`, generated `SECRET_KEY` (`python -c "import secrets; print(secrets.token_hex(32))"`), strong `POSTGRES_PASSWORD`, `SEED_ADMIN_PASSWORD`, `SEED_OPERATOR_PASSWORD`.
4. `docker compose -f docker-compose.prod.yml up -d --build` (entrypoint runs migrations).
5. `docker compose -f docker-compose.prod.yml exec app flask seed`.
6. Verify: `https://erp.yourdomain.com` → login page; log in; complete the seeded released work order and watch the dashboard change.
7. Redeploy on change: `git pull && docker compose -f docker-compose.prod.yml up -d --build` (migrations run automatically).

## Security notes (state these in the repo README — the awareness is part of the demo)

- Secrets live only in `.env` on the droplet (gitignored; `.env.example` documents every variable with placeholder values).
- The database is never exposed publicly; only Caddy publishes ports.
- Containers run as non-root; session cookies are Secure+HttpOnly in prod.
- The demo credentials will be shared with the hiring contact — the operator account is the natural one to hand out; keep admin for walkthroughs.
- Known accepted gaps for a demo: no CSRF tokens, no rate limiting, no backups. Listing these honestly beats pretending they don't exist.

## Acceptance criteria

- Fresh clone on a machine with only Docker: `docker compose up` + seed → working app on localhost, zero host Python required.
- Fresh droplet to public HTTPS URL following only the README runbook, < 30 minutes.
- `docker compose -f docker-compose.prod.yml config` shows no published DB port and no debug settings.
- Killing the app container (`docker kill`) and letting it restart loses no data and requires no manual steps.
