// Copyright 2026 The Confidently Wrong Authors

package middleware

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// newMiniredisClient spins up an in-process Redis and returns the
// client + a teardown func. miniredis honors EVAL/EVALSHA so the
// production Lua script runs unmodified here.
func newMiniredisClient(t *testing.T) (*redis.Client, func()) {
	t.Helper()
	mr, err := miniredis.Run()
	if err != nil {
		t.Fatalf("miniredis: %v", err)
	}
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	return client, func() {
		_ = client.Close()
		mr.Close()
	}
}

// withTenant wires a fixed UserContext onto every request, so the
// rate-limit middleware sees a stable UserID for bucket keying. The
// helper name is historical (predates the orgs→users refactor); we
// keep it for grep-stability but it now attaches an account.UserContext.
func withTenant(userID string) gin.HandlerFunc {
	return func(c *gin.Context) {
		uc := account.UserContext{UserID: userID}
		c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), uc))
		c.Next()
	}
}

// --- Lua script math ---------------------------------------------

func TestRateLimitScript_RefillAndConsume(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()

	ctx := context.Background()
	key := "rl:t1"
	// Capacity 5, refill 1 token/sec. Initial bucket starts full.
	now := int64(1_000_000_000_000_000) // arbitrary epoch microseconds
	for i := 0; i < 5; i++ {
		ok, retry, err := runRateLimitForTest(ctx, client, key, 5, 1.0, now, 1)
		if err != nil {
			t.Fatalf("call %d: %v", i, err)
		}
		if !ok {
			t.Fatalf("call %d: expected allow, got reject with retry_us=%d", i, retry)
		}
	}
	// 6th call empties the bucket → reject.
	ok, retry, err := runRateLimitForTest(ctx, client, key, 5, 1.0, now, 1)
	if err != nil {
		t.Fatalf("6th call: %v", err)
	}
	if ok {
		t.Fatal("6th call: expected reject")
	}
	if retry <= 0 {
		t.Fatalf("6th call: expected positive retry_us, got %d", retry)
	}
	// Advance time by 1s — 1 token refilled.
	ok, _, err = runRateLimitForTest(ctx, client, key, 5, 1.0, now+1_000_000, 1)
	if err != nil {
		t.Fatalf("refill call: %v", err)
	}
	if !ok {
		t.Fatal("refill call: expected allow after 1s")
	}
}

func TestRateLimitScript_CapacityRespected(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()
	ctx := context.Background()
	key := "rl:t2"
	now := int64(2_000_000_000_000_000)
	// Drain 2 tokens out of a 5-cap bucket.
	for i := 0; i < 2; i++ {
		if ok, _, _ := runRateLimitForTest(ctx, client, key, 5, 1.0, now, 1); !ok {
			t.Fatalf("drain %d: expected allow", i)
		}
	}
	// Advance time by 1 hour — refill MUST cap at 5, not balloon.
	future := now + 3600*1_000_000
	// Spend 5 tokens — should all succeed because bucket capped at 5.
	for i := 0; i < 5; i++ {
		if ok, _, _ := runRateLimitForTest(ctx, client, key, 5, 1.0, future, 1); !ok {
			t.Fatalf("after-cap call %d: expected allow", i)
		}
	}
	// 6th call must reject — proves cap was honored.
	if ok, _, _ := runRateLimitForTest(ctx, client, key, 5, 1.0, future, 1); ok {
		t.Fatal("after-cap: expected reject, cap was breached")
	}
}

// --- Middleware-level tests --------------------------------------

func TestRateLimit_AllowsThenRejects(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()

	r := gin.New()
	r.Use(withTenant("acme"))
	r.Use(RateLimit(RateLimitOptions{
		Client:          client,
		Capacity:        3,
		RefillPerSecond: 0.01, // tiny refill so we don't accidentally refill mid-test
	}))
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	// 3 OKs.
	for i := 0; i < 3; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("call %d: want 200, got %d", i, w.Code)
		}
	}
	// 4th: 429.
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("want 429, got %d body=%s", w.Code, w.Body.String())
	}
	if got := w.Header().Get("Retry-After"); got == "" {
		t.Fatal("missing Retry-After header")
	} else if n, err := strconv.Atoi(got); err != nil || n <= 0 {
		t.Fatalf("Retry-After=%q want positive integer", got)
	}
	if !strings.Contains(w.Body.String(), "rate_limited") {
		t.Fatalf("body missing rate_limited code: %s", w.Body.String())
	}
}

func TestRateLimit_SeparateOrgsHaveSeparateBuckets(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()

	// Use one engine per org because withTenant pins a single org;
	// the production middleware uses the per-request tenant ctx so
	// this proves the key derivation, not just the wiring.
	makeEngine := func(org string) *gin.Engine {
		r := gin.New()
		r.Use(withTenant(org))
		r.Use(RateLimit(RateLimitOptions{
			Client:          client,
			Capacity:        2,
			RefillPerSecond: 0.001,
		}))
		r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })
		return r
	}
	acme := makeEngine("acme")
	beta := makeEngine("beta")

	// Drain acme to empty.
	for i := 0; i < 2; i++ {
		w := httptest.NewRecorder()
		acme.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("acme drain %d: %d", i, w.Code)
		}
	}
	w := httptest.NewRecorder()
	acme.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("acme should be empty, got %d", w.Code)
	}

	// beta should still have a full bucket — separate key.
	for i := 0; i < 2; i++ {
		w := httptest.NewRecorder()
		beta.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("beta call %d: want 200, got %d", i, w.Code)
		}
	}
}

func TestRateLimit_NoTenantAllowsThrough(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()

	r := gin.New()
	// NB: no withTenant — context has no TenantContext.
	r.Use(RateLimit(RateLimitOptions{
		Client:          client,
		Capacity:        1,
		RefillPerSecond: 0.001,
	}))
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	for i := 0; i < 5; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("call %d: anonymous request should pass, got %d", i, w.Code)
		}
	}
}

func TestRateLimit_SkipPaths(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()

	r := gin.New()
	r.Use(withTenant("acme"))
	r.Use(RateLimit(RateLimitOptions{
		Client:          client,
		Capacity:        1,
		RefillPerSecond: 0.001,
		SkipPaths:       []string{"/health"},
	}))
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })
	r.GET("/health", func(c *gin.Context) { c.Status(http.StatusOK) })

	// Drain the bucket on /x.
	for i := 0; i < 1; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("drain: %d", w.Code)
		}
	}
	// /x now rejects.
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("/x should be 429, got %d", w.Code)
	}
	// /health bypasses.
	for i := 0; i < 10; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/health", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("/health call %d should bypass, got %d", i, w.Code)
		}
	}
}

// Phase 7c bug-fix coverage: SkipPaths is a path-segment match,
// NOT a raw prefix match. A skipped "/api/v1/usage" must NOT
// silently bypass throttling on "/api/v1/usagereport" \u2014 the
// latter is a different route and skipping it would be a
// security-relevant misconfiguration.
func TestRateLimit_SkipPathsIsSegmentMatchNotPrefix(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()

	r := gin.New()
	r.Use(withTenant("acme"))
	r.Use(RateLimit(RateLimitOptions{
		Client:          client,
		Capacity:        1,
		RefillPerSecond: 0.001,
		SkipPaths:       []string{"/api/v1/usage"},
	}))
	r.GET("/api/v1/usage", func(c *gin.Context) { c.Status(http.StatusOK) })
	r.GET("/api/v1/usage/foo", func(c *gin.Context) { c.Status(http.StatusOK) })
	r.GET("/api/v1/usagereport", func(c *gin.Context) { c.Status(http.StatusOK) })

	// Exact skip: unlimited.
	for i := 0; i < 5; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/usage", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("/api/v1/usage exact must bypass; got %d on call %d", w.Code, i)
		}
	}
	// Sub-tree of skipped prefix: also bypasses.
	for i := 0; i < 5; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/usage/foo", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("/api/v1/usage/foo (sub-tree) must bypass; got %d on call %d", w.Code, i)
		}
	}
	// Sibling that SHARES the prefix but is a different route:
	// MUST be throttled. Drain the single token, then expect 429.
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/usagereport", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("/api/v1/usagereport first call should consume the token (200), got %d", w.Code)
	}
	w = httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/usagereport", nil))
	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("/api/v1/usagereport second call should be 429 (NOT skipped), got %d body=%s",
			w.Code, w.Body.String())
	}
}

func TestRateLimit_PreflightBypass(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()
	r := gin.New()
	r.Use(withTenant("acme"))
	r.Use(RateLimit(RateLimitOptions{Client: client, Capacity: 1, RefillPerSecond: 0.001}))
	// No handler — preflight handling is the middleware's job.
	r.OPTIONS("/x", func(c *gin.Context) { c.Status(http.StatusNoContent) })
	for i := 0; i < 5; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodOptions, "/x", nil))
		if w.Code != http.StatusNoContent {
			t.Fatalf("preflight %d: %d", i, w.Code)
		}
	}
}

// --- Fail-open contract ------------------------------------------

func TestRateLimit_FailsOpenOnRedisError(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	teardown() // immediately close the underlying server → all ops fail
	defer client.Close()

	r := gin.New()
	r.Use(withTenant("acme"))
	r.Use(RateLimit(RateLimitOptions{
		Client:          client,
		Capacity:        1,
		RefillPerSecond: 0.001,
	}))
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	// All 5 requests must succeed even though Redis is dead.
	for i := 0; i < 5; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("fail-open call %d: want 200, got %d", i, w.Code)
		}
	}
}

// --- Construction-time fail-open --------------------------------

func TestRateLimit_NilClientNoOp(t *testing.T) {
	r := gin.New()
	r.Use(withTenant("acme"))
	r.Use(RateLimit(RateLimitOptions{Client: nil, Capacity: 1, RefillPerSecond: 1}))
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })
	for i := 0; i < 5; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("nil-client call %d: want 200, got %d", i, w.Code)
		}
	}
}

func TestRateLimit_NonPositiveCapNoOp(t *testing.T) {
	client, teardown := newMiniredisClient(t)
	defer teardown()
	r := gin.New()
	r.Use(withTenant("acme"))
	r.Use(RateLimit(RateLimitOptions{Client: client, Capacity: 0, RefillPerSecond: 1}))
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })
	for i := 0; i < 5; i++ {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/x", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("zero-cap call %d: want 200, got %d", i, w.Code)
		}
	}
}

// --- Env parsing --------------------------------------------------

func TestRateLimitFromEnv_Parses(t *testing.T) {
	env := map[string]string{
		"RATELIMIT_CAPACITY":       "42",
		"RATELIMIT_REFILL_PER_SEC": "1.5",
		"RATELIMIT_KEY_PREFIX":     "custom:",
	}
	getenv := func(k string) string { return env[k] }
	opts := RateLimitFromEnv(nil, getenv)
	if opts.Capacity != 42 {
		t.Errorf("Capacity=%d want 42", opts.Capacity)
	}
	if opts.RefillPerSecond != 1.5 {
		t.Errorf("RefillPerSecond=%g want 1.5", opts.RefillPerSecond)
	}
	if opts.KeyPrefix != "custom:" {
		t.Errorf("KeyPrefix=%q want custom:", opts.KeyPrefix)
	}
}

func TestRateLimitFromEnv_IgnoresGarbage(t *testing.T) {
	env := map[string]string{
		"RATELIMIT_CAPACITY":       "not-a-number",
		"RATELIMIT_REFILL_PER_SEC": "also-bad",
	}
	getenv := func(k string) string { return env[k] }
	opts := RateLimitFromEnv(nil, getenv)
	if opts.Capacity != 0 || opts.RefillPerSecond != 0 {
		t.Errorf("garbage env should yield zero-value opts, got %+v", opts)
	}
}
