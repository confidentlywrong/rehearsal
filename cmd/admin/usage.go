// Copyright 2026 Christian Haeussinger. Apache-2.0.

package main

import (
	"log"
	"context"
	"errors"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/gin-gonic/gin"
)

// Superadmin-only per-user usage viewer for the admin dashboard.
//
// Endpoint: GET /admin/users/:id/usage?year_month=YYYY-MM
//   - 200 with {user_id, year_month, total_tokens, cost_micros,
//     updated_at, agents:[...]} on success.
//   - 400 when :id is blank or year_month is malformed.
//   - 404 when the user_id is unknown.
//   - 503 when the usage reader is nil (degraded mode / DB down).
//
// Defaults year_month to the current UTC month.

// usageReaderForAdmin is wired by main() once the billing.Store is
// available. Stays nil in tests / degraded mode, in which case the
// handler returns 503.
var usageReaderForAdmin billing.UsageReader

// monthRe matches YYYY-MM.
var monthRe = regexp.MustCompile(`^\d{4}-(0[1-9]|1[0-2])$`)

type userUsageResponse struct {
	UserID      string                   `json:"user_id"`
	YearMonth   string                   `json:"year_month"`
	TotalTokens int64                    `json:"total_tokens"`
	CostMicros  int64                    `json:"cost_micros"`
	UpdatedAt   *time.Time               `json:"updated_at,omitempty"`
	Agents      []billing.AgentBreakdown `json:"agents"`
}

func currentYearMonthUTC(now func() time.Time) string {
	return now().UTC().Format("2006-01")
}

func usageUserHandler(reader billing.UsageReader, now func() time.Time) gin.HandlerFunc {
	if now == nil {
		now = time.Now
	}
	return func(c *gin.Context) {
		if reader == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error": "usage reader not configured",
			})
			return
		}
		userID := strings.TrimSpace(c.Param("id"))
		if userID == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "user id required"})
			return
		}
		yearMonth := strings.TrimSpace(c.Query("year_month"))
		if yearMonth == "" {
			yearMonth = currentYearMonthUTC(now)
		} else if !monthRe.MatchString(yearMonth) {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": "year_month must be YYYY-MM",
				"got":   yearMonth,
			})
			return
		}

		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()

		resp := userUsageResponse{
			UserID:    userID,
			YearMonth: yearMonth,
			Agents:    []billing.AgentBreakdown{},
		}

		rollup, err := reader.GetMonthlyUsage(ctx, userID, yearMonth)
		switch {
		case err == nil:
			resp.TotalTokens = rollup.TotalTokens
			resp.CostMicros = rollup.CostMicros
			ts := rollup.UpdatedAt
			resp.UpdatedAt = &ts
		case errors.Is(err, billing.ErrNoRollup):
			// fall through with zero values
		case errors.Is(err, account.ErrNotFound):
			c.JSON(http.StatusNotFound, gin.H{
				"error": "user not found",
				"id":    userID,
			})
			return
		default:
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		if agents, agErr := reader.GetAgentBreakdown(ctx, userID, yearMonth); agErr == nil {
			for i := range agents {
				if agents[i].AgentType == "" {
					agents[i].AgentType = "unknown"
				}
			}
			resp.Agents = agents
		}

		c.JSON(http.StatusOK, resp)
	}
}
