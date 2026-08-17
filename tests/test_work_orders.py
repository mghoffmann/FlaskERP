"""Tests for `app/api/work_orders.py` (05-work-orders.md): the WO lifecycle.

10-testing.md calls this module "the heart of the suite" — the completion
transaction (consume every BOM component, produce the finished good, all
through the stock service, all-or-nothing) is the single most consequential
piece of logic in the whole demo, so these tests are assertion-dense on
exact stock deltas and ledger rows rather than just status codes.

`app/api/work_orders.py` is being written by a parallel agent per its own
task brief (see AGENTS.md) — until it lands and its blueprint is
registered in `app/__init__.py`, every test below that hits
`/api/work-orders/*` fails/errors with 404 (an unmatched route), not the
status codes asserted below. That is expected and does not indicate a bug
in this file; re-run this module once work orders land.

**DetachedInstanceError guidance (see test_inventory.py's module
docstring):** `app/__init__.py`'s `teardown_request` handler really
commits/rolls back and calls `db.session.remove()` after *every* request
the Flask test client makes, which detaches whatever ORM objects
`make_part()`/`make_bom()`/`make_wo()` handed back before that request.
Every test below captures ids into plain `int` variables immediately after
creation and re-fetches fresh model instances (`db.session.get(...)`)
whenever it needs to inspect state *after* an HTTP call, rather than
touching a stale pre-request object.

**`make_part(qty_on_hand=...)` bypasses the ledger.** The factory sets
`Part.qty_on_hand` directly on the model, skipping `apply_movement()`
(the only code AGENTS.md permits to write that column in the *app*, not
in test fixtures) entirely — no `StockMovement` row is written for that
opening balance. That is fine for arranging a test's starting stock
level (and lets shortfall tests start from a total ledger row count of
zero, which matters for the "database totally unchanged" assertions
below); it would not be fine as a pattern inside `app/` itself.
"""

import itertools

from app.extensions import db
from app.models import Part, StockMovement, User
from app.services.stock import apply_movement
from conftest import make_bom, make_part, make_user, make_wo

#: Backs `_admin_creator`'s auto-generated usernames — mirrors conftest's
#: own `_sku_counter` pattern so parallel/repeated calls within one test
#: never collide on `users.username`'s UNIQUE constraint.
_creator_counter = itertools.count(1)


def _movement_count():
    """Total row count across the whole `stock_movements` ledger — used to assert 'nothing moved'."""
    return db.session.query(StockMovement).count()


def test_happy_path_draft_release_complete_moves_stock_exactly_once(admin_client, app):
    """05-work-orders.md's headline acceptance example, end to end.

    Create (admin) -> release (admin) -> complete (operator, exercising
    "Auth: any" for the complete endpoint per 05-work-orders.md and
    covering the operator-role-allowed case the sweep in test_auth.py
    doesn't). Asserts exact stock deltas on both components and the
    product, plus the three resulting ledger rows: two `wo_consume` (one
    per component) and one `wo_produce`, all `ref_type="work_order"`
    with `ref_id` = the WO id, and (since the operator completed it)
    attributed to the operator user.

    Deliberately does *not* take the ``operator_client`` fixture: it and
    ``admin_client`` both build on the same shared ``client`` fixture
    (see conftest.py), so requesting both in one test would just log the
    *same* client in twice — the second login silently overwrites the
    first's session cookie, leaving both fixtures pointing at whichever
    role logged in last rather than two independently authenticated
    sessions. A second, genuinely separate client from ``app.test_client()``
    avoids that trap and lets this test hold an admin session and an
    operator session at the same time, as the scenario requires.
    """
    operator_client = app.test_client()
    make_user("fixture_operator_happy_path", "operator", "operatorpw")
    db.session.commit()
    operator_login_resp = operator_client.post(
        "/api/auth/login",
        json={"username": "fixture_operator_happy_path", "password": "operatorpw"},
    )
    assert operator_login_resp.status_code == 200

    product_id = make_part(part_type="finished").id
    comp_a_id = make_part(part_type="raw", qty_on_hand=100).id
    comp_b_id = make_part(part_type="raw", qty_on_hand=50).id
    make_bom(
        db.session.get(Part, product_id),
        [(db.session.get(Part, comp_a_id), 3), (db.session.get(Part, comp_b_id), 5)],
    )
    db.session.commit()

    wo_qty = 4
    create_resp = admin_client.post(
        "/api/work-orders",
        json={"product_part_id": product_id, "qty": wo_qty, "notes": "test build"},
    )
    assert create_resp.status_code == 201
    create_body = create_resp.get_json()
    assert create_body["status"] == "draft"
    wo_id = create_body["id"]
    assert create_body["wo_number"] == f"WO-{wo_id:04d}"
    assert create_body["created_by_username"] == "fixture_admin"

    release_resp = admin_client.post(f"/api/work-orders/{wo_id}/release")
    assert release_resp.status_code == 200
    release_body = release_resp.get_json()
    assert release_body["status"] == "released"
    assert release_body["released_at"] is not None

    complete_resp = operator_client.post(f"/api/work-orders/{wo_id}/complete")
    assert complete_resp.status_code == 200
    complete_body = complete_resp.get_json()
    assert complete_body["status"] == "completed"
    assert complete_body["completed_at"] is not None

    comp_a = db.session.get(Part, comp_a_id)
    comp_b = db.session.get(Part, comp_b_id)
    product = db.session.get(Part, product_id)
    assert comp_a.qty_on_hand == 100 - 3 * wo_qty  # 88
    assert comp_b.qty_on_hand == 50 - 5 * wo_qty  # 30
    assert product.qty_on_hand == wo_qty

    movements = (
        db.session.query(StockMovement)
        .filter_by(ref_type="work_order", ref_id=wo_id)
        .all()
    )
    assert len(movements) == 3

    consumes = {m.part_id: m for m in movements if m.reason == "wo_consume"}
    produces = [m for m in movements if m.reason == "wo_produce"]
    assert set(consumes) == {comp_a_id, comp_b_id}
    assert consumes[comp_a_id].qty_delta == -3 * wo_qty
    assert consumes[comp_b_id].qty_delta == -5 * wo_qty
    assert len(produces) == 1
    assert produces[0].part_id == product_id
    assert produces[0].qty_delta == wo_qty

    # The *completing* user's attribution lives on the movements, not the
    # WO row (created_by only ever records who drafted it) — every
    # movement from this completion must be attributed to the operator
    # who actually clicked "Complete build", not the admin who created it.
    completer_ids = {m.user_id for m in movements}
    assert len(completer_ids) == 1
    completer = db.session.get(User, next(iter(completer_ids)))
    assert completer.username == "fixture_operator_happy_path"


def test_complete_shortfall_lists_all_short_components_and_nothing_changes(admin_client):
    """05-work-orders.md: a 409 `insufficient_stock` lists *every* short component, not just the first, and leaves the database totally unchanged.

    Two components are made short (not just one) specifically to assert
    "all" rather than "the first." "Totally unchanged" is checked two
    ways per 10-testing.md: exact part quantities *and* the ledger's
    total row count, both before and after the failed completion.
    """
    product_id = make_part(part_type="finished").id
    comp_a_id = make_part(part_type="raw", qty_on_hand=5).id  # needs 10, short 5
    comp_b_id = make_part(part_type="raw", qty_on_hand=2).id  # needs 8, short 6
    make_bom(
        db.session.get(Part, product_id),
        [(db.session.get(Part, comp_a_id), 10), (db.session.get(Part, comp_b_id), 8)],
    )
    db.session.commit()

    wo_id = make_wo(
        db.session.get(Part, product_id), 1, _admin_creator(), status="released"
    ).id
    db.session.commit()

    movements_before = _movement_count()

    resp = admin_client.post(f"/api/work-orders/{wo_id}/complete")

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"]["code"] == "insufficient_stock"
    details = body["error"]["details"]
    assert len(details) == 2
    by_part = {row["part_id"]: row for row in details}
    assert by_part[comp_a_id]["required"] == 10
    assert by_part[comp_a_id]["on_hand"] == 5
    assert by_part[comp_a_id]["short"] == 5
    assert by_part[comp_b_id]["required"] == 8
    assert by_part[comp_b_id]["on_hand"] == 2
    assert by_part[comp_b_id]["short"] == 6

    assert db.session.get(Part, comp_a_id).qty_on_hand == 5
    assert db.session.get(Part, comp_b_id).qty_on_hand == 2
    assert _movement_count() == movements_before == 0


def _admin_creator():
    """Create and return a throwaway admin user to satisfy `make_wo`'s required `creator` arg.

    Used by tests below that build a WO directly via the factory (rather
    than through `POST /api/work-orders`) because the scenario under test
    starts partway through the lifecycle (already `released`), and the
    creator's identity is irrelevant to what's being asserted.
    """
    return make_user(f"wo_creator_{next(_creator_counter):04d}", "admin")


def test_release_with_empty_bom_returns_400(admin_client):
    """05-work-orders.md: releasing a WO whose product has no BOM lines at all is 400, not a silent no-op release."""
    product_id = make_part(part_type="finished").id
    db.session.commit()

    create_resp = admin_client.post(
        "/api/work-orders", json={"product_part_id": product_id, "qty": 1}
    )
    assert create_resp.status_code == 201
    wo_id = create_resp.get_json()["id"]

    resp = admin_client.post(f"/api/work-orders/{wo_id}/release")

    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "validation_error"


def test_release_with_inactive_bom_component_returns_400(admin_client):
    """05-work-orders.md: an inactive BOM component blocks release even though the BOM isn't empty."""
    product_id = make_part(part_type="finished").id
    inactive_comp_id = make_part(part_type="raw", active=False).id
    make_bom(db.session.get(Part, product_id), [(db.session.get(Part, inactive_comp_id), 1)])
    db.session.commit()

    create_resp = admin_client.post(
        "/api/work-orders", json={"product_part_id": product_id, "qty": 1}
    )
    assert create_resp.status_code == 201
    wo_id = create_resp.get_json()["id"]

    resp = admin_client.post(f"/api/work-orders/{wo_id}/release")

    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "validation_error"


def test_complete_from_draft_is_409_and_double_complete_is_409_with_single_stock_effect(
    admin_client,
):
    """05-work-orders.md: `complete` requires `released`; a draft WO can't be completed, and completing an already-completed WO a second time has no further effect on stock (checked via the ledger row count, not just the status code)."""
    draft_product_id = make_part(part_type="finished").id
    make_bom(db.session.get(Part, draft_product_id), [(make_part(part_type="raw", qty_on_hand=10), 1)])
    db.session.commit()
    draft_create_resp = admin_client.post(
        "/api/work-orders", json={"product_part_id": draft_product_id, "qty": 1}
    )
    draft_wo_id = draft_create_resp.get_json()["id"]

    draft_complete_resp = admin_client.post(f"/api/work-orders/{draft_wo_id}/complete")
    assert draft_complete_resp.status_code == 409
    assert draft_complete_resp.get_json()["error"]["code"] == "invalid_transition"

    product_id = make_part(part_type="finished").id
    comp_id = make_part(part_type="raw", qty_on_hand=10).id
    make_bom(db.session.get(Part, product_id), [(db.session.get(Part, comp_id), 2)])
    db.session.commit()
    create_resp = admin_client.post(
        "/api/work-orders", json={"product_part_id": product_id, "qty": 1}
    )
    wo_id = create_resp.get_json()["id"]
    admin_client.post(f"/api/work-orders/{wo_id}/release")

    first_complete_resp = admin_client.post(f"/api/work-orders/{wo_id}/complete")
    assert first_complete_resp.status_code == 200
    movements_after_first = _movement_count()

    second_complete_resp = admin_client.post(f"/api/work-orders/{wo_id}/complete")
    assert second_complete_resp.status_code == 409
    assert second_complete_resp.get_json()["error"]["code"] == "invalid_transition"
    assert _movement_count() == movements_after_first


def test_put_qty_allowed_in_draft_and_rejected_after_release(admin_client):
    """05-work-orders.md: `PUT /api/work-orders/{id}` edits are only legal while `draft`; the same edit is 409 `invalid_transition` once `released`."""
    product_id = make_part(part_type="finished").id
    make_bom(db.session.get(Part, product_id), [(make_part(part_type="raw", qty_on_hand=10), 1)])
    db.session.commit()
    create_resp = admin_client.post(
        "/api/work-orders", json={"product_part_id": product_id, "qty": 1}
    )
    wo_id = create_resp.get_json()["id"]

    put_resp = admin_client.put(f"/api/work-orders/{wo_id}", json={"qty": 7})
    assert put_resp.status_code == 200
    assert put_resp.get_json()["qty"] == 7

    release_resp = admin_client.post(f"/api/work-orders/{wo_id}/release")
    assert release_resp.status_code == 200

    put_after_release_resp = admin_client.put(f"/api/work-orders/{wo_id}", json={"qty": 9})
    assert put_after_release_resp.status_code == 409
    assert put_after_release_resp.get_json()["error"]["code"] == "invalid_transition"


def test_cancel_from_released_then_complete_after_cancel_is_409(admin_client):
    """05-work-orders.md: `cancel` is legal from `released` (not just `draft`) and terminal — a canceled WO can never subsequently be completed."""
    product_id = make_part(part_type="finished").id
    make_bom(db.session.get(Part, product_id), [(make_part(part_type="raw", qty_on_hand=10), 1)])
    db.session.commit()
    create_resp = admin_client.post(
        "/api/work-orders", json={"product_part_id": product_id, "qty": 1}
    )
    wo_id = create_resp.get_json()["id"]
    admin_client.post(f"/api/work-orders/{wo_id}/release")

    cancel_resp = admin_client.post(f"/api/work-orders/{wo_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.get_json()["status"] == "canceled"

    complete_resp = admin_client.post(f"/api/work-orders/{wo_id}/complete")
    assert complete_resp.status_code == 409
    assert complete_resp.get_json()["error"]["code"] == "invalid_transition"


def test_detail_components_block_math_and_can_complete_flip(admin_client):
    """05-work-orders.md's `GET /api/work-orders/{id}` components block: `required`/`short` math, and `can_complete` tracking both stock *and* status.

    Walks one WO through draft (can_complete always false) -> released
    with a shortfall (short > 0, can_complete false) -> released with
    stock topped up (short == 0, can_complete true) -> completed
    (can_complete false again, purely from status this time).
    """
    product_id = make_part(part_type="finished").id
    comp_id = make_part(part_type="raw", qty_on_hand=3).id  # short by design
    make_bom(db.session.get(Part, product_id), [(db.session.get(Part, comp_id), 5)])
    db.session.commit()

    create_resp = admin_client.post(
        "/api/work-orders", json={"product_part_id": product_id, "qty": 2}  # required = 10
    )
    wo_id = create_resp.get_json()["id"]

    draft_detail = admin_client.get(f"/api/work-orders/{wo_id}").get_json()
    draft_component = draft_detail["components"][0]
    assert draft_component["part_id"] == comp_id
    assert draft_component["qty_per"] == 5
    assert draft_component["required"] == 10
    assert draft_component["on_hand"] == 3
    assert draft_component["short"] == 7
    assert draft_detail["can_complete"] is False  # status is draft, not released

    admin_client.post(f"/api/work-orders/{wo_id}/release")
    released_short_detail = admin_client.get(f"/api/work-orders/{wo_id}").get_json()
    assert released_short_detail["components"][0]["short"] == 7
    assert released_short_detail["can_complete"] is False  # still short

    apply_movement(
        part_id=comp_id, qty_delta=7, reason="adjustment", user_id=_admin_creator().id
    )
    db.session.commit()

    released_ready_detail = admin_client.get(f"/api/work-orders/{wo_id}").get_json()
    ready_component = released_ready_detail["components"][0]
    assert ready_component["on_hand"] == 10
    assert ready_component["short"] == 0
    assert released_ready_detail["can_complete"] is True

    complete_resp = admin_client.post(f"/api/work-orders/{wo_id}/complete")
    assert complete_resp.status_code == 200
    completed_detail = admin_client.get(f"/api/work-orders/{wo_id}").get_json()
    assert completed_detail["can_complete"] is False  # status is completed now
