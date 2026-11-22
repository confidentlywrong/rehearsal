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
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestRedisRegistry_TrackSession_StoresAgentType(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Track two sessions for different agent types
	err := reg.TrackSession(ctx, "sess-1", "runner_autopilot", "")
	require.NoError(t, err)

	err = reg.TrackSession(ctx, "sess-2", "simulator", "")
	require.NoError(t, err)

	// FindAgentType should return the correct type for each session
	agentType, ok, err := reg.FindAgentType(ctx, "sess-1")
	require.NoError(t, err)
	assert.True(t, ok)
	assert.Equal(t, "runner_autopilot", agentType)

	agentType, ok, err = reg.FindAgentType(ctx, "sess-2")
	require.NoError(t, err)
	assert.True(t, ok)
	assert.Equal(t, "simulator", agentType)

	// Unknown session should return not-found
	_, ok, err = reg.FindAgentType(ctx, "nonexistent")
	require.NoError(t, err)
	assert.False(t, ok)
}

func TestRedisRegistry_ActiveAgentTypes_ReturnsOnlyTypesWithSessions(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Initially no active types
	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Empty(t, types)

	// Track sessions for runner and simulator
	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-3", "simulator", ""))

	types, err = reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Len(t, types, 2)
	assert.Contains(t, types, "runner_autopilot")
	assert.Contains(t, types, "simulator")
}

func TestRedisRegistry_UntrackSession_RemovesFromAgentTypeSet(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Track two runner sessions
	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "runner_autopilot", ""))

	// Remove one — runner should still be active
	require.NoError(t, reg.UntrackSession(ctx, "sess-1"))

	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Contains(t, types, "runner_autopilot")

	// Find should return not-found for untracked session
	_, ok, err := reg.FindAgentType(ctx, "sess-1")
	require.NoError(t, err)
	assert.False(t, ok)

	// Remove the last runner session — runner should no longer be active
	require.NoError(t, reg.UntrackSession(ctx, "sess-2"))

	types, err = reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.NotContains(t, types, "runner_autopilot")
}

func TestRedisRegistry_ListSessions_ReturnsAllTrackedSessions(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Track sessions across different agent types
	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-3", "runner_autopilot", ""))

	sessions, err := reg.ListSessions(ctx)
	require.NoError(t, err)
	assert.Len(t, sessions, 3)
	assert.ElementsMatch(t, []string{"sess-1", "sess-2", "sess-3"}, sessions)
}

func TestRedisRegistry_ListSessions_EmptyWhenNoSessions(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	sessions, err := reg.ListSessions(ctx)
	require.NoError(t, err)
	assert.Empty(t, sessions)
}

func TestRedisRegistry_Flush_ClearsAllData(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Track sessions across multiple agent types
	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-3", "runner_autopilot", ""))

	// Flush everything
	_, err := reg.Flush(ctx)
	require.NoError(t, err)

	// All lookups should return empty
	_, ok, err := reg.FindAgentType(ctx, "sess-1")
	require.NoError(t, err)
	assert.False(t, ok, "sess-1 should not be found after flush")

	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Empty(t, types, "no active agent types after flush")

	sessions, err := reg.ListSessions(ctx)
	require.NoError(t, err)
	assert.Empty(t, sessions, "no sessions after flush")
}

func TestRedisRegistry_Flush_IdempotentOnEmptyRegistry(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Flush with no data should not error
	count, err := reg.Flush(ctx)
	require.NoError(t, err)
	assert.Equal(t, 0, count)
}

func TestRedisRegistry_Reap_RemovesExpiredSessionsFromSets(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Track 3 sessions: 2 runners, 1 simulator
	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-3", "simulator", ""))

	// Expire sess-1 and sess-3 by deleting their session keys (simulates TTL)
	client.Del(ctx, "test:session:sess-1")
	client.Del(ctx, "test:session:sess-3")

	// Reap should clean up the stale entries
	reaped, err := reg.Reap(ctx)
	require.NoError(t, err)
	assert.Equal(t, 2, reaped)

	// Runner should still be active (sess-2 is alive)
	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Contains(t, types, "runner_autopilot")

	// Simulator should be gone (all its sessions expired)
	assert.NotContains(t, types, "simulator")

	// sess-2 should still be findable
	at, ok, err := reg.FindAgentType(ctx, "sess-2")
	require.NoError(t, err)
	assert.True(t, ok)
	assert.Equal(t, "runner_autopilot", at)
}

func TestRedisRegistry_Reap_IdempotentOnEmptyRegistry(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	reaped, err := reg.Reap(ctx)
	require.NoError(t, err)
	assert.Equal(t, 0, reaped)
}

func TestRedisRegistry_Reap_NoStaleEntries(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", ""))

	reaped, err := reg.Reap(ctx)
	require.NoError(t, err)
	assert.Equal(t, 0, reaped)

	// Both types should still be active
	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Len(t, types, 2)
}

func TestInMemoryRegistry_TrackSession_StoresAgentType(t *testing.T) {
	ctx := context.Background()
	reg := NewInMemorySessionService()

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))

	agentType, ok, err := reg.FindAgentType(ctx, "sess-1")
	require.NoError(t, err)
	assert.True(t, ok)
	assert.Equal(t, "runner_autopilot", agentType)
}

func TestInMemoryRegistry_Flush_ReturnsCount(t *testing.T) {
	ctx := context.Background()
	reg := NewInMemorySessionService()

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", ""))

	count, err := reg.Flush(ctx)
	require.NoError(t, err)
	assert.Equal(t, 2, count)
}

func TestRedisRegistry_Flush_ReturnsCount(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-3", "runner_autopilot", ""))

	count, err := reg.Flush(ctx)
	require.NoError(t, err)
	assert.Equal(t, 3, count)
}

func TestRedisRegistry_BatchTrackSessions_RegistersAll(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Batch-track 5 sessions across 2 agent types
	sessions := []SessionTrackingEntry{
		{SessionID: "sess-1", AgentType: "runner_autopilot"},
		{SessionID: "sess-2", AgentType: "runner_autopilot"},
		{SessionID: "sess-3", AgentType: "simulator"},
		{SessionID: "sess-4", AgentType: "runner_autopilot"},
		{SessionID: "sess-5", AgentType: "simulator"},
	}

	err := reg.BatchTrackSessions(ctx, sessions)
	require.NoError(t, err)

	// All sessions should be findable
	for _, s := range sessions {
		agentType, ok, err := reg.FindAgentType(ctx, s.SessionID)
		require.NoError(t, err)
		assert.True(t, ok, "session %s should be found", s.SessionID)
		assert.Equal(t, s.AgentType, agentType)
	}

	// Both agent types should be active
	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Len(t, types, 2)
	assert.Contains(t, types, "runner_autopilot")
	assert.Contains(t, types, "simulator")
}

func TestRedisRegistry_BatchTrackSessions_EmptySlice(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Empty batch should be a no-op
	err := reg.BatchTrackSessions(ctx, nil)
	require.NoError(t, err)

	err = reg.BatchTrackSessions(ctx, []SessionTrackingEntry{})
	require.NoError(t, err)
}

func TestRedisRegistry_BatchTrackSessions_InteroperatesWithTrackSession(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Mix individual and batch tracking
	require.NoError(t, reg.TrackSession(ctx, "individual-1", "runner_autopilot", ""))

	err := reg.BatchTrackSessions(ctx, []SessionTrackingEntry{
		{SessionID: "batch-1", AgentType: "runner_autopilot"},
		{SessionID: "batch-2", AgentType: "simulator"},
	})
	require.NoError(t, err)

	// All 3 sessions should be findable
	sessions, err := reg.ListSessions(ctx)
	require.NoError(t, err)
	assert.Len(t, sessions, 3)

	// Untrack works normally after batch track
	require.NoError(t, reg.UntrackSession(ctx, "batch-1"))
	at, ok, err := reg.FindAgentType(ctx, "batch-1")
	require.NoError(t, err)
	assert.False(t, ok)
	assert.Empty(t, at)
}

func TestInMemoryRegistry_ActiveAgentTypes(t *testing.T) {
	ctx := context.Background()
	reg := NewInMemorySessionService()

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", ""))

	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Len(t, types, 2)
	assert.Contains(t, types, "runner_autopilot")
	assert.Contains(t, types, "simulator")

	// Untrack runner → only simulator remains
	require.NoError(t, reg.UntrackSession(ctx, "sess-1"))
	types, err = reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Len(t, types, 1)
	assert.Contains(t, types, "simulator")
}

func TestRedisRegistry_TrackSessionWithSimulationID(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	err := reg.TrackSession(ctx, "sess-1", "runner_autopilot", "sim-abc")
	require.NoError(t, err)

	simID, err := reg.FindSimulation(ctx, "sess-1")
	require.NoError(t, err)
	assert.Equal(t, "sim-abc", simID)
}

func TestRedisRegistry_ListSimulations(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", "sim-abc"))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", "sim-abc"))
	require.NoError(t, reg.TrackSession(ctx, "sess-3", "runner_autopilot", "sim-xyz"))

	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Len(t, sims, 2)
	assert.ElementsMatch(t, []string{"sim-abc", "sim-xyz"}, sims)
}

func TestRedisRegistry_UntrackSession_CleansSimulation(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", "sim-abc"))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", "sim-abc"))

	require.NoError(t, reg.UntrackSession(ctx, "sess-1"))
	require.NoError(t, reg.UntrackSession(ctx, "sess-2"))

	simID, err := reg.FindSimulation(ctx, "sess-1")
	require.NoError(t, err)
	assert.Empty(t, simID)
}

func TestRedisRegistry_FindSimulation_NotTracked(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	simID, err := reg.FindSimulation(ctx, "nonexistent")
	require.NoError(t, err)
	assert.Empty(t, simID)
}

func TestRedisRegistry_Flush_ClearsSimulationData(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", "sim-abc"))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "simulator", "sim-abc"))

	_, err := reg.Flush(ctx)
	require.NoError(t, err)

	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Empty(t, sims)

	simID, err := reg.FindSimulation(ctx, "sess-1")
	require.NoError(t, err)
	assert.Empty(t, simID)
}

func TestRedisRegistry_SessionTTL_IsTenMinutes(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 2*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "ttl-sess-1", "runner_autopilot", ""))

	// Verify the session key has a TTL close to 2 hours
	ttl := s.TTL("test:session:ttl-sess-1")
	assert.Greater(t, ttl, 119*time.Minute, "TTL should be close to 2 hours")
	assert.LessOrEqual(t, ttl, 2*time.Hour, "TTL should not exceed 2 hours")
}

func TestRedisRegistry_BatchTrackSessions_WithSimulationID(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	entries := []SessionTrackingEntry{
		{SessionID: "sess-1", AgentType: "runner_autopilot", SimulationID: "sim-abc"},
		{SessionID: "sess-2", AgentType: "simulator", SimulationID: "sim-abc"},
		{SessionID: "sess-3", AgentType: "runner_autopilot", SimulationID: "sim-xyz"},
	}
	err := reg.BatchTrackSessions(ctx, entries)
	require.NoError(t, err)

	simID, err := reg.FindSimulation(ctx, "sess-1")
	require.NoError(t, err)
	assert.Equal(t, "sim-abc", simID)

	simID, err = reg.FindSimulation(ctx, "sess-3")
	require.NoError(t, err)
	assert.Equal(t, "sim-xyz", simID)

	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Len(t, sims, 2)
	assert.ElementsMatch(t, []string{"sim-abc", "sim-xyz"}, sims)
}

func TestRedisRegistry_ListSimulations_PrunesExpiredAfter2TTL(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", "sim-expired"))
	require.NoError(t, reg.TrackSession(ctx, "sess-2", "runner_autopilot", "sim-live"))

	// Simulate >2*ttl expiry of sim-expired: both session key and simulation:*:sessions expired,
	// leaving sim-expired stranded in active-simulations.
	require.NoError(t, client.Del(ctx, "test:session:sess-1", "test:session:sess-1:simulation", "test:simulation:sim-expired:sessions").Err())

	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"sim-live"}, sims, "expired simulation must be pruned and not cause permanent quota lockout")

	// Verify active-simulations SET was also cleaned in Redis.
	members, err := client.SMembers(ctx, "test:active-simulations").Result()
	require.NoError(t, err)
	assert.Equal(t, []string{"sim-live"}, members)
}

func TestRedisRegistry_Reap_PrunesActiveSimulationsWhenSimSetMissing(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "runner_autopilot", "sim-expired"))
	require.NoError(t, client.Del(ctx, "test:session:sess-1", "test:session:sess-1:simulation", "test:simulation:sim-expired:sessions").Err())

	_, err := reg.Reap(ctx)
	require.NoError(t, err)

	members, err := client.SMembers(ctx, "test:active-simulations").Result()
	require.NoError(t, err)
	assert.Empty(t, members)
}

func TestRedisRegistry_OverwriteCleansReverseIndexes(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	require.NoError(t, reg.TrackSession(ctx, "sess-1", "agent-alpha", "sim-old"))
	require.NoError(t, reg.TrackSession(ctx, "sess-1", "agent-beta", "sim-new"))

	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"agent-beta"}, types)

	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"sim-new"}, sims)

	simID, err := reg.FindSimulation(ctx, "sess-1")
	require.NoError(t, err)
	assert.Equal(t, "sim-new", simID)

	// Overwrite via BatchTrackSessions clearing simulationID
	require.NoError(t, reg.BatchTrackSessions(ctx, []SessionTrackingEntry{
		{SessionID: "sess-1", AgentType: "agent-gamma", SimulationID: ""},
	}))

	types, err = reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"agent-gamma"}, types)

	sims, err = reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Empty(t, sims)

	simID, err = reg.FindSimulation(ctx, "sess-1")
	require.NoError(t, err)
	assert.Empty(t, simID)
}

func TestRedisRegistry_RejectsInvalidInputsAndNilClient(t *testing.T) {
	ctx := context.Background()

	// Nil receiver and nil client must never panic
	var nilReg *RedisSessionRegistry
	for _, err := range []error{
		nilReg.TrackSession(ctx, "s1", "a1", ""),
		nilReg.BatchTrackSessions(ctx, []SessionTrackingEntry{{SessionID: "s1", AgentType: "a1"}}),
		nilReg.UntrackSession(ctx, "s1"),
	} {
		require.Error(t, err)
	}
	nilClientReg := NewRedisSessionRegistry(nil, "test", time.Hour)
	_, _, err := nilClientReg.FindAgentType(ctx, "s1")
	require.Error(t, err)
	_, err = nilClientReg.ActiveAgentTypes(ctx)
	require.Error(t, err)
	_, err = nilClientReg.FindSimulation(ctx, "s1")
	require.Error(t, err)
	_, err = nilClientReg.ListSimulations(ctx)
	require.Error(t, err)
	_, err = nilClientReg.ListSessions(ctx)
	require.Error(t, err)
	_, err = nilClientReg.Flush(ctx)
	require.Error(t, err)
	_, err = nilClientReg.Reap(ctx)
	require.Error(t, err)

	// Input validation
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()
	reg := NewRedisSessionRegistry(client, "test", time.Hour)

	for _, badSID := range []string{"", "foo:simulation", "has space", "ctrl\nchar"} {
		require.Error(t, reg.TrackSession(ctx, badSID, "runner", ""), "expected error for sessionID %q", badSID)
		require.Error(t, reg.BatchTrackSessions(ctx, []SessionTrackingEntry{{SessionID: badSID, AgentType: "runner"}}))
		_, _, ferr := reg.FindAgentType(ctx, badSID)
		require.Error(t, ferr)
		_, serr := reg.FindSimulation(ctx, badSID)
		require.Error(t, serr)
		require.Error(t, reg.UntrackSession(ctx, badSID))
	}
	for _, badAgent := range []string{"", "   ", "agent type"} {
		require.Error(t, reg.TrackSession(ctx, "valid-sid", badAgent, ""), "expected error for agentType %q", badAgent)
	}
}

func TestRedisRegistry_PropagatesRedisErrors(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()
	reg := NewRedisSessionRegistry(client, "test", time.Hour)
	ctx := context.Background()

	// Close miniredis to force connection errors
	s.Close()

	require.Error(t, reg.TrackSession(ctx, "s1", "a1", "sim-1"))
	require.Error(t, reg.UntrackSession(ctx, "s1"))
	_, err := reg.ListSimulations(ctx)
	require.Error(t, err)
	_, err = reg.Flush(ctx)
	require.Error(t, err)
	_, err = reg.Reap(ctx)
	require.Error(t, err)
}

func TestRedisRegistry_Flush_DeletesOrphanedAgentSessionSets(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", time.Hour)

	require.NoError(t, client.SAdd(ctx, "test:agent-sessions:orphan_type", "old-sess").Err())
	_, err := reg.Flush(ctx)
	require.NoError(t, err)

	exists, err := client.Exists(ctx, "test:agent-sessions:orphan_type").Result()
	require.NoError(t, err)
	assert.Equal(t, int64(0), exists)
}

func TestRedisRegistry_ConcurrentOverwrites_AtomicReverseIndexes(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	const workers = 16
	start := make(chan struct{})
	var wg sync.WaitGroup
	errs := make(chan error, workers)

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			<-start
			agentType := fmt.Sprintf("agent-%d", idx)
			simID := fmt.Sprintf("sim-%d", idx)
			if err := reg.TrackSession(ctx, "shared-sess", agentType, simID); err != nil {
				errs <- err
			}
		}(w)
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		require.NoError(t, err)
	}

	winningAgent, ok, err := reg.FindAgentType(ctx, "shared-sess")
	require.NoError(t, err)
	require.True(t, ok)

	winningSim, err := reg.FindSimulation(ctx, "shared-sess")
	require.NoError(t, err)
	require.NotEmpty(t, winningSim)

	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{winningAgent}, types, "concurrent overwrites must not leak phantom active-agents")

	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{winningSim}, sims, "concurrent overwrites must not leak phantom active-simulations")

	require.NoError(t, reg.UntrackSession(ctx, "shared-sess"))

	typesAfter, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Empty(t, typesAfter)

	simsAfter, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Empty(t, simsAfter)
}

func TestRedisRegistry_BatchTrackSessions_ConcurrentOverwrites_Atomic(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	const workers = 12
	start := make(chan struct{})
	var wg sync.WaitGroup
	errs := make(chan error, workers)

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			<-start
			entries := []SessionTrackingEntry{
				{
					SessionID:    "shared-batch-sess",
					AgentType:    fmt.Sprintf("batch-agent-%d", idx),
					SimulationID: fmt.Sprintf("batch-sim-%d", idx),
				},
				{
					SessionID:    fmt.Sprintf("owned-sess-%d", idx),
					AgentType:    "batch-shared-type",
					SimulationID: "batch-shared-sim",
				},
			}
			if err := reg.BatchTrackSessions(ctx, entries); err != nil {
				errs <- err
			}
		}(w)
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		require.NoError(t, err)
	}

	winningAgent, ok, err := reg.FindAgentType(ctx, "shared-batch-sess")
	require.NoError(t, err)
	require.True(t, ok)

	winningSim, err := reg.FindSimulation(ctx, "shared-batch-sess")
	require.NoError(t, err)
	require.NotEmpty(t, winningSim)

	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"batch-shared-type", winningAgent}, types)

	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.ElementsMatch(t, []string{"batch-shared-sim", winningSim}, sims)
}

func TestRedisRegistry_ConcurrentTrackAndUntrack_AtomicReverseIndexes(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	const iterations = 60
	for i := 0; i < iterations; i++ {
		require.NoError(t, reg.TrackSession(ctx, "sess-1", "agent-init", "sim-init"))

		start := make(chan struct{})
		var wg sync.WaitGroup
		wg.Add(2)

		var trackErr, untrackErr error
		go func() {
			defer wg.Done()
			<-start
			trackErr = reg.TrackSession(ctx, "sess-1", "agent-new", "sim-new")
		}()
		go func() {
			defer wg.Done()
			<-start
			untrackErr = reg.UntrackSession(ctx, "sess-1")
		}()

		close(start)
		wg.Wait()

		require.NoError(t, trackErr, "iter %d", i)
		require.NoError(t, untrackErr, "iter %d", i)

		agentType, ok, err := reg.FindAgentType(ctx, "sess-1")
		require.NoError(t, err, "iter %d", i)

		simID, err := reg.FindSimulation(ctx, "sess-1")
		require.NoError(t, err, "iter %d", i)

		types, err := reg.ActiveAgentTypes(ctx)
		require.NoError(t, err, "iter %d", i)

		sims, err := reg.ListSimulations(ctx)
		require.NoError(t, err, "iter %d", i)

		if !ok {
			assert.Empty(t, simID, "iter %d: simulation key must not outlive untracked session", i)
			assert.Empty(t, types, "iter %d: FindAgentType==false but ActiveAgentTypes leaked %v", i, types)
			assert.Empty(t, sims, "iter %d: FindAgentType==false but ListSimulations leaked %v", i, sims)
		} else {
			assert.Equal(t, "agent-new", agentType, "iter %d", i)
			assert.Equal(t, "sim-new", simID, "iter %d", i)
			assert.Equal(t, []string{"agent-new"}, types, "iter %d", i)
			assert.Equal(t, []string{"sim-new"}, sims, "iter %d", i)

			// Verify subsequent untrack also cleans up cleanly before next iteration.
			require.NoError(t, reg.UntrackSession(ctx, "sess-1"), "iter %d", i)
			typesAfter, err := reg.ActiveAgentTypes(ctx)
			require.NoError(t, err, "iter %d", i)
			assert.Empty(t, typesAfter, "iter %d", i)
			simsAfter, err := reg.ListSimulations(ctx)
			require.NoError(t, err, "iter %d", i)
			assert.Empty(t, simsAfter, "iter %d", i)
		}
	}
}

func TestRedisRegistry_UntrackSession_EdgeCasesAndScriptFlush(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// 1. Untracking a non-existent session is a clean no-op.
	require.NoError(t, reg.UntrackSession(ctx, "does-not-exist"))
	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Empty(t, types)
	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Empty(t, sims)

	// 2. Untracking a session with empty simulation ID cleans only its agent index.
	require.NoError(t, reg.TrackSession(ctx, "sess-nosim", "agent-only", ""))
	require.NoError(t, reg.TrackSession(ctx, "sess-other", "agent-other", "sim-other"))
	require.NoError(t, reg.UntrackSession(ctx, "sess-nosim"))

	_, ok, err := reg.FindAgentType(ctx, "sess-nosim")
	require.NoError(t, err)
	assert.False(t, ok)

	types, err = reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"agent-other"}, types)

	sims, err = reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"sim-other"}, sims)

	// 3. Cancelled context returns ctx.Err() without mutating Redis.
	cancelledCtx, cancel := context.WithCancel(ctx)
	cancel()
	require.ErrorIs(t, reg.UntrackSession(cancelledCtx, "sess-other"), context.Canceled)

	agentType, ok, err := reg.FindAgentType(ctx, "sess-other")
	require.NoError(t, err)
	assert.True(t, ok)
	assert.Equal(t, "agent-other", agentType)

	// 4. SCRIPT FLUSH (NOSCRIPT) automatically reloads and succeeds on UntrackSession.
	require.NoError(t, client.ScriptFlush(ctx).Err())
	require.NoError(t, reg.UntrackSession(ctx, "sess-other"))

	_, ok, err = reg.FindAgentType(ctx, "sess-other")
	require.NoError(t, err)
	assert.False(t, ok)

	types, err = reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Empty(t, types)

	sims, err = reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Empty(t, sims)
}
