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
import { DEMO_CONFIG, PRECONFIGURED_ROUTES } from '../../../../demo-config';
import { DemoService } from '../../../DemoOverlay/demo.service';
import { A2uiControllerComponent } from '../../../a2ui/a2-ui-controller.component';
import { CACHED_ORGANIZER_MESSAGE } from '../../../../../constants';

/** Minimal shape of a scenario returned by the recall API. */
interface OrganizerScenario {
  scenario_id: string;
  title?: string;
  eval_score?: number;
  created_at?: string;
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
      <ng-container *ngIf="this.isOrganizerAvailable; else noOrganizer">
        <a2ui-controller
          [runCached]="runCachedMessages()"
          [plannerAgentGuid]="plannerAgentGuid"
          [selectedContextIds]="selectedContextIdsList()"
          *ngIf="responseNode"
          [node]="responseNode"
        ></a2ui-controller>

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
            <span class="context-picker__score" *ngIf="s.eval_score != null">
              {{ s.eval_score | number:'1.0-0' }}
            </span>
          </label>
        </div>
      </ng-container>
      <ng-template #noOrganizer>
        <div class="no-organizer-container">
          <h2>Agent has no memory context..</h2>
          <p>See agents with memory from Demo 3 onwards.</p>
        </div>
      </ng-template>
    </div>
  `,
})
export class OrganizerComponent implements OnDestroy {
  /** Bound from AgentScreen: when true, show static cached organizer surface instead of waiting on live gateway. */
  runCachedMessages = input(false);

  openItems = new Set<number>();
  preconfiguredRoutes = Object.values(PRECONFIGURED_ROUTES) as any;
  isOrganizerAvailable: boolean = false;

  responseNode: any = null;

  private demoService = inject(DemoService);
  private gateway = inject(GatewayService);
  private cdr = inject(ChangeDetectorRef);
  protected plannerAgentGuid: string | null = null;
  private chatSub: Subscription | null = null;

  protected isOrganizerAgentGettingRoutes = false;
  protected isLeadershipMode = false;

  /** User-checked past scenario IDs for context injection. */
  selectedContextIds = new Set<string>();
  /** Signal wrapper for the template binding on A2UI controller. */
  selectedContextIdsList = signal<string[]>([]);
  /** Past scenarios available for context selection. */
  pastScenarios: OrganizerScenario[] = [];

  constructor() {
    this.chatSub = this.gateway.chat$.subscribe((msg: ChatMessage) => {
      if (msg.guid === this.plannerAgentGuid && msg.result && msg.result.surfaceUpdate) {
        this.responseNode = msg.result;
        this.isOrganizerAgentGettingRoutes = false;

        // Extract scenario IDs from the A2UI surface for the context picker.
        this.extractScenariosFromSurface(msg.result);
        this.cdr.markForCheck();
      }
    });

    effect(() => {
      const activeDemoKey = this.demoService.activeDemo();
      const activeDemo = DEMO_CONFIG[activeDemoKey] as any;
      this.isOrganizerAvailable =
        activeDemo.agent === 'planner_with_memory' ||
        activeDemo.agent === 'simulator_with_failure' ||
        activeDemo.agent === 'leadership_planner_with_memory';
      this.isLeadershipMode = activeDemo.agent === 'leadership_planner_with_memory';

      if (this.runCachedMessages()) {
        this.responseNode = CACHED_ORGANIZER_MESSAGE.data;
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
          this.plannerAgentGuid = newPlannerAgent;
          this.gateway.sendBroadcast(prompt, [newPlannerAgent], true, true);
          this.cdr.markForCheck();
        }).catch(err => console.error("Failed to add agent", err));
      }
      this.cdr.markForCheck();
    });
  }

  ngOnDestroy(): void {
    this.chatSub?.unsubscribe();
    this.cleanupPreviousAgent();
  }

  /** Remove the previously spawned agent session to avoid orphaned backend sessions. */
  private cleanupPreviousAgent(): void {
    if (this.plannerAgentGuid) {
      this.gateway.removeAgent(this.plannerAgentGuid);
      this.plannerAgentGuid = null;
    }
  }

  toggle(index: number): void {
    if (this.openItems.has(index)) {
      this.openItems.delete(index);
    } else {
      this.openItems.add(index);
    }
  }

  onOrganizerClick(): void { }

  logStripClassic(e: any, route: any): void {
    window.dispatchEvent(
      new CustomEvent('gateway:routeGeojson', { detail: { geojson: route.route_data } }),
    );
  }

  async runSimulation(e: any, name: string): Promise<void> {
    e.stopPropagation();
    const activeDemo = DEMO_CONFIG[this.demoService.activeDemo()] as any;
    const isLeadership = activeDemo?.agent?.includes('leadership') ?? false;
    const agentType = isLeadership ? 'leadership_planner_with_memory' : 'planner_with_memory';
    const newPlannerAgent = await this.gateway.addAgent(agentType);
    this.plannerAgentGuid = newPlannerAgent;

    // Embed selected context scenario IDs for the planner.
    const ids = Array.from(this.selectedContextIds);
    const contextSuffix = isLeadership && ids.length > 0
      ? ` using context from scenarios ${ids.join(', ')}`
      : '';
    this.gateway.sendBroadcast(`Run Simulation for ${name}${contextSuffix}`, [newPlannerAgent]);
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
   *
   * The organizer agent's emit_scenario_list_a2ui produces a surface with
   * components whose `data` carries `scenario_id`, `title`, and `eval_score`.
   * We scan the components tree for these to populate the context picker.
   */
  private extractScenariosFromSurface(result: any): void {
    const scenarios: OrganizerScenario[] = [];
    const seen = new Set<string>();

    const surfaceUpdate = result?.surfaceUpdate;
    if (!surfaceUpdate?.components) return;

    for (const comp of surfaceUpdate.components) {
      this.walkComponentTree(comp, scenarios, seen);
    }

    this.pastScenarios = scenarios;
  }

  /** Recursively walk the A2UI component tree to find scenario metadata. */
  private walkComponentTree(
    comp: any,
    out: OrganizerScenario[],
    seen: Set<string>,
  ): void {
    if (!comp) return;

    // Check for scenario data in this component's data bag.
    const data = comp.data ?? comp.component?.data;
    if (data?.scenario_id && typeof data.scenario_id === 'string' && !seen.has(data.scenario_id)) {
      seen.add(data.scenario_id);
      out.push({
        scenario_id: data.scenario_id,
        title: data.title ?? data.name,
        eval_score: data.eval_score ?? data.score,
        created_at: data.created_at,
      });
    }

    // Recurse into children.
    const children = comp.children ?? comp.component?.children;
    if (Array.isArray(children)) {
      for (const child of children) {
        if (typeof child === 'string') continue; // ref id, skip
        this.walkComponentTree(child, out, seen);
      }
    }
  }
}

