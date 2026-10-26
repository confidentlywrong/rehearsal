# Pen-Test Checklist

**Phase 7c** — Cross-tenant isolation verification before each
release. This is a **grep-based** checklist — not a substitute
for a third-party pen test, but a fast first-pass that catches
the common categories of multi-tenant leak.

Run the queries below from the repo root. Each section
describes what a "clean" result looks like; deviations should
be triaged before the release tag is cut.

**Verification methods used in this checklist:**
- **Grep:** the `grep` command in the section was executed and
  its output compared against the "clean result" allowlist.
- **Code review:** the relevant file was opened and read by
  hand; no grep was executed for that section because the
  property is structural rather than lexical (e.g. middleware
  ordering in `setupRouter`, CORS env-var sourcing).

The "Last verified clean" line of each section names which
method was used so a future reviewer can re-run only the
appropriate one.

## 1. Literal `simulation_runs/` references

**Why:** Direct filesystem writes to `data/simulation_runs/`
without going through the `tenant_paths` helper would write to
the legacy anonymous tree, bypassing tenant isolation.

```bash
grep -rn "simulation_runs/" \
  --include='*.go' --include='*.py' --include='*.ts' \
  cmd/ internal/ agents/ web/
```

**Clean result:** Matches are only in:
- Comments / docstrings (descriptive)
- `agents/utils/tenant_paths.py` (the canonical helper)
- `internal/tenant/legacy.go` + `internal/tenant/bootstrap.go`
  (the one-shot migration helpers from Phase 3)

**Last verified clean (grep):** Phase 7c (2026-06-16) — 13 matches,
all in the allowlist above.

## 2. Unprefixed Redis keys

**Why:** Production Redis keys must be derived through
`SpawnQueueNameForContext` / `RedisKeyPrefix` helpers so they
get the `t:{orgID}:` namespace under `SAAS_MODE=on`. A bare
key like `rdb.Set("session:foo", ...)` in production code
would leak across tenants.

```bash
grep -rn -E '(rdb|sb|client)\.(Set|HSet|SAdd|LPush|RPush|XAdd|Publish|Subscribe)\(' \
  --include='*.go' \
  cmd/ internal/
```

**Clean result:** All matches outside `*_test.go` files use
either:
- A pre-computed key from `SpawnQueueNameForContext(ctx, ...)`
- A key derived via the `RedisKey` helper that consults
  `tenant.FromContext`
- A literal that is documented as cross-tenant by design
  (`gateway:broadcast` for global pubsub)

Test-file matches with literal keys are FINE — they exercise
the legacy single-tenant path on a controlled fixture.

**Last verified clean (grep):** Phase 7c (2026-06-16) — 20 matches,
all in `*_test.go` files OR the `gateway:broadcast` channel
(intentionally global).

## 3. SQL string interpolation

**Why:** Building SQL with `fmt.Sprintf("... WHERE org_id = '%s' ...")`
is a textbook injection vector. All queries must use parameter
binds (`$1`, `$2`, ...).

```bash
grep -rn -E 'fmt\.Sprintf\(.*"(SELECT|INSERT|UPDATE|DELETE).*%s' \
  --include='*.go' \
  cmd/ internal/
```

**Clean result:** No matches. The one legitimate use of
`fmt.Sprintf` in SQL construction is in `internal/tenant/audit.go`
for the dynamic WHERE clause, but that interpolates ONLY the
`$N` parameter position (never a value), so the formatting is
safe.

**Last verified clean (grep):** Phase 7c (2026-06-16) — 0 matches.

## 4. Python SQL f-string injection

```bash
grep -rn -E '(f"|f''')(SELECT|INSERT|UPDATE|DELETE)' \
  --include='*.py' \
  agents/ scripts/
```

**Clean result:** Each match must interpolate ONLY a hardcoded
identifier (table or column name from a controlled allowlist),
NEVER a user-supplied value. The 7 matches found in Phase 7c
review are:
- `scripts/ops/emergency_flush.py` — table name from a literal list
- `scripts/ops/generate_local_seeds.py` (3 matches) — column names from a fixed template
- `agents/leadership_planner_with_memory/memory/store_alloydb.py` — column names only
- `agents/planner_with_memory/tests/test_compliance_tool.py` — assertion on generated SQL (not execution)
- `agents/planner_with_memory/memory/store_alloydb.py` — column names only

**Last verified clean (grep):** Phase 7c (2026-06-16) — all 7 matches
interpolate only static identifiers.

## 5. Raw `${ORG_ID}` template leaks

**Why:** Shell-style `${ORG_ID}` substitution in a code file
would suggest a config template that wasn't rendered before
deployment.

```bash
grep -rn -E '\$\{?ORG_ID\}?' \
  --include='*.go' --include='*.py' --include='*.ts' \
  --include='*.yaml' --include='*.tf' \
  cmd/ internal/ agents/ web/ infra/ configs/
```

**Clean result:** No matches. Matches in `.env.example` or
similar documentation files are OK and would be filtered out
by the `--include` list above.

**Last verified clean (grep):** Phase 7c (2026-06-16) — 0 matches.

## 6. Bypass of the tenant middleware

**Why:** A route registered before `r.Use(TenantIfEnabled(...))`
or after a `r.Use(noTenantMiddleware)` would handle requests
without the tenant context attached.

```bash
grep -n "r\.Use\|r\.GET\|r\.POST\|r\.DELETE\|r\.PUT" cmd/gateway/main.go \
  | head -60
```

**Clean result:** Route registrations come AFTER both
`middleware.TenantIfEnabled` and `middleware.RateLimitIfEnabled`
in the boot order. The only `r.Use` calls before route
registration are CORS + Tenant + RateLimit + (legacy) request
ID middleware.

**Last verified clean (code review):** Phase 7c (2026-06-16) — boot order
confirmed by reading `setupRouter` lines 410-435. (No grep
executed for this section — ordering is structural, not
lexical.)

## 7. Hardcoded tenant fallbacks

**Why:** A code path that falls back to a hardcoded
`"anonymous"` or `"default"` OrgID under `SAAS_MODE=on` would
silently merge tenants.

```bash
grep -rn -E '(OrgID|org_id)\s*=\s*"(anonymous|default|legacy|test|admin)"' \
  --include='*.go' --include='*.py' \
  cmd/ internal/ agents/
```

**Clean result:** Matches are only in:
- `*_test.go` files (test fixtures)
- `internal/tenant/types.go` reserved-org constants
  (`OrgLegacy`, `OrgSystem` — used only when no real tenant
  is resolvable, e.g. cron / migration paths that pre-date
  multi-tenancy)
- `internal/tenant/bootstrap.go` (the explicit bootstrap path
  that creates the `local-dev` and `legacy` orgs)

**Last verified clean (code review):** Phase 7c (2026-06-16) — the grep above
was NOT executed this pass; the conclusion is based on a
manual read of `internal/tenant/types.go` and `bootstrap.go`.
Re-run the grep before the next release tag to catch any
newly-introduced fallback string literals.

## 8. CORS allowlist drift

**Why:** A `*` in production CORS allowlist would let any
origin call the gateway with the user's cookies.

```bash
grep -n "CORS_ALLOWED_ORIGINS\|AllowOrigins" \
  cmd/ internal/middleware/ .env* infra/
```

**Clean result:** Production sets `CORS_ALLOWED_ORIGINS` to a
known list of origins (the customer dashboard, the admin UI).
Local dev uses `*` — that's fine.

**Last verified clean (code review):** Phase 7c (2026-06-16) — grep NOT
executed this pass; conclusion drawn from reading the
`middleware.CORS(os.Getenv("CORS_ALLOWED_ORIGINS"))` call site
in `cmd/admin/main.go` and `cmd/gateway/main.go`. Re-run the
grep before the next release tag.

## 9. Tenant scope on read endpoints

**Why:** Read endpoints that don't filter by `tenant.FromContext(ctx)`
would leak cross-tenant data.

```bash
grep -rn "func.*Handler\|r\.GET" cmd/gateway/main.go \
  | head -40
```

For each read endpoint listed (`GET /api/v1/sessions`,
`GET /api/v1/usage`, `GET /api/v1/plans`, ...), verify the
handler either:
- Calls a registry method that takes `c.Request.Context()`
  (the registry filters by tenant key derived from ctx), OR
- Pulls `tenant.FromContext(ctx)` and uses the OrgID
  explicitly in its query.

**Tested via:** `internal/session/redis_registry_tenant_test.go`
which asserts that `ListSessions(ctxA)` returns ONLY ctxA's
sessions even when ctxB has written to the same registry.

**Last verified clean (code review + automated test):** Phase 5b acceptance test
`TestRedisRegistry_TenantIsolation_ListSessions` + Phase 7c
manual review.

## 10. Rate-limiter bypass paths

**Why:** A `SkipPaths` list that includes an authenticated
endpoint (e.g. `/api/v1/spawn`) would let a single tenant
hammer it past the cap.

```bash
grep -n "SkipPaths\|WithSkips" cmd/gateway/main.go internal/middleware/
```

**Clean result:** The only paths in `SkipPaths` are:
- `/health` — probe endpoint
- `/metrics` — observability probe
- `/api/v1/usage` — read-only, throttled upstream by the
  metering drainer
- `/api/v1/plans` — read-only, no tenant work

No write or spawn endpoint is in the skip list.

**Last verified clean (code review):** Phase 7c (2026-06-16) — confirmed by
reading the `WithSkips` call in `setupRouter`. The Phase 7c
bug-fix moved `SkipPaths` to a strict path-segment match
(see `TestRateLimit_SkipPathsIsSegmentMatchNotPrefix`), so a
future skip-list entry can no longer accidentally match a
sibling endpoint by shared prefix.

## When to re-run this checklist

- Before every release tag.
- After any change to `internal/tenant/`, `internal/middleware/`,
  or `cmd/gateway/main.go`'s middleware chain.
- After adding a new admin-only endpoint.
- After upgrading the pgx, gin, or redis-go-redis library
  version.

A failure in any section blocks the release until triaged. A
false positive (e.g. a new legitimate match for section 1) is
documented by editing this file to add the exception to the
allowlist — the goal is "grep stays clean" not "grep matches
nothing".

## What this checklist intentionally does NOT cover

- **Authentication-token forgery.** The Firebase resolver
  verifies signatures upstream; spoofing requires a Firebase
  project compromise, which is out of scope.
- **DoS resilience.** Rate-limit fail-open by design (Redis
  outage → allow). A real DoS test belongs in load-test
  infrastructure, not a grep checklist.
- **Side-channel timing attacks.** Out of scope for a
  multi-tenant SaaS at this volume.
- **Supply-chain attacks.** Dependency review is handled by
  Dependabot / `go list -m all` audit, not this checklist.
