/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 */

import type { ChangeDetectorRef, ElementRef } from '@angular/core';
import { Injectable } from '@angular/core';
import type { ChatMessage } from '../../../../../gateway.service';
import { agentGateway } from '../../../../../agent-gateway-updates';
import { DEMO_CONFIG } from '../../../../../demo-config';
import { AgentMessageType } from '../../../../../types';
import { demoFiveSpeaker } from '../../../../../../constants';
import type { DemoService } from '../../../../DemoOverlay/demo.service';
import type { DisplayItem } from '../agent-screen.types';
import { getToolDisplay, getToolCallLabel, shouldSynthesizeBody, synthesizeLeadershipBody, applyResultRewrites } from '../../../../../a2ui-tool-vocabulary';

/** Structural subset of AgentScreen passed into display processing (avoids circular imports). */
export interface AgentChatDisplayHost {
  cdr: ChangeDetectorRef;
  agents: Record<string, string | null>;
  currentAgent: { agentType: string; sessionId: string } | null;
  chatMessages: ChatMessage[];
  chatInput: string;
  chatTextAreaRef?: ElementRef<HTMLTextAreaElement>;
  demoService: DemoService;
  filterSettings: {
    showToolCalls: boolean;
    showLoadSkills: boolean;
    chatFilterTier: 'leadership' | 'all' | 'raw';
  };
  isSecuringAgent: boolean;
  isAgentWorking: boolean;
  finalToolCallMessage: DisplayItem | null;
  errorMessageInterval: number | null;
  resetSimulationStatistics(): void;
  handleRaceStarted(): void;
  handleTickUpdate(d: Record<string, unknown>): void;
  scrollChatToBottom(): void;
  autoResize(): void;
}

@Injectable()
export class AgentChatDisplayService {
  displayItems: DisplayItem[] = [];
  expandedToolCalls = new Set<number>();
  activeToolCalls = new Map<string, number[]>();
  /** Tracks the last TEXT message text per session to deduplicate MODEL_END echoes. */
  private _lastTextBySession = new Map<string, string>();
  /** Maps A2UI surfaceId → displayItems index for update-in-place dedup. */
  private _surfaceIndex = new Map<string, number>();

  /** Emit-* artifact tools that should render as inline cards in the chat thread. */
  private static readonly _ARTIFACT_TOOLS = new Set([
    'emit_stakeholder_graph',
    'emit_stakeholder_personas',
    'emit_stakeholder_reactions',
    'emit_simulation_timeline',
    'emit_simulation_report',
    'emit_stakeholder_interview',
    'emit_translation_diagnostic',
    'emit_stakeholder_psychology',
    'emit_sim_results_a2ui',
    'emit_action_plan_a2ui',
  ]);

  private _isArtifactTool(toolName: string | undefined | null): boolean {
    return !!toolName && AgentChatDisplayService._ARTIFACT_TOOLS.has(toolName);
  }

  clearForDemoReset(): void {
    this.displayItems = [];
    this.expandedToolCalls.clear();
    this.activeToolCalls.clear();
    this._lastTextBySession.clear();
    this._surfaceIndex.clear();
  }

  toggleToolCall(idx: number): void {
    if (this.expandedToolCalls.has(idx)) {
      this.expandedToolCalls.delete(idx);
    } else {
      this.expandedToolCalls.add(idx);
    }
  }

  /**
   * Leadership-vocabulary title for the tool-call card.
   *
   * Falls back to the raw tool name when the vocabulary file has no entry
   * for it.  Translation table lives in `a2ui-tool-vocabulary.ts`.
   */
  toolDisplayTitle(toolName: string | undefined | null): string {
    return getToolDisplay(toolName).title;
  }

  /**
   * Optional grey-subtitle string explaining the call's leadership INTENT
   * (e.g. `maps_search_places` → "anchoring stakeholder personas to
   * real-world venues for the DCNS influence graph").  Empty string when
   * no intent is configured.
   */
  toolDisplayIntent(toolName: string | undefined | null): string {
    return getToolDisplay(toolName).intent ?? '';
  }

  /**
   * Single-line label shown on the tool-call card.
   *
   * For tools whose primary argument carries semantic meaning (e.g.
   * `maps_search_places.textQuery`), this returns the argument — rewritten
   * to a leadership-intent phrase via `LEADERSHIP_QUERY_REWRITES` when a
   * pattern matches, otherwise shown literally.  For tools without a
   * salient argument, it falls back to the renamed tool verb from
   * `TOOL_DISPLAY`.
   *
   * The gateway stores the salient argument on `msg.skillName` (see the
   * `tool_start` handler in `agent-gateway-updates.ts`, which extracts
   * `tool_hints.textQuery || tool_hints.query`).
   */
  toolCallLabel(msg: { toolName?: string | null; skillName?: string | null } | null | undefined): string {
    if (!msg) return '';
    return getToolCallLabel(msg.toolName, msg.skillName);
  }

  /**
   * Whether the tool-result body for this message should be replaced with
   * a synthesized leadership-meta line.  True for the Maps Grounding-Lite
   * tools whose raw result is physical-world description; the synthesis
   * itself happens in `displayText()`.  This getter is retained so the
   * template can keep its body-div visible while the body content is
   * authored by `displayText()` rather than the agent backend.
   */
  shouldSynthesizeToolResultBody(msg: { toolName?: string | null } | null | undefined): boolean {
    if (!msg) return false;
    return shouldSynthesizeBody(msg.toolName);
  }

  displayText(m: ChatMessage): string {
    const toolName = m.toolName ?? '';
    // Maps_* tools: ALWAYS substitute a leadership-meta line, regardless of
    // whether the agent emitted a result body.  Keeps the card framed as
    // leadership context instead of leaking raw Places / weather prose.
    if (shouldSynthesizeBody(toolName)) {
      return synthesizeLeadershipBody(toolName, getToolCallLabel(toolName, m.skillName));
    }

    if (m.msgType === 'tool_end' && m.text) {
      try {
        const parsed = JSON.parse(m.text);

        // Handle MCP content array format: [{"type": "text", "text": "..."}]
        if (Array.isArray(parsed) && parsed.length > 0) {
          const textBlocks = parsed.filter(block => block.type === 'text' && block.text);
          if (textBlocks.length > 0) {
            return applyResultRewrites(textBlocks.map(block => block.text).join('\n\n'));
          }
        }

        const parsedObj = parsed as {
          result?: { message?: string };
          message?: string;
        };
        const raw = parsedObj?.result?.message || parsedObj?.message || '';
        const rewritten = applyResultRewrites(raw);
        // Fall back to the tool's leadership-intent line whenever the agent
        // emitted a trivial body (empty, bare tool name, or just "Done") so
        // the card never reads as visually empty in the leadership tier.
        if (!rewritten || rewritten === toolName || rewritten === 'Done') {
          const synth = synthesizeLeadershipBody(toolName, getToolCallLabel(toolName, m.skillName));
          return synth || rewritten;
        }
        return rewritten;
      } catch {
        /* fall through */
      }
    }

    const rewritten = applyResultRewrites(m.text);
    if (!rewritten || rewritten === toolName) {
      const synth = synthesizeLeadershipBody(toolName, getToolCallLabel(toolName, m.skillName));
      return synth || rewritten;
    }
    return rewritten;
  }

  trackByDisplayItem(i: number, item: DisplayItem): string {
    if (item.kind === 'tick_feed') {
      return `tick_feed:${item.tick}`;
    }
    if ('msg' in item && item.msg) {
      const m = item.msg;
      const ts = m.timestamp instanceof Date ? m.timestamp.getTime() : 0;
      return `${item.kind}:${m.guid ?? ''}:${m.msgType ?? ''}:${m.toolName ?? ''}:${ts}:${i}`;
    }
    if (item.kind === 'a2ui') {
      const sid = (item.node as Record<string, unknown>)?.['surfaceUpdate'] as Record<string, unknown> | undefined;
      const surfaceId = sid?.['surfaceId'] as string | undefined;
      return surfaceId ? `a2ui:${surfaceId}` : `a2ui:${i}`;
    }
    return `${(item as DisplayItem).kind}:${i}`;
  }

  setToolCallToWarning(host: AgentChatDisplayHost): void {
    const toolCallToWarn = this.displayItems.find((item) => {
      if (!('msg' in item)) return false;
      return (
        item.msg.speaker.includes('simulation_pipeline') &&
        item.msg.toolName === 'prepare_simulation'
      );
    });
    if (!toolCallToWarn || toolCallToWarn.kind !== 'tool_call') return;

    toolCallToWarn.done = true;
    toolCallToWarn.warning = true;
    toolCallToWarn.msg = { ...toolCallToWarn.msg, text: this.displayText(toolCallToWarn.msg) };

    this.displayItems = [...this.displayItems];
    host.cdr.markForCheck();
  }

  addDisplayItem(host: AgentChatDisplayHost, item: DisplayItem): void {
    if (host.isSecuringAgent) return;
    this.displayItems = [...this.displayItems, item];
    host.cdr.markForCheck();
  }

  /**
   * Add an A2UI surface item, or replace an existing one with the same surfaceId.
   * This enables persistent surfaces (like the live dashboard) to update in-place
   * instead of appending duplicate cards on every tick.
   */
  private _addOrReplaceSurface(host: AgentChatDisplayHost, item: DisplayItem & { kind: 'a2ui' }): void {
    const su = (item.node as Record<string, unknown>)?.['surfaceUpdate'] as Record<string, unknown> | undefined;
    const surfaceId = su?.['surfaceId'] as string | undefined;

    if (surfaceId) {
      const existingIdx = this._surfaceIndex.get(surfaceId);
      if (existingIdx !== undefined && existingIdx < this.displayItems.length) {
        // Replace the existing surface in-place
        this.displayItems[existingIdx] = item;
        this.displayItems = [...this.displayItems];
        host.cdr.markForCheck();
        return;
      }
      // Track new surface position
      this._surfaceIndex.set(surfaceId, this.displayItems.length);
    }

    this.addDisplayItem(host, item);
  }

  finishToolCall(host: AgentChatDisplayHost, msg: ChatMessage): void {
    if (!msg.toolName) return;
    const key = msg.toolName === 'load_skill' ? (msg.skillName ?? msg.toolName) : msg.toolName;
    const indices = this.activeToolCalls.get(key);

    if (!indices || indices.length === 0) return;

    // Remove the first index from the list of concurrent tool calls
    const idx = indices.shift()!;

    const item = this.displayItems[idx];
    if (!item || item.kind !== 'tool_call') return;

    item.done = true;
    // Carry the salient argument captured at TOOL_START forward into the
    // TOOL_END message so `displayText()` can compute the leadership-
    // rewritten label (otherwise the body falls back to the generic tool
    // title because TOOL_END payloads do not include `skillName`).
    const merged: ChatMessage = { ...item.msg, ...msg, skillName: msg.skillName ?? item.msg.skillName };
    item.msg = { ...merged, text: this.displayText(merged) };
    if (indices.length === 0) {
      this.activeToolCalls.delete(key);
    }
    this.displayItems = [...this.displayItems];
    host.cdr.markForCheck();
  }

  processMessageForDisplay(host: AgentChatDisplayHost, msg: ChatMessage): void {
    if (msg.isUser) {
      this.addDisplayItem(host, { kind: 'message', msg });
      return;
    }

    const organizerGuid = agentGateway.getOrganizerGuid();
    if (organizerGuid && msg.guid === organizerGuid) {
      return;
    }

    if (msg.toolName === 'get_planned_routes_data') {
      return;
    }

    if (
      msg.result &&
      (msg.result.beginRendering || msg.result.surfaceUpdate) &&
      (msg.result.beginRendering?.surfaceId === 'route_list' ||
        msg.result.surfaceUpdate?.surfaceId === 'route_list')
    ) {
      return;
    }

    switch (msg.msgType) {
      case AgentMessageType.SYSTEM:
        this.addDisplayItem(host, {
          kind: 'system',
          msg,
        });
        return;

      case AgentMessageType.RUN_START:
        return;

      case AgentMessageType.RUN_END:
        if (host.currentAgent && msg.guid === host.agents[host.currentAgent.agentType]) {
          host.isAgentWorking = false;
        }

        host.isSecuringAgent = false;

        return;

      case AgentMessageType.MODEL_START:
        return;

      case AgentMessageType.MODEL_END:
        if (host.finalToolCallMessage) {
          this.displayItems = [...this.displayItems, host.finalToolCallMessage];
          host.finalToolCallMessage = null;
        }

        // MODEL_END often duplicates the preceding TEXT event, but the backend
        // wraps it in a prefix (e.g. "[Agent] Model End\n\n...").
        const lastText = this._lastTextBySession.get(msg.guid);
        const isDuplicate =
          lastText &&
          (msg.text === lastText || msg.text.includes(lastText.trim()));

        if (
          msg.text &&
          msg.speaker &&
          !isDuplicate
        ) {
          this.displayItems = [
            ...this.displayItems,
            {
              msg,
              kind: 'message',
            },
          ];
        }

        return;

      case AgentMessageType.TOOL_START:
        if (host.filterSettings.showToolCalls && msg.toolName) {
          const idx = this.displayItems.length;
          this.addDisplayItem(host, {
            kind: 'tool_call',
            msg: {
              ...msg,
              toolName: msg.skillName ? `${msg.toolName}: ${msg.skillName}` : msg.toolName,
              text: `tool start : ${msg.toolName}`,
            },
          });
          const key = msg.toolName === 'load_skill' ? msg.skillName! : msg.toolName;
          if (!this.activeToolCalls.has(key)) {
            this.activeToolCalls.set(key, []);
          }
          this.activeToolCalls.get(key)!.push(idx);
        }

        return;
      case AgentMessageType.TOOL_END:

        if (msg.result && msg.result.surfaceUpdate) {
          this._addOrReplaceSurface(host, { kind: 'a2ui', node: msg.result, guid: msg.guid });
          return;
        }

        // beginRendering signals that a surface is ready for display.
        // The a2ui-controller handles rendering via surfaceUpdate alone,
        // so no display item is needed — just acknowledge the message.
        if (msg.result && msg.result.beginRendering) {
          return;
        }

        // Emit-* artifact tools: render as message items with result attached.
        // The chat thread template renders these via dedicated card components
        // (same ones used in the Log tab).
        if (this._isArtifactTool(msg.toolName)) {
          // If tool calls are visible and we have an active card, finish it.
          // We STILL add a separate message item so the artifact card renders unconditionally.
          if (host.filterSettings.showToolCalls && msg.toolName && this.activeToolCalls.has(msg.toolName)) {
            this.finishToolCall(host, msg);
          }
          // Always show as standalone message so the custom Angular component renders
          this.addDisplayItem(host, { kind: 'message', msg });
          return;
        }

        if (msg.toolName === 'set_financial_modeling_mode') {
          this.displayItems = [];

          host.chatMessages = [];
          this.expandedToolCalls.clear();
          this.activeToolCalls.clear();

          const activeDemoKey = host.demoService.activeDemo();
          const activeDemo = DEMO_CONFIG[activeDemoKey] as {
            promptPlaceholder?: string;
          };

          if (activeDemo.promptPlaceholder) {
            if (host.chatTextAreaRef) {
              host.chatTextAreaRef.nativeElement.value = activeDemo.promptPlaceholder;
            }
            host.chatInput = activeDemo.promptPlaceholder;
            if (host.chatTextAreaRef) {
              host.autoResize();
            }
          }
        }

        if (msg.toolName === 'fire_start_gun') {
          host.resetSimulationStatistics();

          host.handleRaceStarted();
        }

        if (msg.toolName === 'advance_tick') {
          const d = msg.result as Record<string, unknown>;
          host.handleTickUpdate(d);

          // tick_feed cards are only shown in 'raw' (debug) tier.
          // In normal mode, the A2UI live-dashboard surface replaces them.
          const tier = host.filterSettings.chatFilterTier;
          if (tier === 'raw') {
            const tick = Number(d['tick']) || 0;
            const notable = (d['notable_events'] as string[]) || [];
            const micro = (d['micro_feedback'] as string) || '';
            const summary = (d['notable_events_summary'] as {
              transitions: Record<string, { total: number; by_persona: Record<string, number> }>;
              echoes: string[];
              other: string[];
            }) || { transitions: {}, echoes: [], other: [] };

            this.displayItems.push({
              kind: 'tick_feed',
              tick,
              maxTicks: Number(d['max_ticks']) || 0,
              voices: notable.filter((e: string) => e.startsWith('💭')),
              statusCounts: (d['status_counts'] as Record<string, number>) || {},
              microFeedback: micro,
              eventsSummary: summary,
              // Phase 1 enrichments
              completionPct: Number(d['completion_pct']) || 0,
              resourceDeltas: (d['resource_deltas'] as Record<string, {
                name: string; current: number; delta: number;
                critical_threshold: number; is_critical: boolean; is_warning: boolean;
              }>) || {},
              infrastructure: (d['infrastructure'] as {
                medical_saves_this_tick: number; medical_events: string[];
                avg_comfort: number; comfort_warning: boolean;
                active_gatekeepers: number; max_friction: number;
                friction_level: string; friction_details: Array<{ role: string; severity: number }>;
              }) || {
                medical_saves_this_tick: 0, medical_events: [],
                avg_comfort: 100, comfort_warning: false,
                active_gatekeepers: 0, max_friction: 0,
                friction_level: 'none', friction_details: [],
              },
              activeAlertsCount: Number(d['active_alerts_count']) || 0,
            });
            host.scrollChatToBottom();
          }
          return;
        }

        if (host.filterSettings.showToolCalls) {
          if (
            (msg.toolName && this.activeToolCalls.has(msg.toolName)) ||
            (msg.toolName === 'load_skill' && this.activeToolCalls.has(msg.skillName!))
          )
            this.finishToolCall(host, msg);
          return;
        }

        return;
      case AgentMessageType.TOOL_ERROR:
        if (host.filterSettings.showToolCalls) {
          if (msg.speaker.includes(demoFiveSpeaker)) {
            const indices = this.activeToolCalls.get('prepare_simulation');
            if (!indices || indices.length === 0) return;
            const idx = indices.shift()!;
            const item = this.displayItems[idx];
            if (!item || item.kind !== 'tool_call') return;
            if (host.demoService.activeDemo() === '5a') {
              setTimeout(() => {
                this.setToolCallToWarning(host);
              }, 1000);

              return;
            }

            item.done = true;
            item.error = true;
            item.msg = { ...item.msg, text: this.displayText(msg) };
            if (indices.length === 0) {
              this.activeToolCalls.delete('prepare_simulation');
            }
            this.displayItems = [...this.displayItems];
          }
        }

        host.isAgentWorking = false;

        this.addDisplayItem(host, { kind: 'message', msg });

        if (msg.speaker.toLowerCase().includes(demoFiveSpeaker)) {
          if (host.demoService.activeDemo() === '5a') {
            return;
          }

          // Clear any previously running error interval to prevent stacking
          if (host.errorMessageInterval) {
            clearInterval(host.errorMessageInterval);
            host.errorMessageInterval = null;
          }

          let count = 0;

          host.errorMessageInterval = window.setInterval(() => {
            count++;
            if (count >= 5) {
              clearInterval(host.errorMessageInterval as number);
              host.errorMessageInterval = null;
              return;
            }

            if (count === 3) {
              window.dispatchEvent(new CustomEvent('sim:reset'));
              window.dispatchEvent(new CustomEvent('sim:triggerError'));
            }

            host.cdr.markForCheck();
            setTimeout(() => host.scrollChatToBottom(), 0);
          }, 1000);
        }

        return;

      case AgentMessageType.INTER_AGENT:
        this.addDisplayItem(host, {
          kind: 'message',
          msg,
        });
        return;
      case AgentMessageType.TEXT:
        // Track for MODEL_END dedup
        if (msg.text && msg.guid) {
          this._lastTextBySession.set(msg.guid, msg.text);
        }
        this.addDisplayItem(host, {
          msg,
          kind: 'message',
        });

        return;
      case AgentMessageType.AGENT_START:
        return;
      case AgentMessageType.AGENT_END:
        return;
      case AgentMessageType.MODEL_ERROR:
        this.displayItems = [...this.displayItems, { kind: 'message', msg }];
        if (host.currentAgent && msg.guid === host.agents[host.currentAgent.agentType]) {
          host.isAgentWorking = false;
        }
        return;
      default:
        console.warn('Message Type not implemented:', msg.msgType);
    }
  }
}
