// Copyright 2026 Christian Haeussinger. Apache-2.0.

package billing

import (
	"errors"
	"testing"
)

func TestValidateRedirectURL(t *testing.T) {
	const base = "https://app.race-condition.example"
	tests := []struct {
		name      string
		candidate string
		base      string
		wantErr   bool
	}{
		{"exact match", "https://app.race-condition.example/billing/success", base, false},
		{"path only", base + "/anything?q=1#frag", base, false},
		{"host case-insensitive", "https://APP.race-condition.example/x", base, false},
		{"https default port match",
			"https://app.race-condition.example:443/x",
			"https://app.race-condition.example", false},
		{"scheme mismatch", "http://app.race-condition.example/x", base, true},
		{"host mismatch (subdomain attack)", "https://app.race-condition.example.evil.com/x", base, true},
		{"host mismatch (typo)", "https://app-race-condition.example/x", base, true},
		{"port mismatch", "https://app.race-condition.example:8443/x", base, true},
		{"javascript scheme", "javascript:alert(1)", base, true},
		{"empty candidate", "", base, true},
		{"empty base fails closed", "https://localhost:9100/x", "", true},
		{"whitespace base fails closed", "https://localhost:9100/x", "   ", true},
		{"empty base still rejects empty candidate", "", "", true},
		{"userinfo rejected even on matching host", "https://evil@app.race-condition.example/x", base, true},
		{"empty userinfo rejected", "https://@app.race-condition.example/x", base, true},
		{"userinfo rejected with empty base", "https://evil@localhost:9100/x", "", true},
		{"malformed base without hostname rejected", "https://app.race-condition.example/x", "https:///nohost", true},
		{"control char newline in candidate rejected", "https://app.race-condition.example/x\n", base, true},
		{"control char cr in candidate rejected", "https://app.race-condition.example/\rx", base, true},
		{"control char tab in candidate rejected", "https://app.race-condition.example/\tx", base, true},
		{"control char nul in candidate rejected", "https://app.race-condition.example/\x00x", base, true},
		{"control char del in base rejected", "https://app.race-condition.example/x", base + "\x7f", true},
		{"backslash in candidate authority rejected", `https://evil.com\@app.race-condition.example/x`, base, true},
		{"backslash in candidate host rejected", `https://app.race-condition.example\evil.com/x`, base, true},
		{"opaque url rejected", "https:app.race-condition.example/x", base, true},
		{"allowedBase with userinfo rejected", "https://app.race-condition.example/x", "https://user@app.race-condition.example", true},
		{"allowedBase non-http scheme rejected", "ftp://app.race-condition.example/x", "ftp://app.race-condition.example", true},
		{"allowedBase trailing whitespace trimmed", " https://app.race-condition.example/x ", " " + base + " ", false},
		{"ipv6 loopback match", "http://[::1]:9118/billing", "http://[::1]:9118", false},
		{"ipv6 loopback port mismatch", "http://[::1]:9101/billing", "http://[::1]:9118", true},
		{"trailing dot host mismatch", "https://app.race-condition.example./x", base, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateRedirectURL(tc.candidate, tc.base)
			if tc.wantErr && err == nil {
				t.Fatalf("want error, got nil for candidate=%q base=%q", tc.candidate, tc.base)
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("unexpected error %v for candidate=%q base=%q", err, tc.candidate, tc.base)
			}
			if tc.wantErr && err != nil && !errors.Is(err, ErrRedirectURLNotAllowed) {
				t.Fatalf("error %v does not wrap ErrRedirectURLNotAllowed", err)
			}
		})
	}
}
