// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package main

// GET /api/v1/me/export — GDPR Article 15 + 20 self-service.
//
// The signed-in user pulls their own personal-data dump (profile
// row + audit rows where they're the subject). No admin
// involvement required.
//
// HARD RULE alignment: nothing fires automatically; the user
// explicitly requests the dump from the Profile page.

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// meExportStore is the minimum surface exportMeHandler needs.
type meExportStore interface {
	GetUser(ctx context.Context, id string) (account.User, error)
	ListAudit(ctx context.Context, userID, action string, limit, offset int) ([]account.AuditEntry, error)
}

// meExport is the JSON blob returned by GET /api/v1/me/export.
// Composed inline (no Store helper) because the constituent reads
// are dirt cheap.
type meExport struct {
	User     account.User         `json:"user"`
	Audit    []account.AuditEntry `json:"audit"`
	Exported time.Time            `json:"exported_at"`
}

// exportMeHandler emits the meExport JSON shape for the signed-in
// user.
//
// Response codes:
//   - 200 with the meExport on success.
//   - 401 when no UserContext.
//   - 404 when the user has no row server-side (Firebase-only
//     account that never inited — nothing to export).
//   - 503 when the account store is nil.
//
// Audit: ActionUserDataExported fires AFTER the read succeeds so
// we don't log empty events for 404s.
func exportMeHandler(store meExportStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		if store == nil {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{
				"error": "export_unavailable",
				"hint":  "account store not configured",
			})
			return
		}
		// 15s timeout absorbs a cold pgx connection. Two reads
		// (user + up to 500 audit rows) at ~50ms p99 each leaves
		// plenty of headroom.
		ctx, cancel := context.WithTimeout(c.Request.Context(), 15*time.Second)
		defer cancel()

		user, err := store.GetUser(ctx, uc.UserID)
		if errors.Is(err, account.ErrNotFound) {
			c.AbortWithStatusJSON(http.StatusNotFound, gin.H{
				"error": "user_not_found",
				"hint":  "no server-side row to export; init the account first",
			})
			return
		}
		if err != nil {
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "export_failed"})
			return
		}
		audit, _ := store.ListAudit(ctx, uc.UserID, "", 500, 0)

		recordAuditBestEffort(c, account.AuditEntry{
			UserID: uc.UserID,
			Action: account.ActionUserDataExported,
			Target: uc.UserID,
			Metadata: map[string]any{
				"kind":        "user",
				"audit_count": len(audit),
			},
		})
		c.Header("Content-Disposition",
			`attachment; filename="me-`+uc.UserID+`-export.json"`)
		c.JSON(http.StatusOK, meExport{
			User:     user,
			Audit:    audit,
			Exported: time.Now().UTC(),
		})
	}
}
