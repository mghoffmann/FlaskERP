"""Shopfloor ERP — the ``app.api`` package: one blueprint per domain.

**What's a blueprint?** Flask's unit for "a related group of routes plus
the URL prefix they share." Each module in this package (``auth.py``,
``inventory.py``, ``bom.py``, ``work_orders.py``, ``purchasing.py``,
``sales_orders.py``, ``dashboard.py``) defines exactly one module-level
``bp = Blueprint("name", __name__, url_prefix="/api/...")`` and decorates
its view functions with ``@bp.route(...)``. A blueprint is not wired into
the running app until something calls ``app.register_blueprint(bp)`` —
that happens once per module, in ``app/__init__.py``'s ``create_app()``,
which is also why importing this package alone never has side effects on
a real ``Flask`` app.

**Organization.** One module per business domain (00-architecture.md's
repository layout), not one file per HTTP verb or one giant file for
everything. Every module needs the database models, so importing any of
them (directly or via this package) pulls in ``app/models.py`` — which is
also how SQLAlchemy's metadata ends up fully populated by the time
``flask db migrate`` or ``db.create_all()`` runs; `app/__init__.py`
doesn't need to import ``app/models.py`` itself as long as every
registered blueprint module does.

This module also hosts small helpers every API module ends up wanting —
see :func:`get_json_or_400` below — so that shared logic has exactly one
home instead of being copy-pasted (or subtly reimplemented) per module.
"""

from datetime import timezone

from flask import request

from app.errors import ApiError


def iso(dt):
    """Format a timezone-aware ``datetime`` as an ISO-8601 UTC string ending in ``Z``.

    00-architecture.md's JSON convention is explicit about the wire
    format: ``"2026-08-17T14:30:00Z"``, not the ``+00:00`` offset suffix
    Python's own ``datetime.isoformat()`` produces by default. Every
    timestamp column in ``app/models.py`` is ``TIMESTAMPTZ`` (timezone-aware),
    so SQLAlchemy always hands this a UTC-aware ``datetime`` — this helper
    just reformats it, it does not need to guess or attach a timezone.
    ``astimezone(timezone.utc)`` is a defensive normalization (in case a
    future caller ever passes something not already in UTC) before the
    ``+00:00`` -> ``Z`` swap.

    Args:
        dt: A timezone-aware ``datetime.datetime``, or ``None``.

    Returns:
        str | None: The formatted timestamp, or ``None`` if ``dt`` is
        ``None`` (lets a caller write ``iso(document.shipped_at)`` for an
        optional timestamp column without a separate null check).
    """
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_json_or_400():
    """Return the request's parsed JSON body, or raise a 400 ``ApiError``.

    Flask's ``request.get_json()`` can fail two different ways that a
    caller almost always wants to treat identically — "there is no JSON
    body to parse":

    - No body at all, or a ``Content-Type`` that isn't
      ``application/json`` — ``get_json(silent=True)`` returns ``None``
      instead of raising, which is why this helper passes
      ``silent=True`` rather than letting Werkzeug's default behavior
      (a bare 400 with an HTML body) escape and violate
      00-architecture.md's "API consumers never receive HTML" rule.
    - A body that *is* present but isn't a JSON *object* (e.g. a bare
      JSON array, number, or string) — every endpoint in this app
      expects ``{"field": value, ...}``, so that case is rejected here
      too rather than letting each route re-check ``isinstance(..., dict)``.

    Routes call this once at the top of a handler and get back a plain
    ``dict`` they can trust is present and object-shaped; every other
    per-field validation (missing keys, blank strings, etc.) still
    belongs to the route/service that knows what fields it needs.

    Returns:
        dict: The parsed JSON request body.

    Raises:
        ApiError: 400 ``validation_error`` if the body is missing, is
            not valid JSON, or is not a JSON object.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "validation_error", "Request body must be a JSON object.")
    return data
