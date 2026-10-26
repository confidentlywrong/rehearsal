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

package middleware

import (
	"net/http"
	"os"
	"strings"

	"github.com/gin-gonic/gin"
)

// SecurityHeadersConfig controls which security headers the middleware
// emits. The zero value yields the recommended default set (CSP in
// report-only mode, frame-ancestors none, MIME sniffing off, etc.).
//
// Why these defaults exist:
//   - HARD-LOCK-DOWN-FIRST is the wrong call here because the Angular SPA
//     loads Firebase Web SDK from www.gstatic.com and the Cloud Run
//     stack may add other sources later. Shipping CSP in report-only
//     for one cycle lets us collect violations from real traffic
//     before flipping enforcement on. Set ReportOnly=false (or env
//     CSP_ENFORCE=true) once the report log is clean.
//   - frame-ancestors 'none' + X-Frame-Options DENY together stop
//     clickjacking; they overlap intentionally because some old
//     browsers still parse only one of them.
//   - Strict-Transport-Security is only emitted when EnableHSTS is
//     true. We auto-detect HTTPS by trusting X-Forwarded-Proto from
//     the LB \u2014 Cloud Run sets it on every request behind the E3
//     loadbalancer. Local docker-compose stays on http:// and never
//     gets HSTS, which would otherwise pin the browser to https on
//     localhost and break dev permanently.
type SecurityHeadersConfig struct {
	// ContentSecurityPolicy is the CSP value emitted as either
	// `Content-Security-Policy` or `Content-Security-Policy-Report-Only`.
	// Empty string skips the header entirely (escape hatch for tests
	// or for an API-only Cloud Run service where CSP is meaningless).
	ContentSecurityPolicy string

	// ReportOnly emits CSP as `Content-Security-Policy-Report-Only`
	// instead of the enforcing header. Defaults to true so the first
	// deploy never breaks the frontend; flip to false once the
	// report log is clean.
	ReportOnly bool

	// EnableHSTS controls whether Strict-Transport-Security is emitted.
	// Off by default for local dev safety; on in production deploys.
	// When on, the middleware still only adds HSTS when the request
	// arrived over HTTPS (X-Forwarded-Proto=https or TLS handshake
	// detected) to avoid pinning a localhost browser to https://.
	EnableHSTS bool

	// PermissionsPolicy controls the `Permissions-Policy` header.
	// Defaults to a deny-list of the APIs this app doesn't use
	// (geolocation, microphone, camera, payment) so a future
	// dependency can't silently start prompting users.
	PermissionsPolicy string

	// ReferrerPolicy controls the `Referrer-Policy` header. Defaults
	// to "strict-origin-when-cross-origin" \u2014 the modern browser
	// default, but emitting it explicitly stops a misconfigured
	// upstream from changing it.
	ReferrerPolicy string
}

// DefaultCSP returns a Content-Security-Policy string suitable for
// every Go service in this monorepo. It allows the sources the static
// /login page + Angular SPA actually use today; new sources should be
// added here (single source of truth) rather than per-service.
//
// Sources whitelisted and why:
//   - 'self'                            : same-origin assets bundled by ng build
//   - 'unsafe-inline' (style-src)       : Angular ships inline component styles;
//                                         disabling this would require a CSP nonce
//                                         injected at SSR-time and the SPA is CSR-only
//   - https://www.gstatic.com           : Firebase Web SDK lazy-loaded by /login
//   - https://*.googleapis.com          : Firebase Auth REST (identitytoolkit) +
//                                         Firebase Firestore + Gemini API
//   - https://*.firebaseio.com          : Firebase Auth listeners
//   - https://*.firebaseapp.com         : Firebase auth domain
//   - https://accounts.google.com       : OAuth popup for "Continue with Google"
//   - data: blob: (img-src)             : Three.js generates blob URLs for textures
//   - ws: wss: (connect-src)            : SaaS dashboard uses websockets
//   - http://localhost:9099             : Firebase Auth Emulator (dev only;
//                                         harmless to leave in production CSP
//                                         since the LB never routes :9099)
//
// frame-ancestors 'none' + form-action 'self' + base-uri 'self' lock
// down clickjacking + form hijacking + <base> tag injection.
func DefaultCSP() string {
	return strings.Join([]string{
		"default-src 'self'",
		// www.google.com hosts reCAPTCHA Enterprise's enterprise.js,
		// loaded by Firebase App Check (ReCaptchaEnterpriseProvider).
		// Without it, enabling CSP_ENFORCE=true breaks App Check init
		// the moment RECAPTCHA_ENTERPRISE_SITE_KEY is set.
		"script-src 'self' https://www.gstatic.com https://www.google.com https://apis.google.com",
		// fonts.googleapis.com serves the @font-face CSS stylesheets
		// for Material Icons + Material Symbols Outlined + Noto Color
		// Emoji (linked from web/frontend/src/index.html). Without it
		// the icon ligatures (e.g. <span class="material-icons">settings
		// </span>) render as raw text "settings", "unfold_more", etc.
		// Verified live regression 2026-06-22.
		"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
		"img-src 'self' data: blob: https:",
		// fonts.gstatic.com hosts the actual .woff2 binaries pointed
		// at by the fonts.googleapis.com stylesheets above. Both must
		// be allowed or the icons fall back to raw text.
		"font-src 'self' data: https://fonts.gstatic.com",
		"connect-src 'self' https://*.googleapis.com " +
			"https://*.firebaseio.com https://*.firebaseapp.com " +
			"https://identitytoolkit.googleapis.com " +
			"https://securetoken.googleapis.com " +
			"ws: wss: http://localhost:9099",
		// www.google.com + www.gstatic.com cover the reCAPTCHA
		// Enterprise challenge iframe rendered by App Check on
		// suspected-bot sign-ins. Without these the iframe is
		// blocked and the user sees a blank reCAPTCHA box.
		"frame-src 'self' https://*.firebaseapp.com https://accounts.google.com https://www.google.com https://www.gstatic.com",
		"frame-ancestors 'none'",
		"form-action 'self'",
		"base-uri 'self'",
		"object-src 'none'",
	}, "; ")
}

// DefaultPermissionsPolicy returns a deny-list of browser APIs this
// app does not use. New entries should be added as the app grows;
// removing entries (i.e. enabling an API) should require an explicit
// product decision.
func DefaultPermissionsPolicy() string {
	return strings.Join([]string{
		"geolocation=()",
		"microphone=()",
		"camera=()",
		"payment=()",
		"usb=()",
		"interest-cohort=()",
	}, ", ")
}

// SecurityHeadersFromEnv builds a SecurityHeadersConfig from process
// env so each service's main() can wire the middleware with one line.
//
// Env vars (all optional):
//   - CSP_ENFORCE=true    \u2192 emit `Content-Security-Policy` instead of
//                         the `-Report-Only` variant
//   - CSP_OVERRIDE=...    \u2192 replace the default CSP body (use when a
//                         single service needs an exotic source list)
//   - CSP_DISABLE=true    \u2192 emit no CSP header at all (escape hatch
//                         for API-only services where CSP is meaningless)
//   - SECURITY_HSTS=true  \u2192 emit HSTS on HTTPS requests. Off in local
//                         dev so localhost doesn't get pinned to https.
func SecurityHeadersFromEnv() SecurityHeadersConfig {
	csp := DefaultCSP()
	if v := os.Getenv("CSP_OVERRIDE"); v != "" {
		csp = v
	}
	if strings.EqualFold(os.Getenv("CSP_DISABLE"), "true") {
		csp = ""
	}
	return SecurityHeadersConfig{
		ContentSecurityPolicy: csp,
		ReportOnly:            !strings.EqualFold(os.Getenv("CSP_ENFORCE"), "true"),
		EnableHSTS:            strings.EqualFold(os.Getenv("SECURITY_HSTS"), "true"),
		PermissionsPolicy:     DefaultPermissionsPolicy(),
		ReferrerPolicy:        "strict-origin-when-cross-origin",
	}
}

// SecurityHeaders returns a gin middleware that emits a set of
// hardening response headers on every response.
//
// Ordering note: mount this BEFORE CORS so that even OPTIONS preflight
// responses carry the hardening headers (some browsers cache OPTIONS
// responses without them, defeating the protection).
func SecurityHeaders(cfg SecurityHeadersConfig) gin.HandlerFunc {
	cspHeader := "Content-Security-Policy"
	if cfg.ReportOnly {
		cspHeader = "Content-Security-Policy-Report-Only"
	}

	return func(c *gin.Context) {
		h := c.Writer.Header()

		// Always emit the non-controversial set. These are cheap and
		// safe on every response regardless of mode or scheme.
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		if cfg.ReferrerPolicy != "" {
			h.Set("Referrer-Policy", cfg.ReferrerPolicy)
		}
		if cfg.PermissionsPolicy != "" {
			h.Set("Permissions-Policy", cfg.PermissionsPolicy)
		}
		if cfg.ContentSecurityPolicy != "" {
			h.Set(cspHeader, cfg.ContentSecurityPolicy)
		}

		// HSTS only on HTTPS so docker-compose http:// keeps working.
		// Cloud Run + the E3 HTTPS LB both set X-Forwarded-Proto=https;
		// direct localhost runs leave it unset. Belt-and-suspenders:
		// also check c.Request.TLS in case someone runs the binary
		// behind a TLS-terminating proxy that doesn't set the header.
		if cfg.EnableHSTS && requestIsHTTPS(c.Request) {
			h.Set("Strict-Transport-Security",
				"max-age=31536000; includeSubDomains")
		}

		c.Next()
	}
}

// requestIsHTTPS returns true when the request arrived over TLS or
// when an upstream LB has tagged X-Forwarded-Proto: https. We never
// emit HSTS on a plaintext request \u2014 that would silently misadvise
// clients to https:// for the same host.
func requestIsHTTPS(r *http.Request) bool {
	if r == nil {
		return false
	}
	if r.TLS != nil {
		return true
	}
	if strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
		return true
	}
	return false
}
