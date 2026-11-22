// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/auth"
	"github.com/hashicorp/go-retryablehttp"
	"golang.org/x/oauth2/google"
)

// AgentCard is a transparent wrapper around the A2A agent card JSON.
// The raw JSON is preserved so the gateway can proxy all fields losslessly.
// Only the fields the gateway needs are extracted into typed accessors.
type AgentCard struct {
	// Raw stores the complete, unmodified JSON from the agent.
	// MarshalJSON returns this directly, so /api/v1/agent-types
	// serves exactly what the agents provide.
	Raw json.RawMessage `json:"-"`

	// Extracted fields — used internally by the gateway.
	Name         string                 `json:"-"`
	URL          string                 `json:"-"`
	BaseURL      string                 `json:"-"` // Discovery base URL for orchestration pokes
	Version      string                 `json:"-"`
	Description  string                 `json:"-"`
	Capabilities map[string]interface{} `json:"-"`
}

// MarshalJSON returns the raw JSON unchanged, making the gateway a transparent proxy.
func (c AgentCard) MarshalJSON() ([]byte, error) {
	if c.Raw != nil {
		return []byte(c.Raw), nil
	}
	// Fallback for programmatically constructed cards (tests)
	return json.Marshal(struct {
		Name         string                 `json:"name"`
		Description  string                 `json:"description,omitempty"`
		Version      string                 `json:"version,omitempty"`
		URL          string                 `json:"url,omitempty"`
		Capabilities map[string]interface{} `json:"capabilities,omitempty"`
	}{
		Name:         c.Name,
		Description:  c.Description,
		Version:      c.Version,
		URL:          c.URL,
		Capabilities: c.Capabilities,
	})
}

// UnmarshalJSON stores the raw bytes and extracts only the fields the gateway needs.
func (c *AgentCard) UnmarshalJSON(data []byte) error {
	c.Raw = append(json.RawMessage{}, data...) // copy

	var extracted struct {
		Name         string                 `json:"name"`
		Description  string                 `json:"description"`
		Version      string                 `json:"version"`
		URL          string                 `json:"url"`
		Capabilities map[string]interface{} `json:"capabilities"`
	}
	if err := json.Unmarshal(data, &extracted); err != nil {
		return err
	}
	c.Name = extracted.Name
	c.Description = extracted.Description
	c.Version = extracted.Version
	c.URL = extracted.URL
	c.Capabilities = extracted.Capabilities
	return nil
}

// SetURL updates the URL in both the extracted field and the raw JSON.
func (c *AgentCard) SetURL(rawURL string) {
	c.URL = rawURL
	if len(c.Raw) == 0 {
		return
	}
	// Update the raw JSON too so it's consistent when proxied.
	var m map[string]json.RawMessage
	if err := json.Unmarshal(c.Raw, &m); err != nil {
		log.Printf("AgentCard.SetURL: WARNING — failed to unmarshal raw card JSON: %v", err)
		return
	}
	if m == nil {
		m = make(map[string]json.RawMessage)
	}
	encodedURL, err := json.Marshal(rawURL)
	if err != nil {
		log.Printf("AgentCard.SetURL: WARNING — failed to marshal URL %q: %v", rawURL, err)
		return
	}
	m["url"] = encodedURL
	updated, err := json.Marshal(m)
	if err != nil {
		log.Printf("AgentCard.SetURL: WARNING — failed to marshal updated card JSON: %v", err)
		return
	}
	c.Raw = updated
}

// OrchestrationBaseURL returns the base URL for orchestration pokes.
// Falls back to URL if BaseURL is not set.
func (c AgentCard) OrchestrationBaseURL() string {
	if c.BaseURL != "" {
		return c.BaseURL
	}
	return c.URL
}

// DispatchMode returns the dispatch mode from the n26:dispatch/1.0 extension.
// Returns "subscriber" if not specified.
func (c AgentCard) DispatchMode() string {
	exts, ok := c.Capabilities["extensions"].([]interface{})
	if !ok {
		return "subscriber"
	}
	for _, ext := range exts {
		m, ok := ext.(map[string]interface{})
		if !ok {
			continue
		}
		if m["uri"] == "n26:dispatch/1.0" {
			params, ok := m["params"].(map[string]interface{})
			if ok {
				if mode, ok := params["mode"].(string); ok {
					return mode
				}
			}
		}
	}
	return "subscriber"
}

// defaultCacheTTL is the default duration for which a complete discovery
// (all configured agent URLs returned a valid card) is cached. 30 seconds
// balances freshness against the cost of fetching all agent cards.
const defaultCacheTTL = 30 * time.Second

// partialCacheTTL is the duration for which a partial discovery (one or more
// configured URLs failed) is cached. It is intentionally short so that an
// agent which lost the boot race becomes spawnable within a few seconds
// rather than after a full defaultCacheTTL window.
const partialCacheTTL = 2 * time.Second

// Catalog discovers agents by fetching their A2A AgentCards over HTTP.
// Results are cached for cacheTTL to avoid redundant network round-trips.
type Catalog struct {
	agentURLs []string
	client    *http.Client
	gcpClient *http.Client // Authenticated client for Agent Engine URLs

	// Cache fields — protected by cacheMu
	fetchMu      sync.Mutex
	cacheMu      sync.RWMutex
	cachedAgents map[string]AgentCard
	cacheExpiry  time.Time
	cacheTTL     time.Duration

	oidcMu      sync.RWMutex
	oidcClients map[string]*http.Client
	oidcFactory func(audience string) (*http.Client, error)
}

// newRetryableClient wraps an HTTP client with retry logic
// (5 retries, exponential backoff 500ms–5s). If httpClient is nil,
// a default client is used. This ensures both standard and AE agents
// get identical retry behavior.
func newRetryableClient(httpClient *http.Client) *http.Client {
	rc := retryablehttp.NewClient()
	if httpClient != nil {
		rc.HTTPClient = httpClient
	}
	rc.RetryMax = 5
	rc.RetryWaitMin = 500 * time.Millisecond
	rc.RetryWaitMax = 5 * time.Second
	rc.Logger = nil // Suppress default retry logs
	return rc.StandardClient()
}

// NewCatalogWithTTL creates a catalog with a custom cache TTL.
// Use this in tests to control cache behavior.
func NewCatalogWithTTL(agentURLs []string, ttl time.Duration) *Catalog {
	c := NewCatalog(agentURLs)
	c.cacheTTL = ttl
	return c
}

// NewCatalog creates a catalog that discovers agents from the given base URLs.
// Each URL should be the agent's base URL (e.g., http://127.0.0.1:9104).
// For standard A2A agents, appends /.well-known/agent-card.json.
// For Agent Engine agents, appends /a2a/v1/card and uses GCP auth.
func NewCatalog(agentURLs []string) *Catalog {
	// Create GCP-authenticated client for Agent Engine calls.
	gcpClient, err := google.DefaultClient(context.Background(),
		"https://www.googleapis.com/auth/cloud-platform",
	)
	if err != nil {
		log.Printf("Catalog: WARNING — failed to create GCP auth client: %v (AE card discovery will fail)", err)
	}

	var gcpRetryClient *http.Client
	if gcpClient != nil {
		gcpClient.Timeout = 10 * time.Second
		// Wrap the GCP client in retryablehttp for AE resilience.
		// The OAuth2 transport is preserved — retries happen around
		// HTTPClient.Do(), so each attempt includes fresh auth headers.
		gcpRetryClient = newRetryableClient(gcpClient)
	}

	return &Catalog{
		agentURLs:   agentURLs,
		client:      newRetryableClient(nil),
		gcpClient:   gcpRetryClient,
		cacheTTL:    defaultCacheTTL,
		oidcClients: make(map[string]*http.Client),
	}
}

// wellKnownPath is the A2A standard path for agent card discovery.
const wellKnownPath = "/.well-known/agent-card.json"

// aeCardPath is the Agent Engine card endpoint (AE doesn't serve well-known).
const aeCardPath = "/a2a/v1/card"

// IsAgentEngineURL returns true if the URL points to a Vertex AI Agent Engine
// resource (contains aiplatform.googleapis.com and reasoningEngines).
func IsAgentEngineURL(u string) bool {
	return strings.Contains(u, "aiplatform.googleapis.com") &&
		strings.Contains(u, "reasoningEngines")
}

// discoverResult holds the outcome of a single agent card fetch.
type discoverResult struct {
	card    AgentCard
	baseURL string
	err     error
}

// DiscoverAgents fetches AgentCards from each configured agent URL concurrently.
// Results are cached for cacheTTL. Unreachable agents are logged and skipped
// (not fatal unless none are found).
func (c *Catalog) DiscoverAgents() (map[string]AgentCard, error) {
	return c.DiscoverAgentsContext(context.Background())
}

// DiscoverAgentsContext fetches AgentCards from each configured agent URL concurrently,
// respecting ctx for cancellation and deadlines. Returns a defensive copy of cached results.
func (c *Catalog) DiscoverAgentsContext(ctx context.Context) (map[string]AgentCard, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if len(c.agentURLs) == 0 {
		return nil, fmt.Errorf("no agent URLs configured (check AGENT_URLS env var)")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	// Fast path: return cached results if still valid (read lock).
	c.cacheMu.RLock()
	if c.cachedAgents != nil && time.Now().Before(c.cacheExpiry) {
		agents := cloneAgentMap(c.cachedAgents)
		c.cacheMu.RUnlock()
		return agents, nil
	}
	c.cacheMu.RUnlock()

	// Slow path: acquire fetch lock to serialize HTTP requests.
	// This prevents thundering-herd: only one goroutine fetches,
	// others block on Lock() and get the cached result on double-check.
	c.fetchMu.Lock()
	defer c.fetchMu.Unlock()

	if err := ctx.Err(); err != nil {
		return nil, err
	}

	// Double-check: another goroutine may have refreshed while we waited.
	c.cacheMu.RLock()
	if c.cachedAgents != nil && time.Now().Before(c.cacheExpiry) {
		agents := cloneAgentMap(c.cachedAgents)
		c.cacheMu.RUnlock()
		return agents, nil
	}
	c.cacheMu.RUnlock()

	newAgents, err := c.fetchAgents(ctx)
	if err != nil {
		return nil, err
	}

	// Use a short TTL when discovery is partial so that an agent which lost
	// the boot race becomes spawnable within a few seconds rather than after
	// a full defaultCacheTTL window. A complete discovery uses the full TTL.
	ttl := c.cacheTTL
	if len(newAgents) < len(c.agentURLs) {
		if partialCacheTTL < ttl {
			ttl = partialCacheTTL
		}
		log.Printf("Catalog: partial discovery — %d/%d agents reachable, caching for %s",
			len(newAgents), len(c.agentURLs), ttl)
	}

	c.cacheMu.Lock()
	c.cachedAgents = newAgents
	c.cacheExpiry = time.Now().Add(ttl)
	c.cacheMu.Unlock()

	return cloneAgentMap(newAgents), nil
}

func cloneAgentMap(src map[string]AgentCard) map[string]AgentCard {
	if src == nil {
		return nil
	}
	dst := make(map[string]AgentCard, len(src))
	for k, v := range src {
		if v.Raw != nil {
			v.Raw = append(json.RawMessage(nil), v.Raw...)
		}
		if v.Capabilities != nil {
			v.Capabilities = cloneCapabilitiesMap(v.Capabilities)
		}
		dst[k] = v
	}
	return dst
}

func cloneCapabilitiesMap(src map[string]interface{}) map[string]interface{} {
	if src == nil {
		return nil
	}
	dst := make(map[string]interface{}, len(src))
	for k, v := range src {
		dst[k] = cloneCapabilityValue(v)
	}
	return dst
}

func cloneCapabilityValue(v interface{}) interface{} {
	switch typed := v.(type) {
	case map[string]interface{}:
		return cloneCapabilitiesMap(typed)
	case []interface{}:
		if typed == nil {
			return ([]interface{})(nil)
		}
		out := make([]interface{}, len(typed))
		for i, elem := range typed {
			out[i] = cloneCapabilityValue(elem)
		}
		return out
	case map[string]string:
		if typed == nil {
			return (map[string]string)(nil)
		}
		out := make(map[string]string, len(typed))
		for k, val := range typed {
			out[k] = val
		}
		return out
	case []string:
		if typed == nil {
			return ([]string)(nil)
		}
		out := make([]string, len(typed))
		copy(out, typed)
		return out
	case []map[string]interface{}:
		if typed == nil {
			return ([]map[string]interface{})(nil)
		}
		out := make([]map[string]interface{}, len(typed))
		for i, elem := range typed {
			out[i] = cloneCapabilitiesMap(elem)
		}
		return out
	case []byte:
		if typed == nil {
			return ([]byte)(nil)
		}
		out := make([]byte, len(typed))
		copy(out, typed)
		return out
	default:
		return v
	}
}

func (c *Catalog) selectHTTPClient(baseURL string, isAE bool) (*http.Client, error) {
	if isAE {
		if c.gcpClient == nil {
			return nil, fmt.Errorf("GCP auth client unavailable for Agent Engine URL %s", baseURL)
		}
		return c.gcpClient, nil
	}
	if auth.IsCloudRunURL(baseURL) {
		c.oidcMu.RLock()
		var cachedOIDC *http.Client
		if c.oidcClients != nil {
			cachedOIDC = c.oidcClients[baseURL]
		}
		c.oidcMu.RUnlock()
		if cachedOIDC != nil {
			return cachedOIDC, nil
		}

		c.oidcMu.Lock()
		defer c.oidcMu.Unlock()
		if c.oidcClients == nil {
			c.oidcClients = make(map[string]*http.Client)
		}
		if cachedOIDC = c.oidcClients[baseURL]; cachedOIDC != nil {
			return cachedOIDC, nil
		}
		oidcFn := c.oidcFactory
		if oidcFn == nil {
			oidcFn = auth.OIDCClientE
		}
		oidcClient, err := oidcFn(auth.AudienceFor(baseURL))
		if err != nil {
			return nil, fmt.Errorf("OIDC auth client unavailable for Cloud Run URL %s: %w", baseURL, err)
		}
		if oidcClient == nil {
			return nil, fmt.Errorf("OIDC auth client unavailable for Cloud Run URL %s", baseURL)
		}
		wrapped := newRetryableClient(oidcClient)
		c.oidcClients[baseURL] = wrapped
		return wrapped, nil
	}
	if c.client == nil {
		return nil, fmt.Errorf("HTTP client unavailable for URL %s", baseURL)
	}
	return c.client, nil
}

// resolveAgentCardURL preserves the card's A2A path (e.g. /a2a/simulator/) while
// replacing the host with the internal discovery baseURL and avoiding double-pathing
// when baseURL already ends with that path.
func resolveAgentCardURL(baseURL, cardURL string) string {
	baseTrimmed := strings.TrimRight(baseURL, "/")
	if cardURL == "" {
		return baseTrimmed
	}
	origURL, err := url.Parse(cardURL)
	if err != nil || origURL.Path == "" || origURL.Path == "/" {
		return baseTrimmed
	}
	baseParsed, baseErr := url.Parse(baseTrimmed)
	if baseErr == nil {
		basePathTrimmed := strings.TrimRight(baseParsed.Path, "/")
		origPathTrimmed := strings.TrimRight(origURL.Path, "/")
		if basePathTrimmed != "" && (basePathTrimmed == origPathTrimmed || strings.HasSuffix(basePathTrimmed, origPathTrimmed)) {
			prefix := strings.TrimSuffix(baseTrimmed, origPathTrimmed)
			return prefix + origURL.Path
		}
	}
	return baseTrimmed + origURL.Path
}

// fetchAgents performs the actual HTTP discovery.
func (c *Catalog) fetchAgents(ctx context.Context) (map[string]AgentCard, error) {
	results := make(chan discoverResult, len(c.agentURLs))
	var wg sync.WaitGroup

	for _, rawURL := range c.agentURLs {
		wg.Add(1)
		go func(baseURL string) {
			defer wg.Done()
			baseURL = strings.TrimRight(baseURL, "/")

			// Agent Engine uses /a2a/v1/card; standard A2A uses /.well-known/agent-card.json.
			cardPath := wellKnownPath
			isAE := IsAgentEngineURL(baseURL)
			if isAE {
				cardPath = aeCardPath
			}
			cardURL := baseURL + cardPath

			// Auth selection per URL kind (fail-closed — no unauthenticated fallback):
			//  - Agent Engine: OAuth bearer (cloud-platform scope) via c.gcpClient.
			//  - Cloud Run (any other https://): OIDC ID token with audience =
			//    service URL. Cloud Run --no-allow-unauthenticated services
			//    return 403 to anything else, including OAuth bearers.
			//  - localhost / http://: plain client (local dev, no auth).
			httpClient, err := c.selectHTTPClient(baseURL, isAE)
			if err != nil {
				results <- discoverResult{baseURL: baseURL, err: err}
				return
			}

			card, err := c.fetchCard(ctx, cardURL, httpClient)
			if err != nil {
				results <- discoverResult{baseURL: baseURL, err: err}
				return
			}

			// Store the discovery base URL for orchestration routing.
			// The gateway's AGENT_URLS contains the internal/private URLs
			// for service-to-service communication.
			card.BaseURL = baseURL

			if isAE {
				// AE agents: always use the discovery base URL as-is.
				// The switchboard appends /a2a/v1/message:send for dispatches.
				card.SetURL(baseURL)
			} else {
				// Local/Cloud Run: preserve the card's A2A path (e.g.,
				// /a2a/simulator/) without double-pathing, replacing the
				// host with the internal discovery URL.
				card.SetURL(resolveAgentCardURL(baseURL, card.URL))
			}

			results <- discoverResult{card: card, baseURL: baseURL}
		}(rawURL)
	}

	// Close results channel after all goroutines complete.
	go func() {
		wg.Wait()
		close(results)
	}()

	agents := make(map[string]AgentCard)
	var lastErr error

	for r := range results {
		if r.err != nil {
			log.Printf("Catalog: skipping agent at %s — DISCOVERY_FAILURE: %v", r.baseURL, r.err)
			lastErr = r.err
			continue
		}
		if r.card.Name == "" {
			err := fmt.Errorf("agent card at %s has no name", r.baseURL)
			log.Printf("Catalog: skipping agent at %s — DISCOVERY_FAILURE: %v", r.baseURL, err)
			lastErr = err
			continue
		}
		// Concurrent discovery means duplicate names resolve nondeterministically
		// (unlike the sequential version which was last-wins-in-URL-order).
		// Log a warning so operators notice the config error.
		if existing, ok := agents[r.card.Name]; ok {
			log.Printf("Catalog: WARNING — duplicate agent name %q from %s (previously from %s)",
				r.card.Name, r.baseURL, existing.URL)
		}
		agents[r.card.Name] = r.card
		log.Printf("Catalog: Discovered agent %q from %s (dispatch=%s)",
			r.card.Name, r.baseURL, r.card.DispatchMode())
	}

	if len(agents) == 0 {
		if lastErr != nil {
			return nil, fmt.Errorf("failed to discover any agents: %w", lastErr)
		}
		return nil, fmt.Errorf("failed to discover any agents (all URLs reachable but returned no cards)")
	}

	return agents, nil
}

// perFetchTimeout caps a single agent-card fetch. Without this, a cold
// or unresponsive agent service can keep retryablehttp (5 retries with
// exponential backoff up to 5s) hanging for 25s+ per URL, which fans
// out across the discovery batch and blows past the BFF's proxy
// timeout (B-18, 2026-06-23 audit). 15s is generous enough to absorb
// a cold-started Cloud Run instance while still bounding the worst
// case at well under the BFF's 90s window.
const perFetchTimeout = 15 * time.Second

// fetchCard fetches and parses an AgentCard from the given URL using the provided HTTP client.
func (c *Catalog) fetchCard(ctx context.Context, url string, httpClient *http.Client) (AgentCard, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	fetchCtx, cancel := context.WithTimeout(ctx, perFetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(fetchCtx, http.MethodGet, url, nil)
	if err != nil {
		return AgentCard{}, fmt.Errorf("build request failed: %w", err)
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return AgentCard{}, fmt.Errorf("fetch failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// Read up to 512 bytes of the response body for error diagnostics.
		// This helps operators debug AE set_up() failures, auth issues, etc.
		errBody, readErr := io.ReadAll(io.LimitReader(resp.Body, 512))
		if readErr != nil {
			return AgentCard{}, fmt.Errorf("unexpected status %d (failed reading error body: %w)", resp.StatusCode, readErr)
		}
		if len(errBody) > 0 {
			return AgentCard{}, fmt.Errorf("unexpected status %d: %s", resp.StatusCode, string(errBody))
		}
		return AgentCard{}, fmt.Errorf("unexpected status %d", resp.StatusCode)
	}

	const maxAgentCardBytes = 4 << 20
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxAgentCardBytes+1))
	if err != nil {
		return AgentCard{}, fmt.Errorf("read body failed: %w", err)
	}
	if len(body) > maxAgentCardBytes {
		return AgentCard{}, fmt.Errorf("agent card exceeds %d byte limit", maxAgentCardBytes)
	}

	// Validate Content-Type before attempting JSON parse.
	// Catches HTML error pages returned with 200 status (e.g., reverse proxy fallbacks).
	ct := resp.Header.Get("Content-Type")
	if ct != "" && !strings.Contains(strings.ToLower(ct), "json") {
		return AgentCard{}, fmt.Errorf("unexpected Content-Type %q (expected application/json)", ct)
	}

	var card AgentCard
	if err := json.Unmarshal(body, &card); err != nil {
		return AgentCard{}, fmt.Errorf("invalid JSON: %w", err)
	}

	return card, nil
}
