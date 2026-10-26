// Copyright 2026 Christian Haeussinger. Apache-2.0.

package billing

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// PgxConn is the minimal pgx-style interface the Store needs. Both
// *pgx.Conn and *pgxpool.Pool satisfy it, and tests can pass a fake.
//
// Kept structurally identical to account.PgxConn so a single pgx
// connection can be shared between the account control plane and
// the usage billing plane without an adapter shim. They are
// intentionally not type-aliased — billing must be free to evolve
// its surface independently.
type PgxConn interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Event is one row of the usage_ledger table. Nullable token fields
// honour the "never invent data" rule end-to-end: a missing
// usage_metadata on the Python side flows through as NULL here and
// is surfaced as "unknown" in the dashboard, never as zero.
//
// UserID is required (the per-user billing boundary); the other
// pointer fields are nullable to model unknown / unattributed
// usage events.
type Event struct {
	UserID           string
	SimID            *string
	AgentType        *string
	Model            *string
	PromptTokens     *int64
	CompletionTokens *int64
	TotalTokens      *int64
	CostMicros       *int64
	Timestamp        time.Time
}

// MonthlyUsage is one row of the usage_rollup_monthly table. Used
// as the response shape for GET /api/v1/usage.
type MonthlyUsage struct {
	UserID      string    `json:"user_id"`
	YearMonth   string    `json:"year_month"`
	TotalTokens int64     `json:"total_tokens"`
	CostMicros  int64     `json:"cost_micros"`
	UpdatedAt   time.Time `json:"updated_at"`
}

// AgentBreakdown reports per-agent token totals for a single user
// + month. Used by the dashboard's per-agent table. agent_type may
// be the empty string when the metering event lacked an
// agent_name — surfaced as "unknown" in the UI.
type AgentBreakdown struct {
	AgentType   string `json:"agent_type"`
	TotalTokens int64  `json:"total_tokens"`
	EventCount  int64  `json:"event_count"`
}

// ErrInvalidYearMonth is returned by ParseYearMonth (and store
// helpers that validate) when the input does not match YYYY-MM.
var ErrInvalidYearMonth = errors.New("billing: year_month must match YYYY-MM")

// Store reads and writes the usage_ledger + usage_rollup_monthly
// tables created by migrations/0001_users.sql.
type Store struct {
	db PgxConn
}

// NewStore wraps a pgx connection. db must point at a database
// where migrations/0001_users.sql has been applied.
func NewStore(db PgxConn) *Store { return &Store{db: db} }

// InsertEvent appends one row to usage_ledger. Returns the
// freshly-assigned id. NULL token fields are written as typed pgx
// NULL — we never coerce to zero.
func (s *Store) InsertEvent(ctx context.Context, e Event) (int64, error) {
	if e.UserID == "" {
		return 0, errors.New("billing: InsertEvent requires user_id")
	}
	if e.Timestamp.IsZero() {
		e.Timestamp = time.Now().UTC()
	}
	var id int64
	err := s.db.QueryRow(ctx, `
		INSERT INTO usage_ledger
			(user_id, sim_id, agent_type, model,
			 prompt_tokens, completion_tokens, total_tokens,
			 cost_micros, ts)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
		RETURNING id
	`,
		e.UserID, e.SimID, e.AgentType, e.Model,
		e.PromptTokens, e.CompletionTokens, e.TotalTokens,
		e.CostMicros, e.Timestamp,
	).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("billing: insert ledger: %w", err)
	}
	return id, nil
}

// UpsertMonthlyRollup adds tokens (and optionally cost_micros) to
// the running monthly total for (user_id, year_month). NULL token
// fields contribute 0 to the rollup but do NOT cause an error.
//
// year_month is derived from the event's UTC timestamp, not the
// caller's clock — keeps the rollup deterministic when the
// consumer reprocesses a backlog.
func (s *Store) UpsertMonthlyRollup(ctx context.Context, userID string, ts time.Time, addTokens int64, addCost int64) error {
	if userID == "" {
		return errors.New("billing: UpsertMonthlyRollup requires user_id")
	}
	ym := ts.UTC().Format("2006-01")
	_, err := s.db.Exec(ctx, `
		INSERT INTO usage_rollup_monthly
			(user_id, year_month, total_tokens, cost_micros, updated_at)
		VALUES ($1, $2, $3, $4, now())
		ON CONFLICT (user_id, year_month) DO UPDATE
		SET total_tokens = usage_rollup_monthly.total_tokens + EXCLUDED.total_tokens,
		    cost_micros  = usage_rollup_monthly.cost_micros  + EXCLUDED.cost_micros,
		    updated_at   = now()
	`, userID, ym, addTokens, addCost)
	if err != nil {
		return fmt.Errorf("billing: upsert rollup: %w", err)
	}
	return nil
}

// GetMonthlyUsage returns the rollup for one (user_id, year_month).
// The zero-value MonthlyUsage is returned with err==ErrNoRollup
// when no row exists — "no tokens used this month", NOT an error
// condition.
func (s *Store) GetMonthlyUsage(ctx context.Context, userID, yearMonth string) (MonthlyUsage, error) {
	if s == nil || s.db == nil {
		return MonthlyUsage{}, errors.New("billing: DB not connected")
	}
	if userID == "" {
		return MonthlyUsage{}, errors.New("billing: GetMonthlyUsage requires user_id")
	}
	if !ValidYearMonth(yearMonth) {
		return MonthlyUsage{}, ErrInvalidYearMonth
	}
	row := s.db.QueryRow(ctx, `
		SELECT user_id, year_month, total_tokens, cost_micros, updated_at
		FROM usage_rollup_monthly
		WHERE user_id = $1 AND year_month = $2
	`, userID, yearMonth)
	var u MonthlyUsage
	if err := row.Scan(&u.UserID, &u.YearMonth, &u.TotalTokens, &u.CostMicros, &u.UpdatedAt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return MonthlyUsage{
				UserID:    userID,
				YearMonth: yearMonth,
			}, ErrNoRollup
		}
		return MonthlyUsage{}, fmt.Errorf("billing: get rollup: %w", err)
	}
	return u, nil
}

// ErrNoRollup is returned by GetMonthlyUsage when the rollup row is
// absent. Callers usually treat it as "0 used this month" rather
// than an error.
var ErrNoRollup = errors.New("billing: no rollup row")

// GetAgentBreakdown returns per-agent_type token totals for one
// (user_id, year_month). Sorted descending by total_tokens so the
// dashboard can render the busiest agents first.
//
// Rows with NULL agent_type are bucketed under "" — the handler
// rewrites that to "unknown" before serialisation.
func (s *Store) GetAgentBreakdown(ctx context.Context, userID, yearMonth string) ([]AgentBreakdown, error) {
	if userID == "" {
		return nil, errors.New("billing: GetAgentBreakdown requires user_id")
	}
	if !ValidYearMonth(yearMonth) {
		return nil, ErrInvalidYearMonth
	}
	rows, err := s.db.Query(ctx, `
		SELECT COALESCE(agent_type, '') AS agent_type,
		       COALESCE(SUM(total_tokens), 0) AS tokens,
		       COUNT(*) AS events
		FROM usage_ledger
		WHERE user_id = $1
		  AND to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM') = $2
		GROUP BY 1
		ORDER BY tokens DESC, agent_type ASC
	`, userID, yearMonth)
	if err != nil {
		return nil, fmt.Errorf("billing: query breakdown: %w", err)
	}
	defer rows.Close()
	var out []AgentBreakdown
	for rows.Next() {
		var b AgentBreakdown
		if err := rows.Scan(&b.AgentType, &b.TotalTokens, &b.EventCount); err != nil {
			return nil, fmt.Errorf("billing: scan breakdown: %w", err)
		}
		out = append(out, b)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("billing: iter breakdown: %w", err)
	}
	return out, nil
}

// CurrentYearMonth returns today's UTC year-month string in YYYY-MM.
func CurrentYearMonth() string {
	return time.Now().UTC().Format("2006-01")
}

// ValidYearMonth reports whether s is a syntactically valid YYYY-MM
// string.
func ValidYearMonth(s string) bool {
	if len(s) != 7 || s[4] != '-' {
		return false
	}
	_, err := time.Parse("2006-01", s)
	return err == nil
}
