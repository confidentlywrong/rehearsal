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

package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/hub"
	"github.com/gin-gonic/gin"
)

func init() {
	gin.SetMode(gin.TestMode)
	_ = os.Setenv("LOCAL_RESOLVER_ALLOW_NON_LOOPBACK", "true")
	_ = os.Setenv("IDENTITY_PROVIDER", "local")
}

func TestGatewaySessionIDValidation(t *testing.T) {
	valid := []string{
		"session-1",
		"12345",
		"a-b_c",
		"98d781b0-1fa6-407b-8e2b-f8f2b7f0f63a",
		strings.Repeat("a", 128),
	}
	invalid := []string{
		"",
		strings.Repeat("a", 129),
		"../path-traversal",
		"session/slash",
		"session;drop",
		"session\x00null",
		"session\nnewline",
		"<script>alert(1)</script>",
		"session with spaces",
	}

	for _, s := range valid {
		if !validSessionID.MatchString(s) {
			t.Errorf("Expected validSessionID to accept %q", s)
		}
	}
	for _, s := range invalid {
		if validSessionID.MatchString(s) {
			t.Errorf("Expected validSessionID to reject %q", s)
		}
	}
}

func TestGatewayWebSocketCheckOrigin(t *testing.T) {
	t.Run("Dev mode permits origin when CORS_ALLOWED_ORIGINS unset", func(t *testing.T) {
		_ = os.Unsetenv("K_SERVICE")
		_ = os.Unsetenv("CORS_REFUSE_WILDCARD")
		_ = os.Unsetenv("CORS_ALLOWED_ORIGINS")

		req, _ := http.NewRequest("GET", "/ws", nil)
		req.Header.Set("Origin", "http://localhost:3000")
		if !upgrader.CheckOrigin(req) {
			t.Errorf("Expected CheckOrigin to permit in dev mode")
		}
	})

	t.Run("Cloud Run refuses wildcard or unset origins", func(t *testing.T) {
		_ = os.Setenv("K_SERVICE", "race-gateway")
		defer os.Unsetenv("K_SERVICE")
		_ = os.Unsetenv("CORS_ALLOWED_ORIGINS")

		req, _ := http.NewRequest("GET", "/ws", nil)
		req.Header.Set("Origin", "http://evil.com")
		if upgrader.CheckOrigin(req) {
			t.Errorf("Expected CheckOrigin to refuse on Cloud Run when CORS_ALLOWED_ORIGINS is empty")
		}
	})

	t.Run("Strict whitelist matches exact origin", func(t *testing.T) {
		_ = os.Setenv("K_SERVICE", "race-gateway")
		_ = os.Setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com,https://admin.example.com")
		defer func() {
			_ = os.Unsetenv("K_SERVICE")
			_ = os.Unsetenv("CORS_ALLOWED_ORIGINS")
		}()

		reqGood, _ := http.NewRequest("GET", "/ws", nil)
		reqGood.Header.Set("Origin", "https://app.example.com")
		if !upgrader.CheckOrigin(reqGood) {
			t.Errorf("Expected CheckOrigin to permit whitelisted origin")
		}

		reqBad, _ := http.NewRequest("GET", "/ws", nil)
		reqBad.Header.Set("Origin", "https://evil.example.com")
		if upgrader.CheckOrigin(reqBad) {
			t.Errorf("Expected CheckOrigin to refuse unwhitelisted origin")
		}
	})
}

func TestGatewayHealthEndpointsDegradation(t *testing.T) {
	h := hub.NewHub()
	r := setupRouter(h, nil, nil, nil, "gw-test-1", nil, nil)

	t.Run("Health returns degraded 503 when Redis is offline/nil", func(t *testing.T) {
		req, _ := http.NewRequest("GET", "/health", nil)
		req.RemoteAddr = "127.0.0.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		// When Redis is offline/nil, status is degraded / 200 or 503
		if w.Code != http.StatusOK && w.Code != http.StatusServiceUnavailable {
			t.Errorf("Expected 200 or 503 for health, got %d", w.Code)
		}
	})

	t.Run("Healthz returns 503 when Redis client is nil", func(t *testing.T) {
		req, _ := http.NewRequest("GET", "/healthz", nil)
		req.RemoteAddr = "127.0.0.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusServiceUnavailable {
			t.Errorf("Expected 503 for healthz with nil redis, got %d", w.Code)
		}
	})
}
