// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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
	"fmt"
	"log"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/gen_proto/gateway"
	"github.com/gorilla/websocket"
	"google.golang.org/protobuf/proto"
)

// droppedMsgCount tracks dropped messages across all hot paths.
// Logged every 100 drops to avoid flooding logs during high-throughput
// simulations (Bug #42).
var droppedMsgCount atomic.Int64

// HubConfig holds tunable buffer sizes for the Hub.
// Zero values are replaced with defaults.
type HubConfig struct {
	BroadcastBuffer  int // Default: 100000
	SessionMsgBuffer int // Default: 100000
	RemoteMsgBuffer  int // Default: 100000
	ClientSendBuffer int // Default: 256
	RemoteWorkers    int // Default: 4
}

const defaultBufferSize = 100000
const defaultClientSendBufferSize = 256

// remoteMessage bundles a gateway.Wrapper with optional pre-serialized bytes.
// When raw is non-nil (e.g., bytes from Redis PubSub), Hub.Run() uses them
// directly instead of re-marshaling, eliminating a redundant proto.Marshal.
type remoteMessage struct {
	wrapper      *gateway.Wrapper
	raw          []byte // pre-serialized protobuf from Redis; nil = must marshal
	tenantUserID string // verified tenant user_id from Redis channel prefix ("" = unprefixed/direct)
	fromChannel  bool   // true when dispatched via Switchboard PubSub channel
}

// simSubscriptionMsg carries a simulation subscription request through a channel.
type simSubscriptionMsg struct {
	client       *client
	simulationID string
}

// SimulationOwnershipValidator reports whether userID is authorized to
// subscribe to simulationID. Returning (false, nil) denies as unauthorized;
// returning a non-nil error fails closed.
type SimulationOwnershipValidator func(ctx context.Context, userID, simulationID string) (bool, error)

// SessionOwnershipValidator reports whether userID is authorized to receive
// messages targeting sessionID. Returning (false, nil) denies as unauthorized;
// returning a non-nil error fails closed.
type SessionOwnershipValidator func(ctx context.Context, userID, sessionID string) (bool, error)

// Hub maintains the set of active clients and routes messages to them.
// Patterned after agentic-citizen's reliable routing hub.
type Hub struct {
	// Map of sessionID to a map of active connections and their send buffers.
	clients    map[string]map[*client]bool
	broadcast  chan []byte
	sessionMsg chan SessionMessage
	register   chan *client
	unregister chan *client
	mu         sync.RWMutex
	remoteMsg  chan *remoteMessage
	config     HubConfig

	// Simulation subscription routing
	simSubscriptions map[*client]map[string]bool // client -> set of simulation IDs (allowlist)
	simToClients     map[string]map[*client]bool // simID -> set of clients (reverse index)
	simBlocked       map[*client]map[string]bool // client -> set of blocked simulation IDs
	simSubscribe     chan simSubscriptionMsg
	simUnsubscribe   chan simSubscriptionMsg
	connToClient     map[*websocket.Conn]*client // reverse index for text message handling

	// Simulation & session ownership enforcement
	simOwnerValidator     SimulationOwnershipValidator
	sessionOwnerValidator SessionOwnershipValidator
	requireSimOwnership   bool

	// Lifecycle context derived from Run(ctx) for background store operations
	lifecycleMu  sync.RWMutex
	lifecycleCtx context.Context
	subWG        sync.WaitGroup

	// Cross-instance subscription persistence (Redis-backed)
	subStore SubscriptionStore

	// Closed when Run(ctx) exits so hijacked WebSocket goroutines never
	// deadlock sending on unbuffered registration/subscription channels.
	done      chan struct{}
	closeOnce sync.Once
}

type client struct {
	hub              *Hub
	sessionID        string
	userID           string
	conn             *websocket.Conn
	send             chan *wsMessage
	closed           atomic.Bool // set before close(send) to prevent send-on-closed-channel panics
	autoSubscribeSim string      // simulation ID to auto-subscribe on register (empty = none)
}

// safeSend attempts a non-blocking send to the client's send channel.
// Returns true if the message was enqueued, false if the client is
// closed or the buffer is full.
func (c *client) safeSend(msg *wsMessage) bool {
	if c.closed.Load() {
		return false
	}
	select {
	case c.send <- msg:
		return true
	default:
		// Forcefully close the connection if the buffer is full.
		// This forces the slow client to reconnect and perform a clean state-resync,
		// preventing silent UI desyncs.
		if c.conn != nil {
			_ = c.conn.Close()
		}
		return false
	}
}

type wsMessage struct {
	mType int
	data  []byte
}

type Registration struct {
	SessionID string
	Conn      *websocket.Conn
}

type SessionMessage struct {
	SessionID string
	Data      []byte
}

// GatewayOrigin identifies the source of a message in the system.
type GatewayOrigin struct {
	Type      string `json:"type"`                 // "agent", "client", "system"
	ID        string `json:"id"`                   // "planner", "tester-ui", "gateway"
	SessionID string `json:"session_id,omitempty"` // UUID
}

// GatewayMessage is the standardized JSON format sent over WebSockets.
type GatewayMessage struct {
	Origin      GatewayOrigin          `json:"origin"`
	Destination []string               `json:"destination"` // Array of target session UUIDs. Empty means broadcast.
	Status      string                 `json:"status"`      // e.g., "success", "error", "info"
	Type        string                 `json:"type"`        // "text", "json", "a2ui"
	Event       string                 `json:"event"`       // e.g., "narrative", "model_start", "tool_call"
	Data        interface{}            `json:"data"`
	Metadata    map[string]interface{} `json:"metadata,omitempty"`
}

func bufSize(v, fallback int) int {
	if v > 0 {
		return v
	}
	return fallback
}

// HubOption is a functional option for configuring the Hub.
type HubOption func(*Hub)

// WithSubscriptionStore sets the cross-instance subscription store.
// When nil or not provided, a NullSubscriptionStore is used.
func WithSubscriptionStore(store SubscriptionStore) HubOption {
	return func(h *Hub) {
		if store != nil {
			h.subStore = store
		}
	}
}

// WithRemoteWorkers sets the number of goroutines that process inbound
// Redis messages before fan-out to WebSocket clients. Default is 4.
// Higher values improve throughput at 1000+ concurrent sessions.
func WithRemoteWorkers(n int) HubOption {
	return func(h *Hub) {
		if n > 0 {
			h.config.RemoteWorkers = n
		}
	}
}

// WithSimulationOwnershipValidator sets the validator used to verify user
// ownership before subscribing a client to a simulation. Passing a non-nil
// validator also enables fail-closed ownership enforcement.
func WithSimulationOwnershipValidator(fn SimulationOwnershipValidator) HubOption {
	return func(h *Hub) {
		h.simOwnerValidator = fn
		if fn != nil {
			h.requireSimOwnership = true
		}
	}
}

// WithRequireSimulationOwnership configures whether simulation subscriptions
// must fail closed when no ownership validator is configured.
func WithRequireSimulationOwnership(required bool) HubOption {
	return func(h *Hub) {
		h.requireSimOwnership = required
	}
}

// SetSimulationOwnershipValidator updates the simulation ownership validator
// on an existing Hub safely at runtime.
func (h *Hub) SetSimulationOwnershipValidator(fn SimulationOwnershipValidator) {
	if h == nil {
		return
	}
	h.mu.Lock()
	h.simOwnerValidator = fn
	h.requireSimOwnership = fn != nil || h.sessionOwnerValidator != nil
	h.mu.Unlock()
}

// WithSessionOwnershipValidator sets the validator used to verify user
// ownership of target sessions before delivering simulation-less wrappers to
// global observers. Passing a non-nil validator also enables fail-closed
// ownership enforcement.
func WithSessionOwnershipValidator(fn SessionOwnershipValidator) HubOption {
	return func(h *Hub) {
		h.sessionOwnerValidator = fn
		if fn != nil {
			h.requireSimOwnership = true
		}
	}
}

// SetSessionOwnershipValidator updates the session ownership validator
// on an existing Hub safely at runtime.
func (h *Hub) SetSessionOwnershipValidator(fn SessionOwnershipValidator) {
	if h == nil {
		return
	}
	h.mu.Lock()
	h.sessionOwnerValidator = fn
	if fn != nil {
		h.requireSimOwnership = true
	}
	h.mu.Unlock()
}

func (h *Hub) baseContext() context.Context {
	if h == nil {
		return context.Background()
	}
	h.lifecycleMu.RLock()
	ctx := h.lifecycleCtx
	h.lifecycleMu.RUnlock()
	if ctx != nil {
		return ctx
	}
	return context.Background()
}

// NewHub creates a Hub with default buffer sizes.
func NewHub(opts ...HubOption) *Hub {
	return NewHubWithConfig(HubConfig{}, opts...)
}

// NewHubWithConfig creates a Hub with the given buffer configuration.
func NewHubWithConfig(cfg HubConfig, opts ...HubOption) *Hub {
	h := &Hub{
		broadcast:        make(chan []byte, bufSize(cfg.BroadcastBuffer, defaultBufferSize)),
		sessionMsg:       make(chan SessionMessage, bufSize(cfg.SessionMsgBuffer, defaultBufferSize)),
		register:         make(chan *client),
		unregister:       make(chan *client),
		clients:          make(map[string]map[*client]bool),
		remoteMsg:        make(chan *remoteMessage, bufSize(cfg.RemoteMsgBuffer, defaultBufferSize)),
		config:           cfg,
		simSubscriptions: make(map[*client]map[string]bool),
		simToClients:     make(map[string]map[*client]bool),
		simBlocked:       make(map[*client]map[string]bool),
		simSubscribe:     make(chan simSubscriptionMsg),
		simUnsubscribe:   make(chan simSubscriptionMsg),
		connToClient:     make(map[*websocket.Conn]*client),
		subStore:         &NullSubscriptionStore{},
		done:             make(chan struct{}),
	}
	for _, opt := range opts {
		opt(h)
	}
	return h
}

func (h *Hub) HandleRemoteMessage(wrapper *gateway.Wrapper, rawBytes ...[]byte) {
	h.handleRemoteMessage("", false, wrapper, rawBytes...)
}

// HandleRemoteMessageForUser enqueues a remote Wrapper associated with a
// verified tenant user ID parsed from the Redis broadcast channel.
func (h *Hub) HandleRemoteMessageForUser(tenantUserID string, wrapper *gateway.Wrapper, rawBytes ...[]byte) {
	h.handleRemoteMessage(strings.TrimSpace(tenantUserID), true, wrapper, rawBytes...)
}

func (h *Hub) handleRemoteMessage(tenantUserID string, fromChannel bool, wrapper *gateway.Wrapper, rawBytes ...[]byte) {
	if h == nil || wrapper == nil {
		return
	}
	// Per-message log removed (Bug #42): fires on every Redis message,
	// flooding logs with 1000+ runners.
	rm := &remoteMessage{
		wrapper:      wrapper,
		tenantUserID: tenantUserID,
		fromChannel:  fromChannel,
	}
	if len(rawBytes) > 0 && rawBytes[0] != nil {
		rm.raw = rawBytes[0]
	}
	select {
	case <-h.done:
		return
	case h.remoteMsg <- rm:
	default:
		if n := droppedMsgCount.Add(1); n%100 == 1 {
			log.Printf("Hub: HandleRemoteMessage dropped (remoteMsg buffer full) [total drops: %d]", n)
		}
	}
}

// Register adds a WebSocket connection to the Hub for message routing.
// An optional simulationID triggers auto-subscription on registration,
// avoiding the race between Register and SubscribeSimulation.
func (h *Hub) Register(sessionID string, conn *websocket.Conn, simulationID ...string) {
	h.RegisterWithUser(sessionID, conn, "", simulationID...)
}

// RegisterWithUser adds an authenticated WebSocket connection (associated with
// userID) to the Hub for message routing. When an optional simulationID is
// supplied and ownership verification is enabled or userID is non-empty,
// ownership is validated before auto-subscribing; unauthorized attempts receive
// an explicit WebSocket error frame without subscribing.
func (h *Hub) RegisterWithUser(sessionID string, conn *websocket.Conn, userID string, simulationID ...string) {
	if h == nil {
		return
	}
	c := &client{
		hub:       h,
		sessionID: sessionID,
		userID:    userID,
		conn:      conn,
		send:      make(chan *wsMessage, bufSize(h.config.ClientSendBuffer, defaultClientSendBufferSize)),
	}
	var autoSim string
	if len(simulationID) > 0 {
		autoSim = strings.TrimSpace(simulationID[0])
	}
	if autoSim != "" {
		h.mu.RLock()
		validator := h.simOwnerValidator
		requireOwnership := h.requireSimOwnership
		h.mu.RUnlock()
		if validator != nil || requireOwnership || userID != "" {
			if errSpec := h.checkSimulationOwnership(validator, requireOwnership, userID, autoSim); errSpec != nil {
				autoSim = ""
				if errMsg, err := buildClientErrorMessage(c.sessionID, errSpec.event, errSpec.code, errSpec.simulationID, errSpec.details); err == nil {
					// c.send is freshly allocated and unreachable by any other goroutine yet.
					c.safeSend(errMsg)
				}
			}
		}
	}
	c.autoSubscribeSim = autoSim
	select {
	case h.register <- c:
		go c.writePump()
	case <-h.done:
		if conn != nil {
			_ = conn.Close()
		}
	}
}

func (h *Hub) Unregister(sessionID string, conn *websocket.Conn) {
	h.mu.RLock()
	if conns, ok := h.clients[sessionID]; ok {
		for c := range conns {
			if c.conn == conn {
				h.mu.RUnlock()
				select {
				case h.unregister <- c:
				case <-h.done:
				}
				return
			}
		}
	}
	h.mu.RUnlock()
}

func (h *Hub) Broadcast() chan<- []byte {
	return h.broadcast
}

// BroadcastQueueLen returns the current number of pending broadcast messages.
func (h *Hub) BroadcastQueueLen() int {
	return len(h.broadcast)
}

// Publish injects a raw payload into the broadcast stream.
// It automatically appends a newline for NDJ (Newline-Delimited JSON) support.
func (h *Hub) Publish(data []byte) {
	out := make([]byte, len(data)+1)
	copy(out, data)
	out[len(data)] = '\n'
	select {
	case h.broadcast <- out:
	case <-h.done:
	}
}

func (h *Hub) SessionMsg() chan<- SessionMessage {
	return h.sessionMsg
}

func (h *Hub) Run(ctx context.Context) {
	if ctx == nil {
		ctx = context.Background()
	}
	h.lifecycleMu.Lock()
	h.lifecycleCtx = ctx
	h.lifecycleMu.Unlock()

	// Start worker pool for remote message processing.
	// Workers read directly from h.remoteMsg, which acts as a work queue.
	numWorkers := h.config.RemoteWorkers
	if numWorkers <= 0 {
		numWorkers = 4
	}
	var workerWG sync.WaitGroup
	for i := 0; i < numWorkers; i++ {
		workerWG.Add(1)
		go func() {
			defer workerWG.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case <-h.done:
					return
				case rm, ok := <-h.remoteMsg:
					if !ok {
						return
					}
					h.processRemoteMessage(ctx, rm)
				}
			}
		}()
	}

	for {
		select {
		case <-ctx.Done():
			h.closeOnce.Do(func() { close(h.done) })
			workerWG.Wait()
			h.subWG.Wait()
			h.mu.Lock()
			for sid, conns := range h.clients {
				for c := range conns {
					if !c.closed.Swap(true) {
						close(c.send)
					}
					if c.conn != nil {
						_ = c.conn.Close()
					}
				}
				delete(h.clients, sid)
			}
			clear(h.simSubscriptions)
			clear(h.simToClients)
			clear(h.simBlocked)
			clear(h.connToClient)
			h.mu.Unlock()
			return
		case c := <-h.register:
			h.mu.Lock()
			if h.clients[c.sessionID] == nil {
				h.clients[c.sessionID] = make(map[*client]bool)
			}
			h.clients[c.sessionID][c] = true
			h.connToClient[c.conn] = c

			// Process auto-subscription inline (same Run() tick) to avoid
			// the race between Register and SubscribeSimulation. We mutate
			// the maps directly since we hold the write lock.
			if c.autoSubscribeSim != "" {
				simID := c.autoSubscribeSim
				if h.simSubscriptions[c] == nil {
					h.simSubscriptions[c] = make(map[string]bool)
				}
				h.simSubscriptions[c][simID] = true
				if h.simToClients[simID] == nil {
					h.simToClients[simID] = make(map[*client]bool)
				}
				h.simToClients[simID][c] = true
				sessionID := c.sessionID
				userID := c.userID
				h.subWG.Add(1)
				go func() {
					defer h.subWG.Done()
					subCtx, cancel := context.WithTimeout(h.baseContext(), subscriptionTimeout)
					defer cancel()
					logSubscriptionError("Subscribe", h.subscribeStore(subCtx, userID, simID, sessionID))
				}()
				log.Printf("Client auto-subscribed to simulation %s", simID)
			}
			h.mu.Unlock()
			log.Printf("Client registered for session %s", c.sessionID)

		case c := <-h.unregister:
			h.mu.Lock()
			lastClientForUserSession := false
			userID := c.userID
			if conns, ok := h.clients[c.sessionID]; ok {
				if _, ok := conns[c]; ok {
					delete(conns, c)
					if !c.closed.Swap(true) {
						close(c.send)
					}
				}
				if len(conns) == 0 {
					delete(h.clients, c.sessionID)
					lastClientForUserSession = true
				} else {
					lastClientForUserSession = true
					for peer := range conns {
						if peer.userID == userID {
							lastClientForUserSession = false
							break
						}
					}
				}
			}
			// Clean up reverse index entries for this client
			if subs, ok := h.simSubscriptions[c]; ok {
				for simID := range subs {
					if clients, ok := h.simToClients[simID]; ok {
						delete(clients, c)
						if len(clients) == 0 {
							delete(h.simToClients, simID)
						}
					}
				}
			}
			delete(h.simSubscriptions, c)
			delete(h.simBlocked, c)
			delete(h.connToClient, c.conn)
			sessionID := c.sessionID
			h.mu.Unlock()
			// Only tear down cross-instance Redis subscriptions for sessionID when
			// its last local WebSocket client disconnects; sibling tabs sharing the
			// same sessionID must continue receiving simulation updates.
			if lastClientForUserSession {
				h.subWG.Add(1)
				go func() {
					defer h.subWG.Done()
					subCtx, cancel := context.WithTimeout(h.baseContext(), subscriptionTimeout)
					defer cancel()
					logSubscriptionError("UnsubscribeAll", h.unsubscribeAllStore(subCtx, userID, sessionID))
				}()
			}
			log.Printf("Client unregistered from session %s", sessionID)

		case sub := <-h.simSubscribe:
			h.mu.Lock()
			if conns, ok := h.clients[sub.client.sessionID]; !ok || !conns[sub.client] {
				h.mu.Unlock()
				continue
			}
			if h.simSubscriptions[sub.client] == nil {
				h.simSubscriptions[sub.client] = make(map[string]bool)
			}
			h.simSubscriptions[sub.client][sub.simulationID] = true
			// Reverse index
			if h.simToClients[sub.simulationID] == nil {
				h.simToClients[sub.simulationID] = make(map[*client]bool)
			}
			h.simToClients[sub.simulationID][sub.client] = true
			// Clear blocklist — client is now in allowlist mode
			delete(h.simBlocked, sub.client)
			sessionID := sub.client.sessionID
			userID := sub.client.userID
			simID := sub.simulationID
			h.mu.Unlock()
			// Async Redis persistence — non-blocking, non-fatal
			h.subWG.Add(1)
			go func() {
				defer h.subWG.Done()
				subCtx, cancel := context.WithTimeout(h.baseContext(), subscriptionTimeout)
				defer cancel()
				logSubscriptionError("Subscribe", h.subscribeStore(subCtx, userID, simID, sessionID))
			}()
			log.Printf("Client subscribed to simulation %s", sub.simulationID)

		case unsub := <-h.simUnsubscribe:
			h.mu.Lock()
			if conns, ok := h.clients[unsub.client.sessionID]; !ok || !conns[unsub.client] {
				h.mu.Unlock()
				continue
			}
			if subs, ok := h.simSubscriptions[unsub.client]; ok {
				delete(subs, unsub.simulationID)
				if len(subs) == 0 {
					delete(h.simSubscriptions, unsub.client)
				}
			}
			// Reverse index
			if clients, ok := h.simToClients[unsub.simulationID]; ok {
				delete(clients, unsub.client)
				if len(clients) == 0 {
					delete(h.simToClients, unsub.simulationID)
				}
			}
			// Add to blocklist — prevents this sim's messages from leaking
			// through Phase 1 global observer delivery after unsubscribe.
			if h.simBlocked[unsub.client] == nil {
				h.simBlocked[unsub.client] = make(map[string]bool)
			}
			h.simBlocked[unsub.client][unsub.simulationID] = true
			sessionID := unsub.client.sessionID
			userID := unsub.client.userID
			simID := unsub.simulationID
			stillSubscribed := false
			if conns, ok := h.clients[sessionID]; ok {
				for peer := range conns {
					if peer.userID != userID {
						continue
					}
					if peerSubs, ok := h.simSubscriptions[peer]; ok && peerSubs[simID] {
						stillSubscribed = true
						break
					}
				}
			}
			h.mu.Unlock()
			// Async Redis cleanup — non-blocking, non-fatal (skip if sibling tab still subscribed)
			if !stillSubscribed {
				h.subWG.Add(1)
				go func() {
					defer h.subWG.Done()
					subCtx, cancel := context.WithTimeout(h.baseContext(), subscriptionTimeout)
					defer cancel()
					logSubscriptionError("Unsubscribe", h.unsubscribeStore(subCtx, userID, simID, sessionID))
				}()
			}
			log.Printf("Client unsubscribed from simulation %s", unsub.simulationID)

		case message := <-h.broadcast:
			h.mu.RLock()
			for _, conns := range h.clients {
				for c := range conns {
					if !c.safeSend(&wsMessage{mType: websocket.TextMessage, data: message}) {
						if n := droppedMsgCount.Add(1); n%100 == 1 {
							log.Printf("Hub: dropped broadcast for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
						}
					}
				}
			}
			h.mu.RUnlock()

		case sm := <-h.sessionMsg:
			h.mu.RLock()
			if conns, ok := h.clients[sm.SessionID]; ok {
				for c := range conns {
					if !c.safeSend(&wsMessage{mType: websocket.TextMessage, data: sm.Data}) {
						if n := droppedMsgCount.Add(1); n%100 == 1 {
							log.Printf("Hub: dropped session message for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
						}
					}
				}
			}
			h.mu.RUnlock()
		}
	}
}

func extractTargetUserID(wrapper *gateway.Wrapper) string {
	if wrapper == nil || len(wrapper.Metadata) == 0 {
		return ""
	}
	var meta struct {
		TargetUserID string `json:"target_user_id"`
	}
	if err := json.Unmarshal(wrapper.Metadata, &meta); err != nil {
		return ""
	}
	return strings.TrimSpace(meta.TargetUserID)
}

func (h *Hub) subscribeStore(ctx context.Context, userID, simID, sessionID string) error {
	if ts, ok := h.subStore.(TenantSubscriptionStore); ok {
		return ts.SubscribeForUser(ctx, userID, simID, sessionID)
	}
	return h.subStore.Subscribe(ctx, simID, sessionID)
}

func (h *Hub) unsubscribeStore(ctx context.Context, userID, simID, sessionID string) error {
	if ts, ok := h.subStore.(TenantSubscriptionStore); ok {
		return ts.UnsubscribeForUser(ctx, userID, simID, sessionID)
	}
	return h.subStore.Unsubscribe(ctx, simID, sessionID)
}

func (h *Hub) unsubscribeAllStore(ctx context.Context, userID, sessionID string) error {
	if ts, ok := h.subStore.(TenantSubscriptionStore); ok {
		return ts.UnsubscribeAllForUser(ctx, userID, sessionID)
	}
	return h.subStore.UnsubscribeAll(ctx, sessionID)
}

func (h *Hub) lookupStore(ctx context.Context, userID, simID string) ([]string, error) {
	if ts, ok := h.subStore.(TenantSubscriptionStore); ok {
		return ts.LookupForUser(ctx, userID, simID)
	}
	return h.subStore.Lookup(ctx, simID)
}

// processRemoteMessage handles a single remote message fan-out.
// Called from worker pool goroutines — uses RLock since it only reads client maps.
func (h *Hub) processRemoteMessage(ctx context.Context, rm *remoteMessage) {
	if rm == nil || rm.wrapper == nil {
		return
	}
	// Use pre-serialized bytes when available (e.g., from Redis PubSub),
	// falling back to proto.Marshal only when raw bytes weren't provided.
	var data []byte
	if rm.raw != nil {
		data = rm.raw
	} else {
		var err error
		data, err = proto.Marshal(rm.wrapper)
		if err != nil {
			log.Printf("Hub: failed to marshal remote message: %v", err)
			return
		}
	}

	// Determine target sessions based on the protobuf Wrapper
	var targetSessions []string
	if len(rm.wrapper.Destination) > 0 {
		targetSessions = rm.wrapper.Destination
	} else if rm.wrapper.SessionId != "" {
		targetSessions = []string{rm.wrapper.SessionId}
	} else if rm.wrapper.Origin != nil && rm.wrapper.Origin.SessionId != "" {
		targetSessions = []string{rm.wrapper.Origin.SessionId}
	}

	simID := rm.wrapper.SimulationId
	tenantUserID := strings.TrimSpace(rm.tenantUserID)
	isClientOrBroadcast := rm.wrapper.Type == "broadcast" || (rm.wrapper.Origin != nil && rm.wrapper.Origin.Type == "client")
	isEnvReset := rm.wrapper.Type == "environment_reset" || rm.wrapper.Event == "environment_reset"
	isSystemOrigin := rm.wrapper.Origin != nil && rm.wrapper.Origin.Type == "system"
	isSystemNotice := (rm.wrapper.Origin == nil || isSystemOrigin) && (rm.wrapper.Type == "system_notice" || rm.wrapper.Type == "system_marker")
	isExplicitSystemBroadcast := simID == "" && !isClientOrBroadcast && (isSystemOrigin || isSystemNotice)

	var targetUserID string
	if isEnvReset || isExplicitSystemBroadcast {
		targetUserID = extractTargetUserID(rm.wrapper)
	}
	if tenantUserID != "" && targetUserID != "" && targetUserID != tenantUserID {
		// Conflicting channel tenant vs metadata target_user_id: fail closed.
		return
	}

	// Pre-resolve session ownership outside h.mu.RLock() so store/Redis latency
	// never blocks Register/Unregister.
	var allowedGlobalUsers map[string]bool
	if simID == "" && !isEnvReset && !isClientOrBroadcast && !isSystemNotice && len(targetSessions) > 0 {
		h.mu.RLock()
		sessionValidator := h.sessionOwnerValidator
		var candidateUsers map[string]bool
		if sessionValidator != nil {
			if globalConns, ok := h.clients[""]; ok && len(globalConns) > 0 {
				candidateUsers = make(map[string]bool)
				for c := range globalConns {
					if c.userID != "" && (tenantUserID == "" || c.userID == tenantUserID) {
						candidateUsers[c.userID] = true
					}
				}
			}
		}
		h.mu.RUnlock()

		if len(candidateUsers) > 0 {
			if ctx == nil {
				ctx = h.baseContext()
			}
			checkCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
			allowedGlobalUsers = make(map[string]bool, len(candidateUsers))
			for uid := range candidateUsers {
				for _, rawSID := range targetSessions {
					sid := strings.TrimSpace(rawSID)
					if sid == "" {
						continue
					}
					owned, err := sessionValidator(checkCtx, uid, sid)
					if err != nil {
						log.Printf("Hub: session ownership check failed user=%s session_id=%s: %v", uid, sid, err)
						continue
					}
					if owned {
						allowedGlobalUsers[uid] = true
						break
					}
				}
			}
			cancel()
		}
	}

	var allowedSimSessionUsers map[string]bool
	var checkSimSessionUsers bool
	if simID != "" && len(targetSessions) > 0 {
		h.mu.RLock()
		sessionValidator := h.sessionOwnerValidator
		simValidator := h.simOwnerValidator
		var candidateUsers map[string]bool
		if sessionValidator != nil {
			candidateUsers = make(map[string]bool)
			if globalConns, ok := h.clients[""]; ok {
				for c := range globalConns {
					if c.userID != "" && (tenantUserID == "" || c.userID == tenantUserID) {
						candidateUsers[c.userID] = true
					}
				}
			}
			if simConns, ok := h.simToClients[simID]; ok {
				for c := range simConns {
					if c.userID != "" && (tenantUserID == "" || c.userID == tenantUserID) {
						candidateUsers[c.userID] = true
					}
				}
			}
		}
		h.mu.RUnlock()

		if sessionValidator != nil {
			checkSimSessionUsers = true
			if len(candidateUsers) > 0 {
				if ctx == nil {
					ctx = h.baseContext()
				}
				checkCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
				allowedSimSessionUsers = make(map[string]bool, len(candidateUsers))
				for uid := range candidateUsers {
					for _, rawSID := range targetSessions {
						sid := strings.TrimSpace(rawSID)
						if sid == "" {
							continue
						}
						owned, err := sessionValidator(checkCtx, uid, sid)
						if err != nil {
							log.Printf("Hub: simulation session ownership check failed user=%s session_id=%s: %v", uid, sid, err)
							continue
						}
						if owned {
							allowedSimSessionUsers[uid] = true
							break
						}
						// When the simulator pipeline emits events whose Origin.SessionId equals
						// SimulationId (e.g. prepare_simulation, spawn_runners, fire_start_gun,
						// advance_tick), sid is the simulation_id rather than a spawned session
						// key; accept when the simulation itself is owned by uid.
						if sid == simID && simValidator != nil {
							simOwned, simErr := simValidator(checkCtx, uid, simID)
							if simErr != nil {
								log.Printf("Hub: simulation target ownership check failed user=%s simulation_id=%s: %v", uid, simID, simErr)
								continue
							}
							if simOwned {
								allowedSimSessionUsers[uid] = true
								break
							}
						}
					}
				}
				cancel()
			}
		}
	}

	// Track which clients already received this message to avoid double-delivery
	delivered := make(map[*client]bool)

	// H7 fix: Single RLock/RUnlock region for all local delivery.
	// Cross-instance Redis lookup is handled separately below with
	// its own lock lifecycle to avoid mid-function lock juggling.
	h.mu.RLock()
	ownershipEnforced := h.requireSimOwnership || h.simOwnerValidator != nil || h.sessionOwnerValidator != nil
	hasSessionValidator := h.sessionOwnerValidator != nil

	// 1. Fan-out to global observers, respecting simulation, channel tenant, and session ownership.
	if globalConns, ok := h.clients[""]; ok {
		for c := range globalConns {
			if tenantUserID != "" && c.userID != tenantUserID {
				continue
			}
			if isEnvReset {
				if targetUserID != "" {
					if c.userID != targetUserID {
						continue
					}
				} else if tenantUserID != "" {
					if c.userID != tenantUserID {
						continue
					}
				} else if ownershipEnforced || c.userID != "" {
					// Unscoped environment_reset must never fan out to
					// authenticated or ownership-enforced global observers.
					continue
				}
			} else if simID != "" {
				if ownershipEnforced || c.userID != "" {
					// Ownership-enforced or authenticated sessionID=="" connections
					// must NEVER receive unsubscribed simulation traffic (even
					// after unsubscribing or after a rejected RegisterWithUser).
					// Exception: messages explicitly targeted at an owned session
					// (e.g. planner start_simulation tool_end returning the newly
					// minted simulation_id so the client can subscribe to it) when
					// not explicitly unsubscribed and verified via sessionValidator.
					subs, hasSubs := h.simSubscriptions[c]
					explicitlySubscribed := hasSubs && subs[simID]
					isBlocked := h.simBlocked[c] != nil && h.simBlocked[c][simID]
					isTargetedOwnedSession := len(targetSessions) > 0 &&
						!isClientOrBroadcast &&
						!isSystemOrigin &&
						!isSystemNotice &&
						hasSessionValidator &&
						checkSimSessionUsers &&
						c.userID != "" &&
						allowedSimSessionUsers[c.userID] &&
						!isBlocked
					if !explicitlySubscribed && !isTargetedOwnedSession {
						continue
					}
					if checkSimSessionUsers && c.userID != "" && !allowedSimSessionUsers[c.userID] {
						continue
					}
				} else {
					// Legacy unauthenticated, non-ownership-enforced global observer:
					// 1. If client has active subscriptions → allowlist mode (only matching sims)
					// 2. If client has no subscriptions but has a blocklist → skip blocked sims
					// 3. If client has neither → deliver all (global observers)
					if subs, hasSubs := h.simSubscriptions[c]; hasSubs && len(subs) > 0 {
						if !subs[simID] {
							continue // Allowlist mode: not subscribed to this sim
						}
					} else if blocked, hasBlocked := h.simBlocked[c]; hasBlocked && blocked[simID] {
						continue // Blocklist mode: this sim was explicitly unsubscribed
					}
				}
			} else if ownershipEnforced || c.userID != "" {
				if isClientOrBroadcast {
					// Never fan out simulation-less client or broadcast wrappers
					// across arbitrary authenticated global observers.
					continue
				}
				if isSystemNotice {
					if targetUserID != "" && c.userID != targetUserID {
						continue
					}
				} else if len(targetSessions) > 0 {
					// Targeted or agent-originated sim-less wrapper: require an
					// authenticated user and verified ownership of a target session.
					if c.userID == "" || !hasSessionValidator || !allowedGlobalUsers[c.userID] {
						continue
					}
				} else if !isSystemOrigin {
					// Unscoped non-system sim-less wrapper (e.g. Origin.Type == "agent")
					// must never fan out to arbitrary global observers.
					continue
				} else if targetUserID != "" && c.userID != targetUserID {
					continue
				}
			}
			if !c.safeSend(&wsMessage{mType: websocket.BinaryMessage, data: data}) {
				if n := droppedMsgCount.Add(1); n%100 == 1 {
					log.Printf("Hub: dropped remote message for global observer (buffer full or closed) [total drops: %d]", n)
				}
			} else {
				delivered[c] = true
			}
		}
	}

	// 2. Route to targeted sessions (or broadcast to all if no target specified)
	if len(targetSessions) == 0 {
		// Under ownership enforcement, simulation-scoped messages, client/broadcast
		// wrappers, non-system wrappers (e.g. Origin.Type == "agent"), or unscoped
		// environment_reset without explicit target sessions or target_user_id must
		// NOT broadcast to arbitrary sessions across tenants.
		if isEnvReset {
			if targetUserID != "" {
				for sid, conns := range h.clients {
					if sid == "" {
						continue
					}
					for c := range conns {
						if c.userID != targetUserID || (tenantUserID != "" && c.userID != tenantUserID) {
							continue
						}
						if c.safeSend(&wsMessage{mType: websocket.BinaryMessage, data: data}) {
							delivered[c] = true
						} else {
							if n := droppedMsgCount.Add(1); n%100 == 1 {
								log.Printf("Hub: dropped remote message for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
							}
						}
					}
				}
			} else if tenantUserID != "" {
				for sid, conns := range h.clients {
					if sid == "" {
						continue
					}
					for c := range conns {
						if c.userID != tenantUserID {
							continue
						}
						if c.safeSend(&wsMessage{mType: websocket.BinaryMessage, data: data}) {
							delivered[c] = true
						} else {
							if n := droppedMsgCount.Add(1); n%100 == 1 {
								log.Printf("Hub: dropped remote message for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
							}
						}
					}
				}
			} else if !ownershipEnforced {
				for sid, conns := range h.clients {
					if sid == "" {
						continue
					}
					for c := range conns {
						if c.safeSend(&wsMessage{mType: websocket.BinaryMessage, data: data}) {
							delivered[c] = true
						} else {
							if n := droppedMsgCount.Add(1); n%100 == 1 {
								log.Printf("Hub: dropped remote message for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
							}
						}
					}
				}
			}
		} else if !ownershipEnforced || isExplicitSystemBroadcast {
			// Broadcast to all active sessions (skip "" since we already sent to it)
			for sid, conns := range h.clients {
				if sid == "" {
					continue
				}
				for c := range conns {
					if tenantUserID != "" && c.userID != tenantUserID {
						continue
					}
					if targetUserID != "" && c.userID != targetUserID {
						continue
					}
					if c.safeSend(&wsMessage{mType: websocket.BinaryMessage, data: data}) {
						delivered[c] = true
					} else {
						if n := droppedMsgCount.Add(1); n%100 == 1 {
							log.Printf("Hub: dropped remote message for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
						}
					}
				}
			}
		}
	} else {
		// Targeted routing
		for _, sid := range targetSessions {
			if sid == "" {
				continue // Handled by global block
			}
			if conns, ok := h.clients[sid]; ok {
				for c := range conns {
					if tenantUserID != "" && c.userID != tenantUserID {
						continue
					}
					if isEnvReset && targetUserID != "" && c.userID != targetUserID {
						continue
					}
					if c.safeSend(&wsMessage{mType: websocket.BinaryMessage, data: data}) {
						delivered[c] = true
					} else {
						if n := droppedMsgCount.Add(1); n%100 == 1 {
							log.Printf("Hub: dropped targeted remote message for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
						}
					}
				}
			}
		}
	}

	// 3. Simulation-based routing: O(1) reverse index lookup by simulation_id,
	// enforcing channel tenant identity and target session ownership.
	if rm.wrapper.SimulationId != "" {
		if clients, ok := h.simToClients[rm.wrapper.SimulationId]; ok {
			for c := range clients {
				if tenantUserID != "" && c.userID != tenantUserID {
					continue
				}
				if checkSimSessionUsers && c.userID != "" && !allowedSimSessionUsers[c.userID] {
					continue
				}
				if !delivered[c] {
					if c.safeSend(&wsMessage{mType: websocket.BinaryMessage, data: data}) {
						delivered[c] = true
					} else {
						if n := droppedMsgCount.Add(1); n%100 == 1 {
							log.Printf("Hub: dropped sim-routed message for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
						}
					}
				}
			}
		}
	}

	h.mu.RUnlock()

	// 3b. Cross-instance Redis lookup — runs outside the RLock to avoid
	// blocking Register/Unregister during Redis latency spikes.
	// Uses its own RLock region for the delivery phase.
	// Copy the delivered map so deliverCrossInstance operates on a
	// snapshot — the original was built under one RLock region and
	// must not be mutated inside a second, independent RLock region.
	if rm.wrapper.SimulationId != "" {
		deliveredCopy := make(map[*client]bool, len(delivered))
		for c := range delivered {
			deliveredCopy[c] = true
		}
		h.deliverCrossInstanceScoped(ctx, rm.wrapper.SimulationId, tenantUserID, checkSimSessionUsers, allowedSimSessionUsers, data, deliveredCopy)
	}
}

// deliverCrossInstance queries Redis for remote subscribers and delivers
// to any locally connected clients not already covered by simToClients.
// Preserved with its 4-argument signature for existing unit tests; delegates
// to deliverCrossInstanceScoped.
func (h *Hub) deliverCrossInstance(ctx context.Context, simID string, data []byte, delivered map[*client]bool) {
	h.deliverCrossInstanceScoped(ctx, simID, "", false, nil, data, delivered)
}

// deliverCrossInstanceScoped queries Redis for remote subscribers (scoped by
// tenantUserID when non-empty) and delivers to any locally connected clients
// not already covered by simToClients.
// Manages its own lock lifecycle — never called under an existing lock.
func (h *Hub) deliverCrossInstanceScoped(
	ctx context.Context,
	simID string,
	tenantUserID string,
	checkSimSessionUsers bool,
	allowedSimSessionUsers map[string]bool,
	data []byte,
	delivered map[*client]bool,
) {
	if ctx == nil {
		ctx = h.baseContext()
	}
	lookupCtx, cancel := context.WithTimeout(ctx, subscriptionTimeout)
	remoteSessions, err := h.lookupStore(lookupCtx, tenantUserID, simID)
	cancel()

	if err != nil {
		if !errors.Is(err, context.Canceled) {
			log.Printf("Hub: SubscriptionStore.Lookup failed for simulation %s: %v", simID, err)
		}
		return
	}
	if len(remoteSessions) == 0 {
		return
	}

	h.mu.RLock()
	defer h.mu.RUnlock()

	for _, sid := range remoteSessions {
		if conns, ok := h.clients[sid]; ok {
			for c := range conns {
				if tenantUserID != "" && c.userID != tenantUserID {
					continue
				}
				if checkSimSessionUsers && c.userID != "" && !allowedSimSessionUsers[c.userID] {
					continue
				}
				if !delivered[c] {
					// Guard: verify client wasn't unregistered during Redis lookup
					if _, stillExists := h.clients[c.sessionID]; !stillExists {
						continue
					}
					// Guard: only deliver if the client is locally subscribed
					// to this simulation. Redis data may be stale after an
					// async unsubscribe.
					if subs, hasSubs := h.simSubscriptions[c]; !hasSubs || !subs[simID] {
						continue
					}
					if c.safeSend(&wsMessage{mType: websocket.BinaryMessage, data: data}) {
						delivered[c] = true
					} else if n := droppedMsgCount.Add(1); n%100 == 1 {
						log.Printf("Hub: dropped cross-instance sim message for session %s (buffer full or closed) [total drops: %d]", c.sessionID, n)
					}
				}
			}
		}
	}
}

// textFrameMsg is the JSON structure for subscription text frames.
type textFrameMsg struct {
	Type         string `json:"type"`
	SimulationID string `json:"simulation_id"`
}

type clientErrorSpec struct {
	event        string
	code         string
	simulationID string
	details      string
}

func buildClientErrorMessage(sessionID, event, code, simulationID, details string, targetSessionIDs ...string) (*wsMessage, error) {
	var targets []string
	seen := make(map[string]bool, len(targetSessionIDs))
	for _, rawTarget := range targetSessionIDs {
		t := strings.TrimSpace(rawTarget)
		if t != "" && !seen[t] {
			seen[t] = true
			targets = append(targets, t)
		}
	}
	dataMap := map[string]string{
		"error":   code,
		"details": details,
	}
	if simulationID != "" {
		dataMap["simulation_id"] = simulationID
	}
	if len(targets) > 0 {
		dataMap["session_id"] = targets[0]
	}
	var dest []string
	if sessionID != "" {
		dest = []string{sessionID}
	} else if len(targets) > 0 {
		dest = targets
	}
	msg := GatewayMessage{
		Origin: GatewayOrigin{
			Type: "system",
			ID:   "gateway",
		},
		Destination: dest,
		Status:      "error",
		Type:        "error",
		Event:       event,
		Data:        dataMap,
	}
	payload, err := json.Marshal(msg)
	if err != nil {
		return nil, err
	}
	return &wsMessage{mType: websocket.TextMessage, data: payload}, nil
}

func (h *Hub) sendClientError(c *client, event, code, simulationID, details string, targetSessionIDs ...string) {
	if h == nil || c == nil {
		return
	}
	errMsg, err := buildClientErrorMessage(c.sessionID, event, code, simulationID, details, targetSessionIDs...)
	if err != nil {
		log.Printf("Hub: failed to marshal client error frame: %v", err)
		return
	}
	h.mu.RLock()
	conns, ok := h.clients[c.sessionID]
	if ok && conns[c] {
		c.safeSend(errMsg)
	}
	h.mu.RUnlock()
}

// SendConnError sends an explicit JSON error frame exclusively to the given
// WebSocket connection (never broadcasting to sibling global observers).
// Optional targetSessionIDs identify rejected session targets in data.session_id
// and (when the connection itself is a global observer with empty sessionID) in
// destination.
func (h *Hub) SendConnError(conn *websocket.Conn, event, code, simulationID, details string, targetSessionIDs ...string) {
	if h == nil || conn == nil {
		return
	}
	h.mu.RLock()
	c := h.connToClient[conn]
	h.mu.RUnlock()
	if c != nil {
		h.sendClientError(c, event, code, simulationID, details, targetSessionIDs...)
	}
}

func (h *Hub) checkSimulationOwnership(validator SimulationOwnershipValidator, requireOwnership bool, userID, simID string) *clientErrorSpec {
	if validator == nil {
		if requireOwnership || userID != "" {
			return &clientErrorSpec{
				event:        "service_unavailable",
				code:         "ownership_validator_unavailable",
				simulationID: simID,
				details:      "simulation ownership verification is unavailable",
			}
		}
		return nil
	}
	if userID == "" {
		return &clientErrorSpec{
			event:        "unauthorized",
			code:         "authentication_required",
			simulationID: simID,
			details:      "authenticated user required for simulation subscription",
		}
	}
	checkCtx, cancel := context.WithTimeout(h.baseContext(), 5*time.Second)
	defer cancel()
	owned, err := validator(checkCtx, userID, simID)
	if err != nil {
		log.Printf("Hub: simulation ownership check failed user=%s simulation_id=%s: %v", userID, simID, err)
		return &clientErrorSpec{
			event:        "service_unavailable",
			code:         "ownership_check_failed",
			simulationID: simID,
			details:      "failed to verify simulation ownership",
		}
	}
	if !owned {
		log.Printf("Hub: rejected unauthorized subscribe_simulation user=%s simulation_id=%s", userID, simID)
		return &clientErrorSpec{
			event:        "forbidden",
			code:         "simulation_not_owned",
			simulationID: simID,
			details:      "simulation is not owned by user",
		}
	}
	return nil
}

// HandleTextMessage parses a JSON text frame from a WebSocket connection and
// routes subscription requests through the Hub's event loop channels.
// Must be called from a goroutine OTHER than Run() to avoid deadlock.
func (h *Hub) HandleTextMessage(conn *websocket.Conn, data []byte) {
	if h == nil || conn == nil {
		return
	}
	select {
	case <-h.done:
		return
	default:
	}

	h.mu.RLock()
	c, ok := h.connToClient[conn]
	validator := h.simOwnerValidator
	requireOwnership := h.requireSimOwnership
	h.mu.RUnlock()
	if !ok || c == nil {
		log.Printf("Hub: text message from unknown connection (ignored)")
		return
	}

	var msg textFrameMsg
	if err := json.Unmarshal(data, &msg); err != nil {
		log.Printf("Hub: failed to parse text message: %v", err)
		h.sendClientError(c, "bad_request", "invalid_json", "", "failed to parse text message")
		return
	}
	simID := strings.TrimSpace(msg.SimulationID)
	switch msg.Type {
	case "subscribe_simulation":
		if simID == "" {
			h.sendClientError(c, "bad_request", "missing_simulation_id", "", "simulation_id is required")
			return
		}
		if errSpec := h.checkSimulationOwnership(validator, requireOwnership, c.userID, simID); errSpec != nil {
			h.sendClientError(c, errSpec.event, errSpec.code, errSpec.simulationID, errSpec.details)
			return
		}
		if c.closed.Load() {
			return
		}
		select {
		case h.simSubscribe <- simSubscriptionMsg{client: c, simulationID: simID}:
		case <-h.done:
		}
	case "unsubscribe_simulation":
		if simID == "" {
			h.sendClientError(c, "bad_request", "missing_simulation_id", "", "simulation_id is required")
			return
		}
		select {
		case h.simUnsubscribe <- simSubscriptionMsg{client: c, simulationID: simID}:
		case <-h.done:
		}
	default:
		log.Printf("Hub: unknown text message type: %s", msg.Type)
		h.sendClientError(c, "bad_request", "unknown_message_type", "", fmt.Sprintf("unsupported text message type %q", msg.Type))
	}
}

// SubscribeSimulation subscribes a client (identified by its WebSocket connection)
// to a simulation ID. Messages with a matching SimulationId will be delivered.
func (h *Hub) SubscribeSimulation(conn *websocket.Conn, simulationID string) {
	if h == nil || conn == nil {
		return
	}
	select {
	case <-h.done:
		return
	default:
	}

	h.mu.RLock()
	c, ok := h.connToClient[conn]
	validator := h.simOwnerValidator
	requireOwnership := h.requireSimOwnership
	h.mu.RUnlock()
	if !ok || c == nil {
		return
	}
	simID := strings.TrimSpace(simulationID)
	if simID == "" {
		h.sendClientError(c, "bad_request", "missing_simulation_id", "", "simulation_id is required")
		return
	}
	if errSpec := h.checkSimulationOwnership(validator, requireOwnership, c.userID, simID); errSpec != nil {
		h.sendClientError(c, errSpec.event, errSpec.code, errSpec.simulationID, errSpec.details)
		return
	}
	if c.closed.Load() {
		return
	}
	select {
	case h.simSubscribe <- simSubscriptionMsg{client: c, simulationID: simID}:
	case <-h.done:
	}
}

// UnsubscribeSimulation removes a client's subscription to a simulation ID.
func (h *Hub) UnsubscribeSimulation(conn *websocket.Conn, simulationID string) {
	if h == nil || conn == nil {
		return
	}
	select {
	case <-h.done:
		return
	default:
	}

	h.mu.RLock()
	c, ok := h.connToClient[conn]
	h.mu.RUnlock()
	if !ok || c == nil {
		return
	}
	simID := strings.TrimSpace(simulationID)
	if simID == "" {
		return
	}
	select {
	case h.simUnsubscribe <- simSubscriptionMsg{client: c, simulationID: simID}:
	case <-h.done:
	}
}

func (c *client) writePump() {
	ticker := time.NewTicker(54 * time.Second)
	hubClosed := false // tracks whether Hub already closed our send channel
	defer func() {
		ticker.Stop()
		if c.conn != nil {
			_ = c.conn.Close()
		}
		if !hubClosed {
			// Self-unregister: notify the Hub so it cleans up maps and
			// simulation subscriptions, unblocking cleanly if Hub already exited.
			select {
			case c.hub.unregister <- c:
			case <-c.hub.done:
			}
		}
	}()
	for {
		select {
		case message, ok := <-c.send:
			if !ok {
				// Hub closed the channel via Unregister.
				hubClosed = true
				if c.conn != nil {
					_ = c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
					_ = c.conn.WriteMessage(websocket.CloseMessage, []byte{})
				}
				return
			}

			if c.conn == nil {
				return
			}
			_ = c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if err := c.conn.WriteMessage(message.mType, message.data); err != nil {
				return
			}
		case <-ticker.C:
			if c.conn == nil {
				return
			}
			_ = c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

// SendMessage sends a JSON-wrapped message to a specific session.
func (h *Hub) SendMessage(sessionID string, msgType string, event string, content interface{}) {
	status := "success"
	if msgType == "error" {
		status = "error"
	}
	msg := GatewayMessage{
		Origin: GatewayOrigin{
			Type: "system",
			ID:   "gateway",
		},
		Destination: []string{sessionID},
		Status:      status,
		Type:        msgType,
		Event:       event,
		Data:        content,
	}

	data, err := json.Marshal(msg)
	if err != nil {
		log.Printf("Failed to marshal hub message: %v", err)
		return
	}
	select {
	case h.sessionMsg <- SessionMessage{
		SessionID: sessionID,
		Data:      data,
	}:
	case <-h.done:
	}
}
