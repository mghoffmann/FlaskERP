"""The stock service: the single choke point for every change to inventory quantities.

03-inventory.md calls this "the most important code in the project" for a
reason — AGENTS.md's hardest rule (`parts.qty_on_hand` is written ONLY
here) exists so that no matter how many modules eventually touch stock
(work order completion, PO receiving, SO shipping, manual adjustments),
there is exactly one place that can get the arithmetic wrong, and exactly
one place a bug hunt ever needs to look.

**Row locking and the lost-update race.** Picture two work orders racing
to complete at the same instant, both consuming the last 5 units of the
same raw-material part:

1. Request A reads ``qty_on_hand = 5``.
2. Request B reads ``qty_on_hand = 5`` (A hasn't written yet).
3. A computes ``5 - 5 = 0``, decides that's fine, writes ``qty_on_hand = 0``.
4. B computes ``5 - 5 = 0`` from *its own* stale read, also decides that's
   fine, and also writes ``qty_on_hand = 0``.

Both requests believe they succeeded. Ten units of a part that only had
five have now been "consumed." Postgres's `qty_on_hand >= 0` CHECK
constraint doesn't catch this — every write it saw was individually
non-negative; the bug is that B's decision was based on a value A had
already invalidated. This is the classic *lost update* problem.

``SELECT ... FOR UPDATE`` (via ``db.session.get(Part, id,
with_for_update=True)``) closes the race: it takes a row-level write lock
on the part the moment it's read, and any other transaction trying to
``SELECT ... FOR UPDATE`` (or update) that same row simply blocks until
the first transaction commits or rolls back. So step 2 above doesn't
happen concurrently with step 1 — Request B's read waits behind Request
A's whole transaction, and by the time B actually reads the row it sees
the post-A value (``0``), correctly fails the insufficient-stock check,
and nothing is lost.

**Why this module never commits.** 00-architecture.md's "each request is
one transaction" rule is enforced in ``app/__init__.py``'s
``teardown_request`` handler, which commits or rolls back after the view
function returns. If ``apply_movement()`` committed internally, a caller
that needs to do more work in the same transaction after the movement
(e.g. a work order completion that calls this once per BOM component,
then flips the work order's own ``status``) would have that later work
land in a *separate* transaction from the movement — defeating the whole
point of doing it "in the caller's transaction." ``db.session.flush()``
is used instead: it sends the pending INSERT/UPDATE to Postgres (so the
row lock is held, generated defaults come back, and later queries in the
same request see the change) without ending the transaction.
"""

import decimal

from app.errors import ApiError
from app.extensions import db
from app.models import Part, StockMovement

#: The complete set of legal ``stock_movements.reason`` values, mirroring
#: the database CHECK constraint in ``app/models.py``. Checking it here
#: too means a bad reason is rejected with a clear 400 *before* any SQL
#: runs, rather than surfacing as an opaque database CHECK-violation
#: exception the caller would have to know how to interpret.
ALLOWED_REASONS = frozenset(
    {"adjustment", "wo_consume", "wo_produce", "po_receive", "so_ship"}
)


class InsufficientStockError(Exception):
    """Raised when a movement would drive a part's ``qty_on_hand`` negative.

    Deliberately a **plain** ``Exception`` subclass, not an
    :class:`app.errors.ApiError`. The stock service has no idea whether
    it's being called from an HTTP request (a work order completion route,
    an adjustment endpoint), a CLI command (``flask seed``), or a future
    background job — none of which should require this module to know
    about HTTP status codes or the ``{"error": {...}}`` JSON envelope.
    Keeping the service layer HTTP-agnostic means it stays reusable
    outside a request context, and it means the *caller* — which knows
    the domain context ("this was a work order completion consuming
    component X") — decides how to present the failure. In practice, HTTP
    callers catch this and re-raise
    ``ApiError(409, "insufficient_stock", ..., details=[...])`` per
    00-architecture.md's error table, building whatever ``details`` shape
    fits their situation (a single part for a manual adjustment, a list of
    shortfalls for a multi-component work order completion).

    Attributes:
        part: The :class:`~app.models.Part` that would have gone negative.
        required: The quantity that was needed (``abs(qty_delta)``).
        on_hand: The quantity actually on hand at the time of the check.
    """

    def __init__(self, part, required, on_hand):
        super().__init__(
            f"Insufficient stock for part {part.sku!r}: "
            f"required {required}, on hand {on_hand}."
        )
        self.part = part
        self.required = required
        self.on_hand = on_hand


def apply_movement(
    part_id, qty_delta, reason, user_id, ref_type=None, ref_id=None, note=None
):
    """Record one stock movement and update the part's running balance, atomically.

    This is the *only* function in the codebase allowed to write
    ``Part.qty_on_hand`` (AGENTS.md). Every caller — manual adjustments,
    work order consume/produce, PO receiving, SO shipping — goes through
    here so the ledger (``stock_movements``) and the running balance
    (``parts.qty_on_hand``) can never drift apart: both changes happen in
    the same flush, inside whatever transaction the caller's request is
    already in (see module docstring on why this never commits).

    Args:
        part_id: id of the :class:`~app.models.Part` to adjust.
        qty_delta: Signed change to apply — positive is stock in
            (receiving, production), negative is stock out (consumption,
            shipping, a negative adjustment). Never zero.
        reason: One of :data:`ALLOWED_REASONS` — what kind of movement
            this is.
        user_id: id of the :class:`~app.models.User` responsible, for
            attribution in the ledger.
        ref_type: Optional document-type this movement is tied to
            (``"work_order"``, ``"purchase_order"``, ``"sales_order"``).
        ref_id: Optional id of that document.
        note: Optional free-text note (required by the caller for manual
            adjustments per 03-inventory.md, but that's a route-layer
            rule, not enforced here).

    Returns:
        The newly created, flushed :class:`~app.models.StockMovement`.

    Raises:
        ApiError: 400 ``validation_error`` if ``reason`` isn't recognized
            or ``qty_delta`` is zero — both are caller bugs (a
            malformed/garbage call), not domain conditions, so they're
            rejected the same way for every caller rather than left for
            each call site to re-validate.
        InsufficientStockError: if applying ``qty_delta`` would leave
            ``qty_on_hand`` negative.
        ApiError: 404 ``not_found`` if ``part_id`` doesn't exist.
    """
    if reason not in ALLOWED_REASONS:
        raise ApiError(
            400,
            "validation_error",
            "Invalid stock movement reason.",
            field_errors={
                "reason": f"Must be one of {sorted(ALLOWED_REASONS)}."
            },
        )

    if qty_delta == 0:
        raise ApiError(
            400,
            "validation_error",
            "qty_delta must not be zero.",
            field_errors={"qty_delta": "Must not be zero."},
        )

    # `with_for_update=True` is what turns this into `SELECT ... FOR
    # UPDATE` at the SQL level — see the module docstring for why that
    # matters here specifically. `Session.get()` normally short-circuits
    # to the identity map (no SQL at all) if the object was already
    # loaded this request; passing `with_for_update` disables that
    # shortcut too, guaranteeing a fresh, lock-holding read every call.
    part = db.session.get(Part, part_id, with_for_update=True)
    if part is None:
        raise ApiError(404, "not_found", "Part not found.")

    # `parts.qty_on_hand` is a NUMERIC column, which SQLAlchemy hydrates
    # as `decimal.Decimal` (not `float`) to avoid binary floating-point
    # rounding error on money/quantity math. Routes accept quantities as
    # plain JSON numbers (00-architecture.md) and may hand this function
    # an `int` or `float`; going through `str()` first (rather than
    # `Decimal(qty_delta)` directly) avoids importing a `float`'s own
    # binary imprecision into the Decimal (`Decimal(0.1) != Decimal("0.1")`).
    delta = decimal.Decimal(str(qty_delta))
    new_qty = part.qty_on_hand + delta

    if new_qty < 0:
        raise InsufficientStockError(part, required=abs(delta), on_hand=part.qty_on_hand)

    part.qty_on_hand = new_qty

    movement = StockMovement(
        part_id=part.id,
        qty_delta=delta,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
        user_id=user_id,
    )
    db.session.add(movement)

    # Send the pending UPDATE (part) and INSERT (movement) to Postgres
    # now, without committing (see module docstring). (The FOR UPDATE row
    # lock was already acquired above, by the SELECT that `db.session.get`
    # issued — a flush only sends buffered DML.) Flushing here populates
    # `movement.id`/`movement.created_at` from their server-side defaults
    # so callers can use them immediately (e.g. for `ref_number` display).
    db.session.flush()

    return movement
