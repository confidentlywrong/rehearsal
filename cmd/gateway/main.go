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
	"fmt"
	"log"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/gen_proto/gateway"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/agent"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/hub"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/GoogleCloudPlatform/race-condition/internal/memory"
	"github.com/GoogleCloudPlatform/race-condition/internal/middleware"
	"github.com/GoogleCloudPlatform/race-condition/internal/session"
	"github.com/GoogleCloudPlatform/race-condition/internal/tracing"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
	"google.golang.org/protobuf/proto"
)

var validSessionID = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,128}$`)

// H8 fix: Track in-flight dispatch goroutines so shutdown can await them.
var dispatchWG sync.WaitGroup

// Shutdown guard: prevents new dispatch goroutines from being spawned
// after the gateway starts shutting down. Without this, a WebSocket
// message arriving during shutdown can call dispatchWG.Add(1)
// concurrently with dispatchWG.Wait(), causing a runtime panic.
var shuttingDown atomic.Bool

// quotaChecker is the SaaS Phase 5a pre-flight quota gate. Wired by
// main() after billingStore is constructed; stays nil under
// SAAS_MODE=off and in tests. When nil, the spawn handlers skip the
// quota check entirely so legacy single-tenant deployments are
// byte-identical to today.
//
// We keep this as a package-level variable rather than threading it
// through setupRouter to avoid touching the 20+ test call sites that
// pin the setupRouter signature — same approach the /api/v1/usage
// route uses in Phase 4b.
var quotaChecker billing.QuotaChecker

// auditRecorder is the best-effort audit sink wired by main() to
// an *account.Store when Postgres is reachable; nil otherwise. ALL
// audit emit paths fail open: a nil recorder, a closed DB, or a
// metadata-marshal error never blocks the caller's response.
//
// Package-level for the same reason as quotaChecker: avoid
// threading a new param through setupRouter and breaking the
// existing test call sites.
type auditRecorder interface {
	RecordAudit(ctx context.Context, e account.AuditEntry) error
}

var auditor auditRecorder

// accountStoreForMe holds the *account.Store needed by the
// /api/v1/me + /api/v1/me/init + profile endpoints. Wired in
// main() to the SAME pgx connection that backs
// billingStore/auditor — opening a second pool would be wasteful
// and pgx connections are safe to share across goroutines for
// the short Exec/Query patterns these handlers use. Stays nil
// when Postgres is unreachable, in which case /me handlers
// return 503 or degraded shells.
var accountStoreForMe meStore

// memoryStoreForGateway holds the *memory.Store backing the
// /api/v1/memory/* surface. Wired in main() to the SAME pgx pool
// as accountStoreForMe. Stays nil when Postgres is unreachable,
// in which case mountMemoryRoutes is a no-op and the SPA's memory
// tabs return network errors on first call — matches the /me degraded posture.
var memoryStoreForGateway memStore

// accountResolverForRouter overrides the resolver setupRouter would
// otherwise build via middleware.AccountResolverFromEnv(). main()
// assigns it AFTER accountStoreForMe is constructed so the
// FirebaseResolver can be wired with the user-lookup hook. Tests
// leave it nil; setupRouter falls back to the env-built resolver
// in that case.
var accountResolverForRouter account.Resolver

// dbPingerForHealthz is the G8 readiness seam. Wired by main() to
// the pgx connection's Ping method when SAAS_MODE=on AND the
// tenancy migration succeeded. Stays nil in tests and under
// SAAS_MODE=off, in which case /healthz keeps its legacy
// Redis-only contract (byte-identical to today). When set, a
// failed DB ping flips /healthz to 503 so Cloud Run pulls the
// instance out of LB rotation while DB is unreachable.
//
// Package-level for the same reason as quotaChecker/auditor: avoid
// threading a new param through setupRouter and breaking the 20+
// existing test call sites.
var dbPingerForHealthz func(ctx context.Context) error

// recordAuditBestEffort writes one audit row and swallows any
// failure with a structured log line. Mirrors cmd/admin's helper
// of the same name.
//
// UserID and ActorID are auto-filled from the resolved
// UserContext when the caller leaves them empty:
//
//   - UserID (the subject) defaults to the acting user.
//   - ActorID (who performed the action) always reflects the
//     caller's id from UserContext.
func recordAuditBestEffort(c *gin.Context, entry account.AuditEntry) {
	if auditor == nil {
		return
	}
	uc := account.FromContext(c.Request.Context())
	if entry.ActorID == "" && uc.UserID != "" {
		entry.ActorID = uc.UserID
	}
	if entry.UserID == "" && uc.UserID != "" {
		entry.UserID = uc.UserID
	}
	if err := auditor.RecordAudit(c.Request.Context(), entry); err != nil {
		logging.FromContext(c.Request.Context()).Warn("audit.record_failed",
			slog.String("action", entry.Action),
			slog.String("user_id", entry.UserID),
			slog.String("error", err.Error()),
		)
	}
}

// enforceQuotaGate runs the pre-flight check for one request and
// returns true when the caller should reject. The caller is expected
// to have already written the response in that case.
//
// activeSimsForUser is an optional callback the handler supplies
// so the gate can apply the per-plan concurrency cap (e.g. "Free
// tier: 1 concurrent run"). When nil, the concurrency check is
// skipped. Always called BEFORE the token cap check so a
// concurrency rejection takes precedence over a (possibly cached)
// token state.
//
// Fail-open contract: any error (plan not found, DB unreachable,
// missing user context, registry hiccup) is LOGGED and treated
// as "allow through" — a misconfigured billing DB must not lock
// customers out. The post-flight metering pipeline still records
// actual usage, so over-cap runs surface in the usage tile + audit
// log even when the pre-flight gate is degraded.
func enforceQuotaGate(c *gin.Context, activeSimsForUser func(context.Context) (int, error)) bool {
	if quotaChecker == nil {
		return false
	}
	uc := account.FromContext(c.Request.Context())
	if uc.UserID == "" {
		return false
	}
	state, err := quotaChecker.CheckQuota(c.Request.Context(), uc.UserID)
	if err != nil {
		logging.FromContext(c.Request.Context()).Warn("quota.preflight_failed",
			slog.String("user_id", uc.UserID),
			slog.String("error", err.Error()),
			slog.Bool("fail_open", true),
		)
		return false
	}

	// Concurrency cap: plan.MaxConcurrentRuns < 0 means
	// "unlimited" (enterprise). When the registry callback is
	// provided AND the plan has a real cap, count active sims for
	// this user and reject when at-or-above the limit. Counting
	// failures fail-open (log + allow) for the same reason token
	// failures do.
	if activeSimsForUser != nil && state.MaxConcurrentRuns >= 0 {
		active, cerr := activeSimsForUser(c.Request.Context())
		if cerr != nil {
			logging.FromContext(c.Request.Context()).Warn("quota.concurrency_count_failed",
				slog.String("user_id", uc.UserID),
				slog.String("error", cerr.Error()),
				slog.Bool("fail_open", true),
			)
		} else if active >= state.MaxConcurrentRuns {
			c.JSON(http.StatusPaymentRequired, gin.H{
				"error": "concurrency_exhausted",
			})
			recordAuditBestEffort(c, account.AuditEntry{
				Action: account.ActionQuotaRejected,
				Target: c.Request.URL.Path,
				Metadata: map[string]any{
					"reason":              "concurrency_exhausted",
					"plan_tier":           state.PlanTier,
					"active_simulations":  active,
					"max_concurrent_runs": state.MaxConcurrentRuns,
					"route":               c.Request.URL.Path,
				},
			})
			return true
		}
	}

	if state.WarnHigh {
		// Advisory only — surface to the client but allow the spawn.
		// Header name is part of the dashboard tile's quota-warning
		// contract (per-user since the org pivot).
		c.Header("X-Usage-Warning", "near-cap")
	}
	if !state.Exhausted {
		return false
	}
	// Hard reject. The 402 status + machine-readable code lets the
	// Angular tile distinguish quota rejections from other 4xx and
	// surface the right upgrade banner.
	c.JSON(http.StatusPaymentRequired, gin.H{
		"error": "quota_exhausted",
	})
	// Audit-log token-cap rejections. Same fail-open semantics:
	// audit write failures must not change the 402 response.
	recordAuditBestEffort(c, account.AuditEntry{
		Action: account.ActionQuotaRejected,
		Target: c.Request.URL.Path,
		Metadata: map[string]any{
			"reason":      "quota_exhausted",
			"plan_tier":   state.PlanTier,
			"tokens_used": state.TokensUsed,
			"tokens_cap":  state.TokensCap,
			"year_month":  state.YearMonth,
			"route":       c.Request.URL.Path,
		},
	})
	return true
}

// countActiveSimulationsFor returns a closure suitable for
// enforceQuotaGate's `activeSimsForUser` parameter. The closure
// delegates to the supplied registry's ListSimulations, which is
// user-scoped via the request context — so no manual filtering
// by user is needed here.
func countActiveSimulationsFor(reg session.DistributedRegistry) func(context.Context) (int, error) {
	if reg == nil {
		return nil
	}
	return func(ctx context.Context) (int, error) {
		sims, err := reg.ListSimulations(ctx)
		if err != nil {
			return 0, err
		}
		return len(sims), nil
	}
}

// newRedisOptions returns production Redis client options with right-sized pool
// configuration. PoolSize=50 balances connection usage against the Memorystore
// connection budget; the gateway uses Redis pipeline batching, so fewer
// concurrent connections are needed than the raw operation count suggests.
//
// B-27 (2026-06-23 audit): ConnMaxIdleTime was 5 minutes which, on a
// service that spends most of its time idle between bursty
// orchestrations, churned the pool down to MinIdleConns and forced
// reconnects (handshake + TLS) at the start of every new burst. 30
// minutes keeps the warm pool around long enough to absorb typical
// inter-burst gaps without holding connections so long that
// Memorystore's own idle-timer prunes them server-side (default 0 =
// never, but operators sometimes set it to 1h).
func newRedisOptions(addr string) *redis.Options {
	return &redis.Options{
		Addr:            addr,
		Password:        os.Getenv("REDIS_PASSWORD"),
		PoolSize:        50,
		MinIdleConns:    10,
		PoolTimeout:     10 * time.Second,
		ConnMaxIdleTime: 30 * time.Minute,
	}
}

var upgrader = websocket.Upgrader{
	HandshakeTimeout: 10 * time.Second,
	CheckOrigin: func(r *http.Request) bool {
		allowedOrigins := config.Optional("CORS_ALLOWED_ORIGINS", "")
		// P2 (2026-06-23 audit): mirror the middleware.CORS
		// wildcard-hardening contract. A wildcard or empty value is
		// REFUSED in deployed environments (Cloud Run sets K_SERVICE
		// automatically) or when CORS_REFUSE_WILDCARD=true is set
		// explicitly. Without this guard a misconfigured deploy
		// (empty CORS_ALLOWED_ORIGINS) silently accepted WebSocket
		// upgrades from any origin even though the HTTP CORS layer
		// would have blocked the corresponding fetch.
		onCloudRun := os.Getenv("K_SERVICE") != ""
		refuseWildcard := onCloudRun || strings.EqualFold(os.Getenv("CORS_REFUSE_WILDCARD"), "true")
		if allowedOrigins == "" || allowedOrigins == "*" {
			if refuseWildcard {
				log.Printf("WebSocket: refusing wildcard origin in deployed env (K_SERVICE=%q origin=%q)",
					os.Getenv("K_SERVICE"), r.Header.Get("Origin"))
				return false
			}
			return true // dev mode
		}
		origin := r.Header.Get("Origin")
		for _, allowed := range strings.Split(allowedOrigins, ",") {
			if strings.TrimSpace(allowed) == origin {
				return true
			}
		}
		return false
	},
}

// dispatchEvent routes an event through the switchboard.
// When agentType is specified, it dispatches only to that agent.
// When agentType is empty, it broadcasts to all agents.
func dispatchEvent(ctx context.Context, sb hub.Switchboard, agentType string, event interface{}) {
	if sb == nil {
		log.Printf("DISPATCH: no switchboard available, skipping")
		return
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if agentType != "" {
		if err := sb.DispatchToAgent(ctx, agentType, event); err != nil {
			log.Printf("ERROR DISPATCH: targeted dispatch to %s failed: %v", agentType, err)
		}
	} else {
		// Route through account.Key so per-user broadcasts land
		// on a per-user channel. Returns the literal
		// "simulation:broadcast" when there is no UserContext on
		// the (background) ctx. The request-scoped path below
		// (environment_reset) is the one that actually carries
		// a user.
		channel := account.Key(ctx, "simulation", "broadcast")
		if err := sb.PublishOrchestration(ctx, channel, event); err != nil {
			log.Printf("ERROR DISPATCH: broadcast failed: %v", err)
		}
	}
}

func handleBinaryMessage(ctx context.Context, sid string, message []byte, sb hub.Switchboard, reg session.DistributedRegistry, h *hub.Hub) {
	log.Printf("GATEWAY: Received binary message from session [%s] (size: %d bytes)", sid, len(message))
	var wrapper gateway.Wrapper
	if err := proto.Unmarshal(message, &wrapper); err == nil {
		log.Printf("GATEWAY: Parsed Wrapper msg. Type=%s, Event=%s", wrapper.Type, wrapper.Event)
		// Gateway handles: broadcast, tool_call, narrative
		if wrapper.Type == "broadcast" {
			log.Printf("GATEWAY: Processing broadcast intent")
			// 1. Internal fan-out to other Gateway instances
			if sb != nil {
				if err := sb.Broadcast(ctx, &wrapper); err != nil {
					log.Printf("GATEWAY: broadcast error: %v", err)
				}
			}

			// 2. Orchestration fan-out to Python agents
			var broadcastReq gateway.BroadcastRequest
			if err := proto.Unmarshal(wrapper.Payload, &broadcastReq); err == nil {
				log.Printf("GATEWAY: Successfully unmarshaled BroadcastRequest (targets: %v)", broadcastReq.TargetSessionIds)
				event := map[string]interface{}{
					"type":    "broadcast",
					"eventId": wrapper.RequestId,
					"payload": map[string]interface{}{
						"data":    string(broadcastReq.Payload),
						"targets": broadcastReq.TargetSessionIds,
					},
				}

				// Session-targeted dispatch: look up which agent types own the
				// target sessions and dispatch only to those types. This prevents
				// broadcasting to all active agents when only specific agents
				// should receive the message.
				if shuttingDown.Load() {
					log.Printf("GATEWAY: Ignoring broadcast dispatch during shutdown")
				} else {
				dispatchWG.Add(1)
				go func() {
					defer dispatchWG.Done()
					acc := account.FromContext(ctx)
					bgCtxBase, bgCancel := context.WithTimeout(context.Background(), 10*time.Second)
					defer bgCancel()
					bgCtx := account.WithContext(bgCtxBase, acc)
					if reg == nil || len(broadcastReq.TargetSessionIds) == 0 {
						// Fallback: no registry or no targets → broadcast to all
						dispatchEvent(bgCtx, sb, "", event)
						return
					}
					dispatched := make(map[string]bool)
					// B1: derive from `ctx` (carries the tenant from the
					// WebSocket upgrade) rather than context.Background(),
					// so reg.FindAgentType reads the org-prefixed Redis
					// keys instead of the unscoped global namespace.
					lookupCtx, cancel := context.WithTimeout(bgCtx, 5*time.Second)
					defer cancel()
					for _, targetSID := range broadcastReq.TargetSessionIds {
						agentType, found, err := reg.FindAgentType(lookupCtx, targetSID)
						if err != nil || !found {
							log.Printf("GATEWAY: Session %s not found in registry, skipping", targetSID)
							continue
						}
						if dispatched[agentType] {
							continue // already dispatched to this agent type
						}
						dispatched[agentType] = true
						log.Printf("GATEWAY: Targeted dispatch to %s for session %s", agentType, targetSID)
						dispatchEvent(bgCtx, sb, agentType, event)
					}
					if len(dispatched) == 0 {
						log.Printf("GATEWAY: No agent types found for targets, falling back to broadcast")
						dispatchEvent(bgCtx, sb, "", event)
					}
				}()
				}
			} else {
				log.Printf("GATEWAY: Failed to unmarshal BroadcastRequest: %v", err)
				if h != nil {
					h.SendMessage(sid, "error", "bad_request", map[string]string{"details": "Failed to unmarshal BroadcastRequest proto"})
				}
			}
		}
		if wrapper.Type == "a2ui_action" {
			log.Printf("GATEWAY: Processing A2UI action")
			var action gateway.A2UIAction
			if err := proto.Unmarshal(wrapper.Payload, &action); err == nil {
				log.Printf("GATEWAY: A2UI action=%s for session=%s", action.ActionName, action.SessionId)
				event := map[string]interface{}{
					"type":      "a2ui_action",
					"eventId":   wrapper.RequestId,
					"sessionId": action.SessionId,
					"payload": map[string]interface{}{
						"actionName": action.ActionName,
						"sessionId":  action.SessionId,
					},
				}
				if shuttingDown.Load() {
					log.Printf("GATEWAY: Ignoring A2UI dispatch during shutdown")
				} else {
				dispatchWG.Add(1)
				go func() {
					defer dispatchWG.Done()
					acc := account.FromContext(ctx)
					bgCtxBase, bgCancel := context.WithTimeout(context.Background(), 10*time.Second)
					defer bgCancel()
					bgCtx := account.WithContext(bgCtxBase, acc)
					if reg == nil {
						log.Printf("GATEWAY: No registry, cannot route A2UI action")
						return
					}
					// B1: same tenant-scoping fix as the broadcast branch.
					lookupCtx, cancel := context.WithTimeout(bgCtx, 5*time.Second)
					defer cancel()
					agentType, found, err := reg.FindAgentType(lookupCtx, action.SessionId)
					if err != nil || !found {
						log.Printf("GATEWAY: Session %s not found in registry for A2UI action", action.SessionId)
						return
					}
					log.Printf("GATEWAY: Dispatching A2UI action to %s", agentType)
					dispatchEvent(bgCtx, sb, agentType, event)
				}()
				}
			} else {
				log.Printf("GATEWAY: Failed to unmarshal A2UIAction: %v", err)
				if h != nil {
					h.SendMessage(sid, "error", "bad_request", map[string]string{"details": "Failed to unmarshal A2UIAction proto"})
				}
			}
		}
		// Future: Handle tool_call and narrative from here if needed
	} else {
		log.Printf("GATEWAY: Failed to unmarshal Wrapper proto: %v", err)
		if h != nil {
			h.SendMessage(sid, "error", "bad_request", map[string]string{"details": "Failed to unmarshal Wrapper proto"})
		}
	}
}

func setupRouter(h *hub.Hub, sb hub.Switchboard, catalog *agent.Catalog, reg session.DistributedRegistry, gatewayID string, rdb *redis.Client, drainer hub.PubSubDrainer) *gin.Engine {
	r := gin.Default()

	// G5a: structured request log + request_id propagation. Mounted
	// FIRST so request_id is on ctx for everything downstream and the
	// deferred end-of-request log captures tenant attrs added later.
	r.Use(middleware.RequestLogger())

	// G5b: distributed tracing. No-op when tracing.Setup wasn't
	// called or OTEL_EXPORTER is unset; otherwise creates one span
	// per request and propagates W3C tracecontext to downstream
	// services so a single trace covers gateway → admin → billing.
	r.Use(middleware.RequestTracing("race-gateway"))

	// G4a: hardening headers (CSP report-only by default + X-Frame-Options,
	// X-Content-Type-Options, Referrer-Policy, Permissions-Policy, optional
	// HSTS on HTTPS). Mounted BEFORE CORS so OPTIONS preflight responses
	// also carry the headers — some browsers cache preflights without
	// them, defeating the protection.
	r.Use(middleware.SecurityHeaders(middleware.SecurityHeadersFromEnv()))

	r.Use(middleware.CORS(os.Getenv("CORS_ALLOWED_ORIGINS")))

	// Firebase App Check (2026-06-22 audit P2.B): verifies the
	// X-Firebase-AppCheck JWT attached by the SPA / login page.
	// Default mode is "off" so existing deploys are unaffected; set
	// APPCHECK_ENFORCE=report-only first, watch logs for a cycle,
	// then flip to "enforce". Mounted before Account so a stolen
	// Firebase ID token replayed via curl is rejected before it
	// reaches user-resolution code paths.
	r.Use(middleware.AppCheck(middleware.AppCheckFromEnv(os.Getenv)))

	// Account middleware: every request that lands here has a
	// UserContext on its ctx (resolved via Firebase or LocalResolver
	// in dev). SkipPaths leaves probes and the no-auth helpers open.
	resolver := accountResolverForRouter
	if resolver == nil {
		var err error
		resolver, err = middleware.AccountResolverFromEnv()
		if err != nil {
			log.Fatalf("GATEWAY: account resolver: %v", err)
		}
	}
	r.Use(middleware.Account(middleware.AccountOptions{
		Resolver: resolver,
		// /health, /healthz, /metrics, and /config must remain
		// unauthenticated so Cloud Run probes and the SPA bootstrap
		// keep working without a token.
		SkipPaths: []string{"/health", "/healthz", "/metrics", "/config"},
		// InitPaths: endpoints reachable when the user has a valid
		// Firebase identity but no users-table row yet
		// (UserContext.NeedsInit=true — the brand-new-signup state).
		// Without this allow-list, the middleware 409s every request
		// AND the SPA can never call /me/init to create the row, so
		// new signups loop forever. Bug surfaced 2026-06-22 as
		// "Sign-in failed at the session exchange step (HTTP 409)".
		//
		//   /api/v1/me           — the SPA's first call after sign-in.
		//                          meHandler explicitly handles
		//                          ErrNotFound by returning needs_init=true
		//                          so the SPA can route into /me/init.
		//   /api/v1/me/init      — the row-creation endpoint itself
		//                          (initMeHandler is idempotent).
		//   /api/v1/auth/exchange — mints the __session cookie from the
		//                          Firebase ID token; must succeed before
		//                          the SPA can even load the document
		//                          where /me/init would run.
		//   /api/v1/me/logout    — escape hatch so a confused new
		//                          user can sign out without being
		//                          forced to complete onboarding.
		InitPaths: []string{
			"/api/v1/me",
			"/api/v1/me/init",
			"/api/v1/auth/exchange",
			"/api/v1/me/logout",
		},
		// ApprovedPaths: routes reachable when the user has a row
		// but is not yet StatusApproved (manual-approval flow,
		// 2026-06-26). The SPA needs /me to read its own status,
		// /me/logout to bail out, and /auth/exchange in case the
		// session cookie expires while the user is waiting on the
		// "pending" screen. Every other path returns 403
		// account_pending or account_rejected via
		// defaultNotApproved. Mirrors InitPaths intentionally — a
		// pending user has the same surface area as a needs_init
		// user.
		ApprovedPaths: []string{
			"/api/v1/me",
			"/api/v1/me/init",
			"/api/v1/auth/exchange",
			"/api/v1/me/logout",
		},
	}))

	// Per-user rate limiter (originally Phase 7b in the multi-tenant
	// SaaS plan; the codebase pivoted to per-user, but the limiter
	// itself — a Redis-backed token bucket keyed on UserID — is
	// unchanged). Sits AFTER the account middleware so the bucket
	// is keyed on the resolved UserID.
	// Skips probes (same set as the account-middleware SkipPaths) plus
	// the metering + usage endpoints which are themselves rate-bounded
	// by their upstream drainer. Misconfigured capacity/refill → noop
	// (logged at boot). Redis outage → fail-open allow (logged
	// per-request). The Redis client is reused from the gateway's
	// existing connection so we don't double the pool.
	// P1 #8: safe rate-limit defaults. The Terraform tfvars don't currently
	// set RATELIMIT_CAPACITY / RATELIMIT_REFILL_PER_SEC, so prior to this
	// the limiter collapsed to a no-op in production and a single user
	// could DoS Vertex AE budget via /api/v1/sessions. Defaults: 60-request
	// burst, 1 req/sec steady-state — generous for a UI client, tight enough
	// to stop scripted abuse. Operators can still tighten or loosen via env.
	rlOpts := middleware.RateLimitFromEnv(rdb, os.Getenv).WithSkips(
		"/health", "/metrics", "/api/v1/usage", "/api/v1/plans",
	)
	if rlOpts.Capacity <= 0 {
		rlOpts.Capacity = 60
	}
	if rlOpts.RefillPerSecond <= 0 {
		rlOpts.RefillPerSecond = 1.0
	}
	r.Use(middleware.RateLimitIfEnabled(rlOpts))

	// Per-route rate limiters (P3 #8 extension, 2026-06-19 audit).
	// The global limiter above guards against bulk abuse across any
	// endpoint; these two extra buckets guard specifically against
	// Vertex/Gemini cost spikes and destructive resets. Each route
	// they're mounted on must pass BOTH the global bucket and the
	// route-specific bucket. KeyPrefix is distinct per limiter so
	// the buckets do not collide in Redis.
	//
	// vertexLimiter: 10-burst / 1 request per 5 seconds steady. An
	// interactive run typically fires a handful of sessions/spawns
	// per minute; an abusive script trying to drain budget hits the
	// wall almost immediately.
	//
	// destructiveLimiter: 3-burst / 1 request per 20 seconds. These
	// routes wipe environment state or flush sessions \u2014 nobody has
	// a legitimate reason to fire them in a loop.
	//
	// Redis outage \u2192 fail-open allow (same contract as the global
	// limiter). Nil rdb \u2192 no-op (matches RateLimit's guard).
	vertexLimiter := middleware.RateLimit(middleware.RateLimitOptions{
		Client:          rdb,
		Capacity:        10,
		RefillPerSecond: 0.2,
		KeyPrefix:       "ratelimit:vertex:",
	})
	destructiveLimiter := middleware.RateLimit(middleware.RateLimitOptions{
		Client:          rdb,
		Capacity:        3,
		RefillPerSecond: 0.05,
		KeyPrefix:       "ratelimit:destructive:",
	})
	// NOTE: the /api/v1/auth/exchange limiter is constructed in
	// main() instead of here, because that route is registered in
	// main() alongside the per-user /me routes (both gated on
	// accountStoreForMe). See "authExchangeLimiter" at the route
	// registration site for the policy + rationale (P2.1,
	// 2026-06-22 audit).

	// MAX_RUNNERS — GLOBAL ceiling on total runners across all
	// users in a single /spawn request (default 100, clamped to
	// [1, 1000]). Local dev default: 100. GCP deployments set
	// MAX_RUNNERS=1000 via .env.dev. Read once at startup; changes
	// require a gateway restart.
	//
	// ─────────────────────────────────────────────────────────
	// Precedence semantics (2026-06-21 audit P3 #8)
	// ─────────────────────────────────────────────────────────
	// There are two concurrency caps in play. They are independent
	// gates and the request must clear BOTH:
	//
	//   1. Per-user `plan.MaxConcurrentRuns` — enforced by
	//      enforceQuotaGate (see line ~145 and call sites at
	//      lines ~708 and ~997). Sourced from the plans table,
	//      keyed on the requester's plan_tier. Read fresh per
	//      request.
	//
	//   2. Global MAX_RUNNERS (this constant) — enforced inside
	//      the /spawn handler (~line 1032) by capping the
	//      total `req.Agents[*].Count`.
	//
	// THE GLOBAL CAP WINS. A request that fits the user's plan
	// but pushes the per-process total above MAX_RUNNERS is
	// silently capped, NOT split fairly across active users.
	// Concrete consequence: user A burning 99 of 100 runners
	// starves user B even when B's plan would allow 50
	// concurrent. This is intentional for now — MAX_RUNNERS
	// exists to protect the simulator from over-subscription,
	// not to provide fairness — but it is NOT a true
	// multi-tenant isolation primitive. Document this in any
	// "fair-share" customer conversation.
	//
	// If/when fairness is needed (operator decision, not a code
	// change to drop in casually), two viable approaches:
	//   - replace the cap with `min(plan.MaxConcurrentRuns,
	//     MAX_RUNNERS / activeUserCount)` (requires a live
	//     active-user counter — Redis SCAN over session keys);
	//   - drop the global cap entirely and rely on plan tiers +
	//     Cloud Run min/max-instance limits to bound load.
	// Either path needs an explicit ADR before merging — there is
	// no "obvious" right answer here.
	maxRunners := 100
	if v := os.Getenv("MAX_RUNNERS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 {
			maxRunners = n
			if maxRunners > 1000 {
				maxRunners = 1000
			}
		} else {
			log.Printf("WARNING: invalid MAX_RUNNERS=%q (must be integer 1-1000), using default %d", v, maxRunners)
		}
	}

	r.GET("/health", func(c *gin.Context) {
		redisStatus := "online"
		if sb != nil {
			if err := sb.Ping(c.Request.Context()); err != nil {
				redisStatus = "offline"
			}
		} else {
			redisStatus = "unknown"
		}

		var pubsubStatus string
		pubsubHost := os.Getenv("PUBSUB_EMULATOR_HOST")
		if pubsubHost == "" {
			pubsubStatus = "not_configured"
		} else {
			pubsubStatus = "online"
			conn, err := net.DialTimeout("tcp", pubsubHost, 1*time.Second)
			if err != nil {
				pubsubStatus = "offline"
			} else {
				conn.Close()
			}
		}

		httpStatus := http.StatusOK
		overallStatus := "ok"
		if redisStatus == "offline" {
			httpStatus = http.StatusServiceUnavailable
			overallStatus = "degraded"
		}

		c.JSON(httpStatus, gin.H{
			"status":  overallStatus,
			"service": "gateway",
			"infra": gin.H{
				"redis":  redisStatus,
				"pubsub": pubsubStatus,
			},
			"metrics": gin.H{
				"broadcast_queue_len": h.BroadcastQueueLen(),
			},
		})
	})

	// Strict health probe for uptime checks and load balancer readiness.
	// Returns 503 when Redis is unreachable (unlike /health which is informational).
	// G8: when dbPingerForHealthz is wired (SAAS_MODE=on path), also ping
	// the tenancy Postgres connection so a DB outage pulls the instance out
	// of LB rotation. Stays Redis-only when the seam is nil (tests +
	// SAAS_MODE=off) so legacy behavior is byte-identical.
	r.GET("/healthz", func(c *gin.Context) {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 1*time.Second)
		defer cancel()
		if rdb == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"status": "unhealthy",
				"reason": "redis client not initialized",
			})
			return
		}
		if err := rdb.Ping(ctx).Err(); err != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"status": "unhealthy",
				"reason": "redis unreachable",
			})
			return
		}
		if dbPingerForHealthz != nil {
			if err := dbPingerForHealthz(ctx); err != nil {
				c.JSON(http.StatusServiceUnavailable, gin.H{
					"status": "unhealthy",
					"reason": "postgres unreachable",
				})
				return
			}
		}
		c.JSON(http.StatusOK, gin.H{"status": "healthy"})
	})

	// Agent Discovery API
	r.GET("/api/v1/agent-types", func(c *gin.Context) {
		agents, err := catalog.DiscoverAgents()
		if err != nil {
			log.Printf("AGENT_DISCOVERY_ERROR: %v", err)
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "failed to discover any agents. Check AGENT_URLS and ensure agents are running."})
			return
		}
		c.JSON(http.StatusOK, agents)
	})

	// Session Management API
	r.GET("/api/v1/sessions", func(c *gin.Context) {
		sessions, err := reg.ListSessions(c.Request.Context())
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		c.JSON(http.StatusOK, sessions)
	})

	r.POST("/api/v1/sessions", vertexLimiter, func(c *gin.Context) {
		// SECURITY: the request body MUST NOT carry a user_id /
		// userId field. The tenancy key is taken EXCLUSIVELY from
		// the verified UserContext attached upstream by the Account
		// middleware. Any `userId` field present in the request
		// body is silently ignored (kept in the struct only so old
		// SPA builds don't 400 during the rollout). This blocks a
		// confused-deputy / cross-tenant write where user A POSTs
		// `{"userId":"victim@..."}` and the agent then writes into
		// victim's owner_id-scoped rows.
		var req struct {
			AgentType    string `json:"agentType" binding:"required"`
			SimulationID string `json:"simulation_id"`
			// IgnoredUserID accepts (and discards) the legacy
			// `userId` body field. Do NOT read this — use
			// actingUserID below.
			IgnoredUserID string `json:"userId"`
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
		if err := c.ShouldBindJSON(&req); err != nil {
			log.Printf("ShouldBindJSON: %v", err)
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
			return
		}

		// Resolve the acting user from the verified middleware
		// context. Anonymous spawn is refused: every simulation
		// run must be attributable to a real user for billing,
		// quota, audit, and owner_id-scoped memory writes.
		actingUserID := ""
		if uc, ok := account.FromContextOK(c.Request.Context()); ok && uc.UserID != "" {
			actingUserID = uc.UserID
		}
		if actingUserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		if req.IgnoredUserID != "" && req.IgnoredUserID != actingUserID {
			// Telemetry-only: surface attempts to spawn on behalf
			// of someone else so we can spot mis-wired clients or
			// active probing. The request still proceeds bound to
			// actingUserID — never to the requested userId.
			log.Printf("SECURITY: rejected cross-user spawn attempt: actor=%q requested=%q route=%s",
				actingUserID, req.IgnoredUserID, c.Request.URL.Path)
		}

		// Phase 5a: pre-flight quota gate. No-op when SAAS_MODE=off
		// (quotaChecker is nil) or when no tenant is on the context.
		// Phase 5b: also enforces the per-plan concurrency cap by
		// counting currently-active simulations for this tenant via
		// the (tenant-scoped) session registry.
		if enforceQuotaGate(c, countActiveSimulationsFor(reg)) {
			return
		}

		// Generate a unique UUID for the session
		sessionID := uuid.NewString()

		// Track the session in the registry with its agent type and optional simulation
		if err := reg.TrackSession(c.Request.Context(), sessionID, req.AgentType, req.SimulationID); err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		// Publish a JSON orchestration event for the Python agents.
		// Both `userId` and `acting_user_id` carry the SAME verified
		// value (actingUserID). Older Python consumers that look at
		// `payload.userId` therefore see the correct, server-vouched
		// tenant key instead of an attacker-controlled string.
		//
		// `user_auth_token` (Deferral A) forwards the SPA's raw
		// bearer so the spawned agent can call our own memory API
		// as *this* user, not as a shared service identity. Empty
		// in local-dev loopback (no cookie/header) — the agent then
		// falls back to MEMORY_GATEWAY_TOKEN or to the LocalResolver
		// loopback path.
		event := map[string]interface{}{
			"type":      "spawn_agent",
			"sessionId": sessionID,
			"eventId":   uuid.NewString(),
			"payload": map[string]string{
				"agentType":       req.AgentType,
				"userId":          actingUserID,
				"simulation_id":   req.SimulationID,
				"acting_user_id":  actingUserID,
				"user_auth_token": rawUserBearer(c),
			},
		}

		queue := hub.SpawnQueueNameForContext(c.Request.Context(), req.AgentType, sessionID, hub.DefaultSpawnShards)
		if sb == nil {
			cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 3*time.Second)
			_ = reg.UntrackSession(cleanupCtx, sessionID)
			cleanupCancel()
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "switchboard not initialized (Redis unavailable)"})
			return
		}
		if err := sb.EnqueueOrchestration(c.Request.Context(), queue, event); err != nil {
			// Try to untrack if orchestration fails to avoid ghost sessions
			cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 3*time.Second)
			_ = reg.UntrackSession(cleanupCtx, sessionID)
			cleanupCancel()
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		// Audit-log a successful spawn. Fires AFTER the
		// orchestration event is enqueued so we never log a sim
		// that didn't actually start. Best-effort: a DB hiccup
		// here doesn't roll back the spawn and doesn't change the
		// 201 response.
		recordAuditBestEffort(c, account.AuditEntry{
			Action: account.ActionSimSpawned,
			Target: sessionID,
			Metadata: map[string]any{
				"agent_type":    req.AgentType,
				"user_id":       actingUserID,
				"simulation_id": req.SimulationID,
				"route":         c.Request.URL.Path,
			},
		})

		c.JSON(http.StatusCreated, gin.H{
			"status":    "pending",
			"sessionId": sessionID,
			"message":   "spawn event published to orchestration channel",
		})
	})

	r.POST("/api/v1/sessions/flush", destructiveLimiter, func(c *gin.Context) {
		count, err := reg.Flush(c.Request.Context())
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "flushed", "count": count})
	})

	// Environment Reset API — selectively flush sessions, queues, and maps
	r.POST("/api/v1/environment/reset", destructiveLimiter, func(c *gin.Context) {
		var req struct {
			Targets []string `json:"targets"`
		}
		// Parse optional JSON body; empty body or missing targets = flush all
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
		if err := c.ShouldBindJSON(&req); err != nil && c.Request.ContentLength > 0 {
			slog.Error("invalid JSON body in environment reset", "error", err)
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid JSON body"})
			return
		}

		flushAll := len(req.Targets) == 0
		targetSet := make(map[string]bool)
		knownTargets := map[string]bool{"sessions": true, "queues": true, "maps": true, "pubsub": true}
		for _, t := range req.Targets {
			if !knownTargets[t] {
				c.JSON(http.StatusBadRequest, gin.H{
					"error": fmt.Sprintf("unknown reset target %q", t),
				})
				return
			}
			targetSet[t] = true
		}

		type flushResult struct {
			Flushed bool `json:"flushed"`
			Count   int  `json:"count"`
		}
		results := map[string]*flushResult{
			"sessions": {Flushed: false, Count: 0},
			"queues":   {Flushed: false, Count: 0},
			"maps":     {Flushed: false, Count: 0},
			"pubsub":   {Flushed: false, Count: 0},
		}

		ctx := c.Request.Context()

		// Snapshot active agent types BEFORE flushing so we can notify them
		// after the flush completes. Without this, PublishOrchestration queries
		// the registry post-flush and gets an empty list, so callable agents
		// (Agent Engine on GCP) never learn about the reset.
		var preFlushAgents []string
		if reg != nil {
			var err error
			preFlushAgents, err = reg.ActiveAgentTypes(ctx)
			if err != nil {
				log.Printf("RESET: snapshot ActiveAgentTypes failed (agents may not be notified): %v", err)
			}
		}

		// Flush sessions
		if flushAll || targetSet["sessions"] {
			count, err := reg.Flush(ctx)
			if err != nil {
				log.Printf("handler error: %v", err)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
				return
			}
			results["sessions"].Flushed = true
			results["sessions"].Count = count
		}

		// Flush queues
		if flushAll || targetSet["queues"] {
			if sb != nil {
				count, err := sb.FlushQueues(ctx)
				if err != nil {
					log.Printf("handler error: %v", err)
					c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
					return
				}
				results["queues"].Flushed = true
				results["queues"].Count = count
			} else {
				results["queues"].Flushed = true
				results["queues"].Count = 0
			}
		}

		// Flush session maps (SCAN and DEL session_map:<account>:* keys)
		if flushAll || targetSet["maps"] {
			mapCount := 0
			if rdb != nil {
				acc := account.FromContext(ctx)
				pattern := fmt.Sprintf("session_map:%s:*", acc.UserID)
				var cursor uint64
				for {
					keys, nextCursor, err := rdb.Scan(ctx, cursor, pattern, 100).Result()
					if err != nil {
						log.Printf("handler error: %v", err)
						c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
						return
					}
					if len(keys) > 0 {
						if err := rdb.Del(ctx, keys...).Err(); err != nil {
							log.Printf("handler error: %v", err)
							c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
							return
						}
						mapCount += len(keys)
					}
					cursor = nextCursor
					if cursor == 0 {
						break
					}
				}
			}
			results["maps"].Flushed = true
			results["maps"].Count = mapCount
		}

		// Flush PubSub subscriptions (seek to now)
		if flushAll || targetSet["pubsub"] {
			if drainer != nil {
				count, err := drainer.Drain(ctx)
				if err != nil {
					log.Printf("RESET: pubsub drain error (non-fatal): %v", err)
				}
				results["pubsub"].Flushed = err == nil || count > 0
				results["pubsub"].Count = count
			} else {
				results["pubsub"].Flushed = true
				results["pubsub"].Count = 0
			}
		}

		// Broadcast environment_reset event
		if sb != nil {
			resetWrapper := &gateway.Wrapper{
				Timestamp: time.Now().UTC().Format(time.RFC3339),
				Type:      "environment_reset",
				Event:     "environment_reset",
				Origin: &gateway.Origin{
					Type: "system",
					Id:   gatewayID,
				},
			}
			if err := sb.Broadcast(ctx, resetWrapper); err != nil {
				log.Printf("RESET: broadcast error: %v", err)
			}

			// Publish to simulation:broadcast via Redis Pub/Sub for subscriber-mode
			// agents listening on the channel. Callable agents get explicit pokes below.
			// account.Key namespaces the channel per-user so users don't see each
			// other's resets.
			simNotification := map[string]interface{}{
				"type":    "environment_reset",
				"eventId": fmt.Sprintf("reset-%d", time.Now().UnixNano()),
			}
			resetChannel := account.Key(ctx, "simulation", "broadcast")
			if err := sb.PublishOrchestration(ctx, resetChannel, simNotification); err != nil {
				log.Printf("RESET: simulation broadcast error: %v", err)
			}

			// Explicitly poke each agent that was active before the flush.
			// PublishOrchestration's session-aware routing returns empty post-flush,
			// so this ensures callable agents (Agent Engine) receive the reset.
			for _, agentType := range preFlushAgents {
				if err := sb.PokeAgent(ctx, agentType, simNotification); err != nil {
					log.Printf("RESET: poke %s failed: %v", agentType, err)
				}
			}
		}

		c.JSON(http.StatusOK, gin.H{
			"status":  "reset",
			"results": results,
		})
	})

	// Simulations Listing API
	r.GET("/api/v1/simulations", func(c *gin.Context) {
		if reg == nil {
			c.JSON(http.StatusOK, gin.H{"simulations": []string{}})
			return
		}
		sims, err := reg.ListSimulations(c.Request.Context())
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		if sims == nil {
			sims = []string{}
		}
		c.JSON(http.StatusOK, gin.H{"simulations": sims})
	})
	// Batch Spawn API — spawn N sessions across multiple agent types
	r.POST("/api/v1/spawn", vertexLimiter, func(c *gin.Context) {
		var req struct {
			Agents []struct {
				AgentType string `json:"agentType" binding:"required"`
				Count     int    `json:"count" binding:"required"`
			} `json:"agents" binding:"required,dive"`
			SimulationID string `json:"simulation_id"`
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 1<<20)
		if err := c.ShouldBindJSON(&req); err != nil {
			log.Printf("ShouldBindJSON: %v", err)
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
			return
		}

		// SECURITY: refuse anonymous batch spawns. Sibling endpoint
		// /api/v1/sessions has the same gate. Every spawn must be
		// attributable to a verified user so:
		//   1. owner_id-scoped memory writes (leadership_scenarios)
		//      get the right tenant,
		//   2. billing/metering can charge the right account,
		//   3. audit logs are useful.
		// The request body intentionally has NO userId field; the
		// tenancy key comes EXCLUSIVELY from the middleware-resolved
		// UserContext (see /api/v1/sessions handler for the same
		// pattern).
		if uc, ok := account.FromContextOK(c.Request.Context()); !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}

		// Phase 5a: pre-flight quota gate. No-op when SAAS_MODE=off
		// or when the request is unauthenticated. We check BEFORE
		// catalog discovery so a quota-exhausted tenant doesn't pay
		// the discovery latency just to be rejected.
		// Phase 5b: also enforces the concurrency cap via the
		// tenant-scoped session registry's ListSimulations.
		if enforceQuotaGate(c, countActiveSimulationsFor(reg)) {
			return
		}

		// Refresh catalog and validagent types synchronously for spawn request
		if catalog == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "agent catalog service not initialized"})
			return
		}

		agents, err := catalog.DiscoverAgents()
		if err != nil {
			slog.Error("SPAWN: discovery failed", "error", err)
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error":   "failed to discover agents required for spawning",
				"details": "internal discovery error",
			})
			return
		}

		for _, a := range req.Agents {
			if a.Count < 1 {
				c.JSON(http.StatusBadRequest, gin.H{"error": fmt.Sprintf("count must be >= 1 for agent %q", a.AgentType)})
				return
			}
			if _, found := agents[a.AgentType]; !found {
				c.JSON(http.StatusBadRequest, gin.H{
					"error":      fmt.Sprintf("unknown agent type %q", a.AgentType),
					"available":  getAgentKeys(agents),
					"suggestion": "ensure the agent is running and its URL is correct in AGENT_URLS",
				})
				return
			}
		}

		// Enforce MAX_RUNNERS limit: cap counts to fit within the limit.
		// This is the GLOBAL ceiling. Per-user `plan.MaxConcurrentRuns`
		// was already checked by enforceQuotaGate above; whichever cap
		// is lower for this request wins. See the precedence comment
		// at the MAX_RUNNERS definition (search for "audit P3 #8").
		totalRequested := 0
		for _, a := range req.Agents {
			totalRequested += a.Count
		}
		if totalRequested > maxRunners {
			log.Printf("Spawn request for %d runners exceeds MAX_RUNNERS=%d, capping", totalRequested, maxRunners)
			nTypes := len(req.Agents)
			if nTypes > maxRunners {
				// More agent types than max runners — only keep the first maxRunners types
				for i := range req.Agents {
					if i < maxRunners {
						req.Agents[i].Count = 1
					} else {
						req.Agents[i].Count = 0
					}
				}
			} else {
				// Save original counts BEFORE resetting to 1
				originalCounts := make([]int, len(req.Agents))
				for i, a := range req.Agents {
					originalCounts[i] = a.Count
				}

				// Give each type at least 1, then distribute remaining proportionally
				remaining := maxRunners - nTypes // slots after giving 1 to each
				for i := range req.Agents {
					req.Agents[i].Count = 1
				}
				// Distribute remaining slots proportionally to original counts
				if remaining > 0 {
					extraTotal := totalRequested - nTypes // original excess beyond 1-per-type
					if extraTotal <= 0 {
						extraTotal = 1
					}
					for i := range req.Agents {
						extra := (originalCounts[i] - 1) * remaining / extraTotal
						if extra < 0 {
							extra = 0
						}
						req.Agents[i].Count = 1 + extra
					}
				}
				// Final safety: ensure total doesn't exceed maxRunners due to rounding
				actualTotal := 0
				for _, a := range req.Agents {
					actualTotal += a.Count
				}
				for actualTotal > maxRunners {
					// Trim from the last agent type with count > 1
					for i := len(req.Agents) - 1; i >= 0; i-- {
						if req.Agents[i].Count > 1 {
							req.Agents[i].Count--
							actualTotal--
							break
						}
					}
					if actualTotal <= maxRunners {
						break
					}
				}
			}
		}

		type spawnedSession struct {
			SessionID    string `json:"sessionId"`
			AgentType    string `json:"agentType"`
			SimulationID string `json:"simulationId,omitempty"`
		}

		// Resolve the acting user once per request — every spawned
		// session in this batch belongs to the same user.
		batchUserID := ""
		if uc := account.FromContext(c.Request.Context()); uc.UserID != "" {
			batchUserID = uc.UserID
		}
		// Deferral A: capture the SPA's raw bearer once per batch
		// so each spawned agent can call the memory API as *this*
		// user. See the single-session handler above for the full
		// contract.
		batchUserAuthToken := rawUserBearer(c)

		// Phase 1: Collect all sessions and events up front.
		var sessions []spawnedSession
		var trackEntries []session.SessionTrackingEntry
		var queueItems []hub.QueueItem

		for _, a := range req.Agents {
			for i := 0; i < a.Count; i++ {
				sessionID := uuid.NewString()

				sessions = append(sessions, spawnedSession{
					SessionID:    sessionID,
					AgentType:    a.AgentType,
					SimulationID: req.SimulationID,
				})
				trackEntries = append(trackEntries, session.SessionTrackingEntry{
					SessionID:    sessionID,
					AgentType:    a.AgentType,
					SimulationID: req.SimulationID,
				})

				event := map[string]interface{}{
					"type":      "spawn_agent",
					"sessionId": sessionID,
					"eventId":   uuid.NewString(),
					"payload": map[string]string{
						"agentType":       a.AgentType,
						"userId":          batchUserID,
						"simulation_id":   req.SimulationID,
						"acting_user_id":  batchUserID,
						"user_auth_token": batchUserAuthToken,
					},
				}
				queueItems = append(queueItems, hub.QueueItem{
					Queue: hub.SpawnQueueNameForContext(c.Request.Context(), a.AgentType, sessionID, hub.DefaultSpawnShards),
					Data:  event,
				})
			}
		}

		// Phase 2: Batch-track all sessions in one Redis pipeline (was N round-trips).
		if err := reg.BatchTrackSessions(c.Request.Context(), trackEntries); err != nil {
			log.Printf("SPAWN: batch track failed: %v", err)
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		// Phase 3: Batch-enqueue all spawn events in one Redis pipeline (was N round-trips).
		// Uses context.Background() so enqueue completes even if the HTTP client disconnects.
		// PokeAgent (HTTP) runs per-agent-type in a background goroutine.
		if sb != nil {
			batchCtx, batchCancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer batchCancel()
			if err := sb.BatchEnqueueOrchestration(batchCtx, queueItems); err != nil {
				log.Printf("SPAWN: batch enqueue failed, untracking sessions: %v", err)
				// Untrack all sessions to avoid ghost entries that never get spawned.
				cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 3*time.Second)
				for _, entry := range trackEntries {
					_ = reg.UntrackSession(cleanupCtx, entry.SessionID)
				}
				cleanupCancel()
				log.Printf("handler error: %v", err)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
				return
			}
			// Poke agents to deliver spawn events.
			// Subscriber agents consume spawn events from their Redis queue,
			// so one poke per type is sufficient as a wake-up signal.
			// Callable agents have no Redis queue listener — they receive
			// events solely via HTTP. Each spawn event must be delivered
			// individually (one poke per session).
			poked := make(map[string]bool)
			for i, s := range sessions {
				card := agents[s.AgentType]
				isCallable := card.DispatchMode() == "callable"

				if !isCallable && poked[s.AgentType] {
					continue // subscriber: already poked this type
				}
				poked[s.AgentType] = true

				if !shuttingDown.Load() {
				dispatchWG.Add(1)
				go func(evt map[string]interface{}, agentType string) {
					defer dispatchWG.Done()
					pokeCtx, pokeCancel := context.WithTimeout(context.Background(), 10*time.Second)
					defer pokeCancel()
					if err := sb.PokeAgent(pokeCtx, agentType, evt); err != nil {
						log.Printf("SPAWN: poke error for %s: %v", agentType, err)
					}
				}(queueItems[i].Data.(map[string]interface{}), s.AgentType)
				}
			}
		}

		c.JSON(http.StatusOK, gin.H{"sessions": sessions})
	})

	// /api/v1/orchestration/push REMOVED 2026-06-21 (P1 audit finding).
	//
	// The previous handler mounted a Pub/Sub push endpoint behind the
	// Firebase Account middleware. That combination is broken: real
	// Pub/Sub push deliveries authenticate with a Google-signed OIDC
	// token (not a Firebase ID token), which the Account middleware
	// rejects. The endpoint was therefore unreachable for its stated
	// purpose AND would have lacked OIDC signature verification if a
	// future operator naïvely moved it into SkipPaths.
	//
	// To re-enable Pub/Sub push:
	//   1. Add `/api/v1/orchestration/push` to the Account middleware
	//      SkipPaths (above).
	//   2. Mount a Google-OIDC verifier middleware on this route ONLY
	//      (verify token signature against Google JWKS, validate `aud`
	//      matches the gateway's Cloud Run URL, validate `email`
	//      matches the configured push-subscription service account).
	//   3. Re-restore the handler body from git history.

	r.GET("/ws", func(c *gin.Context) {
		sessionID := c.Query("sessionId")
		if sessionID != "" && !validSessionID.MatchString(sessionID) {
			log.Printf("WebSocket: rejected invalid sessionId format: %q", sessionID)
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid sessionId format"})
			return
		}
		simulationID := c.Query("simulationId")
		if simulationID != "" && !validSessionID.MatchString(simulationID) {
			log.Printf("WebSocket: rejected invalid simulationId format: %q", simulationID)
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid simulationId format"})
			return
		}

		// P1 #9: refuse the WebSocket upgrade when there is no
		// authenticated user. Previously this code fell through
		// to context.Background(), letting unauthenticated peers
		// open a long-lived socket whose downstream handlers then
		// saw a zero UserContext{}. The Account middleware should
		// already 401 unauthenticated /ws requests (it's not in
		// SkipPaths), so this is defense in depth — but it makes
		// the WS contract explicit and survives any future middleware
		// reorganisation.
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			log.Printf("WebSocket: rejected unauthenticated upgrade (sessionId=%q)", sessionID)
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		wsCtxBase, wsCancel := context.WithCancel(context.Background())
		wsCtx := account.WithContext(wsCtxBase, uc)

		// B-20 (2026-06-23 audit): enforce per-user ownership on the
		// session/simulation IDs being subscribed to. Without this
		// check, isolation relied entirely on UUID unguessability —
		// any authenticated user who learned another tenant's
		// sessionId (logs, screenshots, support tickets) could
		// subscribe to their event stream.
		//
		// reg is a RedisSessionRegistry whose effectivePrefix folds in
		// `{ACCOUNT_KEY_PREFIX}:{user_id}:` when the context carries
		// a UserContext (see internal/session/redis_registry.go). So a
		// lookup performed with `wsCtx` (which carries `uc`) only
		// resolves keys minted under the same user — a foreign session
		// returns found=false naturally, no per-record owner column
		// needed.
		//
		// If only simulationId is supplied (a "subscribe to that sim"
		// upgrade without a specific session), we verify ownership by
		// listing simulations in the user's namespace. If neither is
		// supplied we permit the upgrade for system-broadcast-only
		// streams (no per-user data).
		if reg != nil {
			lookupCtx, lookupCancel := context.WithTimeout(wsCtx, 3*time.Second)
			defer lookupCancel()

			if sessionID != "" {
				_, found, err := reg.FindAgentType(lookupCtx, sessionID)
				if err != nil {
					log.Printf("WebSocket: registry lookup failed for sessionId=%s user=%s: %v", sessionID, uc.UserID, err)
					wsCancel()
					c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": "registry_unavailable"})
					return
				}
				if !found {
					log.Printf("WebSocket: rejected cross-tenant subscribe attempt user=%s sessionId=%s (not in user's namespace)", uc.UserID, sessionID)
					wsCancel()
					c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "session_not_owned"})
					return
				}
			} else if simulationID != "" {
				sims, err := reg.ListSimulations(lookupCtx)
				if err != nil {
					log.Printf("WebSocket: registry list-simulations failed user=%s: %v", uc.UserID, err)
					wsCancel()
					c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": "registry_unavailable"})
					return
				}
				owns := false
				for _, s := range sims {
					if s == simulationID {
						owns = true
						break
					}
				}
				if !owns {
					log.Printf("WebSocket: rejected cross-tenant subscribe attempt user=%s simulationId=%s (not in user's namespace)", uc.UserID, simulationID)
					wsCancel()
					c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "simulation_not_owned"})
					return
				}
			}
		}

		conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
		if err != nil {
			log.Printf("WS Upgrade error: %v", err)
			wsCancel()
			return
		}

		// Enforce a 64KB maximum message size to prevent OOM DOS attacks
		conn.SetReadLimit(65536)

		if err := conn.SetReadDeadline(time.Now().Add(90 * time.Second)); err != nil {
			log.Printf("WS SetReadDeadline error: %v", err)
			conn.Close()
			wsCancel()
			return
		}
		conn.SetPongHandler(func(string) error {
			if err := conn.SetReadDeadline(time.Now().Add(90 * time.Second)); err != nil {
				log.Printf("WS SetReadDeadline error in PongHandler: %v", err)
				return err
			}
			return nil
		})

		h.Register(sessionID, conn, simulationID)

		// Note: keepalive pings are handled by writePump (hub.go) which
		// sends PingMessage every 54 seconds. A separate ping goroutine
		// here would violate gorilla/websocket's single-writer contract
		// and cause data races.

		// Reader loop
		go func(conn *websocket.Conn, sid string, ctx context.Context, cancel context.CancelFunc) {
			defer func() {
				// Cancel the WebSocket context so in-flight dispatch
				// goroutines that derived timeouts from it are cleaned up.
				cancel()
				// Defensively close the connection to prevent TCP half-open leaks
				conn.Close()
				// Safe even if writePump already self-unregistered —
				// Run() treats duplicate unregisters as no-ops.
				h.Unregister(sid, conn)
			}()

			for {
				messageType, message, err := conn.ReadMessage()
				if err != nil {
					if websocket.IsCloseError(err, websocket.CloseMessageTooBig) {
						logging.FromContext(ctx).Warn("Client exceeded 64KB WebSocket payload limit")
					} else if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
						log.Printf("WebSocket unexpected close for session %s: %v", sid, err)
					} else if !websocket.IsCloseError(err, websocket.CloseGoingAway, websocket.CloseNormalClosure, websocket.CloseAbnormalClosure) {
						log.Printf("WebSocket read error for session %s: %v", sid, err)
					}
					break
				}
				if messageType == websocket.BinaryMessage {
					handleBinaryMessage(ctx, sid, message, sb, reg, h)
				}
				if messageType == websocket.TextMessage {
					// Route text messages through Hub for simulation subscription handling.
					// The Hub handles subscribe_simulation/unsubscribe_simulation messages.
					h.HandleTextMessage(conn, message)
				}
			}
		}(conn, sessionID, wsCtx, wsCancel)
	})

	return r
}

func getAgentKeys(m map[string]agent.AgentCard) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}

// buildBillingConnStr assembles a PostgreSQL connection string for
// the billing tables (usage_ledger + usage_rollup_monthly). Same
// resolution rules as admin's buildAlloyDBConnStr so a single env
// file works for both services:
//
//  1. DATABASE_URL (full DSN, wins if set)
//  2. ALLOYDB_* triplet (host/port/user/password/database)
//
// Returns the empty string when no credentials are configured —
// callers must treat that as "billing disabled" and return 503 on
// /api/v1/usage rather than crashing.
//
// Deliberately duplicates a few lines from admin/main.go rather
// than introducing an internal/dbconfig package — the conn-string
// assembly is genuinely shared, but pulling it into its own package
// for one extra caller would be overkill. If a third service starts
// reading Postgres, lift this into internal/config then.
func buildBillingConnStr() string {
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

func main() {
	config.Load()
	logging.Setup()

	// G5b: distributed tracing. No-op when OTEL_EXPORTER is unset
	// (default), so byte-identical to legacy. Set OTEL_EXPORTER=gcp
	// + GOOGLE_CLOUD_PROJECT to ship spans to Cloud Trace.
	tracingShutdown, err := tracing.SetupFromEnv(context.Background(), "race-gateway")
	if err != nil {
		log.Printf("⚠️ Tracing setup failed: %v (continuing without traces)", err)
	}
	defer func() { _ = tracingShutdown(context.Background()) }()

	port := config.Optional("PORT", config.Optional("GATEWAY_PORT", "8101"))
	if err := config.ValidatePort(port); err != nil {
		log.Fatalf("❌ Invalid PORT: %v", err)
	}

	// Distributed Setup
	redisAddr := config.Require("REDIS_ADDR")
	rdb := redis.NewClient(newRedisOptions(redisAddr))

	// 15s timeout — Cloud Run direct VPC egress to Memorystore (DIRECT_PEERING)
	// can take a few seconds for the first packet on cold start. 2s was too
	// aggressive and caused startup probe failures (build #4 step 25).
	ctxPing, cancelPing := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancelPing()
	if err := rdb.Ping(ctxPing).Err(); err != nil {
		log.Fatalf("❌ CRITICAL ERROR: Could not connect to Redis at %s: %v", redisAddr, err)
	}
	// M5 fix: Ensure the Redis client is closed on shutdown.
	defer rdb.Close()

	// One-time per-user account schema + Bootstrap. Postgres is
	// expected for all deployments; a missing DB only disables
	// /api/v1/usage + per-user endpoints (they 503), not the
	// orchestration / WebSocket fast path.
	//
	// 2026-06-21 audit P1 #1: runtime queries MUST go through
	// *pgxpool.Pool because *pgx.Conn is NOT safe for concurrent
	// goroutines (Cloud Run serves many requests in parallel and
	// query bytes would interleave on the wire). Migration, however,
	// uses a session-level pg_advisory_lock — that lock is tied to
	// a single connection, so it must run on a temporary *pgx.Conn,
	// not a pool. We therefore: (1) open a temp conn, (2) apply
	// the migration on it, (3) close the temp conn, (4) open the
	// runtime pool.
	var billingStore *billing.Store
	var dbPool *pgxpool.Pool
	{
		connStr := buildBillingConnStr()
		if connStr == "" {
			log.Printf("GATEWAY: no DATABASE_URL/ALLOYDB_* set; per-user endpoints will return 503")
		} else {
			bctx, bcancel := context.WithTimeout(context.Background(), 5*time.Second)
			migConn, err := pgx.Connect(bctx, connStr)
			bcancel()
			if err != nil {
				log.Printf("GATEWAY: DB connect failed; per-user endpoints will return 503: %v", err)
			} else {
				// Apply the per-user schema before any store
				// wraps the connection. ApplyMigration takes a
				// session-level advisory lock so concurrent
				// gateway+admin+billing cold-starts serialize
				// safely. Failure is fatal-for-this-DB: we
				// close the conn and leave per-user routes off.
				mctx, mcancel := context.WithTimeout(context.Background(), 30*time.Second)
				mErr := account.ApplyMigration(mctx, migConn)
				mcancel()
				closeCtx, closeCancel := context.WithTimeout(context.Background(), 2*time.Second)
				_ = migConn.Close(closeCtx)
				closeCancel()
				if mErr != nil {
					log.Printf("GATEWAY: account migration failed; per-user endpoints disabled: %v", mErr)
				} else {
					pctx, pcancel := context.WithTimeout(context.Background(), 5*time.Second)
					pool, perr := pgxpool.New(pctx, connStr)
					pcancel()
					if perr != nil {
						log.Printf("GATEWAY: DB pool open failed; per-user endpoints disabled: %v", perr)
					} else {
						dbPool = pool
						billingStore = billing.NewStore(pool)
						log.Printf("GATEWAY: billing store ready (pgxpool)")

						// One account.Store powers /me + audit
						// recorder + Stripe customer-id persister.
						// Shared pool keeps the connection count flat.
						acctStore := account.NewStore(pool)

						accountStoreForMe = acctStore
						auditor = acctStore
						log.Printf("GATEWAY: account /me store + audit recorder ready")

						// Organizational Memory & Learning store
						// shares the same pool. Construction is
						// cheap (no schema-application; the
						// memory tables come from
						// migrations/0002_analyses.sql which
						// ApplyMigration above already ran).
						memoryStoreForGateway = memory.NewStore(pool)
						log.Printf("GATEWAY: memory store ready")

						// /healthz pings the runtime pool, which
						// is safe for concurrent callers (the
						// previous *pgx.Conn.Ping path was a
						// concurrency bug under load).
						dbPingerForHealthz = pool.Ping
						log.Printf("GATEWAY: /healthz wired to ping Postgres (pool)")
					}
				}
			}
		}
	}
	if dbPool != nil {
		defer dbPool.Close()
	}

	// Build the account resolver in main() so the FirebaseResolver
	// can be wired with the user-lookup hook BEFORE setupRouter
	// mounts the middleware. Tests don't take this branch (they
	// call setupRouter directly, which falls back to
	// middleware.AccountResolverFromEnv()).
	if resolver, rerr := middleware.AccountResolverFromEnv(); rerr != nil {
		log.Fatalf("GATEWAY: account resolver: %v", rerr)
	} else {
		if fr, ok := resolver.(*account.FirebaseResolver); ok && accountStoreForMe != nil {
			if acctStore, ok := accountStoreForMe.(*account.Store); ok {
				// P1.A (2026-06-22): use GetUserAnyState so the
				// resolver can distinguish "deleted" from "never
				// existed" and emit ErrAccountDeleted → 410 instead
				// of falsely flagging the user NeedsInit.
				fr.WithLookup(acctStore.GetUserAnyState)
				log.Printf("GATEWAY: Firebase resolver wired with user lookup")
			}
		}
		accountResolverForRouter = resolver
	}

	// Initialize core services with Redis-backed subscription store.
	// RemoteWorkers=16 provides 4x throughput over the default (4) for
	// processing inbound Redis pub/sub messages during 1000-runner simulations.
	subStore := hub.NewRedisSubscriptionStore(rdb)
	h := hub.NewHub(hub.WithSubscriptionStore(subStore), hub.WithRemoteWorkers(16))

	gatewayID := config.Optional("HOSTNAME", "local-gw")
	redisPrefix := config.Optional("REDIS_PREFIX", "leader")

	reg := session.NewRedisSessionRegistry(rdb, redisPrefix, 2*time.Hour)

	agentURLsRaw := config.Optional("AGENT_URLS", "")
	var agentURLs []string
	if agentURLsRaw != "" {
		// 2026-06-21 audit P3 #9: SSRF guard. AGENT_URLS is
		// operator-controlled, but a tfvars typo pointing one
		// entry at the GCP metadata service would silently
		// forward SA tokens to the "agent" side. validateAgentURL
		// rejects link-local IPs and the metadata DNS aliases,
		// and in cloud mode requires https (except for loopback).
		// A bad entry is dropped with a log line; the rest of
		// the list continues to boot.
		inCloud := os.Getenv("K_SERVICE") != ""
		for _, u := range strings.Split(agentURLsRaw, ",") {
			u = strings.TrimSpace(u)
			if u == "" {
				continue
			}
			if err := validateAgentURL(u, inCloud, net.LookupIP); err != nil {
				log.Printf("GATEWAY: dropping AGENT_URLS entry %q (%v)", u, err)
				continue
			}
			agentURLs = append(agentURLs, u)
		}
		if len(agentURLs) == 0 {
			log.Printf("GATEWAY: AGENT_URLS was non-empty but every entry was rejected; catalog will be empty")
		}
	}
	catalog := agent.NewCatalog(agentURLs)
	// B-18 (2026-06-23 audit): pre-warm the catalog cache in the
	// background right after construction. Without this, the FIRST
	// request to /api/v1/spawn (or /api/v1/sessions) after a gateway
	// cold start has to fan out HTTP fetches to every AGENT_URL
	// before responding — and with 7 URLs including 5 Vertex AI
	// Reasoning Engines (each capable of a 10–15s cold start) and
	// 2 scale-to-zero Cloud Run runners, that easily blows past the
	// BFF's proxy timeout, surfacing as "context deadline exceeded
	// (Client.Timeout exceeded while awaiting headers)" to the user.
	// We run it in a goroutine (not synchronously) so the gateway
	// can accept traffic on /healthz, /me, etc. while discovery is
	// in flight; the Catalog's internal fetchMu serializes any
	// concurrent /spawn requests onto the in-flight warm-up.
	go func() {
		warmStart := time.Now()
		discovered, err := catalog.DiscoverAgents()
		if err != nil {
			log.Printf("GATEWAY: catalog warm-up failed after %s: %v (lazy discovery will retry on first spawn)",
				time.Since(warmStart), err)
			return
		}
		log.Printf("GATEWAY: catalog warm-up complete in %s — %d/%d agents reachable",
			time.Since(warmStart), len(discovered), len(agentURLs))
	}()
	sb := hub.NewSwitchboardWithRegistry(rdb, gatewayID, h, catalog, reg)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start background workers
	go h.Run(ctx)
	go func() {
		if err := sb.Start(ctx); err != nil && err != context.Canceled {
			log.Printf("Switchboard error: %v", err)
		}
	}()

	// Session-registry cleanup is now lazy: ListSessions() prunes
	// orphan SET entries on each call (typically driven by the
	// Agents-tab refresh). The former 30-minute background reaper
	// goroutine was removed so nothing runs on a schedule — primary
	// session STRING keys still auto-expire via the 2h Redis TTL
	// set by the registry itself.

	// PubSub Drainer: on GCP (no emulator), drain subscriptions during reset.
	var drainer hub.PubSubDrainer
	pubsubProject := config.Optional("PUBSUB_PROJECT_ID", "")
	pubsubResetSubs := config.Optional("PUBSUB_RESET_SUBS", "")
	if os.Getenv("PUBSUB_EMULATOR_HOST") == "" && pubsubProject != "" && pubsubResetSubs != "" {
		var subs []string
		for _, s := range strings.Split(pubsubResetSubs, ",") {
			s = strings.TrimSpace(s)
			if s != "" {
				subs = append(subs, s)
			}
		}
		drainer = hub.NewGCPPubSubDrainer(pubsubProject, subs)
		log.Printf("PubSub drainer configured for %d subscription(s) in project %s", len(subs), pubsubProject)
	} else {
		drainer = &hub.NoOpDrainer{}
		log.Printf("PubSub drainer disabled (emulator mode or no PUBSUB_RESET_SUBS configured)")
	}

	r := setupRouter(h, sb, catalog, reg, gatewayID, rdb, drainer)

	// Phase 4b: register the /api/v1/usage route AFTER setupRouter so
	// we don't have to thread billingStore through 20+ test call
	// sites that pin the setupRouter signature. The tenant
	// middleware mounted inside setupRouter via r.Use() still
	// applies to this route, so tenant.FromContext works correctly.
	if billingStore != nil {
		// Construct the on-demand SQLite drainer BEFORE registering
		// /api/v1/usage so the handler can flush any rows the
		// Python sink wrote since the last call. Replaces the
		// former 2-second background goroutine — drain happens
		// exactly when someone reads /api/v1/usage, satisfying the
		// project rule that nothing runs on a schedule.
		// METERING_SINK=sqlite is the local-dev path; cloud uses
		// Pub/Sub (handled below) and the drainer stays nil so
		// the handler skips the call. Path resolution mirrors the
		// Python side: METERING_SQLITE_PATH env wins, otherwise
		// the repo default `data/metering.db`.
		var usageDrainer billing.OnDemandDrainer
		if strings.EqualFold(os.Getenv("METERING_SINK"), "sqlite") {
			drainerPath := os.Getenv("METERING_SQLITE_PATH")
			if drainerPath == "" {
				drainerPath = "data/metering.db"
			}
			usageDrainer = billing.NewSQLiteDrainer(drainerPath, billingStore)
			log.Printf("GATEWAY: SQLite metering drainer wired on /api/v1/usage (path=%s)", drainerPath)
		}

		r.GET("/api/v1/usage", billing.UsageHandler(billingStore, usageDrainer))

		// Phase 6a: plan catalog. Lets the dashboard render the
		// upgrade options without hardcoding tier names. Tenant
		// middleware still applies (the route is registered after
		// setupRouter mounts r.Use(tenantMiddleware)), so an
		// unauthenticated caller is rejected before plansHandler
		// runs.
		r.GET("/api/v1/plans", billing.PlansHandler(billingStore))

		// Phase 6c / Phase D: outbound Checkout-session creation.
		// When STRIPE_API_KEY + STRIPE_PRICE_MAP + STRIPE_RETURN_BASE_URL
		// are all set, wire the real Stripe creators against the
		// account.Store (which is the per-user
		// CustomerIDPersister). Any missing piece falls back to
		// the stub so /api/v1/billing/{checkout,portal} return
		// 503 instead of 500-ing on a half-configured deploy.
		var checkoutCreator billing.CheckoutCreator = billing.NewStubCheckoutCreator()
		var portalCreator billing.PortalCreator = billing.NewStubPortalCreator()
		var userLookup billing.UserLookup = stubUserLookup{}
		// stripeAllowedBaseURL anchors the open-redirect guard on
		// /api/v1/billing/{checkout,portal}. We read the env once
		// at boot — when STRIPE_RETURN_BASE_URL is the empty
		// string the real Stripe creator never arms (so the stub
		// creator is in use, which doesn't actually redirect
		// users), and ValidateRedirectURL turns into a no-op.
		stripeAllowedBaseURL := strings.TrimSpace(os.Getenv("STRIPE_RETURN_BASE_URL"))
		if acctStore, ok := accountStoreForMe.(*account.Store); ok && acctStore != nil {
			userLookup = acctStore
			stripeKey := strings.TrimSpace(os.Getenv("STRIPE_API_KEY"))
			priceMapRaw := strings.TrimSpace(os.Getenv("STRIPE_PRICE_MAP"))
			if stripeKey != "" && priceMapRaw != "" {
				pm, perr := billing.LoadPriceMapFromEnv(priceMapRaw)
				if perr != nil {
					log.Printf("GATEWAY: STRIPE_PRICE_MAP parse failed; checkout/portal stay stubbed: %v", perr)
				} else {
					if cc, cerr := billing.NewStripeCheckoutCreator(stripeKey, pm, acctStore); cerr != nil {
						log.Printf("GATEWAY: Stripe checkout creator init failed: %v", cerr)
					} else {
						checkoutCreator = cc
						log.Printf("GATEWAY: Stripe checkout creator armed")
					}
					if pc, perr := billing.NewStripePortalCreator(stripeKey); perr != nil {
						log.Printf("GATEWAY: Stripe portal creator init failed: %v", perr)
					} else {
						portalCreator = pc
						log.Printf("GATEWAY: Stripe portal creator armed")
					}
				}
			}
		}

		r.POST(
			"/api/v1/billing/checkout",
			billing.CheckoutHandler(userLookup, checkoutCreator, stripeAllowedBaseURL),
		)

		r.POST(
			"/api/v1/billing/portal",
			billing.PortalHandler(userLookup, portalCreator, stripeAllowedBaseURL),
		)

		// Phase 4b SQLite metering drainer is now constructed
		// above, just before `/api/v1/usage` is registered, and
		// invoked on-demand by UsageHandler rather than from a
		// background goroutine. See the `usageDrainer` block
		// earlier in this function for the on-read drain path.

		// Phase 4c: start the Pub/Sub drainer goroutine when the
		// Python sink is configured to publish to a Pub/Sub topic.
		// METERING_SINK=pubsub is the cloud path; local dev uses
		// SQLite (handled above). Subscription id defaults to
		// `usage-metering-sub`; project id is the same
		// PUBSUB_PROJECT_ID the reset-drainer already reads.
		// Failures to construct the receiver leave the drainer
		// unstarted — the `/api/v1/usage` route still works against
		// any rows that previous boots / the SQLite drainer wrote.
		if strings.EqualFold(os.Getenv("METERING_SINK"), "pubsub") {
			project := config.Optional("PUBSUB_PROJECT_ID", "")
			subID := config.Optional("METERING_PUBSUB_SUBSCRIPTION", "usage-metering-sub")
			if project == "" {
				log.Printf("GATEWAY: METERING_SINK=pubsub but PUBSUB_PROJECT_ID empty; drainer disabled")
			} else {
				rcv, err := billing.NewPubSubReceiver(ctx, project, subID)
				if err != nil {
					log.Printf("GATEWAY: Pub/Sub metering receiver init failed; drainer disabled: %v", err)
				} else {
					pubsubDrainer := billing.NewPubSubDrainer(rcv, billingStore)
					go func() {
						if err := pubsubDrainer.Run(ctx); err != nil && err != context.Canceled {
							log.Printf("GATEWAY: Pub/Sub metering drainer exited: %v", err)
						}
						_ = rcv.Close()
					}()
					log.Printf("GATEWAY: Pub/Sub metering drainer started (project=%s sub=%s)", project, subID)
				}
			}
		}
	}

	// Phase 5a: arm the pre-flight quota gate. The spawn handlers
	// must be assigned BEFORE the server starts accepting traffic
	// — which is the case here (r.Run is the next major step
	// below). When SAAS_MODE=off, billingStore is nil and
	// quotaChecker stays nil, so the gate is a no-op.
	if billingStore != nil {
		quotaChecker = billingStore
	}
	if quotaChecker == nil {
		// P1-10 (2026-06-23 audit): make the silent no-op visible.
		// Without this WARN, an operator who forgot to set
		// SAAS_MODE=on (or whose billingStore failed to initialize)
		// gets unlimited per-user concurrency with NO log line at
		// startup. The WARN points them at the two most likely
		// causes so the fix is obvious before quota-bypass
		// becomes a billing incident.
		log.Printf("WARNING: quotaChecker is nil after billing init — " +
			"per-user concurrency cap will NOT be enforced. " +
			"Verify SAAS_MODE=on and the billing Postgres pool reached ready state.")
	} else {
		log.Printf("GATEWAY: pre-flight quota gate armed (POST /api/v1/spawn + /api/v1/sessions)")
	}

	// GET /me is registered unconditionally: meHandler already
	// implements a degraded-mode response when store is nil, so the
	// SPA can render the user icon even if the DB is temporarily
	// unavailable. Write endpoints remain gated because they
	// genuinely require a working store.
	r.GET("/api/v1/me", meHandler(accountStoreForMe))

	// BFF cookie exchange: /login POSTs the verified bearer
	// token here so the gateway can mint an HttpOnly+Secure
	// __session cookie. P1 #5 fix from the 2026-06-19 audit.
	// Mounted with a dedicated rate-limiter (P2.1, 2026-06-22)
	// so a stolen token cannot drive a cookie-refresh loop.
	// Moved OUTSIDE the accountStoreForMe guard (Fix #8) so that
	// login/auth token exchange succeeds even if the DB is down.
	// 10 burst / 1 req per 30s steady, keyed on Firebase UID;
	// nil rdb → noop (matches RateLimit's guard).
	authExchangeLimiter := middleware.RateLimit(middleware.RateLimitOptions{
		Client:          rdb,
		Capacity:        10,
		RefillPerSecond: 1.0 / 30.0,
		KeyPrefix:       "ratelimit:authexchange:",
	})
	r.POST("/api/v1/auth/exchange", authExchangeLimiter, authExchangeHandler())

	if accountStoreForMe != nil {
		// Rate-limiter for /me/init (MED-4 in the 2026-06-25 audit).
		// /api/v1/me/init is excluded from the global per-user
		// limiter because the global limiter requires a fully-
		// initialised user row, but /me/init is the call that
		// creates that row. Without its own limiter the route was
		// unprotected — a leaked or replayed Firebase token could
		// drive an unbounded loop of CreateUser attempts, each of
		// which is an INSERT plus a GetUser roundtrip. 3 burst /
		// 1 req per 60s keyed on Firebase UID is plenty for
		// legitimate signup retries (the SPA only calls it on a
		// fresh login when /me 404s) while shutting down abuse.
		meInitLimiter := middleware.RateLimit(middleware.RateLimitOptions{
			Client:          rdb,
			Capacity:        3,
			RefillPerSecond: 1.0 / 60.0,
			KeyPrefix:       "ratelimit:meinit:",
		})
		r.POST("/api/v1/me/init", meInitLimiter, initMeHandler(accountStoreForMe))
		r.POST("/api/v1/me/logout", logoutHandler(accountStoreForMe))
		r.DELETE("/api/v1/me", deleteAccountHandler(accountStoreForMe))
		// Profile + export. profileStore needs UpdateUserProfile
		// (not on meStore), so assert to *account.Store.
		if fullStore, ok := accountStoreForMe.(*account.Store); ok {
			r.GET("/api/v1/me/profile", profileHandler(fullStore))
			r.PATCH("/api/v1/me/profile", updateProfileHandler(fullStore))
			r.GET("/api/v1/me/export", exportMeHandler(fullStore))
		}
		log.Printf("GATEWAY: /api/v1/me + /me/init + /me/logout + /me/profile + /me/export registered")
	}

	// Organizational Memory & Learning routes. Mounted
	// AFTER setupRouter so the Account middleware is in place; the
	// routes are NOT in setupRouter's ApprovedPaths allow-list, so
	// the existing manual-approval gate refuses to serve them to
	// any operator who is not StatusApproved. nil store → no-op
	// (same degraded posture as /me).
	if memoryStoreForGateway != nil {

		var embedder Embedder
		if project := os.Getenv("GOOGLE_CLOUD_PROJECT"); project != "" {
			region := os.Getenv("GOOGLE_CLOUD_REGION")
			if region == "" {
				region = "us-central1"
			}
			ve, err := memory.NewVertexEmbedder(project, region)
			if err != nil {
				log.Printf("GATEWAY: Vertex embedder init failed (RAG disabled): %v", err)
			} else {
				embedder = ve
			}
		}
		mountMemoryRoutes(
			r,
			memoryStoreForGateway,
			memoryDocStorageFromEnv(),
			embedder,
			memoryMaxUploadBytes(),
		)
		log.Printf("GATEWAY: /api/v1/memory/* registered")
	}


	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           r,
		ReadTimeout:       15 * time.Second,
		ReadHeaderTimeout: 5 * time.Second,
		WriteTimeout:      0, // Must be 0: gateway serves long-lived WebSocket connections
		IdleTimeout:       60 * time.Second,
	}

	// Start server in a goroutine
	go func() {
		log.Printf("Gateway starting on port %s (ID: %s)", port, gatewayID)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Failed to run gateway: %v", err)
		}
	}()

	// Wait for interrupt signal for graceful shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("Gateway shutting down gracefully...")

	// Give outstanding requests 10 seconds to complete
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("Gateway forced shutdown: %v", err)
	}
	log.Println("HTTP server drained")

	// Cancel background workers only after HTTP traffic has stopped
	cancel()

	// Signal dispatch goroutines to stop spawning before waiting.
	shuttingDown.Store(true)

	// H8 fix: Wait for in-flight dispatch goroutines (with timeout).
	wgDone := make(chan struct{})
	go func() {
		dispatchWG.Wait()
		// Also wait for switchboard's internal dispatch goroutines.
		if rsb, ok := sb.(*hub.RedisSwitchboard); ok {
			rsb.Wait()
		}
		close(wgDone)
	}()
	select {
	case <-wgDone:
		log.Println("All dispatch goroutines completed")
	case <-time.After(30 * time.Second):
		log.Println("WARNING: timed out waiting for dispatch goroutines")
	}

	log.Println("Gateway exited cleanly")
}
