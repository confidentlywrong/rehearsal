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
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

type errReader struct{}

func (errReader) Read([]byte) (int, error) { return 0, errors.New("boom read error") }
func (errReader) Close() error             { return nil }

func TestR4_CatalogRejectsAgentEngineWhenGCPClientNil(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"name":"ae_sim"}`))
	}))
	defer srv.Close()

	aeURL := srv.URL + "/aiplatform.googleapis.com/reasoningEngines/123"
	cat := &Catalog{
		agentURLs: []string{aeURL},
		client:    newRetryableClient(nil),
		gcpClient: nil,
	}

	_, err := cat.DiscoverAgents()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "GCP auth client unavailable")
	assert.Equal(t, int32(0), hits.Load(), "must never send unauthenticated request to AE URL when gcpClient is nil")
}

func TestR4_CatalogRejectsCloudRunWhenOIDCClientUnavailable(t *testing.T) {
	var fallbackHits atomic.Int32
	cat := &Catalog{
		agentURLs: []string{"https://runner-abc.us-central1.run.app"},
		client: newRetryableClient(&http.Client{
			Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				fallbackHits.Add(1)
				return nil, errors.New("unexpected unauthenticated fallback")
			}),
		}),
		oidcFactory: func(audience string) (*http.Client, error) {
			return nil, errors.New("no ADC available")
		},
	}

	_, err := cat.DiscoverAgents()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "OIDC auth client unavailable for Cloud Run URL")
	assert.Contains(t, err.Error(), "no ADC available")
	assert.Equal(t, int32(0), fallbackHits.Load(), "must never fall back to unauthenticated client on Cloud Run OIDC failure")
}

func TestR4_CatalogCloudRunOIDCInitLazyMapAndDoubleCheckedLocking(t *testing.T) {
	var factoryCalls atomic.Int32
	cloudRunURL := "https://sim-xyz.us-central1.run.app"

	cat := &Catalog{
		agentURLs:   []string{cloudRunURL, cloudRunURL},
		client:      newRetryableClient(nil),
		oidcClients: nil, // intentionally nil to verify lazy initialization
		oidcFactory: func(audience string) (*http.Client, error) {
			factoryCalls.Add(1)
			assert.Equal(t, cloudRunURL, audience)
			return &http.Client{
				Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
					return &http.Response{
						StatusCode: http.StatusOK,
						Header:     http.Header{"Content-Type": []string{"Application/JSON"}},
						Body:       io.NopCloser(strings.NewReader(`{"name":"cloud_sim","version":"1.0.0"}`)),
					}, nil
				}),
			}, nil
		},
	}

	agents, err := cat.DiscoverAgents()
	require.NoError(t, err)
	require.Contains(t, agents, "cloud_sim")
	assert.Equal(t, int32(1), factoryCalls.Load(), "duplicate Cloud Run URLs should reuse cached OIDC client")
}

func TestR4_DiscoverAgentsReturnsDefensiveCopy(t *testing.T) {
	card := AgentCard{Name: "copy_agent", Version: "1.0.0"}
	cardJSON, err := json.Marshal(card)
	require.NoError(t, err)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(cardJSON)
	}))
	defer srv.Close()

	cat := NewCatalog([]string{srv.URL})
	first, err := cat.DiscoverAgents()
	require.NoError(t, err)
	require.Contains(t, first, "copy_agent")

	// Mutate caller's map and Raw slice
	c := first["copy_agent"]
	c.Raw[0] = 'X'
	delete(first, "copy_agent")
	first["injected"] = AgentCard{Name: "injected"}

	second, err := cat.DiscoverAgents()
	require.NoError(t, err)
	assert.Contains(t, second, "copy_agent")
	assert.NotContains(t, second, "injected")
	assert.Equal(t, byte('{'), second["copy_agent"].Raw[0])

	// Exercise concurrent readers mutating returned maps under -race
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			m, err := cat.DiscoverAgents()
			if err == nil {
				m["extra"] = AgentCard{Name: "extra"}
				delete(m, "copy_agent")
			}
		}()
	}
	wg.Wait()
}

func TestR4_DiscoverAgentsContext_Cancellation(t *testing.T) {
	started := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-started:
		default:
			close(started)
		}
		select {
		case <-r.Context().Done():
		case <-time.After(5 * time.Second):
		}
	}))
	defer srv.Close()

	cat := NewCatalog([]string{srv.URL})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, err := cat.DiscoverAgentsContext(ctx)
	require.Error(t, err)
	assert.ErrorIs(t, err, context.DeadlineExceeded)
	assert.Less(t, time.Since(start), 2*time.Second)

	// Pre-cancelled context fails immediately before network call
	cancelledCtx, cancel2 := context.WithCancel(context.Background())
	cancel2()
	_, err = cat.DiscoverAgentsContext(cancelledCtx)
	require.ErrorIs(t, err, context.Canceled)
}

func TestR4_ResolveAgentCardURL_PreventsDoublePathing(t *testing.T) {
	tests := []struct {
		name     string
		baseURL  string
		cardURL  string
		expected string
	}{
		{
			name:     "root base preserves card path",
			baseURL:  "http://127.0.0.1:9104",
			cardURL:  "http://public.example.com/a2a/sim/",
			expected: "http://127.0.0.1:9104/a2a/sim/",
		},
		{
			name:     "base already ending in path without trailing slash",
			baseURL:  "http://host/a2a/sim",
			cardURL:  "http://public.example.com/a2a/sim/",
			expected: "http://host/a2a/sim/",
		},
		{
			name:     "base already ending in path with trailing slash",
			baseURL:  "http://host/a2a/sim/",
			cardURL:  "http://public.example.com/a2a/sim/",
			expected: "http://host/a2a/sim/",
		},
		{
			name:     "subpath base ending in card path",
			baseURL:  "http://host/sub/a2a/sim",
			cardURL:  "http://public.example.com/a2a/sim/",
			expected: "http://host/sub/a2a/sim/",
		},
		{
			name:     "segment boundary respected",
			baseURL:  "http://host/mya2a/sim",
			cardURL:  "http://public.example.com/a2a/sim/",
			expected: "http://host/mya2a/sim/a2a/sim/",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.expected, resolveAgentCardURL(tc.baseURL, tc.cardURL))
		})
	}

	// End-to-end discovery check with baseURL already containing A2A path
	card := AgentCard{
		Name:    "path_sim",
		Version: "1.0.0",
		URL:     "http://public.example.com/a2a/sim/",
	}
	cardJSON, err := json.Marshal(card)
	require.NoError(t, err)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/a2a/sim/.well-known/agent-card.json" {
			w.Header().Set("Content-Type", "Application/JSON")
			_, _ = w.Write(cardJSON)
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	cat := NewCatalog([]string{srv.URL + "/a2a/sim"})
	agents, err := cat.DiscoverAgents()
	require.NoError(t, err)
	require.Contains(t, agents, "path_sim")
	assert.Equal(t, srv.URL+"/a2a/sim/", agents["path_sim"].URL)
}

func TestR4_FetchCard_CaseInsensitiveContentTypeAndErrorRead(t *testing.T) {
	cat := NewCatalog(nil)
	client := &http.Client{
		Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
			switch req.URL.Path {
			case "/upper-ct":
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{"Content-Type": []string{"Application/JSON; charset=utf-8"}},
					Body:       io.NopCloser(strings.NewReader(`{"name":"upper_ct"}`)),
				}, nil
			case "/html-ct":
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{"Content-Type": []string{"text/html"}},
					Body:       io.NopCloser(strings.NewReader(`<html>oops</html>`)),
				}, nil
			case "/err-read":
				return &http.Response{
					StatusCode: http.StatusBadRequest,
					Body:       errReader{},
				}, nil
			default:
				return &http.Response{StatusCode: http.StatusNotFound, Body: io.NopCloser(strings.NewReader(""))}, nil
			}
		}),
	}

	card, err := cat.fetchCard(context.Background(), "http://example.invalid/upper-ct", client)
	require.NoError(t, err)
	assert.Equal(t, "upper_ct", card.Name)

	_, err = cat.fetchCard(context.Background(), "http://example.invalid/html-ct", client)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unexpected Content-Type")

	_, err = cat.fetchCard(context.Background(), "http://example.invalid/err-read", client)
	require.Error(t, err)
	assert.Contains(t, err.Error(), "failed reading error body")
	assert.Contains(t, err.Error(), "boom read error")
}

func TestR4_DiscoverAgents_UnnamedCardSetsLastErr(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"version":"1.0.0"}`))
	}))
	defer srv.Close()

	cat := NewCatalog([]string{srv.URL})
	_, err := cat.DiscoverAgents()
	require.Error(t, err)
	assert.Contains(t, err.Error(), "has no name")
}

func TestR4_AgentCardSetURL_LogsInvalidRawJSONAndPreservesIntegers(t *testing.T) {
	origOut := log.Writer()
	defer log.SetOutput(origOut)

	var buf bytes.Buffer
	log.SetOutput(&buf)

	badCard := AgentCard{Raw: json.RawMessage(`{invalid`)}
	badCard.SetURL("http://127.0.0.1:9104")
	assert.Equal(t, "http://127.0.0.1:9104", badCard.URL)
	assert.Contains(t, buf.String(), "AgentCard.SetURL: WARNING")

	goodCard := AgentCard{Raw: json.RawMessage(`{"name":"sim","big":9007199254740993,"url":"http://old"}`)}
	goodCard.SetURL("http://127.0.0.1:9104")
	assert.Contains(t, string(goodCard.Raw), `"big":9007199254740993`)
	assert.Contains(t, string(goodCard.Raw), `"url":"http://127.0.0.1:9104"`)
}

func TestR4_DiscoverAgentsDeepClonesCapabilitiesUnderConcurrentMutation(t *testing.T) {
	payload := `{
		"name": "cap_agent",
		"version": "1.0.0",
		"capabilities": {
			"extensions": [
				{
					"uri": "n26:dispatch/1.0",
					"params": {
						"mode": "streaming"
					}
				}
			]
		}
	}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(payload))
	}))
	defer srv.Close()

	cat := NewCatalog([]string{srv.URL})
	initial, err := cat.DiscoverAgents()
	require.NoError(t, err)
	require.Equal(t, "streaming", initial["cap_agent"].DispatchMode())

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func(idx int) {
			defer wg.Done()
			agents, err := cat.DiscoverAgents()
			if err != nil {
				return
			}
			card := agents["cap_agent"]
			if exts, ok := card.Capabilities["extensions"].([]interface{}); ok && len(exts) > 0 {
				if extMap, ok := exts[0].(map[string]interface{}); ok {
					if params, ok := extMap["params"].(map[string]interface{}); ok {
						params["mode"] = "mutated"
					}
					extMap["uri"] = "mutated"
				}
				exts[0] = "clobbered"
			}
			card.Capabilities["extensions"] = nil
		}(i)

		go func() {
			defer wg.Done()
			agents, err := cat.DiscoverAgents()
			require.NoError(t, err)
			assert.Equal(t, "streaming", agents["cap_agent"].DispatchMode())
		}()
	}
	wg.Wait()

	after, err := cat.DiscoverAgents()
	require.NoError(t, err)
	assert.Equal(t, "streaming", after["cap_agent"].DispatchMode())
}
