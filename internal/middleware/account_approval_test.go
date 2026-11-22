// Copyright 2026 Christian Haeussinger. Apache-2.0.
//
// Tests for the manual-approval gate baked into middleware.Account.
// Exercise only the gate itself (ApprovedPaths + NotApprovedHandler)
// — the rest of Account is covered indirectly by gateway/account
// package tests.

package middleware

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

func init() { gin.SetMode(gin.TestMode) }

// stubResolver returns a fixed UserContext for every request.
type stubResolver struct{ uc account.UserContext }

func (s stubResolver) Resolve(r *http.Request) (account.UserContext, error) {
	return s.uc, nil
}

// buildApprovalRouter wires only the Account middleware with the
// approval allow-list, then mounts a no-op /api/v1/me and a no-op
// "protected" route. The handler bodies are 200 OK so any non-200
// response observed in the test came from the middleware gate.
func buildApprovalRouter(uc account.UserContext) *gin.Engine {
	r := gin.New()
	r.Use(Account(AccountOptions{
		Resolver: stubResolver{uc: uc},
		ApprovedPaths: []string{
			"/api/v1/me",
			"/api/v1/me/init",
			"/api/v1/me/logout",
			"/api/v1/auth/exchange",
		},
	}))
	r.GET("/api/v1/me", func(c *gin.Context) { c.Status(http.StatusOK) })
	r.GET("/api/v1/protected", func(c *gin.Context) { c.Status(http.StatusOK) })
	return r
}

func TestAccountApprovalGate_ApprovedUserPasses(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	r := buildApprovalRouter(account.UserContext{
		UserID: "uid-1",
		Email:  "a@example.com",
		Status: account.StatusApproved,
	})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/v1/protected", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("approved user on protected path: want 200, got %d body=%s", w.Code, w.Body.String())
	}
}

func TestAccountApprovalGate_PendingBlocksProtected(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	r := buildApprovalRouter(account.UserContext{
		UserID: "uid-1",
		Email:  "a@example.com",
		Status: account.StatusPending,
	})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/v1/protected", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusForbidden {
		t.Fatalf("pending user on protected path: want 403, got %d body=%s", w.Code, w.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body["error"] != "account_pending" {
		t.Fatalf("error code: want account_pending, got %v", body["error"])
	}
}

func TestAccountApprovalGate_PendingAllowsMe(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	r := buildApprovalRouter(account.UserContext{
		UserID: "uid-1",
		Email:  "a@example.com",
		Status: account.StatusPending,
	})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("pending user on /me: want 200, got %d body=%s", w.Code, w.Body.String())
	}
}

func TestAccountApprovalGate_RejectedBlocksWithDistinctCode(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	r := buildApprovalRouter(account.UserContext{
		UserID: "uid-1",
		Email:  "a@example.com",
		Status: account.StatusRejected,
	})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/v1/protected", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusForbidden {
		t.Fatalf("rejected user on protected path: want 403, got %d", w.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body: %v", err)
	}
	if body["error"] != "account_rejected" {
		t.Fatalf("error code: want account_rejected, got %v", body["error"])
	}
}

func TestAccountApprovalGate_EmptyAllowListDisablesGate(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	// Legacy installations without ApprovedPaths configured must
	// behave exactly as they did pre-approval-flow -- every signed-in
	// user passes regardless of status.
	r := gin.New()
	r.Use(Account(AccountOptions{
		Resolver: stubResolver{uc: account.UserContext{
			UserID: "uid-1",
			Email:  "a@example.com",
			Status: account.StatusPending,
		}},
	}))
	r.GET("/api/v1/protected", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/v1/protected", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("empty ApprovedPaths must bypass gate: got %d body=%s", w.Code, w.Body.String())
	}
}

// TestAccountApprovalGate_SaaSOffBypassesGate documents the 2026-07-02
// bypass: when SAAS_MODE is off (the default for “make start“ /
// single-tenant local dev), the manual-approval gate MUST NOT fire
// even when ApprovedPaths is configured and the user is StatusPending.
// Local dev has no admin queue and users would otherwise be trapped
// on the SPA's /pending-approval screen forever.
func TestAccountApprovalGate_SaaSOffBypassesGate(t *testing.T) {
	t.Setenv("SAAS_MODE", "off")
	r := buildApprovalRouter(account.UserContext{
		UserID: "uid-1",
		Email:  "a@example.com",
		Status: account.StatusPending,
	})
	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/v1/protected", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("SAAS_MODE=off must bypass approval gate: got %d body=%s", w.Code, w.Body.String())
	}
}

func TestAccount_NegativePaymentGraceClampsToZero(t *testing.T) {
	r := gin.New()
	r.Use(Account(AccountOptions{
		Resolver: stubResolver{uc: account.UserContext{
			UserID: "uid-1",
			Status: account.StatusApproved,
		}},
		PaymentFailedGracePeriod: -1 * time.Second,
		PaymentFailedCheck: func(context.Context, string) (time.Time, error) {
			return time.Now().Add(-10 * time.Millisecond), nil
		},
	}))
	r.POST("/api/v1/sessions", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/api/v1/sessions", nil))
	if w.Code != http.StatusPaymentRequired {
		t.Fatalf("negative grace must fire immediately (402), got %d body=%s", w.Code, w.Body.String())
	}
}

func TestAccount_EmptyStatusBlockedByApprovalGate(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	r := buildApprovalRouter(account.UserContext{
		UserID: "uid-1",
		Email:  "a@example.com",
		Status: "",
	})
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/protected", nil))
	if w.Code != http.StatusForbidden {
		t.Fatalf("empty status must be blocked (403) when ApprovalGate active, got %d", w.Code)
	}
}

func TestAccount_SkipAndApprovedPathsRejectTraversalAndWildcardSlash(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	r := gin.New()
	r.Use(Account(AccountOptions{
		Resolver:      stubResolver{uc: account.UserContext{UserID: "u1", Status: account.StatusPending}},
		SkipPaths:     []string{"", "/", "/health"},
		ApprovedPaths: []string{"", "/", "/api/v1/me"},
	}))
	r.GET("/api/v1/protected", func(c *gin.Context) { c.Status(http.StatusOK) })

	// "/health/../api/v1/protected" cleans to "/api/v1/protected" and must NOT skip or pass approval gate.
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/health/../api/v1/protected", nil))
	if w.Code != http.StatusForbidden {
		t.Fatalf("traversal path must hit approval gate (403), got %d", w.Code)
	}

	// Empty SkipPaths/ApprovedPaths and "/" must not skip unauthenticated requests to /api/v1/protected.
	rUnauth := gin.New()
	rUnauth.Use(Account(AccountOptions{
		Resolver: failingResolver{err: errors.New("no token")},
		SkipPaths: []string{"", "/", "/health"},
	}))
	rUnauth.GET("/api/v1/protected", func(c *gin.Context) { c.Status(http.StatusOK) })
	wUnauth := httptest.NewRecorder()
	rUnauth.ServeHTTP(wUnauth, httptest.NewRequest(http.MethodGet, "/api/v1/protected", nil))
	if wUnauth.Code != http.StatusUnauthorized {
		t.Fatalf("empty/root skip must not bypass auth on /api/v1/protected, got %d", wUnauth.Code)
	}
}

type failingResolver struct{ err error }

func (f failingResolver) Resolve(*http.Request) (account.UserContext, error) {
	return account.UserContext{}, f.err
}

func TestAccountResolverFromEnv_CaseInsensitiveAndCloudRunJob(t *testing.T) {
	t.Setenv("IDENTITY_PROVIDER", "FIREBASE")
	t.Setenv("FIREBASE_PROJECT_ID", "demo-proj")
	res, err := AccountResolverFromEnv()
	if err != nil || res == nil {
		t.Fatalf("expected FIREBASE (uppercase) to resolve, got res=%v err=%v", res, err)
	}

	t.Setenv("IDENTITY_PROVIDER", "local")
	t.Setenv("CLOUD_RUN_JOB", "worker-job")
	t.Setenv("ALLOW_LOCAL_AUTH", "")
	if _, err := AccountResolverFromEnv(); err == nil {
		t.Fatal("expected local provider to be refused when CLOUD_RUN_JOB is set")
	}
}

func TestAccount_NilURLOnResolverErrorDoesNotPanic(t *testing.T) {
	mw := Account(AccountOptions{
		Resolver: failingResolver{err: errors.New("missing token")},
	})
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	req := httptest.NewRequest(http.MethodGet, "/api/v1/protected", nil)
	req.URL = nil
	c.Request = req

	mw(c)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 when Resolver errors with nil URL, got %d body=%s", w.Code, w.Body.String())
	}
}
