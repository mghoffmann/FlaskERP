"""Shared pytest fixtures and factory helpers for the whole test suite.

Read 10-testing.md before touching this file — it is the spec this module
implements. Two design decisions are worth calling out up front because
they diverge from the "default" pytest+SQLAlchemy pattern you'll find in
most tutorials:

**Real Postgres, not SQLite.** ``TestingConfig`` (app/config.py) points at
a dedicated ``erp_test`` database. The schema uses Postgres-specific
features this app's correctness actually depends on — ``CHECK``
constraints (``qty_on_hand >= 0``, status enums) and ``SELECT ... FOR
UPDATE`` row locking in ``app/services/stock.py``. SQLite either doesn't
support these or behaves differently, so testing against it would mean
testing a different database than the one that ships. `db.create_all()`
(not `flask db upgrade`) builds the schema for tests — 10-testing.md
explicitly allows this ("migrations are exercised by the entrypoint and
CI step"), so the migration chain itself is verified elsewhere (CI runs
`flask db upgrade` against a fresh Postgres before `pytest`), and this
suite is free to rebuild the schema the fast way once per session.

**TRUNCATE, not a rolled-back transaction, for per-test isolation.** The
common pytest+SQLAlchemy pattern wraps each test in an outer transaction
and a `SAVEPOINT`, then rolls the whole thing back at teardown so nothing
a test does is ever really committed. That pattern assumes the app being
tested never calls `session.commit()` itself — the test owns the only
real transaction. This app does not meet that assumption:
`app/__init__.py`'s `teardown_request` handler *really* commits (or rolls
back on exception) after every single request the Flask test client
makes, because "each request is one transaction" is 00-architecture.md's
rule and the whole point of building this ERP is to demonstrate that
transaction discipline. Fighting that with a SAVEPOINT-swallows-commit
trick would mean patching the app's own commit calls into no-ops during
tests — testing something other than the app. Truncating every table
after each test (see ``_isolated_db`` below) sidesteps the conflict
entirely: every test starts from a genuinely empty, freshly-committed
database, and the suite is small enough (10-testing.md targets ~25-35
tests) that the extra per-test TRUNCATE is not a performance concern.
"""

import itertools

import pytest
import sqlalchemy as sa
from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import (
    BomLine,
    Customer,
    Part,
    POLine,
    PurchaseOrder,
    SalesOrder,
    SOLine,
    Supplier,
    User,
    WorkOrder,
)

# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app():
    """Build the one Flask app instance the whole test session shares.

    Session-scoped because building the app and (re)creating the schema is
    the expensive part; per-test isolation is handled separately by
    ``_isolated_db`` below via TRUNCATE, not by rebuilding the app.

    ``db.drop_all()`` before ``db.create_all()`` means a stale schema left
    over from a previous, interrupted run (e.g. a model changed since the
    last `pytest` invocation) can't cause confusing failures — every test
    session starts from models.py's current truth, not whatever happened
    to already be in `erp_test`.
    """
    flask_app = create_app("testing")
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
    yield flask_app


@pytest.fixture(autouse=True)
def _isolated_db(app):
    """Push a per-test app context and truncate every table afterward.

    Autouse so every test — including ones in test_stock_service.py and
    test_seed.py that call service/CLI functions directly, with no HTTP
    request in sight — gets an active `flask.g`/`db.session` app context
    without asking for one by name, and so no test can forget to clean up
    after itself.

    The push happens *before* the test body runs (so `make_user()` et al.
    have something to flush into); the TRUNCATE happens *after* (see the
    module docstring for why TRUNCATE instead of a rolled-back
    transaction). ``RESTART IDENTITY`` resets every table's identity
    sequence back to 1, which is what keeps human-facing numbers
    (``WO-0001``), factory-generated SKUs, and any test asserting on a
    literal id predictable run after run instead of drifting upward
    forever. ``CASCADE`` lets a single statement truncate every table at
    once regardless of FK relationships between them.
    """
    ctx = app.app_context()
    ctx.push()
    yield
    table_names = ", ".join(f'"{table.name}"' for table in db.metadata.tables.values())
    db.session.execute(sa.text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    db.session.commit()
    db.session.remove()
    ctx.pop()


@pytest.fixture
def client(app):
    """A plain (not logged in) Flask test client."""
    return app.test_client()


@pytest.fixture
def admin_client(client):
    """A test client already holding a valid session for a fresh admin user.

    Creates the user via ``make_user`` then really calls
    ``POST /api/auth/login`` (rather than poking the session cookie
    directly) so tests using this fixture exercise the real login path,
    same as a browser would.

    Until app/api/auth.py exists (a parallel task is writing it — see
    AGENTS.md), the login POST 404s and this fixture fails loudly via
    ``pytest.fail`` rather than silently returning an unauthenticated
    client — every test that depends on ``admin_client`` should ERROR,
    not quietly fail a 401 assertion for the wrong reason.
    """
    make_user("fixture_admin", "admin", "adminpw")
    db.session.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "fixture_admin", "password": "adminpw"}
    )
    if resp.status_code != 200:
        pytest.fail(
            "admin_client fixture: POST /api/auth/login returned "
            f"{resp.status_code}: {resp.get_data(as_text=True)}"
        )
    return client


@pytest.fixture
def operator_client(client):
    """A test client already holding a valid session for a fresh operator user.

    See ``admin_client`` above for why login failure is a hard
    ``pytest.fail`` rather than a silent pass-through.
    """
    make_user("fixture_operator", "operator", "operatorpw")
    db.session.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "fixture_operator", "password": "operatorpw"}
    )
    if resp.status_code != 200:
        pytest.fail(
            "operator_client fixture: POST /api/auth/login returned "
            f"{resp.status_code}: {resp.get_data(as_text=True)}"
        )
    return client


# ---------------------------------------------------------------------------
# Factory helpers
#
# Plain functions, not fixtures and not a factory library (10-testing.md) —
# a test calls e.g. `make_part(part_type="finished")` directly and gets back
# a flushed (id populated, not committed) model instance. They rely on
# `_isolated_db` having already pushed an app context, which every test gets
# automatically since that fixture is autouse.
# ---------------------------------------------------------------------------

#: Monotonic counter backing `make_part`'s auto-generated SKUs so parallel
#: tests in the same session never collide on the `parts.sku` UNIQUE
#: constraint just because they didn't bother to pass one.
_sku_counter = itertools.count(1)


def make_user(username, role, password="pw"):
    """Create, flush, and return a :class:`~app.models.User`.

    Args:
        username: Must be unique within the test (the `users.username`
            column is UNIQUE).
        role: ``"admin"`` or ``"operator"`` (the `role_valid` CHECK).
        password: Plaintext password to hash with Werkzeug's
            `generate_password_hash` — never stored plaintext, matching
            02-auth.md.
    """
    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
    )
    db.session.add(user)
    db.session.flush()
    return user


def make_part(**overrides):
    """Create, flush, and return a :class:`~app.models.Part` with sensible defaults.

    Any column can be overridden by keyword, e.g. ``make_part(part_type="finished",
    qty_on_hand=50)``. A caller-supplied ``sku`` is honored as-is; otherwise a
    unique one is generated from ``_sku_counter`` so tests never have to think
    about SKU collisions.
    """
    defaults = {
        "sku": f"TEST-PART-{next(_sku_counter):04d}",
        "name": "Test Part",
        "part_type": "raw",
        "unit": "ea",
        "qty_on_hand": 0,
        "reorder_point": 0,
        "unit_cost": 0,
    }
    defaults.update(overrides)
    part = Part(**defaults)
    db.session.add(part)
    db.session.flush()
    return part


def make_bom(product, lines):
    """Create BOM lines for ``product``.

    Args:
        product: The finished :class:`~app.models.Part` the BOM belongs to.
        lines: ``[(component_part, qty_per), ...]``.

    Returns:
        list[BomLine]: the flushed rows, in the same order as ``lines``.
    """
    bom_lines = [
        BomLine(product_part_id=product.id, component_part_id=component.id, qty_per=qty_per)
        for component, qty_per in lines
    ]
    db.session.add_all(bom_lines)
    db.session.flush()
    return bom_lines


def make_wo(product, qty, creator, status="draft", **overrides):
    """Create, flush, and return a :class:`~app.models.WorkOrder`.

    ``wo_number`` follows the same two-step pattern as `app/cli.py`'s seed
    data: it embeds the row's own id, so it can only be set *after* the
    initial flush assigns one — a placeholder is inserted first, then
    overwritten and re-flushed.

    Args:
        product: The finished :class:`~app.models.Part` this WO builds.
        qty: Quantity to build.
        creator: The :class:`~app.models.User` who "created" the WO
            (`created_by`).
        status: One of `draft`/`released`/`completed`/`canceled`.
        **overrides: Any other WorkOrder column (e.g. `released_at`,
            `completed_at`, `notes`).
    """
    wo = WorkOrder(
        wo_number="pending",
        product_part_id=product.id,
        qty=qty,
        status=status,
        created_by=creator.id,
        **overrides,
    )
    db.session.add(wo)
    db.session.flush()
    wo.wo_number = f"WO-{wo.id:04d}"
    db.session.flush()
    return wo


def make_supplier(**overrides):
    """Create, flush, and return a :class:`~app.models.Supplier` with sensible defaults.

    Mirrors :func:`make_part`'s override style. ``name`` is the table's
    only UNIQUE column, so it gets an auto-generated default (reusing
    ``_sku_counter`` rather than standing up a second counter just for
    this) so tests never collide on ``suppliers.name`` just because they
    didn't bother to pass one.
    """
    defaults = {"name": f"Test Supplier {next(_sku_counter):04d}"}
    defaults.update(overrides)
    supplier = Supplier(**defaults)
    db.session.add(supplier)
    db.session.flush()
    return supplier


def make_customer(**overrides):
    """Create, flush, and return a :class:`~app.models.Customer` with sensible defaults.

    See :func:`make_supplier` immediately above — ``Customer`` is
    structurally identical (01-database.md), so the same reasoning
    applies here for the auto-generated ``name``.
    """
    defaults = {"name": f"Test Customer {next(_sku_counter):04d}"}
    defaults.update(overrides)
    customer = Customer(**defaults)
    db.session.add(customer)
    db.session.flush()
    return customer


def make_po(supplier, creator, lines=None, status="draft", **overrides):
    """Create, flush, and return a :class:`~app.models.PurchaseOrder`.

    Args:
        supplier: The :class:`~app.models.Supplier` this PO is placed with.
        creator: The :class:`~app.models.User` who "created" the PO.
        lines: Optional ``[(part, qty, unit_cost), ...]`` — line items to
            attach before the initial flush.
        status: One of `draft`/`ordered`/`received`/`canceled`.
        **overrides: Any other PurchaseOrder column (e.g. `ordered_at`).
    """
    po = PurchaseOrder(
        po_number="pending",
        supplier_id=supplier.id,
        status=status,
        created_by=creator.id,
        **overrides,
    )
    for part, qty, unit_cost in lines or []:
        po.lines.append(POLine(part_id=part.id, qty=qty, unit_cost=unit_cost))
    db.session.add(po)
    db.session.flush()
    po.po_number = f"PO-{po.id:04d}"
    db.session.flush()
    return po


def make_so(customer, creator, lines=None, status="draft", **overrides):
    """Create, flush, and return a :class:`~app.models.SalesOrder`.

    Args:
        customer: The :class:`~app.models.Customer` this SO is for.
        creator: The :class:`~app.models.User` who "created" the SO.
        lines: Optional ``[(part, qty, unit_price), ...]`` — line items to
            attach before the initial flush.
        status: One of `draft`/`confirmed`/`shipped`/`canceled`.
        **overrides: Any other SalesOrder column (e.g. `confirmed_at`).
    """
    so = SalesOrder(
        so_number="pending",
        customer_id=customer.id,
        status=status,
        created_by=creator.id,
        **overrides,
    )
    for part, qty, unit_price in lines or []:
        so.lines.append(SOLine(part_id=part.id, qty=qty, unit_price=unit_price))
    db.session.add(so)
    db.session.flush()
    so.so_number = f"SO-{so.id:04d}"
    db.session.flush()
    return so
