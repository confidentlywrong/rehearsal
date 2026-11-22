/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: validate tool payload objects, arrays, and finite metrics, and guard window event dispatch for SSR.
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
    for (const fn of [...this.listeners]) fn(this.state);
  }

  private handleMessage(msg: ChatMessage): void {
    if (!msg || msg.guid !== this.state.sessionId || msg.isUser) return;

    // Phase transitions from agent_start telemetry
    if (msg.msgType === AgentMessageType.AGENT_START) {
      const text = typeof msg.text === 'string' ? msg.text.toLowerCase() : '';
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
    const isPlainObject = (v: unknown): v is Record<string, unknown> =>
      v !== null && typeof v === 'object' && !Array.isArray(v);
    const finiteNum = (v: unknown, fallback: number, min = Number.NEGATIVE_INFINITY): number =>
      typeof v === 'number' && Number.isFinite(v) && v >= min ? v : fallback;

    let data: Record<string, unknown> = {};
    if (isPlainObject(msg.result)) {
      data = msg.result;
    } else if (typeof msg.rawJson === 'string') {
      try {
        const parsed: unknown = JSON.parse(msg.rawJson);
        if (isPlainObject(parsed)) data = parsed;
      } catch {
        /* ignore */
      }
    }
    const result = isPlainObject(data['result']) ? data['result'] : data;

    switch (msg.toolName) {
      case 'prepare_simulation': {
        const rawCfg = result['simulation_config'] ?? result['config'];
        const config = isPlainObject(rawCfg) ? rawCfg : result;
        this.state = {
          ...this.state,
          config: {
            durationSeconds: finiteNum(config['duration_seconds'], 60, 1),
            tickIntervalSeconds: finiteNum(config['tick_interval_seconds'], 5, 1),
            totalRaceHours: finiteNum(config['total_race_hours'], 6, 0.01),
            runnerCount: finiteNum(config['runner_count'], 10, 0),
          },
        };
        this.notify();
        break;
      }
      case 'spawn_runners': {
        const rawIds = result['session_ids'] ?? result['runner_session_ids'];
        const ids = Array.isArray(rawIds)
          ? rawIds.filter((id): id is string => typeof id === 'string' && id.length > 0)
          : [];
        this.state = { ...this.state, runnerSessionIds: ids };
        this.notify();
        if (typeof window !== 'undefined') {
          window.dispatchEvent(
            new CustomEvent('sim:spawnRunners', {
              detail: { sessionIds: ids, targetGuid: this.state.sessionId },
            }),
          );
        }
        break;
      }
      case 'fire_start_gun':
        this.updatePhase('racing');
        break;
      case 'advance_tick': {
        // Tick data arrives as TOOL_END, not as a separate TICK msgType
        const r = isPlainObject(msg.result) ? msg.result : result;
        const snapshot: SimulationSnapshot = {
          tick: finiteNum(r['tick'], 0, 0),
          maxTicks: finiteNum(r['max_ticks'], 0, 0),
          realTimeMinutes: finiteNum(r['real_time_minutes'], 0, 0),
          runnersReporting: finiteNum(r['runners_reporting'], 0, 0),
          avgVelocity: finiteNum(r['avg_velocity'], 0),
          avgWater: finiteNum(r['avg_water'], 0),
          avgDistance: finiteNum(r['avg_distance'], 0),
          statusCounts: isPlainObject(r['status_counts'])
            ? (r['status_counts'] as Record<string, number>)
            : {},
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
        if (typeof window !== 'undefined') {
          window.dispatchEvent(new CustomEvent('sim:tick', { detail: snapshot }));
        }
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
