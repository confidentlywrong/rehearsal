// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package hub

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/gen_proto/gateway"
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/agent"
	"github.com/GoogleCloudPlatform/race-condition/internal/auth"
	"github.com/GoogleCloudPlatform/race-condition/internal/session"
	"github.com/hashicorp/go-retryablehttp"
	"github.com/redis/go-redis/v9"
	"golang.org/x/oauth2/google"
	"google.golang.org/protobuf/proto"
)

// DefaultSpawnShards is the number of shards used for spawn queue distribution.
// Each agent type's spawn queue is split into this many sub-queues to prevent
// competing-consumer hotspots when multiple dispatcher instances BLPOP.
const DefaultSpawnShards = 8

// SpawnQueueName returns the sharded spawn queue name for a given session.
// Uses FNV-1a hash for deterministic, even distribution across shards.
//
// This is the legacy, user-unaware form. Callers with a request
// context should prefer SpawnQueueNameForContext so user A's spawns
// never land in user B's queue. Direct callers (hub tests, internal
// bookkeeping that has no request context) keep the byte-identical
// legacy format.
func SpawnQueueName(agentType, sessionID string, numShards int) string {
	if numShards <= 0 {
		numShards = DefaultSpawnShards
	}
	h := fnv.New32a()
	h.Write([]byte(sessionID))
	shard := int(h.Sum32() % uint32(numShards))
	return fmt.Sprintf("simulation:spawns:%s:%d", agentType, shard)
}

// SpawnQueueNameForContext returns the spawn queue name scoped to
// the user on the request context.
//
//   - no UserContext on ctx → byte-identical to SpawnQueueName, i.e.
//     `simulation:spawns:{agentType}:{shard}` (legacy format).
//   - UserContext present   → `{ACCOUNT_KEY_PREFIX}:{user_id}:simulation:spawns:{agentType}:{shard}`.
//
// Shard math is unchanged, so a given session always maps to the same
// shard inside its own user namespace.
func SpawnQueueNameForContext(ctx context.Context, agentType, sessionID string, numShards int) string {
	base := SpawnQueueName(agentType, sessionID, numShards)
	if tp := account.KeyPrefix(ctx); tp != "" {
		return tp + ":" + base
	}
	return base
}

// newGCPClient is the function used to create GCP-authenticated HTTP clients.
// It is a variable to allow replacement in tests.
var newGCPClient = func(ctx context.Context, scopes ...string) (*http.Client, error) {
	return google.DefaultClient(ctx, scopes...)
}

// QueueItem pairs a Redis queue name with a JSON-serializable payload
// for use in batch enqueue operations.
type QueueItem struct {
	Queue string
	Data  interface{}
}

var (
	// ErrAgentNotFound is returned when a requested agent type is missing from
	// the catalog or has no configured URL.
	ErrAgentNotFound = errors.New("agent not found in catalog or has no URL")

	// ErrSwitchboardClosed is returned when a dispatch operation is attempted
	// after the switchboard has been shut down.
	ErrSwitchboardClosed = errors.New("switchboard is shut down")
)

// Switchboard handles distributed message fan-out.
type Switchboard interface {
	Broadcast(ctx context.Context, wrapper *gateway.Wrapper) error
	EnqueueOrchestration(ctx context.Context, queue string, event interface{}) error
	BatchEnqueueOrchestration(ctx context.Context, items []QueueItem) error
	PublishOrchestration(ctx context.Context, channel string, event interface{}) error
	DispatchToAgent(ctx context.Context, agentType string, event interface{}) error
	PokeAgent(ctx context.Context, agentType string, event interface{}) error
	FlushQueues(ctx context.Context) (int, error)
	Ping(ctx context.Context) error
	Start(ctx context.Context) error
	Channel() string
}

// RedisSwitchboard handles distributed message fan-out via Redis.
type RedisSwitchboard struct {
	client          *redis.Client
	gatewayID       string
	localHub        *Hub
	channel         string
	catalog         *agent.Catalog
	registry        session.DistributedRegistry // Session-aware routing
	httpClient      *http.Client                // Plain HTTP client for subscriber pokes
	callableClient  *http.Client                // Retryable HTTP client for callable dispatch
	gcpClient       *http.Client                // Authenticated + retryable client for Agent Engine callable agents
	gcpClientErr    error
	oidcClientFn    func(audience string) (*http.Client, error) // Defaults to auth.OIDCClientE
	lifecycleMu     sync.Mutex
	lifecycleCtx    context.Context
	lifecycleCancel context.CancelFunc
	closed          bool
	wg              sync.WaitGroup // Tracks in-flight dispatch goroutines for graceful shutdown
}

// NewSwitchboardWithRegistry creates a distributed switchboard with session-aware routing.
func NewSwitchboardWithRegistry(client *redis.Client, gatewayID string, localHub *Hub, catalog *agent.Catalog, registry session.DistributedRegistry) Switchboard {
	// Create a retryable HTTP client for callable dispatch
	retryClient := retryablehttp.NewClient()
	retryClient.RetryMax = 3
	retryClient.RetryWaitMin = 500 * time.Millisecond
	retryClient.RetryWaitMax = 5 * time.Second
	retryClient.Logger = nil // Suppress retryablehttp's default logging
	callableHTTP := retryClient.StandardClient()
	callableHTTP.Timeout = 5 * time.Minute

	// Pre-create a GCP-authenticated HTTP client for Agent Engine calls.
	// This wraps the retryable transport with GCP OAuth.
	gcpClient, err := newGCPClient(context.Background(),
		"https://www.googleapis.com/auth/cloud-platform",
	)
	if err != nil {
		log.Printf("Switchboard: WARNING — failed to create GCP auth client: %v (Agent Engine requests will fail closed)", err)
	}
	if gcpClient != nil {
		// Agent Engine agents (especially planner_with_eval) execute
		// multi-step tool chains (evaluation pipeline,
		// inter-agent A2A calls) that take 1-3 minutes. The context
		// timeout in dispatchCallable is 5 minutes; the client timeout
		// must match or the client cancels the request prematurely.
		gcpClient.Timeout = 5 * time.Minute
	}

	lifecycleCtx, lifecycleCancel := context.WithCancel(context.Background())

	return &RedisSwitchboard{
		client:    client,
		gatewayID: gatewayID,
		localHub:  localHub,
		channel:   "gateway:broadcast",
		catalog:   catalog,
		registry:  registry,
		httpClient: &http.Client{
			Timeout: 5 * time.Second,
		},
		callableClient:  callableHTTP,
		gcpClient:       gcpClient,
		gcpClientErr:    err,
		oidcClientFn:    auth.OIDCClientE,
		lifecycleCtx:    lifecycleCtx,
		lifecycleCancel: lifecycleCancel,
	}
}

// Shutdown cancels the switchboard lifecycle context, aborting any in-flight
// asynchronous dispatch or poke requests.
func (s *RedisSwitchboard) Shutdown() {
	if s == nil {
		return
	}
	s.lifecycleMu.Lock()
	s.closed = true
	if s.lifecycleCtx == nil {
		s.lifecycleCtx, s.lifecycleCancel = context.WithCancel(context.Background())
	}
	cancel := s.lifecycleCancel
	s.lifecycleMu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (s *RedisSwitchboard) ensureLifecycleCtx() context.Context {
	s.lifecycleMu.Lock()
	defer s.lifecycleMu.Unlock()
	if s.lifecycleCtx == nil {
		s.lifecycleCtx, s.lifecycleCancel = context.WithCancel(context.Background())
	}
	return s.lifecycleCtx
}

func (s *RedisSwitchboard) lifecycleErr() error {
	s.lifecycleMu.Lock()
	closed := s.closed
	lctx := s.lifecycleCtx
	s.lifecycleMu.Unlock()
	if closed {
		return ErrSwitchboardClosed
	}
	if lctx != nil {
		return lctx.Err()
	}
	return nil
}

// beginAsyncDispatch atomically checks that the switchboard is not shut down,
// increments s.wg under s.lifecycleMu before returning, and derives an async
// context detached from caller cancellation/deadlines while still cancelling on
// switchboard shutdown. Holding s.lifecycleMu across the closed check and
// s.wg.Add(1) prevents s.wg.Add(1) from racing with Shutdown() + Wait().
func (s *RedisSwitchboard) beginAsyncDispatch(callerCtx context.Context) (context.Context, context.CancelFunc, bool) {
	var base context.Context
	if callerCtx == nil {
		base = context.Background()
	} else {
		// Detach from caller's immediate deadline/defer cancel() while keeping
		// request values (e.g. account.UserContext, trace context).
		base = context.WithoutCancel(callerCtx)
	}
	derived, cancel := context.WithCancel(base)

	s.lifecycleMu.Lock()
	if s.closed || (s.lifecycleCtx != nil && s.lifecycleCtx.Err() != nil) {
		s.lifecycleMu.Unlock()
		cancel()
		return nil, nil, false
	}
	s.wg.Add(1)
	lctx := s.lifecycleCtx
	s.lifecycleMu.Unlock()

	if lctx == nil {
		return derived, cancel, true
	}
	stop := context.AfterFunc(lctx, cancel)
	return derived, func() {
		stop()
		cancel()
	}, true
}

func (s *RedisSwitchboard) getOIDCClient(audience string) (*http.Client, error) {
	fn := s.oidcClientFn
	if fn == nil {
		fn = auth.OIDCClientE
	}
	return fn(audience)
}

// Wait blocks until all in-flight dispatch goroutines have completed.
// Call during graceful shutdown to avoid leaked goroutines.
func (s *RedisSwitchboard) Wait() {
	if s == nil {
		return
	}
	s.wg.Wait()
}

// Ping checks the Redis connection.
func (s *RedisSwitchboard) Ping(ctx context.Context) error {
	if s == nil || s.client == nil {
		return errors.New("switchboard: nil redis client")
	}
	return s.client.Ping(ctx).Err()
}

// Channel returns the broadcast channel name.
func (s *RedisSwitchboard) Channel() string {
	return s.channel
}

func escapeRedisGlob(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); i++ {
		switch s[i] {
		case '\\', '*', '?', '[', ']':
			b.WriteByte('\\')
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

func (s *RedisSwitchboard) FlushQueues(ctx context.Context) (int, error) {
	if s == nil || s.client == nil {
		return 0, errors.New("switchboard: nil redis client")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	keysCount := 0

	// When SAAS_MODE=on and ctx carries a UserContext, flush ONLY that user's
	// tenant-scoped spawn queues (e.g. u:{user_id}:simulation:spawns:*).
	if prefix := account.KeyPrefix(ctx); prefix != "" {
		queuePrefix := prefix + ":simulation:spawns:"
		var cursor uint64
		for {
			var batch []string
			var err error
			batch, cursor, err = s.client.Scan(ctx, cursor, escapeRedisGlob(queuePrefix)+"*", 100).Result()
			if err != nil {
				return keysCount, fmt.Errorf("scanning user spawn queues: %w", err)
			}
			var owned []string
			for _, k := range batch {
				if strings.HasPrefix(k, queuePrefix) {
					owned = append(owned, k)
				}
			}
			if len(owned) > 0 {
				deleted, err := s.client.Del(ctx, owned...).Result()
				if err != nil {
					return keysCount, fmt.Errorf("deleting user spawn queues: %w", err)
				}
				keysCount += int(deleted)
			}
			if cursor == 0 {
				break
			}
		}
		return keysCount, nil
	}

	// Pass 1: global spawn queues (legacy / no-user-context)
	var cursor uint64
	for {
		var batch []string
		var err error
		batch, cursor, err = s.client.Scan(ctx, cursor, "simulation:spawns:*", 100).Result()
		if err != nil {
			return keysCount, fmt.Errorf("scanning spawn queues: %w", err)
		}
		if len(batch) > 0 {
			deleted, err := s.client.Del(ctx, batch...).Result()
			if err != nil {
				return keysCount, fmt.Errorf("deleting spawn queues: %w", err)
			}
			keysCount += int(deleted)
		}
		if cursor == 0 {
			break
		}
	}

	// Pass 2: tenant-scoped spawn queues (e.g. u:{user_id}:simulation:spawns:*)
	cursor = 0
	for {
		var batch []string
		var err error
		batch, cursor, err = s.client.Scan(ctx, cursor, "*:simulation:spawns:*", 100).Result()
		if err != nil {
			return keysCount, fmt.Errorf("scanning tenant-scoped spawn queues: %w", err)
		}
		if len(batch) > 0 {
			deleted, err := s.client.Del(ctx, batch...).Result()
			if err != nil {
				return keysCount, fmt.Errorf("deleting tenant-scoped spawn queues: %w", err)
			}
			keysCount += int(deleted)
		}
		if cursor == 0 {
			break
		}
	}

	return keysCount, nil
}

// Start listens for remote broadcast messages with automatic reconnection.
// If the Redis subscription drops, it retries with exponential backoff
// to ensure cross-instance message delivery is not permanently lost.
func (s *RedisSwitchboard) Start(ctx context.Context) error {
	if s == nil || s.client == nil {
		return errors.New("switchboard: nil redis client")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	s.ensureLifecycleCtx()
	stop := context.AfterFunc(ctx, s.Shutdown)
	defer stop()

	backoff := 500 * time.Millisecond
	maxBackoff := 30 * time.Second

	for {
		startTime := time.Now()
		err := s.listenBroadcasts(ctx)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		// Only reset backoff if the connection lasted a meaningful duration,
		// indicating it was a real connection loss, not an immediate failure.
		if time.Since(startTime) > 5*time.Second {
			backoff = 500 * time.Millisecond
		}
		log.Printf("Switchboard: Redis subscription lost: %v — reconnecting in %v", err, backoff)
		timer := time.NewTimer(backoff)
		select {
		case <-timer.C:
			backoff *= 2
			if backoff > maxBackoff {
				backoff = maxBackoff
			}
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		}
	}
}

// parseBroadcastChannelUserID extracts the tenant user ID from a Redis
// broadcast channel name. It accepts either the unprefixed baseChannel
// (returning "", true) or a 2-segment tenant prefix "<prefix>:<userID>:<baseChannel>"
// (returning userID, true). Malformed prefixes are rejected ("", false).
func parseBroadcastChannelUserID(channel, baseChannel string) (string, bool) {
	if channel == baseChannel {
		return "", true
	}
	suffix := ":" + baseChannel
	if !strings.HasSuffix(channel, suffix) {
		return "", false
	}
	prefix := strings.TrimSuffix(channel, suffix)
	parts := strings.Split(prefix, ":")
	if len(parts) != 2 || !isValidRedisKeyComponent(parts[0]) || !isValidRedisKeyComponent(parts[1]) {
		return "", false
	}
	return parts[1], true
}

// listenBroadcasts subscribes to the Redis broadcast channel and forwards
// messages to the local Hub. Returns when the subscription fails or the
// channel closes.
//
// M3: pattern-subscribes to both the legacy `gateway:broadcast` channel
// AND the tenant-scoped `*:gateway:broadcast` form (e.g.
// `t:acme:gateway:broadcast`) so the gateway keeps receiving Python
// publishes whether SAAS_MODE is off (legacy literal) or on (per-tenant
// channel name). The parsed tenant user_id is forwarded to the Hub so
// cross-tenant simulation hijacks are blocked.
func (s *RedisSwitchboard) listenBroadcasts(ctx context.Context) error {
	pubsub := s.client.PSubscribe(ctx, s.channel, "*:"+s.channel)
	defer pubsub.Close()

	// Verify the subscription is active before entering the read loop
	if _, err := pubsub.Receive(ctx); err != nil {
		return fmt.Errorf("subscribe confirmation failed: %w", err)
	}
	log.Printf("Switchboard: pattern-subscribed to Redis channels %q and %q", s.channel, "*:"+s.channel)

	ch := pubsub.Channel()
	for {
		select {
		case msg := <-ch:
			if msg == nil {
				return fmt.Errorf("Redis subscription channel closed")
			}
			tenantUserID, ok := parseBroadcastChannelUserID(msg.Channel, s.channel)
			if !ok {
				log.Printf("Switchboard: ignoring broadcast on malformed channel %q", msg.Channel)
				continue
			}
			rawBytes := []byte(msg.Payload)
			var wrapper gateway.Wrapper
			if err := proto.Unmarshal(rawBytes, &wrapper); err != nil {
				log.Printf("Switchboard: failed to unmarshal Redis broadcast: %v", err)
				continue
			}
			// Per-message log removed (Bug #42): fires on every Redis
			// broadcast, flooding logs with 1000+ runners.
			// All messages flow as binary protobuf gateway.Wrapper
			// through handleRemoteMessage.
			// UI clients decode protobuf directly (one format end-to-end).
			// Pass raw bytes to skip redundant proto.Marshal in Hub.Run().
			if s.localHub != nil {
				s.localHub.handleRemoteMessage(tenantUserID, true, &wrapper, rawBytes)
			}
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// Broadcast sends a message to all instances, scoping the Redis channel by
// UserContext when SAAS_MODE=on and a user is present on ctx.
func (s *RedisSwitchboard) Broadcast(ctx context.Context, wrapper *gateway.Wrapper) error {
	if s == nil || s.client == nil {
		return errors.New("switchboard: nil redis client")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	data, err := proto.Marshal(wrapper)
	if err != nil {
		return err
	}
	channel := account.Key(ctx, s.channel)
	return s.client.Publish(ctx, channel, data).Err()
}

// EnqueueOrchestration sends a JSON-encoded event to a Redis list for one-to-one delivery.
func (s *RedisSwitchboard) EnqueueOrchestration(ctx context.Context, queue string, event interface{}) error {
	if s == nil || s.client == nil {
		return errors.New("switchboard: nil redis client")
	}
	data, err := json.Marshal(event)
	if err != nil {
		return err
	}
	return s.client.RPush(ctx, queue, data).Err()
}

// BatchEnqueueOrchestration sends multiple JSON-encoded events to Redis lists
// in a single pipeline, reducing round-trips from N to 1 during spawn bursts.
func (s *RedisSwitchboard) BatchEnqueueOrchestration(ctx context.Context, items []QueueItem) error {
	if s == nil || s.client == nil {
		return errors.New("switchboard: nil redis client")
	}
	if len(items) == 0 {
		return nil
	}
	pipe := s.client.Pipeline()
	for _, item := range items {
		data, err := json.Marshal(item.Data)
		if err != nil {
			return err
		}
		pipe.RPush(ctx, item.Queue, data)
	}
	_, err := pipe.Exec(ctx)
	return err
}

// PublishOrchestration sends a JSON-encoded event to the orchestration Pub/Sub channel
// and dispatches to agents based on session-aware routing:
//   - If a registry is available, only agent types with active sessions are poked
//   - All agents receive /orchestration pokes (subscriber and callable alike)
//   - A2A message/send is reserved for targeted DispatchToAgent only
func (s *RedisSwitchboard) PublishOrchestration(ctx context.Context, channel string, event interface{}) error {
	if s == nil || s.client == nil {
		return errors.New("switchboard: nil redis client")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if s.lifecycleErr() != nil {
		return ErrSwitchboardClosed
	}

	data, err := json.Marshal(event)
	if err != nil {
		return err
	}

	var errs []error

	// 1. Performance Path: Redis Pub/Sub (subscriber agents listen here)
	if err := s.client.Publish(ctx, channel, data).Err(); err != nil {
		log.Printf("Switchboard: Redis PublishOrchestration failed: %v", err)
		errs = append(errs, fmt.Errorf("redis publish %q: %w", channel, err))
	}

	// 2. Session-Aware Dispatch: only poke agent types with active sessions
	if s.catalog != nil {
		agents, err := s.catalog.DiscoverAgents()
		if err != nil {
			log.Printf("Switchboard: catalog discovery failed: %v", err)
			errs = append(errs, fmt.Errorf("catalog discovery: %w", err))
			return errors.Join(errs...)
		}

		if s.registry != nil {
			// Session-aware: only dispatch to agent types that have active sessions
			activeTypes, err := s.registry.ActiveAgentTypes(ctx)
			if err != nil {
				log.Printf("Switchboard: ActiveAgentTypes failed: %v", err)
				errs = append(errs, fmt.Errorf("active agent types: %w", err))
				return errors.Join(errs...)
			}
			for _, agentType := range activeTypes {
				card, ok := agents[agentType]
				if !ok || card.URL == "" {
					log.Printf("Switchboard: Active agentType %q not found in catalog or has no URL", agentType)
					continue
				}
				// Route by URL type:
				//   - Agent Engine: A2A message/send (no /orchestration endpoint)
				//   - All local agents: HTTP poke to /orchestration via BaseURL
				asyncCtx, asyncCancel, ok := s.beginAsyncDispatch(ctx)
				if !ok {
					errs = append(errs, ErrSwitchboardClosed)
					break
				}
				if agent.IsAgentEngineURL(card.URL) {
					go func(c agent.AgentCard, d []byte) {
						defer s.wg.Done()
						defer asyncCancel()
						s.dispatchCallable(asyncCtx, c.URL, c.Name, d)
					}(card, data)
				} else {
					go func(c agent.AgentCard, d []byte) {
						defer s.wg.Done()
						defer asyncCancel()
						s.pokeOrchestrationEndpoint(asyncCtx, c.OrchestrationBaseURL(), c.Name, d)
					}(card, data)
				}
			}
		} else {
			for _, card := range agents {
				if card.URL == "" {
					continue
				}
				asyncCtx, asyncCancel, ok := s.beginAsyncDispatch(ctx)
				if !ok {
					errs = append(errs, ErrSwitchboardClosed)
					break
				}
				if agent.IsAgentEngineURL(card.URL) {
					go func(c agent.AgentCard, d []byte) {
						defer s.wg.Done()
						defer asyncCancel()
						s.dispatchCallable(asyncCtx, c.URL, c.Name, d)
					}(card, data)
				} else {
					go func(c agent.AgentCard, d []byte) {
						defer s.wg.Done()
						defer asyncCancel()
						s.pokeOrchestrationEndpoint(asyncCtx, c.OrchestrationBaseURL(), c.Name, d)
					}(card, data)
				}
			}
		}
	}

	return errors.Join(errs...)
}

// DispatchToAgent dispatches an orchestration event to a single agent by type.
// Uses dispatch-mode routing: subscriber agents get HTTP pokes, callable agents
// get A2A JSON-RPC message/send.
func (s *RedisSwitchboard) DispatchToAgent(ctx context.Context, agentType string, event interface{}) error {
	return s.dispatchAgentEvent(ctx, "dispatch", agentType, event)
}

func (s *RedisSwitchboard) dispatchAgentEvent(ctx context.Context, op, agentType string, event interface{}) error {
	if ctx != nil && ctx.Err() != nil {
		return ctx.Err()
	}
	if s == nil {
		return fmt.Errorf("switchboard: nil receiver for agent %s", op)
	}
	if s.lifecycleErr() != nil {
		return ErrSwitchboardClosed
	}
	data, err := json.Marshal(event)
	if err != nil {
		return err
	}

	if s.catalog == nil {
		return fmt.Errorf("no catalog available for agent %s", op)
	}

	agents, err := s.catalog.DiscoverAgents()
	if err != nil {
		return fmt.Errorf("catalog discovery failed: %w", err)
	}

	card, ok := agents[agentType]
	if !ok || card.URL == "" {
		log.Printf("Switchboard: Agent %q not found in catalog or has no URL", agentType)
		return fmt.Errorf("%w: %q", ErrAgentNotFound, agentType)
	}

	asyncCtx, asyncCancel, ok := s.beginAsyncDispatch(ctx)
	if !ok {
		return ErrSwitchboardClosed
	}
	if agent.IsAgentEngineURL(card.URL) {
		go func() {
			defer s.wg.Done()
			defer asyncCancel()
			s.dispatchCallable(asyncCtx, card.URL, card.Name, data)
		}()
	} else {
		go func() {
			defer s.wg.Done()
			defer asyncCancel()
			s.pokeOrchestrationEndpoint(asyncCtx, card.OrchestrationBaseURL(), card.Name, data)
		}()
	}

	return nil
}

// pokeOrchestrationEndpoint sends an HTTP POST to any agent's /orchestration
// endpoint, selecting the HTTP client based on URL:
//   - Local/http: plain HTTP client
//   - Cloud Run (https): OIDC ID-token authenticated client
//   - Agent Engine (aiplatform.googleapis.com): GCP-authenticated client
func (s *RedisSwitchboard) pokeOrchestrationEndpoint(ctx context.Context, agentURL, agentType string, data []byte) {
	if s == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	// Agents mount at root (each has its own port/service), so the
	// orchestration endpoint is directly at {base}/orchestration.
	pokeURL := strings.TrimRight(agentURL, "/") + "/orchestration"

	// Auth selection per URL kind:
	//  - Agent Engine: OAuth bearer (cloud-platform scope) via s.gcpClient (fail closed if nil).
	//  - Cloud Run (any other https://): OIDC ID token with audience =
	//    service URL (fail closed if OIDC client acquisition fails).
	//  - localhost / http://: plain client (local dev, no auth).
	client := s.httpClient
	if agent.IsAgentEngineURL(agentURL) {
		if s.gcpClient == nil {
			log.Printf("Switchboard: Agent Engine auth client unavailable for poke %s (%s): %v", agentType, pokeURL, s.gcpClientErr)
			return
		}
		client = s.gcpClient
	} else if auth.IsCloudRunURL(agentURL) {
		oidcClient, err := s.getOIDCClient(auth.AudienceFor(agentURL))
		if err != nil || oidcClient == nil {
			log.Printf("Switchboard: Cloud Run OIDC client unavailable for poke %s (%s): %v", agentType, pokeURL, err)
			return
		}
		client = oidcClient
	}
	if client == nil {
		log.Printf("Switchboard: nil HTTP client for poke %s (%s)", agentType, pokeURL)
		return
	}

	log.Printf("Switchboard: Poking %s at %s", agentType, pokeURL)

	reqCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, "POST", pokeURL, bytes.NewBuffer(data))
	if err != nil {
		log.Printf("Switchboard: Failed to create request for %s: %v", agentType, err)
		return
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		log.Printf("Switchboard: Poke failed for %s: %v", agentType, err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		snippet, readErr := io.ReadAll(io.LimitReader(resp.Body, 4096))
		if readErr != nil {
			log.Printf("Switchboard: Poke %s returned HTTP %d (body read error: %v)", agentType, resp.StatusCode, readErr)
		} else {
			log.Printf("Switchboard: Poke %s returned HTTP %d: %s", agentType, resp.StatusCode, string(snippet))
		}
		return
	}
	// Drain response body (bounded) to enable TCP connection reuse.
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
}

// PokeAgent dispatches an event to a single agent, routing by URL type:
//   - Agent Engine URLs: A2A message/send (Agent Engine has no /orchestration endpoint)
//   - All local agents (subscriber or callable): HTTP POST to /orchestration
//
// Local callable agents have /orchestration endpoints registered and their
// dispatcher correctly handles events like spawn_agent without invoking the
// LLM. Sending these as A2A message/send would cause the A2A executor to
// treat the event as a user message, triggering an unwanted LLM invocation.
func (s *RedisSwitchboard) PokeAgent(ctx context.Context, agentType string, event interface{}) error {
	return s.dispatchAgentEvent(ctx, "poke", agentType, event)
}

func extractStringField(m map[string]interface{}, keys ...string) string {
	if m == nil {
		return ""
	}
	for _, k := range keys {
		if raw, ok := m[k]; ok {
			if s, ok := raw.(string); ok {
				if trimmed := strings.TrimSpace(s); trimmed != "" {
					return trimmed
				}
			}
		}
	}
	return ""
}

func isValidRedisKeyComponent(v string) bool {
	if v == "" {
		return false
	}
	for i := 0; i < len(v); i++ {
		b := v[i]
		if b == ':' || b <= 0x20 || b >= 0x7f {
			return false
		}
	}
	return true
}

func extractDispatchUserID(ctx context.Context, event map[string]interface{}) string {
	if uc, ok := account.FromContextOK(ctx); ok {
		if uid := strings.TrimSpace(uc.UserID); uid != "" {
			return uid
		}
	}
	if payload, ok := event["payload"].(map[string]interface{}); ok {
		if uid := extractStringField(payload, "acting_user_id", "user_id", "userId"); uid != "" {
			return uid
		}
	}
	return extractStringField(event, "acting_user_id", "user_id", "userId")
}

// dispatchCallable sends an A2A JSON-RPC message/send request to a callable agent.
// Uses the retryable HTTP client with exponential backoff.
// For Agent Engine URLs, uses GCP-authenticated client.
func (s *RedisSwitchboard) dispatchCallable(ctx context.Context, agentURL, agentType string, eventData []byte) {
	if s == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	// Determine the A2A endpoint URL.
	// After catalog discovery, card.URL for non-AE agents includes the A2A
	// mount path (e.g., /a2a/simulator/). For AE agents, the URL is the
	// reasoning engine base URL. This method is only called for AE agents
	// in the current routing logic (agent.IsAgentEngineURL gate in callers).
	//   - Agent Engine: POST {base}/a2a/v1/message:send (Google API custom method pattern)
	//     Empirically verified: /a2a, /a2a/v1, /a2a/v1/ all return 404.
	//     Only /a2a/v1/message:send accepts POST (returns 400/200, not 404).
	//   - Non-AE (if ever reached): agentURL already contains the full A2A path
	a2aURL := strings.TrimRight(agentURL, "/")
	if agent.IsAgentEngineURL(agentURL) {
		a2aURL += "/a2a/v1/message:send"
	} else {
		a2aURL += "/"
	}

	// Extract event info for the message
	var event map[string]interface{}
	if err := json.Unmarshal(eventData, &event); err != nil {
		log.Printf("Switchboard: Failed to parse event for callable dispatch to %s: %v", agentType, err)
		return
	}

	sessionID, _ := event["sessionId"].(string)
	userID := extractDispatchUserID(ctx, event)

	// Write session→simulation mapping to the agent-side Redis key so
	// DashLogPlugin on the callable AE agent can resolve simulation_id.
	// Without this, callable dispatch bypasses the Python dispatcher entirely,
	// and simulation_registry.register() never runs — so all DashLogPlugin
	// events are emitted without simulation_id.
	if s.client != nil {
		if payload, ok := event["payload"].(map[string]interface{}); ok {
			if simID, ok := payload["simulation_id"].(string); ok && simID != "" && sessionID != "" {
				simregCtx := ctx
				if _, hasAcc := account.FromContextOK(simregCtx); !hasAcc && isValidRedisKeyComponent(userID) {
					simregCtx = account.WithContext(simregCtx, account.UserContext{UserID: userID})
				}
				uc, hasAcc := account.FromContextOK(simregCtx)
				saasOn := account.SaaSModeEnabled()
				if saasOn && (!hasAcc || !isValidRedisKeyComponent(strings.TrimSpace(uc.UserID))) {
					log.Printf("Switchboard: Skipping simreg write for callable %s session %s in SAAS_MODE=on without valid user_id", agentType, sessionID)
				} else if saasOn && !isValidRedisKeyComponent(sessionID) {
					log.Printf("Switchboard: Skipping simreg write for callable %s invalid session %q in SAAS_MODE=on", agentType, sessionID)
				} else {
					simregKey := account.Key(simregCtx, "simreg", "session", sessionID)
					if err := s.client.Set(ctx, simregKey, simID, 2*time.Hour).Err(); err != nil {
						log.Printf("Switchboard: Failed to write simreg for callable %s session %s: %v", agentType, sessionID, err)
					}
				}
			}
		}
	}

	// Fallback: for broadcast events, use the first target as session ID.
	// This ensures the A2A context_id matches the spawn session, providing
	// session continuity via VertexAiSessionService.
	if sessionID == "" {
		if payload, ok := event["payload"].(map[string]interface{}); ok {
			if targets, ok := payload["targets"].([]interface{}); ok && len(targets) > 0 {
				if t, ok := targets[0].(string); ok {
					sessionID = t
				}
			}
		}
	}

	// Construct request body based on agent type.
	// AE uses protobuf SendMessageRequest (proven via curl → HTTP 200):
	//   {request: {message_id, role: "ROLE_USER", content: [{text}], metadata?: {user_id}},
	//    configuration: {accepted_output_modes: ["text"]}}
	// Cloud Run uses standard A2A JSON-RPC envelope.
	var requestBody interface{}

	msgID := fmt.Sprintf("orch-%s-%d", sessionID, time.Now().UnixMilli())

	if agent.IsAgentEngineURL(agentURL) {
		// AE: SendMessageRequest proto (field is "request" not "message")
		// context_id is required — maps to session_id in VertexAiSessionService
		contextID := sessionID
		if contextID == "" {
			contextID = fmt.Sprintf("ctx-%d", time.Now().UnixMilli())
		}
		reqMsg := map[string]interface{}{
			"role":       "ROLE_USER",
			"message_id": msgID,
			"context_id": contextID,
			"content": []map[string]interface{}{
				{"text": string(eventData)},
			},
		}
		if userID != "" {
			reqMsg["metadata"] = map[string]interface{}{
				"user_id": userID,
			}
		}
		requestBody = map[string]interface{}{
			"request": reqMsg,
			"configuration": map[string]interface{}{
				"accepted_output_modes": []string{"text"},
				"blocking":              true,
			},
		}
	} else {
		// Cloud Run: standard A2A JSON-RPC
		msgMap := map[string]interface{}{
			"role":      "user",
			"messageId": msgID,
			"parts": []map[string]interface{}{
				{
					"kind": "text",
					"text": string(eventData),
				},
			},
		}
		if userID != "" {
			msgMap["metadata"] = map[string]interface{}{
				"user_id": userID,
			}
		}
		requestBody = map[string]interface{}{
			"jsonrpc": "2.0",
			"method":  "message/send",
			"id":      fmt.Sprintf("%s-%s", agentType, sessionID),
			"params": map[string]interface{}{
				"message": msgMap,
			},
		}
	}

	body, err := json.Marshal(requestBody)
	if err != nil {
		log.Printf("Switchboard: Failed to marshal A2A request for %s: %v", agentType, err)
		return
	}

	// Select HTTP client: GCP-authenticated for Agent Engine, OIDC for Cloud Run, retryable for local/dev
	httpClient := s.callableClient
	if agent.IsAgentEngineURL(agentURL) {
		if s.gcpClient == nil {
			log.Printf("Switchboard: Agent Engine auth client unavailable for callable %s (%s): %v", agentType, a2aURL, s.gcpClientErr)
			return
		}
		httpClient = s.gcpClient
	} else if auth.IsCloudRunURL(agentURL) {
		oidcClient, err := s.getOIDCClient(auth.AudienceFor(agentURL))
		if err != nil || oidcClient == nil {
			log.Printf("Switchboard: Cloud Run OIDC client unavailable for callable %s (%s): %v", agentType, a2aURL, err)
			return
		}
		httpClient = oidcClient
	}
	if httpClient == nil {
		log.Printf("Switchboard: nil HTTP client for callable %s (%s)", agentType, a2aURL)
		return
	}

	log.Printf("Switchboard: Dispatching A2A message/send to callable %s at %s (session: %s)", agentType, a2aURL, sessionID)

	reqCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, "POST", a2aURL, bytes.NewBuffer(body))
	if err != nil {
		log.Printf("Switchboard: Failed to create request for %s: %v", agentType, err)
		return
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := httpClient.Do(req)
	if err != nil {
		log.Printf("Switchboard: Callable dispatch failed for %s: %v", agentType, err)
		return
	}
	defer resp.Body.Close()

	respBody, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		log.Printf("Switchboard: error reading response from %s: %v", agentType, readErr)
	}
	if resp.StatusCode >= 400 {
		log.Printf("Switchboard: Callable %s returned HTTP %d: %s", agentType, resp.StatusCode, string(respBody))
		return
	}

	log.Printf("Switchboard: Callable %s dispatch OK (HTTP %d, %d bytes)", agentType, resp.StatusCode, len(respBody))

	// Relay AE response to the Hub so WebSocket clients (Tester UI) see it.
	// AE agents emit intermediate pulses (tool_start, model_start, narrative,
	// etc.) directly via Redis through the PSC-I network path. This relay
	// serves as a safety net for the FINAL response text only — it ensures
	// the A2A Task result reaches WebSocket clients even if the agent's
	// direct Redis pulse for the final response was lost or not emitted.
	if len(respBody) > 0 && ctx.Err() == nil {
		relayCtx := ctx
		if _, hasAcc := account.FromContextOK(relayCtx); !hasAcc && isValidRedisKeyComponent(userID) {
			relayCtx = account.WithContext(relayCtx, account.UserContext{UserID: userID})
		}
		s.relayCallableResponse(relayCtx, agentType, sessionID, respBody)
	}
}

// relayCallableResponse extracts text from an A2A SendMessageResponse and
// publishes it as a gateway.Wrapper to the Hub for WebSocket fan-out.
func (s *RedisSwitchboard) relayCallableResponse(ctx context.Context, agentType, sessionID string, body []byte) {
	if ctx == nil {
		ctx = context.Background()
	}
	if ctx.Err() != nil {
		return
	}
	var resp struct {
		Task struct {
			Artifacts []struct {
				Parts []struct {
					Text string `json:"text"`
				} `json:"parts"`
			} `json:"artifacts"`
		} `json:"task"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		log.Printf("Switchboard: Could not parse callable %s response for relay: %v", agentType, err)
		return
	}

	var texts []string
	for _, artifact := range resp.Task.Artifacts {
		for _, part := range artifact.Parts {
			if part.Text != "" {
				texts = append(texts, part.Text)
			}
		}
	}
	if len(texts) == 0 {
		return
	}

	// Look up the simulation UUID from the registry so the wrapper carries the
	// correct SimulationId for Hub simulation-based routing.  On Agent Engine
	// the sessionID is a numeric resource ID, not the simulation UUID.
	var simulationID string
	if s.registry != nil {
		simID, err := s.registry.FindSimulation(ctx, sessionID)
		if err != nil {
			if !errors.Is(err, context.Canceled) {
				log.Printf("Switchboard: FindSimulation failed for session %s: %v", sessionID, err)
			}
		} else if simID != "" {
			simulationID = simID
		}
	}

	for _, text := range texts {
		payload, err := json.Marshal(map[string]string{"text": text})
		if err != nil {
			log.Printf("Switchboard: Failed to marshal relay payload for %s: %v", agentType, err)
			continue
		}
		wrapper := &gateway.Wrapper{
			Timestamp:    time.Now().Format(time.RFC3339Nano),
			Type:         "json",
			RequestId:    fmt.Sprintf("ae-relay-%s-%d", agentType, time.Now().UnixMilli()),
			SessionId:    sessionID,
			Payload:      payload,
			SimulationId: simulationID,
			Origin: &gateway.Origin{
				Type:      "agent",
				Id:        agentType,
				SessionId: sessionID,
			},
			Destination: []string{sessionID},
			Status:      "success",
			Event:       "narrative",
		}
		if err := s.Broadcast(ctx, wrapper); err != nil {
			log.Printf("Switchboard: Failed to relay callable %s response to Hub: %v", agentType, err)
		} else {
			log.Printf("Switchboard: Relayed callable %s response to Hub (session: %s, %d chars)", agentType, sessionID, len(text))
		}
	}
}
