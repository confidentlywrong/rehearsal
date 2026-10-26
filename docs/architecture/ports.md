# Port Allocation

This document is the source of truth for which **The Rehearsal** service binds
which port locally. The values match `.env.example`. If they ever drift, treat
`.env.example` as authoritative and file an issue against this doc.

## Default port range

All services bind in the **9101–9125** range on `127.0.0.1`. Pick this range
for any new local service you add so it stays out of the way of common dev
tools (8080, 8000, 5000) and so that `make stop` can reap stragglers
predictably.

## Service map

| Service | Port | Env var | Notes |
| :--- | :--- | :--- | :--- |
| Gateway | 9101 | `GATEWAY_PORT` | WebSocket hub and A2A router. |
| Redis | 9102 | `REDIS_PORT` | Session state and broadcast fan-out. |
| Pub/Sub emulator | 9103 | `PUBSUB_PORT` | Agent debug-log topic. |
| Simulator | 9104 | `SIMULATOR_PORT` | Simulation lifecycle and tick orchestration. |
| Planner | 9105 | `PLANNER_PORT` | Baseline route + spatial planning. |
| Planner (with eval) | 9106 | `PLANNER_WITH_EVAL_PORT` | Planner variant with evaluation tools. |
| Simulator (with failure) | 9107 | `SIMULATOR_WITH_FAILURE_PORT` | Failure-injection variant for chaos tests. |
| Runner (LLM) | 9108 | `RUNNER_PORT` | Model-driven runner agent. |
| Planner (with memory) | 9109 | `PLANNER_WITH_MEMORY_PORT` | Planner variant backed by PostgreSQL/AlloyDB. |
| Runner (autopilot) | 9110 | `RUNNER_AUTOPILOT_PORT` | Deterministic runner — no LLM calls. |
| Agent debug console | 9111 | `AGENT_DEBUG_PORT` | Telemetry and ADK agent debug server. |
| Tester UI | 9112 | `TESTER_PORT` | Manual A2UI and quest testing lab. |
| PostgreSQL / AlloyDB proxy | 9113 | `POSTGRES_PORT` | Local pgvector container or AlloyDB Auth Proxy. |
| Leadership Planner | 9114 | `LEADERSHIP_PLANNER_PORT` | Baseline leadership rollout planner. |
| Leadership Planner (with eval) | 9115 | `LEADERSHIP_PLANNER_WITH_EVAL_PORT` | Leadership planner with 6-axis evaluation & Translation Diagnostic. |
| Leadership Planner (with memory) | 9116 | `LEADERSHIP_PLANNER_WITH_MEMORY_PORT` | Leadership planner with cross-session coaching memory. |
| Leadership Runner | 9117 | `LEADERSHIP_RUNNER_PORT` | Live stakeholder rehearsal facilitator. |
| Frontend BFF | 9118 | `FRONTEND_SERVER_PORT` | Backend-for-frontend that proxies to the gateway. |
| **The Rehearsal UI (Frontend app)** | **9119** | `FRONTEND_APP_PORT` | **Angular 21 VDIL web application.** |
| Leadership Runner (autopilot) | 9120 | `LEADERSHIP_RUNNER_AUTOPILOT_PORT` | Deterministic zero-LLM leadership runner. |
| Admin dashboard | 9121 | `ADMIN_PORT` | Feature flags and session administration. |
| Billing service | 9122 | `BILLING_PORT` | Token usage and budget service. |
| AI Echo | 9123 | `AI_ECHO_PORT` | Stakeholder persona agent. |
| AI Echo (reactive) | 9124 | `AI_ECHO_REACTIVE_PORT` | Context-reactive stakeholder persona agent. |
| AI Echo (autopilot) | 9125 | `AI_ECHO_AUTOPILOT_PORT` | Deterministic zero-LLM stakeholder persona agent. |

## Conventions

- **Always read from env.** Don't hard-code port numbers in code or scripts;
  read `os.getenv("GATEWAY_PORT", "9101")` (Python) or
  `os.Getenv("GATEWAY_PORT")` (Go) so worktrees with different slot offsets
  don't collide.
- **Bind to `127.0.0.1`, not `0.0.0.0`.** The gateway is the only ingress
  point. Other services are reachable from the host but not over LAN.

## Worktree slot scheme

Parallel checkouts on different branches need disjoint port spaces so they
don't fight for the same socket. **The Rehearsal** supports this via a slot
marker file `.port-slot` at the repo root:

- **Slot 0** (default — file absent or contains `0`): use the 9101–9125 range
  documented above. Containers are named `redis`, `pubsub`.
- **Slot N > 0**: offset all ports by `1000 * N` (slot 1 → 10100s, slot 2 →
  11100s) and rename containers to `redis-slot-N`, `pubsub-slot-N`. The
  `scripts/core/sim.py` start/stop logic reads `.port-slot` for container
  naming and to pick the right `docker-compose.override.yml`. The port
  values themselves are read from `.env`, which you set by hand for the slot
  you want.

To enable a non-default slot, write the slot number to `.port-slot`, copy
`.env.example` to `.env`, and edit each `*_PORT` value with the offset. A
helper script for this is on the roadmap; for now, do it by hand.

## Local vs. cloud routing

> [!IMPORTANT]
> **Local**: path-based routing (e.g. `/admin`) is **not** supported. Hit each
> service directly at its assigned port (e.g. `http://127.0.0.1:9121`).
>
> **Cloud Run**: path-based routing is handled by the Cloud Load Balancer URL
> map. Local emulation of that routing is intentionally absent.
