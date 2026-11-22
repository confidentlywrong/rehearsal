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

package agent

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestR2_CatalogRejectsOversizedAgentCardBody(t *testing.T) {
	// Serve an agent card larger than the 4 MiB safety cap so a rogue/broken
	// endpoint cannot exhaust gateway memory during discovery.
	oversizedCard := append(
		append([]byte(`{"name":"rogue","url":"http://127.0.0.1:9999","pad":"`), bytes.Repeat([]byte("x"), (4<<20)+1024)...),
		[]byte(`"}`)...,
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(oversizedCard)
	}))
	defer srv.Close()

	cat := NewCatalog([]string{srv.URL})
	agents, err := cat.DiscoverAgents()
	if err == nil {
		t.Fatalf("expected DiscoverAgents to fail when only endpoint returns >4 MiB card, got %v", agents)
	}
}
