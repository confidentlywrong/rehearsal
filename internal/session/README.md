# Session management

The `session` package tracks which agent sessions are active across gateway
instances, what agent type each session belongs to, and which simulation it's
part of. The gateway uses this to make routing decisions: which agents to poke
during a broadcast, which sessions to include in a spawn batch, and which
simulation a response belongs to.

## Registry interface and implementations

The package defines **`DistributedRegistry`**, which maps session IDs to agent
types and simulation IDs so the gateway and switchboard can route broadcasts and
enforce per-tenant concurrency quotas across horizontally scaled instances.

- **`RedisSessionRegistry`** is the production Redis-backed implementation
  using per-user key namespacing (`account.KeyPrefix(ctx)`).
- **`InMemorySessionService`** is the in-memory implementation used for local
  development and tests, maintaining the same per-user namespace isolation and
  TTL eviction semantics.

## Redis data model

The `RedisSessionRegistry` uses STRINGs and SETs (no sorted sets, no hashes):

```mermaid
flowchart LR
    subgraph "Primary keys (STRING, TTL = 2h)"
        sessKey["session:{id}<br/>value = agentType"]
        simKey["session:{id}:simulation<br/>value = simulationID"]
    end

    subgraph "Reverse indexes (SET, no TTL)"
        agentSet["agent-sessions:{type}<br/>members = session IDs"]
        activeAgents["active-agents<br/>members = agent types"]
    end

    subgraph "Simulation indexes (SET, TTL = 4h)"
        simSet["simulation:{simID}:sessions<br/>members = session IDs"]
        activeSims["active-simulations<br/>members = simulation IDs"]
    end

    sessKey -.->|"reverse lookup"| agentSet
    agentSet -.->|"membership"| activeAgents
    simKey -.->|"reverse lookup"| simSet
    simSet -.->|"membership"| activeSims

    classDef primary fill:#E8F5E9,stroke:#2E7D32,stroke-width:2px
    classDef index fill:#F3E5F5,stroke:#6A1B9A,stroke-width:2px
    classDef simIdx fill:#E3F2FD,stroke:#1565C0,stroke-width:2px

    class sessKey,simKey primary
    class agentSet,activeAgents index
    class simSet,activeSims simIdx
```

| Key pattern | Type | TTL | Purpose |
|:------------|:-----|:----|:--------|
| `{prefix}:session:{id}` | STRING | 2h | Session-to-agent-type lookup |
| `{prefix}:session:{id}:simulation` | STRING | 2h | Session-to-simulation lookup |
| `{prefix}:agent-sessions:{type}` | SET | none | Which sessions belong to an agent type |
| `{prefix}:active-agents` | SET | none | Which agent types have active sessions |
| `{prefix}:simulation:{simID}:sessions` | SET | 4h | Which sessions belong to a simulation |
| `{prefix}:active-simulations` | SET | none | Which simulations have active sessions |

### Why two TTL tiers?

STRING keys (the primary records) expire after 2 hours via Redis TTL. SET keys
for agent-type indexes have no TTL and are cleaned lazily by `Reap()`.
Simulation SET keys get double the session TTL (4 hours) as a safety margin:
if a session's STRING key expires at the 2-hour mark, the simulation SET
still has 2 hours of runway before it disappears, giving `Reap()` time to
clean it up properly.

## How sessions are tracked

### Single session

`TrackSession` registers one session in a single Redis pipeline (one
round-trip):

```go
reg.TrackSession(ctx, "session-uuid", "runner_autopilot", "sim-uuid")
```

This writes the session STRING, the simulation STRING, adds the session to
both reverse-index SETs, and adds the agent type and simulation ID to their
respective active-index SETs. All six writes go in one pipeline.

### Batch spawn

When the gateway spawns hundreds of runners at once, `BatchTrackSessions`
pipelines all writes into a single Redis round-trip:

```go
entries := []session.SessionTrackingEntry{
    {SessionID: "uuid-1", AgentType: "runner_autopilot", SimulationID: "sim-1"},
    {SessionID: "uuid-2", AgentType: "runner_autopilot", SimulationID: "sim-1"},
    // ... hundreds more
}
reg.BatchTrackSessions(ctx, entries)
```

This avoids the N-round-trip problem during spawn bursts where N can be 100+.

## Routing queries

The gateway and switchboard use three query methods to make routing decisions:

- **`FindAgentType(sessionID)`** -- returns the agent type for a session, used
  by the switchboard to route responses back to the correct agent
- **`ActiveAgentTypes()`** -- returns agent types that have at least one active
  session, used during broadcast to skip poking agents with no sessions
- **`FindSimulation(sessionID)`** -- returns the simulation ID for a session,
  used to attach `SimulationId` to protobuf response wrappers for
  subscription-based routing in the hub

## Cleanup: flush and reap

### Flush (nuclear reset)

`Flush()` deletes everything: all session STRINGs, all SETs, all indexes. Used
during environment reset (the `/api/v1/reset` endpoint).

### Reap (garbage collection)

`Reap()` cleans up orphaned SET entries whose STRING keys have already expired
via TTL. It runs two sweeps:

1. **Agent session sweep**: for each agent type, pipelines `EXISTS` checks for
   all member sessions. Removes stale entries. If the SET is now empty, removes
   the agent type from the active-agents index.

2. **Simulation sweep**: SCANs for `simulation:*:sessions` SET keys AND inspects
   `active-simulations`, checks each member session, removes stale entries, and
   cleans up empty or expired simulation entries from `active-simulations`.

Cleanup runs opportunistically on read paths (`ListSessions` calls `Reap`, and
`ListSimulations` verifies live simulation sets in O(M)) rather than in a
background goroutine.

## File layout

```
internal/session/
├── service.go             # DistributedRegistry interface, InMemorySessionService
├── redis_registry.go      # RedisSessionRegistry (production implementation)
├── service_test.go        # InMemorySessionService tests
├── redis_registry_test.go # Redis tests using miniredis
└── r2_audit_test.go       # Regression tests
```

## Configuration

| Parameter | Production value | Description |
|:----------|:-----------------|:------------|
| `prefix` | `"rehearsal"` (or configured `REDIS_PREFIX`) | Redis key prefix for namespace isolation |
| `ttl` | `2 * time.Hour` | Expiry for session STRING keys |
| `REDIS_ADDR` | (from `.env`) | Redis connection address |

## Design decisions

**STRINGs with TTL for primary records, SETs for indexes.** The primary
session-to-agent-type mapping is a simple STRING that auto-expires. Reverse
indexes (SETs) track membership for efficient queries like "which agent types
are active?" but don't auto-expire because Redis SET TTL would delete all
members at once. Instead, individual entries are cleaned lazily by `Reap()` and
`ListSimulations()`.

**Pipeline everything.** Write operations (`TrackSession`, `BatchTrackSessions`)
use Redis pipelines for minimal round-trips while still cleaning up stale
reverse indexes when an existing session ID is overwritten.

**InMemorySessionService for local dev and tests.** Local development doesn't
need Redis. The in-memory implementation satisfies `DistributedRegistry` with
matching per-user namespace isolation (`SAAS_MODE`) and TTL eviction.

## Further reading

- [Redis pipelining](https://redis.io/docs/latest/develop/use/pipelining/) --
  the technique behind batch operations
- [Redis key expiration](https://redis.io/docs/latest/develop/use/keyspace-notifications/) --
  how TTL-based cleanup works
- The hub package ([internal/hub/](../hub/)) consumes the registry through the
  switchboard for broadcast routing
- The gateway ([cmd/gateway/](../../cmd/gateway/)) instantiates the registry
