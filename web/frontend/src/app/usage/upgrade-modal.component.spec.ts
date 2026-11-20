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
import { UpgradeModalComponent } from './upgrade-modal.component';
import { BillingPlan } from './billing.service';

describe('UpgradeModalComponent lifecycle & formatting', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('formats null/non-finite token counts safely as em-dash', () => {
    const cdr = { markForCheck: vi.fn() } as unknown as ChangeDetectorRef;
    const modal = new UpgradeModalComponent({ listPlans: vi.fn(), createCheckoutSession: vi.fn() } as any, cdr);
    expect(modal.formatTokens(null)).toBe('—');
    expect(modal.formatTokens(undefined)).toBe('—');
    expect(modal.formatTokens(Number.NaN)).toBe('—');
    expect(modal.formatTokens(1_500_000)).toBe('1.5M');
  });

  it('prevents refreshPlans and onSelect post-await state mutations after ngOnDestroy', async () => {
    let resolvePlans!: (v: unknown) => void;
    let resolveCheckout!: (v: unknown) => void;
    const billing = {
      listPlans: vi.fn(() => new Promise((r) => { resolvePlans = r; })),
      createCheckoutSession: vi.fn(() => new Promise((r) => { resolveCheckout = r; })),
    } as any;
    const cdr = { markForCheck: vi.fn() } as unknown as ChangeDetectorRef;
    const assignSpy = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign: assignSpy },
    });

    const modal = new UpgradeModalComponent(billing, cdr);
    modal.ngOnInit();

    const plan: BillingPlan = {
      tier: 'pro',
      monthly_token_cap: 3_000_000,
      max_concurrent_runs: 3,
      description: 'Pro',
    };
    const selectPromise = modal.onSelect(plan);
    (cdr.markForCheck as ReturnType<typeof vi.fn>).mockClear();

    modal.ngOnDestroy();

    resolvePlans({ ok: true, plans: [plan], status: 200 });
    resolveCheckout({ outcome: 'redirect', url: 'https://checkout.stripe.com/pay/1', provider: 'stripe' });
    await selectPromise;
    await Promise.resolve();

    expect(modal.plans).toEqual([]);
    expect(modal.state()).toBe('loading');
    expect(assignSpy).not.toHaveBeenCalled();
    expect(cdr.markForCheck).not.toHaveBeenCalled();
  });
});
