"""``flask seed`` — populate a fresh database with realistic demo data.

**Why a CLI command instead of an import-time side effect?** It would be
technically possible to make ``app/models.py`` (or ``create_app()``)
insert some rows the first time it's imported. That would be a mistake:
importing a module is supposed to be cheap and side-effect-free — pytest
imports these modules constantly (every test run, `create_app("testing")`
included), and a plain `python -c "import app.models"` for a quick REPL
check would silently start writing to whatever database `DATABASE_URL`
happens to point at. A CLI command is explicit and opt-in: nothing
happens until a human (or `scripts/db.py -r`) deliberately runs
``flask seed``, and it is easy to reason about exactly when it runs.

**``@app.cli.command`` / Click basics.** Flask's CLI is built on `Click
<https://click.palletsprojects.com/>`_, the library behind the `flask`
command itself. ``app.cli`` is a Click "group" that ``flask`` dispatches
subcommands to; anything registered on it becomes available as
``flask <name>`` alongside Flask's built-ins (``flask run``, ``flask db
...``). Registering a *separately defined* ``@click.command("seed")``
function with ``app.cli.add_command(...)`` (done in
``app/__init__.py``, see below) — rather than decorating a function with
``@app.cli.command()`` directly inside ``create_app()`` — keeps the
actual command implementation in this module instead of growing the
factory function, matching 00-architecture.md's ``app/cli.py``
placement. Click commands registered this way automatically run inside
an application context (so ``db.session`` works exactly as it would in a
request), but — unlike a request — nothing ever calls
``teardown_request``, so this module is responsible for its own
``commit()``.

**Idempotency.** 01-database.md requires ``flask seed`` to be safe to run
twice. The cheapest correct check is "does at least one user already
exist?" — if so, every table this command populates has presumably
already been seeded (or is real data a human doesn't want clobbered), so
the whole command is a no-op rather than trying to reconcile partial
state.

**Every stock number in this file goes through
``app.services.stock.apply_movement()``** — opening balances for raw
parts, the completed work order's consumption/production, the received
purchase order's receiving — never a bare ``part.qty_on_hand = ...``
assignment. That's not a seed-script-specific rule; it's the same rule
every other writer in the app follows (AGENTS.md), demonstrated here so
the seeded database's ledger is trustworthy from row one: reconciling
``SUM(qty_delta)`` per part against ``qty_on_hand`` (01-database.md's
acceptance criterion) works precisely because no seeded row bypassed the
ledger.
"""

import os
from datetime import datetime, timedelta, timezone

import click
from werkzeug.security import generate_password_hash

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
from app.services.stock import apply_movement

#: Raw materials: (sku, name, unit, reorder_point, unit_cost, opening_qty).
#: Two of these (bearing-608, motor-12v) seed *below* their reorder point
#: on purpose, per 01-database.md ("at least 2 seeded below reorder point
#: so the dashboard shows something").
_RAW_PARTS = [
    ("RAW-STEEL-BAR", "Steel Bar Stock", "m", 50, 12.50, 80),
    ("RAW-BEARING-608", "Ball Bearing 608", "ea", 100, 1.25, 40),
    ("RAW-SCREW-M4", "Screw M4x12", "ea", 500, 0.05, 2000),
    ("RAW-MOTOR-12V", "DC Motor 12V", "ea", 10, 45.00, 6),
    ("RAW-PAINT-BLACK", "Enamel Paint, Black", "L", 20, 8.75, 35),
    ("RAW-WIRE-14GA", "Hookup Wire 14AWG", "m", 100, 0.60, 250),
    ("RAW-PACKAGING-BOX", "Shipping Box, Medium", "ea", 50, 1.10, 120),
    ("RAW-BOLT-M6", "Hex Bolt M6x20", "ea", 300, 0.08, 900),
    ("RAW-BEARING-6202", "Ball Bearing 6202", "ea", 40, 2.10, 60),
    ("RAW-GEAR-BLANK", "Gear Blank, 20T", "ea", 15, 22.00, 25),
]

#: Finished products: (sku, name, unit, reorder_point, unit_cost).
_FINISHED_PARTS = [
    ("FIN-CONVEYOR-S", "Conveyor, Small", "ea", 5, 180.00),
    ("FIN-GEARBOX-A", "Gearbox, Type A", "ea", 3, 95.00),
    ("FIN-CART-HD", "Utility Cart, Heavy Duty", "ea", 5, 140.00),
]

#: BOMs: finished sku -> [(component sku, qty_per), ...]. 3-5 lines each,
#: per 01-database.md's seed-data requirement.
_BOMS = {
    "FIN-CONVEYOR-S": [
        ("RAW-STEEL-BAR", 4),
        ("RAW-BEARING-608", 8),
        ("RAW-BOLT-M6", 20),
        ("RAW-PAINT-BLACK", 0.5),
    ],
    "FIN-GEARBOX-A": [
        ("RAW-GEAR-BLANK", 3),
        ("RAW-BEARING-6202", 4),
        ("RAW-BOLT-M6", 12),
        ("RAW-WIRE-14GA", 1.5),
        ("RAW-PACKAGING-BOX", 1),
    ],
    "FIN-CART-HD": [
        ("RAW-STEEL-BAR", 6),
        ("RAW-BEARING-608", 4),
        ("RAW-BOLT-M6", 16),
    ],
}


def register_cli(app):
    """Attach ``flask seed`` to ``app``. Called once from ``create_app()``."""
    app.cli.add_command(seed_command)


def _seed_users():
    """Create the two seed logins. Passwords come from the environment.

    ``SEED_ADMIN_PASSWORD`` / ``SEED_OPERATOR_PASSWORD`` (00-architecture.md)
    default to ``admin123`` / ``operator123`` — fine for a throwaway dev
    database, never something to rely on outside local development.
    ``generate_password_hash`` (Werkzeug) salts and hashes; 02-auth.md's
    login flow verifies against this hash, never a plaintext password.
    """
    admin = User(
        username="admin",
        password_hash=generate_password_hash(
            os.environ.get("SEED_ADMIN_PASSWORD", "admin123")
        ),
        role="admin",
    )
    operator = User(
        username="operator",
        password_hash=generate_password_hash(
            os.environ.get("SEED_OPERATOR_PASSWORD", "operator123")
        ),
        role="operator",
    )
    db.session.add_all([admin, operator])
    db.session.flush()  # assigns admin.id / operator.id
    return admin, operator


def _seed_parts(admin):
    """Create all 13 parts (10 raw + 3 finished) and load raw opening stock.

    Every raw part starts at ``qty_on_hand = 0`` (the column default) and
    is brought up to its seed quantity with an ``adjustment`` movement
    "from zero" — exactly the pattern 01-database.md prescribes for
    opening balances, and the reason none of this function ever assigns
    ``part.qty_on_hand`` directly. Finished parts start at 0 and stay
    there until the completed work order (below) produces some.
    """
    parts_by_sku = {}

    for sku, name, unit, reorder_point, unit_cost, opening_qty in _RAW_PARTS:
        part = Part(
            sku=sku,
            name=name,
            part_type="raw",
            unit=unit,
            reorder_point=reorder_point,
            unit_cost=unit_cost,
        )
        db.session.add(part)
        parts_by_sku[sku] = part

    for sku, name, unit, reorder_point, unit_cost in _FINISHED_PARTS:
        part = Part(
            sku=sku,
            name=name,
            part_type="finished",
            unit=unit,
            reorder_point=reorder_point,
            unit_cost=unit_cost,
        )
        db.session.add(part)
        parts_by_sku[sku] = part

    db.session.flush()  # assigns part ids, needed by apply_movement below

    for sku, *_rest, opening_qty in _RAW_PARTS:
        apply_movement(
            part_id=parts_by_sku[sku].id,
            qty_delta=opening_qty,
            reason="adjustment",
            user_id=admin.id,
            note="Opening balance (seed data).",
        )

    return parts_by_sku


def _seed_boms(parts_by_sku):
    """Create the BOM line for every (product, component) pair in ``_BOMS``."""
    for product_sku, lines in _BOMS.items():
        product = parts_by_sku[product_sku]
        for component_sku, qty_per in lines:
            db.session.add(
                BomLine(
                    product_part_id=product.id,
                    component_part_id=parts_by_sku[component_sku].id,
                    qty_per=qty_per,
                )
            )


def _seed_parties():
    """Create the 2 suppliers and 2 customers."""
    suppliers = [
        Supplier(
            name="Acme Metal Supply",
            contact_name="Jane Doe",
            email="jane@acmemetal.example",
            phone="555-0101",
        ),
        Supplier(
            name="Bolt & Fastener Co",
            contact_name="Tom Rivera",
            email="tom@boltfastener.example",
            phone="555-0102",
        ),
    ]
    customers = [
        Customer(
            name="Northside Manufacturing",
            contact_name="Alice Chen",
            email="alice@northside.example",
            phone="555-0201",
        ),
        Customer(
            name="Riverside Industries",
            contact_name="Marcus Lee",
            email="marcus@riverside.example",
            phone="555-0202",
        ),
    ]
    db.session.add_all(suppliers + customers)
    db.session.flush()
    return suppliers, customers


def _seed_work_orders(parts_by_sku, admin, now):
    """Create 1 draft, 1 released, and 1 completed work order.

    Document numbers (``WO-0001``, ...) can't be a column default because
    they embed the row's own id — each is set right after *that row's*
    insert flush, once Postgres has assigned its identity value
    (01-database.md). Each work order is flushed and numbered on its own,
    one at a time, rather than as one batch: ``wo_number`` is ``NOT NULL``
    and ``UNIQUE``, so three rows sharing a temporary placeholder value
    would collide with each other the instant they hit the database.
    Only the completed one writes stock movements: the other two haven't
    reached a stock-affecting transition yet.
    """
    draft_wo = WorkOrder(
        wo_number="pending",
        product_part_id=parts_by_sku["FIN-GEARBOX-A"].id,
        qty=10,
        status="draft",
        notes="Standard production run.",
        # All seeded documents are admin-created: 02-auth.md's role matrix
        # says operators can't create documents, and seed data should not
        # contradict the rules the API enforces.
        created_by=admin.id,
    )
    db.session.add(draft_wo)
    db.session.flush()
    draft_wo.wo_number = f"WO-{draft_wo.id:04d}"

    released_wo = WorkOrder(
        wo_number="pending",
        product_part_id=parts_by_sku["FIN-CONVEYOR-S"].id,
        qty=5,
        status="released",
        notes="Released to the floor.",
        created_by=admin.id,
        released_at=now - timedelta(days=1),
    )
    db.session.add(released_wo)
    db.session.flush()
    released_wo.wo_number = f"WO-{released_wo.id:04d}"

    completed_wo = WorkOrder(
        wo_number="pending",
        product_part_id=parts_by_sku["FIN-CART-HD"].id,
        qty=3,
        status="completed",
        notes="Rush order, completed ahead of schedule.",
        created_by=admin.id,
        released_at=now - timedelta(days=3),
        completed_at=now - timedelta(days=1),
    )
    db.session.add(completed_wo)
    db.session.flush()  # assigns completed_wo.id, needed for wo_number and movement ref_id
    completed_wo.wo_number = f"WO-{completed_wo.id:04d}"

    # Completing a work order consumes its BOM components and produces
    # the finished good — the same two-sided transaction 05-work-orders.md
    # will implement for the real completion endpoint, done by hand here
    # since seeding runs before that endpoint exists.
    for component_sku, qty_per in _BOMS["FIN-CART-HD"]:
        apply_movement(
            part_id=parts_by_sku[component_sku].id,
            qty_delta=-(qty_per * float(completed_wo.qty)),
            reason="wo_consume",
            user_id=admin.id,
            ref_type="work_order",
            ref_id=completed_wo.id,
            note=f"Consumed for {completed_wo.wo_number}.",
        )
    apply_movement(
        part_id=parts_by_sku["FIN-CART-HD"].id,
        qty_delta=float(completed_wo.qty),
        reason="wo_produce",
        user_id=admin.id,
        ref_type="work_order",
        ref_id=completed_wo.id,
        note=f"Produced by {completed_wo.wo_number}.",
    )


def _seed_purchase_orders(parts_by_sku, suppliers, admin, now):
    """Create 1 ordered and 1 received purchase order.

    Receiving is the only PO status that moves stock (``po_receive``),
    written through ``apply_movement`` once the line is known — matching
    what 06-purchasing.md's real receiving endpoint will do. As with work
    orders, each PO is flushed and numbered individually so two rows
    never share the ``UNIQUE`` ``po_number`` placeholder at once.
    """
    ordered_po = PurchaseOrder(
        po_number="pending",
        supplier_id=suppliers[0].id,
        status="ordered",
        notes="Restocking bar stock.",
        created_by=admin.id,
        ordered_at=now - timedelta(days=2),
    )
    ordered_po.lines.append(
        POLine(part_id=parts_by_sku["RAW-STEEL-BAR"].id, qty=100, unit_cost=12.00)
    )
    db.session.add(ordered_po)
    db.session.flush()
    ordered_po.po_number = f"PO-{ordered_po.id:04d}"

    received_po = PurchaseOrder(
        po_number="pending",
        supplier_id=suppliers[1].id,
        status="received",
        notes="Bulk bolt order.",
        created_by=admin.id,
        ordered_at=now - timedelta(days=5),
        received_at=now - timedelta(days=1),
    )
    received_po.lines.append(
        POLine(part_id=parts_by_sku["RAW-BOLT-M6"].id, qty=500, unit_cost=0.07)
    )
    db.session.add(received_po)
    db.session.flush()  # assigns received_po.id, needed for po_number and movement ref_id
    received_po.po_number = f"PO-{received_po.id:04d}"

    for line in received_po.lines:
        apply_movement(
            part_id=line.part_id,
            qty_delta=float(line.qty),
            reason="po_receive",
            user_id=admin.id,
            ref_type="purchase_order",
            ref_id=received_po.id,
            note=f"Received {received_po.po_number}.",
        )
        # 06-purchasing.md's "last cost" policy: receiving updates the
        # part's unit_cost to the PO line's cost (when > 0). The real
        # receive endpoint will do this too; the seeded received PO must
        # leave the same state it would have.
        if line.unit_cost and line.unit_cost > 0:
            line.part.unit_cost = line.unit_cost


def _seed_sales_orders(parts_by_sku, customers, admin, now):
    """Create 1 confirmed sales order. Confirmed doesn't ship yet, so no stock movement."""
    confirmed_so = SalesOrder(
        so_number="pending",
        customer_id=customers[0].id,
        status="confirmed",
        notes="Customer requested delivery next week.",
        created_by=admin.id,
        confirmed_at=now - timedelta(hours=6),
    )
    confirmed_so.lines.append(
        SOLine(
            part_id=parts_by_sku["FIN-CONVEYOR-S"].id, qty=2, unit_price=350.00
        )
    )
    db.session.add(confirmed_so)
    db.session.flush()
    confirmed_so.so_number = f"SO-{confirmed_so.id:04d}"


def seed():
    """Populate the database, or do nothing if it's already been seeded."""
    if db.session.query(User.id).first() is not None:
        click.echo("seed: users already exist, skipping.")
        return

    now = datetime.now(timezone.utc)

    # _seed_users() also creates the operator login; only admin is needed
    # below because every seeded document/movement is admin-attributed.
    admin, _operator = _seed_users()
    parts_by_sku = _seed_parts(admin)
    _seed_boms(parts_by_sku)
    suppliers, customers = _seed_parties()
    _seed_work_orders(parts_by_sku, admin, now)
    _seed_purchase_orders(parts_by_sku, suppliers, admin, now)
    _seed_sales_orders(parts_by_sku, customers, admin, now)

    db.session.commit()
    click.echo(
        f"seed: created {len(_RAW_PARTS)} raw parts, {len(_FINISHED_PARTS)} "
        "finished parts, 2 suppliers, 2 customers, 3 work orders, "
        "2 purchase orders, 1 sales order."
    )


@click.command("seed")
def seed_command():
    """``flask seed`` — idempotent demo-data loader (see module docstring)."""
    try:
        seed()
    except Exception:
        db.session.rollback()
        raise
