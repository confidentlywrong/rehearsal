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

// login-app.js
//
// CSP fix (2026-06-22): this block was previously an inline `<script>`
// in index.html. The deployed CSP `script-src 'self' https://www.gstatic.com
// https://apis.google.com` blocks it (no `'unsafe-inline'`, no nonce,
// no hash), which left the login form with no submit handler — clicking
// Continue did nothing in production. Moving the code to a sibling file
// served from /login/ puts it under `'self'`, no CSP changes needed.
//
// Loaded AFTER /login/config-init.js (which sets window.__firebaseConfig
// from the BFF env). Classic scripts run in document order, so keep the
// three <script> tags in index.html in their current order.
(function () {
  var qs = new URLSearchParams(location.search);
  var redirectTarget = qs.get('redirect') || '/';
  // Defense against open-redirect (2026-06-21 audit P2):
  // - reject anything not starting with '/'
  // - reject '//' (protocol-relative URL → cross-origin)
  // - reject '/\' (browsers normalize backslash to '/' in URLs, so
  //   '/\evil.com' becomes '//evil.com' at navigation time)
  // - normalize via the URL constructor and only keep pathname +
  //   search so encoded tricks like '/%2f%2fevil.com' can't
  //   smuggle a cross-origin redirect either.
  try {
    if (typeof redirectTarget !== 'string' ||
        !redirectTarget.startsWith('/') ||
        redirectTarget.startsWith('//') ||
        redirectTarget.startsWith('/\\')) {
      redirectTarget = '/';
    } else {
      var parsed = new URL(redirectTarget, location.origin);
      if (parsed.origin !== location.origin) {
        redirectTarget = '/';
      } else {
        redirectTarget = parsed.pathname + parsed.search + parsed.hash;
      }
    }
  } catch (e) {
    redirectTarget = '/';
  }

  var form = document.getElementById('login-form');
  var emailEl = document.getElementById('email');
  var passwordEl = document.getElementById('password');
  var passwordRow = document.getElementById('password-row');
  var submitBtn = document.getElementById('submit');
  var errorEl = document.getElementById('error');
  var noticeEl = document.getElementById('notice');
  var modePill = document.getElementById('mode-pill');
  var titleEl = document.getElementById('card-title');
  var subtitleEl = document.getElementById('card-subtitle');
  var togglesEl = document.getElementById('toggles');
  var togglesBackEl = document.getElementById('toggles-back');
  var footerEl = document.getElementById('footer');

  // G1: three modes drive the form. signin/signup share the same
  // fields; reset hides the password row and changes the button.
  // Keeping the state in a single variable means the submit
  // handler can branch once instead of having three handlers.
  var mode = 'signin';

  function clearMessages() {
    errorEl.classList.remove('show');
    errorEl.textContent = '';
    noticeEl.classList.remove('show');
    noticeEl.textContent = '';
  }
  function showError(msg) {
    clearMessages();
    errorEl.textContent = msg;
    errorEl.classList.add('show');
  }
  function showNotice(msg) {
    clearMessages();
    noticeEl.textContent = msg;
    noticeEl.classList.add('show');
  }
  function setMode(next) {
    mode = next;
    clearMessages();
    if (next === 'signin') {
      titleEl.textContent = 'The Rehearsal';
      subtitleEl.textContent = 'Sign in to your workspace.';
      submitBtn.textContent = 'Continue';
      passwordRow.style.display = '';
      passwordEl.required = true;
      passwordEl.setAttribute('autocomplete', 'current-password');
      togglesEl.hidden = false;
      togglesBackEl.hidden = true;
    } else if (next === 'signup') {
      titleEl.textContent = 'Create account';
      subtitleEl.textContent = 'Use your email and a password (6+ chars).';
      submitBtn.textContent = 'Create account';
      passwordRow.style.display = '';
      passwordEl.required = true;
      passwordEl.setAttribute('autocomplete', 'new-password');

      togglesEl.hidden = true;
      togglesBackEl.hidden = false;
    } else if (next === 'reset') {
      titleEl.textContent = 'Reset password';
      subtitleEl.textContent =
        'We\u2019ll email you a link to choose a new password.';
      submitBtn.textContent = 'Send reset link';
      passwordRow.style.display = 'none';
      passwordEl.required = false;
      togglesEl.hidden = true;
      togglesBackEl.hidden = false;
    }
  }

  // Persist the token. localStorage feeds the SPA's
  // Authorization header on XHR. The __session cookie used to
  // be set here via document.cookie, but a JS-set cookie cannot
  // carry HttpOnly, so any XSS could exfiltrate the ID token
  // (audit P1 #5). We now POST the bearer token to the BFF
  // (/api/v1/auth/exchange), which echoes it back as an
  // HttpOnly+Secure+SameSite=Lax cookie. The cookie is required
  // for server-side reads on the initial document fetch + for
  // WebSocket upgrades that can't carry custom headers.
  //
  // Bug fix 2026-06-22: previously this function swallowed all
  // exchange failures (including 401s) and ALWAYS redirected to
  // the SPA. But the SPA's document fetch needs the __session
  // cookie to pass the BFF's auth middleware — without it the
  // browser bounces straight back to /login, producing an
  // invisible login loop with no error UI. We now treat a
  // non-2xx exchange as a hard failure and surface it to the
  // user, so the symptom maps to a clear, debuggable message.
  async function persistAndGo(token) {
    // Finding #5 (2026-07-02): removed localStorage.setItem('rc_id_token', token)
    // — the exchange POST below sets an HttpOnly cookie; JS-accessible
    // storage was a token-theft vector via XSS.
    let resp;
    try {
      // P2.B (2026-06-22 audit): attach Firebase App Check JWT
      // when available so the gateway can verify the request
      // came from the registered Firebase app, not a curl
      // replay. Best-effort — null token (App Check disabled
      // or refresh failed) falls through silently; the gateway
      // tolerates missing headers in report-only mode and only
      // blocks in enforce mode.
      var headers = { 'Authorization': 'Bearer ' + token };
      if (typeof window.__rcAppCheckToken === 'function') {
        try {
          var acTok = await window.__rcAppCheckToken();
          if (acTok) headers['X-Firebase-AppCheck'] = acTok;
        } catch (e) { /* swallow */ }
      }
      resp = await fetch('/api/v1/auth/exchange', {
        method: 'POST',
        headers: headers,
        credentials: 'same-origin',
      });
    } catch (e) {
      console.error('[auth] exchange network error:', e);
      showError('Could not reach the sign-in service. Check your connection and try again.');
      submitBtn.disabled = false;
      return;
    }
    if (!resp.ok) {
      var body = '';
      try { body = await resp.text(); } catch (e) {}
      console.error('[auth] exchange failed: HTTP ' + resp.status + ' ' + body);
      if (resp.status === 401) {
        showError('Sign-in succeeded with Firebase but the server rejected the token. This usually means the server-side Firebase project ID is misconfigured. Please contact an administrator.');
      } else {
        showError('Sign-in failed at the session exchange step (HTTP ' + resp.status + '). Please try again or contact an administrator.');
      }
      submitBtn.disabled = false;
      return;
    }
    location.replace(redirectTarget);
  }

  var fbConfig = window.__firebaseConfig;
  if (!fbConfig || !fbConfig.apiKey) {
    // Hardening (2026-06-22): the previous "Local" fallback (clicking
    // Continue posted the literal string 'local-dev' to
    // /api/v1/auth/exchange) was a silent auth-bypass risk if the BFF
    // env ever shipped to GCP without FIREBASE_API_KEY. Single-tenant
    // mode is no longer offered — the page now refuses to render a
    // usable form unless Firebase is configured. Local development
    // does not reach this page: the BFF's LocalResolver auto-
    // authenticates every request and loginHandler in
    // cmd/frontend/main.go 302s /login → / in local mode.
    modePill.textContent = 'Not configured';
    emailEl.disabled = true;
    passwordEl.disabled = true;
    submitBtn.disabled = true;
    togglesEl.hidden = true;
    togglesBackEl.hidden = true;
    footerEl.style.display = 'none';
    showError(
      'Sign-in is not configured for this deployment. ' +
      'FIREBASE_API_KEY / FIREBASE_PROJECT_ID are not set on the ' +
      'frontend service. Contact an administrator.'
    );
    return;
  }
  {
    modePill.textContent = 'Firebase';
    footerEl.style.display = 'none';

    // Lazy-load the Firebase SDK only when needed. The actual
    // module lives in /login/firebase-bootstrap.js so the page
    // can run under a strict CSP (script-src 'self' + the
    // gstatic origins) -- see P3.E + P2.A (2026-06-22 audit).
    // Previously this was an inline `script.textContent = [...]`
    // module, which blocked enabling CSP enforcement.
    var script = document.createElement('script');
    script.type = 'module';
    script.src = '/login/firebase-bootstrap.js';
    document.head.appendChild(script);

    setMode('signin');

    // Single submit handler, branches on mode. Keeps the wiring
    // simple at the cost of one if-else \u2014 worth it.
    // Map Firebase error codes to user-friendly messages so the
    // red banner is actionable instead of leaking SDK internals.
    // The full Firebase Error object is also logged to the console
    // for support / debugging.
    function friendlyAuthError(e) {
      var code = (e && e.code) || '';
      switch (code) {
        case 'auth/email-already-in-use':
          return 'An account already exists for this email. Use "Back to sign in" — or click "Forgot password?" if you don\'t remember the password.';
        case 'auth/invalid-credential':
        case 'auth/wrong-password':
        case 'auth/user-not-found':
          return 'Email or password is incorrect. Use "Forgot password?" if you need a reset link.';
        case 'auth/invalid-email':
          return 'That email address doesn\'t look right.';
        case 'auth/weak-password':
          return 'Password is too weak — use at least 6 characters.';
        case 'auth/too-many-requests':
          return 'Too many failed attempts. Wait a minute and try again, or use "Forgot password?".';
        case 'auth/network-request-failed':
          return 'Network error reaching Firebase. Check your connection and try again.';
        case 'auth/user-disabled':
          return 'This account has been disabled. Contact an administrator.';
        default:
          return (e && e.message) || 'Request failed.';
      }
    }

    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      // Diagnostic safety net: the Firebase SDK is loaded as an
      // ES module from gstatic.com, which executes async. If the
      // user clicks Continue before the module finishes loading,
      // window.__rcSignIn is still undefined — without this guard
      // the call would throw a cryptic TypeError. Show a clear,
      // actionable message instead and let them retry.
      if (mode !== 'reset' && typeof window.__rcSignIn !== 'function') {
        showError('Authentication is still loading. Please wait a moment and try again.');
        return;
      }
      if (mode === 'reset' && typeof window.__rcResetPassword !== 'function') {
        showError('Authentication is still loading. Please wait a moment and try again.');
        return;
      }
      submitBtn.disabled = true;
      try {
        if (mode === 'signin') {
          var t1 = await window.__rcSignIn(emailEl.value, passwordEl.value);
          await persistAndGo(t1);
        } else if (mode === 'signup') {
          // G1: createUserWithEmailAndPassword auto-signs-in on
          // success, so we get an ID token straight back — same
          // persistAndGo() path as sign-in. The SPA will then
          // hit /api/v1/me, see needs_init=true, and call
          // /api/v1/me/init to create the users-table row
          // (IdentityService.ensureInitialized in auth/identity.service.ts).
          // There is no separate onboarding step — this codebase
          // uses a per-user data model (no orgs, no memberships),
          // documented in migrations/0001_users.sql.
          var t2 = await window.__rcSignUp(emailEl.value, passwordEl.value);
          await persistAndGo(t2);
        } else if (mode === 'reset') {
          await window.__rcResetPassword(emailEl.value);
          showNotice(
            'If an account exists for ' + emailEl.value +
            ', a reset link is on its way.'
          );
          submitBtn.disabled = false;
        }
      } catch (e) {
        // Always log the raw error for debugging; show the
        // friendly version in the UI.
        try { console.error('[auth] ' + mode + ' failed:', e); } catch (_) {}
        showError(friendlyAuthError(e));
        submitBtn.disabled = false;
      }
    });

    // Wire the three mode toggles to setMode. Using data-mode
    // attributes keeps the DOM-to-handler mapping declarative.
    ['toggle-signup', 'toggle-reset', 'toggle-signin'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('click', function (ev) {
        ev.preventDefault();
        setMode(el.getAttribute('data-mode'));
        submitBtn.disabled = false;
      });
    });
  }
})();
