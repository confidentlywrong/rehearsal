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

package session

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestR2_InMemoryTrackSessionOverwriteCleansReverseIndexes(t *testing.T) {
	ctx := context.Background()
	svc := NewInMemorySessionService()

	require.NoError(t, svc.TrackSession(ctx, "sess-1", "agent-alpha", "sim-old"))
	require.NoError(t, svc.TrackSession(ctx, "sess-1", "agent-beta", "sim-new"))

	types, err := svc.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"agent-beta"}, types, "overwritten agent-alpha reverse index must be cleaned")

	sims, err := svc.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"sim-new"}, sims, "overwritten sim-old reverse index must be cleaned")

	// Also verify BatchTrackSessions overwrite cleanup.
	require.NoError(t, svc.BatchTrackSessions(ctx, []SessionTrackingEntry{
		{SessionID: "sess-1", AgentType: "agent-gamma", SimulationID: ""},
	}))

	types, err = svc.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Equal(t, []string{"agent-gamma"}, types)

	sims, err = svc.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Empty(t, sims, "clearing simulationID on overwrite must remove session from sim-new")
}

func TestR2_RedisSessionRegistryReapOver8000StaleSessions(t *testing.T) {
	s := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: s.Addr()})
	defer client.Close()

	ctx := context.Background()
	reg := NewRedisSessionRegistry(client, "test", 1*time.Hour)

	// Seed 8,500 stale session IDs directly into agent_sessions and sim_sessions
	// without corresponding session:* heartbeat keys. Prior to chunked SREM unpack,
	// >8,000 stale entries blew Lua 5.1's LUAI_MAXCSTACK limit.
	const staleCount = 8500
	members := make([]any, staleCount)
	for i := 0; i < staleCount; i++ {
		members[i] = fmt.Sprintf("stale-sess-%d", i)
	}
	require.NoError(t, client.SAdd(ctx, "test:active-agents", "runner_bulk").Err())
	require.NoError(t, client.SAdd(ctx, "test:agent-sessions:runner_bulk", members...).Err())
	require.NoError(t, client.SAdd(ctx, "test:active-simulations", "sim_bulk").Err())
	require.NoError(t, client.SAdd(ctx, "test:simulation:sim_bulk:sessions", members...).Err())

	reaped, err := reg.Reap(ctx)
	require.NoError(t, err)
	assert.Equal(t, staleCount, reaped)

	types, err := reg.ActiveAgentTypes(ctx)
	require.NoError(t, err)
	assert.Empty(t, types)

	sims, err := reg.ListSimulations(ctx)
	require.NoError(t, err)
	assert.Empty(t, sims)
}
