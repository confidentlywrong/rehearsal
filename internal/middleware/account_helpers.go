// Copyright 2026 Christian Haeussinger. Apache-2.0.
//
// Small helpers used by the Account middleware. Split out so the
// main middleware file stays focused on the request-time logic.

package middleware

import (
	"errors"
	"net/http"
	"os"
	"strings"
)

// isWriteMethod returns true for HTTP methods that mutate state.
// Used by the payment-failed gate so reads always pass.
func isWriteMethod(m string) bool {
	switch strings.ToUpper(m) {
	case http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete:
		return true
	}
	return false
}

// hasPathPrefix returns true when path starts with any of the
// supplied prefixes. Empty prefixes are skipped so an accidental
// "" entry doesn't match every request.
func hasPathPrefix(path string, prefixes []string) bool {
	for _, p := range prefixes {
		if p == "" {
			continue
		}
		if strings.HasPrefix(path, p) {
			return true
		}
	}
	return false
}

// looksLikeGCP returns true on managed Google compute surfaces.
// Used by AccountResolverFromEnv to refuse IDENTITY_PROVIDER=local
// (the fixture) on Cloud Run / App Engine / Cloud Functions.
func looksLikeGCP() bool {
	for _, k := range []string{"K_SERVICE", "GAE_SERVICE", "FUNCTION_TARGET"} {
		if os.Getenv(k) != "" {
			return true
		}
	}
	return false
}

// errSafetyTrip wraps a boot-time configuration mistake. Returned
// from AccountResolverFromEnv so main() can log a single, actionable
// line and exit.
func errSafetyTrip(msg string) error { return errors.New("middleware/account: " + msg) }
