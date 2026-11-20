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
	"errors"
	"log"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	// subscriptionTTL is the expiry applied to subscription keys in Redis.
	// Keys are refreshed on each Subscribe call.
	subscriptionTTL = 2 * time.Hour

	// subscriptionTimeout is the context timeout for individual Redis calls
	// from within the Hub's Run() loop to keep them non-blocking.
	subscriptionTimeout = 2 * time.Second
)

// SubscriptionStore persists simulation subscription state so that it
// survives WebSocket reconnections to different gateway instances.
type SubscriptionStore interface {
	// Subscribe registers a session as interested in a simulation's messages.
	Subscribe(ctx context.Context, simulationID, sessionID string) error

	// Unsubscribe removes a session's interest in a simulation.
	Unsubscribe(ctx context.Context, simulationID, sessionID string) error

	// UnsubscribeAll removes a session from all simulation subscriptions.
	// Called when a WebSocket disconnects.
	UnsubscribeAll(ctx context.Context, sessionID string) error

	// Lookup returns all session IDs subscribed to a simulation.
	Lookup(ctx context.Context, simulationID string) ([]string, error)
}

// TenantSubscriptionStore extends SubscriptionStore with user-scoped
// persistence so two tenants sharing a simulationID never collide in Redis.
type TenantSubscriptionStore interface {
	SubscriptionStore
	SubscribeForUser(ctx context.Context, userID, simulationID, sessionID string) error
	UnsubscribeForUser(ctx context.Context, userID, simulationID, sessionID string) error
	UnsubscribeAllForUser(ctx context.Context, userID, sessionID string) error
	LookupForUser(ctx context.Context, userID, simulationID string) ([]string, error)
}

func isValidSubKeyPart(v string) bool {
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

// fwdKeyForUser returns the Redis key for the forward index: sim → sessions.
func fwdKeyForUser(userID, simulationID string) string {
	uid := strings.TrimSpace(userID)
	if uid == "" {
		return "sim_sub:" + simulationID
	}
	return "u:" + uid + ":sim_sub:" + simulationID
}

// revKeyForUser returns the Redis key for the reverse index: session → sims.
func revKeyForUser(userID, sessionID string) string {
	uid := strings.TrimSpace(userID)
	if uid == "" {
		return "sim_sub_rev:" + sessionID
	}
	return "u:" + uid + ":sim_sub_rev:" + sessionID
}

// RedisSubscriptionStore persists subscriptions in Redis SETs.
type RedisSubscriptionStore struct {
	client *redis.Client
}

// NewRedisSubscriptionStore creates a subscription store backed by Redis.
func NewRedisSubscriptionStore(client *redis.Client) *RedisSubscriptionStore {
	return &RedisSubscriptionStore{client: client}
}

// Subscribe adds sessionID to the simulation's subscriber set and maintains
// a reverse index for efficient UnsubscribeAll. Both keys get a TTL refresh.
func (s *RedisSubscriptionStore) Subscribe(ctx context.Context, simulationID, sessionID string) error {
	return s.SubscribeForUser(ctx, "", simulationID, sessionID)
}

// SubscribeForUser adds sessionID to the user-scoped simulation subscriber set.
func (s *RedisSubscriptionStore) SubscribeForUser(ctx context.Context, userID, simulationID, sessionID string) error {
	if s == nil || s.client == nil {
		return errors.New("subscription store: nil redis client")
	}
	if simulationID == "" || sessionID == "" {
		return errors.New("subscription store: simulationID and sessionID must be non-empty")
	}
	uid := strings.TrimSpace(userID)
	if uid != "" && (!isValidSubKeyPart(uid) || !isValidSubKeyPart(simulationID) || !isValidSubKeyPart(sessionID)) {
		return errors.New("subscription store: invalid key component")
	}
	pipe := s.client.Pipeline()
	fk := fwdKeyForUser(uid, simulationID)
	rk := revKeyForUser(uid, sessionID)

	pipe.SAdd(ctx, fk, sessionID)
	pipe.Expire(ctx, fk, subscriptionTTL)
	pipe.SAdd(ctx, rk, simulationID)
	pipe.Expire(ctx, rk, subscriptionTTL)

	_, err := pipe.Exec(ctx)
	return err
}

// Unsubscribe removes sessionID from a single simulation's subscriber set.
func (s *RedisSubscriptionStore) Unsubscribe(ctx context.Context, simulationID, sessionID string) error {
	return s.UnsubscribeForUser(ctx, "", simulationID, sessionID)
}

// UnsubscribeForUser removes sessionID from a user-scoped simulation's subscriber set.
func (s *RedisSubscriptionStore) UnsubscribeForUser(ctx context.Context, userID, simulationID, sessionID string) error {
	if s == nil || s.client == nil {
		return errors.New("subscription store: nil redis client")
	}
	if simulationID == "" || sessionID == "" {
		return errors.New("subscription store: simulationID and sessionID must be non-empty")
	}
	uid := strings.TrimSpace(userID)
	if uid != "" && (!isValidSubKeyPart(uid) || !isValidSubKeyPart(simulationID) || !isValidSubKeyPart(sessionID)) {
		return errors.New("subscription store: invalid key component")
	}
	pipe := s.client.Pipeline()
	pipe.SRem(ctx, fwdKeyForUser(uid, simulationID), sessionID)
	pipe.SRem(ctx, revKeyForUser(uid, sessionID), simulationID)
	_, err := pipe.Exec(ctx)
	return err
}

// UnsubscribeAll reads the reverse index for sessionID, removes the session
// from every simulation's forward set, then deletes the reverse key.
func (s *RedisSubscriptionStore) UnsubscribeAll(ctx context.Context, sessionID string) error {
	return s.UnsubscribeAllForUser(ctx, "", sessionID)
}

// UnsubscribeAllForUser removes sessionID from every simulation subscription scoped to userID.
func (s *RedisSubscriptionStore) UnsubscribeAllForUser(ctx context.Context, userID, sessionID string) error {
	if s == nil || s.client == nil {
		return errors.New("subscription store: nil redis client")
	}
	if sessionID == "" {
		return errors.New("subscription store: sessionID must be non-empty")
	}
	uid := strings.TrimSpace(userID)
	if uid != "" && (!isValidSubKeyPart(uid) || !isValidSubKeyPart(sessionID)) {
		return errors.New("subscription store: invalid key component")
	}
	rk := revKeyForUser(uid, sessionID)

	// Read all simulations this session was subscribed to
	simIDs, err := s.client.SMembers(ctx, rk).Result()
	if err != nil {
		return err
	}

	if len(simIDs) == 0 {
		return nil
	}

	pipe := s.client.Pipeline()
	for _, simID := range simIDs {
		pipe.SRem(ctx, fwdKeyForUser(uid, simID), sessionID)
	}
	pipe.Del(ctx, rk)
	_, err = pipe.Exec(ctx)
	return err
}

// Lookup returns all session IDs subscribed to a simulation.
func (s *RedisSubscriptionStore) Lookup(ctx context.Context, simulationID string) ([]string, error) {
	return s.LookupForUser(ctx, "", simulationID)
}

// LookupForUser returns all session IDs subscribed to a user-scoped simulation.
func (s *RedisSubscriptionStore) LookupForUser(ctx context.Context, userID, simulationID string) ([]string, error) {
	if s == nil || s.client == nil {
		return nil, errors.New("subscription store: nil redis client")
	}
	if simulationID == "" {
		return nil, errors.New("subscription store: simulationID must be non-empty")
	}
	uid := strings.TrimSpace(userID)
	if uid != "" && (!isValidSubKeyPart(uid) || !isValidSubKeyPart(simulationID)) {
		return nil, errors.New("subscription store: invalid key component")
	}
	return s.client.SMembers(ctx, fwdKeyForUser(uid, simulationID)).Result()
}

// NullSubscriptionStore is a no-op implementation for use when Redis is
// unavailable (local dev without docker) or in tests that don't care about
// cross-instance subscriptions.
type NullSubscriptionStore struct{}

func (n *NullSubscriptionStore) Subscribe(ctx context.Context, simulationID, sessionID string) error {
	return nil
}

func (n *NullSubscriptionStore) SubscribeForUser(ctx context.Context, userID, simulationID, sessionID string) error {
	return nil
}

func (n *NullSubscriptionStore) Unsubscribe(ctx context.Context, simulationID, sessionID string) error {
	return nil
}

func (n *NullSubscriptionStore) UnsubscribeForUser(ctx context.Context, userID, simulationID, sessionID string) error {
	return nil
}

func (n *NullSubscriptionStore) UnsubscribeAll(ctx context.Context, sessionID string) error {
	return nil
}

func (n *NullSubscriptionStore) UnsubscribeAllForUser(ctx context.Context, userID, sessionID string) error {
	return nil
}

func (n *NullSubscriptionStore) Lookup(ctx context.Context, simulationID string) ([]string, error) {
	return nil, nil
}

func (n *NullSubscriptionStore) LookupForUser(ctx context.Context, userID, simulationID string) ([]string, error) {
	return nil, nil
}

// logSubscriptionError logs a subscription store error without blocking
// the Hub's event loop. Subscription store failures are non-fatal — the
// local in-memory maps continue to work for same-instance routing.
func logSubscriptionError(op string, err error) {
	if err != nil {
		log.Printf("Hub: SubscriptionStore.%s failed (non-fatal): %v", op, err)
	}
}
