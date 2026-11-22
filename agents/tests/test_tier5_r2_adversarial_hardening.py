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

"""Tier 5 Round 2 adversarial regression tests for Python hardening fixes."""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.leadership_planner_with_memory import simulation_timeline, stakeholder_personas
from agents.leadership_planner_with_memory.memory import tools as leadership_mem_tools
from agents.leadership_runner import capital, leadership_tick
from agents.planner_with_eval import tools as planner_eval_tools

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_module(name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SCENARIO_DESIGN = _load_module(
    "t5_r2_scenario_design_tools",
    "agents/leadership_planner/skills/scenario-design/scripts/tools.py",
)
_GIS_TOOLS = _load_module(
    "t5_r2_gis_tools",
    "agents/planner/skills/gis-spatial-engineering/scripts/tools.py",
)

_SAMPLE_ROUTE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[-115.172, 36.086], [-115.172, 36.120], [-115.172, 36.155]],
            },
            "properties": {"type": "marathon_route"},
        }
    ],
}


def _ctx(state: dict | None = None, invocation_id: str = "inv-t5-r2") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id="sess-t5-r2", user_id="user-t5-r2"),
        invocation_id=invocation_id,
        agent_name="test_agent",
    )


# ---------------------------------------------------------------------------
# Finding 1: agents/planner_with_eval/tools.py simulator failure propagation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        {"status": "error", "message": "Connection refused"},
        {"status": "error"},
        {"status": "success", "response": "Processing request...Request failed: 404 NOT_FOUND"},
        {"status": "success", "response": "Processing request...simulator failed to generate response"},
        {"status": "success", "response": "No request provided"},
        "Request failed: upstream timeout",
        "simulator failed to generate response",
        "Processing request...Request failed: gateway 502",
    ],
)
def test_planner_with_eval_simulator_failure_reason_detects_failures(response):
    assert planner_eval_tools._simulator_failure_reason(response)


@pytest.mark.parametrize(
    "response",
    [
        {"status": "success", "response": "Processing request...Simulation complete."},
        {"status": "success", "response": ""},
        {"status": "success", "response": None},
        "Simulator acknowledged",
        None,
    ],
)
def test_planner_with_eval_simulator_failure_reason_allows_success(response):
    assert planner_eval_tools._simulator_failure_reason(response) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sim_response",
    [
        {"status": "error", "message": "Connection refused"},
        "Request failed: upstream timeout",
        "simulator failed to generate response",
        {"status": "success", "response": "Processing request...Request failed: 503 unavailable"},
    ],
)
async def test_planner_with_eval_submit_plan_fails_and_allows_retry(sim_response):
    ctx = _ctx(
        state={
            "marathon_route": _SAMPLE_ROUTE,
            "simulator_session_id": "sim-1",
            "_sim_token": "sim-1",
            "simulation_result": {"stale": True},
        },
        invocation_id="inv-retry-1",
    )

    with (
        patch("agents.utils.communication.call_agent", new=AsyncMock(return_value=sim_response)),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
    ):
        failed = await planner_eval_tools.submit_plan_to_simulator(
            action="execute",
            message="Run marathon",
            tool_context=ctx,  # type: ignore[arg-type]
        )

    assert failed["status"] == "error"
    assert ctx.state["simulation_result"] is None
    assert ctx.state["simulation_error"]
    assert "simulation_executed_invocation" not in ctx.state

    # Same invocation retry must succeed once simulator recovers.
    with (
        patch("agents.utils.communication.call_agent", new=AsyncMock(return_value={"status": "success", "response": "OK"})),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
    ):
        retried = await planner_eval_tools.submit_plan_to_simulator(
            action="execute",
            message="Retry marathon",
            tool_context=ctx,  # type: ignore[arg-type]
        )

    assert retried["status"] == "success"
    assert ctx.state["simulation_executed_invocation"] == "inv-retry-1"
    assert ctx.state["simulation_error"] is None


# ---------------------------------------------------------------------------
# Finding 2: leadership_planner scenario-design numeric / non-finite hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_count",
    ["inf", "-inf", "+inf", "1e500", "-1e500", "nan", "NaN", float("inf"), float("-inf"), float("nan"), True, False, None, "abc", ""],
)
def test_coerce_stakeholder_count_rejects_non_finite_and_invalid(raw_count):
    assert _SCENARIO_DESIGN._coerce_stakeholder_count(raw_count, default=1000) == 1000


def test_coerce_stakeholder_count_valid_inputs():
    assert _SCENARIO_DESIGN._coerce_stakeholder_count("1,500") == 1500
    assert _SCENARIO_DESIGN._coerce_stakeholder_count(0) == 1
    assert _SCENARIO_DESIGN._coerce_stakeholder_count(250.9) == 250


@pytest.mark.asyncio
async def test_plan_consensus_pathway_skips_overflow_and_non_finite_coords_and_travel():
    ctx = _ctx(
        state={
            "leadership_scenario": {
                "echoes": [{"role": "cto", "tier": 1}],
                "resources": {"capital": 100},
            },
            "venue_coords": {
                "huge_int": [10**400, 36.1],
                "inf_lon": [float("inf"), 36.1],
                "neg_inf_lat": [-115.17, float("-inf")],
                "nan_coord": [float("nan"), 36.1],
                "bool_coord": [True, 36.1],
                "dict_huge": {"lon": 10**400, "lat": 36.1},
                "dict_inf": {"lon": float("inf"), "lat": 36.1},
                "valid_hq": [-115.17, 36.11],
            },
            "travel_matrix": {
                "huge": 10**400,
                "inf": float("inf"),
                "valid": 12.5,
            },
        }
    )

    captured: dict = {}

    def _fake_generate_consensus_pathway(**kwargs):
        captured.update(kwargs)
        return {"waypoints": [1, 2], "consensus_score": 0.9, "cost": {}}

    fake_spec = SimpleNamespace(
        name="dcns_engine",
        loader=SimpleNamespace(
            create_module=lambda spec: None,
            exec_module=lambda mod: setattr(mod, "generate_consensus_pathway", _fake_generate_consensus_pathway),
        ),
    )

    with (
        patch("importlib.util.spec_from_file_location", return_value=fake_spec),
        patch("importlib.util.module_from_spec", return_value=SimpleNamespace()),
    ):
        result = await _SCENARIO_DESIGN.plan_consensus_pathway(tool_context=ctx)

    assert result["status"] == "success"
    assert captured["venue_coords"] == {"valid_hq": (-115.17, 36.11)}
    assert captured["travel_matrix"] == {"valid": 12.5}


# ---------------------------------------------------------------------------
# Finding 3: leadership_planner_with_memory huge-int OverflowError hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_num", [10**400, -10**400, float("inf"), float("-inf"), float("nan"), "1e500"])
async def test_emit_stakeholder_personas_handles_overflow_and_non_finite(bad_num):
    ctx = _ctx()
    payload = {
        "personas": [
            {
                "stakeholder_id": "ceo",
                "archetype": "visionary",
                "communication_style": "directive",
                "stance": "supportive",
                "sentiment_bias": bad_num,
                "influence_weight": 0.8,
            }
        ]
    }
    res = await stakeholder_personas.emit_stakeholder_personas(payload, ctx)  # type: ignore[arg-type]
    assert res["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_num", [10**400, -10**400, float("inf"), float("-inf"), float("nan"), "1e500"])
async def test_emit_simulation_timeline_handles_overflow_and_non_finite(bad_num):
    ctx = _ctx()
    payload = {
        "ticks": [
            {
                "tick": 1,
                "headline": "Sprint 1",
                "states": [
                    {
                        "stakeholder_id": "ceo",
                        "stance": "supportive",
                        "sentiment": bad_num,
                    }
                ],
            }
        ]
    }
    res = await simulation_timeline.emit_simulation_timeline(payload, ctx)  # type: ignore[arg-type]
    assert res["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_score", [10**400, -10**400, float("inf"), float("nan"), True, "85"])
async def test_store_scenario_rejects_overflow_and_non_finite_score(bad_score):
    ctx = _ctx(
        state={
            "leadership_scenario": {"title": "Transform"},
            "leadership_evaluation": {"overall_score": bad_score},
        }
    )
    res = await leadership_mem_tools.store_scenario(ctx)  # type: ignore[arg-type]
    assert res["status"] == "needs_evaluation"


@pytest.mark.asyncio
async def test_record_scenario_outcome_handles_overflow_score():
    ctx = _ctx(
        state={
            "active_scenario_id": "sc-1",
            "simulation_result": {"outcome": "ok"},
            "leadership_evaluation": {"overall_score": 10**400},
        }
    )
    fake_store = MagicMock()
    fake_store.record_simulation = AsyncMock(return_value="sim-1")
    fake_store.update_evaluation = AsyncMock(return_value=True)

    with patch.object(leadership_mem_tools, "_store", fake_store):
        res = await leadership_mem_tools.record_scenario_outcome(
            scenario_id="sc-1",
            tool_context=ctx,  # type: ignore[arg-type]
        )

    assert res["status"] == "success"
    fake_store.update_evaluation.assert_awaited_once()
    assert fake_store.update_evaluation.call_args.kwargs["evaluation_score"] is None


# ---------------------------------------------------------------------------
# Finding 4: leadership_runner capital.py & leadership_tick.py huge-int safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leadership_runner_capital_handles_huge_ints():
    assert capital._sanitize_capital(10**400) == 100.0
    assert capital._sanitize_capital(float("inf")) == 100.0

    ctx = _ctx(
        state={
            "water": 10**400,
            "injury_risk": 10**400,
            "_capital_restored_window": [[10**400, 5], [0, 10**400]],
            "persona": "pragmatist",
        }
    )
    res_dep = await capital.deplete_capital(10**400, ctx)  # type: ignore[arg-type]
    assert res_dep["status"] == "success"
    assert res_dep["water"] == 100.0

    ctx.state["water"] = 50.0
    res_res = await capital.restore_capital(10**400, ctx)  # type: ignore[arg-type]
    assert res_res["status"] == "cooldown"

    res_res2 = await capital.restore_capital(10, ctx)  # type: ignore[arg-type]
    assert res_res2["status"] == "success"
    assert res_res2["water"] == 60.0


@pytest.mark.parametrize(
    ("progress", "data"),
    [
        (10**400, {"start_mi": 0.0, "end_mi": 26.2}),
        (5.0, {"start_mi": 10**400, "end_mi": 26.2}),
        (5.0, [10**400, 10.0]),
        (5.0, 10**400),
        (5.0, "1e500"),
    ],
)
def test_progress_in_gatekeeper_zone_handles_huge_ints_and_overflow(progress, data):
    assert leadership_tick._progress_in_gatekeeper_zone(progress, data) is False


# ---------------------------------------------------------------------------
# Finding 5: gis-spatial-engineering add_course_infrastructure runner_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_runner_count",
    ["abc", "", "   ", float("inf"), float("-inf"), float("nan"), "1e500", None, {"count": 10}],
)
async def test_add_course_infrastructure_rejects_invalid_runner_count(bad_runner_count):
    res = await _GIS_TOOLS.add_course_infrastructure(_SAMPLE_ROUTE, runner_count=bad_runner_count)
    assert res["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("good_runner_count", [True, 10**400, "500", "1,200", 0, 5000.9])
async def test_add_course_infrastructure_accepts_or_coerces_valid_inputs(good_runner_count):
    res = await _GIS_TOOLS.add_course_infrastructure(_SAMPLE_ROUTE, runner_count=good_runner_count)
    assert res["status"] == "success"
    assert len(res["geojson"]["features"]) > len(_SAMPLE_ROUTE["features"])
