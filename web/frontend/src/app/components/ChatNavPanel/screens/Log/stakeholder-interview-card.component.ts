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
 * Renders one Artifact-4 stakeholder interview emitted by the
 * `emit_stakeholder_interview` tool of `leadership_planner_with_memory`.
 *
 * Surface: a single per-persona card. Header = persona name + final-
 * stance dot + tone chip. Body = topic line, then a stack of Q→A
 * exchanges (with optional follow-up nub), then optional pull-quote
 * blocks, then optional summary footer.
 *
 * The agent may call the tool multiple times (one per persona); each
 * call appends a NEW card so the user sees the full interview series.
 *
 * Resilience: parses permissively (full tool_end envelope OR bare
 * interview payload). If the payload is empty or malformed, renders
 * nothing.
 */

import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

type Stance = 'supportive' | 'neutral' | 'resistant';
type Tone = 'candid' | 'guarded' | 'hostile' | 'enthusiastic' | 'reflective';

interface Exchange {
  question: string;
  answer: string;
  follow_up?: string;
}

/**
 * A single quoted fact from the accepted `/memory` graph attached to
 * this interview via a preceding `retrieve_document_passages / get_selected_scenarios` call.
 * Rendered as a numbered footnote in the interview's "Sources"
 * strip so the user can see which curated belief / friction shaped
 * this persona's answers.
 */
interface Citation {
  quote: string;
  source_kind: 'belief' | 'friction' | 'outcome' | 'shared_learning';
  source_id: string;
}

interface InterviewPayload {
  stakeholder_id: string;
  topic: string;
  tone: Tone;
  final_stance: Stance;
  exchanges: Exchange[];
  key_quotes: string[];
  citations: Citation[];
  summary?: string;
  rationale?: string;
  inferred: boolean;
}

const STANCE_COLORS: Record<Stance, { dot: string; label: string }> = {
  supportive: { dot: '#0fffd1', label: 'Supportive' },
  neutral: { dot: '#ffd93d', label: 'Neutral' },
  resistant: { dot: '#ff6b6b', label: 'Resistant' },
};

const TONE_STYLES: Record<Tone, { label: string; color: string; bg: string }> = {
  candid: { label: 'Candid', color: '#0fffd1', bg: 'rgba(15, 255, 209, 0.14)' },
  guarded: { label: 'Guarded', color: '#ffd93d', bg: 'rgba(255, 217, 61, 0.14)' },
  hostile: { label: 'Hostile', color: '#ff6b6b', bg: 'rgba(255, 107, 107, 0.14)' },
  enthusiastic: {
    label: 'Enthusiastic',
    color: '#5ee8ff',
    bg: 'rgba(94, 232, 255, 0.14)',
  },
  reflective: {
    label: 'Reflective',
    color: '#c39bff',
    bg: 'rgba(195, 155, 255, 0.14)',
  },
};

@Component({
  selector: 'app-stakeholder-interview-card',
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
        margin-bottom: 6px;
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
      .persona-row {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 4px;
      }
      .stance-dot {
        display: inline-block;
        width: 9px;
        height: 9px;
        border-radius: 50%;
        flex: 0 0 auto;
      }
      .persona-name {
        font-size: 13px;
        font-weight: 600;
        color: #f0f6f4;
        flex: 1;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .tone-chip {
        font-size: 9px;
        line-height: 1;
        padding: 3px 7px;
        border-radius: 9px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        white-space: nowrap;
        flex: 0 0 auto;
      }
      .topic {
        font-size: 11px;
        line-height: 1.35;
        color: rgba(220, 230, 235, 0.78);
        font-style: italic;
        margin-bottom: 10px;
      }
      .exchanges {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .exchange {
        display: flex;
        flex-direction: column;
        gap: 3px;
      }
      .turn {
        display: grid;
        grid-template-columns: 22px 1fr;
        gap: 6px;
        align-items: start;
      }
      .turn-label {
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 0.08em;
        color: rgba(15, 255, 209, 0.75);
        padding-top: 1px;
      }
      .turn-label.a {
        color: rgba(240, 246, 244, 0.55);
      }
      .turn-label.f {
        color: rgba(195, 155, 255, 0.65);
      }
      .turn-body {
        font-size: 11px;
        line-height: 1.45;
      }
      .turn.q .turn-body {
        color: rgba(240, 246, 244, 0.92);
        font-weight: 500;
      }
      .turn.a .turn-body {
        color: rgba(220, 230, 235, 0.85);
      }
      .turn.f .turn-body {
        color: rgba(195, 155, 255, 0.85);
        font-style: italic;
        font-size: 10px;
        line-height: 1.35;
      }
      .quotes {
        margin-top: 12px;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .quote {
        position: relative;
        padding: 6px 10px 6px 18px;
        background: rgba(15, 255, 209, 0.06);
        border-left: 3px solid #0fffd1;
        border-radius: 0 4px 4px 0;
        font-size: 11px;
        line-height: 1.4;
        color: #f0f6f4;
        font-style: italic;
      }
      .quote::before {
        content: '\\201C';
        position: absolute;
        left: 4px;
        top: 0;
        font-size: 18px;
        color: rgba(15, 255, 209, 0.55);
        line-height: 1;
      }
      .summary {
        margin-top: 12px;
        padding: 8px 10px;
        background: rgba(220, 230, 235, 0.04);
        border-radius: 4px;
        font-size: 11px;
        line-height: 1.4;
        color: rgba(220, 230, 235, 0.85);
      }
      .summary-label {
        font-size: 9px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: rgba(15, 255, 209, 0.7);
        margin-bottom: 3px;
      }
      .rationale {
        font-size: 10px;
        line-height: 1.35;
        color: rgba(220, 230, 235, 0.55);
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
    <div
      class="card"
      *ngIf="hasValidInterview; else interviewFallback"
      [attr.id]="'interview-card-' + payload!.stakeholder_id"
      [attr.data-interview-stakeholder-id]="payload!.stakeholder_id"
    >
      <div class="card-header">
        <span>Stakeholder Interview</span>
        <span class="inferred-tag" title="Synthetic dialogue; not observed history">Simulated</span>
      </div>

      <div class="persona-row">
        <span
          class="stance-dot"
          [style.background]="stanceDot(payload!.final_stance)"
          [title]="'Final stance: ' + stanceLabel(payload!.final_stance)"
        ></span>
        <span class="persona-name" [title]="resolveLabel(payload!.stakeholder_id)">
          {{ resolveLabel(payload!.stakeholder_id) }}
        </span>
        <span
          class="tone-chip"
          [style.color]="toneStyle(payload!.tone).color"
          [style.background]="toneStyle(payload!.tone).bg"
          [style.border]="'1px solid ' + toneStyle(payload!.tone).color"
        >{{ toneStyle(payload!.tone).label }}</span>
      </div>

      <div class="topic">{{ payload!.topic }}</div>

      <div class="exchanges">
        <div *ngFor="let x of payload!.exchanges" class="exchange">
          <div class="turn q">
            <span class="turn-label">Q</span>
            <span class="turn-body">{{ x.question }}</span>
          </div>
          <div class="turn a">
            <span class="turn-label a">A</span>
            <span class="turn-body">{{ x.answer }}</span>
          </div>
          <div class="turn f" *ngIf="x.follow_up">
            <span class="turn-label f">↳</span>
            <span class="turn-body">{{ x.follow_up }}</span>
          </div>
        </div>
      </div>

      <div class="quotes" *ngIf="payload!.key_quotes.length > 0">
        <div *ngFor="let q of payload!.key_quotes" class="quote">{{ q }}</div>
      </div>

      <div class="summary" *ngIf="payload!.summary">
        <div class="summary-label">Takeaway</div>
        <div>{{ payload!.summary }}</div>
      </div>

      <div class="rationale" *ngIf="payload!.rationale">{{ payload!.rationale }}</div>

      <!--
        §2 grounding footnote strip — mirrors the after-action
        report's Sources block. Renders when the agent grounded this
        interview in accepted /memory facts via retrieve_document_passages / get_selected_scenarios.
        Server-side cross-validation guarantees every source_id here
        maps to a real node in the /memory graph.
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
    <ng-template #interviewFallback>
      <div class="card card--fallback" *ngIf="receivedPayload" style="padding: 10px 12px; opacity: 0.7; font-size: 12px; display: flex; align-items: center; gap: 6px;">
        <span class="material-icons" style="font-size: 16px;">record_voice_over</span>
        <span>Stakeholder interview data received</span>
      </div>
    </ng-template>
  `,
})
export class StakeholderInterviewCardComponent {
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

  private _payload: InterviewPayload | null = null;
  receivedPayload = false;
  private _labels: Record<string, string> = {};

  get payload(): InterviewPayload | null {
    return this._payload;
  }

  get hasValidInterview(): boolean {
    return !!this._payload && this._payload.exchanges.length > 0;
  }

  stanceDot(s: Stance | string): string {
    return (STANCE_COLORS[s as Stance] || {dot: '#6b7280'}).dot;
  }
  stanceLabel(s: Stance | string): string {
    return (STANCE_COLORS[s as Stance] || {label: 'Unknown'}).label;
  }
  toneStyle(t: Tone | string) {
    return TONE_STYLES[t as Tone] || {color: '#9ca3af', bg: 'rgba(156,163,175,0.15)', label: t};
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
   * Accept either the full tool_end `result` envelope
   * (`{status, stakeholder_interview: {...}}`) or a bare interview payload.
   */
  private parse(value: unknown): InterviewPayload | null {
    if (!value || typeof value !== 'object') return null;
    const obj = value as Record<string, unknown>;
    const inner =
      (obj['stakeholder_interview'] as Record<string, unknown> | undefined) ?? obj;

    const sid = inner['stakeholder_id'];
    const topic = inner['topic'];
    const tone = inner['tone'];
    const finalStance = inner['final_stance'];
    const exchangesRaw = inner['exchanges'];

    if (typeof sid !== 'string' || !sid.trim()) return null;
    if (typeof topic !== 'string' || !topic.trim()) return null;

    const validTones = new Set<Tone>(['candid', 'guarded', 'hostile', 'enthusiastic', 'reflective']);
    const lowerTone = typeof tone === 'string' ? tone.toLowerCase() : '';
    if (!validTones.has(lowerTone as Tone)) return null;

    const validStances = new Set<Stance>(['supportive', 'neutral', 'resistant']);
    const lowerFinalStance = typeof finalStance === 'string' ? finalStance.toLowerCase() : '';
    if (!validStances.has(lowerFinalStance as Stance)) {
      return null;
    }
    if (!Array.isArray(exchangesRaw) || exchangesRaw.length === 0) return null;

    const exchanges: Exchange[] = [];
    for (const xRaw of exchangesRaw) {
      if (!xRaw || typeof xRaw !== 'object') continue;
      const x = xRaw as Record<string, unknown>;
      const q = x['question'];
      const a = x['answer'];
      if (typeof q !== 'string' || !q.trim()) continue;
      if (typeof a !== 'string' || !a.trim()) continue;
      const fu = x['follow_up'];
      const followUp =
        typeof fu === 'string' && fu.trim() ? fu.trim() : undefined;
      exchanges.push({
        question: q.trim(),
        answer: a.trim(),
        follow_up: followUp,
      });
    }
    if (exchanges.length === 0) return null;

    const quotes: string[] = [];
    const quotesRaw = inner['key_quotes'];
    if (Array.isArray(quotesRaw)) {
      for (const q of quotesRaw) {
        if (typeof q === 'string' && q.trim()) quotes.push(q.trim());
      }
    }

    const summary =
      typeof inner['summary'] === 'string' && (inner['summary'] as string).trim()
        ? (inner['summary'] as string).trim()
        : undefined;
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
      stakeholder_id: sid.trim(),
      topic: topic.trim(),
      tone: lowerTone as Tone,
      final_stance: lowerFinalStance as Stance,
      exchanges,
      key_quotes: quotes,
      citations,
      summary,
      rationale,
      inferred,
    };
  }
}
