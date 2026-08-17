/**
 * customers.js — page logic for customers.html, the customer directory.
 *
 * Follows the contract documented at the top of app.js: call initShell()
 * first and only render once it resolves with a user (a null return means
 * initShell() already redirected to /login.html — a bare top-level `return`
 * is a SyntaxError in an ES module, so we guard the rest of the file with
 * `if (user) { ... }` instead, per app.js's documented pattern).
 *
 * This is the sales-side twin of js/suppliers.js — requirements/07-sales-orders.md
 * describes customers as "identical in shape to suppliers" with
 * customer/customers substituted, and the /customers.html page spec says
 * "same pattern as suppliers". The structure below is a deliberate mirror of
 * suppliers.js (same modal-building approach, same role split) with
 * supplier -> customer, purchase order -> sales order.
 *
 * Row-click behavior by role (mirrors suppliers.js's documented choice):
 *   - Admin: opens `openCustomerModal()` in editable mode — name/contact/
 *     email/phone are inputs, Save issues PUT, and a Deactivate/Activate
 *     button is offered.
 *   - Operator: opens the SAME modal in read-only mode (fields rendered as
 *     text, no Save/Deactivate). The modal's other payload — the customer's
 *     sales-order history — is useful read-only information for an operator
 *     too, and GET /api/customers/{id} already exposes it for any role.
 *
 * The customer detail modal is custom-built here (not via modal.js's
 * openFormModal) because it mixes an editable form with a read-only
 * sales-order link list and a role-conditional action button — shapes
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
  const tbody = document.getElementById("customers-tbody");
  const searchInput = document.getElementById("search-input");
  const newCustomerBtn = document.getElementById("new-customer-btn");
  const pageBanner = document.getElementById("page-banner");

  // --- Initialize the search box from the current query string, so a
  // filtered view stays linkable (09-frontend.md's qs()/setQs() convention).
  searchInput.value = qs().search || "";

  let searchDebounce = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      setQs({ search: searchInput.value.trim() || null });
      loadCustomers();
    }, 300);
  });

  if (newCustomerBtn) {
    newCustomerBtn.addEventListener("click", () => openNewCustomerModal());
  }

  /** Opens the shared form modal for POST /api/customers, per 07-sales-orders.md
   * (name required/unique; contact_name/email/phone optional). */
  async function openNewCustomerModal() {
    const result = await openFormModal({
      title: "New customer",
      submitLabel: "Create",
      fields: [
        { name: "name", label: "Name", required: true },
        { name: "contact_name", label: "Contact name" },
        { name: "email", label: "Email" },
        { name: "phone", label: "Phone" },
      ],
      onSubmit: (values) =>
        api("POST", "/api/customers", {
          name: values.name,
          contact_name: values.contact_name || undefined,
          email: values.email || undefined,
          phone: values.phone || undefined,
        }),
    });

    if (result) {
      toast("Customer created.", "ok");
      loadCustomers();
    }
  }

  /** GET /api/customers query: `active=all` so both active and inactive rows
   * render together (the inactive ones badged) — the list itself is the
   * only place an admin can find and reactivate an inactive customer. */
  async function loadCustomers() {
    const params = new URLSearchParams({ active: "all" });
    const search = searchInput.value.trim();
    if (search) params.set("search", search);

    let data;
    try {
      data = await api("GET", `/api/customers?${params.toString()}`);
    } catch {
      toast("Could not load customers.", "error");
      return;
    }
    renderRows(data.items);
  }

  /** @param {Array<Object>} items - customer list items, shape per GET /api/customers. */
  function renderRows(items) {
    tbody.replaceChildren();

    if (!items.length) {
      tbody.appendChild(el("tr", { class: "empty-row" }, [el("td", { colSpan: 5 }, ["No customers match."])]));
      return;
    }

    for (const customer of items) {
      const nameCell = [customer.name];
      if (!customer.active) nameCell.push(" ", el("span", { class: "badge -draft" }, ["Inactive"]));

      const row = el(
        "tr",
        {
          dataset: { href: `#${customer.id}` },
          onClick: () => openCustomerModal(customer.id),
        },
        [
          el("td", {}, nameCell),
          el("td", {}, [customer.contact_name || "—"]),
          el("td", {}, [customer.email || "—"]),
          el("td", {}, [customer.phone || "—"]),
          el("td", {}, []),
        ]
      );
      tbody.appendChild(row);
    }
  }

  // ---------------------------------------------------------------------
  // Customer detail modal — GET /api/customers/{id}, edit form (admin) or
  // read-only view (operator), plus the customer's SO history either way.
  // ---------------------------------------------------------------------

  async function openCustomerModal(customerId) {
    let customer;
    try {
      customer = await api("GET", `/api/customers/${customerId}`);
    } catch {
      toast("Could not load customer.", "error");
      return;
    }

    const previousActive = document.activeElement;
    const titleId = `customer-modal-title-${customerId}`;
    const heading = el("h2", { id: titleId, class: "modal-title" }, [customer.name]);
    const closeBtn = el("button", { type: "button", class: "modal-close", "aria-label": "Close" }, ["×"]);
    const header = el("div", { class: "modal-header" }, [heading, closeBtn]);

    const banner = el("div", { class: "banner -error", hidden: true });
    const body = user.role === "admin" ? buildEditableBody(customer) : buildReadOnlyBody(customer);

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
        await onToggleActive(customer);
      });
    }

    const saveBtn = body.querySelector("[data-action=save]");
    if (saveBtn) {
      const form = body.querySelector("form");
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        saveCustomer(customer, dialog, form, banner);
      });
    }
  }

  /** Admin body: editable name/contact/email/phone, SO link list, Deactivate/
   * Activate button. Wrapped in a <form> so Enter submits it like every other
   * form modal in the app. */
  function buildEditableBody(customer) {
    const nameInput = el("input", { type: "text", required: true, value: customer.name });
    const nameError = el("div", { class: "field-error" });
    const contactInput = el("input", { type: "text", value: customer.contact_name || "" });
    const emailInput = el("input", { type: "text", value: customer.email || "" });
    const phoneInput = el("input", { type: "text", value: customer.phone || "" });

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
        class: customer.active ? "btn btn-danger" : "btn",
        dataset: { action: "toggle-active" },
      },
      [customer.active ? "Deactivate" : "Activate"]
    );
    const saveBtn = el("button", { type: "submit", class: "btn btn-primary", dataset: { action: "save" } }, ["Save"]);
    const actions = el("div", { class: "modal-actions" }, [toggleBtn, saveBtn]);

    const form = el("form", { class: "modal-form" }, [fields, buildSoList(customer.sales_orders), actions]);
    // Stash input refs on the form node itself so saveCustomer() (wired up by
    // the caller, openCustomerModal()) can read current values without a
    // second pass of DOM queries.
    form._nameInput = nameInput;
    form._contactInput = contactInput;
    form._emailInput = emailInput;
    form._phoneInput = phoneInput;
    form._nameError = nameError;
    return form;
  }

  /** Operator body: name/contact/email/phone as plain text, SO link list,
   * no form and no action button — nothing here is editable. */
  function buildReadOnlyBody(customer) {
    const dl = el("dl", { class: "wo-meta" }, [
      el("div", {}, [el("dt", {}, ["Contact"]), el("dd", {}, [customer.contact_name || "—"])]),
      el("div", {}, [el("dt", {}, ["Email"]), el("dd", {}, [customer.email || "—"])]),
      el("div", {}, [el("dt", {}, ["Phone"]), el("dd", {}, [customer.phone || "—"])]),
      el("div", {}, [
        el("dt", {}, ["Status"]),
        el("dd", {}, [
          customer.active
            ? el("span", { class: "badge -completed" }, ["Active"])
            : el("span", { class: "badge -draft" }, ["Inactive"]),
        ]),
      ]),
    ]);
    return el("div", { class: "modal-form" }, [dl, buildSoList(customer.sales_orders)]);
  }

  /** Read-only link list of the customer's sales orders (list shape per
   * GET /api/sales-orders, newest first already per 07-sales-orders.md). */
  function buildSoList(salesOrders) {
    const heading = el("h3", {}, ["Sales orders"]);
    if (!salesOrders || !salesOrders.length) {
      return el("div", {}, [heading, el("p", { class: "hint-text" }, ["No sales orders yet."])]);
    }
    const items = salesOrders.map((so) =>
      el("li", { class: "link-list-item" }, [
        el("a", { href: `/sales-order.html?id=${so.id}` }, [so.so_number]),
        " — ",
        el("span", { class: `badge -${so.status}` }, [capitalize(so.status)]),
        " — ",
        fmtMoney(so.total),
        " — ",
        fmtDate(so.created_at),
      ])
    );
    return el("div", {}, [heading, el("ul", { class: "link-list" }, items)]);
  }

  async function saveCustomer(customer, dialog, form, banner) {
    banner.hidden = true;
    banner.textContent = "";
    form._nameError.textContent = "";

    const saveBtn = form.querySelector("[data-action=save]");
    const toggleBtn = form.querySelector("[data-action=toggle-active]");
    saveBtn.disabled = true;
    if (toggleBtn) toggleBtn.disabled = true;

    try {
      const updated = await api("PUT", `/api/customers/${customer.id}`, {
        name: form._nameInput.value,
        contact_name: form._contactInput.value || undefined,
        email: form._emailInput.value || undefined,
        phone: form._phoneInput.value || undefined,
      });
      toast("Customer updated.", "ok");
      dialog.close();
      loadCustomers();
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
   * after the detail modal is already closed (see openCustomerModal), so a
   * 409 conflict (customer has a draft/confirmed SO) surfaces as a page-level
   * banner as well as a toast, per the task's "409 conflict → banner/toast". */
  async function onToggleActive(customer) {
    const activating = !customer.active;
    const confirmed = await openConfirmModal({
      title: activating ? "Activate customer?" : "Deactivate customer?",
      message: activating
        ? `${customer.name} will reappear in the active-customer picker for new sales orders.`
        : `${customer.name} will disappear from the active-customer picker for new sales orders. Its existing sales orders are unaffected.`,
      confirmLabel: activating ? "Activate" : "Deactivate",
      danger: !activating,
    });
    if (!confirmed) return;

    pageBanner.hidden = true;
    try {
      activating ? await api("POST", `/api/customers/${customer.id}/activate`) : await api("DELETE", `/api/customers/${customer.id}`);
      toast(activating ? "Customer activated." : "Customer deactivated.", "ok");
      loadCustomers();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        pageBanner.hidden = false;
        pageBanner.textContent = err.message || "This customer has an open sales order.";
        toast(err.message || "Could not deactivate: customer is in use.", "error");
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

  loadCustomers();
}
