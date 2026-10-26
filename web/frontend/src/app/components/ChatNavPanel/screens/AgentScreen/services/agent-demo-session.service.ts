/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 */

import { Injectable } from '@angular/core';
import { DEMO_CONFIG, PRECONFIGURED_ROUTES } from '../../../../../demo-config';
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
  async runDemoLifecycle(host: AgentScreenComponent): Promise<void> {
    const gen = ++this._lifecycleGen;
    void host.demoService.cachedMessagesToggle();

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
    if (!sessionId) return;

    host.currentAgent = {
      agentType: activeDemo.agent as ChatAgent,
      sessionId,
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
          host.gateway.sendBroadcast(
            SECURE_AGENT_PROMPT,
            [guid],
            true,
          );
        }
      }
    } else if (activeDemoKey === '7a') {
      host.applyDemoSessionSecureShell(false);

      if (!host.runCachedMessages && host.currentAgent) {
        host.isSecuringAgent = true;
        const guid = host.agents[host.currentAgent.agentType];
        if (guid) {
          host.gateway.sendBroadcast(
            UNSECURE_AGENT_PROMPT,
            [guid],
            true,
          );
        }
      }
    }

    if (this._lifecycleGen !== gen) return;

    // Preload the demo's placeholder route into the 3D map ONLY when
    // the demo's agent is a marathon planner. Leadership demos have
    // `placeholderRoutes: 'cachedRoute0X'` set in demo-config.ts as
    // pre-2026-07-17 marathon leftover — dispatching those preloads a
    // fake Vegas polyline that never corresponds to the selected
    // leadership scenario, and then leaves it stuck on the map until
    // the user clicks Show Scenario. Skipping the dispatch for
    // leadership agents keeps the map honestly empty until an actual
    // route arrives from a marathon flow (never, in this fork). See
    // a2ui-actions.service.ts show_route handler for the click-time
    // counterpart (also clears rather than fabricates).
    const agent = typeof activeDemo.agent === 'string' ? activeDemo.agent : '';
    const isLeadershipDemo = agent.startsWith('leadership_') || agent === 'simulator_with_failure';
    if (activeDemoKey !== 'Sandbox' && activeDemo.placeholderRoutes && !isLeadershipDemo) {
      const routeJson = PRECONFIGURED_ROUTES[activeDemo.placeholderRoutes] as {
        route_data?: unknown;
      };
      const geojson = routeJson.route_data ?? routeJson;

      window.dispatchEvent(new CustomEvent('gateway:routeGeojson', { detail: { geojson } }));
    } else if (isLeadershipDemo) {
      // Actively clear any stale route that may have been left on the
      // map by a prior demo. `undefined` geojson is the documented
      // "no route" signal the viewport-lookdev component honours.
      window.dispatchEvent(new CustomEvent('gateway:routeGeojson', { detail: { geojson: undefined } }));
    }

    if (activeDemo.placeholderAgentMessage) {
      const message = {
        ...activeDemo.placeholderAgentMessage,
        guid: host.currentAgent?.sessionId ?? '',
        speaker: (host.currentAgent?.agentType as string) ?? '',
      };

      host.chatMessages = [message];
      host.chatDisplay.processMessageForDisplay(host, message);
    }

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
