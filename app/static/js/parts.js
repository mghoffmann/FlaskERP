/**
 * parts.js — page logic for parts.html, the parts catalog list.
 *
 * Follows the contract documented at the top of app.js: call initShell()
 * first and only render once it resolves with a user (a null return means
 * initShell() already redirected to /login.html — a bare top-level `return`
 * is a SyntaxError in an ES module, so we guard the rest of the file with
 * `if (user) { ... }` instead, per app.js's documented pattern).
 *
 * Filters (search / part_type / low_stock) are round-tripped through the
 * query string via qs()/setQs() so a link like /parts.html?low_stock=true
 * (as used by the dashboard's low-stock KPI) initializes the toolbar and the
 * list in the filtered state, and every filter change updates the URL so the
 * current view stays bookmarkable — requirements/03-inventory.md +
 * requirements/09-frontend.md.
 */

import { initShell, api, toast, fmtQty, fmtMoney, qs, setQs, el } from "./app.js";
import { openFormModal } from "./modal.js";

const user = await initShell();
if (user) {
  main(user);
}

/**
 * @param {{id:number, username:string, role:string}} user
 */
function main(user) {
  const tbody = document.getElementById("parts-tbody");
  const searchInput = document.getElementById("search-input");
  const typeFilter = document.getElementById("type-filter");
  const lowStockFilter = document.getElementById("low-stock-filter");
  const newPartBtn = document.getElementById("new-part-btn");

  // --- Initialize toolbar controls from the current query string --------
  const initialParams = qs();
  searchInput.value = initialParams.search || "";
  typeFilter.value = initialParams.part_type || "";
  lowStockFilter.checked = initialParams.low_stock === "true";

  // --- Wire up filter controls -------------------------------------------

  // Debounced search: wait for a pause in typing before hitting the API, so
  // we don't fire a request per keystroke.
  let searchDebounce = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      setQs({ search: searchInput.value.trim() || null });
      loadParts();
    }, 300);
  });

  typeFilter.addEventListener("change", () => {
    setQs({ part_type: typeFilter.value || null });
    loadParts();
  });

  lowStockFilter.addEventListener("change", () => {
    setQs({ low_stock: lowStockFilter.checked ? "true" : null });
    loadParts();
  });

  // "New part" is only present for admins (data-role="admin" is stripped for
  // operators by initShell()'s role gating), but guard anyway in case this
  // module ever runs before that DOM mutation settles.
  if (newPartBtn) {
    newPartBtn.addEventListener("click", () => openNewPartModal());
  }

  /** Opens the shared form modal for POST /api/parts, per 03-inventory.md. */
  async function openNewPartModal() {
    const result = await openFormModal({
      title: "New part",
      submitLabel: "Create",
      fields: [
        { name: "sku", label: "SKU", required: true },
        { name: "name", label: "Name", required: true },
        {
          name: "part_type",
          label: "Type",
          type: "select",
          required: true,
          options: [
            { value: "raw", label: "Raw" },
            { value: "finished", label: "Finished" },
          ],
        },
        { name: "unit", label: "Unit", value: "ea" },
        { name: "reorder_point", label: "Reorder point", type: "number", step: "0.01", min: "0", value: 0 },
        { name: "unit_cost", label: "Unit cost", type: "number", step: "0.01", min: "0", value: 0 },
      ],
      onSubmit: (values) =>
        api("POST", "/api/parts", {
          sku: values.sku,
          name: values.name,
          part_type: values.part_type,
          unit: values.unit || undefined,
          reorder_point: values.reorder_point === "" ? undefined : Number(values.reorder_point),
          unit_cost: values.unit_cost === "" ? undefined : Number(values.unit_cost),
        }),
    });

    if (result) {
      toast("Part created.", "ok");
      loadParts();
    }
  }

  /** Builds the /api/parts query string from the current toolbar state. */
  function buildQuery() {
    const params = {};
    if (searchInput.value.trim()) params.search = searchInput.value.trim();
    if (typeFilter.value) params.part_type = typeFilter.value;
    if (lowStockFilter.checked) params.low_stock = "true";
    return params;
  }

  async function loadParts() {
    const query = new URLSearchParams(buildQuery()).toString();
    let data;
    try {
      data = await api("GET", `/api/parts${query ? `?${query}` : ""}`);
    } catch (err) {
      toast("Could not load parts.", "error");
      return;
    }
    renderRows(data.items);
  }

  /** @param {Array<Object>} items - part list items, shape per GET /api/parts. */
  function renderRows(items) {
    tbody.replaceChildren();

    if (!items.length) {
      tbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 6 }, ["No parts match."])]));
      return;
    }

    for (const part of items) {
      const nameCell = [part.name];
      if (part.low_stock) nameCell.push(" ", el("span", { class: "badge -warn" }, ["Low stock"]));
      if (!part.active) nameCell.push(" ", el("span", { class: "badge -draft" }, ["Inactive"]));

      const row = el(
        "tr",
        {
          // data-href is purely a styling hook (see `tbody tr[data-href]` in
          // style.css for the pointer cursor); the actual navigation happens
          // via the click handler below.
          dataset: { href: `/part.html?id=${part.id}` },
          onClick: () => {
            location.href = `/part.html?id=${part.id}`;
          },
        },
        [
          el("td", {}, [part.sku]),
          el("td", {}, nameCell),
          el("td", {}, [part.part_type === "finished" ? "Finished" : "Raw"]),
          el("td", { class: "num" }, [`${fmtQty(part.qty_on_hand)} ${part.unit}`]),
          el("td", { class: "num" }, [fmtQty(part.reorder_point)]),
          el("td", { class: "num" }, [fmtMoney(part.unit_cost)]),
        ]
      );
      tbody.appendChild(row);
    }
  }

  loadParts();
}
