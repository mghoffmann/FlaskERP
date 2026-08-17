/**
 * index.js — page logic for index.html, the post-login dashboard.
 *
 * Follows the contract documented at the top of app.js: call initShell()
 * first and only render once it resolves with a user (a null return means
 * initShell() already redirected to /login.html — a bare top-level `return`
 * is a SyntaxError in an ES module, so the whole page body is wrapped in
 * `if (user) { ... }` instead of returning early).
 *
 * requirements/08-dashboard.md is explicit that this page "renders from a
 * single API call, in one paint (no per-tile spinners)": exactly one
 * `GET /api/dashboard` call feeds the four KPI tiles and all three tables
 * below, all painted from the same response in one pass.
 */

import { initShell, api, toast, fmtQty, fmtDateTime, el } from "./app.js";

/** Human-readable labels for stock_movements.reason values — same mapping
 * as js/part.js's movements ledger, kept in sync by convention (both read
 * the same `reason` CHECK-constrained values from app/models.py). */
const REASON_LABELS = {
  adjustment: "Adjustment",
  wo_consume: "WO consume",
  wo_produce: "WO produce",
  po_receive: "PO receive",
  so_ship: "SO ship",
};

function humanizeReason(reason) {
  return REASON_LABELS[reason] || String(reason || "").replace(/_/g, " ");
}

function capitalize(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/**
 * Maps a document-number prefix ("WO"/"PO"/"SO") to its detail page.
 *
 * The dashboard's `recent_movements` rows (unlike
 * `GET /api/parts/{id}/movements`, which js/part.js links from) carry only
 * `ref_number` — 08-dashboard.md's aggregate shape deliberately doesn't
 * include the raw `ref_type`/`ref_id` a movement points at, since the
 * dashboard is a read-only summary, not the ledger's own detail view. Every
 * document number is generated as `"<PREFIX>-{id:04d}"` (see
 * app/api/work_orders.py's `create_work_order`, purchasing.py's
 * `place_purchase_order`... equivalents), so the id a detail page needs is
 * recoverable by parsing the same prefix + zero-padded id back out of the
 * number the API already sent — no extra field needed on the wire.
 */
const REF_PAGES_BY_PREFIX = {
  WO: "/work-order.html",
  PO: "/purchase-order.html",
  SO: "/sales-order.html",
};

/** @param {string|null} refNumber - e.g. "WO-0007", or null for a plain adjustment. */
function refLink(refNumber) {
  if (!refNumber) return null;
  const match = /^([A-Z]{2})-0*(\d+)$/.exec(refNumber);
  if (!match) return null;
  const page = REF_PAGES_BY_PREFIX[match[1]];
  if (!page) return null;
  return `${page}?id=${Number(match[2])}`;
}

/**
 * Formats a short "how long ago" age string from an ISO-8601 timestamp,
 * e.g. "3d" / "5h" / "12m" / "just now" — compact enough for a table
 * column, unlike fmtDate()/fmtDateTime() which spell out a full date.
 * @param {string} iso
 * @returns {string}
 */
function fmtAge(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const minutes = Math.floor((Date.now() - then) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

const user = await initShell();
if (user) {
  main(user);
}

/**
 * @param {{id:number, username:string, role:string}} user
 */
function main(user) {
  const lowStockTile = document.getElementById("kpi-low-stock");
  const lowStockValueEl = document.getElementById("kpi-low-stock-value");
  const openWoValueEl = document.getElementById("kpi-open-wo-value");
  const poTransitValueEl = document.getElementById("kpi-po-transit-value");
  const soShipValueEl = document.getElementById("kpi-so-ship-value");

  const lowStockTbody = document.getElementById("low-stock-tbody");
  const openWoTbody = document.getElementById("open-wo-tbody");
  const recentTbody = document.getElementById("recent-tbody");

  /** Renders every tile + table from one GET /api/dashboard payload — the
   * single paint requirements/08-dashboard.md asks for. */
  function render(data) {
    renderKpis(data.counts);
    renderLowStock(data.low_stock);
    renderOpenWorkOrders(data.open_work_orders);
    renderRecentActivity(data.recent_movements);
  }

  function renderKpis(counts) {
    const openWoCount = counts.work_orders.draft + counts.work_orders.released;

    lowStockValueEl.textContent = String(counts.low_stock_parts);
    lowStockTile.classList.toggle("-danger", counts.low_stock_parts > 0);

    openWoValueEl.textContent = String(openWoCount);
    poTransitValueEl.textContent = String(counts.purchase_orders.ordered);
    soShipValueEl.textContent = String(counts.sales_orders.confirmed);
  }

  /** @param {Array<Object>} items - shape per GET /api/dashboard's `low_stock`. */
  function renderLowStock(items) {
    lowStockTbody.replaceChildren();

    if (!items.length) {
      lowStockTbody.appendChild(
        el("tr", { class: "empty-row" }, [el("td", { colSpan: 5 }, ["Nothing below reorder point \u{1F389}"])])
      );
      return;
    }

    for (const part of items) {
      lowStockTbody.appendChild(
        el("tr", {}, [
          el("td", {}, [el("a", { href: `/part.html?id=${part.id}` }, [part.sku])]),
          el("td", {}, [part.name]),
          el("td", { class: "num" }, [`${fmtQty(part.qty_on_hand)} ${part.unit}`]),
          el("td", { class: "num" }, [fmtQty(part.reorder_point)]),
          el("td", { class: "num -short" }, [fmtQty(part.shortfall)]),
        ])
      );
    }
  }

  /** @param {Array<Object>} items - shape per GET /api/dashboard's `open_work_orders`. */
  function renderOpenWorkOrders(items) {
    openWoTbody.replaceChildren();

    if (!items.length) {
      openWoTbody.appendChild(
        el("tr", { class: "empty-row" }, [el("td", { colSpan: 5 }, ["No open work orders."])])
      );
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
          el("td", {}, [`${wo.product_sku} — ${wo.product_name}`]),
          el("td", { class: "num" }, [fmtQty(wo.qty)]),
          el("td", {}, [el("span", { class: `badge -${wo.status}` }, [capitalize(wo.status)])]),
          el("td", {}, [fmtAge(wo.created_at)]),
        ]
      );
      openWoTbody.appendChild(row);
    }
  }

  /** @param {Array<Object>} items - shape per GET /api/dashboard's `recent_movements`. */
  function renderRecentActivity(items) {
    recentTbody.replaceChildren();

    if (!items.length) {
      recentTbody.appendChild(
        el("tr", { class: "empty-row" }, [el("td", { colSpan: 6 }, ["No recent activity."])])
      );
      return;
    }

    for (const m of items) {
      const positive = m.qty_delta > 0;
      const qtyText = (positive ? "+" : "") + fmtQty(m.qty_delta);
      const link = refLink(m.ref_number);
      const refCell = link ? el("a", { href: link }, [m.ref_number]) : "—";

      recentTbody.appendChild(
        el("tr", {}, [
          el("td", {}, [fmtDateTime(m.created_at)]),
          // Only "Reference" is specified as a link (08-dashboard.md); the
          // Part column stays plain text — `recent_movements` gives sku/
          // part_name but no part id to link to /part.html with, and
          // linking via a SKU search would be a different (list-filter)
          // navigation than every other Part cell in this app uses.
          el("td", {}, [`${m.sku} — ${m.part_name}`]),
          el("td", { class: `num ${positive ? "-pos" : "-neg"}` }, [qtyText]),
          el("td", {}, [humanizeReason(m.reason)]),
          el("td", {}, [refCell]),
          el("td", {}, [m.username]),
        ])
      );
    }
  }

  async function load() {
    let data;
    try {
      data = await api("GET", "/api/dashboard");
    } catch {
      toast("Could not load the dashboard.", "error");
      return;
    }
    render(data);
  }

  load();
}
