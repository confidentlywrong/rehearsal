/**
 * Copyright 2026 Google LLC
 * Licensed under the Apache License, Version 2.0.
 */

// Audit tab. Lists rows from /admin/audit with per-user / per-action
// filters. No org concept anymore.

import { escapeHTML } from "./main.js";
import { adminApi } from "./api.js";

const DEFAULT_LIMIT = 50;
const KNOWN_ACTIONS = [
  "",
  "user.created",
  "user.erased",
  "user.plan_changed",
  "user.data_exported",
  "user.logged_out",
  "payment.failed",
  "payment.recovered",
  "billing.reconciled",
  "superadmin.granted",
  "superadmin.revoked",
  "users.bulk_erased",
  "quota.rejected",
  "sim.spawned",
];


function fmtTimestamp(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function setStatus(el, msg, kind) {
  el.textContent = msg || "";
  el.className = "tab-status" + (kind ? " tab-status-" + kind : "");
}

export function renderAuditTable(entries) {
  if (!entries || entries.length === 0) {
    return `<div class="tab-empty">No audit entries match this filter.</div>`;
  }
  const rows = entries.map((e) => {
    const metaJSON = e.metadata ? JSON.stringify(e.metadata, null, 0) : "";
    return `
      <tr>
        <td class="dim mono">${escapeHTML(fmtTimestamp(e.created_at))}</td>
        <td class="mono">${escapeHTML(e.user_id || "—")}</td>
        <td class="mono">${escapeHTML(e.actor_id || "—")}</td>
        <td>${escapeHTML(e.action || "—")}</td>
        <td class="mono dim">${escapeHTML(e.target || "—")}</td>
        <td class="mono dim metadata-cell" title="${escapeHTML(metaJSON)}">${escapeHTML(metaJSON)}</td>
      </tr>`;
  });
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>When</th><th>Subject</th><th>Actor</th><th>Action</th><th>Target</th><th>Metadata</th>
        </tr>
      </thead>
      <tbody>${rows.join("")}</tbody>
    </table>`;
}

function tabHTML() {
  const actionOpts = KNOWN_ACTIONS.map(
    (a) => `<option value="${a}">${a || "(any)"}</option>`,
  ).join("");
  return `
    <div class="tab-header">
      <h2>Audit Log</h2>
      <button class="ghost-btn" id="audit-refresh" type="button">↻ Refresh</button>
    </div>

    <form class="card-panel form-grid" id="audit-filters" autocomplete="off">
      <label>
        <span>User ID</span>
        <input name="user_id" placeholder="(all users)" />
      </label>
      <label>
        <span>Action</span>
        <select name="action">${actionOpts}</select>
      </label>
      <label>
        <span>Page size</span>
        <input name="limit" type="number" min="1" max="500" value="${DEFAULT_LIMIT}" />
      </label>
      <div class="form-actions">
        <button class="primary-btn" type="submit">Apply filters</button>
        <span class="tab-status" id="audit-status" aria-live="polite"></span>
      </div>
    </form>

    <div class="card-panel">
      <div id="audit-table-host"><div class="tab-loading">Loading…</div></div>
      <div class="pager">
        <button class="ghost-btn" id="audit-prev" type="button">← Newer</button>
        <span class="pager-position" id="audit-position">offset 0</span>
        <button class="ghost-btn" id="audit-next" type="button">Older →</button>
      </div>
    </div>
  `;
}

async function loadAudit(container, state) {
  const host = container.querySelector("#audit-table-host");
  const status = container.querySelector("#audit-status");
  const position = container.querySelector("#audit-position");
  const prevBtn = container.querySelector("#audit-prev");
  const nextBtn = container.querySelector("#audit-next");
  setStatus(status, "Loading…", "neutral");
  prevBtn.disabled = true;
  nextBtn.disabled = true;
  try {
    const data = await adminApi.listAudit({
      userID: state.userID,
      action: state.action,
      limit: state.limit,
      offset: state.offset,
    });
    const entries = (data && data.entries) || [];
    state.lastCount = entries.length;
    host.innerHTML = renderAuditTable(entries);
    if (entries.length === 0) {
      position.textContent = `rows 0–0 (page size ${state.limit})`;
    } else {
      position.textContent = `rows ${state.offset + 1}–${state.offset + entries.length} (page size ${state.limit})`;
    }
    setStatus(
      status,
      entries.length === 0 ? "No matches." : `${entries.length} entries.`,
      entries.length === 0 ? "neutral" : "success",
    );
  } catch (e) {
    host.innerHTML = `<div class="tab-error">Failed to load audit log.</div>`;
    setStatus(status, e.message, "error");
  } finally {
    prevBtn.disabled = state.offset === 0;
    nextBtn.disabled = state.lastCount < state.limit;
  }
}

export function mountAuditTab(container) {
  if (!container) return;
  const state = {
    offset: 0,
    limit: DEFAULT_LIMIT,
    userID: "",
    action: "",
    lastCount: 0,
  };
  container.innerHTML = tabHTML();

  const form = container.querySelector("#audit-filters");
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    state.userID = String(fd.get("user_id") || "").trim();
    state.action = String(fd.get("action") || "").trim();
    const lim = parseInt(String(fd.get("limit") || DEFAULT_LIMIT), 10);
    state.limit = Number.isFinite(lim) && lim > 0 ? Math.min(lim, 500) : DEFAULT_LIMIT;
    state.offset = 0;
    loadAudit(container, state);
  });

  container
    .querySelector("#audit-refresh")
    .addEventListener("click", () => loadAudit(container, state));
  container.querySelector("#audit-prev").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadAudit(container, state);
  });
  container.querySelector("#audit-next").addEventListener("click", () => {
    state.offset = state.offset + state.limit;
    loadAudit(container, state);
  });

  loadAudit(container, state);
}
