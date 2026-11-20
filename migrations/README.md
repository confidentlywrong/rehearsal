# migrations/

SQL migrations applied automatically on cold start by every Go service
that calls `internal/account.ApplyMigration` (gateway, admin, billing,
frontend). Each file is wrapped in `BEGIN; ... COMMIT;` and uses
idempotent DDL (`CREATE ... IF NOT EXISTS`, seed `INSERT ... WHERE NOT
EXISTS`) so re-running on every boot is safe. Applied filenames are
recorded in `schema_migrations`.

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

- **`0004_analysis_proposals.sql`** — Phase 5/6 reflection diff
  proposals (`analysis_proposals` table and indexes).

- **`0005_analyses_org_name_nullable.sql`** — Makes
  `analyses.org_name` nullable to support single-user memory workflows.

- **`0006_document_chunks.sql`** — Chunked document storage and
  3072-dim `VECTOR` embeddings (`document_chunks`) for leadership RAG.

- **`0007_drop_legacy_tables.sql`** — Drops legacy
  `analysis_proposals`, `analysis_edges`, and `analysis_nodes` tables
  after Stage 2C transition.

## Conventions

- One file per logical change, prefixed with a zero-padded sequence
  number (`0001_`, `0002_`, …). Files are applied in **lexical
  order** by `internal/account/migrate.go` under a fail-closed
  PostgreSQL session advisory lock (`pg_advisory_lock`).
- Every migration is wrapped in `BEGIN; ... COMMIT;`.
- Every `CREATE TABLE` uses `IF NOT EXISTS`.
- Every seed `INSERT` is guarded by `WHERE NOT EXISTS` or `ON
  CONFLICT DO NOTHING` so re-applying is a no-op.
- Multi-tenant rows carry the tenant key as a column and have an
  index on it (e.g. `analysis_id` on the memory tables).
- Applied filenames are tracked in `schema_migrations (version,
  applied_at)` so already-applied migrations are skipped on boot.

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
