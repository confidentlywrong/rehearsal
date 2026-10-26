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

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthService } from './auth.service';

// Hoisted shared spies. vi.mock() factory bodies are hoisted above
// the import of the SUT, so we cannot reference module-scope spies
// inside the factory directly. We use `vi.hoisted` to declare them
// in a way that survives the hoist.
const fb = vi.hoisted(() => {
  const onIdTokenChanged = vi.fn();
  const signOut = vi.fn().mockResolvedValue(undefined);
  const getAuth = vi.fn().mockReturnValue({ __tag: 'auth-instance' });
  const connectAuthEmulator = vi.fn();
  const initializeApp = vi.fn().mockReturnValue({ __tag: 'app-instance' });
  const initializeAppCheck = vi.fn();
  const reCaptchaCtor = vi.fn();
  return {
    onIdTokenChanged,
    signOut,
    getAuth,
    connectAuthEmulator,
    initializeApp,
    initializeAppCheck,
    reCaptchaCtor,
  };
});

vi.mock('firebase/app', () => ({
  initializeApp: fb.initializeApp,
}));

vi.mock('firebase/auth', () => ({
  getAuth: fb.getAuth,
  onIdTokenChanged: fb.onIdTokenChanged,
  connectAuthEmulator: fb.connectAuthEmulator,
  signOut: fb.signOut,
}));

vi.mock('firebase/app-check', () => ({
  initializeAppCheck: fb.initializeAppCheck,
  ReCaptchaEnterpriseProvider: fb.reCaptchaCtor,
}));

const TOKEN_KEY = 'rc_id_token';

function resetEnv(env: Record<string, string> | undefined): void {
  (window as unknown as { ENV?: Record<string, string> }).ENV = env;
}

/**
 * Minimal in-memory Storage stub. happy-dom 20 + Node 22 conflict
 * on which localStorage implementation wins on `window`, and we
 * don't actually need a real Storage for these tests — just the
 * three methods AuthService calls.
 */
function installLocalStorageStub(): void {
  const store = new Map<string, string>();
  const stub = {
    getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k: string, v: string) => {
      store.set(k, String(v));
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: stub,
  });
}

beforeEach(() => {
  installLocalStorageStub();
  resetEnv(undefined);
  fb.onIdTokenChanged.mockReset();
  fb.signOut.mockReset();
  fb.signOut.mockResolvedValue(undefined);
  fb.initializeApp.mockReset();
  fb.initializeApp.mockReturnValue({ __tag: 'app-instance' });
  fb.getAuth.mockReset();
  fb.getAuth.mockReturnValue({ __tag: 'auth-instance' });
  fb.connectAuthEmulator.mockReset();
  fb.initializeAppCheck.mockReset();
  fb.reCaptchaCtor.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AuthService P2 #13 — Firebase token refresh', () => {
  it('skips all Firebase wiring when window.ENV has no FIREBASE_API_KEY (local-dev byte-identical path)', async () => {
    // No env at all. This is the docker-compose / IDENTITY_PROVIDER=local
    // posture — must remain a no-op.
    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    expect(fb.initializeApp).not.toHaveBeenCalled();
    expect(fb.getAuth).not.toHaveBeenCalled();
    expect(fb.initializeAppCheck).not.toHaveBeenCalled();
    expect(fb.onIdTokenChanged).not.toHaveBeenCalled();
  });

  it('initializes Firebase + onIdTokenChanged when FIREBASE_API_KEY + PROJECT_ID present', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    expect(fb.initializeApp).toHaveBeenCalledTimes(1);
    expect(fb.initializeApp).toHaveBeenCalledWith({
      apiKey: 'AIza-fake',
      authDomain: 'demo-proj.firebaseapp.com', // default derived from projectId
      projectId: 'demo-proj',
    });
    expect(fb.getAuth).toHaveBeenCalledTimes(1);
    expect(fb.onIdTokenChanged).toHaveBeenCalledTimes(1);
  });

  it('honors explicit FIREBASE_AUTH_DOMAIN over the projectId default', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
      FIREBASE_AUTH_DOMAIN: 'auth.example.com',
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    expect(fb.initializeApp).toHaveBeenCalledWith(
      expect.objectContaining({ authDomain: 'auth.example.com' }),
    );
  });

  it('initializes App Check when RECAPTCHA_ENTERPRISE_SITE_KEY is set and no emulator', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
      RECAPTCHA_ENTERPRISE_SITE_KEY: 'recap-site-key',
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    expect(fb.initializeAppCheck).toHaveBeenCalledTimes(1);
    expect(fb.reCaptchaCtor).toHaveBeenCalledWith('recap-site-key');
    const callArgs = fb.initializeAppCheck.mock.calls[0];
    expect(callArgs[1]).toMatchObject({ isTokenAutoRefreshEnabled: true });
  });

  it('skips App Check when site key is set but emulator host is configured', async () => {
    // Matches /login behaviour: emulator has no path to reCAPTCHA, so
    // initialising App Check there breaks local Firebase Auth emulator
    // testing.
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
      RECAPTCHA_ENTERPRISE_SITE_KEY: 'recap-site-key',
      FIREBASE_EMULATOR_HOST: '127.0.0.1:9099',
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    expect(fb.initializeAppCheck).not.toHaveBeenCalled();
    expect(fb.connectAuthEmulator).toHaveBeenCalledWith(
      expect.anything(),
      'http://127.0.0.1:9099',
      { disableWarnings: true },
    );
  });

  it('does NOT initialize App Check when site key is missing (App Check optional)', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
      // no RECAPTCHA_ENTERPRISE_SITE_KEY
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    expect(fb.initializeAppCheck).not.toHaveBeenCalled();
  });

  it('refreshes the HttpOnly cookie by POSTing to /api/v1/auth/exchange on token refresh', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
    });

    // Stub fetch so the exchange POST is captured.
    const fetchSpy = vi.fn().mockResolvedValue(new Response('', { status: 204 }));
    window.fetch = fetchSpy;

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    // Pull the callback that was registered with onIdTokenChanged
    // and invoke it with a fake user.
    const [, callback] = fb.onIdTokenChanged.mock.calls[0];
    const fakeUser = { getIdToken: vi.fn().mockResolvedValue('fresh-token-abc') };
    await callback(fakeUser);

    expect(fakeUser.getIdToken).toHaveBeenCalledWith(false);
    // Token is NOT stored in localStorage (Finding #5 hardening).
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
    // getToken() always returns null — token lives in HttpOnly cookie.
    expect(svc.getToken()).toBeNull();
    // Exchange endpoint was called with the fresh token.
    expect(fetchSpy).toHaveBeenCalledWith('/api/v1/auth/exchange', expect.objectContaining({
      method: 'POST',
      headers: { 'Authorization': 'Bearer fresh-token-abc' },
      credentials: 'same-origin',
    }));
  });

  it('does not crash on sign-out (user becomes null) — cookie expires naturally', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    const [, callback] = fb.onIdTokenChanged.mock.calls[0];
    // Should not throw — user-null is the sign-out signal.
    await callback(null);
  });

  it('does not crash when getIdToken() rejects (revoked/offline) — cookie expires and user bounces to /login', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    const [, callback] = fb.onIdTokenChanged.mock.calls[0];
    const badUser = { getIdToken: vi.fn().mockRejectedValue(new Error('revoked')) };
    // Should not throw — the catch block handles gracefully.
    await callback(badUser);
  });

  it('is idempotent — calling initFirebaseRefresh twice initializes Firebase only once', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();
    await svc.initFirebaseRefresh();

    expect(fb.initializeApp).toHaveBeenCalledTimes(1);
    expect(fb.getAuth).toHaveBeenCalledTimes(1);
    expect(fb.onIdTokenChanged).toHaveBeenCalledTimes(1);
  });

  it('swallows App Check init failures and still wires onIdTokenChanged', async () => {
    fb.initializeAppCheck.mockImplementationOnce(() => {
      throw new Error('recaptcha key invalid');
    });
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
      RECAPTCHA_ENTERPRISE_SITE_KEY: 'bad-key',
    });

    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    expect(warnSpy).toHaveBeenCalled();
    // Critical: auth + refresh still wired even when App Check failed,
    // so the user can still sign in (just unattested).
    expect(fb.getAuth).toHaveBeenCalled();
    expect(fb.onIdTokenChanged).toHaveBeenCalled();
  });
});

describe('AuthService.signOut() — revokes Firebase session when present', () => {
  it('calls firebaseAuthMod.signOut(firebaseAuth) when Firebase is initialised', async () => {
    resetEnv({
      FIREBASE_API_KEY: 'AIza-fake',
      FIREBASE_PROJECT_ID: 'demo-proj',
    });

    const svc = new AuthService();
    await svc.initFirebaseRefresh();

    // Stub window.location.assign so signOut doesn't trigger a real navigation.
    const assignSpy = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign: assignSpy },
    });

    // Stub fetch so the keepalive POST doesn't throw in jsdom.
    window.fetch = vi.fn().mockResolvedValue(new Response('', { status: 200 }));

    svc.signOut('/somewhere');

    // Firebase signOut was triggered.
    expect(fb.signOut).toHaveBeenCalledWith({ __tag: 'auth-instance' });
    // Token is NOT stored in localStorage (Finding #5 hardening).
    expect(window.localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(assignSpy).toHaveBeenCalledWith('/login?redirect=%2Fsomewhere');
  });

  it('does NOT call firebase signOut when Firebase was never initialised (local-dev path)', async () => {

    const svc = new AuthService();
    // Skip initFirebaseRefresh entirely — local-dev mode.

    const assignSpy = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign: assignSpy },
    });
    window.fetch = vi.fn().mockResolvedValue(new Response('', { status: 200 }));

    svc.signOut('/');

    expect(fb.signOut).not.toHaveBeenCalled();
  });
});
