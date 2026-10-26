// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/auth"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/GoogleCloudPlatform/race-condition/internal/middleware"
	"github.com/GoogleCloudPlatform/race-condition/internal/tracing"
	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/koding/websocketproxy"
)

// gatewayClient is a shared HTTP client for proxying requests to the gateway.
// Reused across all requests for connection pooling.
//
// B-18 (2026-06-23 audit): timeout bumped from 30s → 90s. The old
// 30s was tight against a worst-case Cloud Run cold-start chain:
// gateway boot (~5–10s) + first catalog discovery fanning out to 7
// agent URLs (5 Vertex AI Reasoning Engines + 2 Cloud Run runners,
// any of which may also be cold). Even with the gateway-side
// pre-warm + per-fetch timeout, a first-spawn-after-idle can
// legitimately approach 60s. 90s leaves headroom while still
// failing fast on actual stuck calls.
var gatewayClient = &http.Client{
	Timeout: 90 * time.Second,
	Transport: &http.Transport{
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 20,
		IdleConnTimeout:     90 * time.Second,
	},
}

// buildTenantConnStr assembles a PostgreSQL connection string for the
// account control-plane tables (users). Same resolution rules as
// cmd/gateway's buildBillingConnStr so a single env file works for
// both services:
//
//  1. DATABASE_URL (full DSN, wins if set)
//  2. ALLOYDB_* triplet (host/port/user/password/database)
//
// Returns the empty string when no credentials are configured —
// callers must treat that as "user lookup disabled" and degrade
// gracefully rather than crashing.
func buildTenantConnStr() string {
	if v := config.Optional("DATABASE_URL", ""); v != "" {
		return v
	}
	host := config.Optional("ALLOYDB_HOST", "")
	port := config.Optional("ALLOYDB_PORT", "")
	user := config.Optional("ALLOYDB_USER", "")
	pass := config.Optional("ALLOYDB_PASSWORD", "")
	db := config.Optional("ALLOYDB_DATABASE", "")
	if host == "" || port == "" || user == "" || db == "" {
		return ""
	}
	u := &url.URL{
		Scheme: "postgresql",
		User:   url.UserPassword(user, pass),
		Host:   fmt.Sprintf("%s:%s", host, port),
		Path:   "/" + db,
	}
	return u.String()
}

// cachedFileServer wraps http.FileServer with cache control headers.
// Hashed assets (non-HTML files) get long-term immutable caching.
// HTML files get no-cache to ensure the latest version is served.
func cachedFileServer(root http.FileSystem) http.Handler {
	fs := http.FileServer(root)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, ".html") || r.URL.Path == "/" || !strings.Contains(r.URL.Path, ".") {
			w.Header().Set("Cache-Control", "no-cache")
		} else {
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		}
		fs.ServeHTTP(w, r)
	})
}

// setupRouter creates the frontend Gin engine. oidcAudience is the OIDC
// audience for proxied gateway requests; empty disables attachment.
func setupRouter(oidcAudience string, accountResolver account.Resolver) *gin.Engine {
	r := gin.Default()

	// G5a: structured request log + request_id propagation. Mounted
	// FIRST so request_id is on ctx for everything downstream.
	r.Use(middleware.RequestLogger())

	// G5b: distributed tracing. No-op when tracing.Setup wasn't
	// called or OTEL_EXPORTER is unset.
	r.Use(middleware.RequestTracing("race-frontend"))

	// G4a: hardening headers (CSP report-only by default + X-Frame-Options,
	// X-Content-Type-Options, Referrer-Policy, Permissions-Policy, optional
	// HSTS on HTTPS). Mounted BEFORE CORS so OPTIONS preflight responses
	// also carry the headers.
	r.Use(middleware.SecurityHeaders(middleware.SecurityHeadersFromEnv()))

	r.Use(middleware.CORS(os.Getenv("CORS_ALLOWED_ORIGINS")))

	// Per-user identity gate. HTML / SPA shell requests get redirected
	// to /login; API and WebSocket get 401 JSON.
	r.Use(middleware.Account(middleware.AccountOptions{
		Resolver: accountResolver,
		SkipPaths: []string{
			"/health",
			"/frontend/health",
			"/config.js",
			"/frontend/config.js",
			"/login",
			"/frontend/login",
			"/__/auth/",
		},
		UnauthorizedHandler: htmlAwareUnauthorized,
		// InitPaths: routes a brand-new Firebase user (no users-table
		// row yet, NeedsInit=true) must reach so they can bootstrap
		// their account. Without this list the BFF 409s every call
		// here BEFORE the proxy even runs, blocking /me/init and
		// trapping signups in a loop. Must stay in sync with the
		// gateway's InitPaths (cmd/gateway/main.go) — the gateway
		// rejects again if its list is narrower. See the 2026-06-22
		// "Sign-in failed at the session exchange step (HTTP 409)"
		// fix-up for the full story.
		InitPaths: []string{
			"/api/v1/me",
			"/api/v1/me/init",
			"/api/v1/auth/exchange",
			"/api/v1/me/logout",
		},
		// NeedsInitHandler: the SPA shell (HTML doc fetch + every
		// asset the browser then pulls in — JS bundle, CSS,
		// favicons, webmanifest, fonts) must all reach the static
		// file server so the SPA can actually boot and call
		// /api/v1/me → /api/v1/me/init. Default behaviour would 409
		// with JSON on every one of those requests, leaving the
		// browser with a half-loaded page (HTML shell + no JS) and
		// the user trapped forever after sign-up. htmlAwareNeedsInit
		// returns 409 JSON only for /api/* and /ws* (so the SPA's
		// identity.service.ts needs_init branch still fires) and
		// passes everything else through.
		NeedsInitHandler: htmlAwareNeedsInit,
	}))

	r.Use(func(c *gin.Context) {
		log.Printf("Incoming Request: %s %s (Remote: %s)", c.Request.Method, c.Request.URL.Path, c.ClientIP())
		c.Next()
	})

	configHandler := middleware.ConfigJSHandler(map[string]string{
		"NG_APP_GATEWAY_URL":  "/ws",
		"NG_APP_GATEWAY_ADDR": "", // Relative
		// Firebase config + reCAPTCHA site key for both the Angular
		// SPA (AuthService) and the static /login page. All keys
		// default to "" when unset; local-dev (IDENTITY_PROVIDER=local)
		// never reads any of them because the BFF's LocalResolver
		// auto-authenticates every request and loginHandler 302s
		// /login → / before the page ever renders.
		//
		// SAAS_MODE removed 2026-06-22 (B-5): the per-user model has
		// no "single-tenant" branch left in any handler, so emitting
		// it served only as dead config the SPA's defunct saasModeOn()
		// helpers read.
		"FIREBASE_PROJECT_ID":    config.Optional("FIREBASE_PROJECT_ID", ""),
		"FIREBASE_API_KEY":       config.Optional("FIREBASE_API_KEY", ""),
		"FIREBASE_AUTH_DOMAIN":   config.Optional("FIREBASE_AUTH_DOMAIN", ""),
		"FIREBASE_EMULATOR_HOST": config.Optional("FIREBASE_AUTH_EMULATOR_HOST", ""),
		// P3 (2026-06-20): reCAPTCHA Enterprise site key for Firebase
		// App Check. When set, /login initializes App Check with the
		// ReCaptchaEnterpriseProvider so signup/sign-in traffic is
		// attested by reCAPTCHA before Firebase Auth accepts the
		// request. Empty = App Check disabled (single-tenant local
		// `make start` keeps working without a Google Cloud setup).
		"RECAPTCHA_ENTERPRISE_SITE_KEY": config.Optional("RECAPTCHA_ENTERPRISE_SITE_KEY", ""),
	})

	backendURL := config.Optional("GATEWAY_INTERNAL_URL",
		config.Optional("GATEWAY_URL", "http://localhost:8101"))

	wsURLStr := strings.Replace(backendURL, "http://", "ws://", 1)
	wsURLStr = strings.Replace(wsURLStr, "https://", "wss://", 1)
	if !strings.HasSuffix(wsURLStr, "/ws") {
		wsURLStr = strings.TrimSuffix(wsURLStr, "/") + "/ws"
	}

	u, err := url.Parse(wsURLStr)
	if err != nil {
		log.Fatalf("WebSocket Proxy: Invalid URL %q: %v", wsURLStr, err)
	}
	log.Printf("WebSocket Proxy: Initializing for %s", wsURLStr)
	wsProxy := websocketproxy.NewProxy(u)

	// Fix D (2026-07-03): configure the BFF's client-side upgrader
	// with a permissive CheckOrigin. ``koding/websocketproxy``
	// installs a default ``gorilla/websocket.Upgrader`` that enforces
	// ``Origin.Host == r.Host`` — which is always false in a proxied
	// setup where the browser is at ``localhost:9119`` (via the
	// Angular dev-server proxy) and the BFF is at ``localhost:9118``.
	// Without this override, the browser's WS handshake was
	// rejected with 403 by the BFF's OWN client-side upgrader BEFORE
	// wsProxy ever dialled the gateway, so the WebSocket never
	// opened and every prompt from the SPA was silently queued into
	// ``pendingFrames``. The real origin enforcement still happens on
	// the gateway (``CheckOrigin`` in cmd/gateway/main.go, gated on
	// CORS_ALLOWED_ORIGINS) — the BFF just needs to stop being a
	// second, stricter checkpoint.
	wsProxy.Upgrader = &websocket.Upgrader{
		CheckOrigin: func(r *http.Request) bool { return true },
	}

	wsProxy.Director = func(incoming *http.Request, out http.Header) {
		out.Set("Host", u.Host)
		for key := range out {
			if strings.HasPrefix(strings.ToLower(key), "x-goog-iap-") {
				out.Del(key)
			}
		}
		// Forward the browser's ``Origin`` header explicitly.
		// ``koding/websocketproxy`` does NOT preserve Origin on the
		// backend dial by default (it starts ``out`` empty and only
		// the Director's writes reach the gateway). Without this
		// line the gateway's WebSocket ``CheckOrigin`` sees an
		// empty Origin, doesn't match anything in
		// ``CORS_ALLOWED_ORIGINS``, and rejects the upgrade with
		// 403. Symptom: browser's WebSocket never opens →
		// ``sendBroadcastNow`` silently queues every prompt to
		// ``pendingFrames`` → the planner never receives a message.
		// See 2026-07-03 audit.
		if o := incoming.Header.Get("Origin"); o != "" {
			out.Set("Origin", o)
		}
		// Forward Cloud Run service-to-service auth via the
		// X-Serverless-Authorization header so the original
		// Authorization header (or __session cookie) carrying the
		// user's Firebase ID token reaches the gateway untouched.
		// See AttachOIDCTokenAs godoc for the underlying Cloud Run
		// contract that strips X-Serverless-Authorization before
		// container delivery.
		middleware.AttachOIDCTokenAs(incoming.Context(), out, oidcAudience, "X-Serverless-Authorization")
	}

	wsHandler := func(c *gin.Context) {
		wsProxy.ServeHTTP(c.Writer, c.Request)
	}

	apiHandler := func(c *gin.Context) {
		backendURL := config.Optional("GATEWAY_INTERNAL_URL",
			config.Optional("GATEWAY_URL", "http://localhost:8101"))

		action := c.Param("action")
		targetURL := strings.TrimSuffix(backendURL, "/") + "/api/v1" + action
		if c.Request.URL.RawQuery != "" {
			targetURL += "?" + c.Request.URL.RawQuery
		}

		log.Printf("Proxy Handler triggered for path: %s, target: %s", c.Request.URL.Path, targetURL)

		req, err := http.NewRequest(c.Request.Method, targetURL, c.Request.Body)
		if err != nil {
			log.Printf("Proxy Error: Failed to create request: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		// IMPORTANT (login-loop fix, 2026-06-22):
		// Forward the caller's Authorization header verbatim. This
		// header carries the user's Firebase ID token, which the
		// gateway's account middleware needs to identify the user —
		// especially for /api/v1/auth/exchange, which mints the
		// __session cookie from that token. We previously stripped
		// it and replaced it with a service-account OIDC token, which
		// silently broke every login.
		for key, values := range c.Request.Header {
			if strings.EqualFold(key, "Host") ||
				strings.EqualFold(key, "X-Serverless-Authorization") ||
				strings.HasPrefix(strings.ToLower(key), "x-goog-") {
				continue
			}
			for _, value := range values {
				req.Header.Add(key, value)
			}
		}

		// Cloud Run private-service auth: send the OIDC token in
		// X-Serverless-Authorization so Cloud Run's auth proxy
		// accepts the call and strips the header before delivering
		// to the gateway container. The user's bearer in
		// Authorization (set above) is then visible to gateway code.
		middleware.AttachOIDCTokenAs(c.Request.Context(), req.Header, oidcAudience, "X-Serverless-Authorization")

		client := gatewayClient
		resp, err := client.Do(req)
		if err != nil {
			log.Printf("Proxy Error: Client.Do failed: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode >= 400 {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
			resp.Body.Close()
			bodyStr := string(body)
			if len(bodyStr) > 500 {
				bodyStr = bodyStr[:500] + "..."
			}
			log.Printf("Proxy: Gateway returned status %d. Body: %s", resp.StatusCode, bodyStr)
			resp.Body = io.NopCloser(bytes.NewBuffer(body))
			resp.ContentLength = int64(len(body))
		}

		for key, values := range resp.Header {
			for _, value := range values {
				c.Writer.Header().Add(key, value)
			}
		}
		c.DataFromReader(resp.StatusCode, resp.ContentLength, resp.Header.Get("Content-Type"), resp.Body, nil)
	}

	fg := r.Group("/frontend")
	{
		fg.GET("/health", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"status": "ok", "service": "frontend"})
		})
		fg.GET("/config.js", configHandler)
		fg.GET("/ws", wsHandler)
		fg.Any("/api/v1/*action", apiHandler)
	}

	r.GET("/config.js", configHandler)
	r.GET("/ws", wsHandler)
	r.Any("/api/v1/*action", apiHandler)

	// SaaS shell (Phase 1): static login UI. Reachable in both modes
	// — in legacy mode the user can still navigate to it, but the
	// fallback session it would build is unused because TenantIfEnabled
	// is a no-op.
	r.GET("/login", loginHandler)
	r.GET("/login/", loginHandler)
	r.GET("/frontend/login", loginHandler)
	r.GET("/frontend/login/", loginHandler)

	// NOTE: there is no /onboard route or onboard handler in this
	// codebase. The data model is per-user (no orgs / no memberships
	// — see migrations/0001_users.sql), so the only first-run step is
	// /api/v1/me/init, which the SPA's IdentityService drives. The
	// previous Phase-8B onboarding shell + handler + htmlAware*
	// helpers were removed on 2026-06-22 as dead code that pointed at
	// a non-existent /api/v1/me/onboard endpoint.

	// Pre-create dev proxy if FRONTEND_DEV_URL is set (GO-3: reuse
	// the proxy's transport + connection pool across requests).
	devURL := config.Optional("FRONTEND_DEV_URL", "")
	var devProxy *httputil.ReverseProxy
	if devURL != "" {
		target, err := url.Parse(devURL)
		if err != nil {
			log.Fatalf("Invalid FRONTEND_DEV_URL: %v", err)
		}
		devProxy = httputil.NewSingleHostReverseProxy(target)
		devProxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("Dev proxy error (is Angular running at %s?): %v", devURL, err)
			http.Error(w, "Frontend dev server unavailable", http.StatusBadGateway)
		}
	}

	r.NoRoute(func(c *gin.Context) {
		if c.Request.Method != "GET" {
			c.Status(http.StatusNotFound)
			return
		}

		path := c.Request.URL.Path

		if devProxy != nil {
			// Strip /frontend/ prefix before proxying to dev server
			if strings.HasPrefix(path, "/frontend/") {
				c.Request.URL.Path = strings.TrimPrefix(path, "/frontend")
			} else if path == "/frontend" {
				c.Request.URL.Path = "/"
			}
			devProxy.ServeHTTP(c.Writer, c.Request)
			return
		}

		// Angular 21 application builder emits to ./web/frontend/dist/client/browser
		staticDir := "./web/frontend/dist/client/browser"
		if _, err := os.Stat(staticDir); os.IsNotExist(err) {
			for _, fallback := range []string{
				"./web/frontend/dist/browser",
				"./web/frontend/dist/client",
				"./web/frontend/dist",
			} {
				if info, err := os.Stat(fallback); err == nil && info.IsDir() {
					staticDir = fallback
					break
				}
			}
		}

		if strings.HasPrefix(path, "/frontend/") || path == "/frontend" {
			trimmed := strings.TrimPrefix(strings.TrimPrefix(path, "/frontend"), "/")
			if trimmed == "" || !strings.Contains(filepath.Base(trimmed), ".") {
				indexPath := filepath.Join(staticDir, "index.html")
				if _, err := os.Stat(indexPath); err == nil {
					c.File(indexPath)
					return
				}
			}
			http.StripPrefix("/frontend", cachedFileServer(http.Dir(staticDir))).ServeHTTP(c.Writer, c.Request)
			return
		}

		// SPA index fallback: for client-side navigation (non-file routes), serve index.html
		// (e.g. ./web/frontend/dist/client/browser/index.html).
		relPath := strings.TrimPrefix(path, "/")
		if relPath != "" && !strings.Contains(filepath.Base(relPath), ".") {
			indexPath := filepath.Join(staticDir, "index.html")
			if _, err := os.Stat(indexPath); err == nil {
				c.File(indexPath)
				return
			}
		}

		cachedFileServer(http.Dir(staticDir)).ServeHTTP(c.Writer, c.Request)
	})

	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok", "service": "frontend"})
	})

	return r
}

// htmlAwareUnauthorized handles unauthenticated requests differently
// depending on what the client expected:
//
//   - HTML requests (browsers loading the SPA shell): 302 to /login
//     so the user reaches the login page instead of seeing a raw
//     JSON error.
//   - Anything else (API, WebSocket, XHR): 401 application/json so
//     the SPA's AuthService can react in-place without a full nav.
//
// Detection is by Accept header — XHR/fetch from Angular sets
// Accept: application/json (we ensure this in AuthService).
func htmlAwareUnauthorized(c *gin.Context) {
	accept := c.GetHeader("Accept")
	wantsHTML := strings.Contains(accept, "text/html") ||
		// Initial document load from a bare URL.
		(c.Request.Method == http.MethodGet && accept == "")
	if wantsHTML {
		// Preserve where the user was going so /login can bounce
		// them back after a successful sign-in.
		dest := c.Request.URL.RequestURI()
		c.Redirect(http.StatusFound, "/login?redirect="+url.QueryEscape(dest))
		c.Abort()
		return
	}
	c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
		"error": "authentication required",
	})
}

// loginHandler serves the static login page. We intentionally keep it
// outside the Angular bundle so the auth flow has zero dependency on
// the SPA build pipeline (which means it works even when Angular fails
// to build, and avoids forcing Angular routing into an app that
// currently has none).
//
// Local-dev short-circuit (2026-06-22): in IDENTITY_PROVIDER=local
// mode the BFF's LocalResolver auto-authenticates every request, so
// the login page serves no purpose — anyone who lands on /login (e.g.
// from a stale bookmark) should be redirected straight to the SPA.
// This also removes the temptation to keep a "Local" branch in the
// login UI, which was historically a silent auth-bypass risk if it
// ever shipped to GCP. Production runs with IDENTITY_PROVIDER=firebase
// and still serves the real login page.
//
// Resolution order: prefer the built artifact (Angular 21 emits
// dist/client/browser/, older toolchains used dist/), then fall back
// to public/ (developer running straight from source without a build).
// Angular copies public/ → dist at build time so all three are
// equivalent — we just try them in order of likelihood.
func loginHandler(c *gin.Context) {
	if isLocalIdentityProvider() {
		// Honor ?redirect=… so a 302→/login→302→dest chain still
		// lands the user on the page they originally wanted. The
		// account middleware already validated the dest via its own
		// open-redirect guard; we re-validate here as defence in
		// depth because the local-dev short-circuit bypasses /login's
		// own redirect-sanitizer (login-app.js).
		dest := safeLocalLoginRedirect(c.Query("redirect"))
		c.Redirect(http.StatusFound, dest)
		return
	}
	c.Header("Cache-Control", "no-store")
	for _, candidate := range []string{
		"./web/frontend/dist/client/browser/login/index.html",
		"./web/frontend/dist/browser/login/index.html",
		"./web/frontend/dist/login/index.html",
		"./web/frontend/public/login/index.html",
	} {
		if _, err := os.Stat(candidate); err == nil {
			c.File(candidate)
			return
		}
	}
	c.String(http.StatusNotFound, "login page not found")
}

// isLocalIdentityProvider reports whether the BFF is configured with
// the in-process LocalResolver — the IDENTITY_PROVIDER env var is
// either empty (defaults to local in AccountResolverFromEnv) or
// explicitly "local". Mirrors the env contract documented at
// internal/middleware/account.go:AccountResolverFromEnv so the two
// stay in lockstep.
func isLocalIdentityProvider() bool {
	p := strings.ToLower(strings.TrimSpace(os.Getenv("IDENTITY_PROVIDER")))
	return p == "" || p == "local"
}

// safeLocalLoginRedirect sanitizes the ?redirect= param passed to
// /login when we short-circuit to / in local-dev mode. Mirrors the
// open-redirect guard in /login/login-app.js: same-origin path-only.
// Defaults to "/" on any rejection.
func safeLocalLoginRedirect(target string) string {
	target = strings.TrimSpace(target)
	if target == "" {
		return "/"
	}

	// Reject targets that do not start with a single '/' or begin with '//' or '/\'.
	if !strings.HasPrefix(target, "/") || strings.HasPrefix(target, "//") || strings.HasPrefix(target, "/\\") {
		return "/"
	}

	// Unescape/decode URL before inspection to prevent encoded open-redirect bypasses (e.g. /%2f%2fevil.com).
	decoded, err := url.QueryUnescape(target)
	if err != nil {
		decoded = target
	}
	decoded = strings.TrimSpace(decoded)
	if !strings.HasPrefix(decoded, "/") || strings.HasPrefix(decoded, "//") || strings.HasPrefix(decoded, "/\\") {
		return "/"
	}

	// Re-parse with url.Parse and ensure neither scheme nor host is present.
	u, err := url.Parse(target)
	if err != nil || u.Host != "" || u.Scheme != "" {
		return "/"
	}

	// Inspect decoded path from url.Parse as well.
	if strings.HasPrefix(u.Path, "//") || strings.HasPrefix(u.Path, "/\\") {
		return "/"
	}

	// Reject targets containing scheme/host delimiters before a path slash (e.g. javascript:, data:, etc.).
	clean := strings.TrimLeft(u.Path, "/")
	if idx := strings.IndexAny(clean, ":@\\"); idx != -1 {
		slashIdx := strings.Index(clean, "/")
		if slashIdx == -1 || idx < slashIdx {
			return "/"
		}
	}

	out := u.EscapedPath()
	if out == "" || !strings.HasPrefix(out, "/") || strings.HasPrefix(out, "//") || strings.HasPrefix(out, "/\\") {
		return "/"
	}
	if u.RawQuery != "" {
		out += "?" + u.RawQuery
	}
	if u.Fragment != "" {
		out += "#" + u.EscapedFragment()
	}
	return out
}

// htmlAwareNeedsInit is the BFF override of the account middleware's
// default 409 handler for the NeedsInit=true state (a verified
// Firebase identity that has no corresponding users-table row yet —
// the brand-new-signup window between createUserWithEmailAndPassword
// succeeding and /api/v1/me/init running).
//
// Path-based split (2026-06-22 fix-up — see below):
//
//   - /api/* and /ws* requests get the 409 JSON contract so the
//     SPA's identity.service.ts needs_init branch fires and POSTs
//     /api/v1/me/init. /api/v1/me, /api/v1/me/init,
//     /api/v1/auth/exchange and /api/v1/me/logout are in InitPaths
//     so they bypass this handler entirely.
//   - Everything else (the HTML doc, the Angular JS bundle, CSS,
//     favicons, webmanifest, fonts, images) is passed through to
//     the static file server so the SPA can actually boot. Without
//     this, the HTML shell loads but every subresource the browser
//     fetches (`Accept: */*` for JS, `text/css,*/*` for stylesheets,
//     `image/*` for favicons) tripped the previous Accept-header
//     check and got a 409 needs_init JSON instead of the asset,
//     leaving the SPA permanently unbooted and the user trapped on
//     the first page after sign-up. The user is already
//     authenticated at this point (resolver populated UserContext);
//     only NeedsInit=true differentiates them from a fully-set-up
//     user, and NeedsInit gates only matter for API contracts.
func htmlAwareNeedsInit(c *gin.Context) {
	p := c.Request.URL.Path
	if strings.HasPrefix(p, "/api/") ||
		strings.HasPrefix(p, "/frontend/api/") ||
		strings.HasPrefix(p, "/ws") ||
		strings.HasPrefix(p, "/frontend/ws") {
		c.AbortWithStatusJSON(http.StatusConflict, gin.H{
			"error":    "needs_init",
			"init_url": "/api/v1/me/init",
		})
		return
	}
	// HTML doc + every SPA subresource. Continues the gin handler
	// chain so the static file server downstream serves the asset.
	c.Next()
}

func main() {
	config.Load()
	logging.Setup()

	// G5b: distributed tracing. No-op when OTEL_EXPORTER is unset.
	tracingShutdown, err := tracing.SetupFromEnv(context.Background(), "race-frontend")
	if err != nil {
		log.Printf("⚠️ Tracing setup failed: %v (continuing without traces)", err)
	}
	defer func() { _ = tracingShutdown(context.Background()) }()

	port := config.Optional("PORT", "8080")
	if err := config.ValidatePort(port); err != nil {
		log.Fatalf("❌ Invalid PORT: %v", err)
	}

	gatewayURL := config.Optional("GATEWAY_INTERNAL_URL",
		config.Optional("GATEWAY_URL", ""))
	oidcAudience := auth.ResolveAudience(gatewayURL)

	// Per-user identity resolver. Built at boot so a
	// misconfigured production deploy fails immediately, not on
	// first request.
	accountResolver, err := middleware.AccountResolverFromEnv()
	if err != nil {
		log.Fatalf("FRONTEND: account resolver: %v", err)
	}

	// When the FirebaseResolver is in use, give it a UserLookup
	// closure so claims-to-user resolution can hydrate from
	// Postgres. Best-effort: a DB outage at boot leaves the
	// resolver in degraded mode (returns NeedsInit for users
	// without a row yet), which is the correct fallback.
	var frontendDBPool *pgxpool.Pool
	if fr, ok := accountResolver.(*account.FirebaseResolver); ok {
		connStr := buildTenantConnStr()
		if connStr == "" {
			log.Printf("FRONTEND: no DATABASE_URL/ALLOYDB_* set; user lookup disabled")
		} else {
			// 2026-06-21 audit P1 #1: runtime lookups go through a
			// pool because *pgx.Conn is NOT safe for concurrent
			// goroutines. Migration uses a session-level
			// pg_advisory_lock that is tied to a single connection,
			// so the migration step must run on a temporary
			// *pgx.Conn before we open the runtime pool.
			bctx, bcancel := context.WithTimeout(context.Background(), 5*time.Second)
			migConn, cerr := pgx.Connect(bctx, connStr)
			bcancel()
			if cerr != nil {
				log.Printf("FRONTEND: account DB connect failed; user lookup disabled: %v", cerr)
			} else {
				// Apply the per-user schema before any lookups. The
				// gateway/admin/billing services also call this on
				// cold start; the advisory lock + IF NOT EXISTS
				// SQL make concurrent applies safe. Adding it here
				// removes the cold-start race where the frontend
				// receives its first request before any of the
				// other 3 services have booted, which previously
				// produced `relation "users" does not exist` 302s
				// on /login (the symptom that motivated this fix).
				mctx, mcancel := context.WithTimeout(context.Background(), 30*time.Second)
				mErr := account.ApplyMigration(mctx, migConn)
				mcancel()
				closeCtx, closeCancel := context.WithTimeout(context.Background(), 2*time.Second)
				_ = migConn.Close(closeCtx)
				closeCancel()
				if mErr != nil {
					log.Printf("FRONTEND: account migration failed; user lookup disabled: %v", mErr)
				} else {
					pctx, pcancel := context.WithTimeout(context.Background(), 5*time.Second)
					pool, perr := pgxpool.New(pctx, connStr)
					pcancel()
					if perr != nil {
						log.Printf("FRONTEND: account DB pool open failed; user lookup disabled: %v", perr)
					} else {
						frontendDBPool = pool
						store := account.NewStore(pool)
						// P1.A (2026-06-22): use GetUserAnyState so the
						// resolver can return ErrAccountDeleted (→ 410)
						// for soft-deleted users instead of treating
						// them as NeedsInit and looping them through
						// /me/init.
						fr.WithLookup(store.GetUserAnyState)
						log.Printf("FRONTEND: Firebase resolver wired with user lookup (pgxpool)")
					}
				}
			}
		}
	}
	if frontendDBPool != nil {
		defer frontendDBPool.Close()
	}

	r := setupRouter(oidcAudience, accountResolver)
	log.Printf("Frontend proxy running on port %s", port)
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           r,
		ReadTimeout:       15 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Failed to run frontend proxy: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("Shutting down frontend proxy...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal("Frontend proxy forced to shutdown:", err)
	}

	log.Println("Frontend proxy exiting")
}
