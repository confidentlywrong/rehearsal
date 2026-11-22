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
 * Changed for The Rehearsal: clamp out-of-range cached stream index, clear host working state when stream lookup or fetch fails, and guard async fetch/text/timer lifecycle against stop() teardown.
 */

import { Injectable } from '@angular/core';
import { DEMO_CONFIG } from '../../../../../demo-config';
import {
  parseAgentGatewayMsgNdjsonInterFrameReplayMeta,
  parseAgentGatewayMsgNdjsonToInboundRecords,
  replayAgentGatewayMsgNdjsonText,
} from '../../../../../../../agent-gateway-message-dump';
import { agentGateway } from '../../../../../agent-gateway-updates';
import type { DemoService } from '../../../../DemoOverlay/demo.service';

/** Minimal host surface for NDJSON replay (avoids circular imports with AgentScreen). */
export interface AgentCachedGatewayReplayHost {
  demoService: DemoService;
  currentDemoCachedRunCount: number;
  cachedMessageTimeScale: number;
  onCachedReplayComplete(): void;
  onCachedReplayAbort?(): void;
}

@Injectable()
export class AgentCachedGatewayReplayService {
  private cachedReplayStop?: () => void;
  private cachedCountdownTimer?: ReturnType<typeof setTimeout>;
  private runGen = 0;
  private abortController: AbortController | null = null;

  /**
   * Loads the active demo’s cached NDJSON stream and replays it into {@link agentGateway.handleMessage}.
   */
  async run(host: AgentCachedGatewayReplayHost): Promise<void> {
    this.stop();
    const gen = ++this.runGen;
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    this.abortController = controller;

    const activeDemoKey = host.demoService.activeDemo();
    const activeDemo = DEMO_CONFIG[activeDemoKey] as {
      agent?: string;
      recordingConfig?: { cachedMessageStreams: string[] };
    };

    const streams = activeDemo?.recordingConfig?.cachedMessageStreams;
    const streamIdx =
      Array.isArray(streams) && streams.length > 0
        ? Math.min(Math.max(0, host.currentDemoCachedRunCount), streams.length - 1)
        : -1;
    const url = streamIdx >= 0 ? streams![streamIdx] : undefined;
    if (url == null || url === '') {
      if (this.abortController === controller) {
        this.abortController = null;
      }
      console.error('[AgentScreen] cached gateway replay: no stream URL for this run index');
      host.onCachedReplayAbort?.();
      return;
    }

    let text: string;
    try {
      const res = await fetch(url, controller ? { signal: controller.signal } : undefined);
      if (gen !== this.runGen) return;
      if (!res.ok) {
        throw new Error(`${res.status} ${res.statusText}`);
      }
      text = await res.text();
      if (gen !== this.runGen) return;
      const ct = res.headers.get('content-type') ?? '';
      if (ct.includes('text/html') || text.trimStart().startsWith('<!')) {
        throw new Error(
          'Got HTML instead of NDJSON (SPA fallback). Ensure the file exists under public/.',
        );
      }
    } catch (e) {
      if (gen !== this.runGen) return;
      console.error('[AgentScreen] cached gateway dump fetch failed', url, e);
      host.onCachedReplayAbort?.();
      return;
    } finally {
      if (this.abortController === controller) {
        this.abortController = null;
      }
    }

    if (gen !== this.runGen) return;

    agentGateway.setReplayPrimaryAgentTypeHint(activeDemo.agent as string);
    agentGateway.beginNdjsonReplay();

    this.scheduleCachedCountdown(host, text, gen);

    this.cachedReplayStop = replayAgentGatewayMsgNdjsonText(
      (e) => agentGateway.handleMessage(e),
      text,
      {
        encodeSemanticLine: (rec) => agentGateway.encodeInboundRecordForReplay(rec),
        intervalMs: 2000,
        useRecordedPerformanceTimestamps: true,
        getTimingScale: () => host.cachedMessageTimeScale,
        onComplete: () => {
          if (gen !== this.runGen) return;
          agentGateway.endNdjsonReplay();
          host.onCachedReplayComplete();
        },
      },
    );
  }

  /** Stops timed replay (same as the prior `stop2` callback). */
  stop(): void {
    ++this.runGen;
    this.abortController?.abort();
    this.abortController = null;
    this.clearCachedCountdownTimer();
    this.cachedReplayStop?.();
    this.cachedReplayStop = undefined;
    agentGateway.endNdjsonReplay();
  }

  private scheduleCachedCountdown(
    host: AgentCachedGatewayReplayHost,
    ndjsonText: string,
    gen: number,
  ): void {
    this.clearCachedCountdownTimer();

    const records = parseAgentGatewayMsgNdjsonToInboundRecords(ndjsonText);
    let startIndex = records.findIndex(
      (r) =>
        r.event === 'tool_end' &&
        (r.data['tool_name'] === 'process_tick' ||
          r.data['tool_name'] === 'process_leadership_tick'),
    );
    if (startIndex < 0) {
      startIndex = records.findIndex(
        (r) => r.event === 'tool_end' && r.data['tool_name'] === 'fire_start_gun',
      );
    }
    if (startIndex <= 0) return;

    const meta = parseAgentGatewayMsgNdjsonInterFrameReplayMeta(ndjsonText, 2000);
    if (meta.delays.length < startIndex) return;

    const scale =
      host.cachedMessageTimeScale > 0 && Number.isFinite(host.cachedMessageTimeScale)
        ? host.cachedMessageTimeScale
        : 1;
    let cumulativeMs = 0;
    for (let i = 0; i < startIndex; i++) {
      const baseGap = meta.delays[i];
      const apply = meta.interFrameApplyTimingScale[i];
      cumulativeMs += apply ? baseGap / scale : baseGap;
    }

    const COUNTDOWN_LEAD_MS = 3000;
    const triggerInMs = Math.max(0, cumulativeMs - COUNTDOWN_LEAD_MS);
    this.cachedCountdownTimer = setTimeout(() => {
      this.cachedCountdownTimer = undefined;
      if (gen !== this.runGen) return;
      window.dispatchEvent(new Event('countdown:autoStart'));
    }, triggerInMs);
  }

  private clearCachedCountdownTimer(): void {
    if (this.cachedCountdownTimer !== undefined) {
      clearTimeout(this.cachedCountdownTimer);
      this.cachedCountdownTimer = undefined;
    }
  }
}
