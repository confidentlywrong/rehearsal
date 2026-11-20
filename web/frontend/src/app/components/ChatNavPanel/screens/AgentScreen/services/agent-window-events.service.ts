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
 * Changed for The Rehearsal: notify AgentRuntimeControlsService when recording dump availability changes.
 */

import { Injectable, NgZone, inject } from '@angular/core';
import {
  AGENT_GATEWAY_MSG_DUMP_CHANGED,
  agentGatewayMsgDumpLineCount,
} from '../../../../../../../agent-gateway-message-dump';
import type { PathEntry, SyncPayload } from '../agent-screen.types';
import { AgentSimulationStatsService } from './agent-simulation-stats.service';
import { AgentPanelTabExpansionService } from './agent-panel-tab-expansion.service';

/** Window/HUD bindings for AgentScreen (keeps listener wiring out of the component). */
export interface AgentWindowEventsScreenBridge {
  agentGatewayMsgDumpCanDownload: boolean;
  paths: PathEntry[];
  selectedId: number | null;
  applyCameraIntroComplete(): void;
  onSyncPayload(d: SyncPayload): void;
  notifyRuntimeControlsUpdate?(): void;
}

@Injectable()
export class AgentWindowEventsService {
  private readonly ngZone = inject(NgZone);
  private readonly sim = inject(AgentSimulationStatsService);
  private readonly panelTab = inject(AgentPanelTabExpansionService);

  private screen!: AgentWindowEventsScreenBridge & { cdr: { markForCheck(): void } };
  private markScreen!: () => void;

  bind(screen: AgentWindowEventsScreenBridge & { cdr: { markForCheck(): void } }): void {
    this.screen = screen;
    this.markScreen = () => screen.cdr.markForCheck();
  }

  seedGatewayDumpFlag(): void {
    this.screen.agentGatewayMsgDumpCanDownload = agentGatewayMsgDumpLineCount() > 0;
  }

  private readonly onAgentGatewayMsgDumpChanged = (e: Event): void => {
    const count = (e as CustomEvent<{ count?: number }>).detail?.count ?? 0;
    this.ngZone.run(() => {
      this.screen.agentGatewayMsgDumpCanDownload = count > 0;
      this.screen.notifyRuntimeControlsUpdate?.();
      this.markScreen();
    });
  };

  private readonly onSync = (e: Event): void => {
    const d = (e as CustomEvent).detail as SyncPayload;
    this.ngZone.run(() => {
      this.screen.onSyncPayload(d);
      this.markScreen();
    });
  };

  private readonly onRouteIntroComplete = (): void => {
    this.panelTab.onRouteIntroComplete();
  };

  private readonly onCameraIntroComplete = (): void => {
    this.ngZone.run(() => {
      this.screen.applyCameraIntroComplete();
      this.markScreen();
    });
  };

  start(): void {
    window.addEventListener(
      AGENT_GATEWAY_MSG_DUMP_CHANGED,
      this.onAgentGatewayMsgDumpChanged as EventListener,
    );
    window.addEventListener('hud:sync', this.onSync);
    window.addEventListener('viewport:followStopped', this.sim.onFollowStopped);
    window.addEventListener('viewport:cameraIntroComplete', this.onCameraIntroComplete);
    window.addEventListener('viewport:routeIntroComplete', this.onRouteIntroComplete);
    window.addEventListener('sim:finished', this.sim.onSimFinished);
    window.addEventListener('sim:raceStarted', this.sim.onSimRaceStarted);
    window.addEventListener('hud:updateSimRunner', this.sim.onHudUpdateSimRunnerForFinishers);
    window.addEventListener('sim:firstBatchAvgVelocity', this.sim.onFirstBatchAvgVelocity);
  }

  stop(): void {
    window.removeEventListener(
      AGENT_GATEWAY_MSG_DUMP_CHANGED,
      this.onAgentGatewayMsgDumpChanged as EventListener,
    );
    window.removeEventListener('hud:sync', this.onSync);
    window.removeEventListener('viewport:followStopped', this.sim.onFollowStopped);
    window.removeEventListener('viewport:cameraIntroComplete', this.onCameraIntroComplete);
    window.removeEventListener('viewport:routeIntroComplete', this.onRouteIntroComplete);
    window.removeEventListener('sim:finished', this.sim.onSimFinished);
    window.removeEventListener('sim:raceStarted', this.sim.onSimRaceStarted);
    window.removeEventListener('hud:updateSimRunner', this.sim.onHudUpdateSimRunnerForFinishers);
    window.removeEventListener('sim:firstBatchAvgVelocity', this.sim.onFirstBatchAvgVelocity);
  }
}
