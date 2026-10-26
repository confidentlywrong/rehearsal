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
	"bytes"
	"context"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/auth"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/GoogleCloudPlatform/race-condition/internal/middleware"
	"github.com/GoogleCloudPlatform/race-condition/internal/tracing"
	"github.com/gin-gonic/gin"
	"github.com/koding/websocketproxy"
)

var proxyClient = &http.Client{Timeout: 30 * time.Second}

// setupRouter creates the tester Gin engine. oidcAudience is the OIDC
// audience for proxied gateway requests; empty disables attachment.
func setupRouter(oidcAudience string) *gin.Engine {
	r := gin.Default()

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
	if strings.EqualFold(os.Getenv("TESTER_REQUIRE_AUTH"), "true") {
		r.Use(func(c *gin.Context) {
			if c.Request.URL.Path == "/health" {
				c.Next()
				return
			}
			authz := c.GetHeader("Authorization")
			if !strings.HasPrefix(authz, "Bearer ") || len(strings.TrimPrefix(authz, "Bearer ")) == 0 {
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
		log.Printf("Incoming Request: %s %s (Remote: %s)", c.Request.Method, c.Request.URL.Path, c.ClientIP())
		c.Next()
	})

	configHandler := middleware.ConfigJSHandler(map[string]string{
		"VITE_GATEWAY_URL": "/ws",
	})

	backendURL := config.Optional("GATEWAY_INTERNAL_URL",
		config.Optional("GATEWAY_URL", "http://localhost:8101"))

	wsURLStr := strings.Replace(backendURL, "http://", "ws://", 1)
	wsURLStr = strings.Replace(wsURLStr, "https://", "wss://", 1)
	if !strings.HasSuffix(wsURLStr, "/ws") {
		wsURLStr = strings.TrimSuffix(wsURLStr, "/") + "/ws"
	}

	wsU, err := url.Parse(wsURLStr)
	if err != nil {
		log.Fatalf("WebSocket Proxy: Invalid URL %q: %v", wsURLStr, err)
	}
	log.Printf("WebSocket Proxy: Initializing for %s", wsURLStr)
	wsProxy := websocketproxy.NewProxy(wsU)

	wsProxy.Director = func(incoming *http.Request, out http.Header) {
		out.Set("Host", wsU.Host)
		for key := range out {
			if strings.HasPrefix(strings.ToLower(key), "x-goog-iap-") {
				out.Del(key)
			}
		}
		middleware.AttachOIDCToken(incoming.Context(), out, oidcAudience)
	}

	wsHandler := func(c *gin.Context) {
		wsProxy.ServeHTTP(c.Writer, c.Request)
	}

	apiHandler := func(c *gin.Context) {
		backendURL := config.Optional("GATEWAY_INTERNAL_URL",
			config.Optional("GATEWAY_URL", "http://localhost:8101"))

		action := c.Param("action")
		targetURL := strings.TrimSuffix(backendURL, "/") + "/api/v1" + action
		if c.Request.URL.RawQuery != "" {
			targetURL += "?" + c.Request.URL.RawQuery
		}

		log.Printf("Proxy Handler triggered for path: %s, target: %s", c.Request.URL.Path, targetURL)

		req, err := http.NewRequest(c.Request.Method, targetURL, c.Request.Body)
		if err != nil {
			log.Printf("Proxy Error: Failed to create request: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		for key, values := range c.Request.Header {
			if strings.EqualFold(key, "Host") || strings.EqualFold(key, "Authorization") || strings.HasPrefix(strings.ToLower(key), "x-goog-") {
				continue
			}
			for _, value := range values {
				req.Header.Add(key, value)
			}
		}

		middleware.AttachOIDCToken(c.Request.Context(), req.Header, oidcAudience)

		client := proxyClient
		resp, err := client.Do(req)
		if err != nil {
			log.Printf("Proxy Error: Client.Do failed: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode >= 400 {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
			bodyStr := string(body)
			if len(bodyStr) > 500 {
				bodyStr = bodyStr[:500] + "..."
			}
			log.Printf("Proxy: Gateway returned status %d. Body: %s", resp.StatusCode, bodyStr)
			resp.Body = io.NopCloser(bytes.NewBuffer(body))
			resp.ContentLength = int64(len(body))
		}

		for key, values := range resp.Header {
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
		if strings.HasSuffix(c.Request.URL.Path, "index.html") || c.Request.URL.Path == prefix+"/" || c.Request.URL.Path == prefix {
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

		path := c.Request.URL.Path
		if strings.HasPrefix(path, "/tester/") || path == "/tester" {
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
		config.Optional("GATEWAY_URL", ""))
	oidcAudience := auth.ResolveAudience(gatewayURL)

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
		WriteTimeout:      15 * time.Second,
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
