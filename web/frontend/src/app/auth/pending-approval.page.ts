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
 * PendingApprovalPageComponent — the screen shown to a signed-in
 * user whose row's status is not yet "approved".
 *
 * Reachable two ways:
 *   - The approvalGuard redirects every non-/pending-approval route
 *     here when /me returns status !== "approved".
 *   - A direct visit to /pending-approval (e.g. from a bookmark).
 *
 * The page itself fetches /me on mount and routes back to the
 * dashboard if status flipped to "approved" in the meantime — that
 * way the user does not have to refresh after the operator
 * approves them. "Refresh" and "Sign out" buttons cover the rest.
 *
 * Status branching:
 *   - "pending"  → friendly "review in progress" copy.
 *   - "rejected" → blunt copy + contact-support hint.
 *   - empty / anything else → defensive "pending" copy.
 */

import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
} from '@angular/core';
import { Router } from '@angular/router';

import { AuthService } from './auth.service';
import { IdentityService } from './identity.service';

@Component({
  selector: 'rc-pending-approval',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  template: `
    <div class="approval-shell">
      <div class="approval-card">
        <div class="approval-glow" aria-hidden="true"></div>
        <h1 class="approval-title">
          {{ rejected ? 'Account not approved' : 'Account awaiting approval' }}
        </h1>

        <p *ngIf="!rejected" class="approval-body">
          Thanks for signing up. Your account is in the review queue —
          an administrator will email you once it's approved. You can
          leave this page open; it refreshes automatically when your
          status changes.
        </p>

        <p *ngIf="rejected" class="approval-body">
          Your sign-up was not approved. If you believe this is in
          error, please contact support with the email address you
          used to sign up.
        </p>

        <p *ngIf="email" class="approval-email">
          Signed in as <strong>{{ email }}</strong>
        </p>

        <div class="approval-actions">
          <button
            type="button"
            class="approval-btn approval-btn--primary"
            [disabled]="loading"
            (click)="refresh()">
            {{ loading ? 'Checking…' : 'Check again' }}
          </button>
          <button
            type="button"
            class="approval-btn"
            (click)="signOut()">
            Sign out
          </button>
        </div>

        <p *ngIf="lastChecked" class="approval-stamp">
          Last checked: {{ lastChecked | date: 'medium' }}
        </p>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        min-height: 100vh;
        background:
          radial-gradient(
            1200px 600px at 50% -10%,
            rgba(0, 242, 255, 0.18),
            transparent 60%
          ),
          radial-gradient(
            900px 500px at 50% 120%,
            rgba(0, 255, 136, 0.12),
            transparent 60%
          ),
          #060a14;
        color: #d7e2ee;
        font-family: 'Inter', system-ui, sans-serif;
      }

      .approval-shell {
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 2rem;
      }

      .approval-card {
        position: relative;
        max-width: 520px;
        width: 100%;
        padding: 2.25rem 2rem;
        background: rgba(8, 14, 26, 0.78);
        border: 1px solid rgba(0, 242, 255, 0.25);
        border-radius: 16px;
        box-shadow:
          0 0 0 1px rgba(0, 242, 255, 0.05) inset,
          0 12px 60px rgba(0, 0, 0, 0.55);
        backdrop-filter: blur(14px);
      }

      .approval-glow {
        position: absolute;
        inset: -1px;
        border-radius: 16px;
        background: linear-gradient(
          135deg,
          rgba(0, 242, 255, 0.35),
          transparent 35%,
          rgba(0, 255, 136, 0.35) 100%
        );
        z-index: -1;
        filter: blur(18px);
        opacity: 0.55;
      }

      .approval-title {
        margin: 0 0 1rem 0;
        font-size: 1.6rem;
        font-weight: 700;
        letter-spacing: 0.01em;
        background: linear-gradient(90deg, #fff, #00f2ff);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
      }

      .approval-body {
        line-height: 1.55;
        color: #b8c3d2;
      }

      .approval-email {
        margin-top: 1rem;
        font-size: 0.9rem;
        color: #93a1b3;
      }

      .approval-actions {
        display: flex;
        gap: 0.75rem;
        margin-top: 1.5rem;
      }

      .approval-btn {
        flex: 1;
        padding: 0.65rem 1rem;
        border-radius: 10px;
        border: 1px solid rgba(255, 255, 255, 0.12);
        background: rgba(255, 255, 255, 0.04);
        color: #d7e2ee;
        font: inherit;
        font-weight: 600;
        cursor: pointer;
        transition:
          transform 0.15s ease,
          border-color 0.15s ease,
          background 0.15s ease;
      }

      .approval-btn:hover {
        border-color: rgba(0, 242, 255, 0.5);
        background: rgba(0, 242, 255, 0.05);
        transform: translateY(-1px);
      }

      .approval-btn--primary {
        border-color: rgba(0, 255, 136, 0.55);
        background: rgba(0, 255, 136, 0.12);
        color: #d6ffe9;
      }

      .approval-btn--primary:hover {
        border-color: rgba(0, 255, 136, 0.9);
        background: rgba(0, 255, 136, 0.2);
      }

      .approval-btn:disabled {
        opacity: 0.55;
        cursor: progress;
        transform: none;
      }

      .approval-stamp {
        margin-top: 1rem;
        font-size: 0.75rem;
        color: #6f7c91;
      }
    `,
  ],
})
export class PendingApprovalPageComponent implements OnInit {
  rejected = false;
  email: string | null = null;
  loading = false;
  lastChecked: Date | null = null;

  constructor(
    private identity: IdentityService,
    private auth: AuthService,
    private router: Router,
    private cdr: ChangeDetectorRef,
  ) {}

  async ngOnInit(): Promise<void> {
    await this.refresh();
  }

  async refresh(): Promise<void> {
    this.loading = true;
    this.cdr.markForCheck();
    const snap = await this.identity.fetchMe();
    this.loading = false;
    this.lastChecked = new Date();

    // A 403 account_pending response yields snap.available=false
    // (any non-2xx), but for that case the user still belongs on
    // this page — keep whatever flag we already inferred from the
    // guard. Only act on a successful payload.
    if (snap.available && snap.data?.user) {
      const status = snap.data.user.status || '';
      this.email = snap.data.user.email || this.email;
      if (status === 'approved') {
        // Operator just approved us. Drop the holding screen.
        this.router.navigateByUrl('/');
        return;
      }
      this.rejected = status === 'rejected';
    }
    this.cdr.markForCheck();
  }

  signOut(): void {
    this.auth.signOut('/');
  }
}
