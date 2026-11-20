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
 * Changed for The Rehearsal: return dispatch status to avoid stuck loading buttons, resolve non-empty planner session targets across live/cached flows, and remove static cached route bundles and synthetic Las Vegas fallback for 100% SSOT.
 */

import { inject, Injectable } from '@angular/core';
import { GatewayService } from '../../gateway.service';
import { AGENT_SCREEN_HOST, type AgentScreenHost } from '../ChatNavPanel/screens/AgentScreen/agent-screen-host.model';
import type { A2uiActionPayload } from './a2ui-surface.model';

export interface A2uiActionDispatchContext {
  runCached: boolean;
  plannerAgentGuid?: string | null;
  /** Scenario IDs the user checked in the Organizer sidebar context picker. */
  selectedContextScenarioIds?: string[];
}

export type A2uiRegisteredActionName =
  | 'run_simulation'
  | 'run_simulation_with_llm_runners'
  | 'run_simulation_with_route_data'
  | 'stop_simulation'
  | 'show_route';

/**
 * View-only actions that the controller component handles inline by mutating
 * local UI state (e.g. toggling card expansion). They never go through the
 * gateway dispatch path, but we still register their names here so:
 *   1. There is a single source of truth for the magic strings.
 *   2. `isViewAction()` can replace ad-hoc string comparisons.
 *   3. Adding a new view action is a one-line, type-checked change.
 */
export type A2uiViewActionName = 'open_card' | 'organizer_show_scorecard';

export const A2UI_VIEW_ACTION_NAMES: readonly A2uiViewActionName[] = [
  'open_card',
  'organizer_show_scorecard',
] as const;

/** Provide on `A2uiControllerComponent` only — requires `AgentScreenComponent` from an ancestor injector. */
@Injectable()
export class A2uiActionsService {
  private readonly gateway = inject(GatewayService);
  private readonly agentScreen = inject(AGENT_SCREEN_HOST);

  private static readonly REGISTERED_NAMES: readonly A2uiRegisteredActionName[] = [
    'run_simulation',
    'run_simulation_with_llm_runners',
    'run_simulation_with_route_data',
    'stop_simulation',
    'show_route',
  ];

  dispatchRegisteredAction(
    name: A2uiRegisteredActionName,
    payload: A2uiActionPayload,
    ctx: A2uiActionDispatchContext,
  ): boolean {
    const handler = this.handlers(ctx)[name];
    return handler ? handler(payload) : false;
  }

  isRegisteredAction(name: string): name is A2uiRegisteredActionName {
    return (A2uiActionsService.REGISTERED_NAMES as readonly string[]).includes(name);
  }

  isViewAction(name: string): name is A2uiViewActionName {
    return (A2UI_VIEW_ACTION_NAMES as readonly string[]).includes(name);
  }

  private resolvePlannerTarget(ctx: A2uiActionDispatchContext): string | undefined {
    const explicit = typeof ctx.plannerAgentGuid === 'string' ? ctx.plannerAgentGuid.trim() : '';
    if (explicit) return explicit;
    const currentSession = this.agentScreen.currentAgent?.sessionId?.trim();
    if (currentSession) return currentSession;
    const agentType = this.agentScreen.currentAgent?.agentType;
    const mapped = agentType ? this.agentScreen.agents?.[agentType]?.trim() : '';
    return mapped || undefined;
  }

  private handlers(ctx: A2uiActionDispatchContext): Record<A2uiRegisteredActionName, (p: A2uiActionPayload) => boolean> {
    return {
      run_simulation: () => {
        if (this.agentScreen.runCachedMessages) {
          this.agentScreen.isAgentWorking = true;
          this.agentScreen.runCachedDataStream();
          return true;
        }
        const target = this.resolvePlannerTarget(ctx);
        if (!target) {
          console.warn('run_simulation: no planner target available (currentAgent and plannerAgentGuid both unset); aborting Re-run.');
          this.agentScreen.isAgentWorking = false;
          return false;
        }
        this.agentScreen.isAgentWorking = true;
        // Embed user-selected past scenario IDs when present so the
        // planner's get_selected_scenarios tool can retrieve them.
        const contextIds = ctx.selectedContextScenarioIds ?? [];
        const contextSuffix = contextIds.length > 0
          ? ` using context from scenarios ${contextIds.join(', ')}`
          : '';
        const prompt = `Run the simulation for the leadership scenario with 10,000 stakeholders${contextSuffix}`;
        void this.gateway.sendBroadcast(prompt, [target], true).catch((err) => {
          console.error('run_simulation broadcast failed:', err);
          this.agentScreen.isAgentWorking = false;
        });
        return true;
      },
      run_simulation_with_llm_runners: () => {
        if (this.agentScreen.runCachedMessages) {
          this.agentScreen.isAgentWorking = true;
          this.agentScreen.runCachedDataStream();
          return true;
        }
        const target = this.resolvePlannerTarget(ctx);
        if (!target) {
          console.warn('run_simulation_with_llm_runners: no planner target available; aborting.');
          this.agentScreen.isAgentWorking = false;
          return false;
        }
        this.agentScreen.isAgentWorking = true;
        void this.gateway.sendBroadcast(
          'Simulate your best plan with 10 LLM stakeholders on GKE',
          [target],
          true,
        ).catch((err) => {
          console.error('run_simulation_with_llm_runners broadcast failed:', err);
          this.agentScreen.isAgentWorking = false;
        });
        return true;
      },
      run_simulation_with_route_data: () => {
        if (this.agentScreen.runCachedMessages) {
          this.agentScreen.isAgentWorking = true;
          this.agentScreen.runCachedDataStream();
          return true;
        }
        const target = this.resolvePlannerTarget(ctx);
        if (!target) {
          console.warn('run_simulation_with_route_data: no planner target available; aborting.');
          this.agentScreen.isAgentWorking = false;
          return false;
        }
        this.agentScreen.isAgentWorking = true;
        void this.gateway.sendBroadcast(
          'Run simulation with the best route available',
          [target],
          true,
        ).catch((err) => {
          console.error('run_simulation_with_route_data broadcast failed:', err);
          this.agentScreen.isAgentWorking = false;
        });
        return true;
      },
      stop_simulation: () => false,
      show_route: (payload) => {
        const rawSeed = (payload as { seed?: unknown } | undefined)?.seed;
        const unwrappedSeed =
          rawSeed && typeof rawSeed === 'object' && 'literalString' in (rawSeed as Record<string, unknown>)
            ? (rawSeed as { literalString?: unknown }).literalString
            : rawSeed;
        const seed = typeof unwrappedSeed === 'string' ? unwrappedSeed.trim() : undefined;
        // Clear any stale route on the viewport until the gateway delivers live data.
        window.dispatchEvent(
          new CustomEvent('gateway:routeGeojson', {
            detail: { geojson: undefined },
          }),
        );

        if (ctx.runCached) return false;

        const targetGuid = typeof ctx.plannerAgentGuid === 'string' ? ctx.plannerAgentGuid.trim() : '';
        if (!targetGuid || !seed) {
          console.warn('show_route: plannerAgentGuid or seed not available, skipping agent broadcast.');
          return false;
        }
        void this.gateway.sendBroadcast(
          `Get the route for the seed ${seed}`,
          [targetGuid],
          true,
        ).catch((err) => {
          console.error('show_route broadcast failed:', err);
        });
        return false;
      },
    };
  }
}
