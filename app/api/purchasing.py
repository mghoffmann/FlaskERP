"""``/api/suppliers/*`` and ``/api/purchase-orders/*`` — vendors and the buying cycle.

06-purchasing.md: a **supplier** is a company the factory buys raw
materials (or, per the doc, any part at all — "buying finished goods for
resale is legitimate") from. A **purchase order (PO)** lists parts and
quantities to buy from one supplier and walks a small state machine::

    draft ──place──▶ ordered ──receive──▶ received
      │                 │
      └─────cancel──────┴──▶ canceled

- **draft** — being written; lines freely editable.
- **ordered** — sent to the supplier (out of scope: actually emailing the
  PO document); frozen; goods are in transit.
- **received** — the delivery arrived. Receiving increments stock for
  *every* line, in one transaction, through ``app/services/stock.py``'s
  ``apply_movement()`` (AGENTS.md: the only code allowed to touch
  ``parts.qty_on_hand``) — see :func:`receive_purchase_order` for why this
  side is simpler than work order completion's consume side (no shortfall
  case is possible when every movement only adds stock) and why it still
  keeps the same row-locking discipline anyway. Single full receipt only —
  no partial receiving (06-purchasing.md: a documented scope cut).
- **canceled** — terminal, allowed from ``draft`` or ``ordered``.

**Why this module's blueprint has no ``url_prefix``.** Every other API
module owns one URL prefix (``/api/parts``, ``/api/work-orders``, ...);
this one owns *two* — ``/api/suppliers/*`` and ``/api/purchase-orders/*``
— because 06-purchasing.md's two resources (the vendor list and the buying
documents) are one business domain but don't share a path segment. A
``Blueprint`` with no ``url_prefix=`` just means every ``@bp.route(...)``
below spells its full path out, rather than a prefix the blueprint would
otherwise prepend.

Every 409 ``invalid_transition`` response in this module is worded the
same way via :func:`_guard_po_transition`, mirroring the ``_guard_transition``
idea in ``app/api/work_orders.py`` (a fresh, local helper here — not an
import — since a PO's status set and its ``po_number`` attribute are
different from a work order's, but the "one place decides what's a legal
transition" shape is worth repeating).
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from flask import Blueprint, g, jsonify, request
from sqlalchemy.orm import joinedload

from app.api import get_json_or_400, iso
from app.api.auth import require_login
from app.errors import ApiError
from app.extensions import db
from app.models import Part, POLine, PurchaseOrder, Supplier
from app.services.stock import apply_movement

#: Blueprint for ``/api/suppliers/*`` and ``/api/purchase-orders/*``. See
#: the module docstring for why it takes no ``url_prefix``. Registered in
#: ``app/__init__.py``'s ``create_app()``.
bp = Blueprint("purchasing", __name__)

#: The set of legal ``purchase_orders.status`` values, mirroring the
#: database CHECK constraint in ``app/models.py``. Used to validate the
#: optional ``?status=`` filter on the list endpoint the same way
#: ``app/api/work_orders.py``'s ``_STATUSES`` does.
_PO_STATUSES = frozenset({"draft", "ordered", "received", "canceled"})

#: A sentinel distinct from every legal JSON value (including ``None``).
#: ``app/api/work_orders.py``'s ``update_work_order`` uses the same trick
#: for the same reason: ``data.get("notes")`` alone can't tell "the client
#: didn't send this key" apart from "the client sent ``null``, meaning
#: clear it" — both would come back as plain ``None``. Used below by
#: :func:`update_supplier` for its three optional text fields.
_UNSET = object()


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _resolve_supplier(supplier_id):
    """Validate a ``supplier_id`` value for PO create/update.

    Shared by :func:`_validate_po_input` (and so, transitively, both
    :func:`create_purchase_order` and :func:`update_purchase_order`), the
    same way ``app/api/work_orders.py``'s ``_resolve_product`` is shared
    by that module's create/update. A PO can only be written against a
    supplier that exists and is currently active — buying from a
    deactivated vendor would resurrect a relationship the catalog says is
    closed.

    Returns:
        tuple[Supplier | None, str | None]: ``(supplier, None)`` on
        success, or ``(None, message)`` with a field-error message on
        failure.
    """
    if not isinstance(supplier_id, int):
        return None, "supplier_id is required and must be an integer."
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        return None, "Supplier not found."
    if not supplier.active:
        return None, "Supplier is not active."
    return supplier, None


def _validate_lines(items):
    """Validate a PO's ``lines`` list, collecting every problem by index.

    Mirrors ``app/api/bom.py``'s ``replace_bom`` validation loop exactly
    (04-bom.md and 06-purchasing.md both want "show every mistake in one
    round trip," not stop-at-the-first-error): each line must resolve to
    a real ``Part`` that is active, ``qty`` must be a positive number,
    ``unit_cost`` a non-negative number, and no ``part_id`` may repeat
    across the lines. Unlike a BOM line, a PO line places **no**
    restriction on ``part_type`` — 06-purchasing.md is explicit that
    "buying finished goods for resale is legitimate," so raw materials
    and finished products are equally valid purchase targets.

    Every offending line is appended to ``details`` as
    ``{"line": index, "message": ...}`` and validation continues past it,
    so a caller fixing a multi-row form sees every problem at once.

    Args:
        items: The parsed (already known to be a non-empty list) JSON
            value of the request's ``lines`` field.

    Returns:
        tuple[list[tuple[int, Decimal, Decimal]], list[dict]]:
        ``(parsed_lines, details)`` where ``parsed_lines`` is
        ``[(part_id, qty, unit_cost), ...]`` for every line that passed,
        and ``details`` is empty exactly when every line passed.
    """
    # Pre-fetch every referenced part in one query, the same batching
    # ``app/api/bom.py`` uses for its component lookups — keeps this
    # whole validation pass at a fixed, small number of queries no matter
    # how many lines the caller submits.
    candidate_ids = {
        item["part_id"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("part_id"), int)
    }
    parts_by_id = (
        {p.id: p for p in Part.query.filter(Part.id.in_(candidate_ids)).all()}
        if candidate_ids
        else {}
    )

    details = []
    seen_part_ids = set()
    parsed_lines = []  # [(part_id, qty Decimal, unit_cost Decimal), ...]

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            details.append({"line": index, "message": "Line must be an object."})
            continue

        part_id = item.get("part_id")
        if not isinstance(part_id, int):
            details.append({"line": index, "message": "part_id must be an integer."})
            continue

        part = parts_by_id.get(part_id)
        if part is None:
            details.append({"line": index, "message": "Part not found."})
            continue
        if not part.active:
            details.append({"line": index, "message": "Part is not active."})
            continue

        if part_id in seen_part_ids:
            details.append({"line": index, "message": "Duplicate part."})
            continue

        # Goes through str(qty) before Decimal(...) for the same reason
        # every other numeric parse in this codebase does (see
        # app/services/stock.py's module docstring): a JSON number
        # arrives as an int/float, and Decimal(a_float) would import that
        # float's own binary imprecision.
        try:
            qty = Decimal(str(item.get("qty")))
        except (InvalidOperation, ValueError, TypeError):
            details.append({"line": index, "message": "qty must be a number."})
            continue
        if qty <= 0:
            details.append({"line": index, "message": "qty must be greater than 0."})
            continue

        try:
            unit_cost = Decimal(str(item.get("unit_cost")))
        except (InvalidOperation, ValueError, TypeError):
            details.append({"line": index, "message": "unit_cost must be a number."})
            continue
        if unit_cost < 0:
            details.append({"line": index, "message": "unit_cost must not be negative."})
            continue

        seen_part_ids.add(part_id)
        parsed_lines.append((part_id, qty, unit_cost))

    return parsed_lines, details


def _validate_po_input(data):
    """Validate a full PO create/update body: ``{supplier_id, notes, lines}``.

    06-purchasing.md requires ``PUT`` to take "the same shape as create"
    with "the same validation" — a PO edit is a full replace, not a
    partial patch, so unlike ``app/api/inventory.py``'s ``update_part``
    (where every field is independently optional), both
    :func:`create_purchase_order` and :func:`update_purchase_order` call
    this *one* function and get identical rules for free, instead of two
    validation blocks that could quietly drift apart.

    Field-level problems (missing/wrong-typed ``supplier_id``, ``notes``,
    or ``lines`` itself not being a non-empty list) are raised as a 400
    ``validation_error`` with ``field_errors`` first; only once the
    request is well-formed enough to have *some* lines to look at does
    this move on to :func:`_validate_lines`'s per-line ``details`` check.

    Args:
        data: The parsed JSON request body (from :func:`get_json_or_400`).

    Returns:
        tuple[Supplier, str | None, list[tuple[int, Decimal, Decimal]]]:
        ``(supplier, notes, parsed_lines)`` — everything the caller needs
        to build or replace a PO's rows.

    Raises:
        ApiError: 400 ``validation_error`` — either top-level
            ``field_errors`` (bad ``supplier_id``/``notes``/``lines``
            shape) or per-line ``details`` (from :func:`_validate_lines`).
    """
    field_errors = {}

    supplier, error = _resolve_supplier(data.get("supplier_id"))
    if error:
        field_errors["supplier_id"] = error

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        field_errors["notes"] = "notes must be a string."

    lines_data = data.get("lines")
    if not isinstance(lines_data, list):
        field_errors["lines"] = "lines must be a list."
    elif not lines_data:
        field_errors["lines"] = "At least one line is required."

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    parsed_lines, details = _validate_lines(lines_data)
    if details:
        raise ApiError(400, "validation_error", "Invalid purchase order lines.", details=details)

    return supplier, (notes.strip() if isinstance(notes, str) else None), parsed_lines


def _guard_po_transition(po, allowed_statuses, action):
    """Raise 409 ``invalid_transition`` unless ``po.status`` is one of ``allowed_statuses``.

    A local reimplementation of ``app/api/work_orders.py``'s
    ``_guard_transition`` — same idea (centralize "is this move even
    legal right now" so every 409 in this module states the current
    status and attempted action in the same words), but its own function
    rather than an import, since it names ``po.po_number`` instead of a
    work order's ``wo_number`` and the two modules' status sets differ.

    Args:
        po: The :class:`~app.models.PurchaseOrder` being acted on.
        allowed_statuses: Tuple of status strings the action is legal
            from.
        action: Human-readable verb for the message (``"edit"``,
            ``"place"``, ``"receive"``, ``"cancel"``).

    Raises:
        ApiError: 409 ``invalid_transition`` if ``po.status`` is not in
            ``allowed_statuses``.
    """
    if po.status not in allowed_statuses:
        raise ApiError(
            409,
            "invalid_transition",
            f"Cannot {action} purchase order {po.po_number}: status is '{po.status}'.",
        )


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _supplier_dict(supplier):
    """Serialize a :class:`~app.models.Supplier` to the JSON shape every endpoint returns."""
    return {
        "id": supplier.id,
        "name": supplier.name,
        "contact_name": supplier.contact_name,
        "email": supplier.email,
        "phone": supplier.phone,
        "active": supplier.active,
    }


def _po_stats(po_ids):
    """Batch-compute ``{po_id: (line_count, total)}`` for a set of purchase order ids.

    Reading ``line_count``/``total`` off ``po.lines`` one PO at a time
    (``len(po.lines)``, ``sum(l.qty * l.unit_cost for l in po.lines)``)
    would be an N+1 query per page of purchase orders the moment
    ``po.lines`` isn't already eager-loaded — and eager-loading a
    *collection* relationship for a list endpoint has its own cost (a
    ``LEFT OUTER JOIN`` that fans one PO row out into one row per line).
    Instead this issues **one** aggregate query — ``GROUP BY po_id`` with
    ``COUNT``/``SUM`` computed in Postgres — covering every requested PO
    regardless of how many there are, the same "batch it into one query"
    move ``app/api/inventory.py``'s ``_resolve_ref_numbers`` makes for
    stock movement references.

    Args:
        po_ids: A list of :class:`~app.models.PurchaseOrder` ids.

    Returns:
        dict[int, tuple[int, Decimal]]: ``po_id -> (line_count, total)``.
        A PO id with no lines simply has no key here — every call site
        uses ``stats.get(po.id, (0, Decimal("0")))``, not ``stats[po.id]``.
    """
    if not po_ids:
        return {}
    rows = (
        db.session.query(
            POLine.po_id,
            sa.func.count(POLine.id),
            sa.func.sum(POLine.qty * POLine.unit_cost),
        )
        .filter(POLine.po_id.in_(po_ids))
        .group_by(POLine.po_id)
        .all()
    )
    return {po_id: (count, total) for po_id, count, total in rows}


def _po_list_dict(po, line_count, total):
    """Serialize a :class:`~app.models.PurchaseOrder` to the list/detail JSON shape.

    ``line_count``/``total`` are passed in rather than computed here —
    every call site already has them from :func:`_po_stats` (batched for
    a whole page) or a single-PO lookup, so this function stays a pure
    "shape the fields" step with no query of its own. ``po.supplier`` and
    ``po.creator`` are read directly the same way
    ``app/api/work_orders.py``'s ``_list_dict`` reads ``wo.product``/
    ``wo.creator``: free from the session's identity map when the caller
    already eager-loaded or otherwise touched the same row this request,
    one extra query otherwise.
    """
    return {
        "id": po.id,
        "po_number": po.po_number,
        "status": po.status,
        "supplier": {"id": po.supplier.id, "name": po.supplier.name},
        "total": float(total),
        "line_count": line_count,
        "notes": po.notes,
        "created_by_username": po.creator.username,
        "created_at": iso(po.created_at),
        "ordered_at": iso(po.ordered_at),
        "received_at": iso(po.received_at),
    }


def _line_dict(line):
    """Serialize one :class:`~app.models.POLine` for the PO detail shape's ``lines`` array."""
    return {
        "id": line.id,
        "part_id": line.part_id,
        "sku": line.part.sku,
        "name": line.part.name,
        "unit": line.part.unit,
        "qty": float(line.qty),
        "unit_cost": float(line.unit_cost),
        "line_total": float(line.qty * line.unit_cost),
    }


def _lines_dict(po_id):
    """Fetch and serialize every :class:`~app.models.POLine` on one PO, in a stable order.

    Queried fresh from the database (rather than read off an in-memory
    ``po.lines`` relationship) so this is always correct right after
    :func:`update_purchase_order`'s delete-then-insert replace — the same
    reason ``app/api/bom.py``'s ``_bom_payload`` re-queries ``BomLine``
    instead of trusting ``product.bom_lines`` after a bulk delete: a bulk
    ``.delete()`` doesn't automatically refresh an already-loaded
    relationship collection in the session. ``joinedload(POLine.part)``
    pulls each line's :class:`~app.models.Part` (for sku/name/unit) into
    the same query instead of one extra ``SELECT`` per line.
    """
    lines = (
        POLine.query.options(joinedload(POLine.part))
        .filter(POLine.po_id == po_id)
        .order_by(POLine.id)
        .all()
    )
    return [_line_dict(line) for line in lines]


def _detail_dict(po):
    """The single-PO JSON shape: the list shape plus the ``lines`` array."""
    line_count, total = _po_stats([po.id]).get(po.id, (0, Decimal("0")))
    payload = _po_list_dict(po, line_count, total)
    payload["lines"] = _lines_dict(po.id)
    return payload


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------


@bp.route("/api/suppliers", methods=["GET"])
@require_login()
def list_suppliers():
    """``GET /api/suppliers`` — the supplier list, sorted by name.

    Auth: any. Query: ``active`` (``true`` — the default — restricts to
    active suppliers; ``all`` includes deactivated ones; any other value
    is treated as the default, matching ``app/api/inventory.py``'s
    ``list_parts`` convention), ``search`` (case-insensitive substring
    match on ``name``).
    """
    query = Supplier.query

    if request.args.get("active", "true") != "all":
        query = query.filter(Supplier.active.is_(True))

    search = request.args.get("search")
    if search:
        query = query.filter(Supplier.name.ilike(f"%{search}%"))

    suppliers = query.order_by(Supplier.name).all()
    return jsonify({"items": [_supplier_dict(s) for s in suppliers]})


@bp.route("/api/suppliers", methods=["POST"])
@require_login(role="admin")
def create_supplier():
    """``POST /api/suppliers`` — add a new vendor.

    Auth: admin. Request: ``{"name", "contact_name", "email", "phone"}``
    — ``name`` required and unique (mirrors ``app/api/inventory.py``'s
    ``create_part`` SKU-uniqueness check); the other three are optional
    free text.
    """
    data = get_json_or_400()
    field_errors = {}

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        field_errors["name"] = "Name is required."
    elif Supplier.query.filter_by(name=name.strip()).first() is not None:
        field_errors["name"] = "Name already exists."

    optional_values = {}
    for field in ("contact_name", "email", "phone"):
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            field_errors[field] = f"{field} must be a string."
        else:
            optional_values[field] = value.strip() if isinstance(value, str) else None

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    supplier = Supplier(name=name.strip(), **optional_values)
    db.session.add(supplier)
    db.session.flush()  # populates supplier.id before serialization
    return jsonify(_supplier_dict(supplier)), 201


@bp.route("/api/suppliers/<int:id>", methods=["GET"])
@require_login()
def get_supplier(id):
    """``GET /api/suppliers/{id}`` — one supplier plus its purchase orders, newest first.

    Auth: any. The ``"purchase_orders"`` array is the list-shape
    documented for ``GET /api/purchase-orders`` — 06-purchasing.md wants
    the supplier detail page able to render its own PO history without a
    second round trip. ``joinedload`` pulls each PO's ``supplier`` (this
    same row, so it's a free identity-map hit) and ``creator`` into the
    one query; :func:`_po_stats` batches the line-count/total aggregate
    for every PO on the page in a second query, so this endpoint's cost
    stays at two queries no matter how many purchase orders the supplier
    has.
    """
    supplier = db.session.get(Supplier, id)
    if supplier is None:
        raise ApiError(404, "not_found", "Supplier not found.")

    pos = (
        PurchaseOrder.query.options(
            joinedload(PurchaseOrder.supplier), joinedload(PurchaseOrder.creator)
        )
        .filter(PurchaseOrder.supplier_id == id)
        .order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
        .all()
    )
    stats = _po_stats([po.id for po in pos])

    payload = _supplier_dict(supplier)
    payload["purchase_orders"] = [
        _po_list_dict(po, *stats.get(po.id, (0, Decimal("0")))) for po in pos
    ]
    return jsonify(payload)


@bp.route("/api/suppliers/<int:id>", methods=["PUT"])
@require_login(role="admin")
def update_supplier(id):
    """``PUT /api/suppliers/{id}`` — edit a supplier's four fields.

    Auth: admin. Every field is optional per-request, matching
    ``app/api/inventory.py``'s ``update_part``/``app/api/work_orders.py``'s
    ``update_work_order`` convention: a caller sends only what it's
    changing. ``name``, if sent, is re-validated for uniqueness excluding
    this row's own current name. ``contact_name``/``email``/``phone`` use
    the :data:`_UNSET` sentinel the same way ``update_work_order``'s
    ``notes`` does — ``"email": null`` explicitly clears it, omitting the
    key entirely leaves it unchanged.
    """
    supplier = db.session.get(Supplier, id)
    if supplier is None:
        raise ApiError(404, "not_found", "Supplier not found.")

    data = get_json_or_400()
    field_errors = {}

    new_name = _UNSET
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            field_errors["name"] = "Name is required."
        elif Supplier.query.filter(Supplier.name == name.strip(), Supplier.id != id).first() is not None:
            field_errors["name"] = "Name already exists."
        else:
            new_name = name.strip()

    new_values = {}
    for field in ("contact_name", "email", "phone"):
        if field in data:
            value = data[field]
            if value is not None and not isinstance(value, str):
                field_errors[field] = f"{field} must be a string."
            else:
                new_values[field] = value.strip() if isinstance(value, str) else None

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    if new_name is not _UNSET:
        supplier.name = new_name
    for field, value in new_values.items():
        setattr(supplier, field, value)

    db.session.flush()
    return jsonify(_supplier_dict(supplier))


@bp.route("/api/suppliers/<int:id>", methods=["DELETE"])
@require_login(role="admin")
def deactivate_supplier(id):
    """``DELETE /api/suppliers/{id}`` — soft delete (``active = false``).

    Auth: admin. 409 ``conflict`` if the supplier has a purchase order in
    ``draft`` or ``ordered`` status — an open commitment to buy from this
    vendor is still in flight, so deactivating it out from under that PO
    would leave a document pointing at a vendor the catalog says is
    closed. A ``received``/``canceled`` PO does not block deactivation:
    those are finished business, exactly the same "only *open*
    transactional documents block it" rule ``app/api/inventory.py``'s
    ``deactivate_part`` applies to PO/SO lines.
    """
    supplier = db.session.get(Supplier, id)
    if supplier is None:
        raise ApiError(404, "not_found", "Supplier not found.")

    open_po = PurchaseOrder.query.filter(
        PurchaseOrder.supplier_id == id,
        PurchaseOrder.status.in_(("draft", "ordered")),
    ).first()
    if open_po is not None:
        raise ApiError(
            409,
            "conflict",
            f"Supplier has an open purchase order ({open_po.po_number}).",
        )

    supplier.active = False
    db.session.flush()
    return jsonify(_supplier_dict(supplier))


@bp.route("/api/suppliers/<int:id>/activate", methods=["POST"])
@require_login(role="admin")
def activate_supplier(id):
    """``POST /api/suppliers/{id}/activate`` — reverse a soft delete.

    Auth: admin. No conflict checks in this direction, matching
    ``app/api/inventory.py``'s ``activate_part``: reactivating a supplier
    can't put any document into an invalid state.
    """
    supplier = db.session.get(Supplier, id)
    if supplier is None:
        raise ApiError(404, "not_found", "Supplier not found.")

    supplier.active = True
    db.session.flush()
    return jsonify(_supplier_dict(supplier))


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


@bp.route("/api/purchase-orders", methods=["GET"])
@require_login()
def list_purchase_orders():
    """``GET /api/purchase-orders`` — every purchase order, newest first.

    Auth: any. Optional ``?status=``/``?supplier_id=`` filters, both
    ignored (not rejected) when unrecognized/non-numeric — a read
    endpoint's filter typo degrades to "no filter," matching every other
    list endpoint in this codebase (``app/api/work_orders.py``'s
    ``list_work_orders``, ``app/api/inventory.py``'s ``list_parts``).
    ``joinedload`` avoids one query per row for ``supplier``/``creator``;
    :func:`_po_stats` avoids one query per row for ``line_count``/``total``
    — together this endpoint costs exactly two queries no matter how many
    purchase orders exist.
    """
    query = PurchaseOrder.query.options(
        joinedload(PurchaseOrder.supplier), joinedload(PurchaseOrder.creator)
    )

    status = request.args.get("status")
    if status in _PO_STATUSES:
        query = query.filter(PurchaseOrder.status == status)

    supplier_id = request.args.get("supplier_id")
    if supplier_id is not None:
        try:
            supplier_id_int = int(supplier_id)
        except ValueError:
            supplier_id_int = None
        if supplier_id_int is not None:
            query = query.filter(PurchaseOrder.supplier_id == supplier_id_int)

    pos = query.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc()).all()
    stats = _po_stats([po.id for po in pos])

    return jsonify(
        {"items": [_po_list_dict(po, *stats.get(po.id, (0, Decimal("0")))) for po in pos]}
    )


@bp.route("/api/purchase-orders", methods=["POST"])
@require_login(role="admin")
def create_purchase_order():
    """``POST /api/purchase-orders`` — start a new PO in ``draft``.

    Auth: admin. Request: ``{"supplier_id", "notes", "lines": [{"part_id",
    "qty", "unit_cost"}, ...]}`` — validated as a whole by
    :func:`_validate_po_input`. 201 with the detail shape.

    ``po_number`` (``PO-0007``-style) follows the exact same
    insert-then-flush-then-number pattern as ``app/api/work_orders.py``'s
    ``create_work_order`` and ``app/cli.py``'s seed data: it embeds the
    row's own id, so it can't be a plain column default. ``flush()``
    sends the pending ``INSERT`` to Postgres (without committing — this
    request's transaction stays open until ``app/__init__.py``'s
    ``teardown_request`` runs) so ``po.id`` is available to build
    ``po_number`` from.
    """
    data = get_json_or_400()
    supplier, notes, parsed_lines = _validate_po_input(data)

    po = PurchaseOrder(
        po_number="pending",
        supplier_id=supplier.id,
        notes=notes,
        created_by=g.user.id,
    )
    db.session.add(po)
    db.session.flush()  # assigns po.id, needed for po_number and the lines below
    po.po_number = f"PO-{po.id:04d}"

    for part_id, qty, unit_cost in parsed_lines:
        db.session.add(POLine(po_id=po.id, part_id=part_id, qty=qty, unit_cost=unit_cost))
    db.session.flush()

    return jsonify(_detail_dict(po)), 201


@bp.route("/api/purchase-orders/<int:id>", methods=["GET"])
@require_login()
def get_purchase_order(id):
    """``GET /api/purchase-orders/{id}`` — one PO with its full line detail.

    Auth: any.
    """
    po = (
        PurchaseOrder.query.options(
            joinedload(PurchaseOrder.supplier), joinedload(PurchaseOrder.creator)
        )
        .filter(PurchaseOrder.id == id)
        .first()
    )
    if po is None:
        raise ApiError(404, "not_found", "Purchase order not found.")
    return jsonify(_detail_dict(po))


@bp.route("/api/purchase-orders/<int:id>", methods=["PUT"])
@require_login(role="admin")
def update_purchase_order(id):
    """``PUT /api/purchase-orders/{id}`` — replace a draft PO's fields and lines.

    Auth: admin, and only while ``status == "draft"`` — once placed, a PO
    represents a commitment the supplier is acting on, so any other
    status is a 409 ``invalid_transition`` via :func:`_guard_po_transition`.
    Request/validation is identical to :func:`create_purchase_order` (both
    go through :func:`_validate_po_input`); ``supplier_id`` and ``notes``
    are replaced outright and ``lines`` is a **full** replace, not a diff.

    The delete-then-insert below is the exact pattern
    ``app/api/bom.py``'s ``replace_bom`` uses for the same "replace-all"
    semantics: it runs inside this request's single transaction, so if
    anything raised after the delete, the whole request rolls back and
    the old lines are never actually lost — which is what makes literal
    delete-then-insert safe here instead of a more careful line-by-line
    diff.
    """
    po = db.session.get(PurchaseOrder, id)
    if po is None:
        raise ApiError(404, "not_found", "Purchase order not found.")
    _guard_po_transition(po, ("draft",), "edit")

    data = get_json_or_400()
    supplier, notes, parsed_lines = _validate_po_input(data)

    po.supplier_id = supplier.id
    po.notes = notes

    POLine.query.filter_by(po_id=po.id).delete()
    for part_id, qty, unit_cost in parsed_lines:
        db.session.add(POLine(po_id=po.id, part_id=part_id, qty=qty, unit_cost=unit_cost))
    db.session.flush()

    return jsonify(_detail_dict(po))


@bp.route("/api/purchase-orders/<int:id>/place", methods=["POST"])
@require_login(role="admin")
def place_purchase_order(id):
    """``POST /api/purchase-orders/{id}/place`` — ``draft`` -> ``ordered``.

    Auth: admin. Sets ``ordered_at``. In real life this is "the PO
    document got emailed to the supplier" (06-purchasing.md: out of
    scope for this demo — no PDF, no email, just the status flip and
    timestamp that record *that* it happened).
    """
    po = db.session.get(PurchaseOrder, id)
    if po is None:
        raise ApiError(404, "not_found", "Purchase order not found.")
    _guard_po_transition(po, ("draft",), "place")

    po.status = "ordered"
    po.ordered_at = datetime.now(timezone.utc)
    db.session.flush()
    return jsonify(_detail_dict(po))


@bp.route("/api/purchase-orders/<int:id>/receive", methods=["POST"])
@require_login()
def receive_purchase_order(id):
    """``POST /api/purchase-orders/{id}/receive`` — ``ordered`` -> ``received``. The delivery arrives.

    Auth: **any** — unlike create/place/cancel, this is 06-purchasing.md's
    "the operator signs for the delivery" action: day-to-day floor work,
    not a planning decision (the same reasoning that makes
    ``app/api/work_orders.py``'s ``complete_work_order`` open to any
    logged-in user while release/cancel stay admin-only).

    **Why this transaction has no shortfall case, unlike work order
    completion.** ``complete_work_order`` must lock every affected part
    row *before* checking availability, because it can fail partway
    through a real business rule (not enough stock) and needs to detect
    that atomically. Receiving only ever calls ``apply_movement`` with a
    **positive** ``qty_delta`` — stock arriving can never drive
    ``qty_on_hand`` negative — so ``InsufficientStockError`` is
    structurally impossible here; there is nothing to check for before
    writing.

    **Why this still locks in a consistent, ascending order anyway.**
    ``apply_movement`` itself still issues a ``SELECT ... FOR UPDATE`` on
    each part row it touches (``app/services/stock.py``'s module
    docstring covers why: even a same-direction write needs the lock to
    keep the read-then-write of ``qty_on_hand`` atomic under concurrent
    callers). Because *some* other transaction touching the same part row
    — a work order completing, or another PO receiving that happens to
    overlap in parts — might be doing a genuine multi-row, ordered lock
    acquisition (see ``complete_work_order``'s docstring for the full
    deadlock argument), this loop processes lines in **ascending
    ``part_id`` order** too, purely so every code path in the app that
    ever locks more than one part row in a single transaction agrees on
    the same global lock order. A receive that only ever touches one
    line's row at a time can't deadlock *itself*, but a consistent order
    is what keeps the *whole app* deadlock-safe, not any single
    endpoint's internal logic — so the discipline is followed here too,
    not because this specific loop needs it in isolation.

    **The "last cost" policy.** For each line, once its stock movement is
    applied, if ``line.unit_cost > 0`` the part's own ``unit_cost`` is set
    to that line's cost — 06-purchasing.md's deliberately simple costing
    model ("worth mentioning in the repo README as a simplification versus
    moving-average costing"): the *most recently received* price becomes
    the part's standing cost, with no weighting by quantity or history.
    A ``unit_cost`` of exactly 0 on a line is treated as "not provided"
    and leaves the part's existing cost untouched, rather than zeroing it
    out.

    All of this — every line's movement, every ``unit_cost`` update, and
    the PO's own status flip — happens in this one request's single
    transaction, committed together by ``app/__init__.py``'s
    ``teardown_request`` (or rolled back together on any exception), so a
    500 partway through can never leave some lines received and others
    not.
    """
    po = db.session.get(PurchaseOrder, id)
    if po is None:
        raise ApiError(404, "not_found", "Purchase order not found.")
    _guard_po_transition(po, ("ordered",), "receive")

    # Ascending part_id order — see the docstring above for why a
    # single-line-at-a-time receive still follows the same global lock
    # ordering discipline as work order completion's up-front sorted lock.
    lines = POLine.query.filter_by(po_id=po.id).order_by(POLine.part_id).all()

    for line in lines:
        apply_movement(
            part_id=line.part_id,
            qty_delta=line.qty,
            reason="po_receive",
            user_id=g.user.id,
            ref_type="purchase_order",
            ref_id=po.id,
            note=f"Received {po.po_number}.",
        )
        # apply_movement() already locked and re-fetched this exact part
        # row (with_for_update=True), so line.part resolves from the
        # session's identity map here — this assignment costs no extra
        # query, and it's safe to mutate because this transaction holds
        # the row's write lock until commit.
        if line.unit_cost and line.unit_cost > 0:
            line.part.unit_cost = line.unit_cost

    po.status = "received"
    po.received_at = datetime.now(timezone.utc)
    db.session.flush()

    return jsonify(_detail_dict(po))


@bp.route("/api/purchase-orders/<int:id>/cancel", methods=["POST"])
@require_login(role="admin")
def cancel_purchase_order(id):
    """``POST /api/purchase-orders/{id}/cancel`` — ``draft`` or ``ordered`` -> ``canceled``.

    Auth: admin. No stock effects either way: a draft never touched
    stock, and an ordered-but-not-yet-received PO hasn't added anything
    yet either — cancellation before receiving is purely a status change.
    A ``received`` PO cannot be canceled (mirrors
    ``app/api/work_orders.py``'s ``cancel_work_order``: no un-completing a
    transaction that already moved real stock; a correction after the
    fact is a manual adjustment through ``app/api/inventory.py``'s
    ``adjust_part``).
    """
    po = db.session.get(PurchaseOrder, id)
    if po is None:
        raise ApiError(404, "not_found", "Purchase order not found.")
    _guard_po_transition(po, ("draft", "ordered"), "cancel")

    po.status = "canceled"
    db.session.flush()
    return jsonify(_detail_dict(po))
