# Shopfloor ERP — Requirements

A small manufacturing ERP built as a job-application demo: **Flask** JSON API + **plain HTML/JS/CSS** frontend, **PostgreSQL** with **Alembic** migrations, everything containerized with **Docker Compose**, deployed to a **Digital Ocean** droplet.

## Purpose

This project demonstrates, for a hiring team:

1. Fluent, reviewable Python (Flask, SQLAlchemy) — small enough that every line has been read and understood by the author.
2. Real relational-database work: a normalized schema, migrations, transactional stock logic.
3. A lightweight HTML/JS "operator UI" with no frontend framework and no build step.
4. Docker/docker-compose discipline and a real public deployment.
5. Effective use of Claude Code: these requirements were produced in a planning session, and the implementation is built from them.

It is a **demo**, not a production system: single-tenant, seed data, no email, no file uploads, no pagination except where noted.

## What the system does (30-second domain tour)

A small factory keeps a catalog of **parts** (raw materials and finished products) and tracks **quantity on hand** for each. Each finished product has a **Bill of Materials (BOM)** — the recipe of components needed to build one unit. A **work order** says "build N units of product X"; completing it consumes the components from stock and adds the finished goods. **Purchase orders** bring raw materials in from suppliers; **sales orders** ship finished goods out to customers. Every stock change is recorded in an append-only **stock movements** ledger. A **dashboard** shows items below their reorder point and open documents.

## Documents

Read in this order. `00` and `01` define conventions and schema that every other document depends on.

| Doc | Contents |
|---|---|
| [00-architecture.md](00-architecture.md) | Repo layout, Flask conventions, API/JSON conventions, error format, config |
| [01-database.md](01-database.md) | Full schema (all tables, columns, constraints), migrations, seed data |
| [02-auth.md](02-auth.md) | Session login, roles (admin/operator), login page |
| [03-inventory.md](03-inventory.md) | Parts CRUD, stock adjustments, movement history |
| [04-bom.md](04-bom.md) | Bill of Materials editor and cost rollup |
| [05-work-orders.md](05-work-orders.md) | Work order lifecycle; the consume/produce completion transaction |
| [06-purchasing.md](06-purchasing.md) | Suppliers, purchase orders, receiving |
| [07-sales-orders.md](07-sales-orders.md) | Customers, sales orders, shipping |
| [08-dashboard.md](08-dashboard.md) | Low-stock report, open-document KPIs, recent activity |
| [09-frontend.md](09-frontend.md) | Shared page shell, nav, fetch helper, table/form/modal conventions |
| [10-testing.md](10-testing.md) | pytest scope and fixtures, GitHub Actions CI |
| [11-deployment.md](11-deployment.md) | Dockerfile, compose files, Caddy/HTTPS, Digital Ocean deploy steps |

## Build order (for the implementing session)

1. **Skeleton** — repo layout, app factory, config, extensions ([00](00-architecture.md)); dev docker-compose with Postgres ([11](11-deployment.md) dev section).
2. **Schema** — all models in one initial Alembic migration; seed command ([01](01-database.md)).
3. **Auth** — login/logout/me, `require_login` decorator ([02](02-auth.md)).
4. **Stock service** — `apply_movement()` with row locking ([03](03-inventory.md)); unit-test it early ([10](10-testing.md)).
5. **Modules** — inventory → BOM → work orders → purchasing → sales orders → dashboard ([03](03-inventory.md)–[08](08-dashboard.md)), building each module's pages ([09](09-frontend.md)) alongside its API.
6. **Tests + CI** — complete the suite, add the workflow ([10](10-testing.md)).
7. **Production deploy** — prod compose + Caddy, Digital Ocean ([11](11-deployment.md)).

## Global rules (apply to every document)

- Every endpoint requires an authenticated session unless a doc explicitly says otherwise; per-endpoint role requirements are listed as `Auth: admin` or `Auth: any` (any = admin or operator).
- Every change to `parts.qty_on_hand` goes through the stock service and writes a `stock_movements` row — no exceptions, no direct writes.
- Documents (work orders, purchase orders, sales orders) move through explicit status transitions; invalid transitions return `409`.
- Deletes of master data (parts, suppliers, customers) are soft deletes (`active = false`); ledger history is never destroyed.
