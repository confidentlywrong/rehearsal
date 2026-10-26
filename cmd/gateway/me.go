// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package main

// Per-user identity endpoints mounted under /api/v1/me:
//
//   GET    /api/v1/me        → meHandler        — self lookup
//   POST   /api/v1/me/init   → initMeHandler    — first-time row insert
//   POST   /api/v1/me/logout → logoutHandler    — audit-only logout
//   DELETE /api/v1/me        → deleteAccountHandler — soft-delete self
//
// The Account middleware (mounted in main.go) populates a
// UserContext on the request context BEFORE any of these handlers
// fire. They each read account.FromContextOK and 401 if it's
// missing.

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
)

// meStore is the minimum surface meHandler + initMeHandler need
// from *account.Store. Declared as an interface so tests can
// inject a fake without a real Postgres dial.
type meStore interface {
	GetUser(ctx context.Context, id string) (account.User, error)
	CreateUser(ctx context.Context, id, email, displayName string) (account.User, bool, error)
	UpdateUserProfile(ctx context.Context, userID string, displayName, timezone *string) error
	SoftDeleteUser(ctx context.Context, id string) error
	RevokeTokens(ctx context.Context, id string) error
}

// meResponse is the JSON shape for GET /api/v1/me. Designed for a
// browser SPA — every field present so `<div>{{ data.x }}</div>`
// works without a null guard.
type meResponse struct {
	User struct {
		ID           string `json:"id"`
		Email        string `json:"email,omitempty"`
		DisplayName  string `json:"display_name,omitempty"`
		Timezone     string `json:"timezone,omitempty"`
		PlanTier     string `json:"plan_tier"`
		IsSuperadmin bool   `json:"is_superadmin"`
		// Status carries the manual-approval lifecycle state
		// ("pending", "approved", "rejected", or empty when
		// NeedsInit=true). The SPA routes to the "Account
		// pending" / "Account rejected" screens based on this
		// value so the user never sees blank panels caused by
		// 403 account_pending responses on the underlying APIs.
		Status string `json:"status,omitempty"`
	} `json:"user"`
	NeedsInit bool `json:"needs_init"`
}

// meHandler returns the currently-authenticated user's identity.
// Works both before /me/init has run (NeedsInit=true) and after.
//
// Response codes:
//   - 200 with meResponse on success.
//   - 401 when no UserContext (defensive double-check).
//   - 410 when the user row exists but is soft-deleted. SPA maps
//     this to "sign out + 'account deleted' toast".
//   - 503 when the account store is nil (degraded mode).
func meHandler(store meStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		resp := meResponse{}
		resp.User.ID = uc.UserID
		resp.User.Email = uc.Email

		if store == nil {
			// Degraded mode: surface the middleware identity so
			// the SPA can still render a header while ops
			// reattaches the DB.
			resp.User.PlanTier = string(account.PlanFree)
			resp.NeedsInit = true
			c.JSON(http.StatusOK, resp)
			return
		}
		u, err := store.GetUser(c.Request.Context(), uc.UserID)
		switch {
		case errors.Is(err, account.ErrNotFound):
			resp.User.PlanTier = string(account.PlanFree)
			resp.NeedsInit = true
			c.JSON(http.StatusOK, resp)
			return
		case err != nil:
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "me_lookup_failed"})
			return
		}
		// NOTE: no deleted-row check here. The Account middleware
		// (P1.A, 2026-06-22) runs the resolver against
		// Store.GetUserAnyState and emits 410 ErrAccountDeleted
		// BEFORE this handler ever fires, so by the time we read
		// store.GetUser (which filters deleted_at IS NULL) the row is
		// guaranteed alive. The earlier `if u.DeletedAt != nil` branch
		// was dead code.
		resp.User.Email = u.Email
		resp.User.DisplayName = u.DisplayName
		resp.User.Timezone = u.Timezone
		resp.User.PlanTier = string(u.PlanTier)
		resp.User.IsSuperadmin = u.IsSuperadmin
		resp.User.Status = string(u.Status)
		// SAAS_MODE=off bypass (2026-07-02): single-tenant local
		// deployments have no admin-approval workflow. CreateUser
		// still inserts rows in StatusPending (uniform DB state so
		// a future switch to SAAS_MODE=on is a config flip rather
		// than a data migration), but ``make start`` users must
		// not be trapped on the SPA's /pending-approval screen.
		// Reporting the status as "approved" in the /me response
		// keeps the frontend approval.guard happy without touching
		// stored rows. Pairs with the matching bypass in
		// ``internal/middleware/account.go`` that skips the
		// server-side 403 gate under the same env condition.
		if !account.SaaSModeEnabled() {
			resp.User.Status = string(account.StatusApproved)
		}
		c.JSON(http.StatusOK, resp)
	}
}

// initMeHandler implements POST /api/v1/me/init — first-time
// per-user provisioning.
//
// Idempotent: a second call after init returns 200 with the
// existing row instead of writing. First call: CreateUser with
// PlanFree, optional display_name / timezone from body.
//
// Hidden-email providers (phone auth, Apple "Hide My Email"
// without a relay) get a synthetic "no-email+<uid>@users.invalid"
// address. RFC 2606 reserves .invalid so the placeholder can
// never collide with a real address.
//
// Response codes:
//   - 201 Created on first init.
//   - 200 OK on idempotent re-init.
//   - 401 when no UserContext.
//   - 410 when the user previously self-deleted (refuse to
//     resurrect silently).
//   - 503 when the store is nil.
func initMeHandler(store meStore) gin.HandlerFunc {
	type req struct {
		DisplayName string `json:"display_name"`
		Timezone    string `json:"timezone"`
	}
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		if store == nil {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{
				"error": "init_unavailable",
				"hint":  "account store not configured",
			})
			return
		}
		var body req
		if c.Request.ContentLength > 0 {
			if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
				log.Printf("/me/init: ignoring malformed JSON body: %v", err)
			}
		}

		ctx := c.Request.Context()

		// Idempotent: existing row → return it (200 not 201).
		// No deleted-row check here — the Account middleware (P1.A,
		// 2026-06-22) intercepts soft-deleted users with 410 via the
		// resolver's ErrAccountDeleted before this handler runs.
		if existing, err := store.GetUser(ctx, uc.UserID); err == nil {
			c.JSON(http.StatusOK, gin.H{
				"id":           existing.ID,
				"email":        existing.Email,
				"display_name": existing.DisplayName,
				"timezone":     existing.Timezone,
				"plan_tier":    string(existing.PlanTier),
				"status":       string(existing.Status),
				"created":      false,
			})
			return
		} else if !errors.Is(err, account.ErrNotFound) {
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "init_lookup_failed"})
			return
		}

		// Email: real claim → use as-is. Missing → synthesise an
		// .invalid placeholder keyed on the UID so the same
		// hidden-email user re-inits into the same row.
		email := strings.TrimSpace(uc.Email)
		emailWasSynthetic := false
		if email == "" {
			email = syntheticEmailForUID(uc.UserID)
			emailWasSynthetic = true
		}

		displayName := strings.TrimSpace(body.DisplayName)
		if len(displayName) > maxDisplayNameLen {
			displayName = displayName[:maxDisplayNameLen]
		}
		timezone := strings.TrimSpace(body.Timezone)
		if timezone != "" {
			// Reject obviously bad zones; the store will accept
			// whatever survives. An empty value is the "no
			// preference" sentinel.
			if _, err := time.LoadLocation(timezone); err != nil {
				timezone = ""
			}
		}

		user := account.User{
			ID:          uc.UserID,
			Email:       email,
			DisplayName: displayName,
			Timezone:    timezone,
			PlanTier:    account.PlanFree,
		}
		newUser, created, err := store.CreateUser(ctx, user.ID, user.Email, user.DisplayName)
		if err != nil {
			// L1 (2026-06-25 audit): branch on ErrEmailTaken so
			// the SPA can show a sane "account already exists,
			// sign in instead" prompt rather than a generic
			// 500. The yes/no signal here is intentional — for
			// a self-service SaaS the discoverability of "your
			// email is taken" is standard UX and worth more
			// than the marginal enumeration risk (the same
			// signal leaks via the Firebase sign-up endpoint
			// anyway).
			if errors.Is(err, account.ErrEmailTaken) {
				c.AbortWithStatusJSON(http.StatusConflict, gin.H{
					"error": "email_taken",
				})
				return
			}
			log.Printf("WARN: /me/init CreateUser failed user=%s: %v", uc.UserID, err)
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "init_failed"})
			return
		}
		// CreateUser ignores timezone. If the caller supplied
		// one, persist it now — the row was just created so an
		// UpdateUserProfile call is safe (no race).
		//
		// P1-8 (2026-06-23 audit): log on failure so silent drops
		// become discoverable. The user is NOT locked out by this
		// path — the row already exists, NeedsInit is now false,
		// and the user can re-patch the field later via
		// PATCH /api/v1/me/profile.
		if timezone != "" && created {
			tz := timezone
			if err := store.UpdateUserProfile(ctx, uc.UserID, nil, &tz); err != nil {
				log.Printf("WARN: /me/init timezone patch failed for user=%s tz=%q: %v "+
					"(user row IS created; client can retry via PATCH /me/profile)",
					uc.UserID, tz, err)
			}
		}
		meta := map[string]any{
			"email":     email,
			"plan_tier": string(account.PlanFree),
		}
		if emailWasSynthetic {
			meta["email_synthetic"] = true
		}
		// P3.B (2026-06-22 audit): only audit ActionUserCreated on a
		// real INSERT — a concurrent /me/init from a second tab that
		// loses the 23505 race must not produce a duplicate audit row.
		// The first tab's call already wrote the audit entry; tab 2
		// just returns the existing row.
		if created {
			recordAuditBestEffort(c, account.AuditEntry{
				UserID:   uc.UserID,
				Action:   account.ActionUserCreated,
				Target:   uc.UserID,
				Metadata: meta,
			})
			// Manual-approval flow (2026-06-26): every fresh row
			// lands in StatusPending. Emit a structured log line so
			// the GCP log-based metric can fire the operator's
			// existing email channel, and an audit row so the
			// "Audit" tab shows the queue history. A superadmin sign-
			// up auto-approves via the migration backfill, so this
			// only fires for non-superadmin signups in practice.
			log.Printf("AUDIT signup_pending user_id=%s email_hash=%s", uc.UserID, logging.HashEmail(email))
			recordAuditBestEffort(c, account.AuditEntry{
				UserID:   uc.UserID,
				Action:   account.ActionUserSignupPending,
				Target:   uc.UserID,
				Metadata: map[string]any{"email_hash": logging.HashEmail(email)},
			})
		}
		// P1.C (2026-06-22 audit): the /auth/exchange that brought
		// this user in granted only a 5-minute __session cookie
		// because they were NeedsInit=true. Now that the row exists,
		// re-stamp the cookie at the full 1-hour lifetime so the
		// rest of the session is indistinguishable from an
		// established user's. Without this the user would be bounced
		// back to /login after 5 minutes even though they completed
		// init successfully.
		refreshSessionCookieFullLifetime(c)
		status := http.StatusOK
		if created {
			status = http.StatusCreated
		}
		c.JSON(status, gin.H{
			"id":           uc.UserID,
			"email":        email,
			"display_name": displayName,
			"timezone":     timezone,
			"plan_tier":    string(account.PlanFree),
			"status":       string(newUser.Status),
			"created":      created,
		})
	}
}

// syntheticEmailForUID builds the placeholder email used when a
// Firebase identity arrives without an email claim.
//
// Format: "no-email+<sanitized-uid>@users.invalid". `.invalid` is
// reserved by RFC 2606 §2; the prefix makes the placeholder origin
// obvious to operators. Callers MUST NOT attempt to send mail
// to these addresses.
func syntheticEmailForUID(uid string) string {
	clean := strings.ToLower(strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z',
			r >= 'A' && r <= 'Z',
			r >= '0' && r <= '9':
			return r
		default:
			return -1
		}
	}, uid))
	if clean == "" {
		clean = "anon"
	}
	return "no-email+" + clean + "@users.invalid"
}

// logoutHandler honours POST /api/v1/me/logout — server-side
// logout. The SPA MUST still call firebase.auth().signOut() after
// this returns 204 to clear the IndexedDB-backed ID token cache.
//
// In addition to recording the audit entry, this handler:
//   - Stamps users.tokens_invalid_after = NOW() so any token whose
//     `iat` predates the logout is rejected by the resolver on the
//     next request (P3 #17 in the 2026-06-19 audit). This is the
//     light-touch alternative to wiring the Firebase Admin SDK: it
//     revokes at OUR verifier without a new dependency. A subsequent
//     legitimate sign-in mints a token with a fresh `iat` and is
//     accepted again.
//   - Clears the HttpOnly __session cookie set by
//     authExchangeHandler so the browser stops attaching the
//     (still-valid-until-expiry) Firebase ID token to subsequent
//     requests (P1 #6 in the same audit).
//
// P2.F (2026-06-22 audit, accepted-as-is): the revoke watermark is
// per-USER, not per-device. Logging out on one browser invalidates
// every other device's outstanding ID token for the same Firebase
// UID. This is documented behaviour, not a bug; per-device revoke
// would require an opaque session table (one row per device, the
// cookie carrying a session id instead of the raw Firebase token)
// which is out of scope for the current architecture. The /login
// flow doesn't expose this to the user, so a multi-device account
// owner who signs out on desktop will silently see their phone
// bounce to /login on the next API call — worth a future fix when
// the SaaS shell adds a sessions UI.
//
// P2.D (2026-06-22 audit): for NeedsInit users (no users-table row
// yet), RevokeTokens returns ErrNotFound and there is no place to
// store the watermark. We swallow that case and still clear the
// cookie, accepting that a stolen bearer from the brief NeedsInit
// window remains usable until natural Firebase ID-token expiry
// (≤1 hour). This gap is closed in defense-in-depth by App Check
// server-side verification (P2.B) once enabled, which blocks calls
// that don't carry a valid App Check token regardless of bearer state.
//
// Response codes:
//   - 204 on success (idempotent).
//   - 401 when there is no UserContext (caller was never
//     authenticated; nothing to log out of).
func logoutHandler(store meStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		recordAuditBestEffort(c, account.AuditEntry{
			UserID: uc.UserID,
			Action: account.ActionUserLoggedOut,
			Target: uc.UserID,
		})
		// Best-effort revoke: a DB outage here must not prevent the
		// cookie clear (the cookie clear alone already breaks BFF
		// auth). We log so an operator notices, but still return 204.
		if store != nil {
			err := store.RevokeTokens(c.Request.Context(), uc.UserID)
			switch {
			case err == nil:
				// revoked
			case errors.Is(err, account.ErrNotFound):
				// P2.D: NeedsInit user logging out before /me/init.
				// No row to watermark; cookie clear is the only
				// revocation. Log so an operator sees the gap.
				log.Printf("LOGOUT: no users row yet for uid=%s (NeedsInit logout); cookie cleared, watermark skipped",
					uc.UserID)
			default:
				log.Printf("LOGOUT: revoke tokens failed user=%s: %v (continuing)", uc.UserID, err)
			}
		}
		clearSessionCookie(c)
		c.Status(http.StatusNoContent)
	}
}

// deleteAccountHandler implements DELETE /api/v1/me — the user's
// self-service "delete my account" entry point.
//
// Semantics:
//   - Soft delete: users.deleted_at is stamped to now(). The row
//     stays so audit_log FKs survive and the email UNIQUE
//     constraint blocks accidental same-email re-signup.
//   - A separate operator-triggered sweep
//     (POST /admin/users/hard-delete) finalises the row after the
//     retention window.
//   - Best-effort audit (user.erased phase=soft) with the email
//     at deletion time so a reviewer can reconstruct the row
//     after a future hard-delete sweep.
//
// We do NOT call into Firebase to revoke the refresh token —
// adding the Admin SDK would be a heavy dependency. The boundary
// check (meHandler → 410, initMeHandler → 410) is sufficient.
//
// Response codes:
//   - 204 on success (account is soft-deleted).
//   - 204 idempotent — already soft-deleted.
//   - 401 when no UserContext.
//   - 503 when the account store is nil.
func deleteAccountHandler(store meStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		if store == nil {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{
				"error": "delete_unavailable",
				"hint":  "account store not configured",
			})
			return
		}
		emailAtDeletion := uc.Email
		// Look up the row for the email snapshot stored in audit.
		// No deleted-row branch — the Account middleware (P1.A,
		// 2026-06-22) returns 410 for soft-deleted users via the
		// resolver's ErrAccountDeleted, so this handler only runs for
		// live users. A second DELETE on an already-deleted account is
		// stopped at the middleware with 410 (idempotent semantics for
		// the SPA: same outcome — the user is gone).
		if u, err := store.GetUser(c.Request.Context(), uc.UserID); err == nil {
			if u.Email != "" {
				emailAtDeletion = u.Email
			}
		}
		if err := store.SoftDeleteUser(c.Request.Context(), uc.UserID); err != nil {
			if errors.Is(err, account.ErrNotFound) {
				// Firebase-authenticated but never inited — no
				// server-side row to delete. Treat as success.
				c.Status(http.StatusNoContent)
				return
			}
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "delete_failed"})
			return
		}
		recordAuditBestEffort(c, account.AuditEntry{
			UserID: uc.UserID,
			Action: account.ActionUserErased,
			Target: uc.UserID,
			Metadata: map[string]any{
				"phase": "soft",
				"email": emailAtDeletion,
			},
		})
		c.Status(http.StatusNoContent)
	}
}
