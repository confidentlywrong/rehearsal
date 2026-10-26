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
	"log"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// EventDedup tracks processed webhook event IDs so a re-delivery
// from Stripe (which happens routinely on 2xx-but-network-dropped
// or operator-triggered replays) doesn't double-apply a plan
// change. The interface is intentionally small so a future
// Postgres-backed implementation can drop in for multi-replica
// deployments without touching the handler.
type EventDedup interface {
	// SeenOrMark returns true if the event was previously seen,
	// and false (after recording it) on first sight. The two-state
	// return matches sync.Map.LoadOrStore so the handler can branch
	// on "already processed" cleanly.
	SeenOrMark(eventID string) bool

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
func (d *InMemoryEventDedup) SeenOrMark(eventID string) bool {
	if eventID == "" {
		// Never dedup empty strings; the handler should already
		// reject events without an ID.
		return false
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if el, ok := d.index[eventID]; ok {
		d.order.MoveToFront(el)
		return true
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
	return false
}

// Len reports the current number of tracked event IDs. Exposed for
// tests; not part of the EventDedup interface.
func (d *InMemoryEventDedup) Len() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.order.Len()
}

// Forget implements EventDedup. Drops the LRU entry for eventID if
// present; no-op otherwise. Called by the webhook handler when a
// dispatched event handler returned 5xx so the next Stripe retry is
// not silently deduped.
func (d *InMemoryEventDedup) Forget(eventID string) {
	if eventID == "" {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
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
// On any DB error we fail OPEN — log via the returned errOnce
// channel (consumed nowhere yet; see ErrPolicy below) and return
// false so the handler proceeds with the apply. This is the
// deliberate trade-off: a transient DB hiccup must not 200 a
// real event we never applied. The handler's SetOrgPlan is
// idempotent (UPDATE … SET plan_tier = $2) so re-application on
// a Stripe retry is safe; missed application is not.
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
	if db == nil {
		return nil, ErrDedupUnavailable
	}
	return &PostgresEventDedup{db: db}, nil
}

// SeenOrMark implements EventDedup. Returns true iff the event ID
// was already in the table; on first sight inserts a row and
// returns false. On DB error returns false (fail-open) — the
// handler will then re-apply, which is safe because SetOrgPlan is
// idempotent. The DB error is recorded internally for observability.
func (d *PostgresEventDedup) SeenOrMark(eventID string) bool {
	if eventID == "" {
		// Mirror InMemoryEventDedup: empty IDs are never deduped.
		return false
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
		// Fail-open: returning false lets the handler proceed. A
		// stripe retry of the same event will hit the same path
		// and apply idempotently.
		//
		// Log the failure so operators can see dedup outages instead
		// of only discovering them via DBErrorCount(). During a
		// sustained Postgres blip, EVERY Stripe retry re-runs the
		// full handler; SetPlan is idempotent but audit-log writes
		// are additive, so duplicate audit entries may appear until
		// the DB recovers. This log is the single point where ops
		// can correlate audit duplicates to a dedup outage.
		log.Printf("BILLING: dedup SeenOrMark DB error (fail-open, handler will proceed) event_id=%s err=%v", eventID, err)
		return false
	}
	// RowsAffected == 0 means the row already existed → replay.
	// RowsAffected == 1 means we inserted it → first sight.
	return tag.RowsAffected() == 0
}

// DBErrorCount reports the running tally of dedup INSERT failures.
// Exposed for tests; an operator-facing /healthz can read this in
// a follow-up phase.
func (d *PostgresEventDedup) DBErrorCount() int {
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
	if eventID == "" {
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
