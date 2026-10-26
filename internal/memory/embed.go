// Copyright 2026 Christian Haeussinger. Apache-2.0.

package memory

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
)

// vertexEmbeddingModel is the Vertex AI model used for RAG embeddings.
const vertexEmbeddingModel = "gemini-embedding-001"


// vertexMaxBatchSize is the maximum number of texts per predict call.
const vertexMaxBatchSize = 100

// VertexEmbedder generates text embeddings by calling the Vertex AI
// REST API directly (no client library). It authenticates via
// Application Default Credentials (ADC).
type VertexEmbedder struct {
	projectID string
	region    string
	client    *http.Client
}

// NewVertexEmbedder creates an embedder targeting the given GCP project
// and region. It obtains an ADC-authenticated HTTP client for the
// cloud-platform scope. Returns an error if ADC cannot be found (the
// caller should degrade gracefully).
func NewVertexEmbedder(projectID, region string) (*VertexEmbedder, error) {
	ctx := context.Background()
	creds, err := google.FindDefaultCredentials(ctx, "https://www.googleapis.com/auth/cloud-platform")
	if err != nil {
		return nil, fmt.Errorf("memory: vertex embedder: ADC: %w", err)
	}
	client := &http.Client{
		Transport: &oauth2.Transport{
			Source: creds.TokenSource,
			Base:   http.DefaultTransport,
		},
	}
	return &VertexEmbedder{
		projectID: projectID,
		region:    region,
		client:    client,
	}, nil
}

func (e *VertexEmbedder) endpoint() string {
	return fmt.Sprintf(
		"https://%s-aiplatform.googleapis.com/v1/projects/%s/locations/%s/publishers/google/models/%s:predict",
		e.region, e.projectID, e.region, vertexEmbeddingModel,
	)
}

// EmbedText embeds a single text string.
func (e *VertexEmbedder) EmbedText(ctx context.Context, text string) ([]float32, error) {
	batched, err := e.EmbedBatch(ctx, []string{text})
	if err != nil {
		return nil, err
	}
	if len(batched) == 0 {
		return nil, fmt.Errorf("memory: embedder returned no results")
	}
	return batched[0], nil
}

// EmbedBatch embeds up to len(texts) strings, automatically splitting
// into sub-batches of vertexMaxBatchSize. Returns one embedding per
// input text in the same order.
func (e *VertexEmbedder) EmbedBatch(ctx context.Context, texts []string) ([][]float32, error) {
	if len(texts) == 0 {
		return nil, nil
	}
	all := make([][]float32, 0, len(texts))
	for start := 0; start < len(texts); start += vertexMaxBatchSize {
		end := start + vertexMaxBatchSize
		if end > len(texts) {
			end = len(texts)
		}
		batch, err := e.callPredict(ctx, texts[start:end])
		if err != nil {
			return nil, err
		}
		all = append(all, batch...)
	}
	return all, nil
}

// --- Vertex AI REST types --------------------------------------------

type vertexPredictRequest struct {
	Instances []vertexInstance `json:"instances"`
}

type vertexInstance struct {
	Content string `json:"content"`
}

type vertexPredictResponse struct {
	Predictions []vertexPrediction `json:"predictions"`
}

type vertexPrediction struct {
	Embeddings vertexEmbeddings `json:"embeddings"`
}

type vertexEmbeddings struct {
	Values []float32 `json:"values"`
}

// callPredict sends one batch of texts to the Vertex AI predict
// endpoint and returns the resulting embeddings.
func (e *VertexEmbedder) callPredict(ctx context.Context, texts []string) ([][]float32, error) {
	instances := make([]vertexInstance, len(texts))
	for i, t := range texts {
		instances[i] = vertexInstance{Content: t}
	}
	body, err := json.Marshal(vertexPredictRequest{Instances: instances})
	if err != nil {
		return nil, fmt.Errorf("memory: embedder: marshal: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.endpoint(), bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("memory: embedder: new request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := e.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("memory: embedder: do: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return nil, fmt.Errorf("memory: embedder: HTTP %d: %s", resp.StatusCode, string(respBody))
	}
	var result vertexPredictResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("memory: embedder: decode: %w", err)
	}
	out := make([][]float32, len(result.Predictions))
	for i, p := range result.Predictions {
		out[i] = p.Embeddings.Values
	}
	return out, nil
}
