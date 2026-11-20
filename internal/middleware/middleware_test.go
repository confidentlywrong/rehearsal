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

package middleware

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
	"golang.org/x/oauth2"
	"google.golang.org/api/idtoken"
)

func init() {
	gin.SetMode(gin.TestMode)
}

func TestCORS_DefaultAllowsAll(t *testing.T) {
	r := gin.New()
	r.Use(CORS(""))
	r.GET("/test", func(c *gin.Context) { c.String(200, "ok") })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	r.ServeHTTP(w, req)

	assert.Equal(t, "*", w.Header().Get("Access-Control-Allow-Origin"))
	assert.Contains(t, w.Header().Get("Access-Control-Allow-Methods"), "GET")
	assert.Contains(t, w.Header().Get("Access-Control-Allow-Methods"), "PATCH")
	assert.Contains(t, w.Header().Get("Access-Control-Allow-Headers"), "Content-Type")
	assert.Contains(t, w.Header().Get("Access-Control-Allow-Headers"), "X-Firebase-AppCheck")
	assert.Contains(t, w.Header().Get("Access-Control-Allow-Headers"), "X-Request-ID")
}

func TestCORS_RespectsConfiguredOrigin(t *testing.T) {
	r := gin.New()
	r.Use(CORS("https://example.com"))
	r.GET("/test", func(c *gin.Context) { c.String(200, "ok") })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("Origin", "https://example.com")
	r.ServeHTTP(w, req)

	assert.Equal(t, "https://example.com", w.Header().Get("Access-Control-Allow-Origin"))
	assert.Equal(t, "true", w.Header().Get("Access-Control-Allow-Credentials"))
	assert.Contains(t, w.Header().Values("Vary"), "Origin")
}

func TestCORS_MultiOriginMatchesCorrectOne(t *testing.T) {
	r := gin.New()
	r.Use(CORS("http://localhost:4200,https://prod.example.com"))
	r.GET("/test", func(c *gin.Context) { c.String(200, "ok") })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("Origin", "https://prod.example.com")
	r.ServeHTTP(w, req)

	assert.Equal(t, "https://prod.example.com", w.Header().Get("Access-Control-Allow-Origin"))
	assert.Equal(t, "true", w.Header().Get("Access-Control-Allow-Credentials"))
	assert.Contains(t, w.Header().Values("Vary"), "Origin")
}

func TestCORS_MultiOriginNoMatch(t *testing.T) {
	r := gin.New()
	r.Use(CORS("http://localhost:4200,https://prod.example.com"))
	r.GET("/test", func(c *gin.Context) { c.String(200, "ok") })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("Origin", "https://evil.com")
	r.ServeHTTP(w, req)

	// No Access-Control-Allow-Origin header when origin doesn't match
	assert.Empty(t, w.Header().Get("Access-Control-Allow-Origin"))
	assert.Contains(t, w.Header().Values("Vary"), "Origin")
}

func TestCORS_WildcardNoVary(t *testing.T) {
	r := gin.New()
	r.Use(CORS("*"))
	r.GET("/test", func(c *gin.Context) { c.String(200, "ok") })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/test", nil)
	r.ServeHTTP(w, req)

	assert.Equal(t, "*", w.Header().Get("Access-Control-Allow-Origin"))
	assert.NotContains(t, w.Header().Values("Vary"), "Origin")
}

func TestCORS_OptionsReturns204(t *testing.T) {
	r := gin.New()
	r.Use(CORS(""))
	r.GET("/test", func(c *gin.Context) { c.String(200, "ok") })

	w := httptest.NewRecorder()
	req := httptest.NewRequest("OPTIONS", "/test", nil)
	r.ServeHTTP(w, req)

	assert.Equal(t, 204, w.Code)
}

func TestConfigJS_SafeEncoding(t *testing.T) {
	result := RenderConfigJS(map[string]string{
		"FOO": "bar",
		"URL": "https://example.com/cb?a=1&b=2",
		"XSS": "</script><script>alert('pwned')</script>",
	})
	assert.Contains(t, result, "window.ENV =")
	assert.NotContains(t, result, "</script>")
	assert.Contains(t, result, `\u003c/script\u003e`)
	assert.Contains(t, result, `"FOO":"bar"`)

	rawJSON := strings.TrimSuffix(strings.TrimPrefix(result, "window.ENV = "), ";")
	var decoded map[string]string
	if err := json.Unmarshal([]byte(rawJSON), &decoded); err != nil {
		t.Fatalf("RenderConfigJS did not emit valid JSON: %v", err)
	}
	assert.Equal(t, "https://example.com/cb?a=1&b=2", decoded["URL"])
	assert.Equal(t, "</script><script>alert('pwned')</script>", decoded["XSS"])
}

func TestConfigJS_EmptyMap(t *testing.T) {
	result := RenderConfigJS(map[string]string{})
	assert.Equal(t, "window.ENV = {};", result)
}

func TestConfigJSHandler_ServesJavascript(t *testing.T) {
	r := gin.New()
	r.GET("/config.js", ConfigJSHandler(map[string]string{"KEY": "value"}))

	w := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/config.js", nil)
	r.ServeHTTP(w, req)

	assert.Equal(t, 200, w.Code)
	assert.Contains(t, w.Header().Get("Content-Type"), "application/javascript")
	assert.Contains(t, w.Body.String(), "window.ENV =")
	assert.Contains(t, w.Body.String(), "KEY")
}

func TestAttachOIDCToken_EmptyAudience(t *testing.T) {
	header := make(http.Header)
	AttachOIDCToken(context.Background(), header, "")
	// With empty audience, no Authorization header should be set
	assert.Empty(t, header.Get("Authorization"))
}

// fakeTokenSource implements oauth2.TokenSource for testing.
type fakeTokenSource struct {
	token *oauth2.Token
	err   error
}

func (f *fakeTokenSource) Token() (*oauth2.Token, error) {
	return f.token, f.err
}

func TestAttachOIDCToken_Success(t *testing.T) {
	resetOIDCTokenCacheForTest()
	orig := newTokenSource
	defer func() {
		newTokenSource = orig
		resetOIDCTokenCacheForTest()
	}()

	newTokenSource = func(_ context.Context, _ string, _ ...idtoken.ClientOption) (oauth2.TokenSource, error) {
		return &fakeTokenSource{
			token: &oauth2.Token{AccessToken: "test-oidc-token"},
		}, nil
	}

	header := make(http.Header)
	AttachOIDCToken(context.Background(), header, "https://my-service.run.app")

	assert.Equal(t, "Bearer test-oidc-token", header.Get("Authorization"))
}

func TestAttachOIDCToken_TokenSourceCreationError(t *testing.T) {
	resetOIDCTokenCacheForTest()
	orig := newTokenSource
	defer func() {
		newTokenSource = orig
		resetOIDCTokenCacheForTest()
	}()

	newTokenSource = func(_ context.Context, _ string, _ ...idtoken.ClientOption) (oauth2.TokenSource, error) {
		return nil, errors.New("no credentials found")
	}

	header := make(http.Header)
	AttachOIDCToken(context.Background(), header, "https://my-service.run.app")

	// Should not set Authorization header when token source creation fails
	assert.Empty(t, header.Get("Authorization"))
}

func TestAttachOIDCToken_TokenFetchError(t *testing.T) {
	resetOIDCTokenCacheForTest()
	orig := newTokenSource
	defer func() {
		newTokenSource = orig
		resetOIDCTokenCacheForTest()
	}()

	newTokenSource = func(_ context.Context, _ string, _ ...idtoken.ClientOption) (oauth2.TokenSource, error) {
		return &fakeTokenSource{err: errors.New("token expired")}, nil
	}

	header := make(http.Header)
	AttachOIDCToken(context.Background(), header, "https://my-service.run.app")

	// Should not set Authorization header when token fetch fails
	assert.Empty(t, header.Get("Authorization"))
}

func TestCORS_TrimsWildcardAndChecksGAE(t *testing.T) {
	t.Setenv("CORS_REFUSE_WILDCARD", "true")
	r := gin.New()
	r.Use(CORS("  *  "))
	r.GET("/test", func(c *gin.Context) { c.String(200, "ok") })
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest("GET", "/test", nil))
	assert.Empty(t, w.Header().Get("Access-Control-Allow-Origin"))
}

func TestAttachOIDCTokenAsE_CachesAndErrorsAndNilHeader(t *testing.T) {
	resetOIDCTokenCacheForTest()
	orig := newTokenSource
	defer func() {
		newTokenSource = orig
		resetOIDCTokenCacheForTest()
	}()

	if err := AttachOIDCTokenAsE(context.Background(), nil, "https://svc.run.app", "Authorization"); err == nil {
		t.Fatal("expected error on nil header")
	}

	calls := 0
	newTokenSource = func(_ context.Context, _ string, _ ...idtoken.ClientOption) (oauth2.TokenSource, error) {
		calls++
		return &fakeTokenSource{token: &oauth2.Token{AccessToken: "cached-tok"}}, nil
	}

	h1 := make(http.Header)
	h2 := make(http.Header)
	assert.NoError(t, AttachOIDCTokenAsE(context.Background(), h1, "https://cached.run.app", "X-Serverless-Authorization"))
	assert.NoError(t, AttachOIDCTokenAsE(context.Background(), h2, "https://cached.run.app", "X-Serverless-Authorization"))
	assert.Equal(t, 1, calls)
	assert.Equal(t, "Bearer cached-tok", h1.Get("X-Serverless-Authorization"))
	assert.Equal(t, "Bearer cached-tok", h2.Get("X-Serverless-Authorization"))

	newTokenSource = func(_ context.Context, _ string, _ ...idtoken.ClientOption) (oauth2.TokenSource, error) {
		return nil, errors.New("boom")
	}
	assert.Error(t, AttachOIDCTokenAsE(context.Background(), make(http.Header), "https://err.run.app", "Authorization"))
}
