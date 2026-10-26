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

import { Injectable } from '@angular/core';
import { BehaviorSubject } from 'rxjs';

/**
 * Narrow surface exposed to the top-right user menu for the demo's
 * agent runtime knobs (Download recording, Cached/Live, Replay speed,
 * Planner security, Cloud Assist). Defined locally instead of pulling
 * in `AgentScreenHost` so the user menu doesn't end up transitively
 * depending on chat-panel internals.
 */
export interface AgentRuntimeControls {
  // Recording download
  agentGatewayMsgDumpCanDownload: boolean;
  agentGatewayMsgDebugDownload(): void;

  // AI Connection (Cached vs Live)
  runCachedMessages: boolean;
  activeDemoHasRecordingConfig: boolean;
  setRunCachedMessages(value: boolean, options?: { showModeFeedback?: boolean }): Promise<void>;

  // Cached replay speed
  cachedMessageTimeScale: number;
  onCachedMessageTimeScaleChange(value: string | number): void;

  // Per-demo toggles
  isSecurityDemo: boolean;
  secureMode: boolean;
  setPlannerSecurity(secure: boolean): void;

  isIntentToInfrastructureDemo: boolean;
  cloudAssistMode: boolean;
  setCloudAssistMode(value: boolean): Promise<void>;
}

/**
 * Bridge between the route-mounted AgentScreen and the persistent
 * top-right user-menu overlay (mounted by AppComponent). The agent
 * runtime knobs used to live inline in the chat panel header; they
 * now render inside the user-menu dropdown.
 *
 * Lifecycle:
 *   - AgentScreen.ngOnInit():    register(this)
 *   - AgentScreen.ngOnDestroy(): unregister(this)
 *
 * UserMenu subscribes to controls$ and renders the runtime section
 * only when a host is currently registered (i.e. the chat panel is
 * mounted). Off any other route the section is hidden.
 *
 * Provided in root because both consumer (UserMenu, mounted at app
 * root) and producer (AgentScreen, route-lazy) need the same
 * instance, and there is no shared module-scope between them.
 */
@Injectable({ providedIn: 'root' })
export class AgentRuntimeControlsService {
  private readonly controls$$ = new BehaviorSubject<AgentRuntimeControls | null>(null);
  readonly controls$ = this.controls$$.asObservable();

  get current(): AgentRuntimeControls | null {
    return this.controls$$.value;
  }

  register(controls: AgentRuntimeControls): void {
    this.controls$$.next(controls);
  }

  /**
   * Only clears the slot if the caller is the currently-registered
   * controls instance. Guards against a stale ngOnDestroy from a
   * previously-mounted AgentScreen wiping out a freshly-mounted one
   * (can happen during route re-mounts in dev).
   */
  unregister(controls: AgentRuntimeControls): void {
    if (this.controls$$.value === controls) {
      this.controls$$.next(null);
    }
  }
}
