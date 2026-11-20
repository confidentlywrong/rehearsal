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

"""Tier 5 Round 9 adversarial regression tests for Python hardening fixes."""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.leadership_planner_with_eval.evaluator.config import EVALUATION_CRITERIA
from agents.leadership_planner_with_eval.evaluator.tools import evaluate_leadership_plan
from agents.planner_with_eval.evaluator.tools import evaluate_plan
from agents.simulator.longitudinal import SimulationRunRecord, SimulationRunStore
from agents.utils.simdata import store_simulation_data
from agents.utils.traffic import (
    _haversine,
    build_segment_distance_index,
    compute_tick_traffic,
    identify_closed_segments,
    validate_route_geojson,
)


def _strict_parse_json(raw: str) -> Any:
    """Parse JSON rejecting RFC-8259-invalid constants (NaN/Infinity/-Infinity)."""

    def _reject_nonfinite(tok: str) -> None:
        raise ValueError(f"Non-RFC-8259 JSON token emitted: {tok}")

    parsed = json.loads(raw, parse_constant=_reject_nonfinite)
    json.dumps(parsed, allow_nan=False)
    return parsed


def _load_scenario_design_module(filename: str, mod_name: str):
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    mod_path = (
        repo_root
        / "agents"
        / "leadership_planner"
        / "skills"
        / "scenario-design"
        / "scripts"
        / filename
    )
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(state: dict | None = None, session_id: str = "sess-t5-r9") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id, user_id="user-t5-r9"),
        invocation_id="inv-t5-r9",
        agent_name="test_agent",
    )


# ---------------------------------------------------------------------------
# Gap 1: agents/utils/traffic.py hardening & parity
# ---------------------------------------------------------------------------


def test_r9_traffic_haversine_near_antipodal_no_domain_error():
    dist = _haversine([-115.099999999994, 36.100000000006], [64.900000000006, -36.100000000006])
    assert math.isfinite(dist)
    assert dist == pytest.approx(math.pi * 3958.8, rel=1e-6)

    # Normal parity
    deg_lat = _haversine([0.0, 0.0], [0.0, 1.0])
    assert 68.5 < deg_lat < 69.5


@pytest.mark.parametrize(
    "bad_coords",
    [
        [[float("nan"), 36.1], [-115.17, 36.12]],
        [[-115.17, float("inf")], [-115.18, 36.12]],
        [[-115.17, float("-inf")], [-115.18, 36.12]],
        [[True, 36.1], [-115.17, 36.12]],
        [["-115.17", 36.1], [-115.17, 36.12]],
        [[-115.17], [-115.17, 36.12]],
        "not-a-list",
    ],
)
def test_r9_traffic_validate_route_geojson_rejects_nonfinite_and_nonnumeric_coords(bad_coords):
    bad_route = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Bad Rd"},
                "geometry": {"type": "LineString", "coordinates": bad_coords},
            }
        ],
    }
    ok, msg = validate_route_geojson(bad_route)
    assert ok is False
    assert "coordinates" in msg.lower()


def test_r9_traffic_validate_route_geojson_valid_and_empty_coords_parity():
    for coords in ([], [[-115.17, 36.08]], [[-115.17, 36.08], [-115.17, 36.09]]):
        route = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "Strip"},
                    "geometry": {"type": "LineString", "coordinates": coords},
                }
            ],
        }
        ok, msg = validate_route_geojson(route)
        assert ok is True
        assert msg == ""


def test_r9_traffic_build_segment_distance_index_guards_nonfinite_coords_and_preserves_str_contract():
    bad_route = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "NaN Rd"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [float("nan"), 36.1],
                        [float("inf"), 36.11],
                        [True, 36.11],
                        ["bad", 36.11],
                        [-115.17, 36.12],
                    ],
                },
            }
        ],
    }
    idx = build_segment_distance_index(bad_route)
    _strict_parse_json(json.dumps(idx, allow_nan=False))
    assert len(idx) == 1
    assert math.isfinite(idx[0]["end_mi"])
    assert idx[0]["end_mi"] == 0.0
    assert idx[0]["coordinates"] == [[-115.17, 36.12]]

    # Preserve intentional contract: unvalidated stringified str features still fail loudly
    str_route = {"type": "FeatureCollection", "features": ['{"geometry": {}}']}
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        build_segment_distance_index(str_route)
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        identify_closed_segments(str_route, {"type": "FeatureCollection", "features": []})


def test_r9_traffic_compute_tick_traffic_sanitizes_bad_params_to_strict_json():
    seg_idx = [
        {
            "start_mi": 0.0,
            "end_mi": 1.0,
            "coordinates": [[-115.17, 36.08], [-115.17, 36.09]],
            "road_name": "Las Vegas Blvd",
        }
    ]
    for bad_max in (float("inf"), float("-inf"), float("nan"), True, False, "10", 10**400):
        res = compute_tick_traffic(
            seg_idx,
            sweep_distance_mi=0.0,
            current_tick=1,
            ticks_closed={},
            max_congestion_ticks=bad_max,  # type: ignore[arg-type]
        )
        parsed = _strict_parse_json(json.dumps(res, allow_nan=False))
        assert 0.0 <= parsed["overall_congestion"] <= 1.0
        assert math.isfinite(parsed["tev_impact"])

    for bad_decay in (-float("inf"), float("inf"), float("nan"), True, "bad"):
        res2 = compute_tick_traffic(
            seg_idx,
            sweep_distance_mi=10.0,
            current_tick=2,
            ticks_closed={"Las Vegas Blvd": {"closed": 0, "peak": 1.0}},
            congestion_decay_rate=bad_decay,  # type: ignore[arg-type]
        )
        parsed2 = _strict_parse_json(json.dumps(res2, allow_nan=False))
        assert 0.0 <= parsed2["overall_congestion"] <= 1.0
        assert math.isfinite(parsed2["tev_impact"])

    res3 = compute_tick_traffic(
        seg_idx,
        sweep_distance_mi=float("nan"),
        current_tick=float("inf"),  # type: ignore[arg-type]
        ticks_closed={"Las Vegas Blvd": {"closed": float("inf"), "peak": float("nan")}},
    )
    parsed3 = _strict_parse_json(json.dumps(res3, allow_nan=False))
    assert 0.0 <= parsed3["overall_congestion"] <= 1.0

    # Normal parity (max_congestion_ticks=0 keeps congestion=1.0; normal decay works)
    res_zero = compute_tick_traffic(seg_idx, 0.0, 1, {}, max_congestion_ticks=0)
    assert res_zero["segments"][0]["congestion_level"] == 1.0
    res_decay = compute_tick_traffic(
        seg_idx,
        1.0,
        2,
        {"Las Vegas Blvd": {"closed": 5, "peak": None}},
        max_congestion_ticks=10,
        congestion_decay_rate=0.3,
    )
    assert res_decay["segments"][0]["congestion_level"] == pytest.approx(0.35, abs=1e-6)


# ---------------------------------------------------------------------------
# Gap 3: scenario-design weather_modifier & dcns_engine 1e200 affinities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_weather",
    [
        {"distance_factor": float("nan"), "time_factor": float("inf"), "capital_factor": float("-inf")},
        {"distance_factor": "not-a-number", "time_factor": True, "perturbation_range": [float("nan"), "x"]},
        {"resistance_delta": float("nan"), "morale_factor": object()},
    ],
)
async def test_r9_scenario_design_plan_consensus_pathway_sanitizes_weather_modifier(bad_weather):
    sd_tools = _load_scenario_design_module("tools.py", "_sd_tools_r9")
    ctx = _ctx(state={"weather_modifier": bad_weather})
    res = await sd_tools.plan_consensus_pathway(stakeholder_count=5, seed=42, tool_context=ctx)
    assert res["status"] == "success"
    parsed = _strict_parse_json(json.dumps(res, allow_nan=False))
    assert all(math.isfinite(v) for v in parsed["cost"].values())
    assert math.isfinite(parsed["consensus_score"])


@pytest.mark.asyncio
async def test_r9_scenario_design_plan_consensus_pathway_valid_weather_parity():
    sd_tools = _load_scenario_design_module("tools.py", "_sd_tools_r9_parity")
    ctx_base = _ctx(state={})
    res_base = await sd_tools.plan_consensus_pathway(stakeholder_count=5, seed=42, tool_context=ctx_base)
    ctx_storm = _ctx(state={"weather_modifier": {"distance_factor": 2.0, "time_factor": 1.5}})
    res_storm = await sd_tools.plan_consensus_pathway(stakeholder_count=5, seed=42, tool_context=ctx_storm)
    assert res_base["status"] == "success"
    assert res_storm["status"] == "success"
    assert res_storm["cost"]["time"] > res_base["cost"]["time"]


def test_r9_dcns_engine_extreme_affinity_vectors_no_overflow_or_nan():
    dcns = _load_scenario_design_module("dcns_engine.py", "_dcns_engine_r9")
    stakeholders = [
        {
            "role": "cto",
            "tier": 1,
            "domain_principles": ["Strategic Foresight"],
            "affinity": [1e200] * 7,
            "initial_sentiment": 0.7,
            "initial_resistance": 0.2,
            "initial_influence": 0.8,
        },
        {
            "role": "cfo",
            "tier": 1,
            "domain_principles": ["Resource Optimization"],
            "affinity": [-1e200] * 7,
            "initial_sentiment": 0.4,
            "initial_resistance": 0.6,
            "initial_influence": 0.8,
        },
        {
            "role": "hr",
            "tier": 2,
            "domain_principles": [],
            "affinity": [0.0, float("nan"), 1e200, 0.0, 0.0, 0.0, 0.0],
            "initial_sentiment": 0.5,
            "initial_resistance": 0.3,
            "initial_influence": 0.5,
        },
    ]
    resources = {"OWB": {"initial": 70, "critical_threshold": 25}}

    # Opposite [1e200]*7 vs [-1e200]*7 -> cosine = -1.0, overlap = 0.0 -> dist = 1.0
    dist_opp = dcns._influence_distance(stakeholders[0], stakeholders[1])
    assert dist_opp == pytest.approx(1.0, abs=1e-9)
    dist_self = dcns._influence_distance(stakeholders[0], stakeholders[0])
    assert dist_self == pytest.approx(0.0, abs=1e-9)

    coords = dcns._project_to_geo(stakeholders)
    assert len(coords) == 3
    b = dcns.LAS_VEGAS_BOUNDS
    for lon, lat in coords:
        assert math.isfinite(lon) and math.isfinite(lat)
        assert b["lon_min"] <= lon <= b["lon_max"]
        assert b["lat_min"] <= lat <= b["lat_max"]

    # Both PCA path (network_path=None) and road-network path must produce strict RFC-8259 JSON
    res_pca = dcns.generate_consensus_pathway(
        stakeholders,
        resources,
        scenario_type="transformation",
        runner_count=3,
        seed=1,
        network_path=None,
        weather_modifier={"distance_factor": float("nan"), "time_factor": float("inf")},
    )
    _strict_parse_json(json.dumps(res_pca, allow_nan=False))

    net_path = str(
        pathlib.Path(__file__).resolve().parents[2]
        / "agents"
        / "leadership_planner"
        / "skills"
        / "scenario-design"
        / "assets"
        / "network.json"
    )
    res_net = dcns.generate_consensus_pathway(
        stakeholders,
        resources,
        scenario_type="transformation",
        runner_count=3,
        seed=1,
        network_path=net_path,
    )
    _strict_parse_json(json.dumps(res_net, allow_nan=False))


# ---------------------------------------------------------------------------
# Gap 4 (Worker 2 subset): longitudinal.py & simdata.py RFC-8259 JSON
# ---------------------------------------------------------------------------


def test_r9_longitudinal_save_run_emits_strict_rfc8259_json(tmp_path):
    store = SimulationRunStore(base_dir=tmp_path)
    bad_rec = SimulationRunRecord(
        run_id="r9-bad-1",
        participant_id="user-r9",
        quest_id="ai_powered_disruptor",
        principle_scores=[float("nan"), float("inf"), float("-inf"), 0.5, 0.6, 0.7, 0.8],
        overall_score=float("nan"),
        final_resources={"lau": float("-inf"), "bt": 100.0},
        tick_snapshots=[{"tick": 0, "score": float("nan"), "delta": float("inf")}],
    )
    summary_path = store.save_run(bad_rec)
    ndjson_lines = summary_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(ndjson_lines) == 1
    parsed_summary = _strict_parse_json(ndjson_lines[0])
    assert parsed_summary["overall_score"] is None
    assert parsed_summary["principle_scores"][:3] == [None, None, None]
    assert parsed_summary["final_resources"] == {"lau": None, "bt": 100.0}

    replay_path = summary_path.parent / "r9-bad-1.json"
    parsed_replay = _strict_parse_json(replay_path.read_text(encoding="utf-8"))
    assert parsed_replay["tick_snapshots"] == [{"tick": 0, "score": None, "delta": None}]

    # Normal parity
    ok_rec = SimulationRunRecord(
        run_id="r9-ok-1",
        participant_id="user-r9",
        quest_id="ai_powered_disruptor",
        principle_scores=[0.8] * 7,
        overall_score=0.8,
        final_resources={"lau": 50.0},
    )
    store.save_run(ok_rec)
    runs = store.get_participant_runs("user-r9")
    assert len(runs) == 2
    assert runs[1]["overall_score"] == 0.8


@pytest.mark.asyncio
async def test_r9_simdata_store_simulation_data_emits_strict_rfc8259_json():
    mock_redis = MagicMock()
    mock_pipe = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    with patch("agents.utils.simdata.get_shared_redis_client", return_value=mock_redis):
        ok = await store_simulation_data(
            simulation_id="sim-r9-bad",
            traffic_assessment={"id": "q", "score": float("nan"), "vals": [float("inf"), 1.5]},
            resources={"lau": {"current": float("-inf")}},
        )

    assert ok is True
    call_args = mock_pipe.hset.call_args
    mapping = call_args[1].get("mapping") or call_args[0][1]
    parsed_traffic = _strict_parse_json(mapping["traffic_assessment"])
    assert parsed_traffic == {"id": "q", "score": None, "vals": [None, 1.5]}
    parsed_res = _strict_parse_json(mapping["resources"])
    assert parsed_res == {"lau": {"current": None}}

    # Normal parity
    mock_pipe.hset.reset_mock()
    with patch("agents.utils.simdata.get_shared_redis_client", return_value=mock_redis):
        ok2 = await store_simulation_data(
            simulation_id="sim-r9-ok",
            traffic_assessment={"overall": 0.42},
        )
    assert ok2 is True
    mapping2 = mock_pipe.hset.call_args[1].get("mapping") or mock_pipe.hset.call_args[0][1]
    assert _strict_parse_json(mapping2["traffic_assessment"]) == {"overall": 0.42}


# ---------------------------------------------------------------------------
# Gap 5: Evaluator prompt serialization (leadership & marathon evaluators)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r9_leadership_evaluator_handles_non_json_and_nan_in_request_and_state():
    captured_prompts: list[str] = []

    async def fake_local_json(prompt: str, **_kwargs):
        captured_prompts.append(prompt)
        if '"suggestions"' in prompt:
            return json.dumps({"suggestions": ["Add risk gates."], "summary": "Evaluated cleanly."})
        return json.dumps(
            {
                "scores": {c: 84 for c in EVALUATION_CRITERIA},
                "findings": [{"title": "Solid", "severity": "MINOR", "description": "Valid"}],
                "summary": "Handled non-JSON and NaN inputs.",
            }
        )

    ctx = _ctx(
        state={
            "leadership_scenario": {
                "name": object(),
                "scenario_type": "transformation",
                "stakeholder_count": float("nan"),
                "complexity": "moderate",
                "summary": float("inf"),
            },
            "consensus_pathway_active": {
                "persona": "pragmatist",
                "milestones": ["m1"],
                "waypoints": ["w1"],
            },
            "stakeholder_map": {"cto": {"ref": object(), "score": float("nan")}},
        }
    )

    with (
        patch("agents.leadership_planner_with_eval.evaluator.tools.is_local_gemma_active", return_value=True),
        patch(
            "agents.leadership_planner_with_eval.evaluator.tools.generate_local_json_async",
            side_effect=fake_local_json,
        ),
        patch("agents.leadership_planner_with_memory.memory.tools.store_scenario", new=AsyncMock(return_value={})),
    ):
        res = await evaluate_leadership_plan(
            {"user_intent": "x", "bad": object(), "score": float("nan")},  # type: ignore[arg-type]
            tool_context=ctx,
        )

    assert res["status"] == "success"
    assert res["overall_score"] == 84
    assert captured_prompts
    assert "NaN" not in captured_prompts[0]
    assert "Infinity" not in captured_prompts[0]


@pytest.mark.asyncio
async def test_r9_marathon_evaluator_handles_non_json_and_nan_in_request_and_state():
    captured_prompts: list[str] = []

    async def fake_local_json(prompt: str, **_kwargs):
        captured_prompts.append(prompt)
        if '"suggestions"' in prompt:
            return json.dumps({"suggestions": ["Verify aid stations."], "summary": "Marathon evaluated."})
        return json.dumps(
            {
                "score": 4,
                "explanation": {
                    "safety_compliance": 4,
                    "logistics_completeness": 4,
                    "participant_experience": 4,
                    "community_impact": 4,
                    "financial_viability": 4,
                    "intent_alignment": 4,
                },
            }
        )

    # Case 1: proposed_plan is string, state contains object() and NaN/Inf
    ctx_str = _ctx(
        state={
            "marathon_route": {"bad": object(), "dist": float("nan")},
            "traffic_assessment": {"bad_t": object(), "cong": float("inf")},
        }
    )
    with (
        patch("agents.utils.local_ai.is_local_gemma_active", return_value=True),
        patch("agents.utils.local_ai.generate_local_json_async", side_effect=fake_local_json),
    ):
        res1 = await evaluate_plan(
            {  # type: ignore[arg-type]
                "user_intent": {"goal": "26.2 miles", "obj": object(), "nan": float("nan")},
                "proposed_plan": "26.2 mile marathon plan",
            },
            tool_context=ctx_str,  # type: ignore[arg-type]
        )

    assert res1["eval_method"] == "local_gemma_eval"
    assert res1["passed"] is True

    # Case 2: proposed_plan is dict, enriched with non-JSON / NaN state blobs
    captured_prompts.clear()
    ctx_dict = _ctx(
        state={
            "marathon_route": {"bad": object(), "dist": float("nan")},
            "traffic_assessment": {"bad_t": object(), "cong": float("-inf")},
        }
    )
    with (
        patch("agents.utils.local_ai.is_local_gemma_active", return_value=True),
        patch("agents.utils.local_ai.generate_local_json_async", side_effect=fake_local_json),
    ):
        res2 = await evaluate_plan(
            {  # type: ignore[arg-type]
                "user_intent": "26.2 miles",
                "proposed_plan": {"distance": "26.2 miles", "nan": float("nan")},
            },
            tool_context=ctx_dict,  # type: ignore[arg-type]
        )

    assert res2["eval_method"] == "local_gemma_eval"
    assert captured_prompts
    assert "NaN" not in captured_prompts[0]
    assert "Infinity" not in captured_prompts[0]
