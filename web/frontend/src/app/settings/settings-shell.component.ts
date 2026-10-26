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
 * SettingsShellComponent — Phase 9.
 *
 * Wraps every /settings/* page in a consistent layout:
 *   - Header strip with "Back to dashboard" link.
 *   - Left-nav with Profile / Billing.
 *   - <router-outlet/> for the active child route.
 *
 * The org-switcher (top-left) and user-menu (top-right) are mounted
 * by AppComponent and are always visible, so they're not duplicated
 * here.
 */

import { ChangeDetectionStrategy, Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

@Component({
  selector: 'app-settings-shell',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, RouterLink, RouterLinkActive, RouterOutlet],
  template: `
    <div class="settings-shell">
      <header class="settings-shell__header">
        <a class="settings-shell__back" routerLink="/">← Dashboard</a>
        <h1 class="settings-shell__title">Settings</h1>
      </header>
      <div class="settings-shell__body">
        <nav class="settings-shell__nav" aria-label="Settings sections">
          <a
            routerLink="/settings/profile"
            routerLinkActive="settings-shell__nav-item--active"
            class="settings-shell__nav-item"
          >
            Profile
          </a>
          <a
            routerLink="/settings/billing"
            routerLinkActive="settings-shell__nav-item--active"
            class="settings-shell__nav-item"
          >
            Billing &amp; plan
          </a>
          <a
            routerLink="/settings/memory"
            routerLinkActive="settings-shell__nav-item--active"
            class="settings-shell__nav-item"
          >
            Memory
          </a>
        </nav>
        <main class="settings-shell__content">
          <router-outlet></router-outlet>
        </main>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        width: 100vw;
        height: 100vh;
        overflow: auto;
        background: #07111a;
        color: #e8f8ff;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
          sans-serif;
      }
      .settings-shell {
        min-height: 100vh;
        display: flex;
        flex-direction: column;
      }
      .settings-shell__header {
        padding: 72px 32px 16px 32px;
        border-bottom: 1px solid rgba(0, 255, 200, 0.12);
        display: flex;
        align-items: baseline;
        gap: 24px;
      }
      .settings-shell__back {
        color: #00ffcc;
        text-decoration: none;
        font-size: 13px;
      }
      .settings-shell__back:hover {
        text-decoration: underline;
      }
      .settings-shell__title {
        margin: 0;
        font-size: 22px;
        font-weight: 600;
        letter-spacing: 0.02em;
      }
      .settings-shell__body {
        flex: 1 1 auto;
        display: grid;
        /* Sidebar hidden — single-column layout. To restore the sidebar,
           change to: grid-template-columns: 220px 1fr; and remove
           display:none from .settings-shell__nav below. */
        grid-template-columns: 1fr;
        gap: 0;
      }
      .settings-shell__nav {
        /* Hidden but preserved in DOM so routes still work. */
        display: none;
        padding: 24px 12px;
        border-right: 1px solid rgba(255, 255, 255, 0.06);
        flex-direction: column;
        gap: 2px;
      }
      .settings-shell__nav-item {
        display: block;
        padding: 8px 14px;
        border-radius: 6px;
        color: #c8d8e6;
        text-decoration: none;
        font-size: 13px;
      }
      .settings-shell__nav-item:hover {
        background: rgba(0, 255, 200, 0.06);
        color: #e8f8ff;
      }
      .settings-shell__nav-item--active {
        background: rgba(0, 255, 200, 0.12);
        color: #00ffcc;
        font-weight: 600;
      }
      .settings-shell__content {
        padding: 32px 40px;
        max-width: 720px;
      }
    `,
  ],
})
export class SettingsShellComponent {}
