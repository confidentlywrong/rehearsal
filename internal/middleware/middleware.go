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

// Package middleware provides shared HTTP middleware for all Go services.
package middleware

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"

	"github.com/gin-gonic/gin"
	"golang.org/x/oauth2"
	"google.golang.org/api/idtoken"
)

// emptyAudienceWarnOnce ensures the B-22 cloud-misconfiguration warning
// from AttachOIDCTokenAs is logged at most once per process. Local dev
// (not on GCP) stays silent because empty audience is the intended
// no-op there.
var emptyAudienceWarnOnce sync.Once

// oidcTokenSourceCache caches oauth2.TokenSource per audience so we
// do not recreate idtoken sources on every proxied HTTP request.
var (
	oidcTokenCacheMu sync.RWMutex
	oidcTokenCache   = make(map[string]oauth2.TokenSource)
)

func resetOIDCTokenCacheForTest() {
	oidcTokenCacheMu.Lock()
	oidcTokenCache = make(map[string]oauth2.TokenSource)
	oidcTokenCacheMu.Unlock()
}

// CORS returns a gin middleware that sets CORS headers.
// allowedOrigin may be a single origin, a comma-separated list of origins,
// "*", or empty (defaults to "*"). When multiple origins are configured the
// middleware matches the request's Origin header and returns only the matching
// one, as required by the CORS spec.
//
// Wildcard hardening (P1 #7 from the 2026-06-19 audit): a configured value
// of "*" is refused when the process is detected as running on Cloud Run
// (the runtime sets K_SERVICE automatically) or when CORS_REFUSE_WILDCARD=true
// is set explicitly. In both cases the middleware behaves as if zero origins
// were configured — preflights still respond well-formed but no
// Access-Control-Allow-Origin header is emitted, so browsers block the
// cross-origin request. This protects against operators inadvertently
// promoting the .env.example default into a deployed environment.
func CORS(allowedOrigin string) gin.HandlerFunc {
	// Pre-parse once at init time so we don't split on every request.
	raw := strings.TrimSpace(allowedOrigin)
	if raw == "" {
		raw = "*"
	}
	onCloudRun := looksLikeGCP()
	refuseWildcard := onCloudRun || strings.EqualFold(strings.TrimSpace(os.Getenv("CORS_REFUSE_WILDCARD")), "true")
	locked := raw == "*" && refuseWildcard
	if locked {
		log.Printf("CORS: refusing wildcard origin '*' (K_SERVICE=%q CORS_REFUSE_WILDCARD=%q); locking cross-origin off. Set CORS_ALLOWED_ORIGINS to an explicit https://... list.",
			os.Getenv("K_SERVICE"), os.Getenv("CORS_REFUSE_WILDCARD"))
	}
	var allowed []string
	if raw != "*" && !locked {
		for _, o := range strings.Split(raw, ",") {
			if t := strings.TrimSpace(o); t != "" {
				if t == "*" && refuseWildcard {
					log.Printf("CORS: skipping wildcard entry '*' in CORS_ALLOWED_ORIGINS under refuseWildcard mode")
					continue
				}
				allowed = append(allowed, t)
			}
		}
	}

	return func(c *gin.Context) {
		var origin string
		switch {
		case locked:
			// Fall through to the no-match branch below — emit
			// preflight allowances but no Allow-Origin.
		case raw == "*":
			origin = "*"
		default:
			reqOrigin := c.Request.Header.Get("Origin")
			for _, a := range allowed {
				if a == reqOrigin {
					origin = a
					break
				}
			}
		}
		if origin == "" {
			// No allowed origin (either explicit no-match, or wildcard
			// refused). Still set method/header allowances so preflight
			// responses are well-formed, but omit Allow-Origin so the
			// browser blocks the request.
			c.Writer.Header().Set("Access-Control-Allow-Methods",
				"GET, POST, OPTIONS, PUT, PATCH, DELETE")
			c.Writer.Header().Set("Access-Control-Allow-Headers",
				"Content-Type, Content-Length, Accept-Encoding, "+
					"X-CSRF-Token, Authorization, X-Local-Dev-Superadmin, "+
					"X-Firebase-AppCheck, X-Request-ID")
			c.Writer.Header().Add("Vary", "Origin")
			if c.Request.Method == http.MethodOptions {
				c.AbortWithStatus(204)
				return
			}
			c.Next()
			return
		}
		c.Writer.Header().Set("Access-Control-Allow-Origin", origin)
		if origin != "*" {
			c.Writer.Header().Set("Access-Control-Allow-Credentials", "true")
			c.Writer.Header().Add("Vary", "Origin")
		}
		c.Writer.Header().Set("Access-Control-Allow-Methods",
			"GET, POST, OPTIONS, PUT, PATCH, DELETE")
		c.Writer.Header().Set("Access-Control-Allow-Headers",
			"Content-Type, Content-Length, Accept-Encoding, "+
				"X-CSRF-Token, Authorization, X-Local-Dev-Superadmin, "+
				"X-Firebase-AppCheck, X-Request-ID")
		if c.Request.Method == http.MethodOptions {
			c.AbortWithStatus(204)
			return
		}
		c.Next()
	}
}

// RenderConfigJS produces a safe `window.ENV = {...};` JS snippet.
// Go's json.Marshal escapes `<`, `>`, `&`, `\u2028`, `\u2029`, `"`, `\`,
// and control characters so values cannot break out of a `<script>` block
// or corrupt URL query strings containing `=` or `&`.
func RenderConfigJS(vars map[string]string) string {
	if vars == nil {
		vars = map[string]string{}
	}
	data, err := json.Marshal(vars)
	if err != nil {
		log.Printf("RenderConfigJS: json.Marshal failed: %v", err)
		return "window.ENV = {};"
	}
	return fmt.Sprintf("window.ENV = %s;", string(data))
}

// ConfigJSHandler returns a gin handler that serves window.ENV config.
// Values are safely JSON-encoded to prevent XSS.
func ConfigJSHandler(vars map[string]string) gin.HandlerFunc {
	js := RenderConfigJS(vars)
	return func(c *gin.Context) {
		c.Header("Content-Type", "application/javascript")
		c.String(200, js)
	}
}

// newTokenSource is the function used to create OIDC token sources.
// It is a variable to allow replacement in tests.
var newTokenSource = idtoken.NewTokenSource

// AttachOIDCToken fetches a Google OIDC token for the given audience
// and sets the Authorization header on the request.
// This is a no-op if audience is empty.
func AttachOIDCToken(ctx context.Context, header http.Header, audience string) {
	AttachOIDCTokenAs(ctx, header, audience, "Authorization")
}

// AttachOIDCTokenE fetches a Google OIDC token for the given audience
// and sets the Authorization header on the request, returning an error
// if header is nil or token acquisition fails (when audience is non-empty).
func AttachOIDCTokenE(ctx context.Context, header http.Header, audience string) error {
	return AttachOIDCTokenAsE(ctx, header, audience, "Authorization")
}

// AttachOIDCTokenAs is like AttachOIDCToken but writes the token to a
// caller-chosen header name. Use this when the request must preserve
// the user's own bearer token in `Authorization` while still
// authenticating to a private Cloud Run service.
//
// The canonical Google pattern is to write the OIDC token to
// `X-Serverless-Authorization`: Cloud Run's auth proxy validates that
// header against the target service's IAM policy and STRIPS it before
// delivering the request to the container, so the user-facing
// `Authorization: Bearer <user-token>` reaches the application
// untouched. See
// https://cloud.google.com/run/docs/authenticating/service-to-service#use_the_authorization_header
func AttachOIDCTokenAs(ctx context.Context, header http.Header, audience, headerName string) {
	if err := AttachOIDCTokenAsE(ctx, header, audience, headerName); err != nil {
		log.Printf("OIDC: %v", err)
	}
}

// AttachOIDCTokenAsE is the error-returning counterpart of AttachOIDCTokenAs.
// TokenSource instances are cached per audience using context.Background()
// so a single cancelled request does not poison the cached TokenSource.
func AttachOIDCTokenAsE(ctx context.Context, header http.Header, audience, headerName string) error {
	if header == nil {
		return errors.New("AttachOIDCTokenAs: nil http.Header")
	}
	audience = strings.TrimSpace(audience)
	if audience == "" {
		if looksLikeGCP() {
			emptyAudienceWarnOnce.Do(func() {
				log.Printf("OIDC: WARN AttachOIDCToken called with empty audience on GCP (K_SERVICE=%q). Downstream IAM-protected calls will be unauthenticated. Check service env vars (GATEWAY_AUDIENCE, AGENT_AUDIENCE, etc.). This warning is logged once per process.", os.Getenv("K_SERVICE"))
			})
		}
		return nil
	}
	if headerName == "" {
		headerName = "Authorization"
	}

	ts, err := getOrCreateTokenSource(ctx, audience)
	if err != nil {
		return fmt.Errorf("create token source for %s: %w", audience, err)
	}
	token, err := ts.Token()
	if err != nil {
		return fmt.Errorf("get token for %s: %w", audience, err)
	}
	header.Set(headerName, "Bearer "+token.AccessToken)
	return nil
}

func getOrCreateTokenSource(_ context.Context, audience string) (oauth2.TokenSource, error) {
	oidcTokenCacheMu.RLock()
	cached := oidcTokenCache[audience]
	oidcTokenCacheMu.RUnlock()
	if cached != nil {
		return cached, nil
	}

	oidcTokenCacheMu.Lock()
	defer oidcTokenCacheMu.Unlock()
	if cached = oidcTokenCache[audience]; cached != nil {
		return cached, nil
	}
	ts, err := newTokenSource(context.Background(), audience)
	if err != nil {
		return nil, err
	}
	if ts == nil {
		return nil, errors.New("token source constructor returned nil")
	}
	oidcTokenCache[audience] = ts
	return ts, nil
}
