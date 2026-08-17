"""Shopfloor ERP — Flask application factory.

This module exposes exactly one thing callers need: ``create_app()``.

**Why a factory function instead of a module-level ``app = Flask(__name__)``?**
Building the ``Flask`` instance inside a function, rather than at import
time, means:

- Multiple independently-configured instances can exist (e.g. pytest
  builds a fresh ``create_app("testing")`` app per test run) without
  fighting over shared global state.
- Extensions (``app/extensions.py``) can be created *unbound* at import
  time and only wired to a real app when ``create_app()`` runs, which
  breaks the circular-import trap where models need ``db`` and blueprints
  need models and the app needs blueprints.
- Configuration is picked at creation time (a string, an env var), not
  baked into the module — the same code runs in dev, test, and prod.

**Blueprints**: Flask's unit for "a related group of routes" — e.g. every
``/api/parts/*`` route will live in one blueprint object registered with
``app.register_blueprint(...)``. This project has none yet (the
``app/api`` package is built in a later phase); the loop near the bottom
of ``create_app()`` is where each one gets appended, one line at a time,
as it's built.
"""

import os

from flask import Flask, redirect

from app.cli import register_cli
from app.config import DevelopmentConfig, config
from app.errors import ApiError, handle_api_error
from app.extensions import db, migrate

#: Default (status -> (code, message)) used for errors Flask/Werkzeug
#: raises on its own — an unmatched route, a wrong HTTP verb, a bare
#: ``abort(409)`` without a specific ``ApiError`` — as opposed to an
#: ``ApiError`` a blueprint raised deliberately with its own code and
#: message. Codes/messages follow the table in 00-architecture.md;
#: 405 has no table entry there, so it uses a plain descriptive code.
_DEFAULT_ERRORS = {
    400: ("validation_error", "Invalid request."),
    401: ("unauthenticated", "Authentication required."),
    403: ("forbidden", "You do not have permission to perform this action."),
    404: ("not_found", "The requested resource was not found."),
    405: ("method_not_allowed", "The HTTP method is not allowed for this route."),
    409: ("conflict", "The request conflicts with the current state of the resource."),
    500: ("internal_error", "An unexpected error occurred."),
}


def register_error_handlers(app):
    """Route every error response — ours or Flask's — through the same JSON envelope.

    Two kinds of errors reach a handler registered here:

    1. ``ApiError``, raised deliberately by route/service code with a
       specific status/code/message (and optionally ``field_errors``/
       ``details``). Handled by ``app.errors.handle_api_error``.
    2. Errors Flask/Werkzeug raise on their own: an unmatched route
       (404), a wrong HTTP verb (405), an unhandled exception (500),
       or a bare ``abort(401)``/``abort(403)``/``abort(409)`` with no
       matching ``ApiError``. Left unhandled, Flask renders its
       default *HTML* error pages here — which is exactly what
       00-architecture.md's "API consumers never receive HTML"
       acceptance criterion forbids. Each status is mapped to a
       generic ``ApiError`` with a sensible default code/message and
       rendered through the same ``handle_api_error``/``to_dict()``
       path, so there is exactly one JSON error shape in this app, no
       matter where the error came from.
    """
    app.register_error_handler(ApiError, handle_api_error)

    def make_handler(status, code, message):
        def handler(_caught_error):
            return handle_api_error(ApiError(status, code, message))

        return handler

    for status, (code, message) in _DEFAULT_ERRORS.items():
        app.register_error_handler(status, make_handler(status, code, message))


def create_app(config_name=None):
    """Build and configure a Flask application instance.

    Args:
        config_name: One of ``"development"``, ``"testing"``,
            ``"production"`` (the keys of ``app.config.config``). Falls
            back to the ``FLASK_CONFIG`` environment variable, then to
            ``"development"``, so plain `flask run` needs no extra
            flags and `create_app("testing")` is all pytest needs.
            ``DATABASE_URL`` is still required in every environment
            (00-architecture.md) — only ``TestingConfig`` ships a
            built-in default, so a database-less `create_app("testing")`
            works out of the box while dev/prod read it from `.env` /
            the container environment.

    Returns:
        A ``Flask`` app with config loaded, extensions bound, error
        handlers and the request-teardown hook registered, and the
        static frontend wired up. No blueprints are registered yet —
        the ``app/api`` package is built in a later phase and appends
        to the ``blueprints`` list below.
    """
    config_name = config_name or os.environ.get("FLASK_CONFIG", "development")
    config_class = config.get(config_name, DevelopmentConfig)

    # static_url_path="" serves everything under app/static/ at the
    # site root, so app/static/parts.html is reachable at "/parts.html"
    # with no "/static/" prefix (00-architecture.md).
    app = Flask(__name__, static_folder="static", static_url_path="")

    # Instantiating the config class (rather than passing the class
    # itself) is deliberate: Flask's app.config.from_object() only
    # *reads* uppercase attributes off whatever object you give it, it
    # never calls the class — so ProductionConfig.__init__'s SECRET_KEY
    # check would silently never run if we passed the bare class.
    app.config.from_object(config_class())

    # Bind the shared, previously-unbound extension instances to this
    # specific app (see app/extensions.py for why they're created
    # unbound in the first place).
    db.init_app(app)
    migrate.init_app(app, db)

    register_error_handlers(app)

    # `flask seed` (app/cli.py) — registered here, not decorated directly
    # in this factory, so the command's implementation lives with the
    # rest of app/cli.py instead of growing create_app().
    register_cli(app)

    @app.teardown_request
    def teardown_db_session(exception=None):
        """Commit or roll back the request's database transaction.

        Flask-SQLAlchemy's ``db.session`` is a *scoped session*: a
        request-local proxy that lazily starts a transaction the first
        time a query runs. 00-architecture.md's "each request is one
        transaction" rule is enforced right here — ``teardown_request``
        runs after every request (whether the view returned normally or
        raised), and Flask passes it the exception when one occurred
        **even if an error handler already turned that exception into a
        valid JSON response** (e.g. an ``ApiError`` for a validation
        failure). That means any request whose handling raised
        anything gets its transaction rolled back, not just requests
        that produced a raw 500. ``db.session.remove()`` then discards
        the session so the next request starts with a clean one.
        """
        if exception is None:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
        else:
            db.session.rollback()
        db.session.remove()

    @app.route("/")
    def index_redirect():
        """Send "/" to the static frontend's entry page.

        ``static_url_path=""`` (above) makes Flask serve
        ``app/static/index.html`` at ``/index.html``, but Flask's
        static handling has no special case for "/" itself — without
        this view, "/" would 404.
        """
        return redirect("/index.html")

    # Each API module (app/api/auth.py, inventory.py, bom.py, ...) will
    # define a `bp` Blueprint; later phases append it to this list as
    # that module is built. Registering from a list (instead of N
    # separate app.register_blueprint(...) calls) keeps that growth to
    # one line per module. The import happens here, inside the factory,
    # rather than at module level: app.api.auth imports app.models (for
    # `User`), and importing every API module is also what guarantees
    # SQLAlchemy's metadata is fully populated by the time `flask db
    # migrate`/`db.create_all()` run, without app/__init__.py having to
    # import app.models itself.
    #
    # NOTE: imported as `from app.api.auth import bp as auth_bp`, not
    # `import app.api.auth` — the latter binds the local name `app`
    # (already bound a few lines up to this function's `Flask` instance)
    # to the top-level `app` *package* instead, silently shadowing it and
    # breaking every `app.register_blueprint(...)`/`app.route(...)` call
    # below for the rest of this function.
    from app.api.auth import bp as auth_bp
    from app.api.inventory import bp as inventory_bp
    from app.api.bom import bp as bom_bp
    from app.api.work_orders import bp as work_orders_bp
    from app.api.purchasing import bp as purchasing_bp
    from app.api.sales_orders import bp as sales_orders_bp

    blueprints = [
        auth_bp,
        inventory_bp,
        bom_bp,
        work_orders_bp,
        purchasing_bp,
        sales_orders_bp,
        # app.api.dashboard.bp,
    ]
    for bp in blueprints:
        app.register_blueprint(bp)

    return app
