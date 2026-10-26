// Copyright 2026 The Confidently Wrong Authors
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
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/gin-gonic/gin"
)

type mockDedup struct {
	mu   sync.Mutex
	seen map[string]bool
}

func newMockDedup() *mockDedup {
	return &mockDedup{seen: make(map[string]bool)}
}

func (m *mockDedup) SeenOrMark(id string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.seen[id] {
		return true
	}
	m.seen[id] = true
	return false
}

func (m *mockDedup) Forget(id string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.seen, id)
}

type adversarialProvider struct {
	failSig bool
	event   *billing.WebhookEvent
}

func (p adversarialProvider) Name() string { return "adversarial" }
func (p adversarialProvider) VerifyAndParse(payload []byte, sigHeader string, now time.Time) (*billing.WebhookEvent, error) {
	if p.failSig {
		return nil, billing.ErrSignatureInvalid
	}
	if p.event == nil {
		return &billing.WebhookEvent{ID: "evt_default", Type: "unknown_event"}, nil
	}
	return p.event, nil
}

func TestBillingAdversarialWebhookAndLimits(t *testing.T) {
	gin.SetMode(gin.TestMode)

	t.Run("Payload exceeding 1MB max limit is rejected", func(t *testing.T) {
		r := newRouter(adversarialProvider{}, billing.PriceTierMap{}, nil, nil, newMockDedup(), time.Now, nil)
		hugeBody := bytes.Repeat([]byte("A"), (1<<20)+100)
		req, _ := http.NewRequest("POST", "/webhook", bytes.NewReader(hugeBody))
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusBadRequest {
			t.Errorf("Expected 400 for payload > 1MB, got %d", w.Code)
		}
	})

	t.Run("Invalid signature returns 400", func(t *testing.T) {
		prov := adversarialProvider{failSig: true}
		r := newRouter(prov, billing.PriceTierMap{}, nil, nil, newMockDedup(), time.Now, nil)
		req, _ := http.NewRequest("POST", "/webhook", strings.NewReader(`{"id":"evt_1"}`))
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusBadRequest {
			t.Errorf("Expected 400 for invalid signature, got %d", w.Code)
		}
	})

	t.Run("Webhook deduplication returns 200 deduped", func(t *testing.T) {
		prov := adversarialProvider{
			event: &billing.WebhookEvent{
				ID:   "evt_dup",
				Type: "test_type",
			},
		}
		dedup := newMockDedup()
		r := newRouter(prov, billing.PriceTierMap{}, nil, nil, dedup, time.Now, nil)

		// First request -> processed/ignored
		req1, _ := http.NewRequest("POST", "/webhook", strings.NewReader(`{"id":"evt_dup"}`))
		w1 := httptest.NewRecorder()
		r.ServeHTTP(w1, req1)
		if w1.Code != http.StatusOK {
			t.Fatalf("First request expected 200, got %d", w1.Code)
		}

		// Second request -> deduped
		req2, _ := http.NewRequest("POST", "/webhook", strings.NewReader(`{"id":"evt_dup"}`))
		w2 := httptest.NewRecorder()
		r.ServeHTTP(w2, req2)
		if w2.Code != http.StatusOK {
			t.Fatalf("Second request expected 200, got %d", w2.Code)
		}
		if !strings.Contains(w2.Body.String(), `"status":"deduped"`) {
			t.Errorf("Expected status deduped in body, got %s", w2.Body.String())
		}
	})

	t.Run("Missing event ID returns 422", func(t *testing.T) {
		prov := adversarialProvider{
			event: &billing.WebhookEvent{
				ID:   "",
				Type: "test_type",
			},
		}
		r := newRouter(prov, billing.PriceTierMap{}, nil, nil, newMockDedup(), time.Now, nil)
		req, _ := http.NewRequest("POST", "/webhook", strings.NewReader(`{"id":""}`))
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusUnprocessableEntity {
			t.Errorf("Expected 422 for missing event ID, got %d", w.Code)
		}
	})

	t.Run("Concurrent duplicate webhook requests race-free", func(t *testing.T) {
		prov := adversarialProvider{
			event: &billing.WebhookEvent{
				ID:   "evt_concurrent_race",
				Type: "test_type",
			},
		}
		dedup := newMockDedup()
		r := newRouter(prov, billing.PriceTierMap{}, nil, nil, dedup, time.Now, nil)

		var wg sync.WaitGroup
		for i := 0; i < 20; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				req, _ := http.NewRequest("POST", "/webhook", strings.NewReader(`{}`))
				w := httptest.NewRecorder()
				r.ServeHTTP(w, req)
				if w.Code != http.StatusOK {
					t.Errorf("Expected 200, got %d", w.Code)
				}
			}()
		}
		wg.Wait()
	})
}
