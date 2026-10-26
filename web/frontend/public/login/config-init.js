// Copyright 2026 The Confidently Wrong Authors
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

// config-init.js
//
// CSP fix (2026-06-22): this block was previously an inline `<script>`
// in index.html, which violates the deployed CSP `script-src 'self'
// https://www.gstatic.com https://apis.google.com` (no `'unsafe-inline'`,
// no nonce, no hash). With CSP_ENFORCE=true the browser blocked the
// script, leaving `window.__firebaseConfig` undefined, which made the
// page fall back to "Local" mode AND prevented login-app.js from
// injecting the Firebase SDK loader. Symptom: clicking Continue did
// nothing on the deployed frontend.
//
// Must execute AFTER /config.js (which sets window.ENV) and BEFORE
// login-app.js (which reads window.__firebaseConfig). Plain classic
// scripts run in document order, so the load-order in index.html is
// what enforces that — keep the three <script> tags in the same order.
(function () {
  var env = window.ENV || {};
  if (env.FIREBASE_API_KEY && env.FIREBASE_PROJECT_ID) {
    window.__firebaseConfig = {
      apiKey: env.FIREBASE_API_KEY,
      authDomain: env.FIREBASE_AUTH_DOMAIN ||
        (env.FIREBASE_PROJECT_ID + '.firebaseapp.com'),
      projectId: env.FIREBASE_PROJECT_ID,
    };
  }
  if (env.FIREBASE_EMULATOR_HOST) {
    window.__firebaseEmulatorHost = env.FIREBASE_EMULATOR_HOST;
  }
  // P3 (2026-06-20): reCAPTCHA Enterprise site key. Set in env to
  // gate signup/sign-in behind Firebase App Check. The Firebase
  // SDK module below initializes App Check ONLY when this is
  // present, so an unconfigured local `make start` keeps working
  // without a Google Cloud reCAPTCHA project.
  if (env.RECAPTCHA_ENTERPRISE_SITE_KEY) {
    window.__recaptchaSiteKey = env.RECAPTCHA_ENTERPRISE_SITE_KEY;
  }
})();
