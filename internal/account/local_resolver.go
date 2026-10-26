// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package account

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net"
	"net/http"
	"os"
	"strings"
)

// LocalDevEmailEnv overrides the synthetic email LocalResolver
// returns. Defaults to "dev@local.test". Useful for tests that need
// two distinct local identities (set the env var per-process).
const LocalDevEmailEnv = "LOCAL_DEV_USER_EMAIL"

const defaultLocalDevEmail = "dev@local.test"

// LocalSuperadminHeader is a request header that, when set to "1" on
// the LocalResolver path, marks the returned UserContext as
// superadmin. This is the local-dev escape hatch that lets the
// bundled admin-dash call /admin/* endpoints without standing up
// Firebase Auth. It is safe by construction because LocalResolver
// itself is REFUSED in cloud deployments (see middleware.ResolverFromEnv).
const LocalSuperadminHeader = "X-Local-Dev-Superadmin"

// LocalResolver returns a synthetic UserContext for local
// development. It is the default when IDENTITY_PROVIDER is unset or
// equal to "local".
//
// The UID is deterministic — sha256(email)[:12] prefixed with
// "local-dev-" — so the same email across restarts maps to the same
// users row. This means /api/v1/me/init only runs once per local dev
// email even though there is no real auth provider.
//
// MUST NOT be used in any cloud deployment; the middleware layer
// enforces that by refusing to install LocalResolver when
// IDENTITY_PROVIDER is anything other than "local".
type LocalResolver struct{}

// NewLocalResolver returns a LocalResolver. No configuration —
// behavior is driven entirely by the request header + env var.
func NewLocalResolver() *LocalResolver { return &LocalResolver{} }

// Resolve returns a synthetic UserContext. When the request carries
// LocalSuperadminHeader=1, IsSuperadmin is set to true. NeedsInit is
// always false because the row, if missing, is created on-demand by
// the gateway's /api/v1/me handler (meHandler does its own
// store.GetUser lookup and reports needs_init=true → the SPA POSTs
// /api/v1/me/init, which is idempotent). There is NO cold-start
// bootstrap call; the earlier comment that claimed one was wrong
// (no such code path exists — verified 2026-06-22 audit, B-3).
//
// P2 (2026-06-23 audit): defense-in-depth — refuse the request when
// the peer is NOT a loopback address. The primary safeguard is the
// middleware refusing to install LocalResolver in cloud deployments
// (gated on IDENTITY_PROVIDER), but if that env-driven check were
// ever bypassed (typo, mis-merged config) the resolver would silently
// hand out superadmin claims to any caller who set the header. This
// remote-addr check makes that misuse impossible by construction.
// Disable with LOCAL_RESOLVER_ALLOW_NON_LOOPBACK=true (only for
// rare CI scenarios where the test client is on a different host).
func (LocalResolver) Resolve(r *http.Request) (UserContext, error) {
	if r != nil && !strings.EqualFold(os.Getenv("LOCAL_RESOLVER_ALLOW_NON_LOOPBACK"), "true") {
		if !isLoopbackRequest(r) {
			return UserContext{}, errors.New("account: LocalResolver refuses non-loopback peer (set LOCAL_RESOLVER_ALLOW_NON_LOOPBACK=true to override)")
		}
	}
	email := strings.ToLower(strings.TrimSpace(os.Getenv(LocalDevEmailEnv)))
	if email == "" {
		email = defaultLocalDevEmail
	}
	uid := LocalDevUserID(email)
	isAdmin := false
	if r != nil && r.Header.Get(LocalSuperadminHeader) == "1" {
		isAdmin = true
	}
	return UserContext{
		UserID:       uid,
		Email:        email,
		IsSuperadmin: isAdmin,
		NeedsInit:    false,
		// LocalResolver is the dev/test path — auto-approve so unit
		// tests don't have to insert a migration row before every
		// case. Production never reaches this code (refused on
		// non-loopback by the guard above + middleware refuses to
		// install LocalResolver outside dev).
		Status: StatusApproved,
	}, nil
}

// isLoopbackRequest reports whether the request peer is on the local
// machine. We deliberately ignore X-Forwarded-For and similar proxy
// headers: a reverse proxy in front of the dev gateway would defeat
// the safeguard, so the user must explicitly disable the check via
// LOCAL_RESOLVER_ALLOW_NON_LOOPBACK in that scenario.
func isLoopbackRequest(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		// Unix socket or otherwise non-host:port — treat as local.
		host = r.RemoteAddr
	}
	if host == "" {
		return false
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	return ip.IsLoopback()
}

// LocalDevUserID returns the deterministic UID a LocalResolver would
// produce for the given email. Exported so the gateway's local-dev
// bootstrap can seed a matching users row without re-implementing
// the hash.
func LocalDevUserID(email string) string {
	email = strings.ToLower(strings.TrimSpace(email))
	if email == "" {
		email = defaultLocalDevEmail
	}
	sum := sha256.Sum256([]byte(email))
	return "local-dev-" + hex.EncodeToString(sum[:])[:12]
}

// Compile-time guard.
var _ Resolver = (*LocalResolver)(nil)
