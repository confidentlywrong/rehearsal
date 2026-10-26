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
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"time"

	pubsub "cloud.google.com/go/pubsub/v2"
)

// PubSubDrainer consumes metering events from a Google Pub/Sub
// subscription and forwards them to a Store. This is the cloud
// counterpart of the SQLiteDrainer — `METERING_SINK=pubsub` on the
// Python side, `METERING_DRAIN=pubsub` on the Go side.
//
// At-least-once contract: every message that succeeds at both
// `InsertEvent` AND `UpsertMonthlyRollup` is Ack'd. Any failure on
// either step Nack's the message so Pub/Sub redelivers it. The
// Postgres BIGSERIAL absorbs the resulting duplicates harmlessly.
//
// If the rollup half fails AFTER the ledger insert succeeded, we
// still Nack — the entire message will replay. The ledger insert
// duplicates harmlessly (BIGSERIAL); the rollup will eventually
// converge. This is the same trade-off the SQLite drainer makes and
// is intentional: the canonical truth is the ledger, the rollup is a
// materialised view we can rebuild from it.
type PubSubDrainer struct {
	receiver  Receiver
	forwarder EventForwarder

	// Indirection points for tests / observability. nil means use
	// the default (log.Printf for logf, time.Now for clock).
	logf  func(format string, args ...any)
	clock func() time.Time
}

// Receiver is the minimal contract PubSubDrainer needs from a
// Pub/Sub subscriber. *PubSubReceiver wraps the real GCP client; the
// unit tests pass a hand-rolled fake to avoid talking to either a
// real topic or the emulator.
//
// Receive must block until ctx is cancelled or a fatal error occurs.
// The handler MUST be called for every received message, and MUST
// itself call either Ack() or Nack() on the message.
type Receiver interface {
	Receive(ctx context.Context, handler func(context.Context, Message)) error
}

// Message is the minimal subset of pubsub.Message the drainer
// needs. *pubsubMessageAdapter (defined in pubsub_drainer_gcp.go)
// wraps a real `*pubsub.Message`.
type Message interface {
	Data() []byte
	Attributes() map[string]string
	Ack()
	Nack()
}

// NewPubSubDrainer constructs a drainer over the given receiver +
// forwarder. The receiver is typically `NewPubSubReceiver(...)`;
// tests pass an in-memory fake.
func NewPubSubDrainer(receiver Receiver, forwarder EventForwarder) *PubSubDrainer {
	return &PubSubDrainer{
		receiver:  receiver,
		forwarder: forwarder,
		logf:      log.Printf,
		clock:     func() time.Time { return time.Now().UTC() },
	}
}

// Run blocks until ctx is cancelled. Returns the receiver's final
// error (typically context.Canceled on graceful shutdown).
func (d *PubSubDrainer) Run(ctx context.Context) error {
	if d.forwarder == nil {
		return errors.New("pubsub drainer: forwarder is nil")
	}
	if d.receiver == nil {
		return errors.New("pubsub drainer: receiver is nil")
	}
	return d.receiver.Receive(ctx, d.handle)
}

// handle is the per-message callback. Exposed (lowercase) so the
// in-process tests can drive it directly without spinning up a
// goroutine via Run.
func (d *PubSubDrainer) handle(ctx context.Context, m Message) {
	event, err := parsePubSubMessage(m.Data(), d.clock)
	if err != nil {
		// Malformed payloads are a developer error, not a transient
		// failure. Ack to remove from the queue rather than loop
		// forever; log loud so it's discoverable.
		d.logf("BILLING_PUBSUB_DRAIN: dropping malformed message: %v", err)
		m.Ack()
		return
	}

	if _, err := d.forwarder.InsertEvent(ctx, event); err != nil {
		// FK violation = user missing from the users table.
		// Bootstrap guarantees superadmins; a real-deployment FK
		// miss is a misconfiguration, not a transient outage —
		// log and Ack so it doesn't loop. The event is lost but
		// surfaced loudly.
		if isPermanentInsertError(err) {
			d.logf("BILLING_PUBSUB_DRAIN: dropping event for user=%q: %v", event.UserID, err)
			m.Ack()
			return
		}
		// Transient (connection dropped, statement timeout): Nack so
		// Pub/Sub redelivers. BIGSERIAL absorbs any duplicate insert
		// on the retry.
		d.logf("BILLING_PUBSUB_DRAIN: insert event nack: %v", err)
		m.Nack()
		return
	}

	if err := d.forwarder.UpsertMonthlyRollup(ctx, event.UserID, event.Timestamp, addTokens(event), addCost(event)); err != nil {
		// Rollup failure with ledger already written → Nack so the
		// whole message replays. The ledger insert will duplicate
		// (absorbed by BIGSERIAL), the rollup will eventually
		// converge. A Phase 7 reconcile job would also catch the
		// drift.
		d.logf("BILLING_PUBSUB_DRAIN: upsert rollup nack: %v", err)
		m.Nack()
		return
	}

	m.Ack()
}

// parsePubSubMessage decodes a metering event from a Pub/Sub message
// payload. Schema matches `agents/utils/metering.MeteringEvent.to_dict()`.
//
// B-24 (2026-06-23 audit): the previous implementation fell back to
// the publisher-set `user_id` attribute when the JSON body omitted
// the field. Pub/Sub attributes are NOT cryptographically signed by
// Google — they are arbitrary key/value pairs set by whichever
// principal called publish(). Trusting them for user attribution
// meant a compromised publisher (or a future internal job emitting
// to the same topic) could mis-attribute token usage to a different
// tenant. The Python emitter always serialises user_id into the JSON
// body (see agents/utils/metering.MeteringEvent.to_json), so dropping
// the fallback closes the attribution-spoof surface without losing
// any legitimate traffic. Malformed messages are now Ack-and-dropped
// with the error logged once.
func parsePubSubMessage(data []byte, clock func() time.Time) (Event, error) {
	if len(data) == 0 {
		return Event{}, errors.New("empty payload")
	}
	var raw struct {
		UserID           string  `json:"user_id"`
		SimID            *string `json:"sim_id"`
		AgentType        *string `json:"agent_type"`
		Model            *string `json:"model"`
		PromptTokens     *int64  `json:"prompt_tokens"`
		CompletionTokens *int64  `json:"completion_tokens"`
		TotalTokens      *int64  `json:"total_tokens"`
		TimestampISO     string  `json:"timestamp_iso"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return Event{}, fmt.Errorf("decode payload: %w", err)
	}

	if raw.UserID == "" {
		return Event{}, errors.New("missing user_id in JSON body (attribute fallback removed in B-24)")
	}
	userID := raw.UserID

	ts, err := parseMeteringTimestamp(raw.TimestampISO)
	if err != nil {
		// Treat absent/malformed timestamp as receive time. The
		// trade-off: a backlog replay would assign rollups to "now"
		// rather than the original month. That's a corner case
		// (Python emitter would have to be broken to drop the
		// field) and Ack-with-now-clock beats infinite retries.
		ts = clock()
	}

	return Event{
		UserID:           userID,
		SimID:            raw.SimID,
		AgentType:        raw.AgentType,
		Model:            raw.Model,
		PromptTokens:     raw.PromptTokens,
		CompletionTokens: raw.CompletionTokens,
		TotalTokens:      raw.TotalTokens,
		Timestamp:        ts,
	}, nil
}

// addTokens / addCost extract the rollup-contributing values from
// an Event. NULL token counts contribute 0 to the rollup (heartbeat
// only); the per-row ledger entry still preserves the "unknown"
// signal.
func addTokens(e Event) int64 {
	if e.TotalTokens == nil {
		return 0
	}
	return *e.TotalTokens
}

func addCost(e Event) int64 {
	if e.CostMicros == nil {
		return 0
	}
	return *e.CostMicros
}

// isPermanentInsertError reports whether err is a permanent
// (non-retryable) failure on InsertEvent. Today this only matches
// the foreign-key violation that occurs when the message's user_id
// is not present in `users` — replaying that message would
// fail identically forever, so we Ack to drop. Everything else is
// treated as transient and Nack'd for redelivery.
//
// Match is text-based because pgx wraps the pgconn.PgError into an
// unexported chain; we don't want billing/ to import pgconn just
// for the constant.
func isPermanentInsertError(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	for _, marker := range []string{
		"foreign key constraint",
		"violates foreign key",
		"23503", // PostgreSQL SQLSTATE for FK violation
	} {
		if containsCaseInsensitive(msg, marker) {
			return true
		}
	}
	return false
}

// pubsubMessageAdapter wraps a real `*pubsub.Message` into the
// minimal Message interface the drainer consumes. Lives here (not
// in pubsub_drainer_gcp.go) so the unit tests don't need to import
// the cloud client to satisfy the interface — they just construct a
// fakeMessage directly.
type pubsubMessageAdapter struct{ m *pubsub.Message }

func (a pubsubMessageAdapter) Data() []byte                  { return a.m.Data }
func (a pubsubMessageAdapter) Attributes() map[string]string { return a.m.Attributes }
func (a pubsubMessageAdapter) Ack()                          { a.m.Ack() }
func (a pubsubMessageAdapter) Nack()                         { a.m.Nack() }

// PubSubReceiver is the production Receiver backed by Google
// Cloud Pub/Sub. Construct via NewPubSubReceiver; the unit tests
// substitute their own Receiver.
type PubSubReceiver struct {
	client     *pubsub.Client
	subscriber *pubsub.Subscriber
}

// NewPubSubReceiver creates a PubSubReceiver attached to the
// given project + subscription. The client is owned by the
// receiver and closed by Close().
//
// PUBSUB_EMULATOR_HOST honoured by the underlying client (set
// it for the local emulator path).
func NewPubSubReceiver(ctx context.Context, projectID, subscriptionID string) (*PubSubReceiver, error) {
	if projectID == "" {
		return nil, errors.New("pubsub receiver: project_id is required")
	}
	if subscriptionID == "" {
		return nil, errors.New("pubsub receiver: subscription_id is required")
	}
	client, err := pubsub.NewClient(ctx, projectID)
	if err != nil {
		return nil, fmt.Errorf("pubsub receiver: new client: %w", err)
	}
	return &PubSubReceiver{
		client:     client,
		subscriber: client.Subscriber(subscriptionID),
	}, nil
}

// Receive blocks until ctx is cancelled or a fatal error occurs.
// The handler is invoked concurrently for each message.
func (r *PubSubReceiver) Receive(ctx context.Context, handler func(context.Context, Message)) error {
	return r.subscriber.Receive(ctx, func(ctx context.Context, m *pubsub.Message) {
		handler(ctx, pubsubMessageAdapter{m: m})
	})
}

// Close releases the underlying client.
func (r *PubSubReceiver) Close() error {
	if r.client == nil {
		return nil
	}
	return r.client.Close()
}
