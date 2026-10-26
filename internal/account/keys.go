// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package account

import (
	"context"
	"os"
	"strings"
)

// saasModeEnabled mirrors Python's tenant_keys._saas_mode_enabled().
// Accepts "on", "true", "1", "yes" (case-insensitive — matches Python).
// When SAAS_MODE is off (the default), all key-prefix helpers return
// "" so Redis keys are byte-identical to the legacy single-tenant
// format. This keeps Go and Python in sync: Python's tenant_keys.py
// also gates on SAAS_MODE before prefixing.
func saasModeEnabled() bool {
	v := strings.ToLower(os.Getenv("SAAS_MODE"))
	return v == "on" || v == "true" || v == "1" || v == "yes"
}

// SaaSModeEnabled exposes the SAAS_MODE gate to callers outside the
// account package (middleware, gateway handlers, tests). Wraps the
// package-internal “saasModeEnabled“ so the single-source-of-truth
// stays here. Introduced 2026-07-02 to let the middleware and
// “meHandler“ skip the manual-approval flow entirely when the
// deployment is single-tenant (SAAS_MODE=off). Local “make start“
// used to trap every new user on the “/pending-approval“ screen
// because “CreateUser“ unconditionally inserts rows in
// “StatusPending“ and the middleware always gated on that status.
func SaaSModeEnabled() bool {
	return saasModeEnabled()
}

// KeyPrefix returns the per-user namespace prefix for Redis keys and
// Pub/Sub queue names.
//
// When SAAS_MODE is off (the default for local dev), per-user
// isolation is disabled and the function always returns "". This
// produces the legacy un-prefixed key format that Python agents
// expect.
//
// When SAAS_MODE is on, per-user isolation kicks in:
//
//   - context has UserContext   : "{ACCOUNT_KEY_PREFIX}:{user_id}"
//   - context has no UserContext: ""  (caller produces legacy key)
//
// The ACCOUNT_KEY_PREFIX env var defaults to "u" and is read on
// every call so test setup can mutate it. The performance cost is
// negligible (single os.Getenv per Redis call).
func KeyPrefix(ctx context.Context) string {
	if !saasModeEnabled() {
		return ""
	}
	uc := FromContext(ctx)
	if uc.UserID == "" {
		return ""
	}
	return keyPrefixForUser(uc.UserID)
}

// keyPrefixForUser builds the prefix for an explicit user_id.
// Useful for code paths that iterate per-user shards without a
// request context. Returns "" when SAAS_MODE is off.
func keyPrefixForUser(userID string) string {
	if !saasModeEnabled() {
		return ""
	}
	if userID == "" {
		return ""
	}
	rootPrefix := os.Getenv("ACCOUNT_KEY_PREFIX")
	if rootPrefix == "" {
		rootPrefix = "u"
	}
	return rootPrefix + ":" + userID
}

// Key composes a Redis key from a sequence of segments, prepending
// the per-user namespace when a UserContext is on ctx.
//
// Examples (with ACCOUNT_KEY_PREFIX=u):
//
//	no ctx user           : "session:abc"  → "session:abc"
//	user=local-dev-abc... : "session:abc"  → "u:local-dev-abc...:session:abc"
//
// Empty segments are silently skipped.
func Key(ctx context.Context, segments ...string) string {
	return KeyForUser(userFromContext(ctx), segments...)
}

// KeyForUser is the user-explicit form of Key. Pass "" to disable
// the prefix (legacy / unprefixed).
func KeyForUser(userID string, segments ...string) string {
	parts := make([]string, 0, len(segments)+1)
	if p := keyPrefixForUser(userID); p != "" {
		parts = append(parts, p)
	}
	for _, s := range segments {
		if s != "" {
			parts = append(parts, s)
		}
	}
	return strings.Join(parts, ":")
}

func userFromContext(ctx context.Context) string {
	return FromContext(ctx).UserID
}
