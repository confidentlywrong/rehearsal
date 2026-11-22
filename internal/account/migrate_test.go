// Copyright 2026 Christian Haeussinger. Apache-2.0.

package account

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// fakeMigDB records every SQL string it is asked to execute. Used to
// verify both the order of migration files AND that the advisory
// lock pair + schema_migrations tracking surround the batch.
type fakeMigDB struct {
	calls           []string
	appliedVersions map[string]bool
	queryErr        error
	// failOn, when non-empty, causes Exec to return an error the
	// first time a query containing this substring is executed.
	failOn string
}

func (f *fakeMigDB) Exec(_ context.Context, sql string, _ ...any) (any, error) {
	f.calls = append(f.calls, sql)
	if f.failOn != "" && strings.Contains(sql, f.failOn) {
		return nil, errors.New("simulated exec failure")
	}
	return nil, nil
}

func (f *fakeMigDB) QueryVersions(_ context.Context) (map[string]bool, error) {
	f.calls = append(f.calls, "SELECT version FROM schema_migrations")
	if f.queryErr != nil {
		return nil, f.queryErr
	}
	out := make(map[string]bool, len(f.appliedVersions))
	for k, v := range f.appliedVersions {
		out[k] = v
	}
	return out, nil
}

// writeFile is a tiny helper used by these tests to lay down a
// throwaway migration file.
func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func TestApplyMigrationFiles_AppliesInOrder(t *testing.T) {
	dir := t.TempDir()
	// Deliberately create them in non-sorted order on disk so we
	// catch any code path that returns directory order rather than
	// lexical order.
	writeFile(t, filepath.Join(dir, "0002_b.sql"), "SELECT 2;")
	writeFile(t, filepath.Join(dir, "0001_a.sql"), "SELECT 1;")
	writeFile(t, filepath.Join(dir, "0003_c.sql"), "SELECT 3;")
	// Non-SQL file should be ignored.
	writeFile(t, filepath.Join(dir, "README.md"), "ignored")

	t.Setenv(MigrationsDirEnv, dir)
	t.Setenv(MigrationFileEnv, "")

	paths, err := migrationFilePaths()
	if err != nil {
		t.Fatalf("migrationFilePaths: %v", err)
	}
	if len(paths) != 3 {
		t.Fatalf("expected 3 paths, got %d: %v", len(paths), paths)
	}
	for i, suffix := range []string{"0001_a.sql", "0002_b.sql", "0003_c.sql"} {
		if !strings.HasSuffix(paths[i], suffix) {
			t.Fatalf("paths[%d]=%q does not end in %q", i, paths[i], suffix)
		}
	}

	db := &fakeMigDB{}
	if err := applyMigrationFiles(context.Background(), db, paths); err != nil {
		t.Fatalf("applyMigrationFiles: %v", err)
	}
	// Expected calls: lock, create schema_migrations, select versions,
	// (apply + insert version) x 3, unlock = 10 calls.
	if len(db.calls) != 10 {
		t.Fatalf("expected 10 calls, got %d: %v", len(db.calls), db.calls)
	}
	if !strings.Contains(db.calls[0], "pg_advisory_lock") {
		t.Fatalf("first call should be advisory lock, got %q", db.calls[0])
	}
	if !strings.Contains(db.calls[1], "CREATE TABLE IF NOT EXISTS schema_migrations") {
		t.Fatalf("second call should create schema_migrations, got %q", db.calls[1])
	}
	if db.calls[2] != "SELECT version FROM schema_migrations" {
		t.Fatalf("third call should query schema_migrations, got %q", db.calls[2])
	}
	if db.calls[3] != "SELECT 1;" || db.calls[5] != "SELECT 2;" || db.calls[7] != "SELECT 3;" {
		t.Fatalf("migrations applied out of order: %v", db.calls)
	}
	if !strings.Contains(db.calls[9], "pg_advisory_unlock") {
		t.Fatalf("last call should be advisory unlock, got %q", db.calls[9])
	}
}

func TestApplyMigrationFiles_SingleFileOverride(t *testing.T) {
	// MigrationFileEnv pins exactly one file; the directory contents
	// must be ignored. This is the test-fixture escape hatch.
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "0001_a.sql"), "SELECT 1;")
	writeFile(t, filepath.Join(dir, "0002_b.sql"), "SELECT 2;")
	loneFile := filepath.Join(dir, "0001_a.sql")

	t.Setenv(MigrationsDirEnv, dir)
	t.Setenv(MigrationFileEnv, loneFile)

	paths, err := migrationFilePaths()
	if err != nil {
		t.Fatalf("migrationFilePaths: %v", err)
	}
	if len(paths) != 1 || paths[0] != loneFile {
		t.Fatalf("single-file override broken: %v", paths)
	}
}

func TestApplyMigrationFiles_EmptyDirIsError(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(MigrationsDirEnv, dir)
	t.Setenv(MigrationFileEnv, "")
	if _, err := migrationFilePaths(); err == nil {
		t.Fatal("expected error for empty migrations dir, got nil")
	}
}

func TestApplyMigrationFiles_NilDB(t *testing.T) {
	if err := applyMigrationFiles(context.Background(), nil, []string{"x.sql"}); err == nil {
		t.Fatal("expected error for nil db")
	}
}

func TestApplyMigrationFiles_NoPathsIsError(t *testing.T) {
	if err := applyMigrationFiles(context.Background(), &fakeMigDB{}, nil); err == nil {
		t.Fatal("expected error for empty paths slice")
	}
}

func TestApplyMigrationFiles_StopsOnFirstFailure(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "0001_a.sql"), "SELECT 1; -- ok")
	writeFile(t, filepath.Join(dir, "0002_b.sql"), "SELECT 2; -- FAIL_HERE")
	writeFile(t, filepath.Join(dir, "0003_c.sql"), "SELECT 3;")
	t.Setenv(MigrationsDirEnv, dir)
	t.Setenv(MigrationFileEnv, "")

	paths, err := migrationFilePaths()
	if err != nil {
		t.Fatalf("paths: %v", err)
	}
	db := &fakeMigDB{failOn: "FAIL_HERE"}
	err = applyMigrationFiles(context.Background(), db, paths)
	if err == nil {
		t.Fatal("expected error from failing migration, got nil")
	}
	// Calls should be: lock, 0001 (ok), 0002 (fails), unlock.
	// 0003 must NOT have been attempted.
	for _, c := range db.calls {
		if strings.Contains(c, "SELECT 3;") {
			t.Fatalf("0003 should not have been applied after 0002 failed: %v", db.calls)
		}
	}
}

func TestApplyMigrationFiles_RealFilesInRepoDir(t *testing.T) {
	// Smoke test: when run inside the account package, the default
	// directory resolution must find the real migrations/ folder
	// containing at least 0001_users.sql and 0002_analyses.sql.
	t.Setenv(MigrationsDirEnv, "")
	t.Setenv(MigrationFileEnv, "")
	paths, err := migrationFilePaths()
	if err != nil {
		t.Fatalf("migrationFilePaths against real repo: %v", err)
	}
	var has1, has2 bool
	for _, p := range paths {
		if strings.HasSuffix(p, "0001_users.sql") {
			has1 = true
		}
		if strings.HasSuffix(p, "0002_analyses.sql") {
			has2 = true
		}
	}
	if !has1 || !has2 {
		t.Fatalf("expected both 0001_users.sql and 0002_analyses.sql in resolved paths, got %v", paths)
	}
}

func TestApplyMigrationFiles_SkipsAlreadyAppliedVersions(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "0001_a.sql"), "SELECT 1;")
	writeFile(t, filepath.Join(dir, "0002_b.sql"), "SELECT 2;")
	writeFile(t, filepath.Join(dir, "0003_c.sql"), "SELECT 3;")
	t.Setenv(MigrationsDirEnv, dir)
	t.Setenv(MigrationFileEnv, "")

	paths, err := migrationFilePaths()
	if err != nil {
		t.Fatalf("paths: %v", err)
	}
	db := &fakeMigDB{
		appliedVersions: map[string]bool{
			"0001_a.sql": true,
			"0002_b.sql": true,
		},
	}
	if err := applyMigrationFiles(context.Background(), db, paths); err != nil {
		t.Fatalf("applyMigrationFiles: %v", err)
	}
	for _, c := range db.calls {
		if c == "SELECT 1;" || c == "SELECT 2;" {
			t.Fatalf("already-applied migration should have been skipped: %v", db.calls)
		}
	}
	var ran3 bool
	for _, c := range db.calls {
		if c == "SELECT 3;" {
			ran3 = true
		}
	}
	if !ran3 {
		t.Fatalf("expected pending 0003_c.sql to execute: %v", db.calls)
	}
}

func TestApplyMigrationFiles_FailsClosedOnLockError(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "0001_a.sql"), "SELECT 1;")
	db := &fakeMigDB{failOn: "pg_advisory_lock"}
	err := applyMigrationFiles(context.Background(), db, []string{filepath.Join(dir, "0001_a.sql")})
	if err == nil || !strings.Contains(err.Error(), "acquire advisory lock") {
		t.Fatalf("expected fail-closed advisory lock error, got %v", err)
	}
	if len(db.calls) != 1 {
		t.Fatalf("no DDL/migration should run when lock acquisition fails: %v", db.calls)
	}
}

func TestApplyMigrationFiles_FailedMigrationNotRecorded(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "0001_a.sql"), "SELECT 1;")
	writeFile(t, filepath.Join(dir, "0002_b.sql"), "SELECT 2; -- FAIL_HERE")
	db := &fakeMigDB{failOn: "FAIL_HERE"}
	err := applyMigrationFiles(context.Background(), db, []string{
		filepath.Join(dir, "0001_a.sql"),
		filepath.Join(dir, "0002_b.sql"),
	})
	if err == nil {
		t.Fatal("expected error when 0002 fails")
	}
	insertCount := 0
	for _, c := range db.calls {
		if strings.Contains(c, "INSERT INTO schema_migrations") {
			insertCount++
		}
	}
	if insertCount != 1 {
		t.Fatalf("expected exactly 1 version insert (for 0001 only), got %d (%v)", insertCount, db.calls)
	}
	if !strings.Contains(db.calls[len(db.calls)-1], "pg_advisory_unlock") {
		t.Fatalf("expected advisory unlock even on failure, got %q", db.calls[len(db.calls)-1])
	}
}

func TestApplyMigrationFiles_QueryAndRecordAndUnlockErrors(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "0001_a.sql")
	writeFile(t, p, "SELECT 1;")

	err := applyMigrationFiles(context.Background(), &fakeMigDB{queryErr: errors.New("db query fail")}, []string{p})
	if err == nil || !strings.Contains(err.Error(), "query schema_migrations") {
		t.Fatalf("expected QueryVersions error, got %v", err)
	}

	err = applyMigrationFiles(context.Background(), &fakeMigDB{failOn: "INSERT INTO schema_migrations"}, []string{p})
	if err == nil || !strings.Contains(err.Error(), "record") {
		t.Fatalf("expected record error, got %v", err)
	}

	err = applyMigrationFiles(context.Background(), &fakeMigDB{failOn: "pg_advisory_unlock"}, []string{p})
	if !errors.Is(err, errMigrationUnlockFailed) {
		t.Fatalf("expected errMigrationUnlockFailed when body succeeds, got %v", err)
	}
}

func TestApplyMigrationFiles_WhitespaceOnlyFileRejected(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "0001_blank.sql")
	writeFile(t, p, "   \n\t  ")
	db := &fakeMigDB{}
	err := applyMigrationFiles(context.Background(), db, []string{p})
	if err == nil || !strings.Contains(err.Error(), "is empty") {
		t.Fatalf("expected empty-file error for whitespace-only SQL, got %v", err)
	}
}

func TestApplyMigration_LivePostgresIntegration(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping live Postgres migration test in -short mode")
	}
	dsn := os.Getenv("TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("TEST_POSTGRES_DSN not set")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("pgxpool.New: %v", err)
	}
	defer pool.Close()

	dir := t.TempDir()
	// Non-idempotent CREATE TABLE: second ApplyMigration call only succeeds if schema_migrations skips it.
	writeFile(t, filepath.Join(dir, "0001_m1_it_init.sql"), "CREATE TABLE m1_it_probe (id INT PRIMARY KEY);")
	t.Setenv(MigrationsDirEnv, dir)
	t.Setenv(MigrationFileEnv, "")

	_, _ = pool.Exec(ctx, "DROP TABLE IF EXISTS m1_it_probe;")
	_, _ = pool.Exec(ctx, "DELETE FROM schema_migrations WHERE version = '0001_m1_it_init.sql';")
	defer func() {
		_, _ = pool.Exec(ctx, "DROP TABLE IF EXISTS m1_it_probe;")
		_, _ = pool.Exec(ctx, "DELETE FROM schema_migrations WHERE version = '0001_m1_it_init.sql';")
	}()

	if err := ApplyMigration(ctx, pool); err != nil {
		t.Fatalf("first ApplyMigration failed: %v", err)
	}
	if err := ApplyMigration(ctx, pool); err != nil {
		t.Fatalf("second ApplyMigration should skip applied version cleanly, got: %v", err)
	}
}
