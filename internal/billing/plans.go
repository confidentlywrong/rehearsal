// Copyright 2026 Christian Haeussinger. Apache-2.0.

package billing

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

// Plan describes one row of the `plans` table seeded by
// migrations/0001_users.sql. The schema uses -1 as a sentinel for
// "unlimited" (enterprise tier); helpers below honour that contract
// so callers do not have to scatter `if cap == -1` checks across
// every quota gate.
type Plan struct {
	Tier              string `json:"tier"`
	MonthlyTokenCap   int64  `json:"monthly_token_cap"`
	MaxConcurrentRuns int    `json:"max_concurrent_runs"`
	Description       string `json:"description"`
}

// IsUnlimitedTokens reports whether this plan has no monthly token
// cap.
func (p Plan) IsUnlimitedTokens() bool { return p.MonthlyTokenCap < 0 }

// IsUnlimitedConcurrent reports whether this plan has no concurrent
// runs cap.
func (p Plan) IsUnlimitedConcurrent() bool { return p.MaxConcurrentRuns < 0 }

// QuotaState is the result of a pre-flight quota check. Carries
// enough context for both the JSON response (when blocked) and the
// X-Usage-Warning header (when near the cap).
//
// "Never invent data" rule: when the rollup row is missing for the
// current month — i.e. the user has not consumed any tokens yet —
// TokensUsed is 0 from a *real* zero (the SELECT returned no row),
// NOT from a fabricated default.
type QuotaState struct {
	UserID            string  `json:"user_id"`
	YearMonth         string  `json:"year_month"`
	PlanTier          string  `json:"plan_tier"`
	TokensUsed        int64   `json:"tokens_used"`
	TokensCap         int64   `json:"tokens_cap"` // -1 = unlimited
	PercentUsed       float64 `json:"percent_used"`
	Exhausted         bool    `json:"exhausted"`
	WarnHigh          bool    `json:"warn_high"`           // ≥ 80% but not exhausted
	MaxConcurrentRuns int     `json:"max_concurrent_runs"` // -1 = unlimited
}

// WarnThresholdPercent is the soft warning threshold: at or above
// 80%, surface an X-Usage-Warning header but do not block.
const WarnThresholdPercent = 80.0

// QuotaChecker is the minimal interface the gateway needs to perform
// a pre-flight quota gate. *Store satisfies it.
type QuotaChecker interface {
	CheckQuota(ctx context.Context, userID string) (QuotaState, error)
}

// ErrPlanNotFound is returned by GetUserPlan when the user references
// a plan_tier that is not present in the `plans` table. Treated as
// a permanent configuration error — the gateway logs and allows the
// request through (fail-open) rather than locking customers out
// because of a bad data row.
var ErrPlanNotFound = errors.New("billing: plan tier not found")

// ErrUserNotFound is returned by GetUserPlan when the user does not
// exist in the `users` table. Distinct from ErrPlanNotFound so the
// caller can decide whether to log loudly (probable bug) or silently
// skip (request from an unauthenticated/local-dev caller).
var ErrUserNotFound = errors.New("billing: user not found")

// GetUserPlan looks up the plan tier that applies to one user and
// returns the full row from the `plans` table. A single JOIN keeps
// the round-trip cost flat.
func (s *Store) GetUserPlan(ctx context.Context, userID string) (Plan, error) {
	if s == nil || s.db == nil {
		return Plan{}, errors.New("billing: DB not connected")
	}
	if userID == "" {
		return Plan{}, errors.New("billing: GetUserPlan requires user_id")
	}
	row := s.db.QueryRow(ctx, `
		SELECT p.tier, p.monthly_token_cap, p.max_concurrent_runs,
		       COALESCE(p.description, '')
		FROM users u
		JOIN plans p ON p.tier = u.plan_tier
		WHERE u.id = $1 AND u.deleted_at IS NULL
	`, userID)
	var p Plan
	if err := row.Scan(&p.Tier, &p.MonthlyTokenCap, &p.MaxConcurrentRuns,
		&p.Description); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// We can't distinguish here whether the user is missing
			// or the plan tier is missing without a second query.
			// Return the less-alarming "plan not found" — the user's
			// existence is already implied by the surrounding account
			// middleware.
			return Plan{}, ErrPlanNotFound
		}
		return Plan{}, fmt.Errorf("billing: get user plan: %w", err)
	}
	return p, nil
}

// CheckQuota runs the pre-flight gate for one user, returning a
// QuotaState the caller can inspect to decide between 200 (allowed),
// 402 (exhausted), or 200+warning-header.
//
// Behavior contract:
//   - Plan with unlimited cap → Exhausted=false, WarnHigh=false,
//     PercentUsed=0 (cosmetically; dashboard renders "unlimited"
//     based on TokensCap=-1).
//   - Missing rollup row → treated as 0 used (NOT an error).
//   - Missing plan / missing user → returns ErrPlanNotFound. Caller
//     should fail-open and log.
//   - DB errors propagate.
func (s *Store) CheckQuota(ctx context.Context, userID string) (QuotaState, error) {
	if s == nil || s.db == nil {
		return QuotaState{}, errors.New("billing: DB not connected")
	}
	if userID == "" {
		return QuotaState{}, errors.New("billing: CheckQuota requires user_id")
	}
	plan, err := s.GetUserPlan(ctx, userID)
	if err != nil {
		return QuotaState{}, err
	}
	ym := CurrentYearMonth()
	usage, err := s.GetMonthlyUsage(ctx, userID, ym)
	if err != nil && !errors.Is(err, ErrNoRollup) {
		return QuotaState{}, err
	}
	state := QuotaState{
		UserID:            userID,
		YearMonth:         ym,
		PlanTier:          plan.Tier,
		TokensUsed:        usage.TotalTokens,
		TokensCap:         plan.MonthlyTokenCap,
		MaxConcurrentRuns: plan.MaxConcurrentRuns,
	}
	if plan.IsUnlimitedTokens() {
		return state, nil
	}
	if plan.MonthlyTokenCap == 0 {
		// Defensive: zero cap would div-by-zero AND mean any usage
		// exhausts the quota. Treat as exhausted to fail safe.
		state.PercentUsed = 100
		state.Exhausted = true
		return state, nil
	}
	state.PercentUsed = float64(usage.TotalTokens) * 100 / float64(plan.MonthlyTokenCap)
	if usage.TotalTokens >= plan.MonthlyTokenCap {
		state.Exhausted = true
	} else if state.PercentUsed >= WarnThresholdPercent {
		state.WarnHigh = true
	}
	return state, nil
}

// ListPlans returns every row of the `plans` table, ordered by
// monthly token cap ascending with unlimited (-1) sentinels sorted
// last. Used by GET /api/v1/plans so the SPA can render the upgrade
// catalog without hardcoding tier names.
func (s *Store) ListPlans(ctx context.Context) ([]Plan, error) {
	rows, err := s.db.Query(ctx, `
		SELECT tier, monthly_token_cap, max_concurrent_runs,
		       COALESCE(description, '')
		FROM plans
		ORDER BY
		  CASE WHEN monthly_token_cap < 0 THEN 1 ELSE 0 END,
		  monthly_token_cap
	`)
	if err != nil {
		return nil, fmt.Errorf("billing: list plans: %w", err)
	}
	defer rows.Close()
	out := make([]Plan, 0, 4)
	for rows.Next() {
		var p Plan
		if err := rows.Scan(&p.Tier, &p.MonthlyTokenCap, &p.MaxConcurrentRuns,
			&p.Description); err != nil {
			return nil, fmt.Errorf("billing: scan plan row: %w", err)
		}
		out = append(out, p)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("billing: list plans rows: %w", err)
	}
	return out, nil
}
