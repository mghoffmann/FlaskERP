"""Tests for `app/api/purchasing.py` (06-purchasing.md): suppliers + the PO lifecycle.

10-testing.md's Purchasing scope is narrow and specific: "place -> receive
increments stock, writes ledger, applies last-cost update" and "receive
from draft / double-receive -> 409, stock moved once" are the two
headline acceptance examples; everything else here (replace-all PUT
semantics, supplier deactivation guarded by an open PO, line-level
validation, the operator/admin role split on `receive`) mirrors patterns
already exercised in test_inventory.py/test_work_orders.py for their own
document types, so is covered with one representative test each rather
than exhaustively.

**DetachedInstanceError guidance (see test_inventory.py's module
docstring):** every id this file needs after an HTTP call is captured
into a plain `int` (or read straight off a JSON response body) rather
than kept as a live attribute on a pre-request ORM object, since
`app/__init__.py`'s `teardown_request` detaches whatever `make_part()`/
`make_supplier()` handed back the moment that request's transaction
commits.

Most scenarios below drive the whole lifecycle through the HTTP API
(create -> place -> receive/cancel) rather than seeding a PO directly at
a target status via `make_po(..., status=...)` — 06-purchasing.md's
state machine is exactly what's under test, so exercising the real
transitions is more representative than teleporting into the middle of
it.
"""

from app.extensions import db
from app.models import Part, POLine, StockMovement
from conftest import make_part, make_supplier, make_user


def test_create_two_line_po_place_receive_moves_stock_and_applies_last_cost(admin_client):
    """06-purchasing.md's headline acceptance example: create a 2-line PO,
    place it, receive it. Both parts' `qty_on_hand` increase by their line
    qty, exactly two `po_receive` ledger rows reference the PO, and the
    "last cost" policy applies `unit_cost` only for the line whose
    `unit_cost` was > 0 (a line's `unit_cost == 0` is "not provided" and
    leaves the part's existing cost alone).
    """
    comp_a_id = make_part(qty_on_hand=0, unit_cost=5).id
    comp_b_id = make_part(qty_on_hand=0, unit_cost=3).id
    supplier_id = make_supplier().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/purchase-orders",
        json={
            "supplier_id": supplier_id,
            "notes": "test po",
            "lines": [
                {"part_id": comp_a_id, "qty": 10, "unit_cost": 2.5},
                {"part_id": comp_b_id, "qty": 5, "unit_cost": 0},
            ],
        },
    )
    assert create_resp.status_code == 201
    po_id = create_resp.get_json()["id"]
    assert create_resp.get_json()["status"] == "draft"

    place_resp = admin_client.post(f"/api/purchase-orders/{po_id}/place")
    assert place_resp.status_code == 200
    assert place_resp.get_json()["status"] == "ordered"

    receive_resp = admin_client.post(f"/api/purchase-orders/{po_id}/receive")
    assert receive_resp.status_code == 200
    assert receive_resp.get_json()["status"] == "received"

    comp_a = db.session.get(Part, comp_a_id)
    comp_b = db.session.get(Part, comp_b_id)
    assert float(comp_a.qty_on_hand) == 10
    assert float(comp_b.qty_on_hand) == 5
    assert float(comp_a.unit_cost) == 2.5  # last-cost applied: line unit_cost > 0
    assert float(comp_b.unit_cost) == 3  # unchanged: line unit_cost == 0 means "not provided"

    movements = (
        db.session.query(StockMovement)
        .filter_by(ref_type="purchase_order", ref_id=po_id)
        .all()
    )
    assert len(movements) == 2
    assert {m.reason for m in movements} == {"po_receive"}
    assert {m.part_id for m in movements} == {comp_a_id, comp_b_id}


def test_receive_from_draft_and_double_receive_are_409_stock_moves_once(admin_client):
    """06-purchasing.md: receiving a draft PO is 409 (there is nothing to
    receive until it's `ordered`); receiving an already-`received` PO a
    second time is also 409, and neither failed attempt moves stock a
    second time — checked via the ledger row count, not just the status
    code, the same "assert the count, not just the code" discipline
    test_work_orders.py's double-complete test uses.
    """
    part_id = make_part(qty_on_hand=0).id
    supplier_id = make_supplier().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/purchase-orders",
        json={
            "supplier_id": supplier_id,
            "lines": [{"part_id": part_id, "qty": 4, "unit_cost": 1}],
        },
    )
    po_id = create_resp.get_json()["id"]

    draft_receive_resp = admin_client.post(f"/api/purchase-orders/{po_id}/receive")
    assert draft_receive_resp.status_code == 409
    assert draft_receive_resp.get_json()["error"]["code"] == "invalid_transition"

    place_resp = admin_client.post(f"/api/purchase-orders/{po_id}/place")
    assert place_resp.status_code == 200

    first_receive_resp = admin_client.post(f"/api/purchase-orders/{po_id}/receive")
    assert first_receive_resp.status_code == 200

    movement_count = (
        db.session.query(StockMovement)
        .filter_by(ref_type="purchase_order", ref_id=po_id)
        .count()
    )
    assert movement_count == 1
    assert float(db.session.get(Part, part_id).qty_on_hand) == 4

    second_receive_resp = admin_client.post(f"/api/purchase-orders/{po_id}/receive")
    assert second_receive_resp.status_code == 409
    assert second_receive_resp.get_json()["error"]["code"] == "invalid_transition"

    assert (
        db.session.query(StockMovement)
        .filter_by(ref_type="purchase_order", ref_id=po_id)
        .count()
        == 1
    )
    assert float(db.session.get(Part, part_id).qty_on_hand) == 4


def test_put_replaces_lines_in_draft_and_is_409_once_ordered(admin_client):
    """06-purchasing.md: `PUT` on a draft PO is a full replace-all of its
    lines (checked here via the resulting row count, not just the response
    body); the same edit on an `ordered` PO is 409 `invalid_transition`,
    since a placed PO represents a commitment already acted on.
    """
    part_a_id = make_part().id
    part_b_id = make_part().id
    part_c_id = make_part().id
    supplier_id = make_supplier().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/purchase-orders",
        json={
            "supplier_id": supplier_id,
            "lines": [
                {"part_id": part_a_id, "qty": 1, "unit_cost": 1},
                {"part_id": part_b_id, "qty": 2, "unit_cost": 1},
            ],
        },
    )
    po_id = create_resp.get_json()["id"]
    assert len(create_resp.get_json()["lines"]) == 2

    put_resp = admin_client.put(
        f"/api/purchase-orders/{po_id}",
        json={
            "supplier_id": supplier_id,
            "lines": [{"part_id": part_c_id, "qty": 3, "unit_cost": 1}],
        },
    )
    assert put_resp.status_code == 200
    put_body = put_resp.get_json()
    assert len(put_body["lines"]) == 1
    assert put_body["lines"][0]["part_id"] == part_c_id
    assert db.session.query(POLine).filter_by(po_id=po_id).count() == 1

    place_resp = admin_client.post(f"/api/purchase-orders/{po_id}/place")
    assert place_resp.status_code == 200

    ordered_put_resp = admin_client.put(
        f"/api/purchase-orders/{po_id}",
        json={
            "supplier_id": supplier_id,
            "lines": [{"part_id": part_c_id, "qty": 1, "unit_cost": 1}],
        },
    )
    assert ordered_put_resp.status_code == 409
    assert ordered_put_resp.get_json()["error"]["code"] == "invalid_transition"


def test_deactivate_supplier_blocked_by_ordered_po_then_succeeds_after_receive(admin_client):
    """06-purchasing.md: deactivating a supplier with an `ordered` PO is
    409 `conflict`; once that PO is received (a closed transaction),
    deactivation succeeds, the supplier drops out of the default
    `GET /api/suppliers` listing, and the now-historical PO's detail page
    still renders (old documents keep pointing at their vendor even after
    it's deactivated).
    """
    part_id = make_part(qty_on_hand=0).id
    supplier_id = make_supplier().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/purchase-orders",
        json={
            "supplier_id": supplier_id,
            "lines": [{"part_id": part_id, "qty": 2, "unit_cost": 1}],
        },
    )
    po_id = create_resp.get_json()["id"]
    place_resp = admin_client.post(f"/api/purchase-orders/{po_id}/place")
    assert place_resp.status_code == 200

    blocked_resp = admin_client.delete(f"/api/suppliers/{supplier_id}")
    assert blocked_resp.status_code == 409
    assert blocked_resp.get_json()["error"]["code"] == "conflict"

    receive_resp = admin_client.post(f"/api/purchase-orders/{po_id}/receive")
    assert receive_resp.status_code == 200

    deactivate_resp = admin_client.delete(f"/api/suppliers/{supplier_id}")
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.get_json()["active"] is False

    list_resp = admin_client.get("/api/suppliers")
    assert supplier_id not in {item["id"] for item in list_resp.get_json()["items"]}

    detail_resp = admin_client.get(f"/api/purchase-orders/{po_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.get_json()["supplier"]["id"] == supplier_id


def test_line_validation_duplicate_part_and_zero_qty_return_400_with_line_indexed_details(
    admin_client,
):
    """06-purchasing.md's PO line rules, both violated in one request so
    the 400's `details` can be checked for naming *both* offending line
    indices (`_validate_lines` in app/api/purchasing.py collects every
    problem rather than stopping at the first): a duplicate `part_id`
    (line 1 repeats line 0's part) and a non-positive `qty` (line 2).
    """
    part_a_id = make_part().id
    part_b_id = make_part().id
    supplier_id = make_supplier().id
    db.session.commit()

    resp = admin_client.post(
        "/api/purchase-orders",
        json={
            "supplier_id": supplier_id,
            "lines": [
                {"part_id": part_a_id, "qty": 5, "unit_cost": 1},
                {"part_id": part_a_id, "qty": 3, "unit_cost": 1},  # duplicate of line 0
                {"part_id": part_b_id, "qty": 0, "unit_cost": 1},  # qty must be > 0
            ],
        },
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "validation_error"
    details_by_line = {d["line"]: d["message"] for d in body["error"]["details"]}
    assert "duplicate" in details_by_line[1].lower()
    assert "greater than 0" in details_by_line[2]


def test_operator_can_receive_ordered_po(admin_client, app):
    """06-purchasing.md: "operator can receive but gets 403 on
    create/place/cancel" — the 403 half is covered generically by
    test_auth.py's sweep; this checks the 200 half explicitly.

    Deliberately opens a second, independent client via `app.test_client()`
    rather than also requesting the `operator_client` fixture: it shares
    the same underlying `client` fixture as `admin_client` (see
    test_work_orders.py's happy-path test for the full explanation), so
    logging in as both roles on one client would just overwrite the first
    session's cookie.
    """
    part_id = make_part(qty_on_hand=0).id
    supplier_id = make_supplier().id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/purchase-orders",
        json={
            "supplier_id": supplier_id,
            "lines": [{"part_id": part_id, "qty": 2, "unit_cost": 1}],
        },
    )
    po_id = create_resp.get_json()["id"]
    place_resp = admin_client.post(f"/api/purchase-orders/{po_id}/place")
    assert place_resp.status_code == 200

    operator_client = app.test_client()
    make_user("fixture_operator_po_receive", "operator", "operatorpw")
    db.session.commit()
    login_resp = operator_client.post(
        "/api/auth/login",
        json={"username": "fixture_operator_po_receive", "password": "operatorpw"},
    )
    assert login_resp.status_code == 200

    receive_resp = operator_client.post(f"/api/purchase-orders/{po_id}/receive")
    assert receive_resp.status_code == 200
    assert receive_resp.get_json()["status"] == "received"
