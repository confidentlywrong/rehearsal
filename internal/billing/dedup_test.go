// Copyright 2026 The Confidently Wrong Authors
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
	"errors"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

func TestInMemoryEventDedup_FirstSightThenRepeat(t *testing.T) {
	d := NewInMemoryEventDedup(10)
	if d.SeenOrMark("evt_1") {
		t.Errorf("first SeenOrMark must return false")
	}
	if !d.SeenOrMark("evt_1") {
		t.Errorf("second SeenOrMark must return true")
	}
}

func TestInMemoryEventDedup_EmptyIDNeverDeduped(t *testing.T) {
	d := NewInMemoryEventDedup(10)
	// An event without an ID must not poison the LRU; the handler
	// should already have rejected it but we defend in depth.
	if d.SeenOrMark("") {
		t.Errorf("empty id must always return false")
	}
	if d.SeenOrMark("") {
		t.Errorf("empty id must always return false on repeat too")
	}
	if d.Len() != 0 {
		t.Errorf("empty IDs must not consume capacity, len=%d", d.Len())
	}
}

func TestInMemoryEventDedup_CapacityEviction(t *testing.T) {
	d := NewInMemoryEventDedup(3)
	d.SeenOrMark("a")
	d.SeenOrMark("b")
	d.SeenOrMark("c")
	d.SeenOrMark("d") // evicts oldest = "a"

	if d.Len() != 3 {
		t.Errorf("len=%d, want 3", d.Len())
	}
	if d.SeenOrMark("a") {
		t.Errorf("'a' should have been evicted, must be re-seen as new")
	}
}

func TestInMemoryEventDedup_LRURefreshes(t *testing.T) {
	// Re-touching "a" must move it to the front so a subsequent
	// eviction picks "b" instead.
	d := NewInMemoryEventDedup(3)
	d.SeenOrMark("a")
	d.SeenOrMark("b")
	d.SeenOrMark("c")
	d.SeenOrMark("a") // moves "a" to front; LRU order: a, c, b
	d.SeenOrMark("d") // evicts "b"

	if d.SeenOrMark("b") {
		t.Errorf("'b' should have been evicted after LRU refresh of 'a'")
	}
	if !d.SeenOrMark("a") {
		t.Errorf("'a' should still be present")
	}
}

func TestInMemoryEventDedup_ZeroCapacityNoEviction(t *testing.T) {
	d := NewInMemoryEventDedup(0)
	for i := 0; i < 100; i++ {
		d.SeenOrMark(string(rune('a' + i%26)))
	}
	// 26 distinct keys, no eviction.
	if d.Len() != 26 {
		t.Errorf("want all 26 retained without eviction, got %d", d.Len())
	}
}

// --- B3: PostgresEventDedup ---------------------------------------------

// fakePgExec stubs pgxExecer. It maintains an in-memory "table" so
// each test can assert insert vs. conflict semantics without
// standing up Postgres. The fake mimics the contract of `INSERT
// ... ON CONFLICT DO NOTHING`: first insert → RowsAffected=1,
// duplicate → RowsAffected=0.
type fakePgExec struct {
	rows     map[string]struct{}
	execErr  error // returned on every Exec call when non-nil
	gotSQL   []string
	gotArgs  [][]any
	callCount int
}

func newFakePgExec() *fakePgExec {
	return &fakePgExec{rows: map[string]struct{}{}}
}

func (f *fakePgExec) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	f.callCount++
	f.gotSQL = append(f.gotSQL, sql)
	f.gotArgs = append(f.gotArgs, args)
	if f.execErr != nil {
		return pgconn.CommandTag{}, f.execErr
	}
	if len(args) == 0 {
		return pgconn.NewCommandTag("INSERT 0 0"), nil
	}
	id, _ := args[0].(string)
	if _, ok := f.rows[id]; ok {
		// Conflict → no row inserted; the real driver returns
		// "INSERT 0 0" for ON CONFLICT DO NOTHING that hit an
		// existing row.
		return pgconn.NewCommandTag("INSERT 0 0"), nil
	}
	f.rows[id] = struct{}{}
	return pgconn.NewCommandTag("INSERT 0 1"), nil
}

func TestPostgresEventDedup_NilConnRejected(t *testing.T) {
	// Operator must see a hard error rather than a silent
	// fallback that re-introduces the multi-replica race.
	d, err := NewPostgresEventDedup(nil)
	if err == nil {
		t.Fatal("expected ErrDedupUnavailable for nil conn")
	}
	if !errors.Is(err, ErrDedupUnavailable) {
		t.Errorf("want ErrDedupUnavailable, got %v", err)
	}
	if d != nil {
		t.Errorf("expected nil dedup on error, got %v", d)
	}
}

func TestPostgresEventDedup_FirstSightThenRepeat(t *testing.T) {
	fake := newFakePgExec()
	d, err := NewPostgresEventDedup(fake)
	if err != nil {
		t.Fatalf("ctor: %v", err)
	}
	if d.SeenOrMark("evt_1") {
		t.Errorf("first SeenOrMark must return false (new event)")
	}
	if !d.SeenOrMark("evt_1") {
		t.Errorf("second SeenOrMark must return true (replay)")
	}
	if fake.callCount != 2 {
		t.Errorf("expected 2 Exec calls, got %d", fake.callCount)
	}
}

func TestPostgresEventDedup_EmptyIDNotPersisted(t *testing.T) {
	fake := newFakePgExec()
	d, _ := NewPostgresEventDedup(fake)
	// Empty IDs short-circuit so the table isn't polluted with
	// duplicate PK-empty rows on replay floods.
	if d.SeenOrMark("") {
		t.Errorf("empty id must return false")
	}
	if d.SeenOrMark("") {
		t.Errorf("empty id must always return false")
	}
	if fake.callCount != 0 {
		t.Errorf("empty id must not hit the DB, got %d Exec calls", fake.callCount)
	}
}

func TestPostgresEventDedup_DBErrorFailsOpen(t *testing.T) {
	// On DB error we MUST fail open (return false → handler
	// applies) so a transient DB hiccup doesn't 200 a real event
	// we never applied. The handler's SetOrgPlan is idempotent
	// so a Stripe retry that re-applies is safe.
	fake := newFakePgExec()
	fake.execErr = errors.New("connection reset by peer")
	d, _ := NewPostgresEventDedup(fake)

	if d.SeenOrMark("evt_x") {
		t.Errorf("DB error must fail open (return false), not dedup")
	}
	if d.DBErrorCount() != 1 {
		t.Errorf("DBErrorCount should track the failure, got %d", d.DBErrorCount())
	}
}

func TestPostgresEventDedup_DistinctIDsAllNew(t *testing.T) {
	fake := newFakePgExec()
	d, _ := NewPostgresEventDedup(fake)
	for _, id := range []string{"evt_a", "evt_b", "evt_c"} {
		if d.SeenOrMark(id) {
			t.Errorf("first sight of %q must return false", id)
		}
	}
	if len(fake.rows) != 3 {
		t.Errorf("expected 3 distinct rows persisted, got %d", len(fake.rows))
	}
}
