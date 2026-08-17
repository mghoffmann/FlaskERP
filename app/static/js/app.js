/**
 * app.js — the shared frontend shell for Shopfloor ERP.
 *
 * CONTRACT FOR PAGE AUTHORS (read this before writing a new <page>.js):
 *
 *   1. Every page except login.html includes `<header id="nav"></header>` somewhere
 *      in its markup (usually right after <body>) as a placeholder for the shared nav.
 *   2. Every page except login.html includes, at the end of <body>:
 *        <script type="module" src="/js/<page>.js"></script>
 *      and that module's FIRST action must be (note: a bare `return` is a
 *      SyntaxError at the top level of an ES module, so guard with `if`):
 *        import { initShell } from './app.js';
 *        const user = await initShell();
 *        if (user) {
 *          // ...render the page (or call your main(user) function)...
 *        } // else: initShell() already redirected to /login.html
 *      `initShell()` does the auth check, fills in #nav, and applies role gating.
 *      Only render the rest of the page after it resolves — this avoids a flash of
 *      content that a 401 redirect would immediately blow away.
 *   3. login.html does NOT call initShell() (there is no session to check yet, and
 *      no nav to render on the login page). See login.js for its own bootstrap.
 *   4. Everything a page needs from the shell — the api() fetch wrapper, toast(),
 *      the fmt* formatters, qs()/setQs(), and the el() DOM builder — is imported
 *      from this module. Nothing here is attached to `window`; ES modules give us
 *      real scoping instead of a global-namespace free-for-all.
 *
 * Why no framework? See requirements/09-frontend.md: this is a plain HTML/JS/CSS
 * demo meant to be readable by view-source, with no build step.
 */

/** Path the shell redirects to whenever there is no valid session. */
const LOGIN_PATH = "/login.html";

/** Nav links shown on every shell page, in display order. `href` doubles as the
 * "current page" match key (compared against `location.pathname`). */
const NAV_LINKS = [
  { label: "Dashboard", href: "/index.html" },
  { label: "Parts", href: "/parts.html" },
  { label: "Work Orders", href: "/work-orders.html" },
  { label: "Purchasing", href: "/purchase-orders.html" },
  { label: "Sales", href: "/sales-orders.html" },
];

/** The logged-in user, cached after initShell() resolves. `{id, username, role}`. */
let currentUser = null;

// ---------------------------------------------------------------------------
// el() — safe DOM builder
// ---------------------------------------------------------------------------

/**
 * Builds a DOM element without ever touching `innerHTML`.
 *
 * WHY: `innerHTML = someString` parses `someString` as HTML — if any part of it
 * came from user input (a part name, a customer note, a SKU) a string like
 * `<img src=x onerror=alert(1)>` would execute as script. This is the classic
 * stored-XSS bug. `el()` instead calls `document.createElement` and
 * `document.createTextNode` directly, so a value like that is inserted as
 * literal, inert text — there is no HTML parser in the path at all. Every page
 * module in this app builds table rows and form fields with `el()` (or sets
 * `.textContent`) instead of `innerHTML`, by rule.
 *
 * @param {string} tag - element tag name, e.g. "div", "tr", "button".
 * @param {Object<string, *>} [attrs] - attributes/properties to apply.
 *   - `class` sets className.
 *   - `dataset: {foo: "bar"}` sets `data-foo="bar"`.
 *   - `onClick`, `onInput`, etc. (any `on<Event>` key mapped to a function)
 *     are wired up with addEventListener (lowercased event name).
 *   - Anything that already exists as a property on the element (`value`,
 *     `checked`, `type`, `href`, `hidden`, ...) is set as a property.
 *   - Everything else falls back to `setAttribute` (e.g. `for`, `aria-*`).
 *   - `null`/`undefined`/`false` values are skipped, so callers can write
 *     `{ required: isRequired && true }` without an extra branch.
 * @param {Array<Node|string|null|false>|Node|string} [children] - child nodes.
 *   Strings become text nodes (never parsed as HTML); `null`/`false` are skipped
 *   so callers can inline conditionals like `cond && el(...)`.
 * @returns {HTMLElement}
 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);

  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") {
      node.className = value;
    } else if (key === "dataset") {
      for (const [dataKey, dataValue] of Object.entries(value)) {
        node.dataset[dataKey] = dataValue;
      }
    } else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in node) {
      node[key] = value;
    } else {
      node.setAttribute(key, value);
    }
  }

  const kids = Array.isArray(children) ? children : [children];
  for (const child of kids) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  return node;
}

// ---------------------------------------------------------------------------
// api() — fetch wrapper + ApiError
// ---------------------------------------------------------------------------

/**
 * Error thrown by api() for any non-2xx response. Mirrors the error envelope
 * documented in requirements/00-architecture.md:
 *   {"error": {"code", "message", "field_errors", "details"}}
 */
export class ApiError extends Error {
  /**
   * @param {number} status - HTTP status code (0 for a network failure).
   * @param {string} [code] - machine-readable error code, e.g. "validation_error".
   * @param {string} [message] - human-readable summary, safe to show to the user.
   * @param {Object<string,string>} [fieldErrors] - field name -> message, present
   *   on 400 validation_error responses.
   * @param {Array} [details] - extra structured detail (e.g. stock shortfalls).
   */
  constructor(status, code, message, fieldErrors, details) {
    super(message || "Request failed.");
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fieldErrors = fieldErrors || {};
    this.details = details || [];
  }
}

/**
 * JSON fetch wrapper used for every API call in the app.
 *
 * - Sends `credentials: "same-origin"` so the session cookie goes along.
 * - Serializes `body` as JSON and sets the matching Content-Type; GETs (no body)
 *   send no body/content-type at all.
 * - A 204 response (logout, etc.) resolves to `null`.
 * - Any non-2xx response is thrown as an ApiError built from the response's
 *   `{error: {...}}` envelope.
 * - A 401 from ANY call, on ANY page except login.html, means the session died
 *   (expired, logged out elsewhere) — we redirect to /login.html immediately
 *   rather than making every caller handle it. login.html is exempt because its
 *   own "am I already logged in?" check (see login.js) is expected to 401 for a
 *   fresh visitor, and that's not an error condition there.
 *
 * @param {string} method - "GET", "POST", "PATCH", "DELETE", etc.
 * @param {string} path - API path, e.g. "/api/parts".
 * @param {*} [body] - request body; omit for GET/DELETE with no payload.
 * @returns {Promise<*>} parsed JSON body, or null for 204 responses.
 * @throws {ApiError}
 */
export async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError(0, "network_error", "Could not reach the server.");
  }

  if (res.status === 204) return null;

  let payload = null;
  try {
    payload = await res.json();
  } catch {
    payload = null;
  }

  if (res.ok) return payload;

  if (res.status === 401 && !location.pathname.endsWith(LOGIN_PATH)) {
    location.href = LOGIN_PATH;
  }

  const errBody = (payload && payload.error) || {};
  throw new ApiError(res.status, errBody.code, errBody.message, errBody.field_errors, errBody.details);
}

// ---------------------------------------------------------------------------
// toast() — transient notifications (never alert())
// ---------------------------------------------------------------------------

let toastContainer = null;

function ensureToastContainer() {
  if (!toastContainer) {
    toastContainer = el("div", { class: "toast-container", "aria-live": "polite" });
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

/**
 * Shows a transient top-right notification. Use this for every success/error
 * message in the app — `alert()` is banned (it's blocking and looks broken on
 * a shop-floor touch screen).
 *
 * @param {string} message
 * @param {"ok"|"error"} [kind="ok"]
 */
export function toast(message, kind = "ok") {
  const container = ensureToastContainer();
  const node = el("div", { class: `toast toast-${kind}` }, [message]);
  container.appendChild(node);
  setTimeout(() => node.remove(), 3500);
}

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

/** Formats a quantity: locale thousands separators, up to 2 decimals, no
 * trailing zeros forced (so an integer quantity reads as "120", not "120.00"). */
export function fmtQty(n) {
  const num = Number(n);
  if (Number.isNaN(num)) return "";
  return num.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/** Formats a money amount with exactly 2 decimals and a $ prefix (demo assumes
 * a single currency; a production system would store/format string decimals). */
export function fmtMoney(n) {
  const num = Number(n);
  if (Number.isNaN(num)) return "";
  return "$" + num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Formats an ISO-8601 UTC timestamp as a local date, e.g. "Aug 17, 2026". */
export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** Formats an ISO-8601 UTC timestamp as a local date + time, e.g. "Aug 17, 2026, 2:30 PM". */
export function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// qs() / setQs() — linkable filter state
// ---------------------------------------------------------------------------

/**
 * Reads the current query string into a plain object, e.g. `?status=open&q=bolt`
 * becomes `{status: "open", q: "bolt"}`. List pages call this once on load to
 * initialize filter controls from the URL.
 * @returns {Object<string,string>}
 */
export function qs() {
  return Object.fromEntries(new URLSearchParams(location.search));
}

/**
 * Merges `updates` into the current query string and pushes the result via
 * `history.replaceState` (no new history entry, no page reload) so the current
 * filtered view stays linkable/bookmarkable. A value of `null`, `undefined`,
 * or `""` removes that key instead of setting it.
 * @param {Object<string, string|number|null|undefined>} updates
 */
export function setQs(updates) {
  const params = new URLSearchParams(location.search);
  for (const [key, value] of Object.entries(updates)) {
    if (value === null || value === undefined || value === "") {
      params.delete(key);
    } else {
      params.set(key, String(value));
    }
  }
  const query = params.toString();
  const newUrl = location.pathname + (query ? `?${query}` : "") + location.hash;
  history.replaceState(null, "", newUrl);
}

// ---------------------------------------------------------------------------
// Shell bootstrap: auth check, nav injection, role gating
// ---------------------------------------------------------------------------

function isCurrentPage(href) {
  return location.pathname === href;
}

async function handleLogout() {
  try {
    await api("POST", "/api/auth/logout");
  } catch {
    // Even if the logout call fails (e.g. session already gone), still send
    // the user back to the login page — that's the desired end state either way.
  }
  location.href = LOGIN_PATH;
}

function renderNav(user) {
  const header = document.getElementById("nav");
  if (!header) return;

  const brand = el("a", { class: "nav-brand", href: "/index.html" }, ["Shopfloor ERP"]);

  const links = el(
    "nav",
    { class: "nav-links" },
    NAV_LINKS.map((link) =>
      el(
        "a",
        {
          href: link.href,
          class: isCurrentPage(link.href) ? "nav-link -current" : "nav-link",
          "aria-current": isCurrentPage(link.href) ? "page" : null,
        },
        [link.label]
      )
    )
  );

  const who = el("span", { class: "nav-user" }, [`${user.username} (${user.role})`]);
  const logoutBtn = el("button", { type: "button", class: "btn btn-ghost", onClick: handleLogout }, ["Logout"]);
  const right = el("div", { class: "nav-right" }, [who, logoutBtn]);

  header.replaceChildren(brand, links, right);
}

/**
 * Removes (never just hides) every `[data-role="admin"]` element for a
 * non-admin user. This is UX polish only — the API enforces roles for real
 * (requirements/02-auth.md) — but `.remove()` rather than `display:none` means
 * an operator can't find the control by poking around devtools either.
 */
function applyRoleGating(user) {
  if (user.role === "admin") return;
  document.querySelectorAll('[data-role="admin"]').forEach((node) => node.remove());
}

/**
 * Boots the shared shell. Call this first thing in every page module except
 * login.js, and await it before doing anything else:
 *
 *   const user = await initShell();
 *   if (!user) return; // already redirected to /login.html
 *
 * Steps: GET /api/auth/me (a 401 is handled inside api() itself, which
 * redirects to /login.html — this function just needs to stop and return null
 * in that case so the caller doesn't go on to render a page with no user),
 * cache the user, render the nav into `<header id="nav">`, and strip
 * admin-only elements for operators.
 *
 * @returns {Promise<{id:number, username:string, role:string}|null>}
 */
export async function initShell() {
  let data;
  try {
    data = await api("GET", "/api/auth/me");
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      // api() already kicked off the redirect to /login.html.
      return null;
    }
    throw err;
  }

  currentUser = data.user;
  renderNav(currentUser);
  applyRoleGating(currentUser);
  return currentUser;
}

/** Returns the cached user set by initShell(), or null before it resolves. */
export function getUser() {
  return currentUser;
}
