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
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/gen_proto/gateway"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/agent"
	"github.com/GoogleCloudPlatform/race-condition/internal/hub"
	"github.com/GoogleCloudPlatform/race-condition/internal/session"
	"github.com/alicebob/miniredis/v2"
	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"google.golang.org/protobuf/proto"
)

func readWSJSONErrorFrame(t *testing.T, conn *websocket.Conn) map[string]any {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	mt, payload, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("expected JSON error frame, read failed: %v", err)
	}
	if mt != websocket.TextMessage {
		t.Fatalf("expected TextMessage error frame, got type=%d", mt)
	}
	var out map[string]any
	if err := json.Unmarshal(payload, &out); err != nil {
		t.Fatalf("unmarshal error frame %q: %v", string(payload), err)
	}
	return out
}

func assertNoWSMessage(t *testing.T, conn *websocket.Conn, wait time.Duration) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(wait))
	mt, payload, err := conn.ReadMessage()
	if err == nil {
		t.Fatalf("expected no WS message, got type=%d payload=%q", mt, string(payload))
	}
}

func TestT5_WSBinaryBroadcast_BlocksCrossTenantAndGlobalInjection(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5ws")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	reg := session.NewRedisSessionRegistry(rdb, "t5ws", time.Hour)
	ctxAlice := account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"})
	ctxBob := account.WithContext(context.Background(), account.UserContext{UserID: "user-bob"})
	if err := reg.TrackSession(ctxAlice, "sess-alice", "planner", "sim-alice"); err != nil {
		t.Fatalf("track alice: %v", err)
	}
	if err := reg.TrackSession(ctxBob, "sess-bob", "planner", "sim-bob"); err != nil {
		t.Fatalf("track bob: %v", err)
	}

	h := hub.NewHub(
		hub.WithSubscriptionStore(hub.NewRedisSubscriptionStore(rdb)),
		hub.WithSimulationOwnershipValidator(newSimulationOwnershipValidator(reg)),
		hub.WithRequireSimulationOwnership(true),
	)
	catalog := agent.NewCatalog(nil)
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-ws", h, catalog, reg)

	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	go h.Run(runCtx)
	go func() { _ = sb.Start(runCtx) }()

	// Wait briefly for Redis Pub/Sub subscription in sb.Start to become active.
	time.Sleep(100 * time.Millisecond)

	r := setupRouter(h, sb, catalog, reg, "gw-t5-ws", rdb, nil)
	srv := httptest.NewServer(r)
	defer srv.Close()
	wsBase := "ws" + strings.TrimPrefix(srv.URL, "http")

	dial := func(uid, query string) *websocket.Conn {
		hdr := http.Header{}
		hdr.Set("Authorization", "Bearer "+uid)
		conn, resp, err := websocket.DefaultDialer.Dial(wsBase+"/ws"+query, hdr)
		if resp != nil && resp.Body != nil {
			defer resp.Body.Close()
		}
		if err != nil {
			t.Fatalf("dial %s %s: %v", uid, query, err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		return conn
	}

	wsAlice := dial("user-alice", "?sessionId=sess-alice")
	wsAliceGlobal := dial("user-alice", "")
	wsBobSession := dial("user-bob", "?sessionId=sess-bob")
	wsBobGlobal := dial("user-bob", "")

	// Subscribe Bob's global observer to sim-bob so any sim-bob injection would reach it.
	if err := wsBobGlobal.WriteJSON(map[string]string{
		"type":          "subscribe_simulation",
		"simulation_id": "sim-bob",
	}); err != nil {
		t.Fatalf("bob subscribe sim-bob: %v", err)
	}
	time.Sleep(60 * time.Millisecond)

	// Subscribe to Alice's orchestration broadcast channel to prove failed targets never fall back to global dispatch.
	orchPubSub := rdb.Subscribe(context.Background(), account.Key(ctxAlice, "simulation", "broadcast"))
	defer orchPubSub.Close()
	_, _ = orchPubSub.ReceiveTimeout(context.Background(), time.Second)

	sendBroadcastWrapper := func(conn *websocket.Conn, simID string, dest []string, reqTargets []string, payload string) {
		breqBytes, err := proto.Marshal(&gateway.BroadcastRequest{
			Payload:          []byte(payload),
			TargetSessionIds: reqTargets,
		})
		if err != nil {
			t.Fatalf("marshal BroadcastRequest: %v", err)
		}
		wrapBytes, err := proto.Marshal(&gateway.Wrapper{
			Type:         "broadcast",
			Event:        "broadcast",
			SimulationId: simID,
			Destination:  dest,
			Payload:      breqBytes,
			Origin:       &gateway.Origin{Type: "client", Id: "tester-ui", SessionId: "tester-ui"},
		})
		if err != nil {
			t.Fatalf("marshal Wrapper: %v", err)
		}
		if err := conn.WriteMessage(websocket.BinaryMessage, wrapBytes); err != nil {
			t.Fatalf("write binary wrapper: %v", err)
		}
	}

	// 1. Cross-tenant SimulationId injection ("sim-bob") -> rejected; Bob never receives it.
	sendBroadcastWrapper(wsAlice, "sim-bob", []string{"sess-alice"}, []string{"sess-alice"}, `{"spoof":"sim-bob"}`)
	err1 := readWSJSONErrorFrame(t, wsAlice)
	if err1["event"] != "forbidden" {
		t.Fatalf("expected forbidden for foreign SimulationId, got %+v", err1)
	}

	// 2. Cross-tenant TargetSessionIds ("sess-bob") -> rejected; no global fallback dispatch.
	sendBroadcastWrapper(wsAlice, "", []string{"sess-bob"}, []string{"sess-bob"}, `{"spoof":"sess-bob"}`)
	err2 := readWSJSONErrorFrame(t, wsAlice)
	if err2["event"] != "forbidden" {
		t.Fatalf("expected forbidden for foreign TargetSessionIds, got %+v", err2)
	}

	// 3. Mixed owned + foreign TargetSessionIds -> fail closed.
	sendBroadcastWrapper(wsAlice, "", []string{"sess-alice"}, []string{"sess-alice", "sess-bob"}, `{"spoof":"mixed"}`)
	err3 := readWSJSONErrorFrame(t, wsAlice)
	if err3["event"] != "forbidden" {
		t.Fatalf("expected forbidden for mixed TargetSessionIds, got %+v", err3)
	}

	// 4. Owned TargetSessionIds with foreign Wrapper.Destination -> fail closed.
	sendBroadcastWrapper(wsAlice, "", []string{"sess-bob"}, []string{"sess-alice"}, `{"spoof":"dest"}`)
	err4 := readWSJSONErrorFrame(t, wsAlice)
	if err4["event"] != "forbidden" {
		t.Fatalf("expected forbidden for foreign Wrapper.Destination, got %+v", err4)
	}

	// 5. Empty TargetSessionIds global injection from global observer -> error goes ONLY to wsAliceGlobal.
	sendBroadcastWrapper(wsAliceGlobal, "", nil, nil, `{"spoof":"global"}`)
	err5 := readWSJSONErrorFrame(t, wsAliceGlobal)
	if err5["event"] != "bad_request" {
		t.Fatalf("expected bad_request for empty TargetSessionIds, got %+v", err5)
	}

	// Confirm NO spoofed message or error frame reached Bob or Alice's simulation:broadcast fallback.
	assertNoWSMessage(t, wsBobSession, 120*time.Millisecond)
	assertNoWSMessage(t, wsBobGlobal, 120*time.Millisecond)
	if msg, err := orchPubSub.ReceiveTimeout(context.Background(), 80*time.Millisecond); err == nil {
		t.Fatalf("unexpected fallback broadcast on simulation:broadcast channel: %+v", msg)
	}

	// 6. Legitimate owned broadcast from Alice succeeds and delivers ONLY to Alice's session.
	// Re-dial Bob's connections fresh because ReadMessage deadline timeout marks gorilla/websocket Conn failed.
	wsBobSession2 := dial("user-bob", "?sessionId=sess-bob")
	wsBobGlobal2 := dial("user-bob", "")
	if err := wsBobGlobal2.WriteJSON(map[string]string{
		"type":          "subscribe_simulation",
		"simulation_id": "sim-bob",
	}); err != nil {
		t.Fatalf("bob2 subscribe sim-bob: %v", err)
	}
	time.Sleep(60 * time.Millisecond)

	sendBroadcastWrapper(wsAlice, "sim-alice", []string{"sess-alice"}, []string{"sess-alice"}, `{"ok":"alice"}`)
	_ = wsAlice.SetReadDeadline(time.Now().Add(3 * time.Second))
	mt, rawEcho, err := wsAlice.ReadMessage()
	if err != nil {
		t.Fatalf("expected authorized broadcast echo for Alice: %v", err)
	}
	if mt != websocket.BinaryMessage {
		t.Fatalf("expected BinaryMessage for Alice broadcast, got type=%d body=%s", mt, string(rawEcho))
	}
	var gotWrap gateway.Wrapper
	if err := proto.Unmarshal(rawEcho, &gotWrap); err != nil {
		t.Fatalf("unmarshal Alice broadcast wrapper: %v", err)
	}
	if gotWrap.SimulationId != "sim-alice" {
		t.Fatalf("expected SimulationId=sim-alice, got %q", gotWrap.SimulationId)
	}

	assertNoWSMessage(t, wsBobSession2, 120*time.Millisecond)
	assertNoWSMessage(t, wsBobGlobal2, 120*time.Millisecond)
}

func TestT5_RateLimiter_NamespacedByRedisPrefix(t *testing.T) {
	gin.SetMode(gin.TestMode)
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	store := &fakeMeStoreR2{
		createUserFn: func(_ context.Context, id, email, displayName string) (account.User, bool, error) {
			return account.User{
				ID:          id,
				Email:       email,
				DisplayName: displayName,
				PlanTier:    account.PlanFree,
				Status:      account.StatusPending,
			}, true, nil
		},
	}

	sameUID := "u-shared-same-email"
	buildRouter := func(prefix string) *gin.Engine {
		r := gin.New()
		r.Use(func(c *gin.Context) {
			c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), account.UserContext{
				UserID: sameUID,
				Email:  "same@local.test",
			}))
			c.Next()
		})
		r.POST("/api/v1/me/init", newMeInitRateLimiter(rdb, prefix), initMeHandler(store))
		return r
	}

	// 4 distinct REDIS_PREFIX namespaces each get full capacity=3 for the identical UserID.
	prefixes := []string{"e2e_ns_a", "e2e_ns_b", "e2e_ns_c", "e2e_ns_d"}
	for _, prefix := range prefixes {
		r := buildRouter(prefix)
		for attempt := 0; attempt < 3; attempt++ {
			req := httptest.NewRequest(http.MethodPost, "/api/v1/me/init", strings.NewReader(`{}`))
			req.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()
			r.ServeHTTP(w, req)
			if w.Code != http.StatusCreated {
				t.Fatalf("prefix=%s attempt=%d: expected 201, got %d body=%s", prefix, attempt, w.Code, w.Body.String())
			}
		}
	}

	// A 4th call within the SAME namespace hits 429 rate_limited.
	req4 := httptest.NewRequest(http.MethodPost, "/api/v1/me/init", strings.NewReader(`{}`))
	req4.Header.Set("Content-Type", "application/json")
	w4 := httptest.NewRecorder()
	buildRouter("e2e_ns_a").ServeHTTP(w4, req4)
	if w4.Code != http.StatusTooManyRequests {
		t.Fatalf("expected 429 on 4th call in same namespace, got %d body=%s", w4.Code, w4.Body.String())
	}

	// Verify all rate-limit keys in Redis carry their REDIS_PREFIX namespace.
	for _, key := range mr.Keys() {
		if !strings.HasPrefix(key, "e2e_ns_") {
			t.Fatalf("found unprefixed rate-limit Redis key: %q", key)
		}
	}

	// Verify setupRouter's global/route limiters also isolate by REDIS_PREFIX.
	t.Setenv("RATELIMIT_CAPACITY", "1")
	t.Setenv("RATELIMIT_REFILL_PER_SEC", "0.01")
	t.Setenv("REDIS_PREFIX", "e2e_ns_router_1")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	r1 := setupRouter(hub.NewHub(), nil, nil, nil, "gw-rl-1", rdb, nil)
	t.Setenv("REDIS_PREFIX", "e2e_ns_router_2")
	r2 := setupRouter(hub.NewHub(), nil, nil, nil, "gw-rl-2", rdb, nil)

	doReq := func(r *gin.Engine) int {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/simulations", nil)
		req.Header.Set("Authorization", "Bearer "+sameUID)
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		return w.Code
	}
	if code := doReq(r1); code != http.StatusOK {
		t.Fatalf("r1 first request = %d, want 200", code)
	}
	if code := doReq(r1); code != http.StatusTooManyRequests {
		t.Fatalf("r1 second request = %d, want 429", code)
	}
	if code := doReq(r2); code != http.StatusOK {
		t.Fatalf("r2 first request in separate REDIS_PREFIX = %d, want 200", code)
	}
}

type t5UserContextResolver struct {
	byToken map[string]account.UserContext
}

func (r t5UserContextResolver) Resolve(req *http.Request) (account.UserContext, error) {
	tok := strings.TrimPrefix(req.Header.Get("Authorization"), "Bearer ")
	if uc, ok := r.byToken[tok]; ok {
		return uc, nil
	}
	return account.UserContext{}, account.ErrNoIdentity
}

func TestT5_SettingsAI_RequiresInitAndApprovalInSaaS(t *testing.T) {
	gin.SetMode(gin.TestMode)
	settingsPath := filepath.Join(t.TempDir(), "ai_settings.json")
	t.Setenv("AI_SETTINGS_PATH", settingsPath)
	t.Setenv("SAAS_MODE", "on")

	origResolver := accountResolverForRouter
	accountResolverForRouter = t5UserContextResolver{
		byToken: map[string]account.UserContext{
			"tok-init": {
				UserID:    "u-init",
				Email:     "init@example.com",
				NeedsInit: true,
			},
			"tok-pending": {
				UserID: "u-pending",
				Email:  "pending@example.com",
				Status: account.StatusPending,
			},
			"tok-rejected": {
				UserID: "u-rejected",
				Email:  "rejected@example.com",
				Status: account.StatusRejected,
			},
			"tok-approved": {
				UserID: "u-approved",
				Email:  "approved@example.com",
				Status: account.StatusApproved,
			},
			"tok-superadmin": {
				UserID:       "u-superadmin",
				Email:        "superadmin@example.com",
				Status:       account.StatusApproved,
				IsSuperadmin: true,
			},
		},
	}
	defer func() { accountResolverForRouter = origResolver }()

	r := setupRouter(hub.NewHub(), nil, nil, nil, "gw-t5-ai", nil, nil)

	aiRoutes := []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodGet, "/api/v1/settings/ai", ""},
		{http.MethodPut, "/api/v1/settings/ai", `{"provider":"local_gemma","endpoint":"http://127.0.0.1:19999","text_model":"gemma3:4b","embed_model":"embeddinggemma:latest"}`},
		{http.MethodPost, "/api/v1/settings/ai/test", `{}`},
		{http.MethodPost, "/api/v1/settings/ai/start", `{}`},
		{http.MethodPost, "/api/v1/settings/ai/pull", `{"model":"gemma3:4b"}`},
	}

	assertGate := func(tok string, wantStatus int, wantErrCode string) {
		t.Helper()
		for _, rt := range aiRoutes {
			var bodyReader *strings.Reader
			if rt.body != "" {
				bodyReader = strings.NewReader(rt.body)
			} else {
				bodyReader = strings.NewReader("")
			}
			req := httptest.NewRequest(rt.method, rt.path, bodyReader)
			req.Header.Set("Authorization", "Bearer "+tok)
			if rt.body != "" {
				req.Header.Set("Content-Type", "application/json")
			}
			w := httptest.NewRecorder()
			r.ServeHTTP(w, req)
			if w.Code != wantStatus || !strings.Contains(w.Body.String(), wantErrCode) {
				t.Fatalf("token=%s %s %s: got %d (%s), want %d (%s)",
					tok, rt.method, rt.path, w.Code, w.Body.String(), wantStatus, wantErrCode)
			}
		}
	}

	assertGate("tok-init", http.StatusConflict, "needs_init")
	assertGate("tok-pending", http.StatusForbidden, "account_pending")
	assertGate("tok-rejected", http.StatusForbidden, "account_rejected")

	if _, err := os.Stat(settingsPath); !os.IsNotExist(err) {
		t.Fatalf("uninitialized/pending/rejected requests must not create %s (err=%v)", settingsPath, err)
	}

	// Approved non-superadmin SaaS user succeeds on GET /api/v1/settings/ai,
	// and is rejected with 403 superadmin_required on mutating routes.
	reqGet := httptest.NewRequest(http.MethodGet, "/api/v1/settings/ai", nil)
	reqGet.Header.Set("Authorization", "Bearer tok-approved")
	wGet := httptest.NewRecorder()
	r.ServeHTTP(wGet, reqGet)
	if wGet.Code != http.StatusOK {
		t.Fatalf("approved user GET /api/v1/settings/ai: got %d (%s), want 200", wGet.Code, wGet.Body.String())
	}

	for _, rt := range aiRoutes[1:] {
		reqMut := httptest.NewRequest(rt.method, rt.path, strings.NewReader(rt.body))
		reqMut.Header.Set("Authorization", "Bearer tok-approved")
		reqMut.Header.Set("Content-Type", "application/json")
		wMut := httptest.NewRecorder()
		r.ServeHTTP(wMut, reqMut)
		if wMut.Code != http.StatusForbidden || !strings.Contains(wMut.Body.String(), "superadmin_required") {
			t.Fatalf("non-superadmin %s %s: got %d (%s), want 403 (superadmin_required)",
				rt.method, rt.path, wMut.Code, wMut.Body.String())
		}
	}

	// Approved superadmin SaaS user succeeds on PUT /api/v1/settings/ai.
	reqPut := httptest.NewRequest(http.MethodPut, "/api/v1/settings/ai", strings.NewReader(`{"provider":"cloud_gemini"}`))
	reqPut.Header.Set("Authorization", "Bearer tok-superadmin")
	reqPut.Header.Set("Content-Type", "application/json")
	wPut := httptest.NewRecorder()
	r.ServeHTTP(wPut, reqPut)
	if wPut.Code != http.StatusOK {
		t.Fatalf("superadmin PUT /api/v1/settings/ai: got %d (%s), want 200", wPut.Code, wPut.Body.String())
	}

	// LocalResolver (local dev mode) continues to return 200.
	t.Setenv("SAAS_MODE", "off")
	accountResolverForRouter = account.NewLocalResolver()
	rLocal := setupRouter(hub.NewHub(), nil, nil, nil, "gw-t5-ai-local", nil, nil)
	reqLocal := httptest.NewRequest(http.MethodGet, "/api/v1/settings/ai", nil)
	reqLocal.RemoteAddr = "127.0.0.1:1234"
	wLocal := httptest.NewRecorder()
	rLocal.ServeHTTP(wLocal, reqLocal)
	if wLocal.Code != http.StatusOK {
		t.Fatalf("LocalResolver GET /api/v1/settings/ai: got %d (%s), want 200", wLocal.Code, wLocal.Body.String())
	}
}

func TestT5_WSBinaryA2UIAction_EmptySessionIdAndNilRegistryErrors(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	h := hub.NewHub()
	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	go h.Run(runCtx)

	r := setupRouter(h, nil, nil, nil, "gw-t5-a2ui", nil, nil)
	srv := httptest.NewServer(r)
	defer srv.Close()
	wsBase := "ws" + strings.TrimPrefix(srv.URL, "http")

	hdr := http.Header{}
	hdr.Set("Authorization", "Bearer user-alice")
	conn, resp, err := websocket.DefaultDialer.Dial(wsBase+"/ws", hdr)
	if resp != nil && resp.Body != nil {
		defer resp.Body.Close()
	}
	if err != nil {
		t.Fatalf("dial ws: %v", err)
	}
	defer conn.Close()

	sendAction := func(sessionID string) map[string]any {
		actBytes, mErr := proto.Marshal(&gateway.A2UIAction{
			ActionName: "approve_plan",
			SessionId:  sessionID,
		})
		if mErr != nil {
			t.Fatalf("marshal A2UIAction: %v", mErr)
		}
		wrapBytes, wErr := proto.Marshal(&gateway.Wrapper{
			Type:    "a2ui_action",
			Event:   "a2ui_action",
			Payload: actBytes,
		})
		if wErr != nil {
			t.Fatalf("marshal Wrapper: %v", wErr)
		}
		if wErr := conn.WriteMessage(websocket.BinaryMessage, wrapBytes); wErr != nil {
			t.Fatalf("write binary a2ui_action: %v", wErr)
		}
		return readWSJSONErrorFrame(t, conn)
	}

	emptyErr := sendAction("   ")
	emptyData, _ := emptyErr["data"].(map[string]any)
	if emptyErr["event"] != "bad_request" || emptyData["error"] != "missing_session_id" {
		t.Fatalf("expected bad_request/missing_session_id for empty SessionId, got %+v", emptyErr)
	}

	nilRegErr := sendAction("sess-alice")
	nilRegData, _ := nilRegErr["data"].(map[string]any)
	if nilRegErr["event"] != "service_unavailable" || nilRegData["error"] != "registry_unavailable" {
		t.Fatalf("expected service_unavailable/registry_unavailable when reg==nil, got %+v", nilRegErr)
	}
}

type t5CountingDrainer struct {
	calls atomic.Int64
}

func (d *t5CountingDrainer) Drain(_ context.Context) (int, error) {
	d.calls.Add(1)
	return 1, nil
}

func TestT5_EnvironmentReset_SaaSTenantIsolation(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5reset")

	origResolver := accountResolverForRouter
	accountResolverForRouter = t5UserContextResolver{
		byToken: map[string]account.UserContext{
			"tok-alice": {
				UserID: "user-alice",
				Email:  "alice@example.com",
				Status: account.StatusApproved,
			},
			"tok-bob": {
				UserID: "user-bob",
				Email:  "bob@example.com",
				Status: account.StatusApproved,
			},
			"tok-admin": {
				UserID:       "user-admin",
				Email:        "admin@example.com",
				IsSuperadmin: true,
				Status:       account.StatusApproved,
			},
		},
	}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	var agentPokeCalls atomic.Int64
	mockAgent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/.well-known/agent-card.json" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"name":"leadership_planner","url":"` + "http://" + r.Host + `","version":"1.0.0"}`))
			return
		}
		if r.URL.Path == "/orchestration" {
			agentPokeCalls.Add(1)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer mockAgent.Close()
	t.Setenv("AGENT_URLS", mockAgent.URL)

	reg := session.NewRedisSessionRegistry(rdb, "t5reset", time.Hour)
	h := hub.NewHub(
		hub.WithSubscriptionStore(hub.NewRedisSubscriptionStore(rdb)),
		hub.WithSimulationOwnershipValidator(newSimulationOwnershipValidator(reg)),
		hub.WithRequireSimulationOwnership(true),
	)
	catalog := agent.NewCatalog([]string{mockAgent.URL})
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-reset", h, catalog, reg)
	drainer := &t5CountingDrainer{}

	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	go h.Run(runCtx)
	go func() { _ = sb.Start(runCtx) }()
	time.Sleep(100 * time.Millisecond)

	r := setupRouter(h, sb, catalog, reg, "gw-t5-reset", rdb, drainer)
	srv := httptest.NewServer(r)
	defer srv.Close()
	wsBase := "ws" + strings.TrimPrefix(srv.URL, "http")

	postJSON := func(tok, path, body string) *http.Response {
		req, err := http.NewRequest(http.MethodPost, srv.URL+path, strings.NewReader(body))
		if err != nil {
			t.Fatalf("new request: %v", err)
		}
		req.Header.Set("Authorization", "Bearer "+tok)
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatalf("POST %s: %v", path, err)
		}
		return resp
	}

	// Both Alice and Bob spawn sessions via POST /api/v1/sessions.
	respAlice := postJSON("tok-alice", "/api/v1/sessions", `{"agentType":"leadership_planner","simulation_id":"sim-alice"}`)
	defer respAlice.Body.Close()
	if respAlice.StatusCode != http.StatusCreated {
		t.Fatalf("alice spawn got %d", respAlice.StatusCode)
	}
	var aliceSpawn struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.NewDecoder(respAlice.Body).Decode(&aliceSpawn); err != nil {
		t.Fatalf("decode alice spawn: %v", err)
	}

	respBob := postJSON("tok-bob", "/api/v1/sessions", `{"agentType":"leadership_planner","simulation_id":"sim-bob"}`)
	defer respBob.Body.Close()
	if respBob.StatusCode != http.StatusCreated {
		t.Fatalf("bob spawn got %d", respBob.StatusCode)
	}
	var bobSpawn struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.NewDecoder(respBob.Body).Decode(&bobSpawn); err != nil {
		t.Fatalf("decode bob spawn: %v", err)
	}

	// Seed session_map entries for both tenants plus a prefix-collision decoy
	// and an unscoped local key.
	if err := rdb.Set(context.Background(), "session_map:user-alice:sim-alice", aliceSpawn.SessionID, time.Hour).Err(); err != nil {
		t.Fatalf("seed alice map: %v", err)
	}
	if err := rdb.Set(context.Background(), "session_map:user-bob:sim-bob", bobSpawn.SessionID, time.Hour).Err(); err != nil {
		t.Fatalf("seed bob map: %v", err)
	}
	if err := rdb.Set(context.Background(), "session_map:user-alice2:ctx-alice2", "v-alice2", time.Hour).Err(); err != nil {
		t.Fatalf("seed alice2 map: %v", err)
	}
	if err := rdb.Set(context.Background(), "session_map:unscoped-ctx", "v-unscoped", time.Hour).Err(); err != nil {
		t.Fatalf("seed unscoped map: %v", err)
	}

	dialWS := func(tok, query string) *websocket.Conn {
		hdr := http.Header{}
		hdr.Set("Authorization", "Bearer "+tok)
		conn, resp, err := websocket.DefaultDialer.Dial(wsBase+"/ws"+query, hdr)
		if resp != nil && resp.Body != nil {
			defer resp.Body.Close()
		}
		if err != nil {
			t.Fatalf("dial %s %s: %v", tok, query, err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		return conn
	}

	wsAliceGlobal := dialWS("tok-alice", "")
	wsAliceSession := dialWS("tok-alice", "?sessionId="+aliceSpawn.SessionID)
	wsBobGlobal := dialWS("tok-bob", "")
	wsBobSession := dialWS("tok-bob", "?sessionId="+bobSpawn.SessionID)

	// Subscribe to Bob's and Alice's simulation:broadcast channels.
	aliceSimSub := rdb.Subscribe(context.Background(), "u:user-alice:simulation:broadcast")
	defer aliceSimSub.Close()
	_, _ = aliceSimSub.ReceiveTimeout(context.Background(), time.Second)

	bobSimSub := rdb.Subscribe(context.Background(), "u:user-bob:simulation:broadcast")
	defer bobSimSub.Close()
	_, _ = bobSimSub.ReceiveTimeout(context.Background(), time.Second)

	// 1. Non-superadmin Alice explicitly requesting pubsub drain must receive 403 superadmin_required.
	respPubSubDenied := postJSON("tok-alice", "/api/v1/environment/reset", `{"targets":["pubsub"]}`)
	defer respPubSubDenied.Body.Close()
	if respPubSubDenied.StatusCode != http.StatusForbidden {
		t.Fatalf("non-superadmin pubsub reset got %d, want 403", respPubSubDenied.StatusCode)
	}
	if drainer.calls.Load() != 0 {
		t.Fatalf("expected 0 drainer calls after 403, got %d", drainer.calls.Load())
	}

	// 2. Non-superadmin Alice calls POST /api/v1/environment/reset with {}.
	respReset := postJSON("tok-alice", "/api/v1/environment/reset", `{}`)
	defer respReset.Body.Close()
	if respReset.StatusCode != http.StatusOK {
		t.Fatalf("alice reset got %d", respReset.StatusCode)
	}
	var resetBody struct {
		Status  string `json:"status"`
		Results map[string]struct {
			Flushed bool `json:"flushed"`
			Count   int  `json:"count"`
		} `json:"results"`
	}
	if err := json.NewDecoder(respReset.Body).Decode(&resetBody); err != nil {
		t.Fatalf("decode reset response: %v", err)
	}
	if resetBody.Status != "reset" {
		t.Fatalf("expected status=reset, got %q", resetBody.Status)
	}
	if !resetBody.Results["sessions"].Flushed || resetBody.Results["sessions"].Count != 1 {
		t.Fatalf("expected alice sessions flushed count=1, got %+v", resetBody.Results["sessions"])
	}
	if !resetBody.Results["queues"].Flushed || resetBody.Results["queues"].Count != 1 {
		t.Fatalf("expected alice queues flushed count=1, got %+v", resetBody.Results["queues"])
	}
	if !resetBody.Results["maps"].Flushed || resetBody.Results["maps"].Count != 1 {
		t.Fatalf("expected alice maps flushed count=1, got %+v", resetBody.Results["maps"])
	}
	if resetBody.Results["pubsub"].Flushed || drainer.calls.Load() != 0 {
		t.Fatalf("expected global pubsub drain skipped for non-superadmin, got %+v calls=%d", resetBody.Results["pubsub"], drainer.calls.Load())
	}

	// Verify Alice's queues/sessions/maps are gone while Bob's survive intact.
	ctxAlice := account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"})
	ctxBob := account.WithContext(context.Background(), account.UserContext{UserID: "user-bob"})
	aliceQueue := hub.SpawnQueueNameForContext(ctxAlice, "leadership_planner", aliceSpawn.SessionID, hub.DefaultSpawnShards)
	bobQueue := hub.SpawnQueueNameForContext(ctxBob, "leadership_planner", bobSpawn.SessionID, hub.DefaultSpawnShards)

	if exists, _ := rdb.Exists(context.Background(), aliceQueue).Result(); exists != 0 {
		t.Fatalf("expected alice spawn queue %q to be deleted", aliceQueue)
	}
	if exists, _ := rdb.Exists(context.Background(), bobQueue).Result(); exists != 1 {
		t.Fatalf("expected bob spawn queue %q to survive Alice's reset", bobQueue)
	}
	if _, found, err := reg.FindAgentType(ctxBob, bobSpawn.SessionID); err != nil || !found {
		t.Fatalf("expected bob session %q to survive Alice's reset (found=%v err=%v)", bobSpawn.SessionID, found, err)
	}
	if exists, _ := rdb.Exists(context.Background(), "session_map:user-bob:sim-bob").Result(); exists != 1 {
		t.Fatalf("expected bob session_map to survive Alice's reset")
	}
	if exists, _ := rdb.Exists(context.Background(), "session_map:user-alice2:ctx-alice2").Result(); exists != 1 {
		t.Fatalf("expected similar-prefix user-alice2 session_map to survive Alice's reset")
	}
	if exists, _ := rdb.Exists(context.Background(), "session_map:unscoped-ctx").Result(); exists != 1 {
		t.Fatalf("expected unscoped session_map to survive Alice's SAAS_MODE=on reset")
	}

	// Verify Alice's WS connections receive environment_reset, while Bob's receive ZERO frames.
	for name, ws := range map[string]*websocket.Conn{
		"wsAliceGlobal":  wsAliceGlobal,
		"wsAliceSession": wsAliceSession,
	} {
		_ = ws.SetReadDeadline(time.Now().Add(3 * time.Second))
		mt, raw, err := ws.ReadMessage()
		if err != nil {
			t.Fatalf("%s expected environment_reset frame: %v", name, err)
		}
		if mt != websocket.BinaryMessage {
			t.Fatalf("%s expected BinaryMessage, got %d", name, mt)
		}
		var w gateway.Wrapper
		if err := proto.Unmarshal(raw, &w); err != nil {
			t.Fatalf("%s unmarshal wrapper: %v", name, err)
		}
		if w.Type != "environment_reset" || w.Event != "environment_reset" {
			t.Fatalf("%s expected environment_reset wrapper, got type=%q event=%q", name, w.Type, w.Event)
		}
	}
	assertNoWSMessage(t, wsBobGlobal, 150*time.Millisecond)
	assertNoWSMessage(t, wsBobSession, 150*time.Millisecond)

	// Verify tenant-scoped simulation:broadcast fired ONLY for Alice, and global HTTP agent pokes stayed 0.
	rawAliceMsg, err := aliceSimSub.ReceiveTimeout(context.Background(), time.Second)
	if err != nil {
		t.Fatalf("expected environment_reset on Alice's simulation:broadcast: %v", err)
	}
	aliceRedisMsg, ok := rawAliceMsg.(*redis.Message)
	if !ok || !strings.Contains(aliceRedisMsg.Payload, "environment_reset") {
		t.Fatalf("expected *redis.Message environment_reset on Alice's simulation:broadcast, got %#v", rawAliceMsg)
	}
	if msg, err := bobSimSub.ReceiveTimeout(context.Background(), 100*time.Millisecond); err == nil {
		t.Fatalf("unexpected message on Bob's simulation:broadcast: %+v", msg)
	}
	if agentPokeCalls.Load() != 0 {
		t.Fatalf("expected 0 global HTTP /orchestration pokes in SAAS_MODE=on, got %d", agentPokeCalls.Load())
	}

	// 3. Superadmin in SAAS_MODE=on can drain pubsub without touching Bob's queue or notifying Bob's WS.
	wsBobGlobal2 := dialWS("tok-bob", "")
	respAdmin := postJSON("tok-admin", "/api/v1/environment/reset", `{"targets":["pubsub","queues"]}`)
	defer respAdmin.Body.Close()
	if respAdmin.StatusCode != http.StatusOK {
		t.Fatalf("admin reset got %d", respAdmin.StatusCode)
	}
	if drainer.calls.Load() != 1 {
		t.Fatalf("expected superadmin pubsub drain call=1, got %d", drainer.calls.Load())
	}
	if exists, _ := rdb.Exists(context.Background(), bobQueue).Result(); exists != 1 {
		t.Fatalf("expected bob spawn queue %q to survive admin tenant-scoped reset", bobQueue)
	}
	assertNoWSMessage(t, wsBobGlobal2, 120*time.Millisecond)
}

func TestT5_WSBinarySessionNotOwned_GlobalObserverIncludesRejectedSession(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5globalerr")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	reg := session.NewRedisSessionRegistry(rdb, "t5globalerr", time.Hour)
	ctxAlice := account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"})
	ctxBob := account.WithContext(context.Background(), account.UserContext{UserID: "user-bob"})
	if err := reg.TrackSession(ctxAlice, "sess-alice-owned", "planner", "sim-alice"); err != nil {
		t.Fatalf("track alice owned: %v", err)
	}
	if err := reg.TrackSession(ctxBob, "sess-bob-other", "planner", "sim-bob"); err != nil {
		t.Fatalf("track bob: %v", err)
	}

	h := hub.NewHub(
		hub.WithSubscriptionStore(hub.NewRedisSubscriptionStore(rdb)),
		hub.WithSimulationOwnershipValidator(newSimulationOwnershipValidator(reg)),
		hub.WithRequireSimulationOwnership(true),
	)
	catalog := agent.NewCatalog(nil)
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-global-err", h, catalog, reg)

	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	go h.Run(runCtx)
	go func() { _ = sb.Start(runCtx) }()
	time.Sleep(80 * time.Millisecond)

	r := setupRouter(h, sb, catalog, reg, "gw-t5-global-err", rdb, nil)
	srv := httptest.NewServer(r)
	defer srv.Close()
	wsBase := "ws" + strings.TrimPrefix(srv.URL, "http")

	dialGlobal := func(uid string) *websocket.Conn {
		hdr := http.Header{}
		hdr.Set("Authorization", "Bearer "+uid)
		conn, resp, err := websocket.DefaultDialer.Dial(wsBase+"/ws", hdr)
		if resp != nil && resp.Body != nil {
			defer resp.Body.Close()
		}
		if err != nil {
			t.Fatalf("dial global %s: %v", uid, err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		return conn
	}

	wsAliceGlobal := dialGlobal("user-alice")
	wsAliceGlobalSibling := dialGlobal("user-alice")
	wsBobGlobal := dialGlobal("user-bob")

	assertRejectedSessionFrame := func(frame map[string]any, wantSID string) {
		t.Helper()
		if frame["status"] != "error" || frame["type"] != "error" || frame["event"] != "forbidden" {
			t.Fatalf("expected forbidden error frame, got %+v", frame)
		}
		destRaw, ok := frame["destination"].([]any)
		if !ok || len(destRaw) != 1 || destRaw[0] != wantSID {
			t.Fatalf("expected destination=[%q] for global observer error frame, got %#v", wantSID, frame["destination"])
		}
		dataMap, ok := frame["data"].(map[string]any)
		if !ok {
			t.Fatalf("expected data map in error frame, got %#v", frame["data"])
		}
		if dataMap["error"] != "session_not_owned" {
			t.Fatalf("expected error=session_not_owned, got %#v", dataMap["error"])
		}
		if dataMap["session_id"] != wantSID {
			t.Fatalf("expected data.session_id=%q, got %#v", wantSID, dataMap["session_id"])
		}
	}

	sendBroadcast := func(conn *websocket.Conn, dest, reqTargets []string, wrapperSessionID string) {
		breqBytes, err := proto.Marshal(&gateway.BroadcastRequest{
			Payload:          []byte(`{"text":"hello"}`),
			TargetSessionIds: reqTargets,
		})
		if err != nil {
			t.Fatalf("marshal BroadcastRequest: %v", err)
		}
		wrapBytes, err := proto.Marshal(&gateway.Wrapper{
			Type:        "broadcast",
			Event:       "broadcast",
			Destination: dest,
			SessionId:   wrapperSessionID,
			Payload:     breqBytes,
			Origin:      &gateway.Origin{Type: "client", Id: "tester-ui", SessionId: "tester-ui"},
		})
		if err != nil {
			t.Fatalf("marshal Wrapper: %v", err)
		}
		if err := conn.WriteMessage(websocket.BinaryMessage, wrapBytes); err != nil {
			t.Fatalf("write broadcast wrapper: %v", err)
		}
	}

	// 1. Expired/unowned TargetSessionIds on global /ws -> rejected SID in destination & data.session_id.
	sendBroadcast(wsAliceGlobal, []string{"sess-expired-broadcast"}, []string{"sess-expired-broadcast"}, "")
	assertRejectedSessionFrame(readWSJSONErrorFrame(t, wsAliceGlobal), "sess-expired-broadcast")

	// 2. Owned TargetSessionIds with unowned Wrapper.Destination -> rejected destSID identified.
	sendBroadcast(wsAliceGlobal, []string{"sess-bob-other"}, []string{"sess-alice-owned"}, "")
	assertRejectedSessionFrame(readWSJSONErrorFrame(t, wsAliceGlobal), "sess-bob-other")

	// 3. Owned TargetSessionIds with unowned Wrapper.SessionId -> rejected wrapperSimSession identified.
	sendBroadcast(wsAliceGlobal, []string{"sess-alice-owned"}, []string{"sess-alice-owned"}, "sess-expired-wrapper")
	assertRejectedSessionFrame(readWSJSONErrorFrame(t, wsAliceGlobal), "sess-expired-wrapper")

	// 4. Unowned a2ui_action.SessionId on global /ws -> rejected targetSessionID identified.
	actBytes, err := proto.Marshal(&gateway.A2UIAction{
		ActionName: "approve_plan",
		SessionId:  "sess-expired-a2ui",
	})
	if err != nil {
		t.Fatalf("marshal A2UIAction: %v", err)
	}
	actWrapBytes, err := proto.Marshal(&gateway.Wrapper{
		Type:    "a2ui_action",
		Event:   "a2ui_action",
		Payload: actBytes,
	})
	if err != nil {
		t.Fatalf("marshal a2ui Wrapper: %v", err)
	}
	if err := wsAliceGlobal.WriteMessage(websocket.BinaryMessage, actWrapBytes); err != nil {
		t.Fatalf("write a2ui wrapper: %v", err)
	}
	assertRejectedSessionFrame(readWSJSONErrorFrame(t, wsAliceGlobal), "sess-expired-a2ui")

	// Neither Alice's sibling global observer nor Bob's global observer receives any error frame.
	assertNoWSMessage(t, wsAliceGlobalSibling, 150*time.Millisecond)
	assertNoWSMessage(t, wsBobGlobal, 150*time.Millisecond)
}

func TestT5_EnvironmentReset_Maps_SaaSOffFlushesPythonSessionMaps(t *testing.T) {
	t.Setenv("SAAS_MODE", "off")
	t.Setenv("REDIS_PREFIX", "t5resetoff")

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	// Seed Python-style unscoped session_map keys (written by SessionManager when SAAS_MODE=off)
	// plus an unrelated key that must NOT be deleted by target "maps".
	if err := rdb.Set(context.Background(), "session_map:spawn-ctx-abc", "vertex-session-1", time.Hour).Err(); err != nil {
		t.Fatalf("seed session_map 1: %v", err)
	}
	if err := rdb.Set(context.Background(), "session_map:ctx-002", "vertex-session-2", time.Hour).Err(); err != nil {
		t.Fatalf("seed session_map 2: %v", err)
	}
	if err := rdb.Set(context.Background(), "simreg:session:123", "sim-123", time.Hour).Err(); err != nil {
		t.Fatalf("seed unrelated key: %v", err)
	}

	reg := session.NewRedisSessionRegistry(rdb, "t5resetoff", time.Hour)
	h := hub.NewHub()
	catalog := agent.NewCatalog(nil)
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-reset-off", h, catalog, reg)
	drainer := &t5CountingDrainer{}

	r := setupRouter(h, sb, catalog, reg, "gw-t5-reset-off", rdb, drainer)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/environment/reset", strings.NewReader(`{"targets":["maps"]}`))
	req.RemoteAddr = "127.0.0.1:1234"
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 OK, got %d body=%s", w.Code, w.Body.String())
	}

	var resetBody struct {
		Status  string `json:"status"`
		Results map[string]struct {
			Flushed bool `json:"flushed"`
			Count   int  `json:"count"`
		} `json:"results"`
	}
	if err := json.NewDecoder(w.Body).Decode(&resetBody); err != nil {
		t.Fatalf("decode reset response: %v", err)
	}
	if resetBody.Status != "reset" {
		t.Fatalf("expected status=reset, got %q", resetBody.Status)
	}
	if !resetBody.Results["maps"].Flushed || resetBody.Results["maps"].Count != 2 {
		t.Fatalf("expected maps flushed count=2, got %+v", resetBody.Results["maps"])
	}

	for _, key := range []string{"session_map:spawn-ctx-abc", "session_map:ctx-002"} {
		if exists, _ := rdb.Exists(context.Background(), key).Result(); exists != 0 {
			t.Fatalf("expected %q to be deleted in SAAS_MODE=off reset", key)
		}
	}
	if exists, _ := rdb.Exists(context.Background(), "simreg:session:123").Result(); exists != 1 {
		t.Fatalf("expected unrelated key simreg:session:123 to survive maps reset")
	}
}

type t5PokeCapturingSwitchboard struct {
	hub.Switchboard
	pokeUserCh chan string
}

func (s *t5PokeCapturingSwitchboard) PokeAgent(ctx context.Context, agentType string, event interface{}) error {
	uid := ""
	if uc, ok := account.FromContextOK(ctx); ok {
		uid = uc.UserID
	}
	select {
	case s.pokeUserCh <- uid:
	default:
	}
	return s.Switchboard.PokeAgent(ctx, agentType, event)
}

func TestT5_R9_SpawnPayloadsIncludeCanonicalUserIDAndPreservePokeContext(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5r9spawn")
	t.Setenv("ACCOUNT_KEY_PREFIX", "u")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	agentPokeHit := make(chan string, 2)
	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/.well-known/agent-card.json":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"name":"leadership_planner","url":"http://` + r.Host + `","version":"1.0"}`))
		case "/orchestration":
			w.WriteHeader(http.StatusOK)
			select {
			case agentPokeHit <- r.URL.Path:
			default:
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer agentSrv.Close()

	reg := session.NewRedisSessionRegistry(rdb, "t5r9spawn", time.Hour)
	h := hub.NewHub()
	catalog := agent.NewCatalog([]string{agentSrv.URL})
	baseSB := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-r9", h, catalog, reg)
	pokeCh := make(chan string, 4)
	sb := &t5PokeCapturingSwitchboard{Switchboard: baseSB, pokeUserCh: pokeCh}

	r := setupRouter(h, sb, catalog, reg, "gw-t5-r9", rdb, nil)

	// 1. POST /api/v1/sessions -> must include payload.user_id == acting_user_id == userId
	req1 := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/sessions",
		strings.NewReader(`{"agentType":"leadership_planner","simulation_id":"sim-r9-single"}`),
	)
	req1.Header.Set("Content-Type", "application/json")
	req1.Header.Set("Authorization", "Bearer user-alice")
	req1.RemoteAddr = "127.0.0.1:4321"
	w1 := httptest.NewRecorder()
	r.ServeHTTP(w1, req1)
	if w1.Code != http.StatusCreated {
		t.Fatalf("POST /api/v1/sessions expected 201, got %d (%s)", w1.Code, w1.Body.String())
	}
	var sessResp struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(w1.Body.Bytes(), &sessResp); err != nil {
		t.Fatalf("decode /api/v1/sessions response: %v", err)
	}
	q1 := hub.SpawnQueueNameForContext(
		account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"}),
		"leadership_planner",
		sessResp.SessionID,
		hub.DefaultSpawnShards,
	)
	raw1, err := rdb.LPop(context.Background(), q1).Result()
	if err != nil {
		t.Fatalf("LPOP %s: %v", q1, err)
	}
	var evt1 map[string]any
	if err := json.Unmarshal([]byte(raw1), &evt1); err != nil {
		t.Fatalf("unmarshal spawn event 1: %v", err)
	}
	p1 := evt1["payload"].(map[string]any)
	for _, field := range []string{"user_id", "acting_user_id", "userId"} {
		if got, _ := p1[field].(string); got != "user-alice" {
			t.Fatalf("/api/v1/sessions payload[%q]=%q, want user-alice", field, got)
		}
	}

	// 2. POST /api/v1/spawn -> must include payload.user_id and preserve UserContext on PokeAgent
	req2 := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/spawn",
		strings.NewReader(`{"agents":[{"agentType":"leadership_planner","count":1}],"simulation_id":"sim-r9-batch"}`),
	)
	req2.Header.Set("Content-Type", "application/json")
	req2.Header.Set("Authorization", "Bearer user-alice")
	req2.RemoteAddr = "127.0.0.1:4321"
	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, req2)
	if w2.Code != http.StatusOK {
		t.Fatalf("POST /api/v1/spawn expected 200, got %d (%s)", w2.Code, w2.Body.String())
	}
	var batchResp struct {
		Sessions []struct {
			SessionID string `json:"sessionId"`
		} `json:"sessions"`
	}
	if err := json.Unmarshal(w2.Body.Bytes(), &batchResp); err != nil || len(batchResp.Sessions) != 1 {
		t.Fatalf("decode /api/v1/spawn response: %v body=%s", err, w2.Body.String())
	}
	q2 := hub.SpawnQueueNameForContext(
		account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"}),
		"leadership_planner",
		batchResp.Sessions[0].SessionID,
		hub.DefaultSpawnShards,
	)
	raw2, err := rdb.LPop(context.Background(), q2).Result()
	if err != nil {
		t.Fatalf("LPOP %s: %v", q2, err)
	}
	var evt2 map[string]any
	if err := json.Unmarshal([]byte(raw2), &evt2); err != nil {
		t.Fatalf("unmarshal spawn event 2: %v", err)
	}
	p2 := evt2["payload"].(map[string]any)
	for _, field := range []string{"user_id", "acting_user_id", "userId"} {
		if got, _ := p2[field].(string); got != "user-alice" {
			t.Fatalf("/api/v1/spawn payload[%q]=%q, want user-alice", field, got)
		}
	}

	select {
	case pokeUser := <-pokeCh:
		if pokeUser != "user-alice" {
			t.Fatalf("PokeAgent ctx user=%q, want user-alice", pokeUser)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for PokeAgent call from /api/v1/spawn")
	}
	dispatchWG.Wait()
}

type t5CoexistenceResolver struct{}

func (t5CoexistenceResolver) Resolve(r *http.Request) (account.UserContext, error) {
	if h := r.Header.Get("X-User-Authorization"); h != "" {
		uid := strings.TrimPrefix(strings.TrimSpace(h), "Bearer ")
		return account.UserContext{
			UserID: uid,
			Email:  uid + "@example.com",
			Status: account.StatusApproved,
		}, nil
	}
	if h := r.Header.Get("Authorization"); h != "" {
		tok := strings.TrimPrefix(strings.TrimSpace(h), "Bearer ")
		if !account.IsGoogleOIDCToken(tok) {
			return account.UserContext{
				UserID: tok,
				Email:  tok + "@example.com",
				Status: account.StatusApproved,
			}, nil
		}
	}
	if c, err := r.Cookie("__session"); err == nil && c.Value != "" {
		uid := strings.TrimSpace(c.Value)
		return account.UserContext{
			UserID: uid,
			Email:  uid + "@example.com",
			Status: account.StatusApproved,
		}, nil
	}
	return account.UserContext{}, account.ErrNoIdentity
}

func TestT5_R10_Gateway_SimlessSpawnWSIsolationAndUserBearerExtraction(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5r10gw")
	t.Setenv("ACCOUNT_KEY_PREFIX", "u")
	origResolver := accountResolverForRouter
	accountResolverForRouter = t5CoexistenceResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/.well-known/agent-card.json":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"name":"leadership_planner","url":"http://` + r.Host + `","version":"1.0"}`))
		case "/orchestration":
			w.WriteHeader(http.StatusOK)
		default:
			http.NotFound(w, r)
		}
	}))
	defer agentSrv.Close()

	reg := session.NewRedisSessionRegistry(rdb, "t5r10gw", time.Hour)
	h := hub.NewHub(hub.WithSubscriptionStore(hub.NewRedisSubscriptionStore(rdb)))
	catalog := agent.NewCatalog([]string{agentSrv.URL})
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-r10", h, catalog, reg)

	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	go h.Run(runCtx)
	go func() { _ = sb.Start(runCtx) }()
	time.Sleep(80 * time.Millisecond)

	r := setupRouter(h, sb, catalog, reg, "gw-t5-r10", rdb, nil)
	srv := httptest.NewServer(r)
	defer srv.Close()
	wsBase := "ws" + strings.TrimPrefix(srv.URL, "http")

	dialGlobal := func(uid string) *websocket.Conn {
		hdr := http.Header{}
		hdr.Set("Authorization", "Bearer "+uid)
		conn, resp, err := websocket.DefaultDialer.Dial(wsBase+"/ws", hdr)
		if resp != nil && resp.Body != nil {
			defer resp.Body.Close()
		}
		if err != nil {
			t.Fatalf("dial global %s: %v", uid, err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		return conn
	}

	wsAliceGlobal := dialGlobal("user-alice")
	wsBobGlobal := dialGlobal("user-bob")

	// Build an unsigned-header-only OIDC-shaped JWT (iss=https://accounts.google.com)
	// to verify Cloud Run OIDC + X-User-Authorization coexistence on POST /api/v1/spawn.
	oidcJWT, err := jwt.NewWithClaims(jwt.SigningMethodNone, jwt.MapClaims{
		"iss": "https://accounts.google.com",
		"aud": "https://gateway-xyz.run.app",
		"sub": "sa-sim",
	}).SigningString()
	if err != nil {
		t.Fatalf("build oidc jwt: %v", err)
	}
	oidcJWT += ".sig"

	reqSpawn, err := http.NewRequest(
		http.MethodPost,
		srv.URL+"/api/v1/spawn",
		strings.NewReader(`{"agents":[{"agentType":"leadership_planner","count":1}]}`),
	)
	if err != nil {
		t.Fatalf("new spawn request: %v", err)
	}
	reqSpawn.Header.Set("Content-Type", "application/json")
	reqSpawn.Header.Set("Authorization", "Bearer "+oidcJWT)
	reqSpawn.Header.Set("X-User-Authorization", "Bearer user-alice")
	respSpawn, err := http.DefaultClient.Do(reqSpawn)
	if err != nil {
		t.Fatalf("POST /api/v1/spawn: %v", err)
	}
	defer respSpawn.Body.Close()
	if respSpawn.StatusCode != http.StatusOK {
		t.Fatalf("POST /api/v1/spawn expected 200, got %d", respSpawn.StatusCode)
	}
	var spawnBody struct {
		Sessions []struct {
			SessionID string `json:"sessionId"`
		} `json:"sessions"`
	}
	if err := json.NewDecoder(respSpawn.Body).Decode(&spawnBody); err != nil || len(spawnBody.Sessions) != 1 {
		t.Fatalf("decode spawn response: %v", err)
	}
	aliceSessID := spawnBody.Sessions[0].SessionID

	q := hub.SpawnQueueNameForContext(
		account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"}),
		"leadership_planner",
		aliceSessID,
		hub.DefaultSpawnShards,
	)
	rawEvt, err := rdb.LPop(context.Background(), q).Result()
	if err != nil {
		t.Fatalf("LPOP %s: %v", q, err)
	}
	var evt map[string]any
	if err := json.Unmarshal([]byte(rawEvt), &evt); err != nil {
		t.Fatalf("unmarshal spawn event: %v", err)
	}
	payloadMap := evt["payload"].(map[string]any)
	if gotTok, _ := payloadMap["user_auth_token"].(string); gotTok != "user-alice" {
		t.Fatalf("expected user_auth_token from X-User-Authorization (user-alice), got %q", gotTok)
	}

	// Emit sim-less agent reply targeting Alice's session -> ONLY wsAliceGlobal receives it.
	if err := sb.Broadcast(context.Background(), &gateway.Wrapper{
		Type:        "json",
		Event:       "narrative",
		Origin:      &gateway.Origin{Type: "agent", Id: "leadership_planner", SessionId: aliceSessID},
		Destination: []string{aliceSessID},
		Payload:     []byte(`{"secret":"alice-simless-reply"}`),
	}); err != nil {
		t.Fatalf("sb.Broadcast: %v", err)
	}

	_ = wsAliceGlobal.SetReadDeadline(time.Now().Add(3 * time.Second))
	mt, rawWS, err := wsAliceGlobal.ReadMessage()
	if err != nil {
		t.Fatalf("wsAliceGlobal read: %v", err)
	}
	if mt != websocket.BinaryMessage {
		t.Fatalf("expected BinaryMessage for wsAliceGlobal, got %d", mt)
	}
	var gotWrap gateway.Wrapper
	if err := proto.Unmarshal(rawWS, &gotWrap); err != nil {
		t.Fatalf("unmarshal wsAliceGlobal wrapper: %v", err)
	}
	if string(gotWrap.Payload) != `{"secret":"alice-simless-reply"}` {
		t.Fatalf("unexpected payload: %s", string(gotWrap.Payload))
	}

	assertNoWSMessage(t, wsBobGlobal, 150*time.Millisecond)
	dispatchWG.Wait()
}

func TestT5_R11_Gateway_SpawnEmptyAgentsRejectedFastAndNonNullSessions(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5r11spawn")
	t.Setenv("ACCOUNT_KEY_PREFIX", "u")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	var discoveryCalls atomic.Int64
	var failDiscovery atomic.Bool
	failDiscovery.Store(true)

	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/.well-known/agent-card.json":
			discoveryCalls.Add(1)
			if failDiscovery.Load() {
				http.Error(w, "discovery should not be called", http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"name":"planner","url":"http://` + r.Host + `","version":"1.0"}`))
		case "/orchestration":
			w.WriteHeader(http.StatusOK)
		default:
			http.NotFound(w, r)
		}
	}))
	defer agentSrv.Close()

	reg := session.NewRedisSessionRegistry(rdb, "t5r11spawn", time.Hour)
	h := hub.NewHub()
	catalog := agent.NewCatalog([]string{agentSrv.URL})
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-r11", h, catalog, reg)

	r := setupRouter(h, sb, catalog, reg, "gw-t5-r11", rdb, nil)

	doSpawn := func(body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/spawn", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer user-alice")
		req.RemoteAddr = "127.0.0.1:4321"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		return w
	}

	badPayloads := []struct {
		name string
		body string
	}{
		{name: "empty_agents", body: `{"agents": []}`},
		{name: "null_agents", body: `{"agents": null}`},
		{name: "zero_count", body: `{"agents":[{"agentType":"planner","count":0}]}`},
	}
	for _, tc := range badPayloads {
		t.Run(tc.name, func(t *testing.T) {
			w := doSpawn(tc.body)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("%s: expected HTTP 400, got %d body=%s", tc.name, w.Code, w.Body.String())
			}
			var errResp map[string]string
			if err := json.Unmarshal(w.Body.Bytes(), &errResp); err != nil {
				t.Fatalf("%s: decode error response %q: %v", tc.name, w.Body.String(), err)
			}
			if errResp["error"] != "invalid request body" {
				t.Fatalf("%s: expected error=\"invalid request body\", got %q", tc.name, errResp["error"])
			}
			if got := discoveryCalls.Load(); got != 0 {
				t.Fatalf("%s: expected 0 DiscoverAgentsContext calls, got %d", tc.name, got)
			}
		})
	}

	failDiscovery.Store(false)
	wValid := doSpawn(`{"agents":[{"agentType":"planner","count":1}],"simulation_id":"sim-r11-valid"}`)
	if wValid.Code != http.StatusOK {
		t.Fatalf("valid spawn: expected HTTP 200, got %d body=%s", wValid.Code, wValid.Body.String())
	}
	rawBody := wValid.Body.String()
	if strings.Contains(rawBody, `"sessions":null`) || !strings.Contains(rawBody, `"sessions":[`) {
		t.Fatalf("valid spawn: expected non-null sessions array, got %s", rawBody)
	}
	var validResp struct {
		Sessions []struct {
			SessionID    string `json:"sessionId"`
			AgentType    string `json:"agentType"`
			SimulationID string `json:"simulationId"`
		} `json:"sessions"`
	}
	if err := json.Unmarshal(wValid.Body.Bytes(), &validResp); err != nil {
		t.Fatalf("valid spawn: decode response %s: %v", rawBody, err)
	}
	if len(validResp.Sessions) != 1 || validResp.Sessions[0].SessionID == "" || validResp.Sessions[0].AgentType != "planner" {
		t.Fatalf("valid spawn: unexpected sessions payload %+v", validResp.Sessions)
	}
	if got := discoveryCalls.Load(); got == 0 {
		t.Fatalf("valid spawn: expected DiscoverAgentsContext to be invoked")
	}
	dispatchWG.Wait()
}

func TestT5_R11_Gateway_WSBinaryBroadcastA2UIAndResetIncludeUserID(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5r11ws")
	t.Setenv("ACCOUNT_KEY_PREFIX", "u")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	dispatchedCh := make(chan map[string]any, 4)
	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/.well-known/agent-card.json":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"name":"planner","url":"http://` + r.Host + `","version":"1.0"}`))
		case "/orchestration":
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			dispatchedCh <- body
			w.WriteHeader(http.StatusOK)
		default:
			http.NotFound(w, r)
		}
	}))
	defer agentSrv.Close()

	reg := session.NewRedisSessionRegistry(rdb, "t5r11ws", time.Hour)
	ctxAlice := account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"})
	if err := reg.TrackSession(ctxAlice, "sess-alice-r11", "planner", "sim-alice-r11"); err != nil {
		t.Fatalf("track alice: %v", err)
	}

	h := hub.NewHub(
		hub.WithSubscriptionStore(hub.NewRedisSubscriptionStore(rdb)),
		hub.WithSimulationOwnershipValidator(newSimulationOwnershipValidator(reg)),
		hub.WithRequireSimulationOwnership(true),
	)
	catalog := agent.NewCatalog([]string{agentSrv.URL})
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-r11-ws", h, catalog, reg)

	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	go h.Run(runCtx)
	go func() { _ = sb.Start(runCtx) }()
	time.Sleep(80 * time.Millisecond)

	r := setupRouter(h, sb, catalog, reg, "gw-t5-r11-ws", rdb, nil)
	srv := httptest.NewServer(r)
	defer srv.Close()
	wsBase := "ws" + strings.TrimPrefix(srv.URL, "http")

	hdr := http.Header{}
	hdr.Set("Authorization", "Bearer user-alice")
	wsConn, resp, err := websocket.DefaultDialer.Dial(wsBase+"/ws?simulation_id=sim-alice-r11", hdr)
	if resp != nil && resp.Body != nil {
		defer resp.Body.Close()
	}
	if err != nil {
		t.Fatalf("dial ws: %v", err)
	}
	defer wsConn.Close()

	// 1. Binary WS broadcast -> /orchestration payload must include verified user_id
	breqBytes, err := proto.Marshal(&gateway.BroadcastRequest{
		Payload:          []byte(`{"text":"hello"}`),
		TargetSessionIds: []string{"sess-alice-r11"},
	})
	if err != nil {
		t.Fatalf("marshal BroadcastRequest: %v", err)
	}
	bWrapBytes, err := proto.Marshal(&gateway.Wrapper{
		Type:         "broadcast",
		RequestId:    "req-bc-r11",
		SimulationId: "sim-alice-r11",
		Payload:      breqBytes,
	})
	if err != nil {
		t.Fatalf("marshal broadcast Wrapper: %v", err)
	}
	if err := wsConn.WriteMessage(websocket.BinaryMessage, bWrapBytes); err != nil {
		t.Fatalf("write broadcast wrapper: %v", err)
	}

	select {
	case evt := <-dispatchedCh:
		if evt["type"] != "broadcast" {
			t.Fatalf("expected type=broadcast, got %v", evt["type"])
		}
		pl, _ := evt["payload"].(map[string]any)
		if pl == nil || pl["user_id"] != "user-alice" {
			t.Fatalf("expected broadcast payload.user_id=user-alice, got %#v", evt["payload"])
		}
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for broadcast orchestration dispatch")
	}

	// 2. Binary WS a2ui_action -> /orchestration payload must include verified user_id
	actBytes, err := proto.Marshal(&gateway.A2UIAction{
		ActionName: "approve",
		SessionId:  "sess-alice-r11",
	})
	if err != nil {
		t.Fatalf("marshal A2UIAction: %v", err)
	}
	aWrapBytes, err := proto.Marshal(&gateway.Wrapper{
		Type:         "a2ui_action",
		RequestId:    "req-a2ui-r11",
		SimulationId: "sim-alice-r11",
		Payload:      actBytes,
	})
	if err != nil {
		t.Fatalf("marshal a2ui_action Wrapper: %v", err)
	}
	if err := wsConn.WriteMessage(websocket.BinaryMessage, aWrapBytes); err != nil {
		t.Fatalf("write a2ui_action wrapper: %v", err)
	}

	select {
	case evt := <-dispatchedCh:
		if evt["type"] != "a2ui_action" {
			t.Fatalf("expected type=a2ui_action, got %v", evt["type"])
		}
		pl, _ := evt["payload"].(map[string]any)
		if pl == nil || pl["user_id"] != "user-alice" {
			t.Fatalf("expected a2ui_action payload.user_id=user-alice, got %#v", evt["payload"])
		}
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for a2ui_action orchestration dispatch")
	}

	// 3. POST /api/v1/environment/reset -> simNotification on u:user-alice:simulation:broadcast carries user_id
	sub := rdb.Subscribe(context.Background(), "u:user-alice:simulation:broadcast")
	defer sub.Close()
	if _, err := sub.ReceiveTimeout(context.Background(), 2*time.Second); err != nil {
		t.Fatalf("subscribe confirm: %v", err)
	}

	reqReset := httptest.NewRequest(http.MethodPost, "/api/v1/environment/reset", strings.NewReader(`{"targets":["sessions"]}`))
	reqReset.Header.Set("Content-Type", "application/json")
	reqReset.Header.Set("Authorization", "Bearer user-alice")
	reqReset.RemoteAddr = "127.0.0.1:4321"
	wReset := httptest.NewRecorder()
	r.ServeHTTP(wReset, reqReset)
	if wReset.Code != http.StatusOK {
		t.Fatalf("POST /api/v1/environment/reset expected 200, got %d body=%s", wReset.Code, wReset.Body.String())
	}

	msg, err := sub.ReceiveMessage(context.Background())
	if err != nil {
		t.Fatalf("receive reset pubsub message: %v", err)
	}
	var simNotif map[string]any
	if err := json.Unmarshal([]byte(msg.Payload), &simNotif); err != nil {
		t.Fatalf("unmarshal simNotification %q: %v", msg.Payload, err)
	}
	if simNotif["type"] != "environment_reset" || simNotif["user_id"] != "user-alice" {
		t.Fatalf("expected environment_reset with user_id=user-alice, got %#v", simNotif)
	}

	dispatchWG.Wait()
}

func TestT5_R12_Gateway_SimulationIDValidation_SessionsAndSpawnReturn400(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5r12simval")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/.well-known/agent-card.json" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"name":"planner","url":"http://` + r.Host + `","version":"1.0"}`))
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer agentSrv.Close()

	reg := session.NewRedisSessionRegistry(rdb, "t5r12simval", time.Hour)
	h := hub.NewHub(
		hub.WithSubscriptionStore(hub.NewRedisSubscriptionStore(rdb)),
		hub.WithSimulationOwnershipValidator(newSimulationOwnershipValidator(reg)),
		hub.WithSessionOwnershipValidator(newSessionOwnershipValidator(reg)),
		hub.WithRequireSimulationOwnership(true),
	)
	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	go h.Run(runCtx)

	catalog := agent.NewCatalog([]string{agentSrv.URL})
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-r12-simval", h, catalog, reg)
	r := setupRouter(h, sb, catalog, reg, "gw-t5-r12-simval", rdb, nil)

	postReq := func(path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer user-alice")
		req.RemoteAddr = "127.0.0.1:4321"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		return w
	}

	invalidSimIDs := []string{
		"sim:colon:inject",
		"sim*",
		"a b",
		strings.Repeat("x", 129),
	}
	for _, bad := range invalidSimIDs {
		sessBody, _ := json.Marshal(map[string]any{
			"agentType":     "planner",
			"simulation_id": bad,
		})
		wSess := postReq("/api/v1/sessions", string(sessBody))
		if wSess.Code != http.StatusBadRequest || !strings.Contains(wSess.Body.String(), "invalid simulation_id") {
			t.Fatalf("POST /api/v1/sessions sim=%q: got %d (%s), want 400 invalid simulation_id",
				bad, wSess.Code, wSess.Body.String())
		}

		spawnBody, _ := json.Marshal(map[string]any{
			"agents":        []map[string]any{{"agentType": "planner", "count": 1}},
			"simulation_id": bad,
		})
		wSpawn := postReq("/api/v1/spawn", string(spawnBody))
		if wSpawn.Code != http.StatusBadRequest || !strings.Contains(wSpawn.Body.String(), "invalid simulation_id") {
			t.Fatalf("POST /api/v1/spawn sim=%q: got %d (%s), want 400 invalid simulation_id",
				bad, wSpawn.Code, wSpawn.Body.String())
		}
	}

	// Valid simulation_id succeeds on both /api/v1/sessions and /api/v1/spawn.
	wOKSess := postReq("/api/v1/sessions", `{"agentType":"planner","simulation_id":"sim_123-abc"}`)
	if wOKSess.Code != http.StatusCreated {
		t.Fatalf("valid /api/v1/sessions: got %d (%s), want 201", wOKSess.Code, wOKSess.Body.String())
	}
	wOKSpawn := postReq("/api/v1/spawn", `{"agents":[{"agentType":"planner","count":1}],"simulation_id":"sim_123-abc"}`)
	if wOKSpawn.Code != http.StatusOK {
		t.Fatalf("valid /api/v1/spawn: got %d (%s), want 200", wOKSpawn.Code, wOKSpawn.Body.String())
	}
	dispatchWG.Wait()
}

func TestT5_R12_Gateway_AISettings_SuperadminAndRateLimitEnforced(t *testing.T) {
	gin.SetMode(gin.TestMode)
	settingsPath := filepath.Join(t.TempDir(), "ai_settings_r12.json")
	t.Setenv("AI_SETTINGS_PATH", settingsPath)
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5r12ai")
	t.Setenv("RATELIMIT_CAPACITY", "2")
	t.Setenv("RATELIMIT_REFILL_PER_SEC", "0.01")

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	origResolver := accountResolverForRouter
	accountResolverForRouter = t5UserContextResolver{
		byToken: map[string]account.UserContext{
			"tok-user": {
				UserID:       "u-regular-r12",
				Email:        "user@example.com",
				Status:       account.StatusApproved,
				IsSuperadmin: false,
			},
			"tok-user-get": {
				UserID:       "u-get-r12",
				Email:        "get@example.com",
				Status:       account.StatusApproved,
				IsSuperadmin: false,
			},
			"tok-admin": {
				UserID:       "u-admin-r12",
				Email:        "admin@example.com",
				Status:       account.StatusApproved,
				IsSuperadmin: true,
			},
		},
	}
	defer func() { accountResolverForRouter = origResolver }()

	r := setupRouter(hub.NewHub(), nil, nil, nil, "gw-t5-r12-ai", rdb, nil)

	doReq := func(tok, method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+tok)
		if body != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		req.RemoteAddr = "127.0.0.1:4321"
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		return w
	}

	// Non-superadmin receives 403 superadmin_required on mutating routes while bucket has tokens,
	// and hits 429 rate_limited once capacity (2) is exhausted.
	wPut := doReq("tok-user", http.MethodPut, "/api/v1/settings/ai", `{"provider":"cloud_gemini"}`)
	if wPut.Code != http.StatusForbidden || !strings.Contains(wPut.Body.String(), "superadmin_required") {
		t.Fatalf("non-superadmin PUT: got %d (%s), want 403 superadmin_required", wPut.Code, wPut.Body.String())
	}
	wStart := doReq("tok-user", http.MethodPost, "/api/v1/settings/ai/start", `{}`)
	if wStart.Code != http.StatusForbidden || !strings.Contains(wStart.Body.String(), "superadmin_required") {
		t.Fatalf("non-superadmin POST /start: got %d (%s), want 403 superadmin_required", wStart.Code, wStart.Body.String())
	}
	wPull := doReq("tok-user", http.MethodPost, "/api/v1/settings/ai/pull", `{"model":"gemma3:4b"}`)
	if wPull.Code != http.StatusTooManyRequests || !strings.Contains(wPull.Body.String(), "rate_limited") {
		t.Fatalf("non-superadmin 3rd AI call must be 429 rate_limited, got %d (%s)", wPull.Code, wPull.Body.String())
	}

	// GET /api/v1/settings/ai is also subject to per-user rate limit (no unconditional skip bypass)
	// and explicitly exposes can_configure=false for non-superadmin users in SAAS_MODE=on.
	for i := 0; i < 2; i++ {
		wGet := doReq("tok-user-get", http.MethodGet, "/api/v1/settings/ai", "")
		if wGet.Code != http.StatusOK {
			t.Fatalf("GET #%d: got %d (%s), want 200", i+1, wGet.Code, wGet.Body.String())
		}
		var raw map[string]any
		if err := json.Unmarshal(wGet.Body.Bytes(), &raw); err != nil {
			t.Fatalf("decode GET #%d: %v", i+1, err)
		}
		canCfg, hasKey := raw["can_configure"].(bool)
		if !hasKey || canCfg {
			t.Fatalf("non-superadmin GET #%d in SAAS_MODE=on: got can_configure=%v (present=%v), want false", i+1, raw["can_configure"], hasKey)
		}
	}
	wGetLimited := doReq("tok-user-get", http.MethodGet, "/api/v1/settings/ai", "")
	if wGetLimited.Code != http.StatusTooManyRequests {
		t.Fatalf("GET #3 must be 429 rate_limited, got %d (%s)", wGetLimited.Code, wGetLimited.Body.String())
	}

	// Superadmin succeeds within rate limit and receives can_configure=true on PUT and GET.
	wAdmPut := doReq("tok-admin", http.MethodPut, "/api/v1/settings/ai", `{"provider":"cloud_gemini"}`)
	if wAdmPut.Code != http.StatusOK {
		t.Fatalf("superadmin PUT: got %d (%s), want 200", wAdmPut.Code, wAdmPut.Body.String())
	}
	var putRaw map[string]any
	if err := json.Unmarshal(wAdmPut.Body.Bytes(), &putRaw); err != nil {
		t.Fatalf("decode superadmin PUT: %v", err)
	}
	if canCfg, ok := putRaw["can_configure"].(bool); !ok || !canCfg {
		t.Fatalf("superadmin PUT in SAAS_MODE=on: got can_configure=%v (ok=%v), want true", putRaw["can_configure"], ok)
	}
	wAdmGet := doReq("tok-admin", http.MethodGet, "/api/v1/settings/ai", "")
	if wAdmGet.Code != http.StatusOK {
		t.Fatalf("superadmin GET: got %d (%s), want 200", wAdmGet.Code, wAdmGet.Body.String())
	}
	var admGetRaw map[string]any
	if err := json.Unmarshal(wAdmGet.Body.Bytes(), &admGetRaw); err != nil {
		t.Fatalf("decode superadmin GET: %v", err)
	}
	if canCfg, ok := admGetRaw["can_configure"].(bool); !ok || !canCfg {
		t.Fatalf("superadmin GET in SAAS_MODE=on: got can_configure=%v (ok=%v), want true", admGetRaw["can_configure"], ok)
	}
	wAdmLimited := doReq("tok-admin", http.MethodPost, "/api/v1/settings/ai/pull", `{"model":"gemma3:4b"}`)
	if wAdmLimited.Code != http.StatusTooManyRequests {
		t.Fatalf("superadmin 3rd AI call must be 429 rate_limited, got %d (%s)", wAdmLimited.Code, wAdmLimited.Body.String())
	}
}

func TestT5_R13_Gateway_AISettings_CanConfigureMatrix(t *testing.T) {
	gin.SetMode(gin.TestMode)
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	origResolver := accountResolverForRouter
	accountResolverForRouter = t5UserContextResolver{
		byToken: map[string]account.UserContext{
			"tok-user": {
				UserID:       "u-regular-r13",
				Email:        "user@example.com",
				Status:       account.StatusApproved,
				IsSuperadmin: false,
			},
			"tok-admin": {
				UserID:       "u-admin-r13",
				Email:        "admin@example.com",
				Status:       account.StatusApproved,
				IsSuperadmin: true,
			},
		},
	}
	defer func() { accountResolverForRouter = origResolver }()

	cases := []struct {
		name     string
		saasMode string
		token    string
		wantCan  bool
	}{
		{name: "saas_off_non_superadmin", saasMode: "off", token: "tok-user", wantCan: true},
		{name: "saas_on_non_superadmin", saasMode: "on", token: "tok-user", wantCan: false},
		{name: "saas_on_superadmin", saasMode: "on", token: "tok-admin", wantCan: true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AI_SETTINGS_PATH", filepath.Join(t.TempDir(), "ai_settings.json"))
			t.Setenv("SAAS_MODE", tc.saasMode)
			t.Setenv("REDIS_PREFIX", "t5r13ai_"+tc.name)

			r := setupRouter(hub.NewHub(), nil, nil, nil, "gw-t5-r13-ai", rdb, nil)
			req := httptest.NewRequest(http.MethodGet, "/api/v1/settings/ai", nil)
			req.Header.Set("Authorization", "Bearer "+tc.token)
			req.RemoteAddr = "127.0.0.1:4321"
			w := httptest.NewRecorder()
			r.ServeHTTP(w, req)
			if w.Code != http.StatusOK {
				t.Fatalf("GET /api/v1/settings/ai: got %d (%s), want 200", w.Code, w.Body.String())
			}
			var raw map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &raw); err != nil {
				t.Fatalf("decode response: %v", err)
			}
			got, ok := raw["can_configure"].(bool)
			if !ok {
				t.Fatalf("missing boolean can_configure in payload: %s", w.Body.String())
			}
			if got != tc.wantCan {
				t.Fatalf("%s: got can_configure=%v, want %v", tc.name, got, tc.wantCan)
			}
		})
	}
}

func TestT5_R12_Gateway_CrossTenantSharedSimulationID_E2E_WS(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("REDIS_PREFIX", "t5r12e2e")
	t.Setenv("ACCOUNT_KEY_PREFIX", "u")
	origResolver := accountResolverForRouter
	accountResolverForRouter = headerTokenResolver{}
	defer func() { accountResolverForRouter = origResolver }()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	reg := session.NewRedisSessionRegistry(rdb, "t5r12e2e", time.Hour)
	ctxAlice := account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"})
	ctxBob := account.WithContext(context.Background(), account.UserContext{UserID: "user-bob"})
	if err := reg.TrackSession(ctxAlice, "sess-alice-hijack", "planner", "sim-shared-hijack"); err != nil {
		t.Fatalf("track alice: %v", err)
	}
	if err := reg.TrackSession(ctxBob, "sess-bob-hijack", "planner", "sim-shared-hijack"); err != nil {
		t.Fatalf("track bob: %v", err)
	}

	h := hub.NewHub(
		hub.WithSubscriptionStore(hub.NewRedisSubscriptionStore(rdb)),
		hub.WithSimulationOwnershipValidator(newSimulationOwnershipValidator(reg)),
		hub.WithSessionOwnershipValidator(newSessionOwnershipValidator(reg)),
		hub.WithRequireSimulationOwnership(true),
	)
	catalog := agent.NewCatalog(nil)
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-t5-r12-e2e", h, catalog, reg)

	runCtx, runCancel := context.WithCancel(context.Background())
	defer runCancel()
	go h.Run(runCtx)
	go func() { _ = sb.Start(runCtx) }()
	time.Sleep(100 * time.Millisecond)

	r := setupRouter(h, sb, catalog, reg, "gw-t5-r12-e2e", rdb, nil)
	srv := httptest.NewServer(r)
	defer srv.Close()
	wsBase := "ws" + strings.TrimPrefix(srv.URL, "http")

	dial := func(uid, query string) *websocket.Conn {
		hdr := http.Header{}
		hdr.Set("Authorization", "Bearer "+uid)
		conn, resp, err := websocket.DefaultDialer.Dial(wsBase+"/ws"+query, hdr)
		if resp != nil && resp.Body != nil {
			defer resp.Body.Close()
		}
		if err != nil {
			t.Fatalf("dial %s %s: %v", uid, query, err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		return conn
	}

	wsAliceSim := dial("user-alice", "?sessionId=sess-alice-hijack&simulationId=sim-shared-hijack")
	wsBobSim := dial("user-bob", "?simulationId=sim-shared-hijack")
	wsBobGlobal := dial("user-bob", "")
	if err := wsBobGlobal.WriteJSON(map[string]string{
		"type":          "subscribe_simulation",
		"simulation_id": "sim-shared-hijack",
	}); err != nil {
		t.Fatalf("bob subscribe: %v", err)
	}
	time.Sleep(100 * time.Millisecond)

	breqBytes, err := proto.Marshal(&gateway.BroadcastRequest{
		Payload:          []byte(`{"text":"TOP_SECRET_ALICE_REHEARSAL_DATA"}`),
		TargetSessionIds: []string{"sess-alice-hijack"},
	})
	if err != nil {
		t.Fatalf("marshal BroadcastRequest: %v", err)
	}
	wrapBytes, err := proto.Marshal(&gateway.Wrapper{
		Type:         "broadcast",
		Event:        "broadcast",
		SimulationId: "sim-shared-hijack",
		Payload:      breqBytes,
		Origin:       &gateway.Origin{Type: "client", Id: "tester-ui"},
	})
	if err != nil {
		t.Fatalf("marshal Wrapper: %v", err)
	}
	if err := wsAliceSim.WriteMessage(websocket.BinaryMessage, wrapBytes); err != nil {
		t.Fatalf("alice write broadcast: %v", err)
	}

	_ = wsAliceSim.SetReadDeadline(time.Now().Add(3 * time.Second))
	mt, rawAlice, err := wsAliceSim.ReadMessage()
	if err != nil || mt != websocket.BinaryMessage {
		t.Fatalf("expected Alice binary frame, got mt=%d err=%v", mt, err)
	}
	var gotAlice gateway.Wrapper
	if err := proto.Unmarshal(rawAlice, &gotAlice); err != nil {
		t.Fatalf("unmarshal Alice wrapper: %v", err)
	}
	if gotAlice.SimulationId != "sim-shared-hijack" {
		t.Fatalf("unexpected simulation_id %q", gotAlice.SimulationId)
	}

	assertNoWSMessage(t, wsBobSim, 200*time.Millisecond)
	assertNoWSMessage(t, wsBobGlobal, 200*time.Millisecond)
	dispatchWG.Wait()
}
