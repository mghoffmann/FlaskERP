/**
 * purchase-orders.js — page logic for purchase-orders.html, the PO list.
 *
 * Follows the contract documented at the top of app.js: call initShell()
 * first and only render once it resolves with a user (a null return means
 * initShell() already redirected to /login.html — a bare top-level `return`
 * is a SyntaxError in an ES module, so we guard the rest of the file with
 * `if (user) { ... }` instead, per app.js's documented pattern).
 *
 * Status tabs + supplier filter: GET /api/purchase-orders (no query params)
 * is fetched exactly once, same fetch-once-and-filter-client-side pattern as
 * work-orders.js. Two filter dimensions this time (status tab, supplier
 * dropdown) rather than one; tab counts are computed against whichever
 * supplier is currently selected (not the unfiltered total) so the numbers
 * next to each tab always match what clicking it will show — a small
 * departure from work-orders.js (which only has one filter dimension, so
 * there's nothing for its counts to be scoped by). Both filters round-trip
 * through the query string via qs()/setQs() so the combined view stays
 * linkable/bookmarkable.
 */

import { initShell, api, toast, fmtMoney, fmtDate, qs, setQs, el } from "./app.js";

/** Status tabs in display order; "" means "All". Kept in one place so the
 * table filter and the tab-count computation agree with the markup. */
const STATUSES = ["", "draft", "ordered", "received", "canceled"];

const user = await initShell();
if (user) {
  main(user);
}

/**
 * @param {{id:number, username:string, role:string}} user
 */
function main(user) {
  const tbody = document.getElementById("po-tbody");
  const tabsNav = document.getElementById("status-tabs");
  const tabButtons = Array.from(tabsNav.querySelectorAll(".tab"));
  const supplierFilter = document.getElementById("supplier-filter");
  const newPoBtn = document.getElementById("new-po-btn");

  /** All purchase orders, fetched once; re-filtered per tab/supplier change. */
  let allItems = [];

  if (newPoBtn) {
    newPoBtn.addEventListener("click", () => {
      location.href = "/purchase-order.html";
    });
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      setQs({ status: btn.dataset.status || null });
      render();
    });
  }

  supplierFilter.addEventListener("change", () => {
    setQs({ supplier_id: supplierFilter.value || null });
    render();
  });

  /** Populates the supplier dropdown from GET /api/suppliers?active=all —
   * `all` rather than the default active-only so a PO placed with a supplier
   * that's since been deactivated can still be found by filtering the list
   * for it (the create-mode picker on purchase-order.html is the one that
   * must stay active-only, per 06-purchasing.md's acceptance criteria). */
  async function loadSupplierOptions() {
    let items;
    try {
      const data = await api("GET", "/api/suppliers?active=all");
      items = data.items;
    } catch {
      toast("Could not load suppliers.", "error");
      return;
    }

    const current = qs().supplier_id || "";
    for (const supplier of items) {
      const label = supplier.active ? supplier.name : `${supplier.name} (inactive)`;
      supplierFilter.appendChild(el("option", { value: String(supplier.id) }, [label]));
    }
    supplierFilter.value = current;
  }

  /** Recomputes tab counts/active state and the table from `allItems` and the
   * current `?status=&supplier_id=` query params — no network call. */
  function render() {
    const { status: currentStatus = "", supplier_id: currentSupplierId = "" } = qs();

    // Tab counts are scoped by the current supplier filter (see module doc
    // comment above), so apply that filter first...
    const supplierScoped = currentSupplierId
      ? allItems.filter((po) => String(po.supplier.id) === currentSupplierId)
      : allItems;

    for (const btn of tabButtons) {
      const status = btn.dataset.status;
      const count = status ? supplierScoped.filter((po) => po.status === status).length : supplierScoped.length;
      btn.querySelector(".tab-count").textContent = `(${count})`;
      btn.classList.toggle("-current", status === currentStatus);
      btn.setAttribute("aria-current", status === currentStatus ? "true" : "false");
    }

    // ...then narrow further by status for the table itself.
    const filtered = currentStatus ? supplierScoped.filter((po) => po.status === currentStatus) : supplierScoped;
    renderRows(filtered);
  }

  /** @param {Array<Object>} items - PO list items, shape per GET /api/purchase-orders. */
  function renderRows(items) {
    tbody.replaceChildren();

    if (!items.length) {
      tbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 7 }, ["No purchase orders match."])]));
      return;
    }

    for (const po of items) {
      const row = el(
        "tr",
        {
          dataset: { href: `/purchase-order.html?id=${po.id}` },
          onClick: () => {
            location.href = `/purchase-order.html?id=${po.id}`;
          },
        },
        [
          el("td", {}, [po.po_number]),
          el("td", {}, [po.supplier.name]),
          el("td", { class: "num" }, [String(po.line_count)]),
          el("td", { class: "num" }, [fmtMoney(po.total)]),
          el("td", {}, [el("span", { class: `badge -${po.status}` }, [capitalize(po.status)])]),
          el("td", {}, [fmtDate(po.created_at)]),
          el("td", {}, [po.received_at ? fmtDate(po.received_at) : "—"]),
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
      const data = await api("GET", "/api/purchase-orders");
      allItems = data.items;
    } catch {
      toast("Could not load purchase orders.", "error");
      return;
    }

    // No need to also set supplierFilter.value here: render() below reads
    // the supplier filter straight from qs(), not from the <select>'s
    // current value, and loadSupplierOptions() (fired alongside this
    // function, not awaited before it) sets the dropdown's displayed value
    // itself once its options exist — regardless of which of the two
    // in-flight fetches lands first.
    render();
  }

  loadSupplierOptions();
  loadAll();
}
