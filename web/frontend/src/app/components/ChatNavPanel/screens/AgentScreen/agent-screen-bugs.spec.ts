// Copyright 2026 Christian Haeussinger
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import '@angular/compiler';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  ElementRef,
  NgZone,
  provideZonelessChangeDetection,
  signal,
  ɵresolveComponentResources as resolveComponentResources,
} from '@angular/core';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { getTestBed, TestBed } from '@angular/core/testing';
import {
  BrowserTestingModule,
  platformBrowserTesting,
} from '@angular/platform-browser/testing';
import * as protobuf from 'protobufjs';
import { Subject } from 'rxjs';
import { MarkdownService } from 'ngx-markdown';
import { describe, expect, it, vi } from 'vitest';
import { formatPaceFromNormalizedAvgVelocity } from './agent-screen-formatters';
import { LogComponent } from '../Log/log.component';
import { StakeholderGraphCardComponent } from '../Log/stakeholder-graph-card.component';
import { TranslationDiagnosticCardComponent } from '../Log/translation-diagnostic-card.component';
import { OrganizerComponent } from '../Organizer/organizer.component';
import { DemoService } from '../../../DemoOverlay/demo.service';
import { GatewayService } from '../../../../gateway.service';
import { agentGateway } from '../../../../agent-gateway-updates';
import { AgentChatDisplayService, type AgentChatDisplayHost } from './services/agent-chat-display.service';
import { AgentDemoSessionService } from './services/agent-demo-session.service';
import { AgentScreenComponent } from './agent-screen.component';
import { AgentSimulationStatsService } from './services/agent-simulation-stats.service';
import { AgentPanelTabExpansionService } from './services/agent-panel-tab-expansion.service';
import { AgentGatewayAgentsService } from './services/agent-gateway-agents.service';
import { AgentWindowEventsService } from './services/agent-window-events.service';
import { AgentCachedGatewayReplayService } from './services/agent-cached-gateway-replay.service';
import { AgentRuntimeControlsService } from './agent-runtime-controls.service';
import { AgentChatComposerComponent } from './agent-chat-composer/agent-chat-composer.component';
import { BillingPageComponent } from '../../../../settings/billing/billing.page';
import { A2uiControllerComponent } from '../../../a2ui/a2-ui-controller.component';
import { A2uiTreeService } from '../../../a2ui/a2ui-tree.service';
import { AGENT_SCREEN_HOST } from './agent-screen-host.model';
import { Runner } from '../../../../runner';
import type { ChatMessage } from '../../../../gateway.service';
import { AgentMessageType } from '../../../../types';

describe('Frontend bug regressions', () => {
  it('rolls 59.9s pace cleanly to next minute instead of emitting ":60"', () => {
    // Choose avgVelocity so 60 / (avgVelocity * 6.2137) = 7 + 59.9 / 60 minutes (479.9 seconds -> rounds to 480s = 8:00)
    const targetMinutes = 479.9 / 60;
    const velocity = 60 / (targetMinutes * 6.2137);
    expect(formatPaceFromNormalizedAvgVelocity(velocity)).toBe('8:00');
    expect(formatPaceFromNormalizedAvgVelocity(0)).toBe('0:00');
    expect(formatPaceFromNormalizedAvgVelocity(Number.NaN)).toBe('0:00');
    expect(formatPaceFromNormalizedAvgVelocity(Number.POSITIVE_INFINITY)).toBe('0:00');
  });

  it('HTML-escapes untrusted fields in LogComponent.summary() and maps leadership speakers', () => {
    const log = new LogComponent(
      { chat$: new Subject<ChatMessage>() } as any,
      { markForCheck: vi.fn() } as any,
      { reset: () => 0, activeDemo: () => 'demo1' } as any,
    );

    const xssUserMsg: ChatMessage = {
      guid: 's1',
      speaker: 'user',
      text: '<img src=x onerror="alert(1)">',
      isUser: true,
      timestamp: new Date(),
      msgType: AgentMessageType.TEXT,
    };
    expect(log.summary(xssUserMsg)).toContain('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
    expect(log.summary(xssUserMsg)).not.toContain('<img');

    const xssInterviewMsg: ChatMessage = {
      guid: 's1',
      speaker: 'leadership_planner_with_memory',
      text: '',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_END,
      toolName: 'emit_stakeholder_interview',
      result: {
        stakeholder_interview: {
          stakeholder_id: '<script>evil()</script>',
          tone: '<b>hostile</b>',
          exchanges: [{}],
        },
      },
    };
    const interviewHtml = log.summary(xssInterviewMsg);
    expect(interviewHtml).toContain('&lt;script&gt;evil()&lt;/script&gt;');
    expect(interviewHtml).toContain('&lt;b&gt;hostile&lt;/b&gt;');
    expect(interviewHtml).not.toContain('<script>');
    expect(log.getLogDisplayName(xssInterviewMsg)).toBe('PLANNER');
    expect(log.getLogDisplayName({ ...xssInterviewMsg, speaker: undefined as unknown as string })).toBe('AGENT');
  });

  it('parses JSON-stringified payloads and hides fallback when payload is null in TranslationDiagnosticCardComponent', () => {
    const card = new TranslationDiagnosticCardComponent();
    card.payload = null;
    expect(card.receivedPayload).toBe(false);
    expect(card.hasValidDiagnostic).toBe(false);

    card.payload = JSON.stringify({
      translation_diagnostic: {
        gaps: [
          {
            stakeholder_id: 'vp_eng',
            stakeholder_label: 'VP Engineering',
            overall_gap_severity: 'high',
            suggested_reframing: 'Anchor in reliability SLOs.',
            trigger_phrases: [
              {
                phrase: 'Move fast on AI rollout',
                tick: 2,
                leader_intent: 'Accelerate adoption',
                stakeholder_decoded_as: 'Bypass safety gates',
                gap_type: 'System vs Lifeworld',
                severity: 'high',
              },
            ],
          },
        ],
        summary: 'High translation gap in Sprint 2.',
      },
    });
    expect(card.receivedPayload).toBe(true);
    expect(card.hasValidDiagnostic).toBe(true);
    expect(card.parsed?.gaps[0].stakeholder_label).toBe('VP Engineering');
  });

  it('extracts stakeholder nodeLabels from emit_stakeholder_graph and resets them on financial mode reset', () => {
    const display = new AgentChatDisplayService();
    const host: AgentChatDisplayHost = {
      cdr: { markForCheck: vi.fn() } as any,
      agents: {},
      currentAgent: { agentType: 'leadership_planner_with_eval', sessionId: 'sess-1' },
      chatMessages: [],
      chatInput: '',
      demoService: { activeDemo: () => 'demo3' } as any,
      filterSettings: { showToolCalls: true, showLoadSkills: true, chatFilterTier: 'all' },
      isSecuringAgent: false,
      isAgentWorking: true,
      finalToolCallMessage: null,
      errorMessageInterval: null,
      resetSimulationStatistics: vi.fn(),
      handleRaceStarted: vi.fn(),
      handleTickUpdate: vi.fn(),
      scrollChatToBottom: vi.fn(),
      autoResize: vi.fn(),
    };

    display.processMessageForDisplay(host, {
      guid: 'sess-1',
      speaker: 'leadership_planner_with_eval',
      text: '',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_END,
      toolName: 'emit_stakeholder_graph',
      result: {
        stakeholder_graph: {
          nodes: [
            { id: 'cfo_1', label: 'Elena Vance (CFO)' },
            { id: 'vp_ops', label: 'Marcus Chen (VP Ops)' },
          ],
          edges: [],
        },
      },
    });

    expect(display.nodeLabels).toEqual({
      cfo_1: 'Elena Vance (CFO)',
      vp_ops: 'Marcus Chen (VP Ops)',
    });

    display.processMessageForDisplay(host, {
      guid: 'sess-1',
      speaker: 'leadership_planner_with_eval',
      text: '',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_END,
      toolName: 'set_financial_modeling_mode',
      result: {},
    });

    expect(display.nodeLabels).toBeNull();
  });

  it('tracks checkoutErrorTier after checkout failure and rejects unsafe javascript: redirect URLs in BillingPageComponent', async () => {
    const createCheckoutSession = vi
      .fn()
      .mockResolvedValueOnce({ outcome: 'unavailable', provider: 'stub' })
      .mockResolvedValueOnce({ outcome: 'redirect', url: 'javascript:alert(1)', provider: 'stripe' });

    const page = new BillingPageComponent(
      { createCheckoutSession, listPlans: vi.fn() } as any,
      { openPortal: vi.fn() } as any,
      { fetchMe: vi.fn() } as any,
      { markForCheck: vi.fn() } as any,
    );
    page.me = { user: { plan_tier: 'free', email: 'test@example.com' } } as any;

    const proPlan = {
      tier: 'pro',
      monthly_token_cap: 1_000_000,
      max_concurrent_runs: 5,
      description: 'Pro tier',
    };

    await page.startCheckout(proPlan as any);
    expect(page.checkoutTier).toBeNull();
    expect(page.checkoutErrorTier).toBe('pro');
    expect(page.checkoutError).toContain('Self-serve upgrades are not yet enabled');

    await page.startCheckout(proPlan as any);
    expect(page.checkoutTier).toBeNull();
    expect(page.checkoutErrorTier).toBe('pro');
    expect(page.checkoutError).toBe('Invalid checkout redirect URL.');
  });

  it('reads Stripe checkout return banner from hash-location URLs in BillingPageComponent', async () => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: {
        ...window.location,
        search: '',
        hash: '#/settings/billing?checkout=success',
      },
    });
    const page = new BillingPageComponent(
      { createCheckoutSession: vi.fn(), listPlans: vi.fn().mockResolvedValue([]) } as any,
      { openPortal: vi.fn() } as any,
      { fetchMe: vi.fn().mockResolvedValue({ user: { plan_tier: 'pro', email: 'a@b.com' } }) } as any,
      { markForCheck: vi.fn() } as any,
    );
    await page.ngOnInit();
    expect(page.returnBanner?.kind).toBe('success');
  });

  it('guards A2uiController cycles, Slider range normalization, MultipleChoice selection, and non-sim button loading', () => {
    const dispatchSpy = vi.fn().mockReturnValue(false);
    const ctrl = Object.assign(Object.create(A2uiControllerComponent.prototype), {
      tree: new A2uiTreeService(),
      actions: {
        isViewAction: () => false,
        isRegisteredAction: () => true,
        dispatchRegisteredAction: dispatchSpy,
      },
      selectionsMap: signal(new Map<string, string[]>()),
      loadingButtonIds: signal(new Set<string>()),
      actionCtx: () => ({ runCached: false, selectedContextScenarioIds: [] }),
    }) as A2uiControllerComponent;

    const nodeA = { id: 'node-a', component: { Card: { child: 'node-b' } } } as any;
    const anc = ctrl.nextAncestors(undefined, nodeA);
    expect(ctrl.isCyclicNode(nodeA, anc)).toBe(true);

    // Slider range [50, 150] at value 100 -> 50%
    expect(ctrl.computeSliderInsetFromValues(50, 150, 100)).toBe('50%');

    // MultipleChoice maxAllowedSelections = 1 replaces selection instead of silently ignoring
    const mcNode = {
      id: 'mc-1',
      component: {
        MultipleChoice: {
          maxAllowedSelections: { literalNumber: 1 },
          options: [
            { label: { literalString: 'Option A' }, value: { literalString: 'a' } },
            { label: { literalString: 'Option B' }, value: { literalString: 'b' } },
          ],
        },
      },
    } as any;
    const stopSpy = vi.fn();
    ctrl.handleOptionClick({ stopPropagation: stopSpy } as any, mcNode, mcNode.component.MultipleChoice.options[0]);
    expect(ctrl.isOptionSelected(mcNode, mcNode.component.MultipleChoice.options[0])).toBe(true);
    ctrl.handleOptionClick({ stopPropagation: stopSpy } as any, mcNode, mcNode.component.MultipleChoice.options[1]);
    expect(ctrl.isOptionSelected(mcNode, mcNode.component.MultipleChoice.options[0])).toBe(false);
    expect(ctrl.isOptionSelected(mcNode, mcNode.component.MultipleChoice.options[1])).toBe(true);
    expect(dispatchSpy).not.toHaveBeenCalled();

    // Failed/aborted simulation dispatch (returning false) must not lock button in loadingButtonIds
    const runBtn = {
      id: 'btn-run',
      component: { Button: { action: { name: 'run_simulation_with_route' } } },
    } as any;
    ctrl.handleButtonClick({ stopPropagation: stopSpy } as any, runBtn);
    expect(dispatchSpy).toHaveBeenCalledTimes(1);
    expect(ctrl.loadingButtonIds().has('btn-run')).toBe(false);
  });

  it('suppresses A2UI surface mutations during isSecuringAgent and resolves active spinners on TOOL_ERROR/MODEL_ERROR', () => {
    const display = new AgentChatDisplayService();
    const host: AgentChatDisplayHost = {
      cdr: { markForCheck: vi.fn() } as any,
      agents: { leadership_planner: 'sess-live' },
      currentAgent: { agentType: 'leadership_planner', sessionId: 'sess-live' },
      chatMessages: [],
      chatInput: '',
      demoService: { activeDemo: () => '1' } as any,
      filterSettings: { showToolCalls: true, showLoadSkills: true, chatFilterTier: 'all' },
      isSecuringAgent: true,
      isAgentWorking: true,
      finalToolCallMessage: null,
      errorMessageInterval: null,
      resetSimulationStatistics: vi.fn(),
      handleRaceStarted: vi.fn(),
      handleTickUpdate: vi.fn(),
      scrollChatToBottom: vi.fn(),
      autoResize: vi.fn(),
    };

    display.processMessageForDisplay(host, {
      guid: 'sess-live',
      speaker: 'leadership_planner',
      text: '',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_END,
      result: { surfaceUpdate: { surfaceId: 'sec-surf', components: [] } },
    });
    expect(display.displayItems.length).toBe(0);

    host.isSecuringAgent = false;
    display.processMessageForDisplay(host, {
      guid: 'sess-live',
      speaker: 'leadership_planner',
      text: '',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_START,
      toolName: 'map_stakeholders',
    });
    expect(display.displayItems.length).toBe(1);
    expect((display.displayItems[0] as any).done).toBeUndefined();

    display.processMessageForDisplay(host, {
      guid: 'sess-live',
      speaker: 'leadership_planner',
      text: 'Upstream timeout',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_ERROR,
      toolName: 'map_stakeholders',
    });
    expect((display.displayItems[0] as any).done).toBe(true);
    expect((display.displayItems[0] as any).error).toBe(true);
    expect(host.isAgentWorking).toBe(false);

    host.isAgentWorking = true;
    display.processMessageForDisplay(host, {
      guid: 'sess-live',
      speaker: 'leadership_planner',
      text: 'Quota exceeded',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.MODEL_ERROR,
    });
    expect(host.isAgentWorking).toBe(false);
    expect(display.displayItems.length).toBe(3);
  });

  it('initializes currentAgent and promptPlaceholder in Cached mode in AgentDemoSessionService', async () => {
    const svc = new AgentDemoSessionService();
    const display = new AgentChatDisplayService();
    const host: any = {
      demoService: {
        cachedMessagesToggle: () => 0,
        reset: () => 1,
        activeDemo: () => '1',
      },
      stopCachedDataStream: vi.fn(),
      onSimFinished: vi.fn(),
      resetSimulationStatistics: vi.fn(),
      removeAllActiveAgents: vi.fn(),
      gateway: {
        removeCurrentSimulationId: vi.fn(),
        resetDemoChatPipelineState: vi.fn(),
        setFilterSettings: vi.fn(),
        removeAgent: vi.fn(),
      },
      chatDisplay: display,
      chatMessages: [],
      cdr: { markForCheck: vi.fn() },
      agents: {},
      runCachedMessages: true,
      activeDemoHasRecordingConfig: true,
      clampSnapCachedMessageTimeScale: (n: number) => n,
      notifyRuntimeControlsUpdate: vi.fn(),
      filterSettings: { showToolCalls: true, showLoadSkills: false, chatFilterTier: 'leadership' },
      onInitAgent: vi.fn().mockResolvedValue(undefined),
      chatInput: '',
    };

    await svc.runDemoLifecycle(host);
    expect(host.currentAgent).toBeTruthy();
    expect(host.currentAgent.sessionId).toBe('');
    expect(host.chatInput.length).toBeGreaterThan(0);
  });

  it('rolls Runner.getPace() 59.9s cleanly to next minute without ":60"', () => {
    const fakeScene = { add: vi.fn(), remove: vi.fn() } as any;
    const fakePath = {
      getTotalLength: () => 1000,
      getPositionAt: () => ({ x: 0, y: 0, z: 0 }),
      getTangentAt: () => ({ x: 1, y: 0, z: 0 }),
      allStations: [],
    } as any;
    const r = new Runner('sim-r1', {
      scene: fakeScene,
      path: fakePath,
      effectiveMph: 60 / (479.9 / 60),
      simDistanceIntegrator: 0.1,
    });
    expect(r.getPace()).toBe('8:00');
  });

  it('surfaces spawnError and catches addAgent/sendBroadcast failures in OrganizerComponent.runSimulation()', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const gateway = {
      chat$: new Subject<ChatMessage>(),
      addAgent: vi.fn().mockRejectedValue(new Error('gateway offline')),
      sendBroadcast: vi.fn().mockResolvedValue(undefined),
    };
    const demoService = {
      activeDemo: signal('1'),
    };
    const cdr = { markForCheck: vi.fn() };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        { provide: GatewayService, useValue: gateway },
        { provide: DemoService, useValue: demoService },
        { provide: ChangeDetectorRef, useValue: cdr },
      ],
    });
    const org = TestBed.runInInjectionContext(() => new OrganizerComponent());

    try {
      await org.runSimulation({ stopPropagation: vi.fn() }, 'TestPlan');
      expect(org.spawnError()).toContain('gateway offline');
      expect((org as unknown as Record<string, unknown>)['openItems']).toBeUndefined();
      expect((org as unknown as Record<string, unknown>)['preconfiguredRoutes']).toBeUndefined();
    } finally {
      org.ngOnDestroy();
      TestBed.resetTestingModule();
    }
  });

  it('dispatches { geojson: undefined } and clears chatMessages in AgentDemoSessionService.runDemoLifecycle()', async () => {
    const svc = new AgentDemoSessionService();
    const display = new AgentChatDisplayService();
    const routeEvents: Array<{ geojson: unknown }> = [];
    const listener = (e: Event) => routeEvents.push((e as CustomEvent).detail);
    window.addEventListener('gateway:routeGeojson', listener);

    const host: any = {
      demoService: {
        cachedMessagesToggle: () => 0,
        reset: () => 1,
        activeDemo: () => '1',
      },
      stopCachedDataStream: vi.fn(),
      onSimFinished: vi.fn(),
      resetSimulationStatistics: vi.fn(),
      removeAllActiveAgents: vi.fn(),
      gateway: {
        removeCurrentSimulationId: vi.fn(),
        resetDemoChatPipelineState: vi.fn(),
        setFilterSettings: vi.fn(),
        removeAgent: vi.fn(),
      },
      chatDisplay: display,
      chatMessages: [{ guid: 'stale' }],
      cdr: { markForCheck: vi.fn() },
      agents: { leadership_planner: 'sess-1' },
      runCachedMessages: true,
      activeDemoHasRecordingConfig: true,
      clampSnapCachedMessageTimeScale: (n: number) => n,
      notifyRuntimeControlsUpdate: vi.fn(),
      filterSettings: { showToolCalls: true, showLoadSkills: false, chatFilterTier: 'leadership' },
      onInitAgent: vi.fn().mockResolvedValue(undefined),
      chatInput: '',
    };

    try {
      await svc.runDemoLifecycle(host);
      expect(host.chatMessages).toEqual([]);
      expect(routeEvents).toContainEqual({ geojson: undefined });
    } finally {
      window.removeEventListener('gateway:routeGeojson', listener);
    }
  });

  it('preserves user-submitted prompt in AgentScreenComponent.onSend() and clears timers on ngOnDestroy()', () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    vi.useFakeTimers();
    try {
      const clearIntervalSpy = vi.spyOn(globalThis, 'clearInterval');
      const gateway = {
        chat$: new Subject<ChatMessage>(),
        sendBroadcast: vi.fn().mockResolvedValue(undefined),
        addAgent: vi.fn().mockResolvedValue('sess-user'),
        removeAgent: vi.fn(),
        setFilterSettings: vi.fn(),
        getOrganizerGuid: vi.fn().mockReturnValue(null),
      };
      const demoService = {
        activeDemo: signal('1'),
        cachedMessagesToggle: signal(0),
        reset: signal(0),
        modeSwitchLabel: signal(null),
      };
      const panelTab = {
        isExpanded: false,
        activeTab: 'agent' as const,
        tabBtnWide: false,
        showTabLabels: false,
        connect: vi.fn(),
        dispose: vi.fn(),
      };
      const chatDisplay = new AgentChatDisplayService();
      const disposeSpy = vi.spyOn(chatDisplay, 'dispose');
      const runtimeControls = {
        register: vi.fn(),
        unregister: vi.fn(),
        notifyUpdate: vi.fn(),
      };
      const demoSessionMock = {
        runDemoLifecycle: vi.fn().mockResolvedValue(undefined),
        dispose: vi.fn(),
      };
      TestBed.configureTestingModule({
        providers: [
          provideZonelessChangeDetection(),
          { provide: AgentSimulationStatsService, useValue: { showSimPanel: false, isSimulationRunning: false, averagePace: '0:00', isFollowingLeader: false, runnersFinishedAwaitingHud: new Set<string>(), connect: vi.fn(), reset: vi.fn(), dispose: vi.fn() } },
          { provide: AgentPanelTabExpansionService, useValue: panelTab },
          { provide: AgentGatewayAgentsService, useValue: { agents: { leadership_planner: 'sess-user' }, initializingAgents: {}, connect: vi.fn(), removeAllActiveAgents: vi.fn() } },
          { provide: AgentWindowEventsService, useValue: { bind: vi.fn(), seedGatewayDumpFlag: vi.fn(), start: vi.fn(), stop: vi.fn() } },
          { provide: AgentRuntimeControlsService, useValue: runtimeControls },
          { provide: DemoService, useValue: demoService },
          { provide: AgentChatDisplayService, useValue: chatDisplay },
          { provide: AgentDemoSessionService, useValue: demoSessionMock },
          { provide: AgentCachedGatewayReplayService, useValue: { stopCachedDataStream: vi.fn(), startCachedDataStream: vi.fn(), stop: vi.fn() } },
          { provide: ElementRef, useValue: new ElementRef(document.createElement('div')) },
          { provide: ChangeDetectorRef, useValue: { markForCheck: vi.fn() } },
          { provide: GatewayService, useValue: gateway },
        ],
      });
      const screen = TestBed.runInInjectionContext(
        () =>
          new AgentScreenComponent(
            TestBed.inject(NgZone),
            TestBed.inject(ChangeDetectorRef),
            TestBed.inject(GatewayService),
          ),
      );
      screen.runCachedMessages = false;
      screen.currentAgent = { agentType: 'leadership_planner', sessionId: 'sess-user' };
      screen.chatInput = 'Audit our Q4 plan';
      screen.onSend();

      expect(gateway.sendBroadcast).toHaveBeenCalledWith(
        'Audit our Q4 plan',
        ['sess-user'],
      );
      expect(screen.isAgentWorking).toBe(true);

      screen.ngOnDestroy();
      expect(clearIntervalSpy).toHaveBeenCalled();
      expect(demoSessionMock.dispose).toHaveBeenCalledTimes(1);
      expect(panelTab.dispose).toHaveBeenCalledTimes(1);
      expect(disposeSpy).toHaveBeenCalledWith(screen);
    } finally {
      TestBed.resetTestingModule();
      vi.useRealTimers();
    }
  });

  it('omits fabricated infrastructure defaults in AgentChatDisplayService and clears LogComponent timers on destroy', () => {
    vi.useFakeTimers();
    try {
      const display = new AgentChatDisplayService();
      const host: AgentChatDisplayHost = {
        cdr: { markForCheck: vi.fn() } as any,
        agents: {},
        currentAgent: { agentType: 'leadership_planner', sessionId: 'sess-tick' },
        chatMessages: [],
        chatInput: '',
        demoService: { activeDemo: () => '3' } as any,
        filterSettings: { showToolCalls: true, showLoadSkills: true, chatFilterTier: 'raw' },
        isSecuringAgent: false,
        isAgentWorking: false,
        finalToolCallMessage: null,
        errorMessageInterval: null,
        resetSimulationStatistics: vi.fn(),
        handleRaceStarted: vi.fn(),
        handleTickUpdate: vi.fn(),
        scrollChatToBottom: vi.fn(),
        autoResize: vi.fn(),
      };
      display.processMessageForDisplay(host, {
        guid: 'sess-tick',
        speaker: 'leadership_planner',
        text: '',
        isUser: false,
        timestamp: new Date(),
        msgType: AgentMessageType.TOOL_END,
        toolName: 'advance_tick',
        result: { tick: 1, notable_events: [] },
      });
      const feedItem = display.displayItems[0] as { kind: string; infrastructure?: unknown };
      expect(feedItem.kind).toBe('tick_feed');
      expect(feedItem.infrastructure).toBeUndefined();
      display.dispose();

      const chat$ = new Subject<ChatMessage>();
      const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');
      const log = new LogComponent(
        { chat$ } as any,
        { markForCheck: vi.fn() } as any,
        { reset: () => 0, activeDemo: () => 'demo1' } as any,
      );
      log.ngOnInit();
      chat$.next({
        guid: 's1',
        speaker: 'user',
        text: 'Hello',
        isUser: true,
        timestamp: new Date(),
        msgType: AgentMessageType.TEXT,
      });
      log.ngOnDestroy();
      expect(clearTimeoutSpy).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('aborts pending cached fetch/text replay and prevents zombie timers on AgentScreenComponent.ngOnDestroy() (4d-1)', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const origFetch = globalThis.fetch;
    vi.useFakeTimers();
    const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout');
    let countdownFired = false;
    const countdownListener = () => {
      countdownFired = true;
    };
    window.addEventListener('countdown:autoStart', countdownListener);

    try {
      let resolveFetch!: (value: Response) => void;
      const fetchMock = vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve;
          }),
      );
      Object.defineProperty(globalThis, 'fetch', { configurable: true, value: fetchMock });

      const replay = new AgentCachedGatewayReplayService();
      const demoService = {
        activeDemo: signal('1'),
        cachedMessagesToggle: signal(0),
        reset: signal(0),
        modeSwitchLabel: signal(null),
      };
      TestBed.configureTestingModule({
        providers: [
          provideZonelessChangeDetection(),
          { provide: AgentSimulationStatsService, useValue: { showSimPanel: false, isSimulationRunning: false, averagePace: '0:00', isFollowingLeader: false, runnersFinishedAwaitingHud: new Set<string>(), connect: vi.fn(), reset: vi.fn(), dispose: vi.fn() } },
          { provide: AgentPanelTabExpansionService, useValue: { isExpanded: false, activeTab: 'agent', tabBtnWide: false, showTabLabels: false, connect: vi.fn(), dispose: vi.fn() } },
          { provide: AgentGatewayAgentsService, useValue: { agents: {}, initializingAgents: {}, connect: vi.fn(), removeAllActiveAgents: vi.fn() } },
          { provide: AgentWindowEventsService, useValue: { bind: vi.fn(), seedGatewayDumpFlag: vi.fn(), start: vi.fn(), stop: vi.fn() } },
          { provide: AgentRuntimeControlsService, useValue: { register: vi.fn(), unregister: vi.fn(), notifyUpdate: vi.fn() } },
          { provide: DemoService, useValue: demoService },
          { provide: AgentChatDisplayService, useValue: new AgentChatDisplayService() },
          { provide: AgentDemoSessionService, useValue: { runDemoLifecycle: vi.fn().mockResolvedValue(undefined), dispose: vi.fn() } },
          { provide: AgentCachedGatewayReplayService, useValue: replay },
          { provide: ElementRef, useValue: new ElementRef(document.createElement('div')) },
          { provide: ChangeDetectorRef, useValue: { markForCheck: vi.fn() } },
          { provide: GatewayService, useValue: { chat$: new Subject<ChatMessage>(), removeAgent: vi.fn(), setFilterSettings: vi.fn(), getOrganizerGuid: vi.fn() } },
        ],
      });
      const screen = TestBed.runInInjectionContext(
        () =>
          new AgentScreenComponent(
            TestBed.inject(NgZone),
            TestBed.inject(ChangeDetectorRef),
            TestBed.inject(GatewayService),
          ),
      );
      const completeSpy = vi.spyOn(screen, 'onCachedReplayComplete');
      const abortSpy = vi.spyOn(screen, 'onCachedReplayAbort');

      const runPromise = screen.runCachedDataStream();
      expect(fetchMock).toHaveBeenCalledTimes(1);

      const callsBeforeDestroy = setTimeoutSpy.mock.calls.length;
      screen.ngOnDestroy();

      const ndjson = [
        JSON.stringify({ event: 'run_start', data: { guid: 's1', speaker: 'leadership_planner' }, t: 0 }),
        JSON.stringify({ event: 'tool_end', data: { guid: 's1', tool_name: 'process_leadership_tick', result: {} }, t: 5000 }),
      ].join('\n');
      resolveFetch(
        new Response(ndjson, {
          status: 200,
          headers: { 'content-type': 'application/x-ndjson' },
        }),
      );
      await runPromise;

      expect(setTimeoutSpy.mock.calls.length).toBe(callsBeforeDestroy);
      expect(agentGateway.isNdjsonReplayActive()).toBe(false);
      expect(screen.currentDemoCachedRunCount).toBe(0);
      expect(completeSpy).not.toHaveBeenCalled();
      expect(abortSpy).not.toHaveBeenCalled();
      vi.advanceTimersByTime(10_000);
      expect(countdownFired).toBe(false);

      // Also verify stop() during pending res.text()
      let resolveText!: (value: string) => void;
      fetchMock.mockImplementationOnce(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          statusText: 'OK',
          headers: new Headers({ 'content-type': 'application/x-ndjson' }),
          text: () =>
            new Promise<string>((resolve) => {
              resolveText = resolve;
            }),
        } as unknown as Response),
      );
      const callsBeforeTextRun = setTimeoutSpy.mock.calls.length;
      const run2 = replay.run(screen);
      await Promise.resolve();
      replay.stop();
      resolveText(ndjson);
      await run2;
      expect(setTimeoutSpy.mock.calls.length).toBe(callsBeforeTextRun);
      expect(agentGateway.isNdjsonReplayActive()).toBe(false);
    } finally {
      window.removeEventListener('countdown:autoStart', countdownListener);
      Object.defineProperty(globalThis, 'fetch', { configurable: true, value: origFetch });
      TestBed.resetTestingModule();
      vi.useRealTimers();
    }
  });

  it('cancels AgentDemoSessionService.runDemoLifecycle() post-destroy host mutations in cached mode on AgentScreenComponent.ngOnDestroy() (4d-4)', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const demoSession = new AgentDemoSessionService();
    const demoService = {
      activeDemo: signal('5b'),
      cachedMessagesToggle: signal(0),
      reset: signal(1),
      modeSwitchLabel: signal(null),
    };
    const cdr = { markForCheck: vi.fn() };
    const dispatchedAfterDestroy: string[] = [];
    const windowSpy = vi.spyOn(window, 'dispatchEvent');

    try {
      TestBed.configureTestingModule({
        providers: [
          provideZonelessChangeDetection(),
          { provide: AgentSimulationStatsService, useValue: { showSimPanel: false, isSimulationRunning: false, averagePace: '0:00', isFollowingLeader: false, runnersFinishedAwaitingHud: new Set<string>(), connect: vi.fn(), reset: vi.fn(), dispose: vi.fn(), onSimFinished: vi.fn(), resetSimulationStatistics: vi.fn() } },
          { provide: AgentPanelTabExpansionService, useValue: { isExpanded: false, activeTab: 'agent', tabBtnWide: false, showTabLabels: false, connect: vi.fn(), dispose: vi.fn() } },
          { provide: AgentGatewayAgentsService, useValue: { agents: {}, initializingAgents: {}, connect: vi.fn(), removeAllActiveAgents: vi.fn(), onInitAgent: vi.fn() } },
          { provide: AgentWindowEventsService, useValue: { bind: vi.fn(), seedGatewayDumpFlag: vi.fn(), start: vi.fn(), stop: vi.fn() } },
          { provide: AgentRuntimeControlsService, useValue: { register: vi.fn(), unregister: vi.fn(), notifyUpdate: vi.fn() } },
          { provide: DemoService, useValue: demoService },
          { provide: AgentChatDisplayService, useValue: new AgentChatDisplayService() },
          { provide: AgentDemoSessionService, useValue: demoSession },
          { provide: AgentCachedGatewayReplayService, useValue: { stop: vi.fn() } },
          { provide: ElementRef, useValue: new ElementRef(document.createElement('div')) },
          { provide: ChangeDetectorRef, useValue: cdr },
          {
            provide: GatewayService,
            useValue: {
              chat$: new Subject<ChatMessage>(),
              removeCurrentSimulationId: vi.fn(),
              resetDemoChatPipelineState: vi.fn(),
              setFilterSettings: vi.fn(),
              removeAgent: vi.fn(),
              getOrganizerGuid: vi.fn(),
            },
          },
        ],
      });
      const screen = TestBed.runInInjectionContext(
        () =>
          new AgentScreenComponent(
            TestBed.inject(NgZone),
            TestBed.inject(ChangeDetectorRef),
            TestBed.inject(GatewayService),
          ),
      );
      screen.runCachedMessages = true;
      let resolveInit!: () => void;
      screen.onInitAgent = vi.fn(
        () =>
          new Promise<void>((resolve) => {
            resolveInit = resolve;
          }),
      );

      const lifecyclePromise = demoSession.runDemoLifecycle(screen);
      await Promise.resolve();
      expect(screen.onInitAgent).toHaveBeenCalledTimes(1);

      windowSpy.mockClear();
      cdr.markForCheck.mockClear();
      screen.ngOnDestroy();

      resolveInit();
      await lifecyclePromise;

      for (const call of windowSpy.mock.calls) {
        dispatchedAfterDestroy.push((call[0] as Event).type);
      }
      expect(screen.currentAgent).toBeNull();
      expect(screen.chatInput).toBe('');
      expect(dispatchedAfterDestroy).not.toContain('sim:giveRunnerThoughts');
      expect(dispatchedAfterDestroy).not.toContain('gateway:routeGeojson');
      expect(cdr.markForCheck).not.toHaveBeenCalled();
    } finally {
      windowSpy.mockRestore();
      TestBed.resetTestingModule();
    }
  });

  it('resets StakeholderGraphCardComponent.modalOpen synchronously in closeModal() even when dialog.close() fires no sync close event', () => {
    const card = new StakeholderGraphCardComponent();
    const dialog = document.createElement('dialog');
    dialog.showModal = vi.fn(() => {
      dialog.setAttribute('open', '');
    });
    dialog.close = vi.fn(() => {
      dialog.removeAttribute('open');
    });
    (card as unknown as { graphDialogRef: ElementRef<HTMLDialogElement> }).graphDialogRef =
      new ElementRef(dialog);

    card.openModal(new Event('click'));
    expect(dialog.open).toBe(true);
    expect(card.modalOpen).toBe(true);

    card.closeModal(new Event('click'));
    expect(dialog.open).toBe(false);
    expect(card.modalOpen).toBe(false);
  });

  it('syncs A2uiControllerComponent modal state with native <dialog> showModal/close and onModalDialogClose', () => {
    const ctrl = Object.assign(Object.create(A2uiControllerComponent.prototype), {
      tree: new A2uiTreeService(),
      modalOpenMap: signal(new Map<string, boolean>()),
      modalPressedOutsideIds: new Set<string>(),
    }) as A2uiControllerComponent;

    const modalNode = { id: 'modal-1', component: { Modal: {} } } as any;
    const dialog = document.createElement('dialog');
    dialog.showModal = vi.fn(() => {
      dialog.setAttribute('open', '');
    });
    dialog.close = vi.fn(() => {
      dialog.removeAttribute('open');
    });

    ctrl.openModal(modalNode, dialog);
    expect(ctrl.isModalOpen(modalNode)).toBe(true);
    expect(dialog.showModal).toHaveBeenCalledTimes(1);

    ctrl.closeModal(modalNode, dialog);
    expect(ctrl.isModalOpen(modalNode)).toBe(false);
    expect(dialog.close).toHaveBeenCalledTimes(1);

    ctrl.openModal(modalNode, dialog);
    expect(ctrl.isModalOpen(modalNode)).toBe(true);
    ctrl.onModalDialogClose(modalNode);
    expect(ctrl.isModalOpen(modalNode)).toBe(false);
  });

  it('renders A2uiControllerComponent Modal without nested <button> or spurious warnings for Text, Button, and Row-with-Button entryPointChild', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      await resolveComponentResources(async (url) => {
        if (url.endsWith('a2-ui-controller.component.html')) {
          return readFileSync(
            resolve(__dirname, '../../../a2ui/a2-ui-controller.component.html'),
            'utf-8',
          );
        }
        return '';
      });
      await TestBed.configureTestingModule({
        imports: [A2uiControllerComponent],
        providers: [
          provideZonelessChangeDetection(),
          {
            provide: DemoService,
            useValue: {
              activeDemo: signal('3'),
              reset: signal(0),
            },
          },
          {
            provide: GatewayService,
            useValue: {
              sendBroadcast: vi.fn(),
            },
          },
          {
            provide: MarkdownService,
            useValue: {
              parse: async (s: string) => s,
            },
          },
          {
            provide: AGENT_SCREEN_HOST,
            useValue: {
              isAgentWorking: false,
              runCachedMessages: false,
              currentAgent: null,
              agents: {},
              runCachedDataStream: vi.fn(),
            },
          },
        ],
      }).compileComponents();

      const stubDialog = (dialog: HTMLDialogElement) => {
        const showModal = vi.fn(() => {
          dialog.setAttribute('open', '');
        });
        const close = vi.fn(() => {
          dialog.removeAttribute('open');
          dialog.dispatchEvent(new Event('close'));
        });
        dialog.showModal = showModal;
        dialog.close = close;
        return { showModal, close };
      };

      const setControllerNode = (
        fixture: { componentInstance: A2uiControllerComponent },
        payload: unknown,
      ) => {
        // Vitest runs Angular JIT without ngtsc's signal-input compiler transform;
        // assign the signal before the first detectChanges() so constructor()'s effect reads it.
        (fixture.componentInstance as unknown as { node: ReturnType<typeof signal<unknown>> }).node =
          signal<unknown>(payload);
      };

      // 1. Text entryPointChild -> single native <button class="modal-entry-point">
      const textFixture = TestBed.createComponent(A2uiControllerComponent);
      setControllerNode(textFixture, {
        surfaceUpdate: {
          surfaceId: 'modal-text-surface',
          components: [
            {
              id: 'modal-text',
              component: {
                Modal: {
                  entryPointChild: 'text-trigger',
                  contentChild: 'text-body',
                },
              },
            },
            {
              id: 'text-trigger',
              component: { Text: { text: { literalString: 'Open from Text' } } },
            },
            {
              id: 'text-body',
              component: { Text: { text: { literalString: 'Text modal content' } } },
            },
          ],
        },
      });
      textFixture.detectChanges();
      await textFixture.whenStable();
      textFixture.detectChanges();

      const textRoot = textFixture.nativeElement as HTMLElement;
      expect(textRoot.querySelectorAll('button button').length).toBe(0);
      const textTrigger = textRoot.querySelector('button.modal-entry-point') as HTMLButtonElement | null;
      expect(textTrigger).not.toBeNull();
      const textDialog = textRoot.querySelector('dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const textDialogSpies = stubDialog(textDialog);
      textTrigger!.click();
      textFixture.detectChanges();
      await textFixture.whenStable();
      await Promise.resolve();
      textFixture.detectChanges();
      expect(textDialogSpies.showModal).toHaveBeenCalledTimes(1);
      expect(textRoot.querySelector('.modal-content')?.textContent).toContain('Text modal content');
      textFixture.destroy();

      // 2. Canonical Button entryPointChild (without action) -> single <button.modal-entry-point>, 0 nested buttons, 0 action warnings
      warnSpy.mockClear();
      const btnFixture = TestBed.createComponent(A2uiControllerComponent);
      setControllerNode(btnFixture, {
        surfaceUpdate: {
          surfaceId: 'modal-button-surface',
          components: [
            {
              id: 'modal-btn',
              component: {
                Modal: {
                  entryPointChild: 'open-btn',
                  contentChild: 'btn-body',
                },
              },
            },
            {
              id: 'open-btn',
              component: {
                Button: {
                  primary: true,
                  child: 'open-btn-label',
                },
              },
            },
            {
              id: 'open-btn-label',
              component: { Text: { text: { literalString: 'Volunteer Sign Up' } } },
            },
            {
              id: 'btn-body',
              component: { Text: { text: { literalString: 'Button modal content' } } },
            },
          ],
        },
      });
      btnFixture.detectChanges();
      await btnFixture.whenStable();
      btnFixture.detectChanges();

      const btnRoot = btnFixture.nativeElement as HTMLElement;
      expect(btnRoot.querySelectorAll('button button').length).toBe(0);
      const btnTriggers = btnRoot.querySelectorAll('button.modal-entry-point');
      expect(btnTriggers.length).toBe(1);
      const btnTrigger = btnTriggers[0] as HTMLButtonElement;
      expect(btnTrigger.classList.contains('primary')).toBe(true);
      const btnDialog = btnRoot.querySelector('dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const btnDialogSpies = stubDialog(btnDialog);
      btnTrigger.click();
      btnFixture.detectChanges();
      await btnFixture.whenStable();
      await Promise.resolve();
      btnFixture.detectChanges();
      expect(btnDialogSpies.showModal).toHaveBeenCalledTimes(1);
      expect(btnRoot.querySelector('.modal-content')?.textContent).toContain('Button modal content');
      const actionWarnings = warnSpy.mock.calls.filter((args) =>
        String(args[0]).includes('is not defined in the registry'),
      );
      expect(actionWarnings).toEqual([]);
      btnFixture.destroy();

      // 3. Container (Row) containing a Button descendant -> non-button wrapper, 0 nested buttons, 0 action warnings
      warnSpy.mockClear();
      const rowFixture = TestBed.createComponent(A2uiControllerComponent);
      setControllerNode(rowFixture, {
        surfaceUpdate: {
          surfaceId: 'modal-row-surface',
          components: [
            {
              id: 'modal-row',
              component: {
                Modal: {
                  entryPointChild: 'entry-row',
                  contentChild: 'row-body',
                },
              },
            },
            {
              id: 'entry-row',
              component: {
                Row: {
                  children: { explicitList: ['row-hint', 'row-btn'] },
                },
              },
            },
            {
              id: 'row-hint',
              component: { Text: { text: { literalString: 'Details:' } } },
            },
            {
              id: 'row-btn',
              component: {
                Button: {
                  child: 'row-btn-label',
                },
              },
            },
            {
              id: 'row-btn-label',
              component: { Text: { text: { literalString: 'Inspect' } } },
            },
            {
              id: 'row-body',
              component: { Text: { text: { literalString: 'Row modal content' } } },
            },
          ],
        },
      });
      rowFixture.detectChanges();
      await rowFixture.whenStable();
      rowFixture.detectChanges();

      const rowRoot = rowFixture.nativeElement as HTMLElement;
      expect(rowRoot.querySelectorAll('button button').length).toBe(0);
      const innerButton = rowRoot.querySelector('.modal-entry-point--container button') as HTMLButtonElement | null;
      expect(innerButton).not.toBeNull();
      const rowDialog = rowRoot.querySelector('dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const rowDialogSpies = stubDialog(rowDialog);
      innerButton!.click();
      rowFixture.detectChanges();
      await rowFixture.whenStable();
      await Promise.resolve();
      rowFixture.detectChanges();
      expect(rowDialogSpies.showModal).toHaveBeenCalledTimes(1);
      expect(rowRoot.querySelector('.modal-content')?.textContent).toContain('Row modal content');
      const rowWarnings = warnSpy.mock.calls.filter((args) =>
        String(args[0]).includes('is not defined in the registry'),
      );
      expect(rowWarnings).toEqual([]);
      rowFixture.destroy();
    } finally {
      warnSpy.mockRestore();
      TestBed.resetTestingModule();
    }
  });

  it('prevents nested <button> for Button->Modal trees and prevents entryPointChild registered-action dispatch while keeping .modal-content dispatch', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const sendBroadcastMock = vi.fn().mockResolvedValue(undefined);
    const hostState = {
      isAgentWorking: false,
      runCachedMessages: false,
      currentAgent: { sessionId: 'planner-guid-1', agentType: 'leadership_planner' },
      agents: { leadership_planner: 'planner-guid-1' },
      runCachedDataStream: vi.fn(),
    };
    try {
      await resolveComponentResources(async (url) => {
        if (url.endsWith('a2-ui-controller.component.html')) {
          return readFileSync(
            resolve(__dirname, '../../../a2ui/a2-ui-controller.component.html'),
            'utf-8',
          );
        }
        return '';
      });
      await TestBed.configureTestingModule({
        imports: [A2uiControllerComponent],
        providers: [
          provideZonelessChangeDetection(),
          {
            provide: DemoService,
            useValue: {
              activeDemo: signal('3'),
              reset: signal(0),
            },
          },
          {
            provide: GatewayService,
            useValue: {
              sendBroadcast: sendBroadcastMock,
            },
          },
          {
            provide: MarkdownService,
            useValue: {
              parse: async (s: string) => s,
            },
          },
          {
            provide: AGENT_SCREEN_HOST,
            useValue: hostState,
          },
        ],
      }).compileComponents();

      const stubDialog = (dialog: HTMLDialogElement) => {
        const showModal = vi.fn(() => {
          dialog.setAttribute('open', '');
        });
        const close = vi.fn(() => {
          dialog.removeAttribute('open');
          dialog.dispatchEvent(new Event('close'));
        });
        dialog.showModal = showModal;
        dialog.close = close;
        return { showModal, close };
      };

      const setControllerNode = (
        fixture: { componentInstance: A2uiControllerComponent },
        payload: unknown,
      ) => {
        (fixture.componentInstance as unknown as { node: ReturnType<typeof signal<unknown>> }).node =
          signal<unknown>(payload);
      };

      // 1i-5. Modal -> entryPointChild: Button -> child: Modal -> entryPointChild: Text
      const nestedModalFixture = TestBed.createComponent(A2uiControllerComponent);
      setControllerNode(nestedModalFixture, {
        surfaceUpdate: {
          surfaceId: 'modal-btn-modal-surface',
          components: [
            {
              id: 'm-outer',
              component: {
                Modal: {
                  entryPointChild: 'btn-outer',
                  contentChild: 'outer-body',
                },
              },
            },
            {
              id: 'btn-outer',
              component: {
                Button: {
                  child: 'm-inner',
                },
              },
            },
            {
              id: 'm-inner',
              component: {
                Modal: {
                  entryPointChild: 't-inner',
                  contentChild: 'inner-body',
                },
              },
            },
            {
              id: 't-inner',
              component: { Text: { text: { literalString: 'Inner Modal Trigger' } } },
            },
            {
              id: 'inner-body',
              component: { Text: { text: { literalString: 'Inner modal body' } } },
            },
            {
              id: 'outer-body',
              component: { Text: { text: { literalString: 'Outer modal body' } } },
            },
          ],
        },
      });
      nestedModalFixture.detectChanges();
      await nestedModalFixture.whenStable();
      nestedModalFixture.detectChanges();

      const nestedRoot = nestedModalFixture.nativeElement as HTMLElement;
      expect(nestedRoot.querySelectorAll('button button').length).toBe(0);
      expect(nestedRoot.querySelectorAll('[role="button"]').length).toBe(0);
      expect(nestedRoot.querySelector('.a2ui-Button-container')).not.toBeNull();

      const outerModalEl = nestedRoot.querySelector('.a2ui-Modal[data-id="m-outer"]') as HTMLElement;
      const innerModalEl = nestedRoot.querySelector('.a2ui-Modal[data-id="m-inner"]') as HTMLElement;
      const outerDialog = outerModalEl.querySelector(':scope > dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const innerDialog = innerModalEl.querySelector(':scope > dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const outerSpies = stubDialog(outerDialog);
      const innerSpies = stubDialog(innerDialog);

      const innerTrigger = innerModalEl.querySelector(':scope > button.modal-entry-point') as HTMLButtonElement | null;
      expect(innerTrigger).not.toBeNull();
      innerTrigger!.click();
      nestedModalFixture.detectChanges();
      await nestedModalFixture.whenStable();
      await Promise.resolve();
      nestedModalFixture.detectChanges();
      expect(innerSpies.showModal).toHaveBeenCalledTimes(1);
      expect(outerSpies.showModal).toHaveBeenCalledTimes(0);
      expect(innerDialog.querySelector('.modal-content')?.textContent).toContain('Inner modal body');

      const innerClose = innerDialog.querySelector('.modal-close') as HTMLButtonElement | null;
      expect(innerClose).not.toBeNull();
      innerClose!.click();
      nestedModalFixture.detectChanges();
      await nestedModalFixture.whenStable();
      nestedModalFixture.detectChanges();
      expect(innerSpies.close).toHaveBeenCalledTimes(1);
      expect(outerSpies.showModal).toHaveBeenCalledTimes(0);
      nestedModalFixture.destroy();

      // 1n. Expandable Card containing a Modal does NOT toggle expansion when clicking inside open <dialog>
      const expModalFixture = TestBed.createComponent(A2uiControllerComponent);
      const expCardNode = {
        id: 'exp-card',
        component: {
          Card: {
            expandable: { literalBoolean: true },
            child: 'exp-col',
          },
        },
      };
      setControllerNode(expModalFixture, {
        surfaceUpdate: {
          surfaceId: 'expandable-card-modal-surface',
          components: [
            expCardNode,
            {
              id: 'exp-col',
              component: {
                Column: {
                  children: { explicitList: ['header-title', 'exp-body-text', 'footer-modal'] },
                },
              },
            },
            {
              id: 'header-title',
              component: { Text: { text: { literalString: 'Expandable Summary' } } },
            },
            {
              id: 'exp-body-text',
              component: { Text: { text: { literalString: 'Expanded details' } } },
            },
            {
              id: 'footer-modal',
              component: {
                Modal: {
                  entryPointChild: 'exp-modal-trigger',
                  contentChild: 'exp-modal-body',
                },
              },
            },
            {
              id: 'exp-modal-trigger',
              component: { Text: { text: { literalString: 'Open Modal Info' } } },
            },
            {
              id: 'exp-modal-body',
              component: { Text: { text: { literalString: 'Non-button modal detail text' } } },
            },
          ],
        },
      });
      expModalFixture.detectChanges();
      await expModalFixture.whenStable();
      expModalFixture.detectChanges();

      const expRoot = expModalFixture.nativeElement as HTMLElement;
      const cardEl = expRoot.querySelector('.a2ui-Card-expandable[data-id="exp-card"]') as HTMLElement;
      expect(cardEl).not.toBeNull();
      expect(cardEl.classList.contains('expanded')).toBe(false);

      const expModalTrigger = cardEl.querySelector('button.modal-entry-point') as HTMLButtonElement | null;
      const expDialog = cardEl.querySelector('dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const expDialogSpies = stubDialog(expDialog);
      expect(expModalTrigger).not.toBeNull();
      expModalTrigger!.click();
      expModalFixture.detectChanges();
      await expModalFixture.whenStable();
      await Promise.resolve();
      expModalFixture.detectChanges();
      expect(expDialogSpies.showModal).toHaveBeenCalledTimes(1);
      expect(cardEl.classList.contains('expanded')).toBe(false);

      const modalTextEl = expDialog.querySelector('.modal-content span') as HTMLElement | null;
      expect(modalTextEl).not.toBeNull();
      modalTextEl!.click();
      expModalFixture.detectChanges();
      expect(cardEl.classList.contains('expanded')).toBe(false);

      // Defense-in-depth: direct onExpandableCardClick with a target inside <dialog> also ignores it
      expModalFixture.componentInstance.onExpandableCardClick(
        { target: modalTextEl, preventDefault: vi.fn() } as unknown as MouseEvent,
        expCardNode,
      );
      expModalFixture.detectChanges();
      expect(cardEl.classList.contains('expanded')).toBe(false);

      // Clicking the card header itself still expands the card as expected
      const headerEl = cardEl.querySelector('.a2ui-Text[data-id="header-title"]') as HTMLElement | null;
      expect(headerEl).not.toBeNull();
      headerEl!.click();
      expModalFixture.detectChanges();
      expect(cardEl.classList.contains('expanded')).toBe(true);
      expModalFixture.destroy();

      // 1o. Modal -> contentChild: Card(expandable: true) expands/collapses on header click
      // while nested interactive buttons inside the card still do not toggle expansion
      const modalExpFixture = TestBed.createComponent(A2uiControllerComponent);
      setControllerNode(modalExpFixture, {
        surfaceUpdate: {
          surfaceId: 'modal-contains-expandable-card-surface',
          components: [
            {
              id: 'm-exp',
              component: {
                Modal: {
                  entryPointChild: 'm-exp-trigger',
                  contentChild: 'inner-exp-card',
                },
              },
            },
            {
              id: 'm-exp-trigger',
              component: { Text: { text: { literalString: 'Open Summary Modal' } } },
            },
            {
              id: 'inner-exp-card',
              component: {
                Card: {
                  expandable: { literalBoolean: true },
                  child: 'inner-exp-col',
                },
              },
            },
            {
              id: 'inner-exp-col',
              component: {
                Column: {
                  children: { explicitList: ['inner-hdr', 'inner-body', 'inner-btn'] },
                },
              },
            },
            {
              id: 'inner-hdr',
              component: { Text: { text: { literalString: 'Inner Expandable Summary' } } },
            },
            {
              id: 'inner-body',
              component: { Text: { text: { literalString: 'Inner expanded details' } } },
            },
            {
              id: 'inner-btn',
              component: {
                Button: {
                  child: 'inner-btn-label',
                },
              },
            },
            {
              id: 'inner-btn-label',
              component: { Text: { text: { literalString: 'Inner Button' } } },
            },
          ],
        },
      });
      modalExpFixture.detectChanges();
      await modalExpFixture.whenStable();
      modalExpFixture.detectChanges();

      const modalExpRoot = modalExpFixture.nativeElement as HTMLElement;
      const modalExpTrigger = modalExpRoot.querySelector('button.modal-entry-point') as HTMLButtonElement | null;
      const modalExpDialog = modalExpRoot.querySelector('dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const modalExpDialogSpies = stubDialog(modalExpDialog);
      expect(modalExpTrigger).not.toBeNull();
      modalExpTrigger!.click();
      modalExpFixture.detectChanges();
      await modalExpFixture.whenStable();
      await Promise.resolve();
      modalExpFixture.detectChanges();
      expect(modalExpDialogSpies.showModal).toHaveBeenCalledTimes(1);

      const innerCardEl = modalExpDialog.querySelector(
        '.modal-content .a2ui-Card-expandable[data-id="inner-exp-card"]',
      ) as HTMLElement | null;
      expect(innerCardEl).not.toBeNull();
      expect(innerCardEl!.classList.contains('expanded')).toBe(false);

      const innerHeaderEl = innerCardEl!.querySelector('.a2ui-Text[data-id="inner-hdr"]') as HTMLElement | null;
      expect(innerHeaderEl).not.toBeNull();
      innerHeaderEl!.click();
      modalExpFixture.detectChanges();
      expect(innerCardEl!.classList.contains('expanded')).toBe(true);

      const innerBtnEl = innerCardEl!.querySelector('.a2ui-Button[data-id="inner-btn"] button') as HTMLButtonElement | null;
      expect(innerBtnEl).not.toBeNull();
      innerBtnEl!.click();
      modalExpFixture.detectChanges();
      expect(innerCardEl!.classList.contains('expanded')).toBe(true);

      innerHeaderEl!.click();
      modalExpFixture.detectChanges();
      expect(innerCardEl!.classList.contains('expanded')).toBe(false);
      modalExpFixture.destroy();

      // 1j. Standalone Button -> child: Modal -> entryPointChild: Text
      warnSpy.mockClear();
      const standaloneFixture = TestBed.createComponent(A2uiControllerComponent);
      setControllerNode(standaloneFixture, {
        surfaceUpdate: {
          surfaceId: 'standalone-btn-modal-surface',
          components: [
            {
              id: 'btn-root',
              component: {
                Button: {
                  child: 'm-standalone-inner',
                },
              },
            },
            {
              id: 'm-standalone-inner',
              component: {
                Modal: {
                  entryPointChild: 't-standalone-inner',
                  contentChild: 'standalone-inner-body',
                },
              },
            },
            {
              id: 't-standalone-inner',
              component: { Text: { text: { literalString: 'Standalone Inner Trigger' } } },
            },
            {
              id: 'standalone-inner-body',
              component: { Text: { text: { literalString: 'Standalone inner content' } } },
            },
          ],
        },
      });
      standaloneFixture.detectChanges();
      await standaloneFixture.whenStable();
      standaloneFixture.detectChanges();

      const standaloneRoot = standaloneFixture.nativeElement as HTMLElement;
      expect(standaloneRoot.querySelectorAll('button button').length).toBe(0);
      expect(standaloneRoot.querySelectorAll('[role="button"]').length).toBe(0);
      const standaloneTrigger = standaloneRoot.querySelector('button.modal-entry-point') as HTMLButtonElement | null;
      expect(standaloneTrigger).not.toBeNull();
      const standaloneDialog = standaloneRoot.querySelector('dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const standaloneDialogSpies = stubDialog(standaloneDialog);
      standaloneTrigger!.click();
      standaloneFixture.detectChanges();
      await standaloneFixture.whenStable();
      await Promise.resolve();
      standaloneFixture.detectChanges();
      expect(standaloneDialogSpies.showModal).toHaveBeenCalledTimes(1);
      expect(standaloneRoot.querySelector('.modal-content')?.textContent).toContain('Standalone inner content');
      expect(
        warnSpy.mock.calls.filter((args) => String(args[0]).includes('is not defined in the registry')),
      ).toEqual([]);
      standaloneFixture.destroy();

      // Ancestor cycle: Card -> Modal -> entryPointChild: Card (ancestors seeded cleanly)
      const cyclicFixture = TestBed.createComponent(A2uiControllerComponent);
      setControllerNode(cyclicFixture, {
        surfaceUpdate: {
          surfaceId: 'cyclic-card-modal-surface',
          components: [
            {
              id: 'card-anc',
              component: { Card: { child: 'm-anc' } },
            },
            {
              id: 'm-anc',
              component: {
                Modal: {
                  entryPointChild: 'card-anc',
                  contentChild: 'anc-body',
                },
              },
            },
            {
              id: 'anc-body',
              component: { Text: { text: { literalString: 'Cyclic safe body' } } },
            },
          ],
        },
      });
      cyclicFixture.detectChanges();
      await cyclicFixture.whenStable();
      cyclicFixture.detectChanges();
      const cyclicRoot = cyclicFixture.nativeElement as HTMLElement;
      expect(cyclicRoot.querySelectorAll('button button').length).toBe(0);
      expect(cyclicRoot.querySelector('button.modal-entry-point')).not.toBeNull();
      expect(cyclicRoot.querySelector('.modal-entry-point--container')).toBeNull();
      cyclicFixture.destroy();

      // 1k. Modal -> entryPointChild: Row -> Button (action: 'run_simulation') opens modal with 0 broadcasts,
      // while buttons inside .modal-content still dispatch registered actions and warn on unknown actions.
      warnSpy.mockClear();
      sendBroadcastMock.mockClear();
      hostState.isAgentWorking = false;

      const actionFixture = TestBed.createComponent(A2uiControllerComponent);
      setControllerNode(actionFixture, {
        surfaceUpdate: {
          surfaceId: 'modal-row-registered-action-surface',
          components: [
            {
              id: 'm-action',
              component: {
                Modal: {
                  entryPointChild: 'entry-row-action',
                  contentChild: 'content-col',
                },
              },
            },
            {
              id: 'entry-row-action',
              component: {
                Row: {
                  children: { explicitList: ['entry-hint', 'entry-run-btn'] },
                },
              },
            },
            {
              id: 'entry-hint',
              component: { Text: { text: { literalString: 'Preview:' } } },
            },
            {
              id: 'entry-run-btn',
              component: {
                Button: {
                  child: 'entry-run-label',
                  action: { name: { literalString: 'run_simulation' } },
                },
              },
            },
            {
              id: 'entry-run-label',
              component: { Text: { text: { literalString: 'Open Preview' } } },
            },
            {
              id: 'content-col',
              component: {
                Column: {
                  children: { explicitList: ['inside-run-btn', 'inside-unknown-btn'] },
                },
              },
            },
            {
              id: 'inside-run-btn',
              component: {
                Button: {
                  child: 'inside-run-label',
                  action: { name: { literalString: 'run_simulation' } },
                },
              },
            },
            {
              id: 'inside-run-label',
              component: { Text: { text: { literalString: 'Run Simulation' } } },
            },
            {
              id: 'inside-unknown-btn',
              component: {
                Button: {
                  child: 'inside-unknown-label',
                  action: { name: { literalString: 'unknown_modal_action' } },
                },
              },
            },
            {
              id: 'inside-unknown-label',
              component: { Text: { text: { literalString: 'Unknown Action' } } },
            },
          ],
        },
      });
      actionFixture.detectChanges();
      await actionFixture.whenStable();
      actionFixture.detectChanges();

      const actionRoot = actionFixture.nativeElement as HTMLElement;
      expect(actionRoot.querySelectorAll('button button').length).toBe(0);
      const entryBtn = actionRoot.querySelector('.modal-entry-point--container button') as HTMLButtonElement | null;
      expect(entryBtn).not.toBeNull();
      const actionDialog = actionRoot.querySelector('dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const actionDialogSpies = stubDialog(actionDialog);

      entryBtn!.click();
      actionFixture.detectChanges();
      await actionFixture.whenStable();
      await Promise.resolve();
      actionFixture.detectChanges();

      expect(actionDialogSpies.showModal).toHaveBeenCalledTimes(1);
      expect(sendBroadcastMock).not.toHaveBeenCalled();
      expect(hostState.isAgentWorking).toBe(false);
      expect(actionFixture.componentInstance.loadingButtonIds().has('entry-run-btn')).toBe(false);
      expect(entryBtn!.disabled).toBe(false);

      const insideButtons = actionRoot.querySelectorAll('.modal-content button') as NodeListOf<HTMLButtonElement>;
      expect(insideButtons.length).toBe(2);

      insideButtons[0]!.click();
      actionFixture.detectChanges();
      await actionFixture.whenStable();
      expect(sendBroadcastMock).toHaveBeenCalledTimes(1);
      expect(hostState.isAgentWorking).toBe(true);

      insideButtons[1]!.click();
      actionFixture.detectChanges();
      expect(
        warnSpy.mock.calls.filter((args) =>
          String(args[0]).includes('Action "unknown_modal_action" is not defined in the registry.'),
        ).length,
      ).toBe(1);

      actionFixture.destroy();
    } finally {
      warnSpy.mockRestore();
      TestBed.resetTestingModule();
    }
  });

  it('isolates direct nested expandable Card header clicks so inner toggles do not collapse outer Cards', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const hostState = {
      isAgentWorking: false,
      runCachedMessages: false,
      currentAgent: { sessionId: 'planner-guid-1', agentType: 'leadership_planner' },
      agents: { leadership_planner: 'planner-guid-1' },
      runCachedDataStream: vi.fn(),
    };
    try {
      await resolveComponentResources(async (url) => {
        if (url.endsWith('a2-ui-controller.component.html')) {
          return readFileSync(
            resolve(__dirname, '../../../a2ui/a2-ui-controller.component.html'),
            'utf-8',
          );
        }
        return '';
      });
      await TestBed.configureTestingModule({
        imports: [A2uiControllerComponent],
        providers: [
          provideZonelessChangeDetection(),
          {
            provide: DemoService,
            useValue: {
              activeDemo: signal('3'),
              reset: signal(0),
            },
          },
          {
            provide: GatewayService,
            useValue: {
              sendBroadcast: vi.fn().mockResolvedValue(undefined),
            },
          },
          {
            provide: MarkdownService,
            useValue: {
              parse: async (s: string) => s,
            },
          },
          {
            provide: AGENT_SCREEN_HOST,
            useValue: hostState,
          },
        ],
      }).compileComponents();

      const fixture = TestBed.createComponent(A2uiControllerComponent);
      (fixture.componentInstance as unknown as { node: ReturnType<typeof signal<unknown>> }).node =
        signal<unknown>({
          surfaceUpdate: {
            surfaceId: 'nested-expandable-cards-surface',
            components: [
              {
                id: 'outer-card',
                component: {
                  Card: {
                    expandable: { literalBoolean: true },
                    child: 'outer-col',
                  },
                },
              },
              {
                id: 'outer-col',
                component: {
                  Column: {
                    children: { explicitList: ['outer-hdr', 'inner-card'] },
                  },
                },
              },
              {
                id: 'outer-hdr',
                component: { Text: { text: { literalString: 'Outer Card Summary' } } },
              },
              {
                id: 'inner-card',
                component: {
                  Card: {
                    expandable: { literalBoolean: true },
                    child: 'inner-col',
                  },
                },
              },
              {
                id: 'inner-col',
                component: {
                  Column: {
                    children: { explicitList: ['inner-hdr', 'inner-body'] },
                  },
                },
              },
              {
                id: 'inner-hdr',
                component: { Text: { text: { literalString: 'Inner Card Summary' } } },
              },
              {
                id: 'inner-body',
                component: { Text: { text: { literalString: 'Inner Card Details' } } },
              },
            ],
          },
        });
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();

      const root = fixture.nativeElement as HTMLElement;
      const outerCardEl = root.querySelector('.a2ui-Card-expandable[data-id="outer-card"]') as HTMLElement;
      const innerCardEl = root.querySelector('.a2ui-Card-expandable[data-id="inner-card"]') as HTMLElement;
      const outerHdrEl = root.querySelector('.a2ui-Text[data-id="outer-hdr"]') as HTMLElement;
      const innerHdrEl = root.querySelector('.a2ui-Text[data-id="inner-hdr"]') as HTMLElement;

      expect(outerCardEl.classList.contains('expanded')).toBe(false);
      expect(innerCardEl.classList.contains('expanded')).toBe(false);

      outerHdrEl.click();
      fixture.detectChanges();
      expect(outerCardEl.classList.contains('expanded')).toBe(true);
      expect(innerCardEl.classList.contains('expanded')).toBe(false);

      innerHdrEl.click();
      fixture.detectChanges();
      expect(innerCardEl.classList.contains('expanded')).toBe(true);
      expect(outerCardEl.classList.contains('expanded')).toBe(true);

      innerHdrEl.click();
      fixture.detectChanges();
      expect(innerCardEl.classList.contains('expanded')).toBe(false);
      expect(outerCardEl.classList.contains('expanded')).toBe(true);

      fixture.destroy();
    } finally {
      TestBed.resetTestingModule();
    }
  });

  it('isolates onExpandableSummaryKeydown from header interactive controls and Modal dialog keydown boundaries', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const hostState = {
      isAgentWorking: false,
      runCachedMessages: false,
      currentAgent: { sessionId: 'planner-guid-1', agentType: 'leadership_planner' },
      agents: { leadership_planner: 'planner-guid-1' },
      runCachedDataStream: vi.fn(),
    };
    try {
      await resolveComponentResources(async (url) => {
        if (url.endsWith('a2-ui-controller.component.html')) {
          return readFileSync(
            resolve(__dirname, '../../../a2ui/a2-ui-controller.component.html'),
            'utf-8',
          );
        }
        return '';
      });
      await TestBed.configureTestingModule({
        imports: [A2uiControllerComponent],
        providers: [
          provideZonelessChangeDetection(),
          {
            provide: DemoService,
            useValue: {
              activeDemo: signal('3'),
              reset: signal(0),
            },
          },
          {
            provide: GatewayService,
            useValue: {
              sendBroadcast: vi.fn().mockResolvedValue(undefined),
            },
          },
          {
            provide: MarkdownService,
            useValue: {
              parse: async (s: string) => s,
            },
          },
          {
            provide: AGENT_SCREEN_HOST,
            useValue: hostState,
          },
        ],
      }).compileComponents();

      const stubDialog = (dialog: HTMLDialogElement) => {
        const showModal = vi.fn(() => {
          dialog.setAttribute('open', '');
        });
        const close = vi.fn(() => {
          dialog.removeAttribute('open');
          dialog.dispatchEvent(new Event('close'));
        });
        dialog.showModal = showModal;
        dialog.close = close;
        return { showModal, close };
      };

      const fixture = TestBed.createComponent(A2uiControllerComponent);
      (fixture.componentInstance as unknown as { node: ReturnType<typeof signal<unknown>> }).node =
        signal<unknown>({
          surfaceUpdate: {
            surfaceId: 'summary-keydown-modal-isolation-surface',
            components: [
              {
                id: 'outer-card',
                component: {
                  Card: {
                    expandable: { literalBoolean: true },
                    child: 'outer-col',
                  },
                },
              },
              {
                id: 'outer-col',
                component: {
                  Column: {
                    children: { explicitList: ['outer-hdr-row', 'outer-body'] },
                  },
                },
              },
              {
                id: 'outer-hdr-row',
                component: {
                  Row: {
                    children: { explicitList: ['outer-hdr-title', 'hdr-btn', 'hdr-input', 'hdr-modal'] },
                  },
                },
              },
              {
                id: 'outer-hdr-title',
                component: { Text: { text: { literalString: 'Outer Header' } } },
              },
              {
                id: 'hdr-btn',
                component: { Button: { child: 'hdr-btn-label' } },
              },
              {
                id: 'hdr-btn-label',
                component: { Text: { text: { literalString: 'Header Action' } } },
              },
              {
                id: 'hdr-input',
                component: { TextField: { label: { literalString: 'Filter' }, text: { literalString: '' } } },
              },
              {
                id: 'hdr-modal',
                component: {
                  Modal: {
                    entryPointChild: 'hdr-modal-trigger',
                    contentChild: 'modal-inner-card',
                  },
                },
              },
              {
                id: 'hdr-modal-trigger',
                component: { Text: { text: { literalString: 'Open Header Modal' } } },
              },
              {
                id: 'modal-inner-card',
                component: {
                  Card: {
                    expandable: { literalBoolean: true },
                    child: 'modal-inner-col',
                  },
                },
              },
              {
                id: 'modal-inner-col',
                component: {
                  Column: {
                    children: { explicitList: ['modal-inner-hdr', 'modal-inner-body'] },
                  },
                },
              },
              {
                id: 'modal-inner-hdr',
                component: { Text: { text: { literalString: 'Inner Modal Summary' } } },
              },
              {
                id: 'modal-inner-body',
                component: { Text: { text: { literalString: 'Inner Modal Details' } } },
              },
              {
                id: 'outer-body',
                component: { Text: { text: { literalString: 'Outer Body' } } },
              },
            ],
          },
        });
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();

      const root = fixture.nativeElement as HTMLElement;
      const outerCardEl = root.querySelector('.a2ui-Card-expandable[data-id="outer-card"]') as HTMLElement;
      const outerSummaryEl = outerCardEl.querySelector(':scope > .a2ui-Card-expandable-summary') as HTMLElement;
      const hdrBtnEl = outerSummaryEl.querySelector('.a2ui-Button[data-id="hdr-btn"] button') as HTMLButtonElement;
      const hdrInputEl = outerSummaryEl.querySelector('.a2ui-TextField[data-id="hdr-input"] input') as HTMLInputElement;
      const modalTriggerEl = outerSummaryEl.querySelector('.a2ui-Modal[data-id="hdr-modal"] > button.modal-entry-point') as HTMLButtonElement;
      const hdrDialogEl = outerSummaryEl.querySelector('.a2ui-Modal[data-id="hdr-modal"] > dialog.a2ui-modal-dialog') as HTMLDialogElement;
      const dialogSpies = stubDialog(hdrDialogEl);

      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      // Interactive <button> and <input> inside summary must not have Enter/Space hijacked
      const btnEnterEvt = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      hdrBtnEl.dispatchEvent(btnEnterEvt);
      fixture.detectChanges();
      expect(btnEnterEvt.defaultPrevented).toBe(false);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      const btnSpaceEvt = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
      hdrBtnEl.dispatchEvent(btnSpaceEvt);
      fixture.detectChanges();
      expect(btnSpaceEvt.defaultPrevented).toBe(false);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      const inputSpaceEvt = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
      hdrInputEl.dispatchEvent(inputSpaceEvt);
      fixture.detectChanges();
      expect(inputSpaceEvt.defaultPrevented).toBe(false);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      const inputEnterEvt = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      hdrInputEl.dispatchEvent(inputEnterEvt);
      fixture.detectChanges();
      expect(inputEnterEvt.defaultPrevented).toBe(false);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      // Enter/Space directly on the summary element toggles the card with defaultPrevented === true
      const summaryEnterEvt = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      outerSummaryEl.dispatchEvent(summaryEnterEvt);
      fixture.detectChanges();
      expect(summaryEnterEvt.defaultPrevented).toBe(true);
      expect(outerCardEl.classList.contains('expanded')).toBe(true);

      const summarySpaceEvt = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
      outerSummaryEl.dispatchEvent(summarySpaceEvt);
      fixture.detectChanges();
      expect(summarySpaceEvt.defaultPrevented).toBe(true);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      // Open header Modal -> contentChild: Card(expandable); Enter/Space on inner summary toggles ONLY inner card
      modalTriggerEl.click();
      fixture.detectChanges();
      await fixture.whenStable();
      await Promise.resolve();
      fixture.detectChanges();
      expect(dialogSpies.showModal).toHaveBeenCalledTimes(1);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      const innerCardEl = hdrDialogEl.querySelector('.a2ui-Card-expandable[data-id="modal-inner-card"]') as HTMLElement;
      const innerSummaryEl = innerCardEl.querySelector(':scope > .a2ui-Card-expandable-summary') as HTMLElement;
      expect(innerCardEl.classList.contains('expanded')).toBe(false);

      const innerEnterEvt = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      innerSummaryEl.dispatchEvent(innerEnterEvt);
      fixture.detectChanges();
      expect(innerEnterEvt.defaultPrevented).toBe(true);
      expect(innerCardEl.classList.contains('expanded')).toBe(true);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      const innerSpaceEvt = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
      innerSummaryEl.dispatchEvent(innerSpaceEvt);
      fixture.detectChanges();
      expect(innerSpaceEvt.defaultPrevented).toBe(true);
      expect(innerCardEl.classList.contains('expanded')).toBe(false);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      // Escape keydown inside <dialog> must not be preventDefault()-ed by Angular event bindings
      const escapeEvt = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
      innerSummaryEl.dispatchEvent(escapeEvt);
      fixture.detectChanges();
      expect(escapeEvt.defaultPrevented).toBe(false);
      expect(outerCardEl.classList.contains('expanded')).toBe(false);

      fixture.destroy();
    } finally {
      TestBed.resetTestingModule();
    }
  });

  it('toggles Cached/Live via Ctrl+L without double-initializing onInitAgent or wiping active chat', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const flushMicrotasks = async () => {
      for (let i = 0; i < 10; i++) {
        await Promise.resolve();
      }
    };

    const cachedMessagesToggle = signal(0);
    const activeDemo = signal('1');
    const reset = signal(0);
    const showModeSwitchFeedback = vi.fn();
    const demoService = {
      activeDemo,
      cachedMessagesToggle,
      reset,
      modeSwitchLabel: signal(null),
      showModeSwitchFeedback,
    };

    let spawnSeq = 0;
    const gateway = {
      chat$: new Subject<ChatMessage>(),
      sendBroadcast: vi.fn().mockResolvedValue(undefined),
      addAgent: vi.fn().mockImplementation(async () => `sess-${++spawnSeq}`),
      removeAgent: vi.fn(),
      setFilterSettings: vi.fn(),
      getOrganizerGuid: vi.fn().mockReturnValue(null),
      removeCurrentSimulationId: vi.fn(),
      resetDemoChatPipelineState: vi.fn(),
      getAgents: vi.fn().mockReturnValue([]),
    };

    try {
      TestBed.configureTestingModule({
        providers: [
          provideZonelessChangeDetection(),
          AgentSimulationStatsService,
          AgentPanelTabExpansionService,
          AgentGatewayAgentsService,
          { provide: AgentWindowEventsService, useValue: { bind: vi.fn(), seedGatewayDumpFlag: vi.fn(), start: vi.fn(), stop: vi.fn() } },
          { provide: AgentRuntimeControlsService, useValue: { register: vi.fn(), unregister: vi.fn(), notifyUpdate: vi.fn() } },
          { provide: DemoService, useValue: demoService },
          AgentChatDisplayService,
          AgentDemoSessionService,
          AgentCachedGatewayReplayService,
          { provide: ElementRef, useValue: new ElementRef(document.createElement('div')) },
          { provide: ChangeDetectorRef, useValue: { markForCheck: vi.fn() } },
          { provide: GatewayService, useValue: gateway },
        ],
      });

      const screen = TestBed.runInInjectionContext(
        () =>
          new AgentScreenComponent(
            TestBed.inject(NgZone),
            TestBed.inject(ChangeDetectorRef),
            TestBed.inject(GatewayService),
          ),
      );
      const initSpy = vi.spyOn(screen, 'onInitAgent');
      screen.ngOnInit();

      // Run initial constructor effects once.
      TestBed.flushEffects();
      await flushMicrotasks();

      expect(initSpy).toHaveBeenCalledTimes(1);
      expect(gateway.addAgent).toHaveBeenCalledTimes(1);
      expect(screen.runCachedMessages).toBe(false);
      expect(screen.currentAgent?.sessionId).toBe('sess-1');

      // Seed active live chat messages before toggling Ctrl+L.
      const liveMsg: ChatMessage = {
        guid: 'sess-1',
        speaker: 'You',
        text: 'Preserve active rehearsal context',
        isUser: true,
        timestamp: new Date(),
        msgType: AgentMessageType.TEXT,
      };
      screen.chatMessages = [liveMsg];
      screen.activeDemoHasRecordingConfig = true;

      // 1. Live -> Cached via Ctrl+L: preserves active chatMessages and live agent session, no re-init.
      cachedMessagesToggle.update((n) => n + 1);
      TestBed.flushEffects();
      await flushMicrotasks();

      expect(screen.runCachedMessages).toBe(true);
      expect(screen.chatMessages).toEqual([liveMsg]);
      expect(initSpy).toHaveBeenCalledTimes(1);
      expect(gateway.addAgent).toHaveBeenCalledTimes(1);
      expect(gateway.removeAgent).not.toHaveBeenCalled();
      expect(showModeSwitchFeedback).toHaveBeenLastCalledWith(true);

      // 2. Cached -> Live via Ctrl+L when no agent was spawned yet (simulate Cached-first session):
      // onInitAgent must fire ONCE (never twice) and chatMessages must not be wiped.
      screen.gatewayAgents.agents = {};
      screen.currentAgent = { agentType: 'leadership_planner', sessionId: '' };
      cachedMessagesToggle.update((n) => n + 1);
      TestBed.flushEffects();
      await flushMicrotasks();

      // Wait one more turn to ensure no delayed duplicate spawn resolves.
      await flushMicrotasks();
      expect(screen.runCachedMessages).toBe(false);
      expect(initSpy).toHaveBeenCalledTimes(2);
      expect(gateway.addAgent).toHaveBeenCalledTimes(2);
      expect(gateway.removeAgent).not.toHaveBeenCalled();
      expect(screen.currentAgent?.sessionId).toBe('sess-2');
      expect(screen.chatMessages).toEqual([liveMsg]);
      expect(showModeSwitchFeedback).toHaveBeenLastCalledWith(false);

      // 3. Ctrl+L on a demo without recordingConfig is a strict no-op (no wipe, no respawn).
      screen.activeDemoHasRecordingConfig = false;
      cachedMessagesToggle.update((n) => n + 1);
      TestBed.flushEffects();
      await flushMicrotasks();

      expect(screen.runCachedMessages).toBe(false);
      expect(screen.chatMessages).toEqual([liveMsg]);
      expect(initSpy).toHaveBeenCalledTimes(2);
      expect(gateway.addAgent).toHaveBeenCalledTimes(2);
      expect(gateway.removeAgent).not.toHaveBeenCalled();

      // 4. Switching activeDemo afterwards must NOT re-trigger Effect 1 (untracked setRunCachedMessages).
      const feedbackCallsBeforeDemoSwitch = showModeSwitchFeedback.mock.calls.length;
      activeDemo.set('5a');
      TestBed.flushEffects();
      await flushMicrotasks();
      expect(showModeSwitchFeedback).toHaveBeenCalledTimes(feedbackCallsBeforeDemoSwitch);

      screen.ngOnDestroy();
    } finally {
      TestBed.resetTestingModule();
    }
  });

  it('unlocks Live composer and clears Thinking state when Ctrl+L interrupts an active cached replay mid-stream', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const flushMicrotasks = async () => {
      for (let i = 0; i < 10; i++) {
        await Promise.resolve();
      }
    };

    const cachedMessagesToggle = signal(0);
    const activeDemo = signal('1');
    const reset = signal(0);
    const showModeSwitchFeedback = vi.fn();
    const demoService = {
      activeDemo,
      cachedMessagesToggle,
      reset,
      modeSwitchLabel: signal(null),
      showModeSwitchFeedback,
    };

    let spawnSeq = 0;
    const gateway = {
      chat$: new Subject<ChatMessage>(),
      sendBroadcast: vi.fn().mockResolvedValue(undefined),
      addAgent: vi.fn().mockImplementation(async () => `sess-live-${++spawnSeq}`),
      removeAgent: vi.fn(),
      setFilterSettings: vi.fn(),
      getOrganizerGuid: vi.fn().mockReturnValue(null),
      removeCurrentSimulationId: vi.fn(),
      resetDemoChatPipelineState: vi.fn(),
      getAgents: vi.fn().mockReturnValue([]),
    };

    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            reject(new DOMException('Aborted', 'AbortError'));
          });
        }),
    );

    try {
      const composerHtml = readFileSync(
        resolve(__dirname, 'agent-chat-composer/agent-chat-composer.component.html'),
        'utf-8',
      );

      @Component({
        standalone: true,
        changeDetection: ChangeDetectionStrategy.Default,
        imports: [AgentChatComposerComponent],
        template: `
          <app-agent-chat-composer
            [text]="host.chatInput"
            (textChange)="host.chatInput = $event"
            [placeholder]="host.chatInputPlaceholder"
            [isAgentWorking]="host.isAgentWorking"
            [canSend]="host.canSend"
            [isSecuringAgent]="host.isSecuringAgent"
            (send)="host.onSend()"
          />
        `,
      })
      class ComposerHostComponent {
        host!: AgentScreenComponent;
      }

      const cdrMarkForCheckSpy = vi.fn();
      TestBed.configureTestingModule({
        imports: [ComposerHostComponent],
        providers: [
          provideZonelessChangeDetection(),
          AgentSimulationStatsService,
          AgentPanelTabExpansionService,
          AgentGatewayAgentsService,
          { provide: AgentWindowEventsService, useValue: { bind: vi.fn(), seedGatewayDumpFlag: vi.fn(), start: vi.fn(), stop: vi.fn() } },
          { provide: AgentRuntimeControlsService, useValue: { register: vi.fn(), unregister: vi.fn(), notifyUpdate: vi.fn() } },
          { provide: DemoService, useValue: demoService },
          AgentChatDisplayService,
          AgentDemoSessionService,
          AgentCachedGatewayReplayService,
          { provide: ElementRef, useValue: new ElementRef(document.createElement('div')) },
          { provide: ChangeDetectorRef, useValue: { markForCheck: cdrMarkForCheckSpy } },
          { provide: GatewayService, useValue: gateway },
        ],
      }).overrideComponent(AgentChatComposerComponent, {
        set: { template: composerHtml, styleUrls: [] },
      });

      await TestBed.compileComponents();
      const screen = TestBed.runInInjectionContext(
        () =>
          new AgentScreenComponent(
            TestBed.inject(NgZone),
            TestBed.inject(ChangeDetectorRef),
            TestBed.inject(GatewayService),
          ),
      );
      screen.ngOnInit();

      const fixture = TestBed.createComponent(ComposerHostComponent);
      fixture.componentInstance.host = screen;
      cdrMarkForCheckSpy.mockImplementation(() => {
        fixture.changeDetectorRef.markForCheck();
      });

      fixture.detectChanges();
      TestBed.flushEffects();
      await flushMicrotasks();
      fixture.detectChanges();

      // Switch to Cached mode and start a cached replay so isAgentWorking becomes true mid-stream.
      screen.activeDemoHasRecordingConfig = true;
      await screen.setRunCachedMessages(true);
      fixture.detectChanges();

      screen.chatInput = 'Start cached replay';
      screen.onSend();
      fixture.detectChanges();
      await flushMicrotasks();
      fixture.detectChanges();

      const wrapperDiv = fixture.nativeElement.querySelector('.agent-chat-composer__input') as HTMLElement;
      const textarea = fixture.nativeElement.querySelector('textarea.agent-chat-composer__textarea') as HTMLTextAreaElement;
      const sendBtn = fixture.nativeElement.querySelector('button.agent-chat-composer__send') as HTMLButtonElement;
      expect(screen.runCachedMessages).toBe(true);
      expect(screen.isAgentWorking).toBe(true);
      expect(textarea.disabled).toBe(true);
      expect(textarea.placeholder).toContain('Thinking');
      expect(sendBtn.disabled).toBe(true);
      expect(wrapperDiv.classList.contains('agent-chat-composer__input--loading')).toBe(true);

      // Press Ctrl+L (cachedMessagesToggle) mid-replay to switch back to Live mode.
      cachedMessagesToggle.update((n) => n + 1);
      TestBed.flushEffects();
      await flushMicrotasks();
      fixture.detectChanges();
      await flushMicrotasks();
      fixture.detectChanges();

      expect(screen.runCachedMessages).toBe(false);
      expect(screen.isAgentWorking).toBe(false);
      expect(screen.loadingMessage).toBe('Thinking...');
      expect(screen.canSend).toBe(true);
      expect(textarea.disabled).toBe(false);
      expect(textarea.placeholder).toBe('Write something here');
      expect(wrapperDiv.classList.contains('agent-chat-composer__input--loading')).toBe(false);

      // Typing a prompt in Live mode enables the send button and dispatches sendBroadcast.
      textarea.value = 'Hello in live mode';
      textarea.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      expect(sendBtn.disabled).toBe(false);

      sendBtn.click();
      fixture.detectChanges();
      expect(gateway.sendBroadcast).toHaveBeenCalledTimes(1);
      expect(gateway.sendBroadcast).toHaveBeenCalledWith('Hello in live mode', ['sess-live-1']);

      // Direct stopCachedDataStream() must also clear an active isAgentWorking lock.
      screen.isAgentWorking = true;
      screen.stopCachedDataStream();
      expect(screen.isAgentWorking).toBe(false);

      screen.ngOnDestroy();
      fixture.destroy();
    } finally {
      fetchSpy.mockRestore();
      TestBed.resetTestingModule();
    }
  });

  it('clears AgentGatewayAgentsService and isAgentWorking on binary environment_reset and unlocks composer on TextMessage JSON error frames', async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    const flushMicrotasks = async () => {
      for (let i = 0; i < 10; i++) {
        await Promise.resolve();
      }
    };

    const cachedMessagesToggle = signal(0);
    const activeDemo = signal('1');
    const reset = signal(0);
    const demoService = {
      activeDemo,
      cachedMessagesToggle,
      reset,
      modeSwitchLabel: signal(null),
      showModeSwitchFeedback: vi.fn(),
    };

    let spawnSeq = 0;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(
        JSON.stringify({
          sessions: [{ sessionId: `sess-ws-${++spawnSeq}`, agentType: 'leadership_planner_with_eval' }],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const internalGateway = agentGateway as any;
    const prevWrapper = internalGateway.Wrapper;
    const prevBroadcastRequest = internalGateway.BroadcastRequest;
    const prevProtoPromise = internalGateway.protoLoadPromise;
    const prevMonitorSocket = internalGateway.monitorSocket;

    try {
      const protoRoot = await protobuf.load(resolve(process.cwd(), 'public/gateway.proto'));
      const WrapperType = protoRoot.lookupType('gateway.Wrapper');
      const BroadcastRequestType = protoRoot.lookupType('gateway.BroadcastRequest');
      internalGateway.Wrapper = WrapperType;
      internalGateway.BroadcastRequest = BroadcastRequestType;
      internalGateway.protoLoadPromise = Promise.resolve();
      internalGateway.monitorSocket = { readyState: 1, send: vi.fn(), close: vi.fn() };
      internalGateway.disposed = false;
      internalGateway.sessions.clear();
      internalGateway.userSpawnedAgents.clear();
      internalGateway.subscribedSimulationId = null;

      TestBed.configureTestingModule({
        providers: [
          provideZonelessChangeDetection(),
          AgentSimulationStatsService,
          AgentPanelTabExpansionService,
          AgentGatewayAgentsService,
          { provide: AgentWindowEventsService, useValue: { bind: vi.fn(), seedGatewayDumpFlag: vi.fn(), start: vi.fn(), stop: vi.fn() } },
          { provide: AgentRuntimeControlsService, useValue: { register: vi.fn(), unregister: vi.fn(), notifyUpdate: vi.fn() } },
          { provide: DemoService, useValue: demoService },
          AgentChatDisplayService,
          AgentDemoSessionService,
          AgentCachedGatewayReplayService,
          { provide: ElementRef, useValue: new ElementRef(document.createElement('div')) },
          { provide: ChangeDetectorRef, useValue: { markForCheck: vi.fn() } },
          {
            provide: GatewayService,
            useFactory: (ngZone: NgZone) => new GatewayService(ngZone),
            deps: [NgZone],
          },
        ],
      });

      const gwService = TestBed.inject(GatewayService);
      const screen = TestBed.runInInjectionContext(
        () =>
          new AgentScreenComponent(
            TestBed.inject(NgZone),
            TestBed.inject(ChangeDetectorRef),
            gwService,
          ),
      );
      screen.ngOnInit();
      TestBed.flushEffects();
      await flushMicrotasks();

      const activeType = screen.currentAgent!.agentType;
      const firstSid = screen.agents[activeType]!;
      expect(firstSid).toBe('sess-ws-1');
      expect(agentGateway.hasSession(firstSid)).toBe(true);

      // 1. Binary environment_reset Wrapper with omitted payload & subscribedSimulationId === null
      screen.isAgentWorking = true;
      const resetWrapper = WrapperType.create({
        timestamp: new Date().toISOString(),
        type: 'environment_reset',
        event: 'environment_reset',
        origin: { type: 'system', id: 'gw-1' },
        destination: [firstSid],
      });
      const resetBuf = new Uint8Array(WrapperType.encode(resetWrapper).finish()).buffer;
      expect(() => agentGateway.handleMessage({ data: resetBuf } as MessageEvent)).not.toThrow();
      await flushMicrotasks();

      expect(agentGateway.getSessions()).toEqual([]);
      expect(screen.agents[activeType]).toBeNull();
      expect(screen.currentAgent).toBeNull();
      expect(screen.isAgentWorking).toBe(false);

      // 2. Re-init session, send prompt (isAgentWorking = true), and deliver Go Hub TextMessage JSON error
      //    matching real global observer /ws wire output (destination: null + data.session_id)
      await screen.onInitAgent(activeType);
      const expiredSid = screen.agents[activeType]!;
      expect(expiredSid).toBe('sess-ws-2');
      screen.currentAgent = { agentType: activeType, sessionId: expiredSid };
      screen.chatInput = 'Test prompt on expired session';
      screen.onSend();
      expect(screen.isAgentWorking).toBe(true);

      const errFrame = JSON.stringify({
        origin: { type: 'system', id: 'gateway' },
        destination: null,
        status: 'error',
        type: 'error',
        event: 'forbidden',
        data: {
          error: 'session_not_owned',
          session_id: expiredSid,
          details: 'target session is not owned by user',
        },
      });
      expect(() => agentGateway.handleMessage({ data: errFrame } as MessageEvent)).not.toThrow();
      await flushMicrotasks();

      expect(agentGateway.hasSession(expiredSid)).toBe(false);
      expect(screen.agents[activeType]).toBeNull();
      expect(screen.currentAgent).toBeNull();
      expect(screen.isAgentWorking).toBe(false);
      expect(screen.canSend).toBe(false);
      expect(
        screen.displayItems.some(
          (item) =>
            item.kind === 'message' &&
            item.msg.msgType === AgentMessageType.MODEL_ERROR &&
            item.msg.text === 'target session is not owned by user',
        ),
      ).toBe(true);

      // 3. Re-init again and verify eviction even when destination is null AND data.session_id is absent
      //    (using tracked lastBroadcastTargetGuids fallback from screen.onSend()).
      await screen.onInitAgent(activeType);
      const expiredSidFallback = screen.agents[activeType]!;
      expect(expiredSidFallback).toBe('sess-ws-3');
      screen.currentAgent = { agentType: activeType, sessionId: expiredSidFallback };
      screen.chatInput = 'Second prompt on expired session';
      screen.onSend();
      await flushMicrotasks();
      expect(screen.isAgentWorking).toBe(true);

      const errFrameNoTarget = JSON.stringify({
        origin: { type: 'system', id: 'gateway' },
        destination: null,
        status: 'error',
        type: 'error',
        event: 'forbidden',
        data: {
          error: 'session_not_owned',
          details: 'target session is not owned by user',
        },
      });
      expect(() => agentGateway.handleMessage({ data: errFrameNoTarget } as MessageEvent)).not.toThrow();
      await flushMicrotasks();

      expect(agentGateway.hasSession(expiredSidFallback)).toBe(false);
      expect(screen.agents[activeType]).toBeNull();
      expect(screen.currentAgent).toBeNull();
      expect(screen.isAgentWorking).toBe(false);
      expect(screen.canSend).toBe(false);

      screen.ngOnDestroy();
      gwService.ngOnDestroy();
    } finally {
      internalGateway.sessions.clear();
      internalGateway.userSpawnedAgents.clear();
      internalGateway.subscribedSimulationId = null;
      internalGateway.Wrapper = prevWrapper;
      internalGateway.BroadcastRequest = prevBroadcastRequest;
      internalGateway.protoLoadPromise = prevProtoPromise;
      internalGateway.monitorSocket = prevMonitorSocket;
      internalGateway.disposed = false;
      fetchSpy.mockRestore();
      TestBed.resetTestingModule();
    }
  });
});
