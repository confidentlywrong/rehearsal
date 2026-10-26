// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

package session

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

// SessionTrackingEntry holds a session-to-agent-type mapping for batch operations.
type SessionTrackingEntry struct {
	SessionID    string
	AgentType    string
	SimulationID string
}

// RedisSessionRegistry implements DistributedRegistry using Redis.
// Data model:
//   - {prefix}:session:{sessionID} → agentType (individual lookup, with TTL)
//   - {prefix}:agent-sessions:{agentType} → SET of sessionIDs (reverse index)
//   - {prefix}:active-agents → SET of agent types with ≥1 session
//
// Per-user model: when the request context carries a UserContext
// every key is additionally prefixed with `{ACCOUNT_KEY_PREFIX}:{user_id}:`
// so two users cannot see each other's sessions in the same Redis
// instance. When no user is on the context (internal cron, tests),
// the helpers emit the legacy un-prefixed format byte-for-byte.
type RedisSessionRegistry struct {
	client *redis.Client
	prefix string
	ttl    time.Duration
}

// NewRedisSessionRegistry creates a new Redis-backed session registry.
func NewRedisSessionRegistry(client *redis.Client, prefix string, ttl time.Duration) *RedisSessionRegistry {
	return &RedisSessionRegistry{
		client: client,
		prefix: prefix,
		ttl:    ttl,
	}
}

// effectivePrefix returns the namespace under which every key for
// the given context lives. When the context carries a UserContext
// the returned value is `{u}:{user_id}:{static_prefix}`; otherwise
// it is the bare static prefix (legacy behavior).
//
// Keeping the static prefix at the END preserves all of the existing
// SCAN patterns (e.g. `r.prefix + ":session:*"`) — they still work,
// they just live under a deeper namespace per user.
func (r *RedisSessionRegistry) effectivePrefix(ctx context.Context) string {
	if tp := account.KeyPrefix(ctx); tp != "" {
		return tp + ":" + r.prefix
	}
	return r.prefix
}

func (r *RedisSessionRegistry) sessionKey(ctx context.Context, sessionID string) string {
	return fmt.Sprintf("%s:session:%s", r.effectivePrefix(ctx), sessionID)
}

func (r *RedisSessionRegistry) agentSessionsKey(ctx context.Context, agentType string) string {
	return fmt.Sprintf("%s:agent-sessions:%s", r.effectivePrefix(ctx), agentType)
}

func (r *RedisSessionRegistry) activeAgentsKey(ctx context.Context) string {
	return fmt.Sprintf("%s:active-agents", r.effectivePrefix(ctx))
}

func (r *RedisSessionRegistry) simulationSessionsKey(ctx context.Context, simID string) string {
	return fmt.Sprintf("%s:simulation:%s:sessions", r.effectivePrefix(ctx), simID)
}

func (r *RedisSessionRegistry) sessionSimulationKey(ctx context.Context, sessionID string) string {
	return fmt.Sprintf("%s:session:%s:simulation", r.effectivePrefix(ctx), sessionID)
}

func (r *RedisSessionRegistry) activeSimulationsKey(ctx context.Context) string {
	return fmt.Sprintf("%s:active-simulations", r.effectivePrefix(ctx))
}

// TrackSession maps a session to an agent type and maintains reverse indexes.
func (r *RedisSessionRegistry) TrackSession(ctx context.Context, sessionID string, agentType string, simulationID string) error {
	pipe := r.client.Pipeline()
	pipe.Set(ctx, r.sessionKey(ctx, sessionID), agentType, r.ttl)
	pipe.SAdd(ctx, r.agentSessionsKey(ctx, agentType), sessionID)
	pipe.SAdd(ctx, r.activeAgentsKey(ctx), agentType)
	if simulationID != "" {
		pipe.SAdd(ctx, r.simulationSessionsKey(ctx, simulationID), sessionID)
		pipe.Expire(ctx, r.simulationSessionsKey(ctx, simulationID), r.ttl*2) // 2x session TTL for safety
		pipe.Set(ctx, r.sessionSimulationKey(ctx, sessionID), simulationID, r.ttl)
		pipe.SAdd(ctx, r.activeSimulationsKey(ctx), simulationID)
	}
	_, err := pipe.Exec(ctx)
	return err
}

// BatchTrackSessions registers multiple sessions in a single Redis pipeline,
// reducing round-trips from N to 1 during spawn bursts.
func (r *RedisSessionRegistry) BatchTrackSessions(ctx context.Context, sessions []SessionTrackingEntry) error {
	if len(sessions) == 0 {
		return nil
	}
	pipe := r.client.Pipeline()
	for _, s := range sessions {
		pipe.Set(ctx, r.sessionKey(ctx, s.SessionID), s.AgentType, r.ttl)
		pipe.SAdd(ctx, r.agentSessionsKey(ctx, s.AgentType), s.SessionID)
		pipe.SAdd(ctx, r.activeAgentsKey(ctx), s.AgentType)
		if s.SimulationID != "" {
			pipe.SAdd(ctx, r.simulationSessionsKey(ctx, s.SimulationID), s.SessionID)
			pipe.Expire(ctx, r.simulationSessionsKey(ctx, s.SimulationID), r.ttl*2)
			pipe.Set(ctx, r.sessionSimulationKey(ctx, s.SessionID), s.SimulationID, r.ttl)
			pipe.SAdd(ctx, r.activeSimulationsKey(ctx), s.SimulationID)
		}
	}
	_, err := pipe.Exec(ctx)
	return err
}

// FindAgentType returns the agent type for a session.
func (r *RedisSessionRegistry) FindAgentType(ctx context.Context, sessionID string) (string, bool, error) {
	val, err := r.client.Get(ctx, r.sessionKey(ctx, sessionID)).Result()
	if err == redis.Nil {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	return val, true, nil
}

// ActiveAgentTypes returns agent types that have at least one active session.
func (r *RedisSessionRegistry) ActiveAgentTypes(ctx context.Context) ([]string, error) {
	return r.client.SMembers(ctx, r.activeAgentsKey(ctx)).Result()
}

// FindSimulation returns the simulation ID for a given session.
func (r *RedisSessionRegistry) FindSimulation(ctx context.Context, sessionID string) (string, error) {
	val, err := r.client.Get(ctx, r.sessionSimulationKey(ctx, sessionID)).Result()
	if err == redis.Nil {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return val, nil
}

// ListSimulations returns all active simulation IDs from the active-simulations index SET.
// This is O(M) where M is the number of simulations, not O(N) over all Redis keys.
func (r *RedisSessionRegistry) ListSimulations(ctx context.Context) ([]string, error) {
	sims, err := r.client.SMembers(ctx, r.activeSimulationsKey(ctx)).Result()
	if err == redis.Nil {
		return nil, nil
	}
	return sims, err
}

// UntrackSession removes a session and updates reverse indexes.
func (r *RedisSessionRegistry) UntrackSession(ctx context.Context, sessionID string) error {
	// Look up agent type before deleting
	agentType, ok, err := r.FindAgentType(ctx, sessionID)
	if err != nil {
		return err
	}

	// Look up simulation ID before deleting
	simID, _ := r.FindSimulation(ctx, sessionID)

	pipe := r.client.Pipeline()
	pipe.Del(ctx, r.sessionKey(ctx, sessionID))
	pipe.Del(ctx, r.sessionSimulationKey(ctx, sessionID))

	if ok {
		pipe.SRem(ctx, r.agentSessionsKey(ctx, agentType), sessionID)
	}
	if simID != "" {
		pipe.SRem(ctx, r.simulationSessionsKey(ctx, simID), sessionID)
	}
	_, err = pipe.Exec(ctx)
	if err != nil {
		return err
	}

	if ok {
		script := redis.NewScript(`
			local count = redis.call('SCARD', KEYS[1])
			if count == 0 then
				redis.call('SREM', KEYS[2], ARGV[1])
			end
			return count
		`)
		err := script.Run(ctx, r.client, []string{r.agentSessionsKey(ctx, agentType), r.activeAgentsKey(ctx)}, agentType).Err()
		if err != nil && err != redis.Nil {
			return err
		}
	}

	// If the simulation set is now empty, clean it up and remove from active index.
	if simID != "" {
		script := redis.NewScript(`
			local count = redis.call('SCARD', KEYS[1])
			if count == 0 then
				redis.call('DEL', KEYS[1])
				redis.call('SREM', KEYS[2], ARGV[1])
			end
			return count
		`)
		err := script.Run(ctx, r.client, []string{r.simulationSessionsKey(ctx, simID), r.activeSimulationsKey(ctx)}, simID).Err()
		if err != nil && err != redis.Nil {
			return err
		}
	}

	return nil
}

// ListSessions returns all tracked session IDs.
//
// Opportunistic cleanup: before scanning, an best-effort Reap()
// pass runs to prune SET-index entries whose backing STRING keys
// have already expired via TTL. This replaces the former 30-minute
// background goroutine — orphan pointers are now cleared exactly
// when someone reads the index (typically the Agents tab refresh),
// honoring the project rule that nothing runs on a schedule. A
// Reap failure is logged and ignored so a transient Redis hiccup
// can never block the caller from seeing live session data.
func (r *RedisSessionRegistry) ListSessions(ctx context.Context) ([]string, error) {
	if _, err := r.Reap(ctx); err != nil {
		log.Printf("session/list: best-effort reap failed (continuing): %v", err)
	}
	var sessions []string
	prefix := r.effectivePrefix(ctx) + ":session:"
	iter := r.client.Scan(ctx, 0, prefix+"*", 0).Iterator()
	for iter.Next(ctx) {
		key := iter.Val()
		sid := strings.TrimPrefix(key, prefix)
		// Skip sub-keys like session:{id}:simulation
		if strings.Contains(sid, ":") {
			continue
		}
		sessions = append(sessions, sid)
	}
	if err := iter.Err(); err != nil {
		return nil, err
	}
	return sessions, nil
}

// Flush clears all session mappings and indexes, returning the number of
// individual session keys removed.
func (r *RedisSessionRegistry) Flush(ctx context.Context) (int, error) {
	// Get all active agent types to clean up their sets
	types, err := r.client.SMembers(ctx, r.activeAgentsKey(ctx)).Result()
	if err != nil && err != redis.Nil {
		return 0, err
	}

	pipe := r.client.Pipeline()
	// Delete all agent-sessions sets
	for _, agentType := range types {
		pipe.Del(ctx, r.agentSessionsKey(ctx, agentType))
	}
	// Delete the active-agents index
	pipe.Del(ctx, r.activeAgentsKey(ctx))
	_, err = pipe.Exec(ctx)
	if err != nil {
		return 0, err
	}

	// Delete simulation keys (collect all, then batch-delete via pipeline)
	var simKeys []string
	simIter := r.client.Scan(ctx, 0, r.effectivePrefix(ctx)+":simulation:*:sessions", 0).Iterator()
	for simIter.Next(ctx) {
		simKeys = append(simKeys, simIter.Val())
	}
	if len(simKeys) > 0 {
		simPipe := r.client.Pipeline()
		for _, sk := range simKeys {
			simPipe.Del(ctx, sk)
		}
		simPipe.Del(ctx, r.activeSimulationsKey(ctx))
		if _, err := simPipe.Exec(ctx); err != nil {
			return 0, err
		}
	} else {
		r.client.Del(ctx, r.activeSimulationsKey(ctx))
	}

	// Delete individual session keys (including :simulation sub-keys)
	count := 0
	prefix := r.effectivePrefix(ctx) + ":session:"
	iter := r.client.Scan(ctx, 0, prefix+"*", 0).Iterator()
	var keysToDelete []string
	for iter.Next(ctx) {
		key := iter.Val()
		keysToDelete = append(keysToDelete, key)
		// Only count primary session keys, not sub-keys
		sid := strings.TrimPrefix(key, prefix)
		if !strings.Contains(sid, ":") {
			count++
		}
	}
	if err := iter.Err(); err != nil {
		return count, err
	}

	if len(keysToDelete) > 0 {
		for i := 0; i < len(keysToDelete); i += 1000 {
			end := i + 1000
			if end > len(keysToDelete) {
				end = len(keysToDelete)
			}
			pipe := r.client.Pipeline()
			for _, k := range keysToDelete[i:end] {
				pipe.Del(ctx, k)
			}
			if _, err := pipe.Exec(ctx); err != nil {
				return count, err
			}
		}
	}
	return count, nil
}

// Reap prunes stale session entries from reverse index sets.
// When a session STRING key expires via TTL, the corresponding entries in
// agent-sessions SETs and active-agents SET are orphaned. Reap checks each
// entry and removes those whose session key no longer exists.
var reapAgentScript = redis.NewScript(`
	local setKey = KEYS[1]
	local activeKey = KEYS[2]
	local agentType = ARGV[1]
	local prefix = ARGV[2]
	
	local sessionIDs = redis.call("SMEMBERS", setKey)
	local staleCount = 0
	local staleSessions = {}
	
	for _, sid in ipairs(sessionIDs) do
		local sessionKey = prefix .. ":session:" .. sid
		if redis.call("EXISTS", sessionKey) == 0 then
			table.insert(staleSessions, sid)
			staleCount = staleCount + 1
		end
	end
	
	if staleCount > 0 then
		redis.call("SREM", setKey, unpack(staleSessions))
	end
	
	local remaining = redis.call("SCARD", setKey)
	if remaining == 0 then
		redis.call("DEL", setKey)
		redis.call("SREM", activeKey, agentType)
	end
	
	return staleCount
`)

var reapSimScript = redis.NewScript(`
	local setKey = KEYS[1]
	local activeKey = KEYS[2]
	local simID = ARGV[1]
	local prefix = ARGV[2]
	
	local sessionIDs = redis.call("SMEMBERS", setKey)
	local staleCount = 0
	local staleSessions = {}
	
	for _, sid in ipairs(sessionIDs) do
		local sessionKey = prefix .. ":session:" .. sid
		if redis.call("EXISTS", sessionKey) == 0 then
			table.insert(staleSessions, sid)
			staleCount = staleCount + 1
			local sessionSimKey = prefix .. ":session:" .. sid .. ":simulation"
			redis.call("DEL", sessionSimKey)
		end
	end
	
	if staleCount > 0 then
		redis.call("SREM", setKey, unpack(staleSessions))
	end
	
	local remaining = redis.call("SCARD", setKey)
	if remaining == 0 then
		redis.call("DEL", setKey)
		redis.call("SREM", activeKey, simID)
	end
	
	return staleCount
`)

func (r *RedisSessionRegistry) Reap(ctx context.Context) (int, error) {
	types, err := r.client.SMembers(ctx, r.activeAgentsKey(ctx)).Result()
	if err != nil {
		if err == redis.Nil {
			return 0, nil
		}
		return 0, err
	}

	reaped := 0
	for _, agentType := range types {
		val, err := reapAgentScript.Run(ctx, r.client,
			[]string{r.agentSessionsKey(ctx, agentType), r.activeAgentsKey(ctx)},
			agentType, r.effectivePrefix(ctx)).Int()
		if err != nil && err != redis.Nil {
			log.Printf("reap: lua error for agent %s: %v", agentType, err)
			continue
		}
		reaped += val
	}

	// Also reap orphaned simulation entries
	ep := r.effectivePrefix(ctx)
	simPattern := ep + ":simulation:*:sessions"
	simPrefixLen := len(ep + ":simulation:")
	simSuffixLen := len(":sessions")
	simIter := r.client.Scan(ctx, 0, simPattern, 0).Iterator()
	for simIter.Next(ctx) {
		simKey := simIter.Val()
		simID := simKey[simPrefixLen : len(simKey)-simSuffixLen]

		val, err := reapSimScript.Run(ctx, r.client,
			[]string{simKey, r.activeSimulationsKey(ctx)},
			simID, ep).Int()
		if err != nil && err != redis.Nil {
			log.Printf("session/reap: lua error for sim %s: %v", simKey, err)
			continue
		}
		// Assuming reaped count tracks agent sessions only based on original code,
		// but we can add simulation reaped count or ignore it as before.
		_ = val
	}

	return reaped, nil
}
