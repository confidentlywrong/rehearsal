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
 * Changed for The Rehearsal: notify AgentRuntimeControlsService when demo runtime flags update, clear stale currentAgent on reset, remove synthetic route/message fallback injection for 100% SSOT, and handle sendBroadcast failures cleanly.
 */

import { Injectable } from '@angular/core';
import { DEMO_CONFIG } from '../../../../../demo-config';
import type { ChatAgent } from '../../../../../types';
import {
  SECURE_AGENT_PROMPT,
  UNSECURE_AGENT_PROMPT,
} from '../../../../../../constants';
import { simulationState } from '../../../../../simulation-state';
import type { AgentScreenComponent } from '../agent-screen.component';

@Injectable()
export class AgentDemoSessionService {
  private _lifecycleGen = 0;

  dispose(): void {
    ++this._lifecycleGen;
  }

  async runDemoLifecycle(host: AgentScreenComponent): Promise<void> {
    const gen = ++this._lifecycleGen;

    host.stopCachedDataStream();

    host.onSimFinished();
    host.resetSimulationStatistics();

    host.removeAllActiveAgents();

    host.gateway.removeCurrentSimulationId();

    host.showSimPanel = false;
    host.currentDemoCachedRunCount = 0;

    window.dispatchEvent(new CustomEvent('sim:fixError'));
    window.dispatchEvent(new CustomEvent('sim:removeRunnerThoughts'));

    if (host.errorMessageInterval) clearInterval(host.errorMessageInterval);

    host.isSecuringAgent = false;
    host.currentAgent = null;
    host.gateway.resetDemoChatPipelineState();
    host.chatDisplay.clearForDemoReset();
    host.chatMessages = [];
    host.cdr.markForCheck();

    window.dispatchEvent(new CustomEvent('hud:removeAllPaths'));

    host.resetCounter = host.demoService.reset();

    window.dispatchEvent(new CustomEvent('sim:reset'));

    if (host.agents['tick_agent']) host.agents['tick_agent'] = null;

    const activeDemoKey = host.demoService.activeDemo();
    const activeDemo = DEMO_CONFIG[activeDemoKey];

    host.isExpanded = activeDemoKey !== 'Sandbox';
    host.tabBtnWide = false;
    host.activeTab = 'agent';

    host.isSecurityDemo = activeDemo.isSecurityDemo || false;
    host.isBuildAgentsDemo = activeDemo.isBuildAgentsDemo || false;
    host.isIntentToInfrastructureDemo = activeDemo.isIntentToInfrastructureDemo || false;
    host.activeDemoHasRecordingConfig = !!activeDemo.recordingConfig;
    if (!host.activeDemoHasRecordingConfig) {
      host.runCachedMessages = false;
      host.stopCachedDataStream();
    }

    host.cachedMessageTimeScale = activeDemo.recordingConfig
      ? host.clampSnapCachedMessageTimeScale(activeDemo.recordingConfig.timeScale ?? 1)
      : 1;
    host.notifyRuntimeControlsUpdate();

    // Respect an explicit Raw chat-filter tier (so demo switches don't
    // silently strip load_skill events the user asked to see). For
    // Leadership/All tiers fall back to the per-demo default.
    if (host.filterSettings.chatFilterTier !== 'raw') {
      host.filterSettings.showLoadSkills = host.isBuildAgentsDemo;
    }
    host.gateway.setFilterSettings({ showLoadSkills: host.filterSettings.showLoadSkills });

    await host.onInitAgent(activeDemo.agent);

    if (this._lifecycleGen !== gen) return;

    Object.entries(host.agents).forEach(([agentType, guid]) => {
      if (activeDemo.agent !== agentType && guid) {
        host.gateway.removeAgent(guid);
        host.agents[agentType] = null;
      }
    });

    host.isAgentWorking = false;

    const sessionId = host.agents[activeDemo.agent];
    if (!sessionId && !host.runCachedMessages) return;

    host.currentAgent = {
      agentType: activeDemo.agent as ChatAgent,
      sessionId: sessionId ?? '',
    };

    if (activeDemoKey === '5b') {
      if (this._lifecycleGen !== gen) return;
      window.dispatchEvent(new CustomEvent('sim:giveRunnerThoughts'));
      host.applyDemoSessionCloudAssistShell(true);
    }

    if (activeDemoKey === '7b') {
      host.applyDemoSessionSecureShell(true);

      if (!host.runCachedMessages && host.currentAgent) {
        host.isSecuringAgent = true;
        const guid = host.agents[host.currentAgent.agentType];
        if (guid) {
          void host.gateway
            .sendBroadcast(SECURE_AGENT_PROMPT, [guid], true)
            .catch((err) => {
              console.error('Failed to enable secure agent mode:', err);
              if (this._lifecycleGen !== gen) return;
              host.isSecuringAgent = false;
              host.cdr.markForCheck();
            });
        }
      }
    } else if (activeDemoKey === '7a') {
      host.applyDemoSessionSecureShell(false);

      if (!host.runCachedMessages && host.currentAgent) {
        host.isSecuringAgent = true;
        const guid = host.agents[host.currentAgent.agentType];
        if (guid) {
          void host.gateway
            .sendBroadcast(UNSECURE_AGENT_PROMPT, [guid], true)
            .catch((err) => {
              console.error('Failed to disable secure agent mode:', err);
              if (this._lifecycleGen !== gen) return;
              host.isSecuringAgent = false;
              host.cdr.markForCheck();
            });
        }
      }
    }

    if (this._lifecycleGen !== gen) return;

    // Keep the 3D viewport and chat thread honestly empty until live gateway
    // messages or user-triggered cached stream replay arrive.
    window.dispatchEvent(new CustomEvent('gateway:routeGeojson', { detail: { geojson: undefined } }));

    if (activeDemo.promptPlaceholder) {
      if (host.chatTextAreaRef) {
        host.chatTextAreaRef.nativeElement.value = activeDemo.promptPlaceholder;
      }
      host.chatInput = activeDemo.promptPlaceholder;
      if (host.chatTextAreaRef) {
        host.autoResize();
      }
    } else {
      if (host.chatTextAreaRef) {
        host.chatTextAreaRef.nativeElement.value = '';
      }
      host.chatInput = '';
      if (host.chatTextAreaRef) {
        host.autoResize();
      }
    }

    simulationState.deactivate();
    host.cdr.markForCheck();
  }
}
