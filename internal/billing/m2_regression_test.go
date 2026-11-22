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
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"cloud.google.com/go/pubsub/v2"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

func seedSQLiteMeteringDB(t *testing.T, schema string, inserts string) string {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "metering.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	defer db.Close()
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	if inserts != "" {
		if _, err := db.Exec(inserts); err != nil {
			t.Fatalf("seed rows: %v", err)
		}
	}
	return dbPath
}

const standardMeteringSchema = `
	CREATE TABLE metering_ledger (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id TEXT,
		sim_id TEXT,
		agent_type TEXT,
		model TEXT,
		prompt_tokens INTEGER,
		completion_tokens INTEGER,
		total_tokens INTEGER,
		cost_micros INTEGER,
		timestamp_iso TEXT NOT NULL,
		drained INTEGER NOT NULL DEFAULT 0
	);
`

type threadSafeForwarder struct {
	mu           sync.Mutex
	events       []Event
	rollupTokens int64
	delay        time.Duration
	failInsertOn string
	failRollupOn string
}

func (f *threadSafeForwarder) InsertEvent(_ context.Context, e Event) (int64, error) {
	if f.delay > 0 {
		time.Sleep(f.delay)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if e.UserID == "" {
		return 0, errors.New("billing: InsertEvent requires user_id")
	}
	if f.failInsertOn != "" && e.UserID == f.failInsertOn {
		return 0, errors.New("transient postgres timeout")
	}
	f.events = append(f.events, e)
	return int64(len(f.events)), nil
}

func (f *threadSafeForwarder) UpsertMonthlyRollup(_ context.Context, userID string, _ time.Time, addTokens int64, _ int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failRollupOn != "" && userID == f.failRollupOn {
		return errors.New("transient rollup deadlock")
	}
	f.rollupTokens += addTokens
	return nil
}

func TestSQLiteDrainer_ConcurrentDrainOnceSerialized(t *testing.T) {
	var inserts strings.Builder
	const totalRows = 12
	var wantTokens int64
	for i := 1; i <= totalRows; i++ {
		tok := int64(i * 10)
		wantTokens += tok
		fmt.Fprintf(&inserts, "INSERT INTO metering_ledger (user_id, total_tokens, cost_micros, timestamp_iso, drained) VALUES ('u-%d', %d, 100, '2026-07-01T00:00:%02dZ', 0);\n", i, tok, i)
	}
	dbPath := seedSQLiteMeteringDB(t, standardMeteringSchema, inserts.String())

	fwd := &threadSafeForwarder{delay: 2 * time.Millisecond}
	drainer := NewSQLiteDrainer(dbPath, fwd)

	const goroutines = 16
	var wg sync.WaitGroup
	errs := make(chan error, goroutines)
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := drainer.DrainOnce(context.Background()); err != nil {
				errs <- err
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("unexpected concurrent DrainOnce error: %v", err)
	}

	if len(fwd.events) != totalRows {
		t.Fatalf("expected exactly %d forwarded events across 16 goroutines, got %d", totalRows, len(fwd.events))
	}
	if fwd.rollupTokens != wantTokens {
		t.Fatalf("expected exact rollup token sum %d, got %d", wantTokens, fwd.rollupTokens)
	}
}

func TestSQLiteDrainer_RollupFailureDoesNotMarkDrainedAndPropagatesError(t *testing.T) {
	dbPath := seedSQLiteMeteringDB(t, standardMeteringSchema, `
		INSERT INTO metering_ledger (user_id, total_tokens, cost_micros, timestamp_iso, drained) VALUES
			('u-1', 100, 10, '2026-07-01T00:00:01Z', 0),
			('u-fail', 200, 20, '2026-07-01T00:00:02Z', 0),
			('u-3', 300, 30, '2026-07-01T00:00:03Z', 0);
	`)

	fwd := &threadSafeForwarder{failRollupOn: "u-fail"}
	drainer := NewSQLiteDrainer(dbPath, fwd)

	n, err := drainer.DrainOnce(context.Background())
	if err == nil {
		t.Fatal("expected error when UpsertMonthlyRollup fails")
	}
	if n != 1 {
		t.Fatalf("expected 1 forwarded before failure, got %d", n)
	}

	db, _ := sql.Open("sqlite", dbPath)
	defer db.Close()
	var undrained int
	if err := db.QueryRow(`SELECT COUNT(*) FROM metering_ledger WHERE drained = 0`).Scan(&undrained); err != nil {
		t.Fatalf("count undrained: %v", err)
	}
	if undrained != 2 {
		t.Fatalf("expected 2 undrained rows (failed row + trailing row), got %d", undrained)
	}

	// Clear the transient failure and re-drain: rows 2 and 3 must now succeed.
	fwd.failRollupOn = ""
	n2, err := drainer.DrainOnce(context.Background())
	if err != nil {
		t.Fatalf("retry DrainOnce failed: %v", err)
	}
	if n2 != 2 {
		t.Fatalf("expected 2 remaining rows drained on retry, got %d", n2)
	}
	if fwd.rollupTokens != 600 {
		t.Fatalf("expected total rollupTokens=600, got %d", fwd.rollupTokens)
	}
}

func TestSQLiteDrainer_TransientInsertErrorPropagatesAndPreservesUndrained(t *testing.T) {
	dbPath := seedSQLiteMeteringDB(t, standardMeteringSchema, `
		INSERT INTO metering_ledger (user_id, total_tokens, cost_micros, timestamp_iso, drained) VALUES
			('u-transient', 50, 5, '2026-07-01T00:00:01Z', 0);
	`)
	fwd := &threadSafeForwarder{failInsertOn: "u-transient"}
	drainer := NewSQLiteDrainer(dbPath, fwd)

	n, err := drainer.DrainOnce(context.Background())
	if err == nil {
		t.Fatal("expected transient InsertEvent error to propagate")
	}
	if n != 0 {
		t.Fatalf("expected 0 forwarded rows, got %d", n)
	}
	db, _ := sql.Open("sqlite", dbPath)
	defer db.Close()
	var undrained int
	_ = db.QueryRow(`SELECT COUNT(*) FROM metering_ledger WHERE drained = 0`).Scan(&undrained)
	if undrained != 1 {
		t.Fatalf("expected undrained row preserved, got %d", undrained)
	}
}

func TestSQLiteDrainer_MarkDrainedErrorPropagates(t *testing.T) {
	dbPath := seedSQLiteMeteringDB(t, standardMeteringSchema+`
		CREATE TRIGGER block_mark_drained BEFORE UPDATE ON metering_ledger
		BEGIN
			SELECT RAISE(ABORT, 'simulated update failure');
		END;
	`, `
		INSERT INTO metering_ledger (user_id, total_tokens, cost_micros, timestamp_iso, drained) VALUES
			('u-ok', 40, 4, '2026-07-01T00:00:01Z', 0);
	`)
	fwd := &threadSafeForwarder{}
	drainer := NewSQLiteDrainer(dbPath, fwd)

	_, err := drainer.DrainOnce(context.Background())
	if err == nil || !strings.Contains(err.Error(), "mark drained") {
		t.Fatalf("expected mark drained error to propagate, got %v", err)
	}
}

func TestSQLiteDrainer_NullUserIDHandledAsPermanentPoisonRow(t *testing.T) {
	dbPath := seedSQLiteMeteringDB(t, standardMeteringSchema, `
		INSERT INTO metering_ledger (user_id, total_tokens, cost_micros, timestamp_iso, drained) VALUES
			(NULL, 10, 1, '2026-07-01T00:00:01Z', 0),
			('u-valid', 90, 9, '2026-07-01T00:00:02Z', 0);
	`)
	fwd := &threadSafeForwarder{}
	drainer := NewSQLiteDrainer(dbPath, fwd)

	n, err := drainer.DrainOnce(context.Background())
	if err != nil {
		t.Fatalf("DrainOnce failed: %v", err)
	}
	if n != 1 || len(fwd.events) != 1 || fwd.events[0].UserID != "u-valid" {
		t.Fatalf("expected 1 valid forwarded event, got n=%d events=%+v", n, fwd.events)
	}
	db, _ := sql.Open("sqlite", dbPath)
	defer db.Close()
	var undrained int
	_ = db.QueryRow(`SELECT COUNT(*) FROM metering_ledger WHERE drained = 0`).Scan(&undrained)
	if undrained != 0 {
		t.Fatalf("expected NULL user_id row to be marked drained=1, undrained=%d", undrained)
	}
}

type panicOnCallCheckoutCreator struct{}

func (panicOnCallCheckoutCreator) Name() string { return "panic" }
func (panicOnCallCheckoutCreator) Create(context.Context, account.User, account.PlanTier, string, string) (string, error) {
	panic("CheckoutCreator.Create must not be called when allowedBaseURL is empty")
}

type panicOnCallPortalCreator struct{}

func (panicOnCallPortalCreator) Name() string { return "panic" }
func (panicOnCallPortalCreator) Create(context.Context, string, string) (string, error) {
	panic("PortalCreator.Create must not be called when allowedBaseURL is empty")
}

func TestCheckoutAndPortalHandler_EmptyAllowedBaseFailsClosed503(t *testing.T) {
	gin.SetMode(gin.TestMode)
	withAuth := func(h gin.HandlerFunc) *gin.Engine {
		r := gin.New()
		r.Use(func(c *gin.Context) {
			c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-1"}))
			c.Next()
		})
		r.POST("/ep", h)
		return r
	}

	user := account.User{ID: "u-1", Email: "u@example.com", StripeCustomerID: "cus_1"}
	w1 := httptest.NewRecorder()
	req1 := httptest.NewRequest(http.MethodPost, "/ep", strings.NewReader(`{"tier":"pro","success_url":"https://app.example/s","cancel_url":"https://app.example/c"}`))
	req1.Header.Set("Content-Type", "application/json")
	withAuth(CheckoutHandler(fakeUserLookupR2{u: user}, panicOnCallCheckoutCreator{}, "   ")).ServeHTTP(w1, req1)
	if w1.Code != http.StatusServiceUnavailable || !strings.Contains(w1.Body.String(), "checkout_unavailable") {
		t.Fatalf("CheckoutHandler empty base: got %d (%s), want 503 checkout_unavailable", w1.Code, w1.Body.String())
	}

	w2 := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodPost, "/ep", strings.NewReader(`{"return_url":"https://app.example/r"}`))
	req2.Header.Set("Content-Type", "application/json")
	withAuth(PortalHandler(fakeUserLookupR2{u: user}, panicOnCallPortalCreator{}, "")).ServeHTTP(w2, req2)
	if w2.Code != http.StatusServiceUnavailable || !strings.Contains(w2.Body.String(), "portal_unavailable") {
		t.Fatalf("PortalHandler empty base: got %d (%s), want 503 portal_unavailable", w2.Code, w2.Body.String())
	}
}

type fakeRow struct {
	scanFn func(dest ...any) error
}

func (r fakeRow) Scan(dest ...any) error { return r.scanFn(dest...) }

type fakePgxConn struct {
	queryRowFn func(ctx context.Context, sql string, args ...any) pgx.Row
	queryFn    func(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	execFn     func(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

func (f *fakePgxConn) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	if f.execFn != nil {
		return f.execFn(ctx, sql, args...)
	}
	return pgconn.CommandTag{}, nil
}

func (f *fakePgxConn) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	if f.queryFn != nil {
		return f.queryFn(ctx, sql, args...)
	}
	return nil, errors.New("unexpected Query")
}

func (f *fakePgxConn) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	if f.queryRowFn != nil {
		return f.queryRowFn(ctx, sql, args...)
	}
	return fakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
}

func TestGetUserPlan_DistinguishesUserNotFoundVsPlanNotFound(t *testing.T) {
	ctx := context.Background()

	// 1. Missing user row -> pgx.ErrNoRows -> ErrUserNotFound
	s1 := NewStore(&fakePgxConn{
		queryRowFn: func(_ context.Context, sql string, _ ...any) pgx.Row {
			if !strings.Contains(sql, "LEFT JOIN plans") {
				t.Fatalf("expected LEFT JOIN plans in SQL, got: %s", sql)
			}
			return fakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
		},
	})
	if _, err := s1.GetUserPlan(ctx, "u-missing"); !errors.Is(err, ErrUserNotFound) {
		t.Fatalf("missing user: got %v, want ErrUserNotFound", err)
	}

	// 2. User row exists, plan_tier missing from plans -> NULL columns -> ErrPlanNotFound
	s2 := NewStore(&fakePgxConn{
		queryRowFn: func(_ context.Context, _ string, _ ...any) pgx.Row {
			return fakeRow{scanFn: func(dest ...any) error {
				*(dest[0].(*sql.NullString)) = sql.NullString{Valid: false}
				*(dest[1].(*sql.NullInt64)) = sql.NullInt64{Valid: false}
				*(dest[2].(*sql.NullInt64)) = sql.NullInt64{Valid: false}
				*(dest[3].(*string)) = ""
				return nil
			}}
		},
	})
	if _, err := s2.GetUserPlan(ctx, "u-orphan-plan"); !errors.Is(err, ErrPlanNotFound) {
		t.Fatalf("missing plan: got %v, want ErrPlanNotFound", err)
	}

	// 3. Valid user + plan -> Plan returned
	s3 := NewStore(&fakePgxConn{
		queryRowFn: func(_ context.Context, _ string, _ ...any) pgx.Row {
			return fakeRow{scanFn: func(dest ...any) error {
				*(dest[0].(*sql.NullString)) = sql.NullString{String: "pro", Valid: true}
				*(dest[1].(*sql.NullInt64)) = sql.NullInt64{Int64: 500000, Valid: true}
				*(dest[2].(*sql.NullInt64)) = sql.NullInt64{Int64: 5, Valid: true}
				*(dest[3].(*string)) = "Pro Plan"
				return nil
			}}
		},
	})
	p, err := s3.GetUserPlan(ctx, "u-valid")
	if err != nil {
		t.Fatalf("valid GetUserPlan err: %v", err)
	}
	if p.Tier != "pro" || p.MonthlyTokenCap != 500000 || p.MaxConcurrentRuns != 5 || p.Description != "Pro Plan" {
		t.Fatalf("unexpected Plan: %+v", p)
	}
}

func TestUsageHandler_ErrUserNotFoundVsErrPlanNotFound(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		name string
		err  error
		want string
	}{
		{"ErrUserNotFound", ErrUserNotFound, "user not found"},
		{"ErrPlanNotFound", ErrPlanNotFound, "plan not found"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := gin.New()
			r.Use(func(c *gin.Context) {
				c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-1"}))
				c.Next()
			})
			r.GET("/usage", UsageHandler(fakeQuotaR2{err: tc.err}, nil))
			w := httptest.NewRecorder()
			r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/usage", nil))
			if w.Code != http.StatusNotFound || !strings.Contains(w.Body.String(), tc.want) {
				t.Fatalf("%s: got %d (%s), want 404 %q", tc.name, w.Code, w.Body.String(), tc.want)
			}
		})
	}
}

type emptyRows struct{}

func (emptyRows) Close()                                       {}
func (emptyRows) Err() error                                   { return nil }
func (emptyRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (emptyRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (emptyRows) Next() bool                                   { return false }
func (emptyRows) Scan(...any) error                            { return nil }
func (emptyRows) Values() ([]any, error)                       { return nil, nil }
func (emptyRows) RawValues() [][]byte                          { return nil }
func (emptyRows) Conn() *pgx.Conn                              { return nil }

func TestGetAgentBreakdown_IndexFriendlyUTCRangeAndInvalidYearMonth(t *testing.T) {
	ctx := context.Background()
	dbCalls := 0
	var capturedSQL string
	var capturedArgs []any

	s := NewStore(&fakePgxConn{
		queryFn: func(_ context.Context, sql string, args ...any) (pgx.Rows, error) {
			dbCalls++
			capturedSQL = sql
			capturedArgs = args
			return emptyRows{}, nil
		},
	})

	for _, bad := range []string{"", "2026-13", "2026-00", "+026-01", "-026-01", "2026- 6", "2026-06-01", "abcd-01"} {
		if _, err := s.GetAgentBreakdown(ctx, "u-1", bad); !errors.Is(err, ErrInvalidYearMonth) {
			t.Fatalf("bad yearMonth %q: got %v, want ErrInvalidYearMonth", bad, err)
		}
		if ValidYearMonth(bad) {
			t.Fatalf("ValidYearMonth(%q) = true, want false", bad)
		}
	}
	if dbCalls != 0 {
		t.Fatalf("expected 0 DB calls on invalid yearMonth, got %d", dbCalls)
	}

	if _, err := s.GetAgentBreakdown(ctx, "u-1", "2026-12"); err != nil {
		t.Fatalf("valid GetAgentBreakdown err: %v", err)
	}
	if dbCalls != 1 {
		t.Fatalf("expected 1 DB call, got %d", dbCalls)
	}
	if strings.Contains(capturedSQL, "to_char") || !strings.Contains(capturedSQL, "ts >= $2") || !strings.Contains(capturedSQL, "ts < $3") {
		t.Fatalf("expected SARGable ts range without to_char, got SQL: %s", capturedSQL)
	}
	wantStart := time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)
	wantEnd := time.Date(2027, 1, 1, 0, 0, 0, 0, time.UTC)
	if len(capturedArgs) != 3 || capturedArgs[1] != wantStart || capturedArgs[2] != wantEnd {
		t.Fatalf("unexpected query args: %+v (want [%v %v])", capturedArgs, wantStart, wantEnd)
	}
}

func TestUpsertMonthlyRollup_RejectsZeroTimestamp(t *testing.T) {
	s := NewStore(&fakePgxConn{})
	if err := s.UpsertMonthlyRollup(context.Background(), "u-1", time.Time{}, 10, 5); err == nil {
		t.Fatal("expected error for zero timestamp")
	}
}

func TestLoadPriceMapFromEnv_RejectsCompTier(t *testing.T) {
	if _, err := LoadPriceMapFromEnv("price_comp=comp"); err == nil {
		t.Fatal("expected error when STRIPE_PRICE_MAP assigns comp tier")
	}
}

func TestStripeCreatorsAndReconciler_NilOptionsAndEmptyActivePrice(t *testing.T) {
	ctx := context.Background()

	// Nil option guards do not overwrite valid default http.Client.
	cc, err := NewStripeCheckoutCreator("sk_test", PriceTierMap{"price_pro": account.PlanPro}, nil, WithCheckoutHTTPClient(nil))
	if err != nil {
		t.Fatalf("NewStripeCheckoutCreator: %v", err)
	}
	if cc.httpClient == nil {
		t.Fatal("WithCheckoutHTTPClient(nil) cleared httpClient")
	}
	var nilCC *StripeCheckoutCreator
	if _, err := nilCC.Create(ctx, account.User{ID: "u-1"}, account.PlanPro, "https://a.example/s", "https://a.example/c"); err == nil {
		t.Fatal("nil StripeCheckoutCreator.Create must return error")
	}

	pc, err := NewStripePortalCreator("sk_test", WithPortalHTTPClient(nil))
	if err != nil {
		t.Fatalf("NewStripePortalCreator: %v", err)
	}
	if pc.httpClient == nil {
		t.Fatal("WithPortalHTTPClient(nil) cleared httpClient")
	}
	var nilPC *StripePortalCreator
	if _, err := nilPC.Create(ctx, "cus_1", "https://a.example/r"); err == nil {
		t.Fatal("nil StripePortalCreator.Create must return error")
	}

	// createCustomer 401/403 wraps ErrCheckoutUnavailable.
	srv401 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"message":"Invalid API Key"}}`))
	}))
	defer srv401.Close()
	cc401, _ := NewStripeCheckoutCreator("sk_bad", PriceTierMap{"price_pro": account.PlanPro}, nil, WithCheckoutAPIBase(srv401.URL), WithCheckoutHTTPClient(srv401.Client()))
	if _, err := cc401.Create(ctx, account.User{ID: "u-1", Email: "u@example.com"}, account.PlanPro, "https://a.example/s", "https://a.example/c"); !errors.Is(err, ErrCheckoutUnavailable) {
		t.Fatalf("401 from /v1/customers: got %v, want wrapping ErrCheckoutUnavailable", err)
	}

	// FetchActiveSubscription with WithHTTPClient(nil) and empty price.id in active item.
	srvEmptyPrice := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"id":"sub_1","status":"active","items":{"data":[{"price":{"id":""}}]}}],"has_more":false}`))
	}))
	defer srvEmptyPrice.Close()
	f, err := NewStripeSubscriptionFetcher("sk_test")
	if err != nil {
		t.Fatalf("NewStripeSubscriptionFetcher: %v", err)
	}
	f.WithHTTPClient(nil).WithAPIBase(srvEmptyPrice.URL)
	if f.httpClient == nil {
		t.Fatal("WithHTTPClient(nil) cleared httpClient")
	}
	if _, err := f.FetchActiveSubscription(ctx, "cus_1"); err == nil {
		t.Fatal("expected error when active subscription item has empty price.id")
	}
}

func TestPubSubDrainer_NilReceiverReturnsError(t *testing.T) {
	var nilDrainer *PubSubDrainer
	if err := nilDrainer.Run(context.Background()); err == nil {
		t.Fatal("expected error from nil *PubSubDrainer.Run")
	}
}

func TestBillingStore_TypedNilPgxConnReturnsErrors(t *testing.T) {
	ctx := context.Background()
	stores := []struct {
		name  string
		store *Store
	}{
		{"untyped nil", NewStore(nil)},
		{"typed nil *pgxpool.Pool", NewStore((*pgxpool.Pool)(nil))},
		{"typed nil *pgx.Conn", NewStore((*pgx.Conn)(nil))},
	}

	for _, tc := range stores {
		t.Run(tc.name, func(t *testing.T) {
			s := tc.store
			if _, err := s.InsertEvent(ctx, Event{UserID: "u-1"}); err == nil {
				t.Fatal("InsertEvent must return error on nil/typed-nil store")
			}
			if err := s.UpsertMonthlyRollup(ctx, "u-1", time.Now().UTC(), 10, 5); err == nil {
				t.Fatal("UpsertMonthlyRollup must return error on nil/typed-nil store")
			}
			if _, err := s.GetMonthlyUsage(ctx, "u-1", "2026-06"); err == nil {
				t.Fatal("GetMonthlyUsage must return error on nil/typed-nil store")
			}
			if _, err := s.GetAgentBreakdown(ctx, "u-1", "2026-06"); err == nil {
				t.Fatal("GetAgentBreakdown must return error on nil/typed-nil store")
			}
			if _, err := s.GetUserPlan(ctx, "u-1"); err == nil {
				t.Fatal("GetUserPlan must return error on nil/typed-nil store")
			}
			if _, err := s.CheckQuota(ctx, "u-1"); err == nil {
				t.Fatal("CheckQuota must return error on nil/typed-nil store")
			}
			if _, err := s.ListPlans(ctx); err == nil {
				t.Fatal("ListPlans must return error on nil/typed-nil store")
			}
		})
	}
}

func TestStripeProvider_NilReceiverReturnsError(t *testing.T) {
	var nilProv *StripeProvider
	var prov Provider = nilProv
	if prov.Name() != "stripe" {
		t.Fatalf("nil StripeProvider.Name() = %q, want stripe", prov.Name())
	}
	if _, err := prov.VerifyAndParse([]byte(`{"id":"evt_1"}`), "t=1,v1=deadbeef", time.Now()); err == nil {
		t.Fatal("expected (*StripeProvider)(nil).VerifyAndParse to return an error without panicking")
	}
}

func TestSQLiteDrainer_ScanErrorReturnsZeroForwarded(t *testing.T) {
	ctx := context.Background()

	t.Run("standard schema null timestamp_iso", func(t *testing.T) {
		dbPath := seedSQLiteMeteringDB(t, `
			CREATE TABLE metering_ledger (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				user_id TEXT,
				sim_id TEXT,
				agent_type TEXT,
				model TEXT,
				prompt_tokens INTEGER,
				completion_tokens INTEGER,
				total_tokens INTEGER,
				cost_micros INTEGER,
				timestamp_iso TEXT,
				drained INTEGER NOT NULL DEFAULT 0
			);
		`, `
			INSERT INTO metering_ledger (user_id, total_tokens, cost_micros, timestamp_iso, drained)
			VALUES ('u-1', 100, 10, '2026-06-25T10:00:00Z', 0);
			INSERT INTO metering_ledger (user_id, total_tokens, cost_micros, timestamp_iso, drained)
			VALUES ('u-2', 200, 20, NULL, 0);
		`)
		fwd := &threadSafeForwarder{}
		drainer := NewSQLiteDrainer(dbPath, fwd)
		n, err := drainer.DrainOnce(ctx)
		if err == nil {
			t.Fatal("expected scan error for NULL timestamp_iso")
		}
		if n != 0 {
			t.Fatalf("DrainOnce returned n=%d on scan error, want 0 (0 rows forwarded)", n)
		}
		if len(fwd.events) != 0 {
			t.Fatalf("expected 0 forwarded events on scan error, got %d", len(fwd.events))
		}
	})

	t.Run("legacy schema null timestamp_iso", func(t *testing.T) {
		dbPath := seedSQLiteMeteringDB(t, `
			CREATE TABLE metering_ledger (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				user_id TEXT,
				sim_id TEXT,
				agent_type TEXT,
				model TEXT,
				prompt_tokens INTEGER,
				completion_tokens INTEGER,
				total_tokens INTEGER,
				timestamp_iso TEXT,
				drained INTEGER NOT NULL DEFAULT 0
			);
		`, `
			INSERT INTO metering_ledger (user_id, total_tokens, timestamp_iso, drained)
			VALUES ('u-1', 100, '2026-06-25T10:00:00Z', 0);
			INSERT INTO metering_ledger (user_id, total_tokens, timestamp_iso, drained)
			VALUES ('u-2', 200, NULL, 0);
		`)
		fwd := &threadSafeForwarder{}
		drainer := NewSQLiteDrainer(dbPath, fwd)
		n, err := drainer.DrainOnce(ctx)
		if err == nil {
			t.Fatal("expected legacy scan error for NULL timestamp_iso")
		}
		if n != 0 {
			t.Fatalf("DrainOnce legacy returned n=%d on scan error, want 0 (0 rows forwarded)", n)
		}
		if len(fwd.events) != 0 {
			t.Fatalf("expected 0 forwarded events on legacy scan error, got %d", len(fwd.events))
		}
	})
}

type fakeReconcileUsers struct {
	user account.User
}

func (f *fakeReconcileUsers) GetUser(_ context.Context, _ string) (account.User, error) {
	return f.user, nil
}

type fakePlanWriter struct {
	calls int
	tier  account.PlanTier
}

func (f *fakePlanWriter) SetPlan(_ context.Context, _ string, tier account.PlanTier) error {
	f.calls++
	f.tier = tier
	return nil
}

func TestFetchActiveSubscription_EmptyItemsReturnsErrorAndDoesNotDowngrade(t *testing.T) {
	ctx := context.Background()
	respBody := `{"data":[{"id":"sub_1","status":"active","items":{"data":[]}}],"has_more":false}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(respBody))
	}))
	defer srv.Close()

	f, err := NewStripeSubscriptionFetcher("sk_test")
	if err != nil {
		t.Fatalf("NewStripeSubscriptionFetcher: %v", err)
	}
	f.WithAPIBase(srv.URL).WithHTTPClient(srv.Client())

	if _, err := f.FetchActiveSubscription(ctx, "cus_1"); err == nil {
		t.Fatal("expected error when active subscription has empty items.data")
	}

	writer := &fakePlanWriter{}
	rec := &Reconciler{
		Users:    &fakeReconcileUsers{user: account.User{ID: "u-1", PlanTier: account.PlanPro, StripeCustomerID: "cus_1"}},
		Fetcher:  f,
		PriceMap: PriceTierMap{"price_pro": account.PlanPro},
		Writer:   writer,
	}
	if _, err := rec.Reconcile(ctx, "u-1"); err == nil {
		t.Fatal("expected Reconcile to fail when active subscription has empty items.data")
	}
	if writer.calls != 0 {
		t.Fatalf("Reconcile must NOT downgrade user when active subscription has empty items.data (SetPlan calls=%d)", writer.calls)
	}

	// Contrast: genuinely empty subscription list (no active subscription) still downgrades to free.
	respBody = `{"data":[],"has_more":false}`
	res, err := rec.Reconcile(ctx, "u-1")
	if err != nil {
		t.Fatalf("Reconcile with no active subscription failed: %v", err)
	}
	if res.ToTier != account.PlanFree || writer.calls != 1 {
		t.Fatalf("expected downgrade to free with 1 SetPlan call, got res=%+v calls=%d", res, writer.calls)
	}
}

func TestPubSubReceiverAndStripeFetcher_NilReceivers(t *testing.T) {
	var nilRcv *PubSubReceiver
	if err := nilRcv.Close(); err != nil {
		t.Fatalf("expected (*PubSubReceiver)(nil).Close() == nil, got %v", err)
	}
	if err := (&PubSubReceiver{}).Close(); err != nil {
		t.Fatalf("expected (&PubSubReceiver{}).Close() == nil, got %v", err)
	}
	if err := nilRcv.Receive(context.Background(), nil); err == nil {
		t.Fatal("expected (*PubSubReceiver)(nil).Receive to return error")
	}
	if err := (&PubSubReceiver{}).Receive(context.Background(), nil); err == nil {
		t.Fatal("expected (&PubSubReceiver{}).Receive to return error")
	}

	var nilFetcher *StripeSubscriptionFetcher
	if got := nilFetcher.WithAPIBase("http://example.com"); got != nil {
		t.Fatalf("expected (*StripeSubscriptionFetcher)(nil).WithAPIBase to return nil, got %v", got)
	}
	if got := nilFetcher.WithHTTPClient(http.DefaultClient); got != nil {
		t.Fatalf("expected (*StripeSubscriptionFetcher)(nil).WithHTTPClient to return nil, got %v", got)
	}
}

func TestStripeProvider_ZeroValueAndNilWrapErrSignatureInvalid(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	payload := []byte(sampleCheckoutPayload)
	forgedHeader := signStripePayload(t, "", now, payload)

	var nilProv *StripeProvider
	if _, err := nilProv.VerifyAndParse(payload, forgedHeader, now); !errors.Is(err, ErrSignatureInvalid) {
		t.Fatalf("(*StripeProvider)(nil).VerifyAndParse = %v, want wrapping ErrSignatureInvalid", err)
	}

	zeroProv := &StripeProvider{}
	if ev, err := zeroProv.VerifyAndParse(payload, forgedHeader, now); !errors.Is(err, ErrSignatureInvalid) || ev != nil {
		t.Fatalf("(&StripeProvider{}).VerifyAndParse with empty-secret signature = (%v, %v), want (nil, wrapping ErrSignatureInvalid)", ev, err)
	}
}

func TestInMemoryEventDedup_ZeroValueAndNilSafe(t *testing.T) {
	var nilDedup *InMemoryEventDedup
	if got := nilDedup.Len(); got != 0 {
		t.Fatalf("(*InMemoryEventDedup)(nil).Len() = %d, want 0", got)
	}
	if seen, err := nilDedup.SeenOrMark("e1"); seen || !errors.Is(err, ErrDedupUnavailable) {
		t.Fatalf("(*InMemoryEventDedup)(nil).SeenOrMark = (%v, %v), want (false, ErrDedupUnavailable)", seen, err)
	}
	nilDedup.Forget("e1")

	zeroDedup := &InMemoryEventDedup{}
	if got := zeroDedup.Len(); got != 0 {
		t.Fatalf("(&InMemoryEventDedup{}).Len() = %d, want 0", got)
	}
	if seen, err := zeroDedup.SeenOrMark("e1"); seen || !errors.Is(err, ErrDedupUnavailable) {
		t.Fatalf("(&InMemoryEventDedup{}).SeenOrMark(e1) = (%v, %v), want (false, ErrDedupUnavailable)", seen, err)
	}
	if seen, err := zeroDedup.SeenOrMark(""); seen || !errors.Is(err, ErrDedupUnavailable) {
		t.Fatalf("(&InMemoryEventDedup{}).SeenOrMark('') = (%v, %v), want (false, ErrDedupUnavailable)", seen, err)
	}
	zeroDedup.Forget("e1")

	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			id := fmt.Sprintf("evt_%d", idx)
			seen, err := zeroDedup.SeenOrMark(id)
			if seen || !errors.Is(err, ErrDedupUnavailable) {
				t.Errorf("concurrent zero SeenOrMark(%s) = (%v, %v), want (false, ErrDedupUnavailable)", id, seen, err)
			}
			if got := zeroDedup.Len(); got != 0 {
				t.Errorf("concurrent zero Len() = %d, want 0", got)
			}
			zeroDedup.Forget(id)
		}(i)
	}
	wg.Wait()
}

func TestSQLiteDrainer_ZeroValueAndNilRunNoPanic(t *testing.T) {
	cancelledCtx, cancel := context.WithCancel(context.Background())
	cancel()

	var nilDrainer *SQLiteDrainer
	nilDrainer.Run(cancelledCtx)

	zeroDrainer := &SQLiteDrainer{}
	zeroDrainer.Run(cancelledCtx)

	shortCtx, shortCancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer shortCancel()
	zeroDrainer.Run(shortCtx)

	missingDBPath := filepath.Join(t.TempDir(), "missing.db")
	noIntervalDrainer := &SQLiteDrainer{
		dbPath:    missingDBPath,
		forwarder: &threadSafeForwarder{},
		sqlOpen:   sql.Open,
	}
	noIntervalCtx, noIntervalCancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer noIntervalCancel()
	noIntervalDrainer.Run(noIntervalCtx)

	noSQLOpenDrainer := &SQLiteDrainer{
		dbPath:    missingDBPath,
		forwarder: &threadSafeForwarder{},
	}
	if _, err := noSQLOpenDrainer.DrainOnce(context.Background()); err == nil {
		t.Fatal("expected DrainOnce with nil sqlOpen to return error")
	}
}

type fakeMsg struct {
	data   []byte
	acked  bool
	nacked bool
}

func (m *fakeMsg) Data() []byte                  { return m.data }
func (m *fakeMsg) Attributes() map[string]string { return nil }
func (m *fakeMsg) Ack()                          { m.acked = true }
func (m *fakeMsg) Nack()                         { m.nacked = true }

func TestPubSubReceiver_NilHandlerReturnsError(t *testing.T) {
	rcv := &PubSubReceiver{subscriber: &pubsub.Subscriber{}}
	err := rcv.Receive(context.Background(), nil)
	if err == nil || !strings.Contains(err.Error(), "handler is nil") {
		t.Fatalf("expected handler is nil error, got %v", err)
	}

	// Zero-optional-fields PubSubDrainer with nil logf/clock uses defaults without panic.
	fwd := &threadSafeForwarder{}
	drainer := &PubSubDrainer{forwarder: fwd}
	msg := &fakeMsg{data: []byte(`{"user_id":"u-1","total_tokens":5}`)}
	drainer.handle(context.Background(), msg)
	if !msg.acked || len(fwd.events) != 1 {
		t.Fatalf("expected message acked and 1 event forwarded, got acked=%v events=%d", msg.acked, len(fwd.events))
	}
}

func TestStripePartialStructLiterals_FailClosed(t *testing.T) {
	ctx := context.Background()
	partialCC := &StripeCheckoutCreator{httpClient: http.DefaultClient}
	if _, err := partialCC.Create(ctx, account.User{ID: "u-1"}, account.PlanPro, "https://a.example/s", "https://a.example/c"); err == nil {
		t.Fatal("expected partial StripeCheckoutCreator without apiKey/apiBase to fail closed")
	}
	partialPC := &StripePortalCreator{httpClient: http.DefaultClient}
	if _, err := partialPC.Create(ctx, "cus_1", "https://a.example/r"); err == nil {
		t.Fatal("expected partial StripePortalCreator without apiKey/apiBase to fail closed")
	}
	partialSF := (&StripeSubscriptionFetcher{}).WithHTTPClient(http.DefaultClient)
	if _, err := partialSF.FetchActiveSubscription(ctx, "cus_1"); err == nil {
		t.Fatal("expected partial StripeSubscriptionFetcher without apiKey/apiBase to fail closed")
	}
}
