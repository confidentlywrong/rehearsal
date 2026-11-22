# Scripts directory

Automation, deployment, testing, and operational utilities organized into
semantic subdirectories.

## Directory structure

```
scripts/
  core/          Simulation lifecycle, infrastructure, build tooling
  deploy/        Vertex AI Agent Engine deployment and unit tests
  bench/         Performance benchmarks and stress tests
  diagnostics/   Diagnostic reliability harnesses
  e2e/           End-to-end integration tests (run against live services)
  ops/           Operational and ad-hoc utilities
  postgres-init/ Local PostgreSQL schema and fixture SQL scripts
  tests/         Unit and regression tests for the scripts themselves
```

Top-level shell scripts:

| Script | Integration | Description |
|:---|:---|:---|
| `deploy.sh` | Interactive CLI | One-click Cloud Build bootstrap deployment |
| `deploy_test.sh` | `Makefile` (`test-deploy-sh`) | Unit tests for `deploy.sh` |
| `teardown.sh` | Interactive CLI | One-click GCP infrastructure & Agent Engine teardown |
| `check-node-versions.sh` | `Makefile` (`lint-configs`) | Verifies Node major versions match `.nvmrc` |

---

## `core/` -- Lifecycle & Infrastructure

| Script | Integration | Description |
|:---|:---|:---|
| `sim.py` | `pyproject.toml` (`uv run start/stop/restart/test/test-integration`) | Simulation lifecycle CLI: boots Redis, PubSub, Postgres, Go services, and Python agents via Honcho |
| `agent_dash.py` | `Procfile`, `Dockerfile` | FastAPI telemetry dashboard with PubSub WebSocket relay (default port `9111`) |
| `generate_proto.sh` | `Makefile` (`proto`) | Generates Go + Python protobuf bindings from `gen_proto/` |
| `start_db.sh` | `Procfile` | Starts local Postgres log tailer (Docker) or AlloyDB Auth Proxy |
| `install_alloydb_proxy.sh` | Called by `start_db.sh` | Downloads AlloyDB Auth Proxy binary |
| `run_agent.sh` | -- | Thin ADK CLI wrapper for starting an agent API server |

## `deploy/` -- Deployment & Tests

| Script | Integration | Description |
|:---|:---|:---|
| `deploy.py` | `pyproject.toml` (`uv run deploy`), `cloudbuild-bootstrap.yaml` | Deploys ADK agents to Vertex AI Agent Engine |
| `deploy_test.py` | `uv run pytest scripts/deploy/deploy_test.py` | Unit tests for `deploy.py` (Terraform outputs, staging, deploy modes) |

## `bench/` -- Performance & Stress Testing

| Script | Integration | Description |
|:---|:---|:---|
| `perf_diagnostic.py` | `Makefile` (`perf-diagnostic`) | Gateway performance diagnostic: HTTP health, WebSocket stability, simulation lifecycle |
| `perf_test.py` | `Makefile` (`perf-test`) | Generic ADK agent perf test with OTel span capture |
| `bench_concurrency.py` | `Makefile` (`bench-concurrency`) | Benchmarks per-instance session capacity for Cloud Run sizing |
| `bench_helpers.py` | Imported by `bench_concurrency.py` | Percentile, duration, and byte formatting utilities |
| `eval_stress_test.py` | `Makefile` (`eval-stress`) | Runs `planner_with_eval` N times, reports latency and eval consistency |
| `stress_test_a2a.py` | -- | Benchmarks A2A communication via `SimulationA2AClient` |
| `stress_test_db.py` | -- | Tests DB pool contention under old vs new defaults |

## `diagnostics/` -- Reliability Harnesses

| Script | Integration | Description |
|:---|:---|:---|
| `demo1_harness.py` | -- | Drives planner sessions via Gateway + WebSocket to measure terminal narrative reliability |

## `e2e/` -- End-to-End Tests

| Script | Integration | Description |
|:---|:---|:---|
| `e2e_simulation_test.py` | `Makefile` (`test-e2e-simulation`) | Full planner-to-simulator integration test |
| `e2e_fanout_test.py` | -- | Fan-out scale test: spawns N runner sessions, sends broadcast |
| `e2e_scale_test.py` | `Makefile` (`scale-test`) | 10k session GCP scale validation |
| `e2e_dispatch_test.sh` | -- | Gateway dispatch routing E2E with mock agents |
| `test_runner.py` | -- | Multi-mode runner harness (A2A, Gateway, Redis transports) |
| `test_vllm_runner.py` | -- | vLLM server integration test (OpenAI-compatible API) |

## `ops/` -- Operational Utilities

| Script | Integration | Description |
|:---|:---|:---|
| `emergency_flush.py` | -- | Clears all state across Redis, AlloyDB, and PubSub; can kill/revive Cloud Run instances |
| `discover_maps_tools.py` | -- | Validates Maps MCP server tools against documented SKILL.md |
| `spark_alloydb_processor.py` | -- | PySpark + Document AI pipeline for ingesting PDF regulations into AlloyDB |
| `generate_local_seeds.py` | -- | Generates `seed_local.sql` with pre-computed embeddings for local Postgres |

## `tests/` -- Script Tests

| Test File | Tests For |
|:---|:---|
| `test_sim_logging.py` | `core/sim.py` (Honcho log tee) |
| `test_sim_docker.py` | `core/sim.py` (Docker lifecycle, container pruning, stop safety) |
| `test_agent_dash_reconnect.py` | `core/agent_dash.py` (PubSub reconnect backoff & teardown) |
| `test_bench_concurrency.py` | `bench/bench_concurrency.py` (agent loading, concurrency runs) |
| `test_bench_helpers.py` | `bench/bench_helpers.py` (percentiles, formatting) |
| `test_perf_diagnostic.py` | `bench/perf_diagnostic.py` (latency buckets, phases, reporting) |
| `test_r2_scripts.py` | Round 2 hardening regression tests (`sim.py`, `agent_dash.py`, `emergency_flush.py`, `spark_alloydb_processor.py`) |
| `test_r3_scripts.py` | Round 3 hardening regression tests (`emergency_flush.py`, `deploy.py`, `test_runner.py`, ports & Ollama safety) |
| `test_r4_scripts.py` | Round 4 hardening regression tests (`deploy.sh`, `teardown.sh`, `discover_maps_tools.py`, `generate_local_seeds.py`) |
| `test_r5_scripts.py` | Round 5 hardening regression tests (`sim.py`, `agent_dash.py`, `spark_alloydb_processor.py`, `e2e/`, `bench/`, `pyproject.toml`) |

Run all script tests: `make bench-test`
