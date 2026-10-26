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
 * Renders the Artifact-1 stakeholder personas emitted by the
 * `emit_stakeholder_personas` tool of `leadership_planner_with_memory`.
 *
 * Surface: a compact grid of persona tiles between the Step-0 graph
 * and the Step-0.5 reactions. Each tile shows the stakeholder's
 * persona attributes (archetype, communication style, stance,
 * sentiment bias as a colored bar, influence weight as a thicker
 * underline, response delay as a sprint pill, and primary concerns
 * as tag chips). Every persona is flagged "Simulated" when
 * `inferred: true` (the default) — these are simulation knobs, not
 * observed facts about real people.
 *
 * Resilience: parses permissively (full tool_end envelope OR bare
 * `{personas}` payload). If the payload is empty or malformed,
 * renders nothing.
 */

import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

type Archetype =
  | 'visionary'
  | 'pragmatist'
  | 'guardian'
  | 'resistor'
  | 'bridge_builder';
type CommStyle = 'data_driven' | 'narrative' | 'consensus' | 'directive';
type Stance = 'supportive' | 'neutral' | 'resistant';

interface Persona {
  stakeholder_id: string;
  archetype: Archetype;
  communication_style: CommStyle;
  stance: Stance;
  sentiment_bias: number;
  influence_weight: number;
  response_delay_sprints: number;
  primary_concerns: string[];
  inferred: boolean;
}

interface PersonasPayload {
  personas: Persona[];
  rationale?: string;
}

// Keep aligned with Python ALLOWED_ARCHETYPES.
const ARCHETYPE_COLORS: Record<Archetype, { fill: string; label: string }> = {
  visionary: { fill: '#0fffd1', label: 'Visionary' },
  pragmatist: { fill: '#80ffd2', label: 'Pragmatist' },
  guardian: { fill: '#a8a8ff', label: 'Guardian' },
  resistor: { fill: '#ff6b6b', label: 'Resistor' },
  bridge_builder: { fill: '#ffd93d', label: 'Bridge Builder' },
};

const STANCE_COLORS: Record<Stance, { dot: string; label: string }> = {
  supportive: { dot: '#0fffd1', label: 'Supportive' },
  neutral: { dot: '#ffd93d', label: 'Neutral' },
  resistant: { dot: '#ff6b6b', label: 'Resistant' },
};

const COMM_STYLE_LABELS: Record<CommStyle, string> = {
  data_driven: 'Data-Driven',
  narrative: 'Narrative',
  consensus: 'Consensus',
  directive: 'Directive',
};

@Component({
  selector: 'app-stakeholder-personas-card',
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
        border: 1px solid rgba(168, 168, 255, 0.3);
        border-radius: 8px;
        padding: 10px 12px 10px 12px;
        box-shadow: 0 0 14px rgba(168, 168, 255, 0.08);
      }
      .card-header {
        font-size: 11px;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #a8a8ff;
        margin-bottom: 8px;
        display: flex;
        justify-content: space-between;
        align-items: center;
      }
      .card-header .count {
        color: rgba(168, 168, 255, 0.7);
      }
      .rationale {
        font-size: 11px;
        line-height: 1.35;
        color: rgba(220, 230, 235, 0.7);
        margin: 0 2px 8px 2px;
        font-style: italic;
      }
      .personas-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
        gap: 6px;
      }
      .persona {
        position: relative;
        padding: 8px 10px 9px 10px;
        border-radius: 6px;
        background: rgba(168, 168, 255, 0.05);
        border-left: 3px solid;
      }
      .persona-head {
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 4px;
      }
      .stakeholder {
        font-size: 11px;
        font-weight: 600;
        color: #f0f6f4;
        letter-spacing: 0.02em;
      }
      .archetype-badge {
        font-size: 9px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        padding: 1px 6px;
        border-radius: 3px;
        color: rgba(8, 16, 22, 0.92);
        font-weight: 600;
      }
      .inferred-tag {
        margin-left: auto;
        font-size: 8px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: rgba(255, 217, 61, 0.75);
        border: 1px solid rgba(255, 217, 61, 0.4);
        padding: 1px 4px;
        border-radius: 3px;
      }
      .row {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-top: 4px;
        font-size: 10px;
        color: rgba(220, 230, 235, 0.75);
      }
      .stance-dot {
        display: inline-block;
        width: 7px;
        height: 7px;
        border-radius: 50%;
      }
      .meta-label {
        font-size: 9px;
        color: rgba(220, 230, 235, 0.45);
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }
      .bias-bar-wrap {
        position: relative;
        flex: 1;
        height: 5px;
        background: rgba(255, 255, 255, 0.08);
        border-radius: 2px;
        overflow: hidden;
      }
      .bias-bar-mid {
        position: absolute;
        top: 0;
        bottom: 0;
        left: 50%;
        width: 1px;
        background: rgba(255, 255, 255, 0.25);
      }
      .bias-bar-fill {
        position: absolute;
        top: 0;
        bottom: 0;
      }
      .bias-num {
        min-width: 28px;
        text-align: right;
        font-variant-numeric: tabular-nums;
        color: rgba(220, 230, 235, 0.85);
      }
      .influence-wrap {
        flex: 1;
        position: relative;
        height: 5px;
        background: rgba(255, 255, 255, 0.08);
        border-radius: 2px;
        overflow: hidden;
      }
      .influence-fill {
        position: absolute;
        left: 0;
        top: 0;
        bottom: 0;
        background: linear-gradient(90deg, rgba(168,168,255,0.35), rgba(168,168,255,0.95));
        border-radius: 2px;
      }
      .delay-pill {
        font-size: 9px;
        letter-spacing: 0.04em;
        padding: 1px 6px;
        border-radius: 9px;
        background: rgba(15, 255, 209, 0.08);
        color: rgba(15, 255, 209, 0.85);
        border: 1px solid rgba(15, 255, 209, 0.25);
      }
      .delay-pill.delayed {
        background: rgba(255, 217, 61, 0.08);
        color: rgba(255, 217, 61, 0.9);
        border-color: rgba(255, 217, 61, 0.3);
      }
      .concerns {
        display: flex;
        flex-wrap: wrap;
        gap: 3px;
        margin-top: 6px;
      }
      .concern-chip {
        font-size: 9px;
        line-height: 1;
        padding: 2px 6px;
        border-radius: 9px;
        background: rgba(220, 230, 235, 0.06);
        color: rgba(220, 230, 235, 0.75);
        border: 1px solid rgba(220, 230, 235, 0.12);
      }
      .legend {
        margin-top: 8px;
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        font-size: 10px;
        color: rgba(220, 230, 235, 0.6);
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
    <div class="card" *ngIf="hasValidPersonas; else personasFallback">
      <div class="card-header">
        <span>Stakeholder Personas <span style="opacity:.55">· simulation model</span></span>
        <span class="count">{{ payload!.personas.length }} personas</span>
      </div>

      <div class="rationale" *ngIf="payload!.rationale">{{ payload!.rationale }}</div>

      <div class="personas-grid">
        <div
          *ngFor="let p of payload!.personas"
          class="persona"
          [style.borderLeftColor]="(archetypeColors[p.archetype] || {fill: '#6b7280'}).fill"
        >
          <div class="persona-head">
            <span class="stakeholder">{{ resolveLabel(p.stakeholder_id) }}</span>
            <span
              class="archetype-badge"
              [style.background]="(archetypeColors[p.archetype] || {fill: '#6b7280'}).fill"
              [title]="'Archetype'"
            >{{ (archetypeColors[p.archetype] || {label: 'Unknown'}).label }}</span>
            <span class="inferred-tag" *ngIf="p.inferred" title="Synthetic persona generated by the simulation layer">
              Simulated
            </span>
          </div>

          <div class="row">
            <span class="stance-dot" [style.background]="(stanceColors[p.stance] || {dot: '#6b7280'}).dot"></span>
            <span>{{ (stanceColors[p.stance] || {label: 'Unknown'}).label }}</span>
            <span style="opacity:.5">·</span>
            <span>{{ commStyleLabels[p.communication_style] || 'Unknown' }}</span>
          </div>

          <div class="row" title="Sentiment bias drives stance shifts in multi-tick simulation">
            <span class="meta-label">Bias</span>
            <span class="bias-bar-wrap">
              <span class="bias-bar-mid"></span>
              <span
                class="bias-bar-fill"
                [style.left.%]="biasFillLeft(p.sentiment_bias)"
                [style.width.%]="biasFillWidth(p.sentiment_bias)"
                [style.background]="biasFillColor(p.sentiment_bias)"
              ></span>
            </span>
            <span class="bias-num">{{ formatBias(p.sentiment_bias) }}</span>
          </div>

          <div class="row" title="Influence weight = how strongly this persona pulls neighbors">
            <span class="meta-label">Pull</span>
            <span class="influence-wrap">
              <span class="influence-fill" [style.width.%]="p.influence_weight * 100"></span>
            </span>
            <span
              class="delay-pill"
              [class.delayed]="p.response_delay_sprints > 0"
              [title]="'Sprints this persona waits before acting'"
            >sprint +{{ p.response_delay_sprints }}</span>
          </div>

          <div class="concerns" *ngIf="p.primary_concerns.length > 0">
            <span class="concern-chip" *ngFor="let c of p.primary_concerns">{{ c }}</span>
          </div>
        </div>
      </div>

      <div class="legend">
        <span *ngFor="let a of usedArchetypes">
          <span class="legend-dot" [style.background]="(archetypeColors[a] || {fill: '#6b7280'}).fill"></span>{{ (archetypeColors[a] || {label: 'Unknown'}).label }}
        </span>
      </div>
    </div>
    <ng-template #personasFallback>
      <div class="card card--fallback" *ngIf="receivedPayload" style="padding: 10px 12px; opacity: 0.7; font-size: 12px; display: flex; align-items: center; gap: 6px;">
        <span class="material-icons" style="font-size: 16px;">people</span>
        <span>Stakeholder personas data received</span>
      </div>
    </ng-template>
  `,
})
export class StakeholderPersonasCardComponent {
  @Input() set payload(value: unknown) {
    if (typeof value === 'string') {
      try {
        value = JSON.parse(value);
      } catch (e) {}
    }
    this.receivedPayload = value != null;
    this._payload = this.parse(value);
    this.recompute();
  }

  @Input() set nodeLabels(value: Record<string, string> | undefined | null) {
    this._labels = value ?? {};
  }

  private _payload: PersonasPayload | null = null;
  receivedPayload = false;
  private _labels: Record<string, string> = {};
  usedArchetypes: Archetype[] = [];

  readonly archetypeColors = ARCHETYPE_COLORS;
  readonly stanceColors = STANCE_COLORS;
  readonly commStyleLabels = COMM_STYLE_LABELS;

  get hasValidPersonas(): boolean {
    return !!this._payload && this._payload.personas.length > 0;
  }

  get payload(): PersonasPayload | null {
    return this._payload;
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

  formatBias(v: number): string {
    if (v > 0) return `+${v.toFixed(2)}`;
    return v.toFixed(2);
  }

  /** Bar fills from center outward. Negative bias = left side, positive = right. */
  biasFillLeft(v: number): number {
    if (v >= 0) return 50;
    return 50 + v * 50; // v in [-1,0] → [0, 50)
  }
  biasFillWidth(v: number): number {
    return Math.abs(v) * 50;
  }
  biasFillColor(v: number): string {
    if (v < -0.1) return 'rgba(255, 107, 107, 0.85)';
    if (v > 0.1) return 'rgba(15, 255, 209, 0.85)';
    return 'rgba(255, 217, 61, 0.85)';
  }

  /**
   * Accept either the full tool_end `result` envelope
   * (`{status, stakeholder_personas: {...}}`) or a bare
   * `{personas, rationale?}` payload.
   */
  private parse(value: unknown): PersonasPayload | null {
    if (!value || typeof value !== 'object') return null;
    const obj = value as Record<string, unknown>;
    const inner =
      (obj['stakeholder_personas'] as Record<string, unknown> | undefined) ?? obj;
    const raw = inner['personas'];
    if (!Array.isArray(raw) || raw.length === 0) return null;

    const validArchetypes = new Set<Archetype>([
      'visionary',
      'pragmatist',
      'guardian',
      'resistor',
      'bridge_builder',
    ]);
    const validStyles = new Set<CommStyle>([
      'data_driven',
      'narrative',
      'consensus',
      'directive',
    ]);
    const validStances = new Set<Stance>(['supportive', 'neutral', 'resistant']);

    const out: Persona[] = [];
    const seen = new Set<string>();
    for (const p of raw) {
      if (!p || typeof p !== 'object') continue;
      const x = p as Record<string, unknown>;
      const sid = x['stakeholder_id'];
      const archetype = x['archetype'];
      const commStyle = x['communication_style'];
      const stance = x['stance'];
      const sb = x['sentiment_bias'];
      const iw = x['influence_weight'];
      const delay = x['response_delay_sprints'];
      const concerns = x['primary_concerns'];

      if (typeof sid !== 'string' || !sid.trim()) continue;
      const lowerArchetype = typeof archetype === 'string' ? archetype.toLowerCase() : '';
      const lowerCommStyle = typeof commStyle === 'string' ? commStyle.toLowerCase() : '';
      const lowerStance = typeof stance === 'string' ? stance.toLowerCase() : '';

      if (!validArchetypes.has(lowerArchetype as Archetype)) continue;
      if (!validStyles.has(lowerCommStyle as CommStyle)) continue;
      if (!validStances.has(lowerStance as Stance)) continue;
      if (typeof sb !== 'number' || sb < -1 || sb > 1) continue;
      if (typeof iw !== 'number' || iw < 0 || iw > 1) continue;
      const delayNum = typeof delay === 'number' && Number.isInteger(delay) ? delay : 0;
      if (delayNum < 0) continue;
      if (seen.has(sid)) continue;
      seen.add(sid);

      const concernsList: string[] = [];
      if (Array.isArray(concerns)) {
        for (const c of concerns) {
          if (typeof c === 'string' && c.trim()) concernsList.push(c.trim());
        }
      }
      const inferred = x['inferred'] === false ? false : true;

      out.push({
        stakeholder_id: sid.trim(),
        archetype: lowerArchetype as Archetype,
        communication_style: lowerCommStyle as CommStyle,
        stance: lowerStance as Stance,
        sentiment_bias: sb,
        influence_weight: iw,
        response_delay_sprints: delayNum,
        primary_concerns: concernsList,
        inferred,
      });
    }
    if (out.length === 0) return null;

    const rationale =
      typeof inner['rationale'] === 'string'
        ? (inner['rationale'] as string)
        : undefined;
    return { personas: out, rationale };
  }

  private recompute(): void {
    if (!this._payload) {
      this.usedArchetypes = [];
      return;
    }
    const set = new Set<Archetype>();
    for (const p of this._payload.personas) set.add(p.archetype);
    const order: Archetype[] = [
      'visionary',
      'pragmatist',
      'bridge_builder',
      'guardian',
      'resistor',
    ];
    this.usedArchetypes = order.filter((a) => set.has(a));
  }
}
