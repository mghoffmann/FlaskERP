/**
 * part.js — page logic for part.html, the part detail page.
 *
 * Sections, per requirements/03-inventory.md and requirements/04-bom.md:
 *   - Header: sku/name/type badge/active badge, admin Edit + Deactivate.
 *   - Stock card: qty_on_hand, reorder point, low-stock warning, "Adjust
 *     stock" (any role).
 *   - BOM section (finished parts only): read view with a material-cost
 *     rollup, admin "Edit BOM" toggles to an editable line-item grid.
 *   - Movements ledger: offset-paged "Load more" table.
 *
 * Follows the initShell() contract from app.js: a bare top-level `return` is
 * illegal in an ES module, so the whole page body is wrapped in
 * `if (user) { ... }` instead of returning early.
 */

import { initShell, api, toast, fmtQty, fmtMoney, fmtDateTime, qs, el, ApiError } from "./app.js";
import { openFormModal, openConfirmModal } from "./modal.js";

/** Maps stock_movements.ref_type (requirements/01-database.md) to the detail
 * page that documents each other in this app. */
const REF_PAGES = {
  work_order: "/work-order.html",
  purchase_order: "/purchase-order.html",
  sales_order: "/sales-order.html",
};

/** Human-readable labels for stock_movements.reason values. */
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

const user = await initShell();
if (user) {
  main(user);
}

/**
 * @param {{id:number, username:string, role:string}} user
 */
function main(user) {
  const rawId = qs().id;
  const partId = Number(rawId);

  // --- DOM refs -----------------------------------------------------------
  const notFoundBanner = document.getElementById("not-found-banner");
  const partContent = document.getElementById("part-content");
  const conflictBanner = document.getElementById("conflict-banner");

  const partTitleEl = document.getElementById("part-title");
  const partBadgesEl = document.getElementById("part-badges");
  const editPartBtn = document.getElementById("edit-part-btn");
  const toggleActiveBtn = document.getElementById("toggle-active-btn");

  const stockQtyValueEl = document.getElementById("stock-qty-value");
  const stockQtyUnitEl = document.getElementById("stock-qty-unit");
  const stockReorderEl = document.getElementById("stock-reorder");
  const lowStockWarningEl = document.getElementById("low-stock-warning");
  const adjustStockBtn = document.getElementById("adjust-stock-btn");

  const bomSection = document.getElementById("bom-section");
  const bomContent = document.getElementById("bom-content");
  const editBomBtn = document.getElementById("edit-bom-btn");

  const movementsTbody = document.getElementById("movements-tbody");
  const loadMoreBtn = document.getElementById("load-more-btn");

  if (!rawId || Number.isNaN(partId)) {
    notFoundBanner.hidden = false;
    partContent.hidden = true;
    return;
  }

  /** Current part object, refreshed after every mutation. */
  let currentPart = null;
  /** Last BOM payload fetched (GET /api/parts/{id}/bom shape), used to
   * repopulate the read view after a Cancel in edit mode. */
  let lastBomData = null;
  const movementsState = { offset: 0, limit: 50, total: 0 };

  // -------------------------------------------------------------------------
  // Load
  // -------------------------------------------------------------------------

  async function loadPart() {
    try {
      currentPart = await api("GET", `/api/parts/${partId}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        notFoundBanner.hidden = false;
        partContent.hidden = true;
        return;
      }
      toast("Could not load part.", "error");
      return;
    }

    notFoundBanner.hidden = true;
    partContent.hidden = false;

    renderHeader(currentPart);
    renderStock(currentPart);

    bomSection.hidden = currentPart.part_type !== "finished";
    if (currentPart.part_type === "finished") loadBom();

    resetAndLoadMovements();
  }

  // -------------------------------------------------------------------------
  // Header (sku/name/badges, Edit + Deactivate)
  // -------------------------------------------------------------------------

  function renderHeader(part) {
    partTitleEl.textContent = `${part.sku} — ${part.name}`;

    const typeBadge = el("span", { class: "badge -info" }, [part.part_type === "finished" ? "Finished" : "Raw"]);
    const activeBadge = part.active
      ? el("span", { class: "badge -completed" }, ["Active"])
      : el("span", { class: "badge -canceled" }, ["Inactive"]);
    partBadgesEl.replaceChildren(typeBadge, activeBadge);

    toggleActiveBtn.textContent = part.active ? "Deactivate" : "Activate";
    toggleActiveBtn.classList.toggle("btn-danger", part.active);
  }

  editPartBtn.addEventListener("click", openEditPartModal);
  toggleActiveBtn.addEventListener("click", onToggleActive);

  async function openEditPartModal() {
    const p = currentPart;
    const result = await openFormModal({
      title: "Edit part",
      fields: [
        { name: "sku", label: "SKU", required: true, value: p.sku },
        { name: "name", label: "Name", required: true, value: p.name },
        { name: "unit", label: "Unit", value: p.unit },
        { name: "reorder_point", label: "Reorder point", type: "number", step: "0.01", min: "0", value: p.reorder_point },
        { name: "unit_cost", label: "Unit cost", type: "number", step: "0.01", min: "0", value: p.unit_cost },
      ],
      onSubmit: (values) =>
        api("PUT", `/api/parts/${partId}`, {
          sku: values.sku,
          name: values.name,
          unit: values.unit,
          reorder_point: Number(values.reorder_point),
          unit_cost: Number(values.unit_cost),
        }),
    });

    if (result) {
      currentPart = result;
      renderHeader(result);
      renderStock(result);
      toast("Part updated.", "ok");
    }
  }

  /** Deactivate (DELETE, soft delete) / Activate (POST .../activate). A 409
   * means the part is referenced by an open document — shown as both a
   * page-level banner and a toast per the task spec. */
  async function onToggleActive() {
    const part = currentPart;
    const activating = !part.active;

    const confirmed = await openConfirmModal({
      title: activating ? "Activate part?" : "Deactivate part?",
      message: activating
        ? `${part.sku} will reappear in default lists and part pickers.`
        : `${part.sku} will disappear from default lists and part pickers. Its detail page and history stay reachable.`,
      confirmLabel: activating ? "Activate" : "Deactivate",
      danger: !activating,
    });
    if (!confirmed) return;

    conflictBanner.hidden = true;
    try {
      const updated = activating
        ? await api("POST", `/api/parts/${partId}/activate`)
        : await api("DELETE", `/api/parts/${partId}`);
      currentPart = updated;
      renderHeader(updated);
      toast(activating ? "Part activated." : "Part deactivated.", "ok");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        conflictBanner.hidden = false;
        conflictBanner.textContent = err.message || "This part is referenced by an open document.";
        toast(err.message || "Could not deactivate: part is in use.", "error");
      } else if (err instanceof ApiError) {
        toast(err.message, "error");
      } else {
        toast("Something went wrong.", "error");
        throw err;
      }
    }
  }

  // -------------------------------------------------------------------------
  // Stock card
  // -------------------------------------------------------------------------

  function renderStock(part) {
    stockQtyValueEl.textContent = fmtQty(part.qty_on_hand);
    stockQtyUnitEl.textContent = part.unit;
    stockReorderEl.textContent = fmtQty(part.reorder_point);
    lowStockWarningEl.hidden = !part.low_stock;
  }

  adjustStockBtn.addEventListener("click", openAdjustModal);

  async function openAdjustModal() {
    const result = await openFormModal({
      title: "Adjust stock",
      submitLabel: "Adjust",
      fields: [
        { name: "qty_delta", label: `Quantity change (${currentPart.unit})`, type: "number", step: "any", required: true },
        { name: "note", label: "Note", type: "textarea", required: true },
      ],
      onSubmit: (values) =>
        api("POST", `/api/parts/${partId}/adjust`, {
          qty_delta: Number(values.qty_delta),
          note: values.note,
        }),
    });

    if (result) {
      currentPart = result;
      renderStock(result);
      toast("Stock adjusted.", "ok");
      resetAndLoadMovements();
    }
  }

  // -------------------------------------------------------------------------
  // BOM section (finished parts only) — requirements/04-bom.md
  // -------------------------------------------------------------------------

  async function loadBom() {
    try {
      lastBomData = await api("GET", `/api/parts/${partId}/bom`);
    } catch {
      toast("Could not load the bill of materials.", "error");
      return;
    }
    renderBomRead(lastBomData);
  }

  /** Read view: Component SKU/Name/Qty per/Unit cost/Line cost + material
   * cost footer; warning icon on inactive or zero-stock components. */
  function renderBomRead(data) {
    const rows = data.items.map((item) => {
      const warn = !item.active || item.on_hand === 0;
      const skuCell = [item.sku];
      if (warn) {
        skuCell.unshift(
          el(
            "span",
            { class: "warn-icon", title: !item.active ? "Component is inactive" : "Component is out of stock" },
            ["⚠ "]
          )
        );
      }
      return el("tr", {}, [
        el("td", {}, skuCell),
        el("td", {}, [item.name]),
        el("td", { class: "num" }, [fmtQty(item.qty_per)]),
        el("td", { class: "num" }, [fmtMoney(item.unit_cost)]),
        el("td", { class: "num" }, [fmtMoney(item.line_cost)]),
      ]);
    });

    if (!rows.length) {
      rows.push(el("tr", { class: "empty-row" }, [el("td", { colSpan: 5 }, ["No components."])]));
    }

    const table = el("table", {}, [
      el("thead", {}, [
        el("tr", {}, [
          el("th", {}, ["Component SKU"]),
          el("th", {}, ["Name"]),
          el("th", { class: "num" }, ["Qty per"]),
          el("th", { class: "num" }, ["Unit cost"]),
          el("th", { class: "num" }, ["Line cost"]),
        ]),
      ]),
      el("tbody", {}, rows),
      el("tfoot", {}, [
        el("tr", {}, [
          el("td", { colSpan: 4 }, ["Material cost"]),
          el("td", { class: "num" }, [fmtMoney(data.material_cost)]),
        ]),
      ]),
    ]);

    bomContent.replaceChildren(table);
  }

  editBomBtn.addEventListener("click", enterBomEditMode);

  async function enterBomEditMode() {
    editBomBtn.disabled = true;
    let activeParts;
    try {
      const data = await api("GET", "/api/parts"); // default active=true
      activeParts = data.items;
    } catch {
      toast("Could not load parts for the component picker.", "error");
      editBomBtn.disabled = false;
      return;
    }
    editBomBtn.disabled = false;
    renderBomEdit(lastBomData.items, activeParts);
  }

  /**
   * Edit mode: editable rows (SKU picker + qty_per + remove), Add line,
   * Save/Cancel. Save issues PUT /api/parts/{id}/bom with replace-all
   * semantics; on 400 the response's `details` (one entry per offending
   * line, per requirements/04-bom.md) are matched back to their row.
   *
   * @param {Array<Object>} originalItems - current BOM lines (GET shape).
   * @param {Array<Object>} activeParts - GET /api/parts items, active only.
   */
  function renderBomEdit(originalItems, activeParts) {
    const datalistId = `bom-picker-${partId}`;

    // Validation lookup: SKU -> component_part_id. Seeded from every active
    // part (excluding the product itself, per 09-frontend.md's part-picker
    // convention) plus the BOM's *current* components, so an unchanged row
    // referencing a component that was deactivated after being added still
    // resolves client-side (the server's own 400 will still flag it if the
    // line is actually saved as-is).
    const lookup = new Map();
    for (const p of activeParts) {
      if (p.id === partId) continue;
      lookup.set(p.sku.toUpperCase(), p.id);
    }
    for (const item of originalItems) {
      if (!lookup.has(item.sku.toUpperCase())) lookup.set(item.sku.toUpperCase(), item.component_part_id);
    }

    const datalist = el(
      "datalist",
      { id: datalistId },
      activeParts.filter((p) => p.id !== partId).map((p) => el("option", { value: p.sku }, [p.name]))
    );

    const banner = el("div", { class: "banner -error", hidden: true });
    const rows = []; // {rowEl, skuInput, qtyInput, errorEl}
    const rowsTbody = el("tbody");

    function addRow(item) {
      const skuInput = el("input", { type: "text", required: true, placeholder: "SKU", value: item ? item.sku : "" });
      // HTMLInputElement.list is a read-only IDL property (it returns the
      // associated <datalist>, it isn't settable) — el() would try a plain
      // property assignment for any attrs key that already exists on the
      // node, which throws in strict mode (ES modules are always strict).
      // setAttribute is the correct way to wire up list="...".
      skuInput.setAttribute("list", datalistId);

      const qtyInput = el("input", {
        type: "number",
        step: "0.0001",
        min: "0.0001",
        required: true,
        value: item ? item.qty_per : 1,
      });

      const errorEl = el("div", { class: "field-error" });
      const removeBtn = el("button", { type: "button", class: "btn btn-ghost", "aria-label": "Remove line" }, ["Remove"]);

      const rowEl = el("tr", {}, [
        el("td", {}, [skuInput, errorEl]),
        el("td", {}, [qtyInput]),
        el("td", {}, [removeBtn]),
      ]);

      const row = { rowEl, skuInput, qtyInput, errorEl };
      removeBtn.addEventListener("click", () => {
        rows.splice(rows.indexOf(row), 1);
        rowEl.remove();
      });

      rows.push(row);
      rowsTbody.appendChild(rowEl);
    }

    for (const item of originalItems) addRow(item);

    const addLineBtn = el("button", { type: "button", class: "btn" }, ["Add line"]);
    addLineBtn.addEventListener("click", () => addRow(null));

    const saveBtn = el("button", { type: "button", class: "btn btn-primary" }, ["Save"]);
    const cancelBtn = el("button", { type: "button", class: "btn btn-ghost" }, ["Cancel"]);
    cancelBtn.addEventListener("click", () => renderBomRead(lastBomData));
    saveBtn.addEventListener("click", () => saveBom());

    async function saveBom() {
      banner.hidden = true;
      banner.textContent = "";
      for (const row of rows) row.errorEl.textContent = "";

      const items = [];
      const seen = new Set();
      let hasError = false;

      rows.forEach((row) => {
        const skuRaw = row.skuInput.value.trim();
        if (!skuRaw) {
          row.errorEl.textContent = "Component required.";
          hasError = true;
          return;
        }
        const componentId = lookup.get(skuRaw.toUpperCase());
        if (!componentId) {
          row.errorEl.textContent = "Unknown or inactive SKU.";
          hasError = true;
          return;
        }
        if (seen.has(componentId)) {
          row.errorEl.textContent = "Duplicate component.";
          hasError = true;
          return;
        }
        seen.add(componentId);

        const qty = Number(row.qtyInput.value);
        if (!row.qtyInput.value || Number.isNaN(qty) || qty <= 0) {
          row.errorEl.textContent = "Qty per must be greater than 0.";
          hasError = true;
          return;
        }
        items.push({ component_part_id: componentId, qty_per: qty });
      });

      if (hasError) return;

      saveBtn.disabled = true;
      cancelBtn.disabled = true;
      try {
        const data = await api("PUT", `/api/parts/${partId}/bom`, { items });
        lastBomData = data;
        toast("BOM saved.", "ok");
        renderBomRead(data);
      } catch (err) {
        if (err instanceof ApiError && err.status === 400) {
          banner.hidden = false;
          banner.textContent = err.message || "Fix the highlighted lines below.";
          applyLineErrors(err.details, rows);
        } else if (err instanceof ApiError) {
          toast(err.message, "error");
        } else {
          toast("Something went wrong.", "error");
          throw err;
        }
      } finally {
        saveBtn.disabled = false;
        cancelBtn.disabled = false;
      }
    }

    const table = el("table", {}, [
      el("thead", {}, [
        el("tr", {}, [el("th", {}, ["Component SKU"]), el("th", {}, ["Qty per"]), el("th", {}, [""])]),
      ]),
      rowsTbody,
    ]);

    const actions = el("div", { class: "modal-actions" }, [addLineBtn, cancelBtn, saveBtn]);
    bomContent.replaceChildren(datalist, banner, table, actions);
  }

  /**
   * Matches a 400 response's `details` entries back to their editor row.
   * The exact shape of each entry isn't pinned down beyond "names the line
   * index" (requirements/04-bom.md), so this looks for any of the common
   * `{line|index|row, message|error|msg}` shapes and otherwise leaves the
   * top-level banner (already set by the caller) as the fallback.
   */
  function applyLineErrors(details, rows) {
    if (!Array.isArray(details)) return;
    for (const detail of details) {
      if (!detail || typeof detail !== "object") continue;
      const idx =
        typeof detail.line === "number" ? detail.line : typeof detail.index === "number" ? detail.index : typeof detail.row === "number" ? detail.row : null;
      const message = detail.message || detail.error || detail.msg;
      if (idx !== null && rows[idx] && message) {
        rows[idx].errorEl.textContent = message;
      }
    }
  }

  // -------------------------------------------------------------------------
  // Movements ledger — offset-paged "Load more"
  // -------------------------------------------------------------------------

  loadMoreBtn.addEventListener("click", () => loadMoreMovements());

  async function resetAndLoadMovements() {
    movementsState.offset = 0;
    movementsState.total = 0;
    movementsTbody.replaceChildren();
    loadMoreBtn.hidden = true;
    await loadMoreMovements();
  }

  async function loadMoreMovements() {
    let data;
    try {
      data = await api(
        "GET",
        `/api/parts/${partId}/movements?limit=${movementsState.limit}&offset=${movementsState.offset}`
      );
    } catch {
      toast("Could not load movements.", "error");
      return;
    }

    movementsState.total = data.total;

    if (movementsState.offset === 0 && data.items.length === 0) {
      movementsTbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 6 }, ["No movements yet."])]));
    } else {
      for (const m of data.items) movementsTbody.appendChild(buildMovementRow(m));
    }

    movementsState.offset += data.items.length;
    loadMoreBtn.hidden = movementsState.offset >= movementsState.total;
  }

  function buildMovementRow(m) {
    const positive = m.qty_delta > 0;
    const qtyText = (positive ? "+" : "") + fmtQty(m.qty_delta);
    const refPage = REF_PAGES[m.ref_type];
    const refCell = refPage ? el("a", { href: `${refPage}?id=${m.ref_id}` }, [m.ref_number || `#${m.ref_id}`]) : "—";

    return el("tr", {}, [
      el("td", {}, [fmtDateTime(m.created_at)]),
      el("td", { class: `num ${positive ? "-pos" : "-neg"}` }, [qtyText]),
      el("td", {}, [humanizeReason(m.reason)]),
      el("td", {}, [refCell]),
      el("td", {}, [m.note || ""]),
      el("td", {}, [m.username]),
    ]);
  }

  loadPart();
}
