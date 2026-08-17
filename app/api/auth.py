"""``/api/auth/*`` — login, logout, "who am I", and the ``require_login`` decorator.

Session model (02-auth.md): Flask's session is a *signed cookie* — the
browser holds ``{"user_id": 3}`` (plus an HMAC signature keyed by
``SECRET_KEY``) and sends it back on every request. "Signed" means the
server never stores session state anywhere (no session table, no Redis);
it just verifies the signature matches before trusting the cookie's
content. That also means the cookie can be *read* by anyone (it's not
encrypted, only tamper-proof), which is exactly why it stores nothing
more sensitive than a user id — never a password or role. The user's
*current* role is looked up fresh from the database on every request (see
``require_login`` below), so revoking/changing a user takes effect on
their very next request instead of only after their session expires.
"""

import functools

from flask import Blueprint, g, jsonify, session
from werkzeug.security import check_password_hash, generate_password_hash

from app.api import get_json_or_400
from app.errors import ApiError
from app.extensions import db
from app.models import User

#: Blueprint for every ``/api/auth/*`` route. Registered in
#: ``app/__init__.py``'s ``create_app()``.
bp = Blueprint("auth", __name__, url_prefix="/api/auth")

#: A password hash for a password nobody has, computed once at import
#: time. ``check_password_hash`` does real cryptographic work (it's
#: deliberately slow — see below), so it takes measurably longer than
#: "user not found, return immediately." Without this, an attacker could
#: send many login attempts and use *response timing* (not response
#: content) to tell "unknown username" apart from "known username, wrong
#: password" — a side channel around the "identical response body" rule
#: below. Running a real ``check_password_hash`` against this dummy hash
#: on the unknown-user path keeps the timing profile close to the
#: known-user path.
_DUMMY_PASSWORD_HASH = generate_password_hash("not-a-real-password")


def _user_dict(user):
    """Serialize a :class:`~app.models.User` to the ``{"user": {...}}`` shape.

    Shared by ``POST /login`` and ``GET /me`` because 02-auth.md requires
    both to return "the same shape" — defining that shape once here means
    they cannot silently drift apart. Deliberately excludes
    ``password_hash``: this is the *only* representation of a user this
    API ever sends over the wire.
    """
    return {"id": user.id, "username": user.username, "role": user.role}


def require_login(role=None):
    """Decorator factory: require a valid session, optionally a specific role.

    **Why a function that returns a decorator, instead of a plain
    decorator?** Compare the two call shapes a decorator can support:

    - ``@some_decorator`` — Python calls ``some_decorator(view_func)``
      directly. One layer.
    - ``@some_decorator(role="admin")`` — the thing after ``@`` must
      itself be a *call* that returns something usable as a decorator.
      Python first evaluates ``some_decorator(role="admin")``, then
      applies whatever that returned to ``view_func``.

    This route table needs both ``@require_login()`` (any logged-in user)
    and ``@require_login(role="admin")`` (admin only), which means
    ``require_login`` can't be a two-layer ``@wraps``-decorator itself —
    it needs a **three-layer closure**:

    1. ``require_login(role=None)`` — the outer function, called
       immediately when the module is imported (at ``@require_login()``
       or ``@require_login(role="admin")`` decoration time, *not* per
       request). It captures ``role`` in a closure and returns...
    2. ``decorator(view_func)`` — the actual decorator, called once per
       decorated view function, immediately after step 1, with the
       view function itself. It captures both ``role`` (from the outer
       closure) and ``view_func``, and returns...
    3. ``wrapper(*args, **kwargs)`` — the function that actually replaces
       the view in Flask's URL map. *This* is what runs on every
       request; everything above it ran once, at import/decoration time.

    ``functools.wraps(view_func)`` on ``wrapper`` copies ``view_func``'s
    ``__name__`` (and other metadata) onto ``wrapper``. Flask uses the
    function's ``__name__`` as the route's internal endpoint name unless
    ``@bp.route(...)`` is given an explicit ``endpoint=``; without
    ``wraps``, every view decorated with ``@require_login()`` would
    register under the endpoint name ``"wrapper"`` and Flask would raise
    on the second one for colliding with the first.

    **``flask.g``**: a namespace Flask resets at the start of every
    request and discards at the end — a place to stash per-request state
    (here, the loaded :class:`~app.models.User`) that any code handling
    *this* request can read, without threading an extra parameter through
    every function call. Route handlers read the logged-in user as
    ``flask.g.user`` (e.g. stock movements record ``g.user.id``).

    Args:
        role: ``None`` to require only a valid session (any role), or
            ``"admin"`` to additionally require the session user's role
            to be ``"admin"``.

    Returns:
        A decorator to apply to a Flask view function.
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(*args, **kwargs):
            user_id = session.get("user_id")
            if user_id is None:
                raise ApiError(401, "unauthenticated", "Authentication required.")

            # The session stores only the id (module docstring above) —
            # the row is fetched fresh on every request. If the user was
            # deleted since the cookie was issued, `db.session.get`
            # returns None here rather than raising, so a stale session
            # fails cleanly as "not authenticated" instead of a 500.
            user = db.session.get(User, user_id)
            if user is None:
                session.clear()
                raise ApiError(401, "unauthenticated", "Authentication required.")

            if role == "admin" and user.role != "admin":
                raise ApiError(
                    403,
                    "forbidden",
                    "You do not have permission to perform this action.",
                )

            g.user = user
            return view_func(*args, **kwargs)

        return wrapper

    return decorator


@bp.route("/login", methods=["POST"])
def login():
    """``POST /api/auth/login`` — exchange username/password for a session cookie.

    Auth: none (this is how a session is obtained in the first place).
    """
    data = get_json_or_400()
    username = data.get("username")
    password = data.get("password")

    field_errors = {}
    if not isinstance(username, str) or not username.strip():
        field_errors["username"] = "Username is required."
    if not isinstance(password, str) or not password:
        field_errors["password"] = "Password is required."
    if field_errors:
        raise ApiError(400, "validation_error", "Invalid input.", field_errors=field_errors)

    user = User.query.filter_by(username=username).first()
    if user is not None:
        password_ok = check_password_hash(user.password_hash, password)
    else:
        # Burn roughly the same amount of time as a real check so the
        # response timing doesn't reveal whether `username` exists — see
        # _DUMMY_PASSWORD_HASH above.
        check_password_hash(_DUMMY_PASSWORD_HASH, password)
        password_ok = False

    # Unknown user and "known user, wrong password" return the exact same
    # status/code/message. Distinguishing them (e.g. "no such user" vs.
    # "wrong password") would let an attacker enumerate valid usernames
    # one guess at a time — the response must not leak which case
    # happened, only *that* the credentials were invalid.
    if user is None or not password_ok:
        raise ApiError(401, "unauthenticated", "Invalid username or password.")

    # session.clear() before setting the new user_id: if a *different*
    # user was previously logged in on this browser/cookie (e.g. someone
    # logged out and back in as someone else without the cookie ever
    # being deleted), this guarantees no leftover session state survives
    # the switch.
    session.clear()
    session["user_id"] = user.id
    return jsonify({"user": _user_dict(user)}), 200


@bp.route("/logout", methods=["POST"])
@require_login()
def logout():
    """``POST /api/auth/logout`` — clear the session.

    Auth: any (must be logged in to log out).
    """
    session.clear()
    return "", 204


@bp.route("/me", methods=["GET"])
@require_login()
def me():
    """``GET /api/auth/me`` — the currently logged-in user, if any.

    02-auth.md: "no role required" — ``@require_login()`` with no
    ``role=`` argument is exactly that: any valid session passes, only a
    *missing* session is rejected. The frontend's page shell calls this
    on every page load to decide whether to redirect to ``/login.html``.
    """
    return jsonify({"user": _user_dict(g.user)}), 200
