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
import * as path from 'node:path';
import * as protobuf from 'protobufjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AgentGateway } from './agent-gateway-updates';
import { isRunnerProcessTickToolEndRow, SIMULATION_GATEWAY_TOOL_NAMES } from '../../agent-gateway-message-dump';
import { extractOrganizerScenariosFromSurface } from './components/ChatNavPanel/screens/Organizer/organizer.component';
import { AgentMessageType } from './types';

const ORGANIZER_SURFACE_FIXTURE = {
  surfaceUpdate: {
    surfaceId: 'route_list',
    components: [
      { id: 'tag-1', component: { Text: { text: { literalString: 'EVALUATED' }, usageHint: 'label' } } },
      { id: 'sim-meta-1', component: { Text: { text: { literalString: 'b59e317a-8f2c-4e1a-bd84-362203e85764' }, usageHint: 'caption' } } },
      { id: 'tag-row-1', component: { Row: { children: { explicitList: ['tag-1', 'sim-meta-1'] } } } },
      { id: 'title-1', component: { Text: { text: { literalString: 'AI Enterprise Transformation' }, usageHint: 'h2' } } },
      { id: 'left-col-1', component: { Column: { children: { explicitList: ['tag-row-1', 'title-1'] } } } },
      { id: 'score-num-1', component: { Text: { text: { literalString: '83' }, usageHint: 'h1' } } },
      { id: 'score-lbl-1', component: { Text: { text: { literalString: 'Score' }, usageHint: 'caption' } } },
      { id: 'score-col-1', component: { Column: { children: { explicitList: ['score-num-1', 'score-lbl-1'] } } } },
      { id: 'header-1', component: { Row: { children: { explicitList: ['left-col-1', 'score-col-1'] } } } },
      { id: 'dist-l-1', component: { Text: { text: { literalString: 'Decision Rounds' }, usageHint: 'body' } } },
      { id: 'dist-v-1', component: { Text: { text: { literalString: '8' }, usageHint: 'body' } } },
      { id: 'dist-r-1', component: { Row: { children: { explicitList: ['dist-l-1', 'dist-v-1'] } } } },
      { id: 'content-1', component: { Column: { children: { explicitList: ['header-1', 'dist-r-1'] } } } },
      { id: 'card-1', component: { Card: { child: 'content-1' } } },
      { id: 'list-1', component: { List: { children: { explicitList: ['card-1'] } } } },
      { id: 'root-card', component: { Card: { child: 'list-1' } } },
    ],
  },
};

describe('AgentGateway & Organizer surface extraction', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('preserves user-spawned planner sessions when unsubscribing from a simulation', async () => {
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          sessions: [{ sessionId: 'planner-sess-1', agentType: 'leadership_planner_with_memory' }],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    Object.defineProperty(globalThis, 'fetch', { configurable: true, value: fetchSpy });

    const gw = new AgentGateway({ autoConnect: false });
    const plannerId = await gw.addAgent('leadership_planner_with_memory');
    expect(plannerId).toBe('planner-sess-1');

    // Simulate active simulation + auto-registered simulation NPC session
    gw.subscribeSimulation('sim-42');
    gw.setSessionForReplay('npc-cfo-session', 'stakeholder_npc');
    expect(gw.getSessions().map((s) => s.guid)).toEqual(['planner-sess-1', 'npc-cfo-session']);

    // Unsubscribing at run_end must evict simulation sub-sessions while preserving user-spawned planners
    gw.unsubscribeSimulation('sim-42');
    expect(gw.getSessions().map((s) => s.guid)).toEqual(['planner-sess-1']);
    expect(gw.hasSession('planner-sess-1')).toBe(true);
    expect(gw.hasSession('npc-cfo-session')).toBe(false);

    gw.dispose();
  });

  it('clears pending reconnect timer and closes monitor socket on dispose()', () => {
    const gw = new AgentGateway({ autoConnect: false });
    const closeSpy = vi.fn();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const internal = gw as any;
    internal.reconnectTimer = setTimeout(() => {
      throw new Error('reconnect timer should have been cleared');
    }, 50);
    internal.monitorSocket = {
      onopen: () => {},
      onmessage: () => {},
      onclose: () => {},
      onerror: () => {},
      close: closeSpy,
    };

    gw.dispose();

    expect(internal.disposed).toBe(true);
    expect(internal.reconnectTimer).toBeNull();
    expect(internal.monitorSocket).toBeNull();
    expect(closeSpy).toHaveBeenCalledTimes(1);
  });

  it('extracts scenarios from flat A2UI v0.8 emit_scenario_list_a2ui surfaces', () => {
    const extracted = extractOrganizerScenariosFromSurface(ORGANIZER_SURFACE_FIXTURE);
    expect(extracted.length).toBeGreaterThan(0);
    expect(extracted[0].scenario_id).toBeTruthy();
    expect(extracted[0].title).toBeTruthy();
    expect(typeof extracted[0].eval_score).toBe('number');

    // Detail surface without scenario list entries returns [] so caller preserves pastScenarios
    const detailOnly = {
      surfaceUpdate: {
        surfaceId: 'detail-surface',
        components: [
          { id: 'root-card', component: { Card: { child: 'detail-txt' } } },
          { id: 'detail-txt', component: { Text: { text: { literalString: 'Details view' } } } },
        ],
      },
    };
    expect(extractOrganizerScenariosFromSurface(detailOnly)).toEqual([]);
  });

  it('resets raceComplete across resetDemoChatPipelineState and beginNdjsonReplay and emits model_error frames', async () => {
    const gw = new AgentGateway({ autoConnect: false });
    const received: any[] = [];
    gw.onChat((m) => received.push(m));
    const internal = gw as any;
    const protoRoot = await protobuf.load(path.resolve(process.cwd(), 'public/gateway.proto'));
    internal.Wrapper = protoRoot.lookupType('gateway.Wrapper');

    internal.raceComplete = true;
    gw.resetDemoChatPipelineState();
    expect(internal.raceComplete).toBe(false);

    internal.raceComplete = true;
    gw.beginNdjsonReplay();
    expect(internal.raceComplete).toBe(false);
    gw.endNdjsonReplay();

    // Encode and handle a semantic model_error record on a registered session
    gw.setSessionForReplay('sess-err', 'leadership_planner');
    const buf = gw.encodeInboundRecordForReplay({
      wrapperType: 'json',
      event: 'model_error',
      data: { error: { message: 'Vertex AI 429 Resource Exhausted' } },
      timestamp: new Date().toISOString(),
      origin: { sessionId: 'sess-err', type: 'leadership_planner' },
    });
    expect(buf).toBeTruthy();
    internal.handleMessage({ data: buf });
    expect(received.some((m) => m.msgType === AgentMessageType.MODEL_ERROR && m.text.includes('429'))).toBe(true);

    // Leadership tick without velocity must not push NaN into firstPaceVelocities
    gw.setSessionForReplay('npc-1', 'stakeholder_npc');
    internal.firstPaceSampleTarget = 1;
    internal.firstPaceVelocities = [];
    const tickBuf = gw.encodeInboundRecordForReplay({
      wrapperType: 'json',
      event: 'tool_end',
      data: {
        tool_name: 'process_leadership_tick',
        result: { inner_thought: 'Evaluating risk posture' },
      },
      timestamp: new Date().toISOString(),
      origin: { sessionId: 'npc-1', type: 'stakeholder_npc' },
    });
    internal.handleMessage({ data: tickBuf });
    expect(internal.firstPaceVelocities).toEqual([]);

    expect(SIMULATION_GATEWAY_TOOL_NAMES.has('process_leadership_tick')).toBe(true);
    expect(
      isRunnerProcessTickToolEndRow({
        event: 'tool_end',
        data: { tool_name: 'process_leadership_tick' },
      }),
    ).toBe(true);

    gw.dispose();
  });

  it('handles binary environment_reset Wrapper with omitted payload pre-simulation without crashing and clears sessions', async () => {
    const gw = new AgentGateway({ autoConnect: false });
    const sessionSnapshots: number[] = [];
    gw.onSessionsChange((sessions) => sessionSnapshots.push(sessions.length));

    const internal = gw as any;
    const protoRoot = await protobuf.load(path.resolve(process.cwd(), 'public/gateway.proto'));
    const WrapperType = protoRoot.lookupType('gateway.Wrapper');
    internal.Wrapper = WrapperType;

    gw.setSessionForReplay('sess-pre-sim', 'leadership_planner');
    internal.userSpawnedAgents.add('sess-pre-sim');
    internal.primaryChatAgentType = 'leadership_planner';
    expect(gw.getSimulationId()).toBeNull();
    expect(gw.getSessions().length).toBe(1);

    // Go Gateway marshals environment_reset with Payload: nil -> protobufjs decodes payload as []
    const resetMsg = WrapperType.create({
      timestamp: new Date().toISOString(),
      type: 'environment_reset',
      event: 'environment_reset',
      origin: { type: 'system', id: 'gw-1' },
      destination: ['sess-pre-sim'],
    });
    const encoded = WrapperType.encode(resetMsg).finish();
    const buf = new Uint8Array(encoded).buffer;

    expect(() => gw.handleMessage({ data: buf } as MessageEvent)).not.toThrow();
    expect(gw.getSessions()).toEqual([]);
    expect(internal.userSpawnedAgents.size).toBe(0);
    expect(internal.primaryChatAgentType).toBeNull();
    expect(sessionSnapshots.at(-1)).toBe(0);

    gw.dispose();
  });

  it('parses Go Hub TextMessage JSON error frames, emits MODEL_ERROR, evicts unowned sessions, and ignores invalid payloads', async () => {
    const gw = new AgentGateway({ autoConnect: false });
    const received: any[] = [];
    const sessionSnapshots: string[][] = [];
    gw.onChat((m) => received.push(m));
    gw.onSessionsChange((sessions) => sessionSnapshots.push(sessions.map((s) => s.guid)));

    const internal = gw as any;
    const protoRoot = await protobuf.load(path.resolve(process.cwd(), 'public/gateway.proto'));
    internal.Wrapper = protoRoot.lookupType('gateway.Wrapper');

    gw.setSessionForReplay('sess-expired', 'leadership_planner');
    internal.userSpawnedAgents.add('sess-expired');
    expect(gw.hasSession('sess-expired')).toBe(true);

    const jsonErrFrame = JSON.stringify({
      origin: { type: 'system', id: 'gateway' },
      destination: ['sess-expired'],
      status: 'error',
      type: 'error',
      event: 'forbidden',
      data: {
        error: 'session_not_owned',
        details: 'target session is not owned by user',
      },
    });

    expect(() => gw.handleMessage({ data: jsonErrFrame } as MessageEvent)).not.toThrow();
    expect(gw.hasSession('sess-expired')).toBe(false);
    expect(sessionSnapshots.at(-1)).toEqual([]);
    expect(
      received.some(
        (m) =>
          m.msgType === AgentMessageType.MODEL_ERROR &&
          m.guid === 'sess-expired' &&
          m.text === 'target session is not owned by user',
      ),
    ).toBe(true);

    // Non-JSON text frames and unexpected binary payload types must not throw
    expect(() => gw.handleMessage({ data: 'not-json' } as MessageEvent)).not.toThrow();
    expect(() => gw.handleMessage({ data: { unexpected: true } } as unknown as MessageEvent)).not.toThrow();

    gw.dispose();
  });

  it('evicts expired sessions on global /ws session_not_owned frames when destination is null (via data.session_id and tracked broadcast target fallback)', async () => {
    const gw = new AgentGateway({ autoConnect: false });
    const received: any[] = [];
    const sessionSnapshots: string[][] = [];
    gw.onChat((m) => received.push(m));
    gw.onSessionsChange((sessions) => sessionSnapshots.push(sessions.map((s) => s.guid)));

    const internal = gw as any;
    const protoRoot = await protobuf.load(path.resolve(process.cwd(), 'public/gateway.proto'));
    internal.Wrapper = protoRoot.lookupType('gateway.Wrapper');
    internal.BroadcastRequest = protoRoot.lookupType('gateway.BroadcastRequest');
    internal.protoLoadPromise = Promise.resolve();
    internal.monitorSocket = { readyState: 1, send: vi.fn(), close: vi.fn() };

    // Case 1: destination: null with data.session_id (Go Hub global observer frame with target session_id)
    gw.setSessionForReplay('sess-null-dest-data', 'leadership_planner_with_eval');
    internal.userSpawnedAgents.add('sess-null-dest-data');
    internal.primaryChatAgentType = 'leadership_planner_with_eval';
    expect(gw.hasSession('sess-null-dest-data')).toBe(true);

    gw.handleMessage({
      data: JSON.stringify({
        origin: { type: 'system', id: 'gateway' },
        destination: null,
        status: 'error',
        type: 'error',
        event: 'forbidden',
        data: {
          error: 'session_not_owned',
          session_id: 'sess-null-dest-data',
          details: 'target session is not owned by user',
        },
      }),
    } as MessageEvent);

    expect(gw.hasSession('sess-null-dest-data')).toBe(false);
    expect(internal.userSpawnedAgents.has('sess-null-dest-data')).toBe(false);
    expect(internal.primaryChatAgentType).toBeNull();
    expect(sessionSnapshots.at(-1)).toEqual([]);
    expect(
      received.some(
        (m) =>
          m.msgType === AgentMessageType.MODEL_ERROR &&
          m.guid === 'sess-null-dest-data' &&
          m.text === 'target session is not owned by user',
      ),
    ).toBe(true);

    // Case 2: destination: null and no data.session_id -> fallback to last targeted broadcast session ID
    gw.setSessionForReplay('sess-null-dest-fallback', 'leadership_planner_with_eval');
    internal.userSpawnedAgents.add('sess-null-dest-fallback');
    internal.primaryChatAgentType = 'leadership_planner_with_eval';
    await gw.sendBroadcast('Ping stale session', ['sess-null-dest-fallback']);

    gw.handleMessage({
      data: JSON.stringify({
        origin: { type: 'system', id: 'gateway' },
        destination: null,
        status: 'error',
        type: 'error',
        event: 'forbidden',
        data: {
          error: 'session_not_owned',
          details: 'target session is not owned by user',
        },
      }),
    } as MessageEvent);

    expect(gw.hasSession('sess-null-dest-fallback')).toBe(false);
    expect(internal.userSpawnedAgents.has('sess-null-dest-fallback')).toBe(false);
    expect(internal.primaryChatAgentType).toBeNull();
    expect(sessionSnapshots.at(-1)).toEqual([]);
    expect(
      received.some(
        (m) =>
          m.msgType === AgentMessageType.MODEL_ERROR &&
          m.guid === 'sess-null-dest-fallback' &&
          m.text === 'target session is not owned by user',
      ),
    ).toBe(true);

    gw.dispose();
  });

  it('restores session.connected on WebSocket reconnect and reconnects immediately on visibilitychange when closed', () => {
    const sockets: any[] = [];
    class MockWS {
      static OPEN = 1;
      static CONNECTING = 0;
      static CLOSED = 3;
      readyState = MockWS.CONNECTING;
      binaryType = 'arraybuffer';
      onopen: (() => void) | null = null;
      onmessage: ((e: MessageEvent) => void) | null = null;
      onclose: (() => void) | null = null;
      onerror: (() => void) | null = null;
      send = vi.fn();
      close = vi.fn(() => {
        this.readyState = MockWS.CLOSED;
      });
      constructor(_url: string) {
        sockets.push(this);
      }
    }
    vi.stubGlobal('WebSocket', MockWS);
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });

    const gw = new AgentGateway({ autoConnect: false });
    const internal = gw as any;
    internal.Wrapper = {};
    gw.setSessionForReplay('sess-reconnect-1', 'leadership_planner_with_memory');
    expect(gw.getSessions()[0].connected).toBe(true);

    internal.connectMonitor();
    expect(sockets.length).toBe(1);
    sockets[0].readyState = MockWS.OPEN;
    sockets[0].onopen?.();

    // Simulate background WebSocket drop
    sockets[0].readyState = MockWS.CLOSED;
    sockets[0].onclose?.();
    expect(gw.getSessions()[0].connected).toBe(false);

    // Cancel pending backoff timer to simulate waking up on visibilitychange
    if (internal.reconnectTimer) {
      clearTimeout(internal.reconnectTimer);
      internal.reconnectTimer = null;
    }
    document.dispatchEvent(new Event('visibilitychange'));
    expect(sockets.length).toBe(2);

    // Reconnect open restores connected=true without wiping existing sessions
    sockets[1].readyState = MockWS.OPEN;
    sockets[1].onopen?.();
    expect(gw.getSessions()[0].connected).toBe(true);
    expect(gw.hasSession('sess-reconnect-1')).toBe(true);

    gw.dispose();
  });
});
