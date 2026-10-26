# Ops Runbooks — SaaS Multi-Tenant

Operational runbooks for the multi-tenant SaaS deployment of Race
Condition. Each runbook is self-contained — read the one that
matches your task, not the whole directory.

Audience: site reliability + compliance engineers operating a
SAAS_MODE=on deployment. These docs assume you have admin
credentials to the cloud project and superadmin privileges on
the gateway / admin service.

| Runbook | When to use |
|---|---|
| [operator_access.md](operator_access.md) | Reach the operator-only `admin`/`dash`/`tester` services from your laptop (gcloud Cloud Run proxy or browser extension) |
| [backup_restore.md](backup_restore.md) | Backup verification, restore drill, point-in-time recovery for the multi-tenant Postgres tables |
| [billing_stripe.md](billing_stripe.md) | Stripe integration: env vars, Dashboard setup, CLI smoke test, production cutover, failure modes |
| [cloud_logging_filters.md](cloud_logging_filters.md) | Pre-canned Cloud Logging queries + Looker Studio data-source recipe for the audit log |
| [pen_test_checklist.md](pen_test_checklist.md) | Cross-tenant isolation grep checklist (Phase 7c) |
| [audit_log_retention.md](audit_log_retention.md) | Manual `audit_log` retention sweep (no scheduled jobs — operator-triggered only) |

Operational invariants that apply to ALL runbooks:

- **No agent runs on a schedule.** Every action below is a manual
  operator command. Never a Postgres cron, never a background
  job.
- **Never invent data.** If a query returns NULL, the answer is
  NULL — do not fill in a fabricated value to make a dashboard
  look prettier.
- **Test through the UI when possible.** For end-to-end
  verification, drive the Agents tab in the dashboard rather
  than curling endpoints. Restore drills are the main exception.
