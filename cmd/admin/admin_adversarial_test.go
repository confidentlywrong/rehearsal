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
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/gin-gonic/gin"
)

func init() {
	gin.SetMode(gin.TestMode)
	_ = os.Setenv("LOCAL_RESOLVER_ALLOW_NON_LOOPBACK", "true")
	_ = os.Setenv("IDENTITY_PROVIDER", "local")

	if _, err := os.Stat("main.go"); err == nil {
		if _, err := os.Stat("../../web/admin-dash"); err == nil {
			_ = os.Chdir("../..")
		}
	}
}

// TestAdminAdversarialSecurityAndDegradation stress-tests cmd/admin router
// for security boundaries, superadmin checks, and graceful degradation.
func TestAdminAdversarialSecurityAndDegradation(t *testing.T) {
	// Router initialized in degraded mode (nil stores/reconciler/rdb)
	r := setupRouter(nil, newPubSubHealthChecker(), newAlloyDBHealthChecker(), "http://dummy-gateway", nil, account.NewLocalResolver(), nil, nil)

	t.Run("Non-superadmin denied access to /admin routes", func(t *testing.T) {
		req, _ := http.NewRequest("GET", "/admin/users", nil)
		req.RemoteAddr = "127.0.0.1:1234"
		// No X-Local-Dev-Superadmin header -> normal user
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusForbidden {
			t.Errorf("Expected 403 Forbidden for non-superadmin on /admin/users, got %d", w.Code)
		}
	})

	t.Run("Superadmin granted access, returns 503 in degraded mode", func(t *testing.T) {
		req, _ := http.NewRequest("GET", "/admin/users", nil)
		req.RemoteAddr = "127.0.0.1:1234"
		req.Header.Set(account.LocalSuperadminHeader, "1")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		// Superadmin check passed; store is nil -> returns 503
		if w.Code != http.StatusServiceUnavailable {
			t.Errorf("Expected 503 Service Unavailable for nil store, got %d", w.Code)
		}
	})

	t.Run("Reconcile route rejects malformed JSON payload", func(t *testing.T) {
		req, _ := http.NewRequest("POST", "/admin/billing/reconcile", bytes.NewBufferString("{invalid-json"))
		req.RemoteAddr = "127.0.0.1:1234"
		req.Header.Set(account.LocalSuperadminHeader, "1")
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		// Should not panic, either 503 (nil reconciler) or 400
		if w.Code != http.StatusServiceUnavailable && w.Code != http.StatusBadRequest {
			t.Errorf("Expected 503 or 400 for reconcile, got %d", w.Code)
		}
	})

	t.Run("Plan update route rejects invalid JSON", func(t *testing.T) {
		req, _ := http.NewRequest("PUT", "/admin/users/test-user/plan", bytes.NewBufferString("not-json"))
		req.RemoteAddr = "127.0.0.1:1234"
		req.Header.Set(account.LocalSuperadminHeader, "1")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusServiceUnavailable && w.Code != http.StatusBadRequest {
			t.Errorf("Expected 503 or 400 for invalid plan update, got %d", w.Code)
		}
	})

	t.Run("Infra health check degrades gracefully with nil DBs", func(t *testing.T) {
		req, _ := http.NewRequest("GET", "/api/v1/health/infra", nil)
		req.RemoteAddr = "127.0.0.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusOK {
			t.Errorf("Expected 200 OK for infra health check, got %d", w.Code)
		}
		body := w.Body.String()
		if !strings.Contains(body, `"redis":"offline"`) {
			t.Errorf("Expected redis offline in body, got: %s", body)
		}
	})

	t.Run("Services health check handles unreachable gateway", func(t *testing.T) {
		req, _ := http.NewRequest("GET", "/api/v1/health/services", nil)
		req.RemoteAddr = "127.0.0.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusOK {
			t.Errorf("Expected 200 OK for services health check, got %d", w.Code)
		}
	})

	t.Run("Path traversal attempt on /assets", func(t *testing.T) {
		req, _ := http.NewRequest("GET", "/assets/../../../../etc/passwd", nil)
		req.RemoteAddr = "127.0.0.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		// Must be 400 or 404, never 200
		if w.Code == http.StatusOK {
			t.Errorf("Security violation: path traversal returned 200 OK!")
		}
	})
}
