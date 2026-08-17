/**
 * sales-orders.js — page logic for sales-orders.html, the SO list.
 *
 * Follows the contract documented at the top of app.js: call initShell()
 * first and only render once it resolves with a user (a null return means
 * initShell() already redirected to /login.html — a bare top-level `return`
 * is a SyntaxError in an ES module, so we guard the rest of the file with
 * `if (user) { ... }` instead, per app.js's documented pattern).
 *
 * This is the sales-side twin of js/purchase-orders.js. Status tabs +
 * customer filter: GET /api/sales-orders (no query params) is fetched
 * exactly once, same fetch-once-and-filter-client-side pattern as
 * work-orders.js/purchase-orders.js. Tab counts are scoped by the currently
 * selected customer (not the unfiltered total) so the numbers next to each
 * tab always match what clicking it will show, mirroring purchase-orders.js's
 * reasoning. Both filters round-trip through the query string via
 * qs()/setQs() so the combined view stays linkable/bookmarkable.
 */

import { initShell, api, toast, fmtMoney, fmtDate, qs, setQs, el } from "./app.js";

/** Status tabs in display order; "" means "All". Kept in one place so the
 * table filter and the tab-count computation agree with the markup. */
const STATUSES = ["", "draft", "confirmed", "shipped", "canceled"];

const user = await initShell();
if (user) {
  main(user);
}

/**
 * @param {{id:number, username:string, role:string}} user
 */
function main(user) {
  const tbody = document.getElementById("so-tbody");
  const tabsNav = document.getElementById("status-tabs");
  const tabButtons = Array.from(tabsNav.querySelectorAll(".tab"));
  const customerFilter = document.getElementById("customer-filter");
  const newSoBtn = document.getElementById("new-so-btn");

  /** All sales orders, fetched once; re-filtered per tab/customer change. */
  let allItems = [];

  if (newSoBtn) {
    newSoBtn.addEventListener("click", () => {
      location.href = "/sales-order.html";
    });
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      setQs({ status: btn.dataset.status || null });
      render();
    });
  }

  customerFilter.addEventListener("change", () => {
    setQs({ customer_id: customerFilter.value || null });
    render();
  });

  /** Populates the customer dropdown from GET /api/customers?active=all —
   * `all` rather than the default active-only so an SO placed with a
   * customer that's since been deactivated can still be found by filtering
   * the list for it (the create-mode picker on sales-order.html is the one
   * that must stay active-only, mirroring purchase-orders.js). */
  async function loadCustomerOptions() {
    let items;
    try {
      const data = await api("GET", "/api/customers?active=all");
      items = data.items;
    } catch {
      toast("Could not load customers.", "error");
      return;
    }

    const current = qs().customer_id || "";
    for (const customer of items) {
      const label = customer.active ? customer.name : `${customer.name} (inactive)`;
      customerFilter.appendChild(el("option", { value: String(customer.id) }, [label]));
    }
    customerFilter.value = current;
  }

  /** Recomputes tab counts/active state and the table from `allItems` and the
   * current `?status=&customer_id=` query params — no network call. */
  function render() {
    const { status: currentStatus = "", customer_id: currentCustomerId = "" } = qs();

    // Tab counts are scoped by the current customer filter (see module doc
    // comment above), so apply that filter first...
    const customerScoped = currentCustomerId
      ? allItems.filter((so) => String(so.customer.id) === currentCustomerId)
      : allItems;

    for (const btn of tabButtons) {
      const status = btn.dataset.status;
      const count = status ? customerScoped.filter((so) => so.status === status).length : customerScoped.length;
      btn.querySelector(".tab-count").textContent = `(${count})`;
      btn.classList.toggle("-current", status === currentStatus);
      btn.setAttribute("aria-current", status === currentStatus ? "true" : "false");
    }

    // ...then narrow further by status for the table itself.
    const filtered = currentStatus ? customerScoped.filter((so) => so.status === currentStatus) : customerScoped;
    renderRows(filtered);
  }

  /** @param {Array<Object>} items - SO list items, shape per GET /api/sales-orders. */
  function renderRows(items) {
    tbody.replaceChildren();

    if (!items.length) {
      tbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 7 }, ["No sales orders match."])]));
      return;
    }

    for (const so of items) {
      const row = el(
        "tr",
        {
          dataset: { href: `/sales-order.html?id=${so.id}` },
          onClick: () => {
            location.href = `/sales-order.html?id=${so.id}`;
          },
        },
        [
          el("td", {}, [so.so_number]),
          el("td", {}, [so.customer.name]),
          el("td", { class: "num" }, [String(so.line_count)]),
          el("td", { class: "num" }, [fmtMoney(so.total)]),
          el("td", {}, [el("span", { class: `badge -${so.status}` }, [capitalize(so.status)])]),
          el("td", {}, [fmtDate(so.created_at)]),
          el("td", {}, [so.shipped_at ? fmtDate(so.shipped_at) : "—"]),
        ]
      );
      tbody.appendChild(row);
    }
  }

  function capitalize(s) {
    return s ? s[0].toUpperCase() + s.slice(1) : s;
  }

  async function loadAll() {
    try {
      const data = await api("GET", "/api/sales-orders");
      allItems = data.items;
    } catch {
      toast("Could not load sales orders.", "error");
      return;
    }

    // Initialize the customer dropdown from the query string once its
    // options exist (loadCustomerOptions() sets .value from qs() itself).
    customerFilter.value = qs().customer_id || "";
    render();
  }

  loadCustomerOptions();
  loadAll();
}
