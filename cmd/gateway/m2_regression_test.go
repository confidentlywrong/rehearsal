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
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/gen_proto/gateway"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/hub"
	"github.com/GoogleCloudPlatform/race-condition/internal/localai"
	"github.com/GoogleCloudPlatform/race-condition/internal/memory"
	"github.com/GoogleCloudPlatform/race-condition/internal/session"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"google.golang.org/protobuf/proto"
	_ "modernc.org/sqlite"
)

type headerTokenResolver struct{}

func (headerTokenResolver) Resolve(r *http.Request) (account.UserContext, error) {
	uid := strings.TrimPrefix(strings.TrimSpace(r.Header.Get("Authorization")), "Bearer ")
	if uid == "" {
		return account.UserContext{}, account.ErrNoIdentity
	}
	return account.UserContext{
		UserID: uid,
		Email:  uid + "@example.com",
		Status: account.StatusApproved,
	}, nil
}

func TestM2_WSUpgrade_BothSessionAndForeignSimulationRejected(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	reg := session.NewInMemorySessionService()
	ctxA := account.WithContext(context.Background(), account.UserContext{UserID: "user-a"})
	ctxB := account.WithContext(context.Background(), account.UserContext{UserID: "user-b"})
	if err := reg.TrackSession(ctxA, "sess-A", "leadership_runner", "sim-A"); err != nil {
		t.Fatalf("track A: %v", err)
	}
	if err := reg.TrackSession(ctxB, "sess-B", "leadership_runner", "sim-B"); err != nil {
		t.Fatalf("track B: %v", err)
	}

	h := hub.NewHub()
	r := setupRouter(h, nil, nil, reg, "gw-ws-test", nil, nil)
	srv := httptest.NewServer(r)
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http") + "/ws?sessionId=sess-A&simulationId=sim-B"
	hdr := http.Header{}
	hdr.Set("Authorization", "Bearer user-a")
	conn, resp, err := websocket.DefaultDialer.Dial(wsURL, hdr)
	if conn != nil {
		_ = conn.Close()
	}
	if err == nil {
		t.Fatalf("expected handshake failure for foreign simulationId even with owned sessionId")
	}
	if resp == nil || resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expected 403 simulation_not_owned, got resp=%v err=%v", resp, err)
	}
}

func TestM2_WS_SubscribeSimulation_EnforcesOwnershipAndFailClosed(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	reg := session.NewInMemorySessionService()
	ctxA := account.WithContext(context.Background(), account.UserContext{UserID: "user-a"})
	ctxB := account.WithContext(context.Background(), account.UserContext{UserID: "user-b"})
	if err := reg.TrackSession(ctxA, "sess-A", "leadership_runner", "sim-A"); err != nil {
		t.Fatalf("track A: %v", err)
	}
	if err := reg.TrackSession(ctxB, "sess-B", "leadership_runner", "sim-B"); err != nil {
		t.Fatalf("track B: %v", err)
	}

	h := hub.NewHub()
	hubCtx, hubCancel := context.WithCancel(context.Background())
	defer hubCancel()
	go h.Run(hubCtx)

	r := setupRouter(h, nil, nil, reg, "gw-ws-sub", nil, nil)
	srv := httptest.NewServer(r)
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http") + "/ws?sessionId=sess-A"
	hdr := http.Header{}
	hdr.Set("Authorization", "Bearer user-a")
	conn, resp, err := websocket.DefaultDialer.Dial(wsURL, hdr)
	if err != nil {
		t.Fatalf("dial failed: %v resp=%v", err, resp)
	}
	defer conn.Close()

	// Subscribe to foreign sim-B -> expect explicit JSON error frame.
	if err := conn.WriteJSON(map[string]string{
		"type":          "subscribe_simulation",
		"simulation_id": "sim-B",
	}); err != nil {
		t.Fatalf("write foreign subscribe: %v", err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	_, msgBytes, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("read error frame: %v", err)
	}
	var errFrame map[string]any
	if err := json.Unmarshal(msgBytes, &errFrame); err != nil {
		t.Fatalf("decode error frame: %v (%s)", err, string(msgBytes))
	}
	if errFrame["status"] != "error" || errFrame["event"] != "forbidden" {
		t.Fatalf("expected forbidden error frame, got %s", string(msgBytes))
	}

	// Verify validator with nil reg fails closed.
	v := newSimulationOwnershipValidator(nil)
	owned, verr := v(context.Background(), "user-a", "sim-A")
	if owned || verr == nil {
		t.Fatalf("expected nil-registry validator to fail closed, got owned=%v err=%v", owned, verr)
	}

	// Subscribe to owned sim-A -> succeeds and receives sim-A remote frames (never sim-B).
	if err := conn.WriteJSON(map[string]string{
		"type":          "subscribe_simulation",
		"simulation_id": "sim-A",
	}); err != nil {
		t.Fatalf("write owned subscribe: %v", err)
	}
	time.Sleep(50 * time.Millisecond)
	h.HandleRemoteMessage(&gateway.Wrapper{
		SimulationId: "sim-B",
		Type:         "info",
		Event:        "sim_b_secret",
	})
	h.HandleRemoteMessage(&gateway.Wrapper{
		SimulationId: "sim-A",
		Type:         "info",
		Event:        "sim_tick",
	})

	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	msgType, okBytes, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("read sim-A broadcast: %v", err)
	}
	if msgType != websocket.BinaryMessage {
		t.Fatalf("expected binary protobuf frame, got type=%d body=%s", msgType, string(okBytes))
	}
	var gotWrap gateway.Wrapper
	if err := proto.Unmarshal(okBytes, &gotWrap); err != nil {
		t.Fatalf("unmarshal wrapper: %v", err)
	}
	if gotWrap.Event != "sim_tick" || gotWrap.SimulationId != "sim-A" {
		t.Fatalf("expected sim_tick for sim-A (never sim-B), got %+v", &gotWrap)
	}
}

func TestM2_EnvironmentReset_ChunkedMalformedJSONReturns400AndMaxBytesReturns413(t *testing.T) {
	reg := session.NewInMemorySessionService()
	if err := reg.TrackSession(context.Background(), "sess-keep", "planner", "sim-keep"); err != nil {
		t.Fatalf("track session: %v", err)
	}

	h := hub.NewHub()
	r := setupRouter(h, nil, nil, reg, "gw-reset-test", nil, nil)

	// Chunked malformed JSON must return 400 and NOT flush registry.
	reqBad := httptest.NewRequest(http.MethodPost, "/api/v1/environment/reset", strings.NewReader("{bad"))
	reqBad.ContentLength = -1
	reqBad.RemoteAddr = "127.0.0.1:1234"
	reqBad.Header.Set("Content-Type", "application/json")
	wBad := httptest.NewRecorder()
	r.ServeHTTP(wBad, reqBad)
	if wBad.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 on chunked malformed JSON, got %d body=%s", wBad.Code, wBad.Body.String())
	}
	if _, found, _ := reg.FindAgentType(context.Background(), "sess-keep"); !found {
		t.Fatalf("session was flushed despite malformed JSON")
	}

	// Chunked trailing garbage or concatenated JSON must return 400 and NOT flush registry.
	for _, badPayload := range []string{
		`{"targets":["sessions"]}garbage`,
		`{"targets":["sessions"]}{"targets":["queues"]}`,
	} {
		reqGarbage := httptest.NewRequest(http.MethodPost, "/api/v1/environment/reset", strings.NewReader(badPayload))
		reqGarbage.ContentLength = -1
		reqGarbage.RemoteAddr = "127.0.0.1:1234"
		reqGarbage.Header.Set("Content-Type", "application/json")
		wGarbage := httptest.NewRecorder()
		r.ServeHTTP(wGarbage, reqGarbage)
		if wGarbage.Code != http.StatusBadRequest {
			t.Fatalf("payload %q: expected 400 on trailing garbage, got %d body=%s", badPayload, wGarbage.Code, wGarbage.Body.String())
		}
		if _, found, _ := reg.FindAgentType(context.Background(), "sess-keep"); !found {
			t.Fatalf("payload %q: active session was flushed despite trailing garbage", badPayload)
		}
	}

	// Trailing whitespace after valid JSON still succeeds with 200.
	reqWS := httptest.NewRequest(http.MethodPost, "/api/v1/environment/reset", strings.NewReader("{\"targets\":[\"queues\"]}  \n\t "))
	reqWS.ContentLength = -1
	reqWS.RemoteAddr = "127.0.0.1:1234"
	reqWS.Header.Set("Content-Type", "application/json")
	wWS := httptest.NewRecorder()
	r.ServeHTTP(wWS, reqWS)
	if wWS.Code != http.StatusOK {
		t.Fatalf("valid JSON with trailing whitespace: expected 200, got %d body=%s", wWS.Code, wWS.Body.String())
	}

	// Trailing whitespace or second JSON value exceeding 1 MiB returns 413 without flushing.
	for _, bigTrail := range []string{
		`{"targets":["sessions"]}` + strings.Repeat(" ", (1<<20)+128),
		`{"targets":["sessions"]}"` + strings.Repeat("a", (1<<20)+128) + `"`,
	} {
		reqBigTrail := httptest.NewRequest(http.MethodPost, "/api/v1/environment/reset", strings.NewReader(bigTrail))
		reqBigTrail.ContentLength = -1
		reqBigTrail.RemoteAddr = "127.0.0.1:1234"
		reqBigTrail.Header.Set("Content-Type", "application/json")
		wBigTrail := httptest.NewRecorder()
		r.ServeHTTP(wBigTrail, reqBigTrail)
		if wBigTrail.Code != http.StatusRequestEntityTooLarge {
			t.Fatalf("oversized trailing payload: expected 413, got %d body=%s", wBigTrail.Code, wBigTrail.Body.String())
		}
		if _, found, _ := reg.FindAgentType(context.Background(), "sess-keep"); !found {
			t.Fatalf("session was flushed despite oversized trailing payload")
		}
	}

	// Oversized > 1 MiB bodies on /environment/reset, /sessions, /spawn return 413.
	oversized := `{"pad":"` + strings.Repeat("a", (1<<20)+128) + `"}`
	for _, path := range []string{"/api/v1/environment/reset", "/api/v1/sessions", "/api/v1/spawn"} {
		reqBig := httptest.NewRequest(http.MethodPost, path, strings.NewReader(oversized))
		reqBig.RemoteAddr = "127.0.0.1:1234"
		reqBig.Header.Set("Content-Type", "application/json")
		wBig := httptest.NewRecorder()
		r.ServeHTTP(wBig, reqBig)
		if wBig.Code != http.StatusRequestEntityTooLarge {
			t.Fatalf("%s oversized body: expected 413, got %d body=%s", path, wBig.Code, wBig.Body.String())
		}
	}

	// Empty body on /environment/reset still succeeds with 200.
	reqEmpty := httptest.NewRequest(http.MethodPost, "/api/v1/environment/reset", nil)
	reqEmpty.RemoteAddr = "127.0.0.1:1234"
	wEmpty := httptest.NewRecorder()
	r.ServeHTTP(wEmpty, reqEmpty)
	if wEmpty.Code != http.StatusOK {
		t.Fatalf("empty reset body: expected 200, got %d body=%s", wEmpty.Code, wEmpty.Body.String())
	}
}

func TestM2_ResolveStripeCreators_RequiresReturnBaseURL(t *testing.T) {
	acctStore := &account.Store{}
	priceMap := "price_123=pro"

	for _, badBase := range []string{"", "   ", "not-a-url", "ftp://example.com"} {
		ul, cc, pc := resolveBillingCreators(acctStore, "sk_test_123", priceMap, badBase)
		if ul == nil {
			t.Fatalf("expected non-nil userLookup when acctStore is non-nil")
		}
		if _, ok := cc.(*billing.StubCheckoutCreator); !ok {
			t.Fatalf("base=%q: expected StubCheckoutCreator, got %T", badBase, cc)
		}
		if _, ok := pc.(*billing.StubPortalCreator); !ok {
			t.Fatalf("base=%q: expected StubPortalCreator, got %T", badBase, pc)
		}
	}

	// Nil acctStore leaves userLookup nil so CheckoutHandler/PortalHandler return 503.
	ulNil, ccNil, pcNil := resolveBillingCreators(nil, "sk_test_123", priceMap, "https://app.example.com")
	if ulNil != nil {
		t.Fatalf("expected nil userLookup when acctStore is nil, got %T", ulNil)
	}
	if _, ok := ccNil.(*billing.StubCheckoutCreator); !ok {
		t.Fatalf("expected StubCheckoutCreator when acctStore is nil, got %T", ccNil)
	}
	if _, ok := pcNil.(*billing.StubPortalCreator); !ok {
		t.Fatalf("expected StubPortalCreator when acctStore is nil, got %T", pcNil)
	}

	// Valid STRIPE_RETURN_BASE_URL arms live creators.
	ulOK, ccOK, pcOK := resolveBillingCreators(acctStore, "sk_test_123", priceMap, "https://app.example.com")
	if ulOK == nil {
		t.Fatalf("expected non-nil userLookup")
	}
	if _, ok := ccOK.(*billing.StripeCheckoutCreator); !ok {
		t.Fatalf("expected StripeCheckoutCreator, got %T", ccOK)
	}
	if _, ok := pcOK.(*billing.StripePortalCreator); !ok {
		t.Fatalf("expected StripePortalCreator, got %T", pcOK)
	}
}

type fakeUsageForwarder struct {
	mu      sync.Mutex
	events  []billing.Event
	rollups int64
}

func (f *fakeUsageForwarder) InsertEvent(_ context.Context, e billing.Event) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events = append(f.events, e)
	return int64(len(f.events)), nil
}

func (f *fakeUsageForwarder) UpsertMonthlyRollup(_ context.Context, _ string, _ time.Time, addTokens, _ int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rollups += addTokens
	return nil
}

func (f *fakeUsageForwarder) CheckQuota(_ context.Context, userID string) (billing.QuotaState, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return billing.QuotaState{
		UserID:          userID,
		PlanTier:        string(account.PlanPro),
		TokensUsed: f.rollups,
	}, nil
}

func TestM2_SQLiteDrainer_UsageHandlerIntegration(t *testing.T) {
	gin.SetMode(gin.TestMode)
	dbPath := filepath.Join(t.TempDir(), "metering.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	defer db.Close()

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
		INSERT INTO metering_ledger (user_id, sim_id, agent_type, model, prompt_tokens, completion_tokens, total_tokens, cost_micros, timestamp_iso, drained)
		VALUES ('user-u1', 'sim-1', 'leadership_runner', 'gemini', 100, 50, 150, 10, '2026-11-18T09:00:00Z', 0);
	`)
	if err != nil {
		t.Fatalf("seed sqlite: %v", err)
	}

	fwd := &fakeUsageForwarder{}
	drainer := billing.NewSQLiteDrainer(dbPath, fwd)

	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), account.UserContext{UserID: "user-u1"}))
		c.Next()
	})
	r.GET("/api/v1/usage", billing.UsageHandler(fwd, drainer))

	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/usage", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", w.Code, w.Body.String())
	}
	if fwd.rollups != 150 {
		t.Fatalf("expected drained rollup tokens=150, got %d", fwd.rollups)
	}
	var drained int
	if err := db.QueryRow(`SELECT drained FROM metering_ledger WHERE id = 1`).Scan(&drained); err != nil {
		t.Fatalf("query drained: %v", err)
	}
	if drained != 1 {
		t.Fatalf("expected drained=1 in SQLite after UsageHandler call, got %d", drained)
	}
}

func TestM2_AiSettings_MalformedJSON_CorruptConfig_And_AutoStartErr(t *testing.T) {
	gin.SetMode(gin.TestMode)

	t.Run("malformed JSON returns 400 on /test and /start", func(t *testing.T) {
		mgr := localai.NewManager(filepath.Join(t.TempDir(), "ai.json"))
		defer func() { _ = mgr.Close() }()
		r := gin.New()
		mountAiSettingsRoutes(r, mgr)

		for _, path := range []string{"/api/v1/settings/ai/test", "/api/v1/settings/ai/start"} {
			req := httptest.NewRequest(http.MethodPost, path, strings.NewReader("{bad"))
			req.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()
			r.ServeHTTP(w, req)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("%s malformed JSON: expected 400, got %d body=%s", path, w.Code, w.Body.String())
			}
		}
	})

	t.Run("corrupt config returns 500", func(t *testing.T) {
		cfgPath := filepath.Join(t.TempDir(), "corrupt.json")
		if err := os.WriteFile(cfgPath, []byte("{corrupt"), 0o600); err != nil {
			t.Fatalf("write corrupt config: %v", err)
		}
		mgr := localai.NewManager(cfgPath)
		defer func() { _ = mgr.Close() }()
		r := gin.New()
		mountAiSettingsRoutes(r, mgr)

		req := httptest.NewRequest(http.MethodGet, "/api/v1/settings/ai", nil)
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusInternalServerError {
			t.Fatalf("expected 500 on corrupt config, got %d body=%s", w.Code, w.Body.String())
		}
	})

	t.Run("corrupt config with explicit endpoint fails closed on /test /start /pull with zero /api/pull calls", func(t *testing.T) {
		var tagsHits atomic.Int32
		var pullHits atomic.Int32
		mockOllama := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case "/api/tags":
				tagsHits.Add(1)
				_, _ = w.Write([]byte(`{"models":[{"name":"gemma2:9b"}]}`))
			case "/api/pull":
				pullHits.Add(1)
				w.Header().Set("Content-Type", "application/x-ndjson")
				_, _ = w.Write([]byte(`{"status":"success","completed":100,"total":100}` + "\n"))
			default:
				http.NotFound(w, r)
			}
		}))
		defer mockOllama.Close()

		cfgPath := filepath.Join(t.TempDir(), "corrupt_explicit.json")
		if err := os.WriteFile(cfgPath, []byte("{corrupt"), 0o600); err != nil {
			t.Fatalf("write corrupt config: %v", err)
		}
		mgr := localai.NewManager(cfgPath)
		defer func() { _ = mgr.Close() }()
		r := gin.New()
		mountAiSettingsRoutes(r, mgr)

		cases := []struct {
			method string
			path   string
			body   string
		}{
			{http.MethodGet, "/api/v1/settings/ai?endpoint=" + mockOllama.URL, ""},
			{http.MethodPost, "/api/v1/settings/ai/test", `{"endpoint":"` + mockOllama.URL + `"}`},
			{http.MethodPost, "/api/v1/settings/ai/test", `{"endpoint":"` + mockOllama.URL + `","auto_start":true}`},
			{http.MethodPost, "/api/v1/settings/ai/start", `{"endpoint":"` + mockOllama.URL + `"}`},
			{http.MethodPost, "/api/v1/settings/ai/pull", `{"model":"bundle","endpoint":"` + mockOllama.URL + `"}`},
			{http.MethodPost, "/api/v1/settings/ai/pull", `{"model":"gemma2:2b","endpoint":"` + mockOllama.URL + `"}`},
		}
		for _, tc := range cases {
			var bodyReader io.Reader
			if tc.body != "" {
				bodyReader = strings.NewReader(tc.body)
			}
			req := httptest.NewRequest(tc.method, tc.path, bodyReader)
			if tc.body != "" {
				req.Header.Set("Content-Type", "application/json")
			}
			w := httptest.NewRecorder()
			r.ServeHTTP(w, req)
			if w.Code != http.StatusInternalServerError {
				t.Fatalf("%s %s body=%s: expected 500, got %d resp=%s", tc.method, tc.path, tc.body, w.Code, w.Body.String())
			}
			var resp map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatalf("%s %s decode response: %v (%s)", tc.method, tc.path, err, w.Body.String())
			}
			if resp["error"] != "ai settings unavailable" {
				t.Fatalf("%s %s: expected error=\"ai settings unavailable\", got %v", tc.method, tc.path, resp)
			}
		}
		if got := tagsHits.Load(); got != 0 {
			t.Fatalf("expected 0 /api/tags calls when ai_settings.json is corrupt, got %d", got)
		}
		if got := pullHits.Load(); got != 0 {
			t.Fatalf("expected 0 /api/pull calls when ai_settings.json is corrupt, got %d", got)
		}
	})

	t.Run("auto_start error returns 503 instead of 200", func(t *testing.T) {
		// Use a closed httptest server port (ephemeral, never 11434) and missing ollama binary.
		srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
		closedURL := srv.URL
		srv.Close()

		t.Setenv("LOCALAI_OLLAMA_BIN", filepath.Join(t.TempDir(), "missing-ollama"))
		mgr := localai.NewManager(filepath.Join(t.TempDir(), "ai.json"))
		defer func() { _ = mgr.Close() }()

		r := gin.New()
		mountAiSettingsRoutes(r, mgr)

		payload := `{"endpoint":"` + closedURL + `","auto_start":true}`
		req := httptest.NewRequest(http.MethodPost, "/api/v1/settings/ai/test", strings.NewReader(payload))
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("expected 503 when EnsureDaemonRunning fails, got %d body=%s", w.Code, w.Body.String())
		}
		var body map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		if errStr, _ := body["error"].(string); errStr == "" {
			t.Fatalf("expected non-empty error in 503 body, got %v", body)
		}
	})

	t.Run("closeAiSettingsManager closes cleanly", func(t *testing.T) {
		aiSettingsMu.Lock()
		aiSettingsManager = localai.NewManager(filepath.Join(t.TempDir(), "global.json"))
		aiSettingsMu.Unlock()
		closeAiSettingsManager()
		aiSettingsMu.Lock()
		remaining := aiSettingsManager
		aiSettingsMu.Unlock()
		if remaining != nil {
			t.Fatalf("expected aiSettingsManager to be nil after close")
		}
	})
}

func TestM2_MeInitMalformedJSONReturns400_And_MeExportAuditErrReturns500(t *testing.T) {
	gin.SetMode(gin.TestMode)

	createCalled := false
	store := &fakeMeStoreR2{
		createUserFn: func(context.Context, string, string, string) (account.User, bool, error) {
			createCalled = true
			return account.User{}, false, nil
		},
		getUserFn: func(_ context.Context, id string) (account.User, error) {
			return account.User{ID: id, Email: "u@example.com"}, nil
		},
		listAuditFn: func(context.Context, string, string, int, int) ([]account.AuditEntry, error) {
			return nil, errors.New("audit query failed")
		},
	}

	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-m2"}))
		c.Next()
	})
	r.POST("/api/v1/me/init", initMeHandler(store))
	r.GET("/api/v1/me/export", exportMeHandler(store))

	for _, badInit := range []string{
		"{bad",
		`{"display_name":"A"}garbage`,
		`{"display_name":"A"}{"display_name":"B"}`,
	} {
		reqInit := httptest.NewRequest(http.MethodPost, "/api/v1/me/init", strings.NewReader(badInit))
		reqInit.Header.Set("Content-Type", "application/json")
		wInit := httptest.NewRecorder()
		r.ServeHTTP(wInit, reqInit)
		if wInit.Code != http.StatusBadRequest {
			t.Fatalf("/me/init payload %q: expected 400, got %d body=%s", badInit, wInit.Code, wInit.Body.String())
		}
		if createCalled {
			t.Fatalf("CreateUser must not be called on invalid payload %q", badInit)
		}
	}

	wExp := httptest.NewRecorder()
	r.ServeHTTP(wExp, httptest.NewRequest(http.MethodGet, "/api/v1/me/export", nil))
	if wExp.Code != http.StatusInternalServerError {
		t.Fatalf("/me/export ListAudit error: expected 500, got %d body=%s", wExp.Code, wExp.Body.String())
	}
}

type noopDocStorage struct{}

func (noopDocStorage) Save(_ context.Context, _, _ uuid.UUID, _ string, r io.Reader) (string, int64, string, error) {
	n, err := io.Copy(io.Discard, r)
	return "mem://doc", n, "sha", err
}
func (noopDocStorage) Open(context.Context, string) (io.ReadCloser, error) {
	return io.NopCloser(strings.NewReader("")), nil
}
func (noopDocStorage) Delete(context.Context, string) error { return nil }

func TestM2_MemoryUploadExceedingMaxBytesReturns413(t *testing.T) {
	gin.SetMode(gin.TestMode)
	memStore := newFakeMemStore()
	a, err := memStore.CreateAnalysis(context.Background(), memory.CreateAnalysisInput{
		OperatorID: "op-m2",
		OrgName:    "Acme",
		Title:      "Test",
	})
	if err != nil {
		t.Fatalf("CreateAnalysis: %v", err)
	}

	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), account.UserContext{
			UserID: "op-m2",
			Status: account.StatusApproved,
		}))
		c.Next()
	})
	mountMemoryRoutes(r, memStore, noopDocStorage{}, fakeEmbedderR2{}, 128)

	for _, fileFirst := range []bool{true, false} {
		var buf bytes.Buffer
		mw := multipart.NewWriter(&buf)
		if fileFirst {
			fw, _ := mw.CreateFormFile("file", "big.txt")
			_, _ = fw.Write(bytes.Repeat([]byte("X"), 512))
			_ = mw.WriteField("kind", "memo")
		} else {
			_ = mw.WriteField("kind", "memo")
			fw, _ := mw.CreateFormFile("file", "big.txt")
			_, _ = fw.Write(bytes.Repeat([]byte("X"), 512))
		}
		_ = mw.Close()

		req := httptest.NewRequest(http.MethodPost, "/api/v1/memory/analyses/"+a.ID.String()+"/documents", &buf)
		req.Header.Set("Content-Type", mw.FormDataContentType())
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusRequestEntityTooLarge {
			t.Fatalf("fileFirst=%v: expected 413, got %d body=%s", fileFirst, w.Code, w.Body.String())
		}
	}
}

func TestM2_TrustedProxiesAndDefaultGatewayPort(t *testing.T) {
	if config.DefaultGatewayPort != "9101" {
		t.Fatalf("expected DefaultGatewayPort=9101, got %q", config.DefaultGatewayPort)
	}
	t.Setenv("TRUSTED_PROXIES", "")
	h := hub.NewHub()
	r := setupRouter(h, nil, nil, nil, "gw-proxy", nil, nil)
	var clientIP string
	r.GET("/health-ip", func(c *gin.Context) {
		clientIP = c.ClientIP()
		c.Status(http.StatusOK)
	})

	req := httptest.NewRequest(http.MethodGet, "/health-ip", nil)
	req.RemoteAddr = "127.0.0.1:2222"
	req.Header.Set("X-Forwarded-For", "198.51.100.77")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if clientIP != "127.0.0.1" {
		t.Fatalf("expected spoofed X-Forwarded-For to be ignored when TRUSTED_PROXIES is unset, got %q", clientIP)
	}

	// Valid CIDR configures trusted proxies without error.
	t.Setenv("TRUSTED_PROXIES", "127.0.0.1/32")
	r2 := gin.New()
	if err := configureTrustedProxies(r2); err != nil {
		t.Fatalf("expected valid TRUSTED_PROXIES to succeed, got %v", err)
	}
	r2.GET("/health-ip", func(c *gin.Context) {
		clientIP = c.ClientIP()
		c.Status(http.StatusOK)
	})
	w2 := httptest.NewRecorder()
	r2.ServeHTTP(w2, req)
	if clientIP != "198.51.100.77" {
		t.Fatalf("expected trusted proxy X-Forwarded-For to be honored, got %q", clientIP)
	}
}

func TestM2_AllGatewayJSONEndpoints_RejectTrailingGarbage_And_AllowTrailingWhitespace(t *testing.T) {
	gin.SetMode(gin.TestMode)

	h := hub.NewHub()
	reg := session.NewInMemorySessionService()
	r := setupRouter(h, nil, nil, reg, "gw-strict-json", nil, nil)

	memStore := newFakeMemStore()
	a, err := memStore.CreateAnalysis(context.Background(), memory.CreateAnalysisInput{
		OperatorID: "op-strict",
		OrgName:    "Acme",
		Title:      "Test",
	})
	if err != nil {
		t.Fatalf("CreateAnalysis: %v", err)
	}
	mgr := localai.NewManager(filepath.Join(t.TempDir(), "ai.json"))
	defer func() { _ = mgr.Close() }()

	createCalls := 0
	meStore := &fakeMeStoreR2{
		getUserFn: func(context.Context, string) (account.User, error) {
			return account.User{}, account.ErrNotFound
		},
		createUserFn: func(_ context.Context, id, email, displayName string) (account.User, bool, error) {
			createCalls++
			return account.User{
				ID:          id,
				Email:       email,
				DisplayName: displayName,
				PlanTier:    account.PlanFree,
				Status:      account.StatusPending,
			}, true, nil
		},
	}

	rAuth := gin.New()
	rAuth.Use(func(c *gin.Context) {
		c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), account.UserContext{
			UserID: "op-strict",
			Email:  "op@example.com",
			Status: account.StatusApproved,
		}))
		c.Next()
	})
	rAuth.POST("/api/v1/me/init", initMeHandler(meStore))
	rAuth.PATCH("/api/v1/me/profile", updateProfileHandler(meStore))
	mountAiSettingsRoutes(rAuth, mgr)
	mountMemoryRoutes(rAuth, memStore, noopDocStorage{}, fakeEmbedderR2{}, 1<<20)

	// Gateway main router JSON endpoints reject trailing garbage.
	for _, tc := range []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodPost, "/api/v1/sessions", `{"agentType":"planner"}garbage`},
		{http.MethodPost, "/api/v1/spawn", `{"agents":[{"agentType":"planner","count":1}]}garbage`},
	} {
		req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
		req.RemoteAddr = "127.0.0.1:1234"
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("%s %s trailing garbage: expected 400, got %d body=%s", tc.method, tc.path, w.Code, w.Body.String())
		}
	}

	// Authenticated gateway sub-routes reject trailing garbage.
	for _, tc := range []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodPatch, "/api/v1/me/profile", `{"display_name":"Alice"}garbage`},
		{http.MethodPut, "/api/v1/settings/ai", `{"provider":"cloud"}garbage`},
		{http.MethodPost, "/api/v1/settings/ai/test", `{"endpoint":"http://127.0.0.1:19999"}garbage`},
		{http.MethodPost, "/api/v1/settings/ai/start", `{"endpoint":"http://127.0.0.1:19999"}garbage`},
		{http.MethodPost, "/api/v1/settings/ai/pull", `{"model":"gemma3:4b"}garbage`},
		{http.MethodPost, "/api/v1/memory/analyses", `{"org_name":"Acme","title":"T"}garbage`},
		{http.MethodPatch, "/api/v1/memory/analyses/" + a.ID.String(), `{"title":"Updated"}garbage`},
		{http.MethodPost, "/api/v1/memory/analyses/" + a.ID.String() + "/retrieve-passages", `{"query":"hello"}garbage`},
	} {
		req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		rAuth.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("%s %s trailing garbage: expected 400, got %d body=%s", tc.method, tc.path, w.Code, w.Body.String())
		}
	}

	// Valid JSON with trailing whitespace succeeds on /me/init (201).
	reqWS := httptest.NewRequest(http.MethodPost, "/api/v1/me/init", strings.NewReader("{\"display_name\":\"Alice\"} \n\t "))
	reqWS.Header.Set("Content-Type", "application/json")
	wWS := httptest.NewRecorder()
	rAuth.ServeHTTP(wWS, reqWS)
	if wWS.Code != http.StatusCreated || createCalls != 1 {
		t.Fatalf("/me/init valid JSON + whitespace: expected 201 and createCalls=1, got code=%d calls=%d body=%s", wWS.Code, createCalls, wWS.Body.String())
	}
}
