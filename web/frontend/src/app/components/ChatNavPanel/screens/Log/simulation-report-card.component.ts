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
 * Renders the Artifact-3 after-action report emitted by the
 * `emit_simulation_report` tool of `leadership_planner_with_memory`.
 *
 * Surface: a single rich card. Top strip = headline + outcome pill.
 * Below that: executive summary paragraph, numbered key findings
 * (severity-colored), optional per-stakeholder journey strip with
 * final-stance dot, and optional priority-colored recommendations.
 *
 * Resilience: parses permissively (full tool_end envelope OR bare
 * `{headline, outcome, executive_summary, key_findings, ...}`
 * payload). If the payload is empty or malformed, renders nothing.
 */

import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

type Stance = 'supportive' | 'neutral' | 'resistant';
type Outcome = 'success' | 'partial' | 'contested' | 'failed';
type Severity = 'high' | 'medium' | 'low';
type Priority = 'p0' | 'p1' | 'p2';

interface Finding {
  title: string;
  body: string;
  severity: Severity;
}

interface StakeholderSummary {
  stakeholder_id: string;
  final_stance: Stance;
  journey: string;
}

interface Recommendation {
  title: string;
  body: string;
  priority: Priority;
  /**
   * Optional persona id that links this recommendation to a
   * stakeholder interview card (Artifact 4) rendered later in the
   * same chat thread. When set, the row renders a "▶ Interview"
   * button that scrolls the user to the matching interview card.
   * Validated server-side against the persona id set; reaching the
   * frontend means the target id exists.
   */
  interview_with?: string;
}

/**
 * A single quoted fact drawn from the accepted `/memory` graph via
 * the `retrieve_document_passages / get_selected_scenarios` tool. Rendered as a numbered footnote
 * in the "Sources" strip at the bottom of the report so the user
 * can see which curated belief / friction / outcome the agent
 * grounded a specific finding in.
 */
interface Citation {
  quote: string;
  source_kind: 'belief' | 'friction' | 'outcome' | 'shared_learning';
  source_id: string;
}

interface ReportPayload {
  headline: string;
  outcome: Outcome;
  executive_summary: string;
  key_findings: Finding[];
  stakeholder_summaries: StakeholderSummary[];
  recommendations: Recommendation[];
  citations: Citation[];
  rationale?: string;
  inferred: boolean;
}

const OUTCOME_STYLES: Record<Outcome, { label: string; color: string; bg: string }> = {
  success: { label: 'Success', color: '#0fffd1', bg: 'rgba(15, 255, 209, 0.14)' },
  partial: { label: 'Partial Win', color: '#ffd93d', bg: 'rgba(255, 217, 61, 0.14)' },
  contested: { label: 'Contested', color: '#ff9f43', bg: 'rgba(255, 159, 67, 0.14)' },
  failed: { label: 'Failed', color: '#ff6b6b', bg: 'rgba(255, 107, 107, 0.14)' },
};

const SEVERITY_STYLES: Record<Severity, { label: string; color: string; bg: string }> = {
  high: { label: 'High', color: '#ff6b6b', bg: 'rgba(255, 107, 107, 0.14)' },
  medium: { label: 'Medium', color: '#ffd93d', bg: 'rgba(255, 217, 61, 0.14)' },
  low: { label: 'Low', color: '#0fffd1', bg: 'rgba(15, 255, 209, 0.14)' },
};

const PRIORITY_STYLES: Record<Priority, { label: string; color: string; bg: string }> = {
  p0: { label: 'P0 · Pre-kickoff', color: '#ff6b6b', bg: 'rgba(255, 107, 107, 0.14)' },
  p1: { label: 'P1 · Sprint 1', color: '#ffd93d', bg: 'rgba(255, 217, 61, 0.14)' },
  p2: { label: 'P2 · Nice-to-have', color: '#0fffd1', bg: 'rgba(15, 255, 209, 0.14)' },
};

const STANCE_COLORS: Record<Stance, { dot: string; label: string }> = {
  supportive: { dot: '#0fffd1', label: 'Supportive' },
  neutral: { dot: '#ffd93d', label: 'Neutral' },
  resistant: { dot: '#ff6b6b', label: 'Resistant' },
};

@Component({
  selector: 'app-simulation-report-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  styles: [
    `
      :host {
        display: block;
        margin: 6px 0 4px 0;
      }
      .card {
        background: rgba(8, 16, 22, 0.85);
        border: 1px solid rgba(15, 255, 209, 0.3);
        border-radius: 8px;
        padding: 10px 12px 12px 12px;
        box-shadow: 0 0 14px rgba(15, 255, 209, 0.08);
      }
      .card-header {
        font-size: 11px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #0fffd1;
        margin-bottom: 8px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
      }
      .inferred-tag {
        font-size: 8px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: rgba(255, 217, 61, 0.75);
        border: 1px solid rgba(255, 217, 61, 0.4);
        padding: 1px 4px;
        border-radius: 3px;
      }
      .headline-row {
        display: flex;
        align-items: flex-start;
        gap: 8px;
        margin-bottom: 8px;
      }
      .headline {
        flex: 1;
        font-size: 13px;
        line-height: 1.3;
        color: #f0f6f4;
        font-weight: 600;
      }
      .outcome-pill {
        font-size: 9px;
        line-height: 1;
        padding: 4px 8px;
        border-radius: 10px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        flex: 0 0 auto;
      }
      .section {
        margin-top: 10px;
      }
      .section-title {
        font-size: 9px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: rgba(15, 255, 209, 0.8);
        margin-bottom: 6px;
      }
      .exec-summary {
        font-size: 11px;
        line-height: 1.45;
        color: rgba(240, 246, 244, 0.88);
      }
      .findings {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .finding {
        display: grid;
        grid-template-columns: 18px 1fr auto;
        gap: 6px;
        align-items: start;
        padding: 6px 8px;
        background: rgba(15, 255, 209, 0.04);
        border-left: 3px solid rgba(15, 255, 209, 0.55);
        border-radius: 4px;
      }
      .finding-num {
        font-size: 11px;
        font-weight: 700;
        color: #0fffd1;
        font-variant-numeric: tabular-nums;
      }
      .finding-body-wrap {
        min-width: 0;
      }
      .finding-title {
        font-size: 11px;
        font-weight: 600;
        color: #f0f6f4;
        margin-bottom: 2px;
      }
      .finding-body {
        font-size: 10px;
        line-height: 1.4;
        color: rgba(220, 230, 235, 0.78);
      }
      .severity-chip,
      .priority-chip {
        font-size: 8px;
        line-height: 1;
        padding: 3px 6px;
        border-radius: 8px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        white-space: nowrap;
        flex: 0 0 auto;
        align-self: start;
      }
      .stakeholder-strip {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .stakeholder-row {
        display: grid;
        grid-template-columns: 8px 110px 1fr;
        gap: 8px;
        align-items: center;
        font-size: 10px;
      }
      .stance-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
      }
      .stakeholder-name {
        color: rgba(240, 246, 244, 0.92);
        font-weight: 500;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .journey {
        color: rgba(220, 230, 235, 0.78);
        line-height: 1.35;
      }
      .recs {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .rec {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 6px;
        align-items: start;
        padding: 6px 8px;
        background: rgba(220, 230, 235, 0.04);
        border-radius: 4px;
        border-left: 3px solid rgba(220, 230, 235, 0.2);
      }
      .rec-title {
        font-size: 11px;
        font-weight: 600;
        color: #f0f6f4;
        margin-bottom: 2px;
      }
      .rec-body {
        font-size: 10px;
        line-height: 1.4;
        color: rgba(220, 230, 235, 0.78);
      }
      .rec-meta {
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 4px;
        flex: 0 0 auto;
      }
      .interview-btn {
        font-size: 8px;
        line-height: 1;
        padding: 3px 6px;
        border-radius: 8px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        white-space: nowrap;
        background: rgba(94, 232, 255, 0.08);
        color: #5ee8ff;
        border: 1px solid rgba(94, 232, 255, 0.55);
        cursor: pointer;
        font-family: inherit;
      }
      .interview-btn:hover {
        background: rgba(94, 232, 255, 0.18);
      }
      .interview-btn:focus-visible {
        outline: 1px solid #5ee8ff;
        outline-offset: 2px;
      }
      .rationale {
        font-size: 10px;
        line-height: 1.35;
        color: rgba(220, 230, 235, 0.6);
        margin-top: 8px;
        font-style: italic;
      }
      .citations {
        margin-top: 10px;
        padding: 8px 8px 6px 8px;
        background: linear-gradient(
          180deg,
          rgba(94, 232, 255, 0.06) 0%,
          rgba(15, 255, 209, 0.04) 100%
        );
        border: 1px solid rgba(94, 232, 255, 0.28);
        border-radius: 5px;
      }
      .citations-title {
        font-size: 9px;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: #5ee8ff;
        margin-bottom: 6px;
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .citations-title .from-memory {
        font-size: 8px;
        color: rgba(94, 232, 255, 0.7);
        font-weight: 400;
        letter-spacing: 0.08em;
      }
      .citation-row {
        display: grid;
        grid-template-columns: 18px 70px 1fr;
        gap: 6px;
        align-items: baseline;
        padding: 3px 0;
        font-size: 10px;
        line-height: 1.4;
      }
      .citation-num {
        color: #5ee8ff;
        font-weight: 700;
        font-variant-numeric: tabular-nums;
        font-size: 9px;
      }
      .citation-kind {
        font-size: 8px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        padding: 2px 5px;
        border-radius: 3px;
        font-weight: 700;
        text-align: center;
        white-space: nowrap;
      }
      .citation-kind.kind-belief {
        color: #0fffd1;
        background: rgba(15, 255, 209, 0.12);
        border: 1px solid rgba(15, 255, 209, 0.5);
      }
      .citation-kind.kind-friction {
        color: #ff9f43;
        background: rgba(255, 159, 67, 0.12);
        border: 1px solid rgba(255, 159, 67, 0.5);
      }
      .citation-kind.kind-outcome {
        color: #ffd93d;
        background: rgba(255, 217, 61, 0.12);
        border: 1px solid rgba(255, 217, 61, 0.5);
      }
      .citation-kind.kind-shared_learning {
        color: #b98cff;
        background: rgba(185, 140, 255, 0.12);
        border: 1px solid rgba(185, 140, 255, 0.5);
      }
      .citation-quote {
        color: rgba(240, 246, 244, 0.88);
        font-style: italic;
      }
    `,
  ],
  template: `
    <div class="card" *ngIf="hasValidReport; else reportFallback">
      <div class="card-header">
        <span>After-Action Report</span>
        <span class="inferred-tag" title="Synthesized from simulation; not observed history">Simulated</span>
      </div>

      <div class="headline-row">
        <div class="headline">{{ payload!.headline }}</div>
        <span
          class="outcome-pill"
          [style.color]="outcomeStyle(payload!.outcome).color"
          [style.background]="outcomeStyle(payload!.outcome).bg"
          [style.border]="'1px solid ' + outcomeStyle(payload!.outcome).color"
        >{{ outcomeStyle(payload!.outcome).label }}</span>
      </div>

      <div class="section">
        <div class="section-title">Executive Summary</div>
        <div class="exec-summary">{{ payload!.executive_summary }}</div>
      </div>

      <div class="section">
        <div class="section-title">Key Findings · {{ payload!.key_findings.length }}</div>
        <div class="findings">
          <div *ngFor="let f of payload!.key_findings; let i = index" class="finding">
            <span class="finding-num">{{ i + 1 }}</span>
            <div class="finding-body-wrap">
              <div class="finding-title">{{ f.title }}</div>
              <div class="finding-body">{{ f.body }}</div>
            </div>
            <span
              class="severity-chip"
              [style.color]="severityStyle(f.severity).color"
              [style.background]="severityStyle(f.severity).bg"
              [style.border]="'1px solid ' + severityStyle(f.severity).color"
            >{{ severityStyle(f.severity).label }}</span>
          </div>
        </div>
      </div>

      <div class="section" *ngIf="payload!.stakeholder_summaries.length > 0">
        <div class="section-title">Stakeholder Journeys</div>
        <div class="stakeholder-strip">
          <div *ngFor="let s of payload!.stakeholder_summaries" class="stakeholder-row">
            <span class="stance-dot" [style.background]="stanceDot(s.final_stance)" [title]="stanceLabel(s.final_stance)"></span>
            <span class="stakeholder-name" [title]="resolveLabel(s.stakeholder_id)">{{ resolveLabel(s.stakeholder_id) }}</span>
            <span class="journey">{{ s.journey }}</span>
          </div>
        </div>
      </div>

      <div class="section" *ngIf="payload!.recommendations.length > 0">
        <div class="section-title">Recommendations · {{ payload!.recommendations.length }}</div>
        <div class="recs">
          <div *ngFor="let r of payload!.recommendations" class="rec">
            <div>
              <div class="rec-title">{{ r.title }}</div>
              <div class="rec-body">{{ r.body }}</div>
            </div>
            <div class="rec-meta">
              <span
                class="priority-chip"
                [style.color]="priorityStyle(r.priority).color"
                [style.background]="priorityStyle(r.priority).bg"
                [style.border]="'1px solid ' + priorityStyle(r.priority).color"
              >{{ priorityStyle(r.priority).label }}</span>
              <button
                *ngIf="r.interview_with"
                type="button"
                class="interview-btn"
                (click)="jumpToInterview(r.interview_with!)"
                [title]="'Jump to ' + resolveLabel(r.interview_with) + ' interview below'"
                [attr.aria-label]="'Jump to ' + resolveLabel(r.interview_with) + ' interview'"
              >▶ Interview</button>
            </div>
          </div>
        </div>
      </div>

      <div class="rationale" *ngIf="payload!.rationale">{{ payload!.rationale }}</div>

      <!--
        §2 grounding footnote strip — renders when a preceding
        retrieve_document_passages / get_selected_scenarios call surfaced citable /memory facts and
        the agent quoted them via the report's "citations" field.
        Server-side cross-validation guarantees every source_id here
        maps to a real accepted node in the /memory graph.
      -->
      <div class="citations" *ngIf="payload!.citations.length > 0">
        <div class="citations-title">
          <span>Sources · {{ payload!.citations.length }}</span>
          <span class="from-memory">from /memory</span>
        </div>
        <div *ngFor="let c of payload!.citations; let i = index" class="citation-row">
          <span class="citation-num">[{{ i + 1 }}]</span>
          <span
            class="citation-kind"
            [ngClass]="'kind-' + c.source_kind"
            [title]="'source_id: ' + c.source_id"
          >{{ citationKindLabel(c.source_kind) }}</span>
          <span class="citation-quote">“{{ c.quote }}”</span>
        </div>
      </div>
    </div>
    <ng-template #reportFallback>
      <div class="card card--fallback" *ngIf="receivedPayload" style="padding: 10px 12px; opacity: 0.7; font-size: 12px; display: flex; align-items: center; gap: 6px;">
        <span class="material-icons" style="font-size: 16px;">assessment</span>
        <span>Simulation report data received</span>
      </div>
    </ng-template>
  `,
})
export class SimulationReportCardComponent {
  @Input() set payload(value: unknown) {
    if (typeof value === 'string') {
      try {
        value = JSON.parse(value);
      } catch (e) {}
    }
    this.receivedPayload = value != null;
    this._payload = this.parse(value);
  }

  @Input() set nodeLabels(value: Record<string, string> | undefined | null) {
    this._labels = value ?? {};
  }

  private _payload: ReportPayload | null = null;
  receivedPayload = false;
  private _labels: Record<string, string> = {};

  get payload(): ReportPayload | null {
    return this._payload;
  }

  get hasValidReport(): boolean {
    return !!this._payload && this._payload.key_findings.length > 0;
  }

  outcomeStyle(o: Outcome | string) {
    return OUTCOME_STYLES[o as Outcome] || {color: '#9ca3af', bg: 'rgba(156,163,175,0.15)', label: o};
  }
  severityStyle(s: Severity | string) {
    return SEVERITY_STYLES[s as Severity] || {color: '#9ca3af', bg: 'rgba(156,163,175,0.15)', label: s};
  }
  priorityStyle(p: Priority | string) {
    return PRIORITY_STYLES[p as Priority] || {color: '#9ca3af', bg: 'rgba(156,163,175,0.15)', label: p};
  }
  stanceDot(s: Stance | string): string {
    return (STANCE_COLORS[s as Stance] || {dot: '#6b7280'}).dot;
  }
  stanceLabel(s: Stance | string): string {
    return (STANCE_COLORS[s as Stance] || {label: 'Unknown'}).label;
  }

  citationKindLabel(kind: Citation['source_kind']): string {
    switch (kind) {
      case 'belief':
        return 'Belief';
      case 'friction':
        return 'Friction';
      case 'outcome':
        return 'Outcome';
      case 'shared_learning':
        return 'Learning';
    }
  }

  resolveLabel(id: string): string {
    const v = this._labels[id];
    if (v && v.trim()) return v;
    return id
      .split(/[_\s-]+/)
      .filter(Boolean)
      .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
      .join(' ');
  }

  /**
   * Scroll the matching stakeholder-interview card into view. The
   * interview card sets `id="interview-card-<stakeholder_id>"` on its
   * root element; we look it up by id and centre it in the viewport.
   * No-op (with a debug log) when the card has not been emitted yet
   * — the agent typically calls `emit_stakeholder_interview` AFTER
   * `emit_simulation_report`, but the recommendation buttons appear
   * as soon as the report renders, so a click before the interview
   * card lands is plausible.
   */
  jumpToInterview(stakeholderId: string): void {
    if (typeof document === 'undefined') return;
    const id = 'interview-card-' + stakeholderId;
    const el = document.getElementById(id);
    if (!el) {
      // The interview card has not been emitted yet (race) or this
      // stakeholder never got an interview pass. Silent no-op so the
      // click is harmless rather than throwing.
      console.debug('[simulation-report-card] no interview card for', stakeholderId);
      return;
    }
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  /**
   * Accept either the full tool_end `result` envelope
   * (`{status, simulation_report: {...}}`) or a bare report payload.
   */
  private parse(value: unknown): ReportPayload | null {
    if (!value || typeof value !== 'object') return null;
    const obj = value as Record<string, unknown>;
    const inner =
      (obj['simulation_report'] as Record<string, unknown> | undefined) ?? obj;

    const headline = inner['headline'];
    const outcome = inner['outcome'];
    const execSummary = inner['executive_summary'];
    const findingsRaw = inner['key_findings'];

    if (typeof headline !== 'string' || !headline.trim()) return null;
    const validOutcomes = new Set<Outcome>(['success', 'partial', 'contested', 'failed']);
    const lowerOutcome = typeof outcome === 'string' ? outcome.toLowerCase() : '';
    if (!validOutcomes.has(lowerOutcome as Outcome)) {
      return null;
    }
    if (typeof execSummary !== 'string' || !execSummary.trim()) return null;
    if (!Array.isArray(findingsRaw) || findingsRaw.length === 0) return null;

    const validSeverities = new Set<Severity>(['high', 'medium', 'low']);
    const findings: Finding[] = [];
    for (const fRaw of findingsRaw) {
      if (!fRaw || typeof fRaw !== 'object') continue;
      const f = fRaw as Record<string, unknown>;
      const title = f['title'];
      const body = f['body'];
      const severity = f['severity'];
      if (typeof title !== 'string' || !title.trim()) continue;
      if (typeof body !== 'string' || !body.trim()) continue;
      const lowerSeverity = typeof severity === 'string' ? severity.toLowerCase() : '';
      if (!validSeverities.has(lowerSeverity as Severity)) continue;
      findings.push({
        title: title.trim(),
        body: body.trim(),
        severity: lowerSeverity as Severity,
      });
    }
    if (findings.length === 0) return null;

    const validStances = new Set<Stance>(['supportive', 'neutral', 'resistant']);
    const summaries: StakeholderSummary[] = [];
    const summariesRaw = inner['stakeholder_summaries'];
    if (Array.isArray(summariesRaw)) {
      for (const sRaw of summariesRaw) {
        if (!sRaw || typeof sRaw !== 'object') continue;
        const s = sRaw as Record<string, unknown>;
        const sid = s['stakeholder_id'];
        const finalStance = s['final_stance'];
        const journey = s['journey'];
        if (typeof sid !== 'string' || !sid.trim()) continue;
        const lowerFinalStance = typeof finalStance === 'string' ? finalStance.toLowerCase() : '';
        if (!validStances.has(lowerFinalStance as Stance)) continue;
        if (typeof journey !== 'string' || !journey.trim()) continue;
        summaries.push({
          stakeholder_id: sid.trim(),
          final_stance: lowerFinalStance as Stance,
          journey: journey.trim(),
        });
      }
    }

    const validPriorities = new Set<Priority>(['p0', 'p1', 'p2']);
    const recs: Recommendation[] = [];
    const recsRaw = inner['recommendations'];
    if (Array.isArray(recsRaw)) {
      for (const rRaw of recsRaw) {
        if (!rRaw || typeof rRaw !== 'object') continue;
        const r = rRaw as Record<string, unknown>;
        const title = r['title'];
        const body = r['body'];
        const priority = r['priority'];
        if (typeof title !== 'string' || !title.trim()) continue;
        if (typeof body !== 'string' || !body.trim()) continue;
        const lowerPriority = typeof priority === 'string' ? priority.toLowerCase() : '';
        if (!validPriorities.has(lowerPriority as Priority)) continue;
        const interviewWithRaw = r['interview_with'];
        const interviewWith =
          typeof interviewWithRaw === 'string' && interviewWithRaw.trim()
            ? interviewWithRaw.trim()
            : undefined;
        recs.push({
          title: title.trim(),
          body: body.trim(),
          priority: lowerPriority as Priority,
          ...(interviewWith ? { interview_with: interviewWith } : {}),
        });
      }
    }

    const rationale =
      typeof inner['rationale'] === 'string' && (inner['rationale'] as string).trim()
        ? (inner['rationale'] as string).trim()
        : undefined;
    const inferred = inner['inferred'] === false ? false : true;

    const validCitationKinds = new Set<Citation['source_kind']>([
      'belief',
      'friction',
      'outcome',
      'shared_learning',
    ]);
    const citations: Citation[] = [];
    const citationsRaw = inner['citations'];
    if (Array.isArray(citationsRaw)) {
      for (const cRaw of citationsRaw) {
        if (!cRaw || typeof cRaw !== 'object') continue;
        const c = cRaw as Record<string, unknown>;
        const quote = c['quote'];
        const kind = c['source_kind'];
        const sid = c['source_id'];
        if (typeof quote !== 'string' || !quote.trim()) continue;
        if (
          typeof kind !== 'string' ||
          !validCitationKinds.has(kind as Citation['source_kind'])
        )
          continue;
        if (typeof sid !== 'string' || !sid.trim()) continue;
        citations.push({
          quote: quote.trim(),
          source_kind: kind as Citation['source_kind'],
          source_id: sid.trim(),
        });
      }
    }

    return {
      headline: headline.trim(),
      outcome: lowerOutcome as Outcome,
      executive_summary: execSummary.trim(),
      key_findings: findings,
      stakeholder_summaries: summaries,
      recommendations: recs,
      citations,
      rationale,
      inferred,
    };
  }
}
