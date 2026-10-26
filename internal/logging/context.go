// Copyright 2026 The Confidently Wrong Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package logging

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"log/slog"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// ctxKey is the unexported type for context keys this package owns.
// Using a private type prevents collisions with other packages'
// context keys.
type ctxKey int

const (
	requestIDKey ctxKey = iota
)

// WithRequestID returns a new context that carries the given
// request_id. Used by the request-logger middleware so handlers
// downstream can emit logs that link back to a single HTTP request.
func WithRequestID(ctx context.Context, id string) context.Context {
	if id == "" {
		return ctx
	}
	return context.WithValue(ctx, requestIDKey, id)
}

// RequestID returns the request_id stored on ctx, or the empty
// string if none was attached.
func RequestID(ctx context.Context) string {
	if v, ok := ctx.Value(requestIDKey).(string); ok {
		return v
	}
	return ""
}

// FromContext returns slog.Default() decorated with whatever
// per-request attributes are present on ctx:
//
//   - request_id (set by request-logger middleware)
//   - user_id, email_hash (set by account middleware via
//     account.WithContext / account.FromContext). The email itself
//     is NEVER logged — only the first 8 hex chars of its SHA-256
//     are emitted so an operator can correlate log lines for the
//     same user without retaining the raw email in Cloud Logging
//     for the (potentially years-long) retention window. The hash
//     is deterministic so an operator who already knows a user's
//     email can compute the same prefix locally to find their rows.
//   - is_superadmin (only when true, to keep ordinary logs clean)
//
// Each attribute is added only when non-empty so unauthenticated
// logs stay clean.
//
// FromContext NEVER returns nil — even with an empty context it
// returns slog.Default(). Safe to call from any code path.
func FromContext(ctx context.Context) *slog.Logger {
	logger := slog.Default()
	if ctx == nil {
		return logger
	}
	attrs := make([]any, 0, 4)
	if id := RequestID(ctx); id != "" {
		attrs = append(attrs, slog.String("request_id", id))
	}
	uc := account.FromContext(ctx)
	if uc.UserID != "" {
		attrs = append(attrs, slog.String("user_id", uc.UserID))
	}
	if uc.Email != "" {
		// P3.D (2026-06-22 audit): hash the email so Cloud Logging
		// doesn't retain PII across every authenticated request.
		// 8-char hex prefix is enough to disambiguate users in a
		// log search; collisions are ~1 in 4B, irrelevant at this
		// scale.
		attrs = append(attrs, slog.String("email_hash", HashEmail(uc.Email)))
	}
	if uc.IsSuperadmin {
		attrs = append(attrs, slog.Bool("is_superadmin", true))
	}
	if len(attrs) == 0 {
		return logger
	}
	return logger.With(attrs...)
}

// HashEmail returns the first 8 hex chars of SHA-256(email) as a
// privacy-preserving correlation key for structured logs. See the
// FromContext doc for rationale.
func HashEmail(email string) string {
	sum := sha256.Sum256([]byte(email))
	return hex.EncodeToString(sum[:4])
}
