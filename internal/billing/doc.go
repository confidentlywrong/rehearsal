// Copyright 2026 Christian Haeussinger
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Package billing implements the token-usage transparency layer
// introduced by Phase 4 of the SaaS conversion.
//
// Contents:
//
//   - Store (store.go) — pgx-backed reads/writes for usage_ledger +
//     usage_rollup_monthly. Schema-owned by migrations/0001_tenancy.sql.
//   - SQLiteDrainer (sqlite_drainer.go) — polls the SQLite ledger
//     written by the Python `agents.utils.metering.SqliteSink` and
//     forwards rows into Postgres. Used in local dev when
//     METERING_SINK=sqlite. The cloud deployment uses the (TBD,
//     deferred to Phase 4c) PubSubDrainer instead.
//   - UsageHandler (handler.go) — GET /api/v1/usage. Org-scoped via
//     tenant.FromContext. Surfaces
//     "unknown" (not zero) for missing tokens or agent_type.
//
// Phases still outstanding (in this package's scope):
//
//   - Phase 4c: PubSubDrainer for the cloud path; Angular usage tile.
//   - Phase 5: pre-flight quota check (HTTP 402 quota_exhausted) and
//     the concurrent-runs counter.
//   - Phase 6: Stripe webhook handler and plan-change logic.
//
// Hard rule observed throughout: NULL token fields are never coerced
// to zero. They flow through as nil pointers in Go, NULL in Postgres,
// and serialise as JSON null. The dashboard renders them as
// "unknown" so operators can distinguish a quiet period from an
// undermeasured one.
package billing
