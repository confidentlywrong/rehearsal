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
    start_simulation,
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


@pytest.mark.asyncio
async def test_start_and_submit_simulation_default_arguments():
    """Verify start_simulation and submit_plan_to_simulator work with default action and message."""
    ctx = _make_tool_context()
    res_start = await start_simulation(tool_context=ctx)
    assert res_start["status"] == "ready"
    assert res_start["action"] == "execute"
    assert "submit_plan_to_simulator to execute" in res_start["message"]

    sim_response = {"status": "success", "agent": "simulator", "response": "Processing request...All good"}
    sim_data = {"vitals_trend": [{"tick": 0}], "resources": {"lau": {"current": 80}}}
    with (
        patch("agents.utils.communication.call_agent", new=AsyncMock(return_value=sim_response)),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value=sim_data)),
    ):
        res_submit = await submit_plan_to_simulator(tool_context=ctx)
    assert res_submit["status"] == "success"


@pytest.mark.asyncio
async def test_submit_and_start_without_tool_context():
    """Calling start or submit without tool_context returns error dict instead of crashing."""
    res_start = await start_simulation()
    assert res_start["status"] == "error"
    assert "No tool context" in res_start["message"]

    res_submit = await submit_plan_to_simulator()
    assert res_submit["status"] == "error"
    assert "No tool context" in res_submit["message"]


def test_ensure_runner_count_from_scenario_upgrades_default_to_designed():
    from agents.leadership_planner_with_eval.tools import _ensure_runner_count_from_scenario

    cfg = {"runner_count": 1000}
    scenario = {"stakeholder_count": 10000}
    upgraded = _ensure_runner_count_from_scenario(cfg, scenario)
    assert upgraded["runner_count"] == 10000


@pytest.mark.asyncio
async def test_submit_plan_propagates_simdata_store_error():
    """SimDataError on store_simulation_data must return an explicit error dict."""
    from agents.utils.simdata import SimDataError

    ctx = _make_tool_context()
    with patch(
        "agents.utils.simdata.store_simulation_data",
        new=AsyncMock(side_effect=SimDataError("redis write timeout")),
    ):
        result = await submit_plan_to_simulator(action="execute", message="Run", tool_context=ctx)
    assert result["status"] == "error"
    assert "redis write timeout" in result["message"]


@pytest.mark.asyncio
async def test_submit_plan_propagates_simdata_load_error():
    """SimDataError on load_simulation_data must return explicit error instead of empty success."""
    from agents.utils.simdata import SimDataError

    ctx = _make_tool_context()
    ctx.state["echo_voice_log"] = [{"tick": 1}]
    with (
        patch("agents.utils.communication.call_agent", new=AsyncMock(return_value={"status": "success", "response": "OK"})),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(side_effect=SimDataError("redis read failed"))),
    ):
        result = await submit_plan_to_simulator(action="execute", message="Run", tool_context=ctx)
    assert result["status"] == "error"
    assert "redis read failed" in result["message"]
    assert ctx.state["simulation_result"] is None
    assert ctx.state["echo_voice_log"] == []


def test_load_default_route_geojson_no_hardcoded_cache_and_validates_env(tmp_path, monkeypatch):
    """_load_default_route_geojson returns None unless state/scenario/env provides it, and errors on bad env path."""
    from agents.leadership_planner_with_eval.tools import _load_default_route_geojson

    monkeypatch.delenv("DEFAULT_ROUTE_GEOJSON_PATH", raising=False)
    assert _load_default_route_geojson() is None

    state_route = {"type": "FeatureCollection", "features": []}
    ctx = MagicMock()
    ctx.state = {"route_geojson": state_route}
    assert _load_default_route_geojson(tool_context=ctx) == state_route

    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("DEFAULT_ROUTE_GEOJSON_PATH", str(missing))
    with pytest.raises(FileNotFoundError):
        _load_default_route_geojson()

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("DEFAULT_ROUTE_GEOJSON_PATH", str(bad_json))
    with pytest.raises(ValueError):
        _load_default_route_geojson()


@pytest.mark.asyncio
async def test_start_simulation_uses_default_runner_count():
    """start_simulation uses DEFAULT_RUNNER_COUNT when scenario omits stakeholder_count."""
    from agents.utils.sim_defaults import DEFAULT_RUNNER_COUNT

    ctx = _make_tool_context()
    ctx.state["leadership_scenario"] = {"title": "No Count"}
    res = await start_simulation(tool_context=ctx)
    assert res["status"] == "ready"
    assert ctx.state["expected_stakeholder_count"] == DEFAULT_RUNNER_COUNT


