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

package account

import (
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

func TestTokenIssuedAt_BoundsAndTypes(t *testing.T) {
	if tm, ok := tokenIssuedAt(jwt.MapClaims{"iat": float64(1700000000)}); !ok || tm.Unix() != 1700000000 {
		t.Fatalf("float64 iat failed: %v %v", tm, ok)
	}
	if tm, ok := tokenIssuedAt(jwt.MapClaims{"iat": int64(1700000001)}); !ok || tm.Unix() != 1700000001 {
		t.Fatalf("int64 iat failed: %v %v", tm, ok)
	}
	if tm, ok := tokenIssuedAt(jwt.MapClaims{"iat": json.Number("1700000002")}); !ok || tm.Unix() != 1700000002 {
		t.Fatalf("json.Number iat failed: %v %v", tm, ok)
	}

	for _, bad := range []any{
		math.NaN(),
		math.Inf(1),
		math.Inf(-1),
		float64(1e30),
		float64(-1e30),
		int64(math.MaxInt64),
		int64(math.MinInt64),
		json.Number("9223372036854775807"),
	} {
		if tm, ok := tokenIssuedAt(jwt.MapClaims{"iat": bad}); ok {
			t.Errorf("tokenIssuedAt(%T(%v)) = (%v, true), want false", bad, bad, tm)
		}
	}
}

func TestParseMaxAge_CaseInsensitiveAndOverflow(t *testing.T) {
	if d, ok := parseMaxAge(`public, Max-Age="120"`); !ok || d != 120*time.Second {
		t.Fatalf("Max-Age parse = (%v, %v), want 120s", d, ok)
	}
	if d, ok := parseMaxAge("max-age=60abc"); ok || d != 0 {
		t.Fatalf("trailing suffix must be rejected, got (%v, %v)", d, ok)
	}
	if d, ok := parseMaxAge("max-age=99999999999999999999999999"); !ok || d != time.Duration(math.MaxInt64) {
		t.Fatalf("overflow max-age = (%v, %v), want MaxInt64 clamped positive", d, ok)
	}
}

func TestExtractBearer_CaseInsensitiveAndCookieSafety(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("Authorization", "bearer jwt.tok.val")
	if got := extractBearer(req); got != "jwt.tok.val" {
		t.Fatalf("extractBearer(lowercase) = %q", got)
	}

	reqBadHdr := httptest.NewRequest(http.MethodGet, "/", nil)
	reqBadHdr.Header.Set("Authorization", "Bearer jwt;tok")
	reqBadHdr.AddCookie(&http.Cookie{Name: "__session", Value: "fallback.should.not.run"})
	if got := extractBearer(reqBadHdr); got != "" {
		t.Fatalf("extractBearer(unsafe header) = %q, want empty", got)
	}

	reqBadCookie := httptest.NewRequest(http.MethodGet, "/", nil)
	reqBadCookie.Header.Set("Cookie", "__session=bad,cookie")
	if got := extractBearer(reqBadCookie); got != "" {
		t.Fatalf("extractBearer(unsafe cookie) = %q, want empty", got)
	}
}

func TestIsLoopbackRequest_BracketedIPv6(t *testing.T) {
	for _, addr := range []string{"[::1]:9101", "[::1]", "::1", "127.0.0.1:9101", "127.0.0.1"} {
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.RemoteAddr = addr
		if !isLoopbackRequest(req) {
			t.Errorf("isLoopbackRequest(%q) = false, want true", addr)
		}
	}
	for _, addr := range []string{"192.0.2.1:9101", ""} {
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.RemoteAddr = addr
		if isLoopbackRequest(req) {
			t.Errorf("isLoopbackRequest(%q) = true, want false", addr)
		}
	}
}
