# 07 — Sales (Customers & Sales Orders)

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md); schema in [01-database.md](01-database.md); stock service in [03-inventory.md](03-inventory.md).

## Concept

**Customers** buy the factory's finished products. A **sales order (SO)** lists what a customer ordered. Lifecycle mirrors purchasing, with the stock arrow reversed:

```
draft ──confirm──▶ confirmed ──ship──▶ shipped
  │                   │
  └──────cancel───────┴──▶ canceled
```

- **draft** — quote/being entered; editable.
- **confirmed** — the customer committed; frozen. Confirmation does **not** reserve stock (reservations/allocations are a real-ERP feature deliberately out of scope — note in README).
- **shipped** — goods left the building: one transaction decrements stock per line (reason `so_ship`) via the stock service. Like WO completion, shipping fails atomically with a shortfall report if any line lacks stock. Single full shipment only — no partials.
- **canceled** — terminal, from draft or confirmed.

Only `finished` parts may appear on SO lines (the factory doesn't sell raw stock — a scope choice that keeps the domain story clean).

## Endpoints — customers

Identical in shape to suppliers ([06-purchasing.md](06-purchasing.md)) with `customer`/`customers` substituted:

- `GET /api/customers` (any; `active`, `search`) — sorted by name.
- `POST /api/customers` (admin) — name required/unique.
- `GET /api/customers/{id}` (any) — includes `"sales_orders": [...]`.
- `PUT /api/customers/{id}` (admin).
- `DELETE /api/customers/{id}` (admin) — soft delete; 409 `conflict` with a draft/confirmed SO; `POST /api/customers/{id}/activate` reverses.

## Endpoints — sales orders

### GET /api/sales-orders
- Auth: any. Query: `status`, `customer_id`.
- 200: `{"items": [{"id", "so_number", "status", "customer": {"id", "name"}, "total", "line_count", "notes", "created_by_username", "created_at", "confirmed_at", "shipped_at"}]}` newest first. `total` = Σ qty × unit_price.

### POST /api/sales-orders
- Auth: admin.
- Request: `{"customer_id", "notes", "lines": [{"part_id", "qty", "unit_price"}, ...]}` — at least one line; customer active; each part active **and `finished`** (400 naming the offending line otherwise); qty > 0; unit_price ≥ 0; no duplicate parts.
- 201 with detail shape, status `draft`.

### GET /api/sales-orders/{id}
- Auth: any. List shape plus `"lines": [{"id", "part_id", "sku", "name", "unit", "qty", "unit_price", "line_total", "on_hand", "short"}]` — `on_hand`/`short` computed live so the page can show availability before shipping, same idea as the WO components block.

### PUT /api/sales-orders/{id}
- Auth: admin. Draft only. Replace-all lines, same validation as create.

### POST /api/sales-orders/{id}/confirm
- Auth: admin. `draft → confirmed`; sets `confirmed_at`. 409 if not draft.

### POST /api/sales-orders/{id}/ship
- Auth: any (the operator loads the truck). `confirmed → shipped`; sets `shipped_at`.
- One transaction, same pattern as WO completion: verify all lines (lock part rows in part_id order), then `apply_movement(part_id, -qty, "so_ship", ref_type="sales_order", ref_id=so.id)` per line.
- 409 `insufficient_stock` listing all short lines (same `details` shape as [05-work-orders.md](05-work-orders.md)); nothing moves on failure.
- 409 `invalid_transition` if not confirmed.

### POST /api/sales-orders/{id}/cancel
- Auth: admin. `draft or confirmed → canceled`. 409 otherwise.

## Pages

### /customers.html
- Same pattern as suppliers: searchable table, (admin) new/edit modals with SO link-list and Deactivate.

### /sales-orders.html — list
- Status tabs with counts, customer filter; (admin) "New sales order" → `/sales-order.html` create mode.
- Table: SO #, Customer, Lines, Total, Status badge, Created, Shipped.

### /sales-order.html?id={id} — detail / create / edit
- Same three-mode pattern as the PO page, with the part picker limited to **active finished parts** and a price column.
- View mode shows per-line availability: On hand and Short columns, short highlighted; banner "Ready to ship" / "Short N lines" when confirmed.
- Actions: draft → Edit / Confirm / Cancel (admin); confirmed → **Ship** (any role, confirm dialog), Cancel (admin); shipped/canceled → none.
- On 409 from Ship, merge the returned shortfall details into the lines table, as on the WO page.

## Acceptance criteria

- Create a 2-line SO, confirm, ship (with stock available): both parts decremented, two `so_ship` ledger rows referencing the SO.
- Ship with one line short: 409 lists the short line(s); no stock or ledger changes.
- Adding a `raw` part to an SO line: 400 naming the line.
- Ship on a draft → 409; edit after confirm → 409; cancel after ship → 409.
- The buy→build→sell loop works end to end on seed data: receive a PO for components, complete a WO building the product, ship an SO for it — dashboard and ledger reflect all three.
- Operator can ship but gets 403 on create/confirm/cancel.
