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
 * BillingService — SaaS Phase 6c.
 *
 * Thin fetch wrapper around two gateway endpoints:
 *   - GET  /api/v1/plans                (catalog; from Phase 6a)
 *   - POST /api/v1/billing/checkout     (create checkout session; from 6c)
 *
 * The upgrade modal calls both. SAAS_MODE-off short-circuits to
 * "unavailable" so the modal can render a clean disabled state
 * without the gateway being involved.
 *
 * "Never invent data" rule:
 *   - When the gateway returns 503 from /api/v1/billing/checkout
 *     (stub creator or ErrCheckoutUnavailable), we surface that as
 *     a typed `unavailable` outcome with the provider name echoed
 *     back. The modal renders "self-serve upgrades not yet
 *     enabled — contact your admin"; it MUST NOT fabricate a URL.
 *   - When the catalog request fails, the modal renders "Unable
 *     to load plans" rather than a hardcoded fallback list.
 */

import { Injectable } from '@angular/core';

declare global {
  interface Window {
    ENV?: Record<string, string>;
  }
}

/**
 * Mirrors `billing.Plan` (Go side). All numeric caps -1 mean
 * "unlimited" — the modal renders that as a dedicated badge
 * rather than the number "-1".
 */
export interface BillingPlan {
  tier: string;
  monthly_token_cap: number;
  max_concurrent_runs: number;
  max_users: number;
  description: string;
}

export interface PlansListResult {
  ok: boolean;
  plans: BillingPlan[];
  /** HTTP status from the most recent fetch — surfaced in error UI. */
  status: number;
}

/**
 * Discriminated union for the checkout-create outcome. The modal
 * `switch`es on `outcome` so adding a future state (e.g. "queued")
 * is a single new case rather than threading more booleans.
 */
export type CheckoutOutcome =
  | { outcome: 'redirect'; url: string; provider: string }
  | { outcome: 'unavailable'; provider: string; message: string }
  | { outcome: 'error'; message: string; status: number };

// saasModeOn() was removed 2026-06-22 (B-5). See usage.service.ts
// for the rationale: SAAS_MODE has no consumers and is no longer
// emitted into window.ENV.

@Injectable({ providedIn: 'root' })
export class BillingService {
  async listPlans(): Promise<PlansListResult> {
    try {
      const resp = await fetch('/api/v1/plans', {
        method: 'GET',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      if (!resp.ok) {
        return { ok: false, plans: [], status: resp.status };
      }
      const body = (await resp.json()) as { plans?: BillingPlan[] };
      // Go side enforces `plans: []` on the wire (locked at the
      // JSON boundary in PlansHandler), but defend in depth: a
      // future regression that ships `plans: null` MUST NOT
      // crash the modal.
      const plans = Array.isArray(body.plans) ? body.plans : [];
      return { ok: true, plans, status: resp.status };
    } catch {
      return { ok: false, plans: [], status: 0 };
    }
  }

  async createCheckoutSession(planTier: string): Promise<CheckoutOutcome> {
    try {
      const resp = await fetch('/api/v1/billing/checkout', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        credentials: 'same-origin',
        body: JSON.stringify({ plan_tier: planTier }),
      });
      if (resp.status === 503) {
        // Stub creator OR Stripe-without-key. Echo the provider
        // back so the modal can render the right copy.
        const body = (await resp.json().catch(() => ({}))) as {
          provider?: string;
          error?: string;
        };
        return {
          outcome: 'unavailable',
          provider: body.provider ?? '',
          message: body.error ?? 'Checkout provider unavailable.',
        };
      }
      if (!resp.ok) {
        const body = (await resp.json().catch(() => ({}))) as { error?: string };
        return {
          outcome: 'error',
          message: body.error ?? `HTTP ${resp.status}`,
          status: resp.status,
        };
      }
      const body = (await resp.json()) as { url?: string; provider?: string };
      if (!body.url) {
        // 200 without a URL would mean the gateway shipped a
        // broken response. Never redirect to undefined — surface
        // as an error.
        return {
          outcome: 'error',
          message: 'gateway returned empty checkout URL',
          status: 200,
        };
      }
      return {
        outcome: 'redirect',
        url: body.url,
        provider: body.provider ?? '',
      };
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'network error';
      return { outcome: 'error', message: msg, status: 0 };
    }
  }
}
