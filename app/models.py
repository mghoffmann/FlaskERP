"""Shopfloor ERP — all SQLAlchemy models, in one file.

Small enough that every table is reviewable at a glance (00-architecture.md
keeps the whole schema in a single module on purpose). This module is the
**only** place table structure is defined; ``flask db migrate`` compares it
against the live database to generate migrations, and every table it
defines follows the same shape rule from 01-database.md:

    "All tables get `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`
    and `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` unless noted."

No table in this schema is noted as an exception, so every model below
inherits both columns from :class:`TimestampedModel` instead of repeating
them eleven times.

**SQLAlchemy 2.x declarative style — ``Mapped[]`` / ``mapped_column()``.**
Classic SQLAlchemy wrote ``name = db.Column(db.String(120))`` — one
statement that was simultaneously "here is a Python attribute" and "here is
a SQL column," with no separate type-checker-visible annotation. 2.0's
*Annotated Declarative* style splits that into two cooperating pieces on
the same line:

- ``Mapped[str]`` is a plain Python type annotation. It tells your editor,
  mypy/pyright, and SQLAlchemy itself what Python type this attribute
  holds once an instance is loaded — nothing more. ``Mapped[str | None]``
  says the column is nullable.
- ``mapped_column(...)`` is where the actual SQL lives: column type
  (``sa.String(120)``), constraints (``nullable``, ``unique``), and
  defaults. SQLAlchemy reads the ``Mapped[...]`` annotation to infer
  nullability/basic type when you don't spell it out, but every table here
  is explicit about type and nullability for clarity.

**``server_default`` vs. ``default``.** Two different mechanisms show up
below, and mixing them up is a common beginner trap:

- ``server_default=sa.func.now()`` bakes ``DEFAULT now()`` into the
  column's DDL itself. *Any* inserter gets the default — this ORM, a
  future script, another service, a human at ``psql`` — because Postgres
  itself supplies the value when a row is inserted without one.
- ``default=...`` (not used for timestamps here, but relevant conceptually)
  is Python-side: SQLAlchemy computes the value only when *this* ORM layer
  builds the ``INSERT``. Raw SQL bypassing the ORM would insert `NULL`/omit
  the column entirely and get nothing.

Timestamps and status defaults in this schema always use ``server_default``
so the database stays the single source of truth no matter what writes to
it — a raw ``psql`` insert during a support fix still gets a correct
``created_at``.

**Why CHECK-constrained ``VARCHAR`` instead of native Postgres ``ENUM``
types for statuses/roles/reasons?** 01-database.md answers this directly:

    "Statuses and enums are PostgreSQL `VARCHAR` columns with `CHECK`
    constraints (simpler to migrate than native enums, still
    database-enforced)."

Native Postgres enums require an `ALTER TYPE ... ADD VALUE` (which, prior
to Postgres 12, couldn't even run inside a transaction) every time a new
status is needed, and Alembic's autogenerate support for enum type changes
is notoriously fiddly. A `VARCHAR` + `CHECK (col IN (...))` is just a
column and a constraint — both trivially expressed in a normal migration,
still rejected by the database (not just app code) if something tries to
write `'bogus_status'`.

**Naming convention.** ``app/extensions.py`` creates ``db = SQLAlchemy()``
with no explicit ``metadata=``, so unnamed constraints (a bare
``unique=True`` on a column, an inline ``CheckConstraint`` with no
``name=``) would otherwise get Postgres's auto-generated names
(``parts_sku_key``, ``$2`` for checks, ...). Autogenerate diffs constraints
*by name*, so anonymous/inconsistent names make every future ``flask db
migrate`` produce noisy, hard-to-review drop/recreate pairs for
constraints that didn't actually change. Setting a naming convention here,
once, means every constraint Alembic ever sees has a deterministic,
greppable name like ``ck_parts_qty_on_hand_non_negative`` or
``fk_bom_lines_product_part_id_parts``.
"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

# Retrofit a naming convention onto the MetaData instance app/extensions.py
# already created. This has to happen before any model class is defined
# (constraints pick up the convention that is active on ``db.metadata`` at
# the moment SQLAlchemy builds their ``Table`` object), but it does not
# have to happen *inside* extensions.py — mutating the attribute here, at
# import time of this module, is enough, and keeps this module the single
# owner of "how the schema is shaped."
#
# Tokens like %(table_name)s / %(column_0_N_name)s are filled in per
# constraint at DDL-build time. ``column_0_N_name`` (rather than
# ``column_0_name``) is used for indexes/uniques so a two-column unique
# constraint (e.g. bom_lines' (product_part_id, component_part_id)) gets
# both column names in its generated name instead of just the first.
db.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class TimestampedModel(db.Model):
    """Abstract base providing the ``id`` / ``created_at`` pair every table shares.

    ``__abstract__ = True`` tells SQLAlchemy this class itself is never
    mapped to a real table — it exists only so concrete subclasses
    (``Part``, ``WorkOrder``, ...) inherit its two columns instead of each
    redeclaring them, which is exactly how 01-database.md's "all tables
    get ``id``... and ``created_at``... unless noted" rule reads in code:
    stated once, applied everywhere, impossible for a new table to forget.

    ``sa.Identity(always=True)`` is what ``GENERATED ALWAYS AS IDENTITY``
    means in code: the column's value always comes from a database-owned
    sequence. "Always" (as opposed to ``Identity(always=False)``, i.e.
    ``GENERATED BY DEFAULT AS IDENTITY``) means an ``INSERT`` that tries to
    supply its own ``id`` is *rejected* by Postgres unless it explicitly
    opts in with ``OVERRIDING SYSTEM VALUE`` — which nothing in this app
    ever does. That's a deliberate simplification over the classic
    ``SERIAL`` pseudo-type: identity columns are the SQL-standard,
    Postgres-recommended replacement for ``SERIAL``, and ``ALWAYS`` closes
    off an entire class of bugs where application code accidentally
    supplies a colliding id.
    """

    __abstract__ = True

    id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.Identity(always=True), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class User(TimestampedModel):
    """A login: either an ``admin`` (full access) or an ``operator`` (day-to-day floor work).

    ``password_hash`` stores a Werkzeug ``generate_password_hash(...)``
    output (see 02-auth.md), never a plaintext password. ``role`` is the
    CHECK-constrained-varchar pattern described in the module docstring —
    just two values today, but adding a third role later is a one-line
    migration instead of an enum-type surgery.
    """

    __tablename__ = "users"
    __table_args__ = (
        sa.CheckConstraint("role IN ('admin', 'operator')", name="role_valid"),
    )

    username: Mapped[str] = mapped_column(sa.String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    role: Mapped[str] = mapped_column(sa.String(20), nullable=False)

    # Reverse side of every "created_by" / "user_id" FK elsewhere in the
    # schema — lets code do ``user.stock_movements`` instead of a manual
    # query, and later modules (dashboard "recent activity", audit trails)
    # will want exactly this kind of reverse access.
    created_work_orders: Mapped[list["WorkOrder"]] = relationship(
        back_populates="creator"
    )
    created_purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(
        back_populates="creator"
    )
    created_sales_orders: Mapped[list["SalesOrder"]] = relationship(
        back_populates="creator"
    )
    stock_movements: Mapped[list["StockMovement"]] = relationship(
        back_populates="user"
    )


class Part(TimestampedModel):
    """Anything the factory buys, builds, or sells — raw material or finished product.

    01-database.md is explicit about ``qty_on_hand``'s status:

        "`parts.qty_on_hand` is a denormalized running balance; the source
        of truth for *how it got there* is `stock_movements`. The two must
        always change together in one transaction."

    In code, "always together" means exactly one writer:
    ``app/services/stock.py``'s ``apply_movement()`` is the only function
    in the whole codebase allowed to change this column (AGENTS.md), and
    it always does so in the same flush as inserting the
    ``StockMovement`` row that explains the change.
    """

    __tablename__ = "parts"
    __table_args__ = (
        sa.CheckConstraint("part_type IN ('raw', 'finished')", name="part_type_valid"),
        sa.CheckConstraint("qty_on_hand >= 0", name="qty_on_hand_non_negative"),
    )

    sku: Mapped[str] = mapped_column(sa.String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    part_type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(20), nullable=False, server_default="ea")
    qty_on_hand: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, server_default="0"
    )
    reorder_point: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, server_default="0"
    )
    unit_cost: Mapped[Decimal] = mapped_column(
        sa.Numeric(10, 2), nullable=False, server_default="0"
    )
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())

    # BOM lines where this part is the *product* (its recipe). The
    # component side deliberately has no symmetric collection here — a
    # part can be a component of many BOMs, but nothing in the requirement
    # docs needs "everywhere this part is used" as a first-class relation
    # yet, and adding an unused relationship just to be symmetric invites
    # confusion about which FK it follows.
    bom_lines: Mapped[list["BomLine"]] = relationship(
        foreign_keys="BomLine.product_part_id",
        back_populates="product",
        cascade="all, delete-orphan",
    )
    # Work orders that build this part, PO/SO lines that buy/sell it, and
    # its full movement history — all needed by later modules (part
    # detail page, the "can this part be deactivated?" open-document
    # check in 03-inventory.md, the movements ledger endpoint).
    work_orders: Mapped[list["WorkOrder"]] = relationship(back_populates="product")
    po_lines: Mapped[list["POLine"]] = relationship(back_populates="part")
    so_lines: Mapped[list["SOLine"]] = relationship(back_populates="part")
    movements: Mapped[list["StockMovement"]] = relationship(
        back_populates="part",
        order_by="StockMovement.created_at.desc()",
    )


class BomLine(TimestampedModel):
    """One component of a finished product's recipe: "product needs qty_per of component."

    Two parts, two different FKs to the *same* table (``parts``) — that's
    why both relationships below pass ``foreign_keys=`` explicitly rather
    than letting SQLAlchemy guess which column joins to which; without it,
    SQLAlchemy has no way to know whether ``BomLine.product`` should join
    on ``product_part_id`` or ``component_part_id``.

    A product's *own* row is a valid component of some other product's BOM
    (sub-assemblies), so the schema only forbids a product listing itself
    — enforced at the database via the ``product_component_distinct``
    check. Whether the product is actually ``part_type='finished'`` is an
    application-level rule (01-database.md), not a CHECK, because it's a
    cross-row rule a simple column CHECK can't express.
    """

    __tablename__ = "bom_lines"
    __table_args__ = (
        sa.UniqueConstraint("product_part_id", "component_part_id"),
        sa.CheckConstraint(
            "product_part_id <> component_part_id", name="product_component_distinct"
        ),
        sa.CheckConstraint("qty_per > 0", name="qty_per_positive"),
    )

    product_part_id: Mapped[int] = mapped_column(
        sa.ForeignKey("parts.id"), nullable=False
    )
    component_part_id: Mapped[int] = mapped_column(
        sa.ForeignKey("parts.id"), nullable=False
    )
    qty_per: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)

    product: Mapped["Part"] = relationship(
        foreign_keys=[product_part_id], back_populates="bom_lines"
    )
    component: Mapped["Part"] = relationship(foreign_keys=[component_part_id])


class WorkOrder(TimestampedModel):
    """"Build ``qty`` units of ``product``." Completing it consumes components, produces the product.

    ``wo_number`` (``WO-0007``-style) can't be a simple column default
    because it embeds the row's own id — it's set by application code
    right after the initial insert flush (see 05-work-orders.md), once
    Postgres has assigned the identity value. ``status`` walks
    ``draft -> released -> completed`` (or ``-> canceled``), each
    transition guarded by application code, not a CHECK (a CHECK can
    validate *which* values are legal, not which *transitions* between
    them are).
    """

    __tablename__ = "work_orders"
    __table_args__ = (
        sa.CheckConstraint("qty > 0", name="qty_positive"),
        sa.CheckConstraint(
            "status IN ('draft', 'released', 'completed', 'canceled')",
            name="status_valid",
        ),
    )

    wo_number: Mapped[str] = mapped_column(sa.String(20), unique=True, nullable=False)
    product_part_id: Mapped[int] = mapped_column(
        sa.ForeignKey("parts.id"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default="draft"
    )
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_by: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    product: Mapped["Part"] = relationship(back_populates="work_orders")
    creator: Mapped["User"] = relationship(back_populates="created_work_orders")


class Supplier(TimestampedModel):
    """A vendor the factory buys raw materials from. Soft-deleted (``active=False``), never dropped."""

    __tablename__ = "suppliers"

    name: Mapped[str] = mapped_column(sa.String(120), unique=True, nullable=False)
    contact_name: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())

    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(
        back_populates="supplier"
    )


class Customer(TimestampedModel):
    """A buyer of finished goods. Structurally identical to :class:`Supplier` — separate table because a real ERP's supplier and customer records diverge over time (payment terms, shipping addresses, ...) even though they start out looking the same."""

    __tablename__ = "customers"

    name: Mapped[str] = mapped_column(sa.String(120), unique=True, nullable=False)
    contact_name: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())

    sales_orders: Mapped[list["SalesOrder"]] = relationship(back_populates="customer")


class PurchaseOrder(TimestampedModel):
    """An order to a supplier for raw materials. ``status`` walks ``draft -> ordered -> received`` (or ``-> canceled``).

    Receiving (``received``) is where ``po_lines`` turn into
    ``stock_movements`` with ``reason='po_receive'`` through the stock
    service — see 03-inventory.md / 06-purchasing.md.
    """

    __tablename__ = "purchase_orders"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('draft', 'ordered', 'received', 'canceled')",
            name="status_valid",
        ),
    )

    po_number: Mapped[str] = mapped_column(sa.String(20), unique=True, nullable=False)
    supplier_id: Mapped[int] = mapped_column(
        sa.ForeignKey("suppliers.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default="draft"
    )
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_by: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), nullable=False)
    ordered_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    supplier: Mapped["Supplier"] = relationship(back_populates="purchase_orders")
    creator: Mapped["User"] = relationship(back_populates="created_purchase_orders")
    # cascade="all, delete-orphan" is the ORM-level mirror of the
    # database-level ondelete="CASCADE" on POLine.po_id below: deleting a
    # PurchaseOrder through the ORM deletes its lines through the ORM too
    # (so any SQLAlchemy-side events/hooks on POLine still fire), while
    # the database-level CASCADE is the backstop that keeps referential
    # integrity even for a delete issued outside this app (raw SQL, an
    # admin console).
    lines: Mapped[list["POLine"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )


class POLine(TimestampedModel):
    """One line item of a purchase order: buy ``qty`` of ``part`` at ``unit_cost``."""

    __tablename__ = "po_lines"
    __table_args__ = (
        sa.UniqueConstraint("po_id", "part_id"),
        sa.CheckConstraint("qty > 0", name="qty_positive"),
    )

    po_id: Mapped[int] = mapped_column(
        sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(sa.ForeignKey("parts.id"), nullable=False)
    qty: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(
        sa.Numeric(10, 2), nullable=False, server_default="0"
    )

    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="lines")
    part: Mapped["Part"] = relationship(back_populates="po_lines")


class SalesOrder(TimestampedModel):
    """An order from a customer for finished goods. ``status`` walks ``draft -> confirmed -> shipped`` (or ``-> canceled``).

    Shipping (``shipped``) is where ``so_lines`` turn into
    ``stock_movements`` with ``reason='so_ship'`` through the stock
    service — see 03-inventory.md / 07-sales-orders.md.
    """

    __tablename__ = "sales_orders"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed', 'shipped', 'canceled')",
            name="status_valid",
        ),
    )

    so_number: Mapped[str] = mapped_column(sa.String(20), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        sa.ForeignKey("customers.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default="draft"
    )
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_by: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    shipped_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    customer: Mapped["Customer"] = relationship(back_populates="sales_orders")
    creator: Mapped["User"] = relationship(back_populates="created_sales_orders")
    lines: Mapped[list["SOLine"]] = relationship(
        back_populates="sales_order", cascade="all, delete-orphan"
    )


class SOLine(TimestampedModel):
    """One line item of a sales order: ship ``qty`` of ``part`` at ``unit_price``.

    ``part`` must be ``part_type='finished'`` — an application rule (a raw
    material can't be sold directly), not a CHECK, for the same
    cross-table reason as :class:`BomLine`'s product-type rule.
    """

    __tablename__ = "so_lines"
    __table_args__ = (
        sa.UniqueConstraint("so_id", "part_id"),
        sa.CheckConstraint("qty > 0", name="qty_positive"),
    )

    so_id: Mapped[int] = mapped_column(
        sa.ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False
    )
    part_id: Mapped[int] = mapped_column(sa.ForeignKey("parts.id"), nullable=False)
    qty: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(
        sa.Numeric(10, 2), nullable=False, server_default="0"
    )

    sales_order: Mapped["SalesOrder"] = relationship(back_populates="lines")
    part: Mapped["Part"] = relationship(back_populates="so_lines")


class StockMovement(TimestampedModel):
    """One append-only ledger row: ``part`` changed by ``qty_delta`` for ``reason``, ever recorded.

    "Append-only" is enforced by convention (no code path in this app ever
    issues an ``UPDATE`` or ``DELETE`` against this table — see
    ``app/services/stock.py``), not by a database trigger; a demo-scale
    project accepts that trade-off in exchange for not needing
    trigger-level DDL in the migration.

    ``ref_type``/``ref_id`` are a loose, nullable "polymorphic reference"
    to whichever document caused the movement (a work order, a purchase
    order, a sales order) — deliberately *not* three separate nullable FK
    columns (``wo_id``, ``po_id``, ``so_id``), which would need an
    application-level check ensuring exactly one is set. A plain
    ``adjustment`` movement leaves both null.

    The ``(part_id, created_at DESC)`` index matches the one query this
    table's whole reason for existing optimizes for: "show me this part's
    movement history, newest first" (03-inventory.md's ``GET
    /api/parts/{id}/movements``). ``DESC`` in the index definition lets
    Postgres satisfy an ``ORDER BY created_at DESC`` with a plain index
    scan instead of a sort step.
    """

    __tablename__ = "stock_movements"
    __table_args__ = (
        sa.CheckConstraint("qty_delta <> 0", name="qty_delta_nonzero"),
        sa.CheckConstraint(
            "reason IN ('adjustment', 'wo_consume', 'wo_produce', 'po_receive', 'so_ship')",
            name="reason_valid",
        ),
        sa.CheckConstraint(
            "ref_type IS NULL OR ref_type IN "
            "('work_order', 'purchase_order', 'sales_order')",
            name="ref_type_valid",
        ),
        sa.Index("ix_stock_movements_part_id_created_at", "part_id", sa.text("created_at DESC")),
    )

    part_id: Mapped[int] = mapped_column(sa.ForeignKey("parts.id"), nullable=False)
    qty_delta: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), nullable=False)

    part: Mapped["Part"] = relationship(back_populates="movements")
    user: Mapped["User"] = relationship(back_populates="stock_movements")
