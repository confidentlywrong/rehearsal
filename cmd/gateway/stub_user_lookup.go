// Copyright 2026 Christian Haeussinger. Apache-2.0.

package main

// Minimal fallback used when accountStoreForMe is nil at boot
// (e.g. degraded mode where Postgres is unreachable). Keeps the
// billing routes registered with a UserLookup that always reports
// account.ErrNotFound, so /api/v1/billing/{checkout,portal} return
// 404 instead of 500-ing on a nil-pointer dereference.

import (
	"context"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

type stubUserLookup struct{}

func (stubUserLookup) GetUser(_ context.Context, _ string) (account.User, error) {
	return account.User{}, account.ErrNotFound
}
