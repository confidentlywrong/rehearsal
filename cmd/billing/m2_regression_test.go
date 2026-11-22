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

package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/gin-gonic/gin"
)

type stubPlanSetter struct {
	setCalls int
	setErr   error
}

func (s *stubPlanSetter) SetPlan(ctx context.Context, userID string, plan account.PlanTier) error {
	s.setCalls++
	return s.setErr
}

type stubUserLookup struct {
	getUser       account.User
	getUserErr    error
	byCustomer    account.User
	byCustomerErr error
}

func (s *stubUserLookup) GetUser(ctx context.Context, id string) (account.User, error) {
	return s.getUser, s.getUserErr
}

func (s *stubUserLookup) GetUserByStripeCustomerID(ctx context.Context, customerID string) (account.User, error) {
	return s.byCustomer, s.byCustomerErr
}

func (s *stubUserLookup) MarkPaymentFailed(ctx context.Context, userID string, ts time.Time) error {
	return nil
}

func (s *stubUserLookup) ClearPaymentFailed(ctx context.Context, userID string) error {
	return nil
}

func TestBillingHealthOnly_Returns503(t *testing.T) {
	t.Setenv("TRUSTED_PROXIES", "")

	for _, tc := range []struct {
		name     string
		provider billing.Provider
		wantProv string
	}{
		{name: "with provider", provider: dummyProvider{}, wantProv: "dummy"},
		{name: "nil provider", provider: nil, wantProv: ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := newHealthOnlyRouter(tc.provider, billing.PriceTierMap{"price_1": account.PlanPro})
			w := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
			req.RemoteAddr = "127.0.0.1:1234"
			r.ServeHTTP(w, req)

			if w.Code != http.StatusServiceUnavailable {
				t.Fatalf("expected 503, got %d body=%s", w.Code, w.Body.String())
			}
			var got map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
				t.Fatalf("invalid json: %v", err)
			}
			if got["status"] != "unhealthy" || got["mode"] != "degraded" || got["reason"] != "postgres unavailable" {
				t.Fatalf("unexpected healthz payload: %v", got)
			}
			if got["provider"] != tc.wantProv {
				t.Fatalf("expected provider %q, got %v", tc.wantProv, got["provider"])
			}
		})
	}
}

func TestWebhook_DedupErrorFailsClosed503_WithoutForget(t *testing.T) {
	gin.SetMode(gin.TestMode)
	dedup := newMockDedup()
	dedup.failErr = errors.New("postgres down")
	store := &stubPlanSetter{}
	prov := adversarialProvider{
		event: &billing.WebhookEvent{
			ID:   "evt_dedup_err",
			Type: billing.EventCheckoutCompleted,
			CheckoutCompleted: &billing.CheckoutCompletedEvent{
				UserID:  "user-1",
				PriceID: "price_pro",
			},
		},
	}
	r := newRouter(prov, billing.PriceTierMap{"price_pro": account.PlanPro}, store, nil, dedup, time.Now, nil)

	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(`{}`))
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 on dedup error, got %d body=%s", w.Code, w.Body.String())
	}
	if store.setCalls != 0 {
		t.Fatalf("expected SetPlan not to be called, got %d", store.setCalls)
	}
	if dedup.forgetCalls != 0 {
		t.Fatalf("expected Forget not to be called on SeenOrMark error, got %d", dedup.forgetCalls)
	}
}

func TestWebhook_NilGuards_FailClosedWithoutPanic(t *testing.T) {
	gin.SetMode(gin.TestMode)

	t.Run("nil dedup returns 503", func(t *testing.T) {
		r := newRouter(adversarialProvider{}, billing.PriceTierMap{}, nil, nil, nil, nil, nil)
		req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(`{}`))
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("expected 503, got %d body=%s", w.Code, w.Body.String())
		}
	})

	t.Run("nil provider returns 503", func(t *testing.T) {
		r := newRouter(nil, billing.PriceTierMap{}, nil, nil, newMockDedup(), nil, nil)
		req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(`{}`))
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("expected 503, got %d body=%s", w.Code, w.Body.String())
		}
	})

	t.Run("nil body returns 400", func(t *testing.T) {
		r := newRouter(adversarialProvider{}, billing.PriceTierMap{}, nil, nil, newMockDedup(), time.Now, nil)
		req := httptest.NewRequest(http.MethodPost, "/webhook", nil)
		req.Body = nil
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("expected 400, got %d body=%s", w.Code, w.Body.String())
		}
	})
}

func TestCheckoutCompleted_CustomerLookupDBError500_TriggersForget(t *testing.T) {
	gin.SetMode(gin.TestMode)
	dedup := newMockDedup()
	store := &stubPlanSetter{}
	users := &stubUserLookup{byCustomerErr: errors.New("db timeout")}
	prov := adversarialProvider{
		event: &billing.WebhookEvent{
			ID:   "evt_lookup_err",
			Type: billing.EventCheckoutCompleted,
			CheckoutCompleted: &billing.CheckoutCompletedEvent{
				UserID:   "user-1",
				PriceID:  "price_pro",
				Customer: "cus_123",
			},
		},
	}
	r := newRouter(prov, billing.PriceTierMap{"price_pro": account.PlanPro}, store, users, dedup, time.Now, nil)

	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(`{}`))
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500, got %d body=%s", w.Code, w.Body.String())
	}
	if store.setCalls != 0 {
		t.Fatalf("expected SetPlan not to be called when customer lookup fails, got %d", store.setCalls)
	}
	if dedup.forgetCalls != 1 {
		t.Fatalf("expected Forget to be called once on 500, got %d", dedup.forgetCalls)
	}
}

func TestApplyPlan_NilStoreReturns503_TriggersForget(t *testing.T) {
	gin.SetMode(gin.TestMode)
	dedup := newMockDedup()
	prov := adversarialProvider{
		event: &billing.WebhookEvent{
			ID:   "evt_nil_store",
			Type: billing.EventCheckoutCompleted,
			CheckoutCompleted: &billing.CheckoutCompletedEvent{
				UserID:  "user-1",
				PriceID: "price_pro",
			},
		},
	}
	r := newRouter(prov, billing.PriceTierMap{"price_pro": account.PlanPro}, nil, nil, dedup, time.Now, nil)

	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(`{}`))
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d body=%s", w.Code, w.Body.String())
	}
	if dedup.forgetCalls != 1 {
		t.Fatalf("expected Forget to be called once on 503, got %d", dedup.forgetCalls)
	}
}

func TestBillingTrustedProxies_IgnoresSpoofedXForwardedFor(t *testing.T) {
	gin.SetMode(gin.TestMode)
	t.Setenv("TRUSTED_PROXIES", "")

	r := newRouter(dummyProvider{}, billing.PriceTierMap{}, nil, nil, nil, time.Now, nil)
	var gotClientIP string
	r.GET("/ip", func(c *gin.Context) {
		gotClientIP = c.ClientIP()
		c.Status(http.StatusOK)
	})

	req := httptest.NewRequest(http.MethodGet, "/ip", nil)
	req.RemoteAddr = "127.0.0.1:4567"
	req.Header.Set("X-Forwarded-For", "203.0.113.99")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if gotClientIP != "127.0.0.1" {
		t.Fatalf("expected spoofed X-Forwarded-For to be ignored (got %q)", gotClientIP)
	}
}
