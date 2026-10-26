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
 * Renders the stakeholder relationship graph emitted by the
 * `emit_stakeholder_graph` Step-0 tool of `leadership_planner_with_memory`.
 *
 * Surface: inline ~260px card in the agent chat log (right-side panel of
 * the keynote demo shell). Click to expand to a full-screen modal.
 *
 * Layout: nodes are placed on a circle by index — deterministic, no
 * simulation library needed for 5–10 nodes. Each node is colored by
 * `kind` (Actor | Group | Structure | Friction | Belief).
 *
 * Resilience: this component is rendered from a `tool_end` chat message
 * whose `result.stakeholder_graph` may be missing or malformed if the
 * agent skipped Step 0 or a cached stream is out of date. In that case
 * we render nothing and let the surrounding row keep its default
 * "✔ Result from emit_stakeholder_graph" summary line.
 */

import { Component, Input, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';

interface GraphNode {
  id: string;
  label: string;
  kind: 'Actor' | 'Group' | 'Structure' | 'Friction' | 'Belief';
  note?: string;
  governing?: boolean;
}

interface GraphEdge {
  from: string;
  to: string;
  label: string;
}

interface StakeholderGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  rationale?: string;
}

interface PlacedNode extends GraphNode {
  cx: number;
  cy: number;
}

interface PlacedEdge {
  from: PlacedNode;
  to: PlacedNode;
  label: string;
  midX: number;
  midY: number;
}

// Keep KIND_COLORS in sync with the Python `ALLOWED_KINDS` set in
// agents/leadership_planner_with_memory/stakeholder_graph.py.
const KIND_COLORS: Record<GraphNode['kind'], { fill: string; stroke: string }> = {
  Actor:     { fill: '#0fffd1', stroke: '#003a30' },
  Group:     { fill: '#80ffd2', stroke: '#0a4d3a' },
  Structure: { fill: '#a8a8ff', stroke: '#1f1f5a' },
  Friction:  { fill: '#ff6b6b', stroke: '#5a1010' },
  Belief:    { fill: '#ffd93d', stroke: '#4a3500' },
};

const EDGE_COLOR = '#80ffd2';

@Component({
  selector: 'app-stakeholder-graph-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  styles: [
    `
      :host {
        display: block;
        margin: 8px 0 4px 0;
      }
      .card {
        position: relative;
        background: rgba(8, 16, 22, 0.85);
        border: 1px solid rgba(15, 255, 209, 0.35);
        border-radius: 8px;
        padding: 10px 12px 8px 12px;
        cursor: zoom-in;
        box-shadow: 0 0 18px rgba(15, 255, 209, 0.12);
        transition: box-shadow 120ms ease;
      }
      .card:hover {
        box-shadow: 0 0 26px rgba(15, 255, 209, 0.22);
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
      }
      .card-header .count {
        color: rgba(128, 255, 210, 0.7);
      }
      .rationale {
        font-size: 11px;
        line-height: 1.35;
        color: rgba(220, 230, 235, 0.78);
        margin: 6px 2px 0 2px;
        font-style: italic;
      }
      .legend {
        margin-top: 6px;
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        font-size: 10px;
        color: rgba(220, 230, 235, 0.7);
      }
      .legend-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        margin-right: 3px;
        vertical-align: middle;
      }

      /* Modal overlay */
      .modal-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.75);
        z-index: 1000;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
      }
      .modal {
        background: #050c10;
        border: 1px solid rgba(15, 255, 209, 0.5);
        border-radius: 10px;
        padding: 18px 22px;
        max-width: min(92vw, 960px);
        max-height: 92vh;
        overflow: auto;
        box-shadow: 0 0 40px rgba(15, 255, 209, 0.25);
      }
      .modal-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 12px;
      }
      .modal-title {
        font-size: 13px;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: #0fffd1;
      }
      .modal-close {
        background: none;
        border: 1px solid rgba(15, 255, 209, 0.4);
        color: #0fffd1;
        padding: 4px 10px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 11px;
      }
      .modal-close:hover {
        background: rgba(15, 255, 209, 0.1);
      }
      .node-list {
        margin-top: 14px;
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
        gap: 6px;
        font-size: 11px;
        color: rgba(220, 230, 235, 0.85);
      }
      .node-list-item {
        padding: 6px 8px;
        border-radius: 4px;
        background: rgba(15, 255, 209, 0.04);
        border-left: 3px solid;
      }
      .node-list-item .lbl {
        font-weight: 600;
      }
      .node-list-item .knd {
        font-size: 10px;
        color: rgba(220, 230, 235, 0.55);
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }
      .node-list-item .note {
        font-size: 10px;
        color: rgba(220, 230, 235, 0.65);
        margin-top: 2px;
      }

      svg {
        display: block;
        width: 100%;
        height: auto;
      }
      .edge-line {
        stroke: ${EDGE_COLOR};
        stroke-width: 1.2;
        opacity: 0.55;
        fill: none;
      }
      .edge-label {
        fill: rgba(220, 230, 235, 0.65);
        font-size: 8px;
        text-anchor: middle;
        pointer-events: none;
      }
      .modal svg .edge-label {
        font-size: 11px;
      }
      .node-circle {
        stroke-width: 1.5;
      }
      .node-label {
        text-anchor: middle;
        font-size: 9px;
        fill: #f0f6f4;
        pointer-events: none;
        dominant-baseline: middle;
      }
      .modal svg .node-label {
        font-size: 12px;
      }
      .governing-ring {
        fill: none;
        stroke: #ffd93d;
        stroke-width: 1;
        stroke-dasharray: 2 2;
        opacity: 0.7;
      }
    `,
  ],
  template: `
    <ng-container *ngIf="hasValidGraph; else graphFallback">
      <div
        class="card"
        (click)="openModal($event)"
        role="button"
        tabindex="0"
        (keydown.enter)="openModal($event)"
        title="Click to expand"
      >
        <div class="card-header">
          <span>Stakeholder Graph</span>
          <span class="count">{{ graph!.nodes.length }} nodes · {{ graph!.edges.length }} edges</span>
        </div>

        <svg
          [attr.viewBox]="'0 0 ' + INLINE_SIZE + ' ' + INLINE_SIZE"
          preserveAspectRatio="xMidYMid meet"
          [style.max-height.px]="260"
        >
          <g>
            <ng-container *ngFor="let edge of placedEdges">
              <line
                class="edge-line"
                [attr.x1]="edge.from.cx"
                [attr.y1]="edge.from.cy"
                [attr.x2]="edge.to.cx"
                [attr.y2]="edge.to.cy"
              ></line>
            </ng-container>
          </g>
          <g>
            <ng-container *ngFor="let n of placedNodes">
              <circle
                class="node-circle"
                [attr.cx]="n.cx"
                [attr.cy]="n.cy"
                [attr.r]="INLINE_NODE_R"
                [attr.fill]="(kindColors[n.kind] || {fill: '#6b7280', stroke: '#9ca3af'}).fill"
                [attr.stroke]="(kindColors[n.kind] || {fill: '#6b7280', stroke: '#9ca3af'}).stroke"
              ></circle>
              <circle
                *ngIf="n.governing"
                class="governing-ring"
                [attr.cx]="n.cx"
                [attr.cy]="n.cy"
                [attr.r]="INLINE_NODE_R + 3"
              ></circle>
              <text
                class="node-label"
                [attr.x]="n.cx"
                [attr.y]="n.cy + INLINE_NODE_R + 9"
              >{{ truncate(n.label, 14) }}</text>
            </ng-container>
          </g>
        </svg>

        <div class="rationale" *ngIf="graph!.rationale">{{ graph!.rationale }}</div>

        <div class="legend">
          <div class="legend-item" *ngFor="let k of usedKinds">
            <span class="legend-dot" [style.background]="(kindColors[k] || {fill: '#6b7280'}).fill"></span>{{ k }}
          </div>
        </div>
      </div>

      <div
        *ngIf="modalOpen"
        class="modal-backdrop"
        (click)="closeModal($event)"
      >
        <div class="modal" (click)="$event.stopPropagation()">
          <div class="modal-header">
            <span class="modal-title">Stakeholder Graph — Full View</span>
            <button type="button" class="modal-close" (click)="closeModal($event)">Close</button>
          </div>

          <svg
            [attr.viewBox]="'0 0 ' + MODAL_SIZE + ' ' + MODAL_SIZE"
            preserveAspectRatio="xMidYMid meet"
            [style.max-height.vh]="65"
          >
            <g>
              <ng-container *ngFor="let edge of placedEdgesModal">
                <line
                  class="edge-line"
                  [attr.x1]="edge.from.cx"
                  [attr.y1]="edge.from.cy"
                  [attr.x2]="edge.to.cx"
                  [attr.y2]="edge.to.cy"
                ></line>
                <text
                  class="edge-label"
                  [attr.x]="edge.midX"
                  [attr.y]="edge.midY - 4"
                >{{ edge.label }}</text>
              </ng-container>
            </g>
            <g>
              <ng-container *ngFor="let n of placedNodesModal">
                <circle
                  class="node-circle"
                  [attr.cx]="n.cx"
                  [attr.cy]="n.cy"
                  [attr.r]="MODAL_NODE_R"
                  [attr.fill]="(kindColors[n.kind] || {fill: '#6b7280', stroke: '#9ca3af'}).fill"
                  [attr.stroke]="(kindColors[n.kind] || {fill: '#6b7280', stroke: '#9ca3af'}).stroke"
                ></circle>
                <circle
                  *ngIf="n.governing"
                  class="governing-ring"
                  [attr.cx]="n.cx"
                  [attr.cy]="n.cy"
                  [attr.r]="MODAL_NODE_R + 5"
                ></circle>
                <text
                  class="node-label"
                  [attr.x]="n.cx"
                  [attr.y]="n.cy + MODAL_NODE_R + 14"
                >{{ n.label }}</text>
              </ng-container>
            </g>
          </svg>

          <div class="rationale" *ngIf="graph!.rationale" style="margin-top: 12px; font-size: 12px;">
            {{ graph!.rationale }}
          </div>

          <div class="node-list">
            <div
              class="node-list-item"
              *ngFor="let n of graph!.nodes"
              [style.borderLeftColor]="(kindColors[n.kind] || {fill: '#6b7280'}).fill"
            >
              <div class="lbl">{{ n.label }}</div>
              <div class="knd">{{ n.kind }}<span *ngIf="n.governing"> · governing</span></div>
              <div class="note" *ngIf="n.note">{{ n.note }}</div>
            </div>
          </div>
        </div>
      </div>
    </ng-container>
    <ng-template #graphFallback>
      <div class="card card--fallback" *ngIf="receivedPayload" style="padding: 10px 12px; opacity: 0.7; font-size: 12px; display: flex; align-items: center; gap: 6px;">
        <span class="material-icons" style="font-size: 16px;">hub</span>
        <span>Stakeholder graph data received</span>
      </div>
    </ng-template>
  `,
})
export class StakeholderGraphCardComponent {
  @Input() set payload(value: unknown) {
    if (typeof value === 'string') {
      try {
        value = JSON.parse(value);
      } catch (e) {}
    }
    this.receivedPayload = value != null;
    this.graph = this.parseGraph(value);
    this.recompute();
  }

  graph: StakeholderGraph | null = null;
  receivedPayload = false;
  placedNodes: PlacedNode[] = [];
  placedEdges: PlacedEdge[] = [];
  placedNodesModal: PlacedNode[] = [];
  placedEdgesModal: PlacedEdge[] = [];
  usedKinds: GraphNode['kind'][] = [];
  modalOpen = false;

  readonly kindColors = KIND_COLORS;

  // Inline ~260px. Modal up to 720px. Both keep the same circular layout
  // so the user sees the same shape, just larger.
  readonly INLINE_SIZE = 260;
  readonly INLINE_NODE_R = 14;
  readonly MODAL_SIZE = 720;
  readonly MODAL_NODE_R = 32;

  get hasValidGraph(): boolean {
    return !!this.graph && this.graph.nodes.length > 0;
  }

  openModal(evt: Event): void {
    evt.stopPropagation();
    this.modalOpen = true;
  }

  closeModal(evt: Event): void {
    evt.stopPropagation();
    this.modalOpen = false;
  }

  truncate(s: string, n: number): string {
    return s.length > n ? s.substring(0, n - 1) + '…' : s;
  }

  /**
   * Accept either the full tool_end `result` envelope
   * (`{status, stakeholder_graph: {...}}`) or a bare graph
   * (`{nodes, edges, rationale?}`). The cached NDJSON stream sometimes
   * wraps it once, sometimes not — be permissive on the read side.
   */
  private parseGraph(value: unknown): StakeholderGraph | null {
    if (!value || typeof value !== 'object') return null;
    const obj = value as Record<string, unknown>;
    const inner = (obj['stakeholder_graph'] as Record<string, unknown> | undefined) ?? obj;
    const nodes = inner['nodes'];
    const edges = inner['edges'] ?? [];
    if (!Array.isArray(nodes) || nodes.length === 0) return null;
    if (!Array.isArray(edges)) return null;

    const validKinds = new Set(['actor', 'group', 'structure', 'friction', 'belief']);
    const cleanNodes: GraphNode[] = [];
    const seen = new Set<string>();
    for (const raw of nodes) {
      if (!raw || typeof raw !== 'object') continue;
      const r = raw as Record<string, unknown>;
      const id = r['id'];
      const label = r['label'];
      const rawKind = r['kind'];
      if (typeof id !== 'string' || typeof label !== 'string') continue;
      if (typeof rawKind !== 'string') continue;
      const lowerKind = rawKind.toLowerCase();
      if (!validKinds.has(lowerKind)) continue;
      const kind = lowerKind.charAt(0).toUpperCase() + lowerKind.slice(1);
      if (seen.has(id)) continue;
      seen.add(id);
      const node: GraphNode = { id, label, kind: kind as GraphNode['kind'] };
      if (typeof r['note'] === 'string') node.note = r['note'] as string;
      if (r['governing'] === true) node.governing = true;
      cleanNodes.push(node);
    }
    if (cleanNodes.length === 0) return null;

    const ids = new Set(cleanNodes.map((n) => n.id));
    const cleanEdges: GraphEdge[] = [];
    for (const raw of edges) {
      if (!raw || typeof raw !== 'object') continue;
      const r = raw as Record<string, unknown>;
      const from = r['from'];
      const to = r['to'];
      const label = r['label'];
      if (typeof from !== 'string' || !ids.has(from)) continue;
      if (typeof to !== 'string' || !ids.has(to)) continue;
      if (from === to) continue;
      cleanEdges.push({
        from,
        to,
        label: typeof label === 'string' ? label : '',
      });
    }

    const rationale = typeof inner['rationale'] === 'string'
      ? (inner['rationale'] as string)
      : undefined;

    return { nodes: cleanNodes, edges: cleanEdges, rationale };
  }

  private recompute(): void {
    if (!this.graph) {
      this.placedNodes = [];
      this.placedEdges = [];
      this.placedNodesModal = [];
      this.placedEdgesModal = [];
      this.usedKinds = [];
      return;
    }
    this.placedNodes = this.placeOnCircle(this.graph.nodes, this.INLINE_SIZE, this.INLINE_NODE_R);
    this.placedEdges = this.linkEdges(this.placedNodes, this.graph.edges);
    this.placedNodesModal = this.placeOnCircle(this.graph.nodes, this.MODAL_SIZE, this.MODAL_NODE_R);
    this.placedEdgesModal = this.linkEdges(this.placedNodesModal, this.graph.edges);

    const kinds = new Set<GraphNode['kind']>();
    for (const n of this.graph.nodes) kinds.add(n.kind);
    // Preserve a canonical legend order so kind colors line up with the
    // human reading order (actors first, friction last).
    const order: GraphNode['kind'][] = ['Actor', 'Group', 'Structure', 'Belief', 'Friction'];
    this.usedKinds = order.filter((k) => kinds.has(k));
  }

  private placeOnCircle(nodes: GraphNode[], size: number, nodeR: number): PlacedNode[] {
    const cx = size / 2;
    const cy = size / 2;
    // Leave 1.7 × nodeR of padding so labels under the node don't clip.
    const r = size / 2 - nodeR * 2.2;
    const n = nodes.length;
    return nodes.map((node, i) => {
      // Start from the top (-π/2) so the first node sits at 12 o'clock.
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
      return {
        ...node,
        cx: cx + r * Math.cos(angle),
        cy: cy + r * Math.sin(angle),
      };
    });
  }

  private linkEdges(placed: PlacedNode[], edges: GraphEdge[]): PlacedEdge[] {
    const byId = new Map<string, PlacedNode>();
    for (const p of placed) byId.set(p.id, p);
    const out: PlacedEdge[] = [];
    for (const e of edges) {
      const from = byId.get(e.from);
      const to = byId.get(e.to);
      if (!from || !to) continue;
      out.push({
        from,
        to,
        label: e.label,
        midX: (from.cx + to.cx) / 2,
        midY: (from.cy + to.cy) / 2,
      });
    }
    return out;
  }
}
