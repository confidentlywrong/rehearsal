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
	"sync"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type t5AuditRows struct {
	entries []account.AuditEntry
	idx     int
}

func (r *t5AuditRows) Close()                                       {}
func (r *t5AuditRows) Err() error                                   { return nil }
func (r *t5AuditRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *t5AuditRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *t5AuditRows) Values() ([]any, error)                       { return nil, nil }
func (r *t5AuditRows) RawValues() [][]byte                          { return nil }
func (r *t5AuditRows) Conn() *pgx.Conn                              { return nil }

func (r *t5AuditRows) Next() bool {
	if r.idx >= len(r.entries) {
		return false
	}
	r.idx++
	return true
}

func (r *t5AuditRows) Scan(dest ...any) error {
	e := r.entries[r.idx-1]
	*(dest[0].(*int64)) = e.ID
	*(dest[1].(*string)) = e.UserID
	*(dest[2].(*string)) = e.ActorID
	*(dest[3].(*string)) = e.Action
	*(dest[4].(*string)) = e.Target
	var metaBytes []byte
	if len(e.Metadata) > 0 {
		b, err := json.Marshal(e.Metadata)
		if err != nil {
			return err
		}
		metaBytes = b
	}
	*(dest[5].(*[]byte)) = metaBytes
	*(dest[6].(*time.Time)) = e.CreatedAt
	return nil
}

type t5AuditPgxConn struct {
	mu     sync.Mutex
	users  map[string]account.User
	audits []account.AuditEntry
	nextID int64
}

func (db *t5AuditPgxConn) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	db.mu.Lock()
	defer db.mu.Unlock()

	switch {
	case strings.Contains(sql, "UPDATE users") && strings.Contains(sql, "SET status = $2"):
		uid := args[0].(string)
		status := account.UserStatus(args[1].(string))
		actorID := args[3].(string)
		u, ok := db.users[uid]
		if !ok || u.DeletedAt != nil {
			return pgconn.NewCommandTag("UPDATE 0"), nil
		}
		u.Status = status
		u.ApprovedBy = actorID
		if approvedAt, ok := args[2].(time.Time); ok {
			u.ApprovedAt = &approvedAt
		}
		db.users[uid] = u
		return pgconn.NewCommandTag("UPDATE 1"), nil

	case strings.Contains(sql, "INSERT INTO audit_log"):
		db.nextID++
		var uid string
		if s, ok := args[0].(string); ok {
			uid = s
		}
		actorID, _ := args[1].(string)
		action, _ := args[2].(string)
		target, _ := args[3].(string)
		var meta map[string]any
		if raw, ok := args[4].([]byte); ok && len(raw) > 0 {
			_ = json.Unmarshal(raw, &meta)
		}
		db.audits = append(db.audits, account.AuditEntry{
			ID:        db.nextID,
			UserID:    uid,
			ActorID:   actorID,
			Action:    action,
			Target:    target,
			Metadata:  meta,
			CreatedAt: time.Now().UTC(),
		})
		return pgconn.NewCommandTag("INSERT 0 1"), nil

	default:
		return pgconn.NewCommandTag("OK 0"), nil
	}
}

func (db *t5AuditPgxConn) Query(_ context.Context, sql string, args ...any) (pgx.Rows, error) {
	db.mu.Lock()
	defer db.mu.Unlock()

	if !strings.Contains(sql, "FROM audit_log") {
		return &t5AuditRows{}, nil
	}
	filterUser, _ := args[0].(string)
	filterAction, _ := args[1].(string)
	var matched []account.AuditEntry
	for i := len(db.audits) - 1; i >= 0; i-- {
		e := db.audits[i]
		if filterUser != "" && e.UserID != filterUser {
			continue
		}
		if filterAction != "" && e.Action != filterAction {
			continue
		}
		matched = append(matched, e)
	}
	return &t5AuditRows{entries: matched}, nil
}

func (db *t5AuditPgxConn) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	db.mu.Lock()
	defer db.mu.Unlock()

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

type t5StaticAdminResolver struct {
	uc account.UserContext
}

func (r t5StaticAdminResolver) Resolve(_ *http.Request) (account.UserContext, error) {
	return r.uc, nil
}

func TestT5_SetUserStatus_AuditsSubjectUserIDDistinctFromActorID(t *testing.T) {
	gin.SetMode(gin.TestMode)

	const (
		adminActorID  = "admin-actor-1"
		approveUserID = "subject-approve-1"
		rejectUserID  = "subject-reject-1"
	)

	now := time.Now().UTC()
	db := &t5AuditPgxConn{
		users: map[string]account.User{
			adminActorID: {
				ID:           adminActorID,
				Email:        "admin@example.com",
				PlanTier:     account.PlanFree,
				Status:       account.StatusApproved,
				IsSuperadmin: true,
				CreatedAt:    now,
			},
			approveUserID: {
				ID:        approveUserID,
				Email:     "approve@example.com",
				PlanTier:  account.PlanFree,
				Status:    account.StatusPending,
				CreatedAt: now,
			},
			rejectUserID: {
				ID:        rejectUserID,
				Email:     "reject@example.com",
				PlanTier:  account.PlanFree,
				Status:    account.StatusPending,
				CreatedAt: now,
			},
		},
	}
	store := account.NewStore(db)
	resolver := t5StaticAdminResolver{
		uc: account.UserContext{
			UserID:       adminActorID,
			Email:        "admin@example.com",
			IsSuperadmin: true,
			Status:       account.StatusApproved,
		},
	}

	r := setupRouter(nil, nil, nil, "http://127.0.0.1:9101", nil, resolver, store, nil)

	// 1. Approve subject user -> 200 and audit attributed to approveUserID (not adminActorID).
	wApp := httptest.NewRecorder()
	r.ServeHTTP(wApp, httptest.NewRequest(http.MethodPost, "/admin/users/"+approveUserID+"/approve", nil))
	if wApp.Code != http.StatusOK {
		t.Fatalf("POST approve status=%d body=%s", wApp.Code, wApp.Body.String())
	}

	// 2. Reject subject user -> 200 and audit attributed to rejectUserID (not adminActorID).
	wRej := httptest.NewRecorder()
	r.ServeHTTP(wRej, httptest.NewRequest(http.MethodPost, "/admin/users/"+rejectUserID+"/reject", nil))
	if wRej.Code != http.StatusOK {
		t.Fatalf("POST reject status=%d body=%s", wRej.Code, wRej.Body.String())
	}

	// 3. GET /admin/audit?user_id=<subject> must contain each subject's event with ActorID == adminActorID.
	assertAuditForSubject := func(subjectID, wantAction, wantStatus string) {
		t.Helper()
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/admin/audit?user_id="+subjectID, nil))
		if w.Code != http.StatusOK {
			t.Fatalf("GET /admin/audit?user_id=%s status=%d body=%s", subjectID, w.Code, w.Body.String())
		}
		var out struct {
			Entries []account.AuditEntry `json:"entries"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatalf("decode audit response: %v", err)
		}
		if len(out.Entries) != 1 {
			t.Fatalf("GET /admin/audit?user_id=%s expected 1 entry, got %d (%+v)", subjectID, len(out.Entries), out.Entries)
		}
		got := out.Entries[0]
		if got.UserID != subjectID || got.ActorID != adminActorID || got.Action != wantAction || got.Target != subjectID {
			t.Fatalf("unexpected audit entry for %s: %+v", subjectID, got)
		}
		if got.Metadata["status"] != wantStatus {
			t.Fatalf("expected metadata.status=%q for %s, got %+v", wantStatus, subjectID, got.Metadata)
		}
	}
	assertAuditForSubject(approveUserID, account.ActionUserApproved, "approved")
	assertAuditForSubject(rejectUserID, account.ActionUserRejected, "rejected")

	// 4. GET /admin/audit?user_id=<adminActorID> must NOT contain misattributed subject status events.
	wAdminAudit := httptest.NewRecorder()
	r.ServeHTTP(wAdminAudit, httptest.NewRequest(http.MethodGet, "/admin/audit?user_id="+adminActorID, nil))
	if wAdminAudit.Code != http.StatusOK {
		t.Fatalf("GET /admin/audit?user_id=%s status=%d body=%s", adminActorID, wAdminAudit.Code, wAdminAudit.Body.String())
	}
	var adminAuditOut struct {
		Entries []account.AuditEntry `json:"entries"`
	}
	if err := json.Unmarshal(wAdminAudit.Body.Bytes(), &adminAuditOut); err != nil {
		t.Fatalf("decode admin audit response: %v", err)
	}
	if len(adminAuditOut.Entries) != 0 {
		t.Fatalf("expected 0 audit entries under admin actor user_id, got %+v", adminAuditOut.Entries)
	}

	// 5. GDPR export for both subjects must include their approval/rejection audit event.
	assertExportHasAction := func(subjectID, wantAction string) {
		t.Helper()
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/admin/users/"+subjectID+"/export", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("GET /admin/users/%s/export status=%d body=%s", subjectID, w.Code, w.Body.String())
		}
		var exp userExport
		if err := json.Unmarshal(w.Body.Bytes(), &exp); err != nil {
			t.Fatalf("decode userExport: %v", err)
		}
		found := false
		for _, e := range exp.Audit {
			if e.Action == wantAction && e.UserID == subjectID && e.ActorID == adminActorID {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("GET /admin/users/%s/export missing action=%q for subject=%q actor=%q (got %+v)",
				subjectID, wantAction, subjectID, adminActorID, exp.Audit)
		}
	}
	assertExportHasAction(approveUserID, account.ActionUserApproved)
	assertExportHasAction(rejectUserID, account.ActionUserRejected)
}
