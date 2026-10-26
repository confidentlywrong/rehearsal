# migrations/

SQL migrations applied automatically on cold start by every Go service
that calls `internal/account.ApplyMigration` (gateway, admin, billing).
Each file is wrapped in `BEGIN; ... COMMIT;` and uses idempotent DDL
(`CREATE ... IF NOT EXISTS`, seed `INSERT ... WHERE NOT EXISTS`) so
re-running on every boot is safe.

## Files

- **`0001_users.sql`** — identity, billing, plans, quotas, usage
  ledger + monthly rollup, audit log, processed webhook events, and
  the manual-approval flow (`users.status` / `approved_at` /
  `approved_by`).

- **`0002_analyses.sql`** — Organizational Memory & Learning v1.
  Tables `analyses`, `analysis_documents`, `analysis_nodes`,
  `analysis_edges`. Postgres is the canonical store; the optional
  Neo4j projection is derived and rebuildable.

- **`0003_node_fts.sql`** — Phase 3 retrieval support. Adds a
  generated `tsvector` column (`analysis_nodes.search_vector`) over
  the well-known JSONB payload fields plus a partial GIN index for
  live, accepted nodes. Consumed by `internal/memory/retrieval`.

## Conventions

- One file per logical change, prefixed with a zero-padded sequence
  number (`0001_`, `0002_`, …). Files are applied in **lexical
  order** by `internal/account/migrate.go`.
- Every migration is wrapped in `BEGIN; ... COMMIT;`.
- Every `CREATE TABLE` uses `IF NOT EXISTS`.
- Every seed `INSERT` is guarded by `WHERE NOT EXISTS` or `ON
  CONFLICT DO NOTHING` so re-applying is a no-op.
- Multi-tenant rows carry the tenant key as a column and have an
  index on it (e.g. `analysis_id` on the memory tables).
- We do **not** track applied migrations in a versions table yet;
  the idempotency contract is what keeps re-application safe. Add a
  proper runner when a non-idempotent migration is needed.

## Environment overrides (for tests / local dev)

- `USERS_MIGRATIONS_DIR` — override the directory globbed for `*.sql`
  files. Default: walk up from cwd looking for `migrations/`.
- `USERS_MIGRATION_PATH` — pin a single file to apply, ignoring the
  directory. Test-fixture escape hatch.

## Manual apply (dev only)

```bash
docker compose up -d postgres
psql "$DATABASE_URL" -f migrations/0001_users.sql
psql "$DATABASE_URL" -f migrations/0002_analyses.sql
psql "$DATABASE_URL" -f migrations/0003_node_fts.sql
```
