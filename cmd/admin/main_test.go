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
	"net/http"
	"net/http/httptest"
	"testing"
	
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

func TestHealthCheck(t *testing.T) {
	// Simple test to hit the health endpoint
	// Pass nils for DBs since healthcheck handles nil checks
	r := setupRouter(nil, nil, nil, "http://dummy", nil, account.NewLocalResolver(), nil, nil)
	w := httptest.NewRecorder()
	req, _ := http.NewRequest("GET", "/health", nil)
	req.RemoteAddr = "127.0.0.1:1234"
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("Expected status 200, got %d", w.Code)
	}
}
