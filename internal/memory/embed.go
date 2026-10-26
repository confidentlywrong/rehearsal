// Copyright 2026 Christian Haeussinger. Apache-2.0.

package memory

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/localai"
	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
)

// TargetVectorDimension is the pgvector column width in document_chunks (VECTOR(3072)).
const TargetVectorDimension = 3072

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

func hasValidEmbeddingNorm(vec []float32, targetDim int) bool {
	if len(vec) == 0 || targetDim <= 0 {
		return false
	}
	limit := len(vec)
	if limit > targetDim {
		limit = targetDim
	}
	var sumSq float64
	for i := 0; i < limit; i++ {
		f := float64(vec[i])
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return false
		}
		sumSq += f * f
	}
	norm := math.Sqrt(sumSq)
	return norm > 1e-12 && !math.IsNaN(norm) && !math.IsInf(norm, 0)
}

// NormalizeAndPadVector L2-normalizes vec and zero-pads (or truncates) it to targetDim.
// Zero-padding an L2-normalized 768-d embeddinggemma vector to 3072 dimensions preserves
// exact cosine similarity in pgvector's VECTOR(3072) column without schema migration.
func NormalizeAndPadVector(vec []float32, targetDim int) []float32 {
	if targetDim <= 0 {
		return nil
	}
	out := make([]float32, targetDim)
	if len(vec) == 0 {
		return out
	}
	limit := len(vec)
	if limit > targetDim {
		limit = targetDim
	}
	var sumSq float64
	for i := 0; i < limit; i++ {
		f := float64(vec[i])
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return out
		}
		sumSq += f * f
	}
	norm := float32(math.Sqrt(sumSq))
	if norm > 1e-12 && !math.IsNaN(float64(norm)) && !math.IsInf(float64(norm), 0) {
		for i := 0; i < limit; i++ {
			out[i] = vec[i] / norm
		}
	}
	return out
}

// LocalGemmaEmbedder generates text embeddings by calling the local Ollama
// /api/embed endpoint (defaulting to Google's embeddinggemma:latest model)
// and zero-padding the L2-normalized vector to TargetVectorDimension (3072).
type LocalGemmaEmbedder struct {
	endpointProvider func() (endpoint string, model string)
	client           *http.Client
}

// NewLocalGemmaEmbedder creates an embedder that reads the active Ollama endpoint
// and embedding model dynamically via endpointProvider.
func NewLocalGemmaEmbedder(endpointProvider func() (string, string)) *LocalGemmaEmbedder {
	return &LocalGemmaEmbedder{
		endpointProvider: endpointProvider,
		client:           &http.Client{Timeout: 60 * time.Second},
	}
}

type ollamaEmbedRequest struct {
	Model string   `json:"model"`
	Input []string `json:"input"`
}

type ollamaEmbedResponse struct {
	Embeddings [][]float32 `json:"embeddings"`
}

type ollamaLegacyEmbedRequest struct {
	Model  string `json:"model"`
	Prompt string `json:"prompt"`
}

type ollamaLegacyEmbedResponse struct {
	Embedding []float32 `json:"embedding"`
}

// EmbedText embeds a single text string using the local Google embedding engine.
func (e *LocalGemmaEmbedder) EmbedText(ctx context.Context, text string) ([]float32, error) {
	batched, err := e.EmbedBatch(ctx, []string{text})
	if err != nil {
		return nil, err
	}
	if len(batched) == 0 {
		return nil, fmt.Errorf("memory: local embedder returned no results")
	}
	return batched[0], nil
}

// EmbedBatch embeds multiple texts using Ollama's /api/embed batch endpoint,
// falling back to /api/embeddings if needed, and normalizes/pads each vector to 3072.
func (e *LocalGemmaEmbedder) EmbedBatch(ctx context.Context, texts []string) ([][]float32, error) {
	if len(texts) == 0 {
		return nil, nil
	}
	endpoint, model := localai.DefaultEndpoint, localai.DefaultEmbedModel
	if e.endpointProvider != nil {
		ep, m := e.endpointProvider()
		if strings.TrimSpace(ep) != "" {
			endpoint = strings.TrimSpace(ep)
		}
		if strings.TrimSpace(m) != "" {
			model = strings.TrimSpace(m)
		}
	}
	cleanEndpoint, err := localai.ValidateLocalEndpoint(endpoint)
	if err != nil {
		return nil, fmt.Errorf("memory: local embedder invalid endpoint: %w", err)
	}
	fetchEndpoint := localai.ResolveIPv4Endpoint(cleanEndpoint)
	cleanModel := localai.StripModelPrefix(model)
	if cleanModel == "" {
		cleanModel = localai.DefaultEmbedModel
	}

	var allEmbeddings [][]float32
	for start := 0; start < len(texts); start += vertexMaxBatchSize {
		end := start + vertexMaxBatchSize
		if end > len(texts) {
			end = len(texts)
		}
		batch, err := e.embedBatchChunk(ctx, fetchEndpoint, cleanModel, texts[start:end])
		if err != nil {
			return nil, err
		}
		allEmbeddings = append(allEmbeddings, batch...)
	}
	return allEmbeddings, nil
}

func (e *LocalGemmaEmbedder) embedBatchChunk(ctx context.Context, endpoint, model string, texts []string) ([][]float32, error) {
	reqBody, err := json.Marshal(ollamaEmbedRequest{
		Model: model,
		Input: texts,
	})
	if err != nil {
		return nil, fmt.Errorf("memory: local embedder: marshal: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint+"/api/embed", bytes.NewReader(reqBody))
	if err != nil {
		return nil, fmt.Errorf("memory: local embedder: request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := e.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("memory: local embedder (%s): %w", endpoint, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		// Fallback to legacy /api/embeddings for older Ollama daemons
		return e.embedLegacy(ctx, endpoint, model, texts)
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return nil, fmt.Errorf("memory: local embedder HTTP %d: %s", resp.StatusCode, string(body))
	}

	var parsed ollamaEmbedResponse
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, fmt.Errorf("memory: local embedder decode: %w", err)
	}
	if len(parsed.Embeddings) != len(texts) {
		return nil, fmt.Errorf("memory: local embedder returned %d embeddings for %d texts", len(parsed.Embeddings), len(texts))
	}

	out := make([][]float32, len(parsed.Embeddings))
	for i, raw := range parsed.Embeddings {
		if !hasValidEmbeddingNorm(raw, TargetVectorDimension) {
			return nil, fmt.Errorf("memory: local embedder returned empty or zero-norm embedding vector at index %d", i)
		}
		out[i] = NormalizeAndPadVector(raw, TargetVectorDimension)
	}
	return out, nil
}

func (e *LocalGemmaEmbedder) embedLegacy(ctx context.Context, endpoint, model string, texts []string) ([][]float32, error) {
	out := make([][]float32, len(texts))
	for i, t := range texts {
		body, err := json.Marshal(ollamaLegacyEmbedRequest{Model: model, Prompt: t})
		if err != nil {
			return nil, err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint+"/api/embeddings", bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := e.client.Do(req)
		if err != nil {
			return nil, err
		}
		if resp.StatusCode != http.StatusOK {
			b, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
			resp.Body.Close()
			return nil, fmt.Errorf("memory: local legacy embedder HTTP %d: %s", resp.StatusCode, string(b))
		}
		var parsed ollamaLegacyEmbedResponse
		err = json.NewDecoder(resp.Body).Decode(&parsed)
		resp.Body.Close()
		if err != nil {
			return nil, err
		}
		if !hasValidEmbeddingNorm(parsed.Embedding, TargetVectorDimension) {
			return nil, fmt.Errorf("memory: local legacy embedder returned empty or zero-norm embedding vector at index %d", i)
		}
		out[i] = NormalizeAndPadVector(parsed.Embedding, TargetVectorDimension)
	}
	return out, nil
}

// DynamicEmbedder routes EmbedText and EmbedBatch calls to either LocalGemmaEmbedder
// (when local_gemma is selected in localai.Manager) or VertexEmbedder (when cloud_gemini is active).
type DynamicEmbedder struct {
	aiManager *localai.Manager
	vertex    *VertexEmbedder
	local     *LocalGemmaEmbedder
}

// NewDynamicEmbedder creates an Embedder that switches between Cloud Vertex AI and
// Local Google embeddinggemma at runtime based on the user's AI Engine settings.
func NewDynamicEmbedder(aiManager *localai.Manager, vertex *VertexEmbedder) *DynamicEmbedder {
	local := NewLocalGemmaEmbedder(func() (string, string) {
		if aiManager == nil {
			return localai.DefaultEndpoint, localai.DefaultEmbedModel
		}
		cfg := aiManager.GetConfig()
		return cfg.Endpoint, cfg.EmbedModel
	})
	return &DynamicEmbedder{
		aiManager: aiManager,
		vertex:    vertex,
		local:     local,
	}
}

func (d *DynamicEmbedder) useLocal() bool {
	if d.aiManager != nil {
		return d.aiManager.GetConfig().Provider == localai.ProviderLocalGemma
	}
	return d.vertex == nil
}

// EmbedText delegates to either LocalGemmaEmbedder or VertexEmbedder.
func (d *DynamicEmbedder) EmbedText(ctx context.Context, text string) ([]float32, error) {
	if d.useLocal() {
		return d.local.EmbedText(ctx, text)
	}
	if d.vertex == nil {
		return nil, fmt.Errorf("memory: cloud_gemini embedding requires GOOGLE_CLOUD_PROJECT (or switch AI Engine to Local Google Gemma)")
	}
	return d.vertex.EmbedText(ctx, text)
}

// EmbedBatch delegates to either LocalGemmaEmbedder or VertexEmbedder.
func (d *DynamicEmbedder) EmbedBatch(ctx context.Context, texts []string) ([][]float32, error) {
	if d.useLocal() {
		return d.local.EmbedBatch(ctx, texts)
	}
	if d.vertex == nil {
		return nil, fmt.Errorf("memory: cloud_gemini embedding requires GOOGLE_CLOUD_PROJECT (or switch AI Engine to Local Google Gemma)")
	}
	return d.vertex.EmbedBatch(ctx, texts)
}
