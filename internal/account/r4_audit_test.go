// Copyright 2026 Christian Haeussinger
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

package account

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type r4MigRecord struct {
	sql         string
	ctxErr      error
	hasDeadline bool
}

type r4FakeMigConn struct {
	records   []r4MigRecord
	onSQLHook func(sql string)
	failSQL   string
}

func (f *r4FakeMigConn) Exec(ctx context.Context, sql string, _ ...any) (pgconn.CommandTag, error) {
	_, hasDeadline := ctx.Deadline()
	f.records = append(f.records, r4MigRecord{
		sql:         sql,
		ctxErr:      ctx.Err(),
		hasDeadline: hasDeadline,
	})
	if f.onSQLHook != nil {
		f.onSQLHook(sql)
	}
	if f.failSQL != "" && strings.Contains(sql, f.failSQL) {
		return pgconn.CommandTag{}, errors.New("boom")
	}
	return pgconn.CommandTag{}, nil
}

func (f *r4FakeMigConn) Query(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
	return nil, nil
}

func (f *r4FakeMigConn) QueryRow(_ context.Context, _ string, _ ...any) pgx.Row {
	return nil
}

func TestR4_ApplyMigration_NilAndTypedNilChecks(t *testing.T) {
	if err := ApplyMigration(context.Background(), nil); err == nil {
		t.Fatal("expected error for nil PgxConn")
	}
	var nilConn *pgx.Conn
	if err := ApplyMigration(context.Background(), nilConn); err == nil {
		t.Fatal("expected error for typed-nil *pgx.Conn")
	}
	var nilPool *pgxpool.Pool
	if err := ApplyMigration(context.Background(), nilPool); err == nil {
		t.Fatal("expected error for typed-nil *pgxpool.Pool")
	}
}

func TestR4_ApplyMigration_UnlockUsesNonCancelledBoundedContext(t *testing.T) {
	tmp := t.TempDir()
	migFile := filepath.Join(tmp, "0001_test.sql")
	if err := os.WriteFile(migFile, []byte("CREATE TABLE IF NOT EXISTS t(id INT);"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(MigrationFileEnv, migFile)

	ctx, cancel := context.WithCancel(context.Background())
	fake := &r4FakeMigConn{
		onSQLHook: func(sql string) {
			if strings.Contains(sql, "CREATE TABLE") {
				// Simulate caller ctx cancellation mid-migration.
				cancel()
			}
		},
		failSQL: "CREATE TABLE",
	}

	err := ApplyMigration(ctx, fake)
	if err == nil {
		t.Fatal("expected migration error on CREATE TABLE failure")
	}
	if len(fake.records) < 3 {
		t.Fatalf("expected lock + body + unlock calls, got %d (%+v)", len(fake.records), fake.records)
	}
	unlock := fake.records[len(fake.records)-1]
	if !strings.Contains(unlock.sql, "pg_advisory_unlock") {
		t.Fatalf("expected final call to be pg_advisory_unlock, got %q", unlock.sql)
	}
	if unlock.ctxErr != nil {
		t.Fatalf("pg_advisory_unlock must receive non-cancelled context, got ctxErr=%v", unlock.ctxErr)
	}
	if !unlock.hasDeadline {
		t.Fatal("pg_advisory_unlock context must carry a bounded timeout deadline")
	}
}

func TestR4_ApplyMigration_PoolAcquiresPinnedConnRespectingContext(t *testing.T) {
	cfg, err := pgxpool.ParseConfig("postgres://user:pass@127.0.0.1:1/testdb?sslmode=disable")
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatalf("NewWithConfig: %v", err)
	}
	defer pool.Close()

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()

	err = ApplyMigration(cancelled, pool)
	if err == nil || !strings.Contains(err.Error(), "acquire conn") {
		t.Fatalf("expected pool acquire failure before executing lock/DDL across pool, got %v", err)
	}
}
