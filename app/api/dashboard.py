"""``/api/dashboard`` — the landing-page aggregate: low stock, open documents, recent activity.

08-dashboard.md calls the dashboard "the walk into the office, what needs
attention?" view and is explicit that it renders from **one** aggregate
call — "so the page renders with a single request" — rather than the
frontend firing off four separate fetches (and showing four separate
per-tile loading spinners) for counts, low stock, open work orders, and
recent activity. This module is therefore shaped differently from every
other ``app/api/*`` blueprint: one route, ``GET /api/dashboard``, that
assembles four independent aggregates and returns them as one JSON
object.

**Counting with ``GROUP BY``, not ``len(query.all())``.** For the counts
block this endpoint only ever needs *how many* work orders/POs/SOs are in
each of two statuses — never the rows themselves. Fetching every row of
``work_orders`` into Python just to call ``len()`` on a filtered list
would transfer an unbounded amount of data over the wire for a number
that Postgres can compute on its own. ``SELECT status, COUNT(*) ... GROUP
BY status`` does that counting *inside* the database and ships back only
as many rows as there are distinct statuses (at most a handful) — see
:func:`_status_counts`, used once per document table. That is the
"efficient grouped/aggregate queries" AGENTS.md's task description asks
for: one query per table for the counts block (four total, including the
low-stock count), each doing O(rows) work in Postgres instead of Python.

**Why this module keeps its own tiny ``_REF_TYPE_MODELS``/``_resolve_ref_numbers``
instead of importing ``app/api/inventory.py``'s.** ``app/api/inventory.py``'s
``list_movements`` endpoint solves the exact same "resolve a page of
``StockMovement`` rows' ``(ref_type, ref_id)`` pairs to a human-readable
document number without an N+1 query per row" problem, and this module
copies that same batching *shape* rather than reinventing it. It does not
*import* inventory.py's version because that mapping and helper are
named with a leading underscore there — a private implementation detail
of that module's own endpoint, not a contract this module should couple
itself to. Duplicating ~15 lines here is cheaper than two blueprints
silently depending on each other's "private" internals, especially since
this endpoint only ever resolves at most 10 rows' worth of references.
"""

import sqlalchemy as sa
from flask import Blueprint, jsonify
from sqlalchemy.orm import joinedload

from app.api import iso
from app.api.auth import require_login
from app.extensions import db
from app.models import Part, PurchaseOrder, SalesOrder, StockMovement, WorkOrder

#: Blueprint for ``GET /api/dashboard``. Registered in
#: ``app/__init__.py``'s ``create_app()``.
bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")

#: Referenced-document types a :class:`~app.models.StockMovement` can
#: point at, mapped to the model class and the attribute holding its
#: human-facing number (``WO-0007``-style) — mirrors
#: ``app/api/inventory.py``'s ``_REF_TYPE_MODELS`` (see module docstring
#: for why this is a local copy, not a shared import).
_REF_TYPE_MODELS = {
    "work_order": (WorkOrder, "wo_number"),
    "purchase_order": (PurchaseOrder, "po_number"),
    "sales_order": (SalesOrder, "so_number"),
}


def _status_counts(model, statuses):
    """Return ``{status: count}`` for every value in ``statuses``, via one ``GROUP BY`` query.

    ``model.status`` is a plain ``VARCHAR`` column (01-database.md's
    CHECK-constrained-varchar pattern, not a native enum — see
    ``app/models.py``'s module docstring), so grouping by it and counting
    is an ordinary aggregate query: ``SELECT status, COUNT(*) FROM
    <table> WHERE status IN (...) GROUP BY status``. Filtering to
    ``statuses`` *before* grouping (rather than grouping every status and
    discarding the ones the caller doesn't want) means Postgres never
    computes a count for, say, ``completed``/``canceled`` work orders at
    all — this dashboard never displays those.

    A status with zero matching rows simply doesn't appear in the
    ``GROUP BY`` result set (there's no row to group), so the dict is
    pre-seeded with every requested status at ``0`` and then updated from
    the query result — otherwise the caller would have to remember to
    ``.get(status, 0)`` at every read site instead of every key always
    being present.

    Args:
        model: A mapped class with a ``status`` column (``WorkOrder``,
            ``PurchaseOrder``, or ``SalesOrder``).
        statuses: The status values to count (and the only keys the
            returned dict will have).

    Returns:
        dict[str, int]: ``status -> count``, one entry per element of
        ``statuses``.
    """
    rows = (
        db.session.query(model.status, sa.func.count())
        .filter(model.status.in_(statuses))
        .group_by(model.status)
        .all()
    )
    counts = {status: 0 for status in statuses}
    counts.update({status: count for status, count in rows})
    return counts


def _resolve_ref_numbers(movements):
    """Batch-resolve ``{(ref_type, ref_id): human_number}`` for a page of movements.

    Same N+1-avoidance shape as ``app/api/inventory.py``'s
    ``_resolve_ref_numbers`` (see this module's docstring for why it's a
    separate copy): group the referenced ids by document type, then issue
    at most one ``WHERE id IN (...)`` query per type actually present
    among ``movements`` — at most three queries (work order, purchase
    order, sales order) no matter how many movements are passed in,
    instead of one query per movement.
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
def get_dashboard():
    """``GET /api/dashboard`` — the landing-page aggregate.

    Auth: any (08-dashboard.md: "any role" — there is nothing on this
    page an operator shouldn't see).

    Returns the exact shape 08-dashboard.md specifies::

        {
          "counts": {
            "work_orders": {"draft": int, "released": int},
            "purchase_orders": {"draft": int, "ordered": int},
            "sales_orders": {"draft": int, "confirmed": int},
            "low_stock_parts": int
          },
          "low_stock": [{"id", "sku", "name", "unit", "qty_on_hand",
                          "reorder_point", "shortfall"}, ...],
          "open_work_orders": [{"id", "wo_number", "product_sku",
                                 "product_name", "qty", "status",
                                 "created_at"}, ...],
          "recent_movements": [{"id", "sku", "part_name", "qty_delta",
                                 "reason", "ref_number", "username",
                                 "created_at"}, ...]
        }

    Four independent aggregates, each built with its own tightly-scoped
    query (or two) rather than one giant join, since they don't share a
    ``FROM`` clause: counts group by status per document table plus a
    low-stock count; ``low_stock``/``open_work_orders``/``recent_movements``
    are each a single capped, ordered ``SELECT`` with eager-loaded
    relationships where a row needs data from another table, so nothing
    below issues an N+1 query no matter how many rows come back.
    """
    counts = {
        "work_orders": _status_counts(WorkOrder, ("draft", "released")),
        "purchase_orders": _status_counts(PurchaseOrder, ("draft", "ordered")),
        "sales_orders": _status_counts(SalesOrder, ("draft", "confirmed")),
        "low_stock_parts": (
            db.session.query(sa.func.count(Part.id))
            .filter(Part.active.is_(True), Part.qty_on_hand <= Part.reorder_point)
            .scalar()
        ),
    }

    # --- low_stock: active, qty_on_hand <= reorder_point, worst shortfall
    # first, max 20. `shortfall` is computed in SQL (not fetched then
    # subtracted in Python) so the ORDER BY can use the same expression —
    # Postgres sorts on it directly instead of the app re-deriving and
    # re-sorting a value the database already had in hand.
    shortfall_expr = (Part.reorder_point - Part.qty_on_hand).label("shortfall")
    low_stock_rows = (
        db.session.query(Part, shortfall_expr)
        .filter(Part.active.is_(True), Part.qty_on_hand <= Part.reorder_point)
        .order_by(shortfall_expr.desc())
        .limit(20)
        .all()
    )
    low_stock = [
        {
            "id": part.id,
            "sku": part.sku,
            "name": part.name,
            "unit": part.unit,
            "qty_on_hand": float(part.qty_on_hand),
            "reorder_point": float(part.reorder_point),
            "shortfall": float(shortfall),
        }
        for part, shortfall in low_stock_rows
    ]

    # --- open_work_orders: draft + released, newest first, max 10.
    # joinedload(WorkOrder.product) pulls each WO's Part into the same
    # query via a LEFT OUTER JOIN, instead of one extra SELECT per row
    # when the dict comprehension below reads `wo.product.sku` — the same
    # N+1 avoidance app/api/work_orders.py's list_work_orders uses.
    open_wos = (
        WorkOrder.query.options(joinedload(WorkOrder.product))
        .filter(WorkOrder.status.in_(("draft", "released")))
        .order_by(WorkOrder.created_at.desc(), WorkOrder.id.desc())
        .limit(10)
        .all()
    )
    open_work_orders = [
        {
            "id": wo.id,
            "wo_number": wo.wo_number,
            "product_sku": wo.product.sku,
            "product_name": wo.product.name,
            "qty": float(wo.qty),
            "status": wo.status,
            "created_at": iso(wo.created_at),
        }
        for wo in open_wos
    ]

    # --- recent_movements: last 10, newest first. joinedload both
    # relationships this loop reads (`part`, `user`) for the same reason
    # as above; `_resolve_ref_numbers` then does its own batched lookup
    # for the loose (ref_type, ref_id) polymorphic reference, which isn't
    # a real SQLAlchemy relationship and so can't be joinedload'ed.
    movements = (
        StockMovement.query.options(
            joinedload(StockMovement.part), joinedload(StockMovement.user)
        )
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(10)
        .all()
    )
    ref_numbers = _resolve_ref_numbers(movements)
    recent_movements = [
        {
            "id": m.id,
            "sku": m.part.sku,
            "part_name": m.part.name,
            "qty_delta": float(m.qty_delta),
            "reason": m.reason,
            "ref_number": ref_numbers.get((m.ref_type, m.ref_id)),
            "username": m.user.username,
            "created_at": iso(m.created_at),
        }
        for m in movements
    ]

    return jsonify(
        {
            "counts": counts,
            "low_stock": low_stock,
            "open_work_orders": open_work_orders,
            "recent_movements": recent_movements,
        }
    )
