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

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
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
// Exec and QueryVersions.
type migrationDB interface {
	Exec(ctx context.Context, sql string, args ...any) (any, error)
	QueryVersions(ctx context.Context) (map[string]bool, error)
}

var errMigrationUnlockFailed = errors.New("advisory unlock failed")

const createSchemaMigrationsSQL = `CREATE TABLE IF NOT EXISTS schema_migrations (
	version    TEXT        PRIMARY KEY,
	applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);`

// pgxExecAdapter wraps a PgxConn to fit migrationDB. The adapter
// drops the concrete pgconn.CommandTag return value (callers here
// only ever inspect err).
type pgxExecAdapter struct{ inner PgxConn }

func (a pgxExecAdapter) Exec(ctx context.Context, sql string, args ...any) (any, error) {
	tag, err := a.inner.Exec(ctx, sql, args...)
	return tag, err
}

func (a pgxExecAdapter) QueryVersions(ctx context.Context) (map[string]bool, error) {
	rows, err := a.inner.Query(ctx, "SELECT version FROM schema_migrations")
	if err != nil {
		return nil, err
	}
	if rows == nil {
		return map[string]bool{}, nil
	}
	defer rows.Close()
	applied := make(map[string]bool)
	for rows.Next() {
		var version string
		if err := rows.Scan(&version); err != nil {
			return nil, err
		}
		applied[version] = true
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return applied, nil
}

// ApplyMigration discovers every migrations/*.sql file in lexical
// order, ensures schema_migrations exists, skips already-recorded
// versions, and applies pending migrations under a fail-closed
// PostgreSQL advisory lock. Called from every service's main() right
// after connecting.
func ApplyMigration(ctx context.Context, db PgxConn) error {
	if db == nil {
		return errors.New("account: ApplyMigration: nil db")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	paths, err := migrationFilePaths()
	if err != nil {
		return fmt.Errorf("account: ApplyMigration: %w", err)
	}
	// Session-level pg_advisory_lock requires connection affinity across lock,
	// DDL statements, and unlock. If a caller passes a *pgxpool.Pool directly,
	// acquire and pin a single pooled connection for the entire migration run.
	switch v := db.(type) {
	case *pgx.Conn:
		if v == nil {
			return errors.New("account: ApplyMigration: nil db")
		}
	case *pgxpool.Pool:
		if v == nil {
			return errors.New("account: ApplyMigration: nil db")
		}
		conn, aerr := v.Acquire(ctx)
		if aerr != nil {
			return fmt.Errorf("account: ApplyMigration: acquire conn: %w", aerr)
		}
		var connBad bool
		defer func() {
			if connBad {
				cctx, ccancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer ccancel()
				_ = conn.Conn().Close(cctx)
			}
			conn.Release()
		}()
		err := applyMigrationFiles(ctx, pgxExecAdapter{inner: conn}, paths)
		if errors.Is(err, errMigrationUnlockFailed) {
			connBad = true
		}
		return err
	}
	return applyMigrationFiles(ctx, pgxExecAdapter{inner: db}, paths)
}

// applyMigrationFiles is the testable inner of ApplyMigration. Tests
// call it directly with a fake migrationDB. The advisory lock is
// acquired fail-closed once for the whole batch so all files apply
// atomically w.r.t. other booting services.
func applyMigrationFiles(ctx context.Context, db migrationDB, paths []string) (retErr error) {
	if db == nil {
		return errors.New("account: applyMigrationFiles: nil db")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if len(paths) == 0 {
		return errors.New("account: applyMigrationFiles: no migration files found")
	}

	// Acquire a project-wide advisory lock fail-closed. Concurrent cold
	// starts must serialize so schema_migrations and non-idempotent DDL
	// cannot race.
	if _, err := db.Exec(ctx, fmt.Sprintf("SELECT pg_advisory_lock(%d)", migrationLockKey)); err != nil {
		return fmt.Errorf("account: acquire advisory lock: %w", err)
	}
	defer func() {
		// Unlock on a fresh context so a cancelled parent
		// doesn't leave the lock held until session close.
		uctx, ucancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer ucancel()
		if _, uerr := db.Exec(uctx, fmt.Sprintf("SELECT pg_advisory_unlock(%d)", migrationLockKey)); uerr != nil {
			log.Printf("ACCOUNT MIGRATION: advisory unlock failed (%v)", uerr)
			unlockErr := fmt.Errorf("account: %w: %v", errMigrationUnlockFailed, uerr)
			if retErr == nil {
				retErr = unlockErr
			} else {
				retErr = errors.Join(retErr, unlockErr)
			}
		}
	}()

	if _, err := db.Exec(ctx, createSchemaMigrationsSQL); err != nil {
		return fmt.Errorf("account: create schema_migrations: %w", err)
	}
	applied, err := db.QueryVersions(ctx)
	if err != nil {
		return fmt.Errorf("account: query schema_migrations: %w", err)
	}

	for _, path := range paths {
		version := filepath.Base(path)
		if applied[version] {
			log.Printf("ACCOUNT MIGRATION: skipping already-applied %s", path)
			continue
		}
		sqlBytes, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("account: read %s: %w", path, err)
		}
		if strings.TrimSpace(string(sqlBytes)) == "" {
			return fmt.Errorf("account: %s is empty", path)
		}
		if _, err := db.Exec(ctx, string(sqlBytes)); err != nil {
			return fmt.Errorf("account: apply %s: %w", path, err)
		}
		if _, err := db.Exec(
			ctx,
			"INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT (version) DO NOTHING",
			version,
		); err != nil {
			return fmt.Errorf("account: record %s in schema_migrations: %w", path, err)
		}
		applied[version] = true
		log.Printf("ACCOUNT MIGRATION: applied %s (advisory lock held)", path)
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
