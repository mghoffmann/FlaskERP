# 01 — Database Schema, Migrations & Seed Data

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md).

## Principles

- Normalized schema, real foreign keys, `NOT NULL` wherever the domain requires a value.
- Statuses and enums are PostgreSQL `VARCHAR` columns with `CHECK` constraints (simpler to migrate than native enums, still database-enforced).
- `parts.qty_on_hand` is a denormalized running balance; the source of truth for *how it got there* is `stock_movements`. The two must always change together in one transaction (see stock service in [03-inventory.md](03-inventory.md)).
- All tables get `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` and `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` unless noted.

## Tables

### users
| Column | Type | Constraints |
|---|---|---|
| username | varchar(50) | unique, not null |
| password_hash | varchar(255) | not null (Werkzeug `generate_password_hash`) |
| role | varchar(20) | not null, check in (`admin`, `operator`) |

### parts
| Column | Type | Constraints |
|---|---|---|
| sku | varchar(40) | unique, not null |
| name | varchar(120) | not null |
| part_type | varchar(20) | not null, check in (`raw`, `finished`) |
| unit | varchar(20) | not null, default `ea` (e.g. `ea`, `kg`, `m`) |
| qty_on_hand | numeric(12,2) | not null, default 0, check >= 0 |
| reorder_point | numeric(12,2) | not null, default 0 |
| unit_cost | numeric(10,2) | not null, default 0 |
| active | boolean | not null, default true |

### bom_lines
One row per component of a finished product's recipe.

| Column | Type | Constraints |
|---|---|---|
| product_part_id | bigint | FK → parts, not null |
| component_part_id | bigint | FK → parts, not null |
| qty_per | numeric(12,4) | not null, check > 0 |

Constraints: unique (product_part_id, component_part_id); check product_part_id != component_part_id. Application enforces that the product is `finished` (components may be raw *or* finished, allowing sub-assemblies).

### work_orders
| Column | Type | Constraints |
|---|---|---|
| wo_number | varchar(20) | unique, not null — `WO-` + zero-padded id, set right after insert flush |
| product_part_id | bigint | FK → parts, not null |
| qty | numeric(12,2) | not null, check > 0 |
| status | varchar(20) | not null, default `draft`, check in (`draft`, `released`, `completed`, `canceled`) |
| notes | text | nullable |
| created_by | bigint | FK → users, not null |
| released_at / completed_at | timestamptz | nullable |

### suppliers / customers
Two structurally identical tables:

| Column | Type | Constraints |
|---|---|---|
| name | varchar(120) | not null, unique |
| contact_name | varchar(120) | nullable |
| email | varchar(120) | nullable |
| phone | varchar(40) | nullable |
| active | boolean | not null, default true |

### purchase_orders
| Column | Type | Constraints |
|---|---|---|
| po_number | varchar(20) | unique, not null — `PO-` + zero-padded id |
| supplier_id | bigint | FK → suppliers, not null |
| status | varchar(20) | not null, default `draft`, check in (`draft`, `ordered`, `received`, `canceled`) |
| notes | text | nullable |
| created_by | bigint | FK → users, not null |
| ordered_at / received_at | timestamptz | nullable |

### po_lines
| Column | Type | Constraints |
|---|---|---|
| po_id | bigint | FK → purchase_orders (cascade delete), not null |
| part_id | bigint | FK → parts, not null |
| qty | numeric(12,2) | not null, check > 0 |
| unit_cost | numeric(10,2) | not null, default 0 |

Unique (po_id, part_id).

### sales_orders
| Column | Type | Constraints |
|---|---|---|
| so_number | varchar(20) | unique, not null — `SO-` + zero-padded id |
| customer_id | bigint | FK → customers, not null |
| status | varchar(20) | not null, default `draft`, check in (`draft`, `confirmed`, `shipped`, `canceled`) |
| notes | text | nullable |
| created_by | bigint | FK → users, not null |
| confirmed_at / shipped_at | timestamptz | nullable |

### so_lines
Same shape as po_lines: so_id FK (cascade), part_id FK, qty (check > 0), unit_price numeric(10,2). Unique (so_id, part_id). Application enforces part is `finished`.

### stock_movements
Append-only ledger. Never updated, never deleted.

| Column | Type | Constraints |
|---|---|---|
| part_id | bigint | FK → parts, not null |
| qty_delta | numeric(12,2) | not null, check != 0 (positive = stock in, negative = stock out) |
| reason | varchar(20) | not null, check in (`adjustment`, `wo_consume`, `wo_produce`, `po_receive`, `so_ship`) |
| ref_type | varchar(20) | nullable, check in (`work_order`, `purchase_order`, `sales_order`) |
| ref_id | bigint | nullable (id of the referenced document) |
| note | text | nullable |
| user_id | bigint | FK → users, not null |

Index on (part_id, created_at desc).

## Migrations

- Initialize with `flask db init`; the entire schema above is **one initial migration** (`flask db migrate -m "initial schema"`), reviewed by hand before committing.
- Migrations run automatically on container start (`flask db upgrade` in `entrypoint.sh`) — see [11-deployment.md](11-deployment.md).
- Any later schema change gets its own migration with a descriptive message. Never edit an applied migration.

## Seed data (`flask seed`)

Idempotent CLI command (safe to run twice; skips if users already exist). Creates:

- Users: `admin` (role admin) and `operator` (role operator), passwords from `SEED_ADMIN_PASSWORD` / `SEED_OPERATOR_PASSWORD` env vars.
- ~10 raw parts (e.g. `RAW-STEEL-BAR`, `RAW-BEARING-608`, `RAW-SCREW-M4`, `RAW-MOTOR-12V`, paint, wire, packaging…) with varied `qty_on_hand`, `reorder_point` (at least 2 seeded *below* reorder point so the dashboard shows something), and `unit_cost`.
- 3 finished products (e.g. `FIN-CONVEYOR-S`, `FIN-GEARBOX-A`, `FIN-CART-HD`) each with a 3–5 line BOM.
- 2 suppliers, 2 customers.
- Documents in mixed statuses: 1 draft + 1 released work order; 1 ordered purchase order; 1 confirmed sales order; plus 1 completed WO and 1 received PO so the movements ledger has history.
- Stock movements consistent with the above (seed documents that are completed/received must have written their movements through the stock service, not raw inserts).

## Acceptance criteria

- `flask db upgrade` on an empty database creates all tables; `flask db downgrade base` removes them.
- `psql` inspection shows the CHECK constraints present (attempting `UPDATE parts SET qty_on_hand = -1` fails at the database).
- `flask seed` twice in a row produces no duplicates and exits cleanly.
- After seeding, the dashboard low-stock list is non-empty and the movements ledger reconciles: for every part, `qty_on_hand` equals the sum of its movements plus its seeded opening balance (opening balances are themselves seeded as `adjustment` movements from zero).
