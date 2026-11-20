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
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

func TestFrontend_DefaultGatewayPort9101_And_OIDCLocalGating(t *testing.T) {
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

func TestFrontend_APIProxy_OIDCFailureFailsFast502(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", t.TempDir()+"/missing-adc.json")

	called := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("https://gateway-xyz.a.run.app", account.NewLocalResolver())
	req := httptest.NewRequest(http.MethodGet, "/api/v1/sessions", nil)
	req.RemoteAddr = "127.0.0.1:1234"
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 on OIDC failure, got %d body=%s", w.Code, w.Body.String())
	}
	if called {
		t.Fatalf("upstream must not be called when OIDC token fails")
	}
}

func TestFrontend_WSProxy_OIDCFailureFailsFast502(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", t.TempDir()+"/missing-adc.json")

	dialed := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dialed = true
		w.WriteHeader(http.StatusSwitchingProtocols)
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("https://gateway-xyz.a.run.app", account.NewLocalResolver())
	req := httptest.NewRequest(http.MethodGet, "/ws", nil)
	req.RemoteAddr = "127.0.0.1:1234"
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 on WS OIDC failure, got %d body=%s", w.Code, w.Body.String())
	}
	if dialed {
		t.Fatalf("upstream WS server must not be dialed when OIDC token fails")
	}
}

func TestFrontend_WSProxy_GroupPrefixRewritesToWS_AndPreservesHeaders(t *testing.T) {
	gin.SetMode(gin.TestMode)
	var (
		gotPath   string
		gotQuery  string
		gotAuth   string
		gotOrigin string
	)
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotAuth = r.Header.Get("Authorization")
		gotOrigin = r.Header.Get("Origin")
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		_ = c.WriteMessage(websocket.TextMessage, []byte("pong"))
	}))
	defer upstream.Close()
	t.Setenv("GATEWAY_INTERNAL_URL", upstream.URL)

	r := setupRouter("", account.NewLocalResolver())
	bff := httptest.NewServer(r)
	defer bff.Close()

	wsURL := "ws" + strings.TrimPrefix(bff.URL, "http") + "/frontend/ws?sessionId=s-1"
	hdr := http.Header{}
	hdr.Set("Origin", "https://app.example.com")
	hdr.Set("Authorization", "Bearer user-firebase-jwt")
	conn, resp, err := websocket.DefaultDialer.Dial(wsURL, hdr)
	if err != nil {
		t.Fatalf("dial /frontend/ws failed: %v resp=%v", err, resp)
	}
	defer conn.Close()

	_, msg, err := conn.ReadMessage()
	if err != nil || string(msg) != "pong" {
		t.Fatalf("expected pong, got msg=%q err=%v", string(msg), err)
	}
	if gotPath != "/ws" {
		t.Fatalf("expected upstream WS path /ws, got %q", gotPath)
	}
	if gotQuery != "sessionId=s-1" {
		t.Fatalf("expected query sessionId=s-1, got %q", gotQuery)
	}
	if gotAuth != "Bearer user-firebase-jwt" {
		t.Fatalf("expected Authorization preserved, got %q", gotAuth)
	}
	if gotOrigin != "https://app.example.com" {
		t.Fatalf("expected Origin preserved, got %q", gotOrigin)
	}
}

func TestFrontend_TrustedProxies_IgnoresSpoofedXForwardedFor(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("TRUSTED_PROXIES", "")

	r := setupRouter("", account.NewLocalResolver())
	var clientIP string
	r.GET("/frontend/ip", func(c *gin.Context) {
		clientIP = c.ClientIP()
		c.Status(http.StatusOK)
	})

	req := httptest.NewRequest(http.MethodGet, "/frontend/ip", nil)
	req.RemoteAddr = "127.0.0.1:4321"
	req.Header.Set("X-Forwarded-For", "203.0.113.99")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if clientIP != "127.0.0.1" {
		t.Fatalf("expected spoofed X-Forwarded-For ignored, got %q", clientIP)
	}
}

func TestFrontend_APIProxy_UpstreamReadErrorReturns502(t *testing.T) {
	gin.SetMode(gin.TestMode)

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

	r := setupRouter("", account.NewLocalResolver())
	req := httptest.NewRequest(http.MethodGet, "/api/v1/sessions", nil)
	req.RemoteAddr = "127.0.0.1:1234"
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadGateway {
		t.Fatalf("expected 502 on truncated upstream error body, got %d body=%s", w.Code, w.Body.String())
	}
}

func TestFrontend_CachedFileServer_NoImmutableOn404AndDotDir(t *testing.T) {
	dir := t.TempDir()
	sub := filepath.Join(dir, "v1.0")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sub, "route"), []byte("ok"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "app.js"), []byte("console.log(1)"), 0o600); err != nil {
		t.Fatal(err)
	}

	h := cachedFileServer(http.Dir(dir))

	// Extensionless file under dotted directory must get no-cache, not immutable.
	w1 := httptest.NewRecorder()
	h.ServeHTTP(w1, httptest.NewRequest(http.MethodGet, "/v1.0/route", nil))
	if cc := w1.Header().Get("Cache-Control"); cc != "no-cache" {
		t.Fatalf("dotted dir extensionless path Cache-Control = %q, want no-cache", cc)
	}

	// Missing .js asset (404) must NOT get immutable cache headers.
	w2 := httptest.NewRecorder()
	h.ServeHTTP(w2, httptest.NewRequest(http.MethodGet, "/missing.js", nil))
	if w2.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", w2.Code)
	}
	if cc := w2.Header().Get("Cache-Control"); strings.Contains(cc, "immutable") {
		t.Fatalf("404 response must not get immutable Cache-Control, got %q", cc)
	}

	// Existing .js asset (200) gets immutable cache headers.
	w3 := httptest.NewRecorder()
	h.ServeHTTP(w3, httptest.NewRequest(http.MethodGet, "/app.js", nil))
	if w3.Code != http.StatusOK || !strings.Contains(w3.Header().Get("Cache-Control"), "immutable") {
		t.Fatalf("200 .js expected immutable Cache-Control, got code=%d cc=%q", w3.Code, w3.Header().Get("Cache-Control"))
	}

	// Trailing-slash dotted directory (/v1.0/index.html) must get no-cache, never immutable.
	if err := os.WriteFile(filepath.Join(sub, "index.html"), []byte("<html>sub</html>"), 0o600); err != nil {
		t.Fatal(err)
	}
	w4 := httptest.NewRecorder()
	h.ServeHTTP(w4, httptest.NewRequest(http.MethodGet, "/v1.0/", nil))
	if w4.Code != http.StatusOK {
		t.Fatalf("/v1.0/ expected 200, got %d body=%s", w4.Code, w4.Body.String())
	}
	if cc := w4.Header().Get("Cache-Control"); cc != "no-cache" || strings.Contains(cc, "immutable") {
		t.Fatalf("/v1.0/ index.html Cache-Control = %q, want no-cache (never immutable)", cc)
	}
}

func TestFrontend_NoRoute_TrailingSlashDotDir_NoImmutableAndSPAFallback(t *testing.T) {
	gin.SetMode(gin.TestMode)

	t.Run("existing /v1.0/index.html serves with no-cache", func(t *testing.T) {
		tmp := t.TempDir()
		distDir := filepath.Join(tmp, "web", "frontend", "dist", "client", "browser")
		subDir := filepath.Join(distDir, "v1.0")
		if err := os.MkdirAll(subDir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(distDir, "index.html"), []byte("<html>root</html>"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(subDir, "index.html"), []byte("<html>sub</html>"), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Chdir(tmp)

		r := setupRouter("", account.NewLocalResolver())
		for _, reqPath := range []string{"/v1.0/", "/frontend/v1.0/"} {
			req := httptest.NewRequest(http.MethodGet, reqPath, nil)
			req.RemoteAddr = "127.0.0.1:1234"
			w := httptest.NewRecorder()
			r.ServeHTTP(w, req)
			if w.Code != http.StatusOK {
				t.Fatalf("GET %s: expected 200, got %d body=%s", reqPath, w.Code, w.Body.String())
			}
			if cc := w.Header().Get("Cache-Control"); cc != "no-cache" || strings.Contains(cc, "immutable") {
				t.Fatalf("GET %s: Cache-Control = %q, want no-cache", reqPath, cc)
			}
			if !strings.Contains(w.Body.String(), "<html>sub</html>") {
				t.Fatalf("GET %s: expected sub index.html body, got %q", reqPath, w.Body.String())
			}
		}
	})

	t.Run("non-existent /v1.0/ triggers SPA index.html fallback with no-cache", func(t *testing.T) {
		tmp := t.TempDir()
		distDir := filepath.Join(tmp, "web", "frontend", "dist", "client", "browser")
		if err := os.MkdirAll(distDir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(distDir, "index.html"), []byte("<html>spa-root</html>"), 0o600); err != nil {
			t.Fatal(err)
		}
		t.Chdir(tmp)

		r := setupRouter("", account.NewLocalResolver())
		for _, reqPath := range []string{"/v1.0/", "/frontend/v1.0/"} {
			req := httptest.NewRequest(http.MethodGet, reqPath, nil)
			req.RemoteAddr = "127.0.0.1:1234"
			w := httptest.NewRecorder()
			r.ServeHTTP(w, req)
			if w.Code != http.StatusOK {
				t.Fatalf("GET %s: expected 200 SPA fallback, got %d body=%s", reqPath, w.Code, w.Body.String())
			}
			if cc := w.Header().Get("Cache-Control"); cc != "no-cache" || strings.Contains(cc, "immutable") {
				t.Fatalf("GET %s: Cache-Control = %q, want no-cache", reqPath, cc)
			}
			if !strings.Contains(w.Body.String(), "<html>spa-root</html>") {
				t.Fatalf("GET %s: expected SPA root index.html body, got %q", reqPath, w.Body.String())
			}
		}
	})
}
