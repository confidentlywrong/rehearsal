// Copyright 2026 Christian Haeussinger. Apache-2.0.

package middleware

// Firebase App Check middleware (2026-06-22 audit, item P2.B).
//
// The SPA and /login page already obtain an App Check JWT via
// ReCaptchaEnterpriseProvider and attach it as the
// `X-Firebase-AppCheck` header on /api/* fetches. This middleware
// is the server-side counterpart: it parses the header and rejects
// (or just logs) requests whose App Check token is missing or
// invalid.
//
// Three modes, controlled by APPCHECK_ENFORCE env var:
//
//	off (default)  — middleware is a no-op. Existing deployments
//	                 keep working unchanged.
//	report-only    — missing/invalid tokens log a WARN line but the
//	                 request still proceeds. Use this for at least
//	                 one production rollout cycle so you can spot
//	                 paths/clients that aren't sending the header
//	                 yet (e.g. the login page during the first
//	                 deploy after enabling SPA support).
//	enforce        — missing/invalid tokens cause 401 with
//	                 {"error":"appcheck_required"|"appcheck_invalid"}.
//
// Mounted BEFORE middleware.Account in cmd/gateway/main.go so an
// attacker who somehow gets a valid Firebase ID token via XSS can't
// replay it against /me/* etc. without also stealing a fresh App
// Check JWT (which is bound to the registered app + reCAPTCHA
// challenge).

import (
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// AppCheckHeader is the canonical request header. Matches the name
// the Firebase Admin SDKs and client SDKs use.
const AppCheckHeader = "X-Firebase-AppCheck"

// AppCheckOptions configures the middleware. Construct via
// AppCheckFromEnv in production paths; tests can build it directly.
type AppCheckOptions struct {
	// Verifier validates the JWT. If nil the middleware degrades to
	// a no-op regardless of Mode (so a misconfigured deploy can't
	// hard-fail every request).
	Verifier *account.AppCheckVerifier
	// Mode is "off" | "report-only" | "enforce". Anything else is
	// treated as "off" with a startup warning.
	Mode string
	// SkipPaths is a list of URL-path prefixes that bypass App
	// Check verification entirely. /health and /metrics are common
	// candidates because load balancers and Prom scrapers can't
	// produce App Check tokens.
	SkipPaths []string
}

// AppCheck returns a gin middleware honouring opts. Reentrant-safe;
// pass the same handler to multiple Engine instances if desired.
//
// B-21 (2026-06-23 audit): unknown mode values used to silently
// degrade to report-only because the code only matched "enforce"
// explicitly. A typo like APPCHECK_ENFORCE=enabled would therefore
// log warnings while accepting all traffic — invisible to the
// operator who believed they had enabled enforcement. The mode is
// now validated against an allow-list and the process aborts on
// an unknown value so the operator notices at boot.
func AppCheck(opts AppCheckOptions) gin.HandlerFunc {
	mode := strings.ToLower(strings.TrimSpace(opts.Mode))
	switch mode {
	case "", "off", "report-only", "enforce":
		// recognised; carry on
	default:
		log.Fatalf("APPCHECK: unknown APPCHECK_ENFORCE=%q. Allowed: off | report-only | enforce (or empty for off). Refusing to start to avoid silent enforcement degradation.", opts.Mode)
	}
	if mode == "off" || mode == "" || opts.Verifier == nil {
		return func(c *gin.Context) { c.Next() }
	}
	enforce := mode == "enforce"
	skips := append([]string{}, opts.SkipPaths...)
	return func(c *gin.Context) {
		// CORS preflights never carry custom headers; skip.
		if c.Request.Method == http.MethodOptions {
			c.Next()
			return
		}
		path := c.Request.URL.Path
		for _, p := range skips {
			if p != "" && strings.HasPrefix(path, p) {
				c.Next()
				return
			}
		}
		tok := c.Request.Header.Get(AppCheckHeader)
		if tok == "" {
			if enforce {
				log.Printf("APPCHECK: enforce: reject %s %s: missing header (ip=%s)",
					c.Request.Method, path, c.ClientIP())
				c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
					"error": "appcheck_required",
					"hint":  "missing X-Firebase-AppCheck header",
				})
				return
			}
			log.Printf("APPCHECK: report-only: missing header %s %s (ip=%s)",
				c.Request.Method, path, c.ClientIP())
			c.Next()
			return
		}
		if _, err := opts.Verifier.Verify(c.Request.Context(), tok); err != nil {
			if enforce {
				log.Printf("APPCHECK: enforce: reject %s %s: %v", c.Request.Method, path, err)
				c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
					"error": "appcheck_invalid",
					"hint":  "invalid X-Firebase-AppCheck token",
				})
				return
			}
			log.Printf("APPCHECK: report-only: invalid token %s %s: %v",
				c.Request.Method, path, err)
		}
		c.Next()
	}
}

// AppCheckFromEnv reads APPCHECK_ENFORCE plus FIREBASE_PROJECT_NUMBER
// (preferred) or FIREBASE_PROJECT_ID and returns a ready
// AppCheckOptions. Returns {Mode:"off"} on any config error so a
// typo can't bring the gateway down — log lines explain why the
// middleware degraded.
func AppCheckFromEnv(getenv func(string) string) AppCheckOptions {
	if getenv == nil {
		getenv = os.Getenv
	}
	mode := strings.ToLower(strings.TrimSpace(getenv("APPCHECK_ENFORCE")))
	switch mode {
	case "", "off", "false", "0":
		return AppCheckOptions{Mode: "off"}
	case "true", "1":
		mode = "enforce"
	case "report-only", "enforce":
		// ok
	default:
		log.Printf("APPCHECK: unknown APPCHECK_ENFORCE=%q (want off|report-only|enforce); defaulting to off", mode)
		return AppCheckOptions{Mode: "off"}
	}
	projectNumber := strings.TrimSpace(getenv("FIREBASE_PROJECT_NUMBER"))
	projectID := strings.TrimSpace(getenv("FIREBASE_PROJECT_ID"))
	if projectID == "" {
		projectID = strings.TrimSpace(getenv("GOOGLE_CLOUD_PROJECT"))
	}
	if projectNumber == "" && projectID == "" {
		log.Printf("APPCHECK: %s mode requested but FIREBASE_PROJECT_NUMBER/FIREBASE_PROJECT_ID both empty; disabling", mode)
		return AppCheckOptions{Mode: "off"}
	}
	v, err := account.NewAppCheckVerifier(account.AppCheckConfig{
		ProjectNumber: projectNumber,
		ProjectID:     projectID,
		HTTPClient:    &http.Client{Timeout: 5 * time.Second},
	})
	if err != nil {
		log.Printf("APPCHECK: verifier init failed: %v; disabling", err)
		return AppCheckOptions{Mode: "off"}
	}
	log.Printf("APPCHECK: enabled mode=%s project_number=%q project_id=%q",
		mode, projectNumber, projectID)
	return AppCheckOptions{
		Verifier: v,
		Mode:     mode,
		SkipPaths: []string{
			"/health",
			"/api/v1/health",
			"/metrics",
		},
	}
}
