/**
 * Copyright 2026 Christian Haeussinger
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

import '@angular/compiler';
import { ChangeDetectorRef } from '@angular/core';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BillingPageComponent } from './billing.page';
import { BillingPlan } from '../../usage/billing.service';

describe('BillingPageComponent lifecycle & schema parity', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function createPage(overrides?: {
    fetchMe?: ReturnType<typeof vi.fn>;
    listPlans?: ReturnType<typeof vi.fn>;
    createCheckoutSession?: ReturnType<typeof vi.fn>;
    openPortal?: ReturnType<typeof vi.fn>;
  }) {
    const cdr = { markForCheck: vi.fn() } as unknown as ChangeDetectorRef;
    const billing = {
      listPlans: overrides?.listPlans ?? vi.fn().mockResolvedValue({ ok: true, plans: [], status: 200 }),
      createCheckoutSession: overrides?.createCheckoutSession ?? vi.fn(),
    } as any;
    const portal = {
      openPortal: overrides?.openPortal ?? vi.fn(),
    } as any;
    const identity = {
      fetchMe: overrides?.fetchMe ?? vi.fn().mockResolvedValue({
        available: true,
        status: 200,
        data: { user: { plan_tier: 'free', email: 'u@example.com' } },
      }),
    } as any;
    const page = new BillingPageComponent(billing, portal, identity, cdr);
    return { page, cdr, billing, portal, identity };
  }

  it('formats negative caps as infinity symbol and non-finite/nullish caps as em-dash', () => {
    const { page } = createPage();
    expect(page.formatCap(-1)).toBe('∞');
    expect(page.formatCap(3_000_000)).toBe('3M');
    expect(page.formatCap(500_000)).toBe('500K');
    expect(page.formatCap(null as unknown as number)).toBe('—');
    expect(page.formatCap(undefined as unknown as number)).toBe('—');
    expect(page.formatCap(Number.NaN)).toBe('—');
    expect(page.formatCap(Number.POSITIVE_INFINITY)).toBe('—');
  });

  it('ignores ngOnInit fetchMe and listPlans resolution after ngOnDestroy', async () => {
    let resolveMe!: (val: unknown) => void;
    const fetchMe = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveMe = resolve;
        }),
    );
    const { page, cdr } = createPage({ fetchMe });

    const initPromise = page.ngOnInit();
    page.ngOnDestroy();

    resolveMe({
      available: true,
      status: 200,
      data: { user: { plan_tier: 'pro', email: 'exec@example.com' } },
    });
    await initPromise;

    expect(page.me).toBeNull();
    expect(page.loadState).toBe('loading');
    expect(cdr.markForCheck).not.toHaveBeenCalled();
  });

  it('prevents startCheckout and openPortal mutations or navigation after ngOnDestroy', async () => {
    let resolveCheckout!: (val: unknown) => void;
    let resolvePortal!: (val: unknown) => void;
    const createCheckoutSession = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveCheckout = resolve;
        }),
    );
    const openPortal = vi.fn(
      () =>
        new Promise((resolve) => {
          resolvePortal = resolve;
        }),
    );
    const assignSpy = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign: assignSpy },
    });

    const { page, cdr } = createPage({ createCheckoutSession, openPortal });
    page.me = { user: { plan_tier: 'free', email: 'u@example.com' } } as any;
    const proPlan: BillingPlan = {
      tier: 'pro',
      monthly_token_cap: 3_000_000,
      max_concurrent_runs: 3,
      description: 'Pro',
    };

    const checkoutPromise = page.startCheckout(proPlan);
    const portalPromise = page.openPortal();
    (cdr.markForCheck as ReturnType<typeof vi.fn>).mockClear();

    page.ngOnDestroy();

    resolveCheckout({ outcome: 'redirect', url: 'https://checkout.stripe.com/pay/1', provider: 'stripe' });
    resolvePortal({ outcome: 'error', message: 'boom', status: 500 });
    await Promise.all([checkoutPromise, portalPromise]);

    expect(assignSpy).not.toHaveBeenCalled();
    expect(page.portalError).toBe('');
    expect(cdr.markForCheck).not.toHaveBeenCalled();
  });
});
