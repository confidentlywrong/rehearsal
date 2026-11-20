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
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestR2_HubLateSubscribeAfterUnregisterDoesNotLeak(t *testing.T) {
	subStore, _ := newTestRedisSubscriptionStore(t)
	h := NewHub()
	h.subStore = subStore

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go h.Run(ctx)

	c := &client{
		hub:       h,
		sessionID: "sess-stale",
		send:      make(chan *wsMessage, 4),
	}

	// Register then immediately unregister the client.
	h.register <- c
	h.unregister <- c

	// Simulate an in-flight readPump sending subscribe + unsubscribe AFTER unregister.
	h.simSubscribe <- simSubscriptionMsg{client: c, simulationID: "sim-zombie"}
	h.simUnsubscribe <- simSubscriptionMsg{client: c, simulationID: "sim-zombie"}

	// Synchronize with the Run loop by registering/unregistering a barrier client.
	barrier := &client{
		hub:       h,
		sessionID: "sess-barrier",
		send:      make(chan *wsMessage, 1),
	}
	h.register <- barrier
	h.unregister <- barrier
	time.Sleep(30 * time.Millisecond)

	h.mu.RLock()
	_, subExists := h.simSubscriptions[c]
	_, revExists := h.simToClients["sim-zombie"]
	_, blockedExists := h.simBlocked[c]
	h.mu.RUnlock()

	assert.False(t, subExists, "unregistered client must not be re-inserted into simSubscriptions")
	assert.False(t, revExists, "unregistered client must not populate simToClients")
	assert.False(t, blockedExists, "unregistered client must not populate simBlocked")

	sessions, err := subStore.Lookup(context.Background(), "sim-zombie")
	require.NoError(t, err)
	assert.Empty(t, sessions, "unregistered client must not persist zombie subscription in Redis")
}

func TestR2_HubPublishAndSendMessageDoNotHangAfterRunExits(t *testing.T) {
	h := NewHubWithConfig(HubConfig{
		BroadcastBuffer:  1,
		SessionMsgBuffer: 1,
		RemoteWorkers:    1,
	})

	ctx, cancel := context.WithCancel(context.Background())
	doneRun := make(chan struct{})
	go func() {
		h.Run(ctx)
		close(doneRun)
	}()

	cancel()
	select {
	case <-doneRun:
	case <-time.After(2 * time.Second):
		t.Fatal("Hub.Run did not exit after context cancellation")
	}

	// Fill channel buffers completely; subsequent Publish / SendMessage calls
	// must select <-h.done instead of blocking forever.
	finished := make(chan struct{})
	go func() {
		for i := 0; i < 5; i++ {
			h.Publish([]byte(`{"type":"after-stop"}`))
			h.SendMessage("sess-1", "text", "narrative", map[string]string{"k": "v"})
		}
		close(finished)
	}()

	select {
	case <-finished:
	case <-time.After(1 * time.Second):
		t.Fatal("Publish or SendMessage hung after Hub.Run exited")
	}
}
