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
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"
)

func TestParsePubSubMessage_PreservesCostMicros(t *testing.T) {
	payload := []byte(`{
		"user_id": "u-123",
		"sim_id": "sim-1",
		"agent_type": "planner",
		"model": "gemini-2.5-pro",
		"prompt_tokens": 100,
		"completion_tokens": 50,
		"total_tokens": 150,
		"cost_micros": 4200,
		"timestamp_iso": "2026-06-25T12:00:00Z"
	}`)
	ev, err := parsePubSubMessage(payload, time.Now)
	if err != nil {
		t.Fatalf("parsePubSubMessage failed: %v", err)
	}
	if ev.CostMicros == nil || *ev.CostMicros != 4200 {
		t.Fatalf("CostMicros = %v, want 4200", ev.CostMicros)
	}
	if got := addCost(ev); got != 4200 {
		t.Fatalf("addCost(ev) = %d, want 4200", got)
	}
}

type recordingForwarder struct {
	events   []Event
	rollCost int64
}

func (r *recordingForwarder) InsertEvent(_ context.Context, e Event) (int64, error) {
	r.events = append(r.events, e)
	return int64(len(r.events)), nil
}

func (r *recordingForwarder) UpsertMonthlyRollup(_ context.Context, _ string, _ time.Time, _ int64, addCost int64) error {
	r.rollCost += addCost
	return nil
}

func TestSQLiteDrainer_MissingCostMicrosColumnFallsBack(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "legacy_metering.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	// Legacy schema WITHOUT cost_micros column.
	_, err = db.Exec(`
		CREATE TABLE metering_ledger (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			user_id TEXT NOT NULL,
			sim_id TEXT,
			agent_type TEXT,
			model TEXT,
			prompt_tokens INTEGER,
			completion_tokens INTEGER,
			total_tokens INTEGER,
			timestamp_iso TEXT NOT NULL,
			drained INTEGER NOT NULL DEFAULT 0
		);
		INSERT INTO metering_ledger (user_id, total_tokens, timestamp_iso, drained)
		VALUES ('user-legacy', 250, '2026-06-25T10:00:00Z', 0);
	`)
	_ = db.Close()
	if err != nil {
		t.Fatalf("seed legacy schema: %v", err)
	}

	rec := &recordingForwarder{}
	drainer := NewSQLiteDrainer(dbPath, rec)
	n, err := drainer.DrainOnce(context.Background())
	if err != nil {
		t.Fatalf("DrainOnce on legacy schema failed: %v", err)
	}
	if n != 1 || len(rec.events) != 1 {
		t.Fatalf("drained %d rows (events=%d), want 1", n, len(rec.events))
	}
	if rec.events[0].UserID != "user-legacy" {
		t.Fatalf("user_id = %q, want user-legacy", rec.events[0].UserID)
	}
}

func TestSQLiteDrainer_SurfacesRealSQLiteErrorInsteadOfSwallowing(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "broken_metering.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	// Table exists with cost_micros, but missing required 'drained' column
	// -> produces SQLITE_ERROR ("no such column: drained"), NOT a missing table.
	_, err = db.Exec(`CREATE TABLE metering_ledger (id INTEGER PRIMARY KEY, cost_micros INTEGER);`)
	_ = db.Close()
	if err != nil {
		t.Fatalf("seed broken schema: %v", err)
	}

	drainer := NewSQLiteDrainer(dbPath, &recordingForwarder{})
	_, err = drainer.DrainOnce(context.Background())
	if err == nil {
		t.Fatal("expected SQLITE_ERROR on broken schema to surface, got nil")
	}
}

func TestStripeSubscriptionFetcher_EscapesCustomerID(t *testing.T) {
	var gotCustomer, gotLimit string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotCustomer = r.URL.Query().Get("customer")
		gotLimit = r.URL.Query().Get("limit")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"id":"sub_1","status":"active","items":{"data":[{"price":{"id":"price_pro"}}]}}],"has_more":false}`))
	}))
	defer srv.Close()

	f, err := NewStripeSubscriptionFetcher("sk_test_123")
	if err != nil {
		t.Fatalf("NewStripeSubscriptionFetcher: %v", err)
	}
	f.WithAPIBase(srv.URL).WithHTTPClient(srv.Client())

	maliciousID := "cus_123&limit=99#injected"
	priceID, err := f.FetchActiveSubscription(context.Background(), maliciousID)
	if err != nil {
		t.Fatalf("FetchActiveSubscription: %v", err)
	}
	if priceID != "price_pro" {
		t.Fatalf("priceID = %q, want price_pro", priceID)
	}
	if gotCustomer != maliciousID {
		t.Fatalf("query customer = %q, want %q", gotCustomer, maliciousID)
	}
	if gotLimit != "1" {
		t.Fatalf("query limit = %q, want 1 (uninjected)", gotLimit)
	}
}

func TestStore_NilReceiverGuards(t *testing.T) {
	var nilStore *Store
	ctx := context.Background()
	if _, err := nilStore.InsertEvent(ctx, Event{UserID: "u1"}); err == nil {
		t.Fatal("InsertEvent on nil *Store must return error")
	}
	if err := nilStore.UpsertMonthlyRollup(ctx, "u1", time.Now(), 10, 5); err == nil {
		t.Fatal("UpsertMonthlyRollup on nil *Store must return error")
	}
	if _, err := nilStore.GetAgentBreakdown(ctx, "u1", "2026-06"); err == nil {
		t.Fatal("GetAgentBreakdown on nil *Store must return error")
	}
	if _, err := nilStore.ListPlans(ctx); err == nil {
		t.Fatal("ListPlans on nil *Store must return error")
	}
}
