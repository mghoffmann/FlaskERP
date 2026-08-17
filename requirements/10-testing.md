# 10 — Testing & CI

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md).

## Philosophy

Test the code that can be *wrong in a costly way*: the stock service, the document state machines, and role enforcement. Skip testing trivial CRUD serialization beyond one representative case. Target is a fast, meaningful suite (~25–35 tests, < 30s), not coverage theater.

## Setup

- pytest + fixtures in `tests/conftest.py`:
  - `app` — app factory with a test config pointing at a **PostgreSQL** test database (`DATABASE_URL` from `TEST_DATABASE_URL` env var, default `postgresql+psycopg://erp:erp@localhost:5432/erp_test`). Tests run against real Postgres — the schema uses CHECK constraints and `FOR UPDATE`, and SQLite would test a different database than production.
  - Schema created once per session (`db.create_all()` is acceptable for tests; migrations are exercised by the entrypoint and CI step below), each test wrapped in a rolled-back transaction (or table truncation) for isolation.
  - `client` — Flask test client; `admin_client` / `operator_client` — pre-logged-in clients.
  - Factory helpers (plain functions, no factory library): `make_part()`, `make_bom()`, `make_wo()`, `make_po()`, `make_so()`, `make_user()`.
- Local workflow: `docker compose up -d db`, create `erp_test` database, `pytest`.

## Required test areas

**Auth ([02](02-auth.md))**
- Login happy path; wrong password → 401 with the same body as unknown user.
- Parametrized sweep: every mutating endpoint × (no session → 401, operator on admin-only → 403). Maintain the endpoint list in one place so a new endpoint must be classified.

**Stock service ([03](03-inventory.md))**
- Adjustment updates qty and writes a ledger row with user attribution.
- Negative-result movement raises; qty and ledger unchanged.
- Zero delta rejected.

**Parts/BOM ([03](03-inventory.md), [04](04-bom.md))**
- Duplicate SKU → 400. Deactivate blocked by open document → 409.
- BOM replace-all semantics; self-reference, duplicate line, and sub-assembly cycle each → 400.

**Work orders ([05](05-work-orders.md))** — the heart of the suite
- Happy path draft→release→complete: exact stock deltas and ledger rows (component consumption + production, correct refs).
- Completion with shortfall: 409 lists all short components; database totally unchanged (assert part quantities *and* movement count).
- Release with empty BOM → 400; complete from draft → 409; double-complete → 409 with single stock effect.

**Purchasing ([06](06-purchasing.md))**
- Place→receive increments stock, writes ledger, applies last-cost update.
- Receive from draft / double-receive → 409, stock moved once.

**Sales ([07](07-sales-orders.md))**
- Confirm→ship decrements stock; shortfall → 409, nothing moves.
- Raw part on an SO line → 400.

**Dashboard ([08](08-dashboard.md))**
- Seeded fixture state produces correct counts and low-stock membership (boundary: qty == reorder_point is low).

**Seed ([01](01-database.md))**
- `flask seed` twice → no duplicates; ledger reconciles with qty_on_hand for every part.

## CI (`.github/workflows/ci.yml`)

- Trigger: push and pull_request on main.
- Job on `ubuntu-latest` with a `postgres:16` service container (health-checked).
- Steps: checkout → setup Python 3.12 with pip cache → `pip install -r requirements.txt` → **run migrations against the service DB** (`flask db upgrade` — this makes CI verify the migration chain itself) → `pytest -q`.
- Badge in the repo README.

## Acceptance criteria

- `pytest` passes locally against the compose database and in CI.
- Deleting the `FOR UPDATE` lock or a status guard makes at least one test fail (spot-check two such mutations by hand — a poor man's mutation test).
- CI fails if a migration is missing (a model change without a migration breaks the `db upgrade` + `create_all` comparison via a `flask db check`/`alembic check` step — include it).
