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
	"bytes"
	"context"
	"database/sql"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgconn"
)

type permanentErrForwarder struct {
	events []Event
}

func (p *permanentErrForwarder) InsertEvent(_ context.Context, e Event) (int64, error) {
	if e.UserID == "" {
		return 0, errors.New("billing: InsertEvent requires user_id")
	}
	if e.UserID == "deleted-user" {
		return 0, &pgconn.PgError{Code: "23503", Message: "insert or update on table \"usage_ledger\" violates foreign key constraint"}
	}
	p.events = append(p.events, e)
	return int64(len(p.events)), nil
}

func (p *permanentErrForwarder) UpsertMonthlyRollup(context.Context, string, time.Time, int64, int64) error {
	return nil
}

func TestR2_SQLiteDrainerSkipsPermanentFKErrorWithoutWedgingBatch(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "permanent_err_metering.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
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
			cost_micros INTEGER,
			timestamp_iso TEXT NOT NULL,
			drained INTEGER NOT NULL DEFAULT 0
		);
		INSERT INTO metering_ledger (user_id, total_tokens, cost_micros, timestamp_iso, drained) VALUES
			('', 10, 100, '2026-07-01T00:00:00Z', 0),
			('deleted-user', 20, 200, '2026-07-01T00:00:01Z', 0),
			('live-user', 30, 300, '2026-07-01T00:00:02Z', 0);
	`)
	_ = db.Close()
	if err != nil {
		t.Fatalf("seed sqlite: %v", err)
	}

	fwd := &permanentErrForwarder{}
	drainer := NewSQLiteDrainer(dbPath, fwd)
	n, err := drainer.DrainOnce(context.Background())
	if err != nil {
		t.Fatalf("DrainOnce failed: %v", err)
	}
	if n != 1 {
		t.Fatalf("expected n=1 actually forwarded row (skipping 2 permanent-error rows), got %d", n)
	}
	if len(fwd.events) != 1 || fwd.events[0].UserID != "live-user" {
		t.Fatalf("unexpected forwarded events: %+v", fwd.events)
	}

	// Verify all 3 rows are marked drained=1 so permanent errors cannot wedge future DrainOnce passes.
	dbCheck, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("reopen sqlite: %v", err)
	}
	defer dbCheck.Close()
	var undrained int
	if err := dbCheck.QueryRow(`SELECT COUNT(*) FROM metering_ledger WHERE drained = 0`).Scan(&undrained); err != nil {
		t.Fatalf("count undrained: %v", err)
	}
	if undrained != 0 {
		t.Fatalf("expected 0 undrained rows remaining, got %d", undrained)
	}
}

type fakeUserLookupR2 struct {
	u   account.User
	err error
}

func (f fakeUserLookupR2) GetUser(context.Context, string) (account.User, error) {
	return f.u, f.err
}

type fakeQuotaR2 struct {
	err error
}

func (f fakeQuotaR2) CheckQuota(context.Context, string) (QuotaState, error) {
	return QuotaState{}, f.err
}

type fakePlansListerR2 struct {
	plans []Plan
}

func (f fakePlansListerR2) ListPlans(context.Context) ([]Plan, error) {
	return f.plans, nil
}

type fakeCheckoutCreatorR2 struct{}

func (fakeCheckoutCreatorR2) Name() string { return "fake" }

func (fakeCheckoutCreatorR2) Create(context.Context, account.User, account.PlanTier, string, string) (string, error) {
	return "https://checkout.stripe.com/test", nil
}

type fakePortalCreatorR2 struct{}

func (fakePortalCreatorR2) Name() string { return "fake" }

func (fakePortalCreatorR2) Create(context.Context, string, string) (string, error) {
	return "https://billing.stripe.com/test", nil
}

func TestR2_BillingHandlersNil503Body413AndPlanNotFound404(t *testing.T) {
	gin.SetMode(gin.TestMode)
	withAuth := func(h gin.HandlerFunc) *gin.Engine {
		r := gin.New()
		r.Use(func(c *gin.Context) {
			ctx := account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-bill-1"})
			c.Request = c.Request.WithContext(ctx)
			c.Next()
		})
		r.Any("/ep", h)
		return r
	}

	// 1. CheckoutHandler + PortalHandler nil dependencies -> 503
	for name, h := range map[string]gin.HandlerFunc{
		"checkout nil users":   CheckoutHandler(nil, fakeCheckoutCreatorR2{}, ""),
		"checkout nil creator": CheckoutHandler(fakeUserLookupR2{}, nil, ""),
		"portal nil users":     PortalHandler(nil, fakePortalCreatorR2{}, ""),
		"portal nil creator":   PortalHandler(fakeUserLookupR2{}, nil, ""),
		"usage nil quota":      UsageHandler(nil, nil),
		"plans nil lister":     PlansHandler(nil),
	} {
		w := httptest.NewRecorder()
		withAuth(h).ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/ep", strings.NewReader(`{}`)))
		if w.Code != http.StatusServiceUnavailable {
			t.Errorf("%s: expected 503, got %d (%s)", name, w.Code, w.Body.String())
		}
	}

	// 2. CheckoutHandler + PortalHandler >1 MiB body -> 413
	huge := append(append([]byte(`{"return_url":"`), bytes.Repeat([]byte("x"), (1<<20)+128)...), []byte(`"}`)...)
	for name, h := range map[string]gin.HandlerFunc{
		"checkout 413": CheckoutHandler(fakeUserLookupR2{}, fakeCheckoutCreatorR2{}, ""),
		"portal 413":   PortalHandler(fakeUserLookupR2{}, fakePortalCreatorR2{}, ""),
	} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/ep", bytes.NewReader(huge))
		req.Header.Set("Content-Type", "application/json")
		withAuth(h).ServeHTTP(w, req)
		if w.Code != http.StatusRequestEntityTooLarge {
			t.Errorf("%s: expected 413, got %d (%s)", name, w.Code, w.Body.String())
		}
	}

	// 3. UsageHandler maps ErrPlanNotFound -> 404 (not 500)
	wUsage := httptest.NewRecorder()
	withAuth(UsageHandler(fakeQuotaR2{err: ErrPlanNotFound}, nil)).ServeHTTP(wUsage, httptest.NewRequest(http.MethodGet, "/ep", nil))
	if wUsage.Code != http.StatusNotFound {
		t.Fatalf("UsageHandler(ErrPlanNotFound): expected 404, got %d (%s)", wUsage.Code, wUsage.Body.String())
	}

	// 4. PlansHandler normalizes nil slice -> {"plans":[]}
	wPlans := httptest.NewRecorder()
	withAuth(PlansHandler(fakePlansListerR2{plans: nil})).ServeHTTP(wPlans, httptest.NewRequest(http.MethodGet, "/ep", nil))
	if wPlans.Code != http.StatusOK || !strings.Contains(wPlans.Body.String(), `"plans":[]`) {
		t.Fatalf("PlansHandler(nil slice): expected 200 with \"plans\":[], got %d (%s)", wPlans.Code, wPlans.Body.String())
	}
}

func TestR2_PriceTierMapDeterministicPriceForTier(t *testing.T) {
	m := PriceTierMap{
		"price_z_pro": account.PlanPro,
		"price_a_pro": account.PlanPro,
		"price_m_pro": account.PlanPro,
	}
	for i := 0; i < 50; i++ {
		if got := m.PriceForTier(account.PlanPro); got != "price_a_pro" {
			t.Fatalf("iter %d: PriceForTier(PlanPro) = %q, want \"price_a_pro\"", i, got)
		}
	}
}
