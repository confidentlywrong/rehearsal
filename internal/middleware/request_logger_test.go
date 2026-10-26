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
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
)

// withCapturedLogger swaps slog.Default() for a JSON logger writing
// to buf for the duration of the test.
func withCapturedLogger(t *testing.T) *bytes.Buffer {
	t.Helper()
	prev := slog.Default()
	t.Cleanup(func() { slog.SetDefault(prev) })
	var buf bytes.Buffer
	slog.SetDefault(logging.NewLogger(logging.FormatJSON, slog.LevelDebug, &buf))
	return &buf
}

func decodeLines(t *testing.T, buf *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimRight(buf.String(), "\n"), "\n") {
		if line == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("invalid JSON line %q: %v", line, err)
		}
		out = append(out, m)
	}
	return out
}

func newReqLogRouter() *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(RequestLogger())
	return r
}

func TestRequestLogger_LogsBasicFields(t *testing.T) {
	buf := withCapturedLogger(t)
	r := newReqLogRouter()
	r.GET("/ping", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})

	req := httptest.NewRequest(http.MethodGet, "/ping?x=1", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status: want 200 got %d", w.Code)
	}
	if w.Header().Get("X-Request-ID") == "" {
		t.Error("X-Request-ID response header missing")
	}

	lines := decodeLines(t, buf)
	if len(lines) != 1 {
		t.Fatalf("expected 1 log line, got %d: %s", len(lines), buf.String())
	}
	got := lines[0]
	if got["msg"] != "http.request" {
		t.Errorf("msg: want http.request got %v", got["msg"])
	}
	if got["method"] != "GET" {
		t.Errorf("method: want GET got %v", got["method"])
	}
	if got["path"] != "/ping" {
		t.Errorf("path: want /ping got %v", got["path"])
	}
	if got["query"] != "x=1" {
		t.Errorf("query: want x=1 got %v", got["query"])
	}
	if got["status"].(float64) != 200 {
		t.Errorf("status: want 200 got %v", got["status"])
	}
	if got["request_id"] == "" || got["request_id"] == nil {
		t.Errorf("request_id missing: %v", got)
	}
	if _, has := got["latency_ms"]; !has {
		t.Errorf("latency_ms missing: %v", got)
	}
}

func TestRequestLogger_HonorsInboundRequestID(t *testing.T) {
	buf := withCapturedLogger(t)
	r := newReqLogRouter()
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusNoContent) })

	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	req.Header.Set("X-Request-ID", "client-supplied-abc123")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if got := w.Header().Get("X-Request-ID"); got != "client-supplied-abc123" {
		t.Errorf("response X-Request-ID: want client-supplied-abc123 got %q", got)
	}
	lines := decodeLines(t, buf)
	if lines[0]["request_id"] != "client-supplied-abc123" {
		t.Errorf("log request_id: want client-supplied-abc123 got %v", lines[0]["request_id"])
	}
}

func TestRequestLogger_RejectsUnsafeInboundID(t *testing.T) {
	buf := withCapturedLogger(t)
	r := newReqLogRouter()
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	// CR/LF injection attempt: request_id with newline.
	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	req.Header.Set("X-Request-ID", "abc\r\ninjected: yes")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	got := lines(t, buf)[0]["request_id"].(string)
	if strings.Contains(got, "\n") || strings.Contains(got, "\r") {
		t.Errorf("CR/LF must be stripped from request_id, got %q", got)
	}
	if got == "abc\r\ninjected: yes" {
		t.Errorf("unsafe inbound request_id was accepted")
	}
}

func TestRequestLogger_OverlongInboundIDRejected(t *testing.T) {
	buf := withCapturedLogger(t)
	r := newReqLogRouter()
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	long := strings.Repeat("a", 200)
	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	req.Header.Set("X-Request-ID", long)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	got := lines(t, buf)[0]["request_id"].(string)
	if got == long {
		t.Errorf("expected overlong inbound id to be replaced, got it back unchanged")
	}
	if len(got) != 32 { // hex.EncodeToString of 16 bytes
		t.Errorf("expected 32-char hex fallback, got %d-char %q", len(got), got)
	}
}

func TestRequestLogger_4xxLogsAsWarn(t *testing.T) {
	buf := withCapturedLogger(t)
	r := newReqLogRouter()
	r.GET("/missing", func(c *gin.Context) { c.Status(http.StatusNotFound) })

	req := httptest.NewRequest(http.MethodGet, "/missing", nil)
	r.ServeHTTP(httptest.NewRecorder(), req)

	if got := lines(t, buf)[0]["level"]; got != "WARN" {
		t.Errorf("4xx must log at WARN, got %v", got)
	}
}

func TestRequestLogger_5xxLogsAsError(t *testing.T) {
	buf := withCapturedLogger(t)
	r := newReqLogRouter()
	r.GET("/boom", func(c *gin.Context) { c.Status(http.StatusInternalServerError) })

	req := httptest.NewRequest(http.MethodGet, "/boom", nil)
	r.ServeHTTP(httptest.NewRecorder(), req)

	if got := lines(t, buf)[0]["level"]; got != "ERROR" {
		t.Errorf("5xx must log at ERROR, got %v", got)
	}
}

func TestIsSafeRequestID(t *testing.T) {
	cases := []struct {
		in   string
		safe bool
	}{
		{"abc123", true},
		{"req-deadbeef-42", true},
		{"", false},
		{strings.Repeat("a", 129), false},
		{"abc\nxyz", false},
		{"abc\rxyz", false},
		{"abc\tdef", false},
		{"abc\x00def", false},
	}
	for _, tc := range cases {
		if got := isSafeRequestID(tc.in); got != tc.safe {
			t.Errorf("isSafeRequestID(%q): want %v got %v", tc.in, tc.safe, got)
		}
	}
}

// lines is a thin wrapper used by tests above (avoids re-typing
// decodeLines + helper-marker boilerplate).
func lines(t *testing.T, buf *bytes.Buffer) []map[string]any {
	t.Helper()
	return decodeLines(t, buf)
}
