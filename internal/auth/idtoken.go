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

// Package auth contains shared OIDC helpers for service-to-service calls.
//
// Two auth modes:
//   - IAP (dev/prod): audience = IAP_CLIENT_ID.
//   - Cloud Run IAM (OSS): audience = target service's .run.app URL.
package auth

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"google.golang.org/api/idtoken"
)

// ResolveAudience returns the OIDC audience for a downstream service:
// IAP_CLIENT_ID if set, otherwise normalized fallbackURL (scheme://host).
// An empty return means "do not attach a token" (matches
// middleware.AttachOIDCToken's no-op).
func ResolveAudience(fallbackURL string) string {
	if iap := strings.TrimSpace(os.Getenv("IAP_CLIENT_ID")); iap != "" {
		return iap
	}
	fb := strings.TrimSpace(fallbackURL)
	if fb == "" {
		return ""
	}
	if aud := AudienceFor(fb); aud != "" {
		return aud
	}
	return fb
}

// ParseBearerHeader extracts the bearer token from an Authorization header value
// per RFC 6750 / RFC 7235: case-insensitive "Bearer" scheme followed by one or
// more SP/HTAB whitespace characters and a non-empty token. Tokens containing
// whitespace, control characters, or non-ASCII bytes are rejected.
func ParseBearerHeader(h string) string {
	h = strings.Trim(h, " \t")
	const scheme = "Bearer"
	if len(h) <= len(scheme) || !strings.EqualFold(h[:len(scheme)], scheme) {
		return ""
	}
	rest := h[len(scheme):]
	if rest[0] != ' ' && rest[0] != '\t' {
		return ""
	}
	token := strings.Trim(rest, " \t")
	if token == "" {
		return ""
	}
	for i := 0; i < len(token); i++ {
		b := token[i]
		if b <= 0x20 || b >= 0x7f {
			return ""
		}
	}
	return token
}

// IsCookieSafeToken reports whether tok is a non-empty RFC 6265 cookie-octet
// string, rejecting whitespace, control characters, non-ASCII bytes, and
// cookie delimiter/quote characters ('"', ',', ';', '\\').
func IsCookieSafeToken(tok string) bool {
	if tok == "" {
		return false
	}
	for i := 0; i < len(tok); i++ {
		b := tok[i]
		if b <= 0x20 || b >= 0x7f || b == '"' || b == ',' || b == ';' || b == '\\' {
			return false
		}
	}
	return true
}

// IsCloudRunURL returns true for HTTPS URLs that aren't Agent Engine and aren't
// loopback hosts. Treated as Cloud Run-style endpoints that require an OIDC ID
// token (audience = service URL) rather than an OAuth bearer.
func IsCloudRunURL(u string) bool {
	parsed, err := url.Parse(u)
	if err != nil || !strings.EqualFold(parsed.Scheme, "https") {
		return false
	}
	host := strings.TrimSuffix(strings.ToLower(parsed.Hostname()), ".")
	if host == "" || host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return false
	}
	if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
		return false
	}
	// Agent Engine has its own OAuth-bearer auth path.
	if strings.Contains(host, "aiplatform.googleapis.com") && strings.Contains(parsed.Path, "reasoningEngines") {
		return false
	}
	return true
}

// AudienceFor returns the OIDC audience for an arbitrary URL: scheme + host
// (including port if present, no path). This matches the audience format Cloud
// Run validates against an injected ID token. Returns empty string if the URL
// is unparseable or lacks scheme/host.
func AudienceFor(u string) string {
	parsed, err := url.Parse(u)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return ""
	}
	return strings.ToLower(parsed.Scheme) + "://" + parsed.Host
}

// newIDTokenClient is replaceable in unit tests.
var newIDTokenClient = idtoken.NewClient

// oidcClientCache caches per-audience OIDC HTTP clients to avoid repeated
// metadata-server lookups. Keyed by the resolved audience string.
var (
	oidcClientMu    sync.RWMutex
	oidcClientCache = make(map[string]*http.Client)
)

// OIDCClientE returns an *http.Client that auto-attaches a Google-signed ID
// token with the specified audience to every request. Cached per-audience.
// Returns an explicit error if audience is empty or if the underlying idtoken
// client cannot be constructed (e.g., no Application Default Credentials).
func OIDCClientE(audience string) (*http.Client, error) {
	audience = strings.TrimSpace(audience)
	if audience == "" {
		return nil, errors.New("auth: empty OIDC audience")
	}
	oidcClientMu.RLock()
	cached := oidcClientCache[audience]
	oidcClientMu.RUnlock()
	if cached != nil {
		return cached, nil
	}

	oidcClientMu.Lock()
	defer oidcClientMu.Unlock()
	if cached = oidcClientCache[audience]; cached != nil {
		return cached, nil
	}
	client, err := newIDTokenClient(context.Background(), audience)
	if err != nil {
		return nil, fmt.Errorf("auth: create OIDC client for audience %q: %w", audience, err)
	}
	if client == nil {
		return nil, fmt.Errorf("auth: idtoken client returned nil for audience %q", audience)
	}
	client.Timeout = 10 * time.Second
	oidcClientCache[audience] = client
	return client, nil
}

// OIDCClient returns an *http.Client that auto-attaches a Google-signed ID
// token with the specified audience to every request. Cached per-audience.
// Returns nil if the underlying idtoken client cannot be constructed (e.g.,
// no Application Default Credentials available); prefer OIDCClientE for
// fail-closed callers.
//
// The returned client is intended for service-to-service calls into Cloud Run
// services secured by IAM (--no-allow-unauthenticated). Each request carries
// `Authorization: Bearer <id-token>` with `aud == audience`.
func OIDCClient(audience string) *http.Client {
	client, err := OIDCClientE(audience)
	if err != nil {
		if audience != "" {
			log.Printf("auth: WARNING — %v", err)
		}
		return nil
	}
	return client
}
