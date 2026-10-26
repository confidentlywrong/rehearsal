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

/**
 * Framework-agnostic gateway for agent sessions.
 *
 * Architecture (matching the react-example / new backend):
 *  - A single "global observer" WebSocket connects to /ws (no query params).
 *  - Agents are spawned via HTTP POST /api/v1/spawn; the backend assigns session IDs.
 *  - All agent messages arrive on the single observer socket.
 *  - Broadcasts are sent through the observer socket using BroadcastRequest.
 *
 * Consumers subscribe via onSessionsChange() / onChat() and receive plain data.
 */

import * as protobuf from 'protobufjs';
import {
  type AgentGatewayInboundRecord,
  type AgentGatewayOriginCapture,
  recordAgentGatewayInboundMessage,
} from '../../agent-gateway-message-dump';
import { AgentMessageType } from './types';
import { simLog } from './sim-logger';

// ── Types ────────────────────────────────────────────────────────────────────

/**
 * Implemented by the 3D runner manager so the gateway can control runners
 * directly without routing through window events or Angular.
 */
export interface RunnerController {
  setVelocity(guid: string, velocity: number): void;
  setWater(guid: string, water: number): void;
  setProgress?(guid: string, t: number): void;
  setCollapsed?(guid: string): void;
}

export interface BackendAgent {
  guid: string;
  agentType: string;
  /** Human-readable name from backend (e.g. "Cloud Marathon Planner"). */
  displayName: string;
  connected: boolean;
}

export interface ChatMessage {
  guid: string;
  speaker: string;
  text: string;
  isUser: boolean;
  timestamp: Date;
  msgType: AgentMessageType;
  title?: string;

  // Optional
  toolName?: string;
  skillName?: string;
  icon?: string;
  rawJson?: string;
  result?: any;
  simulationId?: string;
}

enum TOOLS {
  SHOW_ROUTE = 'report_marathon_route',
  SHOW_CONSENSUS_PATHWAY = 'report_leadership_scenario',
  LOAD_SKILL = 'load_skill',
  FINANCIAL_SECURITY = 'set_financial_modeling_mode',
  START_SIMULATION = 'start_simulation',
  SIMULATION_PIPELINE = 'simulation_pipeline',
  /** Kept in sync with `SIMULATION_GATEWAY_TOOL_NAMES` in `agent-gateway-message-dump.ts`. */
  PREPARE_SIMULATION = 'prepare_simulation',
  GET_LAWS = 'get_local_and_traffic_rules',
  SPAWN_RUNNERS = 'spawn_runners',
  START_RACE = 'fire_start_gun',
  ADVANCE_TICK = 'advance_tick',
  RUNNER_PROCESS_TICK = 'process_tick',
  LEADERSHIP_PROCESS_TICK = 'process_leadership_tick',
  ECHO_DECISION_ROUND = 'process_decision_round_tool',
  CHECK_RACE_STATUS = 'check_race_complete',
  COMPILE_RESULTS = 'compile_results',
  A2UI = 'validate_and_emit_a2ui',
  EMIT_SIM_RESULTS = 'emit_sim_results_a2ui',
  EMIT_ACTION_PLAN = 'emit_action_plan_a2ui',
  TRAFFIC = 'assess_traffic_impact',
  DESIGN_SCENARIO = 'design_leadership_scenario',
  PLAN_PATHWAY = 'plan_consensus_pathway',
  MAP_STAKEHOLDERS = 'map_stakeholders',
  EMIT_STAKEHOLDER_GRAPH = 'emit_stakeholder_graph',
  EMIT_SCENARIO_LIST = 'emit_scenario_list_a2ui',
  EMIT_SCENARIO_DETAIL = 'emit_scenario_detail_a2ui',
}

enum RACE_STATUS {
  IN_PROGRESS = 'in_progress',
  COMPLETE = 'race_complete',
}

type SessionsListener = (sessions: BackendAgent[]) => void;
type ChatListener = (msg: ChatMessage) => void;
type VoidListener = () => void;

type LoadSkillData = {
  tool_hints: {
    skill_name: string;
  };
};
// ── Helpers ──────────────────────────────────────────────────────────────────
/**
 * Resolve the gateway WebSocket URL from runtime config (BFF), build-time
 * env, or localhost fallback.  The BFF serves /config.js which sets
 * window.ENV = { NG_APP_GATEWAY_URL: "/ws", NG_APP_GATEWAY_ADDR: "" }.
 * Relative paths like "/ws" are expanded to wss://host/ws.
 */
function resolveGatewayWsUrl(): string {
  const env = (window as any).ENV || {};
  const configured: string = env.NG_APP_GATEWAY_URL ?? import.meta.env.NG_APP_GATEWAY_URL ?? '';
  if (!configured) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/ws`;
  }
  if (configured.startsWith('/')) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}${configured}`;
  }
  return configured;
}

/**
 * Resolve the gateway HTTP base URL from runtime config (BFF), build-time
 * env, or localhost fallback.  Returns an empty string when the BFF is
 * serving (meaning all API calls should use relative paths).
 */
function resolveGatewayHttpBase(): string {
  const env = (window as any).ENV || {};
  // Empty string from BFF means "use relative URLs" (BFF proxies /api/v1/*).
  // Build-time .env provides the absolute URL for local dev.
  return env.NG_APP_GATEWAY_ADDR ?? import.meta.env.NG_APP_GATEWAY_ADDR ?? '';
}

// ── AgentGateway ────────────────────────────────────────────────────────────

class AgentGateway {
  private Wrapper: protobuf.Type | null = null;
  private BroadcastRequest: protobuf.Type | null = null;

  private reconnectDelay = 2000;
  private readonly MAX_RECONNECT_DELAY = 30000;

  /** Single global observer WebSocket. */
  private monitorSocket: WebSocket | null = null;

  /** Outbound frames queued while the WebSocket is disconnected. */
  private pendingFrames: Array<string | ArrayBuffer> = [];

  /** Agent sessions tracked locally (spawned via HTTP API). */
  private sessions = new Map<string, BackendAgent>();
  private seenRequests = new Set<string>();

  private sessionListeners: SessionsListener[] = [];
  private chatListeners: ChatListener[] = [];
  private controller: RunnerController | null = null;

  /** Optional hook so debug logs can compare backend tick counts to AgentScreen UI state. */
  private uiNumberOfFinishersGetter: (() => number) | null = null;

  /** Sessions currently inside a run (between run_start and run_end). */
  private workingSessions = new Set<string>();

  private organizerGuid: string = '';

  /** Pending tool call queue per session — used to match tool results with their tool names. */
  private pendingTools = new Map<string, string[]>();
  /** Last model text per session — used to deduplicate trailing narrative echoes. */
  private lastModelText = new Map<string, string>();
  /** Dedup key for route imports — prevents the same route from being imported twice. */
  private _lastRouteKey = '';
  /** Track runner sessions discovered from tick messages to auto-create 3D runners. */
  private _knownRunnerSessions = new Set<string>();
  /** Last function_call raw text per session — dedup duplicate model_end messages. */
  private lastFunctionCallText = new Map<string, string>();

  /** Simulation ID returned by the most recent start_simulation tool call. */
  private subscribedSimulationId: string | null = null;

  /** True while NDJSON replay is driving `handleMessage` — avoids clearing subscription on replayed `run_end` / duplicate WS subscribe on `model_start`. */
  private ndjsonReplayActive = false;

  private previousMessageIsA2ui = new Map<string, boolean>();
  private previousMessageIsFinancialPlanner: boolean = false;

  private isSpinningUpSubAgent: boolean = false;
  private raceComplete: boolean = false;

  filterSettings = { showLoadSkills: false };

  private collapsedRunners = new Set<string>();
  /** Dedupe backend `runner_status === finished` (process_tick may repeat until race ends). */
  private finishedRunners = new Set<string>();

  /** After `fire_start_gun`, collect the first N `process_tick` effective velocities (N = runner_count). */
  private firstPaceSampleTarget = 0;
  private firstPaceVelocities: number[] = [];

  private userSpawnedAgents = new Set<string>();

  /**
   * Agent type from the user-spawned session (first resolved). Used so pipeline / sub-session
   * messages (same simulation, different origin.session_id) still show the main chat agent in UI.
   */
  private primaryChatAgentType: string | null = null;

  /** Demo agent key while cached NDJSON runs (spawn GUIDs won't match recorded session ids). */
  private replayPrimaryAgentTypeHint: string | null = null;

  setFilterSettings(settings: Partial<{ showLoadSkills: boolean }>): void {
    Object.assign(this.filterSettings, settings);
  }

  private emitSessions(): void {
    const sessions = this.getSessions();
    for (const fn of this.sessionListeners) fn(sessions);
  }

  private _preProtoQueue: MessageEvent[] | null = null;
  private protoLoadPromise: Promise<void> | null = null;

  constructor() {
    this.protoLoadPromise = this.loadProto();
    // Listen for broadcast requests from the viewport to send RunnerEvents
    // window.addEventListener('sim:broadcastToRunner', (e: Event) => {
    //   const detail = (e as CustomEvent).detail as { sessionId: string; payload: string };
    //   if (detail?.sessionId && detail?.payload) {
    //     this.sendBroadcastNow(detail.payload, [detail.sessionId]);
    //   }
    // });
  }

  /** Register the 3D runner manager so commands reach runners directly. */
  setController(ctrl: RunnerController): void {
    this.controller = ctrl;
  }

  setUiNumberOfFinishersGetter(getter: (() => number) | null): void {
    this.uiNumberOfFinishersGetter = getter;
  }

  /** True when proto loading failed after all retries — gateway operates in degraded mode. */
  private protoLoadFailed = false;

  private async loadProto(): Promise<void> {
    const MAX_RETRIES = 3;
    const BASE_DELAY_MS = 1000;

    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      try {
        const root = await protobuf.load('/gateway.proto');
        this.Wrapper = root.lookupType('gateway.Wrapper');
        this.BroadcastRequest = root.lookupType('gateway.BroadcastRequest');

        if (this._preProtoQueue) {
          const queue = this._preProtoQueue;
          this._preProtoQueue = null;
          for (const evt of queue) this.handleMessage(evt);
        }

        // Connect the monitor socket once proto is ready
        this.connectMonitor();
        return;
      } catch (e) {
        console.error(
          `AgentGateway: proto load attempt ${attempt}/${MAX_RETRIES} failed`,
          e,
        );
        if (attempt < MAX_RETRIES) {
          const delay = BASE_DELAY_MS * Math.pow(2, attempt - 1); // 1s, 2s, 4s
          console.warn(`AgentGateway: retrying proto load in ${delay}ms…`);
          await new Promise((resolve) => setTimeout(resolve, delay));
        }
      }
    }

    // All retries exhausted — connect monitor in degraded mode
    console.error(
      '🚨 AgentGateway: proto load failed after all retries. ' +
        'Gateway will operate in DEGRADED MODE — binary protobuf messages cannot be decoded.',
    );
    this.protoLoadFailed = true;
    this.connectMonitor();
  }

  // ── Monitor WebSocket ───────────────────────────────────────────────────────

  private connectMonitor(): void {
    const wsUrl = resolveGatewayWsUrl();
    console.debug('AgentGateway: connecting monitor to', wsUrl);

    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';
    this.monitorSocket = ws;

    ws.onopen = () => {
      console.debug('AgentGateway: monitor connected');
      this.reconnectDelay = 2000;

      // Re-subscribe to active simulation after reconnect
      if (this.subscribedSimulationId) {
        this._sendTextFrame({
          type: 'subscribe_simulation',
          simulation_id: this.subscribedSimulationId,
        });
      }

      // Flush any messages queued during disconnection
      if (this.pendingFrames.length > 0) {
        console.debug(`AgentGateway: flushing ${this.pendingFrames.length} queued frame(s)`);
        for (const frame of this.pendingFrames) {
          ws.send(frame);
        }
        this.pendingFrames = [];
      }
    };

    ws.onmessage = (evt) => {
      this.handleMessage(evt);
    };

    ws.onclose = () => {
      const delay = this.reconnectDelay + Math.random() * 1000;
      console.warn(`AgentGateway: monitor disconnected — reconnecting in ${Math.round(delay)}ms`);
      this.monitorSocket = null;

      // Bug #35: Mark all sessions as disconnected so UI doesn't show ghost agents
      for (const session of this.sessions.values()) {
        session.connected = false;
      }
      this.emitSessions();

      setTimeout(() => this.connectMonitor(), delay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.MAX_RECONNECT_DELAY);
    };

    ws.onerror = () => {
      console.error('AgentGateway: monitor connection error');
    };
  }

  // ── Subscriptions ──────────────────────────────────────────────────────────

  onSessionsChange(fn: SessionsListener): () => void {
    this.sessionListeners.push(fn);
    return () => {
      this.sessionListeners = this.sessionListeners.filter((l) => l !== fn);
    };
  }

  onChat(fn: ChatListener): () => void {
    this.chatListeners.push(fn);
    return () => {
      this.chatListeners = this.chatListeners.filter((l) => l !== fn);
    };
  }

  subscribeSimulation(simulationId: string): void {
    this.subscribedSimulationId = simulationId;

    this._sendTextFrame({ type: 'subscribe_simulation', simulation_id: simulationId });
  }
  unsubscribeSimulation(simulationId: string): void {
    if (this.subscribedSimulationId === simulationId) {
      this.subscribedSimulationId = null;
      // M13 fix: Clear sessions from the prior simulation to prevent stale
      // auto-registered entries persisting across simulation switches.
      this.sessions.clear();
      this.seenRequests.clear();
    }

    this._sendTextFrame({ type: 'unsubscribe_simulation', simulation_id: simulationId });
  }
  /** Send a JSON text frame over the WebSocket, queuing if disconnected. */
  private _sendTextFrame(payload: object): void {
    const frame = JSON.stringify(payload);
    if (this.monitorSocket?.readyState === WebSocket.OPEN) {
      this.monitorSocket.send(frame);
    } else {
      // Bug #19: Queue text frames for delivery on reconnect instead of dropping
      console.warn('AgentGateway: socket not open, queuing text frame for reconnect', payload);
      const MAX_PENDING = 500;
      if (this.pendingFrames.length >= MAX_PENDING) {
        console.warn('AgentGateway: pendingFrames cap reached, dropping oldest frame');
        this.pendingFrames.shift();
      }
      this.pendingFrames.push(frame);
    }
  }

  // ── Session management ─────────────────────────────────────────────────────

  async addAgent(agentType = 'runner_autopilot'): Promise<string> {
    try {
      const baseUrl = resolveGatewayHttpBase();
      const resp = await fetch(`${baseUrl}/api/v1/spawn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agents: [{ agentType, count: 1 }] }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.error || resp.statusText);
      }

      const result = await resp.json();
      const spawnedSessions = result.sessions || [];

      if (spawnedSessions.length === 0) {
        throw new Error('No sessions returned from spawn API');
      }

      const s = spawnedSessions[0];
      const sessionId = s.sessionId;
      this.userSpawnedAgents.add(sessionId);

      const info: BackendAgent = {
        guid: sessionId,
        agentType: s.agentType || agentType,
        displayName: '', // populated from proto Origin.display_name on first message
        connected: true,
      };
      this.sessions.set(sessionId, info);
      this.emitSessions();

      return sessionId;
    } catch (e) {
      const errMsg = e instanceof Error ? e.message : String(e);
      this.emitChat({
        guid: 'system',
        speaker: 'system',
        text: `Spawn failed: ${errMsg}`,

        isUser: false,
        timestamp: new Date(),

        msgType: 'system',
      });
      throw e;
    }
  }

  removeAgent(guid: string): void {
    if (this.userSpawnedAgents.has(guid)) {
      this.userSpawnedAgents.delete(guid);
      this.primaryChatAgentType = null;
    }
    this.sessions.delete(guid);
    this.emitSessions();
  }

  getSessions(): BackendAgent[] {
    return Array.from(this.sessions.values());
  }

  hasSession(guid: string): boolean {
    return this.sessions.has(guid);
  }

  /** Session id for the organizer `planner_with_memory` agent — used to filter agent chat display only. */
  getOrganizerGuid(): string {
    return this.organizerGuid;
  }

  getSimulationId(): string | null {
    return this.subscribedSimulationId;
  }

  removeCurrentSimulationId(): void {
    if (this.subscribedSimulationId) {
      this.unsubscribeSimulation(this.subscribedSimulationId);
    }
  }

  /** Clears chat dedup maps and emit-gating flags when switching demos or resetting. */
  resetDemoChatPipelineState(): void {
    // M16: Clear ALL session-scoped dedup maps to prevent unbounded growth.
    this.lastModelText.clear();
    this.previousMessageIsA2ui.clear();
    this.previousMessageIsFinancialPlanner = false;
    this.organizerGuid = '';
    this.pendingTools.clear();
    this.lastFunctionCallText.clear();
    this._knownRunnerSessions.clear();
    this.collapsedRunners.clear();
    this.finishedRunners.clear();
    this.seenRequests.clear();
    this.firstPaceVelocities = [];
    this.firstPaceSampleTarget = 0;
  }

  // ── Broadcast ──────────────────────────────────────────────────────────────

  setSessionForReplay(sessionId: string, agentType: string) {
    const info: BackendAgent = {
      guid: sessionId,
      agentType: agentType,
      displayName: '', // populated from proto Origin.display_name on first message
      connected: true,
    };
    this.sessions.set(sessionId, info);
  }

  /**
   * Call before timed NDJSON replay: sets `subscribedSimulationId` from the dump and
   * enables replay mode so replayed `run_end` / `model_start` do not mutate live subscription state.
   */
  beginNdjsonReplay(): void {
    this.ndjsonReplayActive = true;
  }

  endNdjsonReplay(): void {
    this.ndjsonReplayActive = false;
    this.replayPrimaryAgentTypeHint = null;
  }

  /** True while timed NDJSON cached replay is active (see {@link beginNdjsonReplay}). */
  isNdjsonReplayActive(): boolean {
    return this.ndjsonReplayActive;
  }

  /**
   * While cached replay runs, recorded session UUIDs rarely match the live spawn id.
   * Set to the demo's agent key (e.g. `simulator_with_failure`) so chat labels match the demo bot.
   */
  setReplayPrimaryAgentTypeHint(agentType: string | null): void {
    this.replayPrimaryAgentTypeHint = agentType && agentType.length > 0 ? agentType : null;
  }

  /**
   * Builds wire-format `Wrapper` bytes from a thin NDJSON row so timed replay can call `handleMessage`
   * without `rawBase64` in the dump file (see `replayAgentGatewayMsgNdjsonText`).
   */
  encodeInboundRecordForReplay(rec: AgentGatewayInboundRecord): ArrayBuffer | null {
    if (!this.Wrapper) return null;
    let payloadBytes: Uint8Array;
    try {
      payloadBytes = new TextEncoder().encode(JSON.stringify(rec.data));
    } catch {
      return null;
    }
    const d = rec.data;
    const o = rec.origin;
    const simId =
      typeof d['simulation_id'] === 'string'
        ? d['simulation_id']
        : typeof d['simulationId'] === 'string'
          ? d['simulationId']
          : '';
    const sessFromData =
      typeof d['session_id'] === 'string'
        ? d['session_id']
        : typeof d['sessionId'] === 'string'
          ? d['sessionId']
          : '';
    const sess =
      o?.sessionId && o.sessionId.length > 0 ? o.sessionId : sessFromData || 'replay-session';
    const agentId =
      o?.id && o.id.length > 0 ? o.id : typeof d['agent'] === 'string' ? d['agent'] : 'planner';
    const originType = o?.type && o.type.length > 0 ? o.type : 'agent';
    const originOut: {
      type: string;
      id: string;
      sessionId: string;
      displayName?: string;
    } = {
      type: originType,
      id: agentId,
      sessionId: sess,
    };
    if (o?.displayName && o.displayName.length > 0) {
      originOut.displayName = o.displayName;
    }
    const meta: Record<string, unknown> = {};
    if (typeof rec.speaker === 'string' && rec.speaker.length > 0) {
      meta['_csReplaySpeaker'] = rec.speaker;
    }
    const metadataBytes =
      Object.keys(meta).length > 0 ? new TextEncoder().encode(JSON.stringify(meta)) : null;
    const wrapper = this.Wrapper.create({
      type: rec.wrapperType,
      event: rec.event,
      payload: payloadBytes,
      simulationId: simId || undefined,
      origin: originOut,
      metadata: metadataBytes && metadataBytes.length > 0 ? metadataBytes : undefined,
    });
    const fin = this.Wrapper.encode(wrapper).finish();
    return new Uint8Array(fin).buffer;
  }

  handleMessage(messageEvent: MessageEvent) {
    if (!this.Wrapper) {
      if (!this._preProtoQueue) this._preProtoQueue = [];
      this._preProtoQueue.push(messageEvent);
      return;
    }

    try {
      const wrapper = this.Wrapper.decode(new Uint8Array(messageEvent.data)) as any;

      let replaySpeakerFromMeta: string | undefined;
      try {
        const md = wrapper.metadata as Uint8Array | undefined;
        if (md?.length) {
          const parsed = JSON.parse(new TextDecoder().decode(md)) as { _csReplaySpeaker?: string };
          if (typeof parsed._csReplaySpeaker === 'string' && parsed._csReplaySpeaker.length > 0) {
            replaySpeakerFromMeta = parsed._csReplaySpeaker;
          }
        }
      } catch {
        /* ignore */
      }

      const wrapperType: string = wrapper.type || '';
      const event: string = wrapper.event || '';

      const rawPayload: Uint8Array | null = wrapper.payload?.length ? wrapper.payload : null;
      const decodedText = rawPayload ? new TextDecoder().decode(rawPayload) : '';
      let data: Record<string, unknown> = {};
      try {
        data = JSON.parse(decodedText);
      } catch {
        /* ignore */
      }

      // Skip broadcast echoes (messages we sent)
      if (event === 'broadcast') return;

      simLog.log(
        'recv',
        wrapper.origin?.sessionId || '?',
        `type=${wrapperType} event=${event} payload=${new TextDecoder().decode(wrapper.payload ?? new Uint8Array())}`,
      );

      // Resolve session identity from origin (preferred) or legacy fields
      const origin = wrapper.origin || {};
      const sessionId: string =
        origin.sessionId || origin.session_id || wrapper.sessionId || wrapper.session_id || '';

      // Look up session for display metadata, auto-registering unknown
      // sessions from the protobuf origin field. On GCP, Agent Engine
      // agents create their own session IDs that differ from the spawn
      // API's session IDs, so events arrive from sessions the frontend
      // never explicitly spawned. Scoped to the current simulation to
      // prevent cross-simulation "bleed".
      let session = this.sessions.get(sessionId);
      // protobufjs camelCases snake_case field names
      const displayName: string = origin.displayName || origin.display_name || '';

      if (
        !session &&
        sessionId &&
        (this.ndjsonReplayActive ||
          (this.subscribedSimulationId && wrapper.simulationId === this.subscribedSimulationId))
      ) {
        const inferredType =
          (typeof origin.id === 'string' && origin.id.length > 0 && origin.id !== 'agent'
            ? origin.id
            : null) ??
          (typeof origin.type === 'string' && origin.type.length > 0 && origin.type !== 'agent'
            ? origin.type
            : null) ??
          'unknown';

        session = {
          guid: sessionId,
          agentType: inferredType,
          displayName,
          connected: true,
        };
        this.sessions.set(sessionId, session);
        this.emitSessions();
      }

      // Update display name from proto origin if we didn't have one yet
      if (session && displayName && !session.displayName) {
        session.displayName = displayName;
        this.emitSessions();
      }

      if (sessionId && this.userSpawnedAgents.has(sessionId) && session) {
        this.primaryChatAgentType = session.agentType;
      }

      const isUserSpawned = Boolean(sessionId && this.userSpawnedAgents.has(sessionId));
      const roleFromOrigin =
        typeof origin.id === 'string' && origin.id.length > 0 && origin.id !== 'agent'
          ? origin.id
          : '';
      const roleFromPayload =
        typeof data['agent'] === 'string' && data['agent'].length > 0
          ? (data['agent'] as string)
          : '';

      let roleForSpeaker: string;
      if (isUserSpawned && session) {
        roleForSpeaker = session.agentType;
      } else {
        roleForSpeaker =
          this.primaryChatAgentType ||
          this.replayPrimaryAgentTypeHint ||
          roleFromPayload ||
          roleFromOrigin ||
          session?.agentType ||
          'agent';
      }

      const speaker = origin.id; //replaySpeakerFromMeta ?? `${label} (${sessionId.substring(0, 6)})`;

      const skillName = (data as LoadSkillData)['tool_hints']?.skill_name;

      const isRunner =
        origin.id === 'runner_autopilot' || origin.id === 'leadership_runner' ||
        origin.id === 'ai_echo' || origin.id === 'ai_echo_autopilot' || origin.id === 'ai_echo_reactive';
      const isRunnerProcessTickToolEnd =
        wrapperType === 'json' &&
        event === 'tool_end' &&
        (data['tool_name'] === TOOLS.RUNNER_PROCESS_TICK ||
          data['tool_name'] === TOOLS.LEADERSHIP_PROCESS_TICK ||
          data['tool_name'] === TOOLS.ECHO_DECISION_ROUND ||
          data['tool_name'] === 'process_decision_round');

      if (isRunner && !isRunnerProcessTickToolEnd) {
        return;
      }

      if (origin.id === 'tick' && event !== 'tool_end') {
        return;
      }

      if (
        !this.ndjsonReplayActive &&
        this.subscribedSimulationId &&
        wrapper.simulationId !== this.subscribedSimulationId
      ) {
        return;
      }

      if (!this.ndjsonReplayActive && !this.subscribedSimulationId && !session) {
        // Drop messages from unknown sessions when no simulation is active.
        // Do NOT call unsubscribeSimulation here — the simulation_id on
        // this message may be reused later by start_simulation, and
        // unsubscribing it now would cause the Hub to stop delivering
        // all future messages for that simulation (e.g., simulator
        // internal events during verify leak through before the frontend
        // subscribes, poisoning the simulation_id for the later execute).
        // this.unsubscribeSimulation(wrapper.simulationId);
        return;
      }

      if (!this.ndjsonReplayActive) {
        const captureOrigin: AgentGatewayOriginCapture = {
          id:
            session?.agentType ||
            (typeof origin.id === 'string' ? origin.id : '') ||
            (typeof origin.type === 'string' ? origin.type : '') ||
            'unknown',
        };
        if (typeof origin.type === 'string' && origin.type.length > 0) {
          captureOrigin.type = origin.type;
        }
        if (sessionId.length > 0) {
          captureOrigin.sessionId = sessionId;
        }
        const capturedDisplay = session?.displayName || displayName;
        if (capturedDisplay.length > 0) {
          captureOrigin.displayName = capturedDisplay;
        }
        recordAgentGatewayInboundMessage({
          timestamp: wrapper.timestamp,
          wrapperType,
          event,
          data,
          origin: captureOrigin,
          speaker,
        });
      }

      switch (wrapperType) {
        // ── JSON events ──────────────────────────────────────────────────────
        // payload: structured JSON dict
        case 'json': {
          switch (event) {
            case 'run_start':
              this.emitChat({
                guid: sessionId,
                speaker: speaker,
                text: 'run start',
                isUser: false,
                timestamp: new Date(),
                msgType: AgentMessageType.RUN_START,
              });
              return;
            case 'run_end':
              if (session && this.userSpawnedAgents.has(sessionId) && !this.ndjsonReplayActive) {
                this.removeCurrentSimulationId();
              }

              this.emitChat({
                guid: sessionId,
                speaker: speaker,
                text: 'run end',
                isUser: false,
                timestamp: new Date(),
                msgType: AgentMessageType.RUN_END,
              });
              return;
            case 'marathon_route_final':
              console.warn('MARATHON_ROUTE_FINAL call removed.');

              return;
            case 'tool_start': {
              const toolName = data['tool'];

              switch (toolName) {
                case TOOLS.LOAD_SKILL:
                  if (!this.filterSettings.showLoadSkills) return;

                  break;
                case TOOLS.SIMULATION_PIPELINE:
                  return;
                case TOOLS.A2UI:
                case TOOLS.EMIT_SIM_RESULTS:
                case TOOLS.EMIT_ACTION_PLAN:
                  return;
                // case TOOLS.SHOW_ROUTE:
                //   return;
              }

              const hints = (data['tool_hints'] as Record<string, any>) || {};
              let displaySkillName = hints['textQuery'] || hints['query'] || hints['agent_name'] || skillName || '';
              if (hints['script_path']) {
                displaySkillName = displaySkillName ? `${displaySkillName} (${hints['script_path']})` : hints['script_path'];
              }
              this.emitChat({
                guid: sessionId,
                speaker: speaker,
                text: '',
                isUser: false,
                timestamp: new Date(),
                msgType: AgentMessageType.TOOL_START,
                toolName: toolName as string,
                skillName: displaySkillName,
              });

              return;
            }
            case 'tool_result':
              console.warn('missing :: telemetry tool_result', data);
              // Before a tool call is executed
              return;

            case 'tool_end': {
              const result = data['result'] as {
                message: string;
                status: string;
                simulation_id?: string;
                simulationId?: string;
                session_ids?: string[];
                runner_count?: number;
                velocity?: number;
                effective_velocity?: number;
                water?: number;
                distance_km?: number;
                distance_mi?: number;
                exhausted?: boolean;
                collapsed?: boolean;
                skill_name?: string;
                runner_status?: string;
                status_counts?: any;
                inner_thought?: string;
              };
              const toolName = data['tool_name'];

              if (!result || !toolName) return;

              switch (toolName) {
                case TOOLS.LOAD_SKILL:
                  if (!this.filterSettings.showLoadSkills) return;
                  break;
                case TOOLS.SHOW_ROUTE:
                  window.dispatchEvent(
                    new CustomEvent('gateway:routeGeojson', { detail: { geojson: result } }),
                  );
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,
                    text: 'Route visualization data has been plotted on the map.',
                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: toolName as string,
                    skillName: result['skill_name'],
                    result,
                  });
                  return;
                case TOOLS.SHOW_CONSENSUS_PATHWAY: {
                  const geojson = (result as any)?.scenario_summary?.consensus_pathway;
                  if (geojson) {
                    window.dispatchEvent(
                      new CustomEvent('gateway:routeGeojson', { detail: { geojson } }),
                    );
                  }
                  // Flag viewport as leadership route for HUD label remapping.
                  window.dispatchEvent(new CustomEvent('gateway:leadershipRoute'));
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,
                    text: 'Leadership pathway data has been successfully mapped to the UI.',
                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: toolName as string,
                    skillName: result['skill_name'],
                    result,
                  });
                  return;
                }
                case TOOLS.FINANCIAL_SECURITY:
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,
                    text: 'Financial modeling mode set.',
                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: toolName as string,
                    skillName: result['skill_name'],
                    result,
                  });
                  return;
                case TOOLS.TRAFFIC: {
                  const affectedIntersections = (result as any)['affected_intersections'];
                  const affectedSegments = (result as any)['affected_segments'];
                  const closedSegments = (result as any)['closed_segments'];
                  // if (Array.isArray(intersections) && intersections.length > 0) {
                  window.dispatchEvent(
                    new CustomEvent('gateway:trafficZones', {
                      detail: {
                        affectedIntersections,
                        affectedSegments,
                        closedSegments,
                      },
                    }),
                  );
                  // }
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,
                    text: 'Traffic impact assessment completed and mapped to UI.',
                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: toolName as string,
                    skillName: result['skill_name'],
                    result,
                  });
                  return;
                }

                case TOOLS.START_SIMULATION:
                  if (wrapper.simulationId && wrapper.simulationId !== this.subscribedSimulationId) {
                    if (this.subscribedSimulationId) {
                      this.unsubscribeSimulation(this.subscribedSimulationId);
                    }
                    this.subscribeSimulation(wrapper.simulationId as string);
                    this.raceComplete = false;
                  }
                  break;
                case TOOLS.GET_LAWS:
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,

                    text: result.message,

                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: data['tool_name'] as string,
                    result,
                  });
                  return;
                case TOOLS.SPAWN_RUNNERS: {
                  const sessionIds = result['session_ids'] as string[] | undefined;
                  if (Array.isArray(sessionIds)) {
                    for (const runnerId of sessionIds) {
                      // Register runner in session map for display metadata
                      if (!this.sessions.has(runnerId)) {
                        this.sessions.set(runnerId, {
                          guid: runnerId,
                          agentType: 'runner_autopilot',
                          displayName: '',
                          connected: true,
                        });
                      }

                      // Track for tick routing. Always dispatch: viewport dedupes re-delivery / same
                      // guid via runnerManager.getRunner (early return if mesh already exists).
                      this._knownRunnerSessions.add(runnerId);
                      window.dispatchEvent(
                        new CustomEvent('hud:addSimRunner', {
                          detail: {
                            guid: `sim-${runnerId}`,

                            velocity: 0,
                            distanceMi: 0,
                            progress: 0,
                          },
                        }),
                      );
                    }
                    this.emitSessions();
                  }
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,
                    text: `Successfully processed spawn of runners.`,
                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: toolName as string,
                    skillName: result['skill_name'],
                    result,
                  });
                  return;
                }
                case TOOLS.START_RACE: {
                  const rc = result['runner_count'] as number | undefined;
                  this.firstPaceSampleTarget = typeof rc === 'number' && rc > 0 ? rc : 0;
                  this.firstPaceVelocities = [];

                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,

                    text: result.message,

                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: data['tool_name'] as string,
                    result,
                  });
                  return;
                }
                case TOOLS.ADVANCE_TICK:
                  if (this.raceComplete) return;
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,

                    text: result.message,
                    result,

                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: data['tool_name'] as string,
                  });
                  return;
                case TOOLS.RUNNER_PROCESS_TICK:
                case TOOLS.LEADERSHIP_PROCESS_TICK:
                  const velocity = result['velocity'] as number;
                  const effectiveVelocity = (result['effective_velocity'] as number) ?? velocity;
                  const water = result['water'] as number;
                  const runnerThought = result['inner_thought'];

                  if (
                    this.firstPaceSampleTarget > 0 &&
                    this.firstPaceVelocities.length < this.firstPaceSampleTarget
                  ) {
                    this.firstPaceVelocities.push(effectiveVelocity);
                    if (this.firstPaceVelocities.length === this.firstPaceSampleTarget) {
                      const sum = this.firstPaceVelocities.reduce((a, b) => a + b, 0);
                      const avg = sum / this.firstPaceVelocities.length;
                      window.dispatchEvent(
                        new CustomEvent('sim:firstBatchAvgVelocity', {
                          detail: { avgVelocity: avg },
                        }),
                      );
                    }
                  }

                  // window.dispatchEvent(
                  //   new CustomEvent('sim:eventLog', {
                  //     detail: {
                  //       guid: `sim-${sessionId}`,
                  //       type: 'in',
                  //       message: `v=${velocity} ev=${effectiveVelocity} w=${water}%`,
                  //     },
                  //   }),
                  // );
                  window.dispatchEvent(
                    new CustomEvent('hud:updateSimRunner', {
                      detail: {
                        guid: `sim-${sessionId}`,
                        velocity: effectiveVelocity,
                        water,
                        runnerThought,
                        _fromGateway: true,
                      },
                    }),
                  );

                  // ── Runner events: detect water station, rehydration, exhaustion, collapse ─
                  if (sessionId && this._knownRunnerSessions.has(sessionId)) {
                    if (result['exhausted']) {
                      window.dispatchEvent(
                        new CustomEvent('sim:runnerEvent', {
                          detail: { guid: `sim-${sessionId}`, event: 'exhausted' },
                        }),
                      );
                    }
                    if (result['collapsed'] && !this.collapsedRunners.has(sessionId)) {
                      window.dispatchEvent(
                        new CustomEvent('sim:runnerEvent', {
                          detail: { guid: `sim-${sessionId}`, event: 'collapsed' },
                        }),
                      );
                      this.collapsedRunners.add(sessionId);
                    }
                    if (
                      result['runner_status'] === 'finished' &&
                      !this.finishedRunners.has(sessionId)
                    ) {
                      this.finishedRunners.add(sessionId);
                      window.dispatchEvent(
                        new CustomEvent('sim:runnerEvent', {
                          detail: { guid: `sim-${sessionId}`, event: 'finished' },
                        }),
                      );
                    }
                  }

                  ///

                  return;
                case TOOLS.CHECK_RACE_STATUS:
                  if (result.status === RACE_STATUS.COMPLETE) {
                    this.raceComplete = true;
                    window.dispatchEvent(new CustomEvent('sim:finished'));
                  }
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,
                    text: `Race status check completed: ${result.status || 'unknown'}`,
                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: toolName as string,
                    skillName: result['skill_name'],
                    result,
                  });
                  return;
                case TOOLS.COMPILE_RESULTS:
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,
                    text: `Race results compiled successfully.`,
                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: toolName as string,
                    skillName: result['skill_name'],
                    result,
                  });
                  return;
                case 'stop_race_collector':
                  this.emitChat({
                    guid: sessionId,
                    speaker: speaker,
                    text: `Race collector stopped.`,
                    isUser: false,
                    timestamp: new Date(),
                    msgType: AgentMessageType.TOOL_END,
                    toolName: toolName as string,
                    skillName: result['skill_name'],
                    result,
                  });
                  return;
                case TOOLS.EMIT_SIM_RESULTS:
                case TOOLS.EMIT_ACTION_PLAN:
                  return;
                default:
                  console.debug('[A2UI] Unhandled tool case (forwarding blindly):', { event, toolName });
              }

              let textPayload = result.message;
              if (!textPayload) {
                const anyResult = result as any;
                if (anyResult.summary) {
                  textPayload = anyResult.summary;
                } else if (anyResult.structuredContent && anyResult.structuredContent.summary) {
                  textPayload = anyResult.structuredContent.summary;
                } else if (anyResult.content) {
                  textPayload = JSON.stringify(anyResult.content, null, 2);
                } else if (anyResult.structuredContent) {
                  textPayload = JSON.stringify(anyResult.structuredContent, null, 2);
                }
              }

              this.emitChat({
                guid: sessionId,
                speaker: speaker,

                text: textPayload || '',

                isUser: false,
                timestamp: new Date(),
                msgType: AgentMessageType.TOOL_END,
                toolName: data['tool_name'] as string,
                skillName: result['skill_name'],
                result,
              });

              return;
            }

            case 'tool_error':
              this.emitChat({
                guid: sessionId,
                speaker,

                isUser: false,
                timestamp: new Date(),

                msgType: AgentMessageType.TOOL_ERROR,
                toolName: data['tool_name'] as string,
                text: `${data['text']}`,
              });
              break;

            case 'model_start':
              // M7: when a sub-agent starts emitting events for a *new*
              // simulation, unsubscribe from the previous one and subscribe
              // to the new one. The old guard (`!subscribedSimulationId`)
              // pinned the session to whichever simulation it first saw and
              // silently ignored every subsequent run for the lifetime of
              // the tab.
              if (wrapper.simulationId && wrapper.simulationId !== this.subscribedSimulationId) {
                if (this.subscribedSimulationId) {
                  this.unsubscribeSimulation(this.subscribedSimulationId);
                }
                this.subscribedSimulationId = wrapper.simulationId as string;
                if (!this.ndjsonReplayActive) {
                  this.subscribeSimulation(this.subscribedSimulationId);
                }
                this.isSpinningUpSubAgent = false;
              }

              return;
            case 'model_end':
              break;

            case 'json':
              const result = (data['result'] ?? {}) as {
                financial_modeling_mode?: any;
              };

              // Handle set_financial_modeling_mode wrapped in tool_name envelope
              if (
                data['tool_name'] === 'set_financial_modeling_mode' &&
                result?.['financial_modeling_mode']
              ) {
                this.emitChat({
                  guid: sessionId,
                  speaker: speaker,
                  isUser: false,
                  timestamp: new Date(),
                  toolName: 'set_financial_modeling_mode',
                  msgType: AgentMessageType.TOOL_END,
                  text: JSON.stringify(data['result']),
                });
                return;
              }

              if (data['tool_name']) return;
              // Suppress dispatcher passthrough of tool results — these are duplicates
              // of the structured tool_end events already handled by DashLogPlugin.
              if (data['status'] && !data['tool_name'] && !data['text']) return;
              // Leadership payloads pass through without marathon-specific key check

              // Legacy: top-level financial_modeling_mode (pre-envelope format)
              if (data['financial_modeling_mode']) {
                this.emitChat({
                  guid: sessionId,
                  speaker: speaker,
                  isUser: false,
                  timestamp: new Date(),
                  toolName: 'set_financial_modeling_mode',
                  msgType: AgentMessageType.TOOL_END,
                  text: JSON.stringify(data),
                });
                return;
              }

              this.emitChat({
                guid: sessionId,
                speaker: speaker,
                isUser: false,
                timestamp: new Date(),
                msgType: AgentMessageType.TOOL_END,
                text: JSON.stringify(data),
              });
              // Generic parsed JSON from agent output (RedisOrchestratorDispatcher)
              break;

            case 'inter-agent':
              this.isSpinningUpSubAgent = true;

              break;

            case 'tick:advance':
              break;
            case 'text':
              // Emit narrative text alongside any preceding A2UI tile. Previously
              // this branch silently dropped text-after-A2UI, which lost
              // step-detail narration. Financial-planner mode keeps its
              // explicit one-shot suppression because the follow-up text is
              // a known duplicate of the modal payload.
              if (this.previousMessageIsFinancialPlanner) {
                this.previousMessageIsFinancialPlanner = false;
                break;
              }
              this.emitChat({
                guid: sessionId,
                speaker: speaker,
                isUser: false,
                timestamp: new Date(),
                msgType: AgentMessageType.TEXT,
                text: `${data['text']}`,
              });

              break;
            default:
              break;
          }
          break;
        }

        // ── A2UI events ──────────────────────────────────────────────────────
        // payload: A2UI JSON object (surfaceUpdate / dataModelUpdate / etc.)
        case 'a2ui': {
          this.emitChat({
            guid: sessionId,
            speaker: speaker,
            isUser: false,
            timestamp: new Date(),
            msgType: AgentMessageType.TOOL_END,
            result: data,
            text: '',
          });

          // event is always "a2ui"
          // payload is the A2UI object — beginRendering | surfaceUpdate |
          //   dataModelUpdate | deleteSurface
          break;
        }
        // ── Text events ──────────────────────────────────────────────────────
        // payload: { "text": "<string>" }
        case 'text': {
          switch (event) {
            case 'tool_start':
              console.warn('missing :: text tool_start', decodedText);
              // After a tool returns a non-JSON text result
              // payload: { "text": "[Agent] Tool End: <tool>\n\n<result>" }
              break;
            case 'tool_end':
              this.emitChat({
                guid: sessionId,
                speaker: speaker,
                text: decodedText || 'Tool completed',
                isUser: false,
                timestamp: new Date(),
                msgType: AgentMessageType.TOOL_END,
              });
              break;
            case 'tool_error':
              this.emitChat({
                guid: sessionId,
                speaker: speaker,
                text: decodedText || 'Tool error occurred',
                isUser: false,
                timestamp: new Date(),
                msgType: AgentMessageType.TOOL_ERROR,
              });
              break;

            case 'model_start':
              console.warn('missing :: text model_start', decodedText);
              // After an LLM call completes (turn_complete=true)
              // payload: { "text": "[Agent] Model End\n\n<response>" }
              break;

            case 'model_end':
              console.warn('missing :: text model_end', decodedText);
              // After an LLM call completes (turn_complete=true)
              // payload: { "text": "[Agent] Model End\n\n<response>" }
              break;
            case 'model_error':
              console.warn('missing :: text model_error', decodedText);
              // When an LLM call fails
              // payload: { "text": "[Agent] Model Error\n\nError: <msg>" }
              break;

            case 'text':
              console.warn('missing :: text text', decodedText);
              // Plain text from RedisOrchestratorDispatcher runner events
              // payload: { "text": "<agent output>" }
              break;
          }
          break;
        }

        // ── Telemetry events ─────────────────────────────────────────────────
        // payload: full plugin payload dict — always includes session_id,
        //          invocation_id, seq (monotonic per-session sequence number)
        case 'telemetry': {
          console.warn('Missing : telemetry event -> ', event);
          break;
        }

        // ── Environment reset ────────────────────────────────────────────────
        // Broadcast to ALL clients after POST /api/v1/environment/reset.
        // Each RedisOrchestratorDispatcher cancels background tasks, clears
        // active sessions, dedup caches, and the simulation registry.
        case 'environment_reset': {
          console.warn('missing :: environment_reset');
          break;
        }
        default:
          console.error('no case for ', wrapperType);
      }
    } catch (e) {
      console.error('AgentGateway: error processing message', e);
      throw e;
    }
  }

  emitChat(message: ChatMessage) {
    if (!message.rawJson) {
      try {
        message.rawJson = JSON.stringify(
          message,
          (_k, v) => (v instanceof Date ? v.toISOString() : v),
          2,
        );
      } catch {
        /* ignore circular refs */
      }
    }
    for (const fn of this.chatListeners) fn(message);

    const isOrganizerSession = this.organizerGuid.length > 0 && message.guid === this.organizerGuid;
    if (!isOrganizerSession) {
      // NOTE: `previousMessageIsA2ui` is no longer read by the text branch — it
      // used to gate text-after-A2UI suppression but that dropped legitimate
      // step-detail narration. Kept here as inert state for any future
      // out-of-tree consumers; safe to remove if confirmed unused.
      this.previousMessageIsA2ui.set(
        message.guid,
        !!(message.msgType === AgentMessageType.TOOL_END &&
        message.result &&
        (message.result['beginRendering'] || message.result['surfaceUpdate'])),
      );

      this.previousMessageIsFinancialPlanner =
        message.msgType === AgentMessageType.TOOL_END &&
        message.toolName === 'set_financial_modeling_mode';
    }
  }

  // ── Broadcast ──────────────────────────────────────────────────────────────

  async sendBroadcast(text: string, targetGuids: (string | null | undefined)[], silent = false, isOrganizer = false): Promise<void> {
    const validTargetGuids = targetGuids.filter((g): g is string => !!g);
    
    if (isOrganizer && validTargetGuids.length > 0) {
      this.organizerGuid = validTargetGuids[0];
    }
    if (validTargetGuids.length === 0) {
      console.warn('AgentGateway: validTargetGuids is empty. Message dropped.');
      if (!silent) {
        this.emitChat({
          guid: '',
          speaker: 'System',
          isUser: false,
          msgType: AgentMessageType.TOOL_ERROR,
          text: 'Failed to send message: no active agent found (spawn may still be in-flight). Please wait a moment or refresh.',
          title: 'Error Sending Message',
          timestamp: new Date(),
        });
      }
      return;
    }

    if (!silent) {
      this.emitChat({
        guid: '',
        speaker: 'You',
        isUser: true,
        msgType: AgentMessageType.SYSTEM,
        text,
        title: `${text.split(' ').slice(0, 3).join(' ')}...`,
        timestamp: new Date(),
      });
    }

    if (this.protoLoadPromise) {
      await this.protoLoadPromise;
    }

    if (this.protoLoadFailed || !this.Wrapper || !this.BroadcastRequest) {
      console.warn('AgentGateway: proto not ready or failed to load. Message dropped.');
      if (!silent) {
        this.emitChat({
          guid: '',
          speaker: 'System',
          isUser: false,
          msgType: AgentMessageType.TOOL_ERROR,
          text: 'Failed to send message: connection to gateway is degraded (protocol definition failed to load). Please refresh the page.',
          title: 'Error Sending Message',
          timestamp: new Date(),
        });
      }
      return;
    }

    this.sendBroadcastNow(text, validTargetGuids);
  }

  /** Actually encode and send a broadcast over the wire. */
  private sendBroadcastNow(text: string, targetGuids: string[]): void {
    if (!this.Wrapper || !this.BroadcastRequest) return;

    let jsonPayload: unknown;
    try {
      jsonPayload = JSON.parse(text);
    } catch {
      jsonPayload = { text };
    }

    const innerPayload = new TextEncoder().encode(JSON.stringify(jsonPayload));
    const broadcastReq = this.BroadcastRequest.create({
      payload: innerPayload,
      targetSessionIds: targetGuids,
    });
    const brBinary = this.BroadcastRequest.encode(broadcastReq).finish();

    const wrapper = this.Wrapper.create({
      origin: { type: 'client', id: 'tester-ui', sessionId: 'tester-ui' },
      destination: targetGuids,
      status: 'success',
      type: 'broadcast',
      event: 'broadcast',
      payload: brBinary,
    });
    const binary = this.Wrapper.encode(wrapper).finish();

    simLog.log('send', targetGuids.join(','), `broadcast payload=${JSON.stringify(jsonPayload)}`);
    if (this.monitorSocket && this.monitorSocket.readyState === WebSocket.OPEN) {
      this.monitorSocket.send(binary);
    } else {
      // Queue for delivery when the socket reconnects.
      // M16 fix: Cap the queue to prevent unbounded memory growth during
      // extended disconnections.
      const MAX_PENDING = 500;
      if (this.pendingFrames.length >= MAX_PENDING) {
        console.warn('AgentGateway: pendingFrames cap reached, dropping oldest frame');
        this.pendingFrames.shift();
      } else if (this.pendingFrames.length === 0) {
        // Emit a single warning on the first queued frame to notify the user of the disconnect.
        this.emitChat({
          guid: '',
          speaker: 'System',
          isUser: false,
          msgType: AgentMessageType.TOOL_ERROR,
          text: 'Connection to gateway is currently offline. Your message has been queued and will be sent automatically when reconnected.',
          title: 'Network Disconnected',
          timestamp: new Date(),
        });
      }
      this.pendingFrames.push(new Uint8Array(binary).buffer);
    }
  }

  dispose(): void {
    if (this.monitorSocket) {
      this.monitorSocket.close();
      this.monitorSocket = null;
    }
  }
}

// ── Singleton export ──────────────────────────────────────────────────────────

export const agentGateway = new AgentGateway();
