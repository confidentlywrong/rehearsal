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
 * ProfilePageComponent — Phase 10.
 *
 * Reads /api/v1/me to render the signed-in user's email, display
 * name, and timezone; PATCHes /api/v1/me to save edits. Email is
 * read-only (it comes from the Firebase token, not the DB).
 *
 * "Never invent data" rule:
 *   - Missing display_name → empty input, never a placeholder string
 *     written to the DB on save.
 *   - Missing timezone → blank dropdown, never auto-filled.
 *   - When the gateway is unreachable, render an error banner and
 *     disable the save button rather than pretend the save worked.
 */

import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import { AuthService } from '../../auth/auth.service';
import { IdentityService, MeResponse } from '../../auth/identity.service';
import { ProfileService, ProfileUpdate } from './profile.service';

type SaveState = 'idle' | 'saving' | 'saved' | 'error';
type AsyncActionState = 'idle' | 'pending' | 'done' | 'error';

@Component({
  selector: 'app-profile-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule],
  template: `
    <section class="profile-page">
      <header class="profile-page__header">
        <h2>Profile</h2>
        <p class="profile-page__lead">
          Manage how your account appears across the app.
        </p>
      </header>

      <div class="profile-page__loading" *ngIf="loading">Loading…</div>

      <div *ngIf="!loading && unavailable" class="profile-page__banner" role="alert">
        Profile is unavailable right now. Try again in a moment.
      </div>

      <form
        *ngIf="!loading && me as data"
        class="profile-page__form"
        (ngSubmit)="save()"
        autocomplete="off"
      >
        <label class="profile-page__field">
          <span class="profile-page__label">Email</span>
          <input
            class="profile-page__input"
            type="email"
            [value]="data.user?.email || ''"
            disabled
            aria-readonly="true"
          />
          <span class="profile-page__hint">
            Sourced from your identity provider — change there to update here.
          </span>
        </label>

        <label class="profile-page__field">
          <span class="profile-page__label">Display name</span>
          <input
            class="profile-page__input"
            type="text"
            name="displayName"
            [(ngModel)]="displayName"
            maxlength="80"
            placeholder="How others see you"
          />
        </label>

        <label class="profile-page__field">
          <span class="profile-page__label">Timezone</span>
          <input
            class="profile-page__input"
            type="text"
            name="timezone"
            [(ngModel)]="timezone"
            list="profile-page-tz-list"
            maxlength="64"
            placeholder="e.g. Europe/Berlin"
          />
          <datalist id="profile-page-tz-list">
            <option value="UTC"></option>
            <option value="America/Los_Angeles"></option>
            <option value="America/New_York"></option>
            <option value="Europe/London"></option>
            <option value="Europe/Berlin"></option>
            <option value="Asia/Tokyo"></option>
            <option value="Australia/Sydney"></option>
          </datalist>
          <span class="profile-page__hint">
            IANA timezone name. Used to format timestamps in reports.
          </span>
        </label>

        <div class="profile-page__actions">
          <button
            type="submit"
            class="profile-page__btn"
            [disabled]="saveState === 'saving'"
          >
            <ng-container [ngSwitch]="saveState">
              <span *ngSwitchCase="'saving'">Saving…</span>
              <span *ngSwitchCase="'saved'">Saved ✓</span>
              <span *ngSwitchDefault>Save changes</span>
            </ng-container>
          </button>
          <span
            *ngIf="saveState === 'error'"
            class="profile-page__error"
            role="alert"
          >
            {{ errorMsg || 'Could not save profile.' }}
          </span>
        </div>
      </form>

      <!-- Phase 14: Account actions. Logically distinct from the
           profile form above — these are session/identity controls
           rather than profile data. -->
      <section
        *ngIf="!loading && me as data"
        class="profile-page__account-actions"
        aria-label="Account actions"
      >
        <h3 class="profile-page__section-title">Account</h3>

        <div class="profile-page__action-row">
          <div class="profile-page__action-text">
            <div class="profile-page__action-name">Sign out</div>
            <div class="profile-page__action-hint">
              End this session on this device. Your data is unchanged.
            </div>
          </div>
          <button
            type="button"
            class="profile-page__btn profile-page__btn--secondary"
            (click)="signOut()"
          >
            Sign out
          </button>
        </div>

        <div class="profile-page__action-row">
          <div class="profile-page__action-text">
            <div class="profile-page__action-name">Change password</div>
            <div class="profile-page__action-hint">
              We'll email a password-reset link to
              <strong>{{ data.user?.email || '(no email on file)' }}</strong>.
              Follow the link to set a new password.
            </div>
          </div>
          <button
            type="button"
            class="profile-page__btn profile-page__btn--secondary"
            (click)="sendPasswordReset()"
            [disabled]="resetState === 'pending' || !data.user?.email"
          >
            <ng-container [ngSwitch]="resetState">
              <span *ngSwitchCase="'pending'">Sending…</span>
              <span *ngSwitchCase="'done'">Email sent ✓</span>
              <span *ngSwitchDefault>Send reset email</span>
            </ng-container>
          </button>
        </div>
        <div
          *ngIf="resetState === 'error'"
          class="profile-page__error"
          role="alert"
        >
          {{ resetError || 'Could not send the reset email.' }}
        </div>

        <!-- Danger zone: visually separated, opens a confirm step
             before the destructive POST. We do NOT use window.confirm
             because the cyan/neon styling is meaningful here — users
             need to register that this is irreversible. -->
        <div class="profile-page__danger">
          <div class="profile-page__danger-row">
            <div class="profile-page__action-text">
              <div class="profile-page__action-name profile-page__action-name--danger">
                Delete account
              </div>
              <div class="profile-page__action-hint">
                Removes your access to every workspace immediately.
                Your audit history is preserved for compliance;
                personal data is purged after a 30-day grace
                window.
              </div>
            </div>
            <button
              *ngIf="!confirmingDelete"
              type="button"
              class="profile-page__btn profile-page__btn--danger"
              (click)="confirmingDelete = true"
              [disabled]="deleteState === 'pending'"
            >
              Delete my account…
            </button>
          </div>

          <div *ngIf="confirmingDelete" class="profile-page__confirm">
            <p class="profile-page__confirm-text">
              Type <strong>delete</strong> to confirm. This cannot be undone.
            </p>
            <div class="profile-page__confirm-row">
              <input
                class="profile-page__input"
                type="text"
                name="confirmDeleteText"
                [(ngModel)]="confirmDeleteText"
                placeholder="delete"
                autocomplete="off"
                aria-label="Type delete to confirm"
              />
              <button
                type="button"
                class="profile-page__btn profile-page__btn--danger"
                (click)="deleteAccount()"
                [disabled]="
                  deleteState === 'pending' ||
                  confirmDeleteText.trim().toLowerCase() !== 'delete'
                "
              >
                <ng-container [ngSwitch]="deleteState">
                  <span *ngSwitchCase="'pending'">Deleting…</span>
                  <span *ngSwitchDefault>Confirm delete</span>
                </ng-container>
              </button>
              <button
                type="button"
                class="profile-page__btn profile-page__btn--secondary"
                (click)="cancelDelete()"
                [disabled]="deleteState === 'pending'"
              >
                Cancel
              </button>
            </div>
            <div
              *ngIf="deleteState === 'error'"
              class="profile-page__error"
              role="alert"
            >
              {{ deleteError || 'Could not delete the account.' }}
            </div>
          </div>
        </div>
      </section>
    </section>
  `,
  styles: [
    `
      .profile-page__header h2 {
        margin: 0 0 4px 0;
        font-size: 18px;
        color: #00ffcc;
      }
      .profile-page__lead {
        color: #8aa1b3;
        font-size: 13px;
        margin: 0 0 20px 0;
      }
      .profile-page__banner {
        padding: 12px 14px;
        border: 1px solid rgba(255, 90, 90, 0.4);
        background: rgba(255, 90, 90, 0.08);
        border-radius: 6px;
        color: #ffb0b0;
        font-size: 13px;
      }
      .profile-page__form {
        display: flex;
        flex-direction: column;
        gap: 18px;
        max-width: 520px;
      }
      .profile-page__field {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .profile-page__label {
        font-size: 12px;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: #8aa1b3;
      }
      .profile-page__input {
        padding: 9px 12px;
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(0, 255, 200, 0.18);
        border-radius: 6px;
        color: #e8f8ff;
        font-size: 13px;
        font-family: inherit;
      }
      .profile-page__input:focus {
        outline: none;
        border-color: rgba(0, 255, 200, 0.6);
      }
      .profile-page__input[disabled] {
        opacity: 0.6;
      }
      .profile-page__hint {
        font-size: 11px;
        color: #6f8497;
      }
      .profile-page__actions {
        display: flex;
        align-items: center;
        gap: 14px;
      }
      .profile-page__btn {
        padding: 8px 18px;
        background: rgba(0, 255, 200, 0.16);
        color: #00ffcc;
        border: 1px solid rgba(0, 255, 200, 0.5);
        border-radius: 6px;
        font-size: 13px;
        cursor: pointer;
      }
      .profile-page__btn:hover:not([disabled]) {
        background: rgba(0, 255, 200, 0.26);
      }
      .profile-page__btn[disabled] {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .profile-page__error {
        color: #ffb0b0;
        font-size: 12px;
      }

      /* --- Phase 14: account actions section ------------------------ */
      .profile-page__account-actions {
        max-width: 520px;
        margin-top: 36px;
        padding-top: 20px;
        border-top: 1px solid rgba(0, 255, 200, 0.18);
        display: flex;
        flex-direction: column;
        gap: 16px;
      }
      .profile-page__section-title {
        margin: 0 0 4px 0;
        font-size: 14px;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: #00ffcc;
      }
      .profile-page__action-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 12px 14px;
        border: 1px solid rgba(0, 255, 200, 0.12);
        border-radius: 6px;
        background: rgba(255, 255, 255, 0.02);
      }
      .profile-page__action-text {
        flex: 1;
        min-width: 0;
      }
      .profile-page__action-name {
        font-size: 13px;
        color: #e8f8ff;
        margin-bottom: 2px;
      }
      .profile-page__action-name--danger {
        color: #ffb0b0;
      }
      .profile-page__action-hint {
        font-size: 11px;
        color: #6f8497;
        line-height: 1.4;
      }
      .profile-page__btn--secondary {
        background: rgba(255, 255, 255, 0.04);
        color: #cfe8ff;
        border-color: rgba(0, 255, 200, 0.25);
      }
      .profile-page__btn--secondary:hover:not([disabled]) {
        background: rgba(255, 255, 255, 0.08);
      }
      .profile-page__btn--danger {
        background: rgba(255, 90, 90, 0.12);
        color: #ffb0b0;
        border-color: rgba(255, 90, 90, 0.5);
      }
      .profile-page__btn--danger:hover:not([disabled]) {
        background: rgba(255, 90, 90, 0.22);
      }
      .profile-page__danger {
        margin-top: 8px;
        padding: 14px;
        border: 1px solid rgba(255, 90, 90, 0.35);
        background: rgba(255, 90, 90, 0.04);
        border-radius: 6px;
      }
      .profile-page__danger-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
      }
      .profile-page__confirm {
        margin-top: 14px;
        padding-top: 12px;
        border-top: 1px dashed rgba(255, 90, 90, 0.35);
      }
      .profile-page__confirm-text {
        margin: 0 0 10px 0;
        font-size: 12px;
        color: #ffb0b0;
      }
      .profile-page__confirm-row {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .profile-page__confirm-row .profile-page__input {
        flex: 1;
        min-width: 0;
      }
    `,
  ],
})
export class ProfilePageComponent implements OnInit, OnDestroy {
  loading = true;
  unavailable = false;
  me: MeResponse | null = null;
  displayName = '';
  timezone = '';
  saveState: SaveState = 'idle';
  errorMsg = '';

  // Phase 14 — password reset
  resetState: AsyncActionState = 'idle';
  resetError = '';

  // Phase 14 — danger-zone delete
  confirmingDelete = false;
  confirmDeleteText = '';
  deleteState: AsyncActionState = 'idle';
  deleteError = '';

  private saveTimeoutId?: number;
  private resetTimeoutId?: number;

  constructor(
    private identity: IdentityService,
    private profile: ProfileService,
    private auth: AuthService,
    private cdr: ChangeDetectorRef,
  ) {}

  ngOnDestroy(): void {
    if (this.saveTimeoutId) window.clearTimeout(this.saveTimeoutId);
    if (this.resetTimeoutId) window.clearTimeout(this.resetTimeoutId);
  }

  async ngOnInit(): Promise<void> {
    const snap = await this.identity.fetchMe();
    this.loading = false;
    if (!snap.available || !snap.data) {
      this.unavailable = true;
      this.cdr.markForCheck();
      return;
    }
    this.me = snap.data;
    // Hydrate display_name + timezone from /me/profile. /me itself
    // already carries display_name; we refetch so the form starts
    // from the canonical store value rather than a possibly stale
    // cached value on /me.
    const extra = await this.profile.fetchProfile();
    if (extra.available && extra.data) {
      this.displayName = extra.data.display_name || '';
      this.timezone = extra.data.timezone || '';
    }
    this.cdr.markForCheck();
  }

  async save(): Promise<void> {
    if (!this.me) return;
    this.saveState = 'saving';
    this.errorMsg = '';
    this.cdr.markForCheck();
    const patch: ProfileUpdate = {
      display_name: this.displayName.trim(),
      timezone: this.timezone.trim(),
    };
    const res = await this.profile.updateProfile(patch);
    if (res.ok) {
      this.saveState = 'saved';
      this.cdr.markForCheck();
      // Clear the "saved" indicator after a short delay so the
      // button reverts to its idle label. Pure UX polish — no
      // server state mutation here.
      this.saveTimeoutId = window.setTimeout(() => {
        this.saveState = 'idle';
        this.cdr.markForCheck();
      }, 2200);
    } else {
      this.saveState = 'error';
      this.errorMsg = res.error || '';
      this.cdr.markForCheck();
    }
  }

  /**
   * Phase 14: sign the user out and return them to /.
   *
   * AuthService.signOut() posts to /api/v1/me/logout BEFORE
   * clearing the local token, so the audit row gets written.
   * It then redirects via window.location, so nothing else
   * to do here.
   */
  signOut(): void {
    this.auth.signOut('/');
  }

  async sendPasswordReset(): Promise<void> {
    const email = this.me?.user?.email || '';
    if (!email) {
      this.resetState = 'error';
      this.resetError = 'No email on file to send a reset link to.';
      this.cdr.markForCheck();
      return;
    }
    this.resetState = 'pending';
    this.resetError = '';
    this.cdr.markForCheck();
    const res = await this.profile.requestPasswordReset(email);
    if (res.ok) {
      this.resetState = 'done';
      this.cdr.markForCheck();
      this.resetTimeoutId = window.setTimeout(() => {
        this.resetState = 'idle';
        this.cdr.markForCheck();
      }, 4000);
    } else {
      this.resetState = 'error';
      this.resetError = res.error || '';
      this.cdr.markForCheck();
    }
  }

  cancelDelete(): void {
    this.confirmingDelete = false;
    this.confirmDeleteText = '';
    this.deleteState = 'idle';
    this.deleteError = '';
  }

  async deleteAccount(): Promise<void> {
    if (this.confirmDeleteText.trim().toLowerCase() !== 'delete') return;
    this.deleteState = 'pending';
    this.deleteError = '';
    this.cdr.markForCheck();
    const res = await this.profile.deleteAccount();
    if (res.ok) {
      // The gateway already cleared the active-org cookie. Now
      // we tear down the local session and return to /. The user
      // will never see the post-delete page render because
      // signOut() does window.location.assign.
      this.auth.signOut('/');
    } else {
      this.deleteState = 'error';
      this.deleteError = res.error || '';
      this.cdr.markForCheck();
    }
  }
}
