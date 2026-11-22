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
	"bytes"
	"crypto/tls"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
)

func TestR4_RequestIsHTTPS_MultiHopAndSpoofing(t *testing.T) {
	t.Parallel()

	if RequestIsHTTPS(nil) {
		t.Fatal("nil request must return false")
	}

	tlsReq := httptest.NewRequest(http.MethodGet, "http://example.com/", nil)
	tlsReq.TLS = &tls.ConnectionState{}
	if !RequestIsHTTPS(tlsReq) {
		t.Fatal("direct TLS connection must return true")
	}

	cases := []struct {
		name    string
		headers []string
		want    bool
	}{
		{name: "single https", headers: []string{"https"}, want: true},
		{name: "single uppercase HTTPS", headers: []string{"HTTPS"}, want: true},
		{name: "trimmed https", headers: []string{"  https  "}, want: true},
		{name: "multi-hop https", headers: []string{"https, https"}, want: true},
		{name: "multi-hop trimmed case", headers: []string{" https , HTTPS "}, want: true},
		{name: "multiple header lines all https", headers: []string{"https", "HTTPS"}, want: true},
		{name: "spoofed https then http", headers: []string{"https, http"}, want: false},
		{name: "spoofed http then https", headers: []string{"http, https"}, want: false},
		{name: "multiple header lines mixed", headers: []string{"https", "http"}, want: false},
		{name: "single http", headers: []string{"http"}, want: false},
		{name: "empty header", headers: []string{""}, want: false},
		{name: "whitespace and commas only", headers: []string{" , , "}, want: false},
		{name: "missing header", headers: nil, want: false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "http://example.com/", nil)
			for _, v := range tc.headers {
				req.Header.Add("X-Forwarded-Proto", v)
			}
			if got := RequestIsHTTPS(req); got != tc.want {
				t.Fatalf("RequestIsHTTPS(%v) = %v, want %v", tc.headers, got, tc.want)
			}
		})
	}
}

func TestR4_SecurityHeaders_HSTSRespectsMultiHopHeader(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(SecurityHeaders(SecurityHeadersConfig{EnableHSTS: true}))
	r.GET("/ok", func(c *gin.Context) { c.Status(http.StatusOK) })

	// Multi-hop https must emit HSTS.
	reqOK := httptest.NewRequest(http.MethodGet, "/ok", nil)
	reqOK.Header.Set("X-Forwarded-Proto", "https, https")
	wOK := httptest.NewRecorder()
	r.ServeHTTP(wOK, reqOK)
	if wOK.Header().Get("Strict-Transport-Security") == "" {
		t.Fatal("expected HSTS header on X-Forwarded-Proto: https, https")
	}

	// Spoofed mixed proto chain must NOT emit HSTS.
	reqSpoof := httptest.NewRequest(http.MethodGet, "/ok", nil)
	reqSpoof.Header.Set("X-Forwarded-Proto", "https, http")
	wSpoof := httptest.NewRecorder()
	r.ServeHTTP(wSpoof, reqSpoof)
	if hsts := wSpoof.Header().Get("Strict-Transport-Security"); hsts != "" {
		t.Fatalf("expected no HSTS header on mixed proto chain, got %q", hsts)
	}
}

func TestR4_SecurityHeadersFromEnv_StripsCRLF(t *testing.T) {
	t.Setenv("CSP_DISABLE", "false")
	t.Setenv("CSP_OVERRIDE", "default-src 'none'\r\nX-Injected: evil")

	cfg := SecurityHeadersFromEnv()
	if strings.ContainsAny(cfg.ContentSecurityPolicy, "\r\n") {
		t.Fatalf("CSP_OVERRIDE retained CR/LF: %q", cfg.ContentSecurityPolicy)
	}
	if cfg.ContentSecurityPolicy != "default-src 'none'X-Injected: evil" {
		t.Fatalf("unexpected cleaned CSP: %q", cfg.ContentSecurityPolicy)
	}

	// A whitespace/CRLF-only override must not silently erase the default CSP.
	t.Setenv("CSP_OVERRIDE", "\r\n  \r\n")
	cfgBlank := SecurityHeadersFromEnv()
	if cfgBlank.ContentSecurityPolicy != DefaultCSP() {
		t.Fatalf("expected DefaultCSP on CRLF-only override, got %q", cfgBlank.ContentSecurityPolicy)
	}
}

func TestR4_RequestTracing_SanitizesSensitiveQueryAndControlChars(t *testing.T) {
	rec := installTestTracer(t)
	logBuf := withCapturedLogger(t)
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(RequestLogger())
	r.Use(RequestTracing("svc-r4"))
	r.GET("/ws", func(c *gin.Context) {
		_ = c.Error(errors.New("bad\r\nline\tmsg"))
		c.Status(http.StatusBadRequest)
	})

	req := httptest.NewRequest(http.MethodGet, "/ws?token=super-secret-jwt&foo=bar&api_key=abc123&baz=qux", nil)
	r.ServeHTTP(httptest.NewRecorder(), req)

	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	qAttr, ok := findAttr(spans[0].Attributes(), "url.query")
	if !ok {
		t.Fatal("missing url.query span attribute")
	}
	wantQuery := "token=REDACTED&foo=bar&api_key=REDACTED&baz=qux"
	if qAttr.AsString() != wantQuery {
		t.Fatalf("url.query = %q, want %q", qAttr.AsString(), wantQuery)
	}

	errAttr, ok := findAttr(spans[0].Attributes(), "error.message")
	if !ok {
		t.Fatal("missing error.message span attribute")
	}
	if strings.ContainsAny(errAttr.AsString(), "\r\n\t") {
		t.Fatalf("error.message retained control characters: %q", errAttr.AsString())
	}

	lines := decodeLines(t, logBuf)
	if len(lines) != 1 {
		t.Fatalf("expected 1 log line, got %d", len(lines))
	}
	if gotLogQ, _ := lines[0]["query"].(string); gotLogQ != wantQuery {
		t.Fatalf("logged query = %q, want %q", gotLogQ, wantQuery)
	}

	// Verify sanitizeSpanAttr bounds long multi-byte UTF-8 without splitting runes.
	longUTF8 := strings.Repeat("a", maxSpanAttrBytes-2) + "🚀" + "tail"
	cleaned := sanitizeSpanAttr(longUTF8)
	if !utf8.ValidString(cleaned) {
		t.Fatalf("sanitizeSpanAttr produced invalid UTF-8: %q", cleaned)
	}
	if len(cleaned) > maxSpanAttrBytes {
		t.Fatalf("sanitizeSpanAttr exceeded %d bytes: %d", maxSpanAttrBytes, len(cleaned))
	}
	if bytes.Contains([]byte(cleaned), []byte("\xef\xbf\xbd")) {
		t.Fatalf("valid multi-byte boundary should back up cleanly rather than emitting replacement chars: %q", cleaned)
	}
}
