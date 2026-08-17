"""Tests for `app/api/dashboard.py` (08-dashboard.md): the landing-page aggregate.

08-dashboard.md's whole point is "the page renders from a single API
call" — one `GET /api/dashboard` that assembles four independent
aggregates (status counts, low stock, open work orders, recent activity)
that would otherwise need four separate round trips. 10-testing.md's
scope for this area is narrow: "seeded fixture state produces correct
counts and low-stock membership (boundary: qty == reorder_point is low)".
The two tests below cover that plus the shapes the frontend's empty
states (index.html) depend on not 500ing when there is nothing to show.

`app/api/dashboard.py` was being written by a parallel agent per its own
task brief (see AGENTS.md) at the time this file was drafted; by the time
these tests actually run it has landed and its blueprint is registered in
`app/__init__.py`.

**DetachedInstanceError guidance (see test_inventory.py's module
docstring):** `app/__init__.py`'s `teardown_request` handler really
commits/rolls back and calls `db.session.remove()` after *every* request
the Flask test client makes, which detaches whatever ORM objects the
`make_*` factories handed back before that request. Every id/number/sku
this file needs after the `GET /api/dashboard` call is captured into a
plain variable immediately after creation, before that call, rather than
read off a stale post-request object.
"""

from app.extensions import db
from app.services.stock import apply_movement
from conftest import make_customer, make_part, make_po, make_so, make_supplier, make_user, make_wo


def test_dashboard_known_state_produces_exact_counts_low_stock_and_recent_activity(admin_client):
    """08-dashboard.md's headline shape, end to end, from one arranged database state.

    Arranges: five parts covering the low-stock boundary (below, exactly
    *at* the reorder point — which must still count as low per
    08-dashboard.md's `qty_on_hand <= reorder_point` — above, and an
    inactive part that would be low if active but must be excluded); one
    draft and one released work order; one ordered purchase order; one
    confirmed sales order; and four stock movements (one plain adjustment,
    one referencing each document type) to exercise `recent_movements`'
    `ref_number` resolution and shape.

    Then asserts the `counts` block exactly, `low_stock` membership +
    shortfall-descending ordering (including the boundary row), and
    `open_work_orders`/`recent_movements` ordering + row shape.
    """
    creator = make_user("dash_creator", "admin")
    actor = make_user("dash_actor", "operator")
    db.session.commit()

    # --- low_stock: below, boundary (==), above, and inactive-but-below
    # (must not appear despite qty_on_hand <= reorder_point). A fifth,
    # `very_low`, gives a bigger shortfall than `low` so ordering-by-
    # shortfall-descending is actually exercised (two distinct, unequal
    # shortfalls to order) rather than just checked at a single value.
    very_low = make_part(qty_on_hand=0, reorder_point=50)  # shortfall 50
    low = make_part(qty_on_hand=5, reorder_point=10)  # shortfall 5
    boundary = make_part(qty_on_hand=10, reorder_point=10)  # shortfall 0 -- the boundary case
    above = make_part(qty_on_hand=20, reorder_point=10)  # not low: 20 > 10
    inactive_low = make_part(qty_on_hand=1, reorder_point=100, active=False)
    very_low_id, low_id, boundary_id = very_low.id, low.id, boundary.id
    above_id, inactive_low_id = above.id, inactive_low.id

    # --- open_work_orders: a product that is deliberately NOT low stock
    # itself (qty 100 > reorder 0), so it can't be confused with the
    # low_stock rows above, plus a draft and a released WO against it.
    product = make_part(part_type="finished", qty_on_hand=100, reorder_point=0)
    product_sku, product_name = product.sku, product.name
    wo_draft = make_wo(product, 3, creator, status="draft")
    wo_released = make_wo(product, 7, creator, status="released")
    wo_draft_id, wo_released_id = wo_draft.id, wo_released.id
    wo_released_number = wo_released.wo_number

    # --- counts: one ordered PO, one confirmed SO.
    supplier = make_supplier()
    po = make_po(supplier, creator, status="ordered")
    customer = make_customer()
    so = make_so(customer, creator, status="confirmed")
    po_number, so_number = po.po_number, so.so_number
    po_id, so_id = po.id, so.id

    # --- recent_movements: a dedicated part (kept out of the low_stock
    # arrangement above so its final qty_on_hand doesn't matter to those
    # assertions), moved four times through the real stock service so
    # each movement carries real attribution/ref data to assert on.
    movement_part = make_part(qty_on_hand=0, reorder_point=0)
    movement_part_sku, movement_part_name = movement_part.sku, movement_part.name
    db.session.commit()

    m1 = apply_movement(movement_part.id, 50, "adjustment", actor.id, note="initial load")
    m2 = apply_movement(
        movement_part.id, -20, "wo_consume", actor.id,
        ref_type="work_order", ref_id=wo_released_id,
    )
    m3 = apply_movement(
        movement_part.id, 15, "po_receive", actor.id,
        ref_type="purchase_order", ref_id=po_id,
    )
    m4 = apply_movement(
        movement_part.id, -5, "so_ship", actor.id,
        ref_type="sales_order", ref_id=so_id,
    )
    m1_id, m2_id, m3_id, m4_id = m1.id, m2.id, m3.id, m4.id
    db.session.commit()

    resp = admin_client.get("/api/dashboard")
    assert resp.status_code == 200
    body = resp.get_json()

    # --- counts: exactly the arranged draft/released/ordered/confirmed
    # rows -- no bleed from `completed`/`received`/`shipped`/`canceled`,
    # none of which exist here, and none from `dash_creator`'s own
    # `admin` role or `fixture_admin`'s login.
    assert body["counts"] == {
        "work_orders": {"draft": 1, "released": 1},
        "purchase_orders": {"draft": 0, "ordered": 1},
        "sales_orders": {"draft": 0, "confirmed": 1},
        "low_stock_parts": 3,
    }

    # --- low_stock: shortfall descending (very_low=50, low=5, boundary=0),
    # boundary counted as low, above/inactive excluded entirely.
    low_stock = body["low_stock"]
    assert [row["id"] for row in low_stock] == [very_low_id, low_id, boundary_id]
    assert [row["shortfall"] for row in low_stock] == [50.0, 5.0, 0.0]
    low_stock_ids = {row["id"] for row in low_stock}
    assert above_id not in low_stock_ids
    assert inactive_low_id not in low_stock_ids
    boundary_row = low_stock[2]
    assert boundary_row["qty_on_hand"] == 10.0
    assert boundary_row["reorder_point"] == 10.0

    # --- open_work_orders: newest (released, created second) first, both
    # rows present, correct product/qty/status.
    open_wos = body["open_work_orders"]
    assert [row["id"] for row in open_wos] == [wo_released_id, wo_draft_id]
    assert open_wos[0]["status"] == "released"
    assert open_wos[0]["qty"] == 7.0
    assert open_wos[0]["product_sku"] == product_sku
    assert open_wos[0]["product_name"] == product_name
    assert open_wos[1]["status"] == "draft"
    assert open_wos[1]["qty"] == 3.0

    # --- recent_movements: newest first (m4, m3, m2, m1), each row's
    # shape/ref_number resolved per document type, plain adjustment has
    # no ref_number.
    recent = body["recent_movements"]
    assert [row["id"] for row in recent] == [m4_id, m3_id, m2_id, m1_id]
    assert all(row["sku"] == movement_part_sku for row in recent)
    assert all(row["part_name"] == movement_part_name for row in recent)
    assert all(row["username"] == "dash_actor" for row in recent)

    assert recent[0]["reason"] == "so_ship"
    assert recent[0]["qty_delta"] == -5.0
    assert recent[0]["ref_number"] == so_number

    assert recent[1]["reason"] == "po_receive"
    assert recent[1]["qty_delta"] == 15.0
    assert recent[1]["ref_number"] == po_number

    assert recent[2]["reason"] == "wo_consume"
    assert recent[2]["qty_delta"] == -20.0
    assert recent[2]["ref_number"] == wo_released_number

    assert recent[3]["reason"] == "adjustment"
    assert recent[3]["qty_delta"] == 50.0
    assert recent[3]["ref_number"] is None


def test_dashboard_on_empty_database_returns_zero_counts_and_empty_lists(admin_client):
    """08-dashboard.md / 10-testing.md: an empty database must not 500.

    `_isolated_db` (conftest.py) truncates every table between tests, so
    this test starts from a database with nothing in it except the one
    `User` row `admin_client`'s own fixture creates to log in -- no
    parts, documents, or movements at all. index.html's per-tile/table
    empty states (08-dashboard.md: "Nothing below reorder point") depend
    on every count being `0` and every list being `[]` rather than the
    endpoint omitting keys or erroring on an empty aggregate query.
    """
    resp = admin_client.get("/api/dashboard")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["counts"] == {
        "work_orders": {"draft": 0, "released": 0},
        "purchase_orders": {"draft": 0, "ordered": 0},
        "sales_orders": {"draft": 0, "confirmed": 0},
        "low_stock_parts": 0,
    }
    assert body["low_stock"] == []
    assert body["open_work_orders"] == []
    assert body["recent_movements"] == []
