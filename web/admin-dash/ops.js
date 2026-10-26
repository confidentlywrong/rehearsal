/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * Operations tab (G6).
 *
 * Platform-wide superadmin actions that don't belong on a specific
 * customer row. Two sections today:
 *
 *   1. Billing Reconcile (E1): operator-triggered Stripe→DB sync
 *      for a single org. Returns the full before/after plan +
 *      payment-failed diff so the operator can see exactly what
 *      changed. 503 when STRIPE_API_KEY is missing at boot — the
 *      result panel surfaces that as an info banner rather than an
 *      error since it's a configuration state, not a failure.
 *
 *   2. Hard-Delete Sweep (GDPR): bulk purge of users that have been
 *      soft-deleted longer than the configured threshold (default
 *      30 days). The form clamps the input to >=1 client-side; the
 *      server re-validates.
 *
 * HARD RULE alignment: every action here is operator-triggered via a
 * button click. Nothing on this tab polls, schedules, or auto-runs.
 *
 * Per-org actions (usage view, export, delete) live as row-action
 * buttons on the Customers tab, not here — that keeps the operator's
 * mental model "select a row, take an action" coherent.
 */

import { adminApi } from "./api.js";
import { escapeHTML } from "./main.js";

/**
 * Pure renderer for the reconcile result payload returned by
 * POST /admin/billing/reconcile. Shape (from cmd/admin/main.go
 * reconcileBillingHandler):
 *   { user_id, plan_before, plan_after, payment_failed_before,
 *     payment_failed_after, active_subscription_id }
 *
 * Returns an HTML string. The empty-state copy is the operator's
 * signal that nothing actually changed (a no-op reconcile is
 * common and not an error).
 */
export function renderReconcileResult(r) {
  if (!r || typeof r !== "object") {
    return `<div class="panel-empty">No result.</div>`;
  }
  const planChanged = r.plan_before !== r.plan_after;
  const failChanged = r.payment_failed_before !== r.payment_failed_after;
  const banner = !planChanged && !failChanged
    ? `<div class="usage-summary"><strong>No change.</strong> Stripe and DB already in sync.</div>`
    : "";
  return `<div class="usage-panel">
    ${banner}
    <table class="panel-table sub-table">
      <tbody>
        <tr><th>User</th><td class="mono">${escapeHTML(r.user_id || "")}</td></tr>
        <tr><th>Plan</th><td>
          <span class="mono">${escapeHTML(r.plan_before || "—")}</span>
          → <span class="mono">${escapeHTML(r.plan_after || "—")}</span>
        </td></tr>
        <tr><th>Payment failed at</th><td>
          <span class="mono">${escapeHTML(r.payment_failed_before || "—")}</span>
          → <span class="mono">${escapeHTML(r.payment_failed_after || "—")}</span>
        </td></tr>
        <tr><th>Stripe subscription</th><td class="mono">${escapeHTML(
          r.active_subscription_id || "—",
        )}</td></tr>
      </tbody>
    </table>
  </div>`;
}

/**
 * Pure renderer for the hard-delete sweep result payload returned by
 * POST /admin/users/hard-delete. Shape (from cmd/admin/gdpr.go
 * hardDeleteExpiredUsersHandler):
 *   { purged, older_than_days, cutoff }
 *
 * `purged` of 0 is a normal outcome (nothing aged out yet) — not
 * an error.
 */
export function renderSweepResult(r) {
  if (!r || typeof r !== "object") {
    return `<div class="panel-empty">No result.</div>`;
  }
  return `<div class="usage-panel">
    <div class="usage-summary">
      <div><strong>Purged:</strong> ${Number(r.purged || 0).toLocaleString()} user(s)</div>
      <div><strong>Threshold:</strong> ${Number(r.older_than_days || 0)} day(s)</div>
      <div><strong>Cutoff:</strong> <span class="mono">${escapeHTML(r.cutoff || "")}</span></div>
    </div>
  </div>`;
}

export function mountOpsTab(root) {
  if (!root) return;
  root.innerHTML = "";

  const header = document.createElement("header");
  header.className = "panel-header";
  header.innerHTML = `
    <h2>Operations</h2>
    <p class="panel-lead">
      Platform-wide superadmin actions. Every action on this tab is
      manual — nothing runs on a schedule.
    </p>`;
  root.appendChild(header);

  root.appendChild(buildReconcileSection());
  root.appendChild(buildSweepSection());
}

function buildReconcileSection() {
  const section = document.createElement("section");
  section.className = "ops-section";
  section.innerHTML = `<h3>Billing reconcile</h3>
    <p class="panel-lead">
      Force a Stripe → DB sync for one user. Use after a webhook
      delivery failure or a manual Stripe-dashboard change.
    </p>`;

  const form = document.createElement("form");
  form.className = "panel-form";
  const idIn = Object.assign(document.createElement("input"), {
    name: "user_id",
    placeholder: "user-id",
    required: true,
  });
  const submit = Object.assign(document.createElement("button"), {
    type: "submit",
    textContent: "Reconcile",
  });
  form.append(idIn, submit);

  const banner = document.createElement("div");
  banner.className = "panel-banner";
  banner.setAttribute("role", "alert");
  banner.hidden = true;

  const resultHost = document.createElement("div");
  resultHost.className = "ops-result";

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const userID = idIn.value.trim();
    if (!userID) return;
    submit.disabled = true;
    banner.hidden = true;
    resultHost.innerHTML = `<div class="panel-loading">Reconciling…</div>`;
    try {
      const r = await adminApi.reconcileBilling(userID);
      resultHost.innerHTML = renderReconcileResult(r);
    } catch (e) {
      resultHost.innerHTML = "";
      banner.textContent =
        e && e.status === 503
          ? "Reconcile unavailable: STRIPE_API_KEY not configured on this deployment."
          : `Reconcile failed: ${e.message}`;
      banner.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });

  section.append(form, banner, resultHost);
  return section;
}

function buildSweepSection() {
  const section = document.createElement("section");
  section.className = "ops-section";
  section.innerHTML = `<h3>Hard-delete sweep</h3>
    <p class="panel-lead">
      Permanently purge users that have been soft-deleted longer than
      the threshold. GDPR-destructive — there is no undo.
    </p>`;

  const form = document.createElement("form");
  form.className = "panel-form";
  const daysIn = Object.assign(document.createElement("input"), {
    name: "older_than_days",
    type: "number",
    min: "1",
    step: "1",
    value: "30",
    required: true,
  });
  const submit = Object.assign(document.createElement("button"), {
    type: "submit",
    textContent: "Run sweep",
  });
  form.append(daysIn, submit);

  const banner = document.createElement("div");
  banner.className = "panel-banner";
  banner.setAttribute("role", "alert");
  banner.hidden = true;

  const resultHost = document.createElement("div");
  resultHost.className = "ops-result";

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const days = Math.max(1, parseInt(daysIn.value, 10) || 0);
    // eslint-disable-next-line no-alert
    if (
      !window.confirm(
        `Permanently delete users soft-deleted more than ${days} day(s) ago? This cannot be undone.`,
      )
    ) {
      return;
    }
    submit.disabled = true;
    banner.hidden = true;
    resultHost.innerHTML = `<div class="panel-loading">Sweeping…</div>`;
    try {
      const r = await adminApi.hardDeleteUsers(days);
      resultHost.innerHTML = renderSweepResult(r);
    } catch (e) {
      resultHost.innerHTML = "";
      banner.textContent = `Sweep failed: ${e.message}`;
      banner.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });

  section.append(form, banner, resultHost);
  return section;
}
