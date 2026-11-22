/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
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
 *
 * Changed for The Rehearsal: skip background organizer spawns when memory is unavailable, guard stale async spawns across demo switches/destroy, extract past scenarios from flat A2UI v0.8 surfaces, add explicit spawnError UI state, fix empty-context copy, remove dead legacy route/cached state, and handle runSimulation/sendBroadcast errors.
 */

import {
  Component,
  effect,
  inject,
  input,
  OnDestroy,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subscription } from 'rxjs';
import { GatewayService, ChatMessage } from '../../../../gateway.service';
import { DEMO_CONFIG } from '../../../../demo-config';
import { DemoService } from '../../../DemoOverlay/demo.service';
import { A2uiControllerComponent } from '../../../a2ui/a2-ui-controller.component';

/** Minimal shape of a scenario returned by the recall API. */
export interface OrganizerScenario {
  scenario_id: string;
  title?: string;
  eval_score?: number;
  created_at?: string;
}

function collectOrganizerSubtreeNodes(node: any, byId: Map<string, any>, visited: Set<string>): any[] {
  if (!node || typeof node !== 'object') return [];
  const nodeId = node.id ? String(node.id) : '';
  if (nodeId) {
    if (visited.has(nodeId)) return [];
    visited.add(nodeId);
  }
  const result = [node];
  const compMap = node.component && typeof node.component === 'object' ? node.component : {};
  for (const inner of Object.values(compMap) as any[]) {
    if (!inner || typeof inner !== 'object') continue;
    const refs: string[] = [];
    if (typeof inner.child === 'string') refs.push(inner.child);
    if (Array.isArray(inner.children?.explicitList)) {
      for (const r of inner.children.explicitList) {
        if (typeof r === 'string') refs.push(r);
      }
    }
    for (const ref of refs) {
      const childNode = byId.get(ref);
      if (childNode) {
        result.push(...collectOrganizerSubtreeNodes(childNode, byId, visited));
      }
    }
  }
  return result;
}

function walkOrganizerComponentTree(
  comp: any,
  out: OrganizerScenario[],
  seen: Set<string>,
): void {
  if (!comp) return;
  const data = comp.data ?? comp.component?.data;
  if (data?.scenario_id && typeof data.scenario_id === 'string' && !seen.has(data.scenario_id)) {
    seen.add(data.scenario_id);
    const rawScore = data.eval_score ?? data.score;
    const numScore = rawScore != null && rawScore !== '' ? Number(rawScore) : undefined;
    out.push({
      scenario_id: data.scenario_id,
      title: data.title ?? data.name,
      eval_score: numScore !== undefined && Number.isFinite(numScore) ? numScore : undefined,
      created_at: data.created_at,
    });
  }
  const children = comp.children ?? comp.component?.children;
  if (Array.isArray(children)) {
    for (const child of children) {
      if (typeof child === 'string') continue;
      walkOrganizerComponentTree(child, out, seen);
    }
  }
}

/**
 * Pure extractor for scenario metadata from A2UI surface payloads.
 * Handles both nested `comp.data` bags and flat A2UI v0.8 ID-referenced component arrays.
 */
export function extractOrganizerScenariosFromSurface(result: any): OrganizerScenario[] {
  const scenarios: OrganizerScenario[] = [];
  const seen = new Set<string>();

  const surfaceUpdate = result?.surfaceUpdate;
  const components: any[] | undefined = surfaceUpdate?.components;
  if (!Array.isArray(components) || components.length === 0) return scenarios;

  for (const comp of components) {
    walkOrganizerComponentTree(comp, scenarios, seen);
  }

  if (scenarios.length === 0) {
    const byId = new Map<string, any>();
    for (const comp of components) {
      if (comp?.id) byId.set(String(comp.id), comp);
    }
    const listComp = components.find((c) => c?.component?.List);
    const cardIds: string[] =
      listComp?.component?.List?.children?.explicitList ??
      components
        .filter((c) => c?.component?.Card && c.id !== 'root-card')
        .map((c) => String(c.id));

    for (const cardId of cardIds) {
      const card = byId.get(cardId);
      if (!card) continue;
      const subtree = collectOrganizerSubtreeNodes(card, byId, new Set<string>());
      let scenarioId = '';
      let title: string | undefined;
      let evalScore: number | undefined;

      for (const node of subtree) {
        const id = String(node?.id ?? '');
        const btn = node?.component?.Button;
        const payload = btn?.action?.payload;
        if (!scenarioId && payload) {
          const candidate = payload.scenario_id ?? payload.seed;
          if (typeof candidate === 'string' && candidate.trim()) {
            scenarioId = candidate.trim();
          }
        }
        const txtObj = node?.component?.Text;
        const rawText =
          typeof txtObj?.text === 'string'
            ? txtObj.text
            : txtObj?.text?.literalString;
        if (typeof rawText === 'string' && rawText.trim()) {
          const trimmed = rawText.trim();
          if (!title && (id.startsWith('title-') || txtObj?.usageHint === 'h2')) {
            title = trimmed;
          }
          if (evalScore === undefined && id.startsWith('score-num-')) {
            const num = Number(trimmed);
            if (Number.isFinite(num)) evalScore = num;
          }
          if (!scenarioId && (id.startsWith('sim-meta-') || id.startsWith('meta-'))) {
            const cleaned = trimmed.replace(/^#/, '');
            if (cleaned) scenarioId = cleaned;
          }
        }
      }

      if (scenarioId && !seen.has(scenarioId)) {
        seen.add(scenarioId);
        scenarios.push({
          scenario_id: scenarioId,
          title,
          eval_score: evalScore,
        });
      }
    }
  }

  return scenarios;
}

@Component({
  selector: 'app-organizer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, A2uiControllerComponent],
  styleUrls: ['./Organizer.scss'],
  template: `
    <div class="panel" [class.organizer-panel--loading]="isOrganizerAgentGettingRoutes">
      <div class="wave-blur"><div class="wave-inner"></div></div>
      <div class="organizer-error" role="alert" *ngIf="spawnError() as err">
        {{ err }}
      </div>
      <ng-container *ngIf="this.isOrganizerAvailable; else noOrganizer">
        <a2ui-controller
          [runCached]="runCachedMessages()"
          [plannerAgentGuid]="plannerAgentGuid"
          [selectedContextIds]="selectedContextIdsList()"
          *ngIf="responseNode"
          [node]="responseNode"
        ></a2ui-controller>

        <div class="no-organizer-container" *ngIf="!responseNode && !isOrganizerAgentGettingRoutes && !spawnError()">
          <h2>No past scenarios loaded.</h2>
          <p *ngIf="runCachedMessages()">Switch to Live mode or replay a recorded session to populate scenario memory.</p>
        </div>

        <!-- Context Scenario Picker (leadership only) -->
        <div
          class="context-picker"
          *ngIf="isLeadershipMode && pastScenarios.length > 0"
        >
          <h3 class="context-picker__title">
            <span class="material-icons">history</span>
            Past context
          </h3>
          <label
            class="context-picker__item"
            *ngFor="let s of pastScenarios"
            [class.context-picker__item--selected]="selectedContextIds.has(s.scenario_id)"
          >
            <input
              type="checkbox"
              [checked]="selectedContextIds.has(s.scenario_id)"
              (change)="toggleContextScenario(s.scenario_id)"
            />
            <span class="context-picker__label">
              {{ s.title || 'Untitled scenario' }}
            </span>
            <span class="context-picker__score" *ngIf="isFiniteScore(s.eval_score)">
              {{ s.eval_score | number:'1.0-0' }}
            </span>
          </label>
        </div>
      </ng-container>
      <ng-template #noOrganizer>
        <div class="no-organizer-container">
          <h2>Agent has no memory context.</h2>
          <p>See agents with memory from Demo 3 onwards.</p>
        </div>
      </ng-template>
    </div>
  `,
})
export class OrganizerComponent implements OnDestroy {
  /** Bound from AgentScreen: when true, avoid live organizer spawning until replay or live mode provides surface data. */
  runCachedMessages = input(false);

  isOrganizerAvailable: boolean = false;

  responseNode: any = null;

  private demoService = inject(DemoService);
  private gateway = inject(GatewayService);
  private cdr = inject(ChangeDetectorRef);
  protected plannerAgentGuid: string | null = null;
  private chatSub: Subscription | null = null;

  protected isOrganizerAgentGettingRoutes = false;
  protected isLeadershipMode = false;
  readonly spawnError = signal<string | null>(null);
  private spawnSeq = 0;
  private destroyed = false;

  /** User-checked past scenario IDs for context injection. */
  selectedContextIds = new Set<string>();
  /** Signal wrapper for the template binding on A2UI controller. */
  selectedContextIdsList = signal<string[]>([]);
  /** Past scenarios available for context selection. */
  pastScenarios: OrganizerScenario[] = [];

  constructor() {
    this.chatSub = this.gateway.chat$.subscribe((msg: ChatMessage) => {
      if (this.destroyed) return;
      if (msg.guid === this.plannerAgentGuid && msg.result && msg.result.surfaceUpdate) {
        this.responseNode = msg.result;
        this.isOrganizerAgentGettingRoutes = false;
        this.spawnError.set(null);

        // Extract scenario IDs from the A2UI surface for the context picker.
        this.extractScenariosFromSurface(msg.result);
        this.cdr.markForCheck();
      }
    });

    effect(() => {
      const seq = ++this.spawnSeq;
      const activeDemoKey = this.demoService.activeDemo();
      const activeDemo = DEMO_CONFIG[activeDemoKey] as any;
      this.spawnError.set(null);
      this.isOrganizerAvailable =
        activeDemo?.agent === 'planner_with_memory' ||
        activeDemo?.agent === 'simulator_with_failure' ||
        activeDemo?.agent === 'leadership_planner_with_memory';
      this.isLeadershipMode = activeDemo?.agent === 'leadership_planner_with_memory';

      if (!this.isOrganizerAvailable) {
        this.cleanupPreviousAgent();
        this.responseNode = null;
        this.pastScenarios = [];
        this.selectedContextIds.clear();
        this.selectedContextIdsList.set([]);
        this.isOrganizerAgentGettingRoutes = false;
        this.cdr.markForCheck();
        return;
      }

      if (this.runCachedMessages()) {
        this.cleanupPreviousAgent();
        this.responseNode = null;
        this.pastScenarios = [];
        this.selectedContextIds.clear();
        this.selectedContextIdsList.set([]);
        this.isOrganizerAgentGettingRoutes = false;
      } else {
        // Clean up previous agent session before spawning a new one
        this.cleanupPreviousAgent();

        this.responseNode = null;
        this.pastScenarios = [];
        this.selectedContextIds.clear();
        this.selectedContextIdsList.set([]);
        this.isOrganizerAgentGettingRoutes = true;
        // Spawn the active demo's agent — leadership demos use a different
        // agent type and prompt than the marathon demos.
        const isLeadership = activeDemo.agent === 'leadership_planner_with_memory';
        const agentType = isLeadership ? 'leadership_planner_with_memory' : 'planner_with_memory';
        const prompt = isLeadership ? 'list the top 3 best scenarios' : 'list the top 3 best routes';
        this.gateway.addAgent(agentType).then((newPlannerAgent) => {
          if (this.destroyed || seq !== this.spawnSeq) {
            this.gateway.removeAgent(newPlannerAgent);
            return;
          }
          this.plannerAgentGuid = newPlannerAgent;
          this.cdr.markForCheck();
          return this.gateway.sendBroadcast(prompt, [newPlannerAgent], true, true);
        }).catch((err) => {
          console.error('Failed to add agent or request scenarios', err);
          if (!this.destroyed && seq === this.spawnSeq) {
            this.spawnError.set(
              err instanceof Error && err.message
                ? err.message
                : 'Unable to load scenarios from memory agent.',
            );
            this.isOrganizerAgentGettingRoutes = false;
            this.cdr.markForCheck();
          }
        });
      }
      this.cdr.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.spawnSeq++;
    this.chatSub?.unsubscribe();
    this.chatSub = null;
    this.selectedContextIds.clear();
    this.selectedContextIdsList.set([]);
    this.cleanupPreviousAgent();
  }

  /** Remove the previously spawned agent session to avoid orphaned backend sessions. */
  private cleanupPreviousAgent(): void {
    if (this.plannerAgentGuid) {
      this.gateway.removeAgent(this.plannerAgentGuid);
      this.plannerAgentGuid = null;
    }
  }

  isFiniteScore(value: number | undefined): value is number {
    return typeof value === 'number' && Number.isFinite(value);
  }

  async runSimulation(e: any, name: string): Promise<void> {
    e?.stopPropagation?.();
    const seq = ++this.spawnSeq;
    this.spawnError.set(null);
    this.cleanupPreviousAgent();
    const activeDemo = DEMO_CONFIG[this.demoService.activeDemo()] as any;
    const isLeadership = activeDemo?.agent?.includes('leadership') ?? false;
    const agentType = isLeadership ? 'leadership_planner_with_memory' : 'planner_with_memory';
    try {
      const newPlannerAgent = await this.gateway.addAgent(agentType);
      if (this.destroyed || seq !== this.spawnSeq) {
        this.gateway.removeAgent(newPlannerAgent);
        return;
      }
      this.plannerAgentGuid = newPlannerAgent;

      // Embed selected context scenario IDs for the planner.
      const ids = Array.from(this.selectedContextIds);
      const contextSuffix = isLeadership && ids.length > 0
        ? ` using context from scenarios ${ids.join(', ')}`
        : '';
      await this.gateway.sendBroadcast(`Run Simulation for ${name}${contextSuffix}`, [newPlannerAgent]);
    } catch (err) {
      console.error('Failed to run simulation', err);
      if (!this.destroyed && seq === this.spawnSeq) {
        this.spawnError.set(
          err instanceof Error && err.message ? err.message : 'Failed to start simulation.',
        );
        this.cdr.markForCheck();
      }
    }
  }

  /** Toggle a past scenario's selection state. */
  toggleContextScenario(scenarioId: string): void {
    if (this.selectedContextIds.has(scenarioId)) {
      this.selectedContextIds.delete(scenarioId);
    } else {
      this.selectedContextIds.add(scenarioId);
    }
    this.selectedContextIdsList.set(Array.from(this.selectedContextIds));
    this.cdr.markForCheck();
  }

  /**
   * Extract scenario metadata from the A2UI surface payload.
   */
  extractScenariosFromSurface(result: any): void {
    const scenarios = extractOrganizerScenariosFromSurface(result);
    if (scenarios.length > 0) {
      this.pastScenarios = scenarios;
    }
  }
}
