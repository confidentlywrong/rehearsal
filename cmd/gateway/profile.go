// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package main

// Per-user profile endpoints mounted under /api/v1/me/profile:
//
//   GET   /api/v1/me/profile  → profileHandler
//   PATCH /api/v1/me/profile  → updateProfileHandler
//
// Profile is just the display_name + timezone the user picks for
// themselves. Plan tier, superadmin flag, email, etc. live on the
// /api/v1/me response; that endpoint is the single source of
// truth for "who am I and what can I do". This one is the slow,
// human-edited subset.

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// profileStore is the minimum surface the profile handlers need
// from *account.Store. Declared as an interface so tests can
// inject a fake without a real Postgres dial.
type profileStore interface {
	GetUser(ctx context.Context, id string) (account.User, error)
	UpdateUserProfile(ctx context.Context, userID string, displayName, timezone *string) error
}

// profileResponse is the JSON shape returned by GET /api/v1/me/profile.
// All fields are always present so the SPA can bind
// `<input [value]="data.display_name">` without a null guard.
type profileResponse struct {
	UserID      string `json:"user_id"`
	Email       string `json:"email,omitempty"`
	DisplayName string `json:"display_name"`
	Timezone    string `json:"timezone"`
}

// profileHandler returns the current user's profile row.
//
// Response codes:
//   - 200 with profileResponse on success.
//   - 401 when no UserContext.
//   - 404 when the user has no row yet (init not run).
//   - 503 when the account store is nil.
func profileHandler(store profileStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "profile store not configured"})
			return
		}
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		u, err := store.GetUser(c.Request.Context(), uc.UserID)
		if err != nil {
			if errors.Is(err, account.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "user_not_found"})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "profile_lookup_failed"})
			return
		}
		c.JSON(http.StatusOK, profileResponse{
			UserID:      u.ID,
			Email:       u.Email,
			DisplayName: u.DisplayName,
			Timezone:    u.Timezone,
		})
	}
}

// profileUpdateRequest is the PATCH body. Pointers preserve
// partial-update semantics: nil → leave unchanged; non-nil →
// write (empty string clears the column).
type profileUpdateRequest struct {
	DisplayName *string `json:"display_name,omitempty"`
	Timezone    *string `json:"timezone,omitempty"`
}

// maxDisplayNameLen / maxTimezoneLen cap the per-field write size
// so a hostile caller can't dump megabytes into the users row.
// 80 / 64 match the input element maxlength attributes on the SPA.
const (
	maxDisplayNameLen = 80
	maxTimezoneLen    = 64
)

// updateProfileHandler patches the user's profile row.
//
// Body: {"display_name": "...", "timezone": "Europe/London"}
// Either field is optional; omitting both is a 200 no-op.
//
// Response codes:
//   - 200 with profileResponse on success.
//   - 400 on malformed JSON, oversize field, or invalid timezone.
//   - 401 when no UserContext.
//   - 404 when the user row does not exist (caller should /init first).
//   - 503 when the store is nil.
func updateProfileHandler(store profileStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		if store == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "profile store not configured"})
			return
		}
		uc, ok := account.FromContextOK(c.Request.Context())
		if !ok || uc.UserID == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
			return
		}
		var body profileUpdateRequest
		// Decode tolerantly — an empty body is a valid no-op
		// PATCH. Only malformed JSON is a 400.
		if c.Request.ContentLength != 0 {
			if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_json", "hint": err.Error()})
				return
			}
		}
		// Trim + validate. Pointers stay nil for absent fields so
		// the store's per-column SET skips them entirely.
		var changedFields []string
		if body.DisplayName != nil {
			trimmed := strings.TrimSpace(*body.DisplayName)
			if len(trimmed) > maxDisplayNameLen {
				c.JSON(http.StatusBadRequest, gin.H{
					"error":     "display_name_too_long",
					"max_chars": maxDisplayNameLen,
				})
				return
			}
			body.DisplayName = &trimmed
			changedFields = append(changedFields, "display_name")
		}
		if body.Timezone != nil {
			trimmed := strings.TrimSpace(*body.Timezone)
			if len(trimmed) > maxTimezoneLen {
				c.JSON(http.StatusBadRequest, gin.H{
					"error":     "timezone_too_long",
					"max_chars": maxTimezoneLen,
				})
				return
			}
			// Validate against the IANA tz database. An empty
			// string is allowed (explicit clear: caller wants to
			// remove their preference). A non-empty value MUST
			// round-trip through time.LoadLocation so a typo
			// like "Europ/Berlin" cannot poison the row.
			if trimmed != "" {
				if _, err := time.LoadLocation(trimmed); err != nil {
					c.JSON(http.StatusBadRequest, gin.H{
						"error":    "timezone_invalid",
						"hint":     "must be an IANA zone name (e.g. Europe/London, America/New_York, UTC)",
						"timezone": trimmed,
					})
					return
				}
			}
			body.Timezone = &trimmed
			changedFields = append(changedFields, "timezone")
		}
		if err := store.UpdateUserProfile(c.Request.Context(), uc.UserID, body.DisplayName, body.Timezone); err != nil {
			if errors.Is(err, account.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "user_not_found"})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "profile_update_failed"})
			return
		}
		// Emit the audit row with the field NAMES that changed
		// — we deliberately do NOT log the values themselves
		// (display names can be PII, timezones a weak
		// fingerprint). Empty changedFields → no-op PATCH → no
		// audit entry.
		if len(changedFields) > 0 {
			recordAuditBestEffort(c, account.AuditEntry{
				UserID: uc.UserID,
				Action: "user.profile_updated",
				Target: uc.UserID,
				Metadata: map[string]any{
					"fields": changedFields,
				},
			})
		}
		// Re-read so the response carries the post-update values
		// (matters when the caller PATCHed only one field and
		// wants to render the other unchanged).
		u, err := store.GetUser(c.Request.Context(), uc.UserID)
		if err != nil {
			c.JSON(http.StatusOK, gin.H{"updated": true})
			return
		}
		c.JSON(http.StatusOK, profileResponse{
			UserID:      u.ID,
			Email:       u.Email,
			DisplayName: u.DisplayName,
			Timezone:    u.Timezone,
		})
	}
}
