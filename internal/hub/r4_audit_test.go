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

package hub

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	pubsub "cloud.google.com/go/pubsub/v2/apiv1"
	"cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"github.com/GoogleCloudPlatform/race-condition/gen_proto/gateway"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/agent"
	"github.com/GoogleCloudPlatform/race-condition/internal/session"
	"github.com/alicebob/miniredis/v2"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/api/option"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

type r4WSConnRecord struct {
	sessionID string
	userID    string
	autoSim   string
	conn      *websocket.Conn
}

type r4HubHarness struct {
	hub    *Hub
	server *httptest.Server
	mu     sync.Mutex
	conns  []r4WSConnRecord
}

func newR4HubHarness(t *testing.T, opts ...HubOption) *r4HubHarness {
	t.Helper()
	h := NewHub(opts...)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go h.Run(ctx)

	harness := &r4HubHarness{hub: h}
	upgrader := websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}
	harness.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		sid := r.URL.Query().Get("sessionId")
		uid := r.URL.Query().Get("userId")
		simID := r.URL.Query().Get("simulationId")
		harness.mu.Lock()
		harness.conns = append(harness.conns, r4WSConnRecord{
			sessionID: sid,
			userID:    uid,
			autoSim:   simID,
			conn:      conn,
		})
		harness.mu.Unlock()
		if uid == "" && r.URL.Query().Get("useLegacyRegister") == "1" {
			if simID != "" {
				h.Register(sid, conn, simID)
			} else {
				h.Register(sid, conn)
			}
			return
		}
		if simID != "" {
			h.RegisterWithUser(sid, conn, uid, simID)
		} else {
			h.RegisterWithUser(sid, conn, uid)
		}
	}))
	t.Cleanup(harness.server.Close)
	return harness
}

func (rh *r4HubHarness) dial(t *testing.T, query string) (*websocket.Conn, *websocket.Conn) {
	t.Helper()
	before := len(rh.connsSnapshot())
	wsURL := strings.Replace(rh.server.URL, "http", "ws", 1) + "?" + query
	ws, resp, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if resp != nil && resp.Body != nil {
		defer resp.Body.Close()
	}
	require.NoError(t, err)
	t.Cleanup(func() { _ = ws.Close() })

	var srvConn *websocket.Conn
	require.Eventually(t, func() bool {
		conns := rh.connsSnapshot()
		if len(conns) <= before {
			return false
		}
		srvConn = conns[len(conns)-1].conn
		rh.hub.mu.RLock()
		_, registered := rh.hub.connToClient[srvConn]
		rh.hub.mu.RUnlock()
		return registered
	}, 2*time.Second, 10*time.Millisecond)
	return ws, srvConn
}

func (rh *r4HubHarness) connsSnapshot() []r4WSConnRecord {
	rh.mu.Lock()
	defer rh.mu.Unlock()
	out := make([]r4WSConnRecord, len(rh.conns))
	copy(out, rh.conns)
	return out
}

func readTextErrorFrame(t *testing.T, ws *websocket.Conn) (GatewayMessage, map[string]string) {
	t.Helper()
	_ = ws.SetReadDeadline(time.Now().Add(2 * time.Second))
	mt, payload, err := ws.ReadMessage()
	require.NoError(t, err)
	require.Equal(t, websocket.TextMessage, mt)

	var gm GatewayMessage
	require.NoError(t, json.Unmarshal(payload, &gm))
	dataBytes, err := json.Marshal(gm.Data)
	require.NoError(t, err)
	var dataMap map[string]string
	require.NoError(t, json.Unmarshal(dataBytes, &dataMap))
	return gm, dataMap
}

type recordingSubStore struct {
	mu          sync.Mutex
	subCalls    []string
	lookupErr   error
	blockLookup chan struct{}
}

func (r *recordingSubStore) Subscribe(_ context.Context, simID, sessionID string) error {
	r.mu.Lock()
	r.subCalls = append(r.subCalls, simID+":"+sessionID)
	r.mu.Unlock()
	return nil
}

func (r *recordingSubStore) Unsubscribe(_ context.Context, _, _ string) error { return nil }
func (r *recordingSubStore) UnsubscribeAll(_ context.Context, _ string) error   { return nil }
func (r *recordingSubStore) Lookup(ctx context.Context, _ string) ([]string, error) {
	if r.blockLookup != nil {
		select {
		case <-r.blockLookup:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return nil, r.lookupErr
}

func (r *recordingSubStore) countSubCalls() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.subCalls)
}

func TestR4_HubHandleTextMessage_ValidationAndErrorFrames(t *testing.T) {
	harness := newR4HubHarness(t)
	ws, srvConn := harness.dial(t, "sessionId=sess-val&useLegacyRegister=1")

	// 1. Invalid JSON -> explicit JSON error frame
	harness.hub.HandleTextMessage(srvConn, []byte("{not-json"))
	gm, data := readTextErrorFrame(t, ws)
	assert.Equal(t, "error", gm.Status)
	assert.Equal(t, "error", gm.Type)
	assert.Equal(t, "bad_request", gm.Event)
	assert.Equal(t, "invalid_json", data["error"])

	// 2. Empty / whitespace simulation_id on subscribe_simulation -> error frame, not subscribed
	harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"subscribe_simulation","simulation_id":"   "}`))
	gm, data = readTextErrorFrame(t, ws)
	assert.Equal(t, "error", gm.Status)
	assert.Equal(t, "bad_request", gm.Event)
	assert.Equal(t, "missing_simulation_id", data["error"])

	// 3. Empty / whitespace simulation_id on unsubscribe_simulation -> error frame
	harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"unsubscribe_simulation","simulation_id":""}`))
	gm, data = readTextErrorFrame(t, ws)
	assert.Equal(t, "error", gm.Status)
	assert.Equal(t, "bad_request", gm.Event)
	assert.Equal(t, "missing_simulation_id", data["error"])

	// 4. Unknown message type -> error frame
	harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"bogus_op","simulation_id":"sim-1"}`))
	gm, data = readTextErrorFrame(t, ws)
	assert.Equal(t, "error", gm.Status)
	assert.Equal(t, "bad_request", gm.Event)
	assert.Equal(t, "unknown_message_type", data["error"])
}

func TestR4_HubOwnership_AllowsOwnedAndRejectsUnowned(t *testing.T) {
	subStore := &recordingSubStore{}
	validator := func(_ context.Context, userID, simulationID string) (bool, error) {
		return userID == "user-A" && simulationID == "sim-A", nil
	}
	harness := newR4HubHarness(t,
		WithSubscriptionStore(subStore),
		WithSimulationOwnershipValidator(validator),
	)

	ws, srvConn := harness.dial(t, "sessionId=sess-owner&userId=user-A")

	// Unowned sim-B via HandleTextMessage -> forbidden error frame, not subscribed
	harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-B"}`))
	gm, data := readTextErrorFrame(t, ws)
	assert.Equal(t, "error", gm.Status)
	assert.Equal(t, "error", gm.Type)
	assert.Equal(t, "forbidden", gm.Event)
	assert.Equal(t, "simulation_not_owned", data["error"])
	assert.Equal(t, "sim-B", data["simulation_id"])

	// Direct SubscribeSimulation with unowned sim-B -> also rejected
	harness.hub.SubscribeSimulation(srvConn, "sim-B")
	gm, data = readTextErrorFrame(t, ws)
	assert.Equal(t, "forbidden", gm.Event)
	assert.Equal(t, "simulation_not_owned", data["error"])

	assert.Equal(t, 0, subStore.countSubCalls())
	harness.hub.mu.RLock()
	_, simBExists := harness.hub.simToClients["sim-B"]
	harness.hub.mu.RUnlock()
	assert.False(t, simBExists)

	// Remote message for sim-B targeted at other session must NOT arrive.
	// Send sim-B first, then subscribe & send sim-A: because gorilla/websocket
	// permanently fails a Conn after any ReadMessage timeout, verifying that
	// the very next binary message received is sim-A (and not sim-B) proves
	// sim-B was never delivered while keeping ws healthy.
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "test_event",
		SimulationId: "sim-B",
		SessionId:    "other-session",
	})
	time.Sleep(80 * time.Millisecond)

	// Owned sim-A -> subscribed and receives sim-A remote frames
	harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-A"}`))
	require.Eventually(t, func() bool {
		return subStore.countSubCalls() == 1
	}, time.Second, 10*time.Millisecond)

	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "test_event",
		SimulationId: "sim-A",
		SessionId:    "other-session",
	})
	raw := helperExpectBinaryMessage(t, ws, time.Second)
	assert.Contains(t, string(raw), "sim-A")
	helperExpectNoMessage(t, ws, 100*time.Millisecond)
}

func TestR4_HubOwnership_FailClosedMatrix(t *testing.T) {
	t.Run("validator_error_fails_closed", func(t *testing.T) {
		validator := func(_ context.Context, _, _ string) (bool, error) {
			return false, errors.New("redis down")
		}
		harness := newR4HubHarness(t, WithSimulationOwnershipValidator(validator))
		ws, srvConn := harness.dial(t, "sessionId=s1&userId=user-A")

		harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-A"}`))
		gm, data := readTextErrorFrame(t, ws)
		assert.Equal(t, "service_unavailable", gm.Event)
		assert.Equal(t, "ownership_check_failed", data["error"])
	})

	t.Run("unauthenticated_user_with_validator_fails_closed", func(t *testing.T) {
		validator := func(_ context.Context, _, _ string) (bool, error) {
			return true, nil
		}
		harness := newR4HubHarness(t, WithSimulationOwnershipValidator(validator))
		ws, srvConn := harness.dial(t, "sessionId=s2&useLegacyRegister=1")

		harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-A"}`))
		gm, data := readTextErrorFrame(t, ws)
		assert.Equal(t, "unauthorized", gm.Event)
		assert.Equal(t, "authentication_required", data["error"])
	})

	t.Run("authenticated_user_without_validator_fails_closed", func(t *testing.T) {
		harness := newR4HubHarness(t)
		ws, srvConn := harness.dial(t, "sessionId=s3&userId=user-A")

		harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-A"}`))
		gm, data := readTextErrorFrame(t, ws)
		assert.Equal(t, "service_unavailable", gm.Event)
		assert.Equal(t, "ownership_validator_unavailable", data["error"])
	})

	t.Run("require_ownership_without_validator_fails_closed_even_empty_user", func(t *testing.T) {
		harness := newR4HubHarness(t, WithRequireSimulationOwnership(true))
		ws, srvConn := harness.dial(t, "sessionId=s4&useLegacyRegister=1")

		harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-A"}`))
		gm, data := readTextErrorFrame(t, ws)
		assert.Equal(t, "service_unavailable", gm.Event)
		assert.Equal(t, "ownership_validator_unavailable", data["error"])
	})

	t.Run("SetSimulationOwnershipValidator_updates_runtime", func(t *testing.T) {
		harness := newR4HubHarness(t)
		ws, srvConn := harness.dial(t, "sessionId=s5&userId=user-A")
		harness.hub.SetSimulationOwnershipValidator(func(_ context.Context, userID, simID string) (bool, error) {
			return userID == "user-A" && simID == "sim-allowed", nil
		})

		harness.hub.HandleTextMessage(srvConn, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-denied"}`))
		gm, data := readTextErrorFrame(t, ws)
		assert.Equal(t, "forbidden", gm.Event)
		assert.Equal(t, "simulation_not_owned", data["error"])
	})
}

func TestR4_HubGlobalObserver_ErrorIsolation(t *testing.T) {
	validator := func(_ context.Context, userID, simID string) (bool, error) {
		return userID == "user-A" && simID == "sim-A", nil
	}
	harness := newR4HubHarness(t, WithSimulationOwnershipValidator(validator))

	wsA, srvConnA := harness.dial(t, "sessionId=&userId=user-A")
	wsB, _ := harness.dial(t, "sessionId=&userId=user-B")

	harness.hub.HandleTextMessage(srvConnA, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-victim"}`))

	gm, data := readTextErrorFrame(t, wsA)
	assert.Equal(t, "forbidden", gm.Event)
	assert.Equal(t, "simulation_not_owned", data["error"])
	assert.Empty(t, gm.Destination)

	// user-B (also sessionID == "") must NOT receive user-A's error frame.
	helperExpectNoMessage(t, wsB, 150*time.Millisecond)
}

func TestR4_HubRegisterWithUser_RejectsUnauthorizedAutoSubscribe(t *testing.T) {
	subStore := &recordingSubStore{}
	validator := func(_ context.Context, userID, simID string) (bool, error) {
		return userID == "user-A" && simID == "sim-owned", nil
	}
	harness := newR4HubHarness(t,
		WithSubscriptionStore(subStore),
		WithSimulationOwnershipValidator(validator),
	)

	ws, _ := harness.dial(t, "sessionId=sess-auto&userId=user-A&simulationId=sim-secret")

	gm, data := readTextErrorFrame(t, ws)
	assert.Equal(t, "forbidden", gm.Event)
	assert.Equal(t, "simulation_not_owned", data["error"])
	assert.Equal(t, "sim-secret", data["simulation_id"])

	harness.hub.mu.RLock()
	_, exists := harness.hub.simToClients["sim-secret"]
	harness.hub.mu.RUnlock()
	assert.False(t, exists)
	assert.Equal(t, 0, subStore.countSubCalls())

	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "test_event",
		SimulationId: "sim-secret",
		SessionId:    "other-session",
	})
	helperExpectNoMessage(t, ws, 150*time.Millisecond)
}

func TestR4_HubNilWrapperAndSendMessageErrorStatus(t *testing.T) {
	harness := newR4HubHarness(t)
	ws, _ := harness.dial(t, "sessionId=s-msg&useLegacyRegister=1")

	// Must not panic on nil wrapper or nil remoteMessage
	harness.hub.HandleRemoteMessage(nil)
	harness.hub.processRemoteMessage(context.Background(), nil)
	harness.hub.processRemoteMessage(context.Background(), &remoteMessage{wrapper: nil})

	harness.hub.SendMessage("s-msg", "error", "dispatch_failed", map[string]string{"error": "boom"})
	gm, _ := readTextErrorFrame(t, ws)
	assert.Equal(t, "error", gm.Status)
	assert.Equal(t, "error", gm.Type)
	assert.Equal(t, "dispatch_failed", gm.Event)

	harness.hub.SendMessage("s-msg", "text", "narrative", map[string]string{"text": "ok"})
	_ = ws.SetReadDeadline(time.Now().Add(2 * time.Second))
	mt, payload, err := ws.ReadMessage()
	require.NoError(t, err)
	require.Equal(t, websocket.TextMessage, mt)
	var okMsg GatewayMessage
	require.NoError(t, json.Unmarshal(payload, &okMsg))
	assert.Equal(t, "success", okMsg.Status)
}

func TestR4_HubSubStoreLookupErrorAndRunWaitsForSubStore(t *testing.T) {
	store := &recordingSubStore{
		lookupErr:   errors.New("redis lookup error"),
		blockLookup: make(chan struct{}),
	}
	h := NewHub(WithSubscriptionStore(store), WithRemoteWorkers(1))
	ctx, cancel := context.WithCancel(context.Background())
	runDone := make(chan struct{})
	go func() {
		h.Run(ctx)
		close(runDone)
	}()

	// Trigger Lookup which blocks until ctx is cancelled, and also send a Subscribe
	h.deliverCrossInstance(ctx, "sim-1", []byte("x"), map[*client]bool{})
	// Now cancel ctx and verify Run exits promptly
	cancel()
	select {
	case <-runDone:
	case <-time.After(2 * time.Second):
		t.Fatal("Hub.Run did not exit promptly after context cancellation")
	}
}

type r4MockRegistry struct {
	session.DistributedRegistry
	mu               sync.Mutex
	activeTypes      []string
	activeTypesErr   error
	findSimID        string
	findSimErr       error
	lastFindSimUser  string
}

func (m *r4MockRegistry) ActiveAgentTypes(_ context.Context) ([]string, error) {
	if m.activeTypesErr != nil {
		return nil, m.activeTypesErr
	}
	return m.activeTypes, nil
}

func (m *r4MockRegistry) FindSimulation(ctx context.Context, _ string) (string, error) {
	m.mu.Lock()
	if uc, ok := account.FromContextOK(ctx); ok {
		m.lastFindSimUser = uc.UserID
	}
	m.mu.Unlock()
	if m.findSimErr != nil {
		return "", m.findSimErr
	}
	return m.findSimID, nil
}

func TestR4_SwitchboardDispatchAndPoke_ErrAgentNotFound(t *testing.T) {
	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/.well-known/agent-card.json" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"name":    "simulator",
				"url":     "http://127.0.0.1:9999/",
				"version": "1.0.0",
			})
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer agentSrv.Close()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	catalog := agent.NewCatalog([]string{agentSrv.URL})
	sb := NewSwitchboardWithRegistry(rdb, "gw-r4", NewHub(), catalog, nil)

	err := sb.DispatchToAgent(context.Background(), "nonexistent_agent", map[string]string{"type": "tick"})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAgentNotFound)

	err = sb.PokeAgent(context.Background(), "nonexistent_agent", map[string]string{"type": "tick"})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrAgentNotFound)
}

func TestR4_SwitchboardAsyncDispatch_SurvivesCallerCancelAndPreservesUserContext(t *testing.T) {
	receivedCh := make(chan string, 2)
	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/.well-known/agent-card.json" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"name":    "simulator",
				"url":     "https://example.com/aiplatform.googleapis.com/reasoningEngines/123",
				"version": "1.0.0",
			})
			return
		}
		// Small delay so caller's defer cancel() has definitely run before HTTP Do() executes
		time.Sleep(40 * time.Millisecond)
		receivedCh <- r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"task":{"artifacts":[{"parts":[{"text":"final reply"}]}]}}`))
	}))
	defer agentSrv.Close()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	catalog := agent.NewCatalog([]string{agentSrv.URL})
	reg := &r4MockRegistry{findSimID: "sim-user-scoped"}
	sb := NewSwitchboardWithRegistry(rdb, "gw-r4", NewHub(), catalog, reg).(*RedisSwitchboard)
	sb.gcpClient = &http.Client{Timeout: 5 * time.Second}

	// Simulate cmd/gateway handler creating a short-lived ctx with UserContext and defer cancel()
	userCtx := account.WithContext(context.Background(), account.UserContext{UserID: "user-42"})
	callerCtx, callerCancel := context.WithTimeout(userCtx, 10*time.Second)
	err := sb.DispatchToAgent(callerCtx, "simulator", map[string]string{
		"type":      "spawn_agent",
		"sessionId": "sess-42",
	})
	callerCancel() // immediately cancelled as soon as DispatchToAgent returns!
	require.NoError(t, err)

	select {
	case gotPath := <-receivedCh:
		assert.Equal(t, "/aiplatform.googleapis.com/reasoningEngines/123/a2a/v1/message:send", gotPath)
	case <-time.After(2 * time.Second):
		t.Fatal("async dispatch did not survive immediate caller cancellation")
	}

	sb.Wait()
	reg.mu.Lock()
	gotUser := reg.lastFindSimUser
	reg.mu.Unlock()
	assert.Equal(t, "user-42", gotUser, "async dispatch must preserve account.UserContext across WithoutCancel")
}

func TestR4_SwitchboardLifecycle_ShutdownCancelsHangingHTTPAndBlocksPostShutdown(t *testing.T) {
	reqStarted := make(chan struct{})
	reqCancelled := make(chan struct{})
	testDone := make(chan struct{})
	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/.well-known/agent-card.json" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"name":    "simulator",
				"url":     "http://127.0.0.1:9999/",
				"version": "1.0.0",
			})
			return
		}
		// Drain request body so net/http's background reader detects client TCP FIN.
		_, _ = io.Copy(io.Discard, r.Body)
		close(reqStarted)
		select {
		case <-r.Context().Done():
			close(reqCancelled)
		case <-testDone:
		}
	}))
	defer func() {
		close(testDone)
		agentSrv.CloseClientConnections()
		agentSrv.Close()
	}()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	catalog := agent.NewCatalog([]string{agentSrv.URL})
	sb := NewSwitchboardWithRegistry(rdb, "gw-r4", NewHub(), catalog, nil).(*RedisSwitchboard)

	startCtx, startCancel := context.WithCancel(context.Background())
	startDone := make(chan error, 1)
	go func() {
		startDone <- sb.Start(startCtx)
	}()

	require.NoError(t, sb.DispatchToAgent(context.Background(), "simulator", map[string]string{"type": "tick"}))

	select {
	case <-reqStarted:
	case <-time.After(2 * time.Second):
		t.Fatal("hanging HTTP request did not start")
	}

	// Cancelling Start(ctx) must cancel lifecycleCtx -> abort hanging HTTP request -> Wait() returns promptly
	startCancel()

	select {
	case <-reqCancelled:
	case <-time.After(2 * time.Second):
		t.Fatal("hanging HTTP request was not cancelled on Start(ctx) shutdown")
	}

	waitDone := make(chan struct{})
	go func() {
		sb.Wait()
		close(waitDone)
	}()
	select {
	case <-waitDone:
	case <-time.After(2 * time.Second):
		t.Fatal("RedisSwitchboard.Wait() blocked after shutdown")
	}

	select {
	case err := <-startDone:
		assert.ErrorIs(t, err, context.Canceled)
	case <-time.After(2 * time.Second):
		t.Fatal("Start did not return after cancel")
	}

	// Post-shutdown calls must fail immediately with ErrSwitchboardClosed
	assert.ErrorIs(t, sb.DispatchToAgent(context.Background(), "simulator", map[string]string{}), ErrSwitchboardClosed)
	assert.ErrorIs(t, sb.PokeAgent(context.Background(), "simulator", map[string]string{}), ErrSwitchboardClosed)
	assert.ErrorIs(t, sb.PublishOrchestration(context.Background(), "simulation:broadcast", map[string]string{}), ErrSwitchboardClosed)
}

func TestR4_SwitchboardPublishOrchestration_ErrorsAndAgentEngineBranch(t *testing.T) {
	t.Run("redis_publish_error_propagates", func(t *testing.T) {
		mr := miniredis.RunT(t)
		rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
		defer rdb.Close()
		sb := NewSwitchboardWithRegistry(rdb, "gw-r4", NewHub(), nil, nil)
		mr.Close()

		err := sb.PublishOrchestration(context.Background(), "simulation:broadcast", map[string]string{"k": "v"})
		require.Error(t, err)
	})

	t.Run("catalog_discovery_error_propagates", func(t *testing.T) {
		mr := miniredis.RunT(t)
		rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
		defer rdb.Close()

		// Unreachable agent URL causes DiscoverAgents to return error
		catalog := agent.NewCatalog([]string{"http://127.0.0.1:1"})
		sb := NewSwitchboardWithRegistry(rdb, "gw-r4", NewHub(), catalog, nil)

		err := sb.PublishOrchestration(context.Background(), "simulation:broadcast", map[string]string{"k": "v"})
		require.Error(t, err)
		assert.Contains(t, err.Error(), "catalog discovery")
	})

	t.Run("active_agent_types_error_propagates", func(t *testing.T) {
		agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"name":    "simulator",
				"url":     "http://127.0.0.1:9999/",
				"version": "1.0.0",
			})
		}))
		defer agentSrv.Close()

		mr := miniredis.RunT(t)
		rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
		defer rdb.Close()

		catalog := agent.NewCatalog([]string{agentSrv.URL})
		reg := &r4MockRegistry{activeTypesErr: errors.New("redis active types failed")}
		sb := NewSwitchboardWithRegistry(rdb, "gw-r4", NewHub(), catalog, reg)

		err := sb.PublishOrchestration(context.Background(), "simulation:broadcast", map[string]string{"k": "v"})
		require.Error(t, err)
		assert.Contains(t, err.Error(), "active agent types")
	})

	t.Run("nil_registry_routes_AgentEngine_via_dispatchCallable", func(t *testing.T) {
		pathsCh := make(chan string, 2)
		agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/.well-known/agent-card.json" {
				w.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(w).Encode(map[string]interface{}{
					"name":    "planner_ae",
					"url":     "https://example.com/aiplatform.googleapis.com/reasoningEngines/999",
					"version": "1.0.0",
				})
				return
			}
			pathsCh <- r.URL.Path
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{}`))
		}))
		defer agentSrv.Close()

		mr := miniredis.RunT(t)
		rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
		defer rdb.Close()

		catalog := agent.NewCatalog([]string{agentSrv.URL})
		sb := NewSwitchboardWithRegistry(rdb, "gw-r4", NewHub(), catalog, nil).(*RedisSwitchboard)
		sb.gcpClient = &http.Client{Timeout: 5 * time.Second}

		err := sb.PublishOrchestration(context.Background(), "simulation:broadcast", map[string]string{
			"type":      "environment_reset",
			"sessionId": "sess-ae",
		})
		require.NoError(t, err)

		select {
		case gotPath := <-pathsCh:
			assert.Equal(t, "/aiplatform.googleapis.com/reasoningEngines/999/a2a/v1/message:send", gotPath)
		case <-time.After(2 * time.Second):
			t.Fatal("timed out waiting for AE dispatchCallable route")
		}
		sb.Wait()
	})
}

func TestR4_SwitchboardAuthFailClosed_AndHTTP500Poke(t *testing.T) {
	hitCount := 0
	var hitMu sync.Mutex
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hitMu.Lock()
		hitCount++
		hitMu.Unlock()
		_, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("internal agent failure"))
	}))
	defer srv.Close()

	sb := &RedisSwitchboard{
		httpClient:     &http.Client{Timeout: 2 * time.Second},
		callableClient: &http.Client{Timeout: 2 * time.Second},
		gcpClient:      nil,
		gcpClientErr:   errors.New("no ADC"),
	}

	// 1. AE URL with nil gcpClient must fail closed (never send unauthenticated HTTP)
	aeURL := srv.URL + "/aiplatform.googleapis.com/reasoningEngines/123"
	sb.pokeOrchestrationEndpoint(context.Background(), aeURL, "ae-agent", []byte(`{}`))
	sb.dispatchCallable(context.Background(), aeURL, "ae-agent", []byte(`{"sessionId":"s1"}`))

	// 2. Cloud Run URL with failing oidcClientFn must fail closed
	sb.oidcClientFn = func(_ string) (*http.Client, error) {
		return nil, errors.New("no OIDC token")
	}
	sb.pokeOrchestrationEndpoint(context.Background(), "https://agent-xyz.us-central1.run.app", "cr-agent", []byte(`{}`))
	sb.dispatchCallable(context.Background(), "https://agent-xyz.us-central1.run.app", "cr-agent", []byte(`{"sessionId":"s1"}`))

	hitMu.Lock()
	assert.Equal(t, 0, hitCount, "fail-closed auth must prevent unauthenticated HTTP requests")
	hitMu.Unlock()

	// 3. Cloud Run URL with valid oidcClientFn uses the returned OIDC client
	var customOIDCCalled bool
	sb.oidcClientFn = func(audience string) (*http.Client, error) {
		assert.Equal(t, "https://agent-xyz.us-central1.run.app", audience)
		return &http.Client{
			Transport: roundTripperFunc(func(r *http.Request) (*http.Response, error) {
				customOIDCCalled = true
				return &http.Response{
					StatusCode: http.StatusOK,
					Body:       io.NopCloser(strings.NewReader(`{}`)),
					Header:     make(http.Header),
				}, nil
			}),
		}, nil
	}
	sb.pokeOrchestrationEndpoint(context.Background(), "https://agent-xyz.us-central1.run.app", "cr-agent", []byte(`{}`))
	assert.True(t, customOIDCCalled)

	// 4. HTTP 500 poke on local URL logs error cleanly without panic
	sb.pokeOrchestrationEndpoint(context.Background(), srv.URL, "local-agent", []byte(`{"type":"tick"}`))
	hitMu.Lock()
	assert.Equal(t, 1, hitCount)
	hitMu.Unlock()

	// 5. Nil receiver / nil client guards
	var nilSB *RedisSwitchboard
	nilSB.Shutdown()
	require.Error(t, nilSB.Ping(context.Background()))
	require.Error(t, nilSB.EnqueueOrchestration(context.Background(), "q", "v"))
	require.Error(t, nilSB.BatchEnqueueOrchestration(context.Background(), []QueueItem{{Queue: "q", Data: "v"}}))
	require.Error(t, nilSB.PublishOrchestration(context.Background(), "c", "v"))
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestR4_GCPPubSubDrainer_StopsEarlyOnCancelledContext(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	defer lis.Close()

	fake := &fakePubSubSeekServer{}
	grpcSrv := grpc.NewServer()
	pubsubpb.RegisterSubscriberServer(grpcSrv, fake)
	go func() { _ = grpcSrv.Serve(lis) }()
	defer grpcSrv.Stop()

	origFactory := newSubscriptionAdminClient
	newSubscriptionAdminClient = func(ctx context.Context) (*pubsub.SubscriptionAdminClient, error) {
		conn, dialErr := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
		if dialErr != nil {
			return nil, dialErr
		}
		return pubsub.NewSubscriptionAdminClient(ctx, option.WithGRPCConn(conn))
	}
	defer func() { newSubscriptionAdminClient = origFactory }()

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	d := NewGCPPubSubDrainer("proj-1", []string{"sub-1", "sub-2"})
	defer func() { _ = d.Close() }()

	drained, drainErr := d.Drain(ctx)
	assert.Equal(t, 0, drained)
	require.ErrorIs(t, drainErr, context.Canceled)
	assert.Empty(t, fake.snapshot(), "cancelled context must abort loop before calling Seek")
}

func TestR4_HubOwnership_GlobalObserverDoesNotLeakUnsubscribedOrRejectedSims(t *testing.T) {
	validator := func(_ context.Context, userID, simID string) (bool, error) {
		return userID == "user-A" && simID == "sim-owned", nil
	}
	harness := newR4HubHarness(t, WithSimulationOwnershipValidator(validator))

	// 1. Authenticated sessionID=="" client with no simulationId.
	wsA, srvConnA := harness.dial(t, "sessionId=&userId=user-A")
	// 2. Authenticated sessionID=="" client whose initial auto-subscribe to sim-victim is rejected.
	wsRejected, _ := harness.dial(t, "sessionId=&userId=user-A&simulationId=sim-victim")

	gm, data := readTextErrorFrame(t, wsRejected)
	assert.Equal(t, "forbidden", gm.Event)
	assert.Equal(t, "simulation_not_owned", data["error"])
	assert.Equal(t, "sim-victim", data["simulation_id"])

	// Unsubscribed sim-victim messages must NOT be delivered to either connection.
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "test_event",
		SimulationId: "sim-victim",
		SessionId:    "other-session",
	})
	time.Sleep(80 * time.Millisecond)

	// Both connections MUST still receive simID == "" system messages.
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:      "system_notice",
		SessionId: "other-session",
	})
	for _, ws := range []*websocket.Conn{wsA, wsRejected} {
		raw := helperExpectBinaryMessage(t, ws, time.Second)
		var got gateway.Wrapper
		require.NoError(t, proto.Unmarshal(raw, &got))
		assert.Equal(t, "system_notice", got.Type)
		assert.Empty(t, got.SimulationId)
	}

	// Subscribe wsA to owned simulation, verify delivery, then unsubscribe last sim.
	harness.hub.HandleTextMessage(srvConnA, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-owned"}`))
	require.Eventually(t, func() bool {
		harness.hub.mu.RLock()
		defer harness.hub.mu.RUnlock()
		return len(harness.hub.simToClients["sim-owned"]) == 1
	}, time.Second, 10*time.Millisecond)

	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "owned_event",
		SimulationId: "sim-owned",
		SessionId:    "other-session",
	})
	rawOwned := helperExpectBinaryMessage(t, wsA, time.Second)
	var gotOwned gateway.Wrapper
	require.NoError(t, proto.Unmarshal(rawOwned, &gotOwned))
	assert.Equal(t, "sim-owned", gotOwned.SimulationId)

	// Unsubscribe the last simulation -> must NOT revert to unfiltered global-observer passthrough.
	harness.hub.HandleTextMessage(srvConnA, []byte(`{"type":"unsubscribe_simulation","simulation_id":"sim-owned"}`))
	require.Eventually(t, func() bool {
		harness.hub.mu.RLock()
		defer harness.hub.mu.RUnlock()
		return len(harness.hub.simToClients["sim-owned"]) == 0
	}, time.Second, 10*time.Millisecond)

	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "test_event",
		SimulationId: "sim-victim",
		SessionId:    "other-session",
	})
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "test_event",
		SimulationId: "sim-owned",
		SessionId:    "other-session",
	})
	helperExpectNoMessage(t, wsA, 120*time.Millisecond)
	helperExpectNoMessage(t, wsRejected, 120*time.Millisecond)
}

func TestR4_HubOwnership_Phase2DoesNotBroadcastSimulationScopedMessageToArbitrarySessions(t *testing.T) {
	validator := func(_ context.Context, userID, simID string) (bool, error) {
		return (userID == "user-A" && simID == "sim-A") || (userID == "user-B" && simID == "sim-B"), nil
	}
	harness := newR4HubHarness(t, WithSimulationOwnershipValidator(validator))

	// user-A on sess-A (not subscribed to sim-B); user-B on sess-B (auto-subscribed to sim-B).
	wsA, _ := harness.dial(t, "sessionId=sess-A&userId=user-A")
	wsB, _ := harness.dial(t, "sessionId=sess-B&userId=user-B&simulationId=sim-B")

	// Simulation-scoped wrapper with empty targetSessions (no SessionId, Destination, or Origin.SessionId).
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "sim_broadcast_event",
		SimulationId: "sim-B",
	})

	// user-B receives it via Phase 3 (simToClients).
	rawB := helperExpectBinaryMessage(t, wsB, time.Second)
	var gotB gateway.Wrapper
	require.NoError(t, proto.Unmarshal(rawB, &gotB))
	assert.Equal(t, "sim-B", gotB.SimulationId)
	assert.Equal(t, "sim_broadcast_event", gotB.Type)

	// user-A must NOT receive sim-B via Phase 2 broadcast.
	helperExpectNoMessage(t, wsA, 150*time.Millisecond)
}

func TestR4_SwitchboardShutdown_NoWaitGroupRaceWithConcurrentDispatch(t *testing.T) {
	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/.well-known/agent-card.json" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"name":    "simulator",
				"url":     "http://127.0.0.1:9999/",
				"version": "1.0.0",
			})
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer agentSrv.Close()

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	catalog := agent.NewCatalog([]string{agentSrv.URL})

	for iter := 0; iter < 60; iter++ {
		sb := NewSwitchboardWithRegistry(rdb, "gw-race", NewHub(), catalog, nil).(*RedisSwitchboard)
		sb.httpClient = &http.Client{
			Transport: roundTripperFunc(func(_ *http.Request) (*http.Response, error) {
				return &http.Response{
					StatusCode: http.StatusOK,
					Body:       io.NopCloser(strings.NewReader(`{}`)),
					Header:     make(http.Header),
				}, nil
			}),
		}

		start := make(chan struct{})
		var callersWG sync.WaitGroup
		for w := 0; w < 6; w++ {
			callersWG.Add(1)
			go func(worker int) {
				defer callersWG.Done()
				<-start
				switch worker % 3 {
				case 0:
					err := sb.DispatchToAgent(context.Background(), "simulator", map[string]int{"iter": iter})
					if err != nil {
						assert.ErrorIs(t, err, ErrSwitchboardClosed)
					}
				case 1:
					err := sb.PokeAgent(context.Background(), "simulator", map[string]int{"iter": iter})
					if err != nil {
						assert.ErrorIs(t, err, ErrSwitchboardClosed)
					}
				case 2:
					err := sb.PublishOrchestration(context.Background(), "simulation:broadcast", map[string]int{"iter": iter})
					if err != nil {
						assert.ErrorIs(t, err, ErrSwitchboardClosed)
					}
				}
			}(w)
		}

		callersWG.Add(1)
		go func() {
			defer callersWG.Done()
			<-start
			sb.Shutdown()
			sb.Wait()
		}()

		close(start)
		callersWG.Wait()
	}
}

func TestR6_HubSendConnError_GlobalObserverPropagatesTargetWithoutSiblingLeak(t *testing.T) {
	harness := newR4HubHarness(t)
	wsOffender, srvConnOffender := harness.dial(t, "userId=user-A")
	wsSibling, _ := harness.dial(t, "userId=user-A")
	wsOtherTenant, _ := harness.dial(t, "userId=user-B")

	harness.hub.SendConnError(
		srvConnOffender,
		"forbidden",
		"session_not_owned",
		"sim-1",
		"target session is not owned by user",
		"sess-expired-global",
	)

	gm, data := readTextErrorFrame(t, wsOffender)
	assert.Equal(t, "error", gm.Status)
	assert.Equal(t, "error", gm.Type)
	assert.Equal(t, "forbidden", gm.Event)
	assert.Equal(t, []string{"sess-expired-global"}, gm.Destination)
	assert.Equal(t, "session_not_owned", data["error"])
	assert.Equal(t, "sess-expired-global", data["session_id"])
	assert.Equal(t, "sim-1", data["simulation_id"])

	// Sibling global observers (same user and other user) must never receive the error frame.
	for _, siblingWS := range []*websocket.Conn{wsSibling, wsOtherTenant} {
		_ = siblingWS.SetReadDeadline(time.Now().Add(120 * time.Millisecond))
		_, _, err := siblingWS.ReadMessage()
		assert.Error(t, err)
	}
}
