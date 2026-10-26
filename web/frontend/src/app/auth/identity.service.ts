/**
 * Copyright 2026 Google LLC
 * Licensed under the Apache License, Version 2.0.
 */

/**
 * IdentityService — per-user (orgs/memberships removed).
 *
 * Thin fetch wrapper around the gateway's /api/v1/me endpoints. The
 * AuthService's APP_INITIALIZER has already patched window.fetch to
 * attach `Authorization: Bearer` headers on same-origin requests, so
 * this service does not need to know anything about authentication.
 *
 * On first sign-in the gateway responds with `needs_init=true` and no
 * `user` row yet. ensureInitialized() will then POST /api/v1/me/init
 * to create the row idempotently and re-fetch /me.
 */

import { Injectable } from '@angular/core';

export interface UserDTO {
  id: string;
  email: string;
  display_name: string;
  timezone: string;
  plan_tier: string;
  is_superadmin: boolean;
  /**
   * Manual-approval lifecycle state — one of "pending",
   * "approved", "rejected". Empty when the gateway has not
   * yet provisioned the row (paired with `needs_init: true`).
   * The approvalGuard routes any value other than "approved"
   * to /pending-approval so the user lands on a friendly
   * screen instead of seeing the gateway's 403s in the
   * browser console.
   */
  status?: string;
}

export interface MeResponse {
  user?: UserDTO;
  needs_init: boolean;
}

export interface MeSnapshot {
  /** True when the gateway returned a 200. False on 401/404/503/etc. */
  available: boolean;
  /** Populated when available is true. */
  data?: MeResponse;
  /** HTTP status from the most recent fetch, for debug display. */
  status: number;
}

@Injectable({ providedIn: 'root' })
export class IdentityService {
  /**
   * GET /api/v1/me. Returns `available: false` (rather than throwing)
   * when the gateway responds non-200 — keeps the calling component's
   * render path simple.
   */
  async fetchMe(): Promise<MeSnapshot> {
    try {
      // B-15 (2026-06-22 audit): use 'same-origin' (not 'include'),
      // matching the rest of the SPA. /api/v1/me is always same-
      // origin in this deployment; 'include' would additionally
      // allow the __session cookie to ride along on a hypothetical
      // cross-origin embed, which is not a posture we want.
      const resp = await fetch('/api/v1/me', {
        method: 'GET',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!resp.ok) {
        return { available: false, status: resp.status };
      }
      const data = (await resp.json()) as MeResponse;
      return { available: true, status: resp.status, data };
    } catch {
      return { available: false, status: 0 };
    }
  }

  /**
   * Idempotent first-sign-in handshake.
   *
   * Flow: fetchMe(). If the gateway reports `needs_init=true` (no
   * users row exists yet for this Firebase UID), POST an empty body
   * to /api/v1/me/init. The gateway will create the row using the
   * email + display_name claims, then we re-fetch /me so the caller
   * sees the populated user object on the same call.
   *
   * Why no form: every value the init endpoint needs (email,
   * display_name) is already in the Firebase ID token claims. The
   * user can rename themselves later from the settings page.
   *
   * Errors are swallowed (logged to console only). The worst case
   * for a transient init failure is the SPA showing a "sign-in
   * incomplete" banner until the next page load — no data loss.
   */
  async ensureInitialized(): Promise<MeSnapshot> {
    const first = await this.fetchMe();
    if (!first.available || !first.data) {
      return first;
    }
    if (!first.data.needs_init) {
      return first;
    }
    try {
      const resp = await fetch('/api/v1/me/init', {
        method: 'POST',
        // B-15: see fetchMe() above. Same reasoning.
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
        },
        // Empty body: gateway derives display_name from the
        // Firebase claim and falls back to a synthesized email
        // when the provider doesn't expose one (e.g. Apple).
        body: '{}',
      });
      if (!resp.ok) {
        // eslint-disable-next-line no-console
        console.warn(
          '[IdentityService] /me/init returned',
          resp.status,
          '— SPA will retry on next page load',
        );
        return first;
      }
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('[IdentityService] /me/init request failed:', err);
      return first;
    }
    // Re-fetch so the caller sees the user row created above.
    return this.fetchMe();
  }

  /**
   * Server-side logout. Tears down any auth cookies and records an
   * audit row. Caller is still responsible for clearing the local
   * Firebase ID-token cache (firebase.auth().signOut()).
   */
  async logout(): Promise<boolean> {
    try {
      const resp = await fetch('/api/v1/me/logout', {
        method: 'POST',
        // B-15: see fetchMe() above. Same reasoning.
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      return resp.ok;
    } catch {
      return false;
    }
  }
}
