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
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/agent"
	"github.com/GoogleCloudPlatform/race-condition/internal/hub"
	"github.com/GoogleCloudPlatform/race-condition/internal/localai"
	"github.com/GoogleCloudPlatform/race-condition/internal/memory"
	"github.com/GoogleCloudPlatform/race-condition/internal/session"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

type fakeMeStoreR2 struct {
	getUserFn       func(ctx context.Context, id string) (account.User, error)
	createUserFn    func(ctx context.Context, id, email, displayName string) (account.User, bool, error)
	updateProfileFn func(ctx context.Context, userID string, displayName, timezone *string) error
	listAuditFn     func(ctx context.Context, userID, action string, limit, offset int) ([]account.AuditEntry, error)
}

func (f *fakeMeStoreR2) GetUser(ctx context.Context, id string) (account.User, error) {
	if f.getUserFn != nil {
		return f.getUserFn(ctx, id)
	}
	return account.User{}, account.ErrNotFound
}

func (f *fakeMeStoreR2) CreateUser(ctx context.Context, id, email, displayName string) (account.User, bool, error) {
	if f.createUserFn != nil {
		return f.createUserFn(ctx, id, email, displayName)
	}
	return account.User{ID: id, Email: email, DisplayName: displayName, PlanTier: account.PlanFree, Status: account.StatusPending}, true, nil
}

func (f *fakeMeStoreR2) UpdateUserProfile(ctx context.Context, userID string, displayName, timezone *string) error {
	if f.updateProfileFn != nil {
		return f.updateProfileFn(ctx, userID, displayName, timezone)
	}
	return nil
}

func (f *fakeMeStoreR2) SoftDeleteUser(context.Context, string) error { return nil }
func (f *fakeMeStoreR2) RevokeTokens(context.Context, string) error   { return nil }

func (f *fakeMeStoreR2) ListAudit(ctx context.Context, userID, action string, limit, offset int) ([]account.AuditEntry, error) {
	if f.listAuditFn != nil {
		return f.listAuditFn(ctx, userID, action, limit, offset)
	}
	return nil, nil
}

func TestR2_GatewayNilCatalogAndRegistry503(t *testing.T) {
	h := hub.NewHub()
	r := setupRouter(h, nil, nil, nil, "gw-r2", nil, nil)

	t.Run("GET /api/v1/agent-types returns 503 when catalog is nil", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/agent-types", nil)
		req.RemoteAddr = "127.0.0.1:4321"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("expected 503 for nil catalog, got %d (%s)", w.Code, w.Body.String())
		}
	})

	t.Run("POST /api/v1/spawn returns 503 when reg is nil", func(t *testing.T) {
		payload := `{"agents":[{"agentType":"runner_a","count":1}],"simulation_id":"sim-r2"}`
		req := httptest.NewRequest(http.MethodPost, "/api/v1/spawn", strings.NewReader(payload))
		req.Header.Set("Content-Type", "application/json")
		req.RemoteAddr = "127.0.0.1:4321"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("expected 503 for nil session registry, got %d (%s)", w.Code, w.Body.String())
		}
	})
}

func TestR2_SpawnOverflowAndMaxEntriesGuard(t *testing.T) {
	agentCardSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/.well-known/agent-card.json" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"name":"runner_a","url":"http://127.0.0.1:9999","version":"1.0"}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer agentCardSrv.Close()

	t.Setenv("MAX_RUNNERS", "4")
	cat := agent.NewCatalog([]string{agentCardSrv.URL})
	reg := session.NewInMemorySessionService()
	h := hub.NewHub()
	r := setupRouter(h, nil, cat, reg, "gw-r2-spawn", nil, nil)

	t.Run("Rejects >1000 agent entries with 400", func(t *testing.T) {
		var b strings.Builder
		b.WriteString(`{"agents":[`)
		for i := 0; i < 1001; i++ {
			if i > 0 {
				b.WriteByte(',')
			}
			b.WriteString(`{"agentType":"runner_a","count":1}`)
		}
		b.WriteString(`],"simulation_id":"sim-many"}`)

		req := httptest.NewRequest(http.MethodPost, "/api/v1/spawn", strings.NewReader(b.String()))
		req.Header.Set("Content-Type", "application/json")
		req.RemoteAddr = "127.0.0.1:4321"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("expected 400 for >1000 entries, got %d (%s)", w.Code, w.Body.String())
		}
	})

	t.Run("Clamps extreme int64 counts without overflow and caps total at MAX_RUNNERS", func(t *testing.T) {
		// Two counts of 5e18 previously overflowed signed 64-bit int to a negative
		// totalRequested, bypassing the MAX_RUNNERS cap completely.
		payload := `{"agents":[{"agentType":"runner_a","count":5000000000000000000},{"agentType":"runner_a","count":5000000000000000000}],"simulation_id":"sim-overflow"}`
		req := httptest.NewRequest(http.MethodPost, "/api/v1/spawn", strings.NewReader(payload))
		req.Header.Set("Content-Type", "application/json")
		req.RemoteAddr = "127.0.0.1:4321"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("expected 200 OK, got %d (%s)", w.Code, w.Body.String())
		}
		var resp struct {
			Sessions []json.RawMessage `json:"sessions"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("decode spawn response: %v", err)
		}
		if len(resp.Sessions) != 4 {
			t.Fatalf("expected MAX_RUNNERS=4 spawned sessions after clamped scaling, got len=%d", len(resp.Sessions))
		}
	})

	t.Run("tryStartDispatch blocks new dispatches once shuttingDown is set", func(t *testing.T) {
		shuttingDown.Store(true)
		defer shuttingDown.Store(false)
		if tryStartDispatch() {
			dispatchWG.Done()
			t.Fatal("expected tryStartDispatch to return false while shuttingDown is true")
		}
	})
}

func TestR2_InitMeChunkedAndSoftDeletedAndBodyCap(t *testing.T) {
	withUser := func(handler gin.HandlerFunc) *gin.Engine {
		r := gin.New()
		r.Use(func(c *gin.Context) {
			ctx := account.WithContext(c.Request.Context(), account.UserContext{
				UserID: "u-init-1",
				Email:  "init@example.com",
			})
			c.Request = c.Request.WithContext(ctx)
			c.Next()
		})
		r.POST("/api/v1/me/init", handler)
		return r
	}

	t.Run("Chunked body (ContentLength == -1) parses display_name and timezone", func(t *testing.T) {
		var gotDisplay, gotTZ string
		store := &fakeMeStoreR2{
			createUserFn: func(_ context.Context, id, email, displayName string) (account.User, bool, error) {
				gotDisplay = displayName
				return account.User{ID: id, Email: email, DisplayName: displayName, PlanTier: account.PlanFree, Status: account.StatusPending}, true, nil
			},
			updateProfileFn: func(_ context.Context, _ string, _ *string, timezone *string) error {
				if timezone != nil {
					gotTZ = *timezone
				}
				return nil
			},
		}
		r := withUser(initMeHandler(store))
		req := httptest.NewRequest(http.MethodPost, "/api/v1/me/init", strings.NewReader(`{"display_name":"Ada Lovelace","timezone":"UTC"}`))
		req.ContentLength = -1 // simulate HTTP/1.1 chunked or HTTP/2 stream without Content-Length
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)

		if w.Code != http.StatusCreated {
			t.Fatalf("expected 201 Created, got %d (%s)", w.Code, w.Body.String())
		}
		if gotDisplay != "Ada Lovelace" {
			t.Fatalf("expected chunked display_name 'Ada Lovelace', got %q", gotDisplay)
		}
		if gotTZ != "UTC" {
			t.Fatalf("expected chunked timezone 'UTC', got %q", gotTZ)
		}
	})

	t.Run("Soft-deleted account returns 410 Gone instead of 500", func(t *testing.T) {
		store := &fakeMeStoreR2{
			createUserFn: func(context.Context, string, string, string) (account.User, bool, error) {
				return account.User{}, false, account.ErrAccountDeleted
			},
		}
		r := withUser(initMeHandler(store))
		req := httptest.NewRequest(http.MethodPost, "/api/v1/me/init", strings.NewReader(`{}`))
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusGone {
			t.Fatalf("expected 410 Gone for soft-deleted user, got %d (%s)", w.Code, w.Body.String())
		}
	})

	t.Run("Oversized >1 MiB body returns 413", func(t *testing.T) {
		r := withUser(initMeHandler(&fakeMeStoreR2{}))
		huge := `{"display_name":"` + strings.Repeat("x", (1<<20)+128) + `"}`
		req := httptest.NewRequest(http.MethodPost, "/api/v1/me/init", strings.NewReader(huge))
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusRequestEntityTooLarge {
			t.Fatalf("expected 413 for >1 MiB /me/init body, got %d (%s)", w.Code, w.Body.String())
		}
	})
}

func TestR2_ExportMeEmptyAuditSerializesArray(t *testing.T) {
	store := &fakeMeStoreR2{
		getUserFn: func(_ context.Context, id string) (account.User, error) {
			return account.User{ID: id, Email: "u@example.com", PlanTier: account.PlanFree, Status: account.StatusApproved}, nil
		},
		listAuditFn: func(context.Context, string, string, int, int) ([]account.AuditEntry, error) {
			return nil, nil
		},
	}
	r := gin.New()
	r.Use(func(c *gin.Context) {
		ctx := account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-exp-1"})
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	})
	r.GET("/api/v1/me/export", exportMeHandler(store))

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/me/export", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 OK, got %d (%s)", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"audit":[]`) {
		t.Fatalf("expected empty audit slice to serialize as [], got: %s", w.Body.String())
	}
}

func TestR2_BodySizeCaps413(t *testing.T) {
	oversized := bytes.Repeat([]byte("a"), (1<<20)+256)
	jsonOversized := append(append([]byte(`{"pad":"`), oversized...), []byte(`"}`)...)

	// 1. AI settings routes
	mgr := localai.NewManager(filepath.Join(t.TempDir(), "ai_settings.json"))
	aiRouter := gin.New()
	mountAiSettingsRoutes(aiRouter, mgr)
	for _, tc := range []struct {
		method string
		path   string
	}{
		{http.MethodPut, "/api/v1/settings/ai"},
		{http.MethodPost, "/api/v1/settings/ai/test"},
		{http.MethodPost, "/api/v1/settings/ai/start"},
		{http.MethodPost, "/api/v1/settings/ai/pull"},
	} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest(tc.method, tc.path, bytes.NewReader(jsonOversized))
		req.Header.Set("Content-Type", "application/json")
		aiRouter.ServeHTTP(w, req)
		if w.Code != http.StatusRequestEntityTooLarge {
			t.Errorf("%s %s: expected 413, got %d (%s)", tc.method, tc.path, w.Code, w.Body.String())
		}
	}

	// 2. Profile PATCH route
	profRouter := gin.New()
	profRouter.Use(func(c *gin.Context) {
		ctx := account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-prof-1"})
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	})
	profRouter.PATCH("/api/v1/me/profile", updateProfileHandler(&fakeMeStoreR2{}))
	wProf := httptest.NewRecorder()
	reqProf := httptest.NewRequest(http.MethodPatch, "/api/v1/me/profile", bytes.NewReader(jsonOversized))
	reqProf.Header.Set("Content-Type", "application/json")
	profRouter.ServeHTTP(wProf, reqProf)
	if wProf.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("PATCH /api/v1/me/profile: expected 413, got %d (%s)", wProf.Code, wProf.Body.String())
	}

	// 3. Memory JSON routes (create, patch, retrieve)
	memStore := newFakeMemStore()
	analysis, err := memStore.CreateAnalysis(context.Background(), memory.CreateAnalysisInput{
		OperatorID: "op-r2",
		OrgName:    "Acme",
		Title:      "Analysis",
	})
	if err != nil {
		t.Fatalf("seed analysis: %v", err)
	}
	memRouter := gin.New()
	memRouter.Use(func(c *gin.Context) {
		ctx := account.WithContext(c.Request.Context(), account.UserContext{
			UserID: "op-r2",
			Status: account.StatusApproved,
		})
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	})
	mountMemoryRoutes(memRouter, memStore, nil, fakeEmbedderR2{}, 1<<20)

	for _, tc := range []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/api/v1/memory/analyses"},
		{http.MethodPatch, fmt.Sprintf("/api/v1/memory/analyses/%s", analysis.ID)},
		{http.MethodPost, fmt.Sprintf("/api/v1/memory/analyses/%s/retrieve-passages", analysis.ID)},
	} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest(tc.method, tc.path, bytes.NewReader(jsonOversized))
		req.Header.Set("Content-Type", "application/json")
		memRouter.ServeHTTP(w, req)
		if w.Code != http.StatusRequestEntityTooLarge {
			t.Errorf("%s %s: expected 413, got %d (%s)", tc.method, tc.path, w.Code, w.Body.String())
		}
	}
	_ = uuid.Nil
}

type fakeEmbedderR2 struct{}

func (fakeEmbedderR2) EmbedBatch(_ context.Context, texts []string) ([][]float32, error) {
	out := make([][]float32, len(texts))
	for i := range texts {
		out[i] = []float32{0.1, 0.2}
	}
	return out, nil
}

func (fakeEmbedderR2) EmbedText(context.Context, string) ([]float32, error) {
	return []float32{0.1, 0.2}, nil
}
