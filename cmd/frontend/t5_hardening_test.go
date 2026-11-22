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
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/gin-gonic/gin"
)

type needsInitTestResolver struct{}

func (needsInitTestResolver) Resolve(_ *http.Request) (account.UserContext, error) {
	return account.UserContext{
		UserID:    "u-new-firebase-user",
		Email:     "newuser@example.com",
		NeedsInit: true,
	}, nil
}

func TestT5_Frontend_InitPaths_IncludesFrontendPrefix(t *testing.T) {
	gin.SetMode(gin.TestMode)

	var (
		mu       sync.Mutex
		gotPaths []string
	)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotPaths = append(gotPaths, r.Method+" "+r.URL.Path)
		mu.Unlock()
		if r.URL.Path == "/api/v1/me/init" {
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"id":"u-new-firebase-user","created":true}`))
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("", needsInitTestResolver{})

	// All /frontend-prefixed init paths must proxy through when NeedsInit=true (never 409).
	initCases := []struct {
		method   string
		path     string
		wantCode int
	}{
		{http.MethodGet, "/frontend/api/v1/me", http.StatusOK},
		{http.MethodPost, "/frontend/api/v1/me/init", http.StatusCreated},
		{http.MethodPost, "/frontend/api/v1/auth/exchange", http.StatusOK},
		{http.MethodPost, "/frontend/api/v1/me/logout", http.StatusOK},
	}
	for _, tc := range initCases {
		req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(`{}`))
		req.Header.Set("Content-Type", "application/json")
		req.RemoteAddr = "127.0.0.1:1234"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != tc.wantCode {
			t.Fatalf("%s %s with NeedsInit=true: got status %d (body=%s), want %d",
				tc.method, tc.path, w.Code, w.Body.String(), tc.wantCode)
		}
	}

	// Non-init /frontend/api/* and /api/v1/settings/ai* routes must abort with 409 needs_init without proxying.
	blockedCases := []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/frontend/api/v1/sessions"},
		{http.MethodGet, "/api/v1/settings/ai"},
		{http.MethodPut, "/api/v1/settings/ai"},
		{http.MethodPost, "/api/v1/settings/ai/test"},
		{http.MethodPost, "/api/v1/settings/ai/start"},
		{http.MethodPost, "/api/v1/settings/ai/pull"},
		{http.MethodGet, "/frontend/api/v1/settings/ai"},
		{http.MethodPut, "/frontend/api/v1/settings/ai"},
		{http.MethodPost, "/frontend/api/v1/settings/ai/test"},
		{http.MethodPost, "/frontend/api/v1/settings/ai/start"},
		{http.MethodPost, "/frontend/api/v1/settings/ai/pull"},
	}
	for _, tc := range blockedCases {
		reqBlocked := httptest.NewRequest(tc.method, tc.path, strings.NewReader(`{}`))
		reqBlocked.Header.Set("Content-Type", "application/json")
		reqBlocked.RemoteAddr = "127.0.0.1:1234"
		wBlocked := httptest.NewRecorder()
		r.ServeHTTP(wBlocked, reqBlocked)
		if wBlocked.Code != http.StatusConflict || !strings.Contains(wBlocked.Body.String(), "needs_init") {
			t.Fatalf("%s %s with NeedsInit=true: got %d (%s), want 409 needs_init",
				tc.method, tc.path, wBlocked.Code, wBlocked.Body.String())
		}
	}

	mu.Lock()
	gotLen := len(gotPaths)
	gotCopy := append([]string(nil), gotPaths...)
	mu.Unlock()
	if gotLen != len(initCases) {
		t.Fatalf("expected %d proxied upstream calls, got %d (%v)", len(initCases), gotLen, gotCopy)
	}

	// LocalResolver (NeedsInit=false, StatusApproved) proxies /frontend/api/v1/settings/ai cleanly.
	rLocal := setupRouter("", account.NewLocalResolver())
	reqLocal := httptest.NewRequest(http.MethodGet, "/frontend/api/v1/settings/ai", nil)
	reqLocal.RemoteAddr = "127.0.0.1:1234"
	wLocal := httptest.NewRecorder()
	rLocal.ServeHTTP(wLocal, reqLocal)
	if wLocal.Code != http.StatusOK {
		t.Fatalf("GET /frontend/api/v1/settings/ai under LocalResolver: got %d (%s), want 200",
			wLocal.Code, wLocal.Body.String())
	}
}
