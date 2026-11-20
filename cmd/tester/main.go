// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/GoogleCloudPlatform/race-condition/internal/auth"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/GoogleCloudPlatform/race-condition/internal/middleware"
	"github.com/GoogleCloudPlatform/race-condition/internal/tracing"
	"github.com/gin-gonic/gin"
	"github.com/koding/websocketproxy"
)

var proxyClient = &http.Client{Timeout: 30 * time.Second}

// isHopByHopOrFramingHeader reports whether key is an RFC 7230 hop-by-hop
// header or a body-framing header (Content-Length / Content-Type) already set
// by Gin's c.DataFromReader.
func isHopByHopHeader(key string) bool {
	switch strings.ToLower(strings.TrimSpace(key)) {
	case "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
		"te", "trailer", "trailers", "transfer-encoding", "upgrade":
		return true
	default:
		return false
	}
}

func connectionHeaderTokens(h http.Header) map[string]struct{} {
	var out map[string]struct{}
	for _, v := range h.Values("Connection") {
		for _, tok := range strings.Split(v, ",") {
			tok = strings.ToLower(strings.TrimSpace(tok))
			if tok == "" {
				continue
			}
			if out == nil {
				out = make(map[string]struct{})
			}
			out[tok] = struct{}{}
		}
	}
	return out
}

func isHopByHopOrFramingHeader(key string) bool {
	if isHopByHopHeader(key) {
		return true
	}
	switch strings.ToLower(strings.TrimSpace(key)) {
	case "content-length", "content-type":
		return true
	default:
		return false
	}
}

func buildGatewayAPIURL(backendURL, action string, reqURL *url.URL, groupPrefix string) (string, error) {
	baseU, err := url.Parse(backendURL)
	if err != nil || baseU.Scheme == "" || baseU.Host == "" {
		return "", fmt.Errorf("invalid backend URL %q: %w", backendURL, err)
	}
	targetU := *baseU
	basePath := strings.TrimSuffix(baseU.Path, "/")
	targetU.Path = basePath + "/api/v1" + action
	targetU.RawPath = ""
	if reqURL != nil {
		if escaped := reqURL.EscapedPath(); escaped != "" {
			rawActionPath := escaped
			if groupPrefix != "" && strings.HasPrefix(rawActionPath, groupPrefix+"/api/v1") {
				rawActionPath = strings.TrimPrefix(rawActionPath, groupPrefix)
			}
			if strings.HasPrefix(rawActionPath, "/api/v1") {
				baseEscaped := strings.TrimSuffix(baseU.EscapedPath(), "/")
				targetU.RawPath = baseEscaped + rawActionPath
			}
		}
		targetU.RawQuery = reqURL.RawQuery
	}
	targetU.Fragment = ""
	return targetU.String(), nil
}

type wsOIDCAuthKey struct{}

func resolveBackendURL() string {
	return config.Optional("GATEWAY_INTERNAL_URL",
		config.Optional("GATEWAY_URL", config.DefaultGatewayURL))
}

func resolveGatewayOIDCAudience(gatewayURL string) string {
	if v := strings.TrimSpace(os.Getenv("GATEWAY_AUDIENCE")); v != "" {
		return v
	}
	if strings.TrimSpace(os.Getenv("IAP_CLIENT_ID")) != "" {
		return auth.ResolveAudience(gatewayURL)
	}
	if auth.IsCloudRunURL(gatewayURL) {
		return auth.AudienceFor(gatewayURL)
	}
	return ""
}

func buildGatewayWSURL(backendURL string) (*url.URL, error) {
	u, err := url.Parse(strings.TrimSpace(backendURL))
	if err != nil || u.Host == "" {
		return nil, fmt.Errorf("invalid gateway URL %q: %w", backendURL, err)
	}
	switch strings.ToLower(u.Scheme) {
	case "http":
		u.Scheme = "ws"
	case "https":
		u.Scheme = "wss"
	case "ws", "wss":
	default:
		return nil, fmt.Errorf("unsupported gateway scheme %q", u.Scheme)
	}
	u.Path = strings.TrimSuffix(u.Path, "/") + "/ws"
	u.RawPath = ""
	u.RawQuery = ""
	u.Fragment = ""
	return u, nil
}

func configureTrustedProxies(r *gin.Engine) error {
	raw := strings.TrimSpace(os.Getenv("TRUSTED_PROXIES"))
	if raw == "" {
		return r.SetTrustedProxies(nil)
	}
	var proxies []string
	for _, part := range strings.Split(raw, ",") {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			proxies = append(proxies, trimmed)
		}
	}
	if len(proxies) == 0 {
		return r.SetTrustedProxies(nil)
	}
	return r.SetTrustedProxies(proxies)
}

// setupRouter creates the tester Gin engine. oidcAudience is the OIDC
// audience for proxied gateway requests; empty disables attachment.
func setupRouter(oidcAudience string) *gin.Engine {
	r := gin.Default()
	if err := configureTrustedProxies(r); err != nil {
		log.Printf("TESTER: invalid TRUSTED_PROXIES (%v); disabling trusted proxies", err)
		_ = r.SetTrustedProxies(nil)
	}

	// G5a: structured request log + request_id propagation. Mounted
	// FIRST so request_id is on ctx for everything downstream.
	r.Use(middleware.RequestLogger())

	// G5b: distributed tracing. No-op when tracing.Setup wasn't
	// called or OTEL_EXPORTER is unset.
	r.Use(middleware.RequestTracing("race-tester"))

	// G4a: hardening headers (CSP report-only by default + X-Frame-Options,
	// X-Content-Type-Options, Referrer-Policy, Permissions-Policy, optional
	// HSTS on HTTPS).
	r.Use(middleware.SecurityHeaders(middleware.SecurityHeadersFromEnv()))

	// Defence-in-depth for the operator-only posture (see
	// docs/ops/operator_access.md). The primary gate is Cloud Run
	// IAM (operator_principals in the cloud-run-iam module), which
	// rejects unauthenticated requests at the GCP edge. When
	// TESTER_REQUIRE_AUTH=true the app layer ALSO rejects any
	// request that does not carry an `Authorization: Bearer ...`
	// header. If anyone ever regresses the IAM binding to allUsers
	// by accident, this 401 fires inside the container instead of
	// letting an anonymous caller spend Vertex quota. /health is
	// exempt so Cloud Run startup probes (which do not send
	// Authorization) keep working. Token validity is NOT checked
	// here \u2014 Cloud Run IAM does that cryptographically; this is
	// purely presence enforcement.
	if strings.EqualFold(strings.TrimSpace(os.Getenv("TESTER_REQUIRE_AUTH")), "true") {
		r.Use(func(c *gin.Context) {
			if c.Request.URL != nil && c.Request.URL.Path == "/health" {
				c.Next()
				return
			}
			authz := c.GetHeader("Authorization")
			if auth.ParseBearerHeader(authz) == "" {
				c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
					"error": "operator_auth_required",
					"hint":  "tester is operator-only; reach it via 'gcloud run services proxy tester' (see docs/ops/operator_access.md)",
				})
				return
			}
			c.Next()
		})
	}

	r.Use(func(c *gin.Context) {
		reqPath := ""
		if c.Request.URL != nil {
			reqPath = c.Request.URL.Path
		}
		log.Printf("Incoming Request: %s %s (Remote: %s)", c.Request.Method, reqPath, c.ClientIP())
		c.Next()
	})

	configHandler := middleware.ConfigJSHandler(map[string]string{
		"VITE_GATEWAY_URL": "/ws",
	})

	backendURL := resolveBackendURL()
	wsU, err := buildGatewayWSURL(backendURL)
	if err != nil {
		log.Fatalf("WebSocket Proxy: Invalid URL %q: %v", backendURL, err)
	}
	log.Printf("WebSocket Proxy: Initializing for %s", wsU.String())
	wsProxy := websocketproxy.NewProxy(wsU)
	wsProxy.Backend = func(r *http.Request) *url.URL {
		target := *wsU
		target.Path = wsU.Path
		target.RawPath = wsU.RawPath
		if r != nil && r.URL != nil {
			target.RawQuery = r.URL.RawQuery
			target.Fragment = r.URL.Fragment
		}
		return &target
	}

	wsProxy.Director = func(incoming *http.Request, out http.Header) {
		out.Set("Host", wsU.Host)
		out.Del("X-Serverless-Authorization")
		out.Del("Authorization")
		for key := range out {
			if strings.HasPrefix(strings.ToLower(key), "x-goog-") {
				out.Del(key)
			}
		}
		if tok, ok := incoming.Context().Value(wsOIDCAuthKey{}).(string); ok && tok != "" {
			out.Set("Authorization", tok)
		}
	}

	wsHandler := func(c *gin.Context) {
		if oidcAudience != "" {
			oidcHdr := make(http.Header)
			if err := middleware.AttachOIDCTokenE(c.Request.Context(), oidcHdr, oidcAudience); err != nil {
				log.Printf("WebSocket Proxy: OIDC token acquisition failed: %v", err)
				c.AbortWithStatusJSON(http.StatusBadGateway, gin.H{"error": "upstream_auth_unavailable"})
				return
			}
			if tok := oidcHdr.Get("Authorization"); tok != "" {
				ctx := context.WithValue(c.Request.Context(), wsOIDCAuthKey{}, tok)
				c.Request = c.Request.WithContext(ctx)
			}
		}
		wsProxy.ServeHTTP(c.Writer, c.Request)
	}

	apiHandler := func(c *gin.Context) {
		backendURL := resolveBackendURL()

		action := c.Param("action")
		targetURL, err := buildGatewayAPIURL(backendURL, action, c.Request.URL, "/tester")
		if err != nil {
			log.Printf("Proxy Error: Invalid target URL: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		reqPath := ""
		if c.Request.URL != nil {
			reqPath = c.Request.URL.Path
		}
		log.Printf("Proxy Handler triggered for path: %s, target: %s", reqPath, targetURL)

		req, err := http.NewRequestWithContext(c.Request.Context(), c.Request.Method, targetURL, c.Request.Body)
		if err != nil {
			log.Printf("Proxy Error: Failed to create request: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		connTokens := connectionHeaderTokens(c.Request.Header)
		for key, values := range c.Request.Header {
			lowerKey := strings.ToLower(strings.TrimSpace(key))
			if strings.EqualFold(key, "Host") ||
				strings.EqualFold(key, "Authorization") ||
				strings.EqualFold(key, "X-Serverless-Authorization") ||
				strings.HasPrefix(lowerKey, "x-goog-") ||
				isHopByHopHeader(key) {
				continue
			}
			if _, hop := connTokens[lowerKey]; hop {
				continue
			}
			for _, value := range values {
				req.Header.Add(key, value)
			}
		}

		if err := middleware.AttachOIDCTokenE(c.Request.Context(), req.Header, oidcAudience); err != nil {
			log.Printf("Proxy Error: OIDC token acquisition failed: %v", err)
			c.JSON(http.StatusBadGateway, gin.H{"error": "upstream_auth_unavailable"})
			return
		}

		client := proxyClient
		resp, err := client.Do(req)
		if err != nil {
			log.Printf("Proxy Error: Client.Do failed: %v", err)
			if errors.Is(err, context.DeadlineExceeded) {
				c.JSON(http.StatusGatewayTimeout, gin.H{"error": "gateway timeout"})
				return
			}
			c.JSON(http.StatusBadGateway, gin.H{"error": "bad gateway"})
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode >= 400 {
			body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
			resp.Body.Close()
			if readErr != nil {
				log.Printf("Proxy Error: failed reading upstream error body: %v", readErr)
				c.JSON(http.StatusBadGateway, gin.H{"error": "bad gateway"})
				return
			}
			bodyStr := string(body)
			if len(bodyStr) > 500 {
				cut := 500
				for cut > 0 && !utf8.RuneStart(bodyStr[cut]) {
					cut--
				}
				bodyStr = bodyStr[:cut] + "..."
			}
			log.Printf("Proxy: Gateway returned status %d. Body: %s", resp.StatusCode, bodyStr)
			resp.Body = io.NopCloser(bytes.NewBuffer(body))
			resp.ContentLength = int64(len(body))
		}

		for key, values := range resp.Header {
			if isHopByHopOrFramingHeader(key) {
				continue
			}
			for _, value := range values {
				c.Writer.Header().Add(key, value)
			}
		}
		c.DataFromReader(resp.StatusCode, resp.ContentLength, resp.Header.Get("Content-Type"), resp.Body, nil)
	}

	// /tester/* prefix (GCLB path-based routing)
	tg := r.Group("/tester")
	{
		tg.GET("/health", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"status": "ok", "service": "tester"})
		})
		tg.GET("/config.js", configHandler)
		tg.GET("/ws", wsHandler)
		tg.Any("/api/v1/*action", apiHandler)
	}

	// Root level (direct Cloud Run domain access)
	r.GET("/config.js", configHandler)
	r.GET("/ws", wsHandler)
	r.Any("/api/v1/*action", apiHandler)

	serveStatic := func(c *gin.Context, prefix string, dir http.FileSystem) {
		reqPath := ""
		if c.Request.URL != nil {
			reqPath = c.Request.URL.Path
		}
		if strings.HasSuffix(reqPath, "index.html") || reqPath == prefix+"/" || reqPath == prefix {
			c.Header("Cache-Control", "no-cache, no-store, must-revalidate")
			c.Header("Pragma", "no-cache")
			c.Header("Expires", "0")
		}
		if prefix != "" && prefix != "/" {
			http.StripPrefix(prefix, http.FileServer(dir)).ServeHTTP(c.Writer, c.Request)
		} else {
			http.FileServer(dir).ServeHTTP(c.Writer, c.Request)
		}
	}

	r.NoRoute(func(c *gin.Context) {
		if c.Request.Method != "GET" {
			c.Status(http.StatusNotFound)
			return
		}

		reqPath := ""
		if c.Request.URL != nil {
			reqPath = c.Request.URL.Path
		}
		if strings.HasPrefix(reqPath, "/tester/") || reqPath == "/tester" {
			serveStatic(c, "/tester", http.Dir("./web/tester/dist"))
			return
		}
		serveStatic(c, "", http.Dir("./web/tester/dist"))
	})

	r.GET("/tester", func(c *gin.Context) {
		c.Redirect(http.StatusMovedPermanently, "/tester/")
	})

	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok", "service": "tester"})
	})

	return r
}

func main() {
	config.Load()
	logging.Setup()

	// G5b: distributed tracing. No-op when OTEL_EXPORTER is unset.
	tracingShutdown, err := tracing.SetupFromEnv(context.Background(), "race-tester")
	if err != nil {
		log.Printf("⚠️ Tracing setup failed: %v (continuing without traces)", err)
	}
	defer func() { _ = tracingShutdown(context.Background()) }()

	port := config.Optional("PORT", "8080")
	if err := config.ValidatePort(port); err != nil {
		log.Fatalf("❌ Invalid PORT: %v", err)
	}

	gatewayURL := config.Optional("GATEWAY_INTERNAL_URL",
		config.Optional("GATEWAY_URL", config.DefaultGatewayURL))
	oidcAudience := resolveGatewayOIDCAudience(gatewayURL)

	// Operator-only surface reminder. The tester binary proxies to
	// Vertex AEs / the gateway using the runtime SA's OIDC token, so
	// any caller that reaches this process can spend Vertex quota.
	// On Cloud Run this MUST be protected by Cloud Run IAM
	// (operator_principals in infra/modules/cloud-run-iam) — never
	// by an allUsers binding. The startup banner is a defence-in-
	// depth reminder that surfaces in Cloud Logging if anyone ever
	// regresses the IAM posture.
	if os.Getenv("K_SERVICE") != "" {
		log.Printf("⚠️ OPERATOR-ONLY: tester forwards calls under the runtime SA. " +
			"Confirm Cloud Run IAM has NO allUsers binding on this service " +
			"(see infra/modules/cloud-run-iam/variables.tf).")
	}

	r := setupRouter(oidcAudience)
	log.Printf("Tester proxy running on port %s", port)
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           r,
		ReadTimeout:       15 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      0, // Disabled: WebSocket upgrades and 30s upstream proxies outlive 15s
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Failed to start tester proxy: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("Shutting down tester proxy...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal("Tester proxy forced to shutdown:", err)
	}

	log.Println("Tester proxy exiting")
}
