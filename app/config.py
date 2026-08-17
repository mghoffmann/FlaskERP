"""Configuration classes for Shopfloor ERP, one per environment.

Flask apps read settings from a *config object*: a plain class whose
uppercase attributes become ``app.config`` keys once you call
``app.config.from_object(...)`` on it. Using classes — one per
environment, sharing a common ``Config`` base through inheritance —
means the app factory can pick the right settings with a single string
("development" / "testing" / "production") instead of a pile of
``if ENV == ...`` checks scattered through the codebase, and each
environment's *differences* from the base are the only thing visible
in its class body.

Values that must differ per environment (database URL, secret key,
whether the session cookie requires HTTPS) are read from environment
variables, so the exact same code runs unmodified on a laptop, in CI,
and in the production container — only the environment differs.
"""

import os


class Config:
    """Base configuration: defaults shared by every environment.

    Subclasses override individual attributes; anything they don't
    override keeps the default defined here.
    """

    #: Signs session cookies. Every environment should provide a real
    #: value via the ``SECRET_KEY`` env var; the base class falls back
    #: to an obviously-fake default so `create_app()` never crashes
    #: just because *development* forgot to set one.
    #: ``ProductionConfig`` below refuses to start with this fallback.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-key")

    #: The database connection string, e.g.
    #: ``postgresql+psycopg://erp:erp@db:5432/erp``. SQLAlchemy
    #: connects *lazily* — setting this URI does not open a
    #: connection; the first query does. That laziness is what lets
    #: ``create_app("testing")`` succeed even with no database
    #: reachable (see AGENTS.md verification step).
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")

    #: Disables a Flask-SQLAlchemy change-tracking signal system this
    #: app never uses; leaving it on costs memory for nothing.
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- Session cookie settings (00-architecture.md) -------------------
    # Flask's default session is a signed cookie (no server-side session
    # store to run/manage) — the settings below only change how the
    # browser is told to handle that cookie.

    #: JavaScript cannot read the cookie via ``document.cookie``, which
    #: limits the damage an XSS bug could do (no session-token theft).
    SESSION_COOKIE_HTTPONLY = True

    #: "Lax" sends the cookie on top-level navigation but not on
    #: cross-site subrequests (e.g. an ``<img>`` or fetch from another
    #: origin). Combined with this being a JSON-only API, that is what
    #: lets 00-architecture.md skip CSRF tokens entirely — a
    #: cross-site page cannot trigger an authenticated JSON request
    #: that carries the cookie.
    SESSION_COOKIE_SAMESITE = "Lax"

    #: Requires HTTPS before the browser will send the cookie back.
    #: Left off by default because a plain-HTTP local dev server could
    #: never receive it if this were True everywhere.
    #: ``ProductionConfig`` turns it on.
    SESSION_COOKIE_SECURE = False


class DevelopmentConfig(Config):
    """Used by `flask run --debug` against the docker-compose dev database.

    ``DEBUG = True`` enables the interactive debugger and auto-reload;
    it must never be set in production (it can leak source code and
    let visitors execute arbitrary Python through the debugger).
    """

    DEBUG = True


class TestingConfig(Config):
    """Used by the pytest suite (see 10-testing.md).

    Points at a dedicated ``erp_test`` database — never the dev
    database — so tests can freely create, mutate, and drop data
    without touching anything a developer is looking at.
    ``TESTING = True`` tells Flask this is a test context, which
    (among other things) lets extensions and error handling behave in
    ways more useful for assertions than for a real deployment.
    """

    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://erp:erp@localhost:5432/erp_test",
    )


class ProductionConfig(Config):
    """Used by the deployed container.

    Enforces 00-architecture.md's "the app refuses to start in
    production without a real SECRET_KEY" rule: if ``SECRET_KEY`` was
    never set, or was left as the development placeholder, ``__init__``
    raises immediately rather than quietly signing production session
    cookies with a key anyone reading this file could guess.

    This check only fires when the class is *instantiated*
    (``ProductionConfig()``), not merely referenced — see
    ``create_app()`` in ``app/__init__.py``, which instantiates the
    selected config class specifically so this runs at startup.
    """

    #: Production is served over HTTPS (Caddy terminates TLS — see
    #: 11-deployment.md), so the browser can be required to withhold
    #: the cookie over plain HTTP.
    SESSION_COOKIE_SECURE = True

    def __init__(self):
        if not self.SECRET_KEY or self.SECRET_KEY == "dev-insecure-secret-key":
            raise RuntimeError(
                "SECRET_KEY environment variable must be set to a real "
                "secret value in production (refusing to start with the "
                "development placeholder)."
            )


#: Maps the short name used by the ``FLASK_CONFIG`` env var / the
#: ``create_app(config_name)`` argument to the class that implements
#: it. ``create_app()`` looks up this dict rather than branching on
#: strings directly, so adding a new environment later is a one-line
#: addition here.
config = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
