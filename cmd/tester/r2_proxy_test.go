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
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

func TestR2_TesterBuildGatewayAPIURL_PreservesEncodedPathSegments(t *testing.T) {
	reqURL, err := url.Parse("http://localhost:8100/tester/api/v1/items/foo%23bar%3Fbaz%25qux?limit=5")
	if err != nil {
		t.Fatalf("parse reqURL: %v", err)
	}
	got, err := buildGatewayAPIURL("http://gateway.internal:8101/", "/items/foo#bar?baz%qux", reqURL, "/tester")
	if err != nil {
		t.Fatalf("buildGatewayAPIURL: %v", err)
	}
	want := "http://gateway.internal:8101/api/v1/items/foo%23bar%3Fbaz%25qux?limit=5"
	if got != want {
		t.Fatalf("buildGatewayAPIURL = %q, want %q", got, want)
	}
}

func TestR2_TesterProxyStripsHopByHopAndConnectionTokens(t *testing.T) {
	var seenConn, seenSpoofedHop, seenNormal string
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenConn = r.Header.Get("Connection")
		seenSpoofedHop = r.Header.Get("X-Evil-Hop")
		seenNormal = r.Header.Get("X-Trace-Custom")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer backend.Close()

	t.Setenv("GATEWAY_INTERNAL_URL", backend.URL)
	t.Setenv("TESTER_REQUIRE_AUTH", "false")

	r := setupRouter("")
	req := httptest.NewRequest(http.MethodGet, "/api/v1/sessions", nil)
	req.Header.Set("Connection", "keep-alive, X-Evil-Hop, x-serverless-authorization")
	req.Header.Set("X-Evil-Hop", "smuggled")
	req.Header.Set("X-Serverless-Authorization", "Bearer attacker-forged")
	req.Header.Set("X-Trace-Custom", "preserved-value")

	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 OK from proxied backend, got %d (%s)", w.Code, w.Body.String())
	}
	if seenSpoofedHop != "" {
		t.Fatalf("expected Connection-nominated X-Evil-Hop header to be stripped, got %q", seenSpoofedHop)
	}
	if seenConn == "keep-alive, X-Evil-Hop, x-serverless-authorization" {
		t.Fatalf("expected client Connection header tokens not to be forwarded verbatim, got %q", seenConn)
	}
	if seenNormal != "preserved-value" {
		t.Fatalf("expected normal header X-Trace-Custom preserved, got %q", seenNormal)
	}
}
