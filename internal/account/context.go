// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package account

import (
	"context"
)

// userContextKey is the unexported key under which UserContext is
// stashed on a request's context.Context by the auth middleware.
// Unexported so no other package can accidentally collide with it.
type userContextKey struct{}

// UserContext is the resolved identity for one request. Produced by
// a Resolver (Firebase in production, LocalResolver in dev) and
// attached to the request ctx by middleware.Account(). Handlers read
// it with FromContext(ctx).
//
// Fields are intentionally minimal: anything that needs a DB lookup
// (current plan caps, payment status freshness, audit history) is
// fetched on demand so the struct stays cheap to pass around.
type UserContext struct {
	// UserID is the Firebase UID (or LocalResolver's synthesized id).
	// Required; never empty when this struct is returned from a Resolver.
	UserID string

	// Email is the verified email address of the user. Always set by
	// FirebaseResolver; set by LocalResolver from LOCAL_DEV_USER_EMAIL.
	Email string

	// IsSuperadmin is the denormalized copy of the users.is_superadmin
	// column, populated by the Resolver. Treated as advisory; the
	// authoritative source is the DB row.
	IsSuperadmin bool

	// NeedsInit is true when the resolver verified the user's identity
	// but the user has no row in the `users` table yet. The auth
	// middleware allow-lists /api/v1/me/init in this case and blocks
	// everything else with HTTP 409 so the SPA knows to call init.
	NeedsInit bool

	// Status is the manual-approval lifecycle state of the row,
	// denormalized from users.status for fast middleware checks
	// (no extra DB roundtrip per request). Empty string when
	// NeedsInit=true. The Account middleware refuses to serve any
	// route other than /api/v1/me*, /api/v1/auth/exchange and the
	// probes when this is not StatusApproved.
	Status UserStatus
}

// IsApproved reports whether the request's user row has been moved
// out of the pending queue. False for NeedsInit, StatusPending, and
// StatusRejected — the middleware treats all three as "block every
// protected route".
func (u UserContext) IsApproved() bool {
	return u.HasIdentity() && !u.NeedsInit && u.Status == StatusApproved
}

// HasIdentity reports whether the context represents an authenticated
// user (vs. an anonymous request). True iff UserID is non-empty.
func (u UserContext) HasIdentity() bool { return u.UserID != "" }

// WithContext returns ctx with u attached under the package-private
// key. Used by middleware after a Resolver succeeds.
func (u UserContext) WithContext(ctx context.Context) context.Context {
	return context.WithValue(ctx, userContextKey{}, u)
}

// WithContext is a free-function equivalent of UserContext.WithContext
// provided for symmetry with the legacy tenant.WithContext API.
// Callers may use either style.
func WithContext(ctx context.Context, u UserContext) context.Context {
	return context.WithValue(ctx, userContextKey{}, u)
}

// FromContext returns the UserContext attached by middleware, or the
// zero value if none was set. Never returns nil; HasIdentity() on the
// zero value returns false so handlers can use the same check whether
// auth ran or not.
func FromContext(ctx context.Context) UserContext {
	if ctx == nil {
		return UserContext{}
	}
	if v, ok := ctx.Value(userContextKey{}).(UserContext); ok {
		return v
	}
	return UserContext{}
}

// FromContextOK is like FromContext but also returns false when no
// UserContext is attached. Use this when you need to distinguish
// "no auth ran" from "auth ran and produced an anonymous user" —
// the latter is impossible today (resolvers always set UserID) but
// the dual-return API matches tenant.FromContext and simplifies
// porting call sites that already use the comma-ok pattern.
func FromContextOK(ctx context.Context) (UserContext, bool) {
	if ctx == nil {
		return UserContext{}, false
	}
	v, ok := ctx.Value(userContextKey{}).(UserContext)
	return v, ok
}
