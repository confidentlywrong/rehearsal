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

import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { UserMenuComponent } from './auth/user-menu.component';

/**
 * AppComponent — post-Phase 9 shell.
 *
 * Wraps the router-outlet with the persistent overlay that should
 * appear on every route:
 *   - <app-user-menu>     (top-right, self-hides on SAAS_MODE=off)
 *
 * The org-switcher was removed once we collapsed to a single
 * org-per-customer model — the company name is surfaced in the
 * user-menu sub-line and edited from the Profile page, so there is
 * nothing left to switch between.
 *
 * The 3D viewport and dashboard HUD live in DashboardComponent now
 * and are mounted by the router at path `''`. When SAAS_MODE=off,
 * the user-menu self-hides and the visual surface is byte-identical
 * to the pre-router build.
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, UserMenuComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-user-menu></app-user-menu>
    <router-outlet></router-outlet>
  `,
  styles: [
    `
      :host {
        display: block;
        width: 100vw;
        height: 100vh;
        overflow: hidden;
      }
    `,
  ],
})
export class AppComponent {}
