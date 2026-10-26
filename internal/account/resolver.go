// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package account

import (
	"errors"
	"net/http"
)

// ErrNoIdentity is returned by Resolver.Resolve when the request
// carries no recognizable credential (no Authorization header, no
// __session cookie, etc.). Middleware translates this to 401.
var ErrNoIdentity = errors.New("account: no identity in request")

// Resolver extracts a UserContext from an incoming HTTP request.
//
// Two implementations exist:
//
//   - FirebaseResolver: production. Verifies a Firebase ID token from
//     the Authorization header (or the __session cookie for
//     WebSocket connections) and looks up the users row.
//
//   - LocalResolver: development + tests. Synthesizes a UserContext
//     from the LOCAL_DEV_USER_EMAIL env var. No token verification.
//
// Resolvers MUST NOT mutate the request. They MAY perform a single
// DB roundtrip per call (the users lookup). They MUST set
// NeedsInit=true when the user has authenticated but has no users
// row yet — middleware uses this to allow-list /api/v1/me/init.
type Resolver interface {
	Resolve(r *http.Request) (UserContext, error)
}
