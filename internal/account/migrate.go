// Copyright 2026 Christian Haeussinger. Apache-2.0.

package account

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// MigrationFileEnv overrides the path to a SINGLE migration file.
// When set, only that one file is applied. Useful for tests that want
// to load a narrow fixture. When unset, the runner discovers every
// *.sql file under the migrations directory (see MigrationsDirEnv)
// and applies them in lexical order — 0001_users.sql, 0002_analyses.sql,
// and so on.
const MigrationFileEnv = "USERS_MIGRATION_PATH"

// MigrationsDirEnv overrides the directory the runner globs for *.sql
// files. When unset, the runner walks up from the current working
// directory looking for a `migrations/` folder (works for both
// services started from repo root and tests started from a package
// subdirectory).
const MigrationsDirEnv = "USERS_MIGRATIONS_DIR"

const defaultMigrationsDir = "migrations"

// migrationLockKey is the bigint passed to pg_advisory_lock so every
// service (gateway, admin, billing) that calls ApplyMigration on
// cold-start competes for the same lock and serializes the seed
// INSERTs. 0x55534552 spells "USER" in ASCII for greppability.
const migrationLockKey int64 = 0x55534552

// migrationDB is the minimal pgx surface ApplyMigration needs.
// Smaller than PgxConn so tests can pass a fake that only implements
// Exec.
type migrationDB interface {
	Exec(ctx context.Context, sql string, args ...any) (any, error)
}

// pgxExecAdapter wraps a PgxConn to fit migrationDB. The adapter
// drops the concrete pgconn.CommandTag return value (callers here
// only ever inspect err).
type pgxExecAdapter struct{ inner PgxConn }

func (a pgxExecAdapter) Exec(ctx context.Context, sql string, args ...any) (any, error) {
	tag, err := a.inner.Exec(ctx, sql, args...)
	return tag, err
}

// ApplyMigration discovers every migrations/*.sql file in lexical
// order and applies each in turn. Called from every service's main()
// right after pgx.Connect. The SQL in every file is idempotent
// (CREATE ... IF NOT EXISTS + INSERT ... WHERE NOT EXISTS) so
// re-running on every boot is safe.
//
// We still do NOT track applied migrations in a separate table.
// Lexical ordering + idempotent statements is the v1 contract; if a
// future migration needs to be non-idempotent (rare) we'll add a
// real runner with a versions table then.
func ApplyMigration(ctx context.Context, db PgxConn) error {
	if db == nil {
		return errors.New("account: ApplyMigration: nil db")
	}
	paths, err := migrationFilePaths()
	if err != nil {
		return fmt.Errorf("account: ApplyMigration: %w", err)
	}
	return applyMigrationFiles(ctx, pgxExecAdapter{inner: db}, paths)
}

// applyMigrationFiles is the testable inner of ApplyMigration. Tests
// call it directly with a fake migrationDB. The advisory lock is
// acquired once for the whole batch so all files apply atomically
// w.r.t. other booting services.
func applyMigrationFiles(ctx context.Context, db migrationDB, paths []string) error {
	if db == nil {
		return errors.New("account: applyMigrationFiles: nil db")
	}
	if len(paths) == 0 {
		return errors.New("account: applyMigrationFiles: no migration files found")
	}

	// Acquire a project-wide advisory lock. Best-effort: if the call
	// returns an error (DB hiccup, test fake doesn't implement
	// SELECT) we log and continue — the migrations' CREATEs are
	// already IF NOT EXISTS and seeds are guarded by NOT EXISTS, so
	// the worst case without the lock is a one-time PK collision on
	// the very first concurrent boot.
	lockHeld := false
	if _, err := db.Exec(ctx, fmt.Sprintf("SELECT pg_advisory_lock(%d)", migrationLockKey)); err != nil {
		log.Printf("ACCOUNT MIGRATION: advisory lock unavailable (%v); proceeding without serialization", err)
	} else {
		lockHeld = true
		defer func() {
			// Unlock on a fresh context so a cancelled parent
			// doesn't leave the lock held until session close.
			uctx, ucancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer ucancel()
			if _, uerr := db.Exec(uctx, fmt.Sprintf("SELECT pg_advisory_unlock(%d)", migrationLockKey)); uerr != nil {
				log.Printf("ACCOUNT MIGRATION: advisory unlock failed (%v); will release on session close", uerr)
			}
		}()
	}

	for _, path := range paths {
		sql, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("account: read %s: %w", path, err)
		}
		if len(sql) == 0 {
			return fmt.Errorf("account: %s is empty", path)
		}
		if _, err := db.Exec(ctx, string(sql)); err != nil {
			return fmt.Errorf("account: apply %s: %w", path, err)
		}
		if lockHeld {
			log.Printf("ACCOUNT MIGRATION: applied %s (advisory lock held)", path)
		} else {
			log.Printf("ACCOUNT MIGRATION: applied %s", path)
		}
	}
	return nil
}

// migrationFilePaths resolves the list of *.sql files to apply, in
// lexical order. Single-file override via MigrationFileEnv beats
// directory discovery.
func migrationFilePaths() ([]string, error) {
	if v := os.Getenv(MigrationFileEnv); v != "" {
		return []string{v}, nil
	}
	dir := migrationsDir()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read migrations dir %q: %w", dir, err)
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		if !strings.HasSuffix(name, ".sql") {
			continue
		}
		out = append(out, filepath.Join(dir, name))
	}
	sort.Strings(out)
	if len(out) == 0 {
		return nil, fmt.Errorf("no *.sql files in migrations dir %q", dir)
	}
	return out, nil
}

// migrationsDir resolves the directory holding *.sql files. Honors
// MigrationsDirEnv, otherwise walks up looking for `migrations/`.
func migrationsDir() string {
	if v := os.Getenv(MigrationsDirEnv); v != "" {
		return v
	}
	for _, c := range []string{
		defaultMigrationsDir,
		filepath.Join("..", defaultMigrationsDir),
		filepath.Join("..", "..", defaultMigrationsDir),
	} {
		if info, err := os.Stat(c); err == nil && info.IsDir() {
			return c
		}
	}
	return defaultMigrationsDir
}
