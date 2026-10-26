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

import { inject, Injectable } from '@angular/core';
import { GatewayService } from '../../gateway.service';
import { AGENT_SCREEN_HOST, type AgentScreenHost } from '../ChatNavPanel/screens/AgentScreen/agent-screen-host.model';
import cacheRoute01 from '../../../../cached_routes/cache-1.json';
import cacheRoute02 from '../../../../cached_routes/cache-2.json';
import cacheRoute03 from '../../../../cached_routes/cache-3.json';
import type { A2uiActionPayload } from './a2ui-surface.model';

export interface A2uiActionDispatchContext {
  runCached: boolean;
  plannerAgentGuid?: string | null;
  /** Scenario IDs the user checked in the Organizer sidebar context picker. */
  selectedContextScenarioIds?: string[];
}

const CACHED_ROUTE_KEYS = [
  'dd82048d-914d-4bbc-99f8-d44c33c9834c',
  'seed-0003-east-side-explorer-i9j0k1l2',
  'seed-0004-grand-loop-m3n4o5p6',
] as const;

type CachedRouteSeed = (typeof CACHED_ROUTE_KEYS)[number];

const CACHED_ROUTES: Record<CachedRouteSeed, unknown> = {
  'dd82048d-914d-4bbc-99f8-d44c33c9834c': cacheRoute01,
  'seed-0003-east-side-explorer-i9j0k1l2': cacheRoute02,
  'seed-0004-grand-loop-m3n4o5p6': cacheRoute03,
};

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
  ): void {
    const handler = this.handlers(ctx)[name];
    if (handler) {
      handler(payload);
    }
  }

  isRegisteredAction(name: string): name is A2uiRegisteredActionName {
    return (A2uiActionsService.REGISTERED_NAMES as readonly string[]).includes(name);
  }

  isViewAction(name: string): name is A2uiViewActionName {
    return (A2UI_VIEW_ACTION_NAMES as readonly string[]).includes(name);
  }

  private handlers(ctx: A2uiActionDispatchContext): Record<A2uiRegisteredActionName, (p: A2uiActionPayload) => void> {
    return {
      run_simulation: () => {
        this.agentScreen.isAgentWorking = true;

        if (this.agentScreen.runCachedMessages) {
          this.agentScreen.runCachedDataStream();
          return;
        }
        // Live mode: `currentAgent` is only ever populated by the demo
        // session service (agent-demo-session.service.ts L99). In the live
        // gateway flow the Organizer spawns a planner and exposes its guid
        // via `ctx.plannerAgentGuid`, so prefer that when available.
        // Without this fallback the Re-run button silently no-ops because
        // currentAgent is null, leaving the button stuck in its loading
        // state.
        const target = ctx.plannerAgentGuid ?? this.agentScreen.currentAgent?.sessionId;
        if (!target) {
          console.warn('run_simulation: no planner target available (currentAgent and plannerAgentGuid both unset); aborting Re-run.');
          this.agentScreen.isAgentWorking = false;
          return;
        }
        const isLeadership =
          this.agentScreen.currentAgent?.agentType?.includes('leadership') ?? true;
        // Embed user-selected past scenario IDs when present so the
        // planner's get_selected_scenarios tool can retrieve them.
        const contextIds = ctx.selectedContextScenarioIds ?? [];
        const contextSuffix = contextIds.length > 0
          ? ` using context from scenarios ${contextIds.join(', ')}`
          : '';
        const prompt = isLeadership
          ? `Run the simulation for the leadership scenario with 10,000 stakeholders${contextSuffix}`
          : 'Run the simulation for a marathon in Las Vegas for 10,000 runners';
        this.gateway.sendBroadcast(prompt, [target], true);
      },
      run_simulation_with_llm_runners: () => {
        this.agentScreen.isAgentWorking = true;

        if (this.agentScreen.runCachedMessages) {
          this.agentScreen.runCachedDataStream();
          return;
        }
        const target = ctx.plannerAgentGuid ?? this.agentScreen.currentAgent?.sessionId;
        if (!target) {
          console.warn('run_simulation_with_llm_runners: no planner target available; aborting.');
          this.agentScreen.isAgentWorking = false;
          return;
        }
        this.gateway.sendBroadcast(
          'Simulate your best plan with 10 LLM stakeholders on GKE',
          [target],
          true,
        );
      },
      run_simulation_with_route_data: () => {
        const target = ctx.plannerAgentGuid ?? this.agentScreen.currentAgent?.sessionId;
        if (!target) {
          console.warn('run_simulation_with_route_data: no planner target available; aborting.');
          return;
        }
        this.gateway.sendBroadcast(
          `Run simulation with the best route available`,
          [target],
          true,
        );
      },
      stop_simulation: () => undefined,
      show_route: (payload) => {
        // Update the 3D map in BOTH cached and live modes so the map
        // tracks the selected scenario immediately (pre-2026-07-17
        // bug: live mode never dispatched, so the map stayed stuck on
        // demo-config.placeholderRoutes forever).
        //
        // Route resolution — HONEST, no fabricated data:
        //   * Marathon seed with a real cached Vegas polyline → use it.
        //   * Leadership scenario (seed-leadership-* or UUID) → dispatch
        //     `undefined` to CLEAR the map. Leadership scenarios have
        //     no geographic route; showing an arbitrary marathon
        //     polyline would be fake data. When a leadership scenario
        //     has real simulation data (stakeholder_reactions,
        //     simulation_timeline), that surfaces in the sidebar
        //     Organizer detail card via emit_scenario_detail_a2ui —
        //     the 3D map stays empty because leadership isn't
        //     geographic.
        //   * Unknown seed → also `undefined` (clear).
        const seed = (payload as { seed?: string } | undefined)?.seed;
        let geojson: unknown = undefined;
        if (seed && this.isCachedRouteSeed(seed)) {
          geojson = CACHED_ROUTES[seed];
        }
        window.dispatchEvent(
          new CustomEvent('gateway:routeGeojson', {
            detail: { geojson },
          }),
        );

        if (ctx.runCached) return;

        // Live mode: also poke the agent so the sidebar Organizer
        // renders the scenario detail card (stakeholder graph,
        // ScoreBars, findings, real simulation report when
        // available). The `__list__` sentinel is a UI-only Back
        // button — the agent's classifier maps it to
        // emit_scenario_list_a2ui, no map action needed.
        if (!ctx.plannerAgentGuid) {
          console.warn('show_route: plannerAgentGuid not yet available, skipping agent broadcast.');
          return;
        }
        this.gateway.sendBroadcast(
          `Get the route for the seed ${(payload as { seed?: unknown } | undefined)?.seed}`,
          [ctx.plannerAgentGuid],
          true,
        );
      },
    };
  }

  private isCachedRouteSeed(seed: string): seed is CachedRouteSeed {
    return (CACHED_ROUTE_KEYS as readonly string[]).includes(seed);
  }
}
