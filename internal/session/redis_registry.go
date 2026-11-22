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

package session

import (
	"context"
	"errors"
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

var (
	trackSessionScript = redis.NewScript(`
		local sessionKey = KEYS[1]
		local sessionSimKey = KEYS[2]
		local newAgentSetKey = KEYS[3]
		local activeAgentsKey = KEYS[4]
		local activeSimsKey = KEYS[5]

		local sid = ARGV[1]
		local newAgent = ARGV[2]
		local newSim = ARGV[3]
		local prefix = ARGV[4]
		local ttlMs = tonumber(ARGV[5])

		local prevAgent = redis.call('GET', sessionKey)
		local prevSim = redis.call('GET', sessionSimKey)

		if prevAgent and prevAgent ~= '' and prevAgent ~= newAgent then
			local prevAgentSetKey = prefix .. ':agent-sessions:' .. prevAgent
			redis.call('SREM', prevAgentSetKey, sid)
			if redis.call('SCARD', prevAgentSetKey) == 0 then
				redis.call('DEL', prevAgentSetKey)
				redis.call('SREM', activeAgentsKey, prevAgent)
			end
		end

		if prevSim and prevSim ~= '' and prevSim ~= newSim then
			local prevSimSetKey = prefix .. ':simulation:' .. prevSim .. ':sessions'
			redis.call('SREM', prevSimSetKey, sid)
			if redis.call('SCARD', prevSimSetKey) == 0 then
				redis.call('DEL', prevSimSetKey)
				redis.call('SREM', activeSimsKey, prevSim)
			end
		end

		if ttlMs and ttlMs > 0 then
			redis.call('SET', sessionKey, newAgent, 'PX', ttlMs)
		else
			redis.call('SET', sessionKey, newAgent)
		end
		redis.call('SADD', newAgentSetKey, sid)
		redis.call('SADD', activeAgentsKey, newAgent)

		if newSim ~= '' then
			local newSimSetKey = prefix .. ':simulation:' .. newSim .. ':sessions'
			redis.call('SADD', newSimSetKey, sid)
			if ttlMs and ttlMs > 0 then
				redis.call('PEXPIRE', newSimSetKey, ttlMs * 2)
				redis.call('SET', sessionSimKey, newSim, 'PX', ttlMs)
			else
				redis.call('SET', sessionSimKey, newSim)
			end
			redis.call('SADD', activeSimsKey, newSim)
		else
			redis.call('DEL', sessionSimKey)
		end

		return 1
	`)
	untrackSessionScript = redis.NewScript(`
		local sessionKey = KEYS[1]
		local sessionSimKey = KEYS[2]
		local activeAgentsKey = KEYS[3]
		local activeSimsKey = KEYS[4]

		local sid = ARGV[1]
		local prefix = ARGV[2]

		local prevAgent = redis.call('GET', sessionKey)
		local prevSim = redis.call('GET', sessionSimKey)

		redis.call('DEL', sessionKey, sessionSimKey)

		if prevAgent and prevAgent ~= '' then
			local prevAgentSetKey = prefix .. ':agent-sessions:' .. prevAgent
			redis.call('SREM', prevAgentSetKey, sid)
			if redis.call('SCARD', prevAgentSetKey) == 0 then
				redis.call('DEL', prevAgentSetKey)
				redis.call('SREM', activeAgentsKey, prevAgent)
			end
		end

		if prevSim and prevSim ~= '' then
			local prevSimSetKey = prefix .. ':simulation:' .. prevSim .. ':sessions'
			redis.call('SREM', prevSimSetKey, sid)
			if redis.call('SCARD', prevSimSetKey) == 0 then
				redis.call('DEL', prevSimSetKey)
				redis.call('SREM', activeSimsKey, prevSim)
			end
		end

		return 1
	`)
)

func (r *RedisSessionRegistry) ensureClient() error {
	if r == nil || r.client == nil {
		return errors.New("session: nil redis client")
	}
	return nil
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

// TrackSession maps a session to an agent type and maintains reverse indexes,
// cleaning up any prior agent-type or simulation reverse-index entries on overwrite.
func (r *RedisSessionRegistry) TrackSession(ctx context.Context, sessionID string, agentType string, simulationID string) error {
	if err := r.ensureClient(); err != nil {
		return err
	}
	return r.BatchTrackSessions(ctx, []SessionTrackingEntry{{
		SessionID:    sessionID,
		AgentType:    agentType,
		SimulationID: simulationID,
	}})
}

// BatchTrackSessions registers multiple sessions in pipelined Redis round-trips,
// validating all inputs upfront and cleaning stale reverse indexes on overwrite.
func (r *RedisSessionRegistry) BatchTrackSessions(ctx context.Context, sessions []SessionTrackingEntry) error {
	if err := r.ensureClient(); err != nil {
		return err
	}
	if len(sessions) == 0 {
		return nil
	}
	for _, s := range sessions {
		if err := validateSessionID(s.SessionID); err != nil {
			return err
		}
		if err := validateAgentType(s.AgentType); err != nil {
			return err
		}
		if err := validateSimulationID(s.SimulationID); err != nil {
			return err
		}
	}

	var ttlMs int64
	if r.ttl > 0 {
		ttlMs = r.ttl.Milliseconds()
		if ttlMs == 0 {
			ttlMs = 1
		}
	}
	ep := r.effectivePrefix(ctx)
	activeAgentsKey := r.activeAgentsKey(ctx)
	activeSimsKey := r.activeSimulationsKey(ctx)

	if len(sessions) == 1 {
		s := sessions[0]
		keys := []string{
			r.sessionKey(ctx, s.SessionID),
			r.sessionSimulationKey(ctx, s.SessionID),
			r.agentSessionsKey(ctx, s.AgentType),
			activeAgentsKey,
			activeSimsKey,
		}
		return trackSessionScript.Run(ctx, r.client, keys, s.SessionID, s.AgentType, s.SimulationID, ep, ttlMs).Err()
	}

	runBatch := func() error {
		pipe := r.client.Pipeline()
		for _, s := range sessions {
			keys := []string{
				r.sessionKey(ctx, s.SessionID),
				r.sessionSimulationKey(ctx, s.SessionID),
				r.agentSessionsKey(ctx, s.AgentType),
				activeAgentsKey,
				activeSimsKey,
			}
			trackSessionScript.Run(ctx, pipe, keys, s.SessionID, s.AgentType, s.SimulationID, ep, ttlMs)
		}
		_, err := pipe.Exec(ctx)
		return err
	}

	err := runBatch()
	if err != nil && redis.HasErrorPrefix(err, "NOSCRIPT") {
		if loadErr := trackSessionScript.Load(ctx, r.client).Err(); loadErr != nil {
			return loadErr
		}
		err = runBatch()
	}
	return err
}

// FindAgentType returns the agent type for a session.
func (r *RedisSessionRegistry) FindAgentType(ctx context.Context, sessionID string) (string, bool, error) {
	if err := r.ensureClient(); err != nil {
		return "", false, err
	}
	if err := validateSessionID(sessionID); err != nil {
		return "", false, err
	}
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
	if err := r.ensureClient(); err != nil {
		return nil, err
	}
	return r.client.SMembers(ctx, r.activeAgentsKey(ctx)).Result()
}

// FindSimulation returns the simulation ID for a given session.
func (r *RedisSessionRegistry) FindSimulation(ctx context.Context, sessionID string) (string, error) {
	if err := r.ensureClient(); err != nil {
		return "", err
	}
	if err := validateSessionID(sessionID); err != nil {
		return "", err
	}
	val, err := r.client.Get(ctx, r.sessionSimulationKey(ctx, sessionID)).Result()
	if err == redis.Nil {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return val, nil
}

// ListSimulations returns all active simulation IDs from the active-simulations index SET,
// verifying liveness and pruning expired or empty simulation entries so TTL-expired
// simulations can never permanently lock out quota enforcement.
func (r *RedisSessionRegistry) ListSimulations(ctx context.Context) ([]string, error) {
	if err := r.ensureClient(); err != nil {
		return nil, err
	}
	sims, err := r.client.SMembers(ctx, r.activeSimulationsKey(ctx)).Result()
	if err == redis.Nil {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	ep := r.effectivePrefix(ctx)
	activeKey := r.activeSimulationsKey(ctx)
	live := make([]string, 0, len(sims))
	for _, simID := range sims {
		res, err := reapSimScript.Run(ctx, r.client, []string{r.simulationSessionsKey(ctx, simID), activeKey}, simID, ep).Int64Slice()
		if err != nil && err != redis.Nil {
			return nil, err
		}
		if len(res) >= 2 && res[1] > 0 {
			live = append(live, simID)
		}
	}
	return live, nil
}

// UntrackSession atomically removes a session and updates reverse indexes.
func (r *RedisSessionRegistry) UntrackSession(ctx context.Context, sessionID string) error {
	if err := r.ensureClient(); err != nil {
		return err
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := validateSessionID(sessionID); err != nil {
		return err
	}
	keys := []string{
		r.sessionKey(ctx, sessionID),
		r.sessionSimulationKey(ctx, sessionID),
		r.activeAgentsKey(ctx),
		r.activeSimulationsKey(ctx),
	}
	return untrackSessionScript.Run(ctx, r.client, keys, sessionID, r.effectivePrefix(ctx)).Err()
}

// ListSessions returns all tracked session IDs.
//
// Opportunistic cleanup: before scanning, a best-effort Reap()
// pass runs to prune SET-index entries whose backing STRING keys
// have already expired via TTL.
func (r *RedisSessionRegistry) ListSessions(ctx context.Context) ([]string, error) {
	if err := r.ensureClient(); err != nil {
		return nil, err
	}
	if _, err := r.Reap(ctx); err != nil {
		log.Printf("session/list: best-effort reap failed (continuing): %v", err)
	}
	var sessions []string
	ep := r.effectivePrefix(ctx)
	prefix := ep + ":session:"
	iter := r.client.Scan(ctx, 0, escapeRedisGlob(prefix)+"*", 0).Iterator()
	for iter.Next(ctx) {
		key := iter.Val()
		if !strings.HasPrefix(key, prefix) {
			continue
		}
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
	if err := r.ensureClient(); err != nil {
		return 0, err
	}
	ep := r.effectivePrefix(ctx)

	// Get all active agent types and scan agent-sessions:* to catch orphaned sets too.
	types, err := r.client.SMembers(ctx, r.activeAgentsKey(ctx)).Result()
	if err != nil && err != redis.Nil {
		return 0, err
	}
	agentSetKeys := make(map[string]struct{}, len(types))
	for _, agentType := range types {
		agentSetKeys[r.agentSessionsKey(ctx, agentType)] = struct{}{}
	}
	agentPrefix := ep + ":agent-sessions:"
	agentIter := r.client.Scan(ctx, 0, escapeRedisGlob(agentPrefix)+"*", 0).Iterator()
	for agentIter.Next(ctx) {
		k := agentIter.Val()
		if strings.HasPrefix(k, agentPrefix) {
			agentSetKeys[k] = struct{}{}
		}
	}
	if err := agentIter.Err(); err != nil {
		return 0, err
	}

	pipe := r.client.Pipeline()
	for k := range agentSetKeys {
		pipe.Del(ctx, k)
	}
	pipe.Del(ctx, r.activeAgentsKey(ctx))
	if _, err := pipe.Exec(ctx); err != nil {
		return 0, err
	}

	// Delete simulation keys (collect all, then batch-delete via pipeline)
	var simKeys []string
	simPrefix := ep + ":simulation:"
	simSuffix := ":sessions"
	simIter := r.client.Scan(ctx, 0, escapeRedisGlob(simPrefix)+"*:sessions", 0).Iterator()
	for simIter.Next(ctx) {
		k := simIter.Val()
		if strings.HasPrefix(k, simPrefix) && strings.HasSuffix(k, simSuffix) && len(k) > len(simPrefix)+len(simSuffix) {
			simKeys = append(simKeys, k)
		}
	}
	if err := simIter.Err(); err != nil {
		return 0, err
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
		if err := r.client.Del(ctx, r.activeSimulationsKey(ctx)).Err(); err != nil {
			return 0, err
		}
	}

	// Delete individual session keys (including :simulation sub-keys)
	count := 0
	prefix := ep + ":session:"
	iter := r.client.Scan(ctx, 0, escapeRedisGlob(prefix)+"*", 0).Iterator()
	var keysToDelete []string
	for iter.Next(ctx) {
		key := iter.Val()
		if !strings.HasPrefix(key, prefix) {
			continue
		}
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
			delPipe := r.client.Pipeline()
			for _, k := range keysToDelete[i:end] {
				delPipe.Del(ctx, k)
			}
			if _, err := delPipe.Exec(ctx); err != nil {
				return count, err
			}
		}
	}
	return count, nil
}

// Reap prunes stale session entries from reverse index sets.
// When a session STRING key expires via TTL, the corresponding entries in
// agent-sessions SETs, active-agents SET, simulation sets, and active-simulations SET
// are orphaned. Reap checks each entry and removes those whose backing keys no longer exist.
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
		for i = 1, staleCount, 1000 do
			local last = math.min(i + 999, staleCount)
			redis.call("SREM", setKey, unpack(staleSessions, i, last))
		end
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
		for i = 1, staleCount, 1000 do
			local last = math.min(i + 999, staleCount)
			redis.call("SREM", setKey, unpack(staleSessions, i, last))
		end
	end
	
	local remaining = redis.call("SCARD", setKey)
	if remaining == 0 then
		redis.call("DEL", setKey)
		redis.call("SREM", activeKey, simID)
	end
	
	return {staleCount, remaining}
`)

func (r *RedisSessionRegistry) Reap(ctx context.Context) (int, error) {
	if err := r.ensureClient(); err != nil {
		return 0, err
	}
	ep := r.effectivePrefix(ctx)
	var errs []error

	types, err := r.client.SMembers(ctx, r.activeAgentsKey(ctx)).Result()
	if err != nil && err != redis.Nil {
		return 0, err
	}

	reaped := 0
	for _, agentType := range types {
		val, err := reapAgentScript.Run(ctx, r.client,
			[]string{r.agentSessionsKey(ctx, agentType), r.activeAgentsKey(ctx)},
			agentType, ep).Int()
		if err != nil && err != redis.Nil {
			log.Printf("reap: lua error for agent %s: %v", agentType, err)
			errs = append(errs, fmt.Errorf("reap agent %s: %w", agentType, err))
			continue
		}
		reaped += val
	}

	// Reap orphaned simulation entries discovered via SCAN
	simPrefix := ep + ":simulation:"
	simSuffix := ":sessions"
	simPattern := escapeRedisGlob(simPrefix) + "*" + simSuffix
	seenSims := make(map[string]struct{})
	simIter := r.client.Scan(ctx, 0, simPattern, 0).Iterator()
	for simIter.Next(ctx) {
		simKey := simIter.Val()
		if !strings.HasPrefix(simKey, simPrefix) || !strings.HasSuffix(simKey, simSuffix) || len(simKey) <= len(simPrefix)+len(simSuffix) {
			continue
		}
		simID := simKey[len(simPrefix) : len(simKey)-len(simSuffix)]
		seenSims[simID] = struct{}{}

		if _, err := reapSimScript.Run(ctx, r.client,
			[]string{simKey, r.activeSimulationsKey(ctx)},
			simID, ep).Int64Slice(); err != nil && err != redis.Nil {
			log.Printf("session/reap: lua error for sim %s: %v", simKey, err)
			errs = append(errs, fmt.Errorf("reap sim %s: %w", simID, err))
		}
	}
	if err := simIter.Err(); err != nil {
		errs = append(errs, err)
	}

	// Also inspect active-simulations directly so simulations whose :sessions SET
	// already expired via 2*ttl are pruned from active-simulations.
	activeSims, err := r.client.SMembers(ctx, r.activeSimulationsKey(ctx)).Result()
	if err != nil && err != redis.Nil {
		errs = append(errs, err)
	} else {
		for _, simID := range activeSims {
			if _, ok := seenSims[simID]; ok {
				continue
			}
			if _, err := reapSimScript.Run(ctx, r.client,
				[]string{r.simulationSessionsKey(ctx, simID), r.activeSimulationsKey(ctx)},
				simID, ep).Int64Slice(); err != nil && err != redis.Nil {
				log.Printf("session/reap: lua error for active sim %s: %v", simID, err)
				errs = append(errs, fmt.Errorf("reap active sim %s: %w", simID, err))
			}
		}
	}

	return reaped, errors.Join(errs...)
}
