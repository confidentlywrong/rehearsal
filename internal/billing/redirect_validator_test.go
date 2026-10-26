// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

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
		{"empty base permits anything (stub mode)", "https://localhost:9100/x", "", false},
		{"empty base still rejects empty candidate", "", "", true},
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
