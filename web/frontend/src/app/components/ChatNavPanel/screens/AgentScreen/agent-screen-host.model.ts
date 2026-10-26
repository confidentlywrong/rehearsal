/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 */

import { InjectionToken } from '@angular/core';
import type { ChangeDetectorRef, ElementRef } from '@angular/core';
import type { AnimationEvent } from '@angular/animations';
import type { ChatMessage } from '../../../../gateway.service';
import type { DemoService } from '../../../DemoOverlay/demo.service';
import type { DisplayItem } from './agent-screen.types';
import type { AgentChatDisplayService } from './services/agent-chat-display.service';

/**
 * Narrow surface passed to Agent tab child components (replaces `AgentScreenComponent` in `@Input()` types).
 */
export interface AgentScreenHost {
  readonly chatDisplay: AgentChatDisplayService;
  readonly demoService: DemoService;
  readonly cdr: ChangeDetectorRef;

  chatMessages: ChatMessage[];
  chatInput: string;
  chatTextAreaRef?: ElementRef<HTMLTextAreaElement>;
  agents: Record<string, string | null>;
  currentAgent: { agentType: string; sessionId: string } | null;
  filterSettings: {
    showToolCalls: boolean;
    showLoadSkills: boolean;
    /**
     * Coarse preset that controls how much of the agent's inner monologue
     * (raw tool calls, load_skill spam, mid-run a2ui surfaces) is surfaced
     * in the chat thread.
     *  - 'leadership': only user/agent text, tick feed, and coaching A2UI.
     *  - 'all': adds tool calls and other a2ui surfaces.
     *  - 'raw': adds load_skill events and the full transcript.
     */
    chatFilterTier: 'leadership' | 'all' | 'raw';
  };
  isSecuringAgent: boolean;
  isAgentWorking: boolean;
  finalToolCallMessage: DisplayItem | null;
  errorMessageInterval: number | null;

  get displayItems(): DisplayItem[];
  get expandedToolCalls(): Set<number>;

  get chatInputPlaceholder(): string;
  get canSend(): boolean;
  get secureMode(): boolean;
  get cloudAssistMode(): boolean;

  agentGatewayMsgDumpCanDownload: boolean;
  readonly agentGatewayMsgDebugDownload: () => void;
  runCachedMessages: boolean;
  activeDemoHasRecordingConfig: boolean;
  cachedMessageTimeScale: number;
  isSecurityDemo: boolean;
  isIntentToInfrastructureDemo: boolean;

  setRunCachedMessages(value: boolean, options?: { showModeFeedback?: boolean }): Promise<void>;
  onCachedMessageTimeScaleChange(value: string | number): void;
  setPlannerSecurity(secure: boolean): void;
  setCloudAssistMode(value: boolean): Promise<void>;
  clampSnapCachedMessageTimeScale(value: number): number;

  setActiveTab(tab: 'agent' | 'organizer' | 'log'): void;
  onTabLabelAnimationDone(event: AnimationEvent): void;
  // 2026-06-22 B-13: `togglePanelExpanded(): void;` removed from the
  // AgentScreenHost contract. After the gear/unfold buttons were
  // pulled out of the agent panel header, no external (Input-bound)
  // consumer reaches this method through the host indirection
  // anymore. AgentScreenComponent still defines the method for its
  // own internal callers and for AgentPanelTabExpansionService.

  onChatScroll(event?: Event): void;
  toggleToolCall(idx: number): void;
  onSend(): void;
  autoResize(): void;
  /** Switch the chat thread between Leadership / All / Raw verbosity tiers. */
  setChatFilterTier(tier: 'leadership' | 'all' | 'raw'): void;
  runCachedDataStream(): Promise<void>;
}

/**
 * Injection token for the AgentScreenHost interface.
 * AgentScreenComponent provides itself via this token so that
 * downstream services (e.g. A2uiActionsService) depend on the
 * interface, not the concrete class — breaking the import cycle.
 */
export const AGENT_SCREEN_HOST = new InjectionToken<AgentScreenHost>('AgentScreenHost');
