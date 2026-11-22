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
	"errors"
	"sync"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestInMemoryEventDedup_FirstSightThenRepeat(t *testing.T) {
	d := NewInMemoryEventDedup(10)
	seen, err := d.SeenOrMark("evt_1")
	if err != nil {
		t.Fatalf("first SeenOrMark unexpected err: %v", err)
	}
	if seen {
		t.Errorf("first SeenOrMark must return false")
	}
	seen, err = d.SeenOrMark("evt_1")
	if err != nil {
		t.Fatalf("second SeenOrMark unexpected err: %v", err)
	}
	if !seen {
		t.Errorf("second SeenOrMark must return true")
	}
}

func TestInMemoryEventDedup_EmptyIDNeverDeduped(t *testing.T) {
	d := NewInMemoryEventDedup(10)
	// An event without an ID must not poison the LRU; the handler
	// should already have rejected it but we defend in depth.
	seen, err := d.SeenOrMark("")
	if err != nil || seen {
		t.Errorf("empty id must return (false, nil), got (%v, %v)", seen, err)
	}
	seen, err = d.SeenOrMark("")
	if err != nil || seen {
		t.Errorf("empty id must return (false, nil) on repeat too, got (%v, %v)", seen, err)
	}
	if d.Len() != 0 {
		t.Errorf("empty IDs must not consume capacity, len=%d", d.Len())
	}
}

func TestInMemoryEventDedup_CapacityEviction(t *testing.T) {
	d := NewInMemoryEventDedup(3)
	for _, id := range []string{"a", "b", "c", "d"} {
		if _, err := d.SeenOrMark(id); err != nil {
			t.Fatalf("SeenOrMark(%q): %v", id, err)
		}
	}

	if d.Len() != 3 {
		t.Errorf("len=%d, want 3", d.Len())
	}
	seen, err := d.SeenOrMark("a")
	if err != nil {
		t.Fatalf("SeenOrMark(a): %v", err)
	}
	if seen {
		t.Errorf("'a' should have been evicted, must be re-seen as new")
	}
}

func TestInMemoryEventDedup_LRURefreshes(t *testing.T) {
	// Re-touching "a" must move it to the front so a subsequent
	// eviction picks "b" instead.
	d := NewInMemoryEventDedup(3)
	for _, id := range []string{"a", "b", "c", "a", "d"} {
		if _, err := d.SeenOrMark(id); err != nil {
			t.Fatalf("SeenOrMark(%q): %v", id, err)
		}
	}

	seen, err := d.SeenOrMark("b")
	if err != nil {
		t.Fatalf("SeenOrMark(b): %v", err)
	}
	if seen {
		t.Errorf("'b' should have been evicted after LRU refresh of 'a'")
	}
	seen, err = d.SeenOrMark("a")
	if err != nil {
		t.Fatalf("SeenOrMark(a): %v", err)
	}
	if !seen {
		t.Errorf("'a' should still be present")
	}
}

func TestInMemoryEventDedup_ZeroCapacityNoEviction(t *testing.T) {
	d := NewInMemoryEventDedup(0)
	for i := 0; i < 100; i++ {
		if _, err := d.SeenOrMark(string(rune('a' + i%26))); err != nil {
			t.Fatalf("SeenOrMark: %v", err)
		}
	}
	// 26 distinct keys, no eviction.
	if d.Len() != 26 {
		t.Errorf("want all 26 retained without eviction, got %d", d.Len())
	}
}

func TestInMemoryEventDedup_NilReceiverAndConcurrentRace(t *testing.T) {
	var nilDedup *InMemoryEventDedup
	seen, err := nilDedup.SeenOrMark("evt_nil")
	if !errors.Is(err, ErrDedupUnavailable) || seen {
		t.Fatalf("nil SeenOrMark = (%v, %v), want (false, ErrDedupUnavailable)", seen, err)
	}
	if nilDedup.Len() != 0 {
		t.Fatalf("nil Len() = %d, want 0", nilDedup.Len())
	}
	nilDedup.Forget("evt_nil")

	d := NewInMemoryEventDedup(32)
	var wg sync.WaitGroup
	firstSightCount := make(chan bool, 16)
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			s, derr := d.SeenOrMark("evt_shared")
			if derr != nil {
				t.Errorf("concurrent SeenOrMark err: %v", derr)
				return
			}
			if !s {
				firstSightCount <- true
			}
		}()
	}
	wg.Wait()
	close(firstSightCount)
	if len(firstSightCount) != 1 {
		t.Fatalf("expected exactly 1 first-sight across 16 goroutines, got %d", len(firstSightCount))
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
	conns := []struct {
		name string
		db   pgxExecer
	}{
		{"untyped nil", nil},
		{"typed nil *pgxpool.Pool", (*pgxpool.Pool)(nil)},
		{"typed nil *pgx.Conn", (*pgx.Conn)(nil)},
	}
	for _, tc := range conns {
		t.Run(tc.name, func(t *testing.T) {
			d, err := NewPostgresEventDedup(tc.db)
			if err == nil {
				t.Fatal("expected ErrDedupUnavailable for nil conn")
			}
			if !errors.Is(err, ErrDedupUnavailable) {
				t.Errorf("want ErrDedupUnavailable, got %v", err)
			}
			if d != nil {
				t.Errorf("expected nil dedup on error, got %v", d)
			}
		})
	}
}

func TestPostgresEventDedup_FirstSightThenRepeat(t *testing.T) {
	fake := newFakePgExec()
	d, err := NewPostgresEventDedup(fake)
	if err != nil {
		t.Fatalf("ctor: %v", err)
	}
	seen, err := d.SeenOrMark("evt_1")
	if err != nil || seen {
		t.Errorf("first SeenOrMark must return (false, nil), got (%v, %v)", seen, err)
	}
	seen, err = d.SeenOrMark("evt_1")
	if err != nil || !seen {
		t.Errorf("second SeenOrMark must return (true, nil), got (%v, %v)", seen, err)
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
	seen, err := d.SeenOrMark("")
	if err != nil || seen {
		t.Errorf("empty id must return (false, nil), got (%v, %v)", seen, err)
	}
	seen, err = d.SeenOrMark("")
	if err != nil || seen {
		t.Errorf("empty id must always return (false, nil), got (%v, %v)", seen, err)
	}
	if fake.callCount != 0 {
		t.Errorf("empty id must not hit the DB, got %d Exec calls", fake.callCount)
	}
}

func TestPostgresEventDedup_DBErrorFailsClosedAndPropagatesError(t *testing.T) {
	// On DB error we MUST fail closed and propagate the error so the
	// webhook handler returns 503 and Stripe retries instead of
	// double-applying events or silently dropping them.
	fake := newFakePgExec()
	fake.execErr = errors.New("connection reset by peer")
	d, _ := NewPostgresEventDedup(fake)

	seen, err := d.SeenOrMark("evt_x")
	if err == nil {
		t.Fatalf("DB error must propagate, got nil error")
	}
	if seen {
		t.Errorf("DB error must return seen=false, got true")
	}
	if d.DBErrorCount() != 1 {
		t.Errorf("DBErrorCount should track the failure, got %d", d.DBErrorCount())
	}

	var nilDedup *PostgresEventDedup
	seen, err = nilDedup.SeenOrMark("evt_nil")
	if !errors.Is(err, ErrDedupUnavailable) || seen {
		t.Fatalf("nil receiver SeenOrMark = (%v, %v), want (false, ErrDedupUnavailable)", seen, err)
	}
	if nilDedup.DBErrorCount() != 0 {
		t.Fatalf("nil DBErrorCount = %d, want 0", nilDedup.DBErrorCount())
	}
	uninit := &PostgresEventDedup{}
	seen, err = uninit.SeenOrMark("evt_uninit")
	if !errors.Is(err, ErrDedupUnavailable) || seen {
		t.Fatalf("uninit SeenOrMark = (%v, %v), want (false, ErrDedupUnavailable)", seen, err)
	}
}

func TestPostgresEventDedup_DistinctIDsAllNew(t *testing.T) {
	fake := newFakePgExec()
	d, _ := NewPostgresEventDedup(fake)
	for _, id := range []string{"evt_a", "evt_b", "evt_c"} {
		seen, err := d.SeenOrMark(id)
		if err != nil || seen {
			t.Errorf("first sight of %q must return (false, nil), got (%v, %v)", id, seen, err)
		}
	}
	if len(fake.rows) != 3 {
		t.Errorf("expected 3 distinct rows persisted, got %d", len(fake.rows))
	}
}
