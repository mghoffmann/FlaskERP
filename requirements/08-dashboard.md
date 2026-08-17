# 08 — Dashboard & Low-Stock Report

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md).

## Concept

The landing page after login — the "walk into the office, what needs attention?" view. Three concerns:

- **Low stock**: parts at or below their reorder point → someone should raise a purchase order.
- **Open documents**: work still in flight — released WOs waiting to be built, ordered POs in transit, confirmed SOs waiting to ship.
- **Recent activity**: the last stock movements, proving the ledger is alive.

## Endpoint

### GET /api/dashboard
- Auth: any. One aggregate call so the page renders with a single request.
- 200:
```json
{
  "counts": {
    "work_orders": {"draft": 1, "released": 2},
    "purchase_orders": {"draft": 0, "ordered": 1},
    "sales_orders": {"draft": 0, "confirmed": 1},
    "low_stock_parts": 3
  },
  "low_stock": [
    {"id", "sku", "name", "unit", "qty_on_hand", "reorder_point", "shortfall"}
  ],
  "open_work_orders": [ {"id", "wo_number", "product_sku", "product_name", "qty", "status", "created_at"} ],
  "recent_movements": [ {"id", "sku", "part_name", "qty_delta", "reason", "ref_number", "username", "created_at"} ]
}
```
- `low_stock`: active parts with `qty_on_hand <= reorder_point`, ordered by `shortfall` (= reorder_point − qty_on_hand) descending, max 20.
- `open_work_orders`: draft + released, newest first, max 10.
- `recent_movements`: last 10, newest first.

## Page

### /index.html
- Four KPI tiles across the top, each a link: **Low stock** (count, red accent when > 0) → parts list with low-stock filter on; **Open work orders** → WO list; **POs in transit** (ordered count) → PO list filtered to ordered; **SOs to ship** (confirmed count) → SO list filtered to confirmed.
- **Low stock** table: SKU (link), Name, On hand, Reorder point, Shortfall — empty state "Nothing below reorder point 🎉".
- **Open work orders** table: WO # (link), Product, Qty, Status badge, Age.
- **Recent activity** table: When, Part, ±Qty (color-coded), Reason, Reference (link), User.
- List-page filters are pre-set via query string (e.g. `/parts.html?low_stock=true`); list pages must read their filters from the query string on load ([09-frontend.md](09-frontend.md)).

## Acceptance criteria

- On seed data the page shows non-zero low stock and at least one open document of each type ([01-database.md](01-database.md) guarantees this).
- Completing the seeded released WO, then reloading, moves the counts and adds `wo_consume`/`wo_produce` rows to recent activity.
- Every tile and every table row navigates somewhere useful; no dead links.
- The page renders from a single API call, in one paint (no per-tile spinners).
