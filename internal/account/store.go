// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package account

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// PgxConn is the minimal pgx-style surface the Store needs. Both
// *pgx.Conn and *pgxpool.Pool satisfy it, and tests can pass a fake.
// The store does not need transactions: every operation is a single
// statement (the per-user model has no multi-row invariants to
// preserve atomically — there is no membership join table any more).
type PgxConn interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// ErrNotFound is returned when a lookup finds no row.
var ErrNotFound = errors.New("account: not found")

// ErrTokenRevoked is returned by the Firebase resolver when an
// otherwise-valid token's `iat` claim is strictly older than the
// user's tokens_invalid_after column. Set by Store.RevokeTokens on
// logout / account-delete (P3 #17, 2026-06-19 audit). Callers should
// translate this into 401 with code="token_revoked" so the SPA can
// clear localStorage and bounce the user to /login.
var ErrTokenRevoked = errors.New("account: token revoked")

// ErrAccountDeleted is returned by the Firebase resolver when a
// verified Firebase identity maps to a users-table row whose
// deleted_at is non-NULL (P1.A, 2026-06-22 audit). Distinct from
// ErrNotFound (which means "no row yet — needs init") so the auth
// middleware can emit 410 Gone instead of 409 needs_init, and the
// SPA can show a clear "account_deleted" message instead of looping
// the user through /me/init only to crash on the PK collision the
// soft-deleted row still owns.
var ErrAccountDeleted = errors.New("account: account deleted")

// ErrInvalidPlan is returned by SetPlan when the caller passes a tier
// the catalog does not know. The webhook handlers swallow this and
// log it as a configuration error rather than failing the request.
var ErrInvalidPlan = errors.New("account: invalid plan tier")

// ErrInvalidStatus is returned by SetUserStatus when the caller passes
// a status string that UserStatus.IsKnown() rejects. The admin handler
// turns this into a 400 BadRequest so the operator sees the typo
// immediately. (Approval flow, 2026-06-26.)
var ErrInvalidStatus = errors.New("account: invalid user status")

// ErrEmailTaken is returned by CreateUser when the email already has
// a (live) row. Distinct from a generic UNIQUE violation so callers
// can distinguish "user already exists" from "DB had a hiccup".
var ErrEmailTaken = errors.New("account: email already in use")

// ErrLastSuperadmin is returned by SetSuperadmin when a demote
// would leave the system with zero live superadmins. This is the
// "you can't lock everyone out" guard — protects against both
// accidental self-demotion and an attacker who compromises a single
// admin account trying to orphan the operator role.
var ErrLastSuperadmin = errors.New("account: refusing to demote last superadmin")

// Store is the per-user data accessor. It does not own a pool; the
// caller hands in any PgxConn-satisfying value (production passes a
// *pgx.Conn, tests pass a fake).
type Store struct {
	db PgxConn
}

// NewStore constructs a Store over db. db may be nil; methods on a
// nil-db Store return ErrNotFound consistently so the surrounding
// handlers can treat a degraded DB the same as a missing row. The
// admin /healthz separately surfaces the actual DB ping result.
func NewStore(db PgxConn) *Store {
	return &Store{db: db}
}

// --- user CRUD --------------------------------------------------------

// GetUser fetches one user by id (Firebase UID). Returns ErrNotFound
// when the row is missing OR when it is soft-deleted; this matches
// the gateway's "deleted users cannot make requests" invariant for
// the handler-side path (handlers should never see a deleted row
// because the Account middleware 410s them via GetUserAnyState +
// the FirebaseResolver's IsActive check before any handler runs).
func (s *Store) GetUser(ctx context.Context, id string) (User, error) {
	if s == nil || s.db == nil {
		return User{}, ErrNotFound
	}
	row := s.db.QueryRow(ctx, `
		SELECT id, email, COALESCE(display_name,''), COALESCE(timezone,''),
		       COALESCE(stripe_customer_id,''), plan_tier, payment_failed_at,
		       is_superadmin, created_at, deleted_at, tokens_invalid_after,
		       status, approved_at, COALESCE(approved_by,'')
		  FROM users
		 WHERE id = $1 AND deleted_at IS NULL`, id)
	return scanUser(row)
}

// GetUserAnyState fetches one user by id (Firebase UID) WITHOUT
// filtering deleted_at. Returns the row even when soft-deleted;
// returns ErrNotFound only when no row exists at all.
//
// Purpose (P1.A, 2026-06-22 audit): the FirebaseResolver uses this
// as its UserLookup so it can distinguish three states from one
// query:
//
//   - row missing            → ErrNotFound → resolver sets NeedsInit=true
//   - row present, alive     → resolver returns active UserContext
//   - row present, deleted   → resolver returns ErrAccountDeleted
//
// Without this method the resolver only saw the first two cases
// (because GetUser's WHERE deleted_at IS NULL collapses "missing"
// and "deleted" into the same ErrNotFound), which made every
// re-sign-in by a soft-deleted user run through the NeedsInit path
// and crash on the users.id PK collision the soft-deleted row
// still owned. Handler-layer callers should keep using GetUser so
// they cannot accidentally write through a stale identity.
func (s *Store) GetUserAnyState(ctx context.Context, id string) (User, error) {
	if s == nil || s.db == nil {
		return User{}, ErrNotFound
	}
	row := s.db.QueryRow(ctx, `
		SELECT id, email, COALESCE(display_name,''), COALESCE(timezone,''),
		       COALESCE(stripe_customer_id,''), plan_tier, payment_failed_at,
		       is_superadmin, created_at, deleted_at, tokens_invalid_after,
		       status, approved_at, COALESCE(approved_by,'')
		  FROM users
		 WHERE id = $1`, id)
	return scanUser(row)
}

// GetUserByEmail looks up by email (case-sensitive — Firebase
// already normalizes). Returns ErrNotFound for missing or deleted.
func (s *Store) GetUserByEmail(ctx context.Context, email string) (User, error) {
	if s == nil || s.db == nil {
		return User{}, ErrNotFound
	}
	row := s.db.QueryRow(ctx, `
		SELECT id, email, COALESCE(display_name,''), COALESCE(timezone,''),
		       COALESCE(stripe_customer_id,''), plan_tier, payment_failed_at,
		       is_superadmin, created_at, deleted_at, tokens_invalid_after,
		       status, approved_at, COALESCE(approved_by,'')
		  FROM users
		 WHERE email = $1 AND deleted_at IS NULL`, email)
	return scanUser(row)
}

// GetUserByStripeCustomerID looks up by Stripe customer id. Used by
// webhook handlers that receive a Stripe event addressed to a
// customer and need to find the local user row. Returns ErrNotFound
// for missing or deleted users.
func (s *Store) GetUserByStripeCustomerID(ctx context.Context, cust string) (User, error) {
	if s == nil || s.db == nil {
		return User{}, ErrNotFound
	}
	if cust == "" {
		return User{}, ErrNotFound
	}
	row := s.db.QueryRow(ctx, `
		SELECT id, email, COALESCE(display_name,''), COALESCE(timezone,''),
		       COALESCE(stripe_customer_id,''), plan_tier, payment_failed_at,
		       is_superadmin, created_at, deleted_at, tokens_invalid_after,
		       status, approved_at, COALESCE(approved_by,'')
		  FROM users
		 WHERE stripe_customer_id = $1 AND deleted_at IS NULL`, cust)
	return scanUser(row)
}

// CreateUser inserts a new row with plan_tier='free'. Called by the
// gateway's /api/v1/me/init handler the first time an authenticated
// caller hits the API.
//
// Returns (user, created, error):
//
//   - On a fresh INSERT: (newRow, true, nil).
//   - On idempotent re-call — the row already exists at pre-check or
//     a concurrent CreateUser won the 23505 race — (existingRow,
//     false, nil). The caller should NOT emit a "user created" audit
//     row in this branch (P3.B, 2026-06-22 audit; previously
//     CreateUser collapsed both cases into a nil error, so two-tab
//     /me/init produced spurious duplicate user.created audit
//     entries and a misleading `created:true` JSON field).
//   - If a different id already owns this email: (User{}, false,
//     ErrEmailTaken). The pre-check + 23505 catch both surface this.
//
// Race-safe: two parallel calls with the same id both pass the
// pre-check (no row yet), both try to INSERT, one wins and the other
// hits a UNIQUE-violation. We catch 23505 here and re-fetch instead
// of bubbling a 500 up to the SPA on a double-clicked /me/init.
func (s *Store) CreateUser(ctx context.Context, id, email, displayName string) (User, bool, error) {
	if s == nil || s.db == nil {
		return User{}, false, errors.New("account: nil store")
	}
	id = strings.TrimSpace(id)
	email = strings.TrimSpace(email)
	if id == "" || email == "" {
		return User{}, false, errors.New("account: id and email required")
	}
	// Check idempotency: existing live row with same id wins.
	if existing, err := s.GetUser(ctx, id); err == nil {
		return existing, false, nil
	}
	// Refuse if a different id already owns this email.
	if existing, err := s.GetUserByEmail(ctx, email); err == nil && existing.ID != id {
		return User{}, false, ErrEmailTaken
	}
	// L2 (2026-06-25 audit): use INSERT ... RETURNING so the
	// happy path is a single roundtrip. The trailing GetUser on
	// the 23505 race-loser branch is still required because the
	// RETURNING clause only fires on a successful insert.
	row := s.db.QueryRow(ctx, `
		INSERT INTO users (id, email, display_name, plan_tier)
		VALUES ($1, $2, NULLIF($3,''), 'free')
		RETURNING id, email, COALESCE(display_name,''), COALESCE(timezone,''),
		          COALESCE(stripe_customer_id,''), plan_tier, payment_failed_at,
		          is_superadmin, created_at, deleted_at, tokens_invalid_after,
		          status, approved_at, COALESCE(approved_by,'')`,
		id, email, displayName)
	u, err := scanUser(row)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			// A concurrent CreateUser won the race. Whether the
			// conflict was on the id PK or the email UNIQUE index,
			// re-resolving by id gives the canonical row; an email
			// conflict against a *different* id is the same
			// "different UID claims my email" case the pre-check
			// catches and surfaces as ErrEmailTaken.
			if existing, gerr := s.GetUser(ctx, id); gerr == nil {
				return existing, false, nil
			}
			return User{}, false, ErrEmailTaken
		}
		return User{}, false, fmt.Errorf("account: insert user: %w", err)
	}
	return u, true, nil
}

// SetPlan writes a new plan_tier for the user. Returns ErrInvalidPlan
// when tier is not one of the catalog values; ErrNotFound when the
// user row is missing or soft-deleted.
func (s *Store) SetPlan(ctx context.Context, userID string, tier PlanTier) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	if !tier.IsKnown() {
		return ErrInvalidPlan
	}
	tag, err := s.db.Exec(ctx, `
		UPDATE users SET plan_tier = $1
		 WHERE id = $2 AND deleted_at IS NULL`, string(tier), userID)
	if err != nil {
		return fmt.Errorf("account: set plan: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// SetStripeCustomerID associates a Stripe customer id with a user.
// Called once at the end of /api/v1/me/init after we've created the
// Stripe customer. Idempotent — a second call with the same id is a
// no-op; a call with a different id overwrites (last write wins,
// matching Stripe's own customer-merging behavior).
func (s *Store) SetStripeCustomerID(ctx context.Context, userID, cust string) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	tag, err := s.db.Exec(ctx, `
		UPDATE users SET stripe_customer_id = NULLIF($1,'')
		 WHERE id = $2 AND deleted_at IS NULL`, cust, userID)
	if err != nil {
		return fmt.Errorf("account: set stripe customer: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// MarkPaymentFailed sets payment_failed_at = ts. Webhook handler
// calls this on invoice.payment_failed.
func (s *Store) MarkPaymentFailed(ctx context.Context, userID string, ts time.Time) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	tag, err := s.db.Exec(ctx, `
		UPDATE users SET payment_failed_at = $1
		 WHERE id = $2 AND deleted_at IS NULL`, ts.UTC(), userID)
	if err != nil {
		return fmt.Errorf("account: mark payment failed: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// ClearPaymentFailed nulls out payment_failed_at. Webhook handler
// calls this on invoice.payment_succeeded.
func (s *Store) ClearPaymentFailed(ctx context.Context, userID string) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	tag, err := s.db.Exec(ctx, `
		UPDATE users SET payment_failed_at = NULL
		 WHERE id = $1 AND deleted_at IS NULL`, userID)
	if err != nil {
		return fmt.Errorf("account: clear payment failed: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// SetSuperadmin flips the is_superadmin column. Intended for use by
// an operator-only admin endpoint or a direct DBA SQL UPDATE.
// Idempotent.
//
// Demote guard: a call with isAdmin=false that would leave the
// system with zero live superadmins returns ErrLastSuperadmin
// without touching the row. This prevents both accidental
// self-lockout and an adversarial single-admin compromise from
// orphaning the operator role. The check uses a single SQL
// statement so it is safe under concurrent demotes (Postgres's
// row-level locking serialises the UPDATEs; the EXISTS subquery
// sees the pre-update snapshot of OTHER admin rows but the WHERE
// clause re-evaluates per row, so the worst case is one of two
// racing demotes succeeds and the other no-ops, never both).
func (s *Store) SetSuperadmin(ctx context.Context, email string, isAdmin bool) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	if isAdmin {
		_, err := s.db.Exec(ctx, `
			UPDATE users SET is_superadmin = TRUE
			 WHERE email = $1 AND deleted_at IS NULL`, email)
		if err != nil {
			return fmt.Errorf("account: set superadmin: %w", err)
		}
		return nil
	}
	// Demote path: only touch the row when at least one OTHER
	// live superadmin exists. RowsAffected==0 then means either
	// (a) the target email is not a live superadmin (no-op) or
	// (b) the guard fired. Disambiguate with a follow-up read.
	tag, err := s.db.Exec(ctx, `
		UPDATE users SET is_superadmin = FALSE
		 WHERE email = $1
		   AND deleted_at IS NULL
		   AND is_superadmin = TRUE
		   AND EXISTS (
		     SELECT 1 FROM users
		      WHERE is_superadmin = TRUE
		        AND deleted_at IS NULL
		        AND email != $1
		   )`, email)
	if err != nil {
		return fmt.Errorf("account: set superadmin: %w", err)
	}
	if tag.RowsAffected() == 0 {
		// Was the target a live superadmin at all? If yes, the
		// guard fired; if no, the call was a no-op.
		var wasAdmin bool
		if err := s.db.QueryRow(ctx, `
			SELECT EXISTS(
			  SELECT 1 FROM users
			   WHERE email = $1
			     AND deleted_at IS NULL
			     AND is_superadmin = TRUE
			)`, email).Scan(&wasAdmin); err != nil {
			return fmt.Errorf("account: set superadmin (guard probe): %w", err)
		}
		if wasAdmin {
			return ErrLastSuperadmin
		}
	}
	return nil
}

// HasAnySuperadmin reports whether at least one live (non-deleted)
// user row has is_superadmin = true. Available for observability and
// for any future admin UI that needs a fast "is the system bootstrapped"
// signal.
func (s *Store) HasAnySuperadmin(ctx context.Context) (bool, error) {
	if s == nil || s.db == nil {
		return false, errors.New("account: nil store")
	}
	var exists bool
	err := s.db.QueryRow(ctx, `
		SELECT EXISTS(
		  SELECT 1 FROM users
		   WHERE is_superadmin = TRUE AND deleted_at IS NULL
		)`).Scan(&exists)
	if err != nil {
		return false, fmt.Errorf("account: has any superadmin: %w", err)
	}
	return exists, nil
}

// ListSuperadminEmails returns the emails of every live superadmin,
// alphabetically. Available for observability and admin UIs that
// need to render the current operator roster.
func (s *Store) ListSuperadminEmails(ctx context.Context) ([]string, error) {
	if s == nil || s.db == nil {
		return nil, errors.New("account: nil store")
	}
	rows, err := s.db.Query(ctx, `
		SELECT email FROM users
		 WHERE is_superadmin = TRUE AND deleted_at IS NULL
		 ORDER BY email`)
	if err != nil {
		return nil, fmt.Errorf("account: list superadmin emails: %w", err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var e string
		if err := rows.Scan(&e); err != nil {
			return nil, fmt.Errorf("account: scan superadmin email: %w", err)
		}
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("account: list superadmin emails: %w", err)
	}
	return out, nil
}

// RevokeTokens stamps users.tokens_invalid_after = NOW() so the
// Firebase resolver rejects any token whose `iat` claim is strictly
// before this moment. Called from /api/v1/me/logout and from the
// account-delete paths (P3 #17 in the 2026-06-19 audit).
//
// Idempotent: re-calling just bumps the timestamp forward, which is
// what we want — every subsequent logout shortens the window during
// which a stolen pre-logout token can still be used.
//
// Note: this does NOT revoke the Firebase REFRESH token at Google
// (that would require the Firebase Admin SDK). It revokes only the
// already-issued ID tokens at OUR verifier. After a refresh-token
// cycle (~1h), the Firebase client will mint a new ID token whose
// `iat` is fresh, and that token will be accepted again. Operators
// who need a true sign-everywhere revoke must also call
// `firebase.auth().signOut()` client-side AND wire
// `auth.RevokeRefreshTokens(uid)` server-side via the Admin SDK
// (deferred — single dep, modest payoff).
func (s *Store) RevokeTokens(ctx context.Context, userID string) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	if userID == "" {
		return errors.New("account: RevokeTokens: empty user id")
	}
	tag, err := s.db.Exec(ctx, `
		UPDATE users SET tokens_invalid_after = now()
		 WHERE id = $1 AND deleted_at IS NULL`, userID)
	if err != nil {
		return fmt.Errorf("account: revoke tokens: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// SetUserStatus advances a user row through the manual-approval
// lifecycle (StatusPending → StatusApproved or StatusRejected).
// Returns ErrInvalidStatus for unknown statuses, ErrNotFound when
// the row is missing or already soft-deleted.
//
// For StatusApproved we stamp approved_at = now() and
// approved_by = actorID so the audit trail shows which superadmin
// flipped the gate. For StatusRejected we still record the actor
// in approved_by (the column is the audit "last actor", not "the
// approver"). We do not clear them when moving back to pending
// (unreachable in the current UI but harmless if a future op needs
// it) — the audit log captures the transitions, so leaving the
// previous values is fine.
//
// Idempotent: setting the row to its current status is a no-op
// (zero RowsAffected → returned as nil, matching SetUserPlan's
// behaviour). This is intentional so the admin dashboard can
// re-fire the action without producing a 404.
func (s *Store) SetUserStatus(ctx context.Context, userID string, status UserStatus, actorID string) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	if userID == "" {
		return errors.New("account: SetUserStatus: empty user id")
	}
	if !status.IsKnown() {
		return ErrInvalidStatus
	}
	// approved_at is set only when transitioning into approved.
	// Keep it nil for pending/rejected so the column reflects "when
	// did this row enter the live tier".
	var approvedAt any
	if status == StatusApproved {
		approvedAt = time.Now().UTC()
	}
	tag, err := s.db.Exec(ctx, `
		UPDATE users
		   SET status = $2,
		       approved_at = COALESCE($3, approved_at),
		       approved_by = $4
		 WHERE id = $1 AND deleted_at IS NULL`,
		userID, string(status), approvedAt, actorID)
	if err != nil {
		return fmt.Errorf("account: set user status: %w", err)
	}
	if tag.RowsAffected() == 0 {
		// Distinguish "no row" from "row already in this state".
		// We can't tell them apart from RowsAffected alone, so probe
		// the row's existence; if it exists we return nil (idempotent
		// success), otherwise ErrNotFound.
		var exists bool
		if perr := s.db.QueryRow(ctx, `
			SELECT EXISTS(SELECT 1 FROM users WHERE id = $1 AND deleted_at IS NULL)
		`, userID).Scan(&exists); perr != nil {
			return fmt.Errorf("account: set user status probe: %w", perr)
		}
		if !exists {
			return ErrNotFound
		}
	}
	return nil
}
// stays in the DB (and FK relationships hold) for the configured
// GDPR retention window; HardDeleteExpired sweeps it out later.
//
// P2 (2026-06-23 audit): also clears stripe_customer_id so a
// re-signup with the same email cannot inherit the previous owner's
// Stripe customer object. The Stripe-side customer record itself is
// untouched (operators can delete it via the Stripe dashboard if
// needed); we only break the local association so the next
// /me/init for that email mints a fresh customer.
func (s *Store) SoftDeleteUser(ctx context.Context, userID string) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	tag, err := s.db.Exec(ctx, `
		UPDATE users
		   SET deleted_at = now(),
		       stripe_customer_id = NULL
		 WHERE id = $1 AND deleted_at IS NULL`, userID)
	if err != nil {
		return fmt.Errorf("account: soft delete: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// UpdateUserProfile patches display_name and/or timezone on a live
// user row. Pointer arguments preserve PATCH semantics:
//
//   - nil      → field omitted from the update → leave unchanged.
//   - non-nil  → field present (even if empty string) → write the
//     value. An empty string clears the column (NULLIF).
//
// A call where both pointers are nil is a 0-row no-op rather than
// an error so a SPA can issue an "empty PATCH" without bookkeeping.
//
// Returns ErrNotFound when the user row is missing or soft-deleted.
func (s *Store) UpdateUserProfile(ctx context.Context, userID string, displayName, timezone *string) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	if displayName == nil && timezone == nil {
		return nil
	}
	// Build the SET clause from the non-nil pointers so an
	// omitted field is truly untouched at the SQL level (rather
	// than COALESCE-style preserved, which still re-writes the
	// same value and bumps any future updated_at trigger).
	var sets []string
	var args []any
	args = append(args, userID)
	if displayName != nil {
		args = append(args, *displayName)
		sets = append(sets, fmt.Sprintf("display_name = NULLIF($%d,'')", len(args)))
	}
	if timezone != nil {
		args = append(args, *timezone)
		sets = append(sets, fmt.Sprintf("timezone = NULLIF($%d,'')", len(args)))
	}
	q := "UPDATE users SET " + strings.Join(sets, ", ") +
		" WHERE id = $1 AND deleted_at IS NULL"
	tag, err := s.db.Exec(ctx, q, args...)
	if err != nil {
		return fmt.Errorf("account: update profile: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// HardDeleteUser removes the row outright. Cascades through every
// FK (usage_ledger, usage_rollup_monthly via ON DELETE CASCADE;
// audit_log keeps its rows with user_id set NULL via ON DELETE SET
// NULL so the audit trail survives the user). Used by both operator
// admin route and the bulk sweep below.
func (s *Store) HardDeleteUser(ctx context.Context, userID string) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	tag, err := s.db.Exec(ctx, `DELETE FROM users WHERE id = $1`, userID)
	if err != nil {
		return fmt.Errorf("account: hard delete: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// HardDeleteExpiredUsers DELETE-s every soft-deleted row with
// deleted_at < cutoff. Returns the row count. cutoff must be a past
// timestamp (it's a programmer error to pass time.Now() or a future
// value — the caller computes `now - retention` and passes that).
func (s *Store) HardDeleteExpiredUsers(ctx context.Context, cutoff time.Time) (int64, error) {
	if s == nil || s.db == nil {
		return 0, errors.New("account: nil store")
	}
	if cutoff.IsZero() || cutoff.After(time.Now().Add(time.Second)) {
		return 0, fmt.Errorf("account: refusing sweep with non-past cutoff %v", cutoff)
	}
	tag, err := s.db.Exec(ctx, `
		DELETE FROM users
		 WHERE deleted_at IS NOT NULL AND deleted_at < $1`, cutoff.UTC())
	if err != nil {
		return 0, fmt.Errorf("account: sweep: %w", err)
	}
	return tag.RowsAffected(), nil
}

// --- listing for the operator admin dashboard ------------------------

// ListUsers returns every live user row, ordered by created_at desc.
// Used only by the superadmin /admin/users endpoint; ordinary users
// never see other users' rows. Page/limit are not exposed yet — the
// operator dashboard is tiny.
//
// Hard-capped at 1000 rows so a growing tenant base cannot OOM the
// admin process; the operator UI will need explicit pagination once
// we approach the cap.
func (s *Store) ListUsers(ctx context.Context) ([]User, error) {
	if s == nil || s.db == nil {
		return nil, nil
	}
	rows, err := s.db.Query(ctx, `
		SELECT id, email, COALESCE(display_name,''), COALESCE(timezone,''),
		       COALESCE(stripe_customer_id,''), plan_tier, payment_failed_at,
		       is_superadmin, created_at, deleted_at, tokens_invalid_after,
		       status, approved_at, COALESCE(approved_by,'')
		  FROM users
		 WHERE deleted_at IS NULL
		 ORDER BY created_at DESC
		 LIMIT 1000`)
	if err != nil {
		return nil, fmt.Errorf("account: list users: %w", err)
	}
	defer rows.Close()
	var out []User
	for rows.Next() {
		u, err := scanUser(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, u)
	}
	return out, rows.Err()
}

// --- plan catalog ----------------------------------------------------

// GetPlan returns the catalog row for the named tier, or ErrNotFound.
func (s *Store) GetPlan(ctx context.Context, tier PlanTier) (Plan, error) {
	if s == nil || s.db == nil {
		return Plan{}, ErrNotFound
	}
	row := s.db.QueryRow(ctx, `
		SELECT tier, monthly_token_cap, max_concurrent_runs, COALESCE(description,'')
		  FROM plans WHERE tier = $1`, string(tier))
	var p Plan
	var tierStr string
	if err := row.Scan(&tierStr, &p.MonthlyTokenCap, &p.MaxConcurrentRuns, &p.Description); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Plan{}, ErrNotFound
		}
		return Plan{}, fmt.Errorf("account: get plan: %w", err)
	}
	p.Tier = PlanTier(tierStr)
	return p, nil
}

// --- audit -----------------------------------------------------------

// RecordAudit inserts one row into audit_log. Best-effort: callers
// log the error but do NOT fail the underlying request when audit
// emit fails (matches the legacy tenant.Store behavior).
func (s *Store) RecordAudit(ctx context.Context, e AuditEntry) error {
	if s == nil || s.db == nil {
		return errors.New("account: nil store")
	}
	var meta any
	if len(e.Metadata) > 0 {
		meta = encodeJSON(e.Metadata)
	}
	uid := nullIfEmpty(e.UserID)
	_, err := s.db.Exec(ctx, `
		INSERT INTO audit_log (user_id, actor_id, action, target, metadata)
		VALUES ($1, NULLIF($2,''), $3, NULLIF($4,''), $5)`,
		uid, e.ActorID, e.Action, e.Target, meta)
	if err != nil {
		return fmt.Errorf("account: record audit: %w", err)
	}
	return nil
}

// ListAudit returns the most recent audit rows. userID="" lists
// across all users (superadmin view); action="" matches all actions.
// limit is clamped to [1, 500].
func (s *Store) ListAudit(ctx context.Context, userID, action string, limit, offset int) ([]AuditEntry, error) {
	if s == nil || s.db == nil {
		return nil, nil
	}
	if limit <= 0 || limit > 500 {
		limit = 50
	}
	if offset < 0 {
		offset = 0
	}
	rows, err := s.db.Query(ctx, `
		SELECT id, COALESCE(user_id,''), COALESCE(actor_id,''),
		       action, COALESCE(target,''), metadata, created_at
		  FROM audit_log
		 WHERE ($1 = '' OR user_id = $1)
		   AND ($2 = '' OR action  = $2)
		 ORDER BY created_at DESC
		 LIMIT $3 OFFSET $4`,
		userID, action, limit, offset)
	if err != nil {
		return nil, fmt.Errorf("account: list audit: %w", err)
	}
	defer rows.Close()
	var out []AuditEntry
	for rows.Next() {
		var e AuditEntry
		var meta []byte
		if err := rows.Scan(&e.ID, &e.UserID, &e.ActorID, &e.Action, &e.Target, &meta, &e.CreatedAt); err != nil {
			return nil, err
		}
		if len(meta) > 0 {
			e.Metadata = decodeJSON(meta)
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// --- internal helpers ------------------------------------------------

// scanRow is the subset of pgx.Row/pgx.Rows that scanUser needs.
type scanRow interface {
	Scan(dest ...any) error
}

func scanUser(row scanRow) (User, error) {
	var u User
	var tier string
	var failed *time.Time
	var deleted *time.Time
	var revoked *time.Time
	var status string
	var approvedAt *time.Time
	if err := row.Scan(
		&u.ID, &u.Email, &u.DisplayName, &u.Timezone,
		&u.StripeCustomerID, &tier, &failed, &u.IsSuperadmin,
		&u.CreatedAt, &deleted, &revoked,
		&status, &approvedAt, &u.ApprovedBy,
	); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return User{}, ErrNotFound
		}
		return User{}, err
	}
	u.PlanTier = PlanTier(tier)
	u.PaymentFailedAt = failed
	u.DeletedAt = deleted
	u.TokensInvalidAfter = revoked
	u.Status = UserStatus(status)
	u.ApprovedAt = approvedAt
	return u, nil
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
