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

package billing

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log"
	"os"
	"sync"
	"time"

	// Pure-Go SQLite driver. Registered as "sqlite". No CGO required.
	_ "modernc.org/sqlite"
)

// SQLiteDrainer polls a SQLite ledger written by the Python
// `agents.utils.metering.SqliteSink` and forwards new rows into
// Postgres via Store.
//
// The Python side writes rows with `drained = 0` (the column default).
// On each tick we SELECT a small batch of undrained rows, write them
// to Postgres in order, then UPDATE drained=1 for the rows we
// successfully forwarded. The two writes are NOT in the same
// transaction — that's the standard at-least-once pattern, and the
// Postgres `id BIGSERIAL` is what makes accidental duplicates harmless
// (the worst case is one event counted twice, and Phase 5 quotas use
// a 5% safety margin to absorb it).
//
// Polling cadence defaults to 2s — slow enough that an idle dev box
// doesn't waste IO, fast enough that the /usage tile refreshes
// near-real-time while a sim is running.
type SQLiteDrainer struct {
	mu        sync.Mutex
	dbPath    string
	forwarder EventForwarder
	batchSize int
	interval  time.Duration

	// sqlOpen is indirected so tests can inject a fake. Default is
	// sql.Open("sqlite", path).
	sqlOpen func(driver, dsn string) (*sql.DB, error)
}

// EventForwarder is the minimal interface the SQLiteDrainer needs
// from a destination. *Store satisfies it; tests pass an in-memory
// recorder to avoid a real Postgres dependency.
type EventForwarder interface {
	InsertEvent(ctx context.Context, e Event) (int64, error)
	UpsertMonthlyRollup(ctx context.Context, userID string, ts time.Time, addTokens int64, addCost int64) error
}

// SQLiteDrainerOption configures a SQLiteDrainer. Plain functional
// options to keep the constructor signature stable as we add knobs.
type SQLiteDrainerOption func(*SQLiteDrainer)

// WithBatchSize caps the number of rows drained per tick. Higher
// values reduce SQL round-trips per row at the cost of longer
// individual ticks. Default 100.
func WithBatchSize(n int) SQLiteDrainerOption {
	return func(d *SQLiteDrainer) {
		if n > 0 {
			d.batchSize = n
		}
	}
}

// WithInterval sets the poll interval. Default 2s.
func WithInterval(d time.Duration) SQLiteDrainerOption {
	return func(s *SQLiteDrainer) {
		if d > 0 {
			s.interval = d
		}
	}
}

const (
	defaultSQLiteBatchSize     = 100
	defaultSQLiteDrainInterval = 2 * time.Second
)

// NewSQLiteDrainer constructs a drainer pointing at dbPath. dbPath
// MUST be the same file the Python `SqliteSink` writes (typically
// `data/metering.db`, or whatever METERING_SQLITE_PATH overrides it
// to). The file is opened lazily on the first Run() tick — passing a
// path to a not-yet-created DB is fine; the drainer simply finds no
// rows until the Python side creates the file on its first emit.
//
// forwarder accepts any EventForwarder; in production this is
// *Store. Tests pass an in-memory recorder so they don't need a
// running Postgres.
func NewSQLiteDrainer(dbPath string, forwarder EventForwarder, opts ...SQLiteDrainerOption) *SQLiteDrainer {
	d := &SQLiteDrainer{
		dbPath:    dbPath,
		forwarder: forwarder,
		batchSize: defaultSQLiteBatchSize,
		interval:  defaultSQLiteDrainInterval,
		sqlOpen:   sql.Open,
	}
	for _, opt := range opts {
		opt(d)
	}
	return d
}

// Run blocks until ctx is cancelled, ticking every interval to drain
// new rows. Errors are logged but never returned — we treat the
// drainer as best-effort infrastructure: a SQLite transient failure
// must not propagate up to crash the gateway.
//
// One drain pass is performed immediately on entry so a unit test
// can call Run, observe the first pass via a wait/cancel, and exit
// without waiting a full tick.
func (d *SQLiteDrainer) Run(ctx context.Context) {
	if d == nil || ctx == nil {
		return
	}
	if d.forwarder == nil || d.sqlOpen == nil {
		log.Printf("BILLING_SQLITE_DRAIN: drainer not initialized")
		return
	}
	interval := d.interval
	if interval <= 0 {
		interval = defaultSQLiteDrainInterval
	}
	if err := d.tick(ctx); err != nil {
		log.Printf("BILLING_SQLITE_DRAIN: %v", err)
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := d.tick(ctx); err != nil {
				log.Printf("BILLING_SQLITE_DRAIN: %v", err)
			}
		}
	}
}

// DrainOnce performs a single drain pass and returns the number of
// rows forwarded. Exposed for tests so they don't have to coordinate
// goroutine lifecycles.
func (d *SQLiteDrainer) DrainOnce(ctx context.Context) (int, error) {
	return d.drainBatch(ctx)
}

// tick wraps drainBatch and turns "no source file yet" into a no-op
// (the dev hasn't run any agent yet, so the Python sink hasn't
// created the file). Every other error propagates so Run logs it.
func (d *SQLiteDrainer) tick(ctx context.Context) error {
	if _, err := os.Stat(d.dbPath); errors.Is(err, os.ErrNotExist) {
		return nil
	}
	_, err := d.drainBatch(ctx)
	return err
}

// drainBatch opens the SQLite file, selects up to batchSize undrained
// rows, forwards them to Postgres, then marks them drained. Returns
// the count of rows successfully forwarded. The connection is closed
// after each batch — we expect at most ~50 batches/minute, so the
// open/close overhead is irrelevant compared to keeping a fd open
// across the SQLite writer's checkpoints.
func (d *SQLiteDrainer) drainBatch(ctx context.Context) (int, error) {
	if d == nil {
		return 0, errors.New("sqlite drainer: receiver is nil")
	}
	if d.forwarder == nil {
		return 0, errors.New("sqlite drainer: forwarder is nil")
	}
	if d.sqlOpen == nil {
		return 0, errors.New("sqlite drainer: sqlOpen is nil")
	}
	batchSize := d.batchSize
	if batchSize <= 0 {
		batchSize = defaultSQLiteBatchSize
	}
	d.mu.Lock()
	defer d.mu.Unlock()

	db, err := d.sqlOpen("sqlite", d.dbPath)
	if err != nil {
		return 0, fmt.Errorf("sqlite drainer: open: %w", err)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	// Wait up to 5s on transient writer locks from the Python SqliteSink
	// before surfacing SQLITE_BUSY.
	if _, err := db.ExecContext(ctx, "PRAGMA busy_timeout = 5000"); err != nil {
		return 0, fmt.Errorf("sqlite drainer: pragma busy_timeout: %w", err)
	}

	rows, err := db.QueryContext(ctx, `
		SELECT id, user_id, sim_id, agent_type, model,
		       prompt_tokens, completion_tokens, total_tokens,
		       cost_micros, timestamp_iso
		FROM metering_ledger
		WHERE drained = 0
		ORDER BY id
		LIMIT ?
	`, batchSize)
	if err != nil {
		// cost_micros may be absent on a pre-P2 SQLite file that
		// missed the additive migration (e.g. the gateway started
		// before the Python sink could run its ALTER). Check missing
		// column BEFORE missing table so legacy schemas fall back cleanly.
		if isMissingColumnError(err, "cost_micros") {
			return d.drainBatchLegacy(ctx, db, batchSize)
		}
		// Table missing is normal on the very first poll before the
		// Python side has emitted anything — surface as a no-op so the
		// log isn't flooded with one error per tick during quiet
		// periods.
		if isMissingTableError(err) {
			return 0, nil
		}
		return 0, fmt.Errorf("sqlite drainer: select: %w", err)
	}
	defer rows.Close()

	var batch []drainedRow
	for rows.Next() {
		var (
			id           int64
			userID       sql.NullString
			simID        sql.NullString
			agentType    sql.NullString
			model        sql.NullString
			promptTokens sql.NullInt64
			completion   sql.NullInt64
			totalTokens  sql.NullInt64
			costMicros   sql.NullInt64
			timestampISO string
		)
		if err := rows.Scan(
			&id, &userID, &simID, &agentType, &model,
			&promptTokens, &completion, &totalTokens,
			&costMicros, &timestampISO,
		); err != nil {
			return 0, fmt.Errorf("sqlite drainer: scan: %w", err)
		}
		ts, err := parseMeteringTimestamp(timestampISO)
		if err != nil {
			// Skip the malformed row rather than block the whole
			// drain — log it so it's discoverable. We do NOT mark it
			// drained, so a fix on the Python side could backfill if
			// we ever needed it.
			log.Printf("BILLING_SQLITE_DRAIN: skipping row %d with bad ts %q: %v", id, timestampISO, err)
			continue
		}
		batch = append(batch, drainedRow{
			id: id,
			event: Event{
				UserID:           userID.String,
				SimID:            nullToPtr(simID),
				AgentType:        nullToPtr(agentType),
				Model:            nullToPtr(model),
				PromptTokens:     nullInt64ToPtr(promptTokens),
				CompletionTokens: nullInt64ToPtr(completion),
				TotalTokens:      nullInt64ToPtr(totalTokens),
				CostMicros:       nullInt64ToPtr(costMicros),
				Timestamp:        ts,
			},
		})
	}
	if err := rows.Err(); err != nil {
		return 0, fmt.Errorf("sqlite drainer: iter: %w", err)
	}
	rows.Close()

	return d.forwardBatch(ctx, db, batch)
}

// drainBatchLegacy is the pre-cost_micros fallback path. It SELECTs the
// 9-column legacy shape from an old metering_ledger that hasn't yet
// received the additive ALTER. Once every writer has run the migration
// the legacy table is gone and this branch goes cold.
//
// Caller is responsible for having opened db.
func (d *SQLiteDrainer) drainBatchLegacy(ctx context.Context, db *sql.DB, batchSize int) (int, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT id, user_id, sim_id, agent_type, model,
		       prompt_tokens, completion_tokens, total_tokens,
		       timestamp_iso
		FROM metering_ledger
		WHERE drained = 0
		ORDER BY id
		LIMIT ?
	`, batchSize)
	if err != nil {
		if isMissingTableError(err) {
			return 0, nil
		}
		return 0, fmt.Errorf("sqlite drainer: legacy select: %w", err)
	}
	defer rows.Close()

	var batch []drainedRow
	for rows.Next() {
		var (
			id           int64
			userID       sql.NullString
			simID        sql.NullString
			agentType    sql.NullString
			model        sql.NullString
			promptTokens sql.NullInt64
			completion   sql.NullInt64
			totalTokens  sql.NullInt64
			timestampISO string
		)
		if err := rows.Scan(
			&id, &userID, &simID, &agentType, &model,
			&promptTokens, &completion, &totalTokens, &timestampISO,
		); err != nil {
			return 0, fmt.Errorf("sqlite drainer: legacy scan: %w", err)
		}
		ts, err := parseMeteringTimestamp(timestampISO)
		if err != nil {
			log.Printf("BILLING_SQLITE_DRAIN: skipping row %d with bad ts %q: %v", id, timestampISO, err)
			continue
		}
		batch = append(batch, drainedRow{
			id: id,
			event: Event{
				UserID:           userID.String,
				SimID:            nullToPtr(simID),
				AgentType:        nullToPtr(agentType),
				Model:            nullToPtr(model),
				PromptTokens:     nullInt64ToPtr(promptTokens),
				CompletionTokens: nullInt64ToPtr(completion),
				TotalTokens:      nullInt64ToPtr(totalTokens),
				// CostMicros omitted by design — column doesn't exist.
				Timestamp: ts,
			},
		})
	}
	if err := rows.Err(); err != nil {
		return 0, fmt.Errorf("sqlite drainer: legacy iter: %w", err)
	}
	rows.Close()
	return d.forwardBatch(ctx, db, batch)
}

type drainedRow struct {
	id    int64
	event Event
}

// forwardBatch is shared between the v2 and legacy drain paths.
// Caller has already loaded the batch from SQLite; this method
// pushes it into Postgres and marks the ledger rows drained.
func (d *SQLiteDrainer) forwardBatch(ctx context.Context, db *sql.DB, batch []drainedRow) (int, error) {
	if len(batch) == 0 {
		return 0, nil
	}

	// Forward to Postgres one at a time. We do NOT wrap in a tx
	// because the SQLite drained-flag update is what gives us
	// idempotence at the SQLite side; Postgres-side dupes are
	// absorbed by the at-least-once contract documented on
	// SQLiteDrainer.
	drainedIDs := make([]int64, 0, len(batch))
	forwardedCount := 0
	var firstErr error
	for _, r := range batch {
		if _, err := d.forwarder.InsertEvent(ctx, r.event); err != nil {
			if isPermanentInsertError(err) {
				// Permanent failure (e.g. Postgres 23503 FK violation for an
				// unknown/erased user or empty user_id): mark drained=1 so
				// poisoned rows cannot wedge ORDER BY id ASC LIMIT batchSize.
				log.Printf("BILLING_SQLITE_DRAIN: dropping permanent-error event id=%d user=%q: %v", r.id, r.event.UserID, err)
				drainedIDs = append(drainedIDs, r.id)
				continue
			}
			log.Printf("BILLING_SQLITE_DRAIN: insert event id=%d: %v", r.id, err)
			firstErr = fmt.Errorf("sqlite drainer: insert event id=%d: %w", r.id, err)
			break
		}
		// Update rollup. Tokens may be NULL → contribute 0; we still
		// advance updated_at so the dashboard sees the heartbeat.
		var addTokens int64
		if r.event.TotalTokens != nil {
			addTokens = *r.event.TotalTokens
		}
		var addCost int64
		if r.event.CostMicros != nil {
			addCost = *r.event.CostMicros
		}
		if err := d.forwarder.UpsertMonthlyRollup(ctx, r.event.UserID, r.event.Timestamp, addTokens, addCost); err != nil {
			// Do NOT mark drained=1 when rollup fails: keep row undrained so
			// the next drain pass retries and converges usage_rollup_monthly.
			log.Printf("BILLING_SQLITE_DRAIN: upsert rollup id=%d user=%s: %v", r.id, r.event.UserID, err)
			firstErr = fmt.Errorf("sqlite drainer: upsert rollup id=%d user=%s: %w", r.id, r.event.UserID, err)
			break
		}
		drainedIDs = append(drainedIDs, r.id)
		forwardedCount++
	}

	if len(drainedIDs) > 0 {
		if markErr := markDrained(ctx, db, drainedIDs); markErr != nil {
			log.Printf("BILLING_SQLITE_DRAIN: mark drained (%d rows): %v", len(drainedIDs), markErr)
			return forwardedCount, errors.Join(firstErr, fmt.Errorf("sqlite drainer: mark drained: %w", markErr))
		}
	}
	return forwardedCount, firstErr
}

// markDrained flips drained=1 for the given ledger ids. Splits into
// chunks to stay under SQLite's default 999-parameter limit even on
// future configurations with WithBatchSize larger than that.
func markDrained(ctx context.Context, db *sql.DB, ids []int64) error {
	const chunkSize = 500
	for start := 0; start < len(ids); start += chunkSize {
		end := start + chunkSize
		if end > len(ids) {
			end = len(ids)
		}
		chunk := ids[start:end]
		query := "UPDATE metering_ledger SET drained = 1 WHERE id IN ("
		args := make([]any, len(chunk))
		for i, id := range chunk {
			if i > 0 {
				query += ","
			}
			query += "?"
			args[i] = id
		}
		query += ")"
		if _, err := db.ExecContext(ctx, query, args...); err != nil {
			return err
		}
	}
	return nil
}

// parseMeteringTimestamp parses the RFC3339-ish strings the Python
// emitter writes (eg "2026-01-01T00:00:00.123456Z"). Falls back to
// plain RFC3339 for compatibility.
func parseMeteringTimestamp(s string) (time.Time, error) {
	if t, err := time.Parse("2006-01-02T15:04:05.000000Z", s); err == nil {
		return t, nil
	}
	return time.Parse(time.RFC3339Nano, s)
}

func nullToPtr(n sql.NullString) *string {
	if !n.Valid {
		return nil
	}
	v := n.String
	return &v
}

func nullInt64ToPtr(n sql.NullInt64) *int64 {
	if !n.Valid {
		return nil
	}
	v := n.Int64
	return &v
}

// isMissingTableError recognises the SQLite "no such table" error
// regardless of driver-specific wrapping. Used to suppress noise on
// the first few polls before the Python writer has created the table.
func isMissingTableError(err error) bool {
	if err == nil {
		return false
	}
	return containsCaseInsensitive(err.Error(), "no such table")
}

// isMissingColumnError recognises the SQLite "no such column" error
// for a specific column name. Used by drainBatch to fall back to a
// legacy SELECT when reading from a pre-P2 ledger that predates the
// cost_micros column.
func isMissingColumnError(err error, column string) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return containsCaseInsensitive(msg, "no such column") &&
		containsCaseInsensitive(msg, column)
}

func containsCaseInsensitive(haystack, needle string) bool {
	if len(needle) == 0 {
		return true
	}
	if len(haystack) < len(needle) {
		return false
	}
	for i := 0; i+len(needle) <= len(haystack); i++ {
		match := true
		for j := 0; j < len(needle); j++ {
			a := haystack[i+j]
			b := needle[j]
			if a >= 'A' && a <= 'Z' {
				a += 'a' - 'A'
			}
			if b >= 'A' && b <= 'Z' {
				b += 'a' - 'A'
			}
			if a != b {
				match = false
				break
			}
		}
		if match {
			return true
		}
	}
	return false
}
