# Cloud Logging Filters + Looker Studio Recipe

**Phase 7c** — Pre-canned Cloud Logging queries and a
Looker Studio data-source recipe for the audit log + rate-limit
events.

The gateway and admin services emit structured logs to Cloud
Logging when deployed to Cloud Run. This runbook captures the
queries that have proven useful during incident response and
the data-source wiring for the operator dashboard.

## 1. Pre-canned Cloud Logging queries

Save these as **Saved queries** in the Cloud Logging UI so the
team can re-run them with one click.

### 1.1 Audit-log activity, all orgs, last 24h

```
resource.type="cloud_run_revision"
resource.labels.service_name=~"(gateway|admin|billing)"
jsonPayload.message=~"AUDIT:"
```

### 1.2 Rate-limit rejections, last hour

```
resource.type="cloud_run_revision"
resource.labels.service_name="gateway"
httpRequest.status=429
```

(`429` is unique to the rate-limit middleware — no other
endpoint in the gateway returns this code.)

### 1.3 Rate-limit fail-open events (Redis outage symptom)

The middleware logs `RATELIMIT: script failed for org=...` when
Redis is unreachable. A burst of these means rate limiting is
silently disabled — page the on-call.

```
resource.type="cloud_run_revision"
resource.labels.service_name="gateway"
textPayload=~"RATELIMIT: script failed"
```

### 1.4 Quota rejections by tier, last 7 days

```
resource.type="cloud_run_revision"
resource.labels.service_name="gateway"
httpRequest.status=402
```

Group by `httpRequest.requestUrl` to see which routes are
triggering quota walls.

### 1.5 Best-effort audit failures (compliance gap risk)

The §7a fail-open contract swallows audit-record errors but
logs them. A persistent stream of these means audit rows are
being lost — investigate before the compliance reviewer asks.

```
resource.type="cloud_run_revision"
resource.labels.service_name=~"(gateway|admin|billing)"
textPayload=~"AUDIT: best-effort record failed"
```

### 1.6 Stripe webhook deduplication signal

```
resource.type="cloud_run_revision"
resource.labels.service_name="billing"
textPayload=~"BILLING:"
```

### 1.7 Superadmin actions in the admin service

```
resource.type="cloud_run_revision"
resource.labels.service_name="admin"
httpRequest.requestMethod=~"POST|DELETE"
httpRequest.requestUrl=~"/admin/"
```

A weekly review of this filter is the lightweight
"who-did-what" alternative until the per-org audit viewer
ships.

## 2. Log-based metrics

Set these up in **Logging → Log-based metrics** so you can
graph them in Cloud Monitoring and alert on them.

| Metric name | Filter | Type |
|---|---|---|
| `gateway_429_count` | Query 1.2 above | Counter |
| `ratelimit_fail_open_count` | Query 1.3 above | Counter |
| `audit_emit_failure_count` | Query 1.5 above | Counter |
| `quota_rejection_count` | Query 1.4 above, group by `httpRequest.requestUrl` | Counter |

### Suggested alert thresholds

- **`ratelimit_fail_open_count` > 10 / minute** — page on-call.
  Redis is unreachable; rate limiting is degraded. While the
  request path stays open (fail-open by design), this is the
  symptom of a Redis outage that may also affect queues.
- **`audit_emit_failure_count` > 5 / minute** — warn (not
  page). Compliance log is being lost; investigate the audit
  DB before the next audit review.
- **`quota_rejection_count` sustained > 100 / minute for one
  `requestUrl`** — informational; a single tenant may be
  hammering an endpoint. Consider a per-tenant cap review.

## 3. Looker Studio data source

The audit log is the primary data source for the operator
dashboard's "Compliance" tab. Configure as follows:

1. **Open Looker Studio** → Create → Data Source → BigQuery.
2. **Choose project + dataset** — point at the
   `racecondition` schema in your AlloyDB project.
3. **Custom query** — paste the SQL below. Looker treats this
   as the source table; refresh cadence is 1 hour.
4. **Field types** — Looker auto-detects, but verify:
   - `ts` → Date Hour Minute (not Date)
   - `metadata` → Text (so JSONPath formulas work)
   - `action` → Text (the natural grouping dimension)

### Source SQL

```sql
SELECT
  id,
  org_id,
  user_id,
  action,
  target,
  ts,
  -- Cast metadata JSONB to text so Looker can index it.
  -- JSON_VALUE(metadata, '$.plan_tier') etc. work
  -- downstream as calculated fields.
  metadata::text AS metadata,
  -- Pre-extract the common metadata fields so dashboard
  -- filters don't need a calculated field per chart.
  metadata->>'plan_tier'   AS plan_tier,
  metadata->>'source'      AS source,
  metadata->>'route'       AS route,
  metadata->>'reason'      AS reason
FROM audit_log
WHERE ts > CURRENT_DATE - INTERVAL '90 days'
ORDER BY ts DESC
```

The 90-day window matches the operator dashboard's default
display range. Longer-window queries should be ad-hoc, not
baked into the data source, because they get expensive once the
purge cadence is fully tuned.

### Pre-built calculated fields

| Field | Formula |
|---|---|
| `action_category` | `CASE WHEN action LIKE 'plan.%' THEN 'billing' WHEN action LIKE 'quota.%' THEN 'enforcement' WHEN action = 'sim.spawned' THEN 'activity' WHEN action LIKE 'org.%' THEN 'lifecycle' WHEN action LIKE 'user.%' THEN 'identity' WHEN action = 'audit.purged' THEN 'compliance' ELSE 'other' END` |
| `is_rejection` | `action = 'quota.rejected'` |
| `is_revenue_event` | `action IN ('plan.changed', 'org.created')` |

### Recommended Looker pages

1. **Overview** — Big-number tiles: total audit rows last 30
   days, count by `action_category`, top 10 orgs by event
   volume.
2. **Plan changes** — Filtered to `action = 'plan.changed'`.
   Time-series of upgrades vs. downgrades (use
   `source = 'stripe'` vs `source = 'manual'`).
3. **Quota rejections** — Filtered to `is_rejection`. Group by
   `plan_tier` and `route` to spot which tier+endpoint pairs
   hit the wall most.
4. **Compliance** — Filtered to `action_category = 'compliance'`.
   Shows audit purges with operator UserID + cutoff + deleted
   row count. This is the page the compliance reviewer opens
   first.

## 4. Cloud Run log payload conventions

Every service uses Go's standard `log.Printf` for now, which
Cloud Logging surfaces as `textPayload`. Future structured
JSON logging will move these to `jsonPayload`, but the queries
above use the `textPayload=~"PREFIX:"` regex pattern that works
for both layouts.

Conventional prefixes (grep for these in code to verify
coverage):

| Prefix | Source | Meaning |
|---|---|---|
| `GATEWAY:` | `cmd/gateway/main.go` | Boot + lifecycle events |
| `ADMIN:` | `cmd/admin/main.go` | Admin-service boot + endpoint logs |
| `BILLING:` | `cmd/billing/main.go` | Webhook receipt / dedup / audit |
| `AUDIT:` | helpers in `cmd/gateway` + `cmd/admin` | Best-effort audit emit failures |
| `RATELIMIT:` | `internal/middleware/ratelimit.go` | Disabled-at-boot + per-request fail-open |
| `QUOTA:` | `cmd/gateway/main.go` | Pre-flight gate fail-open |
| `TENANT:` | `internal/middleware/tenant.go` | Resolver rejections |

## 5. What this runbook does NOT cover

- **Trace correlation.** Cloud Run injects a `trace` field
  automatically; we don't yet add custom trace IDs to logs.
  Adding them is a Phase 8 enhancement.
- **PII redaction.** Today's logs include email addresses for
  `AUDIT: best-effort record failed` lines (the audit row
  contains the user's email). If your compliance regime
  requires hashed emails in logs, configure a Cloud Logging
  exclusion + a separate sink to a PII-cleansed bucket.
- **Long-term archival.** The default Cloud Logging retention
  is 30 days. For 7-year audit retention, the source of truth
  is the `audit_log` Postgres table, NOT the log stream.
