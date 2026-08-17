"""The shared exception + envelope every API error response is built from.

Flask's default behaviour on an unhandled exception is to render an HTML
error page. 00-architecture.md requires that API consumers *never* see
HTML — every error, no matter where it originates, must come back as:

    {
      "error": {
        "code": "validation_error",
        "message": "Human-readable summary.",
        "field_errors": {"sku": "SKU already exists."},
        "details": []
      }
    }

with ``field_errors``/``details`` present only when there's something to
put in them. ``ApiError`` is the one exception every blueprint, service
function, or decorator in this codebase should raise to produce that
shape; ``handle_api_error`` is the Flask *error handler* — a function
Flask calls automatically whenever a matching exception type propagates
out of a view — that turns it into the actual HTTP response.

Usage from anywhere in the request-handling path (a route, a service
function called by a route, etc.)::

    from app.errors import ApiError

    part = Part.query.get(part_id)
    if part is None:
        raise ApiError(404, "not_found", "Part not found.")

    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.",
                        field_errors=field_errors)

No ``try/except`` is needed at the call site: ``app/__init__.py``
registers ``handle_api_error`` once, at app-creation time, with
``app.register_error_handler(ApiError, handle_api_error)``, and Flask
routes any ``ApiError`` raised during a request to it — including one
raised several calls deep inside a service module.
"""

from flask import jsonify


class ApiError(Exception):
    """An exception that carries everything needed to render one JSON error.

    Args:
        status: HTTP status code to respond with (400, 401, 403, 404,
            405, 409, 500, ...).
        code: Short machine-readable error code from the table in
            00-architecture.md (e.g. ``"validation_error"``,
            ``"not_found"``, ``"invalid_transition"``).
        message: Human-readable summary shown to API consumers.
        field_errors: Optional ``{field_name: message}`` dict, used for
            400 ``validation_error`` responses.
        details: Optional list of extra structured detail, e.g. the
            stock-shortfall rows on a 409 ``insufficient_stock`` error.
    """

    def __init__(self, status, code, message, field_errors=None, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field_errors = field_errors
        self.details = details

    def to_dict(self):
        """Build the ``{"error": {...}}`` envelope.

        ``field_errors`` and ``details`` are included only when
        truthy, per 00-architecture.md ("field_errors and details
        appear only when relevant") — a plain 404 stays a two-key
        error object instead of always carrying empty placeholders.
        """
        error = {"code": self.code, "message": self.message}
        if self.field_errors:
            error["field_errors"] = self.field_errors
        if self.details:
            error["details"] = self.details
        return {"error": error}


def handle_api_error(error):
    """Flask error handler: turns an ``ApiError`` into its JSON response.

    Registered in ``app/__init__.py`` via
    ``app.register_error_handler(ApiError, handle_api_error)``. Flask
    dispatches error handlers by exception type/status code, so this
    single function is what every ``raise ApiError(...)`` in the codebase
    ultimately becomes on the wire. It is also reused directly by the
    generic 400/401/403/404/405/409/500 handlers registered in
    ``app/__init__.py`` for errors Flask/Werkzeug raises on its own
    (unmatched routes, wrong HTTP verbs, bare ``abort(...)`` calls) —
    they build an ``ApiError`` with a default code/message and pass it
    through this same function, so every error response in the app goes
    through one code path.
    """
    response = jsonify(error.to_dict())
    response.status_code = error.status
    return response
