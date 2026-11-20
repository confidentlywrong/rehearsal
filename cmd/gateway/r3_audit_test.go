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
	"net"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

func TestAuthExchangeHandler_CaseInsensitiveAndCookieSafe(t *testing.T) {
	gin.SetMode(gin.TestMode)

	runReq := func(authHeader string) *httptest.ResponseRecorder {
		r := gin.New()
		r.Use(func(c *gin.Context) {
			c.Request = c.Request.WithContext(
				account.WithContext(c.Request.Context(), account.UserContext{
					UserID: "user-r3",
					Status: account.StatusApproved,
				}),
			)
			c.Next()
		})
		r.POST("/auth/exchange", authExchangeHandler())

		w := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/auth/exchange", nil)
		if authHeader != "" {
			req.Header.Set("Authorization", authHeader)
		}
		r.ServeHTTP(w, req)
		return w
	}

	for _, hdr := range []string{"bearer valid.jwt.tok", "BEARER   valid.jwt.tok  ", "Bearer\tvalid.jwt.tok"} {
		w := runReq(hdr)
		if w.Code != http.StatusNoContent {
			t.Fatalf("%q: status = %d, want 204", hdr, w.Code)
		}
		cookies := w.Result().Cookies()
		if len(cookies) != 1 || cookies[0].Name != sessionCookieName || cookies[0].Value != "valid.jwt.tok" {
			t.Fatalf("%q: unexpected Set-Cookie: %+v", hdr, cookies)
		}
	}

	for _, bad := range []string{
		"Bearer bad;token",
		"Bearer bad,token",
		`Bearer bad"token`,
		`Bearer bad\token`,
		"Bearer bad token",
		"Bearer bad\ttoken",
	} {
		w := runReq(bad)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("%q: status = %d, want 400", bad, w.Code)
		}
		if got := w.Header().Values("Set-Cookie"); len(got) > 0 {
			t.Fatalf("%q: must NOT emit Set-Cookie on invalid token, got %v", bad, got)
		}
	}
}

func TestRefreshAndRawUserBearer_CookieSafety(t *testing.T) {
	gin.SetMode(gin.TestMode)

	wBad := httptest.NewRecorder()
	cBad, _ := gin.CreateTestContext(wBad)
	cBad.Request = httptest.NewRequest(http.MethodPost, "/api/v1/me/init", nil)
	cBad.Request.Header.Set("Authorization", "bearer bad;tok")
	refreshSessionCookieFullLifetime(cBad)
	if len(wBad.Header().Values("Set-Cookie")) > 0 {
		t.Fatalf("refreshSessionCookieFullLifetime set cookie for unsafe token")
	}

	wGood := httptest.NewRecorder()
	cGood, _ := gin.CreateTestContext(wGood)
	cGood.Request = httptest.NewRequest(http.MethodPost, "/api/v1/me/init", nil)
	cGood.Request.Header.Set("Authorization", "bearer good.jwt.tok")
	refreshSessionCookieFullLifetime(cGood)
	if len(wGood.Result().Cookies()) != 1 {
		t.Fatalf("expected __session cookie for lowercase bearer token")
	}

	cCookie, _ := gin.CreateTestContext(httptest.NewRecorder())
	cCookie.Request = httptest.NewRequest(http.MethodGet, "/", nil)
	cCookie.Request.Header.Set("Cookie", "__session=valid.cookie.tok")
	if got := rawUserBearer(cCookie); got != "valid.cookie.tok" {
		t.Fatalf("rawUserBearer(valid cookie) = %q", got)
	}

	cUnsafeCookie, _ := gin.CreateTestContext(httptest.NewRecorder())
	cUnsafeCookie.Request = httptest.NewRequest(http.MethodGet, "/", nil)
	cUnsafeCookie.Request.Header.Set("Cookie", "__session=bad,tok")
	if got := rawUserBearer(cUnsafeCookie); got != "" {
		t.Fatalf("rawUserBearer(unsafe cookie) = %q, want empty", got)
	}
}

func TestValidateAgentURL_MetadataTrailingDot(t *testing.T) {
	noDNS := func(string) ([]net.IP, error) {
		return []net.IP{net.ParseIP("93.184.216.34")}, nil
	}
	for _, raw := range []string{
		"http://metadata.google.internal./computeMetadata/v1/",
		"http://METADATA./",
	} {
		if err := validateAgentURL(raw, false, noDNS); err == nil {
			t.Errorf("validateAgentURL(%q) = nil, want metadata alias error", raw)
		}
	}
}
