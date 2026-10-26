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
 * Renders the Stakeholder Psychology card emitted by the
 * `emit_stakeholder_psychology` tool of `leadership_planner_with_eval`.
 *
 * Surface: per-stakeholder blocks showing WTT score, malicious compliance
 * risk, sentiment trajectory, and psychological quick facts.
 *
 * Resilience: parses permissively (full tool_end envelope OR bare
 * `{profiles, summary}` payload). If the payload is empty or malformed,
 * renders nothing.
 */

import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

type Risk = 'high' | 'medium' | 'low';
type Trend = 'rising' | 'flat' | 'declining';

interface WttBreakdown {
  trust_level: number;
  resistance_absence: number;
  principle_engagement: number;
  trust_integrity: number;
}

interface McSignals {
  displayed_stance: string;
  wall_severity: number;
  inner_sentiment_avg: number;
}

interface SentimentTrajectory {
  initial: number;
  final: number;
  min: number;
  max: number;
  max_delta_tick: number;
  trend: Trend;
}

interface StakeholderProfile {
  stakeholder_id: string;
  stakeholder_label: string;
  tier: number;
  persona_summary: string;
  decision_style: string;
  communication_style: string;
  risk_tolerance: string;
  domain_principles: string[];
  alignment_drivers: string[];
  resistance_triggers: string[];
  coalition_allies: string[];
  wtt_score: number;
  wtt_breakdown: WttBreakdown;
  malicious_compliance_risk: Risk;
  malicious_compliance_signals: McSignals;
  sentiment_trajectory: SentimentTrajectory;
  trust_breaches: number;
  terminal_state: string | null;
  escalation_events: string[];
}

interface PsychologyPayload {
  profiles: StakeholderProfile[];
  summary: string;
  inferred: boolean;
}

const RISK_STYLES: Record<Risk, { label: string; color: string; bg: string }> = {
  high: { label: 'High', color: '#ff6b6b', bg: 'rgba(255, 107, 107, 0.14)' },
  medium: { label: 'Medium', color: '#ffd93d', bg: 'rgba(255, 217, 61, 0.14)' },
  low: { label: 'Low', color: '#0fffd1', bg: 'rgba(15, 255, 209, 0.14)' },
};

const TREND_ICONS: Record<Trend, string> = {
  rising: '↗',
  flat: '→',
  declining: '↘',
};

@Component({
  selector: 'app-stakeholder-psychology-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  template: `
    <div class="card" *ngIf="hasValidPsychology; else psychologyFallback">
      <!-- Header -->
      <div class="card__header">
        <span class="card__title">Stakeholder Psychology</span>
        <span class="card__badge">Simulated</span>
      </div>
      <p class="card__subtitle">
        Willingness to Transform · Malicious Compliance · Trust Architecture
      </p>

      <!-- Per-stakeholder blocks -->
      <div class="profile" *ngFor="let p of parsed!.profiles">
        <div class="profile__top">
          <!-- Name + Tier -->
          <div class="profile__identity">
            <span class="profile__name">{{ p.stakeholder_label }}</span>
            <span class="profile__tier">Tier {{ p.tier }}</span>
            <span
              class="profile__terminal"
              *ngIf="p.terminal_state"
              [style.color]="p.terminal_state === 'aligned' ? '#0fffd1' : '#ff6b6b'"
            >{{ p.terminal_state === 'aligned' ? '✓ Aligned' : '✗ Defected' }}</span>
          </div>
          <!-- WTT Score -->
          <div class="profile__wtt">
            <span class="profile__wtt-num" [style.color]="wttColor(p.wtt_score)">{{ p.wtt_score }}</span>
            <span class="profile__wtt-label">WTT</span>
          </div>
        </div>

        <!-- WTT Breakdown bars -->
        <div class="wtt-bars">
          <div class="wtt-bar" *ngFor="let bar of wttBars(p.wtt_breakdown)">
            <span class="wtt-bar__label">{{ bar.label }}</span>
            <div class="wtt-bar__track">
              <div
                class="wtt-bar__fill"
                [style.width.%]="bar.value"
                [style.background]="barColor(bar.value)"
              ></div>
            </div>
            <span class="wtt-bar__value">{{ bar.value }}</span>
          </div>
        </div>

        <!-- Malicious Compliance -->
        <div class="mc-row">
          <span class="mc-row__label">Malicious Compliance</span>
          <span
            class="mc-row__risk"
            [style.color]="riskStyle(p.malicious_compliance_risk).color"
            [style.background]="riskStyle(p.malicious_compliance_risk).bg"
          >{{ riskStyle(p.malicious_compliance_risk).label }}</span>
          <span class="mc-row__detail" *ngIf="p.malicious_compliance_risk !== 'low'">
            Stance: {{ p.malicious_compliance_signals.displayed_stance }} ·
            Wall: {{ p.malicious_compliance_signals.wall_severity }} ·
            Inner: {{ p.malicious_compliance_signals.inner_sentiment_avg }}
          </span>
        </div>

        <!-- Sentiment Trajectory -->
        <div class="trajectory">
          <span class="trajectory__label">Trust Trajectory</span>
          <span class="trajectory__values">
            {{ p.sentiment_trajectory.initial | number:'1.2-2' }}
            <span class="trajectory__arrow" [style.color]="trendColor(p.sentiment_trajectory.trend)">
              {{ trendIcon(p.sentiment_trajectory.trend) }}
            </span>
            {{ p.sentiment_trajectory.final | number:'1.2-2' }}
          </span>
          <span class="trajectory__breach" *ngIf="p.trust_breaches > 0">
            {{ p.trust_breaches }} breach{{ p.trust_breaches > 1 ? 'es' : '' }}
          </span>
        </div>

        <!-- Psychology Quick Facts -->
        <div class="facts">
          <span class="facts__item">{{ p.decision_style }}</span>
          <span class="facts__sep">·</span>
          <span class="facts__item">{{ p.risk_tolerance }} risk</span>
          <span class="facts__sep" *ngIf="p.coalition_allies.length > 0">·</span>
          <span class="facts__item" *ngIf="p.coalition_allies.length > 0">
            Allies: {{ p.coalition_allies.join(', ') }}
          </span>
        </div>

        <!-- Escalation events -->
        <div class="escalations" *ngIf="p.escalation_events.length > 0">
          <span class="escalations__item" *ngFor="let e of p.escalation_events">⚠ {{ e }}</span>
        </div>
      </div>

      <!-- Summary -->
      <div class="card__summary" *ngIf="parsed!.summary">
        <p>{{ parsed!.summary }}</p>
      </div>
    </div>
    <ng-template #psychologyFallback>
      <div class="card" style="padding: 16px; opacity: 0.6;">Stakeholder psychology data received — rendering details…</div>
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
    .card__header { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
    .card__title { font-size: 16px; font-weight: 600; color: #fff; }
    .card__badge {
      font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;
      padding: 2px 8px; border-radius: 4px; border: 1px solid #ffd93d; color: #ffd93d;
    }
    .card__subtitle { font-size: 12px; color: #8a9bae; margin: 0 0 16px 0; }

    /* Profile block */
    .profile {
      border: 1px solid rgba(255,255,255,0.08); border-radius: 8px;
      padding: 14px; margin-bottom: 12px; background: rgba(255,255,255,0.02);
    }
    .profile__top { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 10px; }
    .profile__identity { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .profile__name { font-size: 14px; font-weight: 600; color: #fff; }
    .profile__tier {
      font-size: 10px; color: #8a9bae; text-transform: uppercase; letter-spacing: 0.5px;
      padding: 1px 6px; border: 1px solid rgba(255,255,255,0.15); border-radius: 3px;
    }
    .profile__terminal { font-size: 11px; font-weight: 600; }
    .profile__wtt { text-align: center; }
    .profile__wtt-num { font-size: 28px; font-weight: 700; display: block; line-height: 1; }
    .profile__wtt-label { font-size: 10px; color: #8a9bae; text-transform: uppercase; letter-spacing: 0.5px; }

    /* WTT breakdown bars */
    .wtt-bars { margin-bottom: 10px; }
    .wtt-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
    .wtt-bar__label { font-size: 10px; color: #8a9bae; width: 110px; text-align: right; flex-shrink: 0; }
    .wtt-bar__track {
      flex: 1; height: 6px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden;
    }
    .wtt-bar__fill { height: 100%; border-radius: 3px; transition: width 0.3s ease; }
    .wtt-bar__value { font-size: 10px; color: #8a9bae; width: 24px; text-align: right; flex-shrink: 0; }

    /* Malicious compliance row */
    .mc-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
    .mc-row__label { font-size: 11px; color: #8a9bae; }
    .mc-row__risk {
      font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;
      padding: 2px 8px; border-radius: 4px;
    }
    .mc-row__detail { font-size: 10px; color: #6b7d8e; }

    /* Trajectory */
    .trajectory { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .trajectory__label { font-size: 11px; color: #8a9bae; }
    .trajectory__values { font-size: 12px; font-family: 'Roboto Mono', monospace; color: #c8d6e5; }
    .trajectory__arrow { font-size: 16px; font-weight: 600; }
    .trajectory__breach {
      font-size: 10px; color: #ff6b6b; padding: 1px 6px;
      border: 1px solid rgba(255,107,107,0.3); border-radius: 3px;
    }

    /* Quick facts */
    .facts { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
    .facts__item { font-size: 11px; color: #8a9bae; text-transform: capitalize; }
    .facts__sep { font-size: 11px; color: #4a5568; }

    /* Escalation events */
    .escalations { margin-top: 6px; }
    .escalations__item {
      display: block; font-size: 11px; color: #ffd93d; margin-bottom: 2px;
    }

    /* Summary */
    .card__summary { border-top: 1px solid rgba(255,255,255,0.08); padding-top: 12px; margin-top: 4px; }
    .card__summary p { font-size: 12px; line-height: 1.5; color: #8a9bae; margin: 0; }
  `],
})
export class StakeholderPsychologyCardComponent {
  parsed: PsychologyPayload | null = null;

  @Input() set payload(value: unknown) {
    this.parsed = this.parse(value);
  }

  get hasValidPsychology(): boolean {
    return this.parsed !== null && this.parsed.profiles.length > 0;
  }

  wttColor(score: number): string {
    if (score >= 70) return '#0fffd1';
    if (score >= 40) return '#ffd93d';
    return '#ff6b6b';
  }

  barColor(value: number): string {
    if (value >= 70) return '#0fffd1';
    if (value >= 40) return '#ffd93d';
    return '#ff6b6b';
  }

  riskStyle(risk: Risk): { label: string; color: string; bg: string } {
    return RISK_STYLES[String(risk || '').trim().toLowerCase() as Risk] ?? RISK_STYLES['medium'];
  }

  trendIcon(trend: Trend): string {
    return TREND_ICONS[trend] ?? '→';
  }

  trendColor(trend: Trend): string {
    if (trend === 'rising') return '#0fffd1';
    if (trend === 'declining') return '#ff6b6b';
    return '#8a9bae';
  }

  wttBars(breakdown: WttBreakdown): { label: string; value: number }[] {
    return [
      { label: 'Trust Level', value: breakdown?.trust_level ?? 0 },
      { label: 'Low Resistance', value: breakdown?.resistance_absence ?? 0 },
      { label: 'Engagement', value: breakdown?.principle_engagement ?? 0 },
      { label: 'Trust Integrity', value: breakdown?.trust_integrity ?? 0 },
    ];
  }

  // ---------------------------------------------------------------------------
  // Permissive parser
  // ---------------------------------------------------------------------------

  private parse(value: unknown): PsychologyPayload | null {
    if (!value || typeof value !== 'object') return null;
    const obj = value as Record<string, unknown>;
    const inner = (obj['stakeholder_psychology'] as Record<string, unknown> | undefined) ?? obj;
    const profilesRaw = inner['profiles'];
    if (!Array.isArray(profilesRaw) || profilesRaw.length === 0) return null;

    const profiles: StakeholderProfile[] = [];
    for (const raw of profilesRaw) {
      if (!raw || typeof raw !== 'object') continue;
      const r = raw as Record<string, unknown>;

      const wttRaw = (r['wtt_breakdown'] ?? {}) as Record<string, unknown>;
      const mcRaw = (r['malicious_compliance_signals'] ?? {}) as Record<string, unknown>;
      const trajRaw = (r['sentiment_trajectory'] ?? {}) as Record<string, unknown>;

      profiles.push({
        stakeholder_id: String(r['stakeholder_id'] || ''),
        stakeholder_label: String(r['stakeholder_label'] || r['stakeholder_id'] || '').replace(/_/g, ' '),
        tier: Number(r['tier'] || 0),
        persona_summary: String(r['persona_summary'] || ''),
        decision_style: String(r['decision_style'] || 'unknown'),
        communication_style: String(r['communication_style'] || 'unknown'),
        risk_tolerance: String(r['risk_tolerance'] || 'unknown'),
        domain_principles: Array.isArray(r['domain_principles']) ? r['domain_principles'].map(String) : [],
        alignment_drivers: Array.isArray(r['alignment_drivers']) ? r['alignment_drivers'].map(String) : [],
        resistance_triggers: Array.isArray(r['resistance_triggers']) ? r['resistance_triggers'].map(String) : [],
        coalition_allies: Array.isArray(r['coalition_allies']) ? r['coalition_allies'].map(String) : [],
        wtt_score: Number(r['wtt_score'] || 0),
        wtt_breakdown: {
          trust_level: Number(wttRaw['trust_level'] || 0),
          resistance_absence: Number(wttRaw['resistance_absence'] || 0),
          principle_engagement: Number(wttRaw['principle_engagement'] || 0),
          trust_integrity: Number(wttRaw['trust_integrity'] || 0),
        },
        malicious_compliance_risk: String(r['malicious_compliance_risk'] || 'low').trim().toLowerCase() as Risk,
        malicious_compliance_signals: {
          displayed_stance: String(mcRaw['displayed_stance'] || ''),
          wall_severity: Number(mcRaw['wall_severity'] || 0),
          inner_sentiment_avg: Number(mcRaw['inner_sentiment_avg'] || 0),
        },
        sentiment_trajectory: {
          initial: Number(trajRaw['initial'] || 0),
          final: Number(trajRaw['final'] || 0),
          min: Number(trajRaw['min'] || 0),
          max: Number(trajRaw['max'] || 0),
          max_delta_tick: Number(trajRaw['max_delta_tick'] || 0),
          trend: String(trajRaw['trend'] || 'flat') as Trend,
        },
        trust_breaches: Number(r['trust_breaches'] || 0),
        terminal_state: r['terminal_state'] ? String(r['terminal_state']) : null,
        escalation_events: Array.isArray(r['escalation_events']) ? r['escalation_events'].map(String) : [],
      });
    }

    if (profiles.length === 0) return null;

    return {
      profiles,
      summary: String(inner['summary'] || ''),
      inferred: inner['inferred'] !== false,
    };
  }
}
