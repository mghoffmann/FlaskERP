/**
 * purchase-order.js — page logic for purchase-order.html, which serves three
 * modes on one page (requirements/06-purchasing.md):
 *   - Create (no ?id=, admin only — an operator landing here is redirected
 *     to /purchase-orders.html): supplier picker, notes, editable lines grid.
 *   - View (?id=, any role): read-only header + lines table.
 *   - Edit (?id=, admin, draft only): the "Edit" button swaps the same lines
 *     grid in over the view, pre-filled; Cancel reverts to view without a
 *     refetch, Save issues PUT and returns to view.
 *
 * Follows the initShell() contract from app.js: a bare top-level `return` is
 * illegal in an ES module, so the whole page body is wrapped in
 * `if (user) { ... }` instead of returning early. Inside main() (a normal
 * function, not the module top level) plain `return` is fine and used the
 * same way work-order.js and part.js use it for their own not-found guards.
 */

import { initShell, api, toast, fmtQty, fmtMoney, fmtDateTime, qs, el, ApiError } from "./app.js";
import { openConfirmModal } from "./modal.js";

function capitalize(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
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
  const isCreate = !rawId;
  const poId = isCreate ? null : Number(rawId);

  // --- DOM refs -------------------------------------------------------------
  const notFoundBanner = document.getElementById("not-found-banner");
  const poContent = document.getElementById("po-content");
  const poBanner = document.getElementById("po-banner");

  const poTitleEl = document.getElementById("po-title");
  const poBadgesEl = document.getElementById("po-badges");
  const poActionsEl = document.getElementById("po-actions");

  const viewSection = document.getElementById("po-view-section");
  const viewLinesSection = document.getElementById("po-view-lines-section");
  const editSection = document.getElementById("po-edit-section");
  const editContainer = document.getElementById("po-edit-container");

  const supplierEl = document.getElementById("po-supplier");
  const notesEl = document.getElementById("po-notes");
  const createdByEl = document.getElementById("po-created-by");
  const createdAtEl = document.getElementById("po-created-at");
  const orderedAtEl = document.getElementById("po-ordered-at");
  const receivedAtEl = document.getElementById("po-received-at");
  const linesTbody = document.getElementById("po-lines-tbody");
  const linesTotalEl = document.getElementById("po-lines-total");

  // Create mode is admin-only server-side too, but the page itself has
  // nothing useful to show an operator here (no id to view) — send them
  // back to the list rather than rendering a form that would 403 on submit.
  if (isCreate && user.role !== "admin") {
    location.href = "/purchase-orders.html";
    return;
  }

  if (!isCreate && Number.isNaN(poId)) {
    notFoundBanner.hidden = false;
    poContent.hidden = true;
    return;
  }

  /** Current PO detail (list shape + lines[]), null in create mode until the
   * first successful POST. Refreshed after every load/mutation. Which of the
   * three modes (create/view/edit) is on screen is never tracked separately
   * from this: it's create while `currentPo` is null, edit while the edit
   * section is un-hidden and `currentPo` is set, view otherwise — each
   * render*/enter*/exit* function below already knows which case it's in
   * from its own call site, so a parallel `mode` variable would just be
   * another thing to keep in sync. */
  let currentPo = null;

  function clearBanner() {
    poBanner.hidden = true;
    poBanner.textContent = "";
  }

  // -------------------------------------------------------------------------
  // Load (view/edit modes only — create mode has nothing to fetch)
  // -------------------------------------------------------------------------

  async function loadPo() {
    try {
      currentPo = await api("GET", `/api/purchase-orders/${poId}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        notFoundBanner.hidden = false;
        poContent.hidden = true;
        return;
      }
      toast("Could not load purchase order.", "error");
      return;
    }

    notFoundBanner.hidden = true;
    poContent.hidden = false;
    renderView(currentPo);
  }

  // -------------------------------------------------------------------------
  // View mode
  // -------------------------------------------------------------------------

  function renderView(po) {
    editSection.hidden = true;
    viewSection.hidden = false;
    viewLinesSection.hidden = false;

    poTitleEl.textContent = po.po_number;
    poBadgesEl.replaceChildren(el("span", { class: `badge -${po.status} wo-status-badge` }, [capitalize(po.status)]));

    // Plain text, not a link — 06-purchasing.md's view-mode header calls for
    // "supplier name linking nowhere special — plain text ok" (there's no
    // supplier detail page of its own; suppliers.html's modal is the only
    // place to see/edit one).
    supplierEl.textContent = po.supplier.name;
    notesEl.textContent = po.notes || "—";
    createdByEl.textContent = po.created_by_username;
    createdAtEl.textContent = fmtDateTime(po.created_at);
    orderedAtEl.textContent = po.ordered_at ? fmtDateTime(po.ordered_at) : "—";
    receivedAtEl.textContent = po.received_at ? fmtDateTime(po.received_at) : "—";

    linesTbody.replaceChildren();
    if (!po.lines.length) {
      linesTbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 4 }, ["No lines."])]));
    } else {
      for (const line of po.lines) {
        linesTbody.appendChild(
          el("tr", {}, [
            el("td", {}, [`${line.sku} — ${line.name}`]),
            el("td", { class: "num" }, [`${fmtQty(line.qty)} ${line.unit}`]),
            el("td", { class: "num" }, [fmtMoney(line.unit_cost)]),
            el("td", { class: "num" }, [fmtMoney(line.line_total)]),
          ])
        );
      }
    }
    linesTotalEl.textContent = fmtMoney(po.total);

    renderActions(po);
  }

  /** Action buttons by status/role, per 06-purchasing.md:
   *   draft    -> Edit / Place order / Cancel (admin)
   *   ordered  -> Receive delivery (any role) + Cancel (admin)
   *   received/canceled -> none
   */
  function renderActions(po) {
    const buttons = [];

    if (po.status === "draft") {
      buttons.push(
        el("button", { type: "button", class: "btn", "data-role": "admin", onClick: enterEditMode }, ["Edit"]),
        el("button", { type: "button", class: "btn btn-primary", "data-role": "admin", onClick: onPlace }, [
          "Place order",
        ]),
        el("button", { type: "button", class: "btn btn-danger", "data-role": "admin", onClick: onCancel }, ["Cancel"])
      );
    } else if (po.status === "ordered") {
      buttons.push(
        el("button", { type: "button", class: "btn btn-primary", onClick: onReceive }, ["Receive delivery"]),
        el("button", { type: "button", class: "btn btn-danger", "data-role": "admin", onClick: onCancel }, ["Cancel"])
      );
    }

    poActionsEl.replaceChildren(...buttons);

    // renderActions() runs after initShell() already stripped [data-role]
    // elements once at page load, so freshly-built admin buttons need the
    // same gating applied again for operators (same pattern as work-order.js).
    if (user.role !== "admin") {
      poActionsEl.querySelectorAll('[data-role="admin"]').forEach((node) => node.remove());
    }
  }

  async function onPlace() {
    const confirmed = await openConfirmModal({
      title: "Place order?",
      message: `Place ${currentPo.po_number} with ${currentPo.supplier.name}? It will no longer be editable until it's received or canceled.`,
      confirmLabel: "Place order",
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/purchase-orders/${poId}/place`);
      toast("Purchase order placed.", "ok");
      await loadPo();
    } catch (err) {
      handleActionError(err);
    }
  }

  async function onCancel() {
    const confirmed = await openConfirmModal({
      title: "Cancel purchase order?",
      message: `Cancel ${currentPo.po_number}? This cannot be undone.`,
      confirmLabel: "Cancel purchase order",
      danger: true,
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/purchase-orders/${poId}/cancel`);
      toast("Purchase order canceled.", "ok");
      await loadPo();
    } catch (err) {
      handleActionError(err);
    }
  }

  /** Receive delivery: any role (the operator signs for it). Confirm wording
   * is pinned by 06-purchasing.md: "This will add N line items to stock." —
   * N is line_count, not total units, matching the doc's exact phrasing. */
  async function onReceive() {
    const po = currentPo;
    const confirmed = await openConfirmModal({
      title: "Receive delivery?",
      message: `This will add ${po.line_count} line item${po.line_count === 1 ? "" : "s"} to stock.`,
      confirmLabel: "Receive delivery",
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/purchase-orders/${poId}/receive`);
      toast("Delivery received.", "ok");
      await loadPo();
    } catch (err) {
      handleActionError(err);
    }
  }

  /** Shared error handling for place/cancel/receive: any ApiError's message
   * goes on the page-level banner (never alert()); anything else is an
   * unexpected bug, so toast it and rethrow instead of swallowing it. */
  function handleActionError(err) {
    if (err instanceof ApiError) {
      poBanner.hidden = false;
      poBanner.textContent = err.message;
    } else {
      toast("Something went wrong.", "error");
      throw err;
    }
  }

  // -------------------------------------------------------------------------
  // Create / edit mode — shared lines-grid editor
  // -------------------------------------------------------------------------

  function enterCreateMode() {
    poTitleEl.textContent = "New purchase order";
    poBadgesEl.replaceChildren();
    poActionsEl.replaceChildren();
    viewSection.hidden = true;
    viewLinesSection.hidden = true;
    editSection.hidden = false;
    renderEditForm(null);
  }

  function enterEditMode() {
    editSection.hidden = false;
    viewSection.hidden = true;
    viewLinesSection.hidden = true;
    poActionsEl.replaceChildren();
    renderEditForm(currentPo);
  }

  function exitEditMode() {
    clearBanner();
    renderView(currentPo);
  }

  /**
   * Builds the create/edit form into #po-edit-container: supplier select,
   * notes, an editable lines grid (part picker + datalist, qty, unit cost,
   * computed line total, add/remove row), and a running grand total that
   * updates on input. Fetches active suppliers + active parts fresh on every
   * entry (same "re-fetch rather than cache" choice part.js's BOM editor
   * makes for enterBomEditMode() — freshness over one saved round trip, fine
   * at demo scale).
   *
   * @param {Object|null} po - existing PO detail to pre-fill (edit mode), or
   *   null for an empty create-mode form.
   */
  async function renderEditForm(po) {
    editContainer.replaceChildren(el("p", { class: "hint-text" }, ["Loading…"]));

    let suppliers, parts;
    try {
      const [supplierData, partData] = await Promise.all([
        api("GET", "/api/suppliers"), // default active=true — the create-mode picker must be active-only per 06-purchasing.md
        api("GET", "/api/parts"), // default active=true; no part_type filter — any part type may be purchased
      ]);
      suppliers = supplierData.items;
      parts = partData.items;
    } catch {
      toast("Could not load suppliers/parts for the editor.", "error");
      editContainer.replaceChildren(el("p", { class: "banner -error" }, ["Could not load the editor. Reload the page to try again."]));
      return;
    }

    // A draft/ordered PO's own supplier can never have been deactivated out
    // while this PO exists in one of those statuses (06-purchasing.md: a 409
    // blocks deactivating a supplier with a draft/ordered PO), so `po`'s
    // supplier is guaranteed to already be in the active list when editing.

    clearBanner();

    // --- Supplier + notes fields ------------------------------------------
    const supplierError = el("div", { class: "field-error" });
    const supplierSelect = el(
      "select",
      { required: true },
      suppliers.map((s) => el("option", { value: String(s.id) }, [s.name]))
    );
    if (po) supplierSelect.value = String(po.supplier.id);

    const notesError = el("div", { class: "field-error" });
    const notesInput = el("textarea", { value: po ? po.notes || "" : "" });

    if (!suppliers.length) {
      supplierSelect.disabled = true;
      supplierError.textContent = "No active suppliers — add one on the Suppliers page first.";
    }

    // --- Lines grid ---------------------------------------------------------
    const datalistId = "po-part-picker";
    const datalist = el(
      "datalist",
      { id: datalistId },
      parts.map((p) => el("option", { value: p.sku }, [p.name]))
    );

    // SKU -> part lookup for client-side validation + line-total math. Seeded
    // from every active part, plus (edit mode) the PO's *current* lines, so
    // an unchanged line referencing a part deactivated after the PO was
    // created still resolves here (same fallback part.js's BOM editor uses;
    // the server's own validation still applies when the line is saved).
    const lookup = new Map();
    for (const p of parts) lookup.set(p.sku.toUpperCase(), p);
    if (po) {
      for (const line of po.lines) {
        if (!lookup.has(line.sku.toUpperCase())) {
          lookup.set(line.sku.toUpperCase(), { id: line.part_id, sku: line.sku, name: line.name, unit: line.unit, unit_cost: line.unit_cost });
        }
      }
    }

    const rows = []; // {rowEl, partInput, qtyInput, costInput, totalCell, errorEl}
    // Named distinctly from the outer, module-scope `linesTbody` (the
    // read-only view table's <tbody>, looked up at the top of main()) — this
    // one is the editable grid's <tbody>, built fresh on every entry into
    // create/edit mode.
    const gridTbody = el("tbody");
    const gridTotalEl = el("td", { class: "num" }, [fmtMoney(0)]);

    function recomputeTotal() {
      let total = 0;
      for (const row of rows) {
        const qty = Number(row.qtyInput.value) || 0;
        const cost = Number(row.costInput.value) || 0;
        const lineTotal = qty * cost;
        row.totalCell.textContent = fmtMoney(lineTotal);
        total += lineTotal;
      }
      gridTotalEl.textContent = fmtMoney(total);
    }

    function addRow(line) {
      const partInput = el("input", {
        type: "text",
        required: true,
        placeholder: "SKU",
        value: line ? line.sku : "",
      });
      // list is a read-only IDL property on <input> (returns the associated
      // <datalist>, not settable as a plain property) — el() falls back to a
      // property assignment for any attrs key that already exists on the
      // node, which throws in strict mode. setAttribute is the correct way
      // to wire up list="..." (same note as part.js's BOM editor).
      partInput.setAttribute("list", datalistId);

      const qtyInput = el("input", {
        type: "number",
        step: "0.01",
        min: "0.01",
        required: true,
        value: line ? line.qty : "",
      });
      const costInput = el("input", {
        type: "number",
        step: "0.01",
        min: "0",
        required: true,
        value: line ? line.unit_cost : "",
      });
      const totalCell = el("td", { class: "num" }, [fmtMoney(line ? line.qty * line.unit_cost : 0)]);
      const errorEl = el("div", { class: "field-error" });
      const removeBtn = el("button", { type: "button", class: "btn btn-ghost", "aria-label": "Remove line" }, [
        "Remove",
      ]);

      // Convenience: filling in a recognized SKU auto-fills a still-empty
      // unit cost from the part's catalog cost (the "last cost" the part was
      // last received/adjusted at) — never overwrites a value the user
      // already typed.
      partInput.addEventListener("change", () => {
        const part = lookup.get(partInput.value.trim().toUpperCase());
        if (part && !costInput.value) {
          costInput.value = part.unit_cost;
          recomputeTotal();
        }
      });

      qtyInput.addEventListener("input", recomputeTotal);
      costInput.addEventListener("input", recomputeTotal);

      const rowEl = el("tr", {}, [
        el("td", {}, [partInput, errorEl]),
        el("td", {}, [qtyInput]),
        el("td", {}, [costInput]),
        totalCell,
        el("td", {}, [removeBtn]),
      ]);

      const row = { rowEl, partInput, qtyInput, costInput, totalCell, errorEl };
      removeBtn.addEventListener("click", () => {
        rows.splice(rows.indexOf(row), 1);
        rowEl.remove();
        recomputeTotal();
      });

      rows.push(row);
      gridTbody.appendChild(rowEl);
    }

    if (po && po.lines.length) {
      for (const line of po.lines) addRow(line);
    } else {
      addRow(null); // seed one blank row so create mode isn't an empty grid
    }
    recomputeTotal();

    const addLineBtn = el("button", { type: "button", class: "btn" }, ["Add line"]);
    addLineBtn.addEventListener("click", () => {
      addRow(null);
      recomputeTotal();
    });

    const linesTable = el("table", {}, [
      el("thead", {}, [
        el(
          "tr",
          {},
          [
            el("th", {}, ["Part"]),
            el("th", { class: "num" }, ["Qty"]),
            el("th", { class: "num" }, ["Unit cost"]),
            el("th", { class: "num" }, ["Line total"]),
            el("th", {}, [""]),
          ]
        ),
      ]),
      gridTbody,
      el("tfoot", {}, [el("tr", {}, [el("td", { colSpan: 3 }, ["Total"]), gridTotalEl, el("td", {}, [])])]),
    ]);

    // --- Save / Cancel --------------------------------------------------
    const saveBtn = el("button", { type: "button", class: "btn btn-primary" }, ["Save"]);
    const cancelBtn = el("button", { type: "button", class: "btn btn-ghost" }, ["Cancel"]);
    cancelBtn.addEventListener("click", () => {
      if (po) {
        exitEditMode();
      } else {
        location.href = "/purchase-orders.html";
      }
    });
    saveBtn.addEventListener("click", () => save());

    async function save() {
      clearBanner();
      supplierError.textContent = "";
      notesError.textContent = "";
      for (const row of rows) row.errorEl.textContent = "";

      // --- client-side validation, mirrors what the server checks per
      // 06-purchasing.md: at least one line, each part known+active, qty >
      // 0, unit_cost >= 0, no duplicate parts.
      if (!supplierSelect.value) {
        supplierError.textContent = "Supplier is required.";
        return;
      }
      if (!rows.length) {
        poBanner.hidden = false;
        poBanner.textContent = "Add at least one line.";
        return;
      }

      const lines = [];
      const seenPartIds = new Set();
      let hasError = false;

      rows.forEach((row) => {
        const skuRaw = row.partInput.value.trim();
        if (!skuRaw) {
          row.errorEl.textContent = "Part required.";
          hasError = true;
          return;
        }
        const part = lookup.get(skuRaw.toUpperCase());
        if (!part) {
          row.errorEl.textContent = "Unknown or inactive SKU.";
          hasError = true;
          return;
        }
        if (seenPartIds.has(part.id)) {
          row.errorEl.textContent = "Duplicate part.";
          hasError = true;
          return;
        }

        const qty = Number(row.qtyInput.value);
        if (!row.qtyInput.value || Number.isNaN(qty) || qty <= 0) {
          row.errorEl.textContent = "Qty must be greater than 0.";
          hasError = true;
          return;
        }

        const unitCost = Number(row.costInput.value);
        if (row.costInput.value === "" || Number.isNaN(unitCost) || unitCost < 0) {
          row.errorEl.textContent = "Unit cost must be 0 or more.";
          hasError = true;
          return;
        }

        seenPartIds.add(part.id);
        lines.push({ part_id: part.id, qty, unit_cost: unitCost });
      });

      if (hasError) return;

      const payload = {
        supplier_id: Number(supplierSelect.value),
        notes: notesInput.value || undefined,
        lines,
      };

      saveBtn.disabled = true;
      cancelBtn.disabled = true;
      try {
        if (po) {
          const updated = await api("PUT", `/api/purchase-orders/${poId}`, payload);
          currentPo = updated;
          toast("Purchase order updated.", "ok");
          exitEditMode();
        } else {
          const created = await api("POST", "/api/purchase-orders", payload);
          toast("Purchase order created.", "ok");
          location.href = `/purchase-order.html?id=${created.id}`;
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 400) {
          poBanner.hidden = false;
          poBanner.textContent = err.message || "Fix the highlighted fields below.";
          if (err.fieldErrors.supplier_id) supplierError.textContent = err.fieldErrors.supplier_id;
          if (err.fieldErrors.notes) notesError.textContent = err.fieldErrors.notes;
          applyLineDetailErrors(err.details, rows);
        } else if (err instanceof ApiError) {
          poBanner.hidden = false;
          poBanner.textContent = err.message;
        } else {
          toast("Something went wrong.", "error");
          throw err;
        }
      } finally {
        saveBtn.disabled = false;
        cancelBtn.disabled = false;
      }
    }

    const actions = el("div", { class: "modal-actions" }, [addLineBtn, cancelBtn, saveBtn]);

    editContainer.replaceChildren(
      datalist,
      el("div", { class: "field" }, [el("label", {}, ["Supplier"]), supplierSelect, supplierError]),
      el("div", { class: "field" }, [el("label", {}, ["Notes"]), notesInput, notesError]),
      linesTable,
      actions
    );
  }

  /** Matches a 400 response's `details: [{line, message}]` (per
   * 06-purchasing.md — `line` is the 0-based index into the submitted
   * `lines` array, which is built in the same order as `rows`) back to the
   * offending row's error slot. */
  function applyLineDetailErrors(details, rows) {
    if (!Array.isArray(details)) return;
    for (const detail of details) {
      if (!detail || typeof detail.line !== "number") continue;
      if (rows[detail.line] && detail.message) {
        rows[detail.line].errorEl.textContent = detail.message;
      }
    }
  }

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------

  if (isCreate) {
    poContent.hidden = false;
    enterCreateMode();
  } else {
    loadPo();
  }
}
