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
 * UsageService — SaaS Phase 4c.
 *
 * Thin fetch wrapper around the gateway's GET /api/v1/usage endpoint.
 * The AuthService's APP_INITIALIZER has already patched window.fetch
 * to attach `Authorization: Bearer` headers on same-origin requests
 * when SAAS_MODE is on, so this service does not need to know
 * anything about authentication.
 *
 * In SAAS_MODE=off the gateway never registers the /api/v1/usage
 * route — the request will 404. UsageService surfaces that as a
 * disabled-state response rather than throwing, so the tile can
 * cleanly hide itself.
 */

import { Injectable } from '@angular/core';

declare global {
  interface Window {
    ENV?: Record<string, string>;
  }
}

/**
 * UsageResponse — Phase 4c, aligned 2026-06-22 with the actual
 * gateway payload.
 *
 * Mirrors `billing.QuotaState` (internal/billing/plans.go) one-for-one.
 * The fields are emitted at the TOP level by /api/v1/usage; an
 * earlier draft of this interface nested them under a `quota`
 * sub-object and added speculative `total_tokens`, `agents[]`,
 * `cost_micros`, `updated_at`, `payment_failed_at` fields. The
 * gateway never emitted those — reading them produced
 * `undefined tokens` in the tile and silently swallowed the quota
 * meter. Per the "never invent data" rule, the interface now
 * matches the wire exactly and the tile template renders only
 * fields that actually arrive.
 *
 * If/when the gateway gains per-agent breakdowns or invoice
 * history, add the fields here and to QuotaState in lockstep.
 */
export interface UsageResponse {
  user_id: string;
  year_month: string;
  plan_tier: string;
  tokens_used: number;
  /** -1 = unlimited (enterprise). */
  tokens_cap: number;
  percent_used: number;
  exhausted: boolean;
  warn_high: boolean;
  /** -1 = unlimited (enterprise). */
  max_concurrent_runs: number;
}

export interface UsageSnapshot {
  /** True when the gateway returned a 200. False on 404/503/etc. */
  available: boolean;
  /** Populated when available is true. */
  data?: UsageResponse;
  /** HTTP status from the most recent fetch, for debug display. */
  status: number;
}

// saasModeOn() was removed 2026-06-22 (B-5). The per-user model
// has no single-tenant branch left in any handler; SAAS_MODE is no
// longer emitted by /config.js. The helper was defined here but
// never called from any callsite in this file or the SPA.

@Injectable({ providedIn: 'root' })
export class UsageService {
  /**
   * Fetch the current month's usage for the caller.
   *
   * Returns `available: false` (rather than throwing) when the
   * gateway responds non-200 — keeps the calling component's
   * render path simple. A surrounding visibility gate (e.g. the
   * usage tile self-hiding when no Firebase auth is wired) is
   * still allowed to use saasModeOn() as a cheap pre-check; the
   * service itself no longer short-circuits.
   */
  async fetchUsage(yearMonth?: string): Promise<UsageSnapshot> {
    const qs = yearMonth ? `?year_month=${encodeURIComponent(yearMonth)}` : '';
    try {
      const resp = await fetch(`/api/v1/usage${qs}`, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      if (!resp.ok) {
        return { available: false, status: resp.status };
      }
      const data = (await resp.json()) as UsageResponse;
      return { available: true, data, status: resp.status };
    } catch {
      // Network error — treat the same as disabled. The tile shows
      // "unavailable" rather than crashing the dashboard.
      return { available: false, status: 0 };
    }
  }
}
