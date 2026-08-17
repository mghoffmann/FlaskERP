# 05 — Work Orders

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md); schema in [01-database.md](01-database.md); stock service in [03-inventory.md](03-inventory.md); BOM in [04-bom.md](04-bom.md).

## Concept

A **work order (WO)** is the instruction "build N units of finished product X." Its lifecycle:

```
draft ──release──▶ released ──complete──▶ completed
  │                   │
  └──────cancel───────┴──▶ canceled
```

- **draft** — being planned; editable.
- **released** — approved for the floor; no longer editable; waiting for an operator to build it.
- **completed** — the build happened. This is the transition with real logic: in **one database transaction**, consume every BOM component (qty_per × WO qty, reason `wo_consume`) and add the finished goods (+qty, reason `wo_produce`), all through the stock service. If any component is short, the whole transaction fails with a per-component shortfall report and *nothing* moves.
- **canceled** — abandoned; terminal. Completed is also terminal (no un-complete; a correction is done with stock adjustments — realistic and simple).

The completion transaction is the centerpiece of the whole demo — the thing to walk a hiring manager through.

## Endpoints

### GET /api/work-orders
- Auth: any. Query: `status` (optional).
- 200: `{"items": [{"id", "wo_number", "status", "qty", "product": {"id", "sku", "name", "unit"}, "notes", "created_by_username", "created_at", "released_at", "completed_at"}]}` newest first.

### POST /api/work-orders
- Auth: admin.
- Request: `{"product_part_id", "qty", "notes"}` (notes optional).
- Validation: product exists, is active, is `finished`; qty > 0. (An empty BOM is allowed at draft — it blocks *release*, not creation.)
- 201 with the WO detail shape (below), status `draft`, `wo_number` assigned.

### GET /api/work-orders/{id}
- Auth: any. 200 with the list shape **plus** a components availability block computed live from the current BOM:
```json
{
  "...": "...",
  "components": [
    {"part_id": 4, "sku": "RAW-BEARING-608", "name": "608 bearing", "unit": "ea",
     "qty_per": 2, "required": 20, "on_hand": 12, "short": 8}
  ],
  "can_complete": false
}
```
- `required` = qty_per × WO qty; `short` = max(0, required − on_hand); `can_complete` = status is `released` and every `short` is 0. For completed/canceled WOs the block is still returned (it's informational) but `can_complete` is false.

### PUT /api/work-orders/{id}
- Auth: admin. Only while `draft` (else 409 `invalid_transition`).
- Editable: `product_part_id`, `qty`, `notes` — same validation as create.

### POST /api/work-orders/{id}/release
- Auth: admin. `draft → released`; sets `released_at`.
- 409 `invalid_transition` if not draft. 400 `validation_error` if the product's BOM is empty or any BOM component is inactive ("Cannot release: BOM is empty" / "component X is inactive").
- Release does **not** require stock to be available — shortages are allowed until completion (realistic: material may be arriving).

### POST /api/work-orders/{id}/complete
- Auth: any (this is the operator's button). `released → completed`; sets `completed_at`.
- In one transaction: read the BOM; **first verify** every component's availability (lock rows in part_id order to avoid deadlocks), then apply `wo_consume` movements for each line and one `wo_produce` movement for the product, all with `ref_type="work_order"`, `ref_id`.
- 409 `insufficient_stock` listing *all* shortfalls, not just the first:
```json
{"error": {"code": "insufficient_stock", "message": "2 components are short.",
  "details": [{"part_id": 4, "sku": "RAW-BEARING-608", "required": 20, "on_hand": 12, "short": 8},
              {"part_id": 9, "sku": "RAW-SCREW-M4",   "required": 80, "on_hand": 40, "short": 40}]}}
```
- 409 `invalid_transition` if not released.

### POST /api/work-orders/{id}/cancel
- Auth: admin. `draft or released → canceled`. 409 otherwise.

## Pages

### /work-orders.html — list
- Toolbar: status filter tabs (All / Draft / Released / Completed / Canceled) with counts; (admin) "New work order" button → modal: product picker (active finished parts), qty, notes.
- Table: WO #, Product (sku — name), Qty, Status badge, Created, Completed. Row click → detail.

### /work-order.html?id={id} — detail
- Header: WO number, big status badge, product link, qty, notes, timestamps, created-by.
- Components table from the detail endpoint: Component, Qty per, Required, On hand, Short — short cells highlighted red. Banner "Ready to build" (green) when can_complete, "Short N components" (amber) otherwise.
- Action buttons by status and role: draft → Edit / Release / Cancel (admin); released → **Complete build** (any role, confirm dialog "This will consume components and add N × PRODUCT to stock"), Cancel (admin); completed/canceled → no actions.
- On a 409 `insufficient_stock` from Complete, render the shortfall details as rows highlighted in the components table plus an error banner (the on-screen numbers may be stale — this is the fresh truth from the server).

## Acceptance criteria

- Full happy path: create (draft) → release → complete moves stock exactly once: each component down by qty_per × qty, product up by qty, ledger rows all referencing the WO number.
- Completing with one component short: 409 lists every short component; **no** part quantity and **no** ledger row changed (verified in a test by asserting counts before/after).
- Completing the same WO twice: second call returns 409 `invalid_transition`.
- Releasing a WO whose product has an empty BOM fails with 400.
- Editing qty is possible in draft, returns 409 after release.
- Two concurrent completions competing for the same last components: one succeeds, one gets 409 (row locking works). A test may simulate this with two sessions if practical; otherwise the FOR UPDATE + ordered locking is verified by code review.
