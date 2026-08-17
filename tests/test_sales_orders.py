"""Tests for `app/api/sales_orders.py` (07-sales-orders.md): customers + the SO lifecycle.

10-testing.md's Sales scope: "confirm -> ship decrements stock; shortfall
-> 409, nothing moves" and "raw part on an SO line -> 400" are the two
headline acceptance examples. The rest of this module mirrors, on the
sales side, the same shapes test_purchasing.py already checks on the
purchasing side (replace-all PUT semantics gated by status, a
deactivation blocked by an open document, invalid-transition 409s) plus
one behavior that has no purchasing analogue: an SO line's live
`on_hand`/`short` fields, which read straight off `qty_on_hand` at
request time rather than a cached value.

**DetachedInstanceError guidance:** ids used after an HTTP call are
captured as plain `int`s (or read off a JSON response body) rather than
kept on a pre-request ORM object — see test_inventory.py's module
docstring for the full explanation of why `teardown_request` makes that
necessary.

Every scenario below drives the lifecycle through the HTTP API (create ->
confirm -> ship/cancel) rather than seeding an SO directly at a target
status, since 07-sales-orders.md's state machine is exactly what's under
test.
"""

from app.extensions import db
from app.models import Part, StockMovement
from conftest import make_customer, make_part


def test_create_two_line_so_confirm_ship_decrements_stock_and_writes_ledger(admin_client):
    """07-sales-orders.md's headline acceptance example: create a 2-line
    SO against stocked finished parts, confirm it, ship it. Both parts'
    `qty_on_hand` decrease by their line qty, and exactly two `so_ship`
    ledger rows reference the SO.
    """
    part_a_id = make_part(part_type="finished", qty_on_hand=20).id
    part_b_id = make_part(part_type="finished", qty_on_hand=15).id
    customer_id = make_customer().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/sales-orders",
        json={
            "customer_id": customer_id,
            "notes": "test so",
            "lines": [
                {"part_id": part_a_id, "qty": 5, "unit_price": 10},
                {"part_id": part_b_id, "qty": 3, "unit_price": 20},
            ],
        },
    )
    assert create_resp.status_code == 201
    so_id = create_resp.get_json()["id"]
    assert create_resp.get_json()["status"] == "draft"

    confirm_resp = admin_client.post(f"/api/sales-orders/{so_id}/confirm")
    assert confirm_resp.status_code == 200
    assert confirm_resp.get_json()["status"] == "confirmed"

    ship_resp = admin_client.post(f"/api/sales-orders/{so_id}/ship")
    assert ship_resp.status_code == 200
    assert ship_resp.get_json()["status"] == "shipped"

    assert float(db.session.get(Part, part_a_id).qty_on_hand) == 15
    assert float(db.session.get(Part, part_b_id).qty_on_hand) == 12

    movements = (
        db.session.query(StockMovement).filter_by(ref_type="sales_order", ref_id=so_id).all()
    )
    assert len(movements) == 2
    assert {m.reason for m in movements} == {"so_ship"}
    deltas_by_part = {m.part_id: m.qty_delta for m in movements}
    assert deltas_by_part[part_a_id] == -5
    assert deltas_by_part[part_b_id] == -3


def test_ship_with_one_line_short_returns_409_and_nothing_moves(admin_client):
    """07-sales-orders.md: shipping fails atomically if any line is short
    — the 409's `details` names the short line, and neither part's
    `qty_on_hand` nor the ledger's total row count changes, even for the
    line that *did* have enough stock.
    """
    short_part_id = make_part(part_type="finished", qty_on_hand=2).id  # needs 5, short 3
    ok_part_id = make_part(part_type="finished", qty_on_hand=10).id
    customer_id = make_customer().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/sales-orders",
        json={
            "customer_id": customer_id,
            "lines": [
                {"part_id": short_part_id, "qty": 5, "unit_price": 10},
                {"part_id": ok_part_id, "qty": 3, "unit_price": 5},
            ],
        },
    )
    so_id = create_resp.get_json()["id"]
    confirm_resp = admin_client.post(f"/api/sales-orders/{so_id}/confirm")
    assert confirm_resp.status_code == 200

    movements_before = db.session.query(StockMovement).count()

    ship_resp = admin_client.post(f"/api/sales-orders/{so_id}/ship")
    assert ship_resp.status_code == 409
    body = ship_resp.get_json()
    assert body["error"]["code"] == "insufficient_stock"
    details = body["error"]["details"]
    assert len(details) == 1
    assert details[0]["part_id"] == short_part_id
    assert details[0]["required"] == 5
    assert details[0]["on_hand"] == 2
    assert details[0]["short"] == 3

    assert float(db.session.get(Part, short_part_id).qty_on_hand) == 2
    assert float(db.session.get(Part, ok_part_id).qty_on_hand) == 10
    assert db.session.query(StockMovement).count() == movements_before


def test_raw_part_on_line_returns_400_naming_the_line(admin_client):
    """07-sales-orders.md: "the factory doesn't sell raw stock" — a line
    naming a `raw` part is rejected with a 400 whose `details` name the
    offending line index, not a generic "invalid lines" message.
    """
    raw_part_id = make_part(part_type="raw").id
    customer_id = make_customer().id
    db.session.commit()

    resp = admin_client.post(
        "/api/sales-orders",
        json={
            "customer_id": customer_id,
            "lines": [{"part_id": raw_part_id, "qty": 1, "unit_price": 1}],
        },
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "validation_error"
    details = body["error"]["details"]
    assert len(details) == 1
    assert details[0]["line"] == 0
    assert details[0]["field"] == "part_id"


def test_ship_on_draft_edit_after_confirm_and_cancel_after_ship_are_all_409(admin_client):
    """07-sales-orders.md's three status-guard edges in one walk: `ship`
    requires `confirmed` (a `draft` order can't be shipped), `PUT`
    requires `draft` (a `confirmed` order is frozen), and `cancel` is not
    legal from `shipped` (no "un-shipping" a completed transaction).
    """
    part_id = make_part(part_type="finished", qty_on_hand=10).id
    customer_id = make_customer().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/sales-orders",
        json={
            "customer_id": customer_id,
            "lines": [{"part_id": part_id, "qty": 2, "unit_price": 5}],
        },
    )
    so_id = create_resp.get_json()["id"]

    draft_ship_resp = admin_client.post(f"/api/sales-orders/{so_id}/ship")
    assert draft_ship_resp.status_code == 409
    assert draft_ship_resp.get_json()["error"]["code"] == "invalid_transition"

    confirm_resp = admin_client.post(f"/api/sales-orders/{so_id}/confirm")
    assert confirm_resp.status_code == 200

    edit_resp = admin_client.put(
        f"/api/sales-orders/{so_id}",
        json={
            "customer_id": customer_id,
            "lines": [{"part_id": part_id, "qty": 1, "unit_price": 5}],
        },
    )
    assert edit_resp.status_code == 409
    assert edit_resp.get_json()["error"]["code"] == "invalid_transition"

    ship_resp = admin_client.post(f"/api/sales-orders/{so_id}/ship")
    assert ship_resp.status_code == 200

    cancel_resp = admin_client.post(f"/api/sales-orders/{so_id}/cancel")
    assert cancel_resp.status_code == 409
    assert cancel_resp.get_json()["error"]["code"] == "invalid_transition"


def test_deactivate_customer_blocked_by_confirmed_so_then_succeeds_after_cancel(admin_client):
    """07-sales-orders.md: deactivating a customer with a `confirmed` SO
    is 409 `conflict` (an open commitment is still in flight); once that
    SO is canceled (closed business), deactivation succeeds — mirrors
    test_purchasing.py's supplier/PO version of the same rule.
    """
    part_id = make_part(part_type="finished", qty_on_hand=10).id
    customer_id = make_customer().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/sales-orders",
        json={
            "customer_id": customer_id,
            "lines": [{"part_id": part_id, "qty": 1, "unit_price": 5}],
        },
    )
    so_id = create_resp.get_json()["id"]
    confirm_resp = admin_client.post(f"/api/sales-orders/{so_id}/confirm")
    assert confirm_resp.status_code == 200

    blocked_resp = admin_client.delete(f"/api/customers/{customer_id}")
    assert blocked_resp.status_code == 409
    assert blocked_resp.get_json()["error"]["code"] == "conflict"

    cancel_resp = admin_client.post(f"/api/sales-orders/{so_id}/cancel")
    assert cancel_resp.status_code == 200

    deactivate_resp = admin_client.delete(f"/api/customers/{customer_id}")
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.get_json()["active"] is False


def test_so_detail_lines_show_live_on_hand_and_short_that_flip_after_stock_changes(admin_client):
    """07-sales-orders.md: an SO line's `on_hand`/`short` are computed
    live off the part's current `qty_on_hand`, not cached at order-create
    time — topping up stock via `POST /api/parts/{id}/adjust` between two
    `GET`s on the same SO must flip `short` from positive to zero without
    any change to the order itself.
    """
    part_id = make_part(part_type="finished", qty_on_hand=3).id
    customer_id = make_customer().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/sales-orders",
        json={
            "customer_id": customer_id,
            "lines": [{"part_id": part_id, "qty": 10, "unit_price": 5}],
        },
    )
    so_id = create_resp.get_json()["id"]

    short_detail = admin_client.get(f"/api/sales-orders/{so_id}").get_json()
    short_line = short_detail["lines"][0]
    assert short_line["on_hand"] == 3
    assert short_line["short"] == 7

    adjust_resp = admin_client.post(
        f"/api/parts/{part_id}/adjust", json={"qty_delta": 7, "note": "top up"}
    )
    assert adjust_resp.status_code == 200

    ready_detail = admin_client.get(f"/api/sales-orders/{so_id}").get_json()
    ready_line = ready_detail["lines"][0]
    assert ready_line["on_hand"] == 10
    assert ready_line["short"] == 0
