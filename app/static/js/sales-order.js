/**
 * sales-order.js — page logic for sales-order.html, which serves three modes
 * on one page (requirements/07-sales-orders.md), mirroring
 * js/purchase-order.js's structure with the stock arrow reversed:
 *   - Create (no ?id=, admin only — an operator landing here is redirected
 *     to /sales-orders.html): customer picker, notes, editable lines grid.
 *     The line-item part picker is limited to ACTIVE FINISHED parts (the
 *     factory doesn't sell raw stock) and each line has a unit_price
 *     instead of a unit_cost.
 *   - View (?id=, any role): read-only header + lines table, the lines
 *     table adding live On hand / Short columns (short highlighted) plus a
 *     "Ready to ship" / "Short N lines" banner while confirmed.
 *   - Edit (?id=, admin, draft only): the "Edit" button swaps the same lines
 *     grid in over the view, pre-filled; Cancel reverts to view without a
 *     refetch, Save issues PUT and returns to view.
 *
 * Follows the initShell() contract from app.js: a bare top-level `return` is
 * illegal in an ES module, so the whole page body is wrapped in
 * `if (user) { ... }` instead of returning early. Inside main() (a normal
 * function, not the module top level) plain `return` is fine, same as
 * purchase-order.js/work-order.js's own not-found guards.
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
  const soId = isCreate ? null : Number(rawId);

  // --- DOM refs -------------------------------------------------------------
  const notFoundBanner = document.getElementById("not-found-banner");
  const soContent = document.getElementById("so-content");
  const soBanner = document.getElementById("so-banner");
  const availabilityBanner = document.getElementById("availability-banner");

  const soTitleEl = document.getElementById("so-title");
  const soBadgesEl = document.getElementById("so-badges");
  const soActionsEl = document.getElementById("so-actions");

  const viewSection = document.getElementById("so-view-section");
  const viewLinesSection = document.getElementById("so-view-lines-section");
  const editSection = document.getElementById("so-edit-section");
  const editContainer = document.getElementById("so-edit-container");

  const customerEl = document.getElementById("so-customer");
  const notesEl = document.getElementById("so-notes");
  const createdByEl = document.getElementById("so-created-by");
  const createdAtEl = document.getElementById("so-created-at");
  const confirmedAtEl = document.getElementById("so-confirmed-at");
  const shippedAtEl = document.getElementById("so-shipped-at");
  const linesTbody = document.getElementById("so-lines-tbody");
  const linesTotalEl = document.getElementById("so-lines-total");

  // Create mode is admin-only server-side too, but the page itself has
  // nothing useful to show an operator here (no id to view) — send them
  // back to the list rather than rendering a form that would 403 on submit.
  if (isCreate && user.role !== "admin") {
    location.href = "/sales-orders.html";
    return;
  }

  if (!isCreate && Number.isNaN(soId)) {
    notFoundBanner.hidden = false;
    soContent.hidden = true;
    return;
  }

  /** Current SO detail (list shape + lines[]), null in create mode until the
   * first successful POST. Refreshed after every load/mutation. Which of the
   * three modes (create/view/edit) is on screen is never tracked separately
   * from this: it's create while `currentSo` is null, edit while the edit
   * section is un-hidden and `currentSo` is set, view otherwise — same
   * reasoning as purchase-order.js's `currentPo`. */
  let currentSo = null;

  function clearBanner() {
    soBanner.hidden = true;
    soBanner.textContent = "";
  }

  // -------------------------------------------------------------------------
  // Load (view/edit modes only — create mode has nothing to fetch)
  // -------------------------------------------------------------------------

  async function loadSo() {
    try {
      currentSo = await api("GET", `/api/sales-orders/${soId}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        notFoundBanner.hidden = false;
        soContent.hidden = true;
        return;
      }
      toast("Could not load sales order.", "error");
      return;
    }

    notFoundBanner.hidden = true;
    soContent.hidden = false;
    renderView(currentSo);
  }

  // -------------------------------------------------------------------------
  // View mode
  // -------------------------------------------------------------------------

  function renderView(so) {
    editSection.hidden = true;
    viewSection.hidden = false;
    viewLinesSection.hidden = false;

    soTitleEl.textContent = so.so_number;
    soBadgesEl.replaceChildren(el("span", { class: `badge -${so.status} wo-status-badge` }, [capitalize(so.status)]));

    // Plain text, not a link — mirrors purchase-order.html's view-mode
    // header: there's no customer detail page of its own, customers.html's
    // modal is the only place to see/edit one.
    customerEl.textContent = so.customer.name;
    notesEl.textContent = so.notes || "—";
    createdByEl.textContent = so.created_by_username;
    createdAtEl.textContent = fmtDateTime(so.created_at);
    confirmedAtEl.textContent = so.confirmed_at ? fmtDateTime(so.confirmed_at) : "—";
    shippedAtEl.textContent = so.shipped_at ? fmtDateTime(so.shipped_at) : "—";

    renderLines(so.lines);
    renderAvailabilityBanner(so);
    renderActions(so);
  }

  /** @param {Array<{part_id, sku, name, unit, qty, unit_price, line_total, on_hand, short}>} lines */
  function renderLines(lines) {
    linesTbody.replaceChildren();
    if (!lines.length) {
      linesTbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 6 }, ["No lines."])]));
    } else {
      for (const line of lines) linesTbody.appendChild(buildLineRow(line));
    }

    const total = lines.reduce((sum, line) => sum + line.qty * line.unit_price, 0);
    linesTotalEl.textContent = fmtMoney(total);
  }

  /** Builds one read-only line row; tagged with data-* (raw, unformatted
   * values) so a post-Ship 409's shortfall details can be spliced back into
   * the right row without re-parsing formatted text — same technique
   * work-order.js's buildComponentRow uses for its shortfall splice. */
  function buildLineRow(line) {
    const short = Number(line.short) > 0;
    return el(
      "tr",
      {
        dataset: {
          partId: line.part_id,
          sku: line.sku,
          name: line.name,
          unit: line.unit,
          qty: line.qty,
          unitPrice: line.unit_price,
        },
      },
      [
        el("td", {}, [`${line.sku} — ${line.name}`]),
        el("td", { class: "num" }, [`${fmtQty(line.qty)} ${line.unit}`]),
        el("td", { class: "num" }, [fmtMoney(line.unit_price)]),
        el("td", { class: "num" }, [fmtMoney(line.line_total)]),
        el("td", { class: "num" }, [fmtQty(line.on_hand)]),
        el("td", { class: `num${short ? " -short" : ""}` }, [fmtQty(line.short)]),
      ]
    );
  }

  /**
   * Splices a Ship 409 insufficient_stock response's `details` (fresh
   * {part_id, sku, required, on_hand, short} rows, per
   * requirements/07-sales-orders.md) into the lines table in place, so the
   * shortfall the user just hit is highlighted with server-fresh numbers
   * even before the full reload (triggered right after by the caller)
   * repaints everything from GET /api/sales-orders/{id}). unit/qty/unit_price
   * aren't part of the 409 detail row's shape, so they're pulled back out of
   * the existing row's dataset rather than parsed from formatted text —
   * `required` from the detail is what `qty` already was, so it's used
   * directly for the line-total recompute.
   */
  function applyShortfallDetails(details) {
    if (!Array.isArray(details)) return;
    for (const d of details) {
      const row = linesTbody.querySelector(`tr[data-part-id="${d.part_id}"]`);
      if (!row) continue;
      const unitPrice = Number(row.dataset.unitPrice);
      row.replaceWith(
        buildLineRow({
          part_id: d.part_id,
          sku: d.sku || row.dataset.sku,
          name: row.dataset.name,
          unit: row.dataset.unit,
          qty: d.required,
          unit_price: unitPrice,
          line_total: d.required * unitPrice,
          on_hand: d.on_hand,
          short: d.short,
        })
      );
    }
  }

  // -------------------------------------------------------------------------
  // Ship-readiness banner — confirmed only; draft/shipped/canceled get none
  // (the lines table's On hand/Short columns are informational enough at
  // draft, same reasoning as work-order.html's availability banner).
  // -------------------------------------------------------------------------

  function renderAvailabilityBanner(so) {
    if (so.status !== "confirmed") {
      availabilityBanner.hidden = true;
      return;
    }

    availabilityBanner.hidden = false;
    const shortLines = so.lines.filter((line) => Number(line.short) > 0);
    if (shortLines.length === 0) {
      availabilityBanner.className = "banner -ok";
      availabilityBanner.textContent = "Ready to ship.";
    } else {
      availabilityBanner.className = "banner -warn";
      availabilityBanner.textContent = `Short ${shortLines.length} line${shortLines.length === 1 ? "" : "s"}.`;
    }
  }

  /** Action buttons by status/role, per 07-sales-orders.md:
   *   draft     -> Edit / Confirm / Cancel (admin)
   *   confirmed -> Ship (any role) + Cancel (admin)
   *   shipped/canceled -> none
   */
  function renderActions(so) {
    const buttons = [];

    if (so.status === "draft") {
      buttons.push(
        el("button", { type: "button", class: "btn", "data-role": "admin", onClick: enterEditMode }, ["Edit"]),
        el("button", { type: "button", class: "btn btn-primary", "data-role": "admin", onClick: onConfirm }, [
          "Confirm",
        ]),
        el("button", { type: "button", class: "btn btn-danger", "data-role": "admin", onClick: onCancel }, ["Cancel"])
      );
    } else if (so.status === "confirmed") {
      buttons.push(
        el("button", { type: "button", class: "btn btn-primary", onClick: onShip }, ["Ship"]),
        el("button", { type: "button", class: "btn btn-danger", "data-role": "admin", onClick: onCancel }, ["Cancel"])
      );
    }

    soActionsEl.replaceChildren(...buttons);

    // renderActions() runs after initShell() already stripped [data-role]
    // elements once at page load, so freshly-built admin buttons need the
    // same gating applied again for operators (same pattern as
    // purchase-order.js/work-order.js).
    if (user.role !== "admin") {
      soActionsEl.querySelectorAll('[data-role="admin"]').forEach((node) => node.remove());
    }
  }

  async function onConfirm() {
    const confirmed = await openConfirmModal({
      title: "Confirm sales order?",
      message: `Confirm ${currentSo.so_number} for ${currentSo.customer.name}? It will no longer be editable.`,
      confirmLabel: "Confirm",
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/sales-orders/${soId}/confirm`);
      toast("Sales order confirmed.", "ok");
      await loadSo();
    } catch (err) {
      handleActionError(err);
    }
  }

  async function onCancel() {
    const confirmed = await openConfirmModal({
      title: "Cancel sales order?",
      message: `Cancel ${currentSo.so_number}? This cannot be undone.`,
      confirmLabel: "Cancel sales order",
      danger: true,
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/sales-orders/${soId}/cancel`);
      toast("Sales order canceled.", "ok");
      await loadSo();
    } catch (err) {
      handleActionError(err);
    }
  }

  /** Ship: any role (the operator loads the truck). Confirm wording states
   * the consequence, per 09-frontend.md's "confirm dialogs state the
   * consequence" rule. On success, stock moved — toast + reload. On 409
   * insufficient_stock, splice the fresh per-line shortfall into the table,
   * show the error banner, then reload so status/buttons/availability stay
   * consistent with the server (same pattern as work-order.js's onComplete). */
  async function onShip() {
    const so = currentSo;
    const confirmed = await openConfirmModal({
      title: "Ship sales order?",
      message: `This will ship ${so.so_number} and remove stock for ${so.line_count} line item${so.line_count === 1 ? "" : "s"}.`,
      confirmLabel: "Ship",
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/sales-orders/${soId}/ship`);
      toast("Sales order shipped.", "ok");
      await loadSo();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.code === "insufficient_stock") {
        applyShortfallDetails(err.details);
        soBanner.hidden = false;
        soBanner.textContent = err.message;
        await loadSo();
      } else {
        handleActionError(err);
      }
    }
  }

  /** Shared error handling for confirm/cancel/ship: any ApiError's message
   * goes on the page-level banner (never alert()); anything else is an
   * unexpected bug, so toast it and rethrow instead of swallowing it. */
  function handleActionError(err) {
    if (err instanceof ApiError) {
      soBanner.hidden = false;
      soBanner.textContent = err.message;
    } else {
      toast("Something went wrong.", "error");
      throw err;
    }
  }

  // -------------------------------------------------------------------------
  // Create / edit mode — shared lines-grid editor
  // -------------------------------------------------------------------------

  function enterCreateMode() {
    soTitleEl.textContent = "New sales order";
    soBadgesEl.replaceChildren();
    soActionsEl.replaceChildren();
    availabilityBanner.hidden = true;
    viewSection.hidden = true;
    viewLinesSection.hidden = true;
    editSection.hidden = false;
    renderEditForm(null);
  }

  function enterEditMode() {
    editSection.hidden = false;
    viewSection.hidden = true;
    viewLinesSection.hidden = true;
    availabilityBanner.hidden = true;
    soActionsEl.replaceChildren();
    renderEditForm(currentSo);
  }

  function exitEditMode() {
    clearBanner();
    renderView(currentSo);
  }

  /**
   * Builds the create/edit form into #so-edit-container: customer select,
   * notes, an editable lines grid (part picker + datalist limited to active
   * finished parts, qty, unit price, computed line total, add/remove row),
   * and a running grand total that updates on input. Fetches active
   * customers + active finished parts fresh on every entry (same
   * "re-fetch rather than cache" choice purchase-order.js's renderEditForm
   * makes — freshness over one saved round trip, fine at demo scale).
   *
   * @param {Object|null} so - existing SO detail to pre-fill (edit mode), or
   *   null for an empty create-mode form.
   */
  async function renderEditForm(so) {
    editContainer.replaceChildren(el("p", { class: "hint-text" }, ["Loading…"]));

    let customers, parts;
    try {
      const [customerData, partData] = await Promise.all([
        api("GET", "/api/customers"), // default active=true — the create-mode picker must be active-only
        api("GET", "/api/parts?part_type=finished"), // active finished parts only — the factory doesn't sell raw stock
      ]);
      customers = customerData.items;
      parts = partData.items;
    } catch {
      toast("Could not load customers/parts for the editor.", "error");
      editContainer.replaceChildren(el("p", { class: "banner -error" }, ["Could not load the editor. Reload the page to try again."]));
      return;
    }

    // A draft/confirmed SO's own customer can never have been deactivated
    // out while this SO exists in one of those statuses (07-sales-orders.md:
    // a 409 blocks deactivating a customer with a draft/confirmed SO), so
    // `so`'s customer is guaranteed to already be in the active list when
    // editing.

    clearBanner();

    // --- Customer + notes fields ------------------------------------------
    const customerError = el("div", { class: "field-error" });
    const customerSelect = el(
      "select",
      { required: true },
      customers.map((c) => el("option", { value: String(c.id) }, [c.name]))
    );
    if (so) customerSelect.value = String(so.customer.id);

    const notesError = el("div", { class: "field-error" });
    const notesInput = el("textarea", { value: so ? so.notes || "" : "" });

    if (!customers.length) {
      customerSelect.disabled = true;
      customerError.textContent = "No active customers — add one on the Customers page first.";
    }

    // --- Lines grid ---------------------------------------------------------
    const datalistId = "so-part-picker";
    const datalist = el(
      "datalist",
      { id: datalistId },
      parts.map((p) => el("option", { value: p.sku }, [p.name]))
    );

    // SKU -> part lookup for client-side validation + line-total math. Seeded
    // from every active finished part, plus (edit mode) the SO's *current*
    // lines, so an unchanged line referencing a part deactivated after the
    // SO was created still resolves here (same fallback purchase-order.js's
    // renderEditForm uses; the server's own validation still applies when
    // the line is saved).
    const lookup = new Map();
    for (const p of parts) lookup.set(p.sku.toUpperCase(), p);
    if (so) {
      for (const line of so.lines) {
        if (!lookup.has(line.sku.toUpperCase())) {
          lookup.set(line.sku.toUpperCase(), { id: line.part_id, sku: line.sku, name: line.name, unit: line.unit });
        }
      }
    }

    const rows = []; // {rowEl, partInput, partError, qtyInput, qtyError, priceInput, priceError, totalCell}
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
        const price = Number(row.priceInput.value) || 0;
        const lineTotal = qty * price;
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
      // to wire up list="..." (same note as purchase-order.js's grid).
      partInput.setAttribute("list", datalistId);
      const partError = el("div", { class: "field-error" });

      const qtyInput = el("input", {
        type: "number",
        step: "0.01",
        min: "0.01",
        required: true,
        value: line ? line.qty : "",
      });
      const qtyError = el("div", { class: "field-error" });

      const priceInput = el("input", {
        type: "number",
        step: "0.01",
        min: "0",
        required: true,
        value: line ? line.unit_price : "",
      });
      const priceError = el("div", { class: "field-error" });

      const totalCell = el("td", { class: "num" }, [fmtMoney(line ? line.qty * line.unit_price : 0)]);
      const removeBtn = el("button", { type: "button", class: "btn btn-ghost", "aria-label": "Remove line" }, [
        "Remove",
      ]);

      qtyInput.addEventListener("input", recomputeTotal);
      priceInput.addEventListener("input", recomputeTotal);

      const rowEl = el("tr", {}, [
        el("td", {}, [partInput, partError]),
        el("td", {}, [qtyInput, qtyError]),
        el("td", {}, [priceInput, priceError]),
        totalCell,
        el("td", {}, [removeBtn]),
      ]);

      const row = { rowEl, partInput, partError, qtyInput, qtyError, priceInput, priceError, totalCell };
      removeBtn.addEventListener("click", () => {
        rows.splice(rows.indexOf(row), 1);
        rowEl.remove();
        recomputeTotal();
      });

      rows.push(row);
      gridTbody.appendChild(rowEl);
    }

    if (so && so.lines.length) {
      for (const line of so.lines) addRow(line);
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
            el("th", { class: "num" }, ["Unit price"]),
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
      if (so) {
        exitEditMode();
      } else {
        location.href = "/sales-orders.html";
      }
    });
    saveBtn.addEventListener("click", () => save());

    async function save() {
      clearBanner();
      customerError.textContent = "";
      notesError.textContent = "";
      for (const row of rows) {
        row.partError.textContent = "";
        row.qtyError.textContent = "";
        row.priceError.textContent = "";
      }

      // --- client-side validation, mirrors what the server checks per
      // 07-sales-orders.md: at least one line, each part known+active+finished,
      // qty > 0, unit_price >= 0, no duplicate parts.
      if (!customerSelect.value) {
        customerError.textContent = "Customer is required.";
        return;
      }
      if (!rows.length) {
        soBanner.hidden = false;
        soBanner.textContent = "Add at least one line.";
        return;
      }

      const lines = [];
      const seenPartIds = new Set();
      let hasError = false;

      rows.forEach((row) => {
        const skuRaw = row.partInput.value.trim();
        if (!skuRaw) {
          row.partError.textContent = "Part required.";
          hasError = true;
          return;
        }
        const part = lookup.get(skuRaw.toUpperCase());
        if (!part) {
          row.partError.textContent = "Unknown or inactive/non-finished SKU.";
          hasError = true;
          return;
        }
        if (seenPartIds.has(part.id)) {
          row.partError.textContent = "Duplicate part.";
          hasError = true;
          return;
        }

        const qty = Number(row.qtyInput.value);
        if (!row.qtyInput.value || Number.isNaN(qty) || qty <= 0) {
          row.qtyError.textContent = "Qty must be greater than 0.";
          hasError = true;
          return;
        }

        const unitPrice = Number(row.priceInput.value);
        if (row.priceInput.value === "" || Number.isNaN(unitPrice) || unitPrice < 0) {
          row.priceError.textContent = "Unit price must be 0 or more.";
          hasError = true;
          return;
        }

        seenPartIds.add(part.id);
        lines.push({ part_id: part.id, qty, unit_price: unitPrice });
      });

      if (hasError) return;

      const payload = {
        customer_id: Number(customerSelect.value),
        notes: notesInput.value || undefined,
        lines,
      };

      saveBtn.disabled = true;
      cancelBtn.disabled = true;
      try {
        if (so) {
          const updated = await api("PUT", `/api/sales-orders/${soId}`, payload);
          currentSo = updated;
          toast("Sales order updated.", "ok");
          exitEditMode();
        } else {
          const created = await api("POST", "/api/sales-orders", payload);
          toast("Sales order created.", "ok");
          location.href = `/sales-order.html?id=${created.id}`;
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 400) {
          soBanner.hidden = false;
          soBanner.textContent = err.message || "Fix the highlighted fields below.";
          if (err.fieldErrors.customer_id) customerError.textContent = err.fieldErrors.customer_id;
          if (err.fieldErrors.notes) notesError.textContent = err.fieldErrors.notes;
          applyLineDetailErrors(err.details, rows);
        } else if (err instanceof ApiError) {
          soBanner.hidden = false;
          soBanner.textContent = err.message;
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
      el("div", { class: "field" }, [el("label", {}, ["Customer"]), customerSelect, customerError]),
      el("div", { class: "field" }, [el("label", {}, ["Notes"]), notesInput, notesError]),
      linesTable,
      actions
    );
  }

  /** Matches a 400 response's `details: [{line, field, message}]` (per
   * 07-sales-orders.md — `line` is the 0-based index into the submitted
   * `lines` array, which is built in the same order as `rows`) back to the
   * offending row's error slot. When `field` is present the message goes
   * under that specific input (part_id/qty/unit_price); otherwise it falls
   * back to the part-picker slot so the row still shows *something* (e.g.
   * the "each line must be an object" case, which has no field). */
  function applyLineDetailErrors(details, rows) {
    if (!Array.isArray(details)) return;
    for (const detail of details) {
      if (!detail || typeof detail.line !== "number") continue;
      const row = rows[detail.line];
      if (!row || !detail.message) continue;
      if (detail.field === "qty") {
        row.qtyError.textContent = detail.message;
      } else if (detail.field === "unit_price") {
        row.priceError.textContent = detail.message;
      } else {
        // field === "part_id" or absent.
        row.partError.textContent = detail.message;
      }
    }
  }

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------

  if (isCreate) {
    soContent.hidden = false;
    enterCreateMode();
  } else {
    loadSo();
  }
}
