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
 * ProfileService.
 *
 * Thin fetch wrapper around the gateway's profile endpoints:
 *   - GET   /api/v1/me/profile  → ProfileSnapshot
 *   - PATCH /api/v1/me/profile  → SaveResult
 *
 * Returns `available: false` when the gateway responds non-200 so
 * the profile page can render a benign banner instead of throwing.
 */

import { Injectable } from '@angular/core';

export interface UserProfile {
  display_name?: string;
  timezone?: string;
}

export interface ProfileSnapshot {
  available: boolean;
  data?: UserProfile;
  status: number;
}

export interface ProfileUpdate {
  display_name?: string;
  timezone?: string;
}

export interface SaveResult {
  ok: boolean;
  status: number;
  error?: string;
}

@Injectable({ providedIn: 'root' })
export class ProfileService {
  async fetchProfile(): Promise<ProfileSnapshot> {
    try {
      const resp = await fetch('/api/v1/me/profile', {
        method: 'GET',
        // B-15 (2026-06-22 audit): same-origin matches the rest of the
        // SPA. /api/v1/me/profile is always served from the same
        // origin; 'include' would let the __session cookie ride on
        // hypothetical cross-origin embeds, which is not desired.
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!resp.ok) {
        return { available: false, status: resp.status };
      }
      const data = (await resp.json()) as UserProfile;
      return { available: true, status: resp.status, data };
    } catch {
      return { available: false, status: 0 };
    }
  }

  async updateProfile(patch: ProfileUpdate): Promise<SaveResult> {
    try {
      const resp = await fetch('/api/v1/me/profile', {
        method: 'PATCH',
        // B-15: see fetchProfile() above.
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body: JSON.stringify(patch),
      });
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try {
          const body = (await resp.json()) as { error?: string; hint?: string };
          msg = body.error || body.hint || msg;
        } catch {
          // body wasn't JSON — keep the generic message.
        }
        return { ok: false, status: resp.status, error: msg };
      }
      return { ok: true, status: resp.status };
    } catch {
      return { ok: false, status: 0, error: 'Network error.' };
    }
  }

  /**
   * Phase 14: request a Firebase password-reset email.
   *
   * Hits Firebase Identity Toolkit REST directly with the API key
   * already exposed via window.ENV['FIREBASE_API_KEY']. We do NOT
   * add the firebase JS SDK to the SPA just for this call —
   * sendOobCode is a single POST with a documented contract.
   *
   * The endpoint is intentionally vague about whether the email
   * existed (returns 200 either way), so we don't leak account
   * presence to the caller — perfect for our use case where the
   * email is already on the signed-in user's record.
   *
   * Returns ok=false with a message when the API key is missing
   * (SAAS_MODE=off or local-dev without Firebase wired up).
   */
  async requestPasswordReset(email: string): Promise<SaveResult> {
    const apiKey =
      (typeof window !== 'undefined' && window.ENV && window.ENV['FIREBASE_API_KEY']) || '';
    if (!apiKey) {
      return {
        ok: false,
        status: 0,
        error: 'Password reset is unavailable: Firebase is not configured.',
      };
    }
    if (!email) {
      return { ok: false, status: 0, error: 'No email on file to send a reset link to.' };
    }
    try {
      const url = `https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key=${encodeURIComponent(apiKey)}`;
      const resp = await fetch(url, {
        method: 'POST',
        // No credentials: this is a cross-origin call to Google.
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requestType: 'PASSWORD_RESET', email }),
      });
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try {
          // Identity Toolkit error shape: { error: { message: "..." } }
          const body = (await resp.json()) as { error?: { message?: string } };
          msg = body.error?.message || msg;
        } catch {
          // ignore
        }
        return { ok: false, status: resp.status, error: msg };
      }
      return { ok: true, status: resp.status };
    } catch {
      return { ok: false, status: 0, error: 'Network error.' };
    }
  }

  /**
   * Phase 14: soft-delete the signed-in user via the gateway.
   *
   * The gateway response is 204 on success (including the
   * already-deleted idempotent path). The caller is responsible
   * for triggering the local logout flow after this resolves —
   * the gateway already cleared the active-org cookie but the
   * local token + Firebase session live in the SPA.
   */
  async deleteAccount(): Promise<SaveResult> {
    try {
      const resp = await fetch('/api/v1/me', {
        method: 'DELETE',
        // B-15: see fetchProfile() above.
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try {
          const body = (await resp.json()) as { error?: string; hint?: string };
          msg = body.error || body.hint || msg;
        } catch {
          // ignore
        }
        return { ok: false, status: resp.status, error: msg };
      }
      return { ok: true, status: resp.status };
    } catch {
      return { ok: false, status: 0, error: 'Network error.' };
    }
  }
}
