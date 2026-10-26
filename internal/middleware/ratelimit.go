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

package middleware

// Per-user rate limiting.
//
// Token-bucket implementation backed by Redis. The bucket is keyed
// on the resolved UserID — abuse is throttled at the user boundary,
// the same boundary the per-user plan caps think in (see
// concepts/multi_tenant_saas_plan.md for the plan-cap design;
// note the doc still calls them per-org because it predates the
// per-user pivot, but the limiter and the caps both key on UserID
// today).
//
// Why Redis (not in-process):
//   - Multi-replica gateway deployments need a shared counter
//     otherwise a 100-req/s limit becomes 100×N when there are N
//     pods. Redis is already a hard dependency on the gateway
//     (queues, registry), so adding one more key namespace is
//     zero-cost operationally.
//   - The token-bucket math is implemented as a single Lua script
//     so the read+compute+write is atomic.
//
// Why token bucket (not leaky bucket / fixed window):
//   - Token bucket allows bursts up to `capacity` — important for
//     interactive dashboards that fire a batch of requests on
//     load.
//
// Fail-open contract:
//   - Any Redis error logs and ALLOWS the request through. Redis
//     outage MUST NOT prevent customers from working.
//   - A missing account context is also fail-open (the auth
//     middleware handles unauthenticated requests upstream).

import (
	"context"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
)

// RateLimitOptions configures the per-user token bucket.
//
// All zero values are valid: an unconfigured RateLimitOptions
// disables rate limiting by returning a no-op middleware.
type RateLimitOptions struct {
	// Client is the Redis client used for the atomic Lua script.
	// When nil the middleware is a no-op (fail-open contract
	// applied even at construction time).
	Client *redis.Client

	// Capacity is the maximum burst size — the bucket starts
	// full and refills at RefillPerSecond up to this cap. Values
	// <= 0 disable the middleware (we never silently fall back
	// to a default cap because a caller's intent is ambiguous).
	Capacity int64

	// RefillPerSecond is the steady-state allowed request rate.
	// Floating point so callers can express "30 requests per
	// minute" as 0.5. Values <= 0 disable the middleware.
	RefillPerSecond float64

	// KeyPrefix lets multiple services share one Redis without
	// clobbering each other's buckets. Empty defaults to
	// "ratelimit:user:".
	KeyPrefix string

	// SkipPaths are URL path prefixes exempted from rate
	// limiting (e.g. /health, /version, /api/v1/usage).
	SkipPaths []string
}

// rateLimitScript is the atomic token-bucket implementation.
// Arguments:
//
//	KEYS[1] — bucket key (e.g. "ratelimit:org:acme")
//	ARGV[1] — capacity        (integer, max tokens)
//	ARGV[2] — refill_per_sec  (float, tokens added per second)
//	ARGV[3] — now_microseconds (integer, current time)
//	ARGV[4] — cost            (integer, tokens to subtract for this request)
//
// Returns a two-element array:
//
//	[1] = 1 (allowed) or 0 (rejected)
//	[2] = retry_after_microseconds when rejected, else 0
//
// The bucket state is stored as a Redis hash with two fields:
// "t" (token count, float) and "u" (last update time, integer
// microseconds). TTL is set to (capacity / refill_per_sec) + 60s
// so abandoned tenants don't leak memory.
//
// The script runs server-side, so the read/compute/write is
// atomic w.r.t. concurrent clients — no race between two requests
// from the same org both seeing "9 tokens" and both decrementing
// to 8.
const rateLimitScript = `
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 't', 'u')
local tokens = tonumber(data[1])
local last = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  last = now
end
local elapsed_us = now - last
if elapsed_us < 0 then elapsed_us = 0 end
local refill_tokens = (elapsed_us / 1000000.0) * refill
tokens = math.min(capacity, tokens + refill_tokens)
local allowed = 0
local retry_us = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  local deficit = cost - tokens
  -- Refill rate is tokens/sec; convert to microseconds.
  retry_us = math.ceil((deficit / refill) * 1000000)
end
redis.call('HSET', key, 't', tokens, 'u', now)
local ttl = math.ceil((capacity / refill) + 60)
redis.call('EXPIRE', key, ttl)
return {allowed, retry_us}
`

// RateLimitIfEnabled returns the rate-limiting middleware when
// options are fully configured. Otherwise it returns a no-op
// handler. Kept under the legacy name so existing service main.go
// boot code does not need to change; the SAAS_MODE branch is gone.
func RateLimitIfEnabled(opts RateLimitOptions) gin.HandlerFunc {
	return RateLimit(opts)
}

// RateLimit returns a token-bucket middleware. Configuration that
// would create a degenerate or unsafe bucket (nil client,
// non-positive capacity, non-positive refill) collapses to a no-op
// — the fail-open contract applies at construction too. A misuse
// is logged once so an operator can correlate "rate limit not
// working" with the boot-time message.
func RateLimit(opts RateLimitOptions) gin.HandlerFunc {
	if opts.Client == nil || opts.Capacity <= 0 || opts.RefillPerSecond <= 0 {
		log.Printf("RATELIMIT: disabled (client=%v capacity=%d refill=%g)",
			opts.Client != nil, opts.Capacity, opts.RefillPerSecond)
		return noop
	}
	prefix := opts.KeyPrefix
	if prefix == "" {
		prefix = "ratelimit:user:"
	}
	script := redis.NewScript(rateLimitScript)
	skips := append([]string{}, opts.SkipPaths...)

	return func(c *gin.Context) {
		if c.Request.Method == http.MethodOptions {
			c.Next()
			return
		}
		// Skip-path match is a strict path-segment compare,
		// NOT a raw prefix compare. "/api/v1/usage" must skip
		// exactly that path and its sub-tree ("/api/v1/usage/foo")
		// but MUST NOT silently bypass throttling on a future
		// sibling like "/api/v1/usagereport".
		path := c.Request.URL.Path
		for _, p := range skips {
			if path == p || strings.HasPrefix(path, p+"/") {
				c.Next()
				return
			}
		}
		uc := account.FromContext(c.Request.Context())
		if uc.UserID == "" {
			// No user on the request — either an unauthenticated
			// path (upstream gate handles it) or a test that
			// bypassed middleware. Either way, do not throttle
			// by user key.
			c.Next()
			return
		}
		key := prefix + uc.UserID
		now := time.Now().UnixMicro()
		res, err := script.Run(c.Request.Context(), opts.Client,
			[]string{key},
			opts.Capacity,
			opts.RefillPerSecond,
			now,
			int64(1),
		).Result()
		if err != nil {
			log.Printf("RATELIMIT: script failed for user=%s (allowing through): %v",
				uc.UserID, err)
			c.Next()
			return
		}
		arr, ok := res.([]interface{})
		if !ok || len(arr) < 2 {
			log.Printf("RATELIMIT: unexpected script result for user=%s (allowing): %T %v",
				uc.UserID, res, res)
			c.Next()
			return
		}
		allowed, _ := arr[0].(int64)
		retryUs, _ := arr[1].(int64)
		if allowed == 1 {
			c.Next()
			return
		}
		retrySec := (retryUs + 999_999) / 1_000_000
		if retrySec < 1 {
			retrySec = 1
		}
		c.Header("Retry-After", strconv.FormatInt(retrySec, 10))
		c.AbortWithStatusJSON(http.StatusTooManyRequests, gin.H{
			"error":               "rate_limited",
			"user_id":             uc.UserID,
			"retry_after_seconds": retrySec,
		})
	}
}

// noop is the fail-open middleware. Exported via the
// RateLimitIfEnabled factory only.
func noop(c *gin.Context) { c.Next() }

// RateLimitFromEnv builds RateLimitOptions from the standard env
// vars used across services. Callers still own the Redis client
// (we don't construct it here because services with multiple
// independent clients need to decide which one this middleware
// should share). Env vars (all optional — any missing/invalid one
// collapses to a no-op middleware):
//
//	RATELIMIT_CAPACITY        — burst size (int)
//	RATELIMIT_REFILL_PER_SEC  — steady-state rate (float)
//	RATELIMIT_KEY_PREFIX      — override the default key namespace
//
// Skipped paths are NOT env-driven on purpose — they're a
// security-sensitive list (a misconfigured "/admin" skip would be
// a real bug) and belong in code. Use the WithSkips builder on
// the returned options to set them at the call site.
func RateLimitFromEnv(client *redis.Client, getenv func(string) string) RateLimitOptions {
	if getenv == nil {
		getenv = func(string) string { return "" }
	}
	opts := RateLimitOptions{Client: client}
	if v := getenv("RATELIMIT_CAPACITY"); v != "" {
		if n, err := strconv.ParseInt(v, 10, 64); err == nil {
			opts.Capacity = n
		}
	}
	if v := getenv("RATELIMIT_REFILL_PER_SEC"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			opts.RefillPerSecond = f
		}
	}
	if v := getenv("RATELIMIT_KEY_PREFIX"); v != "" {
		opts.KeyPrefix = v
	}
	return opts
}

// WithSkips returns a copy of the options with the given paths
// appended to SkipPaths. Designed for the fluent call-site
// pattern used in cmd/gateway/main.go so the wiring fits on one
// chain without an intermediate variable.
func (o RateLimitOptions) WithSkips(paths ...string) RateLimitOptions {
	o.SkipPaths = append(append([]string{}, o.SkipPaths...), paths...)
	return o
}

// runRateLimitForTest exposes the inner Lua script to tests so
// they can validate the math without standing up a full gin
// engine. Not for use outside the test package — exported via the
// internal/ path constraint only.
//
// Returns (allowed, retryAfterMicros, error).
func runRateLimitForTest(ctx context.Context, client *redis.Client, key string, capacity int64, refill float64, now int64, cost int64) (bool, int64, error) {
	script := redis.NewScript(rateLimitScript)
	res, err := script.Run(ctx, client, []string{key}, capacity, refill, now, cost).Result()
	if err != nil {
		return false, 0, err
	}
	arr := res.([]interface{})
	allowed, _ := arr[0].(int64)
	retryUs, _ := arr[1].(int64)
	return allowed == 1, retryUs, nil
}
