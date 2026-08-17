"""Tests for `app/services/stock.py`'s `apply_movement()`.

03-inventory.md calls this "the most important code in the project" and
10-testing.md asks for it to be tested early and directly — these tests
call `apply_movement()` in-process (no HTTP, no auth) inside the app
context `conftest.py`'s `_isolated_db` fixture already pushed for us, so
they exercise exactly the function AGENTS.md says is the only code allowed
to write `parts.qty_on_hand`.

Per 10-testing.md's acceptance criterion ("deleting the FOR UPDATE lock or
a status guard makes at least one test fail"): `test_negative_result_...`
below is one of the two hand-verified spot checks — if `apply_movement`'s
`if new_qty < 0` guard were deleted, that test's
`pytest.raises(InsufficientStockError)` would fail immediately.
"""

import pytest

from app.errors import ApiError
from app.extensions import db
from app.models import StockMovement
from app.services.stock import InsufficientStockError, apply_movement
from conftest import make_part, make_user


def test_adjustment_updates_qty_and_writes_ledger_row_with_user_attribution():
    """A positive adjustment raises qty_on_hand and writes one ledger row."""
    user = make_user("stock_adj_user", "operator")
    part = make_part(qty_on_hand=10)

    movement = apply_movement(part.id, 5, "adjustment", user.id, note="cycle count")

    assert part.qty_on_hand == 15
    assert movement.part_id == part.id
    assert movement.qty_delta == 5
    assert movement.reason == "adjustment"
    assert movement.user_id == user.id
    assert movement.note == "cycle count"

    ledger_rows = db.session.query(StockMovement).filter_by(part_id=part.id).all()
    assert len(ledger_rows) == 1
    assert ledger_rows[0].id == movement.id
    assert ledger_rows[0].user_id == user.id


def test_two_adjustments_leave_correct_qty_and_two_ledger_rows():
    """03-inventory.md's headline acceptance example: +100 then -30 -> qty 70, 2 rows."""
    user = make_user("stock_two_adj_user", "operator")
    part = make_part(qty_on_hand=0)

    apply_movement(part.id, 100, "adjustment", user.id, note="opening count")
    apply_movement(part.id, -30, "adjustment", user.id, note="shrinkage")

    assert part.qty_on_hand == 70
    assert db.session.query(StockMovement).filter_by(part_id=part.id).count() == 2


def test_negative_result_movement_raises_and_changes_nothing():
    """A movement that would drive qty_on_hand negative raises and touches nothing."""
    user = make_user("stock_neg_user", "operator")
    part = make_part(qty_on_hand=5)

    with pytest.raises(InsufficientStockError) as exc_info:
        apply_movement(part.id, -10, "adjustment", user.id, note="too much")

    assert exc_info.value.part.id == part.id
    assert exc_info.value.required == 10
    assert exc_info.value.on_hand == 5

    # Neither the part's running balance nor the ledger changed.
    assert part.qty_on_hand == 5
    assert db.session.query(StockMovement).filter_by(part_id=part.id).count() == 0


def test_zero_delta_rejected_with_400_validation_error():
    """qty_delta == 0 is a caller bug, rejected before any row is touched."""
    user = make_user("stock_zero_user", "operator")
    part = make_part(qty_on_hand=10)

    with pytest.raises(ApiError) as exc_info:
        apply_movement(part.id, 0, "adjustment", user.id)

    assert exc_info.value.status == 400
    assert exc_info.value.code == "validation_error"
    assert "qty_delta" in (exc_info.value.field_errors or {})

    assert part.qty_on_hand == 10
    assert db.session.query(StockMovement).filter_by(part_id=part.id).count() == 0
