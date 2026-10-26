# Backup & Restore Runbook

**Phase 7c** — Backup verification and restore procedure for the
multi-tenant Postgres tables.

Scope: the tables added under `SAAS_MODE=on`:

- `orgs`
- `users`
- `memberships`
- `audit_log`
- `plans`
- `usage_events`
- `usage_monthly` (materialized aggregation)
- `billing_events` (Stripe webhook dedup)

Today's deployment runs on AlloyDB / Cloud SQL Postgres. These
runbooks assume Cloud SQL Backups + Point-in-Time Recovery (PITR)
are enabled (set in `infra/main.tf`).

## 1. What gets backed up

Cloud SQL automated backups capture **all** databases in the
instance, including the ADK session tables (`sessions`,
`events`, `app_states`, `user_states`) that are NOT in the SaaS
shell. The backup is whole-instance — you cannot restore a
single SaaS table without also restoring everything else, which
is the safest default for cross-table consistency (FK
constraints between `memberships`, `orgs`, `users` would
otherwise be violable).

**Backup retention:** 7 days of automated backups + PITR window
of 7 days. Configure in `infra/modules/database/main.tf`:

```hcl
backup_configuration {
  enabled                        = true
  point_in_time_recovery_enabled = true
  start_time                     = "03:00"             # UTC
  backup_retention_settings {
    retained_backups = 7
    retention_unit   = "COUNT"
  }
  transaction_log_retention_days = 7
}
```

## 2. Backup verification (monthly cadence)

A backup you have never restored is not a backup. Run this drill
on the **first business day of each month**:

1. **Identify the most recent automated backup:**
   ```bash
   gcloud sql backups list --instance=$INSTANCE_NAME \
     --filter="status=SUCCESSFUL" --limit=1 --format=json | jq '.[0]'
   ```
2. **Provision a one-off restore instance** (NOT in-place):
   ```bash
   gcloud sql instances clone $INSTANCE_NAME \
     "${INSTANCE_NAME}-restore-drill-$(date +%Y%m%d)" \
     --point-in-time="$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)"
   ```
3. **Verify table row counts match expectations:**
   ```sql
   SELECT 'orgs' AS table, count(*) FROM orgs UNION ALL
   SELECT 'users', count(*) FROM users UNION ALL
   SELECT 'memberships', count(*) FROM memberships UNION ALL
   SELECT 'audit_log', count(*) FROM audit_log UNION ALL
   SELECT 'usage_events', count(*) FROM usage_events;
   ```
   Compare against the production counts (from the read replica
   or a `--readonly` admin endpoint). A delta of more than 5%
   for any table flags a backup-window misconfiguration.
4. **Tear down the drill instance:**
   ```bash
   gcloud sql instances delete \
     "${INSTANCE_NAME}-restore-drill-$(date +%Y%m%d)" --quiet
   ```
5. **Record the drill outcome** in `audit_log` via the admin
   API:
   ```bash
   curl -X POST "$ADMIN_URL/admin/orgs/system/audit-note" \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -d '{"note":"backup-restore drill complete, deltas under 5%"}'
   ```
   *(This endpoint is a placeholder for a future Phase 7d
   addition — for now, record the drill in your team's normal
   change log.)*

## 3. Full restore procedure (disaster recovery)

**When to use:** primary instance is unrecoverable (region
outage, accidental DROP, corruption discovered after backups
have rotated out the bad transaction).

**Pre-flight:**

1. Confirm the operator triggering this is authorized — restore
   loses any data written since the chosen restore point.
2. Page the on-call before starting. A restore takes 30–120
   minutes depending on instance size; the SaaS shell will
   serve degraded responses (no `/api/v1/usage`, no audit
   reads, no plan changes) until DNS cuts over.

**Procedure:**

1. **Put the gateway and admin service in maintenance mode.**
   Set the env var `SAAS_MODE=off` and redeploy. This routes
   the services back into the legacy single-tenant path —
   `/api/v1/usage` returns 404, `RecordAudit` becomes a no-op,
   and quota enforcement is bypassed. Today's deployments are
   byte-identical to the pre-SaaS world during this window.
2. **Restore from backup to a new instance:**
   ```bash
   gcloud sql backups restore $BACKUP_ID \
     --restore-instance=$INSTANCE_NAME-restored \
     --backup-instance=$INSTANCE_NAME
   ```
3. **Verify the restored schema matches the migration tip:**
   ```bash
   psql $RESTORED_URL -c "\d audit_log"
   psql $RESTORED_URL -c "\d orgs"
   ```
   Compare column lists against `migrations/0001_tenancy.sql`.
   A mismatch means the backup predates the latest migration —
   replay forward migrations before continuing.
4. **Update connection strings.** Point `DATABASE_URL` /
   `ALLOYDB_*` env vars to the restored instance in Cloud Run
   service config.
5. **Take the gateway and admin out of maintenance mode.**
   `SAAS_MODE=on`, redeploy. Smoke-test through the Agents tab
   (HARD RULE — UI, not curl) for at least one tenant.
6. **Record the restore in the audit log.** The restore itself
   has no API hook, so manually issue a `POST /admin/orgs`
   with a placeholder org (later soft-deleted) — the
   `org.created` audit row gives you a known timestamp marker.
   This is a workaround; a dedicated `system.restore` action
   is a future Phase 7d enhancement.

## 4. Point-in-time recovery (PITR) for narrow incidents

When a buggy migration or a fat-finger DELETE has corrupted a
small window:

```bash
# Clone the instance to 5 minutes before the incident.
gcloud sql instances clone $INSTANCE_NAME ${INSTANCE_NAME}-pitr \
  --point-in-time="2026-06-16T14:23:00Z"

# Export the affected tables from the PITR clone:
gcloud sql export sql ${INSTANCE_NAME}-pitr \
  gs://my-bucket/pitr-export.sql \
  --database=racecondition --table=audit_log --table=orgs

# Re-import selectively into production via a one-shot SQL
# editor session (NOT a wholesale restore — see "What is NOT
# in this runbook").
psql $PROD_URL < selective_recovery.sql
```

Selective recovery is risky because FK constraints can fail
mid-import. Prefer a full restore unless the affected window is
truly narrow.

## 5. What this runbook does NOT cover

- **Logical backups via `pg_dump`.** Cloud SQL automated
  backups are the source of truth. Operators occasionally run
  `pg_dump` for offline schema review, but those exports MUST
  NOT be used for restore — they lose the auto-commit timing
  and may produce broken FK chains.
- **Cross-region failover.** Today the instance is single-region.
  Cross-region replicas need a separate Terraform module and
  are deferred.
- **Audit-log-only restore.** Because the FK constraints
  between `audit_log.org_id → orgs.id` are ON DELETE SET NULL
  (intentionally permissive — see the §6.6 plan-doc rationale),
  restoring `audit_log` without restoring `orgs` is technically
  possible but produces orphaned `org_id IS NULL` rows. Don't
  do it.
- **Encryption-key rotation.** Cloud SQL CMEK rotation is a
  separate runbook that lives with the security team's key
  custody documents.

## 6. Verification checklist (after every restore)

- [ ] `SELECT count(*) FROM orgs` matches the pre-incident count
      (or differs by exactly the known damage).
- [ ] `SELECT count(*) FROM audit_log WHERE action = 'plan.changed'`
      includes the last plan change you remember making.
- [ ] `SELECT * FROM usage_monthly WHERE year_month = <current>`
      returns rows for every active tenant.
- [ ] The gateway returns 200 on `GET /api/v1/usage` for at
      least one known org (via Agents tab — UI not curl).
- [ ] The dashboard's Audit tab loads without 500 errors.
