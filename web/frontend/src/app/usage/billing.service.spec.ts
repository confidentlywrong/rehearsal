/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: align BillingPlan fixtures with Go API and test createCheckoutSession tier/success_url/cancel_url payload.
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
 * BillingService tests — exercises listPlans() and createCheckoutSession()
 * wire contracts and error mapping.
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
        { tier: 'free', monthly_token_cap: 300000, max_concurrent_runs: 1, description: 'Free' },
        { tier: 'pro', monthly_token_cap: 3000000, max_concurrent_runs: 3, description: 'Pro' },
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

  describe('createCheckoutSession', () => {
    it('sends tier, success_url, and cancel_url (never plan_tier) and returns redirect on valid HTTPS URL', async () => {
      const spy = vi.fn().mockResolvedValue(
        jsonResponse({ url: 'https://checkout.stripe.com/c/pay/cs_123', provider: 'stripe' }),
      );
      installFetch(spy);

      const outcome = await svc.createCheckoutSession('pro');

      expect(spy).toHaveBeenCalledTimes(1);
      const [url, init] = spy.mock.calls[0] as [string, RequestInit];
      expect(url).toBe('/api/v1/billing/checkout');
      expect(init.method).toBe('POST');
      expect(init.credentials).toBe('same-origin');
      const sent = JSON.parse(init.body as string) as Record<string, unknown>;
      expect(sent['tier']).toBe('pro');
      expect(sent['plan_tier']).toBeUndefined();
      expect(String(sent['success_url'])).toContain('/#/settings/billing?checkout=success');
      expect(String(sent['cancel_url'])).toContain('/#/settings/billing?checkout=cancel');
      expect(outcome).toEqual({
        outcome: 'redirect',
        url: 'https://checkout.stripe.com/c/pay/cs_123',
        provider: 'stripe',
      });
    });

    it('uses explicit successUrl and cancelUrl when provided', async () => {
      const spy = vi.fn().mockResolvedValue(
        jsonResponse({ url: 'https://checkout.stripe.com/c/pay/cs_456', provider: 'stripe' }),
      );
      installFetch(spy);

      await svc.createCheckoutSession(
        'team',
        'https://app.example.com/#/settings/billing?checkout=success',
        'https://app.example.com/#/settings/billing?checkout=cancel',
      );
      const [, init] = spy.mock.calls[0] as [string, RequestInit];
      expect(JSON.parse(init.body as string)).toEqual({
        tier: 'team',
        success_url: 'https://app.example.com/#/settings/billing?checkout=success',
        cancel_url: 'https://app.example.com/#/settings/billing?checkout=cancel',
      });
    });

    it('maps 503 to unavailable, 400 to error, and rejects unsafe or empty URLs', async () => {
      const spy = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ provider: 'stub', error: 'stub mode' }, 503))
        .mockResolvedValueOnce(jsonResponse({ error: 'invalid tier' }, 400))
        .mockResolvedValueOnce(jsonResponse({ url: '' }, 200))
        .mockResolvedValueOnce(jsonResponse({ url: 'javascript:alert(1)' }, 200))
        .mockRejectedValueOnce(new TypeError('network down'));
      installFetch(spy);

      expect(await svc.createCheckoutSession('pro')).toEqual({
        outcome: 'unavailable',
        provider: 'stub',
        message: 'stub mode',
      });
      expect(await svc.createCheckoutSession('free')).toEqual({
        outcome: 'error',
        message: 'invalid tier',
        status: 400,
      });
      expect(await svc.createCheckoutSession('pro')).toEqual({
        outcome: 'error',
        message: 'gateway returned empty checkout URL',
        status: 200,
      });
      expect(await svc.createCheckoutSession('pro')).toEqual({
        outcome: 'error',
        message: 'Invalid checkout redirect URL.',
        status: 200,
      });
      expect(await svc.createCheckoutSession('pro')).toEqual({
        outcome: 'error',
        message: 'network down',
        status: 0,
      });
    });
  });
});
