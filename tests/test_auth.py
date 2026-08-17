"""Tests for `app/api/auth.py` (02-auth.md): login/logout/me and `require_login`.

`app/api/auth.py` is being written by a parallel agent per its own task
brief — until it lands and its blueprint is registered in
`app/__init__.py`, every test in this module that hits `/api/auth/*`
ERRORs or fails with 404 (an unmatched route), not the status codes
asserted below. That is expected and does not indicate a bug in this
file; re-run this module once auth lands.

This module also owns the module-level `ENDPOINTS` registry 10-testing.md
requires: a single place listing every endpoint that must reject an
unauthenticated request with 401, and (for admin-only ones) reject an
operator session with 403. See the giant comment above `ENDPOINTS` below
for the contract every later test module MUST follow.
"""

from types import SimpleNamespace
from typing import Callable, NamedTuple, Optional, Union

import pytest

from app.extensions import db
from conftest import make_part, make_user

# ---------------------------------------------------------------------------
# The protected-endpoint sweep
# ---------------------------------------------------------------------------


class Endpoint(NamedTuple):
    """One row of the sweep: an HTTP method/path pair and its role requirement.

    Attributes:
        method: HTTP verb, e.g. ``"POST"``.
        path: Either a plain path string (``"/api/auth/logout"``) or a
            zero-... one-argument callable ``seeded -> str`` for endpoints
            with path params, e.g.
            ``lambda seeded: f"/api/parts/{seeded.part.id}"``. The
            ``seeded`` fixture below is where a later module attaches
            whatever objects its callables need.
        role: ``None`` if any authenticated session (admin or operator) is
            enough (02-auth.md's ``Auth: any``); ``"admin"`` if the
            endpoint is admin-only. There is no third value for "no auth
            required at all" — login and (GET) me are deliberately left
            out of this list; see the comment below.
    """

    method: str
    path: Union[str, Callable[[SimpleNamespace], str]]
    role: Optional[str]


# ***************************************************************************
# EVERY MUTATING/PROTECTED ENDPOINT MUST BE LISTED HERE. This is the single
# place 10-testing.md's parametrized sweep reads from — a new blueprint
# (inventory, bom, work_orders, purchasing, sales_orders, dashboard) that
# does not add its routes to this list is *not covered* by the 401/403
# sweep, silently. When you build a new `app/api/*.py` module:
#
#   1. Add one Endpoint(...) entry per route it registers, with role=None
#      for "Auth: any" routes and role="admin" for "Auth: admin" routes.
#   2. If a route needs a path param (e.g. `/api/parts/<id>`), write path
#      as `lambda seeded: f"/api/parts/{seeded.part.id}"` and extend the
#      `seeded` fixture below (or add a module-level fixture that layers
#      on top of it) to attach `.part` (or whatever the lambda needs) via
#      the `make_*` factories in conftest.py.
#   3. GET-only, unauthenticated-by-design endpoints (today: nothing;
#      GET /api/auth/me is a special case, see below) do NOT belong here.
#
# `POST /api/auth/login` is intentionally NOT in this list: 02-auth.md
# says its `Auth: none`, so "no session -> 401" does not apply to it.
# `GET /api/auth/me` IS listed, even though 02-auth.md also says
# "Auth: none required" for it, because its actual behavior without a
# session is 401 (it just doesn't return 403 for a *valid* non-admin
# session — there's no admin-only version of "me"). That makes it,
# functionally, exactly what this sweep checks for a role=None endpoint.
# ***************************************************************************
ENDPOINTS = [
    Endpoint("POST", "/api/auth/logout", None),
    Endpoint("GET", "/api/auth/me", None),
    # --- app/api/inventory.py (03-inventory.md) ---
    Endpoint("GET", "/api/parts", None),
    Endpoint("POST", "/api/parts", "admin"),
    Endpoint("GET", lambda seeded: f"/api/parts/{seeded.part.id}", None),
    Endpoint("PUT", lambda seeded: f"/api/parts/{seeded.part.id}", "admin"),
    Endpoint("DELETE", lambda seeded: f"/api/parts/{seeded.part.id}", "admin"),
    Endpoint("POST", lambda seeded: f"/api/parts/{seeded.part.id}/activate", "admin"),
    Endpoint("POST", lambda seeded: f"/api/parts/{seeded.part.id}/adjust", None),
    Endpoint("GET", lambda seeded: f"/api/parts/{seeded.part.id}/movements", None),
    # --- app/api/bom.py (04-bom.md) ---
    Endpoint("GET", lambda seeded: f"/api/parts/{seeded.part.id}/bom", None),
    Endpoint("PUT", lambda seeded: f"/api/parts/{seeded.part.id}/bom", "admin"),
]


@pytest.fixture
def seeded():
    """Lazily-populated namespace for `Endpoint.path` callables that need ids.

    Attaches ``.part`` via ``make_part``: every inventory/BOM endpoint
    above with a path param reads a part id, and one shared part covers
    all of them — the ``require_login`` decorator (see app/api/auth.py)
    short-circuits with 401/403 before any route body/type validation
    runs, so the part's ``part_type``/status don't matter for this sweep.
    A later module needing a *different* kind of seeded object (a work
    order, a supplier, ...) should extend this same fixture rather than
    duplicating it.
    """
    part = make_part()
    db.session.commit()
    return SimpleNamespace(part=part)


def _resolve_path(endpoint, seeded):
    return endpoint.path(seeded) if callable(endpoint.path) else endpoint.path


def _endpoint_id(endpoint):
    path = endpoint.path if isinstance(endpoint.path, str) else "<dynamic>"
    return f"{endpoint.method} {path}"


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=_endpoint_id)
def test_sweep_no_session_returns_401(client, endpoint, seeded):
    """Every listed endpoint rejects a request with no session at all."""
    path = _resolve_path(endpoint, seeded)
    resp = client.open(path, method=endpoint.method)
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "endpoint",
    [e for e in ENDPOINTS if e.role == "admin"],
    ids=_endpoint_id,
)
def test_sweep_operator_forbidden_on_admin_only(operator_client, endpoint, seeded):
    """An operator session gets 403 (not 404, not 500) on every admin-only endpoint.

    Empty parametrization today (no admin-only endpoint is listed yet) —
    that's fine; pytest just collects zero tests for it until a later
    module adds one. Do not delete this test to "fix" the empty run.
    """
    path = _resolve_path(endpoint, seeded)
    resp = operator_client.open(path, method=endpoint.method)
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Endpoint-specific behavior
# ---------------------------------------------------------------------------


def test_login_happy_path_returns_user_and_sets_session_cookie(client):
    make_user("login_happy", "admin", password="correct-horse")
    db.session.commit()

    resp = client.post(
        "/api/auth/login", json={"username": "login_happy", "password": "correct-horse"}
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"]["username"] == "login_happy"
    assert body["user"]["role"] == "admin"
    assert isinstance(body["user"]["id"], int)
    assert "password" not in body["user"]
    assert "password_hash" not in body["user"]

    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "session=" in set_cookie


def test_login_wrong_password_and_unknown_username_get_byte_identical_bodies(client):
    """02-auth.md: "identical response — don't leak which" (bad password vs. unknown user)."""
    make_user("login_wrongpw", "operator", password="the-real-password")
    db.session.commit()

    wrong_password_resp = client.post(
        "/api/auth/login",
        json={"username": "login_wrongpw", "password": "not-it"},
    )
    unknown_user_resp = client.post(
        "/api/auth/login",
        json={"username": "no-such-user-exists", "password": "whatever"},
    )

    assert wrong_password_resp.status_code == 401
    assert unknown_user_resp.status_code == 401
    assert wrong_password_resp.data == unknown_user_resp.data
    body = wrong_password_resp.get_json()
    assert body["error"]["code"] == "unauthenticated"
    assert body["error"]["message"] == "Invalid username or password."


def test_login_missing_fields_returns_400_with_field_errors(client):
    resp = client.post("/api/auth/login", json={"username": "only_username"})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "validation_error"
    assert "field_errors" in body["error"]


def test_me_without_session_returns_401(client):
    resp = client.get("/api/auth/me")

    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "unauthenticated"


def test_me_with_session_returns_user(client):
    make_user("me_with_session", "operator", password="pw123")
    db.session.commit()
    login_resp = client.post(
        "/api/auth/login", json={"username": "me_with_session", "password": "pw123"}
    )
    assert login_resp.status_code == 200

    resp = client.get("/api/auth/me")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"]["username"] == "me_with_session"
    assert body["user"]["role"] == "operator"


def test_logout_then_me_returns_401(client):
    make_user("logout_user", "operator", password="pw123")
    db.session.commit()
    login_resp = client.post(
        "/api/auth/login", json={"username": "logout_user", "password": "pw123"}
    )
    assert login_resp.status_code == 200

    logout_resp = client.post("/api/auth/logout")
    assert logout_resp.status_code == 204
    assert logout_resp.data == b""

    me_resp = client.get("/api/auth/me")
    assert me_resp.status_code == 401
