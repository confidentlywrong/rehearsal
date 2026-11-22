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

"""Tier 5 Round 3 adversarial regression tests for Python hardening fixes (Challenges 1-6)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from pydantic import ValidationError

from agents.leadership_planner_with_eval import tools as lp_eval_tools
from agents.leadership_planner_with_memory.memory import tools as leadership_mem_tools
from agents.planner_with_eval.evaluator import tools as pe_eval_tools
from agents.planner_with_eval.evaluator.config import CRITERION_WEIGHTS
from agents.planner_with_memory.memory import tools as planner_mem_tools
from agents.runner import hydration
from agents.simulator import ecsi, pre_race_callback, quest_loader
from agents.utils import local_ai, station_index

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_module(name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PRE_RACE = _load_module(
    "t5_r3_pre_race_tools",
    "agents/simulator/skills/preparing-the-race/tools.py",
)
_TICK_TOOLS = _load_module(
    "t5_r3_tick_tools",
    "agents/simulator/skills/advancing-race-ticks/tools.py",
)
_CONSULTANT = _load_module(
    "t5_r3_consultant_tools",
    "agents/leadership_planner_with_eval/skills/consultant/tools.py",
)
_TRANSLATION_DIAG = _load_module(
    "t5_r3_translation_diag_tools",
    "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
)
_GEN_SEEDS = _load_module(
    "t5_r3_generate_local_seeds",
    "scripts/ops/generate_local_seeds.py",
)


def _ctx(state: dict | None = None, invocation_id: str = "inv-t5-r3") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id="sess-t5-r3", user_id="user-t5-r3"),
        invocation_id=invocation_id,
        agent_name="test_agent",
    )


def _valid_quest_payload() -> dict:
    return {
        "id": "t5_r3_quest",
        "name": "Tier 5 R3 Quest",
        "resources": {
            "LAU": {"initial": 100.0, "regen_per_tick": 1.0, "critical_threshold": 20.0},
            "BT": {"initial": 100.0, "regen_per_tick": 0.0, "critical_threshold": 20.0},
            "TBU": {"initial": 100.0, "regen_per_tick": 0.0, "critical_threshold": 20.0},
            "OWB": {"initial": 100.0, "regen_per_tick": 0.0, "critical_threshold": 20.0},
        },
        "echoes": [
            {
                "role": "cfo",
                "tier": 2,
                "persona": "Budget focused CFO",
                "affinity": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.9],
                "initial_sentiment": 0.2,
                "initial_resistance": 0.4,
                "initial_influence": 0.8,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Challenge 1: agents/leadership_planner_with_eval/tools.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        "Request failed: upstream timeout",
        "simulator failed to generate response",
        "Processing request...Request failed: 502 Bad Gateway",
        "No request provided",
        {"status": "error", "error": "gateway error"},
        {"status": "error", "message": "connection refused"},
        {"status": "success", "response": "Processing request...Request failed: 404 NOT_FOUND"},
    ],
)
def test_c1_leadership_simulator_failure_reason_detects_str_and_dict_failures(response):
    assert lp_eval_tools._simulator_failure_reason(response)


@pytest.mark.parametrize(
    "response",
    [
        "plain string",
        {"status": "success", "response": "Processing request...Simulation complete."},
        {"status": "success", "response": None},
        None,
    ],
)
def test_c1_leadership_simulator_failure_reason_allows_success(response):
    assert lp_eval_tools._simulator_failure_reason(response) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sim_response",
    [
        "Request failed: upstream timeout",
        "simulator failed to generate response",
        {"status": "error", "error": "503 unavailable"},
    ],
)
async def test_c1_submit_plan_to_simulator_fails_on_raw_str_and_allows_retry(sim_response):
    ctx = _ctx(
        state={
            "leadership_scenario": {"name": "Pivot", "stakeholder_count": 5},
            "simulator_session_id": "sim-c1",
            "_sim_token": "sim-c1",
            "simulation_result": {"stale": True},
        },
        invocation_id="inv-c1-retry",
    )

    with (
        patch("agents.utils.communication.call_agent", new=AsyncMock(return_value=sim_response)),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={})),
    ):
        failed = await lp_eval_tools.submit_plan_to_simulator(
            action="execute",
            message="Run scenario",
            tool_context=ctx,  # type: ignore[arg-type]
        )

    assert failed["status"] == "error"
    assert ctx.state["simulation_result"] is None
    assert ctx.state["simulation_error"]
    assert "simulation_executed_invocation" not in ctx.state

    with (
        patch("agents.utils.communication.call_agent", new=AsyncMock(return_value={"status": "success", "response": "OK"})),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={"vitals_trend": [{"tick": 0}]})),
    ):
        retried = await lp_eval_tools.submit_plan_to_simulator(
            action="execute",
            message="Retry scenario",
            tool_context=ctx,  # type: ignore[arg-type]
        )

    assert retried["status"] == "success"
    assert ctx.state["simulation_executed_invocation"] == "inv-c1-retry"
    assert ctx.state["simulation_error"] is None


@pytest.mark.asyncio
async def test_c1_start_and_submit_guard_none_state():
    none_state_ctx = SimpleNamespace(state=None, invocation_id="inv-none")
    res_start = await lp_eval_tools.start_simulation(tool_context=none_state_ctx)  # type: ignore[arg-type]
    res_submit = await lp_eval_tools.submit_plan_to_simulator(tool_context=none_state_ctx)  # type: ignore[arg-type]
    assert res_start["status"] == "error"
    assert res_submit["status"] == "error"


@pytest.mark.parametrize(
    "bad_count",
    [True, False, None, 0, -5, 0.4, 10**400, -10**400, "1e500", "1" + "0" * 5000, float("inf"), float("-inf"), float("nan"), "abc", ""],
)
def test_c1_coerce_positive_int_rejects_bool_overflow_and_non_finite(bad_count):
    assert lp_eval_tools._coerce_positive_int(bad_count) is None


def test_c1_coerce_positive_int_accepts_bounded_counts_and_normalizes_config():
    assert lp_eval_tools._coerce_positive_int("1,500") == 1500
    assert lp_eval_tools._coerce_positive_int(10000) == 10000
    assert lp_eval_tools._coerce_positive_int(500.0) == 500

    cfg = lp_eval_tools._normalize_sim_config({"stakeholder_count": 10**400}, None)
    assert "runner_count" not in cfg


# ---------------------------------------------------------------------------
# Challenge 2: agents/simulator/skills/preparing-the-race/tools.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tick_interval_seconds", "duration_seconds", "total_race_hours", "race_distance_mi"])
@pytest.mark.parametrize("bad_val", [10**400, -10**400, float("inf"), float("-inf"), float("nan"), True, False, 0, -1])
async def test_c2_prepare_simulation_rejects_overflow_and_non_finite_config(field, bad_val):
    ctx = _ctx({})
    plan = {"action": "execute", "simulation_config": {field: bad_val}}

    with (
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={})),
        patch("agents.simulator.quest_loader.load_quest_from_yaml", return_value=quest_loader.QuestConfig.model_validate(_valid_quest_payload())),
        patch("agents.simulator.quest_loader.inject_quest_into_state"),
    ):
        res = await _PRE_RACE.prepare_simulation(json.dumps(plan), ctx)  # type: ignore[arg-type]

    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_c2_prepare_simulation_rejects_overflow_tick_ratio():
    ctx = _ctx({})
    plan = {
        "action": "execute",
        "simulation_config": {"duration_seconds": 1e308, "tick_interval_seconds": 0.001},
    }
    with (
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={})),
        patch("agents.simulator.quest_loader.load_quest_from_yaml", return_value=quest_loader.QuestConfig.model_validate(_valid_quest_payload())),
        patch("agents.simulator.quest_loader.inject_quest_into_state"),
    ):
        res = await _PRE_RACE.prepare_simulation(json.dumps(plan), ctx)  # type: ignore[arg-type]

    assert res["status"] == "error"


# ---------------------------------------------------------------------------
# Challenge 3: quest_loader.py, ecsi.py, pre_race_callback.py
# ---------------------------------------------------------------------------


def test_c3_quest_loader_raises_validation_error_on_overflow_not_overflow_error():
    payload_res = _valid_quest_payload()
    payload_res["resources"]["LAU"]["initial"] = 10**400
    with pytest.raises(ValidationError):
        quest_loader.QuestConfig.model_validate(payload_res)

    payload_echo = _valid_quest_payload()
    payload_echo["echoes"][0]["initial_resistance"] = 10**400
    with pytest.raises(ValidationError):
        quest_loader.QuestConfig.model_validate(payload_echo)

    payload_gk = _valid_quest_payload()
    payload_gk["gatekeeper_route_map"] = {"cfo": 10**400}
    with pytest.raises(ValidationError):
        quest_loader.QuestConfig.model_validate(payload_gk)


def test_c3_ecsi_handles_overflow_trigger_tick_and_snapshot_tick():
    alerts = ecsi._detect_hidden_agenda_triggers(
        current_tick=5,
        echo_configs=[{"role": "cfo", "has_hidden_agenda": True, "agenda_trigger_tick": 10**400}],
        revealed_agendas=set(),
    )
    assert alerts == []

    detected = ecsi.detect_contention(
        tick_snapshot={"tick": 10**400},
        quest_config={},
        resource_state={},
        state=None,
    )
    assert detected == []


@pytest.mark.parametrize("bad_count", [float("inf"), float("-inf"), float("nan"), 10**400, True, "bad"])
def test_c3_pre_race_callback_spawn_and_done_handle_non_finite_and_overflow(bad_count):
    spawn_req = LlmRequest(
        contents=[
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="prepare_simulation",
                            response={"status": "success"},
                        )
                    )
                ],
            )
        ]
    )
    ctx = SimpleNamespace(
        state={"runner_count": bad_count, "runner_type": "leadership_runner"},
        session=SimpleNamespace(id="sess-cb"),
    )
    resp = pre_race_callback.pre_race_callback(ctx, spawn_req)  # type: ignore[arg-type]
    fc = resp.content.parts[0].function_call
    assert fc is not None and fc.name == "spawn_runners"
    assert isinstance(fc.args["count"], int) and fc.args["count"] >= 1

    done_req = LlmRequest(
        contents=[
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="fire_start_gun",
                            response={"status": "success"},
                        )
                    )
                ],
            )
        ]
    )
    done_resp = pre_race_callback.pre_race_callback(ctx, done_req)  # type: ignore[arg-type]
    assert "Pre-race setup complete." in (done_resp.content.parts[0].text or "")


# ---------------------------------------------------------------------------
# Challenge 4: consultant/tools.py & planner_with_eval/evaluator/tools.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_score", [10**400, float("inf"), float("nan"), True])
async def test_c4_consultant_emit_sim_results_rejects_overflow_and_non_finite(bad_score):
    ctx = _ctx({"leadership_evaluation": {"status": "success", "overall_score": bad_score}})
    with patch.object(_CONSULTANT, "emit_gateway_message", new=AsyncMock()) as mock_emit:
        res_state = await _CONSULTANT.emit_sim_results_a2ui(ctx)  # type: ignore[arg-type]
        res_arg = await _CONSULTANT.emit_sim_results_a2ui(_ctx({}), overall_score=bad_score)  # type: ignore[arg-type]
    assert res_state.startswith("ERROR:")
    assert res_arg.startswith("ERROR:")
    mock_emit.assert_not_called()


def test_c4_planner_evaluator_build_result_handles_nan_inf_overflow_and_bool():
    criteria = list(CRITERION_WEIGHTS.keys())
    scores = {
        criteria[0]: float("nan"),
        criteria[1]: float("inf"),
        criteria[2]: 10**400,
        criteria[3]: True,
        criteria[4]: "bad",
        criteria[5]: 85,
        criteria[6]: 70,
    }
    details = {c: f"detail-{c}" for c in criteria}
    res = pe_eval_tools._build_result(scores, details, eval_method="test")  # type: ignore[arg-type]
    assert res["scores"][criteria[0]] == 0
    assert res["scores"][criteria[1]] == 0
    assert res["scores"][criteria[2]] == 100
    assert res["scores"][criteria[3]] == 0
    assert res["scores"][criteria[4]] == 0
    assert res["scores"][criteria[5]] == 85
    assert res["scores"][criteria[6]] == 70
    assert isinstance(res["overall_score"], int)

    with pytest.raises(ValueError, match="Missing evaluation score"):
        pe_eval_tools._build_result({criteria[0]: 80}, details, eval_method="test")


# ---------------------------------------------------------------------------
# Challenge 5: agents/utils/local_ai.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_sc", ["1e500", 10**400, float("inf"), float("-inf"), float("nan"), True])
def test_c5_try_fast_path_response_handles_overflow_stakeholder_count_and_score(bad_sc):
    req_design = LlmRequest(
        contents=[
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name="design_leadership_scenario",
                            args={"stakeholder_count": bad_sc},
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="design_leadership_scenario",
                            response={"status": "success", "stakeholder_count": bad_sc},
                        )
                    )
                ],
            ),
        ]
    )
    resp_design = local_ai.LocalGemmaOllama._try_fast_path_response(
        req_design,
        {"plan_consensus_pathway"},
    )
    assert resp_design is not None
    fc = resp_design.content.parts[0].function_call
    assert fc is not None and fc.name == "plan_consensus_pathway"
    assert "stakeholder_count" not in fc.args

    req_eval = LlmRequest(
        contents=[
            types.Content(role="user", parts=[types.Part.from_text(text="Evaluate")]),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="evaluate_leadership_plan",
                            response={"overall_score": bad_sc},
                        )
                    )
                ],
            ),
        ]
    )
    resp_eval = local_ai.LocalGemmaOllama._try_fast_path_response(
        req_eval,
        {"validate_and_emit_a2ui"},
    )
    assert resp_eval is None


@pytest.mark.parametrize("bad_vec", [[10**400], [float("inf")], [float("nan")], [True], ["not-a-float"]])
def test_c5_embedding_helpers_handle_overflow_and_invalid_elements(bad_vec):
    assert local_ai._has_valid_embedding_norm(bad_vec, target_dim=4) is False
    assert local_ai.normalize_and_pad_embedding(bad_vec, target_dim=4) == [0.0, 0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Challenge 6: Remaining float() OverflowError sites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c6_planner_with_memory_handles_overflow_score_and_limit():
    ctx = _ctx(
        {
            "marathon_route": {"type": "FeatureCollection", "features": []},
            "evaluation_result": {"overall_score": 10**400},
        }
    )
    fake_store = MagicMock()
    fake_store.store_route = AsyncMock(return_value="route-1")
    with patch.object(planner_mem_tools, "_store", fake_store):
        res = await planner_mem_tools.store_route(ctx)  # type: ignore[arg-type]

    assert res["status"] == "success"
    assert fake_store.store_route.call_args.kwargs["evaluation_score"] is None
    assert planner_mem_tools._clamp_non_negative_limit("9" * 5000, 10, 500) == 10
    with pytest.raises(ValueError, match="invalid value"):
        planner_mem_tools._to_pgvector([10**400])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_amount", [10**400, float("inf"), float("nan"), True])
async def test_c6_hydration_handles_overflow_and_non_finite(bad_amount):
    ctx = _ctx({"water": 80.0, "finished": False, "exhausted": False, "collapsed": False})
    res_dep = await hydration.deplete_water(bad_amount, ctx)  # type: ignore[arg-type]
    assert res_dep["status"] == "success"
    assert res_dep["water"] == 80.0

    res_reh = await hydration.rehydrate(bad_amount, ctx)  # type: ignore[arg-type]
    assert res_reh["status"] == "success"
    assert res_reh["water"] == 80.0


def test_c6_advancing_race_ticks_and_station_index_handle_overflow():
    assert _TICK_TOOLS._tick_matches(10**400, 1) is False
    assert _TICK_TOOLS._tick_matches(float("inf"), 1) is False
    assert _TICK_TOOLS._tick_matches(float("nan"), 1) is False
    assert _TICK_TOOLS._tick_matches(True, 1) is False
    assert _TICK_TOOLS._tick_matches(1, 1) is True

    idx = station_index.extract_station_index(
        {
            "type": "FeatureCollection",
            "features": [
                {"properties": {"type": "water_station", "mi": 10**400}},
                {"properties": {"type": "water_station", "mi": float("inf")}},
                {"properties": {"type": "water_station", "mi": float("nan")}},
                {"properties": {"type": "water_station", "mi": True}},
                {"properties": {"type": "water_station", "mi": 5.5}},
            ],
        }
    )
    assert idx["water_stations"] == [5.5]


def test_c6_sibling_helpers_handle_overflow_and_huge_digit_strings():
    assert _TRANSLATION_DIAG._safe_int("1e500", 7) == 7
    assert _TRANSLATION_DIAG._safe_int("9" * 5000, 7) == 7
    assert leadership_mem_tools._clamp_limit("1e500", default=10, max_limit=500) == 10
    assert leadership_mem_tools._clamp_limit("9" * 5000, default=10, max_limit=500) == 10
    assert _GEN_SEEDS._sql_float_or_null(10**400) == "NULL"
    assert _GEN_SEEDS._sql_float_or_null("1e500") == "NULL"
    with pytest.raises(ValueError, match="Non-finite embedding value"):
        _GEN_SEEDS._format_vector([10**400])
