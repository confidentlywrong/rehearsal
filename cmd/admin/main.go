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

	"golang.org/x/sync/errgroup"

	pubsub "cloud.google.com/go/pubsub/v2/apiv1"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/agent"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/GoogleCloudPlatform/race-condition/internal/middleware"
	"github.com/GoogleCloudPlatform/race-condition/internal/tracing"
	"github.com/gin-gonic/gin"
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
		_, getErr := client.GetTopic(context.Background(), &pubsubpb.GetTopicRequest{Topic: topicName})
		if getErr != nil {
			log.Printf("Creating PubSub topic %s in emulator", topicID)
			if _, createErr := client.CreateTopic(context.Background(), &pubsubpb.Topic{Name: topicName}); createErr != nil {
				log.Printf("Warning: Failed to create topic %s: %v", topicID, createErr)
			}
		}
	}
	return &grpcPubSubChecker{client: client}, nil
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
		// Connection may be stale; clear it so next check retries
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
	"planner":                "9105",
	"simulator":              "9104",
	"runner_autopilot":       "9110",
	"planner_with_eval":      "9106",
	"simulator_with_failure": "9107",
	"planner_with_memory":    "9109",
}

// staticServices returns the hardcoded non-agent services.
func staticServices() []ServiceInfo {
	return []ServiceInfo{
		{ID: "admin", Name: "Admin Dash", Category: "admin", Description: "Centralized health monitoring and service portal.", URL: resolveURL("ADMIN", "9121"), HealthPath: "/health", Browseable: true, Type: "go"},
		{ID: "gateway", Name: "Gateway", Category: "core", Description: "Primary API entry point and session router.", URL: resolveURL("GATEWAY", "9101"), HealthPath: "/health", Browseable: true, Type: "go"},
		{ID: "redis", Name: "Redis", Category: "core", Description: "Session state & orchestration store.", URL: "n/a", HealthPath: "/", Browseable: false, Type: "infra"},
		{ID: "pubsub", Name: "PubSub", Category: "core", Description: "Global telemetry bus emulator.", URL: "n/a", HealthPath: "/", Browseable: false, Type: "infra"},
		{ID: "alloydb", Name: "AlloyDB", Category: "core", Description: "Durable session state store (PostgreSQL-compatible).", URL: "n/a", HealthPath: "/", Browseable: false, Type: "infra"},
		{ID: "tester", Name: "Tester UI", Category: "ui", Description: "Manual A2UI component testing laboratory.", URL: resolveURL("TESTER", "9112"), HealthPath: "/health", Browseable: true, Type: "vite"},
		{ID: "dash", Name: "Telemetry Dash", Category: "ui", Description: "Pub/Sub telemetry log and agent activity stream.", URL: resolveURL("DASH", "9111"), HealthPath: "/health", Browseable: true, Type: "python"},
		{ID: "frontend-app", Name: "Primary Frontend", Category: "frontend", Description: "Main consumer-facing simulation interface.", URL: resolveURL("FRONTEND_APP", "9119"), HealthPath: "/health", Browseable: false, Type: "external"},
		{ID: "frontend-bff", Name: "Frontend BFF", Category: "frontend", Description: "Local BFF proxy mirroring cloud frontend service.", URL: resolveURL("FRONTEND_BFF", "9118"), HealthPath: "/health", Browseable: true, Type: "go"},
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

// loadAgentsFromGateway fetches the agent catalog from the gateway's HTTP API.
// This replaces the old loadAgentsFromCatalog that read from a static JSON file.
func loadAgentsFromGateway(gatewayURL string) []ServiceInfo {
	if gatewayURL == "" {
		log.Printf("Warning: GATEWAY_URL not set, cannot discover agents")
		return nil
	}

	url := strings.TrimRight(gatewayURL, "/") + "/api/v1/agent-types"
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		log.Printf("Warning: Could not fetch agent catalog from %s: %v", url, err)
		return nil
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("Warning: Gateway returned %d for agent catalog", resp.StatusCode)
		return nil
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
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
		// Title-case the name for display
		if len(name) > 0 {
			name = strings.ToUpper(name[:1]) + name[1:]
		}

		prefix := strings.ToUpper(id)
		defaultPort := defaultPortMap[id]
		if defaultPort == "" {
			defaultPort = "0"
		}

		// Detect Agent Engine agents from their card URL.
		agentType := "python"
		healthPath := "/health"
		agentURL := resolveURL(prefix, defaultPort)

		if agent.IsAgentEngineURL(entry.URL) {
			agentType = "ae"
			healthPath = aeCardPath
			agentURL = strings.TrimRight(entry.URL, "/")
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

// setupRouter creates the admin Gin engine with all routes.
//
// accountResolver is required; it resolves the per-user identity for
// every request. accountStore may be nil when the admin process has
// no Postgres reach — superadmin endpoints then return 503.
// reconciler may be nil when STRIPE_API_KEY is unset at boot —
// /admin/billing/reconcile returns 503 in that case.
func setupRouter(rdb *redis.Client, psHealthChecker *pubsubHealthChecker, dbHealthChecker *alloydbHealthChecker, gatewayURL string, gcpClient *http.Client, accountResolver account.Resolver, accountStore *account.Store, reconciler *billing.Reconciler) *gin.Engine {
	r := gin.Default()

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

		pubsubStatus := psHealthChecker.Check()
		alloydbConnStr := buildAlloyDBConnStr()
		alloydbStatus := dbHealthChecker.Check(alloydbConnStr)

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

	// Serve index.html for root and any unknown routes (SPA support)
	r.NoRoute(func(c *gin.Context) {
		if c.Request.Method != "GET" {
			c.Status(http.StatusNotFound)
			return
		}

		path := c.Request.URL.Path
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
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
		if err := c.ShouldBindJSON(&body); err != nil {
			log.Printf("ShouldBindJSON: %v", err)
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
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
		if err := c.ShouldBindJSON(&body); err != nil {
			log.Printf("ShouldBindJSON: %v", err)
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
			c.JSON(http.StatusInternalServerError, gin.H{"error": msg})
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
				log.Printf("ShouldBindJSON: %v", err)
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
			UserID:   actorID,
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

	port := config.Optional("PORT", config.Optional("ADMIN_PORT", "9121"))
	if err := config.ValidatePort(port); err != nil {
		log.Fatalf("❌ Invalid PORT: %v", err)
	}

	// Initialize Redis Client
	redisAddr := config.Optional("REDIS_ADDR", "")
	var rdb *redis.Client
	if redisAddr != "" {
		rdb = redis.NewClient(newAdminRedisOptions(redisAddr))
	}

	// Initialize PubSub Health Checker (lazy, self-healing)
	psHealthChecker := newPubSubHealthChecker()

	// Use internal URL for service-to-service agent catalog fetch.
	// The external GATEWAY_URL is IAP-protected and unreachable from Cloud Run.
	gatewayURL := config.Optional("GATEWAY_INTERNAL_URL",
		config.Optional("GATEWAY_URL", "http://127.0.0.1:8101"))
	registry := buildServiceRegistry(gatewayURL)
	gcpClient := newGCPClient()

	dbHealthChecker := newAlloyDBHealthChecker()

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
		// defaultAlloyDBConnect() at the bottom of this file still
		// uses *pgx.Conn, but that path is mutex-serialized (a.mu)
		// so it is not part of this bug class and is left as-is.
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
		WriteTimeout:      15 * time.Second,
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
