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
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
)

type dummyProvider struct{}
func (d dummyProvider) Name() string { return "dummy" }
func (d dummyProvider) VerifyAndParse(payload []byte, sigHeader string, now time.Time) (*billing.WebhookEvent, error) { 
	return &billing.WebhookEvent{ID: "evt_dummy"}, nil 
}

func TestBillingHealthCheck(t *testing.T) {
	r := newRouter(dummyProvider{}, billing.PriceTierMap{}, nil, nil, nil, time.Now, nil)
	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/healthz", nil)
	req.RemoteAddr = "127.0.0.1:1234"
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Expected status 200, got %d", w.Code)
	}
}
