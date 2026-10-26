// Copyright 2026 Christian Haeussinger
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * Copyright 2026 Google LLC. Apache-2.0.
 *
 * BillingService smoke tests — exercises listPlans() error mapping
 * to catch regressions in API fetch error handling.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BillingService } from './billing.service';

type FetchSpy = ReturnType<typeof vi.fn>;

function installFetch(spy: FetchSpy): void {
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: spy,
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('BillingService', () => {
  let svc: BillingService;

  beforeEach(() => {
    svc = new BillingService();
  });

  describe('listPlans', () => {
    it('returns plans on 200', async () => {
      const plans = [
        { tier: 'free', monthly_token_cap: 300000, max_concurrent_runs: 1, max_users: 5, description: 'Free' },
        { tier: 'pro', monthly_token_cap: 3000000, max_concurrent_runs: 3, max_users: 25, description: 'Pro' },
      ];
      const spy = vi.fn().mockResolvedValue(jsonResponse({ plans }));
      installFetch(spy);

      const result = await svc.listPlans();
      expect(result.ok).toBe(true);
      expect(result.plans).toHaveLength(2);
      expect(result.plans[0].tier).toBe('free');
      expect(result.status).toBe(200);
    });

    it('returns ok:false on 404 (endpoint not mounted)', async () => {
      const spy = vi.fn().mockResolvedValue(new Response('Not Found', { status: 404 }));
      installFetch(spy);

      const result = await svc.listPlans();
      expect(result.ok).toBe(false);
      expect(result.plans).toEqual([]);
      expect(result.status).toBe(404);
    });

    it('returns ok:false with status 0 on network error', async () => {
      const spy = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
      installFetch(spy);

      const result = await svc.listPlans();
      expect(result.ok).toBe(false);
      expect(result.plans).toEqual([]);
      expect(result.status).toBe(0);
    });

    it('handles null plans gracefully', async () => {
      const spy = vi.fn().mockResolvedValue(jsonResponse({ plans: null }));
      installFetch(spy);

      const result = await svc.listPlans();
      expect(result.ok).toBe(true);
      expect(result.plans).toEqual([]);
    });
  });
});
