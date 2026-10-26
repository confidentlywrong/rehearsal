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
 * UsageTileComponent — SaaS Phase 4c.
 *
 * Renders a single neon/cyan/green KPI card that reports the
 * caller's current-month token usage. Self-hiding when SAAS_MODE is
 * off OR when the gateway returns a non-200 (e.g. billing DB
 * unreachable) — the dashboard MUST stay visually unchanged in
 * legacy single-tenant deployments.
 *
 * Polls /api/v1/usage every 30 seconds while the tile is mounted.
 * 30s is slow enough to keep gateway load negligible (one HTTP +
 * one Postgres SELECT per tab per 30s) but fast enough that a
 * just-completed sim shows up within a tick. The SQLite drainer
 * runs every 2s on the Go side so by the time we poll, the rollup
 * has converged.
 *
 * "Never invent data" rule:
 *   - When `updated_at` is missing the row has never been written;
 *     we render "0 tokens this month" explicitly, not a dash.
 *   - When the gateway responds 503 we render "Usage unavailable",
 *     never a zero.
 *   - An empty `agents[]` renders "No activity this month" rather
 *     than a confusing empty table.
 */

import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  Input,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { UsageService, UsageSnapshot } from './usage.service';

const POLL_INTERVAL_MS = 30_000;

@Component({
  selector: 'app-usage-tile',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  template: `
    <div
      class="usage-tile"
      *ngIf="snapshot?.available; else hiddenState"
      [class.usage-tile--warn]="quotaState() === 'warn'"
      [class.usage-tile--exhausted]="quotaState() === 'exhausted'"
      [class.usage-tile--inline]="inline"
    >
      <header class="usage-tile__header">
        <span class="usage-tile__label">TOKEN USAGE</span>
        <span class="usage-tile__month">{{ snapshot!.data!.year_month }}</span>
      </header>
      <div class="usage-tile__total">
        <span class="usage-tile__total-value">{{ formatTokens(snapshot!.data!.tokens_used) }}</span>
        <span class="usage-tile__total-unit">tokens</span>
      </div>
      <div class="usage-tile__org">user: {{ snapshot!.data!.user_id }}</div>

      <!--
        Quota meter + banner. All fields are TOP-LEVEL on the wire
        (rewritten 2026-06-22 after the UsageResponse contract was
        re-aligned with billing.QuotaState — the earlier nested
        "quota" sub-object was a phantom that produced an empty
        meter forever). Renders the plan pill, percentage bar,
        and exhausted/warn banners based on the same fields.
        Enterprise (tokens_cap=-1) gets a dedicated "unlimited"
        pill instead of a percentage bar.
      -->
      <div class="usage-tile__quota">
        <ng-container *ngIf="snapshot!.data!.tokens_cap < 0; else metered">
          <div class="usage-tile__plan-pill usage-tile__plan-pill--unlimited">
            {{ snapshot!.data!.plan_tier }} · unlimited
          </div>
        </ng-container>
        <ng-template #metered>
          <div class="usage-tile__plan-pill">{{ snapshot!.data!.plan_tier }}</div>
          <div class="usage-tile__bar">
            <div
              class="usage-tile__bar-fill"
              [style.width.%]="barWidth(snapshot!.data!.percent_used)"
            ></div>
          </div>
          <div class="usage-tile__bar-caption">
            {{ snapshot!.data!.percent_used | number: '1.0-1' }}% of
            {{ formatTokens(snapshot!.data!.tokens_cap) }}
          </div>
          <div
            *ngIf="snapshot!.data!.exhausted"
            class="usage-tile__banner usage-tile__banner--exhausted"
            role="alert"
          >
            Quota exhausted — new simulations are blocked until
            next month or a plan upgrade.
          </div>
          <div
            *ngIf="!snapshot!.data!.exhausted && snapshot!.data!.warn_high"
            class="usage-tile__banner usage-tile__banner--warn"
            role="status"
          >
            {{ snapshot!.data!.percent_used | number: '1.0-0' }}% of your monthly quota used.
          </div>
        </ng-template>
        <!--
          2026-06-23 (B-19): the "Change plan ▸" button that used
          to live here was removed. The user-menu and the inline
          usage tile were duplicating the plan-change entry point;
          Settings → Billing is now the sole place to upgrade.
          The whole upgrade-modal hoist machinery (B-10) was
          removed along with it.
        -->
      </div>
    </div>

    <ng-template #hiddenState>
      <!--
        Render absolutely nothing when SAAS_MODE is off or the
        gateway is unavailable. The tile must never leak debug text
        into the production-style dashboard.
      -->
    </ng-template>
  `,
  styles: [
    `
      .usage-tile {
        position: fixed;
        top: 16px;
        right: 16px;
        z-index: 99000;
        min-width: 220px;
        padding: 12px 16px;
        background: rgba(8, 16, 24, 0.85);
        border: 1px solid rgba(0, 255, 200, 0.4);
        border-radius: 8px;
        box-shadow: 0 0 20px rgba(0, 255, 200, 0.15);
        color: #e8f8ff;
        font-family: 'Inter', system-ui, sans-serif;
        font-size: 12px;
        line-height: 1.4;
        backdrop-filter: blur(6px);
        -webkit-backdrop-filter: blur(6px);
        pointer-events: all;
      }
      .usage-tile__header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 8px;
        font-size: 10px;
        letter-spacing: 0.12em;
        opacity: 0.75;
      }
      .usage-tile__label {
        color: #00ffcc;
        font-weight: 600;
      }
      .usage-tile__month {
        color: #88ffd0;
      }
      .usage-tile__total {
        display: flex;
        align-items: baseline;
        gap: 6px;
        margin: 4px 0 6px;
      }
      .usage-tile__total-value {
        font-size: 24px;
        font-weight: 700;
        color: #00ffaa;
        text-shadow: 0 0 8px rgba(0, 255, 170, 0.4);
      }
      .usage-tile__total-unit {
        font-size: 11px;
        opacity: 0.7;
      }
      .usage-tile__org {
        font-size: 10px;
        opacity: 0.55;
        margin-bottom: 8px;
      }
      .usage-tile__agents {
        border-top: 1px solid rgba(0, 255, 200, 0.18);
        padding-top: 6px;
      }
      .usage-tile__agent-row {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        padding: 2px 0;
      }
      .usage-tile__agent-name {
        opacity: 0.85;
      }
      .usage-tile__agent-tokens {
        color: #88ffd0;
        font-variant-numeric: tabular-nums;
      }
      .usage-tile__empty {
        font-size: 11px;
        opacity: 0.55;
        font-style: italic;
        padding: 4px 0;
      }
      .usage-tile__footer {
        margin-top: 8px;
        font-size: 10px;
        opacity: 0.45;
      }
      /* --- Phase 5c: quota meter + banner ---------------------- */
      .usage-tile--warn {
        border-color: rgba(255, 200, 60, 0.55);
        box-shadow: 0 0 22px rgba(255, 200, 60, 0.18);
      }
      .usage-tile--exhausted {
        border-color: rgba(255, 90, 90, 0.7);
        box-shadow: 0 0 26px rgba(255, 90, 90, 0.28);
      }
      .usage-tile__quota {
        margin: 6px 0 8px;
      }
      .usage-tile__plan-pill {
        display: inline-block;
        padding: 1px 8px;
        margin-bottom: 6px;
        font-size: 10px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        border-radius: 999px;
        border: 1px solid rgba(0, 255, 200, 0.4);
        color: #00ffcc;
      }
      .usage-tile__plan-pill--unlimited {
        border-color: rgba(180, 120, 255, 0.55);
        color: #c8a8ff;
      }
      .usage-tile__bar {
        position: relative;
        height: 6px;
        border-radius: 3px;
        background: rgba(0, 255, 200, 0.12);
        overflow: hidden;
      }
      .usage-tile__bar-fill {
        height: 100%;
        background: linear-gradient(90deg, #00ffaa, #00ffcc);
        transition: width 0.6s ease;
      }
      .usage-tile--warn .usage-tile__bar-fill {
        background: linear-gradient(90deg, #ffd066, #ffb84d);
      }
      .usage-tile--exhausted .usage-tile__bar-fill {
        background: linear-gradient(90deg, #ff7070, #ff5050);
      }
      .usage-tile__bar-caption {
        margin-top: 4px;
        font-size: 10px;
        opacity: 0.65;
        font-variant-numeric: tabular-nums;
      }
      .usage-tile__banner {
        margin-top: 8px;
        padding: 6px 8px;
        border-radius: 4px;
        font-size: 11px;
        line-height: 1.35;
      }
      .usage-tile__banner--warn {
        background: rgba(255, 200, 60, 0.12);
        border: 1px solid rgba(255, 200, 60, 0.4);
        color: #ffd066;
      }
      .usage-tile__banner--exhausted {
        background: rgba(255, 90, 90, 0.16);
        border: 1px solid rgba(255, 90, 90, 0.55);
        color: #ffb0b0;
        font-weight: 600;
      }
      /*
       * Phase 11 \u2014 payment-failed banner. Distinct from
       * --warn/--exhausted (those are quota-driven) so the user
       * can tell a billing problem from a usage one at a glance.
       * Magenta-ish tone keeps it readable against the cyan tile
       * without clashing with the red "quota exhausted" banner
       * (the two can render at the same time on an org that
       * blew its cap AND has a failed invoice).
       */
      .usage-tile__banner--payment-failed {
        background: rgba(255, 90, 200, 0.14);
        border: 1px solid rgba(255, 90, 200, 0.55);
        color: #ffb0e0;
        font-weight: 500;
      }
      .usage-tile__banner-link {
        display: inline;
        margin-left: 4px;
        padding: 0;
        background: transparent;
        border: none;
        color: inherit;
        font: inherit;
        text-decoration: underline;
        text-underline-offset: 2px;
        cursor: pointer;
      }
      .usage-tile__banner-link:hover {
        opacity: 0.85;
      }
      /*
       * 2026-06-23 (B-19): the .usage-tile__change-plan button
       * rules that used to live here were removed along with the
       * "Change plan ▸" trigger. The tile no longer exposes any
       * plan-change UI — Settings → Billing owns that flow.
       */

      /*
       * 2026-06-22 — Inline mode. Used when the tile is mounted
       * inside the user-menu dropdown instead of floating in the
       * top-right corner. We turn off everything that makes the
       * tile a self-contained card (fixed position, blur, shadow,
       * outer border, dark background) because the host dropdown
       * already provides those. The inner content (header,
       * total, quota meter, banners) renders identically.
       */
      .usage-tile--inline {
        position: static;
        top: auto;
        right: auto;
        z-index: auto;
        min-width: 0;
        width: 100%;
        padding: 8px 12px 4px;
        background: transparent;
        border: none;
        border-radius: 0;
        box-shadow: none;
        backdrop-filter: none;
        -webkit-backdrop-filter: none;
        font-size: 12px;
      }
    `,
  ],
})
export class UsageTileComponent implements OnInit, OnDestroy {
  snapshot: UsageSnapshot | null = null;
  /**
   * 2026-06-22 — when true the tile renders as an inline block
   * inside an enclosing panel (e.g. the user-menu dropdown)
   * instead of as a fixed-position overlay. Drops the floating
   * background/border/shadow because the host panel already
   * supplies them. The relocation was requested because the
   * floating tile was overlapping the user-menu pill in the
   * top-right corner.
   */
  @Input() inline = false;
  private pollHandle: number | null = null;

  constructor(
    private readonly usageService: UsageService,
    private readonly cdr: ChangeDetectorRef,
  ) {}

  ngOnInit(): void {
    void this.refresh();
    // setInterval returns a NodeJS.Timeout in some toolings but a
    // number in the browser. We're DOM-only here.
    this.pollHandle = window.setInterval(() => {
      void this.refresh();
    }, POLL_INTERVAL_MS);
  }

  ngOnDestroy(): void {
    if (this.pollHandle !== null) {
      window.clearInterval(this.pollHandle);
      this.pollHandle = null;
    }
  }

  formatTokens(n: number | null | undefined): string {
    // "Never invent data": when the gateway omitted the field we
    // render an em-dash instead of fabricating "0" or letting
    // String(undefined) leak the literal "undefined" into the UI.
    if (n == null || !Number.isFinite(n)) {
      return '—';
    }
    if (n >= 1_000_000) {
      return (n / 1_000_000).toFixed(1) + 'M';
    }
    if (n >= 1_000) {
      return (n / 1_000).toFixed(1) + 'k';
    }
    return String(n);
  }

  /**
   * Quota state for the tile-level CSS modifier classes. Reads
   * the top-level flags directly off the QuotaState payload
   * (rewritten 2026-06-22; the earlier `quota.exhausted` /
   * `quota.warn_high` indirection never existed on the wire).
   * Defaults to 'normal' when no snapshot is available — we
   * never invent a state.
   */
  quotaState(): 'normal' | 'warn' | 'exhausted' {
    const d = this.snapshot?.data;
    if (!d) return 'normal';
    if (d.exhausted) return 'exhausted';
    if (d.warn_high) return 'warn';
    return 'normal';
  }

  /**
   * Clamp the percent bar to [0, 100] so a brief over-cap render
   * doesn't blow past the container. The numeric caption still
   * shows the real value so users see the actual overage.
   */
  barWidth(pct: number): number {
    if (!isFinite(pct) || pct < 0) return 0;
    if (pct > 100) return 100;
    return pct;
  }

  private async refresh(): Promise<void> {
    const next = await this.usageService.fetchUsage();
    this.snapshot = next;
    this.cdr.markForCheck();
  }
}
