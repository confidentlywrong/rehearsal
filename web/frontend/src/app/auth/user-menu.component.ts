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
 * UserMenuComponent — SaaS Phase 9.
 *
 * Top-right avatar pill that opens a small dropdown with:
 *   - the signed-in email (read-only),
 *   - a "Settings" link routed to /settings/profile,
 *   - a "Sign out" action that calls AuthService.signOut().
 *
 * As of 2026-06-22 the dropdown ALSO hosts the agent runtime knobs
 * (Download recording, AI Connection Cached/Live, Replay speed,
 * Planner security, Cloud Assist) that used to live in the chat
 * panel header. AgentScreen registers itself with
 * AgentRuntimeControlsService on mount; this component subscribes
 * and renders the relevant section only when a host is registered
 * (i.e. only when the user is on a route that mounts AgentScreen).
 * Off other routes the agent section is hidden and the dropdown
 * looks identical to its pre-change form.
 *
 * Self-hides in single-tenant mode (SAAS_MODE=off) for the same
 * reason the org-switcher does — IdentityService.fetchMe() returns
 * available:false and the *ngIf collapses.
 *
 * Hand-rolled HTML (no UI framework) to match the rest of the app's
 * dependency-flat overlay style.
 */

import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  HostListener,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';

import { AuthService } from './auth.service';
import { IdentityService, MeResponse } from './identity.service';
import {
  AgentRuntimeControls,
  AgentRuntimeControlsService,
} from '../components/ChatNavPanel/screens/AgentScreen/agent-runtime-controls.service';
import { UsageTileComponent } from '../usage/usage-tile.component';


@Component({
  selector: 'app-user-menu',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    FormsModule,
    RouterLink,
    UsageTileComponent,
  ],
  template: `
    <div class="user-menu" *ngIf="me as data">
      <button
        type="button"
        class="user-menu__trigger"
        [attr.aria-expanded]="open"
        aria-haspopup="menu"
        (click)="toggle($event)"
      >
        <span class="user-menu__avatar" aria-hidden="true">{{ initials(data) }}</span>
        <span class="user-menu__email">{{ data.user?.email || 'Signed in' }}</span>
        <span class="user-menu__caret" aria-hidden="true">▾</span>
      </button>

      <div
        class="user-menu__dropdown"
        role="menu"
        *ngIf="open"
        (click)="$event.stopPropagation()"
      >
        <div class="user-menu__header">
          <div class="user-menu__name">{{ data.user?.email || 'Signed in' }}</div>
          <div class="user-menu__sub" *ngIf="planLabel(data) as plan">
            Plan: {{ plan }}
          </div>
        </div>

        <a
          class="user-menu__item"
          role="menuitem"
          routerLink="/settings/profile"
          (click)="open = false"
        >
          Settings
        </a>

        <!--
          2026-06-23 (B-19): the "Billing & plan" shortcut was
          removed from this dropdown. Settings is now the single
          entry point for billing/plan management — the Settings
          shell already exposes Billing in its left-nav, so two
          access points were redundant.
        -->

        <!--
          2026-06-22 — Token usage tile, relocated from the
          dashboard top-right (where it was overlapping this
          user-menu pill). The tile owns its own polling and
          render gates; we just render it inline. The inline
          input drops the fixed-position card chrome so the
          contents flow inside the dropdown.

          2026-06-23 (B-19): the tile no longer emits
          changePlanRequested — the "Change plan ▸" button was
          removed in favour of Settings → Billing as the sole
          plan-change entry point. The user-menu therefore no
          longer mounts the upgrade modal either.
        -->
        <div class="user-menu__section-divider"></div>
        <app-usage-tile [inline]="true"></app-usage-tile>

        <!--
          Agent runtime knobs — only rendered when AgentScreen is
          currently mounted (i.e. the chat panel is on screen).
          Otherwise the whole block collapses and the dropdown looks
          like the pre-2026-06-22 user menu.
        -->
        <ng-container *ngIf="agentControls as ac">
          <div class="user-menu__section-divider"></div>
          <div class="user-menu__section-heading">Agent runtime</div>

          <div class="user-menu__row">
            <span class="user-menu__row-label">Keep latest recording</span>
            <button
              type="button"
              class="user-menu__pill-btn"
              [disabled]="!ac.agentGatewayMsgDumpCanDownload"
              (click)="ac.agentGatewayMsgDebugDownload()"
            >
              Download
            </button>
          </div>

          <div class="user-menu__row">
            <span class="user-menu__row-label">AI Connection</span>
            <div class="user-menu__segmented" role="group" aria-label="AI Connection">
              <button
                type="button"
                class="user-menu__segment"
                [attr.aria-pressed]="ac.runCachedMessages"
                [class.user-menu__segment--active]="ac.runCachedMessages"
                [disabled]="!ac.activeDemoHasRecordingConfig"
                (click)="setCached(ac, true)"
              >
                Cached
              </button>
              <button
                type="button"
                class="user-menu__segment"
                [attr.aria-pressed]="!ac.runCachedMessages"
                [class.user-menu__segment--active]="!ac.runCachedMessages"
                (click)="setCached(ac, false)"
              >
                Live
              </button>
            </div>
          </div>

          <div *ngIf="ac.activeDemoHasRecordingConfig" class="user-menu__row">
            <span class="user-menu__row-label">Replay speed (×)</span>
            <input
              type="number"
              class="user-menu__numeric"
              min="0.5"
              max="10"
              step="0.5"
              [ngModel]="ac.cachedMessageTimeScale"
              (ngModelChange)="ac.onCachedMessageTimeScaleChange($event)"
              aria-label="Cached message replay speed multiplier"
            />
          </div>

          <div *ngIf="ac.isSecurityDemo" class="user-menu__row">
            <span class="user-menu__row-label">Planner security</span>
            <div class="user-menu__segmented" role="group" aria-label="Planner security">
              <button
                type="button"
                class="user-menu__segment"
                [attr.aria-pressed]="ac.secureMode"
                [class.user-menu__segment--active]="ac.secureMode"
                (click)="ac.setPlannerSecurity(true)"
              >
                Secure
              </button>
              <button
                type="button"
                class="user-menu__segment"
                [attr.aria-pressed]="!ac.secureMode"
                [class.user-menu__segment--active]="!ac.secureMode"
                (click)="ac.setPlannerSecurity(false)"
              >
                Unsecure
              </button>
            </div>
          </div>

          <div *ngIf="ac.isIntentToInfrastructureDemo" class="user-menu__row">
            <span class="user-menu__row-label">Gemini Cloud Assist</span>
            <div class="user-menu__segmented" role="group" aria-label="Gemini Cloud Assist">
              <button
                type="button"
                class="user-menu__segment"
                [attr.aria-pressed]="ac.cloudAssistMode"
                [class.user-menu__segment--active]="ac.cloudAssistMode"
                (click)="setCloudAssist(ac, true)"
              >
                Upgrade
              </button>
              <button
                type="button"
                class="user-menu__segment"
                [attr.aria-pressed]="!ac.cloudAssistMode"
                [class.user-menu__segment--active]="!ac.cloudAssistMode"
                (click)="setCloudAssist(ac, false)"
              >
                Standard
              </button>
            </div>
          </div>

          <div class="user-menu__section-divider"></div>
        </ng-container>

        <button
          type="button"
          class="user-menu__item user-menu__item--danger"
          role="menuitem"
          (click)="signOut()"
        >
          Sign out
        </button>
      </div>

      <!--
        2026-06-23 (B-19): the upgrade modal mount that used to
        live here (originally hoisted in B-10 to dodge the
        document-click-tears-down-modal bug) is gone. With the
        "Change plan ▸" trigger removed from this dropdown, no
        component below this point can request the modal anymore;
        the upgrade flow lives entirely on Settings → Billing.
      -->
    </div>
  `,
  styles: [
    `
      .user-menu {
        position: fixed;
        top: 12px;
        right: 12px;
        z-index: 60;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
          sans-serif;
        font-size: 12px;
      }
      .user-menu__trigger {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 10px 6px 6px;
        background: rgba(17, 22, 31, 0.85);
        backdrop-filter: blur(6px);
        border: 1px solid #1c2531;
        border-radius: 999px;
        color: #e6edf3;
        cursor: pointer;
        max-width: 260px;
      }
      .user-menu__trigger:hover {
        border-color: rgba(0, 255, 200, 0.45);
      }
      .user-menu__avatar {
        width: 24px;
        height: 24px;
        border-radius: 50%;
        background: linear-gradient(135deg, #00ffcc 0%, #4aa8ff 100%);
        color: #001018;
        font-weight: 700;
        font-size: 11px;
        display: flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
      }
      .user-menu__email {
        flex: 1 1 auto;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 180px;
      }
      .user-menu__caret {
        opacity: 0.6;
        font-size: 10px;
      }
      .user-menu__dropdown {
        position: absolute;
        top: calc(100% + 6px);
        right: 0;
        min-width: 260px;
        background: rgba(10, 18, 26, 0.96);
        border: 1px solid rgba(0, 255, 200, 0.32);
        border-radius: 8px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
        padding: 6px 0;
        color: #e8f8ff;
      }
      .user-menu__header {
        padding: 8px 12px 10px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.06);
        margin-bottom: 4px;
      }
      .user-menu__name {
        font-size: 12px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .user-menu__sub {
        font-size: 11px;
        opacity: 0.65;
        margin-top: 2px;
      }
      .user-menu__item {
        display: block;
        width: 100%;
        text-align: left;
        padding: 8px 12px;
        background: transparent;
        border: none;
        color: #e8f8ff;
        text-decoration: none;
        font-size: 12px;
        cursor: pointer;
      }
      .user-menu__item:hover {
        background: rgba(0, 255, 200, 0.08);
        color: #00ffcc;
      }
      .user-menu__item--danger:hover {
        background: rgba(255, 90, 90, 0.12);
        color: #ff9b9b;
      }

      /* ---- Agent runtime section ---------------------------------- */
      .user-menu__section-divider {
        height: 1px;
        background: rgba(255, 255, 255, 0.08);
        margin: 6px 0;
      }
      .user-menu__section-heading {
        padding: 4px 12px 6px;
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: rgba(0, 255, 200, 0.7);
      }
      .user-menu__row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        padding: 6px 12px;
        font-size: 12px;
      }
      .user-menu__row-label {
        flex: 1 1 auto;
        color: rgba(255, 255, 255, 0.85);
      }
      .user-menu__pill-btn {
        flex: 0 0 auto;
        padding: 4px 12px;
        font: inherit;
        font-size: 11px;
        font-weight: 600;
        color: rgba(10, 10, 12, 0.92);
        background: #fff;
        border: none;
        border-radius: 999px;
        cursor: pointer;
        transition: opacity 0.15s ease;
      }
      .user-menu__pill-btn:hover {
        opacity: 0.92;
      }
      .user-menu__pill-btn:disabled {
        cursor: not-allowed;
        opacity: 0.4;
      }
      .user-menu__segmented {
        flex: 0 0 auto;
        display: inline-flex;
        align-items: stretch;
        padding: 2px;
        background: rgba(255, 255, 255, 0.08);
        border-radius: 999px;
      }
      .user-menu__segment {
        flex: 0 0 auto;
        min-width: 56px;
        padding: 3px 10px;
        font: inherit;
        font-size: 11px;
        font-weight: 500;
        color: rgba(255, 255, 255, 0.6);
        background: transparent;
        border: none;
        border-radius: 999px;
        cursor: pointer;
        transition:
          color 0.15s,
          background 0.15s;
      }
      .user-menu__segment:hover:not(:disabled):not(.user-menu__segment--active) {
        color: #e8f8ff;
      }
      .user-menu__segment--active {
        color: #1a1f24;
        background: #fff;
      }
      .user-menu__segment:disabled {
        cursor: not-allowed;
        opacity: 0.4;
      }
      .user-menu__numeric {
        flex: 0 0 auto;
        width: 72px;
        padding: 4px 8px;
        font: inherit;
        font-size: 12px;
        color: #e8f8ff;
        text-align: center;
        background: rgba(12, 12, 14, 0.95);
        border: 1px solid rgba(255, 255, 255, 0.2);
        border-radius: 6px;
        appearance: textfield;
      }
      .user-menu__numeric::-webkit-outer-spin-button,
      .user-menu__numeric::-webkit-inner-spin-button {
        margin: 0;
        -webkit-appearance: none;
      }
    `,
  ],
})
export class UserMenuComponent implements OnInit, OnDestroy {
  me: MeResponse | null = null;
  open = false;
  private destroyed = false;

  /**
   * Currently-registered AgentScreen runtime controls, or null if the
   * agent route is not mounted. Re-published by
   * AgentRuntimeControlsService and refreshed on every change so
   * OnPush re-renders the dropdown's runtime section live.
   */
  agentControls: AgentRuntimeControls | null = null;
  private agentControlsSub: Subscription | null = null;

  constructor(
    private identity: IdentityService,
    private auth: AuthService,
    private cdr: ChangeDetectorRef,
    private agentRuntime: AgentRuntimeControlsService,
  ) {}

  async ngOnInit(): Promise<void> {
    this.pollInitMe();

    // Pick up the current host immediately (in case AgentScreen
    // mounted before this user-menu did, which never happens on
    // initial load but can during HMR), then subscribe for future
    // mount/unmount events.
    this.agentControls = this.agentRuntime.current;
    this.agentControlsSub = this.agentRuntime.controls$.subscribe((ctrls) => {
      this.agentControls = ctrls;
      this.cdr.markForCheck();
    });
  }

  private async pollInitMe(): Promise<void> {
    let delay = 2000;
    while (!this.destroyed) {
      try {
        const snap = await this.identity.ensureInitialized();
        if (this.destroyed) return;
        
        if (snap.available && snap.data) {
          this.me = snap.data;
          this.cdr.markForCheck();
          return;
        }
        // Backend not ready yet — fall through to retry delay.
      } catch (err) {
        console.warn('UserMenu: fetch /me failed, retrying...', err);
      }
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 30000); // Back off up to 30s
    }
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.agentControlsSub?.unsubscribe();
    this.agentControlsSub = null;
  }

  toggle(ev: MouseEvent): void {
    ev.stopPropagation();
    this.open = !this.open;
  }

  /** Close the dropdown on any outside click. */
  @HostListener('document:click')
  onDocumentClick(): void {
    if (this.open) {
      this.open = false;
      this.cdr.markForCheck();
    }
  }

  signOut(): void {
    this.open = false;
    this.auth.signOut('/');
  }

  initials(data: MeResponse): string {
    const email = data.user?.email || '';
    if (!email) return '·';
    const local = email.split('@')[0] || email;
    return local.slice(0, 2).toUpperCase();
  }

  planLabel(data: MeResponse): string | null {
    const tier = data.user?.plan_tier;
    if (!tier) return null;
    return tier.charAt(0).toUpperCase() + tier.slice(1);
  }

  /**
   * Thin wrappers that ignore the returned Promise from the host so
   * the template handlers stay synchronous (Angular logs a warning
   * if (click) returns a Promise that throws).
   */
  setCached(ac: AgentRuntimeControls, value: boolean): void {
    void ac.setRunCachedMessages(value);
  }
  setCloudAssist(ac: AgentRuntimeControls, value: boolean): void {
    void ac.setCloudAssistMode(value);
  }
}
