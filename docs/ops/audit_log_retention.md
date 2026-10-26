# Audit Log Retention (manual sweep)

The `audit_log` table records every privileged user action
(`account.RecordAudit` callers — logout, plan changes, GDPR
export/delete, payment events, simulation spawn rejections, etc.).
It has **no automatic retention sweep**. This is intentional — see
the hard rules in [README.md](README.md): no scheduled jobs.

This runbook documents the manual sweep an operator runs when the
table gets too large for admin "user activity" queries to stay
fast, or when the compliance team signs off on a retention horizon
(default suggestion: 180 days).

## When to sweep

Run a sweep when ANY of the following is true:

- `SELECT pg_size_pretty(pg_total_relation_size('audit_log'));` is
  above ~1 GB and the admin Users tab "Recent activity" panel
  starts feeling sluggish.
- A compliance review has set a retention policy (GDPR /
  contractual). Document the cutoff in the change record before
  running.
- A regulator or DPA requests purge of records older than a
  specific date.

Do NOT sweep just because the table is "big" — `audit_log` is the
forensic record. Always confirm there is a retention policy or a
performance problem first.

## What the sweep deletes

The `audit_log` row schema (from `migrations/0001_users.sql`):

| Column      | Meaning                                            |
|-------------|----------------------------------------------------|
| `id`        | bigserial                                          |
| `created_at`| timestamptz, default `now()`                       |
| `user_id`   | the subject of the audited action (FK → users.id)  |
| `actor_id`  | the principal that performed the action (FK)       |
| `action`    | enum-like text (see `account.Action*` constants)    |
| `target`    | optional opaque identifier (sim id, stripe id, …)  |
| `details`   | optional JSONB blob                                 |

`user_id` is `ON DELETE SET NULL`, so rows tied to a hard-deleted
user already lose their user FK but remain. The sweep is the only
way they leave the table.

## Procedure

### 1. Dry-run count

Open a psql session against the production DB (or staging, when
practising). Run as a superuser role with read access to
`audit_log`:

```sql
-- Choose your cutoff. 180 days is a defensible default; adjust
-- to whatever the retention policy says.
WITH cutoff AS (
  SELECT now() - interval '180 days' AS ts
)
SELECT
  count(*) AS rows_to_delete,
  min(created_at) AS oldest_row,
  max(created_at) AS newest_eligible,
  pg_size_pretty(
    (SELECT pg_total_relation_size('audit_log'))
  ) AS table_size_now
FROM audit_log, cutoff
WHERE audit_log.created_at < cutoff.ts;
```

Verify the row count is in the expected range. If it is suspiciously
high (e.g. you expect ~10 k and see ~10 M), STOP — something else
is filling the table. Investigate before deleting.

### 2. Snapshot (recommended)

Export the rows you are about to delete, in case the retention
policy is reversed:

```sh
psql "$DATABASE_URL" \
  -c "\copy (SELECT * FROM audit_log
             WHERE created_at < now() - interval '180 days'
             ORDER BY created_at)
       TO 'audit_log_sweep_$(date +%Y%m%d).csv'
       WITH (FORMAT csv, HEADER true)"
```

Move the CSV to long-term object storage (GCS bucket with retention
lock or compliance vault) before step 3. Do this from a privileged
operator workstation — never paste the CSV anywhere with PII
restrictions weaker than the bucket's.

### 3. Delete in chunks

The full delete in one statement holds a row-exclusive lock for the
duration. For very large sweeps prefer chunked deletes so admin
queries against the table do not stall:

```sql
DO $$
DECLARE
  cutoff timestamptz := now() - interval '180 days';
  deleted_total bigint := 0;
  batch_size int := 10000;
  rows_in_batch int;
BEGIN
  LOOP
    DELETE FROM audit_log
    WHERE id IN (
      SELECT id FROM audit_log
      WHERE created_at < cutoff
      ORDER BY id
      LIMIT batch_size
    );
    GET DIAGNOSTICS rows_in_batch = ROW_COUNT;
    deleted_total := deleted_total + rows_in_batch;
    RAISE NOTICE 'deleted so far: %', deleted_total;
    EXIT WHEN rows_in_batch = 0;
    PERFORM pg_sleep(0.05);
  END LOOP;
  RAISE NOTICE 'sweep complete: % rows deleted', deleted_total;
END;
$$;
```

`pg_sleep(0.05)` keeps replication lag manageable on AlloyDB.

### 4. Reclaim space

Postgres does not return space to the OS after a DELETE; the rows
are marked dead and reused by future INSERTs. For a one-off large
sweep where you want the storage back:

```sql
VACUUM (VERBOSE, ANALYZE) audit_log;
```

Or, if the table has bloated badly (rare for an append-only audit
trail):

```sql
VACUUM FULL audit_log;
```

`VACUUM FULL` takes an `ACCESS EXCLUSIVE` lock — admin reads of
`audit_log` will block. Schedule a maintenance window or skip this
step.

### 5. Record the sweep

Add an entry to the operator change log (or ticket) noting:

- cutoff timestamp used
- row count deleted (from step 1)
- CSV path in GCS (from step 2)
- who ran it + when

The audit trail of the sweep itself lives outside `audit_log` —
deliberately, so a compromised superadmin cannot quietly purge
their own history.

## Anti-patterns

- **Do NOT add a pg_cron job, scheduled Cloud Run job, or any
  other automated trigger.** Every
  action runs on operator command, never on a schedule.
- **Do NOT delete from the application code.** The retention
  decision belongs to the operator and the compliance review, not
  to a service. Future code should NEVER write a "delete old
  audit rows on startup" branch.
- **Do NOT add an admin UI button without a confirm step.** If a
  future ticket adds `POST /admin/audit/sweep?older_than_days=N`,
  it must require
  the same re-type confirmation pattern used for `DELETE
  /admin/users/:id`, and must record a row in `audit_log` BEFORE
  starting the sweep so the operator action itself is not erased.

## Audit reference

Source finding: "`audit_log` unbounded growth". This runbook documents the manual sweep procedure.
