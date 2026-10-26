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
 * Renders the Artifact-2 simulation timeline emitted by the
 * `emit_simulation_timeline` tool of `leadership_planner_with_memory`.
 *
 * Surface: a vertically-stacked sequence of tick blocks. Each block
 * has the sprint header (number + optional label + headline + events
 * as chips) followed by a per-persona row showing stance dot,
 * sentiment bar, optional note, and an arrow indicating whether
 * stance/sentiment moved relative to the previous tick.
 *
 * Resilience: parses permissively (full tool_end envelope OR bare
 * `{ticks}` payload). If the payload is empty or malformed, renders
 * nothing.
 */

import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

type Stance = 'supportive' | 'neutral' | 'resistant';

interface TickState {
  stakeholder_id: string;
  stance: Stance;
  sentiment: number;
  note?: string;
  inferred: boolean;
}

interface Tick {
  tick: number;
  sprint_label?: string;
  headline: string;
  events: string[];
  states: TickState[];
}

interface TimelinePayload {
  ticks: Tick[];
  rationale?: string;
}

const STANCE_COLORS: Record<Stance, { dot: string; label: string }> = {
  supportive: { dot: '#0fffd1', label: 'Supportive' },
  neutral: { dot: '#ffd93d', label: 'Neutral' },
  resistant: { dot: '#ff6b6b', label: 'Resistant' },
};

const STANCE_ORDER: Record<Stance, number> = {
  supportive: 2,
  neutral: 1,
  resistant: 0,
};

interface PersonaRow {
  id: string;
  cells: Array<{
    state: TickState;
    stanceShift: 'up' | 'down' | 'flat';
    sentimentShift: 'up' | 'down' | 'flat';
  }>;
}

@Component({
  selector: 'app-simulation-timeline-card',
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
      .card-header .count {
        color: rgba(15, 255, 209, 0.65);
        font-variant-numeric: tabular-nums;
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
      .rationale {
        font-size: 11px;
        line-height: 1.35;
        color: rgba(220, 230, 235, 0.7);
        margin: 0 2px 8px 2px;
        font-style: italic;
      }
      .tick-block {
        padding: 8px 10px 9px 10px;
        border-radius: 6px;
        background: rgba(15, 255, 209, 0.04);
        border-left: 3px solid rgba(15, 255, 209, 0.55);
        margin-top: 6px;
      }
      .tick-block:first-of-type {
        margin-top: 0;
      }
      .tick-head {
        display: flex;
        align-items: baseline;
        gap: 8px;
        margin-bottom: 4px;
      }
      .tick-num {
        font-size: 11px;
        font-weight: 700;
        color: #0fffd1;
        font-variant-numeric: tabular-nums;
        letter-spacing: 0.04em;
      }
      .sprint-label {
        font-size: 10px;
        color: rgba(220, 230, 235, 0.65);
        letter-spacing: 0.04em;
      }
      .headline {
        font-size: 11px;
        line-height: 1.35;
        color: #f0f6f4;
        margin-bottom: 6px;
      }
      .events {
        display: flex;
        flex-wrap: wrap;
        gap: 3px;
        margin-bottom: 6px;
      }
      .event-chip {
        font-size: 9px;
        line-height: 1;
        padding: 2px 6px;
        border-radius: 9px;
        background: rgba(220, 230, 235, 0.06);
        color: rgba(220, 230, 235, 0.78);
        border: 1px solid rgba(220, 230, 235, 0.14);
      }
      .states {
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        gap: 3px;
      }
      .state-row {
        display: grid;
        grid-template-columns: 90px 14px 80px 1fr;
        align-items: center;
        gap: 6px;
        font-size: 10px;
        color: rgba(220, 230, 235, 0.85);
      }
      .stakeholder {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        color: rgba(240, 246, 244, 0.92);
        font-weight: 500;
      }
      .stance-cell {
        display: flex;
        align-items: center;
        gap: 4px;
        justify-content: center;
      }
      .stance-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex: 0 0 auto;
      }
      .shift {
        font-size: 9px;
        line-height: 1;
        color: rgba(220, 230, 235, 0.55);
      }
      .shift.up {
        color: #0fffd1;
      }
      .shift.down {
        color: #ff6b6b;
      }
      .sentiment-wrap {
        position: relative;
        height: 5px;
        background: rgba(255, 255, 255, 0.08);
        border-radius: 2px;
        overflow: hidden;
      }
      .sentiment-mid {
        position: absolute;
        top: 0;
        bottom: 0;
        left: 50%;
        width: 1px;
        background: rgba(255, 255, 255, 0.25);
      }
      .sentiment-fill {
        position: absolute;
        top: 0;
        bottom: 0;
      }
      .note {
        font-size: 10px;
        color: rgba(220, 230, 235, 0.6);
        font-style: italic;
        line-height: 1.3;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .legend {
        margin-top: 8px;
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        font-size: 9px;
        color: rgba(220, 230, 235, 0.55);
      }
      .legend-dot {
        display: inline-block;
        width: 7px;
        height: 7px;
        border-radius: 50%;
        margin-right: 4px;
        vertical-align: middle;
      }
    `,
  ],
  template: `
    <div class="card" *ngIf="hasValidTimeline; else timelineFallback">
      <div class="card-header">
        <span>Simulation Timeline <span style="opacity:.55">· {{ payload!.ticks.length }} sprint(s)</span></span>
        <span style="display:flex;gap:6px;align-items:center">
          <span class="inferred-tag" title="Projected simulation; not observed history">Simulated</span>
        </span>
      </div>

      <div class="rationale" *ngIf="payload!.rationale">{{ payload!.rationale }}</div>

      <div *ngFor="let tick of payload!.ticks; let tIdx = index" class="tick-block">
        <div class="tick-head">
          <span class="tick-num">Sprint {{ tick.tick }}</span>
          <span class="sprint-label" *ngIf="tick.sprint_label">{{ tick.sprint_label }}</span>
        </div>
        <div class="headline">{{ tick.headline }}</div>
        <div class="events" *ngIf="tick.events.length > 0">
          <span class="event-chip" *ngFor="let e of tick.events">{{ e }}</span>
        </div>
        <div class="states">
          <div *ngFor="let s of tick.states" class="state-row">
            <span class="stakeholder" [title]="resolveLabel(s.stakeholder_id)">{{ resolveLabel(s.stakeholder_id) }}</span>
            <span class="stance-cell" [title]="(stanceColors[s.stance] || {label: 'Unknown'}).label">
              <span class="stance-dot" [style.background]="(stanceColors[s.stance] || {dot: '#6b7280'}).dot"></span>
              <span class="shift" [class.up]="getStanceShift(tIdx, s.stakeholder_id) === 'up'" [class.down]="getStanceShift(tIdx, s.stakeholder_id) === 'down'">{{ shiftArrow(getStanceShift(tIdx, s.stakeholder_id)) }}</span>
            </span>
            <span class="sentiment-wrap" [title]="'Sentiment ' + formatSentiment(s.sentiment)">
              <span class="sentiment-mid"></span>
              <span
                class="sentiment-fill"
                [style.left.%]="sentimentFillLeft(s.sentiment)"
                [style.width.%]="sentimentFillWidth(s.sentiment)"
                [style.background]="sentimentFillColor(s.sentiment)"
              ></span>
            </span>
            <span class="note" *ngIf="s.note; else noNote">{{ s.note }}</span>
            <ng-template #noNote><span class="note" style="opacity:0">·</span></ng-template>
          </div>
        </div>
      </div>

      <div class="legend">
        <span><span class="legend-dot" style="background:#0fffd1"></span>Supportive</span>
        <span><span class="legend-dot" style="background:#ffd93d"></span>Neutral</span>
        <span><span class="legend-dot" style="background:#ff6b6b"></span>Resistant</span>
        <span style="opacity:.55">↑ = stance hardened toward support · ↓ = toward resistance</span>
      </div>
    </div>
    <ng-template #timelineFallback>
      <div class="card card--fallback" *ngIf="receivedPayload" style="padding: 10px 12px; opacity: 0.7; font-size: 12px; display: flex; align-items: center; gap: 6px;">
        <span class="material-icons" style="font-size: 16px;">timeline</span>
        <span>Simulation timeline data received</span>
      </div>
    </ng-template>
  `,
})
export class SimulationTimelineCardComponent {
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

  private _payload: TimelinePayload | null = null;
  receivedPayload = false;
  private _labels: Record<string, string> = {};

  readonly stanceColors = STANCE_COLORS;

  get payload(): TimelinePayload | null {
    return this._payload;
  }

  get hasValidTimeline(): boolean {
    return !!this._payload && this._payload.ticks.length > 0;
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

  formatSentiment(v: number): string {
    return v >= 0 ? `+${v.toFixed(2)}` : v.toFixed(2);
  }

  sentimentFillLeft(v: number): number {
    if (v >= 0) return 50;
    return 50 + v * 50;
  }
  sentimentFillWidth(v: number): number {
    return Math.abs(v) * 50;
  }
  sentimentFillColor(v: number): string {
    if (v < -0.1) return 'rgba(255, 107, 107, 0.85)';
    if (v > 0.1) return 'rgba(15, 255, 209, 0.85)';
    return 'rgba(255, 217, 61, 0.85)';
  }

  /** Returns the stance direction this tick moved relative to the previous tick. */
  getStanceShift(tickIdx: number, stakeholderId: string): 'up' | 'down' | 'flat' {
    if (!this._payload || tickIdx === 0) return 'flat';
    const cur = this._payload.ticks[tickIdx].states.find(
      (s) => s.stakeholder_id === stakeholderId,
    );
    const prev = this._payload.ticks[tickIdx - 1].states.find(
      (s) => s.stakeholder_id === stakeholderId,
    );
    if (!cur || !prev) return 'flat';
    const d = STANCE_ORDER[cur.stance] - STANCE_ORDER[prev.stance];
    if (d > 0) return 'up';
    if (d < 0) return 'down';
    return 'flat';
  }

  shiftArrow(s: 'up' | 'down' | 'flat'): string {
    return s === 'up' ? '↑' : s === 'down' ? '↓' : '·';
  }

  /**
   * Accept either the full tool_end `result` envelope
   * (`{status, simulation_timeline: {...}}`) or a bare
   * `{ticks, rationale?}` payload.
   */
  private parse(value: unknown): TimelinePayload | null {
    if (!value || typeof value !== 'object') return null;
    const obj = value as Record<string, unknown>;
    const inner =
      (obj['simulation_timeline'] as Record<string, unknown> | undefined) ?? obj;
    const raw = inner['ticks'];
    if (!Array.isArray(raw) || raw.length === 0) return null;

    const validStances = new Set<Stance>(['supportive', 'neutral', 'resistant']);
    const ticks: Tick[] = [];
    for (const tRaw of raw) {
      if (!tRaw || typeof tRaw !== 'object') continue;
      const t = tRaw as Record<string, unknown>;
      const tickNum = t['tick'];
      const headline = t['headline'];
      if (typeof tickNum !== 'number' || !Number.isInteger(tickNum)) continue;
      if (typeof headline !== 'string' || !headline.trim()) continue;

      const statesRaw = t['states'];
      if (!Array.isArray(statesRaw) || statesRaw.length === 0) continue;

      const states: TickState[] = [];
      for (const sRaw of statesRaw) {
        if (!sRaw || typeof sRaw !== 'object') continue;
        const s = sRaw as Record<string, unknown>;
        const sid = s['stakeholder_id'];
        const stance = s['stance'];
        const sentiment = s['sentiment'];
        if (typeof sid !== 'string' || !sid.trim()) continue;
        const lowerStance = typeof stance === 'string' ? stance.toLowerCase() : '';
        if (!validStances.has(lowerStance as Stance)) continue;
        if (typeof sentiment !== 'number' || sentiment < -1 || sentiment > 1) continue;

        const note = typeof s['note'] === 'string' && (s['note'] as string).trim()
          ? (s['note'] as string).trim()
          : undefined;
        const inferred = s['inferred'] === false ? false : true;

        states.push({
          stakeholder_id: sid.trim(),
          stance: lowerStance as Stance,
          sentiment,
          note,
          inferred,
        });
      }
      if (states.length === 0) continue;

      const sprintLabel =
        typeof t['sprint_label'] === 'string' && (t['sprint_label'] as string).trim()
          ? (t['sprint_label'] as string).trim()
          : undefined;
      const events: string[] = [];
      if (Array.isArray(t['events'])) {
        for (const e of t['events'] as unknown[]) {
          if (typeof e === 'string' && e.trim()) events.push(e.trim());
        }
      }

      ticks.push({
        tick: tickNum,
        headline: headline.trim(),
        sprint_label: sprintLabel,
        events,
        states,
      });
    }
    if (ticks.length === 0) return null;

    const rationale =
      typeof inner['rationale'] === 'string' ? (inner['rationale'] as string) : undefined;
    return { ticks, rationale };
  }
}
