// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.
//
// Audit P1 #1 (2026-06-21): cmd/{gateway,admin,billing}/main.go swap
// their runtime DB handle from *pgx.Conn (NOT goroutine-safe) to
// *pgxpool.Pool. Two billing-side interfaces depend on this:
//
//   - billing.PgxConn   (Exec/Query/QueryRow) — used by *billing.Store
//   - billing.pgxExecer (Exec only)            — used by *PostgresEventDedup
//
// This file locks in — at compile time — that *pgxpool.Pool continues
// to satisfy both. The pgxExecer assertion lives in this package
// because pgxExecer is unexported.
//
// There is no runtime DB in this test. We never call pgxpool.New; the
// nil-typed pointer is sufficient to drive the compiler's interface
// check.

package billing

import (
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Compile-time assertions. If any of these fails to build, the
// pgxpool swap in cmd/*/main.go cannot work and the audit fix has
// regressed.
var (
	_ PgxConn   = (*pgxpool.Pool)(nil)
	_ PgxConn   = (*pgx.Conn)(nil)
	_ pgxExecer = (*pgxpool.Pool)(nil)
	_ pgxExecer = (*pgx.Conn)(nil)
)

// TestPgxPoolSatisfiesBillingInterfaces is a no-op runtime test so
// `go test ./internal/billing/...` reports the assertion's package by
// name in CI output. The real check is the compile-time `var _ =`
// above; running this just confirms the file is wired into the test
// binary.
func TestPgxPoolSatisfiesBillingInterfaces(t *testing.T) {
	// Intentionally empty. See compile-time assertions above.
	_ = (*pgxpool.Pool)(nil)
}
