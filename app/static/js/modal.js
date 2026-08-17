/**
 * modal.js — shared <dialog>-based modal helpers.
 *
 * WHAT IS <dialog>? It's a native HTML element (no library needed) that gives
 * us, for free:
 *   - `dialog.showModal()` — displays it centered-by-default, makes it *modal*
 *     (everything outside becomes inert: unclickable and unreachable by Tab),
 *     and renders a `::backdrop` pseudo-element behind it that we can style
 *     (see the `dialog::backdrop` rule in style.css).
 *   - Esc closes it automatically. The browser fires a cancellable `cancel`
 *     event first, then `close` — we don't have to wire up a keydown listener.
 *   - `dialog.close(returnValue?)` closes it programmatically and fires `close`.
 *   - Focus handling: showModal() moves focus into the dialog (browsers focus
 *     the first focusable control, or autofocus target) and, per spec, focus
 *     does NOT automatically return anywhere after close — we restore it
 *     ourselves below by remembering `document.activeElement` before opening.
 *   - `dialog.returnValue` is just a string we can set before calling close()
 *     to communicate *why* it closed to whoever is listening for `close`.
 *
 * This module wraps that primitive with the two shapes requirements/09-frontend.md
 * asks for: form modals (create/edit) and confirm modals (destructive actions).
 * Both return Promises so page code reads top-to-bottom instead of nesting
 * callbacks:
 *
 *   const result = await openFormModal({ title: "New Part", fields, onSubmit });
 *   if (result) toast("Part created.", "ok");
 *
 *   const ok = await openConfirmModal({ title: "Cancel order?", message: "..." });
 *   if (ok) await api("POST", `/api/orders/${id}/cancel`);
 */

import { el, toast, ApiError } from "./app.js";

/**
 * Builds the outer <dialog> shell (header with title + close button, wrapping
 * `bodyNode`), appends it to <body>, and wires up the two "dismiss" gestures
 * that both modal flavors share: clicking the header's close button, and
 * clicking the backdrop.
 *
 * Backdrop-click detection: the native `::backdrop` isn't a real DOM node you
 * can attach a listener to, but a click on it still dispatches a `click` event
 * on the `<dialog>` element itself (since the dialog's padding box doesn't
 * cover the backdrop area). A click on something *inside* the dialog has that
 * descendant as `event.target`, not the dialog. So `event.target === dialog`
 * reliably means "the user clicked outside the content" — the standard trick
 * for backdrop-dismiss without a library.
 *
 * The dialog removes itself from the DOM on `close` (it was only ever a
 * temporary overlay); callers add their own `close` listener for their own
 * cleanup/resolution logic.
 *
 * @param {{title: string, bodyNode: Node}} config
 * @returns {HTMLDialogElement}
 */
function buildDialogShell({ title, bodyNode }) {
  const titleId = `modal-title-${Math.random().toString(36).slice(2, 8)}`;
  const heading = el("h2", { id: titleId, class: "modal-title" }, [title]);
  const closeBtn = el("button", { type: "button", class: "modal-close", "aria-label": "Close" }, ["×"]);
  const header = el("div", { class: "modal-header" }, [heading, closeBtn]);
  const dialog = el("dialog", { class: "modal", "aria-labelledby": titleId }, [header, bodyNode]);

  document.body.appendChild(dialog);

  closeBtn.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => dialog.remove());

  return dialog;
}

/**
 * Opens a modal form for creating/editing a record.
 *
 * Renders one `.field` block per entry in `fields` (label + input + an empty
 * error slot under it), a Cancel and Submit button. On submit it calls
 * `onSubmit(values)` with `{name: currentValue}` for every field:
 *   - While `onSubmit` is in flight, both buttons are disabled (prevents a
 *     double-submit from a second click or double-tap).
 *   - If `onSubmit` resolves, the modal closes and the returned promise
 *     resolves with whatever `onSubmit` returned (typically the created/updated
 *     record from `api()`).
 *   - If `onSubmit` throws an `ApiError` with `status === 400`, its
 *     `fieldErrors` are written into the matching field's error slot; if there
 *     are no field-specific errors (or the status is 409, e.g. a conflict) the
 *     message is shown instead as a page-level banner at the top of the form,
 *     per the 400-vs-409 display convention in requirements/09-frontend.md.
 *   - Any other error is toasted and re-thrown (a bug, not a user-fixable
 *     validation problem) so it isn't silently swallowed.
 * Cancel, the header close button, backdrop click, or Esc all close the modal
 * without calling `onSubmit`; the returned promise then resolves to `null`.
 *
 * @param {Object} config
 * @param {string} config.title
 * @param {Array<{
 *   name: string,
 *   label: string,
 *   type?: "text"|"number"|"password"|"select"|"textarea",
 *   value?: string|number,
 *   required?: boolean,
 *   options?: Array<{value: string, label: string}>, // for type: "select"
 *   step?: string, min?: string|number, max?: string|number, // for type: "number"
 * }>} config.fields
 * @param {string} [config.submitLabel="Save"]
 * @param {string} [config.cancelLabel="Cancel"]
 * @param {(values: Object<string,string>) => Promise<*>} config.onSubmit
 * @returns {Promise<*>} resolves with onSubmit's result, or null if dismissed.
 */
export function openFormModal({ title, fields, submitLabel = "Save", cancelLabel = "Cancel", onSubmit }) {
  return new Promise((resolve) => {
    const previousActive = document.activeElement;

    const banner = el("div", { class: "banner -error", hidden: true });
    const inputEls = {};
    const errorEls = {};

    const fieldNodes = fields.map((field) => {
      const inputId = `field-${field.name}`;
      const label = el("label", { for: inputId }, [field.label]);
      let input;

      if (field.type === "select") {
        input = el(
          "select",
          { id: inputId, name: field.name, required: !!field.required },
          (field.options || []).map((opt) => el("option", { value: opt.value }, [opt.label]))
        );
        if (field.value !== undefined && field.value !== null) input.value = field.value;
      } else if (field.type === "textarea") {
        input = el("textarea", { id: inputId, name: field.name, required: !!field.required });
        input.value = field.value ?? "";
      } else {
        input = el("input", {
          id: inputId,
          name: field.name,
          type: field.type || "text",
          required: !!field.required,
          step: field.step,
          min: field.min,
          max: field.max,
        });
        input.value = field.value ?? "";
      }

      inputEls[field.name] = input;
      const errorNode = el("div", { class: "field-error" });
      errorEls[field.name] = errorNode;
      return el("div", { class: "field" }, [label, input, errorNode]);
    });

    const submitBtn = el("button", { type: "submit", class: "btn btn-primary" }, [submitLabel]);
    const cancelBtn = el("button", { type: "button", class: "btn btn-ghost" }, [cancelLabel]);
    const actions = el("div", { class: "modal-actions" }, [cancelBtn, submitBtn]);
    const form = el("form", { class: "modal-form" }, [banner, ...fieldNodes, actions]);

    const dialog = buildDialogShell({ title, bodyNode: form });

    function clearErrors() {
      banner.hidden = true;
      banner.textContent = "";
      for (const node of Object.values(errorEls)) node.textContent = "";
    }

    function setBusy(busy) {
      submitBtn.disabled = busy;
      cancelBtn.disabled = busy;
    }

    cancelBtn.addEventListener("click", () => dialog.close());

    let submittedResult;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearErrors();
      setBusy(true);

      const values = {};
      for (const field of fields) values[field.name] = inputEls[field.name].value;

      try {
        const result = await onSubmit(values);
        submittedResult = result;
        dialog.returnValue = "submitted";
        dialog.close();
      } catch (err) {
        if (err instanceof ApiError && err.status === 400 && Object.keys(err.fieldErrors).length) {
          for (const [name, message] of Object.entries(err.fieldErrors)) {
            if (errorEls[name]) errorEls[name].textContent = message;
          }
        } else if (err instanceof ApiError) {
          banner.hidden = false;
          banner.textContent = err.message;
        } else {
          toast("Something went wrong.", "error");
          throw err;
        }
      } finally {
        setBusy(false);
      }
    });

    dialog.addEventListener(
      "close",
      () => {
        if (previousActive instanceof HTMLElement) previousActive.focus();
        resolve(dialog.returnValue === "submitted" ? submittedResult : null);
      },
      { once: true }
    );

    dialog.showModal();
    const firstInput = fieldNodes.length ? inputEls[fields[0].name] : null;
    if (firstInput) firstInput.focus();
  });
}

/**
 * Opens a confirmation dialog for a destructive/irreversible action. The
 * `message` should state the concrete consequence ("This will consume 4
 * components from stock and mark the work order Completed.") rather than a
 * generic "Are you sure?" — see the module docs for exact wording per action.
 *
 * @param {Object} config
 * @param {string} config.title
 * @param {string} config.message
 * @param {string} [config.confirmLabel="Confirm"]
 * @param {string} [config.cancelLabel="Cancel"]
 * @param {boolean} [config.danger=false] - styles the confirm button as destructive.
 * @returns {Promise<boolean>} true if confirmed, false if dismissed any other way.
 */
export function openConfirmModal({ title, message, confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    const previousActive = document.activeElement;

    const text = el("p", { class: "modal-message" }, [message]);
    const confirmBtn = el("button", { type: "button", class: danger ? "btn btn-danger" : "btn btn-primary" }, [
      confirmLabel,
    ]);
    const cancelBtn = el("button", { type: "button", class: "btn btn-ghost" }, [cancelLabel]);
    const actions = el("div", { class: "modal-actions" }, [cancelBtn, confirmBtn]);
    const body = el("div", { class: "modal-confirm" }, [text, actions]);

    const dialog = buildDialogShell({ title, bodyNode: body });

    let confirmed = false;
    confirmBtn.addEventListener("click", () => {
      confirmed = true;
      dialog.close();
    });
    cancelBtn.addEventListener("click", () => dialog.close());

    dialog.addEventListener(
      "close",
      () => {
        if (previousActive instanceof HTMLElement) previousActive.focus();
        resolve(confirmed);
      },
      { once: true }
    );

    dialog.showModal();
    confirmBtn.focus();
  });
}
