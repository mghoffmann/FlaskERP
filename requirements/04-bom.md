# 04 — Bill of Materials (BOM)

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md); schema in [01-database.md](01-database.md).

## Concept

A **Bill of Materials** is the recipe for one unit of a finished product: *1 × FIN-GEARBOX-A = 2 × RAW-BEARING-608 + 1 × RAW-STEEL-BAR + 4 × RAW-SCREW-M4*. When a work order for N units completes, each BOM line's `qty_per` is multiplied by N and consumed from stock ([05-work-orders.md](05-work-orders.md)).

Components are usually raw parts, but a finished part may appear as a component of another (a **sub-assembly** — e.g. the gearbox is itself a component of the conveyor). Only single-level consumption is in scope: completing a work order consumes its direct components only; it does not recursively build missing sub-assemblies.

The **material cost rollup** is the BOM's theoretical cost: Σ (qty_per × component.unit_cost). It's a nice interview talking point and cheap to compute.

## Endpoints

### GET /api/parts/{id}/bom
- Auth: any. 404 if part unknown; 400 `validation_error` if the part is not `finished`.
- 200:
```json
{
  "items": [
    {"component_part_id": 4, "sku": "RAW-BEARING-608", "name": "608 bearing",
     "unit": "ea", "qty_per": 2, "unit_cost": 1.20, "line_cost": 2.40,
     "on_hand": 250, "active": true}
  ],
  "material_cost": 9.85
}
```
- `line_cost` = qty_per × unit_cost; `material_cost` = sum of line costs. `on_hand` and `active` let the editor show availability warnings inline.

### PUT /api/parts/{id}/bom
- Auth: admin. **Replace-all semantics**: the request body is the complete new BOM; lines not present are deleted. (Simpler than per-line CRUD and matches the editor UX.)
- Request: `{"items": [{"component_part_id": 4, "qty_per": 2}, ...]}` — empty `items` list clears the BOM.
- Validation (400 `validation_error` with `details` listing each offending line):
  - Product must be `finished`.
  - Every component must exist and be active.
  - No component equals the product; no duplicate components; every `qty_per` > 0.
  - Reject if the edit would create a cycle through sub-assemblies (A contains B, B contains A — check by walking the component graph; depth is tiny at demo scale).
- No restriction on editing a BOM while work orders exist: WOs read the BOM at completion time (this is a documented, realistic behavior — note it in the page UI as "changes affect unfinished work orders").
- 200 with the same payload shape as GET.

## Pages

No standalone page — the BOM lives in a section of **/part.html** ([03-inventory.md](03-inventory.md)) shown only for finished parts.

- Read view (any role): table Component SKU / Name / Qty per unit / Unit cost / Line cost, footer row with total material cost. Inline warning icon on any line whose component is inactive or whose on_hand is 0.
- Edit mode (admin, "Edit BOM" button): rows become editable — component picker (searchable dropdown of active parts, excluding the product itself), qty_per number input, remove-row button, "Add line" button, Save/Cancel. Save issues the PUT with the full line set; on 400, per-line messages render next to the offending rows.

## Acceptance criteria

- Setting a 3-line BOM, editing one qty, deleting one line, and saving results in exactly 2 rows in `bom_lines` for that product.
- Attempting to add the product to its own BOM, a duplicate component, or a qty_per of 0 returns 400 naming the line.
- Creating a two-part cycle via sub-assemblies is rejected with 400.
- Material cost shown equals Σ qty_per × unit_cost of current lines, and updates after a component's unit_cost is edited.
- Operator sees the BOM read view with no Edit button; direct PUT as operator returns 403.
