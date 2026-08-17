"""Tests for `app/api/bom.py` (04-bom.md): the per-product Bill of Materials.

PUT /api/parts/{id}/bom uses **replace-all semantics** (the whole BOM,
not per-line CRUD) — most of these tests exist to pin down what that
means precisely: an edit-and-drop PUT truly replaces the row set, the
several ways a submitted BOM can be structurally invalid are each
rejected with the offending line identified, and the material-cost
rollup is a pure function of the *current* lines (so it moves when a
component's cost does, without the BOM itself being touched).

**Why every test below captures ids/skus into plain variables before the
first HTTP call, instead of reading them off the ORM object whenever
they're needed:** see test_inventory.py's module docstring —
`teardown_request` tears down the scoped session after every request the
test client makes, and touching an attribute on an object created before
that request raises `DetachedInstanceError` rather than transparently
refetching. A part that needs mutating *after* an HTTP call (the
material-cost test's component cost edit) is re-fetched fresh by id
instead of reusing the pre-request instance.
"""

import pytest

from app.extensions import db
from app.models import BomLine, Part
from conftest import make_part


def test_get_bom_of_non_finished_part_is_400_and_of_unknown_part_is_404(admin_client):
    """04-bom.md: GET .../bom validates the part is `finished` (400) before anything else, and 404s for an id that doesn't exist at all."""
    raw_part_id = make_part(part_type="raw").id
    db.session.commit()

    non_finished_resp = admin_client.get(f"/api/parts/{raw_part_id}/bom")
    assert non_finished_resp.status_code == 400
    assert non_finished_resp.get_json()["error"]["code"] == "validation_error"

    unknown_resp = admin_client.get("/api/parts/999999/bom")
    assert unknown_resp.status_code == 404
    assert unknown_resp.get_json()["error"]["code"] == "not_found"


def test_put_bom_replace_all_semantics_leaves_exactly_two_rows(admin_client):
    """04-bom.md's headline acceptance example: set a 3-line BOM, then PUT a 2-line BOM (one line edited, one dropped) -> exactly 2 rows in `bom_lines` for that product, verified against the model directly rather than trusting the response echo."""
    product_id = make_part(part_type="finished").id
    comp_a_id = make_part(part_type="raw").id
    comp_b_id = make_part(part_type="raw").id
    comp_c_id = make_part(part_type="raw").id
    db.session.commit()

    first_resp = admin_client.put(
        f"/api/parts/{product_id}/bom",
        json={
            "items": [
                {"component_part_id": comp_a_id, "qty_per": 1},
                {"component_part_id": comp_b_id, "qty_per": 2},
                {"component_part_id": comp_c_id, "qty_per": 3},
            ]
        },
    )
    assert first_resp.status_code == 200

    second_resp = admin_client.put(
        f"/api/parts/{product_id}/bom",
        json={
            "items": [
                {"component_part_id": comp_a_id, "qty_per": 5},  # edited qty_per
                {"component_part_id": comp_b_id, "qty_per": 2},
                # comp_c dropped entirely
            ]
        },
    )
    assert second_resp.status_code == 200

    rows = BomLine.query.filter_by(product_part_id=product_id).all()
    assert len(rows) == 2
    by_component = {row.component_part_id: row.qty_per for row in rows}
    assert by_component[comp_a_id] == 5
    assert by_component[comp_b_id] == 2
    assert comp_c_id not in by_component


def _assert_details_name_a_line(details, valid_indices):
    """Shared shape check: `details` is a non-empty list of `{"line": <index>, "message": ...}` rows, each pointing at one of the request's `items` indices.

    04-bom.md only promises "`details` listing each offending line", not an
    exact shape — the landed implementation identifies a line by its
    (0-based) position in the submitted `items` array, which is what this
    checks rather than assuming id/sku text appears in the message.
    """
    assert details, "expected details naming the offending line"
    for entry in details:
        assert "line" in entry
        assert entry["line"] in valid_indices
        assert entry.get("message")


def test_put_bom_self_reference_duplicate_and_zero_qty_each_400_naming_the_line(admin_client):
    """04-bom.md's three per-line validation rules, each checked in its own PUT: self-reference, a duplicated component, and `qty_per <= 0`. Each must come back 400 `validation_error` with `details` naming the offending line."""
    product_id = make_part(part_type="finished").id
    comp_id = make_part(part_type="raw").id
    db.session.commit()

    self_ref_resp = admin_client.put(
        f"/api/parts/{product_id}/bom",
        json={"items": [{"component_part_id": product_id, "qty_per": 1}]},
    )
    assert self_ref_resp.status_code == 400
    self_ref_error = self_ref_resp.get_json()["error"]
    assert self_ref_error["code"] == "validation_error"
    _assert_details_name_a_line(self_ref_error.get("details"), valid_indices={0})

    duplicate_resp = admin_client.put(
        f"/api/parts/{product_id}/bom",
        json={
            "items": [
                {"component_part_id": comp_id, "qty_per": 1},
                {"component_part_id": comp_id, "qty_per": 2},
            ]
        },
    )
    assert duplicate_resp.status_code == 400
    duplicate_error = duplicate_resp.get_json()["error"]
    assert duplicate_error["code"] == "validation_error"
    _assert_details_name_a_line(duplicate_error.get("details"), valid_indices={0, 1})

    zero_qty_resp = admin_client.put(
        f"/api/parts/{product_id}/bom",
        json={"items": [{"component_part_id": comp_id, "qty_per": 0}]},
    )
    assert zero_qty_resp.status_code == 400
    zero_qty_error = zero_qty_resp.get_json()["error"]
    assert zero_qty_error["code"] == "validation_error"
    _assert_details_name_a_line(zero_qty_error.get("details"), valid_indices={0})


def test_put_bom_two_part_subassembly_cycle_rejected(admin_client):
    """04-bom.md: a BOM edit that would create a cycle through sub-assemblies (A already contains B; PUT-ing B's BOM to contain A) is rejected with 400, walking the component graph rather than only checking the immediate line set."""
    part_a_id = make_part(part_type="finished").id
    part_b_id = make_part(part_type="finished").id
    db.session.commit()

    a_contains_b_resp = admin_client.put(
        f"/api/parts/{part_a_id}/bom",
        json={"items": [{"component_part_id": part_b_id, "qty_per": 1}]},
    )
    assert a_contains_b_resp.status_code == 200

    cycle_resp = admin_client.put(
        f"/api/parts/{part_b_id}/bom",
        json={"items": [{"component_part_id": part_a_id, "qty_per": 1}]},
    )
    assert cycle_resp.status_code == 400
    assert cycle_resp.get_json()["error"]["code"] == "validation_error"


def test_material_cost_equals_rollup_and_updates_after_component_cost_edit(admin_client):
    """04-bom.md: `material_cost` = Σ qty_per × component.unit_cost, recomputed fresh on every GET — so editing a component's cost (through no BOM write at all) changes what the *next* GET reports."""
    product_id = make_part(part_type="finished").id
    comp_a_id = make_part(part_type="raw", unit_cost="1.20").id
    comp_b_id = make_part(part_type="raw", unit_cost="3.00").id
    db.session.commit()

    put_resp = admin_client.put(
        f"/api/parts/{product_id}/bom",
        json={
            "items": [
                {"component_part_id": comp_a_id, "qty_per": 2},
                {"component_part_id": comp_b_id, "qty_per": 1},
            ]
        },
    )
    assert put_resp.status_code == 200
    assert float(put_resp.get_json()["material_cost"]) == pytest.approx(5.40)

    # Re-fetched fresh (rather than reusing a pre-request instance) since
    # the PUT above tore down the scoped session used to create it — see
    # module docstring.
    comp_a = db.session.get(Part, comp_a_id)
    comp_a.unit_cost = "2.00"
    db.session.commit()

    get_resp = admin_client.get(f"/api/parts/{product_id}/bom")
    assert get_resp.status_code == 200
    assert float(get_resp.get_json()["material_cost"]) == pytest.approx(7.00)
