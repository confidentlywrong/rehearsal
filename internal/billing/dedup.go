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
	"container/list"
	"context"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// EventDedup tracks processed webhook event IDs so a re-delivery
// from Stripe (which happens routinely on 2xx-but-network-dropped
// or operator-triggered replays) doesn't double-apply a plan
// change. The interface is intentionally small so a future
// Postgres-backed implementation can drop in for multi-replica
// deployments without touching the handler.
type EventDedup interface {
	// SeenOrMark returns (true, nil) if the event was previously seen,
	// and (false, nil) after recording it on first sight. Backend
	// failures return (false, err) so callers fail closed (e.g. HTTP 503)
	// and Stripe retries cleanly rather than double-applying events.
	SeenOrMark(eventID string) (bool, error)

	// Forget removes the given event ID from the dedup record.
	// Intended to be called by the webhook handler when the
	// downstream apply path returned a 5xx — we marked the event
	// as "seen" before dispatch (so concurrent retries don't
	// double-apply), but a transient failure means Stripe will
	// re-deliver and we want the next attempt to actually run
	// rather than be silently swallowed as a replay.
	//
	// MUST be a no-op for unknown IDs and for empty strings; the
	// handler calls it in a defer that doesn't know whether the
	// initial mark succeeded.
	Forget(eventID string)
}

// InMemoryEventDedup is a bounded LRU set. Suitable for single
// replica or as a defence-in-depth layer in front of a DB-backed
// dedup. Capacity 0 means "track everything" (no eviction); the
// micro-service defaults to a finite cap so a long-lived process
// can't accumulate event IDs unboundedly.
type InMemoryEventDedup struct {
	mu       sync.Mutex
	capacity int
	order    *list.List               // LRU: front = newest
	index    map[string]*list.Element // O(1) lookup + move-to-front
}

// NewInMemoryEventDedup builds an LRU dedup with the given capacity.
// capacity ≤ 0 disables eviction (use only when paired with a
// separate compaction strategy).
func NewInMemoryEventDedup(capacity int) *InMemoryEventDedup {
	return &InMemoryEventDedup{
		capacity: capacity,
		order:    list.New(),
		index:    make(map[string]*list.Element),
	}
}

// SeenOrMark implements EventDedup.
func (d *InMemoryEventDedup) SeenOrMark(eventID string) (bool, error) {
	if d == nil {
		return false, ErrDedupUnavailable
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.order == nil || d.index == nil {
		return false, ErrDedupUnavailable
	}
	if eventID == "" {
		// Never dedup empty strings; the handler should already
		// reject events without an ID.
		return false, nil
	}
	if el, ok := d.index[eventID]; ok {
		d.order.MoveToFront(el)
		return true, nil
	}
	el := d.order.PushFront(eventID)
	d.index[eventID] = el
	if d.capacity > 0 && d.order.Len() > d.capacity {
		oldest := d.order.Back()
		if oldest != nil {
			d.order.Remove(oldest)
			delete(d.index, oldest.Value.(string))
		}
	}
	return false, nil
}

// Len reports the current number of tracked event IDs. Exposed for
// tests; not part of the EventDedup interface.
func (d *InMemoryEventDedup) Len() int {
	if d == nil {
		return 0
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.order == nil {
		return 0
	}
	return d.order.Len()
}

// Forget implements EventDedup. Drops the LRU entry for eventID if
// present; no-op otherwise. Called by the webhook handler when a
// dispatched event handler returned 5xx so the next Stripe retry is
// not silently deduped.
func (d *InMemoryEventDedup) Forget(eventID string) {
	if d == nil || eventID == "" {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.order == nil || d.index == nil {
		return
	}
	if el, ok := d.index[eventID]; ok {
		d.order.Remove(el)
		delete(d.index, eventID)
	}
}

// ----- PostgresEventDedup ------------------------------------------------

// pgxExecer is the tiny subset of *pgx.Conn / *pgxpool.Pool the
// PostgresEventDedup needs. Defined here (rather than reusing
// tenant.PgxConn) so internal/billing has no compile-time edge to
// internal/tenant — a future split would not regress.
type pgxExecer interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

// dedupTimeout caps a single dedup INSERT so a slow DB can't
// stretch the webhook handler past Stripe's expectations. The
// outer handler is itself bounded; this is belt-and-suspenders so
// a hung connection cannot wedge the whole request.
const dedupTimeout = 3 * time.Second

// PostgresEventDedup records processed Stripe event IDs in the
// `processed_webhook_events` table. SeenOrMark uses INSERT … ON
// CONFLICT DO NOTHING — a single round-trip whose RowsAffected
// distinguishes first-sight (1) from replay (0). Suitable for
// horizontally-scaled billing replicas where the in-memory LRU
// would let the same event apply twice if Stripe re-delivered
// during a fail-over.
//
// On any DB error we fail CLOSED — log, increment dbErr, and return
// (false, err) so the webhook handler returns 503 and Stripe retries
// rather than double-applying events and emitting duplicate audit rows.
type PostgresEventDedup struct {
	db pgxExecer
	// dbErr counts DB failures since process start. Exposed for
	// tests; in production an operator can scrape it via /healthz
	// in a follow-up. Atomic-free because we serialise on the DB.
	mu    sync.Mutex
	dbErr int
}

// ErrDedupUnavailable is returned by NewPostgresEventDedup when the
// underlying pgx connection is nil. We surface this at construction
// rather than silently substituting InMemoryEventDedup so the
// operator sees a hard error in the log instead of a quiet
// degradation that re-introduces the multi-replica race.
var ErrDedupUnavailable = errors.New("billing: postgres event dedup requires a non-nil pgx connection")

// NewPostgresEventDedup builds a dedup backed by the
// processed_webhook_events table. Returns ErrDedupUnavailable if
// the connection is nil so the caller can fall back to
// InMemoryEventDedup explicitly (rather than us choosing for them).
func NewPostgresEventDedup(db pgxExecer) (*PostgresEventDedup, error) {
	switch v := db.(type) {
	case *pgx.Conn:
		if v == nil {
			db = nil
		}
	case *pgxpool.Pool:
		if v == nil {
			db = nil
		}
	}
	if db == nil {
		return nil, ErrDedupUnavailable
	}
	return &PostgresEventDedup{db: db}, nil
}

// SeenOrMark implements EventDedup. Returns (true, nil) iff the
// event ID was already in the table; on first sight inserts a row and
// returns (false, nil). Fails closed on nil receiver/DB or DB errors,
// incrementing DBErrorCount and returning (false, err).
func (d *PostgresEventDedup) SeenOrMark(eventID string) (bool, error) {
	if d == nil || d.db == nil {
		return false, ErrDedupUnavailable
	}
	if eventID == "" {
		// Mirror InMemoryEventDedup: empty IDs are never deduped.
		return false, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), dedupTimeout)
	defer cancel()
	tag, err := d.db.Exec(ctx, `
		INSERT INTO processed_webhook_events (event_id)
		VALUES ($1)
		ON CONFLICT (event_id) DO NOTHING
	`, eventID)
	if err != nil {
		d.mu.Lock()
		d.dbErr++
		d.mu.Unlock()
		log.Printf("BILLING: dedup SeenOrMark DB error (fail-closed) event_id=%s err=%v", eventID, err)
		return false, fmt.Errorf("billing: dedup seen_or_mark: %w", err)
	}
	// RowsAffected == 0 means the row already existed → replay.
	// RowsAffected == 1 means we inserted it → first sight.
	return tag.RowsAffected() == 0, nil
}

// DBErrorCount reports the running tally of dedup INSERT failures.
// Exposed for tests; an operator-facing /healthz can read this in
// a follow-up phase.
func (d *PostgresEventDedup) DBErrorCount() int {
	if d == nil {
		return 0
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.dbErr
}

// Forget implements EventDedup. Deletes the row for eventID so the
// next Stripe retry can be processed. Called by the webhook handler
// after a 5xx response from the dispatched event handler. Errors
// are logged via the DB error counter and otherwise swallowed — a
// dedup-table outage MUST NOT block the webhook from returning its
// response.
func (d *PostgresEventDedup) Forget(eventID string) {
	if d == nil || d.db == nil || eventID == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), dedupTimeout)
	defer cancel()
	if _, err := d.db.Exec(ctx, `
		DELETE FROM processed_webhook_events
		WHERE event_id = $1
	`, eventID); err != nil {
		d.mu.Lock()
		d.dbErr++
		d.mu.Unlock()
		// Log so a stuck dedup marker (which would silently swallow
		// Stripe's next retry as "deduped") is visible in the
		// operator logs. Otherwise the failure only surfaces via the
		// DBErrorCount() metric and by observing that the user was
		// billed but not upgraded.
		log.Printf("BILLING: dedup Forget DB error (retry may be silently deduped by stale marker) event_id=%s err=%v", eventID, err)
	}
}
