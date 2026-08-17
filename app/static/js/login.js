/**
 * login.js — page logic for login.html.
 *
 * This page is the one exception to the "call initShell() first" contract
 * documented at the top of app.js: there is no session yet to bootstrap, and
 * no `<header id="nav">` to fill in. Instead it does its own, narrower
 * version of the same idea: check whether a session already exists, and if
 * so skip the form entirely.
 *
 * Per requirements/02-auth.md:
 *   - If GET /api/auth/me already returns 200 (user re-visits /login.html
 *     with a live session, e.g. via back-button), redirect straight to
 *     /index.html instead of showing the form.
 *   - On submit, POST /api/auth/login. Success -> redirect to /index.html.
 *     401 -> show the message inline (never alert()). 400 (missing fields)
 *     is handled the same way, since the only fields here are the two the
 *     browser already marks `required`.
 */

import { api, ApiError } from "./app.js";

const form = document.getElementById("login-form");
const errorLine = document.getElementById("login-error");
const submitBtn = form.querySelector('button[type="submit"]');

/** Clears the inline error line. */
function clearError() {
  errorLine.textContent = "";
}

/** Shows a message on the inline error line under the form. */
function showError(message) {
  errorLine.textContent = message;
}

// Already logged in? Skip the form. api()'s global-401-redirect is disabled
// on login.html specifically so this 401 is just "not logged in yet" here,
// not treated as a session-expiry event elsewhere in the app.
try {
  await api("GET", "/api/auth/me");
  location.href = "/index.html";
} catch (err) {
  if (!(err instanceof ApiError) || err.status !== 401) {
    // Network hiccup or unexpected error checking session state — fall
    // through to showing the login form rather than leaving a blank page.
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();

  const username = form.username.value;
  const password = form.password.value;

  submitBtn.disabled = true;
  try {
    await api("POST", "/api/auth/login", { username, password });
    location.href = "/index.html";
  } catch (err) {
    if (err instanceof ApiError) {
      showError(err.message || "Invalid username or password.");
    } else {
      showError("Could not reach the server. Please try again.");
    }
  } finally {
    submitBtn.disabled = false;
  }
});
