"""``/api/work-orders/*`` — the work order lifecycle and the completion transaction.

05-work-orders.md calls the completion transaction "the centerpiece of the
whole demo" — the concrete answer to "how does this ERP guarantee stock
never lies." A work order (WO) says "build ``qty`` units of finished
product X" and walks a small state machine::

    draft --release--> released --complete--> completed
      |                    |
      +-------cancel-------+--> canceled

- **draft** — being planned; every field is still editable.
- **released** — approved for the floor; no longer editable; waiting for
  an operator to build it. Release does *not* require stock to be on hand
  (material may still be arriving) — it only requires the product to have
  *some* BOM and every component on it to be active.
- **completed** — the build happened. In one database transaction, every
  BOM component is consumed (``wo_consume``, negative) and the finished
  product is produced (``wo_produce``, positive), both through
  ``app/services/stock.py``'s ``apply_movement()`` (AGENTS.md: that
  module is the only code allowed to touch ``parts.qty_on_hand``). If any
  component would go negative, *nothing* is written — see
  :func:`complete_work_order` for the row-locking discipline that makes
  this safe under concurrent completions.
- **canceled** — abandoned; terminal, like **completed** (no un-complete;
  a correction is a stock adjustment, per 05-work-orders.md — "realistic
  and simple").

Every action below funnels its "is this move even legal right now"
question through :func:`_guard_transition`, so every 409
``invalid_transition`` response in this module is worded the same way and
none of the five action handlers has to hand-roll its own status check.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, g, jsonify, request
from sqlalchemy.orm import joinedload

from app.api import get_json_or_400, iso
from app.api.auth import require_login
from app.errors import ApiError
from app.extensions import db
from app.models import BomLine, Part, WorkOrder
from app.services.stock import apply_movement

#: Blueprint for every ``/api/work-orders/*`` route. Registered in
#: ``app/__init__.py``'s ``create_app()``.
bp = Blueprint("work_orders", __name__, url_prefix="/api/work-orders")

#: The set of legal ``work_orders.status`` values, mirroring the database
#: CHECK constraint in ``app/models.py``. Used to validate the optional
#: ``?status=`` filter on the list endpoint.
_STATUSES = frozenset({"draft", "released", "completed", "canceled"})

#: A sentinel distinct from every legal JSON value (including ``None``),
#: used by :func:`update_work_order` to tell "the client didn't send a
#: ``notes`` key at all" apart from "the client sent ``"notes": null``"
#: (meaning "clear the notes"). ``data.get("notes")`` alone can't make
#: that distinction since both cases would return ``None``.
_UNSET = object()


def _resolve_product(product_part_id):
    """Validate a ``product_part_id`` value for work order create/update.

    Shared by :func:`create_work_order` and :func:`update_work_order`
    since 05-work-orders.md requires identical validation in both places:
    the value must be an integer naming a :class:`~app.models.Part` that
    exists, is active, and is a finished product (raw materials have no
    BOM and can't be built).

    Returns:
        tuple[Part | None, str | None]: ``(product, None)`` on success, or
        ``(None, message)`` with a field-error message on failure.
    """
    if not isinstance(product_part_id, int):
        return None, "product_part_id is required and must be an integer."
    product = db.session.get(Part, product_part_id)
    if product is None:
        return None, "Part not found."
    if not product.active:
        return None, "Part is not active."
    if product.part_type != "finished":
        return None, "Part is not a finished product."
    return product, None


def _parse_qty(qty):
    """Validate a ``qty`` value for work order create/update.

    Goes through ``str(qty)`` before ``Decimal(...)`` for the same reason
    ``app/services/stock.py``'s ``apply_movement`` does: a JSON number
    reaches Python as an ``int``/``float``, and building a ``Decimal``
    straight from a ``float`` imports that float's own binary imprecision
    (``Decimal(0.1) != Decimal("0.1")``).

    Returns:
        tuple[Decimal | None, str | None]: ``(qty, None)`` on success, or
        ``(None, message)`` on failure.
    """
    try:
        qty_dec = Decimal(str(qty))
    except (InvalidOperation, ValueError, TypeError):
        return None, "qty must be a number."
    if qty_dec <= 0:
        return None, "qty must be greater than 0."
    return qty_dec, None


def _guard_transition(wo, allowed_statuses, action):
    """Raise 409 ``invalid_transition`` unless ``wo.status`` is one of ``allowed_statuses``.

    Every status-changing endpoint in this module (edit, release,
    complete, cancel) is "only legal from certain current statuses," and
    05-work-orders.md's acceptance criteria are explicit that the wrong
    starting status is always a 409 with this exact code — never a 400.
    Centralizing the check here means every 409 in this module states the
    current status and the attempted action in the same words, instead of
    five slightly-different hand-written messages that could drift apart.

    Args:
        wo: The :class:`~app.models.WorkOrder` being acted on.
        allowed_statuses: A tuple of status strings the action is legal
            from (e.g. ``("draft",)`` for edit/release, ``("draft",
            "released")`` for cancel).
        action: A human-readable verb for the message (``"edit"``,
            ``"release"``, ``"complete"``, ``"cancel"``).

    Raises:
        ApiError: 409 ``invalid_transition`` if ``wo.status`` is not in
            ``allowed_statuses``.
    """
    if wo.status not in allowed_statuses:
        raise ApiError(
            409,
            "invalid_transition",
            f"Cannot {action} work order {wo.wo_number}: status is '{wo.status}'.",
        )


def _list_dict(wo):
    """Serialize a :class:`~app.models.WorkOrder` to the list/detail JSON shape.

    Reads ``wo.product`` and ``wo.creator`` — both many-to-one
    relationships SQLAlchemy loads lazily by primary key, which means a
    lookup that's already in the session's identity map (e.g. because
    :func:`list_work_orders` eager-loaded it with ``joinedload``, or
    because a create/update handler already holds the same row in memory)
    costs no extra query; only a genuinely unloaded relationship issues
    one. :func:`list_work_orders` still eager-loads explicitly, because
    "list all work orders" run once per row would otherwise be an N+1
    query against both ``parts`` and ``users``.
    """
    return {
        "id": wo.id,
        "wo_number": wo.wo_number,
        "status": wo.status,
        "qty": float(wo.qty),
        "product": {
            "id": wo.product.id,
            "sku": wo.product.sku,
            "name": wo.product.name,
            "unit": wo.product.unit,
        },
        "notes": wo.notes,
        "created_by_username": wo.creator.username,
        "created_at": iso(wo.created_at),
        "released_at": iso(wo.released_at),
        "completed_at": iso(wo.completed_at),
    }


def _components_block(wo):
    """Compute the live "can this WO be built right now" availability block.

    05-work-orders.md requires this to be computed **live** from the
    product's *current* BOM every time it's requested — not cached on the
    work order row — because both the BOM and on-hand quantities can
    change after the WO was created (an admin edits the recipe, stock
    arrives on a PO). ``required`` is ``qty_per`` scaled by *this* WO's
    quantity; ``short`` is how far ``on_hand`` falls below that,
    floored at zero so an over-stocked component reads as "0 short," not
    a negative number.

    Returned for every status (draft/released/completed/canceled) since
    05-work-orders.md calls it "informational" even when the WO isn't
    buildable — ``can_complete`` is what actually gates the Complete
    button, and it is unconditionally ``False`` outside ``released``.
    """
    lines = (
        BomLine.query.options(joinedload(BomLine.component))
        .filter(BomLine.product_part_id == wo.product_part_id)
        .join(Part, BomLine.component_part_id == Part.id)
        .order_by(Part.sku)
        .all()
    )

    components = []
    all_available = True
    for line in lines:
        component = line.component
        required = line.qty_per * wo.qty
        short = max(Decimal("0"), required - component.qty_on_hand)
        if short > 0:
            all_available = False
        components.append(
            {
                "part_id": component.id,
                "sku": component.sku,
                "name": component.name,
                "unit": component.unit,
                "qty_per": float(line.qty_per),
                "required": float(required),
                "on_hand": float(component.qty_on_hand),
                "short": float(short),
            }
        )

    return {
        "components": components,
        "can_complete": wo.status == "released" and all_available,
    }


def _detail_dict(wo):
    """The single-WO JSON shape: the list shape plus the live components block."""
    payload = _list_dict(wo)
    payload.update(_components_block(wo))
    return payload


@bp.route("", methods=["GET"])
@require_login()
def list_work_orders():
    """``GET /api/work-orders`` — every work order, newest first.

    Auth: any. Optional ``?status=`` filters to one status; an
    unrecognized value is ignored rather than rejected (matches
    ``app/api/inventory.py``'s ``list_parts`` precedent — a typo'd filter
    degrades to "no filter," this is a read endpoint, not a validation
    boundary).

    ``joinedload`` pulls each WO's ``product`` (a :class:`~app.models.Part`)
    and ``creator`` (a :class:`~app.models.User`) into the *same* SQL
    query via a ``LEFT OUTER JOIN``, instead of Flask-SQLAlchemy lazily
    issuing one extra ``SELECT`` per relationship per row when
    :func:`_list_dict` reads ``wo.product``/``wo.creator`` — the classic
    N+1 query pattern a list endpoint must avoid.
    """
    query = WorkOrder.query.options(
        joinedload(WorkOrder.product), joinedload(WorkOrder.creator)
    )

    status = request.args.get("status")
    if status in _STATUSES:
        query = query.filter(WorkOrder.status == status)

    work_orders = query.order_by(WorkOrder.created_at.desc(), WorkOrder.id.desc()).all()
    return jsonify({"items": [_list_dict(wo) for wo in work_orders]})


@bp.route("", methods=["POST"])
@require_login(role="admin")
def create_work_order():
    """``POST /api/work-orders`` — start a new work order in ``draft``.

    Auth: admin. Request: ``{"product_part_id", "qty", "notes"}`` (``notes``
    optional). An empty BOM is allowed at create time — 05-work-orders.md
    is explicit that this only blocks *release*, since a draft WO is just
    a plan an admin may still be assembling a recipe for.

    ``wo_number`` (``WO-0007``-style) can't be a plain column default
    because it embeds the row's own id. The pattern below — insert with a
    placeholder, ``flush()`` to make Postgres assign the identity value,
    then set the real ``wo_number`` in Python — mirrors ``app/cli.py``'s
    seed data exactly, which is deliberate: two code paths creating the
    same kind of row should number it the same way. ``flush()`` sends the
    pending ``INSERT`` to Postgres without committing (this request's
    transaction is still open, per ``app/__init__.py``'s
    ``teardown_request``), which is what makes ``wo.id`` available to
    build ``wo_number`` from.
    """
    data = get_json_or_400()
    field_errors = {}

    product, error = _resolve_product(data.get("product_part_id"))
    if error:
        field_errors["product_part_id"] = error

    qty, error = _parse_qty(data.get("qty"))
    if error:
        field_errors["qty"] = error

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        field_errors["notes"] = "notes must be a string."

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    wo = WorkOrder(
        wo_number="pending",
        product_part_id=product.id,
        qty=qty,
        notes=notes.strip() if isinstance(notes, str) else None,
        created_by=g.user.id,
    )
    db.session.add(wo)
    db.session.flush()  # assigns wo.id, needed for wo_number below
    wo.wo_number = f"WO-{wo.id:04d}"

    return jsonify(_detail_dict(wo)), 201


@bp.route("/<int:id>", methods=["GET"])
@require_login()
def get_work_order(id):
    """``GET /api/work-orders/{id}`` — one work order plus its live component availability.

    Auth: any. See :func:`_components_block` for what "live" means and
    why the block is returned regardless of status.
    """
    wo = (
        WorkOrder.query.options(joinedload(WorkOrder.product), joinedload(WorkOrder.creator))
        .filter(WorkOrder.id == id)
        .first()
    )
    if wo is None:
        raise ApiError(404, "not_found", "Work order not found.")
    return jsonify(_detail_dict(wo))


@bp.route("/<int:id>", methods=["PUT"])
@require_login(role="admin")
def update_work_order(id):
    """``PUT /api/work-orders/{id}`` — edit a draft work order.

    Auth: admin, and only while ``status == "draft"`` — once released, a
    WO represents a commitment the floor is acting on (05-work-orders.md:
    "no longer editable"), so any other status is a 409
    ``invalid_transition`` via :func:`_guard_transition`.

    Editable: ``product_part_id``, ``qty``, ``notes`` — each is optional
    per-request (a caller sends only what it's changing, matching
    ``app/api/inventory.py``'s ``update_part`` convention), but *if
    present* goes through the exact same validation as
    :func:`create_work_order` via the shared :func:`_resolve_product` /
    :func:`_parse_qty` helpers. ``"notes": null`` explicitly clears the
    notes; omitting the key entirely leaves it unchanged — the
    :data:`_UNSET` sentinel is what makes that distinction possible where
    a plain ``.get("notes")`` could not.
    """
    wo = db.session.get(WorkOrder, id)
    if wo is None:
        raise ApiError(404, "not_found", "Work order not found.")
    _guard_transition(wo, ("draft",), "edit")

    data = get_json_or_400()
    field_errors = {}

    new_product = None
    if "product_part_id" in data:
        new_product, error = _resolve_product(data.get("product_part_id"))
        if error:
            field_errors["product_part_id"] = error

    new_qty = None
    if "qty" in data:
        new_qty, error = _parse_qty(data.get("qty"))
        if error:
            field_errors["qty"] = error

    new_notes = _UNSET
    if "notes" in data:
        notes_val = data["notes"]
        if notes_val is not None and not isinstance(notes_val, str):
            field_errors["notes"] = "notes must be a string."
        else:
            new_notes = notes_val

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    if new_product is not None:
        wo.product_part_id = new_product.id
    if new_qty is not None:
        wo.qty = new_qty
    if new_notes is not _UNSET:
        wo.notes = new_notes.strip() if isinstance(new_notes, str) else None

    db.session.flush()
    return jsonify(_detail_dict(wo))


@bp.route("/<int:id>/release", methods=["POST"])
@require_login(role="admin")
def release_work_order(id):
    """``POST /api/work-orders/{id}/release`` — ``draft`` -> ``released``.

    Auth: admin. Does **not** check stock availability — 05-work-orders.md
    is explicit that shortages are allowed at release ("material may be
    arriving"); only completion enforces stock. What release *does*
    enforce, as a 400 ``validation_error`` (not 409 — this is a defect in
    the *content* being released, not an illegal state transition), is
    that the product actually has a recipe to build from:

    - the BOM must be non-empty ("Cannot release: BOM is empty.");
    - every component on it must currently be active — a component could
      have been deactivated (soft-deleted) any time after the BOM was
      last edited, so this is re-checked fresh at release time rather
      than trusted from whenever the BOM was last saved.
    """
    wo = db.session.get(WorkOrder, id)
    if wo is None:
        raise ApiError(404, "not_found", "Work order not found.")
    _guard_transition(wo, ("draft",), "release")

    lines = (
        BomLine.query.options(joinedload(BomLine.component))
        .filter(BomLine.product_part_id == wo.product_part_id)
        .all()
    )
    if not lines:
        raise ApiError(400, "validation_error", "Cannot release: BOM is empty.")

    inactive_skus = [line.component.sku for line in lines if not line.component.active]
    if inactive_skus:
        if len(inactive_skus) == 1:
            message = f"Cannot release: component {inactive_skus[0]} is inactive."
        else:
            message = f"Cannot release: components {', '.join(inactive_skus)} are inactive."
        raise ApiError(400, "validation_error", message)

    wo.status = "released"
    wo.released_at = datetime.now(timezone.utc)
    db.session.flush()
    return jsonify(_detail_dict(wo))


@bp.route("/<int:id>/complete", methods=["POST"])
@require_login()
def complete_work_order(id):
    """``POST /api/work-orders/{id}/complete`` — ``released`` -> ``completed``. The build itself.

    Auth: any (05-work-orders.md: "the operator's button" — unlike
    create/release/cancel, completing a build is day-to-day floor work,
    not a planning decision).

    **This is the transaction 05-work-orders.md calls the centerpiece of
    the whole demo**, so it earns a longer explanation than the rest of
    this module.

    **The shape of the transaction.** Read the BOM, lock every part row
    it (and the product) touches, verify every component has enough stock
    *before writing anything*, and only then apply the consume/produce
    movements — all inside this one request's single database transaction
    (``app/__init__.py``'s ``teardown_request`` commits at the end or
    rolls back the whole thing on any exception, so a shortfall discovered
    on component #3 of 5 can never leave components #1-2 partially
    consumed).

    **Why lock rows *before* checking, not check-then-lock.** Without a
    lock, two concurrent completions of different work orders that both
    need the last few units of the same raw material could each read
    "10 on hand, I only need 8, I'm fine," and both proceed to consume —
    the classic lost-update race ``app/services/stock.py``'s module
    docstring walks through in detail. Locking every row the shortfall
    check is about to read (via ``SELECT ... FOR UPDATE``,
    ``db.session.get(Part, id, with_for_update=True)``) *before* reading
    its ``qty_on_hand`` closes that race: a second transaction trying to
    lock the same row simply blocks until this one commits or rolls back,
    so by the time it gets to check, it sees this transaction's actual
    result, not a stale snapshot.

    **Why lock in ``part_id`` order — the deadlock-avoidance argument, in
    full.** Locking rows one at a time is exactly the scenario that can
    deadlock two transactions against each other. Concretely: suppose WO
    #1 needs components with ``part_id`` 4 and 9, and WO #2 (completing at
    the same instant) needs the *same two* parts. If each transaction
    locked them in whatever order its own BOM lines happened to be
    fetched in — say WO #1 locks 9 then 4, while WO #2 locks 4 then 9 —
    you can get:

    1. WO #1 locks part 9.
    2. WO #2 locks part 4.
    3. WO #1 tries to lock part 4 -> blocks, waiting on WO #2.
    4. WO #2 tries to lock part 9 -> blocks, waiting on WO #1.

    Neither can proceed; each holds a lock the other needs. Postgres
    eventually detects this and kills one transaction with a deadlock
    error, but that's a database-level failure surfacing as an unhandled
    500, not a clean "you lost the race, here's what was short" response
    — exactly the kind of bug that only shows up under real concurrent
    load, is painful to reproduce, and is entirely avoidable by
    construction. The fix is a **global, consistent lock order**: every
    transaction that ever needs to lock more than one part row locks them
    in the *same* order (here, ascending ``part_id``) — so in the example
    above, both WO #1 and WO #2 would lock part 4 first, then part 9.
    Whichever transaction gets to part 4 first simply makes the other one
    wait for *that whole* row, never for a row it's already holding a
    conflicting lock on — the circular "each waits on the other" shape
    above becomes structurally impossible. This is the standard
    database-textbook answer to lock-ordering deadlocks, and it is the
    reason ``lock_ids`` below is explicitly ``sorted()`` before the
    locking loop runs, rather than iterated in whatever order the BOM
    query happened to return.

    The product's own row is included in that same sorted, single lock
    set (not locked separately, before or after the components) —
    otherwise "lock components in order, then lock the product" would
    just relocate the same ordering hazard to "components vs. product"
    instead of eliminating it.

    **Why ``apply_movement`` re-locking the same rows is safe, not
    wasteful.** Once the loop below has locked a part with
    ``with_for_update=True``, calling ``apply_movement()`` for that same
    ``part_id`` makes it issue its own ``SELECT ... FOR UPDATE`` on the
    identical row. That is not a second, separate lock queued behind the
    first — Postgres row locks are per-transaction, not per-statement:
    the *same* transaction re-acquiring a lock it already holds is a
    no-op (it already owns it, so there's nothing to wait for). The
    up-front ordered locking above is what prevents deadlocks between
    *different* transactions; ``apply_movement`` re-locking within *this*
    transaction is just how it always behaves, unaware of (and
    unaffected by) the extra care its caller already took.

    **The shortfall check itself.** For every BOM line, ``required`` is
    ``qty_per`` scaled by this WO's ``qty``; a component is short if
    ``required`` exceeds its (now lock-guaranteed-fresh) ``on_hand``. If
    *any* line is short, the whole request raises 409
    ``insufficient_stock`` listing **every** short component (not just the
    first) — 05-work-orders.md's acceptance criteria require this so an
    operator sees the complete picture in one round trip instead of
    fixing one shortage at a time — and nothing has been written yet
    (the loop that calls ``apply_movement`` hasn't started), so the
    transaction rolling back afterward is a formality, not a safety net.
    """
    wo = db.session.get(WorkOrder, id)
    if wo is None:
        raise ApiError(404, "not_found", "Work order not found.")
    _guard_transition(wo, ("released",), "complete")

    lines = BomLine.query.filter(BomLine.product_part_id == wo.product_part_id).all()

    # Global, ascending part_id lock order — see the docstring above for
    # why this specific ordering is what makes two concurrent completions
    # deadlock-proof instead of merely "usually fine." The product's part
    # id is unioned in here (not locked separately) so it participates in
    # the same single, consistently-ordered lock acquisition.
    component_ids = {line.component_part_id for line in lines}
    lock_ids = sorted(component_ids | {wo.product_part_id})

    locked_parts = {}
    for part_id in lock_ids:
        part = db.session.get(Part, part_id, with_for_update=True)
        locked_parts[part_id] = part

    # Verify every component *before* writing anything — every row this
    # loop reads is already lock-held (above), so `qty_on_hand` here is
    # guaranteed fresh: no concurrent transaction could have changed it
    # out from under this check.
    shortfalls = []
    for line in lines:
        component = locked_parts[line.component_part_id]
        required = line.qty_per * wo.qty
        short = required - component.qty_on_hand
        if short > 0:
            shortfalls.append(
                {
                    "part_id": component.id,
                    "sku": component.sku,
                    "required": float(required),
                    "on_hand": float(component.qty_on_hand),
                    "short": float(short),
                }
            )

    if shortfalls:
        noun = "component" if len(shortfalls) == 1 else "components"
        verb = "is" if len(shortfalls) == 1 else "are"
        raise ApiError(
            409,
            "insufficient_stock",
            f"{len(shortfalls)} {noun} {verb} short.",
            details=shortfalls,
        )

    # Every component confirmed available: consume each BOM line, then
    # produce the finished good, all through apply_movement (AGENTS.md —
    # this is the only code path allowed to change qty_on_hand). Both
    # kinds of movement carry ref_type="work_order" / ref_id=wo.id so the
    # ledger can be traced back to this WO from either direction.
    for line in lines:
        apply_movement(
            part_id=line.component_part_id,
            qty_delta=-(line.qty_per * wo.qty),
            reason="wo_consume",
            user_id=g.user.id,
            ref_type="work_order",
            ref_id=wo.id,
            note=f"Consumed for {wo.wo_number}.",
        )
    apply_movement(
        part_id=wo.product_part_id,
        qty_delta=wo.qty,
        reason="wo_produce",
        user_id=g.user.id,
        ref_type="work_order",
        ref_id=wo.id,
        note=f"Produced by {wo.wo_number}.",
    )

    wo.status = "completed"
    wo.completed_at = datetime.now(timezone.utc)
    db.session.flush()

    return jsonify(_detail_dict(wo))


@bp.route("/<int:id>/cancel", methods=["POST"])
@require_login(role="admin")
def cancel_work_order(id):
    """``POST /api/work-orders/{id}/cancel`` — ``draft`` or ``released`` -> ``canceled``.

    Auth: admin. No stock effects either way: a draft never touched
    stock, and a released-but-not-yet-built WO hasn't consumed anything
    yet either — cancellation before completion is purely a status
    change. ``completed`` work orders cannot be canceled
    (05-work-orders.md: "no un-complete"; a correction after the fact is
    a manual stock adjustment through ``app/api/inventory.py``'s
    ``adjust_part``, which lands in the ledger like any other change).
    """
    wo = db.session.get(WorkOrder, id)
    if wo is None:
        raise ApiError(404, "not_found", "Work order not found.")
    _guard_transition(wo, ("draft", "released"), "cancel")

    wo.status = "canceled"
    db.session.flush()
    return jsonify(_detail_dict(wo))
