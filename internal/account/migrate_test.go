// Copyright 2026 Christian Haeussinger. Apache-2.0.

package account

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeMigDB records every SQL string it is asked to execute. Used to
// verify both the order of migration files AND that the advisory
// lock pair surrounds the batch.
type fakeMigDB struct {
	calls []string
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
	// Expected calls: pg_advisory_lock, then the three migrations in
	// order, then pg_advisory_unlock.
	if len(db.calls) != 5 {
		t.Fatalf("expected 5 Exec calls (lock + 3 migrations + unlock), got %d: %v", len(db.calls), db.calls)
	}
	if !strings.Contains(db.calls[0], "pg_advisory_lock") {
		t.Fatalf("first call should be advisory lock, got %q", db.calls[0])
	}
	if db.calls[1] != "SELECT 1;" || db.calls[2] != "SELECT 2;" || db.calls[3] != "SELECT 3;" {
		t.Fatalf("migrations applied out of order: %v", db.calls[1:4])
	}
	if !strings.Contains(db.calls[4], "pg_advisory_unlock") {
		t.Fatalf("last call should be advisory unlock, got %q", db.calls[4])
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
