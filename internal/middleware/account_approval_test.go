// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.
//
// Tests for the manual-approval gate baked into middleware.Account.
// Exercise only the gate itself (ApprovedPaths + NotApprovedHandler)
// — the rest of Account is covered indirectly by gateway/account
// package tests.

package middleware

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

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
