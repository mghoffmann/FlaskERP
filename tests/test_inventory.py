"""Tests for `app/api/inventory.py` (03-inventory.md): the parts catalog + stock adjustments.

10-testing.md's philosophy for this area is "representative, not
exhaustive CRUD" — the stock-service math itself (qty deltas, ledger
rows, the negative-balance guard) is already covered in-process by
`test_stock_service.py`; what's new here is the HTTP layer on top of it:
request/response shapes, role enforcement, and the two document-shaped
behaviors (duplicate SKU, deactivate-blocked-by-open-document) that only
exist at the API layer.

**Why every test below captures ids into plain variables before the
first HTTP call, instead of reading `some_part.id` off the ORM object
whenever it's needed:** `app/__init__.py`'s `teardown_request` handler
calls `db.session.remove()` after *every* request the Flask test client
makes (see conftest.py's module docstring — that handler really commits
or rolls back each request's transaction, same as production). Removing
the scoped session detaches whatever ORM objects `make_part()`/`make_wo()`
handed back before that request; touching an attribute on a detached,
expired instance afterward raises `DetachedInstanceError` rather than
silently refetching. Reading `.id`/`.sku` once, right after creation,
sidesteps this entirely — plain ints/strings have no session to be
detached from.
"""

from app.extensions import db
from app.models import User, WorkOrder
from conftest import make_part, make_wo


def test_create_part_as_admin_returns_201_with_full_shape(admin_client):
    """03-inventory.md: POST /api/parts is admin-only and returns the part shape.

    `qty_on_hand` is deliberately not part of the request (new parts start
    at 0 — the doc is explicit that opening stock is loaded via an
    adjustment, not the create call), so this also doubles as a check that
    the response reflects that default rather than echoing back whatever
    the client sent. A fresh part with `qty_on_hand=0` and `reorder_point=10`
    is, by definition (`qty_on_hand <= reorder_point`), low stock.
    """
    resp = admin_client.post(
        "/api/parts",
        json={
            "sku": "RAW-WIDGET-01",
            "name": "Test widget",
            "part_type": "raw",
            "unit": "ea",
            "reorder_point": 10,
            "unit_cost": 2.5,
        },
    )

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["sku"] == "RAW-WIDGET-01"
    assert body["name"] == "Test widget"
    assert body["part_type"] == "raw"
    assert body["unit"] == "ea"
    assert float(body["qty_on_hand"]) == 0
    assert float(body["reorder_point"]) == 10
    assert float(body["unit_cost"]) == 2.5
    assert body["active"] is True
    assert body["low_stock"] is True


def test_create_part_duplicate_sku_returns_400_with_field_errors(admin_client):
    """03-inventory.md: duplicate SKU is a 400 `validation_error`, not a 409/500 from the DB's UNIQUE constraint leaking through."""
    make_part(sku="DUP-SKU-01")
    db.session.commit()

    resp = admin_client.post(
        "/api/parts",
        json={"sku": "DUP-SKU-01", "name": "Another widget", "part_type": "raw"},
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "validation_error"
    assert "sku" in body["error"]["field_errors"]


def test_list_parts_low_stock_filter_and_active_default(admin_client):
    """One representative list-filter test covering both `low_stock=true` and the active/all toggle (10-testing.md: skip exhaustive CRUD/filter combinatorics).

    Boundary case per 03-inventory.md ("qty_on_hand <= reorder_point"):
    a part sitting exactly *at* its reorder point counts as low stock.
    """
    low_id = make_part(qty_on_hand=5, reorder_point=10).id
    boundary_id = make_part(qty_on_hand=10, reorder_point=10).id
    plenty_id = make_part(qty_on_hand=20, reorder_point=10).id
    inactive_low_id = make_part(qty_on_hand=1, reorder_point=100, active=False).id
    db.session.commit()

    low_stock_resp = admin_client.get("/api/parts?low_stock=true")
    assert low_stock_resp.status_code == 200
    low_stock_ids = {item["id"] for item in low_stock_resp.get_json()["items"]}
    assert low_stock_ids == {low_id, boundary_id}

    default_resp = admin_client.get("/api/parts")
    default_ids = {item["id"] for item in default_resp.get_json()["items"]}
    assert inactive_low_id not in default_ids
    assert {low_id, boundary_id, plenty_id} <= default_ids

    all_resp = admin_client.get("/api/parts?active=all")
    all_ids = {item["id"] for item in all_resp.get_json()["items"]}
    assert inactive_low_id in all_ids


def test_adjust_twice_updates_qty_and_writes_attributed_ledger_rows(admin_client):
    """03-inventory.md's headline acceptance example, via HTTP: +100 then -30 -> qty 70, two ledger rows attributed to the acting user, read back through GET /api/parts/{id}/movements."""
    part_id = make_part(qty_on_hand=0).id
    db.session.commit()

    first_resp = admin_client.post(
        f"/api/parts/{part_id}/adjust", json={"qty_delta": 100, "note": "opening count"}
    )
    assert first_resp.status_code == 200
    assert float(first_resp.get_json()["qty_on_hand"]) == 100

    second_resp = admin_client.post(
        f"/api/parts/{part_id}/adjust", json={"qty_delta": -30, "note": "shrinkage"}
    )
    assert second_resp.status_code == 200
    assert float(second_resp.get_json()["qty_on_hand"]) == 70

    movements_resp = admin_client.get(f"/api/parts/{part_id}/movements")
    assert movements_resp.status_code == 200
    movements_body = movements_resp.get_json()
    assert movements_body["total"] == 2
    assert len(movements_body["items"]) == 2
    assert {float(item["qty_delta"]) for item in movements_body["items"]} == {100.0, -30.0}
    assert all(item["username"] == "fixture_admin" for item in movements_body["items"])


def test_adjust_insufficient_stock_returns_409_and_changes_nothing(admin_client):
    """03-inventory.md: an adjustment that would drive stock negative is 409 `insufficient_stock` and leaves qty/ledger untouched."""
    part_id = make_part(qty_on_hand=70).id
    db.session.commit()

    resp = admin_client.post(
        f"/api/parts/{part_id}/adjust", json={"qty_delta": -100, "note": "big miscount"}
    )

    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "insufficient_stock"

    get_resp = admin_client.get(f"/api/parts/{part_id}")
    assert float(get_resp.get_json()["qty_on_hand"]) == 70

    movements_resp = admin_client.get(f"/api/parts/{part_id}/movements")
    assert movements_resp.get_json()["total"] == 0


def test_adjust_blank_or_missing_note_returns_400(admin_client):
    """03-inventory.md: `note` is required ("adjustments without a reason are an ERP smell") — both an absent key and a whitespace-only string must be rejected."""
    part_id = make_part(qty_on_hand=10).id
    db.session.commit()

    missing_note_resp = admin_client.post(
        f"/api/parts/{part_id}/adjust", json={"qty_delta": 5}
    )
    assert missing_note_resp.status_code == 400
    assert missing_note_resp.get_json()["error"]["code"] == "validation_error"

    blank_note_resp = admin_client.post(
        f"/api/parts/{part_id}/adjust", json={"qty_delta": 5, "note": "   "}
    )
    assert blank_note_resp.status_code == 400
    assert blank_note_resp.get_json()["error"]["code"] == "validation_error"


def test_operator_can_adjust_stock_but_cannot_create_part(operator_client):
    """03-inventory.md's role pair, spelled out explicitly (the parametrized sweep in test_auth.py also covers the 403 half generically): an operator may adjust stock (any role) but is forbidden from POST /api/parts (admin only)."""
    part_id = make_part(qty_on_hand=10).id
    db.session.commit()

    adjust_resp = operator_client.post(
        f"/api/parts/{part_id}/adjust", json={"qty_delta": 5, "note": "cycle count"}
    )
    assert adjust_resp.status_code == 200

    create_resp = operator_client.post(
        "/api/parts", json={"sku": "OP-CREATE-01", "name": "nope", "part_type": "raw"}
    )
    assert create_resp.status_code == 403
    assert create_resp.get_json()["error"]["code"] == "forbidden"


def test_deactivate_blocked_by_open_document_then_succeeds_after_it_closes(admin_client):
    """03-inventory.md: DELETE (soft-deactivate) is 409 `conflict` while the part is on an open document (here: a `released` work order as the product), succeeds once that document leaves the open state, and the part remains reachable by id (just hidden from the default list) afterward."""
    admin_user = db.session.query(User).filter_by(username="fixture_admin").first()
    product = make_part(part_type="finished", qty_on_hand=0)
    wo = make_wo(product, 5, admin_user, status="released")
    product_id, wo_id = product.id, wo.id
    db.session.commit()

    blocked_resp = admin_client.delete(f"/api/parts/{product_id}")
    assert blocked_resp.status_code == 409
    assert blocked_resp.get_json()["error"]["code"] == "conflict"

    wo_row = db.session.get(WorkOrder, wo_id)
    wo_row.status = "canceled"
    db.session.commit()

    deactivate_resp = admin_client.delete(f"/api/parts/{product_id}")
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.get_json()["active"] is False

    list_resp = admin_client.get("/api/parts")
    assert product_id not in {item["id"] for item in list_resp.get_json()["items"]}

    get_resp = admin_client.get(f"/api/parts/{product_id}")
    assert get_resp.status_code == 200


def test_put_part_cannot_change_part_type_or_qty_on_hand(admin_client):
    """03-inventory.md: `part_type` and `qty_on_hand` are explicitly "Not editable" via PUT -> 400 `validation_error`, even alongside a legitimate editable field in the same request body."""
    part_id = make_part(part_type="raw", qty_on_hand=10).id
    db.session.commit()

    resp = admin_client.put(
        f"/api/parts/{part_id}",
        json={"name": "Renamed part", "part_type": "finished", "qty_on_hand": 999},
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "validation_error"
