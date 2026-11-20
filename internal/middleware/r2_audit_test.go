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

import (
	"context"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
	"github.com/alicebob/miniredis/v2"
	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
)

type fixedResolverR2 struct {
	uc account.UserContext
}

func (f fixedResolverR2) Resolve(_ *http.Request) (account.UserContext, error) {
	return f.uc, nil
}

func TestR2_HasPathPrefixSegmentBoundaries(t *testing.T) {
	if hasPathPrefix("/anything", []string{""}) {
		t.Fatal("empty prefix must never match")
	}
	if !hasPathPrefix("/", []string{"/"}) {
		t.Fatal("root prefix '/' must match '/'")
	}
	if hasPathPrefix("/api/v1/sessions", []string{"/"}) {
		t.Fatal("root prefix '/' must not match non-root paths")
	}
	if !hasPathPrefix("/api/v1/me", []string{"/api/v1/me"}) {
		t.Fatal("exact prefix match failed")
	}
	if !hasPathPrefix("/api/v1/me/logout", []string{"/api/v1/me/"}) {
		t.Fatal("segment child match failed")
	}
	if hasPathPrefix("/api/v1/memory", []string{"/api/v1/me"}) {
		t.Fatal("segment boundary bypass: /api/v1/me must NOT match /api/v1/memory")
	}
	if hasPathPrefix("/api/v1/me/initial", []string{"/api/v1/me/init"}) {
		t.Fatal("segment boundary bypass: /api/v1/me/init must NOT match /api/v1/me/initial")
	}
}

func TestR2_PaymentFailedBlocksMemoryWritesWhileAllowingMeAndBilling(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(Account(AccountOptions{
		Resolver: fixedResolverR2{uc: account.UserContext{
			UserID: "u-delinquent",
			Email:  "late@example.com",
			Status: account.StatusApproved,
		}},
		PaymentFailedCheck: func(context.Context, string) (time.Time, error) {
			return time.Now().Add(-30 * 24 * time.Hour), nil
		},
		PaymentFailedGracePeriod: 7 * 24 * time.Hour,
	}))
	r.POST("/api/v1/memory/analyses", func(c *gin.Context) { c.Status(http.StatusCreated) })
	r.POST("/api/v1/memories", func(c *gin.Context) { c.Status(http.StatusCreated) })
	r.POST("/api/v1/me/logout", func(c *gin.Context) { c.Status(http.StatusOK) })
	r.POST("/api/v1/billing/portal", func(c *gin.Context) { c.Status(http.StatusOK) })

	for _, blocked := range []string{"/api/v1/memory/analyses", "/api/v1/memories"} {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodPost, blocked, strings.NewReader(`{}`)))
		if w.Code != http.StatusPaymentRequired {
			t.Errorf("POST %s: expected 402 Payment Required, got %d (%s)", blocked, w.Code, w.Body.String())
		}
	}

	for _, exempt := range []string{"/api/v1/me/logout", "/api/v1/billing/portal"} {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodPost, exempt, strings.NewReader(`{}`)))
		if w.Code != http.StatusOK {
			t.Errorf("POST %s: expected exempt 200 OK, got %d (%s)", exempt, w.Code, w.Body.String())
		}
	}
}

func TestR2_AccountInitPathsRejectPrefixCollision(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(Account(AccountOptions{
		Resolver: fixedResolverR2{uc: account.UserContext{
			UserID:    "u-uninit",
			NeedsInit: true,
		}},
		InitPaths: []string{"/api/v1/me/init"},
	}))
	r.POST("/api/v1/me/init", func(c *gin.Context) { c.Status(http.StatusCreated) })
	r.POST("/api/v1/me/initial", func(c *gin.Context) { c.Status(http.StatusOK) })

	wAllowed := httptest.NewRecorder()
	r.ServeHTTP(wAllowed, httptest.NewRequest(http.MethodPost, "/api/v1/me/init", nil))
	if wAllowed.Code != http.StatusCreated {
		t.Fatalf("POST /api/v1/me/init: expected 201, got %d", wAllowed.Code)
	}

	wBlocked := httptest.NewRecorder()
	r.ServeHTTP(wBlocked, httptest.NewRequest(http.MethodPost, "/api/v1/me/initial", nil))
	if wBlocked.Code != http.StatusConflict {
		t.Fatalf("POST /api/v1/me/initial: expected 409 needs_init, got %d (%s)", wBlocked.Code, wBlocked.Body.String())
	}
}

func TestR2_RateLimitNaNAndInfFailOpen(t *testing.T) {
	gin.SetMode(gin.TestMode)
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	for _, refill := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		r := gin.New()
		r.Use(func(c *gin.Context) {
			ctx := account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-rl"})
			c.Request = c.Request.WithContext(ctx)
			c.Next()
		})
		r.Use(RateLimit(RateLimitOptions{
			Client:          rdb,
			Capacity:        1,
			RefillPerSecond: refill,
		}))
		r.GET("/test", func(c *gin.Context) { c.Status(http.StatusOK) })

		for i := 0; i < 3; i++ {
			w := httptest.NewRecorder()
			r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/test", nil))
			if w.Code != http.StatusOK {
				t.Fatalf("refill=%v iter=%d: expected fail-open 200, got %d", refill, i, w.Code)
			}
		}
	}
}

func TestR2_RequestTracingRedactsEmailToHash(t *testing.T) {
	rec := installTestTracer(t)
	r := newTracingRouter("svc-pii")
	r.GET("/whoami", func(c *gin.Context) {
		ctx := account.WithContext(c.Request.Context(), account.UserContext{
			UserID: "u-pii-1",
			Email:  "alice@example.com",
		})
		c.Request = c.Request.WithContext(ctx)
		c.Status(http.StatusOK)
	})

	r.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/whoami", nil))

	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	attrs := spans[0].Attributes()
	if _, leaked := findAttr(attrs, "user.email"); leaked {
		t.Fatalf("PII leak: raw user.email attribute found on trace span: %v", attrs)
	}
	gotHash, ok := findAttr(attrs, "user.email_hash")
	if !ok {
		t.Fatalf("expected user.email_hash attribute on span, got %v", attrs)
	}
	wantHash := logging.HashEmail("alice@example.com")
	if gotHash.AsString() != wantHash {
		t.Fatalf("user.email_hash = %q, want %q", gotHash.AsString(), wantHash)
	}
}
