// Copyright 2026 Christian Haeussinger
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// firebase-bootstrap.js
//
// P3.E + P2.A (2026-06-22 audit): the four window.__rc* entrypoints
// used to live in an inline `<script type="module">` injected via
// `script.textContent = [...].join('\n')` in index.html. That made
// the page incompatible with a strict CSP (script-src 'self') and
// blocked enabling CSP enforcement (P2.A). Moving the module to a
// sibling file served by the BFF lets the same CSP cover both
// without `'unsafe-inline'`.
//
// We MUST be loaded as `type="module"` AFTER index.html has set
// `window.__firebaseConfig`, `window.__recaptchaSiteKey`, and
// `window.__firebaseEmulatorHost` on the global. The login form's
// submit handler in index.html reads `window.__rcSignIn` /
// `window.__rcSignUp` / `window.__rcResetPassword` so we attach to
// `window` exactly as the inline version did.
//
// If the Firebase config is missing (local-dev IDENTITY_PROVIDER=local
// path), this file is never loaded — index.html guards the script
// tag with the same `fbConfig && fbConfig.apiKey` check.

import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js';
import {
  initializeAppCheck,
  ReCaptchaEnterpriseProvider,
  getToken as getAppCheckToken,
} from 'https://www.gstatic.com/firebasejs/10.7.1/firebase-app-check.js';
import {
  getAuth,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  sendPasswordResetEmail,
  connectAuthEmulator,
} from 'https://www.gstatic.com/firebasejs/10.7.1/firebase-auth.js';

const app = initializeApp(window.__firebaseConfig);

// Bind App Check BEFORE getAuth so the auth SDK picks up the App
// Check token on its first call. Skip when running against the
// emulator (App Check has no path there) or when the site key is
// unset.
let appCheckInstance = null;
if (window.__recaptchaSiteKey && !window.__firebaseEmulatorHost) {
  try {
    appCheckInstance = initializeAppCheck(app, {
      provider: new ReCaptchaEnterpriseProvider(window.__recaptchaSiteKey),
      isTokenAutoRefreshEnabled: true,
    });
  } catch (e) {
    console.warn('App Check init failed; continuing without attestation:', e);
  }
}

// P2.B (2026-06-22 audit): expose an App Check token getter so the
// login form in index.html can attach X-Firebase-AppCheck to the
// POST /api/v1/auth/exchange call. Without this, enabling
// APPCHECK_ENFORCE=enforce in the gateway would block every fresh
// sign-in — the only path that mints the gateway session cookie.
// Best-effort: returns null on any failure so report-only mode logs
// the gap without breaking auth.
window.__rcAppCheckToken = async function () {
  if (!appCheckInstance) return null;
  try {
    const result = await getAppCheckToken(appCheckInstance, /* forceRefresh */ false);
    return result?.token || null;
  } catch (e) {
    return null;
  }
};

const auth = getAuth(app);
if (window.__firebaseEmulatorHost) {
  connectAuthEmulator(auth, 'http://' + window.__firebaseEmulatorHost, {
    disableWarnings: true,
  });
}

window.__rcSignIn = async function (email, password) {
  const cred = await signInWithEmailAndPassword(auth, email, password);
  return await cred.user.getIdToken();
};

window.__rcSignUp = async function (email, password) {
  const cred = await createUserWithEmailAndPassword(auth, email, password);
  return await cred.user.getIdToken();
};

window.__rcResetPassword = async function (email) {
  // P1.2 (2026-06-22): pass actionCodeSettings so the reset email's
  // 'Continue' button after a successful password reset routes the
  // user back to our /login page instead of Firebase's default
  // firebaseapp.com landing. The URL's host MUST be on the project's
  // Authorized Domains list (Firebase Console > Authentication >
  // Settings) or the SDK throws auth/unauthorized-continue-uri.
  const actionCodeSettings = {
    url: window.location.origin + '/login',
    handleCodeInApp: false,
  };
  await sendPasswordResetEmail(auth, email, actionCodeSettings);
};

// Signal to index.html's submit handler that the module finished
// loading; the handler shows a "still loading" hint when this is
// false. Pre-extraction the inline script set these by side-effect,
// which is fine — same behaviour applies here.
window.__rcAuthReady = true;
