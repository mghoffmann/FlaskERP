"""Tests for `flask seed` (`app.cli.seed`) -- idempotency and ledger integrity.

01-database.md's acceptance criteria for seeding are: running it twice
produces no duplicates, and after seeding the movements ledger reconciles
with `qty_on_hand` for every part. These tests call `seed()` (the plain
function `app/cli.py` wraps in the `flask seed` Click command) directly,
inside the app context `conftest.py`'s `_isolated_db` fixture already
pushed — no subprocess, no CLI invocation needed.

Nothing here is monkeypatched: `seed()`'s own idempotency check ("does a
user already exist?") is exactly what's under test, so calling it twice
for real is the point.
"""

import sqlalchemy as sa

from app.cli import seed
from app.extensions import db
from app.models import Part, StockMovement, User, WorkOrder


def test_seed_twice_creates_no_duplicate_users_or_parts():
    seed()
    seed()

    usernames = [u.username for u in db.session.query(User).all()]
    assert len(usernames) == len(set(usernames)) == 2

    skus = [p.sku for p in db.session.query(Part).all()]
    assert len(skus) == len(set(skus)) == 13  # 10 raw + 3 finished (01-database.md)


def test_seed_ledger_reconciles_with_qty_on_hand_for_every_part():
    seed()
    seed()  # idempotency shouldn't disturb reconciliation either

    parts = db.session.query(Part).all()
    assert parts, "seed() should have created parts"

    for part in parts:
        movement_total = db.session.query(
            sa.func.coalesce(sa.func.sum(StockMovement.qty_delta), 0)
        ).filter(StockMovement.part_id == part.id).scalar()

        assert part.qty_on_hand == movement_total, (
            f"{part.sku}: qty_on_hand={part.qty_on_hand} != "
            f"sum(movements.qty_delta)={movement_total}"
        )


def test_seed_has_at_least_two_parts_at_or_below_reorder_point():
    """01-database.md: "at least 2 seeded below reorder point so the dashboard shows something"."""
    seed()

    low_stock_parts = [
        part
        for part in db.session.query(Part).filter(Part.active.is_(True)).all()
        if part.qty_on_hand <= part.reorder_point
    ]
    assert len(low_stock_parts) >= 2


def test_seed_completed_wo_movements_reference_it_with_consume_and_produce_reasons():
    seed()

    completed_wo = db.session.query(WorkOrder).filter_by(status="completed").one()

    movements = (
        db.session.query(StockMovement)
        .filter_by(ref_type="work_order", ref_id=completed_wo.id)
        .all()
    )
    assert movements

    reasons = {m.reason for m in movements}
    assert reasons == {"wo_consume", "wo_produce"}

    produce_rows = [m for m in movements if m.reason == "wo_produce"]
    assert len(produce_rows) == 1
    assert produce_rows[0].qty_delta == completed_wo.qty
    assert produce_rows[0].part_id == completed_wo.product_part_id

    consume_rows = [m for m in movements if m.reason == "wo_consume"]
    assert len(consume_rows) >= 1
    assert all(row.qty_delta < 0 for row in consume_rows)
