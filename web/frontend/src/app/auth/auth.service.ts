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

import { APP_INITIALIZER, Injectable, Provider } from '@angular/core';

/**
 * AuthService — per-user auth with in-SPA Firebase ID-token refresh.
 *
 * Auth is carried exclusively via the HttpOnly `__session` cookie,
 * set by the BFF endpoint `POST /api/v1/auth/exchange`. The SPA
 * never stores the ID token in JavaScript-accessible storage
 * (localStorage, sessionStorage, or a non-HttpOnly cookie), so an
 * XSS cannot steal the credential (security audit Finding #5,
 * 2026-07-02).
 *
 * The fetch interceptor hooks `window.fetch` so every same-origin
 * request carries the `Accept` and `X-Firebase-AppCheck` headers.
 * The browser sends the HttpOnly cookie automatically — no
 * `Authorization: Bearer` header is set by the SPA.
 *
 * ─────────────────────────────────────────────────────────────
 * P2 #13 (token refresh) + P2 #6 (App Check coverage gap) — IMPLEMENTED
 * ─────────────────────────────────────────────────────────────
 * Source: 2026-06-21 multi-user audit, P2 #6 + P2 #13.
 *
 * Background: the static `/login/index.html` page loads the Firebase
 * SDK from a CDN and POSTs the freshly-minted ID token to
 * `/api/v1/auth/exchange`, which echoes it as an HttpOnly+Secure
 * `__session` cookie. The SPA never sees the raw token value.
 *
 * This file dynamically imports `firebase/app`, `firebase/auth`,
 * and `firebase/app-check` (split into separate chunks, loaded
 * only when Firebase mode is on). `initFirebaseRefresh()` runs at
 * bootstrap. When `window.ENV` contains `FIREBASE_API_KEY` and
 * `FIREBASE_PROJECT_ID`:
 *
 *   1. `initializeApp(firebaseConfig)` — same config the /login page reads.
 *   2. If `RECAPTCHA_ENTERPRISE_SITE_KEY` is set AND no emulator host:
 *      `initializeAppCheck(app, { ... })`.  Same site key as /login.
 *   3. `getAuth(app)` + optional `connectAuthEmulator(...)`.
 *   4. `onIdTokenChanged(auth, cb)` — fires whenever Firebase refreshes
 *      the token (auto-refresh happens ~5 min before expiry). Callback
 *      POSTs the fresh token to `/api/v1/auth/exchange` so the gateway
 *      can re-stamp the HttpOnly cookie.
 *
 * Local-dev path (`IDENTITY_PROVIDER=local`, no `FIREBASE_API_KEY` in
 * window.ENV) is byte-identical to before: the early return skips all
 * Firebase calls, no chunks are loaded, no App Check init runs.
 *
 * DO NOT delete this comment when reading. It documents the
 * "why this lives here" so a future refactor that splits the
 * Firebase SDK back out into a separate module knows the
 * App Check init MUST stay paired with the auth init (otherwise
 * post-login traffic becomes unattested again).
 */

const SESSION_COOKIE = '__session';
const FETCH_PATCHED_FLAG = '__rcFetchPatched';

declare global {
  interface Window {
    ENV?: Record<string, string>;
    [FETCH_PATCHED_FLAG]?: boolean;
  }
}

@Injectable({ providedIn: 'root' })
export class AuthService {
  // Set by initFirebaseRefresh() once the Firebase SDK has loaded and
  // an Auth instance exists. Used by signOut() to also revoke the
  // Firebase session so the browser does not silently re-mint tokens
  // for a user we just logged out.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private firebaseAuth: any | null = null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private firebaseAuthMod: any | null = null;
  // App Check handles, captured from initializeAppCheck() so the
  // fetch interceptor can attach X-Firebase-AppCheck to /api/*
  // requests (P2.B, 2026-06-22 audit). Null when App Check is
  // disabled (e.g. emulator runs, missing site key).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private appCheck: any | null = null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private appCheckMod: any | null = null;

  /**
   * Returns null. The ID token is stored exclusively in the HttpOnly
   * `__session` cookie which JavaScript cannot read. Auth is carried
   * by the browser automatically on same-origin requests.
   *
   * Retained for API compatibility — callers that check truthiness
   * before acting (e.g. signOut's fire-and-forget POST) gracefully
   * degrade when this returns null.
   */
  getToken(): string | null {
    return null;
  }

  /**
   * Returns a Firebase App Check JWT, or null if App Check isn't
   * initialised (emulator runs, missing site key, init failed). The
   * fetch interceptor calls this and attaches the result as
   * X-Firebase-AppCheck on same-origin /api/* requests; the gateway
   * verifies it server-side when APPCHECK_ENFORCE is set
   * (report-only or enforce). Errors are swallowed because App
   * Check failure must NEVER block product traffic — the gateway's
   * report-only mode is the safe rollout step that flags issues
   * without breaking users.
   */
  async getAppCheckToken(): Promise<string | null> {
    if (!this.appCheck || !this.appCheckMod) {
      return null;
    }
    try {
      const result = await this.appCheckMod.getToken(this.appCheck, false);
      return result?.token ?? null;
    } catch {
      return null;
    }
  }

  /**
   * Clears the session and bounces the user back to /login.
   *
   * Fire-and-forget POST so the gateway can emit a `user.logged_out`
   * audit row and clear the HttpOnly cookie via Set-Cookie header.
   * keepalive lets the POST survive the window.location.assign().
   */
  signOut(returnTo: string = '/'): void {
    if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
      try {
        void window
          .fetch('/api/v1/me/logout', {
            method: 'POST',
            credentials: 'same-origin',
            keepalive: true,
          })
          .catch(() => undefined);
      } catch {
        // ignore
      }
    }
    // P2 #13: Firebase signOut so the SDK's onIdTokenChanged
    // observer fires (user becomes null). This prevents a stale
    // Firebase session from re-minting tokens in another tab.
    if (this.firebaseAuth && this.firebaseAuthMod) {
      try {
        void this.firebaseAuthMod
          .signOut(this.firebaseAuth)
          .catch(() => undefined);
      } catch {
        // ignore
      }
    }
    // Best-effort client-side cookie clear.
    if (typeof document !== 'undefined') {
      document.cookie = `${SESSION_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax`;
    }
    const next = `/login?redirect=${encodeURIComponent(returnTo)}`;
    if (typeof window !== 'undefined' && window.location) {
      window.location.assign(next);
    }
  }

  /**
   * Bootstraps Firebase in the SPA when `window.ENV` carries a
   * Firebase config. Wires `onIdTokenChanged` to mirror the auto-
   * refreshed ID token into `localStorage.rc_id_token`, and binds
   * App Check (reCAPTCHA Enterprise) BEFORE `getAuth` so the very
   * first auth call carries the App Check token. Local-dev path
   * (no Firebase config) returns immediately — no SDK chunks load.
   *
   * Idempotent: bails out early when called twice. Fire-and-forget
   * from `provideAuth()` — bootstrap must not block on the Firebase
   * SDK download.
   */
  async initFirebaseRefresh(): Promise<void> {
    if (typeof window === 'undefined') {
      return;
    }
    if (this.firebaseAuth) {
      return; // already initialised
    }
    const env = window.ENV || {};
    const apiKey = env['FIREBASE_API_KEY'];
    const projectId = env['FIREBASE_PROJECT_ID'];
    if (!apiKey || !projectId) {
      // Local-dev / IDENTITY_PROVIDER=local: NO Firebase wiring at
      // all. The /login page handles auth (or LocalResolver bypass);
      // the SPA never refreshes a token because there is no token
      // to refresh. Behaviour byte-identical to pre-P2-#13.
      return;
    }
    try {
      const [appMod, authMod] = await Promise.all([
        import('firebase/app'),
        import('firebase/auth'),
      ]);
      const app = appMod.initializeApp({
        apiKey,
        authDomain: env['FIREBASE_AUTH_DOMAIN'] || `${projectId}.firebaseapp.com`,
        projectId,
      });

      // App Check FIRST. Pairing with /login's identical block — see
      // web/frontend/public/login/index.html around line 388. Same
      // site key, same emulator skip, same swallow-and-continue on
      // failure. Doing it before getAuth() so the auth SDK's first
      // network call already carries the App Check header.
      const siteKey = env['RECAPTCHA_ENTERPRISE_SITE_KEY'];
      const emulatorHost = env['FIREBASE_EMULATOR_HOST'];
      if (siteKey && !emulatorHost) {
        try {
          const appCheckMod = await import('firebase/app-check');
          const appCheck = appCheckMod.initializeAppCheck(app, {
            provider: new appCheckMod.ReCaptchaEnterpriseProvider(siteKey),
            isTokenAutoRefreshEnabled: true,
          });
          // Stash for the fetch interceptor. Without this the SPA
          // initialises App Check but never attaches the token, so
          // the gateway's APPCHECK_ENFORCE=report-only logs every
          // /api/* call as "missing header" (P2.B audit).
          this.appCheck = appCheck;
          this.appCheckMod = appCheckMod;
        } catch (e) {
          // Match /login's behaviour: warn and continue rather than
          // breaking auth entirely.
          // eslint-disable-next-line no-console
          console.warn('App Check init failed; continuing without attestation:', e);
        }
      }

      const auth = authMod.getAuth(app);
      if (emulatorHost) {
        authMod.connectAuthEmulator(auth, `http://${emulatorHost}`, {
          disableWarnings: true,
        });
      }

      this.firebaseAuth = auth;
      this.firebaseAuthMod = authMod;

      // P2 #13 + Finding #5 hardening: Firebase auto-refreshes ID
      // tokens ~5min before expiry; `onIdTokenChanged` fires every
      // time, including the initial restore from IndexedDB persistence.
      // We POST the fresh token to /api/v1/auth/exchange so the
      // gateway re-stamps the HttpOnly __session cookie. The SPA
      // never stores the token in JS-accessible storage.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      authMod.onIdTokenChanged(auth, async (user: any) => {
        if (!user) {
          // User signed out — cookie will expire naturally or be
          // cleared by the /api/v1/me/logout POST in signOut().
          return;
        }
        try {
          const token = await user.getIdToken(false);
          // Refresh the HttpOnly cookie via the exchange endpoint.
          // The Authorization header is set explicitly here — the
          // fetch interceptor no longer attaches it automatically.
          await fetch('/api/v1/auth/exchange', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            credentials: 'same-origin',
          });
          // P1.B: a successful token mint means the user is signed
          // in again. Clear the bounce flag so a future 401/410 can
          // fire a fresh signOut instead of being suppressed.
          try {
            window.sessionStorage.removeItem(AUTH_BOUNCE_FLAG);
          } catch {
            // ignore
          }
        } catch {
          // Token refresh or exchange failed — the existing cookie
          // will eventually expire and the user will be bounced to
          // /login on the next 401.
        }
      });
    } catch (e) {
      // Firebase SDK failed to load (offline, CSP, bundling regression).
      // Don't block bootstrap — the SPA still works with the existing
      // HttpOnly cookie until it expires, at which point the gateway
      // will 401 and bounce to /login.
      // eslint-disable-next-line no-console
      console.warn('Firebase init failed in SPA; falling back to /login bounce on expiry:', e);
    }
  }

  /**
   * Idempotently patches window.fetch to attach App Check headers
   * and handle 401/410 reactions for same-origin requests. Auth is
   * carried by the HttpOnly `__session` cookie (sent automatically
   * by the browser). Safe to call multiple times.
   */
  installFetchInterceptor(): void {
    if (typeof window === 'undefined' || typeof window.fetch !== 'function') {
      return;
    }
    if (window[FETCH_PATCHED_FLAG]) {
      return;
    }
    window[FETCH_PATCHED_FLAG] = true;

    const originalFetch = window.fetch.bind(window);
    const auth = this;

    window.fetch = async function patchedFetch(
      input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> {
      // Only intercept same-origin requests. Cross-origin XHRs
      // don't need our cookie or App Check headers.
      let url: string;
      if (typeof input === 'string') {
        url = input;
      } else if (input instanceof URL) {
        url = input.toString();
      } else {
        url = input.url;
      }
      if (!isSameOrigin(url)) {
        return originalFetch(input, init);
      }
      const merged: RequestInit = { ...(init || {}) };
      const headers = new Headers(merged.headers || {});
      // Hint to the BFF's htmlAwareUnauthorized that this is an
      // XHR, not a top-level document load — so failure should be
      // 401 JSON rather than a 302 to /login.
      if (!headers.has('Accept')) {
        headers.set('Accept', 'application/json');
      }
      // P2.B (2026-06-22 audit): attach Firebase App Check JWT so
      // the gateway can reject stolen-ID-token replays from curl /
      // other non-browser clients when APPCHECK_ENFORCE=enforce.
      // Best-effort — App Check init failure or refresh failure
      // must NOT block the request; the gateway's report-only mode
      // catches missing headers in the rollout window.
      if (auth.appCheck && !headers.has('X-Firebase-AppCheck')) {
        const acTok = await auth.getAppCheckToken();
        if (acTok) {
          headers.set('X-Firebase-AppCheck', acTok);
        }
      }
      merged.headers = headers;
      // P1.B (2026-06-22 audit): react to 401 token_revoked / 410
      // account_deleted by tearing down local session and bouncing
      // to /login.
      //
      // Guards:
      //  - only same-origin requests (already enforced above)
      //  - skip /api/v1/me/logout to avoid recursion
      //  - skip /login* so login can call /api/v1/auth/* freely
      //  - sessionStorage flag so concurrent XHRs trigger ONE
      //    signOut, not N parallel signOut+redirect races
      if (isAuthReactionEligible(url)) {
        const res = await originalFetch(input, merged);
        if (res.status === 401 || res.status === 410) {
          triggerSignOutOnce(auth, res.status);
        }
        return res;
      }
      return originalFetch(input, merged);
    };
  }
}

const AUTH_BOUNCE_FLAG = 'rc_auth_bouncing';

function isAuthReactionEligible(url: string): boolean {
  try {
    const u = new URL(url, window.location.href);
    const p = u.pathname;
    if (p === '/api/v1/me/logout') return false;
    if (p.startsWith('/login')) return false;
    if (p.startsWith('/api/v1/auth/')) return false;
    return p.startsWith('/api/') || p.startsWith('/ws');
  } catch {
    return false;
  }
}

function triggerSignOutOnce(auth: AuthService, status: number): void {
  try {
    if (window.sessionStorage.getItem(AUTH_BOUNCE_FLAG)) {
      return;
    }
    window.sessionStorage.setItem(AUTH_BOUNCE_FLAG, String(status));
  } catch {
    // sessionStorage unavailable (Safari private etc.); fall through
    // and let signOut run — worst case a couple of redundant POSTs
    // to /me/logout, all fire-and-forget anyway.
  }
  // Preserve the user's pre-bounce location so the /login redirect
  // can return them after a fresh sign-in. Skip if we're already on
  // /login (defence in depth — isAuthReactionEligible already filters).
  let returnTo = '/';
  try {
    if (!window.location.pathname.startsWith('/login')) {
      returnTo = window.location.pathname + window.location.search;
    }
  } catch {
    // ignore
  }
  auth.signOut(returnTo);
}

function isSameOrigin(url: string): boolean {
  if (typeof window === 'undefined') {
    return false;
  }
  if (url.startsWith('/')) {
    return true;
  }
  try {
    const u = new URL(url, window.location.href);
    return u.origin === window.location.origin;
  } catch {
    return false;
  }
}

/**
 * provideAuth() — registers the AuthService and patches window.fetch
 * during Angular bootstrap. Drop this into the providers array in
 * app.config.ts.
 *
 * Also kicks off Firebase init (token refresh + App Check) as a
 * fire-and-forget — the Firebase SDK is dynamic-imported and we
 * MUST NOT block bootstrap on its download. The HttpOnly cookie
 * carries auth from the start; once Firebase loads,
 * `onIdTokenChanged` refreshes the cookie via /api/v1/auth/exchange.
 */
export function provideAuth(): Provider[] {
  return [
    AuthService,
    {
      provide: APP_INITIALIZER,
      multi: true,
      useFactory: (auth: AuthService) => () => {
        auth.installFetchInterceptor();
        // No await — see function doc above.
        void auth.initFirebaseRefresh();
      },
      deps: [AuthService],
    },
  ];
}
