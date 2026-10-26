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
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/localai"
	"github.com/gin-gonic/gin"
)

func TestAiSettingsRoutes_EndToEnd(t *testing.T) {
	gin.SetMode(gin.TestMode)

	mockOllama := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/tags":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"models":[{"name":"gemma2:9b"},{"name":"embeddinggemma:latest"}]}`))
		case "/api/pull":
			w.Header().Set("Content-Type", "application/x-ndjson")
			flusher, _ := w.(http.Flusher)
			_, _ = fmt.Fprintln(w, `{"status":"pulling manifest","completed":50,"total":100}`)
			if flusher != nil {
				flusher.Flush()
			}
			_, _ = fmt.Fprintln(w, `{"status":"success","completed":100,"total":100}`)
			if flusher != nil {
				flusher.Flush()
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer mockOllama.Close()

	cfgPath := filepath.Join(t.TempDir(), "ai_settings.json")
	mgr := localai.NewManager(cfgPath)

	r := gin.New()
	mountAiSettingsRoutes(r, mgr)

	// 1. GET /api/v1/settings/ai returns default cloud_gemini status
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/settings/ai", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("GET /api/v1/settings/ai status=%d body=%s", w.Code, w.Body.String())
	}
	var st localai.EngineStatus
	if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
		t.Fatalf("decode status: %v", err)
	}
	if st.Provider != localai.ProviderCloudGemini {
		t.Fatalf("expected default provider %s, got %s", localai.ProviderCloudGemini, st.Provider)
	}
	if len(st.ModelTiers) < 3 {
		t.Fatalf("expected at least 3 model tiers, got %d", len(st.ModelTiers))
	}

	// 2. PUT /api/v1/settings/ai rejects SSRF non-loopback endpoint
	badPayload := []byte(`{"provider":"local_gemma","endpoint":"http://169.254.169.254:11434","text_model":"gemma2:9b","embed_model":"embeddinggemma:latest"}`)
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPut, "/api/v1/settings/ai", bytes.NewReader(badPayload))
	req.Header.Set("Content-Type", "application/json")
	r.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for SSRF endpoint, got %d (%s)", w.Code, w.Body.String())
	}

	// 3. PUT /api/v1/settings/ai saves local_gemma pointing at mockOllama
	goodPayload, _ := json.Marshal(localai.Config{
		Provider:   localai.ProviderLocalGemma,
		Endpoint:   mockOllama.URL,
		TextModel:  "gemma2:9b",
		EmbedModel: "embeddinggemma:latest",
	})
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPut, "/api/v1/settings/ai", bytes.NewReader(goodPayload))
	req.Header.Set("Content-Type", "application/json")
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("PUT /api/v1/settings/ai status=%d body=%s", w.Code, w.Body.String())
	}
	if err := json.Unmarshal(w.Body.Bytes(), &st); err != nil {
		t.Fatalf("decode PUT response: %v", err)
	}
	if st.Provider != localai.ProviderLocalGemma || !st.Connected {
		t.Fatalf("expected local_gemma and connected=true, got provider=%s connected=%v", st.Provider, st.Connected)
	}
	if !st.EmbedEngine.Installed {
		t.Fatalf("expected embeddinggemma:latest installed=true")
	}

	// 4. POST /api/v1/settings/ai/test verifies live connection
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/api/v1/settings/ai/test", bytes.NewReader([]byte(`{}`)))
	req.Header.Set("Content-Type", "application/json")
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("POST /test status=%d body=%s", w.Code, w.Body.String())
	}

	// 5. POST /api/v1/settings/ai/pull streams model pull and returns progress
	pullPayload, _ := json.Marshal(aiPullRequest{
		Model:    "gemma2:2b",
		Endpoint: mockOllama.URL,
	})
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, "/api/v1/settings/ai/pull", bytes.NewReader(pullPayload))
	req.Header.Set("Content-Type", "application/json")
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("POST /pull status=%d body=%s", w.Code, w.Body.String())
	}

	// 6. GET /api/v1/settings/ai?endpoint=... probes the specified endpoint and rejects SSRF
	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/api/v1/settings/ai?endpoint="+mockOllama.URL, nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("GET /api/v1/settings/ai?endpoint=... status=%d body=%s", w.Code, w.Body.String())
	}

	w = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/api/v1/settings/ai?endpoint=http://169.254.169.254:11434", nil)
	r.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for SSRF ?endpoint=, got %d", w.Code)
	}
}

