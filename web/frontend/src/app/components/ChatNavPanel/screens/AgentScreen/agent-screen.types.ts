/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 */

import type { ChatMessage } from '../../../../gateway.service';

export interface RouteCard {
  id: string;
  name: string;
  distance: string;
  color: string;
  imageDataUrl: string;
  coords: [number, number][];
}

export interface SimRunner {
  id: string;
  name: string;
  status: 'active' | 'finished' | 'did-not-finish';
  velocity: number;
  hydration: number;
  energy: number;
  percentComplete: number;
  color: string;
}

/** Standalone tick-progress card shown while a simulation is running */
export interface TickProgressItem {
  kind: 'tick_progress';
  guid: string;
  tick: number;
  maxTicks: number;
  label: string;
  done: boolean;
}

/** Union type for display items in the chat list */
export type DisplayItem =
  | { kind: 'system'; msg: ChatMessage }
  | { kind: 'message'; msg: ChatMessage }
  // `guid` is the session id of the agent that emitted the surface so the
  // controller can route registered actions (Re-run, etc.) back to that agent.
  | { kind: 'a2ui'; node: unknown; guid?: string }
  | { kind: 'tool_call'; msg: ChatMessage; done?: boolean; error?: boolean; warning?: boolean }
  | {
      kind: 'tick_feed';
      tick: number;
      maxTicks: number;
      voices: string[];
      statusCounts: Record<string, number>;
      microFeedback: string;
      /** Aggregated notable events summary */
      eventsSummary: {
        transitions: Record<string, {
          total: number;
          by_persona: Record<string, number>;
        }>;
        echoes: string[];
        other: string[];
      };
      // Phase 1 enrichments
      completionPct: number;
      resourceDeltas: Record<string, {
        name: string;
        current: number;
        delta: number;
        critical_threshold: number;
        is_critical: boolean;
        is_warning: boolean;
      }>;
      infrastructure: {
        medical_saves_this_tick: number;
        medical_events: string[];
        avg_comfort: number;
        comfort_warning: boolean;
        active_gatekeepers: number;
        max_friction: number;
        friction_level: string;
        friction_details: Array<{ role: string; severity: number }>;
      };
      activeAlertsCount: number;
    };

export interface PathEntry {
  id: number;
  name: string;
  lengthMi: number;
  colorHex: string;
  waterStationCount: number;
}

export interface SyncPayload {
  paths: PathEntry[];
  selectedId: number | null;
}
