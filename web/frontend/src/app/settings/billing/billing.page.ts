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
 * BillingPageComponent — Phase 11/12.
 *
 * Full settings/billing surface:
 *   - Current plan card sourced from /api/v1/me (active membership's
 *     plan_tier joins to /api/v1/plans for the human description).
 *   - Plan comparison grid with "Switch" buttons that POST to
 *     /api/v1/billing/checkout and 303-redirect to the returned URL.
 *   - "Manage subscription" button that POSTs to
 *     /api/v1/billing/portal and redirects to the Stripe Customer
 *     Portal — only enabled when the org already has a
 *     stripe_customer_id (the gateway returns 404/409 otherwise and
 *     we surface a clean disabled state).
 *   - Optional return-from-checkout banner read off ?checkout=success
 *     / ?checkout=cancel query strings.
 *
 * "Never invent data" rule:
 *   - When /api/v1/plans returns no plans, render "Unable to load
 *     plans" instead of a hardcoded fallback list.
 *   - When checkout returns 503 (stub provider, no STRIPE_API_KEY),
 *     render "Self-serve upgrades not yet enabled — contact your
 *     admin" — never fabricate a URL.
 */

import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';

import { BillingService, BillingPlan } from '../../usage/billing.service';
import { IdentityService, MeResponse } from '../../auth/identity.service';
import { PortalService } from './portal.service';

type LoadState = 'loading' | 'ready' | 'unavailable';

@Component({
  selector: 'app-billing-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  template: `
    <section class="billing-page">
      <header class="billing-page__header">
        <h2>Billing &amp; plan</h2>
        <p class="billing-page__lead">
          Manage your subscription, payment method, and invoices.
        </p>
      </header>

      <div *ngIf="returnBanner as banner" class="billing-page__banner" [class.billing-page__banner--ok]="banner.kind === 'success'" [class.billing-page__banner--warn]="banner.kind === 'cancel'" role="status">
        {{ banner.text }}
      </div>

      <div *ngIf="loadState === 'loading'" class="billing-page__loading">
        Loading plan…
      </div>

      <div
        *ngIf="loadState === 'unavailable'"
        class="billing-page__banner billing-page__banner--warn"
        role="alert"
      >
        Billing is unavailable right now. Try again in a moment.
      </div>

      <ng-container *ngIf="loadState === 'ready'">
        <!-- Current plan card -->
        <article class="billing-page__current" *ngIf="me?.user as u">
          <div class="billing-page__current-row">
            <div>
              <div class="billing-page__current-label">Current plan</div>
              <div class="billing-page__current-tier">{{ u.plan_tier }}</div>
              <div class="billing-page__current-org">{{ u.email }}</div>
            </div>
            <button
              type="button"
              class="billing-page__btn billing-page__btn--ghost"
              [disabled]="portalState === 'loading'"
              (click)="openPortal()"
            >
              <span *ngIf="portalState !== 'loading'">Manage subscription</span>
              <span *ngIf="portalState === 'loading'">Opening…</span>
            </button>
          </div>
          <p *ngIf="portalError" class="billing-page__error" role="alert">
            {{ portalError }}
          </p>
        </article>

        <!-- Plan comparison grid -->
        <h3 class="billing-page__plans-heading">Switch plan</h3>
        <div *ngIf="plansError" class="billing-page__banner billing-page__banner--warn">
          {{ plansError }}
        </div>
        <ul class="billing-page__plans" *ngIf="plans.length">
          <li
            *ngFor="let p of plans"
            class="billing-page__plan"
            [class.billing-page__plan--current]="isCurrent(p)"
          >
            <div class="billing-page__plan-header">
              <span class="billing-page__plan-name">{{ p.tier }}</span>
              <span *ngIf="isCurrent(p)" class="billing-page__plan-badge">Current</span>
            </div>
            <div class="billing-page__plan-meta">
              <span>{{ formatCap(p.monthly_token_cap) }} tokens/mo</span>
              <span aria-hidden="true">·</span>
              <span>{{ formatCap(p.max_concurrent_runs) }} concurrent</span>
              <span aria-hidden="true">·</span>
              <span>{{ formatCap(p.max_users) }} users</span>
            </div>
            <p class="billing-page__plan-desc">{{ p.description }}</p>
            <button
              type="button"
              class="billing-page__btn"
              [disabled]="isCurrent(p) || checkoutTier !== null"
              (click)="startCheckout(p)"
            >
              <ng-container *ngIf="checkoutTier === p.tier; else pickLabel">
                Redirecting…
              </ng-container>
              <ng-template #pickLabel>
                <ng-container *ngIf="isCurrent(p); else other">Current plan</ng-container>
                <ng-template #other>Switch to {{ p.tier }}</ng-template>
              </ng-template>
            </button>
            <p *ngIf="checkoutError && checkoutTier === p.tier" class="billing-page__error" role="alert">
              {{ checkoutError }}
            </p>
          </li>
        </ul>
      </ng-container>
    </section>
  `,
  styles: [
    `
      .billing-page__header h2 {
        margin: 0 0 4px 0;
        font-size: 18px;
        color: #00ffcc;
      }
      .billing-page__lead {
        color: #8aa1b3;
        font-size: 13px;
        margin: 0 0 20px 0;
      }
      .billing-page__loading {
        padding: 20px 0;
        color: #8aa1b3;
        font-size: 13px;
      }
      .billing-page__banner {
        padding: 10px 12px;
        border-radius: 6px;
        font-size: 13px;
        margin-bottom: 18px;
        border: 1px solid rgba(255, 255, 255, 0.12);
        background: rgba(255, 255, 255, 0.04);
      }
      .billing-page__banner--ok {
        border-color: rgba(0, 255, 160, 0.4);
        background: rgba(0, 255, 160, 0.06);
        color: #b6ffe4;
      }
      .billing-page__banner--warn {
        border-color: rgba(255, 200, 90, 0.4);
        background: rgba(255, 200, 90, 0.06);
        color: #ffd9a0;
      }
      .billing-page__current {
        padding: 18px 20px;
        border: 1px solid rgba(0, 255, 200, 0.32);
        border-radius: 10px;
        background: rgba(0, 255, 200, 0.04);
        margin-bottom: 28px;
      }
      .billing-page__current-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 20px;
      }
      .billing-page__current-label {
        font-size: 11px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #8aa1b3;
      }
      .billing-page__current-tier {
        font-size: 22px;
        font-weight: 600;
        color: #00ffcc;
        text-transform: uppercase;
        margin-top: 2px;
      }
      .billing-page__current-org {
        font-size: 12px;
        color: #c8d8e6;
        margin-top: 4px;
      }
      .billing-page__plans-heading {
        margin: 0 0 12px 0;
        font-size: 14px;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: #8aa1b3;
      }
      .billing-page__plans {
        list-style: none;
        margin: 0;
        padding: 0;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .billing-page__plan {
        padding: 14px 16px;
        border: 1px solid rgba(0, 255, 200, 0.22);
        border-radius: 8px;
        background: rgba(0, 255, 200, 0.04);
      }
      .billing-page__plan--current {
        border-color: rgba(180, 120, 255, 0.5);
        background: rgba(180, 120, 255, 0.06);
      }
      .billing-page__plan-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 4px;
      }
      .billing-page__plan-name {
        text-transform: uppercase;
        font-weight: 600;
        letter-spacing: 0.08em;
        color: #00ffcc;
      }
      .billing-page__plan-badge {
        font-size: 10px;
        letter-spacing: 0.08em;
        padding: 1px 6px;
        border-radius: 999px;
        background: rgba(180, 120, 255, 0.18);
        color: #c8a8ff;
        text-transform: uppercase;
      }
      .billing-page__plan-meta {
        display: flex;
        gap: 6px;
        font-size: 11px;
        opacity: 0.75;
        margin-bottom: 6px;
      }
      .billing-page__plan-desc {
        font-size: 12px;
        opacity: 0.7;
        margin: 0 0 12px 0;
      }
      .billing-page__btn {
        padding: 7px 14px;
        background: rgba(0, 255, 200, 0.14);
        color: #00ffcc;
        border: 1px solid rgba(0, 255, 200, 0.4);
        border-radius: 6px;
        cursor: pointer;
        font-size: 12px;
        letter-spacing: 0.04em;
      }
      .billing-page__btn:hover:not([disabled]) {
        background: rgba(0, 255, 200, 0.24);
      }
      .billing-page__btn[disabled] {
        opacity: 0.45;
        cursor: not-allowed;
      }
      .billing-page__btn--ghost {
        background: transparent;
      }
      .billing-page__error {
        margin: 10px 0 0 0;
        padding: 6px 8px;
        background: rgba(255, 90, 90, 0.10);
        border: 1px solid rgba(255, 90, 90, 0.4);
        border-radius: 6px;
        color: #ffb0b0;
        font-size: 12px;
      }
    `,
  ],
})
export class BillingPageComponent implements OnInit {
  loadState: LoadState = 'loading';
  plans: BillingPlan[] = [];
  me: MeResponse | null = null;
  checkoutTier: string | null = null;
  checkoutError = '';
  portalState: 'idle' | 'loading' = 'idle';
  portalError = '';
  plansError = '';
  returnBanner: { kind: 'success' | 'cancel'; text: string } | null = null;

  constructor(
    private billing: BillingService,
    private portal: PortalService,
    private identity: IdentityService,
    private cdr: ChangeDetectorRef,
  ) {}

  async ngOnInit(): Promise<void> {
    this.readReturnBanner();
    const meSnap = await this.identity.fetchMe();
    if (!meSnap.available || !meSnap.data) {
      this.loadState = 'unavailable';
      this.cdr.markForCheck();
      return;
    }
    this.me = meSnap.data;

    const plansRes = await this.billing.listPlans();
    if (!plansRes.ok) {
      this.plansError = plansRes.status === 0
        ? 'Unable to reach the server. Check that the gateway is running.'
        : plansRes.status === 404
          ? 'Plans endpoint not available — database may not be configured in this environment.'
          : `Unable to load plans (HTTP ${plansRes.status}).`;
    } else if (!plansRes.plans.length) {
      this.plansError = 'No plans found in the database.';
    }
    this.plans = plansRes.plans;
    this.loadState = 'ready';
    this.cdr.markForCheck();
  }

  isCurrent(p: BillingPlan): boolean {
    return !!this.me?.user && this.me.user.plan_tier === p.tier;
  }

  formatCap(n: number): string {
    if (n < 0) return '∞';
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
    if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
    return String(n);
  }

  async startCheckout(p: BillingPlan): Promise<void> {
    if (this.isCurrent(p) || this.checkoutTier) return;
    this.checkoutTier = p.tier;
    this.checkoutError = '';
    this.cdr.markForCheck();
    const outcome = await this.billing.createCheckoutSession(p.tier);
    if (outcome.outcome === 'redirect') {
      window.location.assign(outcome.url);
      return;
    }
    if (outcome.outcome === 'unavailable') {
      this.checkoutError =
        'Self-serve upgrades are not yet enabled on this deployment. Contact your admin.';
    } else {
      this.checkoutError = outcome.message;
    }
    this.checkoutTier = null;
    this.cdr.markForCheck();
  }

  async openPortal(): Promise<void> {
    if (this.portalState === 'loading') return;
    this.portalState = 'loading';
    this.portalError = '';
    this.cdr.markForCheck();
    const outcome = await this.portal.openPortal();
    if (outcome.outcome === 'redirect') {
      window.location.assign(outcome.url);
      return;
    }
    if (outcome.outcome === 'no_customer') {
      this.portalError =
        'You have not started a subscription yet. Pick a plan below to set up billing.';
    } else if (outcome.outcome === 'unavailable') {
      this.portalError =
        'The billing portal is not yet enabled on this deployment. Contact your admin.';
    } else {
      this.portalError = outcome.message;
    }
    this.portalState = 'idle';
    this.cdr.markForCheck();
  }

  /**
   * Reads `?checkout=success|cancel` so the user sees a friendly
   * banner when Stripe redirects back. We do NOT trust the query
   * string to mutate any state — the webhook is the source of truth
   * for plan changes; this is purely cosmetic.
   */
  private readReturnBanner(): void {
    if (typeof window === 'undefined' || !window.location) return;
    const params = new URLSearchParams(window.location.search);
    const v = params.get('checkout');
    if (v === 'success') {
      this.returnBanner = {
        kind: 'success',
        text: 'Payment received. Your plan will update in a moment.',
      };
    } else if (v === 'cancel') {
      this.returnBanner = {
        kind: 'cancel',
        text: 'Checkout cancelled. No charges were made.',
      };
    }
  }
}
