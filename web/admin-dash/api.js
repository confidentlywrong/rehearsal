/**
 * Copyright 2026 Google LLC
 * Licensed under the Apache License, Version 2.0.
 */

// Shared admin API client. All Users / Audit / Ops tabs call /admin/*
// endpoints on this same origin; the admin Go service mounts them
// behind RequireSuperadmin which checks UserContext.IsSuperadmin.
// In local dev the LocalResolver elevates to that role when the
// request carries `X-Local-Dev-Superadmin: 1` — every /admin/* call
// from this client carries that header. It is harmless in cloud
// (FirebaseResolver ignores it).

export const LOCAL_SUPERADMIN_HEADER = "X-Local-Dev-Superadmin";

export class AdminApiError extends Error {
  constructor(message, { status = 0, body = null } = {}) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
    this.body = body;
  }
}

async function parseJSONSafe(resp) {
  try {
    return await resp.json();
  } catch {
    return null;
  }
}

async function adminFetch(method, path, body) {
  const headers = { [LOCAL_SUPERADMIN_HEADER]: "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new AdminApiError(`network error: ${e.message}`);
  }

  // 204 No Content (hard-delete, soft-delete) has no body.
  if (resp.status === 204) {
    if (!resp.ok) {
      throw new AdminApiError(`HTTP ${resp.status}`, { status: resp.status });
    }
    return null;
  }

  const payload = await parseJSONSafe(resp);
  if (!resp.ok) {
    const msg = (payload && payload.error) || `HTTP ${resp.status}`;
    throw new AdminApiError(msg, { status: resp.status, body: payload });
  }
  return payload;
}

export const adminApi = {
  // ---------------- Users tab ----------------

  // GET /admin/users → { users: [{id, email, display_name,
  //   plan_tier, stripe_customer_id, payment_failed_at,
  //   is_superadmin, created_at, deleted_at}] }
  listUsers() {
    return adminFetch("GET", "/admin/users");
  },

  // PUT /admin/users/:id/plan { plan_tier: "free|pro|enterprise" }
  setUserPlan(userID, planTier) {
    return adminFetch(
      "PUT",
      `/admin/users/${encodeURIComponent(userID)}/plan`,
      { plan_tier: planTier },
    );
  },

  // GET /admin/users/:id/export → full per-user JSON dump (user
  // row + audit history).
  exportUser(userID) {
    return adminFetch(
      "GET",
      `/admin/users/${encodeURIComponent(userID)}/export`,
    );
  },

  // DELETE /admin/users/:id → soft-delete (sets deleted_at). The
  // row stays in place so audit FKs survive; hard-delete sweeps
  // away anything older than ?older_than_days=N.
  softDeleteUser(userID) {
    return adminFetch(
      "DELETE",
      `/admin/users/${encodeURIComponent(userID)}`,
    );
  },

  // POST /admin/users/:id/erase → immediate hard-delete of one
  // user. Audit row written first so the email is preserved.
  eraseUser(userID) {
    return adminFetch(
      "POST",
      `/admin/users/${encodeURIComponent(userID)}/erase`,
    );
  },

  // POST /admin/users/:id/approve → flip the user's status from
  // pending → approved. Idempotent. Audited as user.approved.
  approveUser(userID) {
    return adminFetch(
      "POST",
      `/admin/users/${encodeURIComponent(userID)}/approve`,
    );
  },

  // POST /admin/users/:id/reject → flip the user's status to
  // rejected. Idempotent. Audited as user.rejected. The Firebase
  // account is left in place so the operator can flip back to
  // approved later without re-issuing credentials; the gateway
  // refuses every API for the user until then.
  rejectUser(userID) {
    return adminFetch(
      "POST",
      `/admin/users/${encodeURIComponent(userID)}/reject`,
    );
  },

  // POST /admin/users/hard-delete?older_than_days=N → sweeps every
  // soft-deleted user older than the cutoff. Returns {purged,
  // older_than_days, cutoff}. Operator-triggered only.
  hardDeleteUsers(olderThanDays) {
    const params = new URLSearchParams();
    params.set("older_than_days", String(olderThanDays));
    return adminFetch("POST", "/admin/users/hard-delete?" + params.toString());
  },

  // GET /admin/users/:id/usage[?year_month=YYYY-MM] → monthly
  // usage breakdown (tokens, runs, agent split).
  getUserUsage(userID, yearMonth = "") {
    const params = new URLSearchParams();
    if (yearMonth) params.set("year_month", yearMonth);
    const qs = params.toString();
    return adminFetch(
      "GET",
      `/admin/users/${encodeURIComponent(userID)}/usage${qs ? "?" + qs : ""}`,
    );
  },

  // ---------------- Audit tab ----------------

  // GET /admin/audit?user_id=&action=&limit=&offset=
  listAudit({ userID = "", action = "", limit = 50, offset = 0 } = {}) {
    const params = new URLSearchParams();
    if (userID) params.set("user_id", userID);
    if (action) params.set("action", action);
    if (Number.isFinite(limit)) params.set("limit", String(limit));
    if (Number.isFinite(offset)) params.set("offset", String(offset));
    const qs = params.toString();
    return adminFetch("GET", `/admin/audit${qs ? "?" + qs : ""}`);
  },

  // ---------------- Billing ops ----------------

  // POST /admin/billing/reconcile { user_id } → force Stripe→DB
  // sync for one user. 503 when STRIPE_API_KEY is missing at
  // admin boot.
  reconcileBilling(userID) {
    return adminFetch("POST", "/admin/billing/reconcile", { user_id: userID });
  },
};
