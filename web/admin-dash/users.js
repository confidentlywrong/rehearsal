/**
 * Copyright 2026 Google LLC
 * Licensed under the Apache License, Version 2.0.
 */

// Users tab. One row per user — replaces the legacy Customers + Members
// tabs since the per-user model collapses the two. Row actions:
//   • change plan (free / pro / enterprise)
//   • view usage for current month
//   • export GDPR JSON dump
//   • soft-delete  (DELETE /admin/users/:id)
//   • erase NOW   (POST /admin/users/:id/erase — destructive)
//   • reconcile billing (POST /admin/billing/reconcile)

import { escapeHTML } from "./main.js";
import { adminApi, AdminApiError } from "./api.js";

// 'comp' is the manual complimentary tier — same caps as enterprise,
// distinct row so usage reports can tell comped accounts apart from
// paying enterprise customers. Stripe webhook + reconciler refuse to
// overwrite it, so granting it here is sticky until a superadmin
// changes it back.
const PLAN_TIERS = ["free", "pro", "enterprise", "comp"];

function fmtTimestamp(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function setStatus(el, msg, kind) {
  if (!el) return;
  el.textContent = msg || "";
  el.className = "tab-status" + (kind ? " tab-status-" + kind : "");
}

function tabHTML() {
  return `
    <div class="tab-header">
      <h2>Users</h2>
      <div class="tab-controls">
        <label class="filter-label" for="users-status-filter">Status:</label>
        <select id="users-status-filter" class="filter-select">
          <option value="pending" selected>Pending only</option>
          <option value="all">All</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
        </select>
        <button class="ghost-btn" id="users-refresh" type="button">↻ Refresh</button>
      </div>
    </div>
    <div class="card-panel">
      <div id="users-table-host"><div class="tab-loading">Loading…</div></div>
      <div id="users-status" class="tab-status" aria-live="polite"></div>
    </div>
    <div class="card-panel" id="users-detail-host" hidden>
      <div class="tab-header">
        <h3 id="users-detail-title">Detail</h3>
        <button class="ghost-btn" id="users-detail-close" type="button">Close</button>
      </div>
      <pre id="users-detail-body" class="json-block"></pre>
    </div>
  `;
}

function renderUsersTable(users) {
  if (!users || users.length === 0) {
    return `<div class="tab-empty">No users to show.</div>`;
  }
  const planOpts = (current) =>
    PLAN_TIERS.map(
      (t) =>
        `<option value="${t}"${t === current ? " selected" : ""}>${t}</option>`,
    ).join("");
  const rows = users.map((u) => {
    // Two independent status pills:
    //   - approval pill  → users.status column (pending/approved/rejected).
    //     The manual-approval flow's primary signal — colour-coded so the
    //     operator can scan the queue at a glance.
    //   - account pill   → soft-delete / payment-failed state, the legacy
    //     "deleted"/"payment-failed"/"active" column. Kept on the same row
    //     so a single sweep covers both gates.
    const approval = (u.status || "pending").toLowerCase();
    const account =
      u.deleted_at ? "deleted" : u.payment_failed_at ? "payment-failed" : "active";
    const showApprove = approval === "pending" || approval === "rejected";
    const showReject = approval === "pending" || approval === "approved";
    const approvedMeta =
      approval === "approved" && u.approved_by
        ? `<div class="dim">by ${escapeHTML(u.approved_by)} · ${escapeHTML(fmtTimestamp(u.approved_at))}</div>`
        : "";
    return `
      <tr data-user-id="${escapeHTML(u.id)}">
        <td class="mono">${escapeHTML(u.id)}</td>
        <td>${escapeHTML(u.email || "—")}</td>
        <td>${escapeHTML(u.display_name || "—")}</td>
        <td>
          <span class="status-pill status-pill-${approval}">${approval}</span>
          ${approvedMeta}
        </td>
        <td>
          <select class="plan-select" data-user-id="${escapeHTML(u.id)}">
            ${planOpts(u.plan_tier || "free")}
          </select>
        </td>
        <td>${u.is_superadmin ? "★ yes" : "—"}</td>
        <td class="mono dim">${escapeHTML(u.stripe_customer_id || "—")}</td>
        <td class="dim mono">${escapeHTML(fmtTimestamp(u.created_at))}</td>
        <td><span class="status-pill status-pill-${account}">${account}</span></td>
        <td class="row-actions">
          ${showApprove ? `<button class="ghost-btn act-approve" data-user-id="${escapeHTML(u.id)}" type="button">Approve</button>` : ""}
          ${showReject ? `<button class="ghost-btn act-reject" data-user-id="${escapeHTML(u.id)}" type="button">Reject</button>` : ""}
          <button class="ghost-btn act-usage" data-user-id="${escapeHTML(u.id)}" type="button">Usage</button>
          <button class="ghost-btn act-export" data-user-id="${escapeHTML(u.id)}" type="button">Export</button>
          <button class="ghost-btn act-reconcile" data-user-id="${escapeHTML(u.id)}" type="button">Reconcile</button>
          <button class="ghost-btn act-soft-delete" data-user-id="${escapeHTML(u.id)}" type="button">Soft-delete</button>
          <button class="ghost-btn danger act-erase" data-user-id="${escapeHTML(u.id)}" type="button">Erase</button>
        </td>
      </tr>`;
  });
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>ID</th><th>Email</th><th>Display name</th><th>Approval</th><th>Plan</th>
          <th>Superadmin</th><th>Stripe customer</th><th>Created</th>
          <th>Account</th><th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows.join("")}</tbody>
    </table>`;
}

function showDetail(container, title, body) {
  const host = container.querySelector("#users-detail-host");
  const titleEl = container.querySelector("#users-detail-title");
  const bodyEl = container.querySelector("#users-detail-body");
  titleEl.textContent = title;
  bodyEl.textContent = typeof body === "string" ? body : JSON.stringify(body, null, 2);
  host.hidden = false;
  host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function loadUsers(container) {
  const host = container.querySelector("#users-table-host");
  const status = container.querySelector("#users-status");
  const filterEl = container.querySelector("#users-status-filter");
  const filter = filterEl ? filterEl.value : "pending";
  setStatus(status, "Loading…", "neutral");
  try {
    const data = await adminApi.listUsers();
    const allUsers = (data && data.users) || [];
    // Client-side filter — keeps the backend list endpoint simple
    // (one query, ORDER BY created_at DESC) and lets the operator
    // flip the filter without re-hitting the API.
    const filtered =
      filter === "all"
        ? allUsers
        : allUsers.filter((u) => (u.status || "pending") === filter);
    host.innerHTML = renderUsersTable(filtered);
    const total = allUsers.length;
    const shown = filtered.length;
    const pending = allUsers.filter((u) => (u.status || "pending") === "pending").length;
    const summary =
      filter === "all"
        ? `${total} users · ${pending} pending.`
        : `${shown} of ${total} users (filter: ${filter}) · ${pending} pending overall.`;
    setStatus(status, summary, "success");
    bindRowActions(container);
  } catch (e) {
    host.innerHTML = `<div class="tab-error">Failed to load users.</div>`;
    setStatus(status, e.message, "error");
  }
}

function bindRowActions(container) {
  container.querySelectorAll(".plan-select").forEach((sel) => {
    sel.addEventListener("change", async (ev) => {
      const userID = ev.target.dataset.userId;
      const tier = ev.target.value;
      try {
        await adminApi.setUserPlan(userID, tier);
        setStatus(
          container.querySelector("#users-status"),
          `Plan for ${userID} set to ${tier}.`,
          "success",
        );
      } catch (e) {
        setStatus(
          container.querySelector("#users-status"),
          `Set plan failed: ${e.message}`,
          "error",
        );
      }
    });
  });

  const bind = (cls, fn) => {
    container.querySelectorAll(cls).forEach((btn) => {
      btn.addEventListener("click", () => fn(btn.dataset.userId));
    });
  };

  bind(".act-approve", async (userID) => {
    if (!confirm(`Approve user ${userID}? They will gain immediate access to the app.`)) return;
    try {
      await adminApi.approveUser(userID);
      setStatus(container.querySelector("#users-status"), `Approved ${userID}.`, "success");
      loadUsers(container);
    } catch (e) {
      setStatus(container.querySelector("#users-status"), e.message, "error");
    }
  });

  bind(".act-reject", async (userID) => {
    if (!confirm(`Reject user ${userID}? The gateway will refuse every API for this user until you re-approve.`)) return;
    try {
      await adminApi.rejectUser(userID);
      setStatus(container.querySelector("#users-status"), `Rejected ${userID}.`, "success");
      loadUsers(container);
    } catch (e) {
      setStatus(container.querySelector("#users-status"), e.message, "error");
    }
  });

  bind(".act-usage", async (userID) => {
    try {
      const data = await adminApi.getUserUsage(userID);
      showDetail(container, `Usage — ${userID}`, data);
    } catch (e) {
      setStatus(container.querySelector("#users-status"), e.message, "error");
    }
  });

  bind(".act-export", async (userID) => {
    try {
      const data = await adminApi.exportUser(userID);
      showDetail(container, `Export — ${userID}`, data);
    } catch (e) {
      setStatus(container.querySelector("#users-status"), e.message, "error");
    }
  });

  bind(".act-reconcile", async (userID) => {
    try {
      const data = await adminApi.reconcileBilling(userID);
      showDetail(container, `Reconcile — ${userID}`, data);
      setStatus(container.querySelector("#users-status"), "Reconcile complete.", "success");
    } catch (e) {
      if (e instanceof AdminApiError && e.status === 503) {
        setStatus(container.querySelector("#users-status"),
          "Reconcile unavailable: STRIPE_API_KEY not configured.", "error");
      } else {
        setStatus(container.querySelector("#users-status"), e.message, "error");
      }
    }
  });

  bind(".act-soft-delete", async (userID) => {
    if (!confirm(`Soft-delete user ${userID}? They will be unable to sign in. Row is recoverable until hard-delete sweep.`)) return;
    try {
      await adminApi.softDeleteUser(userID);
      setStatus(container.querySelector("#users-status"), `Soft-deleted ${userID}.`, "success");
      loadUsers(container);
    } catch (e) {
      setStatus(container.querySelector("#users-status"), e.message, "error");
    }
  });

  bind(".act-erase", async (userID) => {
    if (!confirm(`HARD-DELETE user ${userID} immediately? This cannot be undone.`)) return;
    if (!confirm(`Final confirm: erase ${userID} NOW?`)) return;
    try {
      await adminApi.eraseUser(userID);
      setStatus(container.querySelector("#users-status"), `Erased ${userID}.`, "success");
      loadUsers(container);
    } catch (e) {
      setStatus(container.querySelector("#users-status"), e.message, "error");
    }
  });
}

export function mountUsersTab(container) {
  if (!container) return;
  container.innerHTML = tabHTML();
  container
    .querySelector("#users-refresh")
    .addEventListener("click", () => loadUsers(container));
  const filterEl = container.querySelector("#users-status-filter");
  if (filterEl) {
    filterEl.addEventListener("change", () => loadUsers(container));
  }
  container
    .querySelector("#users-detail-close")
    .addEventListener("click", () => {
      container.querySelector("#users-detail-host").hidden = true;
    });
  loadUsers(container);
}
