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

func TestAppCheck_NilVerifierIsNoop(t *testing.T) {
	r := newAppCheckTestRouter(AppCheckOptions{Mode: "enforce", Verifier: nil})
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d (want 200 — nil verifier degrades to off)", w.Code)
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

func TestAppCheckFromEnv_DisablesWhenProjectMissing(t *testing.T) {
	env := map[string]string{"APPCHECK_ENFORCE": "report-only"}
	get := func(k string) string { return env[k] }
	opts := AppCheckFromEnv(get)
	if opts.Mode != "off" {
		t.Fatalf("mode = %q (want off — no project number/id)", opts.Mode)
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

func TestAppCheckFromEnv_UnknownModeDisables(t *testing.T) {
	env := map[string]string{
		"APPCHECK_ENFORCE":        "yes-please",
		"FIREBASE_PROJECT_NUMBER": "12345",
	}
	get := func(k string) string { return env[k] }
	opts := AppCheckFromEnv(get)
	if opts.Mode != "off" {
		t.Fatalf("mode = %q (want off for unknown)", opts.Mode)
	}
}
