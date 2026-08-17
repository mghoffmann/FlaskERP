/**
 * work-order.js — page logic for work-order.html, the work order detail page.
 *
 * Sections, per requirements/05-work-orders.md:
 *   - Header: WO number, big status badge, product link, qty, notes,
 *     created-by, timestamps.
 *   - Availability banner: green "Ready to build" when `can_complete`, amber
 *     "Short N components" when released but short; hidden for draft/
 *     completed/canceled (the components table itself already shows
 *     availability numbers, which is enough info at draft).
 *   - Components table: Component / Qty per / Required / On hand / Short,
 *     with short>0 cells highlighted red.
 *   - Action buttons by status/role: draft -> Edit/Release/Cancel (admin);
 *     released -> Complete build (any role) + Cancel (admin);
 *     completed/canceled -> none.
 *
 * Follows the initShell() contract from app.js: a bare top-level `return` is
 * illegal in an ES module, so the whole page body is wrapped in
 * `if (user) { ... }` instead of returning early.
 */

import { initShell, api, toast, fmtQty, fmtDateTime, qs, el, ApiError } from "./app.js";
import { openFormModal, openConfirmModal } from "./modal.js";

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
  const woId = Number(rawId);

  // --- DOM refs -------------------------------------------------------------
  const notFoundBanner = document.getElementById("not-found-banner");
  const woContent = document.getElementById("wo-content");
  const woBanner = document.getElementById("wo-banner");
  const availabilityBanner = document.getElementById("availability-banner");

  const woTitleEl = document.getElementById("wo-title");
  const woBadgesEl = document.getElementById("wo-badges");
  const woActionsEl = document.getElementById("wo-actions");

  const productEl = document.getElementById("wo-product");
  const qtyEl = document.getElementById("wo-qty");
  const notesEl = document.getElementById("wo-notes");
  const createdByEl = document.getElementById("wo-created-by");
  const createdAtEl = document.getElementById("wo-created-at");
  const releasedAtEl = document.getElementById("wo-released-at");
  const completedAtEl = document.getElementById("wo-completed-at");

  const componentsTbody = document.getElementById("components-tbody");

  if (!rawId || Number.isNaN(woId)) {
    notFoundBanner.hidden = false;
    woContent.hidden = true;
    return;
  }

  /** Current work order detail (list shape + components[] + can_complete),
   * refreshed after every load/mutation. */
  let currentWo = null;

  // -------------------------------------------------------------------------
  // Load
  // -------------------------------------------------------------------------

  async function loadWo() {
    try {
      currentWo = await api("GET", `/api/work-orders/${woId}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        notFoundBanner.hidden = false;
        woContent.hidden = true;
        return;
      }
      toast("Could not load work order.", "error");
      return;
    }

    notFoundBanner.hidden = true;
    woContent.hidden = false;

    renderHeader(currentWo);
    renderAvailabilityBanner(currentWo);
    renderComponents(currentWo.components);
    renderActions(currentWo);
  }

  // -------------------------------------------------------------------------
  // Header
  // -------------------------------------------------------------------------

  function renderHeader(wo) {
    woTitleEl.textContent = wo.wo_number;

    const statusBadge = el("span", { class: `badge -${wo.status} wo-status-badge` }, [capitalize(wo.status)]);
    woBadgesEl.replaceChildren(statusBadge);

    productEl.replaceChildren(
      el("a", { href: `/part.html?id=${wo.product.id}` }, [`${wo.product.sku} — ${wo.product.name}`])
    );
    qtyEl.textContent = `${fmtQty(wo.qty)} ${wo.product.unit}`;
    notesEl.textContent = wo.notes || "—";
    createdByEl.textContent = wo.created_by_username;
    createdAtEl.textContent = fmtDateTime(wo.created_at);
    releasedAtEl.textContent = wo.released_at ? fmtDateTime(wo.released_at) : "—";
    completedAtEl.textContent = wo.completed_at ? fmtDateTime(wo.completed_at) : "—";
  }

  // -------------------------------------------------------------------------
  // Availability banner — released only; draft/completed/canceled get none
  // (the components table's numbers are informational enough at draft).
  // -------------------------------------------------------------------------

  function renderAvailabilityBanner(wo) {
    if (wo.status !== "released") {
      availabilityBanner.hidden = true;
      return;
    }

    availabilityBanner.hidden = false;
    if (wo.can_complete) {
      availabilityBanner.className = "banner -ok";
      availabilityBanner.textContent = "Ready to build.";
    } else {
      const shortCount = wo.components.filter((c) => c.short > 0).length;
      availabilityBanner.className = "banner -warn";
      availabilityBanner.textContent = `Short ${shortCount} component${shortCount === 1 ? "" : "s"}.`;
    }
  }

  // -------------------------------------------------------------------------
  // Components table
  // -------------------------------------------------------------------------

  /** @param {Array<{part_id, sku, name, unit, qty_per, required, on_hand, short}>} components */
  function renderComponents(components) {
    componentsTbody.replaceChildren();

    if (!components.length) {
      componentsTbody.appendChild(
        el("tr", { class: "empty-row" }, [el("td", { colSpan: 5 }, ["No components (empty BOM)."])])
      );
      return;
    }

    for (const c of components) {
      componentsTbody.appendChild(buildComponentRow(c));
    }
  }

  /** Builds one component row; tagged with data-* (raw, unformatted values)
   * so a post-Complete 409's shortfall details can be spliced back into the
   * right row without re-parsing formatted text (see applyShortfallDetails). */
  function buildComponentRow(c) {
    const short = Number(c.short) > 0;
    return el(
      "tr",
      { dataset: { partId: c.part_id, sku: c.sku, name: c.name, qtyPer: c.qty_per } },
      [
        el("td", {}, [el("a", { href: `/part.html?id=${c.part_id}` }, [`${c.sku} — ${c.name}`])]),
        el("td", { class: "num" }, [fmtQty(c.qty_per)]),
        el("td", { class: "num" }, [fmtQty(c.required)]),
        el("td", { class: "num" }, [fmtQty(c.on_hand)]),
        el("td", { class: `num${short ? " -short" : ""}` }, [fmtQty(c.short)]),
      ]
    );
  }

  /**
   * Splices a Complete-build 409 insufficient_stock response's `details`
   * (fresh {part_id, sku, required, on_hand, short} rows, per
   * requirements/05-work-orders.md) into the components table in place, so
   * the shortfall the user just hit is highlighted with server-fresh numbers
   * even before the full reload (triggered right after by the caller)
   * repaints everything from GET /api/work-orders/{id}). sku/name/qty_per
   * aren't part of the 409 detail row's shape, so they're pulled back out of
   * the existing row's dataset rather than parsed from formatted text.
   */
  function applyShortfallDetails(details) {
    if (!Array.isArray(details)) return;
    for (const d of details) {
      const row = componentsTbody.querySelector(`tr[data-part-id="${d.part_id}"]`);
      if (!row) continue;
      row.replaceWith(
        buildComponentRow({
          part_id: d.part_id,
          sku: d.sku || row.dataset.sku,
          name: row.dataset.name,
          qty_per: row.dataset.qtyPer,
          required: d.required,
          on_hand: d.on_hand,
          short: d.short,
        })
      );
    }
  }

  // -------------------------------------------------------------------------
  // Actions — draft: Edit/Release/Cancel (admin); released: Complete build
  // (any role) + Cancel (admin); completed/canceled: none.
  // -------------------------------------------------------------------------

  function renderActions(wo) {
    const buttons = [];

    if (wo.status === "draft") {
      buttons.push(
        el("button", { type: "button", class: "btn", "data-role": "admin", onClick: openEditModal }, ["Edit"]),
        el("button", { type: "button", class: "btn btn-primary", "data-role": "admin", onClick: onRelease }, [
          "Release",
        ]),
        el("button", { type: "button", class: "btn btn-danger", "data-role": "admin", onClick: onCancel }, [
          "Cancel",
        ])
      );
    } else if (wo.status === "released") {
      buttons.push(
        el("button", { type: "button", class: "btn btn-primary", onClick: onComplete }, ["Complete build"]),
        el("button", { type: "button", class: "btn btn-danger", "data-role": "admin", onClick: onCancel }, [
          "Cancel",
        ])
      );
    }

    woActionsEl.replaceChildren(...buttons);

    // renderActions() runs after initShell() already stripped [data-role]
    // elements once at page load, so freshly-built admin buttons need the
    // same gating applied again for operators.
    if (user.role !== "admin") {
      woActionsEl.querySelectorAll('[data-role="admin"]').forEach((node) => node.remove());
    }
  }

  function clearBanner() {
    woBanner.hidden = true;
    woBanner.textContent = "";
  }

  async function openEditModal() {
    const wo = currentWo;
    let products;
    try {
      const data = await api("GET", "/api/parts?part_type=finished");
      products = data.items;
    } catch {
      toast("Could not load products.", "error");
      return;
    }

    const result = await openFormModal({
      title: "Edit work order",
      fields: [
        {
          name: "product_part_id",
          label: "Product",
          type: "select",
          required: true,
          value: String(wo.product.id),
          options: products.map((p) => ({ value: String(p.id), label: `${p.sku} — ${p.name}` })),
        },
        { name: "qty", label: "Qty", type: "number", step: "0.01", min: "0.01", required: true, value: wo.qty },
        { name: "notes", label: "Notes", type: "textarea", value: wo.notes || "" },
      ],
      onSubmit: (values) =>
        api("PUT", `/api/work-orders/${woId}`, {
          product_part_id: Number(values.product_part_id),
          qty: Number(values.qty),
          notes: values.notes || undefined,
        }),
    });

    if (result) {
      toast("Work order updated.", "ok");
      await loadWo();
    }
  }

  async function onRelease() {
    const confirmed = await openConfirmModal({
      title: "Release work order?",
      message: `Release ${currentWo.wo_number}? It will no longer be editable.`,
      confirmLabel: "Release",
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/work-orders/${woId}/release`);
      toast("Work order released.", "ok");
      await loadWo();
    } catch (err) {
      handleActionError(err);
    }
  }

  async function onCancel() {
    const confirmed = await openConfirmModal({
      title: "Cancel work order?",
      message: `Cancel ${currentWo.wo_number}? This cannot be undone.`,
      confirmLabel: "Cancel work order",
      danger: true,
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/work-orders/${woId}/cancel`);
      toast("Work order canceled.", "ok");
      await loadWo();
    } catch (err) {
      handleActionError(err);
    }
  }

  /** Complete build: any role. On success, stock moved — toast + reload. On
   * 409 insufficient_stock, splice the fresh per-component shortfall into the
   * table, show the error banner, then reload so status/buttons/can_complete
   * stay consistent with the server (requirements/05-work-orders.md: "the
   * on-screen numbers may be stale — this is the fresh truth from the
   * server"). */
  async function onComplete() {
    const wo = currentWo;
    const confirmed = await openConfirmModal({
      title: "Complete build?",
      message: `This will consume components and add ${fmtQty(wo.qty)} × ${wo.product.sku} — ${wo.product.name} to stock.`,
      confirmLabel: "Complete build",
    });
    if (!confirmed) return;

    clearBanner();
    try {
      await api("POST", `/api/work-orders/${woId}/complete`);
      toast("Work order completed.", "ok");
      await loadWo();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.code === "insufficient_stock") {
        applyShortfallDetails(err.details);
        woBanner.hidden = false;
        woBanner.textContent = err.message;
        await loadWo();
      } else {
        handleActionError(err);
      }
    }
  }

  /** Shared error handling for release/cancel/complete: any ApiError's
   * message goes on the page-level banner (never alert()); anything else is
   * an unexpected bug, so toast it and rethrow instead of swallowing it. */
  function handleActionError(err) {
    if (err instanceof ApiError) {
      woBanner.hidden = false;
      woBanner.textContent = err.message;
    } else {
      toast("Something went wrong.", "error");
      throw err;
    }
  }

  loadWo();
}
