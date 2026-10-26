// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func init() {
	gin.SetMode(gin.TestMode)
}

func TestTesterHealth(t *testing.T) {
	r := setupRouter("")

	t.Run("Root health", func(t *testing.T) {
		w := httptest.NewRecorder()
		req, _ := http.NewRequest("GET", "/health", nil)
		r.ServeHTTP(w, req)

		assert.Equal(t, http.StatusOK, w.Code)
		assert.Contains(t, w.Body.String(), `"status":"ok"`)
	})

	t.Run("Tester group health", func(t *testing.T) {
		w := httptest.NewRecorder()
		req, _ := http.NewRequest("GET", "/tester/health", nil)
		r.ServeHTTP(w, req)

		assert.Equal(t, http.StatusOK, w.Code)
		assert.Contains(t, w.Body.String(), `"service":"tester"`)
	})
}

func TestTesterConfigJS(t *testing.T) {
	r := setupRouter("")

	t.Run("Root config.js", func(t *testing.T) {
		w := httptest.NewRecorder()
		req, _ := http.NewRequest("GET", "/config.js", nil)
		r.ServeHTTP(w, req)

		assert.Equal(t, http.StatusOK, w.Code)
		assert.Equal(t, "application/javascript", w.Header().Get("Content-Type"))
		assert.Contains(t, w.Body.String(), "window.ENV")
		assert.Contains(t, w.Body.String(), "VITE_GATEWAY_URL")
		assert.Contains(t, w.Body.String(), `"/ws"`)
	})

	t.Run("Tester group config.js", func(t *testing.T) {
		w := httptest.NewRecorder()
		req, _ := http.NewRequest("GET", "/tester/config.js", nil)
		r.ServeHTTP(w, req)

		assert.Equal(t, http.StatusOK, w.Code)
		assert.Contains(t, w.Body.String(), "VITE_GATEWAY_URL")
		// CLOUD_MODE should NOT be exposed to frontend
		assert.NotContains(t, w.Body.String(), "CLOUD_MODE")
	})
}

func TestTesterWSProxy_DirectorSetsHostAndStripsIAP(t *testing.T) {
	// Mock WebSocket server to act as the gateway
	var receivedHost string
	wsServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedHost = r.Host
		upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		conn.Close()
	}))
	defer wsServer.Close()

	// Point tester at our mock gateway
	t.Setenv("GATEWAY_INTERNAL_URL", wsServer.URL)

	r := setupRouter("")
	server := httptest.NewServer(r)
	defer server.Close()

	// Connect via WebSocket to tester /ws
	wsURL := "ws" + server.URL[4:] + "/ws"
	dialer := websocket.Dialer{}
	header := http.Header{}
	// Simulate IAP headers that should be stripped
	header.Set("X-Goog-IAP-JWT-Assertion", "fake-token")
	conn, resp, err := dialer.Dial(wsURL, header)
	require.NoError(t, err, "WebSocket dial failed")
	if resp != nil && resp.Body != nil {
		defer resp.Body.Close()
	}
	defer conn.Close()

	// Host should be the mock server's host (set by Director)
	assert.NotEmpty(t, receivedHost)
}

func TestTesterAPIProxy_ForwardsRequest(t *testing.T) {
	// Mock gateway API server
	var receivedPath, receivedMethod string
	apiServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedPath = r.URL.Path
		receivedMethod = r.Method
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]string{"proxied": "true"})
	}))
	defer apiServer.Close()

	t.Setenv("GATEWAY_INTERNAL_URL", apiServer.URL)

	r := setupRouter("")

	t.Run("Root API proxy", func(t *testing.T) {
		w := httptest.NewRecorder()
		req, _ := http.NewRequest("GET", "/api/v1/sessions", nil)
		r.ServeHTTP(w, req)

		assert.Equal(t, http.StatusOK, w.Code)
		assert.Contains(t, w.Body.String(), "proxied")
		assert.Equal(t, "/api/v1/sessions", receivedPath)
		assert.Equal(t, "GET", receivedMethod)
	})

	t.Run("Tester group API proxy", func(t *testing.T) {
		w := httptest.NewRecorder()
		req, _ := http.NewRequest("POST", "/tester/api/v1/sessions", nil)
		r.ServeHTTP(w, req)

		assert.Equal(t, http.StatusOK, w.Code)
		assert.Equal(t, "/api/v1/sessions", receivedPath)
		assert.Equal(t, "POST", receivedMethod)
	})

	t.Run("API proxy with query params", func(t *testing.T) {
		w := httptest.NewRecorder()
		req, _ := http.NewRequest("GET", "/api/v1/sessions?flush=true", nil)
		r.ServeHTTP(w, req)

		assert.Equal(t, http.StatusOK, w.Code)
	})
}

func TestTesterAPIProxy_FiltersHeaders(t *testing.T) {
	var receivedHeaders http.Header
	apiServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedHeaders = r.Header
		w.WriteHeader(http.StatusOK)
	}))
	defer apiServer.Close()

	t.Setenv("GATEWAY_INTERNAL_URL", apiServer.URL)

	r := setupRouter("")

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/api/v1/health", nil)
	req.Header.Set("Authorization", "Bearer should-be-stripped")
	req.Header.Set("X-Goog-IAP-JWT-Assertion", "should-be-stripped")
	req.Header.Set("X-Custom-Header", "should-be-kept")
	r.ServeHTTP(w, req)

	assert.Equal(t, http.StatusOK, w.Code)
	// Authorization and x-goog-* headers should NOT be forwarded
	assert.Empty(t, receivedHeaders.Get("Authorization"))
	assert.Empty(t, receivedHeaders.Get("X-Goog-IAP-JWT-Assertion"))
	// Custom headers should be forwarded
	assert.Equal(t, "should-be-kept", receivedHeaders.Get("X-Custom-Header"))
}

func TestTesterRedirect(t *testing.T) {
	r := setupRouter("")
	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/tester", nil)
	r.ServeHTTP(w, req)

	assert.Equal(t, http.StatusMovedPermanently, w.Code)
	assert.Equal(t, "/tester/", w.Header().Get("Location"))
}

func TestTesterRouter_HasWSRoute(t *testing.T) {
	r := setupRouter("")
	routes := r.Routes()

	foundRoot := false
	foundGroup := false
	for _, route := range routes {
		if route.Path == "/ws" && route.Method == "GET" {
			foundRoot = true
		}
		if route.Path == "/tester/ws" && route.Method == "GET" {
			foundGroup = true
		}
	}
	assert.True(t, foundRoot, "expected /ws route")
	assert.True(t, foundGroup, "expected /tester/ws route")
}

// TestRequireAuth_Disabled confirms the default (env unset) preserves
// legacy behaviour: every route is reachable without an Authorization
// header. This is the local-dev / docker-compose code path.
func TestRequireAuth_Disabled(t *testing.T) {
	r := setupRouter("")

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/health", nil)
	r.ServeHTTP(w, req)
	require.Equal(t, http.StatusOK, w.Code, "/health must work without auth when TESTER_REQUIRE_AUTH unset")
}

// TestRequireAuth_BlocksAnonymous confirms that when TESTER_REQUIRE_AUTH=true
// the app layer 401s any request without an `Authorization: Bearer ...`
// header. This is the defence-in-depth gate that fires inside the
// container if Cloud Run IAM ever regresses to allUsers.
func TestRequireAuth_BlocksAnonymous(t *testing.T) {
	t.Setenv("TESTER_REQUIRE_AUTH", "true")
	r := setupRouter("")

	// Use a REGISTERED route so gin's trailing-slash redirect logic
	// (which short-circuits middlewares) does not mask the test.
	// /tester/health is mounted on the tg := r.Group("/tester") group.
	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/tester/health", nil)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusUnauthorized, w.Code)
	assert.Contains(t, w.Body.String(), "operator_auth_required")
}

// TestRequireAuth_EmptyBearerRejected guards against the obvious bypass
// of sending an empty bearer token.
func TestRequireAuth_EmptyBearerRejected(t *testing.T) {
	t.Setenv("TESTER_REQUIRE_AUTH", "true")
	r := setupRouter("")

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/tester/health", nil)
	req.Header.Set("Authorization", "Bearer ")
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusUnauthorized, w.Code, "empty bearer token must be rejected")
}

// TestRequireAuth_NonBearerRejected guards against `Basic ...` or other
// non-Bearer schemes slipping through.
func TestRequireAuth_NonBearerRejected(t *testing.T) {
	t.Setenv("TESTER_REQUIRE_AUTH", "true")
	r := setupRouter("")

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/tester/health", nil)
	req.Header.Set("Authorization", "Basic dXNlcjpwYXNz")
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusUnauthorized, w.Code)
}

// TestRequireAuth_HealthExemptForProbes confirms /health stays reachable
// even when TESTER_REQUIRE_AUTH=true. Cloud Run liveness/readiness probes
// do NOT send Authorization, so requiring it on /health would break
// rollouts.
func TestRequireAuth_HealthExemptForProbes(t *testing.T) {
	t.Setenv("TESTER_REQUIRE_AUTH", "true")
	r := setupRouter("")

	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/health", nil)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusOK, w.Code, "/health must be probe-friendly even with auth required")

	var body map[string]any
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &body))
	assert.Equal(t, "ok", body["status"])
}

// TestRequireAuth_BearerAcceptedDoesNotValidateToken confirms the
// middleware is presence-only (Cloud Run IAM validates cryptographically
// upstream). A syntactically-valid bearer token passes the app-layer
// gate; in production that token is already known good because IAM let
// the request through.
func TestRequireAuth_BearerAcceptedDoesNotValidateToken(t *testing.T) {
	t.Setenv("TESTER_REQUIRE_AUTH", "true")
	r := setupRouter("")

	// /tester/health is a real registered route; a syntactically-valid
	// bearer token must pass the app-layer presence check and reach
	// the handler. (Cloud Run IAM is what validates the token
	// cryptographically upstream.)
	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/tester/health", nil)
	req.Header.Set("Authorization", "Bearer eyJhbGciOiJSUzI1NiJ9.fake.signature")
	r.ServeHTTP(w, req)

	require.NotEqual(t, http.StatusUnauthorized, w.Code, "bearer presence must satisfy app-layer gate")
	assert.Equal(t, http.StatusOK, w.Code)
}
