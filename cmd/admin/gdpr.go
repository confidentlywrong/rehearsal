// Copyright 2026 Christian Haeussinger. Apache-2.0.

package main

// Per-user GDPR control-plane handlers. Three superadmin-only
// endpoints (gated by middleware.RequireSuperadmin at the
// route layer in setupRouter):
//
//   DELETE /admin/users/:id           → softDeleteUserHandler
//   GET    /admin/users/:id/export    → exportUserHandler
//   POST   /admin/users/:id/erase     → eraseUserHandler
//   POST   /admin/users/hard-delete   → hardDeleteExpiredUsersHandler
//
// Destructive handlers audit BEFORE the write so the email is
// captured while the row still exists (after the DELETE the
// audit_log.user_id FK SET-NULLs).
//
// HARD RULE alignment: every handler is operator-triggered;
// nothing auto-fires.

import (
	"log"
	"context"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// softDeleteUserHandler returns a gin handler that flips
// deleted_at on the user row. The row stays in the DB (FKs intact)
// for a retention grace window; hardDeleteExpiredUsersHandler
// finalises later.
//
// Response codes:
//   - 204 No Content on success.
//   - 400 when :id is blank.
//   - 404 when the user does not exist or is already soft-deleted.
//   - 503 when the account store is nil (degraded mode).
func softDeleteUserHandler(store *account.Store) gin.HandlerFunc {
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		userID := strings.TrimSpace(c.Param("id"))
		if userID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "user id required"})
			return
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 10*time.Second)
		defer cancel()

		// Capture email for audit before the row is masked.
		var emailAtDelete string
		if u, err := store.GetUser(ctx, userID); err == nil {
			emailAtDelete = u.Email
		}

		// Audit FIRST so a partial failure mid-write still leaves
		// a paper trail. The audit row carries the email since
		// the user_id FK alone might point at a row we just
		// soft-deleted.
		recordAuditBestEffort(ctx, store, c, account.AuditEntry{
			UserID: userID,
			Action: account.ActionUserErased,
			Target: "user:" + userID,
			Metadata: map[string]any{
				"phase":          "soft",
				"email_at_erase": emailAtDelete,
				"source":         "manual",
			},
		})

		err := store.SoftDeleteUser(ctx, userID)
		switch {
		case err == nil:
			c.Status(http.StatusNoContent)
		case errors.Is(err, account.ErrNotFound):
			c.JSON(http.StatusNotFound, gin.H{
				"error": "user not found",
				"id":    userID,
			})
		default:
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
		}
	}
}

// userExport is the GDPR Article 15/20 dump for one user.
// Composed inline (no Store helper) because the constituent reads
// are dirt cheap and only superadmin endpoints touch them.
type userExport struct {
	User    account.User         `json:"user"`
	Audit   []account.AuditEntry `json:"audit"`
	Exported time.Time           `json:"exported_at"`
}

// exportUserHandler returns a gin handler that emits a userExport
// JSON blob. Audit fires AFTER the read succeeds so we don't log
// empty events for 404s.
//
// Response codes:
//   - 200 with the userExport on success.
//   - 400 when :id is blank.
//   - 404 when the user does not exist.
//   - 503 when the account store is nil.
//
// Content-Disposition surfaces a default filename so a browser
// "Save As" lands a useful name on disk.
func exportUserHandler(store *account.Store) gin.HandlerFunc {
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		userID := strings.TrimSpace(c.Param("id"))
		if userID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "user id required"})
			return
		}
		// Generous timeout: GetUser + up to 500 audit rows on a
		// cold pgx conn is well under 5s on p99; 15s leaves slack.
		ctx, cancel := context.WithTimeout(c.Request.Context(), 15*time.Second)
		defer cancel()

		user, err := store.GetUser(ctx, userID)
		if errors.Is(err, account.ErrNotFound) {
			c.JSON(http.StatusNotFound, gin.H{
				"error": "user not found",
				"id":    userID,
			})
			return
		}
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		// Pull up to 500 audit rows for this user (store clamps
		// internally). 500 is the per-page hard cap; an export
		// truncated at that depth still satisfies Article 15 for
		// any account in this product's lifetime.
		audit, _ := store.ListAudit(ctx, userID, "", 500, 0)

		recordAuditBestEffort(ctx, store, c, account.AuditEntry{
			UserID: userID,
			Action: account.ActionUserDataExported,
			Target: "user:" + userID,
			Metadata: map[string]any{
				"kind":        "user",
				"audit_count": len(audit),
			},
		})
		c.Header("Content-Disposition",
			`attachment; filename="user-`+userID+`-export.json"`)
		c.JSON(http.StatusOK, userExport{
			User:     user,
			Audit:    audit,
			Exported: time.Now().UTC(),
		})
	}
}

// eraseUserHandler returns a gin handler that HARD-deletes a user
// row. The GDPR Article 17 "right to be forgotten" terminal step
// after softDeleteUserHandler. Typically run after the retention
// grace window (today: discretionary, no scheduler).
//
// Response codes:
//   - 204 No Content on success.
//   - 400 when :id is blank.
//   - 404 when the user does not exist.
//   - 503 when the account store is nil.
//
// HARD RULE alignment: operator-triggered; nothing auto-erases.
// The handler does NOT enforce a grace window — that's an operator
// policy decision today, not a code constraint.
func eraseUserHandler(store *account.Store) gin.HandlerFunc {
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		userID := strings.TrimSpace(c.Param("id"))
		if userID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "user id required"})
			return
		}
		ctx, cancel := context.WithTimeout(c.Request.Context(), 10*time.Second)
		defer cancel()

		var (
			emailAtErase   string
			softDeletedISO string
		)
		if u, err := store.GetUser(ctx, userID); err == nil {
			emailAtErase = u.Email
			if u.DeletedAt != nil {
				softDeletedISO = u.DeletedAt.UTC().Format(time.RFC3339)
			}
		}

		// Audit FIRST — once the DELETE commits the audit_log
		// FK SET-NULLs, so a row inserted after the cascade
		// would carry user_id=NULL and be hard to attribute.
		recordAuditBestEffort(ctx, store, c, account.AuditEntry{
			UserID: userID,
			Action: account.ActionUserErased,
			Target: "user:" + userID,
			Metadata: map[string]any{
				"phase":           "hard",
				"email_at_erase":  emailAtErase,
				"soft_deleted_at": softDeletedISO,
				"source":          "manual",
			},
		})

		err := store.HardDeleteUser(ctx, userID)
		switch {
		case err == nil:
			c.Status(http.StatusNoContent)
		case errors.Is(err, account.ErrNotFound):
			c.JSON(http.StatusNotFound, gin.H{
				"error": "user not found",
				"id":    userID,
			})
		default:
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
		}
	}
}

// hardDeleteExpiredUsersHandler returns a gin handler for
// POST /admin/users/hard-delete?older_than_days=N — the GDPR
// Article 17 sweep that finalises soft → hard delete for every
// user past the retention window.
//
// Response codes:
//   - 200 with {"purged": N, "older_than_days": M, "cutoff": "..."} on success.
//   - 400 when older_than_days is missing, non-numeric, or < 1.
//     We reject 0 to defend against a typo that would purge a
//     user who soft-deleted seconds ago.
//   - 503 when the account store is nil.
//   - 500 on any other store error.
//
// HARD RULE: operator-triggered only — no scheduler.
func hardDeleteExpiredUsersHandler(store *account.Store) gin.HandlerFunc {
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "account store not configured",
			})
			return
		}
		raw := strings.TrimSpace(c.Query("older_than_days"))
		if raw == "" {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": "older_than_days query param required",
				"hint":  "e.g. ?older_than_days=30",
			})
			return
		}
		days, err := strconv.Atoi(raw)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": "older_than_days must be an integer",
				"got":   raw,
			})
			return
		}
		if days < 1 {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": "older_than_days must be >= 1",
				"got":   days,
				"hint":  "0 would purge users who soft-deleted seconds ago",
			})
			return
		}
		// Compute cutoff in UTC so audit metadata is
		// timezone-stable across operator workstations.
		cutoff := time.Now().UTC().Add(-time.Duration(days) * 24 * time.Hour)

		ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
		defer cancel()

		purged, err := store.HardDeleteExpiredUsers(ctx, cutoff)
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		// Audit AFTER the sweep so purged_count is real. Best-effort:
		// if the audit insert itself fails we still return success
		// to the operator — re-issuing the sweep to "force" an
		// audit would purge a fresh batch of users, which is wrong.
		recordAuditBestEffort(ctx, store, c, account.AuditEntry{
			Action: account.ActionUsersBulkErased,
			Metadata: map[string]any{
				"purged_count":    purged,
				"older_than_days": days,
				"cutoff":          cutoff.Format(time.RFC3339),
				"source":          "manual",
			},
		})

		c.JSON(http.StatusOK, gin.H{
			"purged":          purged,
			"older_than_days": days,
			"cutoff":          cutoff.Format(time.RFC3339),
		})
	}
}
