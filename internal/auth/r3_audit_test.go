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

package auth

import (
	"context"
	"errors"
	"net/http"
	"sync"
	"testing"

	"google.golang.org/api/idtoken"
)

func TestParseBearerHeader_RFC6750AndValidation(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{name: "standard", in: "Bearer tok123", want: "tok123"},
		{name: "lowercase_scheme", in: "bearer tok123", want: "tok123"},
		{name: "uppercase_scheme", in: "BEARER tok123", want: "tok123"},
		{name: "mixed_case_extra_ows", in: "  bEaReR \t  tok.jwt.sig   ", want: "tok.jwt.sig"},
		{name: "empty", in: "", want: ""},
		{name: "scheme_only", in: "Bearer", want: ""},
		{name: "scheme_trailing_space", in: "Bearer   ", want: ""},
		{name: "no_whitespace_separator", in: "Bearertok123", want: ""},
		{name: "basic_scheme", in: "Basic dXNlcjpwYXNz", want: ""},
		{name: "internal_space", in: "Bearer tok 123", want: ""},
		{name: "crlf_injection", in: "Bearer tok\r\nSet-Cookie: evil=1", want: ""},
		{name: "nul_byte", in: "Bearer tok\x00", want: ""},
		{name: "non_ascii", in: "Bearer tok\xc3\xa9", want: ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ParseBearerHeader(tc.in); got != tc.want {
				t.Fatalf("ParseBearerHeader(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestIsCookieSafeToken(t *testing.T) {
	valid := []string{"eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1MSJ9.sig", "dev_tok-123+a/b="}
	for _, tok := range valid {
		if !IsCookieSafeToken(tok) {
			t.Errorf("IsCookieSafeToken(%q) = false, want true", tok)
		}
	}

	invalid := []string{
		"",
		"a b",
		"a\tb",
		"a;b",
		"a,b",
		`a"b`,
		`a\b`,
		"a\x00b",
		"a\x7fb",
		"a\u00e9b",
	}
	for _, tok := range invalid {
		if IsCookieSafeToken(tok) {
			t.Errorf("IsCookieSafeToken(%q) = true, want false", tok)
		}
	}
}

func TestIsCloudRunURL_And_AudienceFor(t *testing.T) {
	if !IsCloudRunURL("HTTPS://svc-123.run.app/path") {
		t.Error("expected HTTPS Cloud Run URL to return true")
	}
	for _, local := range []string{
		"https://localhost:8080",
		"https://localhost.",
		"https://sub.localhost:8080/a2a",
		"https://127.0.0.1:8080",
		"https://[::1]:8080",
		"https://[::1]/a2a",
	} {
		if IsCloudRunURL(local) {
			t.Errorf("IsCloudRunURL(%q) = true, want false for loopback host", local)
		}
	}
	if got := AudienceFor("//evil.example/a2a"); got != "" {
		t.Errorf("AudienceFor(protocol-relative) = %q, want empty", got)
	}
	if got := AudienceFor("HTTPS://svc-123.run.app:443/a2a"); got != "https://svc-123.run.app:443" {
		t.Errorf("AudienceFor(https) = %q", got)
	}
}

func TestResolveAudience_TrimsAndNormalizes(t *testing.T) {
	t.Setenv("IAP_CLIENT_ID", "  12345-abc.apps.googleusercontent.com  ")
	if got := ResolveAudience("https://gw.run.app/"); got != "12345-abc.apps.googleusercontent.com" {
		t.Fatalf("ResolveAudience with IAP_CLIENT_ID = %q", got)
	}
	t.Setenv("IAP_CLIENT_ID", "   ")
	if got := ResolveAudience("  https://gw.run.app/api/v1/  "); got != "https://gw.run.app" {
		t.Fatalf("ResolveAudience fallback normalized = %q, want https://gw.run.app", got)
	}
	if got := ResolveAudience("   "); got != "" {
		t.Fatalf("ResolveAudience whitespace fallback = %q, want empty", got)
	}
}

func TestOIDCClientE_ErrorsAndCaches(t *testing.T) {
	if _, err := OIDCClientE(""); err == nil {
		t.Fatal("expected error for empty audience")
	}

	orig := newIDTokenClient
	oidcClientMu.Lock()
	delete(oidcClientCache, "https://fail.run.app")
	delete(oidcClientCache, "https://ok.run.app")
	oidcClientMu.Unlock()
	t.Cleanup(func() {
		newIDTokenClient = orig
		oidcClientMu.Lock()
		delete(oidcClientCache, "https://fail.run.app")
		delete(oidcClientCache, "https://ok.run.app")
		oidcClientMu.Unlock()
	})

	newIDTokenClient = func(context.Context, string, ...idtoken.ClientOption) (*http.Client, error) {
		return nil, errors.New("no adc")
	}
	if _, err := OIDCClientE("https://fail.run.app"); err == nil {
		t.Fatal("expected OIDCClientE error when idtoken client fails")
	}

	calls := 0
	newIDTokenClient = func(context.Context, string, ...idtoken.ClientOption) (*http.Client, error) {
		calls++
		return &http.Client{}, nil
	}
	c1, err := OIDCClientE("https://ok.run.app")
	if err != nil || c1 == nil {
		t.Fatalf("expected cached client, got c=%v err=%v", c1, err)
	}
	c2, err := OIDCClientE("https://ok.run.app")
	if err != nil || c2 != c1 || calls != 1 {
		t.Fatalf("expected cached client reuse (calls=%d c1==c2=%v err=%v)", calls, c1 == c2, err)
	}
}

func TestOIDCClient_ConcurrentSafety(t *testing.T) {
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_ = OIDCClient("")
			_ = OIDCClient("https://127.0.0.1:9999")
		}()
	}
	wg.Wait()
}
