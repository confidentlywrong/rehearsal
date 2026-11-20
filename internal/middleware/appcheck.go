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
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// AppCheckHeader is the canonical request header. Matches the name
// the Firebase Admin SDKs and client SDKs use.
const AppCheckHeader = "X-Firebase-AppCheck"

// AppCheckOptions configures the middleware. Construct via
// AppCheckFromEnv / AppCheckFromEnvE in production paths; tests can
// build it directly.
type AppCheckOptions struct {
	// Verifier validates the JWT. When Mode is "enforce" and Verifier
	// is nil (or InitErr != nil), the middleware fails closed with
	// HTTP 503 appcheck_unavailable instead of silently degrading to
	// a no-op.
	Verifier *account.AppCheckVerifier
	// Mode is "off" | "report-only" | "enforce". Anything else aborts
	// at AppCheck() boot time (B-21) to prevent silent typos.
	Mode string
	// SkipPaths is a list of URL-path prefixes that bypass App
	// Check verification entirely. /health and /metrics are common
	// candidates because load balancers and Prom scrapers can't
	// produce App Check tokens.
	SkipPaths []string
	// InitErr records any configuration/initialization failure from
	// AppCheckFromEnv so AppCheck can fail closed in enforce mode.
	InitErr error
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
	if mode == "off" || mode == "" {
		return func(c *gin.Context) { c.Next() }
	}
	enforce := mode == "enforce"
	skips := append([]string{}, opts.SkipPaths...)
	if opts.Verifier == nil || opts.InitErr != nil {
		log.Printf("APPCHECK: CRITICAL misconfiguration mode=%s verifier_nil=%v init_err=%v",
			mode, opts.Verifier == nil, opts.InitErr)
		if enforce {
			return func(c *gin.Context) {
				if c.Request.Method == http.MethodOptions {
					c.Next()
					return
				}
				reqPath := cleanRequestPath(c.Request)
				for _, p := range skips {
					if matchesSkipPath(reqPath, p) {
						c.Next()
						return
					}
				}
				c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{
					"error": "appcheck_unavailable",
					"hint":  "App Check enforcement is enabled but verifier is unavailable",
				})
			}
		}
		return func(c *gin.Context) { c.Next() }
	}
	return func(c *gin.Context) {
		// CORS preflights never carry custom headers; skip.
		if c.Request.Method == http.MethodOptions {
			c.Next()
			return
		}
		reqPath := cleanRequestPath(c.Request)
		for _, p := range skips {
			if matchesSkipPath(reqPath, p) {
				c.Next()
				return
			}
		}
		tok := c.Request.Header.Get(AppCheckHeader)
		if tok == "" {
			if enforce {
				log.Printf("APPCHECK: enforce: reject %s %s: missing header (ip=%s)",
					c.Request.Method, reqPath, c.ClientIP())
				c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
					"error": "appcheck_required",
					"hint":  "missing X-Firebase-AppCheck header",
				})
				return
			}
			log.Printf("APPCHECK: report-only: missing header %s %s (ip=%s)",
				c.Request.Method, reqPath, c.ClientIP())
			c.Next()
			return
		}
		if _, err := opts.Verifier.Verify(c.Request.Context(), tok); err != nil {
			if enforce {
				log.Printf("APPCHECK: enforce: reject %s %s: %v", c.Request.Method, reqPath, err)
				c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
					"error": "appcheck_invalid",
					"hint":  "invalid X-Firebase-AppCheck token",
				})
				return
			}
			log.Printf("APPCHECK: report-only: invalid token %s %s: %v",
				c.Request.Method, reqPath, err)
		}
		c.Next()
	}
}

var defaultAppCheckSkipPaths = []string{
	"/health",
	"/healthz",
	"/api/v1/health",
	"/metrics",
	"/config",
	"/ws",
}

// AppCheckFromEnvE reads APPCHECK_ENFORCE plus FIREBASE_PROJECT_NUMBER
// (preferred) or FIREBASE_PROJECT_ID and returns AppCheckOptions plus any
// configuration/initialization error. Unknown modes and missing/broken
// verifier configuration are preserved on the returned AppCheckOptions
// (rather than silently downgraded to "off") so AppCheck() fails closed.
func AppCheckFromEnvE(getenv func(string) string) (AppCheckOptions, error) {
	if getenv == nil {
		getenv = os.Getenv
	}
	mode := strings.ToLower(strings.TrimSpace(getenv("APPCHECK_ENFORCE")))
	switch mode {
	case "", "off", "false", "0":
		return AppCheckOptions{Mode: "off"}, nil
	case "true", "1":
		mode = "enforce"
	case "report-only", "enforce":
		// ok
	default:
		err := fmt.Errorf("APPCHECK: unknown APPCHECK_ENFORCE=%q (want off|report-only|enforce)", mode)
		return AppCheckOptions{
			Mode:      mode,
			SkipPaths: append([]string{}, defaultAppCheckSkipPaths...),
			InitErr:   err,
		}, err
	}
	projectNumber := strings.TrimSpace(getenv("FIREBASE_PROJECT_NUMBER"))
	projectID := strings.TrimSpace(getenv("FIREBASE_PROJECT_ID"))
	if projectID == "" {
		projectID = strings.TrimSpace(getenv("GOOGLE_CLOUD_PROJECT"))
	}
	if projectNumber == "" && projectID == "" {
		err := fmt.Errorf("APPCHECK: %s mode requested but FIREBASE_PROJECT_NUMBER/FIREBASE_PROJECT_ID/GOOGLE_CLOUD_PROJECT all empty", mode)
		return AppCheckOptions{
			Mode:      mode,
			SkipPaths: append([]string{}, defaultAppCheckSkipPaths...),
			InitErr:   err,
		}, err
	}
	v, err := account.NewAppCheckVerifier(account.AppCheckConfig{
		ProjectNumber: projectNumber,
		ProjectID:     projectID,
		HTTPClient:    &http.Client{Timeout: 5 * time.Second},
	})
	if err != nil {
		wrapped := fmt.Errorf("APPCHECK: verifier init failed: %w", err)
		return AppCheckOptions{
			Mode:      mode,
			SkipPaths: append([]string{}, defaultAppCheckSkipPaths...),
			InitErr:   wrapped,
		}, wrapped
	}
	if v == nil {
		err := errors.New("APPCHECK: verifier init returned nil")
		return AppCheckOptions{
			Mode:      mode,
			SkipPaths: append([]string{}, defaultAppCheckSkipPaths...),
			InitErr:   err,
		}, err
	}
	log.Printf("APPCHECK: enabled mode=%s project_number=%q project_id=%q",
		mode, projectNumber, projectID)
	return AppCheckOptions{
		Verifier:  v,
		Mode:      mode,
		SkipPaths: append([]string{}, defaultAppCheckSkipPaths...),
	}, nil
}

// AppCheckFromEnv reads APPCHECK_ENFORCE plus FIREBASE_PROJECT_NUMBER
// (preferred) or FIREBASE_PROJECT_ID and returns AppCheckOptions.
// Configuration errors are logged and preserved in opts.InitErr / opts.Mode
// rather than silently downgrading to "off".
func AppCheckFromEnv(getenv func(string) string) AppCheckOptions {
	opts, err := AppCheckFromEnvE(getenv)
	if err != nil {
		log.Printf("%v", err)
	}
	return opts
}

func cleanRequestPath(r *http.Request) string {
	if r == nil || r.URL == nil || r.URL.Path == "" {
		return "/"
	}
	return path.Clean(r.URL.Path)
}

func matchesSkipPath(reqPath, skip string) bool {
	if skip == "" {
		return false
	}
	skip = path.Clean(skip)
	if skip == "/" {
		return reqPath == "/"
	}
	if reqPath == skip {
		return true
	}
	prefix := strings.TrimSuffix(skip, "/") + "/"
	return strings.HasPrefix(reqPath, prefix)
}
