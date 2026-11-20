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
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

func TestTester_DefaultGatewayPort9101_And_OIDCLocalGating(t *testing.T) {
	t.Setenv("GATEWAY_INTERNAL_URL", "")
	t.Setenv("GATEWAY_URL", "")
	t.Setenv("GATEWAY_AUDIENCE", "")
	t.Setenv("IAP_CLIENT_ID", "")

	got := resolveBackendURL()
	if got != config.DefaultGatewayURL || !strings.Contains(got, ":9101") {
		t.Fatalf("resolveBackendURL() = %q, want %q", got, config.DefaultGatewayURL)
	}
	if aud := resolveGatewayOIDCAudience(got); aud != "" {
		t.Fatalf("local loopback URL must not enable OIDC audience, got %q", aud)
	}
	if aud := resolveGatewayOIDCAudience("https://gateway-xyz-uc.a.run.app"); aud == "" {
		t.Fatalf("Cloud Run URL must resolve non-empty OIDC audience")
	}
}

func TestTester_APIProxy_OIDCFailureFailsFast502(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", t.TempDir()+"/missing-adc.json")
	t.Setenv("TESTER_REQUIRE_AUTH", "false")

	called := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("https://gateway-xyz.a.run.app")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/sessions", nil))

	if w.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 on OIDC failure, got %d body=%s", w.Code, w.Body.String())
	}
	if called {
		t.Fatalf("upstream must not be called when OIDC token fails")
	}
}

func TestTester_WSProxy_OIDCFailureFailsFast502(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", t.TempDir()+"/missing-adc.json")
	t.Setenv("TESTER_REQUIRE_AUTH", "false")

	dialed := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dialed = true
		w.WriteHeader(http.StatusSwitchingProtocols)
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("https://gateway-xyz.a.run.app")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/ws", nil))

	if w.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 on WS OIDC failure, got %d body=%s", w.Code, w.Body.String())
	}
	if dialed {
		t.Fatalf("upstream WS server must not be dialed when OIDC token fails")
	}
}

func TestTester_WSProxy_GroupPrefixRewritesToWS(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("TESTER_REQUIRE_AUTH", "false")

	var gotPath, gotQuery string
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		_ = c.WriteMessage(websocket.TextMessage, []byte("tester-pong"))
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("")
	bff := httptest.NewServer(r)
	defer bff.Close()

	wsURL := "ws" + strings.TrimPrefix(bff.URL, "http") + "/tester/ws?sessionId=t-1"
	conn, resp, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial /tester/ws failed: %v resp=%v", err, resp)
	}
	defer conn.Close()

	_, msg, err := conn.ReadMessage()
	if err != nil || string(msg) != "tester-pong" {
		t.Fatalf("expected tester-pong, got msg=%q err=%v", string(msg), err)
	}
	if gotPath != "/ws" {
		t.Fatalf("expected upstream WS path /ws, got %q", gotPath)
	}
	if gotQuery != "sessionId=t-1" {
		t.Fatalf("expected query sessionId=t-1, got %q", gotQuery)
	}
}

func TestTester_APIProxy_UnconditionallyStripsServerlessAuth(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("TESTER_REQUIRE_AUTH", "false")

	var seenServerless string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenServerless = r.Header.Get("X-Serverless-Authorization")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("")
	req := httptest.NewRequest(http.MethodGet, "/api/v1/sessions", nil)
	req.Header.Set("X-Serverless-Authorization", "Bearer forged-without-connection-header")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", w.Code, w.Body.String())
	}
	if seenServerless != "" {
		t.Fatalf("X-Serverless-Authorization must be unconditionally stripped, got %q", seenServerless)
	}
}

func TestTester_TrustedProxies_And_RequireAuthTrimmed(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("TRUSTED_PROXIES", "")
	t.Setenv("TESTER_REQUIRE_AUTH", "  true  ")

	r := setupRouter("")
	var clientIP string
	r.GET("/ip-check", func(c *gin.Context) {
		clientIP = c.ClientIP()
		c.Status(http.StatusOK)
	})

	reqUnauth := httptest.NewRequest(http.MethodGet, "/ip-check", nil)
	wUnauth := httptest.NewRecorder()
	r.ServeHTTP(wUnauth, reqUnauth)
	if wUnauth.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 when TESTER_REQUIRE_AUTH has whitespace around true, got %d", wUnauth.Code)
	}

	reqAuth := httptest.NewRequest(http.MethodGet, "/ip-check", nil)
	reqAuth.RemoteAddr = "127.0.0.1:4321"
	reqAuth.Header.Set("Authorization", "Bearer ops-tok")
	reqAuth.Header.Set("X-Forwarded-For", "198.51.100.88")
	wAuth := httptest.NewRecorder()
	r.ServeHTTP(wAuth, reqAuth)
	if wAuth.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", wAuth.Code)
	}
	if clientIP != "127.0.0.1" {
		t.Fatalf("expected spoofed X-Forwarded-For ignored, got %q", clientIP)
	}
}

func TestTester_APIProxy_UpstreamReadErrorReturns502(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("TESTER_REQUIRE_AUTH", "false")

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hj, ok := w.(http.Hijacker)
		if !ok {
			t.Fatal("ResponseWriter does not implement Hijacker")
		}
		conn, buf, err := hj.Hijack()
		if err != nil {
			t.Fatal(err)
		}
		_, _ = buf.WriteString("HTTP/1.1 500 Internal Server Error\r\nContent-Length: 1000\r\nContent-Type: text/plain\r\n\r\npartial")
		_ = buf.Flush()
		_ = conn.Close()
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/sessions", nil))

	if w.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 on truncated upstream error body, got %d body=%s", w.Code, w.Body.String())
	}
}
