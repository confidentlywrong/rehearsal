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

package localai

import (
	"context"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func TestR2_PullModelRejectsInvalidBundleAndEmbedTargets(t *testing.T) {
	var pullCalls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/pull" {
			pullCalls.Add(1)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	mgr := NewManager(filepath.Join(t.TempDir(), "ai_settings.json"))
	if _, err := mgr.SaveConfig(Config{
		Provider:   ProviderLocalGemma,
		Endpoint:   srv.URL,
		TextModel:  "gemma2:9b",
		EmbedModel: DefaultEmbedModel,
	}); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}

	for _, invalidTarget := range []string{
		"bundle:",
		"bundle:bundle",
		"bundle:bundle:gemma2:9b",
		"bundle:embeddinggemma:latest",
		"bundle:ollama_chat/embeddinggemma:latest",
	} {
		_, err := mgr.PullModel(context.Background(), srv.URL, invalidTarget)
		if err == nil || !strings.Contains(err.Error(), "invalid") {
			t.Errorf("PullModel(%q) expected invalid target error, got %v", invalidTarget, err)
		}
	}
	if got := pullCalls.Load(); got != 0 {
		t.Fatalf("expected 0 outbound /api/pull calls for invalid targets, got %d", got)
	}
}
