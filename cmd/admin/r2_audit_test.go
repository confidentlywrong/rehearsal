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

package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type fakeAdminUserRow struct {
	user account.User
	err  error
}

func (r fakeAdminUserRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	*(dest[0].(*string)) = r.user.ID
	*(dest[1].(*string)) = r.user.Email
	*(dest[2].(*string)) = r.user.DisplayName
	*(dest[3].(*string)) = r.user.Timezone
	*(dest[4].(*string)) = r.user.StripeCustomerID
	*(dest[5].(*string)) = string(r.user.PlanTier)
	*(dest[6].(**time.Time)) = r.user.PaymentFailedAt
	*(dest[7].(*bool)) = r.user.IsSuperadmin
	*(dest[8].(*time.Time)) = r.user.CreatedAt
	*(dest[9].(**time.Time)) = r.user.DeletedAt
	*(dest[10].(**time.Time)) = r.user.TokensInvalidAfter
	*(dest[11].(*string)) = string(r.user.Status)
	*(dest[12].(**time.Time)) = r.user.ApprovedAt
	*(dest[13].(*string)) = r.user.ApprovedBy
	return nil
}

type fakeAdminPgxConn struct {
	users       map[string]account.User
	audits      int
	lastAudit   []any
	deletedIDs  []string
}

func (db *fakeAdminPgxConn) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	switch {
	case strings.Contains(sql, "INSERT INTO audit_log"):
		db.audits++
		db.lastAudit = args
		return pgconn.NewCommandTag("INSERT 0 1"), nil
	case strings.Contains(sql, "DELETE FROM users WHERE id = $1"):
		id := args[0].(string)
		if _, ok := db.users[id]; !ok {
			return pgconn.NewCommandTag("DELETE 0"), nil
		}
		delete(db.users, id)
		db.deletedIDs = append(db.deletedIDs, id)
		return pgconn.NewCommandTag("DELETE 1"), nil
	default:
		return pgconn.NewCommandTag("OK 0"), nil
	}
}

func (db *fakeAdminPgxConn) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, nil
}

func (db *fakeAdminPgxConn) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	id := args[0].(string)
	u, ok := db.users[id]
	if !ok {
		return fakeAdminUserRow{err: pgx.ErrNoRows}
	}
	if strings.Contains(sql, "deleted_at IS NULL") && u.DeletedAt != nil {
		return fakeAdminUserRow{err: pgx.ErrNoRows}
	}
	return fakeAdminUserRow{user: u}
}

type nilAgentsUsageReader struct{}

func (nilAgentsUsageReader) GetMonthlyUsage(_ context.Context, userID, yearMonth string) (billing.MonthlyUsage, error) {
	return billing.MonthlyUsage{
		UserID:      userID,
		YearMonth:   yearMonth,
		TotalTokens: 120,
		CostMicros:  450,
		UpdatedAt:   time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC),
	}, nil
}

func (nilAgentsUsageReader) GetAgentBreakdown(context.Context, string, string) ([]billing.AgentBreakdown, error) {
	return nil, nil
}

func TestR2_AdminHealthInfraNilCheckers(t *testing.T) {
	// Passing nil psHealthChecker and nil dbHealthChecker must degrade cleanly to "offline" without panicking.
	r := setupRouter(nil, nil, nil, "http://dummy-gateway", nil, account.NewLocalResolver(), nil, nil)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/health/infra", nil)
	req.RemoteAddr = "127.0.0.1:1234"
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 OK with nil checkers, got %d (%s)", w.Code, w.Body.String())
	}
	body := w.Body.String()
	for _, want := range []string{`"redis":"offline"`, `"pubsub":"offline"`, `"alloydb":"offline"`} {
		if !strings.Contains(body, want) {
			t.Errorf("expected %s in body, got %s", want, body)
		}
	}
}

func TestR2_AdminUsageNilAgentsSerializesEmptySlice(t *testing.T) {
	r := gin.New()
	r.GET("/admin/users/:id/usage", usageUserHandler(nilAgentsUsageReader{}, func() time.Time {
		return time.Date(2026, 7, 15, 0, 0, 0, 0, time.UTC)
	}))

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/admin/users/u-1/usage", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 OK, got %d (%s)", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"agents":[]`) {
		t.Fatalf("expected empty agents slice to serialize as [], got %s", w.Body.String())
	}
}

func TestR2_AdminEraseUserSoftDeletedAndMissing(t *testing.T) {
	softDelTime := time.Date(2026, 6, 20, 12, 0, 0, 0, time.UTC)
	fakeDB := &fakeAdminPgxConn{
		users: map[string]account.User{
			"u-soft": {
				ID:        "u-soft",
				Email:     "soft@example.com",
				PlanTier:  account.PlanFree,
				Status:    account.StatusApproved,
				CreatedAt: softDelTime.Add(-24 * time.Hour),
				DeletedAt: &softDelTime,
			},
		},
	}
	store := account.NewStore(fakeDB)

	r := gin.New()
	r.DELETE("/admin/users/:id/erase", eraseUserHandler(store))

	t.Run("Missing user returns 404 without writing a spurious ActionUserErased audit row", func(t *testing.T) {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodDelete, "/admin/users/missing-id/erase", nil))
		if w.Code != http.StatusNotFound {
			t.Fatalf("expected 404 for missing user, got %d (%s)", w.Code, w.Body.String())
		}
		if fakeDB.audits != 0 {
			t.Fatalf("expected 0 audit entries on 404, got %d", fakeDB.audits)
		}
	})

	t.Run("Soft-deleted user is resolved via GetUserAnyState, audited with metadata, and hard-deleted", func(t *testing.T) {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodDelete, "/admin/users/u-soft/erase", nil))
		if w.Code != http.StatusNoContent {
			t.Fatalf("expected 204 No Content erasing soft-deleted user, got %d (%s)", w.Code, w.Body.String())
		}
		if fakeDB.audits != 1 {
			t.Fatalf("expected 1 audit entry on erase, got %d", fakeDB.audits)
		}
		if len(fakeDB.deletedIDs) != 1 || fakeDB.deletedIDs[0] != "u-soft" {
			t.Fatalf("expected u-soft hard-deleted, got %v", fakeDB.deletedIDs)
		}
		// Audit metadata ($5) must capture email_at_erase and soft_deleted_at from the soft-deleted row.
		metaBytes, ok := fakeDB.lastAudit[4].([]byte)
		if !ok {
			t.Fatalf("expected JSON metadata bytes in audit arg, got %T", fakeDB.lastAudit[4])
		}
		var meta map[string]any
		if err := json.Unmarshal(metaBytes, &meta); err != nil {
			t.Fatalf("unmarshal audit metadata: %v", err)
		}
		if meta["email_at_erase"] != "soft@example.com" {
			t.Errorf("expected email_at_erase=soft@example.com, got %v", meta["email_at_erase"])
		}
		if meta["soft_deleted_at"] != "2026-06-20T12:00:00Z" {
			t.Errorf("expected soft_deleted_at=2026-06-20T12:00:00Z, got %v", meta["soft_deleted_at"])
		}
	})
}
