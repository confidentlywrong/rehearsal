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
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode"
	"unicode/utf8"

	"golang.org/x/sync/errgroup"

	pubsub "cloud.google.com/go/pubsub/v2/apiv1"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/agent"
	"github.com/GoogleCloudPlatform/race-condition/internal/auth"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/GoogleCloudPlatform/race-condition/internal/middleware"
	"github.com/GoogleCloudPlatform/race-condition/internal/tracing"
	"github.com/gin-gonic/gin"
	"github.com/gin-gonic/gin/binding"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
	"golang.org/x/oauth2/google"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// CategoryInfo defines a service grouping in the dashboard.
type CategoryInfo struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	Icon string `json:"icon"`
}

// ServiceInfo describes a service displayed on the admin dashboard.
type ServiceInfo struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Category    string `json:"category"`
	Description string `json:"description"`
	URL         string `json:"url"`
	HealthPath  string `json:"healthPath"`
	Browseable  bool   `json:"browseable"`
	Type        string `json:"type"`
}

// ServiceRegistryResponse is the JSON payload for /api/v1/services.
type ServiceRegistryResponse struct {
	Categories []CategoryInfo `json:"categories"`
	Services   []ServiceInfo  `json:"services"`
}

// pubsubChecker abstracts PubSub health checking for testability.
type pubsubChecker interface {
	CheckHealth(projectID, topicID string) error
}

// grpcPubSubChecker wraps the real gRPC PubSub admin client.
type grpcPubSubChecker struct {
	client *pubsub.TopicAdminClient
}

func (g *grpcPubSubChecker) CheckHealth(projectID, topicID string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := g.client.GetTopic(ctx, &pubsubpb.GetTopicRequest{
		Topic: fmt.Sprintf("projects/%s/topics/%s", projectID, topicID),
	})
	return err
}

func (g *grpcPubSubChecker) Close() error {
	if g == nil || g.client == nil {
		return nil
	}
	return g.client.Close()
}

// pubsubHealthChecker lazily connects to PubSub and retries on failure.
type pubsubHealthChecker struct {
	mu          sync.Mutex
	checker     pubsubChecker
	connectFunc func() (pubsubChecker, error)
}

func newPubSubHealthChecker() *pubsubHealthChecker {
	return &pubsubHealthChecker{
		connectFunc: defaultPubSubConnect,
	}
}

func defaultPubSubConnect() (pubsubChecker, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// The low-level apiv1 client does NOT respect PUBSUB_EMULATOR_HOST.
	// We must manually configure the endpoint and insecure transport.
	var opts []option.ClientOption
	emulatorHost := config.Optional("PUBSUB_EMULATOR_HOST", "")
	if emulatorHost != "" {
		opts = append(opts,
			option.WithEndpoint(emulatorHost),
			option.WithoutAuthentication(),
			option.WithGRPCDialOption(grpc.WithTransportCredentials(insecure.NewCredentials())),
		)
	}

	client, err := pubsub.NewTopicAdminClient(ctx, opts...)
	if err != nil {
		return nil, err
	}
	// Proactively create topic in emulator mode
	projectID := config.Optional("PUBSUB_PROJECT_ID", config.Optional("PROJECT_ID", ""))
	topicID := config.Optional("PUBSUB_TOPIC_ID", "")
	if emulatorHost != "" && topicID != "" {
		topicName := fmt.Sprintf("projects/%s/topics/%s", projectID, topicID)
		_, getErr := client.GetTopic(ctx, &pubsubpb.GetTopicRequest{Topic: topicName})
		if getErr != nil {
			log.Printf("Creating PubSub topic %s in emulator", topicID)
			if _, createErr := client.CreateTopic(ctx, &pubsubpb.Topic{Name: topicName}); createErr != nil {
				log.Printf("Warning: Failed to create topic %s: %v", topicID, createErr)
			}
		}
	}
	return &grpcPubSubChecker{client: client}, nil
}

// Close releases any underlying gRPC client held by the health checker.
func (p *pubsubHealthChecker) Close() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if closer, ok := p.checker.(io.Closer); ok {
		_ = closer.Close()
	}
	p.checker = nil
}

// Check returns "online" or "offline" based on PubSub health.
// If the client is nil, it attempts to reconnect (lazy initialization).
func (p *pubsubHealthChecker) Check() string {
	projectID := config.Optional("PUBSUB_PROJECT_ID", config.Optional("PROJECT_ID", ""))
	topicID := config.Optional("PUBSUB_TOPIC_ID", "")
	if projectID == "" || topicID == "" {
		return "offline"
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	// Lazy connect / reconnect
	if p.checker == nil {
		checker, err := p.connectFunc()
		if err != nil {
			log.Printf("PubSub connect failed (will retry next check): %v", err)
			return "offline"
		}
		p.checker = checker
	}

	// Health check
	if err := p.checker.CheckHealth(projectID, topicID); err != nil {
		// Connection may be stale; close and clear it so next check retries cleanly.
		if closer, ok := p.checker.(io.Closer); ok {
			_ = closer.Close()
		}
		p.checker = nil
		return "offline"
	}
	return "online"
}

// alloydbPinger abstracts the database ping for testability.
type alloydbPinger interface {
	Ping(ctx context.Context) error
	Close()
}

// alloydbHealthChecker lazily connects to AlloyDB and retries on failure.
// Follows the same pattern as pubsubHealthChecker.
type alloydbHealthChecker struct {
	mu          sync.Mutex
	conn        alloydbPinger
	connectFunc func(connStr string) (alloydbPinger, error)
}

func newAlloyDBHealthChecker() *alloydbHealthChecker {
	return &alloydbHealthChecker{
		connectFunc: defaultAlloyDBConnect,
	}
}

func defaultAlloyDBConnect(connStr string) (alloydbPinger, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := pgxpool.New(ctx, connStr)
	if err != nil {
		return nil, err
	}
	return conn, nil
}

// Check returns "online" or "offline" based on AlloyDB health.
func (a *alloydbHealthChecker) Check(connStr string) string {
	if connStr == "" {
		return "offline"
	}

	a.mu.Lock()
	defer a.mu.Unlock()

	// Lazy connect / reconnect
	if a.conn == nil {
		conn, err := a.connectFunc(connStr)
		if err != nil {
			log.Printf("AlloyDB connect failed (will retry next check): %v", err)
			return "offline"
		}
		a.conn = conn
	}

	// Ping check
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := a.conn.Ping(ctx); err != nil {
		a.conn.Close()
		a.conn = nil
		return "offline"
	}
	return "online"
}

// Close releases any open database pool held by the health checker.
func (a *alloydbHealthChecker) Close() {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.conn != nil {
		a.conn.Close()
		a.conn = nil
	}
}

// wireAdminAccountLookup connects a FirebaseResolver to the Postgres
// users table so IsSuperadmin, Status, deleted_at, and token revocation
// are hydrated on every admin request (matching cmd/gateway and cmd/frontend).
func wireAdminAccountLookup(resolver account.Resolver, store *account.Store) {
	if store == nil {
		return
	}
	if fr, ok := resolver.(*account.FirebaseResolver); ok && fr != nil {
		fr.WithLookup(store.GetUserAnyState)
		log.Printf("ADMIN: FirebaseResolver wired with DB user lookup")
	}
}

// newGCPClient returns an HTTP client with GCP Application Default Credentials.
// Uses OAuth2 access tokens (not ID tokens) with cloud-platform scope,
// matching the pattern in switchboard.go and catalog.go for AE communication.
// Returns nil if ADC is not available (e.g., local dev without gcloud auth).
func newGCPClient() *http.Client {
	client, err := google.DefaultClient(context.Background(),
		"https://www.googleapis.com/auth/cloud-platform",
	)
	if err != nil {
		log.Printf("GCP client unavailable (AE health checks disabled): %v", err)
		return nil
	}
	client.Timeout = 10 * time.Second
	return client
}

var defaultCategories = []CategoryInfo{
	{ID: "admin", Name: "Admin & System", Icon: "⚙️"},
	{ID: "core", Name: "Core Infrastructure", Icon: "🏗️"},
	{ID: "agent", Name: "AI Agents", Icon: "🤖"},
	{ID: "ui", Name: "Developer UIs", Icon: "🖥️"},
	{ID: "frontend", Name: "Frontend", Icon: "🌐"},
}

// aeCardPath is the Agent Engine card endpoint used for health checks.
const aeCardPath = "/a2a/v1/card"

// defaultPortMap provides fallback ports for agents when env vars are not set.
var defaultPortMap = map[string]string{
	"simulator":                      "9104",
	"planner":                        "9105",
	"planner_with_eval":              "9106",
	"simulator_with_failure":         "9107",
	"runner":                         "9108",
	"planner_with_memory":            "9109",
	"runner_autopilot":               "9110",
	"leadership_planner":             "9114",
	"leadership_planner_with_eval":   "9115",
	"leadership_planner_with_memory": "9116",
	"leadership_runner":              "9117",
	"leadership_runner_autopilot":    "9120",
	"ai_echo":                        "9123",
	"ai_echo_reactive":               "9124",
	"ai_echo_autopilot":              "9125",
}

// staticServices returns the hardcoded non-agent services.
func staticServices() []ServiceInfo {
	return []ServiceInfo{
		{ID: "admin", Name: "Admin Dash", Category: "admin", Description: "Centralized health monitoring and service portal.", URL: resolveURL("ADMIN", config.DefaultAdminPort), HealthPath: "/health", Browseable: true, Type: "go"},
		{ID: "gateway", Name: "Gateway", Category: "core", Description: "Primary API entry point and session router.", URL: resolveURL("GATEWAY", config.DefaultGatewayPort), HealthPath: "/health", Browseable: true, Type: "go"},
		{ID: "redis", Name: "Redis", Category: "core", Description: "Session state & orchestration store.", URL: "n/a", HealthPath: "/", Browseable: false, Type: "infra"},
		{ID: "pubsub", Name: "PubSub", Category: "core", Description: "Global telemetry bus emulator.", URL: "n/a", HealthPath: "/", Browseable: false, Type: "infra"},
		{ID: "alloydb", Name: "AlloyDB", Category: "core", Description: "Durable session state store (PostgreSQL-compatible).", URL: "n/a", HealthPath: "/", Browseable: false, Type: "infra"},
		{ID: "tester", Name: "Tester UI", Category: "ui", Description: "Manual A2UI component testing laboratory.", URL: resolveURL("TESTER", config.DefaultTesterPort), HealthPath: "/health", Browseable: true, Type: "vite"},
		{ID: "dash", Name: "Telemetry Dash", Category: "ui", Description: "Pub/Sub telemetry log and agent activity stream.", URL: resolveURL("DASH", "9111"), HealthPath: "/health", Browseable: true, Type: "python"},
		{ID: "frontend-app", Name: "Primary Frontend", Category: "frontend", Description: "Main consumer-facing simulation interface.", URL: resolveURL("FRONTEND_APP", "9119"), HealthPath: "/health", Browseable: false, Type: "external"},
		{ID: "frontend-bff", Name: "Frontend BFF", Category: "frontend", Description: "Local BFF proxy mirroring cloud frontend service.", URL: resolveURL("FRONTEND_BFF", config.DefaultFrontendPort), HealthPath: "/health", Browseable: true, Type: "go"},
	}
}

// checkServicesHealth checks health endpoints for all services concurrently,
// using Cloud Run's internal networking (VPC) to bypass IAP.
// For AE agents (type "ae"), uses gcpClient with authenticated requests.
func checkServicesHealth(services []ServiceInfo, timeout time.Duration, gcpClient *http.Client) map[string]string {
	results := make(map[string]string)
	var mu sync.Mutex

	// Filter to only services with checkable health endpoints
	var checkable []ServiceInfo
	for _, svc := range services {
		if svc.Type == "external" || svc.Type == "infra" {
			continue
		}
		if svc.URL == "" || svc.URL == "n/a" {
			continue
		}
		checkable = append(checkable, svc)
	}

	g := new(errgroup.Group)
	g.SetLimit(10) // max concurrent health checks

	plainClient := &http.Client{Timeout: timeout}

	for _, svc := range checkable {
		svc := svc // pin loop variable
		g.Go(func() error {
			url := svc.URL + svc.HealthPath

			// Use GCP-authenticated client for AE agents
			c := plainClient
			if svc.Type == "ae" && gcpClient != nil {
				c = gcpClient
			}

			resp, err := c.Get(url)
			status := "offline"
			if err == nil {
				_, _ = io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				if resp.StatusCode >= 200 && resp.StatusCode < 300 {
					status = "online"
				}
			}

			mu.Lock()
			results[svc.ID] = status
			mu.Unlock()
			return nil // never fail the group
		})
	}

	_ = g.Wait()
	return results
}

// resolveURL builds a service URL from env vars with a port fallback.
func resolveURL(prefix, defaultPort string) string {
	envKey := prefix + "_URL"
	if v := config.Optional(envKey, ""); v != "" {
		return v
	}
	return "http://127.0.0.1:" + defaultPort
}

// resolveGatewayOIDCAudience determines whether Cloud Run OIDC tokens should
// be minted when calling the Gateway. Local HTTP loopback dev (127.0.0.1 /
// localhost) returns "" so local runs never fail without GCP ADC.
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

// loadAgentsFromGateway fetches the agent catalog from the gateway's HTTP API.
// This replaces the old loadAgentsFromCatalog that read from a static JSON file.
func loadAgentsFromGateway(gatewayURL string) []ServiceInfo {
	if strings.TrimSpace(gatewayURL) == "" {
		log.Printf("Warning: GATEWAY_URL not set, cannot discover agents")
		return nil
	}

	catalogURL := strings.TrimRight(strings.TrimSpace(gatewayURL), "/") + "/api/v1/agent-types"
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, catalogURL, nil)
	if err != nil {
		log.Printf("Warning: Could not build agent catalog request for %s: %v", catalogURL, err)
		return nil
	}
	if aud := resolveGatewayOIDCAudience(gatewayURL); aud != "" {
		if oidcErr := middleware.AttachOIDCTokenAsE(ctx, req.Header, aud, "X-Serverless-Authorization"); oidcErr != nil {
			log.Printf("Warning: Could not acquire OIDC token for gateway catalog %s: %v", catalogURL, oidcErr)
			return nil
		}
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("Warning: Could not fetch agent catalog from %s: %v", catalogURL, err)
		return nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("Warning: Gateway returned %d for agent catalog", resp.StatusCode)
		return nil
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		log.Printf("Warning: Could not read gateway response: %v", err)
		return nil
	}

	// Gateway returns map[string]AgentCard where the key is the agent name.
	// We only need name and description for the admin dashboard.
	var catalog map[string]json.RawMessage
	if err := json.Unmarshal(body, &catalog); err != nil {
		log.Printf("Warning: Could not parse agent catalog from gateway: %v", err)
		return nil
	}

	var agents []ServiceInfo
	for id := range catalog {
		// Extract just the fields we need
		var entry struct {
			Name        string `json:"name"`
			Description string `json:"description"`
			URL         string `json:"url"`
		}
		if err := json.Unmarshal(catalog[id], &entry); err != nil {
			log.Printf("Warning: Could not parse agent %s: %v", id, err)
			continue
		}

		name := entry.Name
		if name == "" {
			name = id
		}
		// Title-case the name for display (UTF-8 rune-safe).
		if r, size := utf8.DecodeRuneInString(name); r != utf8.RuneError && size > 0 {
			name = string(unicode.ToUpper(r)) + name[size:]
		} else if len(name) > 0 {
			name = strings.ToUpper(name[:1]) + name[1:]
		}

		normID := strings.ToLower(strings.ReplaceAll(id, "-", "_"))
		prefix := strings.ToUpper(normID)

		// Detect Agent Engine agents from their card URL.
		agentType := "python"
		healthPath := "/health"
		var agentURL string
		switch {
		case agent.IsAgentEngineURL(entry.URL):
			agentType = "ae"
			healthPath = aeCardPath
			agentURL = strings.TrimRight(entry.URL, "/")
		case strings.TrimSpace(config.Optional(prefix+"_URL", "")) != "":
			agentURL = strings.TrimSpace(config.Optional(prefix+"_URL", ""))
		case auth.IsCloudRunURL(entry.URL):
			agentURL = auth.AudienceFor(entry.URL)
		case defaultPortMap[normID] != "":
			agentURL = "http://127.0.0.1:" + defaultPortMap[normID]
		default:
			if aud := auth.AudienceFor(entry.URL); aud != "" {
				agentURL = aud
			} else {
				agentURL = "n/a"
			}
		}

		agents = append(agents, ServiceInfo{
			ID:          id,
			Name:        name,
			Category:    "agent",
			Description: entry.Description,
			URL:         agentURL,
			HealthPath:  healthPath,
			Browseable:  false,
			Type:        agentType,
		})
	}
	log.Printf("Discovered %d agents from gateway", len(agents))
	return agents
}

// buildServiceRegistry merges static services with dynamically loaded agents.
func buildServiceRegistry(gatewayURL string) ServiceRegistryResponse {
	services := staticServices()
	agents := loadAgentsFromGateway(gatewayURL)
	services = append(services, agents...)
	return ServiceRegistryResponse{
		Categories: defaultCategories,
		Services:   services,
	}
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

func environmentResetHandler(gatewayURL, oidcAudience string, client *http.Client) gin.HandlerFunc {
	if client == nil {
		client = &http.Client{Timeout: 25 * time.Second}
	}
	trimmedBase := strings.TrimRight(strings.TrimSpace(gatewayURL), "/")
	return func(c *gin.Context) {
		if trimmedBase == "" {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "gateway URL not configured"})
			return
		}
		parsed, err := url.Parse(trimmedBase)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "gateway URL invalid"})
			return
		}

		var bodyBytes []byte
		if c.Request.Body != nil {
			c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
			bodyBytes, err = io.ReadAll(c.Request.Body)
			if err != nil {
				var maxErr *http.MaxBytesError
				if errors.As(err, &maxErr) {
					c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request_body_too_large"})
					return
				}
				c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
				return
			}
		}

		ctx, cancel := context.WithTimeout(c.Request.Context(), 25*time.Second)
		defer cancel()

		targetURL := trimmedBase + "/api/v1/environment/reset"
		var bodyReader io.Reader
		if len(bodyBytes) > 0 {
			bodyReader = bytes.NewReader(bodyBytes)
		}
		outReq, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, bodyReader)
		if err != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "gateway request construction failed"})
			return
		}

		if ct := strings.TrimSpace(c.GetHeader("Content-Type")); ct != "" {
			outReq.Header.Set("Content-Type", ct)
		} else if len(bodyBytes) > 0 {
			outReq.Header.Set("Content-Type", "application/json")
		}
		for _, h := range []string{"Authorization", "Cookie", "X-Firebase-AppCheck", "X-Local-Dev-Superadmin", "X-Request-ID"} {
			if vals := c.Request.Header.Values(h); len(vals) > 0 {
				for _, v := range vals {
					outReq.Header.Add(h, v)
				}
			}
		}
		// Ensure caller cannot smuggle internal OIDC or GCP headers.
		outReq.Header.Del("X-Serverless-Authorization")
		for k := range outReq.Header {
			if strings.HasPrefix(strings.ToLower(k), "x-goog-") {
				outReq.Header.Del(k)
			}
		}

		if oidcAudience != "" {
			if oidcErr := middleware.AttachOIDCTokenAsE(ctx, outReq.Header, oidcAudience, "X-Serverless-Authorization"); oidcErr != nil {
				log.Printf("ADMIN: gateway reset OIDC token error: %v", oidcErr)
				c.JSON(http.StatusBadGateway, gin.H{"error": "gateway service authentication unavailable"})
				return
			}
		}

		resp, err := client.Do(outReq)
		if err != nil {
			log.Printf("ADMIN: gateway reset proxy error: %v", err)
			if errors.Is(err, context.DeadlineExceeded) {
				c.JSON(http.StatusGatewayTimeout, gin.H{"error": "gateway timeout"})
				return
			}
			c.JSON(http.StatusBadGateway, gin.H{"error": "gateway unreachable"})
			return
		}
		defer resp.Body.Close()

		respBody, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		if readErr != nil {
			log.Printf("ADMIN: gateway reset response read error: %v", readErr)
			c.JSON(http.StatusBadGateway, gin.H{"error": "gateway response unreadable"})
			return
		}

		contentType := resp.Header.Get("Content-Type")
		if contentType == "" {
			contentType = "application/json; charset=utf-8"
		}
		c.Data(resp.StatusCode, contentType, respBody)
	}
}

// setupRouter creates the admin Gin engine with all routes.
//
// accountResolver is required; it resolves the per-user identity for
// every request. accountStore may be nil when the admin process has
// no Postgres reach — superadmin endpoints then return 503.
// reconciler may be nil when STRIPE_API_KEY is unset at boot —
// /admin/billing/reconcile returns 503 in that case.
func setupRouter(rdb *redis.Client, psHealthChecker *pubsubHealthChecker, dbHealthChecker *alloydbHealthChecker, gatewayURL string, gcpClient *http.Client, accountResolver account.Resolver, accountStore *account.Store, reconciler *billing.Reconciler) *gin.Engine {
	r := gin.Default()
	if err := configureTrustedProxies(r); err != nil {
		log.Printf("ADMIN: invalid TRUSTED_PROXIES (%v); disabling trusted proxies", err)
		_ = r.SetTrustedProxies(nil)
	}

	// G5a: structured request log + request_id propagation. Mounted
	// FIRST so request_id is on ctx for everything downstream.
	r.Use(middleware.RequestLogger())

	// G5b: distributed tracing. No-op when tracing.Setup wasn't
	// called or OTEL_EXPORTER is unset.
	r.Use(middleware.RequestTracing("race-admin"))

	// G4a: hardening headers (CSP report-only by default + X-Frame-Options,
	// X-Content-Type-Options, Referrer-Policy, Permissions-Policy, optional
	// HSTS on HTTPS). Mounted BEFORE CORS so OPTIONS preflight responses
	// also carry the headers.
	r.Use(middleware.SecurityHeaders(middleware.SecurityHeadersFromEnv()))

	r.Use(middleware.CORS(os.Getenv("CORS_ALLOWED_ORIGINS")))

	// Per-user identity. Mounted before everything except logging /
	// tracing / security headers / CORS so handlers downstream can
	// read account.FromContext(ctx). SkipPaths covers the
	// SPA-shell static endpoints + health checks (no identity needed).
	r.Use(middleware.Account(middleware.AccountOptions{
		Resolver: accountResolver,
		SkipPaths: []string{
			"/health",
			"/config.js",
			"/favicon.ico",
			"/assets",
		},
	}))

	// API Routes (no prefix)
	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok", "service": "admin"})
	})

	r.GET("/api/v1/services", func(c *gin.Context) {
		registry := buildServiceRegistry(gatewayURL)
		c.JSON(http.StatusOK, registry)
	})

	r.GET("/api/v1/health/infra", func(c *gin.Context) {
		redisStatus := "offline"
		if rdb != nil {
			ctx, cancel := context.WithTimeout(c.Request.Context(), 1*time.Second)
			defer cancel()
			if err := rdb.Ping(ctx).Err(); err == nil {
				redisStatus = "online"
			}
		}

		pubsubStatus := "offline"
		if psHealthChecker != nil {
			pubsubStatus = psHealthChecker.Check()
		}
		alloydbStatus := "offline"
		if dbHealthChecker != nil {
			alloydbConnStr := buildAlloyDBConnStr()
			alloydbStatus = dbHealthChecker.Check(alloydbConnStr)
		}

		c.JSON(http.StatusOK, gin.H{
			"redis":   redisStatus,
			"pubsub":  pubsubStatus,
			"alloydb": alloydbStatus,
		})
	})

	r.GET("/api/v1/health/services", func(c *gin.Context) {
		registry := buildServiceRegistry(gatewayURL)
		health := checkServicesHealth(registry.Services, 3*time.Second, gcpClient)
		c.JSON(http.StatusOK, health)
	})

	r.GET("/config.js", middleware.ConfigJSHandler(map[string]string{
		"GATEWAY_URL":      config.Optional("GATEWAY_URL", ""),
		"SIMULATOR_URL":    config.Optional("SIMULATOR_URL", ""),
		"PLANNER_URL":      config.Optional("PLANNER_URL", ""),
		"TESTER_URL":       config.Optional("TESTER_URL", ""),
		"ADMIN_URL":        config.Optional("ADMIN_URL", ""),
		"DASH_URL":         config.Optional("DASH_URL", ""),
		"FRONTEND_APP_URL": config.Optional("FRONTEND_APP_URL", ""),
		"FRONTEND_BFF_URL": config.Optional("FRONTEND_BFF_URL", ""),
		"REDIS_URL":        "n/a",
		"PUBSUB_URL":       "n/a",
		"ALLOYDB_URL":      "n/a",
	}))

	// Static files sharing from dist root
	r.StaticFS("/assets", http.Dir("./web/admin-dash/dist/assets"))
	r.StaticFile("/favicon.ico", "./web/admin-dash/dist/favicon.ico")

	// All /admin/* routes are operator-only. RequireSuperadmin
	// reads is_superadmin from the UserContext attached upstream by
	// the Account middleware. Returns 401 when the user is missing,
	// 403 when present but not superadmin.
	requireSA := middleware.RequireSuperadmin()

	// E1: superadmin-only Stripe-to-DB reconcile. The "fix the
	// drift" lever for when a webhook was missed. Per HARD RULE
	// this is operator-triggered ONLY — no scheduler. Returns
	// 503 if reconciler is nil (STRIPE_API_KEY missing at boot).
	r.POST("/admin/billing/reconcile",
		requireSA,
		reconcileBillingHandler(accountStore, reconciler),
	)

	// Per-user plan change. Manual / enterprise override path.
	r.PUT("/admin/users/:id/plan",
		requireSA,
		setUserPlanHandler(accountStore),
	)

	// E2: GDPR control-plane endpoints. All operator-only; none of
	// them auto-fire. The audit row for each lands BEFORE the
	// destructive write so a partial failure still leaves a paper
	// trail.
	r.DELETE("/admin/users/:id",
		requireSA,
		softDeleteUserHandler(accountStore),
	)
	r.GET("/admin/users/:id/export",
		requireSA,
		exportUserHandler(accountStore),
	)
	r.POST("/admin/users/:id/erase",
		requireSA,
		eraseUserHandler(accountStore),
	)

	// E2a: bulk hard-delete sweep. Operator-triggered only (HARD
	// RULE: no scheduler). Reads ?older_than_days=N (>=1), computes
	// the cutoff, returns {purged, older_than_days, cutoff}.
	r.POST("/admin/users/hard-delete",
		requireSA,
		hardDeleteExpiredUsersHandler(accountStore),
	)

	// Superadmin-only read of every live user row. Used by the
	// admin dashboard Users tab.
	r.GET("/admin/users",
		requireSA,
		listUsersHandler(accountStore),
	)

	// Manual-approval flow (2026-06-26). Superadmin advances a
	// user from StatusPending to StatusApproved or StatusRejected.
	// Both endpoints are idempotent and write an audit row before
	// returning so the "Audit" tab shows who flipped the gate.
	r.POST("/admin/users/:id/approve",
		requireSA,
		setUserStatusHandler(accountStore, account.StatusApproved),
	)
	r.POST("/admin/users/:id/reject",
		requireSA,
		setUserStatusHandler(accountStore, account.StatusRejected),
	)

	// Superadmin-only audit-log read.
	// Optional query params: ?user_id=&action=&limit=&offset=
	// Limit clamping (default 50, max 500) lives in the account
	// package; the handler just maps query params through.
	r.GET("/admin/audit",
		requireSA,
		listAuditHandler(accountStore),
	)

	// Per-user usage viewer. Reads from the usageReaderForAdmin
	// seam wired in main(); stays nil in degraded mode → handler
	// returns 503.
	r.GET("/admin/users/:id/usage",
		requireSA,
		usageUserHandler(usageReaderForAdmin, nil),
	)

	// Authenticated BFF proxy for environment reset -> Gateway /api/v1/environment/reset.
	resetHTTPClient := &http.Client{Timeout: 25 * time.Second}
	r.POST("/api/admin/environment/reset",
		requireSA,
		environmentResetHandler(gatewayURL, resolveGatewayOIDCAudience(gatewayURL), resetHTTPClient),
	)

	// Serve index.html for root and any unknown routes (SPA support)
	r.NoRoute(func(c *gin.Context) {
		var path string
		if c.Request.URL != nil {
			path = c.Request.URL.Path
		}
		if strings.HasPrefix(path, "/api/") || path == "/api" || strings.HasPrefix(path, "/admin/") || path == "/admin" {
			c.JSON(http.StatusNotFound, gin.H{"error": "not found"})
			return
		}
		if c.Request.Method != "GET" {
			c.Status(http.StatusNotFound)
			return
		}

		// If it looks like a file (has an extension) or is in /assets, and we are here, it's a 404
		if strings.Contains(path, ".") || strings.HasPrefix(path, "/assets/") {
			c.Status(http.StatusNotFound)
			return
		}

		c.File("./web/admin-dash/dist/index.html")
	})

	return r
}

// buildAlloyDBConnStr assembles a PostgreSQL connection string from
// the AlloyDB env vars used elsewhere in admin (DATABASE_URL or
// ALLOYDB_* triplet). Returns the empty string when no credentials are
// configured — callers must treat that as "DB disabled".
//
// We deliberately keep this admin-local rather than promoting it to
// internal/config because Python agents read DATABASE_URL via Honcho
// and that has separate semantics in the agent layer.
func buildAlloyDBConnStr() string {
	if v := config.Optional("DATABASE_URL", ""); v != "" {
		return v
	}
	host := config.Optional("ALLOYDB_HOST", "")
	port := config.Optional("ALLOYDB_PORT", "")
	user := config.Optional("ALLOYDB_USER", "")
	pass := config.Optional("ALLOYDB_PASSWORD", "")
	db := config.Optional("ALLOYDB_DATABASE", "")
	if host == "" || port == "" || user == "" || db == "" {
		return ""
	}
	u := &url.URL{
		Scheme: "postgresql",
		User:   url.UserPassword(user, pass),
		Host:   fmt.Sprintf("%s:%s", host, port),
		Path:   "/" + db,
	}
	return u.String()
}

var errTrailingJSON = errors.New("unexpected trailing data after JSON value")

// decodeStrictJSON decodes a single JSON value from c.Request.Body (capped at
// maxBytes via http.MaxBytesReader), rejecting trailing non-whitespace tokens
// or concatenated JSON values.
func decodeStrictJSON(c *gin.Context, dst any, maxBytes int64) error {
	if c.Request == nil || c.Request.Body == nil {
		return errors.New("request body is empty")
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxBytes)
	dec := json.NewDecoder(c.Request.Body)
	if err := dec.Decode(dst); err != nil {
		return err
	}
	var extra any
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errTrailingJSON
		}
		return err
	}
	if binding.Validator != nil {
		if err := binding.Validator.ValidateStruct(dst); err != nil {
			return err
		}
	}
	return nil
}

// setUserPlanHandler returns a gin handler that changes the plan
// tier on an existing user. Operator (superadmin) only — the
// /admin/users/:id/plan path is gated by RequireSuperadmin.
//
// Response codes:
//   - 200 with the new plan on success.
//   - 400 when plan_tier is missing, malformed, or not one of the
//     four catalog tiers (account.ErrInvalidPlan).
//   - 404 when the user does not exist or is soft-deleted.
//   - 503 when the account store is nil (degraded mode).
func setUserPlanHandler(store *account.Store) gin.HandlerFunc {
	type req struct {
		PlanTier string `json:"plan_tier" binding:"required"`
	}
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		userID := strings.TrimSpace(c.Param("id"))
		if userID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "user id required"})
			return
		}
		var body req
		if err := decodeStrictJSON(c, &body, 1<<20); err != nil {
			var maxErr *http.MaxBytesError
			if errors.As(err, &maxErr) {
				c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request_body_too_large"})
				return
			}
			log.Printf("decodeStrictJSON: %v", err)
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
			return
		}
		plan := strings.TrimSpace(body.PlanTier)
		if plan == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "plan_tier required"})
			return
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()
		// Capture BEFORE tier for audit metadata. Miss here is
		// non-fatal; the update below will report ErrNotFound.
		var fromTier string
		if prev, gerr := store.GetUser(ctx, userID); gerr == nil {
			fromTier = string(prev.PlanTier)
		}
		err := store.SetPlan(ctx, userID, account.PlanTier(plan))
		switch {
		case err == nil:
			recordAuditBestEffort(ctx, store, c, account.AuditEntry{
				UserID: userID,
				Action: account.ActionUserPlanChanged,
				Target: "plan:" + plan,
				Metadata: map[string]any{
					"from":   fromTier,
					"to":     plan,
					"source": "manual",
				},
			})
			c.JSON(http.StatusOK, gin.H{
				"id":        userID,
				"plan_tier": plan,
			})
		case errors.Is(err, account.ErrInvalidPlan):
			c.JSON(http.StatusBadRequest, gin.H{
				"error":     "invalid plan_tier",
				"plan_tier": plan,
			})
		case errors.Is(err, account.ErrNotFound):
			c.JSON(http.StatusNotFound, gin.H{
				"error": "user not found",
				"id":    userID,
			})
		default:
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
		}
	}
}

// reconcileBillingHandler returns a gin handler that runs an
// operator-triggered Stripe-to-DB reconcile for one user. E1 — the
// "fix the drift" lever. Operator-triggered only (HARD RULE: no
// scheduler).
//
// Request body: {"user_id": "..."}.
// Response: 200 with the ReconcileResult (from_tier, to_tier,
// price_id, changed).
//
// Status codes:
//   - 200 success — reconcile completed (may or may not have
//     changed state; check `changed`).
//   - 400 missing/blank user_id.
//   - 503 reconciler not configured (no STRIPE_API_KEY at boot)
//     OR user has no stripe_customer_id (never checked out)
//     OR the active subscription's price isn't in STRIPE_PRICE_MAP.
//     The error message disambiguates for the operator.
//   - 404 user_id not found.
//   - 500 any other unexpected error.
func reconcileBillingHandler(store *account.Store, reconciler *billing.Reconciler) gin.HandlerFunc {
	type req struct {
		UserID string `json:"user_id" binding:"required"`
	}
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		if reconciler == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "reconciler not configured (STRIPE_API_KEY missing at boot)",
			})
			return
		}
		var body req
		if err := decodeStrictJSON(c, &body, 1<<20); err != nil {
			var maxErr *http.MaxBytesError
			if errors.As(err, &maxErr) {
				c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request_body_too_large"})
				return
			}
			log.Printf("decodeStrictJSON: %v", err)
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
			return
		}
		userID := strings.TrimSpace(body.UserID)
		if userID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "user_id required"})
			return
		}
		// Generous timeout: Stripe US-East round-trip ~150ms p99,
		// 15s absorbs even a degraded Stripe while still beating
		// the ingress 30s idle.
		ctx, cancel := context.WithTimeout(c.Request.Context(), 15*time.Second)
		defer cancel()
		result, err := reconciler.Reconcile(ctx, userID)
		switch {
		case err == nil:
			recordAuditBestEffort(ctx, store, c, account.AuditEntry{
				UserID: userID,
				Action: account.ActionBillingReconciled,
				Target: userID,
				Metadata: map[string]any{
					"source":    "manual",
					"from_tier": result.FromTier,
					"to_tier":   result.ToTier,
					"price_id":  result.PriceID,
					"changed":   result.Changed,
				},
			})
			c.JSON(http.StatusOK, result)
		case errors.Is(err, account.ErrNotFound):
			c.JSON(http.StatusNotFound, gin.H{
				"error": "user not found",
				"id":    userID,
			})
		default:
			// Reconciler errors for "no stripe customer" /
			// "unmapped price" are plain errors (not sentinel);
			// surface them as 503 so the operator can correlate
			// with the env var or onboarding state.
			msg := err.Error()
			if strings.Contains(msg, "no stripe customer") ||
				strings.Contains(msg, "unmapped price") {
				c.JSON(http.StatusServiceUnavailable, gin.H{
					"error": msg,
					"id":    userID,
				})
				return
			}
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
		}
	}
}

// listUsersHandler returns a gin handler that emits every live user
// row. Superadmin-only "Users" tab data source.
//
// Response codes:
//   - 200 with `{users: [...]}` on success. Always emits `users: []`
//     (never null) so the dashboard iterator never crashes.
//   - 503 when the account store is nil (degraded mode).
//   - 500 on any DB error.
func listUsersHandler(store *account.Store) gin.HandlerFunc {
	type userOut struct {
		ID               string     `json:"id"`
		Email            string     `json:"email"`
		DisplayName      string     `json:"display_name,omitempty"`
		PlanTier         string     `json:"plan_tier"`
		StripeCustomerID string     `json:"stripe_customer_id,omitempty"`
		PaymentFailedAt  *time.Time `json:"payment_failed_at,omitempty"`
		IsSuperadmin     bool       `json:"is_superadmin"`
		CreatedAt        time.Time  `json:"created_at"`
		// Manual-approval flow (2026-06-26). Status is one of
		// "pending", "approved", "rejected". ApprovedAt and
		// ApprovedBy are nullable — only set once an operator has
		// advanced the row out of pending.
		Status     string     `json:"status"`
		ApprovedAt *time.Time `json:"approved_at,omitempty"`
		ApprovedBy string     `json:"approved_by,omitempty"`
	}
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()
		rows, err := store.ListUsers(ctx)
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		out := make([]userOut, 0, len(rows))
		for _, u := range rows {
			out = append(out, userOut{
				ID:               u.ID,
				Email:            u.Email,
				DisplayName:      u.DisplayName,
				PlanTier:         string(u.PlanTier),
				StripeCustomerID: u.StripeCustomerID,
				PaymentFailedAt:  u.PaymentFailedAt,
				IsSuperadmin:     u.IsSuperadmin,
				CreatedAt:        u.CreatedAt,
				Status:           string(u.Status),
				ApprovedAt:       u.ApprovedAt,
				ApprovedBy:       u.ApprovedBy,
			})
		}
		c.JSON(http.StatusOK, gin.H{"users": out})
	}
}

// setUserStatusHandler advances a user row through the manual-
// approval lifecycle (StatusPending → StatusApproved or
// StatusRejected). The handler is bound twice in the router — once
// for /admin/users/:id/approve and once for /admin/users/:id/reject —
// with the target status hardcoded so the URL path is the only
// caller-controlled input. Idempotent: re-firing the same status
// is a no-op (matches SetUserPlan's behaviour).
//
// Response codes:
//   - 200 with `{id, status}` on success.
//   - 400 ErrInvalidStatus  — defensive only; the URL pins it.
//   - 404 ErrNotFound      — user id is unknown or soft-deleted.
//   - 503 when the account store is nil.
//   - 500 on any other DB error.
func setUserStatusHandler(store *account.Store, target account.UserStatus) gin.HandlerFunc {
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		userID := strings.TrimSpace(c.Param("id"))
		if userID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "missing user id"})
			return
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()
		// Actor id for audit + approved_by. Pulled from the
		// UserContext attached upstream by middleware.Account()
		// (RequireSuperadmin has already ensured a context is
		// present). If somehow missing (defensive), fall back to
		// the literal "admin" so the audit row is still write-
		// able with a non-empty actor.
		acc := account.FromContext(c.Request.Context())
		actorID := "admin"
		if acc.UserID != "" {
			actorID = acc.UserID
		}
		err := store.SetUserStatus(ctx, userID, target, actorID)
		if err != nil {
			switch {
			case errors.Is(err, account.ErrInvalidStatus):
				log.Printf("SetUserStatus: %v", err)
				c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
				return
			case errors.Is(err, account.ErrNotFound):
				c.JSON(http.StatusNotFound, gin.H{"error": "user not found"})
				return
			default:
				log.Printf("handler error: %v", err)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
				return
			}
		}
		action := account.ActionUserApproved
		if target == account.StatusRejected {
			action = account.ActionUserRejected
		}
		recordAuditBestEffort(ctx, store, c, account.AuditEntry{
			UserID:   userID,
			ActorID:  actorID,
			Action:   action,
			Target:   userID,
			Metadata: map[string]any{"status": string(target)},
		})
		c.JSON(http.StatusOK, gin.H{
			"id":     userID,
			"status": string(target),
		})
	}
}

// listAuditHandler emits audit_log rows matching the query params.
// Superadmin-only "Audit" tab data source.
//
// Query parameters (all optional):
//   - user_id : restrict to a single subject
//   - action  : restrict to one action type (e.g. user.plan_changed)
//   - limit   : max rows (default 50, store clamps to 500)
//   - offset  : pagination offset (default 0)
//
// Bad limit/offset values are coerced rather than 400'd; an
// operator's query-string typo shouldn't break the dashboard.
func listAuditHandler(store *account.Store) gin.HandlerFunc {
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		userID := strings.TrimSpace(c.Query("user_id"))
		action := strings.TrimSpace(c.Query("action"))
		limit := 0
		offset := 0
		if v := c.Query("limit"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n > 0 {
				limit = n
			}
		}
		if v := c.Query("offset"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n >= 0 {
				offset = n
			}
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()
		entries, err := store.ListAudit(ctx, userID, action, limit, offset)
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		if entries == nil {
			entries = []account.AuditEntry{}
		}
		c.JSON(http.StatusOK, gin.H{"entries": entries})
	}
}

// recordAuditBestEffort writes an audit entry and logs (but does
// not surface) any failure. UserID and ActorID are auto-filled from
// the resolved UserContext when the caller leaves them empty:
//
//   - UserID (the subject) defaults to the acting user; superadmin
//     actions on a different user must set it explicitly.
//   - ActorID (who performed the action) always reflects the caller's
//     id from UserContext, even when UserID is overridden.
func recordAuditBestEffort(ctx context.Context, store *account.Store, c *gin.Context, entry account.AuditEntry) {
	if store == nil {
		return
	}
	uc := account.FromContext(c.Request.Context())
	if entry.ActorID == "" && uc.UserID != "" {
		entry.ActorID = uc.UserID
	}
	if entry.UserID == "" && uc.UserID != "" {
		entry.UserID = uc.UserID
	}
	if err := store.RecordAudit(ctx, entry); err != nil {
		// Log to stderr — the access-log row + this line let an
		// operator reconstruct the missing audit entry. We don't
		// retry: the next state change will record afresh; an
		// audit-table outage is operational, not user-facing.
		log.Printf("AUDIT: best-effort record failed: action=%s user=%s err=%v",
			entry.Action, entry.UserID, err)
	}
}

// newAdminRedisOptions returns Redis client options tuned for admin's
// workload (health checks and registry queries only).
func newAdminRedisOptions(addr string) *redis.Options {
	return &redis.Options{
		Addr:         addr,
		Password:     os.Getenv("REDIS_PASSWORD"),
		PoolSize:     5,
		MinIdleConns: 1,
		PoolTimeout:  5 * time.Second,
	}
}

func main() {
	config.Load()
	logging.Setup()

	// G5b: distributed tracing. No-op when OTEL_EXPORTER is unset.
	tracingShutdown, err := tracing.SetupFromEnv(context.Background(), "race-admin")
	if err != nil {
		log.Printf("⚠️ Tracing setup failed: %v (continuing without traces)", err)
	}
	defer func() { _ = tracingShutdown(context.Background()) }()

	port := config.Optional("PORT", config.Optional("ADMIN_PORT", config.DefaultAdminPort))
	if err := config.ValidatePort(port); err != nil {
		log.Fatalf("❌ Invalid PORT: %v", err)
	}

	// Initialize Redis Client
	redisAddr := config.Optional("REDIS_ADDR", "")
	var rdb *redis.Client
	if redisAddr != "" {
		rdb = redis.NewClient(newAdminRedisOptions(redisAddr))
		defer func() { _ = rdb.Close() }()
	}

	// Initialize PubSub Health Checker (lazy, self-healing)
	psHealthChecker := newPubSubHealthChecker()
	defer psHealthChecker.Close()

	// Use internal URL for service-to-service agent catalog fetch.
	// The external GATEWAY_URL is IAP-protected and unreachable from Cloud Run.
	gatewayURL := config.Optional("GATEWAY_INTERNAL_URL",
		config.Optional("GATEWAY_URL", config.DefaultGatewayURL))
	registry := buildServiceRegistry(gatewayURL)
	gcpClient := newGCPClient()

	dbHealthChecker := newAlloyDBHealthChecker()
	defer dbHealthChecker.Close()

	// Build the per-user account resolver + optional store. The
	// resolver is ALWAYS required (the Account middleware fails
	// fast without it). The store is best-effort: when Postgres is
	// not reachable, superadmin endpoints return 503 and the
	// /healthz panel surfaces the DB ping result separately.
	accountResolver, err := middleware.AccountResolverFromEnv()
	if err != nil {
		log.Fatalf("ADMIN: account resolver: %v", err)
	}
	var accountStore *account.Store
	var reconciler *billing.Reconciler
	var adminDBPool *pgxpool.Pool
	connStr := buildAlloyDBConnStr()
	if connStr == "" {
		log.Printf("ADMIN: no DATABASE_URL/ALLOYDB_* configured; superadmin endpoints will return 503")
	} else {
		// 2026-06-21 audit P1 #1: runtime queries must use a pool
		// because *pgx.Conn is NOT safe for concurrent goroutines.
		// Migration uses a session-level pg_advisory_lock, which is
		// tied to a single connection — it MUST run on a temporary
		// *pgx.Conn (not a pool) so the lock/body/unlock land on
		// the same physical session. Order: temp-conn migrate →
		// close temp → open runtime pool.
		//
		// NOTE: the secondary alloydbHealthChecker around
		// defaultAlloyDBConnect() (lines 223-231) uses *pgxpool.Pool
		// and is mutex-serialized (a.mu).
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		migConn, connErr := pgx.Connect(ctx, connStr)
		cancel()
		if connErr != nil {
			log.Printf("ADMIN: account DB connect failed (superadmin endpoints will return 503): %v", connErr)
		} else {
			// Apply the per-user schema BEFORE opening the
			// runtime pool. A migration failure logs and leaves
			// accountStore nil rather than a half-applied schema.
			mctx, mcancel := context.WithTimeout(context.Background(), 30*time.Second)
			mErr := account.ApplyMigration(mctx, migConn)
			mcancel()
			closeCtx, closeCancel := context.WithTimeout(context.Background(), 2*time.Second)
			_ = migConn.Close(closeCtx)
			closeCancel()
			if mErr != nil {
				log.Printf("ADMIN: account migration failed; superadmin endpoints disabled: %v", mErr)
			} else {
				pctx, pcancel := context.WithTimeout(context.Background(), 5*time.Second)
				pool, perr := pgxpool.New(pctx, connStr)
				pcancel()
				if perr != nil {
					log.Printf("ADMIN: account DB pool open failed; superadmin endpoints disabled: %v", perr)
				} else {
					adminDBPool = pool
					accountStore = account.NewStore(pool)
					// G6: same pgxpool powers the per-user usage
					// viewer. billing.Store has read-only methods
					// (GetMonthlyUsage + GetAgentBreakdown) so
					// sharing the pool keeps connection count flat.
					usageReaderForAdmin = billing.NewStore(pool)
					log.Printf("ADMIN: per-user usage reader ready (pgxpool)")
				}
			}
		}
	}
	if adminDBPool != nil {
		defer adminDBPool.Close()
	}
	wireAdminAccountLookup(accountResolver, accountStore)

	// E1: build the Stripe reconciler. Armed only when
	// STRIPE_API_KEY is set AND a valid STRIPE_PRICE_MAP parsed
	// AND the account store is reachable. When any of these is
	// missing reconciler stays nil and /admin/billing/reconcile
	// returns 503 with a clear message rather than 500-ing.
	if accountStore != nil {
		if key := strings.TrimSpace(os.Getenv("STRIPE_API_KEY")); key != "" {
			pm, perr := billing.LoadPriceMapFromEnv(os.Getenv("STRIPE_PRICE_MAP"))
			if perr != nil {
				log.Printf("ADMIN: STRIPE_PRICE_MAP parse failed; reconcile endpoint disabled: %v", perr)
			} else if fetcher, ferr := billing.NewStripeSubscriptionFetcher(key); ferr != nil {
				log.Printf("ADMIN: subscription fetcher init failed; reconcile endpoint disabled: %v", ferr)
			} else {
				reconciler = &billing.Reconciler{
					Users:    accountStore,
					Fetcher:  fetcher,
					PriceMap: pm,
					Writer:   accountStore,
				}
				log.Printf("ADMIN: reconcile endpoint armed")
			}
		}
	}

	r := setupRouter(rdb, psHealthChecker, dbHealthChecker, gatewayURL, gcpClient, accountResolver, accountStore, reconciler)
	_ = registry // initial build validates connectivity at startup
	log.Printf("Admin UI serving on port %s", port)

	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           r,
		ReadTimeout:       15 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Failed to run admin server: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("Shutting down admin server...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal("Admin server forced to shutdown:", err)
	}

	log.Println("Admin server exiting")
}
