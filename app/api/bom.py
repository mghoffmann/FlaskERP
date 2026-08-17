"""``/api/parts/{id}/bom`` — the Bill of Materials editor and material cost rollup.

04-bom.md: a BOM is the recipe for one unit of a finished product — a list
of ``(component_part, qty_per)`` rows. This module owns exactly one
sub-resource, ``GET``/``PUT /api/parts/{id}/bom``, and shares its
``url_prefix`` with ``app/api/inventory.py``'s blueprint rather than
nesting under it: 00-architecture.md's repository layout lists
``app/api/bom.py`` as its own module precisely so a Bill of Materials
(a distinct concept, with its own validation rules — cycle detection,
component-must-be-active, no self-reference) isn't buried inside the
already-large parts-catalog module. Two ``Blueprint`` objects can share a
``url_prefix`` as long as their route *suffixes* never collide — this
module only ever registers ``/<int:id>/bom``, which ``inventory.py``'s
blueprint never defines.

``PUT`` uses **replace-all semantics**: the request body is the complete
new BOM, not a diff. 04-bom.md's own reasoning: "simpler than per-line
CRUD and matches the editor UX" (the frontend's BOM editor is a table of
rows the admin edits freely and saves as a whole, not one row at a time).
"""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify
from sqlalchemy.orm import joinedload

from app.api import get_json_or_400
from app.api.auth import require_login
from app.errors import ApiError
from app.extensions import db
from app.models import BomLine, Part

#: Blueprint for ``/api/parts/{id}/bom``. Shares ``url_prefix`` with
#: ``app.api.inventory.bp`` (see module docstring) — both are appended to
#: ``app/__init__.py``'s ``blueprints`` list, imported as ``bp as
#: bom_bp``/``bp as inventory_bp`` the same way ``auth_bp`` already is, so
#: neither import silently shadows the other under the shared local name
#: ``bp``.
bp = Blueprint("bom", __name__, url_prefix="/api/parts")


def _bom_payload(product):
    """Build the ``{"items": [...], "material_cost": ...}`` shape shared by GET and PUT.

    Uses ``joinedload`` to fetch each line's ``component`` :class:`~app.models.Part`
    in the same query as the lines themselves — a BOM editor page needs
    every component's sku/name/unit/cost/on-hand/active to render its
    table, so without eager loading, accessing ``line.component`` per row
    would be an N+1 query (one per BOM line) instead of one join.
    """
    lines = (
        BomLine.query.options(joinedload(BomLine.component))
        .filter(BomLine.product_part_id == product.id)
        .join(Part, BomLine.component_part_id == Part.id)
        .order_by(Part.sku)
        .all()
    )

    items = []
    material_cost = Decimal("0")
    for line in lines:
        component = line.component
        line_cost = line.qty_per * component.unit_cost
        material_cost += line_cost
        items.append(
            {
                "component_part_id": component.id,
                "sku": component.sku,
                "name": component.name,
                "unit": component.unit,
                "qty_per": float(line.qty_per),
                "unit_cost": float(component.unit_cost),
                "line_cost": float(line_cost),
                "on_hand": float(component.qty_on_hand),
                "active": component.active,
            }
        )

    return {"items": items, "material_cost": float(material_cost)}


def _creates_cycle(product_id, new_component_ids):
    """Would adding ``product_id -> component`` edges for each of ``new_component_ids`` create a cycle?

    04-bom.md: "Reject if the edit would create a cycle through
    sub-assemblies (A contains B, B contains A — check by walking the
    component graph; depth is tiny at demo scale)." A cycle exists if,
    starting from any proposed new component and following existing
    "product contains component" edges forward (i.e. treating each
    component in turn as *its own* product and looking at *its* BOM),
    ``product_id`` is reachable again — that component already contains
    the product being edited, directly or via a chain of sub-assemblies,
    so adding it as a component here would close a loop.

    ``product_id``'s own *current* BOM lines are irrelevant to this
    check (they are about to be deleted and replaced by ``new_component_ids``
    regardless of the outcome here), which is why this walks the existing
    ``bom_lines`` table rather than needing any "exclude this product's
    old edges" special case — the walk simply never starts from
    ``product_id`` itself.

    Demo scale (01-database.md) means loading every BOM edge in the
    database once and walking it in Python is simpler and plenty fast,
    versus a recursive CTE that would need hand-rolled SQL for what's at
    most a handful of finished products with shallow sub-assembly chains.
    """
    if not new_component_ids:
        return False

    graph = {}
    for parent_id, child_id in db.session.query(
        BomLine.product_part_id, BomLine.component_part_id
    ).all():
        graph.setdefault(parent_id, []).append(child_id)

    visited = set()
    stack = list(new_component_ids)
    while stack:
        current = stack.pop()
        if current == product_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        stack.extend(graph.get(current, []))
    return False


@bp.route("/<int:id>/bom", methods=["GET"])
@require_login()
def get_bom(id):
    """``GET /api/parts/{id}/bom`` — the product's current BOM and cost rollup.

    Auth: any. 404 if ``id`` doesn't name a part at all; 400
    ``validation_error`` if it names a part that isn't ``finished`` (a
    raw material has no recipe).
    """
    product = db.session.get(Part, id)
    if product is None:
        raise ApiError(404, "not_found", "Part not found.")
    if product.part_type != "finished":
        raise ApiError(
            400,
            "validation_error",
            "Only finished parts have a Bill of Materials.",
            field_errors={"part_type": "Part is not finished."},
        )
    return jsonify(_bom_payload(product))


@bp.route("/<int:id>/bom", methods=["PUT"])
@require_login(role="admin")
def replace_bom(id):
    """``PUT /api/parts/{id}/bom`` — replace the product's entire BOM in one transaction.

    Auth: admin. Request: ``{"items": [{"component_part_id": 4, "qty_per": 2}, ...]}``;
    an empty ``items`` list clears the BOM. Every line is validated
    *before* anything is written — component exists and is active, no
    component equals the product itself, no duplicate components,
    ``qty_per > 0`` — and **all** offending lines are collected into one
    400 ``validation_error`` response's ``details`` (naming each bad line
    by index) rather than stopping at the first problem, so an admin
    fixing a multi-row form doesn't have to resubmit once per mistake.

    Cycle detection (see :func:`_creates_cycle`) runs only after every
    line passes the per-line checks, since a line that doesn't even
    resolve to a real, active, non-duplicate component has nothing valid
    to walk the graph from yet.

    04-bom.md deliberately allows editing a BOM while work orders exist —
    "WOs read the BOM at completion time" — so there is no open-document
    conflict check here the way ``DELETE /api/parts/{id}`` has one.

    The delete-then-insert below runs inside this request's single
    transaction (00-architecture.md: one transaction per request,
    committed by ``app/__init__.py``'s teardown handler) — if anything
    after the delete raised, the whole request rolls back and the old
    BOM lines are never actually lost, which is what makes this
    "replace-all" safe to implement as literal delete-then-insert instead
    of a more careful diff.
    """
    product = db.session.get(Part, id)
    if product is None:
        raise ApiError(404, "not_found", "Part not found.")
    if product.part_type != "finished":
        raise ApiError(
            400,
            "validation_error",
            "Only finished parts have a Bill of Materials.",
            field_errors={"part_type": "Part is not finished."},
        )

    data = get_json_or_400()
    items = data.get("items")
    if not isinstance(items, list):
        raise ApiError(
            400, "validation_error", "items must be a list.", field_errors={"items": "Must be a list."}
        )

    # Pre-fetch every referenced component part in one query — building
    # `components_by_id` up front, instead of a `Part.query.get(...)` per
    # line, keeps this whole validation pass at a fixed, small number of
    # queries regardless of how many BOM lines are submitted.
    candidate_ids = {
        item["component_part_id"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("component_part_id"), int)
    }
    components_by_id = (
        {p.id: p for p in Part.query.filter(Part.id.in_(candidate_ids)).all()}
        if candidate_ids
        else {}
    )

    details = []
    seen_component_ids = set()
    parsed_lines = []  # [(component_part_id, qty_per Decimal), ...]

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            details.append({"line": index, "message": "Line must be an object."})
            continue

        component_part_id = item.get("component_part_id")
        if not isinstance(component_part_id, int):
            details.append({"line": index, "message": "component_part_id must be an integer."})
            continue

        if component_part_id == product.id:
            details.append(
                {"line": index, "message": "A product cannot be a component of its own BOM."}
            )
            continue

        component = components_by_id.get(component_part_id)
        if component is None:
            details.append({"line": index, "message": "Component part not found."})
            continue
        if not component.active:
            details.append({"line": index, "message": "Component part is not active."})
            continue

        if component_part_id in seen_component_ids:
            details.append({"line": index, "message": "Duplicate component."})
            continue

        try:
            qty_per = Decimal(str(item.get("qty_per")))
        except (InvalidOperation, ValueError, TypeError):
            details.append({"line": index, "message": "qty_per must be a number."})
            continue
        if qty_per <= 0:
            details.append({"line": index, "message": "qty_per must be greater than 0."})
            continue

        seen_component_ids.add(component_part_id)
        parsed_lines.append((component_part_id, qty_per))

    if details:
        raise ApiError(400, "validation_error", "Invalid BOM.", details=details)

    if _creates_cycle(product.id, [component_id for component_id, _ in parsed_lines]):
        raise ApiError(
            400,
            "validation_error",
            "This BOM would create a cycle through sub-assemblies.",
            details=[{"message": "One of these components already contains this product as a sub-assembly."}],
        )

    BomLine.query.filter_by(product_part_id=product.id).delete()
    for component_part_id, qty_per in parsed_lines:
        db.session.add(
            BomLine(product_part_id=product.id, component_part_id=component_part_id, qty_per=qty_per)
        )
    db.session.flush()

    return jsonify(_bom_payload(product))
