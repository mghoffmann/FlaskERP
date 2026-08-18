# Shopfloor ERP

A small manufacturing ERP demo: **Flask** JSON API, a **plain HTML/JS/CSS** frontend (no
framework, no build step), **PostgreSQL** with **Alembic** migrations, containerized with
**Docker Compose**, deployed to a **Digital Ocean** droplet behind **Caddy**.

Built as a job-application demo — small enough to read end to end, real enough to show
actual relational-database and transactional-inventory work.

<!-- Update OWNER/REPO once the GitHub remote exists. -->
![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)

## Screenshots

<!-- Dashboard: low-stock report + open-document KPIs -->
<!-- Work order detail: component availability + complete transaction -->
<!-- Parts list or BOM editor -->

## What it does

A small factory keeps a catalog of **parts** (raw materials and finished products) and
tracks quantity on hand for each.

| Concept | What happens |
|---|---|
| **BOM** | Each finished product has a Bill of Materials — the recipe of components needed to build one unit. |
| **Work order** | "Build N units of product X." Completing it consumes components and adds finished goods in one atomic transaction. |
| **Purchase order** | Brings raw materials in from suppliers; receiving increments stock. |
| **Sales order** | Ships finished goods to customers; shipping decrements stock. |
| **Stock ledger** | Every stock change is recorded in an append-only movements ledger — nothing touches `qty_on_hand` directly. |
| **Dashboard** | Items below their reorder point, open documents, recent activity. |

Two roles: **admin** (master data, documents, everything) and **operator** (the
factory-floor user — completes work orders, receives POs, ships SOs, adjusts stock counts;
cannot create/edit master data or documents).

## Quick start (Docker)

```bash
cp .env.example .env
docker compose up
docker compose exec app flask seed
```

Open `http://localhost:8000` and log in:

| User | Password | Role |
|---|---|---|
| `admin` | `admin123` | admin |
| `operator` | `operator123` | operator |

### Quick start (host Python, no Docker for the app)

Useful for editing without a container rebuild loop; still needs Postgres, so bring up
just the `db` service first.

```bash
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt
python scripts/db.py -u        # docker compose up db, wait until ready
python scripts/db.py -r        # reset dev schema, migrate, seed
flask run --debug
```

## Tests

```bash
docker compose up -d db
python scripts/db.py -t        # drop/recreate empty erp_test
python scripts/t.py            # or: pytest
```

118 tests covering: document status-transition state machines (work orders, purchase
orders, sales orders — invalid transitions return `409`), the stock service's row-locking
and shortfall behavior in isolation, a role-permission sweep (every endpoint checked
against both admin and operator), plus inventory, BOM, auth, and seed-idempotency.

## Architecture notes

- **App factory** (`create_app(config_name)`) registers extensions, blueprints, error
  handlers, and CLI commands; one config class per environment
  (`app/config.py`).
- **One transaction per request** — handlers get the DB session via `db.session`; commit
  on success, roll back on error.
- **`parts.qty_on_hand` has exactly one writer**: `app/services/stock.py`'s
  `apply_movement()`. Every module (work orders, purchasing, sales orders, manual
  adjustments) goes through it — no direct writes, anywhere.
- **Append-only ledger**: every stock change also writes a `stock_movements` row; history
  is never edited or deleted.
- **Database CHECK constraints** back up application logic (e.g. `qty_on_hand` can't go
  negative at the DB layer, not just in Python).
- **Row locking**: multi-part transactions (work order completion, PO receiving, SO
  shipping) take `SELECT ... FOR UPDATE` on every affected part, in a fixed ascending
  `part_id` order, before touching stock — this is what makes concurrent completions safe
  from both shortfalls and deadlocks. The deep-dive is the module docstring and
  `complete_work_order()` in
  [`app/api/work_orders.py`](app/api/work_orders.py).

## Deliberate scope cuts & tradeoffs

Honest list — a demo built in hours, not a production system. Each is a conscious
tradeoff, not an oversight.

| Cut | Why it's acceptable here |
|---|---|
| No CSRF tokens | JSON-only API + `SameSite=Lax` session cookie means a cross-site page can't trigger an authenticated request that carries the cookie. Standard tradeoff for this shape of API; would revisit for a cookie-auth app serving HTML forms cross-origin. |
| No rate limiting / login lockout | Single-tenant demo with two seeded users; not internet-scale, not handling real credentials. |
| No user registration or user CRUD UI | Both users come from seed data. Adding user management is straightforward but out of scope for the demo's purpose. |
| Single full receipt / shipment (no partials) | Real ERPs receive/ship line-by-line over multiple deliveries. Full-only keeps the transaction logic reviewable; partial support is an additive change, not a redesign. |
| No stock reservations/allocations | Confirming a sales order doesn't reserve stock against it. A real-ERP feature deliberately left out. |
| Last-cost costing (vs. moving average) | Receiving a PO sets a part's `unit_cost` to the line's cost. Simple and explainable; moving-average costing is a well-understood follow-up. |
| No pagination beyond movements | Demo data volumes don't need it anywhere else; the movements ledger is the one list that grows unbounded, so it's the one that's paginated. |
| No backups | Nothing irreplaceable lives in the demo database. |

## Deployment (Digital Ocean)

`docker-compose.prod.yml` and `Caddyfile` (repo root) are the production stack:

- **`db`** — same Postgres image as dev, but **no published port** (reachable only from
  other containers on the compose network) and a distinct named volume.
- **`app`** — built from the same `Dockerfile` as dev, run as-is: no bind mounts, no
  command override, so it runs the image's real entrypoint (migrate, then gunicorn) instead
  of the dev live-reload server. No published port either — only Caddy is internet-facing.
- **`caddy`** — publishes 80/443 and reverse-proxies to `app:8000`. Given a real domain
  pointed at the droplet, it obtains and renews a Let's Encrypt certificate automatically —
  no manual TLS setup. A commented-out plain-HTTP fallback in the Caddyfile covers the
  no-domain case.

### Runbook

1. Create the cheapest droplet from the **Docker on Ubuntu** marketplace image; add your
   SSH key. Enable the Digital Ocean cloud firewall: inbound `22`/`80`/`443` only.
2. DNS: add an A record (e.g. `erp.yourdomain.com`) pointing at the droplet's IP.
3. SSH in, clone the repo, then:
   ```bash
   cp .env.example .env
   ```
   Fill in `.env`: `DOMAIN`, a generated `SECRET_KEY`
   (`python -c "import secrets; print(secrets.token_hex(32))"`), a strong
   `POSTGRES_PASSWORD`, and strong `SEED_ADMIN_PASSWORD` / `SEED_OPERATOR_PASSWORD`.
4. ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   ```
   (the entrypoint runs migrations automatically).
5. ```bash
   docker compose -f docker-compose.prod.yml exec app flask seed
   ```
6. Verify: open `https://erp.yourdomain.com`, log in, complete the seeded released work
   order, and watch the dashboard change.
7. Continuous deployment: the CI workflow's `deploy` job redeploys the droplet on every
   push to `main` — but only after the test suite passes. One-time setup: generate a
   dedicated CI keypair (`ssh-keygen -t ed25519`), append the public key to the droplet's
   `~/.ssh/authorized_keys`, and add two GitHub Actions repository secrets: `DEPLOY_HOST`
   (droplet IP) and `DEPLOY_SSH_KEY` (the private key). Manual fallback, on the droplet:
   ```bash
   git pull && docker compose -f docker-compose.prod.yml up -d --build
   ```
   (migrations run automatically on start).

### Security notes

- Secrets live only in `.env` on the droplet (gitignored); `.env.example` documents every
  variable with placeholder values.
- The database is never exposed publicly — only Caddy publishes ports.
- Containers run as a non-root user; session cookies are `Secure` + `HttpOnly` in
  production.
- Known accepted gaps for a demo: no CSRF tokens, no rate limiting, no backups (see
  scope-cuts table above).
- Credentials handed to a hiring contact: the **operator** account — it's the natural one
  to share for a walkthrough of day-to-day use. Keep `admin` for your own demos.

## Planning documents used

The `requirements/` directory (12 documents) was produced in a **Claude Code** planning
session before any implementation code was written — architecture, schema, and
per-module specs, each with acceptance criteria. The implementation was then built from
those documents by Claude Code agents working in phases (skeleton → schema → auth → stock
service → feature modules → tests/CI → production deploy): sub-agents implemented each
phase under the shared conventions and guardrails in [`AGENTS.md`](AGENTS.md), a lead
agent verified every deliverable against the requirement docs and the test suite before
committing, and the commits are deliberately granular — each sized to be human-reviewable
in a few minutes. `job_description.md` is the job posting this demo was built for.

This is deliberate, not incidental: the posting asks for someone who can direct an AI
coding agent effectively — scope the work, review its output critically, ship correct code
fast. The repo is meant to show that in practice: planning documents with explicit
acceptance criteria → a phased, multi-agent build against those documents → human-reviewed,
granular commits → a test suite that gates whether a phase is actually done, not just
whether it compiles.
