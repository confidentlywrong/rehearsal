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
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestT5_R12_ValidateSimulationID_EnforcesStrictCharsetAndLength(t *testing.T) {
	invalidIDs := []string{
		"sim:colon:inject",
		"sim*",
		"sim?",
		"sim[0]",
		"sim/slash",
		"sim space",
		"sim\nnewline",
		"sim\ttab",
		strings.Repeat("a", 129),
	}
	validIDs := []string{
		"",
		"sim-123_ABC",
		"s",
		strings.Repeat("z", 128),
	}

	inMem := NewInMemorySessionService()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()
	redisReg := NewRedisSessionRegistry(rdb, "t5r12", time.Hour)
	ctx := context.Background()

	for _, bad := range invalidIDs {
		err := ValidateSimulationID(bad)
		require.Error(t, err, "ValidateSimulationID(%q) must fail", bad)
		assert.True(t, errors.Is(err, ErrInvalidSimulationID))

		errMem := inMem.TrackSession(ctx, "sess-1", "planner", bad)
		require.Error(t, errMem, "InMemory TrackSession(%q) must fail", bad)
		assert.True(t, errors.Is(errMem, ErrInvalidSimulationID))

		errBatchMem := inMem.BatchTrackSessions(ctx, []SessionTrackingEntry{{
			SessionID:    "sess-1",
			AgentType:    "planner",
			SimulationID: bad,
		}})
		require.Error(t, errBatchMem, "InMemory BatchTrackSessions(%q) must fail", bad)
		assert.True(t, errors.Is(errBatchMem, ErrInvalidSimulationID))

		errRed := redisReg.TrackSession(ctx, "sess-1", "planner", bad)
		require.Error(t, errRed, "Redis TrackSession(%q) must fail", bad)
		assert.True(t, errors.Is(errRed, ErrInvalidSimulationID))

		errBatchRed := redisReg.BatchTrackSessions(ctx, []SessionTrackingEntry{{
			SessionID:    "sess-1",
			AgentType:    "planner",
			SimulationID: bad,
		}})
		require.Error(t, errBatchRed, "Redis BatchTrackSessions(%q) must fail", bad)
		assert.True(t, errors.Is(errBatchRed, ErrInvalidSimulationID))
	}

	for _, good := range validIDs {
		require.NoError(t, ValidateSimulationID(good), "ValidateSimulationID(%q) must pass", good)
		require.NoError(t, inMem.TrackSession(ctx, "sess-ok", "planner", good))
		require.NoError(t, redisReg.TrackSession(ctx, "sess-ok", "planner", good))
	}
}
