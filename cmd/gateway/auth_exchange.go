// Copyright 2026 Christian Haeussinger. Apache-2.0.

package main

// BFF session cookie endpoint.
//
//   POST /api/v1/auth/exchange → authExchangeHandler
//
// The login page (web/frontend/public/login/index.html) used to
// set the __session cookie directly via document.cookie. That made
// it impossible to mark the cookie HttpOnly+Secure, exposing the
// Firebase ID token to any XSS that can read document.cookie
// (P1 #5 in the 2026-06-19 audit).
//
// New flow:
//
//   1. /login obtains an ID token from Firebase (signInWithEmailAndPassword
//      or createUserWithEmailAndPassword).
//   2. /login POSTs to /api/v1/auth/exchange with
//      Authorization: Bearer <token>.
//   3. The Account middleware verifies the token; if valid, this
//      handler echoes the same token back as an HttpOnly+Secure
//      __session cookie that the browser sets automatically.
//   4. /login redirects to the SPA. The SPA still keeps the token
//      in localStorage for its Authorization header.
//
// This handler does NOT mint a new token or issue a server-side
// session — it just narrows the surface area of the existing
// Firebase ID token by keeping the cookie copy out of reach of
// JavaScript.

import (
	"log"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// sessionCookieName is the cookie the FirebaseResolver reads when
// no Authorization header is present (WebSocket upgrades, doc
// fetches). Must stay in sync with internal/account/firebase.go.
const sessionCookieName = "__session"

// sessionCookieMaxAge matches the Firebase ID token lifetime
// (1 hour). After expiry the SPA will refresh the token via the
// Firebase SDK and POST it back through /auth/exchange again.
const sessionCookieMaxAge = 60 * 60

// sessionCookieMaxAgeNeedsInit (P1.C, 2026-06-22 audit) is the
// cookie lifetime granted to identities whose users-table row does
// not exist yet (NeedsInit=true). Tightening forces the SPA to
// complete /api/v1/me/init promptly — the legitimate path takes
// ~1 second — and prevents a brand-new Firebase identity from
// sitting on a 1-hour BFF cookie without ever creating its row.
// After /me/init succeeds, initMeHandler re-stamps the cookie at
// the full sessionCookieMaxAge so the rest of the session is
// unaffected.
//
// B-23 (2026-06-23 audit): bumped from 5 min to 15 min. The shorter
// window made transient Postgres latency (>5 min on first-init
// during a Cloud SQL cold start, etc.) bounce users back to /login
// with no error context. 15 min is still well below the 1 h ID
// token TTL, so the security envelope is unchanged in practice
// while the UX papercut goes away.
const sessionCookieMaxAgeNeedsInit = 15 * 60

// authExchangeHandler stamps an HttpOnly+Secure __session cookie
// from the verified bearer token.
//
// Response codes:
//   - 204 on success (cookie set).
//   - 401 when there is no UserContext (token invalid / missing).
//   - 400 when the Authorization header is missing or malformed
//     (Account middleware would have already 401'd in this case,
//     but we double-check to avoid setting an empty cookie).
func authExchangeHandler() gin.HandlerFunc {
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}

		// Re-extract the raw token from the header. The middleware
		// already verified it; we just need the value to echo into
		// the cookie. The cookie path is what allows server-side
		// reads on subsequent navigations (SPA doc fetch + WS).
		token := bearerFromHeader(c.Request.Header.Get("Authorization"))
		if token == "" {
			c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "missing_bearer_token"})
			return
		}

		// HttpOnly: JS can't read it (XSS defense — the entire
		// point of this endpoint).
		// SameSite=Lax: cookie travels on top-level navigation
		// from /login → / and on same-origin XHR; blocks
		// cross-site POST CSRF.
		// Secure: only travel over HTTPS. Auto-detect via TLS
		// handshake or X-Forwarded-Proto from the LB.
		secure := requestIsHTTPS(c.Request)
		// P1.C (2026-06-22 audit): cap cookie lifetime when the
		// identity has not initialized its users-table row yet. See
		// sessionCookieMaxAgeNeedsInit doc above for rationale.
		maxAge := sessionCookieMaxAge
		if uc.NeedsInit {
			maxAge = sessionCookieMaxAgeNeedsInit
		}
		http.SetCookie(c.Writer, &http.Cookie{
			Name:     sessionCookieName,
			Value:    token,
			Path:     "/",
			MaxAge:   maxAge,
			HttpOnly: true,
			Secure:   secure,
			SameSite: http.SameSiteLaxMode,
		})
		// P3.2 (2026-06-22 audit): emit a one-line success log so
		// production login issues are trivially traceable in Cloud
		// Logging. Failures already log via middleware.Account /
		// FirebaseResolver upstream; this closes the success path.
		// needs_init is included so an operator can immediately tell
		// whether this is an established user or a brand-new signup.
		// P3.D (2026-06-22 audit): drop the email field — the
		// structured logger already attaches email_hash from
		// uc.Email via logging.FromContext when downstream code
		// uses it. Keeping a raw email on a per-request log line
		// retains PII in Cloud Logging for the bucket's retention
		// window. uid is the stable correlation key.
		log.Printf("AUTH: exchange ok uid=%s needs_init=%t secure=%t",
			uc.UserID, uc.NeedsInit, secure)
		c.Status(http.StatusNoContent)
	}
}

// clearSessionCookie writes a Set-Cookie header that erases the
// __session cookie at the browser. Called from logoutHandler so
// that POST /api/v1/me/logout actually drops the BFF-side credential
// (P1 #6 in the 2026-06-19 audit).
//
// MaxAge=-1 (per net/http docs) emits "Max-Age=0" plus a past
// Expires, telling every spec-compliant browser to delete the
// cookie immediately. Attributes (Path, SameSite, Secure) must
// match the original Set-Cookie or the browser keeps the old one.
func clearSessionCookie(c *gin.Context) {
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     sessionCookieName,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   requestIsHTTPS(c.Request),
		SameSite: http.SameSiteLaxMode,
	})
}

// refreshSessionCookieFullLifetime re-stamps the __session cookie
// at the full sessionCookieMaxAge (1 hour) using the bearer token
// from the current request's Authorization header.
//
// Called by initMeHandler on the create path so that a brand-new
// identity \u2014 which got a short-lived (5-minute, P1.C) cookie from
// /auth/exchange because NeedsInit was true \u2014 transitions seamlessly
// to a full-session cookie once the users-table row exists. No-op
// when the Authorization header is missing (defence in depth; the
// account middleware would have already 401'd).
func refreshSessionCookieFullLifetime(c *gin.Context) {
	token := bearerFromHeader(c.Request.Header.Get("Authorization"))
	if token == "" {
		return
	}
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     sessionCookieName,
		Value:    token,
		Path:     "/",
		MaxAge:   sessionCookieMaxAge,
		HttpOnly: true,
		Secure:   requestIsHTTPS(c.Request),
		SameSite: http.SameSiteLaxMode,
	})
}

// bearerFromHeader extracts the token from an Authorization
// header value formatted as "Bearer <token>". Returns "" for any
// other shape (including empty input).
func bearerFromHeader(h string) string {
	const prefix = "Bearer "
	if !strings.HasPrefix(h, prefix) {
		return ""
	}
	return strings.TrimSpace(h[len(prefix):])
}

// rawUserBearer returns the raw user bearer token attached to the
// current request, checking the Authorization header first and
// falling back to the __session cookie the SPA sets after
// /api/v1/auth/exchange. Returns "" when neither carries a token
// (loopback local-dev calls) — the caller must treat empty as
// "no per-user token available, agent will fall back to shared
// service auth or loopback LocalResolver".
//
// This helper exists so the spawn handlers can propagate the
// user's Firebase ID token into the agent process (Deferral A —
// per-user memory-gateway auth), without giving up on the cookie
// auth path that the SPA also uses.
func rawUserBearer(c *gin.Context) string {
	if tok := bearerFromHeader(c.Request.Header.Get("Authorization")); tok != "" {
		return tok
	}
	if cookie, err := c.Cookie(sessionCookieName); err == nil {
		return strings.TrimSpace(cookie)
	}
	return ""
}

// requestIsHTTPS mirrors internal/middleware/security_headers.go.
// Duplicated (rather than exported) to avoid widening the
// middleware package's public API for a 5-line helper. Cloud Run
// + the E3 HTTPS LB both set X-Forwarded-Proto=https; locally
// (HTTP only) we get false and the Secure attribute is omitted so
// the cookie still works.
func requestIsHTTPS(r *http.Request) bool {
	if r == nil {
		return false
	}
	if r.TLS != nil {
		return true
	}
	return strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https")
}
