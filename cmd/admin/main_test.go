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
	"errors"
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

type closablePubSubStub struct {
	err    error
	closed int
}

func (c *closablePubSubStub) CheckHealth(_, _ string) error { return c.err }
func (c *closablePubSubStub) Close() error {
	c.closed++
	return nil
}

func TestPubSubHealthChecker_ClosesStaleClientOnFailureAndShutdown(t *testing.T) {
	t.Setenv("PUBSUB_PROJECT_ID", "test-proj")
	t.Setenv("PUBSUB_TOPIC_ID", "test-topic")

	stub1 := &closablePubSubStub{err: errors.New("broken pipe")}
	stub2 := &closablePubSubStub{err: nil}
	calls := 0
	hc := &pubsubHealthChecker{
		connectFunc: func() (pubsubChecker, error) {
			calls++
			if calls == 1 {
				return stub1, nil
			}
			return stub2, nil
		},
	}

	if got := hc.Check(); got != "offline" {
		t.Fatalf("first Check() = %q, want offline", got)
	}
	if stub1.closed != 1 {
		t.Fatalf("stub1.closed = %d, want 1 after failed health check", stub1.closed)
	}

	if got := hc.Check(); got != "online" {
		t.Fatalf("second Check() = %q, want online", got)
	}
	hc.Close()
	if stub2.closed != 1 {
		t.Fatalf("stub2.closed = %d, want 1 after Close()", stub2.closed)
	}
}

func TestWireAdminAccountLookup_SafeWithNilAndLocalResolver(t *testing.T) {
	// Nil store or non-Firebase resolver must be a safe no-op.
	wireAdminAccountLookup(nil, nil)
	wireAdminAccountLookup(account.NewLocalResolver(), nil)

	fb, err := account.NewFirebaseResolver(account.FirebaseConfig{ProjectID: "demo-project"})
	if err != nil {
		t.Fatalf("NewFirebaseResolver: %v", err)
	}
	wireAdminAccountLookup(fb, nil)
	wireAdminAccountLookup(fb, &account.Store{})
}

func TestSanitizeHeaderFilename(t *testing.T) {
	got := sanitizeHeaderFilename("uid\"/..\r\n;evil.txt")
	if got != "uid__..___evil.txt" {
		t.Fatalf("sanitizeHeaderFilename = %q", got)
	}
	if empty := sanitizeHeaderFilename(""); empty != "unknown" {
		t.Fatalf("sanitizeHeaderFilename(\"\") = %q, want unknown", empty)
	}
}
