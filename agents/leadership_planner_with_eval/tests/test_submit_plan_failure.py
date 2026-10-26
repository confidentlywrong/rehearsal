# Copyright 2026 Christian Haeussinger
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

"""submit_plan_to_simulator must surface simulator failures as errors.

Regression: the simulator's A2A task failed (Vertex 404 on its model), but
call_agent relays the failure text as ``{"status": "success", "response":
"Processing request...Request failed: ..."}``. The planner reported success
and emitted result cards with every KPI empty.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.leadership_planner_with_eval.tools import (
    _simulator_failure_reason,
    submit_plan_to_simulator,
)


def _make_tool_context() -> MagicMock:
    ctx = MagicMock()
    ctx.state = {
        "leadership_scenario": {"title": "T", "stakeholder_count": 5},
        "simulator_session_id": "sim-1",
        "_sim_token": "sim-1",
        "simulation_result": {"status": "success", "response": "stale"},
    }
    ctx.session = MagicMock()
    ctx.session.id = "planner-session"
    ctx.invocation_id = "inv-1"
    ctx.agent_name = "leadership_planner_with_memory"
    return ctx


async def _submit(ctx: MagicMock, response: dict, sim_data: dict | None = None) -> dict:
    with (
        patch("agents.utils.communication.call_agent", new=AsyncMock(return_value=response)),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value=sim_data)),
    ):
        return await submit_plan_to_simulator(action="execute", message="Run it", tool_context=ctx)


@pytest.mark.parametrize(
    "response",
    [
        {"status": "success", "agent": "simulator", "response": "Processing request...Request failed: 404 NOT_FOUND"},
        {"status": "success", "agent": "simulator", "response": "Request failed: boom"},
        {"status": "success", "agent": "simulator", "response": "Processing request...simulator failed to generate response"},
        {"status": "success", "agent": "simulator", "response": "No request provided"},
        {"status": "error", "message": "connection refused"},
    ],
)
def test_failure_reason_detects_failures(response):
    assert _simulator_failure_reason(response)


@pytest.mark.parametrize(
    "response",
    [
        {"status": "success", "agent": "simulator", "response": "Processing request...Simulation complete. 12 ticks."},
        {"status": "success", "agent": "simulator", "response": None},
        {"status": "success", "agent": "simulator", "response": ""},
        {"status": "success", "summary": "ok"},
        None,
        "plain string",
    ],
)
def test_failure_reason_accepts_success(response):
    assert _simulator_failure_reason(response) is None


@pytest.mark.asyncio
async def test_failed_simulation_returns_error_and_clears_stale_result():
    ctx = _make_tool_context()
    result = await _submit(
        ctx,
        {"status": "success", "agent": "simulator", "response": "Processing request...Request failed: 404 NOT_FOUND"},
    )
    assert result["status"] == "error"
    assert "404 NOT_FOUND" in result["message"]
    assert ctx.state["simulation_result"] is None
    assert "404 NOT_FOUND" in ctx.state["simulation_error"]
    # A failed run leaves the re-entrancy guard unset.
    assert "simulation_executed_invocation" not in ctx.state


@pytest.mark.asyncio
async def test_empty_relay_with_compiled_results_is_treated_as_completed():
    """The pipeline finished (vitals_trend in Redis) but the root LLM relay was empty."""
    ctx = _make_tool_context()
    sim_data = {"vitals_trend": [{"tick": 0}, {"tick": 1}], "resources": {"lau": {"current": 50}}}
    result = await _submit(
        ctx,
        {"status": "success", "agent": "simulator", "response": "Processing request...simulator failed to generate response"},
        sim_data,
    )
    assert result["status"] == "success"
    assert ctx.state["simulation_error"] is None
    assert ctx.state["simulation_result"]["status"] == "success"
    assert "failed to generate response" in ctx.state["simulation_result"]["simulator_warning"]
    assert ctx.state["vitals_trend"] == sim_data["vitals_trend"]
    assert ctx.state["simulation_executed_invocation"] == "inv-1"


@pytest.mark.asyncio
async def test_failure_with_only_pre_race_data_is_still_an_error():
    """Pre-race data (quest_config/route) alone means the simulation never compiled."""
    ctx = _make_tool_context()
    result = await _submit(
        ctx,
        {"status": "success", "agent": "simulator", "response": "Processing request...Request failed: 404 NOT_FOUND"},
        {"quest_config": {"title": "T"}, "route_geojson": {}},
    )
    assert result["status"] == "error"
    assert ctx.state["simulation_result"] is None


@pytest.mark.asyncio
async def test_successful_simulation_loads_side_channel():
    ctx = _make_tool_context()
    response = {"status": "success", "agent": "simulator", "response": "Processing request...Simulation complete."}
    sim_data = {"resources": {"capital": 40}, "triggered_black_swans": ["x"], "alert_history": []}
    result = await _submit(ctx, response, sim_data)
    assert result["status"] == "success"
    assert ctx.state["simulation_result"] == response
    assert ctx.state["simulation_error"] is None
    assert ctx.state["simulation_executed_invocation"] == "inv-1"
    assert ctx.state["resources"] == {"capital": 40}
    assert ctx.state["triggered_black_swans"] == ["x"]


@pytest.mark.asyncio
async def test_execute_resets_previous_run_telemetry():
    """Fields missing from this run's side-channel must not keep the previous run's values."""
    ctx = _make_tool_context()
    ctx.state.update(
        {
            "echo_voice_log": [{"tick": 3, "voice": "old run"}],
            "resources": {"lau": {"current": 1, "initial": 100}},
            "defected_echo_ids": ["old-echo"],
        }
    )
    response = {"status": "success", "agent": "simulator", "response": "Processing request...# Debrief"}
    sim_data = {"vitals_trend": [{"tick": 0}], "resources": {"lau": {"current": 70, "initial": 100}}}
    result = await _submit(ctx, response, sim_data)
    assert result["status"] == "success"
    assert ctx.state["echo_voice_log"] == []
    assert ctx.state["defected_echo_ids"] == []
    assert ctx.state["resources"] == {"lau": {"current": 70, "initial": 100}}


@pytest.mark.asyncio
async def test_failed_execute_clears_previous_run_telemetry():
    ctx = _make_tool_context()
    ctx.state["vitals_trend"] = [{"tick": 0}, {"tick": 1}]
    result = await _submit(
        ctx,
        {"status": "success", "agent": "simulator", "response": "Processing request...Request failed: 404 NOT_FOUND"},
    )
    assert result["status"] == "error"
    assert ctx.state["vitals_trend"] == []
