# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

alloydb-proxy: bash scripts/core/start_db.sh
gateway: PORT=$GATEWAY_PORT go run ./cmd/gateway
simulator: PORT=$SIMULATOR_PORT DISPATCH_MODE=callable .venv/bin/python3 agents/simulator/agent.py
simulator-with-failure: PORT=$SIMULATOR_WITH_FAILURE_PORT DISPATCH_MODE=callable .venv/bin/python3 agents/simulator_with_failure/agent.py
# --- Original Marathon Planners (disabled — leadership planners replace these) ---
# planner: DISPATCH_MODE=callable .venv/bin/python3 agents/planner/agent.py
# planner_with_eval: DISPATCH_MODE=callable .venv/bin/python3 agents/planner_with_eval/agent.py
# planner_with_memory: DISPATCH_MODE=callable .venv/bin/python3 agents/planner_with_memory/agent.py
leadership_planner: PORT=$LEADERSHIP_PLANNER_PORT DISPATCH_MODE=callable .venv/bin/python3 agents/leadership_planner/agent.py
leadership_planner_with_eval: PORT=$LEADERSHIP_PLANNER_WITH_EVAL_PORT DISPATCH_MODE=callable .venv/bin/python3 agents/leadership_planner_with_eval/agent.py
leadership_planner_with_memory: PORT=$LEADERSHIP_PLANNER_WITH_MEMORY_PORT DISPATCH_MODE=callable .venv/bin/python3 agents/leadership_planner_with_memory/agent.py
dash: PORT=$DASH_PORT .venv/bin/python3 scripts/core/agent_dash.py
tester: PORT=$TESTER_PORT go run cmd/tester/main.go
admin: PORT=$ADMIN_PORT go run ./cmd/admin
# Phase 6b: billing webhook receiver. Runs in every environment but
# refuses every webhook unless BILLING_PROVIDER=stripe AND
# SAAS_MODE=on. With defaults (stub/off) it only serves /healthz.
billing: PORT=$BILLING_PORT go run cmd/billing/main.go

# --- Original Marathon Runners (disabled — leadership_runner replaces these) ---
# runner: DISPATCH_MODE=subscriber .venv/bin/python3 agents/runner/agent.py
# runner_autopilot: DISPATCH_MODE=subscriber .venv/bin/python3 agents/runner_autopilot/agent.py
leadership_runner: PORT=$LEADERSHIP_RUNNER_PORT DISPATCH_MODE=subscriber .venv/bin/python3 agents/leadership_runner/agent.py
leadership_runner_autopilot: PORT=$LEADERSHIP_RUNNER_AUTOPILOT_PORT DISPATCH_MODE=subscriber .venv/bin/python3 agents/leadership_runner_autopilot/agent.py
frontend: PORT=${FRONTEND_PORT:-$FRONTEND_BFF_PORT} GATEWAY_INTERNAL_URL=http://localhost:$GATEWAY_PORT FRONTEND_DEV_URL=http://localhost:$FRONTEND_APP_PORT go run cmd/frontend/main.go
frontend-app: if lsof -i :$FRONTEND_APP_PORT -sTCP:LISTEN -t >/dev/null 2>&1; then echo "Port $FRONTEND_APP_PORT already in use — reusing existing Angular dev server"; tail -f /dev/null; else cd web/frontend && npm start; fi
ai_echo: PORT=$AI_ECHO_PORT DISPATCH_MODE=subscriber .venv/bin/python3 agents/ai_echo/agent.py
ai_echo_reactive: PORT=$AI_ECHO_REACTIVE_PORT DISPATCH_MODE=subscriber .venv/bin/python3 agents/ai_echo_reactive/agent.py
ai_echo_autopilot: PORT=$AI_ECHO_AUTOPILOT_PORT DISPATCH_MODE=subscriber .venv/bin/python3 agents/ai_echo_autopilot/agent.py
