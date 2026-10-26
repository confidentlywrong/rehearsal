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
 * Renders the regime-B stakeholder-reactions stream emitted by the
 * `emit_stakeholder_reactions` tool of `leadership_planner_with_memory`.
 *
 * Surface: a list of short speech-bubble cards under the Step-0
 * stakeholder graph. Each bubble is one synthetic utterance from one
 * stakeholder in response to the proposed plan — MiroFish-style
 * "tweets → report" pipeline, but in organizational discourse units
 * (meeting comments, hallway gripes, Slack reactions).
 *
 * Stance is encoded by a colored dot + left border:
 *   supportive → cyan/green
 *   neutral    → amber
 *   resistant  → red
 *
 * Reactions tagged `inferred: true` get a small "(simulated)" badge so
 * the user can never mistake a synthetic utterance for an observed
 * quote — this honors the refined rule: invented data IS expected in
 * the simulation layer, but it must always be UI-marked as synthetic.
 *
 * Resilience: parses permissively (full tool_end envelope OR bare
 * `{reactions}` payload). If the payload is empty or malformed,
 * renders nothing and lets the surrounding row keep its default
 * tool-call summary.
 */

import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

type Stance = 'supportive' | 'neutral' | 'resistant';

interface Reaction {
  stakeholder_id: string;
  stance: Stance;
  reaction_text: string;
  inferred: boolean;
}

interface ReactionsPayload {
  reactions: Reaction[];
  rationale?: string;
}

// Keep STANCE_COLORS aligned with the Python `ALLOWED_STANCES` set in
// agents/leadership_planner_with_memory/stakeholder_reactions.py.
const STANCE_COLORS: Record<Stance, { dot: string; border: string; label: string }> = {
  supportive: { dot: '#0fffd1', border: 'rgba(15, 255, 209, 0.55)', label: 'Supportive' },
  neutral: { dot: '#ffd93d', border: 'rgba(255, 217, 61, 0.55)', label: 'Neutral' },
  resistant: { dot: '#ff6b6b', border: 'rgba(255, 107, 107, 0.55)', label: 'Resistant' },
};

@Component({
  selector: 'app-stakeholder-reactions-card',
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
        border: 1px solid rgba(15, 255, 209, 0.25);
        border-radius: 8px;
        padding: 10px 12px 10px 12px;
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
      }
      .card-header .count {
        color: rgba(128, 255, 210, 0.7);
      }
      .rationale {
        font-size: 11px;
        line-height: 1.35;
        color: rgba(220, 230, 235, 0.7);
        margin: 0 2px 8px 2px;
        font-style: italic;
      }
      .reactions {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .reaction {
        position: relative;
        padding: 7px 10px 8px 12px;
        border-radius: 6px;
        background: rgba(15, 255, 209, 0.04);
        border-left: 3px solid;
      }
      .reaction-head {
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 3px;
      }
      .stance-dot {
        display: inline-block;
        width: 7px;
        height: 7px;
        border-radius: 50%;
      }
      .stakeholder {
        font-size: 11px;
        font-weight: 600;
        color: #f0f6f4;
        letter-spacing: 0.02em;
      }
      .stance-label {
        font-size: 9px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: rgba(220, 230, 235, 0.55);
      }
      .inferred-tag {
        margin-left: auto;
        font-size: 9px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: rgba(255, 217, 61, 0.75);
        border: 1px solid rgba(255, 217, 61, 0.4);
        padding: 1px 5px;
        border-radius: 3px;
      }
      .quote {
        font-size: 12px;
        line-height: 1.4;
        color: rgba(220, 230, 235, 0.92);
      }
      .quote::before {
        content: '“';
        color: rgba(15, 255, 209, 0.5);
        margin-right: 2px;
      }
      .quote::after {
        content: '”';
        color: rgba(15, 255, 209, 0.5);
        margin-left: 2px;
      }
      .legend {
        margin-top: 8px;
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        font-size: 10px;
        color: rgba(220, 230, 235, 0.65);
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
    <div class="card" *ngIf="hasValidReactions; else reactionsFallback">
      <div class="card-header">
        <span>Stakeholder Reactions <span style="opacity:.55">· tick 1</span></span>
        <span class="count">{{ payload!.reactions.length }} voices</span>
      </div>

      <div class="rationale" *ngIf="payload!.rationale">{{ payload!.rationale }}</div>

      <div class="reactions">
        <div
          *ngFor="let r of payload!.reactions"
          class="reaction"
          [style.borderLeftColor]="(stanceColors[r.stance] || {border: '#6b7280'}).border"
        >
          <div class="reaction-head">
            <span class="stance-dot" [style.background]="(stanceColors[r.stance] || {dot: '#6b7280'}).dot"></span>
            <span class="stakeholder">{{ resolveLabel(r.stakeholder_id) }}</span>
            <span class="stance-label">{{ (stanceColors[r.stance] || {label: 'Unknown'}).label }}</span>
            <span class="inferred-tag" *ngIf="r.inferred" title="Synthetic reaction generated by the simulation layer">
              Simulated
            </span>
          </div>
          <div class="quote">{{ r.reaction_text }}</div>
        </div>
      </div>

      <div class="legend">
        <span *ngFor="let s of usedStances">
          <span class="legend-dot" [style.background]="(stanceColors[s] || {dot: '#6b7280'}).dot"></span>{{ (stanceColors[s] || {label: 'Unknown'}).label }}
        </span>
      </div>
    </div>
    <ng-template #reactionsFallback>
      <div class="card card--fallback" *ngIf="receivedPayload" style="padding: 10px 12px; opacity: 0.7; font-size: 12px; display: flex; align-items: center; gap: 6px;">
        <span class="material-icons" style="font-size: 16px;">forum</span>
        <span>Stakeholder reactions data received</span>
      </div>
    </ng-template>
  `,
})
export class StakeholderReactionsCardComponent {
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

  /**
   * Optional id → label map sourced from the previously-emitted
   * stakeholder_graph in the same agent run. When absent we display
   * the raw stakeholder_id (title-cased) as a fallback.
   */
  @Input() set nodeLabels(value: Record<string, string> | undefined | null) {
    this._labels = value ?? {};
  }

  private _payload: ReactionsPayload | null = null;
  receivedPayload = false;
  private _labels: Record<string, string> = {};
  usedStances: Stance[] = [];

  readonly stanceColors = STANCE_COLORS;

  get hasValidReactions(): boolean {
    return !!this._payload && this._payload.reactions.length > 0;
  }

  get payload(): ReactionsPayload | null {
    return this._payload;
  }

  resolveLabel(id: string): string {
    const v = this._labels[id];
    if (v && v.trim()) return v;
    // Fallback: turn `mid_mgmt` into `Mid Mgmt`.
    return id
      .split(/[_\s-]+/)
      .filter(Boolean)
      .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
      .join(' ');
  }

  /**
   * Accept either the full tool_end `result` envelope
   * (`{status, stakeholder_reactions: {...}}`) or a bare
   * `{reactions, rationale?}` payload.
   */
  private parse(value: unknown): ReactionsPayload | null {
    if (!value || typeof value !== 'object') return null;
    const obj = value as Record<string, unknown>;
    const inner =
      (obj['stakeholder_reactions'] as Record<string, unknown> | undefined) ?? obj;
    const raw = inner['reactions'];
    if (!Array.isArray(raw) || raw.length === 0) return null;

    const validStances = new Set<Stance>(['supportive', 'neutral', 'resistant']);
    const out: Reaction[] = [];
    const seen = new Set<string>();
    for (const r of raw) {
      if (!r || typeof r !== 'object') continue;
      const x = r as Record<string, unknown>;
      const sid = x['stakeholder_id'];
      const stance = x['stance'];
      const text = x['reaction_text'];
      if (typeof sid !== 'string' || !sid.trim()) continue;
      const lowerStance = typeof stance === 'string' ? stance.toLowerCase() : '';
      if (!validStances.has(lowerStance as Stance)) continue;
      if (typeof text !== 'string' || !text.trim()) continue;
      if (seen.has(sid)) continue;
      seen.add(sid);
      const inferred = x['inferred'] === false ? false : true;
      out.push({
        stakeholder_id: sid.trim(),
        stance: lowerStance as Stance,
        reaction_text: text.trim(),
        inferred,
      });
    }
    if (out.length === 0) return null;

    const rationale =
      typeof inner['rationale'] === 'string'
        ? (inner['rationale'] as string)
        : undefined;
    return { reactions: out, rationale };
  }

  private recompute(): void {
    if (!this._payload) {
      this.usedStances = [];
      return;
    }
    const set = new Set<Stance>();
    for (const r of this._payload.reactions) set.add(r.stance);
    const order: Stance[] = ['supportive', 'neutral', 'resistant'];
    this.usedStances = order.filter((s) => set.has(s));
  }
}
