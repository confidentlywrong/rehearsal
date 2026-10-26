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

import { agentGateway, type ChatMessage } from './agent-gateway-updates';
import { AgentMessageType } from './types';

export interface SimulationSnapshot {
  tick: number;
  maxTicks: number;
  realTimeMinutes: number;
  runnersReporting: number;
  avgVelocity: number;
  avgWater: number;
  avgDistance: number;
  statusCounts: Record<string, number>;
}

export interface SimulationConfig {
  durationSeconds: number;
  tickIntervalSeconds: number;
  totalRaceHours: number;
  runnerCount: number;
}

export type SimPhase = 'idle' | 'pre_race' | 'racing' | 'post_race' | 'complete';

export interface SimulationStateData {
  active: boolean;
  sessionId: string | null;
  config: SimulationConfig | null;
  currentTick: number;
  maxTicks: number;
  snapshots: SimulationSnapshot[];
  runnerSessionIds: string[];
  phase: SimPhase;
  finalResults: Record<string, unknown> | null;
}

type StateListener = (state: SimulationStateData) => void;

class SimulationStateService {
  private state: SimulationStateData = {
    active: false,
    sessionId: null,
    config: null,
    currentTick: 0,
    maxTicks: 0,
    snapshots: [],
    runnerSessionIds: [],
    phase: 'idle',
    finalResults: null,
  };

  private listeners: StateListener[] = [];
  private unsub: (() => void) | null = null;

  /** Call once the simulator session ID is known. */
  activate(sessionId: string): void {
    this.state = {
      active: true,
      sessionId,
      phase: 'idle',
      snapshots: [],
      currentTick: 0,
      maxTicks: 0,
      runnerSessionIds: [],
      config: null,
      finalResults: null,
    };
    this.notify();
    // L8 fix: Clean up any existing subscription before creating a new one.
    this.unsub?.();
    this.unsub = agentGateway.onChat((msg) => this.handleMessage(msg));
  }

  deactivate(): void {
    this.unsub?.();
    this.unsub = null;
    this.state = {
      active: false,
      sessionId: null,
      config: null,
      currentTick: 0,
      maxTicks: 0,
      snapshots: [],
      runnerSessionIds: [],
      phase: 'idle',
      finalResults: null,
    };
    this.notify();
  }

  getState(): SimulationStateData {
    return this.state;
  }

  onChange(fn: StateListener): () => void {
    this.listeners.push(fn);
    return () => {
      this.listeners = this.listeners.filter((l) => l !== fn);
    };
  }

  removeAllListeners(): void {
    this.listeners = [];
  }

  private notify(): void {
    for (const fn of this.listeners) fn(this.state);
  }

  private handleMessage(msg: ChatMessage): void {
    if (msg.guid !== this.state.sessionId || msg.isUser) return;

    // Phase transitions from agent_start telemetry
    if (msg.msgType === AgentMessageType.AGENT_START) {
      const text = msg.text.toLowerCase();
      if (text.includes('pre_race')) this.updatePhase('pre_race');
      else if (text.includes('race_engine') || text.includes('tick')) this.updatePhase('racing');
      else if (text.includes('post_race')) this.updatePhase('post_race');
    }

    // Tick snapshots — advance_tick results arrive as TOOL_END, handled in handleToolEnd()

    // Tool results
    if (msg.msgType === AgentMessageType.TOOL_END && msg.toolName) {
      this.handleToolEnd(msg);
    }

    // Run end
    if (msg.msgType === AgentMessageType.RUN_END) {
      this.updatePhase('complete');
    }
  }

  private handleToolEnd(msg: ChatMessage): void {
    let data: Record<string, unknown> = {};
    if (msg.result && typeof msg.result === 'object') {
      data = msg.result;
    } else if (msg.rawJson) {
      try {
        data = JSON.parse(msg.rawJson);
      } catch {
        /* ignore */
      }
    }
    const result = (data['result'] as Record<string, unknown>) ?? data;

    switch (msg.toolName) {
      case 'prepare_simulation': {
        const config = (result['simulation_config'] ?? result['config'] ?? result) as Record<
          string,
          unknown
        >;
        this.state = {
          ...this.state,
          config: {
            durationSeconds: (config['duration_seconds'] as number) ?? 60,
            tickIntervalSeconds: (config['tick_interval_seconds'] as number) ?? 5,
            totalRaceHours: (config['total_race_hours'] as number) ?? 6,
            runnerCount: (config['runner_count'] as number) ?? 10,
          },
        };
        this.notify();
        break;
      }
      case 'spawn_runners': {
        const ids = (result['session_ids'] ?? result['runner_session_ids'] ?? []) as string[];
        this.state = { ...this.state, runnerSessionIds: ids };
        this.notify();
        window.dispatchEvent(
          new CustomEvent('sim:spawnRunners', {
            detail: { sessionIds: ids, targetGuid: this.state.sessionId },
          }),
        );
        break;
      }
      case 'fire_start_gun':
        this.updatePhase('racing');
        break;
      case 'advance_tick': {
        // Tick data arrives as TOOL_END, not as a separate TICK msgType
        const r = (msg.result as Record<string, unknown>) ?? {};
        const snapshot: SimulationSnapshot = {
          tick: (r['tick'] as number) ?? 0,
          maxTicks: (r['max_ticks'] as number) ?? 0,
          realTimeMinutes: (r['real_time_minutes'] as number) ?? 0,
          runnersReporting: (r['runners_reporting'] as number) ?? 0,
          avgVelocity: (r['avg_velocity'] as number) ?? 0,
          avgWater: (r['avg_water'] as number) ?? 0,
          avgDistance: (r['avg_distance'] as number) ?? 0,
          statusCounts: (r['status_counts'] as Record<string, number>) ?? {},
        };
        // Bug #20: Cap snapshots to a rolling window to prevent unbounded growth
        const MAX_SNAPSHOTS = 200;
        const updatedSnapshots = [...this.state.snapshots, snapshot];
        if (updatedSnapshots.length > MAX_SNAPSHOTS) {
          updatedSnapshots.splice(0, updatedSnapshots.length - MAX_SNAPSHOTS);
        }
        this.state = {
          ...this.state,
          currentTick: snapshot.tick,
          maxTicks: snapshot.maxTicks,
          snapshots: updatedSnapshots,
        };
        this.notify();
        window.dispatchEvent(new CustomEvent('sim:tick', { detail: snapshot }));
        break;
      }
      case 'compile_results':
        this.state = { ...this.state, finalResults: result, phase: 'complete' };
        this.notify();
        break;
    }
  }

  private updatePhase(phase: SimPhase): void {
    if (this.state.phase !== phase) {
      this.state = { ...this.state, phase };
      this.notify();
    }
  }
}

export const simulationState = new SimulationStateService();
