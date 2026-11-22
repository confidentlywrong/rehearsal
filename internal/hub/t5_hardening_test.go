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
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/gen_proto/gateway"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/alicebob/miniredis/v2"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/protobuf/proto"
)

func TestT5_Hub_OwnershipEnforced_BlocksClientBroadcastGlobalLeak(t *testing.T) {
	validator := func(_ context.Context, userID, simID string) (bool, error) {
		return userID == "user-A" && simID == "sim-A", nil
	}
	harness := newR4HubHarness(t, WithSimulationOwnershipValidator(validator))

	wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")
	wsBobSession, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")

	// Client-originated broadcast wrapper with empty SimulationId and empty targetSessions
	// must NOT leak to global observers or arbitrary sessions under ownership enforcement.
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:   "broadcast",
		Event:  "broadcast",
		Origin: &gateway.Origin{Type: "client", Id: "tester-ui", SessionId: ""},
	})

	helperExpectNoMessage(t, wsBobGlobal, 120*time.Millisecond)
	helperExpectNoMessage(t, wsBobSession, 120*time.Millisecond)
}

func TestT5_Hub_OwnershipEnforced_EnvironmentResetIsolation(t *testing.T) {
	validator := func(_ context.Context, userID, simID string) (bool, error) {
		return (userID == "user-A" && simID == "sim-A") || (userID == "user-B" && simID == "sim-B"), nil
	}
	harness := newR4HubHarness(t, WithSimulationOwnershipValidator(validator))

	wsAliceGlobal, _ := harness.dial(t, "sessionId=&userId=user-A")
	wsAliceSession, _ := harness.dial(t, "sessionId=sess-A&userId=user-A")
	wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")
	wsBobSession, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")

	// 1. Unscoped environment_reset (no target_user_id, no Destination) must NOT leak under ownership enforcement.
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:   "environment_reset",
		Event:  "environment_reset",
		Origin: &gateway.Origin{Type: "system", Id: "gw-test"},
	})
	helperExpectNoMessage(t, wsAliceGlobal, 80*time.Millisecond)
	helperExpectNoMessage(t, wsAliceSession, 80*time.Millisecond)
	helperExpectNoMessage(t, wsBobGlobal, 80*time.Millisecond)
	helperExpectNoMessage(t, wsBobSession, 80*time.Millisecond)

	// Re-dial connections after read deadline timeouts.
	wsAliceGlobal2, _ := harness.dial(t, "sessionId=&userId=user-A")
	wsAliceSession2, _ := harness.dial(t, "sessionId=sess-A&userId=user-A")
	wsBobGlobal2, _ := harness.dial(t, "sessionId=&userId=user-B")
	wsBobSession2, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")

	// 2. User-scoped environment_reset delivers ONLY to user-A (both global & session), never user-B.
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:        "environment_reset",
		Event:       "environment_reset",
		Destination: []string{"sess-A"},
		Metadata:    []byte(`{"target_user_id":"user-A"}`),
		Origin:      &gateway.Origin{Type: "system", Id: "gw-test"},
	})

	for _, ws := range []*websocket.Conn{wsAliceGlobal2, wsAliceSession2} {
		raw := helperExpectBinaryMessage(t, ws, time.Second)
		var got gateway.Wrapper
		require.NoError(t, proto.Unmarshal(raw, &got))
		assert.Equal(t, "environment_reset", got.Type)
	}
	helperExpectNoMessage(t, wsBobGlobal2, 120*time.Millisecond)
	helperExpectNoMessage(t, wsBobSession2, 120*time.Millisecond)
}

func TestT5_Switchboard_FlushQueues_ScopedInSaaSMode(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	ctxAlice := account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"})
	ctxBob := account.WithContext(context.Background(), account.UserContext{UserID: "user-bob"})

	require.NoError(t, rdb.RPush(context.Background(), "u:user-alice:simulation:spawns:planner:0", `{"sessionId":"a1"}`).Err())
	require.NoError(t, rdb.RPush(context.Background(), "u:user-bob:simulation:spawns:planner:0", `{"sessionId":"b1"}`).Err())
	require.NoError(t, rdb.RPush(context.Background(), "simulation:spawns:planner:0", `{"sessionId":"legacy"}`).Err())

	sb := NewSwitchboardWithRegistry(rdb, "gw-t5-flush", NewHub(), nil, nil)
	n, err := sb.FlushQueues(ctxAlice)
	require.NoError(t, err)
	assert.Equal(t, 1, n)

	aliceExists, err := rdb.Exists(context.Background(), "u:user-alice:simulation:spawns:planner:0").Result()
	require.NoError(t, err)
	assert.Equal(t, int64(0), aliceExists)

	bobExists, err := rdb.Exists(context.Background(), "u:user-bob:simulation:spawns:planner:0").Result()
	require.NoError(t, err)
	assert.Equal(t, int64(1), bobExists, "Bob's tenant spawn queue must survive Alice's FlushQueues")

	legacyExists, err := rdb.Exists(context.Background(), "simulation:spawns:planner:0").Result()
	require.NoError(t, err)
	assert.Equal(t, int64(1), legacyExists, "Legacy unprefixed spawn queue must survive Alice's FlushQueues")

	_ = ctxBob
}

func TestT5_R9_Switchboard_DispatchCallable_SimregKeyspaceScoping(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	agentSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer agentSrv.Close()

	sb := &RedisSwitchboard{
		client:         rdb,
		gatewayID:      "test-gw-r9",
		localHub:       NewHub(),
		channel:        "gateway:broadcast",
		callableClient: &http.Client{Timeout: 5 * time.Second},
		gcpClient:      &http.Client{Timeout: 5 * time.Second},
	}
	aeURL := agentSrv.URL + "/aiplatform.googleapis.com/reasoningEngines/999"

	t.Run("SAAS_MODE=on scopes simreg by UserContext and payload user_id, fails closed when missing/invalid", func(t *testing.T) {
		t.Setenv("SAAS_MODE", "on")
		t.Setenv("ACCOUNT_KEY_PREFIX", "u")

		// 1. UserContext present -> u:user-alice:simreg:session:sess-alice
		evtAlice, err := json.Marshal(map[string]any{
			"type":      "spawn_agent",
			"sessionId": "sess-alice",
			"payload": map[string]any{
				"agentType":     "simulator",
				"simulation_id": "sim-alice",
				"userId":        "spoofed-bob",
			},
		})
		require.NoError(t, err)
		ctxAlice := account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"})
		sb.dispatchCallable(ctxAlice, aeURL, "simulator", evtAlice)

		val, err := rdb.Get(context.Background(), "u:user-alice:simreg:session:sess-alice").Result()
		require.NoError(t, err)
		assert.Equal(t, "sim-alice", val)
		unscopedExists, _ := rdb.Exists(context.Background(), "simreg:session:sess-alice").Result()
		assert.Equal(t, int64(0), unscopedExists, "must never write unscoped simreg:session in SAAS_MODE=on")

		// 2. No UserContext, valid payload acting_user_id -> u:user-bob:simreg:session:sess-bob
		evtBob, err := json.Marshal(map[string]any{
			"type":      "spawn_agent",
			"sessionId": "sess-bob",
			"payload": map[string]any{
				"agentType":      "simulator",
				"simulation_id":  "sim-bob",
				"acting_user_id": "user-bob",
			},
		})
		require.NoError(t, err)
		sb.dispatchCallable(context.Background(), aeURL, "simulator", evtBob)

		valBob, err := rdb.Get(context.Background(), "u:user-bob:simreg:session:sess-bob").Result()
		require.NoError(t, err)
		assert.Equal(t, "sim-bob", valBob)

		// 3. Missing user_id or colon-injected user_id -> skip unscoped write
		for _, badCase := range []struct {
			sid string
			uid string
		}{
			{"sess-anon", ""},
			{"sess-colon", "bad:user"},
		} {
			evtBad, err := json.Marshal(map[string]any{
				"type":      "spawn_agent",
				"sessionId": badCase.sid,
				"payload": map[string]any{
					"agentType":     "simulator",
					"simulation_id": "sim-bad",
					"user_id":       badCase.uid,
				},
			})
			require.NoError(t, err)
			sb.dispatchCallable(context.Background(), aeURL, "simulator", evtBad)
			exists, _ := rdb.Exists(context.Background(), "simreg:session:"+badCase.sid).Result()
			assert.Equal(t, int64(0), exists)
		}
	})

	t.Run("SAAS_MODE=off writes legacy unscoped simreg:session key", func(t *testing.T) {
		t.Setenv("SAAS_MODE", "off")
		evtLegacy, err := json.Marshal(map[string]any{
			"type":      "spawn_agent",
			"sessionId": "sess-legacy",
			"payload": map[string]any{
				"agentType":     "simulator",
				"simulation_id": "sim-legacy",
			},
		})
		require.NoError(t, err)
		sb.dispatchCallable(context.Background(), aeURL, "simulator", evtLegacy)

		val, err := rdb.Get(context.Background(), "simreg:session:sess-legacy").Result()
		require.NoError(t, err)
		assert.Equal(t, "sim-legacy", val)
	})
}

func TestT5_R10_Hub_GlobalObserver_SimlessAgentRepliesIsolatedBySessionOwnership(t *testing.T) {
	simValidator := func(_ context.Context, userID, simID string) (bool, error) {
		return (userID == "user-A" && simID == "sim-A") || (userID == "user-B" && simID == "sim-B"), nil
	}
	sessionValidator := func(_ context.Context, userID, sessionID string) (bool, error) {
		if userID == "user-err" {
			return false, errors.New("redis unavailable")
		}
		return (userID == "user-A" && sessionID == "sess-A") || (userID == "user-B" && sessionID == "sess-B"), nil
	}

	t.Run("with SessionOwnershipValidator delivers sim-less agent replies only to owning global observer", func(t *testing.T) {
		harness := newR4HubHarness(t,
			WithSimulationOwnershipValidator(simValidator),
			WithSessionOwnershipValidator(sessionValidator),
		)

		wsAliceGlobal, _ := harness.dial(t, "sessionId=&userId=user-A")
		wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")
		wsErrGlobal, _ := harness.dial(t, "sessionId=&userId=user-err")

		// Sim-less agent reply targeting Alice's session sess-A.
		harness.hub.HandleRemoteMessage(&gateway.Wrapper{
			Type:        "json",
			Event:       "narrative",
			Origin:      &gateway.Origin{Type: "agent", Id: "leadership_planner", SessionId: "sess-A"},
			Destination: []string{"sess-A"},
			Payload:     []byte(`{"secret":"tenant-A-confidential"}`),
		})

		rawA := helperExpectBinaryMessage(t, wsAliceGlobal, time.Second)
		var gotA gateway.Wrapper
		require.NoError(t, proto.Unmarshal(rawA, &gotA))
		assert.Equal(t, `{"secret":"tenant-A-confidential"}`, string(gotA.Payload))

		helperExpectNoMessage(t, wsBobGlobal, 120*time.Millisecond)
		helperExpectNoMessage(t, wsErrGlobal, 120*time.Millisecond)

		// Explicit system_notice (Origin == nil) still reaches authenticated global observers.
		wsBobGlobal2, _ := harness.dial(t, "sessionId=&userId=user-B")
		harness.hub.HandleRemoteMessage(&gateway.Wrapper{
			Type:      "system_notice",
			SessionId: "other-session",
		})
		for _, ws := range []*websocket.Conn{wsAliceGlobal, wsBobGlobal2} {
			rawNotice := helperExpectBinaryMessage(t, ws, time.Second)
			var gotNotice gateway.Wrapper
			require.NoError(t, proto.Unmarshal(rawNotice, &gotNotice))
			assert.Equal(t, "system_notice", gotNotice.Type)
		}
	})

	t.Run("without SessionOwnershipValidator fails closed for targeted/agent sim-less wrappers under ownership enforcement", func(t *testing.T) {
		harness := newR4HubHarness(t, WithSimulationOwnershipValidator(simValidator))

		wsAliceGlobal, _ := harness.dial(t, "sessionId=&userId=user-A")
		wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")

		harness.hub.HandleRemoteMessage(&gateway.Wrapper{
			Type:        "json",
			Event:       "narrative",
			Origin:      &gateway.Origin{Type: "agent", Id: "leadership_planner", SessionId: "sess-A"},
			Destination: []string{"sess-A"},
			Payload:     []byte(`{"secret":"must-not-leak-without-session-validator"}`),
		})

		helperExpectNoMessage(t, wsAliceGlobal, 120*time.Millisecond)
		helperExpectNoMessage(t, wsBobGlobal, 120*time.Millisecond)
	})
}

func TestT5_R10_Hub_TargetedClients_NeverReceiveUnscopedAgentBroadcastUnderOwnership(t *testing.T) {
	simValidator := func(_ context.Context, userID, simID string) (bool, error) {
		return userID == "user-A" && simID == "sim-A", nil
	}
	sessionValidator := func(_ context.Context, userID, sessionID string) (bool, error) {
		return (userID == "user-A" && sessionID == "sess-A") || (userID == "user-B" && sessionID == "sess-B"), nil
	}
	harness := newR4HubHarness(t,
		WithSimulationOwnershipValidator(simValidator),
		WithSessionOwnershipValidator(sessionValidator),
	)

	wsAliceSess, _ := harness.dial(t, "sessionId=sess-A&userId=user-A")
	wsBobSess, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")
	wsAliceGlobal, _ := harness.dial(t, "sessionId=&userId=user-A")
	wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")

	// 1. Unscoped agent wrapper (empty Destination, SessionId, Origin.SessionId, SimulationId).
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:    "json",
		Event:   "narrative",
		Origin:  &gateway.Origin{Type: "agent", Id: "planner"},
		Payload: []byte(`{"leak":"unscoped-agent"}`),
	})
	// 2. Unscoped non-system wrapper with Origin == nil.
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:    "json",
		Payload: []byte(`{"leak":"unscoped-nil-origin"}`),
	})

	helperExpectNoMessage(t, wsAliceSess, 100*time.Millisecond)
	helperExpectNoMessage(t, wsBobSess, 100*time.Millisecond)
	helperExpectNoMessage(t, wsAliceGlobal, 100*time.Millisecond)
	helperExpectNoMessage(t, wsBobGlobal, 100*time.Millisecond)

	// 3. Explicit system broadcast (Origin.Type == "system", SimulationId == "") still delivers.
	wsAliceSess2, _ := harness.dial(t, "sessionId=sess-A&userId=user-A")
	wsBobSess2, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")
	wsAliceGlobal2, _ := harness.dial(t, "sessionId=&userId=user-A")
	wsBobGlobal2, _ := harness.dial(t, "sessionId=&userId=user-B")

	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:   "system_notice",
		Origin: &gateway.Origin{Type: "system", Id: "gateway"},
	})
	for _, ws := range []*websocket.Conn{wsAliceSess2, wsBobSess2, wsAliceGlobal2, wsBobGlobal2} {
		raw := helperExpectBinaryMessage(t, ws, time.Second)
		var got gateway.Wrapper
		require.NoError(t, proto.Unmarshal(raw, &got))
		assert.Equal(t, "system_notice", got.Type)
	}
}

func TestT5_R10_Hub_SystemMarker_DeliversBarrierWithoutCrossTenantBypass(t *testing.T) {
	simValidator := func(_ context.Context, userID, simID string) (bool, error) {
		return (userID == "user-A" && simID == "sim-A") || (userID == "user-B" && simID == "sim-B"), nil
	}
	sessionValidator := func(_ context.Context, userID, sessionID string) (bool, error) {
		return (userID == "user-A" && sessionID == "sess-A") || (userID == "user-B" && sessionID == "sess-B"), nil
	}

	t.Run("unscoped nil-origin system_marker delivers barrier to authenticated global observers and targeted session clients", func(t *testing.T) {
		harness := newR4HubHarness(t,
			WithSimulationOwnershipValidator(simValidator),
			WithSessionOwnershipValidator(sessionValidator),
		)

		wsAliceGlobal, _ := harness.dial(t, "sessionId=&userId=user-A")
		wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")
		wsAliceSess, _ := harness.dial(t, "sessionId=sess-A&userId=user-A")
		wsBobSess, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")

		harness.hub.HandleRemoteMessage(&gateway.Wrapper{
			Type:    "system_marker",
			Event:   "system_marker",
			Payload: []byte(`{}`),
		})

		for _, ws := range []*websocket.Conn{wsAliceGlobal, wsBobGlobal, wsAliceSess, wsBobSess} {
			raw := helperExpectBinaryMessage(t, ws, time.Second)
			var got gateway.Wrapper
			require.NoError(t, proto.Unmarshal(raw, &got))
			assert.Equal(t, "system_marker", got.Type)
			assert.Equal(t, "system_marker", got.Event)
		}
	})

	t.Run("simulation-scoped system_marker is not delivered to unsubscribed tenants", func(t *testing.T) {
		harness := newR4HubHarness(t,
			WithSimulationOwnershipValidator(simValidator),
			WithSessionOwnershipValidator(sessionValidator),
		)

		wsAliceSub, _ := harness.dial(t, "sessionId=&userId=user-A&simulationId=sim-A")
		wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")
		wsBobSess, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")

		harness.hub.HandleRemoteMessage(&gateway.Wrapper{
			Type:         "system_marker",
			Event:        "system_marker",
			SimulationId: "sim-A",
			Payload:      []byte(`{}`),
		})

		raw := helperExpectBinaryMessage(t, wsAliceSub, time.Second)
		var got gateway.Wrapper
		require.NoError(t, proto.Unmarshal(raw, &got))
		assert.Equal(t, "system_marker", got.Type)
		assert.Equal(t, "sim-A", got.SimulationId)

		helperExpectNoMessage(t, wsBobGlobal, 120*time.Millisecond)
		helperExpectNoMessage(t, wsBobSess, 120*time.Millisecond)
	})

	t.Run("agent-origin unscoped system_marker is never delivered across tenants", func(t *testing.T) {
		harness := newR4HubHarness(t,
			WithSimulationOwnershipValidator(simValidator),
			WithSessionOwnershipValidator(sessionValidator),
		)

		wsAliceGlobal, _ := harness.dial(t, "sessionId=&userId=user-A")
		wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")
		wsAliceSess, _ := harness.dial(t, "sessionId=sess-A&userId=user-A")
		wsBobSess, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")

		harness.hub.HandleRemoteMessage(&gateway.Wrapper{
			Type:    "system_marker",
			Event:   "system_marker",
			Origin:  &gateway.Origin{Type: "agent", Id: "planner"},
			Payload: []byte(`{}`),
		})

		helperExpectNoMessage(t, wsAliceGlobal, 120*time.Millisecond)
		helperExpectNoMessage(t, wsBobGlobal, 120*time.Millisecond)
		helperExpectNoMessage(t, wsAliceSess, 120*time.Millisecond)
		helperExpectNoMessage(t, wsBobSess, 120*time.Millisecond)
	})

	t.Run("target_user_id metadata scopes system_marker exclusively to target user", func(t *testing.T) {
		harness := newR4HubHarness(t,
			WithSimulationOwnershipValidator(simValidator),
			WithSessionOwnershipValidator(sessionValidator),
		)

		wsAliceGlobal, _ := harness.dial(t, "sessionId=&userId=user-A")
		wsAliceSess, _ := harness.dial(t, "sessionId=sess-A&userId=user-A")
		wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-B")
		wsBobSess, _ := harness.dial(t, "sessionId=sess-B&userId=user-B")

		harness.hub.HandleRemoteMessage(&gateway.Wrapper{
			Type:     "system_marker",
			Event:    "system_marker",
			Metadata: []byte(`{"target_user_id":"user-A"}`),
			Payload:  []byte(`{}`),
		})

		for _, ws := range []*websocket.Conn{wsAliceGlobal, wsAliceSess} {
			raw := helperExpectBinaryMessage(t, ws, time.Second)
			var got gateway.Wrapper
			require.NoError(t, proto.Unmarshal(raw, &got))
			assert.Equal(t, "system_marker", got.Type)
		}
		helperExpectNoMessage(t, wsBobGlobal, 120*time.Millisecond)
		helperExpectNoMessage(t, wsBobSess, 120*time.Millisecond)
	})
}

func TestT5_R12_RedisSubscriptionStore_TenantIsolation(t *testing.T) {
	store, _ := newTestRedisSubscriptionStore(t)
	ctx := context.Background()

	require.NoError(t, store.SubscribeForUser(ctx, "user-alice", "sim-shared-hijack", "sess-alice"))
	require.NoError(t, store.SubscribeForUser(ctx, "user-bob", "sim-shared-hijack", "sess-bob"))

	aliceSubs, err := store.LookupForUser(ctx, "user-alice", "sim-shared-hijack")
	require.NoError(t, err)
	assert.Equal(t, []string{"sess-alice"}, aliceSubs)

	bobSubs, err := store.LookupForUser(ctx, "user-bob", "sim-shared-hijack")
	require.NoError(t, err)
	assert.Equal(t, []string{"sess-bob"}, bobSubs)

	// Legacy unscoped Lookup must not return tenant-scoped subscriptions.
	unscopedSubs, err := store.Lookup(ctx, "sim-shared-hijack")
	require.NoError(t, err)
	assert.Empty(t, unscopedSubs)

	// Unsubscribing Alice must leave Bob intact.
	require.NoError(t, store.UnsubscribeAllForUser(ctx, "user-alice", "sess-alice"))
	aliceAfter, err := store.LookupForUser(ctx, "user-alice", "sim-shared-hijack")
	require.NoError(t, err)
	assert.Empty(t, aliceAfter)

	bobAfter, err := store.LookupForUser(ctx, "user-bob", "sim-shared-hijack")
	require.NoError(t, err)
	assert.Equal(t, []string{"sess-bob"}, bobAfter)

	// Colon injection in user/sim/session keys is rejected.
	require.Error(t, store.SubscribeForUser(ctx, "bad:user", "sim-1", "sess-1"))
	require.Error(t, store.SubscribeForUser(ctx, "user-alice", "sim:colon", "sess-1"))
	require.Error(t, store.SubscribeForUser(ctx, "user-alice", "sim-1", "sess:colon"))
}

func TestT5_R12_Hub_CrossTenantSharedSimulationID_BlockedAcrossSwitchboard(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("ACCOUNT_KEY_PREFIX", "u")

	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	simValidator := func(_ context.Context, userID, simID string) (bool, error) {
		return (userID == "user-alice" || userID == "user-bob") && simID == "sim-shared-hijack", nil
	}
	sessionValidator := func(_ context.Context, userID, sessionID string) (bool, error) {
		return (userID == "user-alice" && sessionID == "sess-alice") ||
			(userID == "user-bob" && sessionID == "sess-bob"), nil
	}

	subStore := NewRedisSubscriptionStore(rdb)
	hubA := NewHub(
		WithSubscriptionStore(subStore),
		WithSimulationOwnershipValidator(simValidator),
		WithSessionOwnershipValidator(sessionValidator),
		WithRequireSimulationOwnership(true),
	)
	hubB := NewHub(
		WithSubscriptionStore(subStore),
		WithSimulationOwnershipValidator(simValidator),
		WithSessionOwnershipValidator(sessionValidator),
		WithRequireSimulationOwnership(true),
	)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go hubA.Run(ctx)
	go hubB.Run(ctx)

	sbA := NewSwitchboardWithRegistry(rdb, "gw-a", hubA, nil, nil).(*RedisSwitchboard)
	sbB := NewSwitchboardWithRegistry(rdb, "gw-b", hubB, nil, nil).(*RedisSwitchboard)
	go func() { _ = sbA.Start(ctx) }()
	go func() { _ = sbB.Start(ctx) }()
	time.Sleep(120 * time.Millisecond)

	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	dialOn := func(h *Hub, userID, sessionID, simID string) (*websocket.Conn, *websocket.Conn) {
		var srvConn *websocket.Conn
		ready := make(chan struct{})
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			c, err := upgrader.Upgrade(w, r, nil)
			require.NoError(t, err)
			srvConn = c
			if simID != "" {
				h.RegisterWithUser(sessionID, c, userID, simID)
			} else {
				h.RegisterWithUser(sessionID, c, userID)
			}
			close(ready)
		}))
		t.Cleanup(srv.Close)

		wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
		ws, resp, err := websocket.DefaultDialer.Dial(wsURL, nil)
		require.NoError(t, err)
		if resp != nil && resp.Body != nil {
			_ = resp.Body.Close()
		}
		t.Cleanup(func() { _ = ws.Close() })
		<-ready
		return ws, srvConn
	}

	// Alice on Hub A (both sim auto-subscribed and global + subscribe_simulation)
	wsAliceSim, _ := dialOn(hubA, "user-alice", "sess-alice", "sim-shared-hijack")
	wsAliceGlobal, srvAliceGlobal := dialOn(hubA, "user-alice", "", "")
	hubA.SubscribeSimulation(srvAliceGlobal, "sim-shared-hijack")

	// Bob on Hub B (both sim auto-subscribed and global + subscribe_simulation)
	wsBobSim, _ := dialOn(hubB, "user-bob", "sess-bob", "sim-shared-hijack")
	wsBobGlobal, srvBobGlobal := dialOn(hubB, "user-bob", "", "")
	hubB.SubscribeSimulation(srvBobGlobal, "sim-shared-hijack")

	// Also connect Bob on Hub A (same-instance co-tenant sharing simulation_id)
	wsBobSameHub, _ := dialOn(hubA, "user-bob", "sess-bob-same", "sim-shared-hijack")

	time.Sleep(100 * time.Millisecond)

	// 1. Switchboard.Broadcast with Alice's UserContext -> routes via u:user-alice:gateway:broadcast
	ctxAlice := account.WithContext(context.Background(), account.UserContext{UserID: "user-alice"})
	require.NoError(t, sbA.Broadcast(ctxAlice, &gateway.Wrapper{
		Type:         "json",
		Event:        "narrative",
		SimulationId: "sim-shared-hijack",
		SessionId:    "sess-alice",
		Destination:  []string{"sess-alice"},
		Payload:      []byte(`{"text":"TOP_SECRET_ALICE_1"}`),
	}))

	// 2. Python-style publish directly to u:user-alice:gateway:broadcast with NO Destination/SessionId
	rawSimOnly, err := proto.Marshal(&gateway.Wrapper{
		Type:         "json",
		Event:        "narrative",
		SimulationId: "sim-shared-hijack",
		Payload:      []byte(`{"text":"TOP_SECRET_ALICE_2"}`),
	})
	require.NoError(t, err)
	require.NoError(t, rdb.Publish(ctx, "u:user-alice:gateway:broadcast", rawSimOnly).Err())

	// 3. Unprefixed gateway:broadcast carrying Alice's target session -> still blocked for Bob
	rawUnprefixed, err := proto.Marshal(&gateway.Wrapper{
		Type:         "json",
		Event:        "narrative",
		SimulationId: "sim-shared-hijack",
		Destination:  []string{"sess-alice"},
		Payload:      []byte(`{"text":"TOP_SECRET_ALICE_3"}`),
	})
	require.NoError(t, err)
	require.NoError(t, rdb.Publish(ctx, "gateway:broadcast", rawUnprefixed).Err())

	// 4. Malformed channel prefix must be dropped cleanly
	require.NoError(t, rdb.Publish(ctx, "u::gateway:broadcast", rawSimOnly).Err())
	require.NoError(t, rdb.Publish(ctx, "a:b:c:gateway:broadcast", rawSimOnly).Err())

	for _, aliceConn := range []*websocket.Conn{wsAliceSim, wsAliceGlobal} {
		for _, want := range []string{"TOP_SECRET_ALICE_1", "TOP_SECRET_ALICE_2", "TOP_SECRET_ALICE_3"} {
			raw := helperExpectBinaryMessage(t, aliceConn, 2*time.Second)
			var got gateway.Wrapper
			require.NoError(t, proto.Unmarshal(raw, &got))
			assert.Contains(t, string(got.Payload), want)
		}
	}

	// Neither cross-instance Bob nor same-hub Bob may receive ANY of Alice's frames.
	helperExpectNoMessage(t, wsBobSim, 200*time.Millisecond)
	helperExpectNoMessage(t, wsBobGlobal, 200*time.Millisecond)
	helperExpectNoMessage(t, wsBobSameHub, 200*time.Millisecond)
}

func TestR4_TargetedOwnedSessionWithSimulationID_DeliversOnlyToOwnerGlobalWS(t *testing.T) {
	sessionValidator := func(_ context.Context, userID, sessionID string) (bool, error) {
		return (userID == "user-alice" && sessionID == "sess-alice-planner") ||
			(userID == "user-bob" && sessionID == "sess-bob-planner"), nil
	}
	simValidator := func(_ context.Context, _ string, _ string) (bool, error) {
		return false, nil
	}

	harness := newR4HubHarness(t,
		WithSimulationOwnershipValidator(simValidator),
		WithSessionOwnershipValidator(sessionValidator),
		WithRequireSimulationOwnership(true),
	)

	// Unsubscribed global WS connections (browser connects with sessionId="")
	wsAliceGlobal, _ := harness.dial(t, "sessionId=&userId=user-alice")
	wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-bob")

	// 1. Agent message with SimulationId targeted at Alice's owned planner session MUST reach wsAliceGlobal
	//    even before wsAliceGlobal subscribes to sim-new-123 (breaking the start_simulation catch-22).
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "json",
		Event:        "tool_end",
		SimulationId: "sim-new-123",
		Origin:       &gateway.Origin{Type: "agent", Id: "leadership_planner_with_memory", SessionId: "sess-alice-planner"},
		Destination:  []string{"sess-alice-planner"},
		Payload:      []byte(`{"tool_name":"start_simulation"}`),
	})

	raw := helperExpectBinaryMessage(t, wsAliceGlobal, 2*time.Second)
	var got gateway.Wrapper
	require.NoError(t, proto.Unmarshal(raw, &got))
	assert.Equal(t, "sim-new-123", got.SimulationId)
	assert.Contains(t, string(got.Payload), "start_simulation")

	// Bob must NEVER see Alice's targeted session message.
	helperExpectNoMessage(t, wsBobGlobal, 120*time.Millisecond)

	// 2. Unscoped (Destination empty) simulation message must STILL NOT reach unsubscribed wsAliceGlobal.
	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "json",
		Event:        "tool_end",
		SimulationId: "sim-new-123",
		Origin:       &gateway.Origin{Type: "agent", Id: "simulator"},
		Payload:      []byte(`{"tool_name":"advance_tick"}`),
	})
	helperExpectNoMessage(t, wsAliceGlobal, 120*time.Millisecond)
}

func TestR4_SubscribedSimulatorOriginSessionEqualsSimulationID_DeliversOnlyToOwner(t *testing.T) {
	sessionValidator := func(_ context.Context, userID, sessionID string) (bool, error) {
		// Simulator's Origin.SessionId equals simID ("sim-owner-only") and is NOT in session table.
		return false, nil
	}
	simValidator := func(_ context.Context, userID, simulationID string) (bool, error) {
		return userID == "user-alice" && simulationID == "sim-owner-only", nil
	}

	harness := newR4HubHarness(t,
		WithSimulationOwnershipValidator(simValidator),
		WithSessionOwnershipValidator(sessionValidator),
		WithRequireSimulationOwnership(true),
	)

	wsAliceSub, _ := harness.dial(t, "sessionId=&userId=user-alice&simulationId=sim-owner-only")
	wsBobGlobal, _ := harness.dial(t, "sessionId=&userId=user-bob")

	harness.hub.HandleRemoteMessage(&gateway.Wrapper{
		Type:         "json",
		Event:        "tool_end",
		SimulationId: "sim-owner-only",
		Origin:       &gateway.Origin{Type: "agent", Id: "tick", SessionId: "sim-owner-only"},
		Payload:      []byte(`{"tool_name":"advance_tick"}`),
	})

	raw := helperExpectBinaryMessage(t, wsAliceSub, 2*time.Second)
	var got gateway.Wrapper
	require.NoError(t, proto.Unmarshal(raw, &got))
	assert.Contains(t, string(got.Payload), "advance_tick")

	helperExpectNoMessage(t, wsBobGlobal, 120*time.Millisecond)
}
