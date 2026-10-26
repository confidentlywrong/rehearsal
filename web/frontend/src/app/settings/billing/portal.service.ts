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
 * PortalService — Phase 12.
 *
 * Wraps POST /api/v1/billing/portal. The gateway proxies to the
 * Stripe Customer Portal session API (or returns 503 when the stub
 * provider is in use; 409 when the org has no stripe_customer_id
 * yet). The discriminated union below mirrors the BillingService
 * checkout outcome shape — the page switches on `outcome`.
 */

import { Injectable } from '@angular/core';

declare global {
  interface Window {
    ENV?: Record<string, string>;
  }
}

export type PortalOutcome =
  | { outcome: 'redirect'; url: string; provider: string }
  | { outcome: 'unavailable'; provider: string; message: string }
  | { outcome: 'no_customer'; message: string }
  | { outcome: 'error'; message: string; status: number };

// saasModeOn() was removed 2026-06-22 (B-5). See usage.service.ts
// for the rationale: SAAS_MODE has no consumers and is no longer
// emitted into window.ENV.

@Injectable({ providedIn: 'root' })
export class PortalService {
  async openPortal(): Promise<PortalOutcome> {
    try {
      const resp = await fetch('/api/v1/billing/portal', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body: JSON.stringify({}),
      });
      if (resp.status === 503) {
        const body = (await resp.json().catch(() => ({}))) as {
          provider?: string;
          error?: string;
        };
        return {
          outcome: 'unavailable',
          provider: body.provider ?? '',
          message: body.error ?? 'Portal provider unavailable.',
        };
      }
      if (resp.status === 409) {
        const body = (await resp.json().catch(() => ({}))) as { error?: string };
        return {
          outcome: 'no_customer',
          message: body.error ?? 'No Stripe customer for this org yet.',
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
        return {
          outcome: 'error',
          message: 'gateway returned empty portal URL',
          status: 200,
        };
      }
      return { outcome: 'redirect', url: body.url, provider: body.provider ?? '' };
    } catch {
      return { outcome: 'error', message: 'Network error.', status: 0 };
    }
  }
}
