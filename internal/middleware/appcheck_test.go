// Copyright 2026 Christian Haeussinger. Apache-2.0.
//
// Tests for the App Check gin middleware (2026-06-22 audit P2.B).
// We don't test signature verification here (covered by
// account/appcheck_test.go); we focus on mode dispatch (off vs
// report-only vs enforce) and skip-path handling.

package middleware

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

func init() { gin.SetMode(gin.TestMode) }

func newAppCheckTestRouter(opts AppCheckOptions) *gin.Engine {
	r := gin.New()
	r.Use(AppCheck(opts))
	r.GET("/health", func(c *gin.Context) { c.String(http.StatusOK, "ok") })
	r.GET("/api/v1/me", func(c *gin.Context) { c.String(http.StatusOK, "me") })
	return r
}

func TestAppCheck_OffIsNoop(t *testing.T) {
	r := newAppCheckTestRouter(AppCheckOptions{Mode: "off"})
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d (want 200)", w.Code)
	}
}

func TestAppCheck_EnforceNilVerifierFailsClosed(t *testing.T) {
	r := newAppCheckTestRouter(AppCheckOptions{
		Mode:      "enforce",
		Verifier:  nil,
		SkipPaths: []string{"/health"},
	})

	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d (want 503 fail-closed when enforce has nil verifier), body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "appcheck_unavailable") {
		t.Fatalf("body = %q (want appcheck_unavailable)", w.Body.String())
	}

	// Skipped paths like /health still pass through.
	wHealth := httptest.NewRecorder()
	r.ServeHTTP(wHealth, httptest.NewRequest(http.MethodGet, "/health", nil))
	if wHealth.Code != http.StatusOK {
		t.Fatalf("/health status = %d (want 200)", wHealth.Code)
	}
}

func TestAppCheck_ReportOnlyAllowsMissingHeader(t *testing.T) {
	// Verifier is set but the JWKS server URL doesn't matter — the
	// middleware short-circuits on missing header before consulting it.
	v := &account.AppCheckVerifier{}
	r := newAppCheckTestRouter(AppCheckOptions{Mode: "report-only", Verifier: v})
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d (want 200 in report-only)", w.Code)
	}
}

func TestAppCheck_EnforceRejectsMissingHeader(t *testing.T) {
	v := &account.AppCheckVerifier{}
	r := newAppCheckTestRouter(AppCheckOptions{Mode: "enforce", Verifier: v})
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d (want 401)", w.Code)
	}
	if !strings.Contains(w.Body.String(), "appcheck_required") {
		t.Fatalf("body = %q (want appcheck_required)", w.Body.String())
	}
}

func TestAppCheck_SkipPathsBypassEnforce(t *testing.T) {
	v := &account.AppCheckVerifier{}
	r := newAppCheckTestRouter(AppCheckOptions{
		Mode:      "enforce",
		Verifier:  v,
		SkipPaths: []string{"/health"},
	})
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d (want 200 on skipped path)", w.Code)
	}
}

func TestAppCheck_OptionsPreflightSkipped(t *testing.T) {
	v := &account.AppCheckVerifier{}
	r := newAppCheckTestRouter(AppCheckOptions{Mode: "enforce", Verifier: v})
	// gin doesn't auto-register OPTIONS; add a handler so the
	// middleware runs and we can confirm it lets the preflight pass
	// through without an App Check header.
	r.OPTIONS("/api/v1/me", func(c *gin.Context) { c.Status(http.StatusNoContent) })
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodOptions, "/api/v1/me", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusNoContent {
		t.Fatalf("status = %d (want 204 — preflight bypasses App Check)", w.Code)
	}
}

func TestAppCheckFromEnv_OffByDefault(t *testing.T) {
	env := map[string]string{}
	get := func(k string) string { return env[k] }
	opts := AppCheckFromEnv(get)
	if opts.Mode != "off" {
		t.Fatalf("mode = %q (want off)", opts.Mode)
	}
}

func TestAppCheckFromEnv_PreservesModeWhenProjectMissing(t *testing.T) {
	env := map[string]string{"APPCHECK_ENFORCE": "report-only"}
	get := func(k string) string { return env[k] }
	opts := AppCheckFromEnv(get)
	if opts.Mode != "report-only" {
		t.Fatalf("mode = %q (want preserved report-only)", opts.Mode)
	}
	if opts.InitErr == nil {
		t.Fatal("expected InitErr when project number/id missing")
	}
	if _, err := AppCheckFromEnvE(get); err == nil {
		t.Fatal("expected AppCheckFromEnvE error when project missing")
	}
}

func TestAppCheckFromEnv_HappyConfig(t *testing.T) {
	env := map[string]string{
		"APPCHECK_ENFORCE":        "report-only",
		"FIREBASE_PROJECT_NUMBER": "12345",
		"FIREBASE_PROJECT_ID":     "my-project",
	}
	get := func(k string) string { return env[k] }
	opts := AppCheckFromEnv(get)
	if opts.Mode != "report-only" {
		t.Fatalf("mode = %q", opts.Mode)
	}
	if opts.Verifier == nil {
		t.Fatal("verifier is nil")
	}
	if len(opts.SkipPaths) == 0 {
		t.Fatal("skip paths empty")
	}
}

func TestAppCheckFromEnv_UnknownModePreserved(t *testing.T) {
	env := map[string]string{
		"APPCHECK_ENFORCE":        "yes-please",
		"FIREBASE_PROJECT_NUMBER": "12345",
	}
	get := func(k string) string { return env[k] }
	opts := AppCheckFromEnv(get)
	if opts.Mode != "yes-please" {
		t.Fatalf("mode = %q (want yes-please preserved for AppCheck boot guard)", opts.Mode)
	}
	if opts.InitErr == nil {
		t.Fatal("expected InitErr for unknown mode")
	}
	if _, err := AppCheckFromEnvE(get); err == nil {
		t.Fatal("expected AppCheckFromEnvE error for unknown mode")
	}
}

func TestAppCheckFromEnv_AllowsExplicitOff(t *testing.T) {
	for _, val := range []string{"", "off", "false", "0"} {
		opts, err := AppCheckFromEnvE(func(k string) string {
			if k == "APPCHECK_ENFORCE" {
				return val
			}
			return ""
		})
		if err != nil {
			t.Fatalf("val=%q unexpected err: %v", val, err)
		}
		if opts.Mode != "off" {
			t.Fatalf("val=%q mode=%q want off", val, opts.Mode)
		}
	}
}

func TestAppCheck_SkipPathsSegmentBoundary(t *testing.T) {
	v := &account.AppCheckVerifier{}
	r := gin.New()
	r.Use(AppCheck(AppCheckOptions{
		Mode:      "enforce",
		Verifier:  v,
		SkipPaths: []string{"/health"},
	}))
	r.GET("/health", func(c *gin.Context) { c.String(http.StatusOK, "ok") })
	r.GET("/health/ready", func(c *gin.Context) { c.String(http.StatusOK, "ready") })
	r.GET("/healthcare", func(c *gin.Context) { c.String(http.StatusOK, "oops") })

	for _, path := range []string{"/health", "/health/ready"} {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
		if w.Code != http.StatusOK {
			t.Fatalf("GET %s status = %d (want 200)", path, w.Code)
		}
	}

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthcare", nil))
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("GET /healthcare status = %d (want 401; /health must not prefix-match /healthcare)", w.Code)
	}
}

func TestAppCheckFromEnv_SkipsWebSocketAndHealthz(t *testing.T) {
	env := map[string]string{
		"APPCHECK_ENFORCE":        "enforce",
		"FIREBASE_PROJECT_NUMBER": "12345",
		"FIREBASE_PROJECT_ID":     "my-project",
	}
	opts := AppCheckFromEnv(func(k string) string { return env[k] })
	for _, want := range []string{"/health", "/healthz", "/config", "/ws"} {
		found := false
		for _, p := range opts.SkipPaths {
			if p == want {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("SkipPaths %v missing %q", opts.SkipPaths, want)
		}
	}
}

func TestAppCheck_SkipPathsTrailingSlashNormalized(t *testing.T) {
	v := &account.AppCheckVerifier{}
	r := gin.New()
	r.RedirectTrailingSlash = false
	r.Use(AppCheck(AppCheckOptions{
		Mode:      "enforce",
		Verifier:  v,
		SkipPaths: []string{"/health/"},
	}))
	r.GET("/health", func(c *gin.Context) { c.String(http.StatusOK, "ok") })
	r.GET("/health/", func(c *gin.Context) { c.String(http.StatusOK, "ok") })
	r.GET("/health/ready", func(c *gin.Context) { c.String(http.StatusOK, "ready") })
	r.GET("/healthcare", func(c *gin.Context) { c.String(http.StatusOK, "oops") })

	for _, p := range []string{"/health", "/health/", "/health/ready"} {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, p, nil))
		if w.Code != http.StatusOK {
			t.Fatalf("GET %s status = %d (want 200 when SkipPaths has /health/)", p, w.Code)
		}
	}

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthcare", nil))
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("GET /healthcare status = %d (want 401)", w.Code)
	}
}
