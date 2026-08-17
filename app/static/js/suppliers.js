/**
 * suppliers.js — page logic for suppliers.html, the supplier directory.
 *
 * Follows the contract documented at the top of app.js: call initShell()
 * first and only render once it resolves with a user (a null return means
 * initShell() already redirected to /login.html — a bare top-level `return`
 * is a SyntaxError in an ES module, so we guard the rest of the file with
 * `if (user) { ... }` instead, per app.js's documented pattern).
 *
 * Row-click behavior by role (requirements/06-purchasing.md leaves this to
 * the implementer: "row click opens an admin edit modal ... Operators get a
 * read-only view (no modals — row click can just do nothing or show a
 * read-only modal; your judgment, document it)"):
 *   - Admin: opens `openSupplierModal()` in editable mode — name/contact/
 *     email/phone are inputs, Save issues PUT, and a Deactivate/Activate
 *     button is offered.
 *   - Operator: opens the SAME modal in read-only mode (fields rendered as
 *     text, no Save/Deactivate) rather than doing nothing. Chosen over "row
 *     click does nothing" because the modal's other payload — the supplier's
 *     purchase-order history — is useful read-only information for an
 *     operator too (e.g. "who do we buy this from, and what's on order"),
 *     and the API already exposes it on GET /api/suppliers/{id} for any role.
 *
 * The supplier detail modal is custom-built here (not via modal.js's
 * openFormModal) because it mixes an editable form with a read-only
 * purchase-order link list and a role-conditional action button — shapes
 * openFormModal's fixed field list doesn't support. It reuses the same
 * <dialog> primitive and .modal-* CSS classes as modal.js for visual/
 * behavioral consistency (Esc/backdrop close, focus restore on close) without
 * modifying modal.js, which this task doesn't own.
 */

import { initShell, api, toast, fmtMoney, fmtDate, qs, setQs, el, ApiError } from "./app.js";
import { openFormModal, openConfirmModal } from "./modal.js";

const user = await initShell();
if (user) {
  main(user);
}

/**
 * @param {{id:number, username:string, role:string}} user
 */
function main(user) {
  const tbody = document.getElementById("suppliers-tbody");
  const searchInput = document.getElementById("search-input");
  const newSupplierBtn = document.getElementById("new-supplier-btn");
  const pageBanner = document.getElementById("page-banner");

  // --- Initialize the search box from the current query string, so a
  // filtered view stays linkable (09-frontend.md's qs()/setQs() convention).
  searchInput.value = qs().search || "";

  let searchDebounce = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      setQs({ search: searchInput.value.trim() || null });
      loadSuppliers();
    }, 300);
  });

  if (newSupplierBtn) {
    newSupplierBtn.addEventListener("click", () => openNewSupplierModal());
  }

  /** Opens the shared form modal for POST /api/suppliers, per 06-purchasing.md
   * (name required/unique; contact_name/email/phone optional). */
  async function openNewSupplierModal() {
    const result = await openFormModal({
      title: "New supplier",
      submitLabel: "Create",
      fields: [
        { name: "name", label: "Name", required: true },
        { name: "contact_name", label: "Contact name" },
        { name: "email", label: "Email" },
        { name: "phone", label: "Phone" },
      ],
      onSubmit: (values) =>
        api("POST", "/api/suppliers", {
          name: values.name,
          contact_name: values.contact_name || undefined,
          email: values.email || undefined,
          phone: values.phone || undefined,
        }),
    });

    if (result) {
      toast("Supplier created.", "ok");
      loadSuppliers();
    }
  }

  /** GET /api/suppliers query: `active=all` so both active and inactive rows
   * render together (the inactive ones badged) — the list itself is the
   * only place an admin can find and reactivate an inactive supplier. */
  async function loadSuppliers() {
    const params = new URLSearchParams({ active: "all" });
    const search = searchInput.value.trim();
    if (search) params.set("search", search);

    let data;
    try {
      data = await api("GET", `/api/suppliers?${params.toString()}`);
    } catch {
      toast("Could not load suppliers.", "error");
      return;
    }
    renderRows(data.items);
  }

  /** @param {Array<Object>} items - supplier list items, shape per GET /api/suppliers. */
  function renderRows(items) {
    tbody.replaceChildren();

    if (!items.length) {
      tbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 5 }, ["No suppliers match."])]));
      return;
    }

    for (const supplier of items) {
      const nameCell = [supplier.name];
      if (!supplier.active) nameCell.push(" ", el("span", { class: "badge -draft" }, ["Inactive"]));

      const row = el(
        "tr",
        {
          dataset: { href: `#${supplier.id}` },
          onClick: () => openSupplierModal(supplier.id),
        },
        [
          el("td", {}, nameCell),
          el("td", {}, [supplier.contact_name || "—"]),
          el("td", {}, [supplier.email || "—"]),
          el("td", {}, [supplier.phone || "—"]),
          el("td", {}, []),
        ]
      );
      tbody.appendChild(row);
    }
  }

  // ---------------------------------------------------------------------
  // Supplier detail modal — GET /api/suppliers/{id}, edit form (admin) or
  // read-only view (operator), plus the supplier's PO history either way.
  // ---------------------------------------------------------------------

  async function openSupplierModal(supplierId) {
    let supplier;
    try {
      supplier = await api("GET", `/api/suppliers/${supplierId}`);
    } catch {
      toast("Could not load supplier.", "error");
      return;
    }

    const previousActive = document.activeElement;
    const titleId = `supplier-modal-title-${supplierId}`;
    const heading = el("h2", { id: titleId, class: "modal-title" }, [supplier.name]);
    const closeBtn = el("button", { type: "button", class: "modal-close", "aria-label": "Close" }, ["×"]);
    const header = el("div", { class: "modal-header" }, [heading, closeBtn]);

    const banner = el("div", { class: "banner -error", hidden: true });
    const body = user.role === "admin" ? buildEditableBody(supplier) : buildReadOnlyBody(supplier);

    const dialog = el("dialog", { class: "modal", "aria-labelledby": titleId }, [header, banner, body]);
    document.body.appendChild(dialog);

    closeBtn.addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener(
      "close",
      () => {
        dialog.remove();
        if (previousActive instanceof HTMLElement) previousActive.focus();
      },
      { once: true }
    );

    dialog.showModal();

    /** Deactivate/Activate needs its own confirm dialog (via modal.js's
     * openConfirmModal). Rather than stack a second native <dialog> on top of
     * this one, close this modal first, then confirm, then act — simpler
     * than reasoning about nested modal focus/backdrop behavior, and the
     * page-level banner covers the 409 case once this modal is gone. */
    const toggleBtn = body.querySelector("[data-action=toggle-active]");
    if (toggleBtn) {
      toggleBtn.addEventListener("click", async () => {
        dialog.close();
        await onToggleActive(supplier);
      });
    }

    const saveBtn = body.querySelector("[data-action=save]");
    if (saveBtn) {
      const form = body.querySelector("form");
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        saveSupplier(supplier, dialog, form, banner);
      });
    }
  }

  /** Admin body: editable name/contact/email/phone, PO link list, Deactivate/
   * Activate button. Wrapped in a <form> so Enter submits it like every other
   * form modal in the app. */
  function buildEditableBody(supplier) {
    const nameInput = el("input", { type: "text", required: true, value: supplier.name });
    const nameError = el("div", { class: "field-error" });
    const contactInput = el("input", { type: "text", value: supplier.contact_name || "" });
    const emailInput = el("input", { type: "text", value: supplier.email || "" });
    const phoneInput = el("input", { type: "text", value: supplier.phone || "" });

    const fields = el("div", {}, [
      el("div", { class: "field" }, [el("label", {}, ["Name"]), nameInput, nameError]),
      el("div", { class: "field" }, [el("label", {}, ["Contact name"]), contactInput]),
      el("div", { class: "field" }, [el("label", {}, ["Email"]), emailInput]),
      el("div", { class: "field" }, [el("label", {}, ["Phone"]), phoneInput]),
    ]);

    const toggleBtn = el(
      "button",
      {
        type: "button",
        class: supplier.active ? "btn btn-danger" : "btn",
        dataset: { action: "toggle-active" },
      },
      [supplier.active ? "Deactivate" : "Activate"]
    );
    const saveBtn = el("button", { type: "submit", class: "btn btn-primary", dataset: { action: "save" } }, ["Save"]);
    const actions = el("div", { class: "modal-actions" }, [toggleBtn, saveBtn]);

    const form = el("form", { class: "modal-form" }, [fields, buildPoList(supplier.purchase_orders), actions]);
    // Stash input refs on the form node itself so saveSupplier() (wired up by
    // the caller, openSupplierModal()) can read current values without a
    // second pass of DOM queries.
    form._nameInput = nameInput;
    form._contactInput = contactInput;
    form._emailInput = emailInput;
    form._phoneInput = phoneInput;
    form._nameError = nameError;
    return form;
  }

  /** Operator body: name/contact/email/phone as plain text, PO link list,
   * no form and no action button — nothing here is editable. */
  function buildReadOnlyBody(supplier) {
    const dl = el("dl", { class: "wo-meta" }, [
      el("div", {}, [el("dt", {}, ["Contact"]), el("dd", {}, [supplier.contact_name || "—"])]),
      el("div", {}, [el("dt", {}, ["Email"]), el("dd", {}, [supplier.email || "—"])]),
      el("div", {}, [el("dt", {}, ["Phone"]), el("dd", {}, [supplier.phone || "—"])]),
      el("div", {}, [
        el("dt", {}, ["Status"]),
        el("dd", {}, [
          supplier.active
            ? el("span", { class: "badge -completed" }, ["Active"])
            : el("span", { class: "badge -draft" }, ["Inactive"]),
        ]),
      ]),
    ]);
    return el("div", { class: "modal-form" }, [dl, buildPoList(supplier.purchase_orders)]);
  }

  /** Read-only link list of the supplier's purchase orders (list shape per
   * GET /api/purchase-orders, newest first already per 06-purchasing.md). */
  function buildPoList(purchaseOrders) {
    const heading = el("h3", {}, ["Purchase orders"]);
    if (!purchaseOrders || !purchaseOrders.length) {
      return el("div", {}, [heading, el("p", { class: "hint-text" }, ["No purchase orders yet."])]);
    }
    const items = purchaseOrders.map((po) =>
      el("li", { class: "link-list-item" }, [
        el("a", { href: `/purchase-order.html?id=${po.id}` }, [po.po_number]),
        " — ",
        el("span", { class: `badge -${po.status}` }, [capitalize(po.status)]),
        " — ",
        fmtMoney(po.total),
        " — ",
        fmtDate(po.created_at),
      ])
    );
    return el("div", {}, [heading, el("ul", { class: "link-list" }, items)]);
  }

  async function saveSupplier(supplier, dialog, form, banner) {
    banner.hidden = true;
    banner.textContent = "";
    form._nameError.textContent = "";

    const saveBtn = form.querySelector("[data-action=save]");
    const toggleBtn = form.querySelector("[data-action=toggle-active]");
    saveBtn.disabled = true;
    if (toggleBtn) toggleBtn.disabled = true;

    try {
      const updated = await api("PUT", `/api/suppliers/${supplier.id}`, {
        name: form._nameInput.value,
        contact_name: form._contactInput.value || undefined,
        email: form._emailInput.value || undefined,
        phone: form._phoneInput.value || undefined,
      });
      toast("Supplier updated.", "ok");
      dialog.close();
      loadSuppliers();
      return updated;
    } catch (err) {
      if (err instanceof ApiError && err.status === 400 && Object.keys(err.fieldErrors).length) {
        if (err.fieldErrors.name) form._nameError.textContent = err.fieldErrors.name;
      } else if (err instanceof ApiError) {
        banner.hidden = false;
        banner.textContent = err.message;
      } else {
        toast("Something went wrong.", "error");
        throw err;
      }
    } finally {
      saveBtn.disabled = false;
      if (toggleBtn) toggleBtn.disabled = false;
    }
  }

  /** Deactivate (DELETE, soft delete) / Activate (POST .../activate). Runs
   * after the detail modal is already closed (see openSupplierModal), so a
   * 409 conflict (supplier has a draft/ordered PO) surfaces as a page-level
   * banner as well as a toast, per the task's "409 conflict → banner/toast". */
  async function onToggleActive(supplier) {
    const activating = !supplier.active;
    const confirmed = await openConfirmModal({
      title: activating ? "Activate supplier?" : "Deactivate supplier?",
      message: activating
        ? `${supplier.name} will reappear in the active-supplier picker for new purchase orders.`
        : `${supplier.name} will disappear from the active-supplier picker for new purchase orders. Its existing purchase orders are unaffected.`,
      confirmLabel: activating ? "Activate" : "Deactivate",
      danger: !activating,
    });
    if (!confirmed) return;

    pageBanner.hidden = true;
    try {
      if (activating) {
        await api("POST", `/api/suppliers/${supplier.id}/activate`);
      } else {
        await api("DELETE", `/api/suppliers/${supplier.id}`);
      }
      toast(activating ? "Supplier activated." : "Supplier deactivated.", "ok");
      loadSuppliers();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        pageBanner.hidden = false;
        pageBanner.textContent = err.message || "This supplier has an open purchase order.";
        toast(err.message || "Could not deactivate: supplier is in use.", "error");
      } else if (err instanceof ApiError) {
        toast(err.message, "error");
      } else {
        toast("Something went wrong.", "error");
        throw err;
      }
    }
  }

  function capitalize(s) {
    return s ? s[0].toUpperCase() + s.slice(1) : s;
  }

  loadSuppliers();
}
