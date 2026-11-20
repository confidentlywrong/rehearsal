/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: validate admin API arguments, preserve non-JSON error text, gate X-Local-Dev-Superadmin, and expose BFF environment reset.
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

// Shared admin API client. All /admin/* and /api/admin/* endpoints run on
// the same origin; cmd/admin mounts them behind RequireSuperadmin which
// checks UserContext.IsSuperadmin. X-Local-Dev-Superadmin is attached ONLY
// when local dev is active (explicitly opted in via window.ENV.LOCAL_DEV or
// running over non-HTTPS loopback), never on cloud/production hosts.

export const LOCAL_SUPERADMIN_HEADER = "X-Local-Dev-Superadmin";

export function isLocalDevSuperadminEnabled() {
  const win = typeof window !== "undefined" ? window : undefined;
  const env = win?.ENV && typeof win.ENV === "object" ? win.ENV : {};
  const saasMode = String(env.SAAS_MODE ?? "").trim().toLowerCase();
  if (saasMode === "on" || saasMode === "true" || saasMode === "1") {
    return false;
  }

  const rawFlag = String(env.LOCAL_DEV ?? "").trim().toLowerCase();
  if (rawFlag === "0" || rawFlag === "false") {
    return false;
  }

  const hostname = String(win?.location?.hostname || "").trim().toLowerCase();
  const isLoopback =
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "::1" ||
    hostname === "[::1]";

  if (rawFlag === "1" || rawFlag === "true") {
    return isLoopback || hostname === "";
  }

  const protocol = String(win?.location?.protocol || "").trim().toLowerCase();
  return isLoopback && protocol !== "https:";
}

export class AdminApiError extends Error {
  constructor(message, { status = 0, body = null } = {}) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
    this.body = body;
  }
}

function requireNonEmptyUserId(userID) {
  if (typeof userID !== "string" || !userID.trim()) {
    throw new AdminApiError("userID must be a non-empty string", { status: 0 });
  }
  return userID.trim();
}

async function parseBodySafe(resp) {
  if (typeof resp.text === "function") {
    let rawText = "";
    try {
      rawText = await resp.text();
    } catch {
      rawText = "";
    }
    const trimmed = typeof rawText === "string" ? rawText.trim() : "";
    if (!trimmed) {
      return { payload: null, rawText: "" };
    }
    try {
      return { payload: JSON.parse(trimmed), rawText };
    } catch {
      return { payload: null, rawText };
    }
  }
  if (typeof resp.json === "function") {
    try {
      return { payload: await resp.json(), rawText: "" };
    } catch {
      return { payload: null, rawText: "" };
    }
  }
  return { payload: null, rawText: "" };
}

export async function adminFetch(method, path, body) {
  const headers = {};
  if (isLocalDevSuperadminEnabled()) {
    headers[LOCAL_SUPERADMIN_HEADER] = "1";
  }
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e || "unknown error");
    throw new AdminApiError(`network error: ${msg}`);
  }

  // 204 No Content (hard-delete, soft-delete) has no body.
  if (resp.status === 204) {
    if (!resp.ok) {
      throw new AdminApiError(`HTTP ${resp.status}`, { status: resp.status });
    }
    return null;
  }

  const { payload, rawText } = await parseBodySafe(resp);
  if (!resp.ok) {
    const snippet = rawText.trim().replace(/\s+/g, " ").slice(0, 200);
    const msg =
      (payload && typeof payload === "object" && payload.error) ||
      (snippet ? `HTTP ${resp.status}: ${snippet}` : `HTTP ${resp.status}`);
    throw new AdminApiError(msg, { status: resp.status, body: payload });
  }

  if (rawText.trim() !== "" && payload === null) {
    throw new AdminApiError("Invalid JSON response from server", {
      status: resp.status,
    });
  }

  return payload;
}

export const adminApi = {
  // ---------------- Users tab ----------------

  // GET /admin/users → { users: [{id, email, display_name,
  //   plan_tier, stripe_customer_id, payment_failed_at,
  //   is_superadmin, created_at, deleted_at, status}] }
  listUsers() {
    return adminFetch("GET", "/admin/users");
  },

  // PUT /admin/users/:id/plan { plan_tier: "free|pro|team|enterprise|comp" }
  setUserPlan(userID, planTier) {
    const id = requireNonEmptyUserId(userID);
    return adminFetch(
      "PUT",
      `/admin/users/${encodeURIComponent(id)}/plan`,
      { plan_tier: planTier },
    );
  },

  // GET /admin/users/:id/export → full per-user JSON dump (user
  // row + audit history).
  exportUser(userID) {
    const id = requireNonEmptyUserId(userID);
    return adminFetch(
      "GET",
      `/admin/users/${encodeURIComponent(id)}/export`,
    );
  },

  // DELETE /admin/users/:id → soft-delete (sets deleted_at). The
  // row stays in place so audit FKs survive; hard-delete sweeps
  // away anything older than ?older_than_days=N.
  softDeleteUser(userID) {
    const id = requireNonEmptyUserId(userID);
    return adminFetch(
      "DELETE",
      `/admin/users/${encodeURIComponent(id)}`,
    );
  },

  // POST /admin/users/:id/erase → immediate hard-delete of one
  // user. Audit row written first so the email is preserved.
  eraseUser(userID) {
    const id = requireNonEmptyUserId(userID);
    return adminFetch(
      "POST",
      `/admin/users/${encodeURIComponent(id)}/erase`,
    );
  },

  // POST /admin/users/:id/approve → flip the user's status from
  // pending → approved. Idempotent. Audited as user.approved.
  approveUser(userID) {
    const id = requireNonEmptyUserId(userID);
    return adminFetch(
      "POST",
      `/admin/users/${encodeURIComponent(id)}/approve`,
    );
  },

  // POST /admin/users/:id/reject → flip the user's status to
  // rejected. Idempotent. Audited as user.rejected.
  rejectUser(userID) {
    const id = requireNonEmptyUserId(userID);
    return adminFetch(
      "POST",
      `/admin/users/${encodeURIComponent(id)}/reject`,
    );
  },

  // POST /admin/users/hard-delete?older_than_days=N → sweeps every
  // soft-deleted user older than the cutoff. Returns {purged,
  // older_than_days, cutoff}. Operator-triggered only.
  hardDeleteUsers(olderThanDays) {
    if (
      typeof olderThanDays !== "number" ||
      !Number.isSafeInteger(olderThanDays) ||
      olderThanDays < 1
    ) {
      throw new AdminApiError("olderThanDays must be a positive integer", {
        status: 0,
      });
    }
    const params = new URLSearchParams();
    params.set("older_than_days", String(olderThanDays));
    return adminFetch("POST", "/admin/users/hard-delete?" + params.toString());
  },

  // GET /admin/users/:id/usage[?year_month=YYYY-MM] → monthly
  // usage breakdown (tokens, runs, agent split).
  getUserUsage(userID, yearMonth = "") {
    const id = requireNonEmptyUserId(userID);
    const params = new URLSearchParams();
    if (yearMonth) params.set("year_month", yearMonth);
    const qs = params.toString();
    return adminFetch(
      "GET",
      `/admin/users/${encodeURIComponent(id)}/usage${qs ? "?" + qs : ""}`,
    );
  },

  // ---------------- Audit tab ----------------

  // GET /admin/audit?user_id=&action=&limit=&offset=
  listAudit({ userID = "", action = "", limit = 50, offset = 0 } = {}) {
    const params = new URLSearchParams();
    if (userID) params.set("user_id", userID);
    if (action) params.set("action", action);
    const safeLimit =
      typeof limit === "number" && Number.isFinite(limit) && limit >= 1
        ? Math.floor(limit)
        : 50;
    const safeOffset =
      typeof offset === "number" && Number.isFinite(offset) && offset >= 0
        ? Math.floor(offset)
        : 0;
    params.set("limit", String(safeLimit));
    params.set("offset", String(safeOffset));
    const qs = params.toString();
    return adminFetch("GET", `/admin/audit${qs ? "?" + qs : ""}`);
  },

  // ---------------- Billing ops ----------------

  // POST /admin/billing/reconcile { user_id } → force Stripe→DB
  // sync for one user.
  reconcileBilling(userID) {
    const id = requireNonEmptyUserId(userID);
    return adminFetch("POST", "/admin/billing/reconcile", { user_id: id });
  },

  // ---------------- Environment ops ----------------

  // POST /api/admin/environment/reset { targets } → authenticated BFF proxy
  resetEnvironment(targets) {
    if (
      !Array.isArray(targets) ||
      targets.length === 0 ||
      !targets.every((t) => typeof t === "string" && t.trim() !== "")
    ) {
      throw new AdminApiError(
        "targets must be a non-empty array of non-empty strings",
        {
          status: 0,
        },
      );
    }
    return adminFetch("POST", "/api/admin/environment/reset", {
      targets: targets.map((t) => t.trim()),
    });
  },
};
