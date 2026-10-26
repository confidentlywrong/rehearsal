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

package memory

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/localai"
)

func cosineSim(a, b []float32) float64 {
	var dot, normA, normB float64
	for i := range a {
		dot += float64(a[i]) * float64(b[i])
		normA += float64(a[i]) * float64(a[i])
		normB += float64(b[i]) * float64(b[i])
	}
	if normA == 0 || normB == 0 {
		return 0
	}
	return dot / (math.Sqrt(normA) * math.Sqrt(normB))
}

func TestNormalizeAndPadVectorPreservesCosineSimilarity(t *testing.T) {
	v1 := make([]float32, 768)
	v2 := make([]float32, 768)
	for i := 0; i < 768; i++ {
		v1[i] = float32(i+1) * 0.01
		v2[i] = float32(768-i) * 0.02
	}

	origSim := cosineSim(v1, v2)
	p1 := NormalizeAndPadVector(v1, TargetVectorDimension)
	p2 := NormalizeAndPadVector(v2, TargetVectorDimension)

	if len(p1) != TargetVectorDimension || len(p2) != TargetVectorDimension {
		t.Fatalf("expected length %d, got %d and %d", TargetVectorDimension, len(p1), len(p2))
	}

	for i := 768; i < TargetVectorDimension; i++ {
		if p1[i] != 0 || p2[i] != 0 {
			t.Fatalf("expected zero padding at index %d, got %f / %f", i, p1[i], p2[i])
		}
	}

	paddedSim := cosineSim(p1, p2)
	if math.Abs(origSim-paddedSim) > 1e-6 {
		t.Fatalf("expected cosine similarity %f to match %f", paddedSim, origSim)
	}
}

func TestLocalGemmaEmbedderAndDynamicEmbedder(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/embed" {
			http.NotFound(w, r)
			return
		}
		var req ollamaEmbedRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if req.Model != "embeddinggemma:latest" {
			t.Errorf("expected model embeddinggemma:latest, got %s", req.Model)
		}
		out := make([][]float32, len(req.Input))
		for i := range req.Input {
			vec := make([]float32, 768)
			vec[0] = 3.0
			vec[1] = 4.0
			out[i] = vec
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ollamaEmbedResponse{Embeddings: out})
	}))
	defer srv.Close()

	cfgPath := filepath.Join(t.TempDir(), "ai_settings.json")
	mgr := localai.NewManager(cfgPath)
	if _, err := mgr.SaveConfig(localai.Config{
		Provider:   localai.ProviderLocalGemma,
		Endpoint:   srv.URL,
		TextModel:  "gemma2:9b",
		EmbedModel: "embeddinggemma:latest",
	}); err != nil {
		t.Fatalf("SaveConfig failed: %v", err)
	}

	dyn := NewDynamicEmbedder(mgr, nil)
	vec, err := dyn.EmbedText(context.Background(), "Board governance threshold")
	if err != nil {
		t.Fatalf("EmbedText failed: %v", err)
	}
	if len(vec) != TargetVectorDimension {
		t.Fatalf("expected %d dimensions, got %d", TargetVectorDimension, len(vec))
	}
	if math.Abs(float64(vec[0])-0.6) > 1e-5 || math.Abs(float64(vec[1])-0.8) > 1e-5 {
		t.Fatalf("expected L2-normalized [0.6, 0.8, 0...], got [%f, %f]", vec[0], vec[1])
	}

	// When switched to cloud_gemini without VertexEmbedder configured, it must NOT
	// silently fall back to local Ollama; it must return a descriptive error.
	if _, err := mgr.SaveConfig(localai.Config{
		Provider:   localai.ProviderCloudGemini,
		Endpoint:   srv.URL,
		TextModel:  "gemma2:9b",
		EmbedModel: "embeddinggemma:latest",
	}); err != nil {
		t.Fatalf("SaveConfig failed: %v", err)
	}
	if _, err := dyn.EmbedText(context.Background(), "should fail without vertex"); err == nil {
		t.Fatalf("expected error when cloud_gemini is active without VertexEmbedder, got nil")
	}

	// SSRF guard in LocalGemmaEmbedder
	ssrfEmb := NewLocalGemmaEmbedder(func() (string, string) {
		return "http://169.254.169.254:11434", "embeddinggemma:latest"
	})
	if _, err := ssrfEmb.EmbedText(context.Background(), "test"); err == nil {
		t.Fatalf("expected SSRF endpoint to be rejected by LocalGemmaEmbedder")
	}

	// Negative or zero targetDim in NormalizeAndPadVector must not panic
	if got := NormalizeAndPadVector([]float32{1, 2}, -5); got != nil {
		t.Fatalf("expected nil for negative targetDim, got %v", got)
	}

	// Empty embedding vector returned by Ollama must error instead of returning all-zeros
	emptySrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ollamaEmbedResponse{Embeddings: [][]float32{{}}})
	}))
	defer emptySrv.Close()
	emptyEmb := NewLocalGemmaEmbedder(func() (string, string) {
		return emptySrv.URL, "embeddinggemma:latest"
	})
	if _, err := emptyEmb.EmbedText(context.Background(), "empty vec"); err == nil {
		t.Fatalf("expected error when Ollama returns an empty embedding vector")
	}

	// Non-empty all-zero embedding vector must also be rejected (prevents NaN cosine distance in pgvector)
	zeroSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ollamaEmbedResponse{Embeddings: [][]float32{make([]float32, 768)}})
	}))
	defer zeroSrv.Close()
	zeroEmb := NewLocalGemmaEmbedder(func() (string, string) {
		return zeroSrv.URL, "embeddinggemma:latest"
	})
	if _, err := zeroEmb.EmbedText(context.Background(), "all zeros"); err == nil {
		t.Fatalf("expected error when Ollama returns an all-zero embedding vector")
	}

	// When len(vec) > targetDim, NormalizeAndPadVector must maintain unit L2 norm on the truncated output
	trunc := NormalizeAndPadVector([]float32{3, 4, 100, 200}, 2)
	if len(trunc) != 2 || math.Abs(float64(trunc[0])-0.6) > 1e-5 || math.Abs(float64(trunc[1])-0.8) > 1e-5 {
		t.Fatalf("expected truncated unit vector [0.6, 0.8], got %v", trunc)
	}
}


