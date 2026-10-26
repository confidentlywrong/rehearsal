// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.
//
// Compile-time conformance: locks in that the real pgx types satisfy
// memory.PgxConn, so swapping the runtime DB handle in main.go does
// not silently break.

package memory

import (
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	_ PgxConn = (*pgxpool.Pool)(nil)
	_ PgxConn = (*pgx.Conn)(nil)
)

func TestPgxPoolSatisfiesPgxConn(t *testing.T) {
	// Intentionally empty. Real check is the compile-time `var _ =`
	// assertions above.
	_ = (*pgxpool.Pool)(nil)
}
