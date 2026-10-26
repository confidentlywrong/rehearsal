// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.
//
// Tests for the AGENT_URLS SSRF guard (audit P3 #9, 2026-06-21).
// Covers the boot-time validator that filters AGENT_URLS entries
// before they reach the agent catalog.

package main

import (
	"errors"
	"net"
	"strings"
	"testing"
)

func TestValidateAgentURL(t *testing.T) {
	// stubLookup lets each case control DNS resolution without
	// touching the real network. Key by host (case-insensitive).
	stubLookup := func(table map[string][]net.IP, err error) func(string) ([]net.IP, error) {
		return func(host string) ([]net.IP, error) {
			if err != nil {
				return nil, err
			}
			if v, ok := table[strings.ToLower(host)]; ok {
				return v, nil
			}
			return nil, errors.New("no such host")
		}
	}

	tests := []struct {
		name      string
		url       string
		inCloud   bool
		lookup    func(string) ([]net.IP, error)
		wantErr   bool
		errSubstr string
	}{
		{
			name:    "loopback http local dev",
			url:     "http://127.0.0.1:9100",
			inCloud: false,
			lookup:  stubLookup(nil, nil),
			wantErr: false,
		},
		{
			name:    "loopback http allowed in cloud (test agent)",
			url:     "http://127.0.0.1:9100",
			inCloud: true,
			lookup:  stubLookup(nil, nil),
			wantErr: false,
		},
		{
			name: "https public hostname",
			url:  "https://leadership-runner-xyz-uc.a.run.app",
			lookup: stubLookup(map[string][]net.IP{
				"leadership-runner-xyz-uc.a.run.app": {net.ParseIP("142.250.4.5")},
			}, nil),
			wantErr: false,
		},
		{
			name: "https private RFC1918 host (VPC connector path)",
			url:  "https://internal-agent.svc",
			lookup: stubLookup(map[string][]net.IP{
				"internal-agent.svc": {net.ParseIP("10.20.30.40")},
			}, nil),
			wantErr: false,
		},

		// --- SSRF rejections ---
		{
			name:      "literal metadata IP rejected",
			url:       "http://169.254.169.254/computeMetadata/v1/",
			lookup:    stubLookup(nil, nil),
			wantErr:   true,
			errSubstr: "link-local",
		},
		{
			name:      "metadata.google.internal alias rejected pre-DNS",
			url:       "http://metadata.google.internal/computeMetadata/v1/",
			lookup:    stubLookup(nil, nil),
			wantErr:   true,
			errSubstr: "metadata alias",
		},
		{
			name:      "bare 'metadata' alias rejected (search-domain hijack)",
			url:       "http://metadata/computeMetadata/v1/",
			lookup:    stubLookup(nil, nil),
			wantErr:   true,
			errSubstr: "metadata alias",
		},
		{
			name:      "alias rejection is case-insensitive",
			url:       "http://Metadata.Google.Internal/",
			lookup:    stubLookup(nil, nil),
			wantErr:   true,
			errSubstr: "metadata alias",
		},
		{
			name: "innocuous hostname resolving to link-local rejected",
			url:  "http://shadow.example.com/",
			lookup: stubLookup(map[string][]net.IP{
				"shadow.example.com": {net.ParseIP("169.254.169.254")},
			}, nil),
			wantErr:   true,
			errSubstr: "link-local",
		},
		{
			name: "host with mixed safe + link-local IPs still rejected (any)",
			url:  "http://dual.example.com/",
			lookup: stubLookup(map[string][]net.IP{
				"dual.example.com": {net.ParseIP("8.8.8.8"), net.ParseIP("169.254.42.1")},
			}, nil),
			wantErr:   true,
			errSubstr: "link-local",
		},

		// --- scheme tightening ---
		{
			name:      "http public host rejected in cloud",
			url:       "http://leadership-runner-xyz-uc.a.run.app",
			inCloud:   true,
			lookup:    stubLookup(nil, nil),
			wantErr:   true,
			errSubstr: "scheme http not allowed in cloud",
		},
		{
			name:      "ftp scheme rejected",
			url:       "ftp://example.com/",
			lookup:    stubLookup(nil, nil),
			wantErr:   true,
			errSubstr: "unsupported scheme",
		},
		{
			name:      "missing host rejected",
			url:       "https://",
			lookup:    stubLookup(nil, nil),
			wantErr:   true,
			errSubstr: "missing host",
		},

		// --- DNS failure path ---
		{
			name:      "dns failure surfaces as dns error not silent drop",
			url:       "https://nonexistent.example.com/",
			lookup:    stubLookup(nil, errors.New("nxdomain")),
			wantErr:   true,
			errSubstr: "dns resolve",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := validateAgentURL(tc.url, tc.inCloud, tc.lookup)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("want error, got nil")
				}
				if tc.errSubstr != "" && !strings.Contains(err.Error(), tc.errSubstr) {
					t.Fatalf("want error containing %q, got %q", tc.errSubstr, err.Error())
				}
				return
			}
			if err != nil {
				t.Fatalf("want no error, got %v", err)
			}
		})
	}
}
