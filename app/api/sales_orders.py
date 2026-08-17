"""``/api/customers/*`` and ``/api/sales-orders/*`` — customers and the sales order lifecycle.

07-sales-orders.md describes sales as "purchasing with the stock arrow
reversed": a :class:`~app.models.Customer` buys the factory's finished
goods, and a **sales order (SO)** walks a small state machine::

    draft --confirm--> confirmed --ship--> shipped
      |                    |
      +-------cancel-------+--> canceled

- **draft** — the quote is still being written; lines fully editable.
- **confirmed** — the customer committed; frozen from further edits.
  Confirmation deliberately does **not** reserve stock — allocating stock
  against a not-yet-shipped order is a real-ERP feature (partial
  fulfillment, backorder tracking, reservation expiry) that adds a whole
  second layer of bookkeeping for a demo-scale project to model correctly.
  07-sales-orders.md calls this out explicitly as an out-of-scope
  simplification, so an SO can sit ``confirmed`` for any length of time
  while stock for its lines is sold to someone else entirely; the shipping
  transaction below is what finally checks (and reserves, by consuming)
  the real inventory, at the moment goods actually leave the building.
- **shipped** — the truck left. One transaction decrements stock per line
  (reason ``so_ship``) through ``app/services/stock.py``. Only a *complete*
  shipment is supported (07-sales-orders.md: "single full shipment only —
  no partials"), so like work order completion, shipping either moves
  every line or moves nothing.
- **canceled** — terminal, reachable from ``draft`` or ``confirmed``.

Only ``finished`` parts may appear on an SO line — 07-sales-orders.md's
scope choice that "the factory doesn't sell raw stock" keeps the buy
(raw materials) / build (BOM) / sell (finished goods) story clean.

This module also owns ``/api/customers/*`` (07-sales-orders.md: "identical
in shape to suppliers" from 06-purchasing.md, with ``customer``/
``sales_orders`` substituted) since a sales order can't exist without a
customer to bill it to, and the two resources ship together in this
project's phase plan.

Every status-changing SO endpoint funnels its "is this move even legal
right now" question through :func:`_guard_so_transition` (naming the
document by its ``so_number`` in the message, same convention as
``app/api/work_orders.py``'s ``_guard_transition``), so every 409
``invalid_transition`` in this module reads the same way.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, g, jsonify, request
from sqlalchemy.orm import joinedload, selectinload

from app.api import get_json_or_400, iso
from app.api.auth import require_login
from app.errors import ApiError
from app.extensions import db
from app.models import Customer, Part, SalesOrder, SOLine
from app.services.stock import apply_movement

#: Blueprint for both ``/api/customers/*`` and ``/api/sales-orders/*``.
#: Unlike most other API modules, this one is given **no**
#: ``url_prefix`` — it owns two independent URL trees, not one — so every
#: ``@bp.route(...)`` call below spells its full ``/api/...`` path
#: explicitly. Registered in ``app/__init__.py``'s ``create_app()``.
bp = Blueprint("sales_orders", __name__)

#: The set of legal ``sales_orders.status`` values, mirroring the database
#: CHECK constraint in ``app/models.py``. Used to validate the optional
#: ``?status=`` filter on the sales order list endpoint.
_SO_STATUSES = frozenset({"draft", "confirmed", "shipped", "canceled"})

#: A sentinel distinct from every legal JSON value (including ``None``),
#: used by the customer/SO ``PUT`` handlers to tell "the client didn't
#: send this key at all" apart from "the client explicitly sent
#: ``null``" — see ``app/api/work_orders.py``'s identical ``_UNSET`` for
#: the full rationale.
_UNSET = object()


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _parse_decimal(value, field_name, allow_zero):
    """Parse a JSON value into a :class:`decimal.Decimal`, enforcing a sign rule.

    Shared by every quantity/money field in this module (``qty`` on an SO
    line must be strictly positive; ``unit_price`` may be zero but not
    negative). Goes through ``str(value)`` before ``Decimal(...)`` for the
    same reason ``app/services/stock.py``'s ``apply_movement`` does: a
    JSON number arrives as a Python ``int``/``float``, and building a
    ``Decimal`` straight from a ``float`` would import that float's own
    binary imprecision (``Decimal(0.1) != Decimal("0.1")``).

    Args:
        value: The raw JSON value to parse.
        field_name: Human-readable field name, used in the error message.
        allow_zero: ``True`` to accept ``0`` (unit prices), ``False`` to
            require a strictly positive value (quantities).

    Returns:
        tuple[Decimal | None, str | None]: ``(value, None)`` on success,
        or ``(None, message)`` on failure.
    """
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None, f"{field_name} must be a number."
    if allow_zero:
        if dec < 0:
            return None, f"{field_name} must be zero or greater."
    else:
        if dec <= 0:
            return None, f"{field_name} must be greater than 0."
    return dec, None


def _resolve_customer(customer_id):
    """Validate a ``customer_id`` value for sales order create/update.

    Mirrors ``app/api/work_orders.py``'s ``_resolve_product``: the value
    must be an integer naming a :class:`~app.models.Customer` that exists
    and is active (an inactive customer shouldn't be receiving new
    orders, same reasoning as an inactive supplier on the purchasing
    side).

    Returns:
        tuple[Customer | None, str | None]: ``(customer, None)`` on
        success, or ``(None, message)`` on failure.
    """
    if not isinstance(customer_id, int):
        return None, "customer_id is required and must be an integer."
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return None, "Customer not found."
    if not customer.active:
        return None, "Customer is not active."
    return customer, None


def _parse_lines(lines_raw):
    """Validate the ``lines`` array shared by ``POST``/``PUT`` on a sales order.

    07-sales-orders.md's validation list for a line, all enforced here:
    at least one line; each ``part_id`` names a part that exists, is
    active, **and** is ``part_type == "finished"`` — the factory doesn't
    sell raw stock, so a raw-material line is rejected with a message
    naming exactly which line index is the offender, not just "invalid
    lines" — ``qty`` > 0; ``unit_price`` >= 0; no two lines naming the
    same part (mirrors the ``uq_so_lines_so_id_part_id`` database
    constraint, so a bad request fails with a clear 400 instead of a raw
    integrity-error 500 at flush time).

    Per-line problems are reported as ``details`` entries of the shape
    ``{"line": <index>, "field": <name>, "message": ...}`` — the same
    line-indexed convention ``app/api/bom.py``'s BOM editor established —
    so the frontend's line-item grid can attach each message to the row
    (and input) it belongs to. One bad request can have several bad lines
    at once, and 05-work-orders.md's "list every shortfall, not just the
    first" spirit applies just as well to input validation as it does to
    the shipping transaction below. Only the list-level problem ("no
    lines at all") is a plain field error, since there is no line to
    index.

    Args:
        lines_raw: The raw ``data.get("lines")`` value from the request.

    Returns:
        tuple[list[dict] | None, dict, list]: ``(parsed_lines, {}, [])``
        on success, where each parsed entry is ``{"part": Part, "qty":
        Decimal, "unit_price": Decimal}``; or ``(None, field_errors,
        details)`` on failure.
    """
    if not isinstance(lines_raw, list) or not lines_raw:
        return None, {"lines": "At least one line is required."}, []

    details = []
    seen_part_ids = set()
    parsed = []

    for idx, raw_line in enumerate(lines_raw):
        if not isinstance(raw_line, dict):
            details.append({"line": idx, "message": "Each line must be an object."})
            continue

        part_id = raw_line.get("part_id")
        part = None
        if not isinstance(part_id, int):
            details.append(
                {"line": idx, "field": "part_id", "message": "part_id is required and must be an integer."}
            )
        else:
            part = db.session.get(Part, part_id)
            if part is None:
                details.append({"line": idx, "field": "part_id", "message": "Part not found."})
            elif not part.active:
                details.append({"line": idx, "field": "part_id", "message": "Part is not active."})
            elif part.part_type != "finished":
                details.append(
                    {
                        "line": idx,
                        "field": "part_id",
                        "message": "Part is not a finished product; the factory doesn't sell raw stock.",
                    }
                )
            elif part_id in seen_part_ids:
                details.append(
                    {"line": idx, "field": "part_id", "message": "Duplicate part on this order."}
                )
            else:
                seen_part_ids.add(part_id)

        qty, qty_error = _parse_decimal(raw_line.get("qty"), "qty", allow_zero=False)
        if qty_error:
            details.append({"line": idx, "field": "qty", "message": qty_error})

        unit_price, price_error = _parse_decimal(
            raw_line.get("unit_price"), "unit_price", allow_zero=True
        )
        if price_error:
            details.append({"line": idx, "field": "unit_price", "message": price_error})

        if part is not None and qty is not None and unit_price is not None:
            parsed.append({"part": part, "qty": qty, "unit_price": unit_price})

    if details:
        return None, {}, details
    return parsed, {}, []


def _guard_so_transition(so, allowed_statuses, action):
    """Raise 409 ``invalid_transition`` unless ``so.status`` is one of ``allowed_statuses``.

    The sales order sibling of ``app/api/work_orders.py``'s
    ``_guard_transition`` — same reasoning applies verbatim: every
    status-changing SO endpoint (edit, confirm, ship, cancel) is only
    legal from certain current statuses, and every wrong-status attempt
    across all of them should read as the same 409, naming the order's
    ``so_number``, its current ``status``, and the ``action`` that was
    refused, rather than five independently-worded checks.

    Args:
        so: The :class:`~app.models.SalesOrder` being acted on.
        allowed_statuses: Tuple of status strings the action is legal
            from.
        action: Human-readable verb for the message (``"edit"``,
            ``"confirm"``, ``"ship"``, ``"cancel"``).

    Raises:
        ApiError: 409 ``invalid_transition`` if ``so.status`` is not in
            ``allowed_statuses``.
    """
    if so.status not in allowed_statuses:
        raise ApiError(
            409,
            "invalid_transition",
            f"Cannot {action} sales order {so.so_number}: status is '{so.status}'.",
        )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _customer_dict(customer):
    """Serialize a :class:`~app.models.Customer` to the JSON shape every customer endpoint returns."""
    return {
        "id": customer.id,
        "name": customer.name,
        "contact_name": customer.contact_name,
        "email": customer.email,
        "phone": customer.phone,
        "active": customer.active,
    }


def _so_list_dict(so):
    """Serialize a :class:`~app.models.SalesOrder` to the list/detail JSON shape.

    ``total``/``line_count`` are derived from ``so.lines`` in Python
    rather than a separate ``SUM(...)``/``COUNT(...)`` query per order —
    see :func:`list_sales_orders` and :func:`get_customer`, both of which
    eager-load ``lines`` with ``selectinload`` before calling this, so
    reading ``so.lines`` here never issues its own query (the classic N+1
    a list endpoint must avoid). Reads ``so.customer``/``so.creator``
    for the same reason: callers eager-load both with ``joinedload``.
    """
    total = sum((line.qty * line.unit_price for line in so.lines), Decimal("0"))
    return {
        "id": so.id,
        "so_number": so.so_number,
        "status": so.status,
        "customer": {"id": so.customer.id, "name": so.customer.name},
        "total": float(total),
        "line_count": len(so.lines),
        "notes": so.notes,
        "created_by_username": so.creator.username,
        "created_at": iso(so.created_at),
        "confirmed_at": iso(so.confirmed_at),
        "shipped_at": iso(so.shipped_at),
    }


def _so_line_dict(line):
    """Serialize one :class:`~app.models.SOLine` plus its *live* stock availability.

    ``on_hand`` is read straight off ``line.part.qty_on_hand`` at request
    time (not cached anywhere on the line), and ``short`` is
    ``max(0, qty - on_hand)`` — the same "computed live, floored at zero"
    idea as ``app/api/work_orders.py``'s ``_components_block``, so the
    sales order detail page can show whether shipping would succeed
    *before* an operator clicks Ship, using numbers that are only ever as
    stale as this one HTTP response.
    """
    part = line.part
    line_total = line.qty * line.unit_price
    short = max(Decimal("0"), line.qty - part.qty_on_hand)
    return {
        "id": line.id,
        "part_id": part.id,
        "sku": part.sku,
        "name": part.name,
        "unit": part.unit,
        "qty": float(line.qty),
        "unit_price": float(line.unit_price),
        "line_total": float(line_total),
        "on_hand": float(part.qty_on_hand),
        "short": float(short),
    }


def _so_detail_dict(so):
    """The single-SO JSON shape: the list shape plus per-line live availability.

    Lines are sorted by ``id`` (insertion order) rather than trusted to
    whatever order ``so.lines`` happens to iterate in — the relationship
    on :class:`~app.models.SalesOrder` has no ``order_by=``, so relying
    on unspecified row order would make the lines list's order an
    implementation detail that could silently shuffle between requests.
    """
    payload = _so_list_dict(so)
    payload["lines"] = [_so_line_dict(line) for line in sorted(so.lines, key=lambda ln: ln.id)]
    return payload


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


@bp.route("/api/customers", methods=["GET"])
@require_login()
def list_customers():
    """``GET /api/customers`` — the customer directory, sorted by name.

    Auth: any. Optional ``active`` (``true`` — the default — restricts to
    active customers; ``all`` includes deactivated ones; any other value
    falls back to the default) and ``search`` (case-insensitive substring
    match on ``name``) — same filter semantics as
    ``app/api/inventory.py``'s ``list_parts``.
    """
    query = Customer.query

    search = request.args.get("search")
    if search:
        query = query.filter(Customer.name.ilike(f"%{search}%"))

    if request.args.get("active", "true") != "all":
        query = query.filter(Customer.active.is_(True))

    customers = query.order_by(Customer.name).all()
    return jsonify({"items": [_customer_dict(c) for c in customers]})


@bp.route("/api/customers", methods=["POST"])
@require_login(role="admin")
def create_customer():
    """``POST /api/customers`` — create a new customer.

    Auth: admin. Request: ``{"name", "contact_name", "email", "phone"}``
    — ``name`` required and unique; the rest optional, matching
    06-purchasing.md's supplier shape exactly.
    """
    data = get_json_or_400()
    field_errors = {}

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        field_errors["name"] = "Name is required."
    elif Customer.query.filter_by(name=name).first() is not None:
        field_errors["name"] = "Name already exists."

    for field in ("contact_name", "email", "phone"):
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            field_errors[field] = f"{field} must be a string."

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    customer = Customer(
        name=name.strip(),
        contact_name=(data.get("contact_name") or "").strip() or None,
        email=(data.get("email") or "").strip() or None,
        phone=(data.get("phone") or "").strip() or None,
    )
    db.session.add(customer)
    db.session.flush()  # assigns customer.id
    return jsonify(_customer_dict(customer)), 201


@bp.route("/api/customers/<int:id>", methods=["GET"])
@require_login()
def get_customer(id):
    """``GET /api/customers/{id}`` — one customer plus its sales order history.

    Auth: any. 07-sales-orders.md: the detail shape adds
    ``"sales_orders": [...]`` in the list shape, newest first.
    ``selectinload(SalesOrder.lines)`` fetches every line for every one of
    this customer's orders in one extra query (rather than one query per
    order) so :func:`_so_list_dict`'s ``total``/``line_count`` computation
    stays N+1-free even on a customer with a long order history.
    """
    customer = db.session.get(Customer, id)
    if customer is None:
        raise ApiError(404, "not_found", "Customer not found.")

    sales_orders = (
        SalesOrder.query.options(
            joinedload(SalesOrder.customer),
            joinedload(SalesOrder.creator),
            selectinload(SalesOrder.lines),
        )
        .filter(SalesOrder.customer_id == id)
        .order_by(SalesOrder.created_at.desc(), SalesOrder.id.desc())
        .all()
    )

    payload = _customer_dict(customer)
    payload["sales_orders"] = [_so_list_dict(so) for so in sales_orders]
    return jsonify(payload)


@bp.route("/api/customers/<int:id>", methods=["PUT"])
@require_login(role="admin")
def update_customer(id):
    """``PUT /api/customers/{id}`` — edit a customer's contact fields.

    Auth: admin. All four fields (``name``, ``contact_name``, ``email``,
    ``phone``) are independently optional per-request, matching
    ``app/api/inventory.py``'s ``update_part`` convention: a caller sends
    only what it's changing.
    """
    customer = db.session.get(Customer, id)
    if customer is None:
        raise ApiError(404, "not_found", "Customer not found.")

    data = get_json_or_400()
    field_errors = {}

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            field_errors["name"] = "Name is required."
        elif Customer.query.filter(Customer.name == name, Customer.id != id).first() is not None:
            field_errors["name"] = "Name already exists."

    for field in ("contact_name", "email", "phone"):
        if field in data:
            value = data[field]
            if value is not None and not isinstance(value, str):
                field_errors[field] = f"{field} must be a string."

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    if "name" in data:
        customer.name = data["name"].strip()
    for field in ("contact_name", "email", "phone"):
        if field in data:
            value = data[field]
            setattr(customer, field, value.strip() or None if isinstance(value, str) else value)

    db.session.flush()
    return jsonify(_customer_dict(customer))


@bp.route("/api/customers/<int:id>", methods=["DELETE"])
@require_login(role="admin")
def deactivate_customer(id):
    """``DELETE /api/customers/{id}`` — soft delete (``active = false``).

    Auth: admin. 409 ``conflict`` if the customer has a ``draft`` or
    ``confirmed`` sales order — those are still-open commitments; a
    ``shipped`` or ``canceled`` order is history and doesn't block
    deactivation, mirroring 06-purchasing.md's "draft or ordered PO"
    check for suppliers exactly.
    """
    customer = db.session.get(Customer, id)
    if customer is None:
        raise ApiError(404, "not_found", "Customer not found.")

    open_so = SalesOrder.query.filter(
        SalesOrder.customer_id == id, SalesOrder.status.in_(("draft", "confirmed"))
    ).first()
    if open_so is not None:
        raise ApiError(409, "conflict", "Customer has a draft or confirmed sales order.")

    customer.active = False
    db.session.flush()
    return jsonify(_customer_dict(customer))


@bp.route("/api/customers/<int:id>/activate", methods=["POST"])
@require_login(role="admin")
def activate_customer(id):
    """``POST /api/customers/{id}/activate`` — reverse a soft delete.

    Auth: admin. No conflict checks in this direction — reactivating a
    customer can't put any document into an invalid state.
    """
    customer = db.session.get(Customer, id)
    if customer is None:
        raise ApiError(404, "not_found", "Customer not found.")

    customer.active = True
    db.session.flush()
    return jsonify(_customer_dict(customer))


# ---------------------------------------------------------------------------
# Sales orders
# ---------------------------------------------------------------------------


@bp.route("/api/sales-orders", methods=["GET"])
@require_login()
def list_sales_orders():
    """``GET /api/sales-orders`` — every sales order, newest first.

    Auth: any. Optional ``status`` (ignored if unrecognized, matching
    ``app/api/work_orders.py``'s ``list_work_orders`` precedent — a
    typo'd filter degrades to "no filter" on a read endpoint) and
    ``customer_id`` (ignored if not a valid integer).

    ``joinedload`` pulls ``customer``/``creator`` into the same query via
    a ``LEFT OUTER JOIN``; ``selectinload`` pulls every matching order's
    ``lines`` in one additional ``WHERE so_id IN (...)`` query. Together
    that's a constant number of queries regardless of how many sales
    orders are returned — the N+1 pattern 07-sales-orders.md's "no N+1"
    requirement is explicit about avoiding, since ``total``/``line_count``
    both need each order's lines.
    """
    query = SalesOrder.query.options(
        joinedload(SalesOrder.customer),
        joinedload(SalesOrder.creator),
        selectinload(SalesOrder.lines),
    )

    status = request.args.get("status")
    if status in _SO_STATUSES:
        query = query.filter(SalesOrder.status == status)

    customer_id = request.args.get("customer_id", type=int)
    if customer_id is not None:
        query = query.filter(SalesOrder.customer_id == customer_id)

    sales_orders = query.order_by(SalesOrder.created_at.desc(), SalesOrder.id.desc()).all()
    return jsonify({"items": [_so_list_dict(so) for so in sales_orders]})


@bp.route("/api/sales-orders", methods=["POST"])
@require_login(role="admin")
def create_sales_order():
    """``POST /api/sales-orders`` — start a new sales order in ``draft``.

    Auth: admin. Request: ``{"customer_id", "notes", "lines": [...]}`` —
    see :func:`_resolve_customer` / :func:`_parse_lines` for the full
    validation this delegates to.

    ``so_number`` (``SO-0007``-style) is assigned the same way
    ``app/api/work_orders.py``'s ``wo_number`` is: insert with a
    placeholder, ``flush()`` so Postgres assigns the identity value, then
    set the real number from ``so.id``.
    """
    data = get_json_or_400()
    field_errors = {}

    customer, error = _resolve_customer(data.get("customer_id"))
    if error:
        field_errors["customer_id"] = error

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        field_errors["notes"] = "notes must be a string."

    lines, line_field_errors, line_details = _parse_lines(data.get("lines"))
    field_errors.update(line_field_errors)

    if field_errors or line_details:
        raise ApiError(
            400,
            "validation_error",
            "Invalid input.",
            field_errors=field_errors or None,
            details=line_details or None,
        )

    so = SalesOrder(
        so_number="pending",
        customer_id=customer.id,
        notes=notes.strip() if isinstance(notes, str) else None,
        created_by=g.user.id,
    )
    for line in lines:
        so.lines.append(SOLine(part_id=line["part"].id, qty=line["qty"], unit_price=line["unit_price"]))
    db.session.add(so)
    db.session.flush()  # assigns so.id, needed for so_number below
    so.so_number = f"SO-{so.id:04d}"

    return jsonify(_so_detail_dict(so)), 201


@bp.route("/api/sales-orders/<int:id>", methods=["GET"])
@require_login()
def get_sales_order(id):
    """``GET /api/sales-orders/{id}`` — one sales order plus live per-line stock availability.

    Auth: any. See :func:`_so_line_dict` for what "live" means. Eager
    loads ``customer``/``creator``/``lines`` (and each line's ``part``, so
    :func:`_so_line_dict` can read ``sku``/``name``/``unit``/``qty_on_hand``
    without a query per line) in one round trip.
    """
    so = (
        SalesOrder.query.options(
            joinedload(SalesOrder.customer),
            joinedload(SalesOrder.creator),
            joinedload(SalesOrder.lines).joinedload(SOLine.part),
        )
        .filter(SalesOrder.id == id)
        .first()
    )
    if so is None:
        raise ApiError(404, "not_found", "Sales order not found.")
    return jsonify(_so_detail_dict(so))


@bp.route("/api/sales-orders/<int:id>", methods=["PUT"])
@require_login(role="admin")
def update_sales_order(id):
    """``PUT /api/sales-orders/{id}`` — replace a draft sales order's contents.

    Auth: admin, and only while ``status == "draft"`` (else 409
    ``invalid_transition``) — 07-sales-orders.md is explicit an SO is
    frozen the moment it's confirmed. Takes the same request shape as
    ``POST`` and **replaces every line wholesale** (07-sales-orders.md:
    "replace-all lines") rather than patching individual lines in place —
    reassigning ``so.lines`` to a fresh list lets SQLAlchemy's
    ``cascade="all, delete-orphan"`` (declared on
    :class:`~app.models.SalesOrder`) delete whichever old lines aren't in
    the new list and insert the new ones, in one flush.
    """
    so = db.session.get(SalesOrder, id)
    if so is None:
        raise ApiError(404, "not_found", "Sales order not found.")
    _guard_so_transition(so, ("draft",), "edit")

    data = get_json_or_400()
    field_errors = {}

    customer, error = _resolve_customer(data.get("customer_id"))
    if error:
        field_errors["customer_id"] = error

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        field_errors["notes"] = "notes must be a string."

    lines, line_field_errors, line_details = _parse_lines(data.get("lines"))
    field_errors.update(line_field_errors)

    if field_errors or line_details:
        raise ApiError(
            400,
            "validation_error",
            "Invalid input.",
            field_errors=field_errors or None,
            details=line_details or None,
        )

    so.customer_id = customer.id
    so.notes = notes.strip() if isinstance(notes, str) else None
    so.lines = [
        SOLine(part_id=line["part"].id, qty=line["qty"], unit_price=line["unit_price"])
        for line in lines
    ]

    db.session.flush()
    return jsonify(_so_detail_dict(so))


@bp.route("/api/sales-orders/<int:id>/confirm", methods=["POST"])
@require_login(role="admin")
def confirm_sales_order(id):
    """``POST /api/sales-orders/{id}/confirm`` — ``draft`` -> ``confirmed``.

    Auth: admin. Sets ``confirmed_at``. Deliberately does **not** touch
    stock in any way — see the module docstring's "confirmed" bullet for
    why reserving inventory at this step is an out-of-scope real-ERP
    feature this project skips: confirming only freezes the order's
    *content* (no more line edits), it makes no promise yet about which
    physical units will fulfill it.
    """
    so = db.session.get(SalesOrder, id)
    if so is None:
        raise ApiError(404, "not_found", "Sales order not found.")
    _guard_so_transition(so, ("draft",), "confirm")

    so.status = "confirmed"
    so.confirmed_at = datetime.now(timezone.utc)
    db.session.flush()
    return jsonify(_so_detail_dict(so))


@bp.route("/api/sales-orders/<int:id>/ship", methods=["POST"])
@require_login()
def ship_sales_order(id):
    """``POST /api/sales-orders/{id}/ship`` — ``confirmed`` -> ``shipped``. Goods leave the building.

    Auth: any (07-sales-orders.md: "the operator loads the truck" — same
    day-to-day-floor-work reasoning as ``app/api/work_orders.py``'s
    ``complete_work_order`` being open to any logged-in user).

    **Same transaction shape as work order completion, stock arrow
    reversed.** Read every line, lock every part row the order's lines
    touch (``SELECT ... FOR UPDATE`` via ``with_for_update=True``) in
    ascending ``part_id`` order, verify *all* lines have enough stock
    before writing anything, and only then apply one ``so_ship`` movement
    per line — all inside this one request's transaction, so a shortfall
    discovered on line 3 of 4 can never leave lines 1-2 partially shipped.

    The row-locking discipline — locking before checking (to close the
    lost-update race two concurrent shipments could otherwise hit on the
    same part) and locking in a single global ascending-``part_id`` order
    (to make that locking deadlock-proof rather than merely "usually
    fine") — is exactly ``complete_work_order``'s reasoning, applied here
    to SO lines instead of BOM lines; see that function's docstring in
    ``app/api/work_orders.py`` for the full derivation rather than
    repeating it verbatim.

    On success, every line's ``apply_movement`` call carries
    ``ref_type="sales_order"``/``ref_id=so.id`` so the ledger traces back
    to this SO from either direction, matching ``wo_consume``/
    ``wo_produce``'s ``ref_type="work_order"`` convention.

    Raises:
        ApiError: 409 ``invalid_transition`` if ``so`` is not
            ``confirmed``. 409 ``insufficient_stock`` — message
            ``"N lines are short."`` (or ``"1 line is short."`` for
            exactly one) — listing every short line as
            ``{"part_id", "sku", "required", "on_hand", "short"}``, with
            nothing written, if any line's ``qty`` exceeds its part's
            (lock-guaranteed-fresh) ``qty_on_hand``.
    """
    so = (
        SalesOrder.query.options(joinedload(SalesOrder.lines))
        .filter(SalesOrder.id == id)
        .first()
    )
    if so is None:
        raise ApiError(404, "not_found", "Sales order not found.")
    _guard_so_transition(so, ("confirmed",), "ship")

    # Global, ascending part_id lock order — see the docstring above and
    # complete_work_order's in app/api/work_orders.py for why this
    # specific ordering is what makes two concurrent shipments (or a
    # shipment racing a work order completion over a shared part)
    # deadlock-proof instead of merely "usually fine".
    lock_ids = sorted({line.part_id for line in so.lines})
    locked_parts = {
        part_id: db.session.get(Part, part_id, with_for_update=True) for part_id in lock_ids
    }

    # Verify every line *before* writing anything — every row this loop
    # reads is already lock-held (above), so `qty_on_hand` here is
    # guaranteed fresh: no concurrent transaction could have changed it
    # out from under this check.
    shortfalls = []
    for line in so.lines:
        part = locked_parts[line.part_id]
        short = line.qty - part.qty_on_hand
        if short > 0:
            shortfalls.append(
                {
                    "part_id": part.id,
                    "sku": part.sku,
                    "required": float(line.qty),
                    "on_hand": float(part.qty_on_hand),
                    "short": float(short),
                }
            )

    if shortfalls:
        noun = "line" if len(shortfalls) == 1 else "lines"
        verb = "is" if len(shortfalls) == 1 else "are"
        raise ApiError(
            409,
            "insufficient_stock",
            f"{len(shortfalls)} {noun} {verb} short.",
            details=shortfalls,
        )

    # Every line confirmed available: ship each one through apply_movement
    # (AGENTS.md — the only code path allowed to change qty_on_hand).
    for line in so.lines:
        apply_movement(
            part_id=line.part_id,
            qty_delta=-line.qty,
            reason="so_ship",
            user_id=g.user.id,
            ref_type="sales_order",
            ref_id=so.id,
            note=f"Shipped for {so.so_number}.",
        )

    so.status = "shipped"
    so.shipped_at = datetime.now(timezone.utc)
    db.session.flush()

    return jsonify(_so_detail_dict(so))


@bp.route("/api/sales-orders/<int:id>/cancel", methods=["POST"])
@require_login(role="admin")
def cancel_sales_order(id):
    """``POST /api/sales-orders/{id}/cancel`` — ``draft`` or ``confirmed`` -> ``canceled``.

    Auth: admin. No stock effects either way: neither status has shipped
    anything yet (confirmation never touches stock, per this module's
    docstring). A ``shipped`` order cannot be canceled — 07-sales-orders.md
    has no "un-ship" concept, mirroring work orders' "no un-complete";
    a correction after shipping is a manual stock adjustment, not a
    cancellation.
    """
    so = db.session.get(SalesOrder, id)
    if so is None:
        raise ApiError(404, "not_found", "Sales order not found.")
    _guard_so_transition(so, ("draft", "confirmed"), "cancel")

    so.status = "canceled"
    db.session.flush()
    return jsonify(_so_detail_dict(so))
