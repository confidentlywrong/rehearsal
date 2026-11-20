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

package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
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

func TestGatewayNilRegistryEndpointsReturn503InsteadOfPanicking(t *testing.T) {
	h := hub.NewHub()
	r := setupRouter(h, nil, nil, nil, "gw-test-nil-reg", nil, nil)

	cases := []struct {
		method string
		path   string
		body   string
		want   int
	}{
		{http.MethodGet, "/api/v1/sessions", "", http.StatusServiceUnavailable},
		{http.MethodPost, "/api/v1/sessions", `{"agentType":"planner","sessionId":"sess-1","simulationId":"sim-1"}`, http.StatusServiceUnavailable},
		{http.MethodPost, "/api/v1/sessions/flush", "", http.StatusServiceUnavailable},
		{http.MethodPost, "/api/v1/environment/reset", "", http.StatusOK},
	}
	for _, tc := range cases {
		req, _ := http.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
		req.RemoteAddr = "127.0.0.1:1234"
		if tc.body != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != tc.want {
			t.Errorf("%s %s with nil registry returned %d, want %d", tc.method, tc.path, w.Code, tc.want)
		}
	}
}

func TestGatewayDispatchShutdownGateAndExportSanitizer(t *testing.T) {
	// Reset global gate state after test so other tests in package aren't affected.
	defer func() {
		dispatchMu.Lock()
		shuttingDown.Store(false)
		dispatchMu.Unlock()
	}()

	dispatchMu.Lock()
	shuttingDown.Store(false)
	dispatchMu.Unlock()

	if !tryStartDispatch() {
		t.Fatal("tryStartDispatch should succeed before shutdown")
	}
	dispatchWG.Done()

	markShuttingDown()
	if tryStartDispatch() {
		dispatchWG.Done()
		t.Fatal("tryStartDispatch must return false after markShuttingDown")
	}
	dispatchWG.Wait()

	if got := sanitizeExportFilename("u1\"\r\n/evil"); got != "u1____evil" {
		t.Fatalf("sanitizeExportFilename = %q", got)
	}
}

type fakeMeStoreForDelete struct {
	softDeleted string
}

func (f *fakeMeStoreForDelete) GetUser(_ context.Context, id string) (account.User, error) {
	return account.User{ID: id, Email: "u@example.com"}, nil
}
func (f *fakeMeStoreForDelete) CreateUser(_ context.Context, id, email, name string) (account.User, bool, error) {
	return account.User{ID: id, Email: email, DisplayName: name}, true, nil
}
func (f *fakeMeStoreForDelete) UpdateUserProfile(_ context.Context, _ string, _, _ *string) error {
	return nil
}
func (f *fakeMeStoreForDelete) SoftDeleteUser(_ context.Context, id string) error {
	f.softDeleted = id
	return nil
}
func (f *fakeMeStoreForDelete) RevokeTokens(_ context.Context, _ string) error {
	return nil
}

func TestDeleteAccountHandler_ClearsSessionCookie(t *testing.T) {
	store := &fakeMeStoreForDelete{}
	r := gin.New()
	r.Use(func(c *gin.Context) {
		ctx := account.WithContext(c.Request.Context(), account.UserContext{
			UserID: "u-del-1",
			Email:  "u@example.com",
		})
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	})
	r.DELETE("/api/v1/me", deleteAccountHandler(store))

	req := httptest.NewRequest(http.MethodDelete, "/api/v1/me", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusNoContent {
		t.Fatalf("DELETE /api/v1/me status = %d, want 204", w.Code)
	}
	if store.softDeleted != "u-del-1" {
		t.Fatalf("softDeleted = %q, want u-del-1", store.softDeleted)
	}
	setCookie := strings.Join(w.Header().Values("Set-Cookie"), "; ")
	if !strings.Contains(setCookie, "__session=") || !strings.Contains(setCookie, "Max-Age=0") {
		t.Fatalf("expected __session cookie clear in Set-Cookie, got %q", setCookie)
	}
}
