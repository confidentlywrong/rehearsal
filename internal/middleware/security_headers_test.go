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

package middleware

import (
	"crypto/tls"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func init() {
	gin.SetMode(gin.TestMode)
}

// newSecRouter wires the SecurityHeaders middleware in front of a
// trivial 200-OK handler so tests can assert on the response headers.
func newSecRouter(cfg SecurityHeadersConfig) *gin.Engine {
	r := gin.New()
	r.Use(SecurityHeaders(cfg))
	r.GET("/x", func(c *gin.Context) { c.Status(200) })
	return r
}

func TestSecurityHeaders_AlwaysOnHeaders(t *testing.T) {
	r := newSecRouter(SecurityHeadersConfig{})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	r.ServeHTTP(w, req)

	if got := w.Header().Get("X-Content-Type-Options"); got != "nosniff" {
		t.Errorf("X-Content-Type-Options = %q, want nosniff", got)
	}
	if got := w.Header().Get("X-Frame-Options"); got != "DENY" {
		t.Errorf("X-Frame-Options = %q, want DENY", got)
	}
}

func TestSecurityHeaders_CSPReportOnlyByDefault(t *testing.T) {
	r := newSecRouter(SecurityHeadersConfig{
		ContentSecurityPolicy: "default-src 'self'",
		ReportOnly:            true,
	})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	r.ServeHTTP(w, req)

	if got := w.Header().Get("Content-Security-Policy-Report-Only"); got == "" {
		t.Error("expected Content-Security-Policy-Report-Only header")
	}
	if got := w.Header().Get("Content-Security-Policy"); got != "" {
		t.Errorf("must not emit enforcing CSP when ReportOnly=true, got %q", got)
	}
}

func TestSecurityHeaders_CSPEnforceWhenReportOnlyFalse(t *testing.T) {
	r := newSecRouter(SecurityHeadersConfig{
		ContentSecurityPolicy: "default-src 'self'",
		ReportOnly:            false,
	})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	r.ServeHTTP(w, req)

	if got := w.Header().Get("Content-Security-Policy"); got == "" {
		t.Error("expected enforcing Content-Security-Policy header")
	}
	if got := w.Header().Get("Content-Security-Policy-Report-Only"); got != "" {
		t.Errorf("must not emit -Report-Only when ReportOnly=false, got %q", got)
	}
}

func TestSecurityHeaders_CSPSkippedWhenEmpty(t *testing.T) {
	r := newSecRouter(SecurityHeadersConfig{ContentSecurityPolicy: ""})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	r.ServeHTTP(w, req)

	if got := w.Header().Get("Content-Security-Policy"); got != "" {
		t.Errorf("CSP must be empty when ContentSecurityPolicy is unset, got %q", got)
	}
	if got := w.Header().Get("Content-Security-Policy-Report-Only"); got != "" {
		t.Errorf("CSP-Report-Only must be empty when ContentSecurityPolicy is unset, got %q", got)
	}
}

func TestSecurityHeaders_HSTSOnlyOnHTTPS(t *testing.T) {
	cfg := SecurityHeadersConfig{EnableHSTS: true}
	r := newSecRouter(cfg)

	// HTTP request: must NOT emit HSTS (would pin localhost to https).
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	r.ServeHTTP(w, req)
	if got := w.Header().Get("Strict-Transport-Security"); got != "" {
		t.Errorf("HSTS must not be emitted on plain HTTP, got %q", got)
	}

	// X-Forwarded-Proto=https: simulates Cloud Run behind the LB.
	w = httptest.NewRecorder()
	req = httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("X-Forwarded-Proto", "https")
	r.ServeHTTP(w, req)
	if got := w.Header().Get("Strict-Transport-Security"); got == "" {
		t.Error("HSTS must be emitted when X-Forwarded-Proto=https")
	}

	// r.TLS set: simulates direct TLS termination at the binary.
	w = httptest.NewRecorder()
	req = httptest.NewRequest("GET", "/x", nil)
	req.TLS = &tls.ConnectionState{}
	r.ServeHTTP(w, req)
	if got := w.Header().Get("Strict-Transport-Security"); got == "" {
		t.Error("HSTS must be emitted when r.TLS is set")
	}
}

func TestSecurityHeaders_HSTSDisabledByDefault(t *testing.T) {
	r := newSecRouter(SecurityHeadersConfig{EnableHSTS: false})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("X-Forwarded-Proto", "https")
	r.ServeHTTP(w, req)
	if got := w.Header().Get("Strict-Transport-Security"); got != "" {
		t.Errorf("HSTS must stay off when EnableHSTS=false, got %q", got)
	}
}

func TestSecurityHeaders_PermissionsAndReferrer(t *testing.T) {
	cfg := SecurityHeadersConfig{
		PermissionsPolicy: "camera=()",
		ReferrerPolicy:    "no-referrer",
	}
	r := newSecRouter(cfg)
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	r.ServeHTTP(w, req)

	if got := w.Header().Get("Permissions-Policy"); got != "camera=()" {
		t.Errorf("Permissions-Policy = %q, want camera=()", got)
	}
	if got := w.Header().Get("Referrer-Policy"); got != "no-referrer" {
		t.Errorf("Referrer-Policy = %q, want no-referrer", got)
	}
}

// TestSecurityHeadersFromEnv exercises the env-var contract that
// every service main() relies on.
func TestSecurityHeadersFromEnv(t *testing.T) {
	t.Run("defaults", func(t *testing.T) {
		t.Setenv("CSP_ENFORCE", "")
		t.Setenv("CSP_OVERRIDE", "")
		t.Setenv("CSP_DISABLE", "")
		t.Setenv("SECURITY_HSTS", "")
		cfg := SecurityHeadersFromEnv()
		if !cfg.ReportOnly {
			t.Error("CSP must default to report-only mode")
		}
		if cfg.EnableHSTS {
			t.Error("HSTS must default to off")
		}
		if cfg.ContentSecurityPolicy == "" {
			t.Error("CSP must default to the DefaultCSP body")
		}
		if !strings.Contains(cfg.ContentSecurityPolicy, "frame-ancestors 'none'") {
			t.Error("default CSP must include frame-ancestors 'none'")
		}
	})
	t.Run("enforce", func(t *testing.T) {
		t.Setenv("CSP_ENFORCE", "true")
		cfg := SecurityHeadersFromEnv()
		if cfg.ReportOnly {
			t.Error("CSP_ENFORCE=true must flip ReportOnly to false")
		}
	})
	t.Run("override", func(t *testing.T) {
		t.Setenv("CSP_OVERRIDE", "default-src 'none'")
		cfg := SecurityHeadersFromEnv()
		if cfg.ContentSecurityPolicy != "default-src 'none'" {
			t.Errorf("CSP_OVERRIDE not applied: %q", cfg.ContentSecurityPolicy)
		}
	})
	t.Run("disable", func(t *testing.T) {
		t.Setenv("CSP_DISABLE", "true")
		cfg := SecurityHeadersFromEnv()
		if cfg.ContentSecurityPolicy != "" {
			t.Errorf("CSP_DISABLE=true must empty the CSP, got %q",
				cfg.ContentSecurityPolicy)
		}
	})
	t.Run("hsts", func(t *testing.T) {
		t.Setenv("SECURITY_HSTS", "true")
		cfg := SecurityHeadersFromEnv()
		if !cfg.EnableHSTS {
			t.Error("SECURITY_HSTS=true must enable HSTS")
		}
	})
}

func TestDefaultCSP_IncludesKnownProductionSources(t *testing.T) {
	csp := DefaultCSP()
	required := []string{
		"https://www.gstatic.com", // Firebase Web SDK on /login
		"frame-ancestors 'none'",  // clickjacking
		"object-src 'none'",       // legacy plugin XSS
		"base-uri 'self'",         // <base> tag injection
	}
	for _, want := range required {
		if !strings.Contains(csp, want) {
			t.Errorf("DefaultCSP missing required clause %q in %q", want, csp)
		}
	}
}

// guard against an obvious regression: nil request must not panic.
func TestRequestIsHTTPS_NilSafe(t *testing.T) {
	if requestIsHTTPS(nil) {
		t.Error("nil request must report HTTP")
	}
	r := httptest.NewRequest("GET", "/", nil)
	if requestIsHTTPS(r) {
		t.Error("plain request must report HTTP")
	}
	r.Header.Set("X-Forwarded-Proto", "https")
	if !requestIsHTTPS(r) {
		t.Error("X-Forwarded-Proto=https must report HTTPS")
	}
}

// guard against an obvious regression: empty Content-Type response
// from a downstream handler must still get nosniff.
func TestSecurityHeaders_HandlerRespects200Path(t *testing.T) {
	r := newSecRouter(SecurityHeadersConfig{})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", w.Code)
	}
}
