"""Flask extension instances, created once and shared across the app.

Why do these live in their own module instead of ``app/__init__.py``?
Flask's *application factory* pattern (see ``create_app()`` in
``app/__init__.py``) builds the real ``Flask`` app object lazily, inside a
function, rather than at import time. But ``app/models.py`` needs an
object to subclass (``db.Model``) and to attach columns to, and it needs
that object the moment it is imported — long before ``create_app()`` is
ever called.

So the extension instances below are created *unbound*: ``SQLAlchemy()``
and ``Migrate()`` with no app passed in. They only start doing anything
once ``create_app()`` calls ``db.init_app(app)`` / ``migrate.init_app(app,
db)``. This split matters for avoiding circular imports: if ``db`` were
defined inside ``app/__init__.py``, then ``app/models.py`` would need to
``import app`` to get it, and ``app/__init__.py`` will eventually import
blueprints that import ``app/models.py`` — a cycle. Every module (models,
blueprints, the factory itself) can safely ``from app.extensions import
db`` with nothing importing ``app/__init__.py`` in return.
"""

from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

#: The SQLAlchemy extension. Unbound until ``db.init_app(app)`` runs
#: inside ``create_app()``. Every model in ``app/models.py`` subclasses
#: ``db.Model``; every request handler reads/writes through
#: ``db.session``.
db = SQLAlchemy()

#: The Flask-Migrate extension (a thin wrapper around Alembic). Unbound
#: until ``migrate.init_app(app, db)`` runs inside ``create_app()``. This
#: is what makes the ``flask db init/migrate/upgrade`` CLI commands work
#: against ``db``'s models.
migrate = Migrate()
