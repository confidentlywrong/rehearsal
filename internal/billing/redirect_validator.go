// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

// redirect_validator.go enforces same-origin restrictions on the
// post-Checkout / post-Portal redirect URLs that the client supplies
// in /api/v1/billing/checkout and /api/v1/billing/portal request
// bodies.
//
// Without this guard the URLs are forwarded verbatim to Stripe's
// API, and Stripe itself does not enforce a domain allowlist by
// default. An authenticated user (any signed-up user, since these
// endpoints require a valid Firebase token) could craft a Checkout
// whose success_url points at an attacker-controlled domain and
// then use the resulting Stripe URL as a phishing lure that lands
// on an attacker page right after a trusted Stripe page —
// exceptionally potent social engineering because the victim just
// finished a payment flow and is in a trusting state.
//
// The allowlist anchor is STRIPE_RETURN_BASE_URL (the public base
// URL operators must configure for the real Stripe creator to arm).
// A redirect URL is accepted only when its scheme + host + port
// exactly match the base URL.

package billing

import (
	"errors"
	"fmt"
	"net/url"
	"strings"
)

// ErrRedirectURLNotAllowed is returned by ValidateRedirectURL when
// the candidate's origin does not match the allowed base.
var ErrRedirectURLNotAllowed = errors.New("billing: redirect URL not allowed")

// ValidateRedirectURL checks that candidate has the same scheme +
// host + port as allowedBase. Both arguments must be absolute http
// or https URLs.
//
// If allowedBase is the empty string the function returns nil — the
// real Stripe creator only arms when STRIPE_RETURN_BASE_URL is
// configured, so an empty allowedBase indicates the stub creator
// path (no real redirect happens) and we don't want to block test
// suites that pass localhost URLs.
//
// Comparison is case-insensitive on host (per RFC 3986) and
// case-sensitive on scheme. Port comparison is exact, with the
// scheme defaults applied (so https://example.com and
// https://example.com:443 are considered equivalent).
func ValidateRedirectURL(candidate, allowedBase string) error {
	candidate = strings.TrimSpace(candidate)
	if candidate == "" {
		return fmt.Errorf("%w: empty url", ErrRedirectURLNotAllowed)
	}
	if allowedBase == "" {
		return nil
	}
	cu, err := url.Parse(candidate)
	if err != nil {
		return fmt.Errorf("%w: parse candidate: %v", ErrRedirectURLNotAllowed, err)
	}
	au, err := url.Parse(allowedBase)
	if err != nil {
		return fmt.Errorf("%w: parse allowed_base: %v", ErrRedirectURLNotAllowed, err)
	}
	if cu.Scheme != "http" && cu.Scheme != "https" {
		return fmt.Errorf("%w: scheme must be http or https, got %q", ErrRedirectURLNotAllowed, cu.Scheme)
	}
	if cu.Scheme != au.Scheme {
		return fmt.Errorf("%w: scheme mismatch (want %q, got %q)", ErrRedirectURLNotAllowed, au.Scheme, cu.Scheme)
	}
	if !strings.EqualFold(cu.Hostname(), au.Hostname()) {
		return fmt.Errorf("%w: host mismatch (want %q, got %q)", ErrRedirectURLNotAllowed, au.Hostname(), cu.Hostname())
	}
	if defaultPort(cu) != defaultPort(au) {
		return fmt.Errorf("%w: port mismatch (want %q, got %q)", ErrRedirectURLNotAllowed, defaultPort(au), defaultPort(cu))
	}
	return nil
}

// defaultPort returns the explicit port on u, or the scheme's
// default when omitted. Keeps "https://x" and "https://x:443" in
// the same bucket.
func defaultPort(u *url.URL) string {
	if p := u.Port(); p != "" {
		return p
	}
	switch u.Scheme {
	case "http":
		return "80"
	case "https":
		return "443"
	default:
		return ""
	}
}
