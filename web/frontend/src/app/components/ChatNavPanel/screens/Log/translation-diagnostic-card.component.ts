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
 * Renders the Translation Diagnostic card emitted by the
 * `emit_translation_diagnostic` tool of `leadership_planner_with_eval`.
 *
 * Surface: a single card per diagnostic. Header = "Translation Diagnostic"
 * + "(Simulated)" badge. Below that: per-stakeholder blocks showing
 * trigger phrases, translation gaps (System → Lifeworld), and
 * suggested reframings.
 *
 * Resilience: parses permissively (full tool_end envelope OR bare
 * `{gaps, summary}` payload). If the payload is empty or malformed,
 * renders nothing.
 */

import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

type GapSeverity = 'high' | 'medium' | 'low';

interface TriggerPhrase {
  phrase: string;
  tick: number;
  leader_intent: string;
  stakeholder_decoded_as: string;
  gap_type: string;
  severity: GapSeverity;
}

interface StakeholderGap {
  stakeholder_id: string;
  stakeholder_label: string;
  trigger_phrases: TriggerPhrase[];
  suggested_reframing: string;
  overall_gap_severity: GapSeverity;
}

interface DiagnosticPayload {
  gaps: StakeholderGap[];
  summary: string;
  inferred: boolean;
}

const SEVERITY_STYLES: Record<GapSeverity, { label: string; color: string; bg: string }> = {
  high: { label: 'High', color: '#ff6b6b', bg: 'rgba(255, 107, 107, 0.14)' },
  medium: { label: 'Medium', color: '#ffd93d', bg: 'rgba(255, 217, 61, 0.14)' },
  low: { label: 'Low', color: '#0fffd1', bg: 'rgba(15, 255, 209, 0.14)' },
};

@Component({
  selector: 'app-translation-diagnostic-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  template: `
    <div class="card" *ngIf="hasValidDiagnostic; else diagnosticFallback">
      <!-- Header -->
      <div class="card__header">
        <span class="card__title">Translation Diagnostic</span>
        <span class="card__badge">Simulated</span>
      </div>

      <p class="card__subtitle">
        System language vs. Lifeworld analysis at sentiment inflection points
      </p>

      <!-- Per-stakeholder blocks -->
      <div class="gap-block" *ngFor="let gap of parsed!.gaps">
        <div class="gap-block__header">
          <span class="gap-block__name">{{ gap.stakeholder_label }}</span>
          <span
            class="gap-block__severity"
            [style.color]="severityStyle(gap.overall_gap_severity).color"
            [style.background]="severityStyle(gap.overall_gap_severity).bg"
          >{{ severityStyle(gap.overall_gap_severity).label }}</span>
        </div>

        <!-- Trigger phrases table -->
        <div class="trigger-table" *ngFor="let tp of gap.trigger_phrases">
          <div class="trigger-table__quote">
            <span class="trigger-table__tick">Sprint {{ tp.tick }}</span>
            <span class="trigger-table__phrase">"{{ tp.phrase }}"</span>
          </div>
          <div class="trigger-table__row">
            <div class="trigger-table__cell trigger-table__cell--intent">
              <span class="trigger-table__label">Leader intended</span>
              <span class="trigger-table__value">{{ tp.leader_intent }}</span>
            </div>
            <div class="trigger-table__arrow">→</div>
            <div class="trigger-table__cell trigger-table__cell--decoded">
              <span class="trigger-table__label">Stakeholder heard</span>
              <span class="trigger-table__value">{{ tp.stakeholder_decoded_as }}</span>
            </div>
          </div>
          <div class="trigger-table__gap-type">
            <span
              class="trigger-table__gap-pill"
              [style.color]="severityStyle(tp.severity).color"
              [style.borderColor]="severityStyle(tp.severity).color"
            >{{ tp.gap_type }}</span>
          </div>
        </div>

        <!-- Suggested reframing -->
        <div class="gap-block__reframing" *ngIf="gap.suggested_reframing">
          <span class="gap-block__reframing-label">↻ Suggested reframing</span>
          <p class="gap-block__reframing-text">{{ gap.suggested_reframing }}</p>
        </div>
      </div>

      <!-- Summary -->
      <div class="card__summary" *ngIf="parsed!.summary">
        <p>{{ parsed!.summary }}</p>
      </div>
    </div>
    <ng-template #diagnosticFallback>
      <div class="card" style="padding: 16px; opacity: 0.6;">Translation diagnostic data received — rendering details…</div>
    </ng-template>
  `,
  styles: [`
    :host { display: block; }

    .card {
      background: rgba(8, 16, 22, 0.85);
      border: 1px solid rgba(15, 255, 209, 0.25);
      border-radius: 12px;
      padding: 20px;
      margin: 8px 0;
      color: #e0e0e0;
      font-family: 'Google Sans', sans-serif;
    }

    .card__header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 4px;
    }

    .card__title {
      font-size: 16px;
      font-weight: 600;
      color: #ffffff;
    }

    .card__badge {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      padding: 2px 8px;
      border-radius: 4px;
      border: 1px solid #ffd93d;
      color: #ffd93d;
    }

    .card__subtitle {
      font-size: 12px;
      color: #8a9bae;
      margin: 0 0 16px 0;
    }

    /* Per-stakeholder block */
    .gap-block {
      background: rgba(255, 255, 255, 0.03);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 12px;
    }

    .gap-block__header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
    }

    .gap-block__name {
      font-size: 14px;
      font-weight: 600;
      color: #ffffff;
    }

    .gap-block__severity {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      padding: 2px 8px;
      border-radius: 4px;
    }

    /* Trigger phrase blocks */
    .trigger-table {
      margin-bottom: 12px;
      padding-bottom: 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }

    .trigger-table:last-of-type {
      border-bottom: none;
      margin-bottom: 8px;
    }

    .trigger-table__quote {
      display: flex;
      align-items: baseline;
      gap: 8px;
      margin-bottom: 8px;
    }

    .trigger-table__tick {
      font-size: 10px;
      color: #0fffd1;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      white-space: nowrap;
    }

    .trigger-table__phrase {
      font-size: 13px;
      color: #ffd93d;
      font-style: italic;
    }

    .trigger-table__row {
      display: flex;
      align-items: flex-start;
      gap: 8px;
    }

    .trigger-table__cell {
      flex: 1;
      padding: 8px 10px;
      border-radius: 6px;
    }

    .trigger-table__cell--intent {
      background: rgba(15, 255, 209, 0.06);
      border-left: 2px solid rgba(15, 255, 209, 0.3);
    }

    .trigger-table__cell--decoded {
      background: rgba(255, 107, 107, 0.06);
      border-left: 2px solid rgba(255, 107, 107, 0.3);
    }

    .trigger-table__label {
      display: block;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #8a9bae;
      margin-bottom: 4px;
    }

    .trigger-table__value {
      font-size: 12px;
      line-height: 1.4;
    }

    .trigger-table__arrow {
      color: #8a9bae;
      font-size: 18px;
      padding-top: 20px;
      flex-shrink: 0;
    }

    .trigger-table__gap-type {
      margin-top: 6px;
    }

    .trigger-table__gap-pill {
      font-size: 11px;
      padding: 2px 10px;
      border-radius: 10px;
      border: 1px solid;
    }

    /* Reframing callout */
    .gap-block__reframing {
      background: rgba(15, 255, 209, 0.04);
      border: 1px solid rgba(15, 255, 209, 0.15);
      border-radius: 6px;
      padding: 10px 12px;
      margin-top: 8px;
    }

    .gap-block__reframing-label {
      font-size: 11px;
      font-weight: 600;
      color: #0fffd1;
      display: block;
      margin-bottom: 4px;
    }

    .gap-block__reframing-text {
      font-size: 12px;
      line-height: 1.5;
      color: #c8d6e5;
      margin: 0;
    }

    /* Summary */
    .card__summary {
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      padding-top: 12px;
      margin-top: 4px;
    }

    .card__summary p {
      font-size: 12px;
      line-height: 1.5;
      color: #8a9bae;
      margin: 0;
    }
  `],
})
export class TranslationDiagnosticCardComponent {
  parsed: DiagnosticPayload | null = null;

  @Input() set payload(value: unknown) {
    this.parsed = this.parse(value);
  }

  get hasValidDiagnostic(): boolean {
    return this.parsed !== null && this.parsed.gaps.length > 0;
  }

  severityStyle(severity: GapSeverity): { label: string; color: string; bg: string } {
    return SEVERITY_STYLES[String(severity || '').trim().toLowerCase() as GapSeverity] ?? SEVERITY_STYLES['medium'];
  }

  // ---------------------------------------------------------------------------
  // Permissive parser (same resilience pattern as simulation-report-card)
  // ---------------------------------------------------------------------------

  private parse(value: unknown): DiagnosticPayload | null {
    if (!value || typeof value !== 'object') return null;
    const obj = value as Record<string, unknown>;

    // Accept either the full tool_end envelope or bare payload.
    const inner = (obj['translation_diagnostic'] as Record<string, unknown> | undefined) ?? obj;

    const gapsRaw = inner['gaps'];
    if (!Array.isArray(gapsRaw) || gapsRaw.length === 0) return null;

    const gaps: StakeholderGap[] = [];
    for (const raw of gapsRaw) {
      if (!raw || typeof raw !== 'object') continue;
      const r = raw as Record<string, unknown>;
      const triggersRaw = r['trigger_phrases'];
      if (!Array.isArray(triggersRaw)) continue;

      const triggers: TriggerPhrase[] = [];
      for (const tRaw of triggersRaw) {
        if (!tRaw || typeof tRaw !== 'object') continue;
        const t = tRaw as Record<string, unknown>;
        triggers.push({
          phrase: String(t['phrase'] || ''),
          tick: Number(t['tick'] || 0),
          leader_intent: String(t['leader_intent'] || ''),
          stakeholder_decoded_as: String(t['stakeholder_decoded_as'] || ''),
          gap_type: String(t['gap_type'] || ''),
          severity: String(t['severity'] || 'medium').trim().toLowerCase() as GapSeverity,
        });
      }

      if (triggers.length === 0) continue;

      gaps.push({
        stakeholder_id: String(r['stakeholder_id'] || ''),
        stakeholder_label: String(r['stakeholder_label'] || r['stakeholder_id'] || '').replace(/_/g, ' '),
        trigger_phrases: triggers,
        suggested_reframing: String(r['suggested_reframing'] || ''),
        overall_gap_severity: String(r['overall_gap_severity'] || 'medium').trim().toLowerCase() as GapSeverity,
      });
    }

    if (gaps.length === 0) return null;

    return {
      gaps,
      summary: String(inner['summary'] || ''),
      inferred: inner['inferred'] !== false,
    };
  }
}
