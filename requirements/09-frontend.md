# 09 — Frontend Conventions

Part of [Shopfloor ERP requirements](README.md). Individual pages are specified in their module docs; this doc defines what they share.

## Principles

- **Plain HTML + vanilla JS + one CSS file. No framework, no build step, no npm.** The repo should be readable by view-source. This mirrors the job posting's "lightweight HTML/JS operator UI."
- Multi-page app: each page is a real `.html` file under `app/static/`; navigation is normal links. JS progressively fills tables/forms via `fetch`.
- ES modules (`<script type="module">`), `const`/`let`, template literals, no globals beyond what `app.js` exports.

## Files

```
app/static/
  index.html  login.html  parts.html  part.html
  work-orders.html  work-order.html
  suppliers.html  purchase-orders.html  purchase-order.html
  customers.html  sales-orders.html  sales-order.html
  css/style.css
  js/app.js        # shared shell: auth bootstrap, nav injection, api(), helpers
  js/<page>.js     # one module per page with page logic
```

## Shared shell (`js/app.js`)

Every page except `login.html` does, on load:

1. `GET /api/auth/me`; on 401 → `location = "/login.html"`. The user object (with `role`) is cached in memory and exported.
2. Injects the shared nav into a `<header id="nav">` placeholder: app name (→ `/index.html`), links — Dashboard, Parts, Work Orders, Purchasing, Sales (Purchasing → purchase-orders.html; suppliers/customers are reachable from secondary links on those pages' toolbars) — current page highlighted; right side: `username (role)` + Logout.
3. Applies role gating: any element marked `data-role="admin"` is removed (not hidden) for operators. This is UX only; the API enforces for real ([02-auth.md](02-auth.md)).

Exported helpers:

- `api(method, path, body?)` — fetch wrapper: JSON headers, `credentials: "same-origin"`, parses JSON, throws an `ApiError {status, code, message, fieldErrors, details}` on non-2xx; a 401 anywhere redirects to login.
- `toast(message, kind)` — top-right transient notification, `kind` ∈ ok|error. Every successful mutating action shows an ok toast; unexpected errors show an error toast. **Never use `alert()`.**
- `fmtQty(n)`, `fmtMoney(n)`, `fmtDate(iso)`, `fmtDateTime(iso)` — consistent formatting everywhere (money 2 decimals; dates local time).
- `qs()` — query-string reader; list pages initialize filters from it and update it (`history.replaceState`) as filters change, so filtered views are linkable ([08-dashboard.md](08-dashboard.md) relies on this).
- `el(tag, attrs, children)` — small DOM-builder used for table rows; **no `innerHTML` with user data** (XSS discipline is part of the demo).

## UI conventions

- **Tables**: `<table>` with sticky header row, hover highlight, row click navigates where a detail page exists. Empty state = a single full-width muted row ("No parts match."). Numeric columns right-aligned.
- **Status badges**: colored pill per status — draft gray, released/ordered/confirmed blue, completed/received/shipped green, canceled strikethrough gray, low-stock/short warnings amber/red. One CSS class family (`.badge.-draft` etc.) shared by all modules.
- **Modals**: a shared `<dialog>`-based helper for create/edit forms and confirmations. Confirm dialogs state the consequence ("This will consume components…" — exact texts in module docs). Esc/backdrop closes; focus moves into the dialog.
- **Forms**: on 400, map `field_errors` to red messages under the matching inputs and `details` line errors to their grid rows; on 409, show the message as a page-level banner. Submit buttons disable while a request is in flight.
- **Line-item grids** (PO/SO/BOM editors): plain table with input rows, add-row button, per-row remove, computed line totals and a footer total that updates on input.
- **Part pickers**: `<input>` + `<datalist>` populated from `GET /api/parts` (filtered per context: active; finished-only where specified). At demo scale, loading all parts once per page is fine.

## Styling (`css/style.css`)

- CSS custom properties for the palette (background, surface, border, text, muted text, accent, ok/warn/danger); a clean neutral light theme — no dark mode (scope cut).
- System font stack; base 15–16px; content max-width ~1100px, centered; cards (surface + border + radius) for dashboard tiles and detail-page sections.
- Sensible focus states and 44px-minimum click targets on primary actions (operators may use touch screens — worth saying in the README).
- Target ≤ ~400 lines of CSS total. Resist utility-class sprawl; style semantic elements and a handful of component classes.

## Acceptance criteria

- With JS disabled, pages show the empty shell gracefully (no raw template junk); with JS on, no console errors on any page.
- Deep-linking works: `/work-order.html?id=3` renders directly after login; unknown id shows a "Not found" banner, not a blank page.
- An operator never sees an admin-only control anywhere in the DOM (removed, not `display:none`).
- The entire frontend passes a view-source sniff test: no framework, no build artifacts, no minified blobs.
