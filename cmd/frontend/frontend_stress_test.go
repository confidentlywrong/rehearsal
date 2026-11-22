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
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/gin-gonic/gin"
)

func init() {
	gin.SetMode(gin.TestMode)
	_ = os.Setenv("LOCAL_RESOLVER_ALLOW_NON_LOOPBACK", "true")
	_ = os.Setenv("IDENTITY_PROVIDER", "local")

	// If running from cmd/frontend, switch to repo root so relative asset paths resolve
	if _, err := os.Stat("main.go"); err == nil {
		if _, err := os.Stat("../../web/frontend"); err == nil {
			_ = os.Chdir("../..")
		}
	}
}

// TestFrontendHandlerRoutingAndBoundaries verifies all routing variations
// for /frontend vs /frontend/ and potential edge/boundary cases.
func TestFrontendHandlerRoutingAndBoundaries(t *testing.T) {
	r := setupRouter("test-aud", account.NewLocalResolver())

	testCases := []struct {
		name           string
		method         string
		path           string
		headers        map[string]string
		expectedStatus int
		bodyMustContain string
	}{
		{
			name:           "Health check bare",
			method:         "GET",
			path:           "/health",
			expectedStatus: http.StatusOK,
			bodyMustContain: `"service":"frontend"`,
		},
		{
			name:           "Health check under /frontend prefix",
			method:         "GET",
			path:           "/frontend/health",
			expectedStatus: http.StatusOK,
			bodyMustContain: `"service":"frontend"`,
		},
		{
			name:           "Config JS bare",
			method:         "GET",
			path:           "/config.js",
			expectedStatus: http.StatusOK,
			bodyMustContain: `window.ENV`,
		},
		{
			name:           "Config JS under /frontend prefix",
			method:         "GET",
			path:           "/frontend/config.js",
			expectedStatus: http.StatusOK,
			bodyMustContain: `window.ENV`,
		},
		{
			name:           "Login handler redirect in local dev",
			method:         "GET",
			path:           "/login",
			expectedStatus: http.StatusFound,
		},
		{
			name:           "Login handler with /frontend prefix in local dev",
			method:         "GET",
			path:           "/frontend/login",
			expectedStatus: http.StatusFound,
		},
		{
			name:           "Root SPA index /",
			method:         "GET",
			path:           "/",
			expectedStatus: http.StatusOK,
			bodyMustContain: "<!doctype html>",
		},
		{
			name:           "Bare /frontend serving SPA index",
			method:         "GET",
			path:           "/frontend",
			expectedStatus: http.StatusOK,
			bodyMustContain: "<!doctype html>",
		},
		{
			name:           "Trailing slash /frontend/ serving SPA index",
			method:         "GET",
			path:           "/frontend/",
			expectedStatus: http.StatusOK,
			bodyMustContain: "<!doctype html>",
		},
		{
			name:           "SPA client-side sub-route /frontend/simulation",
			method:         "GET",
			path:           "/frontend/simulation",
			expectedStatus: http.StatusOK,
			bodyMustContain: "<!doctype html>",
		},
		{
			name:           "SPA client-side nested sub-route /frontend/app/view/settings",
			method:         "GET",
			path:           "/frontend/app/view/settings",
			expectedStatus: http.StatusOK,
			bodyMustContain: "<!doctype html>",
		},
		{
			name:           "Non-GET method POST /frontend should be 404",
			method:         "POST",
			path:           "/frontend",
			expectedStatus: http.StatusNotFound,
		},
		{
			name:           "Non-GET method DELETE /frontend/ should be 404",
			method:         "DELETE",
			path:           "/frontend/",
			expectedStatus: http.StatusNotFound,
		},
		{
			name:           "Path traversal attempt /frontend/../../etc/passwd",
			method:         "GET",
			path:           "/frontend/../../etc/passwd",
			expectedStatus: http.StatusBadRequest,
		},
		{
			name:           "Multiple leading/trailing slashes /frontend////",
			method:         "GET",
			path:           "/frontend////",
			expectedStatus: http.StatusOK,
			bodyMustContain: "<!doctype html>",
		},
		{
			name:           "Query params on /frontend?session=123&debug=true",
			method:         "GET",
			path:           "/frontend?session=123&debug=true",
			expectedStatus: http.StatusOK,
			bodyMustContain: "<!doctype html>",
		},
		{
			name:           "Missing static asset with extension /frontend/does_not_exist.png",
			method:         "GET",
			path:           "/frontend/does_not_exist.png",
			expectedStatus: http.StatusNotFound,
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			w := httptest.NewRecorder()
			req, err := http.NewRequest(tc.method, tc.path, nil)
			if err != nil {
				t.Fatalf("Failed to create request: %v", err)
			}
			req.RemoteAddr = "127.0.0.1:54321"
			for k, v := range tc.headers {
				req.Header.Set(k, v)
			}

			r.ServeHTTP(w, req)

			if w.Code != tc.expectedStatus {
				t.Errorf("Path %s: expected status %d, got %d. Body: %s",
					tc.path, tc.expectedStatus, w.Code, w.Body.String())
			}

			if tc.bodyMustContain != "" {
				body := w.Body.String()
				if !strings.Contains(strings.ToLower(body), strings.ToLower(tc.bodyMustContain)) {
					t.Errorf("Path %s: body expected to contain %q, got (truncated): %s",
						tc.path, tc.bodyMustContain, body[:min(len(body), 200)])
				}
			}
		})
	}
}

// TestFrontendStressConcurrentAccess hammers /frontend and /frontend/ concurrently
// to verify absence of data races, panics, goroutine leaks, and memory exhaustion.
func TestFrontendStressConcurrentAccess(t *testing.T) {
	r := setupRouter("test-aud", account.NewLocalResolver())

	const totalRequests = 2000
	const concurrency = 50

	paths := []string{
		"/frontend",
		"/frontend/",
		"/frontend/health",
		"/frontend/config.js",
		"/frontend/login",
		"/frontend/dashboard",
		"/frontend/app/nested/route",
		"/frontend/nonexistent.js",
		"/health",
		"/config.js",
		"/",
	}

	var (
		wg          sync.WaitGroup
		successCount atomic.Int64
		notFoundCount atomic.Int64
		redirectCount atomic.Int64
		errorCount   atomic.Int64
		panicCount   atomic.Int64
	)

	initialGoroutines := runtime.NumGoroutine()

	ch := make(chan string, totalRequests)
	for i := 0; i < totalRequests; i++ {
		ch <- paths[i%len(paths)]
	}
	close(ch)

	startTime := time.Now()

	for worker := 0; worker < concurrency; worker++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			defer func() {
				if r := recover(); r != nil {
					panicCount.Add(1)
					t.Errorf("Worker %d panicked: %v", workerID, r)
				}
			}()

			for path := range ch {
				w := httptest.NewRecorder()
				req, err := http.NewRequest("GET", path, nil)
				if err != nil {
					errorCount.Add(1)
					continue
				}
				req.RemoteAddr = fmt.Sprintf("127.0.0.1:%d", 10000+workerID)

				r.ServeHTTP(w, req)

				switch w.Code {
				case http.StatusOK:
					successCount.Add(1)
				case http.StatusFound:
					redirectCount.Add(1)
				case http.StatusNotFound:
					notFoundCount.Add(1)
				default:
					errorCount.Add(1)
					t.Errorf("Unexpected status code %d for path %s", w.Code, path)
				}
			}
		}(worker)
	}

	wg.Wait()
	elapsed := time.Since(startTime)

	t.Logf("Completed %d requests in %v (throughput: %.1f req/s)",
		totalRequests, elapsed, float64(totalRequests)/elapsed.Seconds())
	t.Logf("Counts: 200 OK=%d, 302 Redirect=%d, 404 NotFound=%d, Errors=%d, Panics=%d",
		successCount.Load(), redirectCount.Load(), notFoundCount.Load(), errorCount.Load(), panicCount.Load())

	if panicCount.Load() > 0 {
		t.Fatalf("FAILED: Encountered %d panics during concurrent stress test!", panicCount.Load())
	}
	if errorCount.Load() > 0 {
		t.Fatalf("FAILED: Encountered %d unexpected error responses!", errorCount.Load())
	}

	// Verify goroutine cleanup
	time.Sleep(100 * time.Millisecond)
	finalGoroutines := runtime.NumGoroutine()
	leakedGoroutines := finalGoroutines - initialGoroutines
	t.Logf("Goroutine count: initial=%d, final=%d, diff=%d",
		initialGoroutines, finalGoroutines, leakedGoroutines)

	if leakedGoroutines > 10 {
		t.Errorf("Potential goroutine leak detected: leaked %d goroutines", leakedGoroutines)
	}
}

// TestFrontendSafeLocalLoginRedirect reproduces the open-redirect sanitizer gap:
// The comment in main.go:564 states:
// "Reject encoded scheme/host tricks like /%2f%2fevil.com by re-parsing and only keeping path+query+fragment."
// However, url.Parse does not set u.Host for "/%2f%2fevil.com", but rather sets u.Path to "///evil.com".
// Because strings.HasPrefix(u.Path, "//") is not checked on the parsed u.Path,
// the function returns "/%2f%2fevil.com" rather than rejecting to "/".
func TestFrontendSafeLocalLoginRedirect(t *testing.T) {
	// Standard rejection cases that work:
	standardCases := []struct {
		input    string
		expected string
	}{
		{"", "/"},
		{"https://evil.com", "/"},
		{"//evil.com", "/"},
		{"/\\evil.com", "/"},
		{"javascript:alert(1)", "/"},
		{"/valid/path", "/valid/path"},
		{"/valid/path?query=1#frag", "/valid/path?query=1#frag"},
	}

	for _, c := range standardCases {
		out := safeLocalLoginRedirect(c.input)
		if out != c.expected {
			t.Errorf("safeLocalLoginRedirect(%q): expected %q, got %q", c.input, c.expected, out)
		}
	}

	for _, encodedTrick := range []string{"/%2f%2fevil.com", "/%2F%2Fevil.com", "/%5cevil.com"} {
		if actual := safeLocalLoginRedirect(encodedTrick); actual != "/" {
			t.Errorf("safeLocalLoginRedirect(%q): expected \"/\", got %q", encodedTrick, actual)
		}
	}
}

func TestR2_FrontendProxyPreservesEscapedPathAndStripsHopByHopHeaders(t *testing.T) {
	var seenRawURI, seenEvilHop, seenServerlessAuth string
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenRawURI = r.URL.RequestURI()
		seenEvilHop = r.Header.Get("X-Evil-Hop")
		seenServerlessAuth = r.Header.Get("X-Serverless-Authorization")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer backend.Close()

	t.Setenv("GATEWAY_INTERNAL_URL", backend.URL)
	r := setupRouter("", account.NewLocalResolver())

	req := httptest.NewRequest(http.MethodGet, "/frontend/api/v1/items/a%23b%3Fc%25d?q=1", nil)
	req.RemoteAddr = "127.0.0.1:4321"
	req.Header.Set("Connection", "X-Evil-Hop, X-Serverless-Authorization")
	req.Header.Set("X-Evil-Hop", "smuggled")
	req.Header.Set("X-Serverless-Authorization", "Bearer attacker-forged")

	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 OK, got %d (%s)", w.Code, w.Body.String())
	}
	if seenRawURI != "/api/v1/items/a%23b%3Fc%25d?q=1" {
		t.Fatalf("expected escaped path preserved as /api/v1/items/a%%23b%%3Fc%%25d?q=1, got %q", seenRawURI)
	}
	if seenEvilHop != "" {
		t.Fatalf("expected Connection-listed X-Evil-Hop stripped, got %q", seenEvilHop)
	}
	if seenServerlessAuth != "" {
		t.Fatalf("expected forged X-Serverless-Authorization stripped when oidcAudience is empty, got %q", seenServerlessAuth)
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
