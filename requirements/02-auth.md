# 02 — Authentication & Roles

Part of [Shopfloor ERP requirements](README.md). Conventions in [00-architecture.md](00-architecture.md).

## Concept

Two roles model a real shop floor:

- **admin** — the planner/manager: manages master data (parts, BOMs, suppliers, customers), creates and edits documents, and can do everything an operator can.
- **operator** — the factory-floor user: sees everything, and performs the physical-world confirmations — completing work orders, receiving purchase orders, shipping sales orders, and adjusting stock counts. Operators cannot create or edit master data or documents.

Auth is a signed session cookie (Flask's built-in session). Passwords are hashed with Werkzeug's `generate_password_hash` / `check_password_hash`. There is **no user registration and no user CRUD UI** — the two users come from seed data ([01-database.md](01-database.md)). This is a deliberate scope cut; note it in the repo README.

## Implementation requirements

- `require_login(role=None)` decorator in `app/api/auth.py`:
  - No valid session → `401 unauthenticated`.
  - `role="admin"` and session user is not admin → `403 forbidden`.
  - Loads the current user onto `flask.g.user` for handlers (e.g. stock movements record `user_id`).
- Session stores only the user id; the user row is fetched per request (a deleted/changed user invalidates cleanly).
- No lockout/rate limiting (demo scope; note in README).

## Endpoints

### POST /api/auth/login
- Auth: none.
- Request: `{"username": "admin", "password": "..."}`
- 200: `{"user": {"id": 1, "username": "admin", "role": "admin"}}` and sets the session cookie.
- 401 `unauthenticated` with message "Invalid username or password." for unknown user *or* wrong password (identical response — don't leak which).
- 400 `validation_error` if fields missing.

### POST /api/auth/logout
- Auth: any. Clears the session. 204 empty.

### GET /api/auth/me
- Auth: none required, but: with a valid session returns `{"user": {...}}` (same shape as login); without one returns 401. The frontend calls this on every page load ([09-frontend.md](09-frontend.md)).

## Pages

### /login.html
- Centered card: app title, username + password fields, submit button, error line.
- Submit calls `POST /api/auth/login`; on success redirect to `/index.html`; on 401 show the error message inline (no alert boxes).
- The shared shell redirects any other page to `/login.html` when `/api/auth/me` returns 401, and `/login.html` redirects to `/index.html` if already logged in.
- Nav (on all other pages) shows the logged-in username + role and a Logout button (`POST /api/auth/logout`, then redirect to login).

## Role matrix (summary — each module doc is authoritative)

| Action | operator | admin |
|---|---|---|
| View everything (GET endpoints) | ✔ | ✔ |
| Stock adjustments | ✔ | ✔ |
| Complete WO / receive PO / ship SO | ✔ | ✔ |
| Create/edit/cancel documents; release WO; place PO; confirm SO | — | ✔ |
| Create/edit/deactivate parts, BOMs, suppliers, customers | — | ✔ |

The frontend hides admin-only buttons for operators, but the API enforces roles regardless (hiding is UX, not security).

## Acceptance criteria

- Every `/api/*` endpoint except login and me returns 401 without a session (verified by a pytest sweep, see [10-testing.md](10-testing.md)).
- An operator session gets 403 (not 404, not 500) on every admin-only endpoint.
- Login as operator, then as admin, in two browser tabs of different browsers: sessions are independent.
- Cookies are HttpOnly and, in production, Secure.
