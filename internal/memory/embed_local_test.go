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
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
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

func TestLocalGemmaEmbedder_404ModelNotFoundDoesNotFallbackToLegacy(t *testing.T) {
	legacyHit := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/embeddings" {
			legacyHit = true
		}
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"error":"model 'embeddinggemma:latest' not found"}`))
	}))
	defer srv.Close()

	emb := NewLocalGemmaEmbedder(func() (string, string) {
		return srv.URL, "embeddinggemma:latest"
	})
	if _, err := emb.EmbedText(context.Background(), "hello"); err == nil {
		t.Fatal("expected error on 404 model not found")
	}
	if legacyHit {
		t.Fatal("must NOT fall back to /api/embeddings when Ollama returns 404 model not found")
	}

	// Stripped-to-empty explicit model ("ollama/") must error explicitly instead of silent DefaultEmbedModel fallback.
	badModelEmb := NewLocalGemmaEmbedder(func() (string, string) {
		return srv.URL, "ollama/"
	})
	if _, err := badModelEmb.EmbedText(context.Background(), "hello"); err == nil {
		t.Fatal("expected error when explicit model strips to empty")
	}

	// Nil receiver must error cleanly.
	var nilEmb *LocalGemmaEmbedder
	if _, err := nilEmb.EmbedText(context.Background(), "hello"); err == nil {
		t.Fatal("expected error from nil *LocalGemmaEmbedder")
	}
}

func TestDynamicEmbedder_NilAIManagerAndNilVertexReturnsError(t *testing.T) {
	dyn := NewDynamicEmbedder(nil, nil)
	if _, err := dyn.EmbedText(context.Background(), "hello"); err == nil {
		t.Fatal("expected explicit error when both aiManager and vertex are nil")
	}
	if _, err := dyn.EmbedBatch(context.Background(), []string{"hello"}); err == nil {
		t.Fatal("expected explicit error from EmbedBatch when both aiManager and vertex are nil")
	}
	var nilDyn *DynamicEmbedder
	if _, err := nilDyn.EmbedText(context.Background(), "hello"); err == nil {
		t.Fatal("expected error from nil *DynamicEmbedder.EmbedText")
	}
	if _, err := nilDyn.EmbedBatch(context.Background(), []string{"hello"}); err == nil {
		t.Fatal("expected error from nil *DynamicEmbedder.EmbedBatch")
	}
}

func assertUnitNormalizedVector(t *testing.T, got []float32, nonZeroCount int, expectedVal float64) {
	t.Helper()
	if len(got) != TargetVectorDimension {
		t.Fatalf("expected length %d, got %d", TargetVectorDimension, len(got))
	}
	var sumSq float64
	for i := 0; i < TargetVectorDimension; i++ {
		v := float64(got[i])
		if math.IsNaN(v) || math.IsInf(v, 0) {
			t.Fatalf("non-finite component at index %d: %v", i, got[i])
		}
		if i < nonZeroCount {
			if math.Abs(v-expectedVal) > 1e-5 {
				t.Fatalf("got[%d] = %v, want ~%v", i, v, expectedVal)
			}
		} else if got[i] != 0 {
			t.Fatalf("expected zero padding at index %d, got %v", i, got[i])
		}
		sumSq += v * v
	}
	norm := math.Sqrt(sumSq)
	if math.Abs(norm-1.0) > 1e-5 {
		t.Fatalf("expected unit L2 norm ~1.0, got %v", norm)
	}
}

func TestNormalizeAndPadVector_Float32NormOverflow(t *testing.T) {
	// 1. Two-element vector [MaxFloat32, MaxFloat32] whose L2 norm > math.MaxFloat32.
	twoMax := []float32{math.MaxFloat32, math.MaxFloat32}
	if !hasValidEmbeddingNorm(twoMax, TargetVectorDimension) {
		t.Fatal("expected hasValidEmbeddingNorm to accept [MaxFloat32, MaxFloat32]")
	}
	gotTwo := NormalizeAndPadVector(twoMax, TargetVectorDimension)
	assertUnitNormalizedVector(t, gotTwo, 2, 1.0/math.Sqrt(2))

	// 2. 768-d vector filled with 1.3e37 (L2 norm ~3.603e38 > math.MaxFloat32 ~3.403e38).
	vec768 := make([]float32, 768)
	for i := range vec768 {
		vec768[i] = 1.3e37
	}
	if !hasValidEmbeddingNorm(vec768, TargetVectorDimension) {
		t.Fatal("expected hasValidEmbeddingNorm to accept 768-d 1.3e37 vector")
	}
	got768 := NormalizeAndPadVector(vec768, TargetVectorDimension)
	assertUnitNormalizedVector(t, got768, 768, 1.0/math.Sqrt(768))

	// 3. Verify hasValidEmbeddingNorm and NormalizeAndPadVector agree on rejection edge cases.
	rejectCases := []struct {
		name      string
		vec       []float32
		targetDim int
	}{
		{"sub-threshold norm", []float32{1e-13, 0}, 4},
		{"NaN component", []float32{float32(math.NaN()), 1}, 4},
		{"Inf component", []float32{float32(math.Inf(1)), 1}, 4},
		{"zero prefix before truncation", []float32{0, 0, 1}, 2},
	}
	for _, tc := range rejectCases {
		t.Run(tc.name, func(t *testing.T) {
			if hasValidEmbeddingNorm(tc.vec, tc.targetDim) {
				t.Fatalf("expected hasValidEmbeddingNorm=false for %s", tc.name)
			}
			out := NormalizeAndPadVector(tc.vec, tc.targetDim)
			if len(out) != tc.targetDim {
				t.Fatalf("expected len=%d, got %d", tc.targetDim, len(out))
			}
			for i, v := range out {
				if v != 0 {
					t.Fatalf("expected all-zero output for rejected %s, got out[%d]=%v", tc.name, i, v)
				}
			}
		})
	}
}

func TestLocalGemmaEmbedder_Float32NormOverflow(t *testing.T) {
	const rawVal float32 = 3e38
	expected := 1.0 / math.Sqrt(2)

	t.Run("modern /api/embed", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/api/embed" {
				http.NotFound(w, r)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(ollamaEmbedResponse{
				Embeddings: [][]float32{{rawVal, rawVal, 0, 0}},
			})
		}))
		defer srv.Close()

		emb := NewLocalGemmaEmbedder(func() (string, string) {
			return srv.URL, "embeddinggemma:latest"
		})
		got, err := emb.EmbedText(context.Background(), "overflow probe")
		if err != nil {
			t.Fatalf("EmbedText failed: %v", err)
		}
		assertUnitNormalizedVector(t, got, 2, expected)
	})

	t.Run("legacy /api/embeddings", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/api/embed" {
				http.NotFound(w, r)
				return
			}
			if r.URL.Path != "/api/embeddings" {
				http.NotFound(w, r)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(ollamaLegacyEmbedResponse{
				Embedding: []float32{rawVal, rawVal, 0, 0},
			})
		}))
		defer srv.Close()

		emb := NewLocalGemmaEmbedder(func() (string, string) {
			return srv.URL, "embeddinggemma:latest"
		})
		got, err := emb.EmbedText(context.Background(), "legacy overflow probe")
		if err != nil {
			t.Fatalf("EmbedText legacy failed: %v", err)
		}
		assertUnitNormalizedVector(t, got, 2, expected)
	})
}

func TestDynamicEmbedder_CorruptAISettingsFailsClosedWithoutFallback(t *testing.T) {
	var primaryHits atomic.Int32
	primaryOllama := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		primaryHits.Add(1)
		if r.URL.Path != "/api/embed" {
			http.NotFound(w, r)
			return
		}
		var req ollamaEmbedRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
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
	defer primaryOllama.Close()

	var fallbackHits atomic.Int32
	fallbackOllama := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fallbackHits.Add(1)
		http.Error(w, "fallback ollama must not be called", http.StatusInternalServerError)
	}))
	defer fallbackOllama.Close()

	var vertexHits atomic.Int32
	vertex := &VertexEmbedder{
		projectID: "test-project",
		region:    "us-central1",
		client: &http.Client{
			Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				vertexHits.Add(1)
				return nil, errors.New("vertex must not be called on corrupt ai_settings.json")
			}),
		},
	}

	cfgPath := filepath.Join(t.TempDir(), "ai_settings.json")
	mgr := localai.NewManager(cfgPath)
	if _, err := mgr.SaveConfig(localai.Config{
		Provider:   localai.ProviderLocalGemma,
		Endpoint:   primaryOllama.URL,
		TextModel:  "gemma2:9b",
		EmbedModel: "embeddinggemma:latest",
	}); err != nil {
		t.Fatalf("SaveConfig failed: %v", err)
	}

	// Sanity check: healthy local_gemma config succeeds via primaryOllama.
	dyn := NewDynamicEmbedder(mgr, vertex)
	if _, err := dyn.EmbedText(context.Background(), "healthy check"); err != nil {
		t.Fatalf("expected healthy local_gemma EmbedText to succeed, got %v", err)
	}
	if primaryHits.Load() != 1 {
		t.Fatalf("expected 1 hit to primaryOllama before corruption, got %d", primaryHits.Load())
	}
	primaryHits.Store(0)

	// Corrupt ai_settings.json on disk.
	if err := os.WriteFile(cfgPath, []byte("{corrupt"), 0o600); err != nil {
		t.Fatalf("write corrupt config: %v", err)
	}

	t.Run("AI_PROVIDER_unset_nil_vertex", func(t *testing.T) {
		t.Setenv("AI_PROVIDER", "")
		d := NewDynamicEmbedder(mgr, nil)
		if _, err := d.EmbedText(context.Background(), "query"); err == nil || !strings.Contains(err.Error(), "parse settings") {
			t.Fatalf("expected parse settings error from EmbedText, got %v", err)
		}
		if _, err := d.EmbedBatch(context.Background(), []string{"chunk1", "chunk2"}); err == nil || !strings.Contains(err.Error(), "parse settings") {
			t.Fatalf("expected parse settings error from EmbedBatch, got %v", err)
		}
	})

	t.Run("AI_PROVIDER_unset_nonnil_vertex", func(t *testing.T) {
		t.Setenv("AI_PROVIDER", "")
		d := NewDynamicEmbedder(mgr, vertex)
		if _, err := d.EmbedText(context.Background(), "query"); err == nil || !strings.Contains(err.Error(), "parse settings") {
			t.Fatalf("expected parse settings error from EmbedText, got %v", err)
		}
		if _, err := d.EmbedBatch(context.Background(), []string{"chunk1"}); err == nil || !strings.Contains(err.Error(), "parse settings") {
			t.Fatalf("expected parse settings error from EmbedBatch, got %v", err)
		}
	})

	t.Run("AI_PROVIDER_local_gemma_with_fallback_env", func(t *testing.T) {
		t.Setenv("AI_PROVIDER", localai.ProviderLocalGemma)
		t.Setenv("OLLAMA_API_BASE", fallbackOllama.URL)
		d := NewDynamicEmbedder(mgr, vertex)
		if _, err := d.EmbedText(context.Background(), "query"); err == nil || !strings.Contains(err.Error(), "parse settings") {
			t.Fatalf("expected parse settings error from EmbedText, got %v", err)
		}
		if _, err := d.EmbedBatch(context.Background(), []string{"chunk1"}); err == nil || !strings.Contains(err.Error(), "parse settings") {
			t.Fatalf("expected parse settings error from EmbedBatch, got %v", err)
		}
	})

	t.Run("unreadable_settings_path", func(t *testing.T) {
		dirMgr := localai.NewManager(t.TempDir())
		d := NewDynamicEmbedder(dirMgr, vertex)
		if _, err := d.EmbedText(context.Background(), "query"); err == nil || !strings.Contains(err.Error(), "read settings") {
			t.Fatalf("expected read settings error from EmbedText, got %v", err)
		}
		if _, err := d.EmbedBatch(context.Background(), []string{}); err == nil || !strings.Contains(err.Error(), "read settings") {
			t.Fatalf("expected read settings error from EmbedBatch, got %v", err)
		}
	})

	if got := primaryHits.Load(); got != 0 {
		t.Fatalf("expected 0 hits to primaryOllama after corruption, got %d", got)
	}
	if got := fallbackHits.Load(); got != 0 {
		t.Fatalf("expected 0 hits to fallbackOllama after corruption, got %d", got)
	}
	if got := vertexHits.Load(); got != 0 {
		t.Fatalf("expected 0 hits to Vertex after corruption, got %d", got)
	}
}



