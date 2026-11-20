// Copyright 2026 Christian Haeussinger. Apache-2.0.

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
// Fails closed when allowedBase is empty (STRIPE_RETURN_BASE_URL not
// configured). Rejects control characters, opaque URLs, userinfo,
// backslashes in host/authority, and missing hostnames on both inputs.
//
// Comparison is case-insensitive on host (per RFC 3986) and
// case-sensitive on scheme. Port comparison is exact, with the
// scheme defaults applied (so https://example.com and
// https://example.com:443 are considered equivalent).
func ValidateRedirectURL(candidate, allowedBase string) error {
	if hasControlChar(candidate) {
		return fmt.Errorf("%w: control characters in redirect url are not allowed", ErrRedirectURLNotAllowed)
	}
	if hasControlChar(allowedBase) {
		return fmt.Errorf("%w: control characters in allowed_base are not allowed", ErrRedirectURLNotAllowed)
	}
	candidate = strings.TrimSpace(candidate)
	if candidate == "" {
		return fmt.Errorf("%w: empty url", ErrRedirectURLNotAllowed)
	}
	allowedBase = strings.TrimSpace(allowedBase)
	if allowedBase == "" {
		return fmt.Errorf("%w: allowed_base not configured", ErrRedirectURLNotAllowed)
	}
	if authorityHasBackslash(candidate) {
		return fmt.Errorf("%w: backslash in redirect url authority is not allowed", ErrRedirectURLNotAllowed)
	}
	if authorityHasBackslash(allowedBase) {
		return fmt.Errorf("%w: backslash in allowed_base authority is not allowed", ErrRedirectURLNotAllowed)
	}
	cu, err := url.Parse(candidate)
	if err != nil {
		return fmt.Errorf("%w: parse candidate: %v", ErrRedirectURLNotAllowed, err)
	}
	if cu.Opaque != "" {
		return fmt.Errorf("%w: opaque redirect url is not allowed", ErrRedirectURLNotAllowed)
	}
	if cu.User != nil {
		return fmt.Errorf("%w: userinfo in redirect url is not allowed", ErrRedirectURLNotAllowed)
	}
	if cu.Scheme != "http" && cu.Scheme != "https" {
		return fmt.Errorf("%w: scheme must be http or https, got %q", ErrRedirectURLNotAllowed, cu.Scheme)
	}
	if cu.Hostname() == "" {
		return fmt.Errorf("%w: candidate must specify a hostname", ErrRedirectURLNotAllowed)
	}
	if strings.Contains(cu.Host, "\\") {
		return fmt.Errorf("%w: backslash in redirect url host is not allowed", ErrRedirectURLNotAllowed)
	}
	au, err := url.Parse(allowedBase)
	if err != nil {
		return fmt.Errorf("%w: parse allowed_base: %v", ErrRedirectURLNotAllowed, err)
	}
	if au.Opaque != "" {
		return fmt.Errorf("%w: opaque allowed_base is not allowed", ErrRedirectURLNotAllowed)
	}
	if au.User != nil {
		return fmt.Errorf("%w: userinfo in allowed_base is not allowed", ErrRedirectURLNotAllowed)
	}
	if au.Scheme != "http" && au.Scheme != "https" {
		return fmt.Errorf("%w: allowed_base scheme must be http or https, got %q", ErrRedirectURLNotAllowed, au.Scheme)
	}
	if au.Hostname() == "" {
		return fmt.Errorf("%w: allowed_base must specify a hostname", ErrRedirectURLNotAllowed)
	}
	if strings.Contains(au.Host, "\\") {
		return fmt.Errorf("%w: backslash in allowed_base host is not allowed", ErrRedirectURLNotAllowed)
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

func hasControlChar(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < 0x20 || s[i] == 0x7f {
			return true
		}
	}
	return false
}

func authorityHasBackslash(raw string) bool {
	idx := strings.Index(raw, "://")
	if idx < 0 {
		return false
	}
	rest := raw[idx+3:]
	if cut := strings.IndexAny(rest, "/?#"); cut >= 0 {
		rest = rest[:cut]
	}
	return strings.Contains(rest, "\\")
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
