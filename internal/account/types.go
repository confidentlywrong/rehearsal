// Copyright 2026 The Confidently Wrong Authors
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

// Package account is the per-user identity, billing, and authorization
// layer of the application. It replaces the legacy multi-tenant
// `internal/tenant` package with a flat model:
//
//   - Every authenticated request maps to exactly one user.
//   - Every persisted data row carries a `user_id` foreign key.
//   - Stripe customer + subscription + plan tier live on the user row.
//   - Operator (superadmin) authority is a per-user boolean column,
//     not a special "system" tenant.
//
// The schema is in migrations/0001_users.sql and is applied by
// ApplyMigration on cold start. There is no SAAS_MODE flag: a service
// either has a Postgres connection (production) or it falls back to
// the in-memory LocalResolver (test / make start).
package account

import "time"

// PlanTier names the subscription tier a user is on. Caps for each
// tier live in the `plans` table and are read at request time by the
// quota middleware so a plan-cap change does not require a deploy.
type PlanTier string

const (
	PlanFree       PlanTier = "free"
	PlanPro        PlanTier = "pro"
	PlanTeam       PlanTier = "team"
	PlanEnterprise PlanTier = "enterprise"
	// PlanComp is a complimentary tier granted manually by a
	// superadmin (PUT /admin/users/:id/plan). It is NEVER set by
	// the Stripe webhook handler or the reconciler — both refuse
	// to overwrite a comp grant, so an inactive subscription will
	// not silently downgrade a comped user back to free. Caps
	// live in the `plans` table (seeded by migrations/0001_users.sql);
	// the OSS default mirrors enterprise (unlimited tokens, capped
	// concurrency). Distinct from PlanEnterprise so usage reports
	// can tell paying enterprise customers apart from comped
	// accounts.
	PlanComp PlanTier = "comp"
)

// IsKnown reports whether the tier string is one of the catalog
// values. Used by webhook handlers to refuse to write garbage from a
// misconfigured STRIPE_PRICE_MAP back to the users table.
func (p PlanTier) IsKnown() bool {
	switch p {
	case PlanFree, PlanPro, PlanTeam, PlanEnterprise, PlanComp:
		return true
	}
	return false
}

// UserStatus is the manual-approval lifecycle state of a row in the
// `users` table. See migrations/0001_users.sql for the column
// definition and the rationale for using a TEXT+CHECK column rather
// than a Postgres ENUM.
//
// Every new row created by Store.CreateUser starts in StatusPending.
// A superadmin advances it to StatusApproved via the admin dashboard
// (POST /admin/users/:id/approve), or to StatusRejected via the
// dashboard (POST /admin/users/:id/reject). The gateway's Account
// middleware refuses to serve any route other than /api/v1/me*,
// /api/v1/auth/exchange and /healthz when status != approved.
type UserStatus string

const (
	StatusPending  UserStatus = "pending"
	StatusApproved UserStatus = "approved"
	StatusRejected UserStatus = "rejected"
)

// IsKnown reports whether the status string is one of the three
// catalog values. The admin handlers reject any other input.
func (s UserStatus) IsKnown() bool {
	switch s {
	case StatusPending, StatusApproved, StatusRejected:
		return true
	}
	return false
}

// User is the canonical in-memory shape of a row in the `users` table.
// Returned by Store.GetUser and friends. The struct intentionally
// mirrors the SQL column names so an `INSERT ... RETURNING *` style
// scan is mechanical.
type User struct {
	ID               string
	Email            string
	DisplayName      string
	Timezone         string
	StripeCustomerID string
	PlanTier         PlanTier
	PaymentFailedAt  *time.Time
	IsSuperadmin     bool
	CreatedAt        time.Time
	DeletedAt        *time.Time
	// TokensInvalidAfter is set by Store.RevokeTokens on logout / account
	// delete. The Firebase resolver rejects any token whose `iat` claim is
	// strictly before this timestamp (P3 #17). nil = no revocation has
	// happened, every otherwise-valid token is accepted.
	TokensInvalidAfter *time.Time
	// Status is the manual-approval state of the row
	// (StatusPending / StatusApproved / StatusRejected). New rows
	// land in StatusPending; the gateway's middleware refuses to
	// serve protected routes until a superadmin flips this to
	// StatusApproved via the admin dashboard.
	Status UserStatus
	// ApprovedAt is the timestamp when an operator advanced this
	// row out of StatusPending (set by Store.SetUserStatus). nil
	// while the user is still pending.
	ApprovedAt *time.Time
	// ApprovedBy is the actor id (typically a superadmin's user id
	// or the literal "migration:0001_user_approval" for the
	// bootstrap backfill) recorded by Store.SetUserStatus.
	ApprovedBy string
}

// IsApproved reports whether the user has been advanced out of the
// pending queue and into the live tier. False for both StatusPending
// and StatusRejected — the gateway middleware treats both as "block
// every protected route".
func (u User) IsApproved() bool {
	return u.Status == StatusApproved
}

// IsPaymentOK reports whether the user is allowed to perform writes.
// True when payment_failed_at is NULL (no dunning event recorded since
// the last successful charge). The gateway's payment-gate middleware
// reads this on every write and returns HTTP 402 when false.
func (u User) IsPaymentOK() bool {
	return u.PaymentFailedAt == nil
}

// IsActive reports whether the user is currently usable. False when
// the row has been soft-deleted (deleted_at IS NOT NULL). Hard-delete
// removes the row entirely, in which case GetUser returns ErrNotFound.
func (u User) IsActive() bool {
	return u.DeletedAt == nil
}

// Plan is a single row from the `plans` catalog. -1 means unlimited.
type Plan struct {
	Tier               PlanTier
	MonthlyTokenCap    int64
	MaxConcurrentRuns  int
	Description        string
}

// IsUnlimitedTokens returns true when the plan does not cap monthly
// token usage (enterprise). The quota middleware short-circuits in
// that case so it does not even read usage_rollup_monthly.
func (p Plan) IsUnlimitedTokens() bool { return p.MonthlyTokenCap < 0 }

// IsUnlimitedRuns returns true when the plan does not cap concurrent
// simulation runs (enterprise).
func (p Plan) IsUnlimitedRuns() bool { return p.MaxConcurrentRuns < 0 }

// Audit action constants. These name the verbs the audit log uses;
// keeping them as constants here (rather than free-form strings at
// the call site) makes it trivial to grep for every place that emits
// a particular kind of event and to render a friendly label in the
// admin dashboard.
const (
	ActionUserCreated       = "user.created"        // first sign-in
	ActionUserErased        = "user.erased"         // GDPR self-erase or admin hard-delete
	ActionUserPlanChanged   = "user.plan_changed"   // Stripe webhook or admin override
	ActionUserDataExported  = "user.data_exported"  // GDPR export
	ActionPaymentFailed     = "payment.failed"      // Stripe invoice.payment_failed
	ActionPaymentRecovered  = "payment.recovered"   // Stripe invoice.payment_succeeded
	ActionBillingReconciled = "billing.reconciled"  // operator force-sync
	ActionSuperadminGranted = "superadmin.granted"  // bootstrap promotion
	ActionSuperadminRevoked = "superadmin.revoked"  // bootstrap demotion
	ActionUsersBulkErased   = "users.bulk_erased"   // hard-delete sweep
	ActionUserLoggedOut     = "user.logged_out"     // explicit POST /me/logout
	ActionQuotaRejected     = "quota.rejected"      // gateway refused a spawn over plan limits
	ActionSimSpawned        = "sim.spawned"         // gateway enqueued an orchestration event
	ActionUserApproved      = "user.approved"       // superadmin moved user from pending → approved
	ActionUserRejected      = "user.rejected"       // superadmin moved user from pending → rejected
	ActionUserSignupPending = "user.signup_pending" // /me/init created a row in StatusPending
)

// AuditEntry is one row in the audit log. UserID is nullable: a row
// can describe an action whose subject no longer exists (after
// hard-delete the FK is set NULL by the schema), and a system-level
// event (e.g. superadmin bootstrap on cold start) has no subject.
type AuditEntry struct {
	ID        int64
	UserID    string                 // subject (the user the action was about), "" if none
	ActorID   string                 // who performed it (superadmin email for cross-user ops)
	Action    string                 // one of the Action* constants
	Target    string                 // optional descriptor (e.g. "plan:pro", "stripe:sub_xxx")
	Metadata  map[string]any         // arbitrary JSON payload
	CreatedAt time.Time
}
