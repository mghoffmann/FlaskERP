# 03 — Parts & Inventory

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md); schema in [01-database.md](01-database.md).

## Concept

The **parts catalog** is the foundation of the ERP: every physical thing the factory buys, builds, or sells is a part. `raw` parts are purchased materials/components; `finished` parts are what work orders build and sales orders ship. Each part tracks `qty_on_hand` (current stock) and a `reorder_point` (when stock falls to or below this, the dashboard flags it for repurchase).

A **stock adjustment** is a manual correction — e.g. a physical count found 47 on the shelf but the system says 50. Adjustments, like all stock changes, are recorded in the append-only movements ledger.

## The stock service (`app/services/stock.py`)

The single choke point for inventory changes. **This is the most important code in the project** — everything in [05](05-work-orders.md), [06](06-purchasing.md), and [07](07-sales-orders.md) calls it.

```
apply_movement(part_id, qty_delta, reason, user_id, ref_type=None, ref_id=None, note=None) -> StockMovement
```

Requirements:
- Locks the part row (`SELECT ... FOR UPDATE`) before reading `qty_on_hand`, so concurrent completions can't both succeed on the last unit.
- Raises `InsufficientStockError(part, required, on_hand)` if `qty_on_hand + qty_delta < 0`. Callers translate this to the 409 `insufficient_stock` response.
- Updates `parts.qty_on_hand` and inserts the `stock_movements` row in the caller's transaction (the service never commits; the request teardown does).
- Rejects `qty_delta == 0`.

## Endpoints

### GET /api/parts
- Auth: any.
- Query params (all optional): `part_type` (`raw`|`finished`), `search` (case-insensitive substring on sku or name), `low_stock=true` (qty_on_hand <= reorder_point), `active` (`true` default — pass `all` to include deactivated).
- 200: `{"items": [{"id", "sku", "name", "part_type", "unit", "qty_on_hand", "reorder_point", "unit_cost", "active", "low_stock": bool}]}` sorted by sku.

### POST /api/parts
- Auth: admin.
- Request: `{"sku", "name", "part_type", "unit", "reorder_point", "unit_cost"}` (unit/reorder_point/unit_cost optional with schema defaults). `qty_on_hand` is **not accepted** — new parts start at 0; use an adjustment to load opening stock.
- 201 with the part. 400 `validation_error` on duplicate sku, blank name, invalid part_type, negative numbers.

### GET /api/parts/{id}
- Auth: any. 200 with the part object (same shape as list item). 404 if unknown.

### PUT /api/parts/{id}
- Auth: admin.
- Editable: `name`, `unit`, `reorder_point`, `unit_cost`, `sku` (uniqueness re-checked). **Not editable**: `part_type` (would corrupt BOM/document semantics), `qty_on_hand`, `active`.
- 200 with updated part.

### DELETE /api/parts/{id}
- Auth: admin. Soft delete: sets `active = false`.
- 409 `conflict` if the part appears on any document that is not `completed`/`received`/`shipped`/`canceled` (open WO product, open PO line, open SO line). BOM membership does *not* block deactivation, but see [04-bom.md](04-bom.md) for the release-time check.
- 200 with the part. A second endpoint `POST /api/parts/{id}/activate` (admin) reverses it.

### POST /api/parts/{id}/adjust
- Auth: any.
- Request: `{"qty_delta": -3, "note": "cycle count 2026-08-17"}`. `note` required (adjustments without a reason are an ERP smell), `qty_delta` non-zero.
- Calls the stock service with reason `adjustment`. 200 with the updated part. 409 `insufficient_stock` if it would go negative.

### GET /api/parts/{id}/movements
- Auth: any.
- Query: `limit` (default 50, max 200), `offset` (default 0).
- 200: `{"items": [{"id", "qty_delta", "reason", "ref_type", "ref_id", "ref_number", "note", "username", "created_at"}], "total": 123}` newest first. `ref_number` is the human number (WO-0007) of the referenced document, null for plain adjustments.

## Pages

### /parts.html — catalog list
- Toolbar: search box (filters as you type, debounced), part-type filter (All/Raw/Finished), "Low stock only" checkbox, and (admin) "New part" button.
- Table: SKU, Name, Type, On hand + unit, Reorder point, Unit cost, badge when low stock or inactive. Row click → detail page.
- "New part" opens the shared modal form ([09-frontend.md](09-frontend.md)) with the POST fields above.

### /part.html?id={id} — part detail
- Header: SKU, name, type badge, active/inactive badge; (admin) Edit and Deactivate buttons.
- Stock card: large qty_on_hand + unit, reorder point, low-stock warning if applicable; "Adjust stock" button (any role) opening a modal with qty_delta and required note.
- If finished: BOM section ([04-bom.md](04-bom.md)).
- Movements table: date, ±qty (color-coded), reason, reference (link to the document page when ref present), note, user. "Load more" uses offset paging.

## Acceptance criteria

- Creating a part, adjusting +100 then -30, shows qty 70 and two ledger rows with the current user attributed.
- Adjustment of -100 on qty 70 returns 409 and changes nothing (part qty and ledger both unchanged).
- Operator can adjust stock but gets 403 creating a part; the UI never shows them the "New part" button.
- Deactivated parts disappear from default lists and from all document/BOM part pickers, but their detail page and history remain reachable.
