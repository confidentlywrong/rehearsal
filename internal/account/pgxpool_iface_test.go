// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.
//
// Audit P1 #1 (2026-06-21): cmd/{gateway,admin,billing,frontend}/main.go
// swap their runtime DB handle from *pgx.Conn (NOT goroutine-safe) to
// *pgxpool.Pool. The Store accepts any PgxConn-satisfying value, so
// this file just locks in — at compile time — that *pgxpool.Pool
// continues to satisfy the interface. If a future pgx release ever
// drops one of these methods from pgxpool.Pool, `go build ./...` will
// fail HERE with a clear message instead of at the four main.go call
// sites with a confusing one.
//
// There is no runtime DB in this test. We never call pgxpool.New; the
// nil-typed pointer is sufficient to drive the compiler's interface
// check.

package account

import (
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Compile-time assertions. If either of these fails to build, the
// pgxpool swap in cmd/*/main.go cannot work and the audit fix has
// regressed.
var (
	_ PgxConn = (*pgxpool.Pool)(nil)
	_ PgxConn = (*pgx.Conn)(nil)
)

// TestPgxPoolSatisfiesPgxConn is a no-op runtime test that exists so
// `go test ./internal/account/...` reports the assertion's package by
// name in CI output. The real check is the compile-time `var _ =`
// above; running this just confirms the file is wired into the test
// binary.
func TestPgxPoolSatisfiesPgxConn(t *testing.T) {
	// Intentionally empty. See compile-time assertions above.
	_ = (*pgxpool.Pool)(nil)
}
