/**
 * work-orders.js — page logic for work-orders.html, the work order list.
 *
 * Follows the contract documented at the top of app.js: call initShell()
 * first and only render once it resolves with a user (a null return means
 * initShell() already redirected to /login.html — a bare top-level `return`
 * is a SyntaxError in an ES module, so we guard the rest of the file with
 * `if (user) { ... }` instead, per app.js's documented pattern).
 *
 * Status tabs: GET /api/work-orders (no `status` param) is fetched exactly
 * once; tab counts and the table are both derived from that single cached
 * array client-side — requirements/05-work-orders.md says "with counts" but
 * doesn't ask for a per-tab round trip, and at demo scale one fetch covering
 * every status is simpler and cheaper than five. The *chosen* tab still goes
 * through setQs() so `?status=released` is a linkable/bookmarkable view
 * (09-frontend.md's qs()/setQs() convention), it just doesn't trigger a
 * re-fetch — only a re-filter of the cached list.
 */

import { initShell, api, toast, fmtQty, fmtDate, qs, setQs, el } from "./app.js";
import { openFormModal } from "./modal.js";

/** Status tabs in display order; "" means "All". Kept in one place so the
 * table filter and the tab-count computation agree with the markup. */
const STATUSES = ["", "draft", "released", "completed", "canceled"];

const user = await initShell();
if (user) {
  main(user);
}

/**
 * @param {{id:number, username:string, role:string}} user
 */
function main(user) {
  const tbody = document.getElementById("wo-tbody");
  const tabsNav = document.getElementById("status-tabs");
  const tabButtons = Array.from(tabsNav.querySelectorAll(".tab"));
  const newWoBtn = document.getElementById("new-wo-btn");

  /** All work orders, fetched once; re-filtered per tab click. */
  let allItems = [];

  if (newWoBtn) {
    newWoBtn.addEventListener("click", () => openNewWoModal());
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      setQs({ status: btn.dataset.status || null });
      render();
    });
  }

  /** Opens the shared form modal for POST /api/work-orders, per
   * requirements/05-work-orders.md. The product picker only lists active
   * finished parts (the API also enforces this — UX convenience only). */
  async function openNewWoModal() {
    let products;
    try {
      const data = await api("GET", "/api/parts?part_type=finished");
      products = data.items;
    } catch {
      toast("Could not load products.", "error");
      return;
    }

    if (!products.length) {
      toast("No active finished products to build.", "error");
      return;
    }

    const result = await openFormModal({
      title: "New work order",
      submitLabel: "Create",
      fields: [
        {
          name: "product_part_id",
          label: "Product",
          type: "select",
          required: true,
          options: products.map((p) => ({ value: String(p.id), label: `${p.sku} — ${p.name}` })),
        },
        { name: "qty", label: "Qty", type: "number", step: "0.01", min: "0.01", required: true, value: 1 },
        { name: "notes", label: "Notes", type: "textarea" },
      ],
      onSubmit: (values) =>
        api("POST", "/api/work-orders", {
          product_part_id: Number(values.product_part_id),
          qty: Number(values.qty),
          notes: values.notes || undefined,
        }),
    });

    if (result) {
      toast("Work order created.", "ok");
      location.href = `/work-order.html?id=${result.id}`;
    }
  }

  /** Recomputes tab counts/active state and the table from `allItems` and the
   * current `?status=` query param — no network call. */
  function render() {
    const current = qs().status || "";

    for (const btn of tabButtons) {
      const status = btn.dataset.status;
      const count = status ? allItems.filter((wo) => wo.status === status).length : allItems.length;
      btn.querySelector(".tab-count").textContent = `(${count})`;
      btn.classList.toggle("-current", status === current);
      btn.setAttribute("aria-current", status === current ? "true" : "false");
    }

    const filtered = current ? allItems.filter((wo) => wo.status === current) : allItems;
    renderRows(filtered);
  }

  /** @param {Array<Object>} items - work order list items, shape per GET /api/work-orders. */
  function renderRows(items) {
    tbody.replaceChildren();

    if (!items.length) {
      tbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 6 }, ["No work orders match."])]));
      return;
    }

    for (const wo of items) {
      const row = el(
        "tr",
        {
          dataset: { href: `/work-order.html?id=${wo.id}` },
          onClick: () => {
            location.href = `/work-order.html?id=${wo.id}`;
          },
        },
        [
          el("td", {}, [wo.wo_number]),
          el("td", {}, [`${wo.product.sku} — ${wo.product.name}`]),
          el("td", { class: "num" }, [`${fmtQty(wo.qty)} ${wo.product.unit}`]),
          el("td", {}, [el("span", { class: `badge -${wo.status}` }, [capitalize(wo.status)])]),
          el("td", {}, [fmtDate(wo.created_at)]),
          el("td", {}, [wo.completed_at ? fmtDate(wo.completed_at) : "—"]),
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
      const data = await api("GET", "/api/work-orders");
      allItems = data.items;
    } catch {
      toast("Could not load work orders.", "error");
      return;
    }
    render();
  }

  loadAll();
}
