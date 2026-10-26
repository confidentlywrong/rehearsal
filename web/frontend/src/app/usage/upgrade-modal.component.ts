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
 * UpgradeModalComponent — SaaS Phase 6c.
 *
 * Modal launched from the usage tile's "Change plan ▸" link. Lists
 * the plans available from /api/v1/plans and triggers a Checkout
 * session via /api/v1/billing/checkout when the user picks a tier.
 *
 * No router is involved (the dashboard is single-page, no
 * Angular Router by design — see the SAAS plan doc §4). The modal
 * is a child of UsageTileComponent and toggled via an @Input.
 *
 * Render states (the template `*ngSwitch`es on `state()`):
 *   - 'loading'      — plans request in flight
 *   - 'error'        — catalog fetch failed; show retry
 *   - 'unavailable'  — checkout creator is stub (503); show
 *                     "contact admin" copy. NOT a transient state
 *                     — we don't poll, the user closes the modal.
 *   - 'redirecting'  — successful checkout response, location
 *                     change is in flight. Brief; the navigation
 *                     unmounts us. Defensive against a slow nav.
 *   - 'ready'        — plans loaded, awaiting user selection.
 *
 * "Never invent data" rule:
 *   - We never render a placeholder plan list. Loading shows a
 *     spinner; error shows a retry button; empty catalog shows
 *     "No plans configured yet" (also a real state — the seed
 *     migration might not have run).
 */

import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  EventEmitter,
  Input,
  OnInit,
  Output,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  BillingPlan,
  BillingService,
  CheckoutOutcome,
} from './billing.service';

type ModalState = 'loading' | 'error' | 'ready' | 'redirecting' | 'unavailable';

@Component({
  selector: 'app-upgrade-modal',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  template: `
    <div
      class="upgrade-backdrop"
      *ngIf="open"
      role="dialog"
      aria-modal="true"
      aria-labelledby="upgrade-title"
      (click)="onBackdropClick($event)"
    >
      <div class="upgrade-modal" (click)="$event.stopPropagation()">
        <header class="upgrade-modal__header">
          <h2 id="upgrade-title" class="upgrade-modal__title">Change plan</h2>
          <button
            type="button"
            class="upgrade-modal__close"
            aria-label="Close"
            (click)="onClose()"
          >
            ×
          </button>
        </header>

        <ng-container [ngSwitch]="state()">
          <!-- Loading the plans catalog. -->
          <div *ngSwitchCase="'loading'" class="upgrade-modal__loading">
            <span class="upgrade-modal__spinner" aria-hidden="true"></span>
            <span>Loading plans…</span>
          </div>

          <!-- Catalog fetch failed. -->
          <div *ngSwitchCase="'error'" class="upgrade-modal__error" role="alert">
            <p>Unable to load plans.</p>
            <button
              type="button"
              class="upgrade-modal__btn upgrade-modal__btn--ghost"
              (click)="onRetry()"
            >
              Try again
            </button>
          </div>

          <!-- 503 from the checkout endpoint — stub mode or
               Stripe-without-key. Distinct from 'error' because
               there is nothing the user can retry. -->
          <div
            *ngSwitchCase="'unavailable'"
            class="upgrade-modal__error upgrade-modal__error--soft"
            role="status"
          >
            <p>Self-serve upgrades are not yet enabled on this deployment.</p>
            <p class="upgrade-modal__hint">
              Contact your administrator to change plans
              <ng-container *ngIf="checkoutProvider">
                (provider: <code>{{ checkoutProvider }}</code>)
              </ng-container>.
            </p>
            <button
              type="button"
              class="upgrade-modal__btn upgrade-modal__btn--ghost"
              (click)="onClose()"
            >
              Close
            </button>
          </div>

          <!-- Empty catalog. Real state — the seed migration may
               not have run. We say so explicitly. -->
          <div
            *ngSwitchCase="'ready'"
            class="upgrade-modal__body"
          >
            <ng-container *ngIf="plans.length === 0; else planList">
              <p class="upgrade-modal__hint">No plans configured yet.</p>
            </ng-container>
            <ng-template #planList>
              <ul class="upgrade-modal__plans">
                <li
                  *ngFor="let p of plans; trackBy: trackByTier"
                  class="upgrade-modal__plan"
                  [class.upgrade-modal__plan--current]="p.tier === currentTier"
                >
                  <div class="upgrade-modal__plan-header">
                    <span class="upgrade-modal__plan-name">{{ p.tier }}</span>
                    <span
                      class="upgrade-modal__plan-badge"
                      *ngIf="p.tier === currentTier"
                    >
                      current
                    </span>
                  </div>
                  <div class="upgrade-modal__plan-meta">
                    <span>
                      {{
                        p.monthly_token_cap < 0
                          ? 'unlimited tokens'
                          : (formatTokens(p.monthly_token_cap) + ' tokens/mo')
                      }}
                    </span>
                    <span>·</span>
                    <span>
                      {{
                        p.max_concurrent_runs < 0
                          ? 'unlimited concurrent'
                          : (p.max_concurrent_runs + ' concurrent')
                      }}
                    </span>
                  </div>
                  <p class="upgrade-modal__plan-desc" *ngIf="p.description">
                    {{ p.description }}
                  </p>
                  <button
                    type="button"
                    class="upgrade-modal__btn"
                    [disabled]="p.tier === currentTier || pendingTier === p.tier"
                    (click)="onSelect(p)"
                  >
                    <ng-container *ngIf="pendingTier === p.tier; else label">
                      Working…
                    </ng-container>
                    <ng-template #label>
                      {{ p.tier === currentTier ? 'Current plan' : 'Choose ' + p.tier }}
                    </ng-template>
                  </button>
                </li>
              </ul>
            </ng-template>
            <p
              *ngIf="selectError"
              class="upgrade-modal__plan-error"
              role="alert"
            >
              {{ selectError }}
            </p>
          </div>

          <div *ngSwitchCase="'redirecting'" class="upgrade-modal__loading">
            <span class="upgrade-modal__spinner" aria-hidden="true"></span>
            <span>Redirecting to {{ checkoutProvider || 'checkout' }}…</span>
          </div>
        </ng-container>
      </div>
    </div>
  `,
  styles: [
    `
      .upgrade-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 8, 12, 0.6);
        backdrop-filter: blur(4px);
        -webkit-backdrop-filter: blur(4px);
        z-index: 99500;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .upgrade-modal {
        width: min(480px, 92vw);
        max-height: 86vh;
        overflow: auto;
        background: rgba(10, 18, 26, 0.96);
        border: 1px solid rgba(0, 255, 200, 0.4);
        border-radius: 10px;
        box-shadow: 0 0 32px rgba(0, 255, 200, 0.18);
        color: #e8f8ff;
        font-family: 'Inter', system-ui, sans-serif;
        padding: 20px 22px;
      }
      .upgrade-modal__header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 14px;
      }
      .upgrade-modal__title {
        font-size: 16px;
        margin: 0;
        letter-spacing: 0.04em;
        color: #00ffcc;
      }
      .upgrade-modal__close {
        background: transparent;
        color: #e8f8ff;
        border: none;
        font-size: 22px;
        line-height: 1;
        cursor: pointer;
        padding: 0 6px;
      }
      .upgrade-modal__close:hover {
        color: #00ffcc;
      }
      .upgrade-modal__loading,
      .upgrade-modal__error {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 12px;
        padding: 24px 0;
        font-size: 13px;
      }
      .upgrade-modal__error--soft {
        color: #c8d8e6;
      }
      .upgrade-modal__hint {
        opacity: 0.7;
        font-size: 12px;
        text-align: center;
        margin: 0;
      }
      .upgrade-modal__spinner {
        width: 18px;
        height: 18px;
        border: 2px solid rgba(0, 255, 200, 0.25);
        border-top-color: #00ffcc;
        border-radius: 50%;
        animation: upgrade-spin 0.8s linear infinite;
      }
      @keyframes upgrade-spin {
        to { transform: rotate(360deg); }
      }
      .upgrade-modal__plans {
        list-style: none;
        margin: 0;
        padding: 0;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .upgrade-modal__plan {
        border: 1px solid rgba(0, 255, 200, 0.22);
        border-radius: 8px;
        padding: 12px 14px;
        background: rgba(0, 255, 200, 0.04);
      }
      .upgrade-modal__plan--current {
        border-color: rgba(180, 120, 255, 0.5);
        background: rgba(180, 120, 255, 0.06);
      }
      .upgrade-modal__plan-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 4px;
      }
      .upgrade-modal__plan-name {
        text-transform: uppercase;
        font-weight: 600;
        letter-spacing: 0.08em;
        color: #00ffcc;
      }
      .upgrade-modal__plan-badge {
        font-size: 10px;
        letter-spacing: 0.08em;
        padding: 1px 6px;
        border-radius: 999px;
        background: rgba(180, 120, 255, 0.18);
        color: #c8a8ff;
        text-transform: uppercase;
      }
      .upgrade-modal__plan-meta {
        display: flex;
        gap: 6px;
        font-size: 11px;
        opacity: 0.75;
        margin-bottom: 6px;
      }
      .upgrade-modal__plan-desc {
        font-size: 12px;
        opacity: 0.7;
        margin: 0 0 10px;
      }
      .upgrade-modal__btn {
        padding: 6px 14px;
        background: rgba(0, 255, 200, 0.14);
        color: #00ffcc;
        border: 1px solid rgba(0, 255, 200, 0.4);
        border-radius: 6px;
        cursor: pointer;
        font-size: 12px;
        letter-spacing: 0.04em;
      }
      .upgrade-modal__btn:hover:not([disabled]) {
        background: rgba(0, 255, 200, 0.22);
      }
      .upgrade-modal__btn:disabled {
        opacity: 0.45;
        cursor: not-allowed;
      }
      .upgrade-modal__btn--ghost {
        background: transparent;
      }
      .upgrade-modal__plan-error {
        margin-top: 14px;
        padding: 8px 10px;
        background: rgba(255, 90, 90, 0.12);
        border: 1px solid rgba(255, 90, 90, 0.4);
        border-radius: 6px;
        color: #ffb0b0;
        font-size: 12px;
      }
    `,
  ],
})
export class UpgradeModalComponent implements OnInit {
  /** Visibility — controlled by the parent (UsageTileComponent). */
  @Input() open = false;
  /** Echoed back as the "current" badge / disabled "Choose" button. */
  @Input() currentTier: string | null = null;

  @Output() readonly close = new EventEmitter<void>();

  plans: BillingPlan[] = [];
  pendingTier: string | null = null;
  selectError: string | null = null;
  checkoutProvider = '';

  private fetchStatus: 'loading' | 'ok' | 'error' = 'loading';
  private creatorUnavailable = false;
  private redirecting = false;

  constructor(
    private readonly billing: BillingService,
    private readonly cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    void this.refreshPlans();
  }

  state(): ModalState {
    if (this.redirecting) return 'redirecting';
    if (this.creatorUnavailable) return 'unavailable';
    if (this.fetchStatus === 'loading') return 'loading';
    if (this.fetchStatus === 'error') return 'error';
    return 'ready';
  }

  trackByTier(_i: number, p: BillingPlan): string {
    return p.tier;
  }

  formatTokens(n: number): string {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(0) + 'k';
    return String(n);
  }

  async onRetry(): Promise<void> {
    await this.refreshPlans();
  }

  onClose(): void {
    this.close.emit();
  }

  onBackdropClick(_event: MouseEvent): void {
    // Backdrop click closes — same as the × button. We don't
    // close on Escape because the dashboard already wires global
    // shortcuts and we'd risk conflicts.
    this.onClose();
  }

  async onSelect(plan: BillingPlan): Promise<void> {
    if (plan.tier === this.currentTier) return;
    if (this.pendingTier) return;
    this.pendingTier = plan.tier;
    this.selectError = null;
    this.cdr.markForCheck();

    const outcome = await this.billing.createCheckoutSession(plan.tier);
    this.applyOutcome(outcome);
  }

  private applyOutcome(outcome: CheckoutOutcome): void {
    switch (outcome.outcome) {
      case 'redirect':
        // Real navigation. We set `redirecting` so the spinner
        // shows for the brief moment between assignment and the
        // browser tearing down the page. If location.assign is
        // a no-op (e.g. in a test environment), the modal stays
        // in this state — that's acceptable; production never
        // hits that case.
        this.redirecting = true;
        this.checkoutProvider = outcome.provider;
        this.cdr.markForCheck();
        window.location.assign(outcome.url);
        return;
      case 'unavailable':
        // Latch the unavailable state so further clicks don't
        // re-trigger the same 503. The user must close + reopen.
        this.creatorUnavailable = true;
        this.checkoutProvider = outcome.provider;
        this.pendingTier = null;
        this.cdr.markForCheck();
        return;
      case 'error':
        this.selectError = outcome.message;
        this.pendingTier = null;
        this.cdr.markForCheck();
        return;
    }
  }

  private async refreshPlans(): Promise<void> {
    this.fetchStatus = 'loading';
    this.creatorUnavailable = false;
    this.selectError = null;
    this.cdr.markForCheck();
    const res = await this.billing.listPlans();
    if (!res.ok) {
      this.fetchStatus = 'error';
    } else {
      this.plans = res.plans;
      this.fetchStatus = 'ok';
    }
    this.cdr.markForCheck();
  }
}
