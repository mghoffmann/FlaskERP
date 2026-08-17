"""``/api/parts/*`` — the parts catalog: CRUD, stock adjustments, movement history.

03-inventory.md calls the parts catalog "the foundation of the ERP": every
physical thing the factory buys, builds, or sells is a :class:`~app.models.Part`.
This module is the HTTP-facing half of that story; the other half —
*how* ``qty_on_hand`` is allowed to change — lives in
``app/services/stock.py`` and is never duplicated here (AGENTS.md: that
module is the only code allowed to write ``parts.qty_on_hand``).

:func:`part_to_dict` is deliberately public (not prefixed ``_``) because
``app/api/bom.py`` needs the exact same "how does a Part look on the
wire" shape for the BOM editor's component picker — defining it once here
and importing it there keeps both modules' JSON in sync instead of two
serializers quietly drifting apart.
"""

from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from flask import Blueprint, g, jsonify, request

from app.api import get_json_or_400, iso
from app.api.auth import require_login
from app.errors import ApiError
from app.extensions import db
from app.models import Part, POLine, PurchaseOrder, SalesOrder, SOLine, StockMovement, User, WorkOrder
from app.services.stock import InsufficientStockError, apply_movement

#: Blueprint for every ``/api/parts*`` route except the BOM sub-resource
#: (``app/api/bom.py`` owns ``/api/parts/{id}/bom`` under its *own*
#: blueprint object, sharing this same ``url_prefix`` — see that module's
#: docstring, and the "shadowing" note in ``app/__init__.py``, for why two
#: blueprints can share a prefix as long as their route *suffixes* never
#: collide). Registered in ``app/__init__.py``'s ``create_app()``.
bp = Blueprint("inventory", __name__, url_prefix="/api/parts")

#: Referenced-document types a :class:`~app.models.StockMovement` can
#: point at, mapped to the model class and the attribute holding its
#: human-facing number (``WO-0007``-style). Used by
#: :func:`_resolve_ref_numbers` to batch-fetch those numbers.
_REF_TYPE_MODELS = {
    "work_order": (WorkOrder, "wo_number"),
    "purchase_order": (PurchaseOrder, "po_number"),
    "sales_order": (SalesOrder, "so_number"),
}


def part_to_dict(part):
    """Serialize a :class:`~app.models.Part` to the JSON shape every endpoint returns.

    ``qty_on_hand``/``reorder_point``/``unit_cost`` are SQLAlchemy
    ``Numeric`` columns, hydrated as :class:`decimal.Decimal` (see
    ``app/services/stock.py`` for why the app uses ``Decimal`` internally
    for money/quantity math). 00-architecture.md's JSON convention is
    plain numbers on the wire ("demo-acceptable; a production system
    would use string decimals"), so every numeric field is cast to
    ``float`` here, once, rather than each route remembering to do it.

    ``low_stock`` is computed here rather than stored: it is nothing more
    than ``qty_on_hand <= reorder_point``, and computing it at serialization
    time means it can never go stale relative to the two columns it derives
    from.
    """
    return {
        "id": part.id,
        "sku": part.sku,
        "name": part.name,
        "part_type": part.part_type,
        "unit": part.unit,
        "qty_on_hand": float(part.qty_on_hand),
        "reorder_point": float(part.reorder_point),
        "unit_cost": float(part.unit_cost),
        "active": part.active,
        "low_stock": part.qty_on_hand <= part.reorder_point,
    }


def _parse_money(raw):
    """Parse a JSON value into a non-negative :class:`decimal.Decimal`.

    Shared by the create/update handlers for ``reorder_point`` and
    ``unit_cost`` — both need "must be a number, must not be negative,"
    just against different field names. Goes through ``str(raw)`` first
    (rather than ``Decimal(raw)`` directly) for the same reason
    ``apply_movement`` does: avoids importing a Python ``float``'s own
    binary imprecision into the ``Decimal`` (``Decimal(0.1) != Decimal("0.1")``).

    Raises:
        ValueError: with a user-facing message, if ``raw`` isn't a valid
            non-negative number.
    """
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Must be a number.")
    if value < 0:
        raise ValueError("Must not be negative.")
    return value


def _resolve_ref_numbers(movements):
    """Batch-resolve ``{(ref_type, ref_id): human_number}`` for a page of movements.

    A :class:`~app.models.StockMovement` references its originating
    document through a loose ``(ref_type, ref_id)`` pair, not a real
    foreign key (see that model's docstring), so there is no SQLAlchemy
    relationship to eager-load. Looking up each movement's document one
    at a time would be an N+1 query per page of movements; instead this
    groups referenced ids by document type and issues **one** ``WHERE id
    IN (...)`` query per type actually present on the page (at most three:
    work order, purchase order, sales order), regardless of how many
    movements there are.
    """
    ids_by_type = {}
    for movement in movements:
        if movement.ref_type is not None and movement.ref_id is not None:
            ids_by_type.setdefault(movement.ref_type, set()).add(movement.ref_id)

    ref_numbers = {}
    for ref_type, ids in ids_by_type.items():
        model_info = _REF_TYPE_MODELS.get(ref_type)
        if model_info is None:
            continue
        model, number_attr = model_info
        for row in model.query.filter(model.id.in_(ids)).all():
            ref_numbers[(ref_type, row.id)] = getattr(row, number_attr)
    return ref_numbers


@bp.route("", methods=["GET"])
@require_login()
def list_parts():
    """``GET /api/parts`` — the filterable parts catalog.

    Auth: any. All query params are optional and combine with AND:
    ``part_type`` (``raw``/``finished``), ``search`` (case-insensitive
    substring match on sku *or* name), ``low_stock=true``
    (``qty_on_hand <= reorder_point``), ``active`` (``true`` — the
    default — restricts to active parts; ``all`` includes deactivated
    ones; any other value is treated as the default).

    An unrecognized ``part_type`` value is ignored rather than rejected:
    this is a read/filter endpoint, not a validation boundary, so a typo'd
    filter degrades to "no filter" instead of a hard error.
    """
    query = Part.query

    part_type = request.args.get("part_type")
    if part_type in ("raw", "finished"):
        query = query.filter(Part.part_type == part_type)

    search = request.args.get("search")
    if search:
        like = f"%{search}%"
        query = query.filter(sa.or_(Part.sku.ilike(like), Part.name.ilike(like)))

    if request.args.get("low_stock") == "true":
        query = query.filter(Part.qty_on_hand <= Part.reorder_point)

    if request.args.get("active", "true") != "all":
        query = query.filter(Part.active.is_(True))

    parts = query.order_by(Part.sku).all()
    return jsonify({"items": [part_to_dict(p) for p in parts]})


@bp.route("", methods=["POST"])
@require_login(role="admin")
def create_part():
    """``POST /api/parts`` — create a new part.

    Auth: admin. ``qty_on_hand`` is deliberately **not** an accepted
    field: 03-inventory.md is explicit that new parts start at 0 and
    opening stock is loaded via an adjustment (so it lands in the ledger
    like every other stock change, instead of a part's very first
    quantity being the one number in the system with no movement behind
    it). Rather than silently ignoring a caller-supplied ``qty_on_hand``,
    this rejects it with a field error — silently dropping a field a
    client explicitly sent is a worse API than telling them it doesn't
    belong here.
    """
    data = get_json_or_400()
    field_errors = {}

    if "qty_on_hand" in data:
        field_errors["qty_on_hand"] = (
            "qty_on_hand cannot be set on create; use an adjustment to load opening stock."
        )

    sku = data.get("sku")
    if not isinstance(sku, str) or not sku.strip():
        field_errors["sku"] = "SKU is required."
    elif Part.query.filter_by(sku=sku).first() is not None:
        field_errors["sku"] = "SKU already exists."

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        field_errors["name"] = "Name is required."

    part_type = data.get("part_type")
    if part_type not in ("raw", "finished"):
        field_errors["part_type"] = "Must be 'raw' or 'finished'."

    unit = data.get("unit", "ea")
    if not isinstance(unit, str) or not unit.strip():
        field_errors["unit"] = "Unit must be a non-blank string."

    reorder_point = Decimal("0")
    if "reorder_point" in data:
        try:
            reorder_point = _parse_money(data["reorder_point"])
        except ValueError as exc:
            field_errors["reorder_point"] = str(exc)

    unit_cost = Decimal("0")
    if "unit_cost" in data:
        try:
            unit_cost = _parse_money(data["unit_cost"])
        except ValueError as exc:
            field_errors["unit_cost"] = str(exc)

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    part = Part(
        sku=sku.strip(),
        name=name.strip(),
        part_type=part_type,
        unit=unit.strip(),
        reorder_point=reorder_point,
        unit_cost=unit_cost,
    )
    db.session.add(part)
    # Flush (not commit — see app/services/stock.py's module docstring on
    # why nothing in this app commits mid-request) so `part.id` is
    # populated by Postgres's identity sequence before the response is
    # serialized.
    db.session.flush()
    return jsonify(part_to_dict(part)), 201


@bp.route("/<int:id>", methods=["GET"])
@require_login()
def get_part(id):
    """``GET /api/parts/{id}`` — a single part, any status.

    Auth: any. Deliberately does **not** filter on ``active`` — 03-inventory.md's
    acceptance criteria require a deactivated part's detail page to
    remain reachable even though it disappears from the default list.
    """
    part = db.session.get(Part, id)
    if part is None:
        raise ApiError(404, "not_found", "Part not found.")
    return jsonify(part_to_dict(part))


@bp.route("/<int:id>", methods=["PUT"])
@require_login(role="admin")
def update_part(id):
    """``PUT /api/parts/{id}`` — edit a part's catalog fields.

    Auth: admin. Editable: ``name``, ``unit``, ``reorder_point``,
    ``unit_cost``, ``sku`` (re-checked for uniqueness). ``part_type``,
    ``qty_on_hand``, and ``active`` are rejected if present — the first
    two would corrupt BOM/document semantics or the stock ledger's
    invariant (03-inventory.md), and ``active`` has its own dedicated
    DELETE/activate endpoints so a part's lifecycle status always goes
    through the conflict checks those enforce.

    Every field is optional here (unlike ``POST``, which requires the
    full set): a caller sends only the fields it wants to change, and
    fields it omits keep their current value.
    """
    part = db.session.get(Part, id)
    if part is None:
        raise ApiError(404, "not_found", "Part not found.")

    data = get_json_or_400()
    field_errors = {}

    for locked_field in ("part_type", "qty_on_hand", "active"):
        if locked_field in data:
            field_errors[locked_field] = f"{locked_field} is not editable via this endpoint."

    if "sku" in data:
        sku = data["sku"]
        if not isinstance(sku, str) or not sku.strip():
            field_errors["sku"] = "SKU is required."
        elif Part.query.filter(Part.sku == sku, Part.id != id).first() is not None:
            field_errors["sku"] = "SKU already exists."

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            field_errors["name"] = "Name is required."

    if "unit" in data:
        unit = data["unit"]
        if not isinstance(unit, str) or not unit.strip():
            field_errors["unit"] = "Unit must be a non-blank string."

    reorder_point = None
    if "reorder_point" in data:
        try:
            reorder_point = _parse_money(data["reorder_point"])
        except ValueError as exc:
            field_errors["reorder_point"] = str(exc)

    unit_cost = None
    if "unit_cost" in data:
        try:
            unit_cost = _parse_money(data["unit_cost"])
        except ValueError as exc:
            field_errors["unit_cost"] = str(exc)

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    if "sku" in data:
        part.sku = data["sku"].strip()
    if "name" in data:
        part.name = data["name"].strip()
    if "unit" in data:
        part.unit = data["unit"].strip()
    if reorder_point is not None:
        part.reorder_point = reorder_point
    if unit_cost is not None:
        part.unit_cost = unit_cost

    db.session.flush()
    return jsonify(part_to_dict(part))


@bp.route("/<int:id>", methods=["DELETE"])
@require_login(role="admin")
def deactivate_part(id):
    """``DELETE /api/parts/{id}`` — soft delete (``active = false``).

    Auth: admin. 01-database.md's global rule — "deletes of master data
    are soft deletes; ledger history is never destroyed" — is why this
    flips a flag instead of issuing a SQL ``DELETE``.

    409 ``conflict`` if the part is still referenced by an *open*
    document: the product of a work order that isn't
    ``completed``/``canceled``, or a line on a purchase/sales order that
    isn't ``received``/``shipped``/``canceled``. BOM membership does
    **not** block deactivation (04-bom.md handles that at WO-release time
    instead) — a part can be pulled from the catalog while it's still
    listed as a sub-assembly component; only *open transactional*
    documents block it here.
    """
    part = db.session.get(Part, id)
    if part is None:
        raise ApiError(404, "not_found", "Part not found.")

    open_wo = WorkOrder.query.filter(
        WorkOrder.product_part_id == id,
        WorkOrder.status.notin_(("completed", "canceled")),
    ).first()
    if open_wo is not None:
        raise ApiError(409, "conflict", "Part is the product of an open work order.")

    open_po_line = (
        db.session.query(POLine)
        .join(PurchaseOrder, POLine.po_id == PurchaseOrder.id)
        .filter(POLine.part_id == id, PurchaseOrder.status.notin_(("received", "canceled")))
        .first()
    )
    if open_po_line is not None:
        raise ApiError(409, "conflict", "Part appears on an open purchase order.")

    open_so_line = (
        db.session.query(SOLine)
        .join(SalesOrder, SOLine.so_id == SalesOrder.id)
        .filter(SOLine.part_id == id, SalesOrder.status.notin_(("shipped", "canceled")))
        .first()
    )
    if open_so_line is not None:
        raise ApiError(409, "conflict", "Part appears on an open sales order.")

    part.active = False
    db.session.flush()
    return jsonify(part_to_dict(part))


@bp.route("/<int:id>/activate", methods=["POST"])
@require_login(role="admin")
def activate_part(id):
    """``POST /api/parts/{id}/activate`` — reverse a soft delete.

    Auth: admin. No conflict checks needed in this direction — reactivating
    a part can't put any document into an invalid state.
    """
    part = db.session.get(Part, id)
    if part is None:
        raise ApiError(404, "not_found", "Part not found.")

    part.active = True
    db.session.flush()
    return jsonify(part_to_dict(part))


@bp.route("/<int:id>/adjust", methods=["POST"])
@require_login()
def adjust_part(id):
    """``POST /api/parts/{id}/adjust`` — a manual stock correction.

    Auth: any (unlike catalog edits, day-to-day stock counts are an
    operator task). ``qty_delta`` must be non-zero; ``note`` is
    **required** and non-blank — 03-inventory.md calls an adjustment
    without a reason "an ERP smell," so the route enforces it even though
    ``apply_movement`` itself treats ``note`` as optional (that
    requirement belongs to this route, not the generic stock service,
    since other callers of ``apply_movement`` — WO completion, PO
    receiving — have their own, different, note conventions).

    Delegates the actual stock change to ``apply_movement`` with
    ``reason="adjustment"``; an :class:`InsufficientStockError` from
    there becomes the 409 ``insufficient_stock`` response
    00-architecture.md's error table specifies, with a single-row
    ``details`` shortfall (the multi-row shape is for the multi-component
    work-order-completion case in 05-work-orders.md — a manual adjustment
    only ever touches one part).
    """
    part = db.session.get(Part, id)
    if part is None:
        raise ApiError(404, "not_found", "Part not found.")

    data = get_json_or_400()
    field_errors = {}

    qty_delta = data.get("qty_delta")
    qty_delta_dec = None
    try:
        qty_delta_dec = Decimal(str(qty_delta))
    except (InvalidOperation, ValueError, TypeError):
        field_errors["qty_delta"] = "Must be a number."
    else:
        if qty_delta_dec == 0:
            field_errors["qty_delta"] = "Must not be zero."

    note = data.get("note")
    if not isinstance(note, str) or not note.strip():
        field_errors["note"] = "Note is required."

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    try:
        apply_movement(
            part_id=part.id,
            qty_delta=qty_delta_dec,
            reason="adjustment",
            user_id=g.user.id,
            note=note.strip(),
        )
    except InsufficientStockError as exc:
        raise ApiError(
            409,
            "insufficient_stock",
            str(exc),
            details=[
                {
                    "part_id": part.id,
                    "sku": part.sku,
                    "required": float(exc.required),
                    "on_hand": float(exc.on_hand),
                    "short": float(exc.required - exc.on_hand),
                }
            ],
        )

    # `apply_movement` mutated this same, session-identity-mapped `part`
    # instance's `qty_on_hand` in place, so re-serializing it here already
    # reflects the new balance without a second query.
    return jsonify(part_to_dict(part))


@bp.route("/<int:id>/movements", methods=["GET"])
@require_login()
def list_movements(id):
    """``GET /api/parts/{id}/movements`` — this part's ledger, newest first.

    Auth: any. ``limit`` (default 50, max 200) / ``offset`` (default 0)
    page the results; ``total`` is the full unpaged count so the frontend's
    "Load more" button (09-frontend.md) knows when to stop showing itself.
    Ordered by ``created_at DESC`` — matching the index
    ``app/models.py`` defines specifically for this query — with ``id
    DESC`` as a tiebreaker for movements created in the same instant.
    """
    part = db.session.get(Part, id)
    if part is None:
        raise ApiError(404, "not_found", "Part not found.")

    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))

    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    offset = max(0, offset)

    base_query = StockMovement.query.filter_by(part_id=id)
    total = base_query.count()
    movements = (
        base_query.order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    ref_numbers = _resolve_ref_numbers(movements)

    user_ids = {m.user_id for m in movements}
    usernames = (
        {u.id: u.username for u in User.query.filter(User.id.in_(user_ids)).all()}
        if user_ids
        else {}
    )

    items = [
        {
            "id": m.id,
            "qty_delta": float(m.qty_delta),
            "reason": m.reason,
            "ref_type": m.ref_type,
            "ref_id": m.ref_id,
            "ref_number": ref_numbers.get((m.ref_type, m.ref_id)),
            "note": m.note,
            "username": usernames.get(m.user_id),
            "created_at": iso(m.created_at),
        }
        for m in movements
    ]

    return jsonify({"items": items, "total": total})
