/**
 * Copyright 2026 Christian Haeussinger
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

import '@angular/compiler';
import { Injector, runInInjectionContext } from '@angular/core';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GatewayService } from '../../gateway.service';
import { AGENT_SCREEN_HOST } from '../ChatNavPanel/screens/AgentScreen/agent-screen-host.model';
import { A2uiActionsService } from './a2ui-actions.service';

describe('A2uiActionsService (SSOT & error handling)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function createService(overrides?: {
    sendBroadcast?: ReturnType<typeof vi.fn>;
    agentScreen?: Record<string, unknown>;
  }) {
    const gateway = {
      sendBroadcast: overrides?.sendBroadcast ?? vi.fn().mockResolvedValue(undefined),
    };
    const agentScreen = {
      runCachedMessages: false,
      isAgentWorking: false,
      currentAgent: { agentType: 'leadership_planner', sessionId: 'sess-9' },
      agents: { leadership_planner: 'sess-9' },
      runCachedDataStream: vi.fn(),
      ...(overrides?.agentScreen ?? {}),
    };
    const injector = Injector.create({
      providers: [
        { provide: GatewayService, useValue: gateway },
        { provide: AGENT_SCREEN_HOST, useValue: agentScreen },
      ],
    });
    const service = runInInjectionContext(injector, () => new A2uiActionsService());
    return { service, gateway, agentScreen };
  }

  it('clears stale geojson without injecting cached static routes on show_route', () => {
    const { service, gateway } = createService();
    const events: Array<{ geojson: unknown }> = [];
    const listener = (e: Event) => events.push((e as CustomEvent).detail);
    window.addEventListener('gateway:routeGeojson', listener);

    try {
      const ok = service.dispatchRegisteredAction(
        'show_route',
        { routeIndex: 1 } as never,
        { runCached: true, plannerAgentGuid: 'sess-9' },
      );
      expect(ok).toBe(false);
      expect(events).toEqual([{ geojson: undefined }]);
      expect(gateway.sendBroadcast).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('gateway:routeGeojson', listener);
    }
  });

  it('forwards show_route seed to gateway when live and swallows broadcast rejections cleanly', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const sendBroadcast = vi.fn().mockRejectedValue(new Error('socket closed'));
    const { service } = createService({ sendBroadcast });

    service.dispatchRegisteredAction(
      'show_route',
      { seed: { literalString: 'seed-abc' } },
      { runCached: false, plannerAgentGuid: 'sess-9' },
    );

    expect(sendBroadcast).toHaveBeenCalledWith('Get the route for the seed seed-abc', ['sess-9'], true);
    await Promise.resolve();
    expect(errSpy).toHaveBeenCalled();
  });
});
