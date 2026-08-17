# 06 — Purchasing (Suppliers & Purchase Orders)

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md); schema in [01-database.md](01-database.md); stock service in [03-inventory.md](03-inventory.md).

## Concept

**Suppliers** are the companies the factory buys from. A **purchase order (PO)** is a document sent to one supplier listing parts and quantities to buy. Lifecycle:

```
draft ──place──▶ ordered ──receive──▶ received
  │                 │
  └─────cancel──────┴──▶ canceled
```

- **draft** — being written; lines editable.
- **ordered** — sent to the supplier; frozen; goods are in transit.
- **received** — the delivery arrived. Receiving increments stock for every line (reason `po_receive`) through the stock service, in one transaction. Single full receipt only — no partial receiving (documented scope cut; real ERPs receive line-by-line).
- **canceled** — terminal, allowed from draft or ordered.

Receiving also updates each part's `unit_cost` to the PO line's `unit_cost` when provided (> 0) — a simple "last cost" policy, worth mentioning in the repo README as a deliberate simplification versus moving-average costing.

## Endpoints — suppliers

### GET /api/suppliers
- Auth: any. Query: `active` (`true` default, `all`), `search` (name substring).
- 200: `{"items": [{"id", "name", "contact_name", "email", "phone", "active"}]}` sorted by name.

### POST /api/suppliers
- Auth: admin. Request: `{"name", "contact_name", "email", "phone"}` (name required/unique; rest optional). 201.

### GET /api/suppliers/{id}
- Auth: any. 200 supplier plus `"purchase_orders": [...]` (list shape below, newest first) for the detail page.

### PUT /api/suppliers/{id}
- Auth: admin. All four fields editable. 200.

### DELETE /api/suppliers/{id}
- Auth: admin. Soft delete (`active=false`); 409 `conflict` if the supplier has a draft or ordered PO. `POST /api/suppliers/{id}/activate` reverses.

## Endpoints — purchase orders

### GET /api/purchase-orders
- Auth: any. Query: `status`, `supplier_id` (both optional).
- 200: `{"items": [{"id", "po_number", "status", "supplier": {"id", "name"}, "total": 123.45, "line_count": 3, "notes", "created_by_username", "created_at", "ordered_at", "received_at"}]}` newest first. `total` = Σ qty × unit_cost.

### POST /api/purchase-orders
- Auth: admin.
- Request: `{"supplier_id", "notes", "lines": [{"part_id", "qty", "unit_cost"}, ...]}` — at least one line; supplier active; each part exists and is active; qty > 0; unit_cost ≥ 0; no duplicate parts. Any part type may be purchased (buying finished goods for resale is legitimate).
- 201 with detail shape, status `draft`.

### GET /api/purchase-orders/{id}
- Auth: any. 200: list shape plus `"lines": [{"id", "part_id", "sku", "name", "unit", "qty", "unit_cost", "line_total"}]`.

### PUT /api/purchase-orders/{id}
- Auth: admin. Draft only (409 `invalid_transition` otherwise). Request same shape as create (**replace-all lines**, same validation); `supplier_id` also editable in draft.

### POST /api/purchase-orders/{id}/place
- Auth: admin. `draft → ordered`; sets `ordered_at`. 409 if not draft. (In real life this is when the PO document is emailed to the supplier — out of scope, note in README.)

### POST /api/purchase-orders/{id}/receive
- Auth: any (the operator signs for the delivery). `ordered → received`; sets `received_at`.
- One transaction: for each line, `apply_movement(part_id, +qty, "po_receive", ref_type="purchase_order", ref_id=po.id)`; update part `unit_cost` per the last-cost policy above. Receiving only adds stock, so there is no shortfall case.
- 409 `invalid_transition` if not ordered.

### POST /api/purchase-orders/{id}/cancel
- Auth: admin. `draft or ordered → canceled`. 409 otherwise.

## Pages

### /suppliers.html
- Table (name, contact, email, phone, badge if inactive) with search; (admin) "New supplier" modal; row click opens an edit modal (admin) that also shows a link-list of the supplier's POs and a Deactivate button.
- (A separate supplier detail page is optional; a modal is acceptable at this scale.)

### /purchase-orders.html — list
- Status filter tabs with counts, supplier dropdown filter; (admin) "New purchase order" → goes to `/purchase-order.html` in create mode.
- Table: PO #, Supplier, Lines, Total, Status badge, Created, Received. Row click → detail.

### /purchase-order.html?id={id} — detail / create / edit
- One page, three modes:
  - **Create** (no id, admin): supplier picker, notes, editable lines grid (part picker of active parts, qty, unit cost, line total, add/remove row), running total. Save → POST → redirect to the new PO's detail.
  - **View** (any role): header with PO number, status badge, supplier link, timestamps; read-only lines table with totals footer.
  - **Edit** (admin, draft only): same grid as create, Save issues PUT.
- Action buttons by status/role: draft → Edit / Place order / Cancel (admin); ordered → **Receive delivery** (any role, confirm dialog "This will add N line items to stock"), Cancel (admin); received/canceled → none.

## Acceptance criteria

- Create a 2-line PO, place it, receive it: both parts' qty_on_hand increased by line qty, two ledger rows referencing the PO, part unit_cost updated where the line had a cost.
- Receive on a draft PO → 409; edit on an ordered PO → 409; receive twice → 409, stock moved only once.
- Deactivating a supplier with an ordered PO → 409 `conflict`; after the PO is received, deactivation succeeds and the supplier vanishes from the new-PO picker but old POs still render.
- Operator can receive but gets 403 on create/place/cancel.
