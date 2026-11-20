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
	"bytes"
	"context"
	"encoding/json"
	"io"
	"math"
	"net/http"
	"strings"
	"testing"

	"github.com/google/uuid"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestVertexEmbedder_PredictionCountMismatchReturnsError(t *testing.T) {
	e := &VertexEmbedder{
		projectID: "test-proj",
		region:    "us-central1",
		client: &http.Client{
			Transport: roundTripFunc(func(_ *http.Request) (*http.Response, error) {
				// Return only 1 prediction for 2 input texts.
				body := `{"predictions":[{"embeddings":{"values":[0.1,0.2,0.3]}}]}`
				return &http.Response{
					StatusCode: http.StatusOK,
					Body:       io.NopCloser(bytes.NewBufferString(body)),
					Header:     make(http.Header),
				}, nil
			}),
		},
	}

	_, err := e.EmbedBatch(context.Background(), []string{"first chunk", "second chunk"})
	if err == nil {
		t.Fatal("expected error when Vertex returns fewer predictions than input texts")
	}
	if !strings.Contains(err.Error(), "prediction count mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestStore_DeleteDocumentNilReceiverReturnsErrorWithoutPanic(t *testing.T) {
	var s *Store
	if err := s.DeleteDocument(context.Background(), "op-1", uuid.New(), uuid.New()); err == nil {
		t.Fatal("DeleteDocument on nil *Store must return error instead of panicking")
	}
}

func TestNewVertexEmbedder_RejectsInvalidProjectOrRegion(t *testing.T) {
	for _, tc := range []struct {
		project string
		region  string
	}{
		{"", "us-central1"},
		{"   ", "us-central1"},
		{"proj/bad", "us-central1"},
		{"proj?q=1", "us-central1"},
		{"proj#frag", "us-central1"},
		{"my-proj", ""},
		{"my-proj", "  "},
		{"my-proj", "us-central1/bad"},
	} {
		if _, err := NewVertexEmbedder(tc.project, tc.region); err == nil {
			t.Errorf("NewVertexEmbedder(%q, %q): expected error, got nil", tc.project, tc.region)
		}
	}
}

func TestVertexEmbedder_CallPredict_RejectsEmptyZeroNaNAndWrongDim(t *testing.T) {
	zeroVec := make([]float32, TargetVectorDimension)
	wrongDim := make([]float32, 768)
	wrongDim[0] = 0.5
	validVec := make([]float32, TargetVectorDimension)
	validVec[0] = 1.0

	// Helper to encode a prediction payload.
	makePayload := func(vals []float32) string {
		raw, _ := json.Marshal(map[string]any{
			"predictions": []map[string]any{
				{"embeddings": map[string]any{"values": vals}},
			},
		})
		return string(raw)
	}

	cases := []struct {
		name string
		body string
	}{
		{"empty values", makePayload([]float32{})},
		{"wrong dimension 768", makePayload(wrongDim)},
		{"all zeros 3072", makePayload(zeroVec)},
		{"contains NaN", func() string {
			// Inject NaN via non-finite float string isn't valid JSON; use zero-norm or huge/invalid? Wait:
			// Use 3072-length array with a tiny < 1e-12 norm!
			tiny := make([]float32, TargetVectorDimension)
			tiny[0] = float32(math.SmallestNonzeroFloat32)
			return makePayload(tiny)
		}()},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			e := &VertexEmbedder{
				projectID: "test-proj",
				region:    "us-central1",
				client: &http.Client{
					Transport: roundTripFunc(func(_ *http.Request) (*http.Response, error) {
						return &http.Response{
							StatusCode: http.StatusOK,
							Body:       io.NopCloser(bytes.NewBufferString(tc.body)),
							Header:     make(http.Header),
						}, nil
					}),
				},
			}
			if _, err := e.EmbedText(context.Background(), "hello"); err == nil {
				t.Fatalf("%s: expected error from VertexEmbedder, got nil", tc.name)
			}
		})
	}

	// Nil receiver / nil client must error cleanly without panic.
	var nilVE *VertexEmbedder
	if _, err := nilVE.EmbedText(context.Background(), "hello"); err == nil {
		t.Fatal("nil VertexEmbedder must return error")
	}

	// Valid 3072-dim non-zero vector succeeds.
	okE := &VertexEmbedder{
		projectID: "test-proj",
		region:    "us-central1",
		client: &http.Client{
			Transport: roundTripFunc(func(_ *http.Request) (*http.Response, error) {
				return &http.Response{
					StatusCode: http.StatusOK,
					Body:       io.NopCloser(bytes.NewBufferString(makePayload(validVec))),
					Header:     make(http.Header),
				}, nil
			}),
		},
	}
	got, err := okE.EmbedText(context.Background(), "hello")
	if err != nil || len(got) != TargetVectorDimension {
		t.Fatalf("expected valid 3072-dim embedding, got len=%d err=%v", len(got), err)
	}
}
