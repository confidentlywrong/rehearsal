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

"""Tier 5 Round 5 adversarial regression tests for Python hardening fixes (Challenges 1-2)."""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.simulator import longitudinal
from agents.utils import local_ai

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_module(name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ADVANCE_TICKS = _load_module(
    "t5_r5_advancing_race_ticks",
    "agents/simulator/skills/advancing-race-ticks/tools.py",
)
_COMPLETE_RACE = _load_module(
    "t5_r5_completing_the_race",
    "agents/simulator/skills/completing-the-race/tools.py",
)


def _ctx(state: dict | None = None, session_id: str = "sess-t5-r5") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id, user_id="user-t5-r5"),
        invocation_id="inv-t5-r5",
        agent_name="test_agent",
    )


# ---------------------------------------------------------------------------
# Challenge 1: generate_local_json_async large-int / non-finite timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_timeout",
    [10**400, -(10**400), float("inf"), float("-inf"), float("nan"), True, False, 0, -5, "bad"],
)
async def test_c1_generate_local_json_async_clamps_overflow_and_invalid_timeouts(
    monkeypatch, bad_timeout
):
    monkeypatch.delenv("LOCAL_AI_TIMEOUT", raising=False)
    monkeypatch.delenv("LOCAL_GEMMA_TIMEOUT", raising=False)

    captured_timeouts: list[object] = []

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"message": {"content": '{"ok": true}'}}

    class _FakeClient:
        def __init__(self, *, timeout: object, **_kwargs):
            captured_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return _FakeResp()

    monkeypatch.setattr(local_ai.httpx, "AsyncClient", _FakeClient)

    out = await local_ai.generate_local_json_async("Return JSON", timeout=bad_timeout)
    assert out == '{"ok": true}'
    assert captured_timeouts == [20.0]


# ---------------------------------------------------------------------------
# Challenge 2: Multi-runner 1e308 sum overflow -> inf across simulator tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c2_advance_tick_two_runner_1e308_sums_stay_finite_and_strict_json():
    messages = [
        {
            "session_id": "runner-1",
            "payload": {
                "tool_name": "process_leadership_tick",
                "result": {
                    "tick": 0,
                    "effective_velocity": 1e308,
                    "velocity": 1e308,
                    "water": 1e308,
                    "distance_mi": 1e308,
                    "runner_status": "running",
                    "persona": "pragmatist",
                    "health": {"comfort": 1e308, "medical_saves": 0},
                },
            },
        },
        {
            "session_id": "runner-2",
            "payload": {
                "tool_name": "process_leadership_tick",
                "result": {
                    "tick": 0,
                    "effective_velocity": 1e308,
                    "velocity": 1e308,
                    "water": 1e308,
                    "distance_mi": 1e308,
                    "runner_status": "running",
                    "persona": "pragmatist",
                    "health": {"comfort": 1e308, "medical_saves": 0},
                },
            },
        },
    ]

    state = {
        "simulation_id": "sim-t5-r5",
        "current_tick": 0,
        "max_ticks": 5,
        "simulation_config": {
            "total_race_hours": 1.0,
            "tick_interval_seconds": 1.0,
            "race_distance_mi": 26.2,
        },
        "resources": {
            "lau": {"current": 1e308, "critical_threshold": 10.0},
            "bt": {"current": 50.0, "critical_threshold": 10.0},
            "tbu": {"current": 50.0, "critical_threshold": 10.0},
            "owb": {"current": 50.0, "critical_threshold": 10.0},
        },
        "_prev_tick_resources": {"lau": -1e308},
        "traffic_model": {
            "segment_index": [],
            "ticks_closed": {},
        },
    }
    ctx = _ctx(state)
    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=messages)
    fake_collector.collect = AsyncMock(return_value=[])

    with (
        patch.object(_ADVANCE_TICKS.RaceCollector, "get", return_value=fake_collector),
        patch.object(_ADVANCE_TICKS, "publish_to_runners", new=AsyncMock()),
        patch.object(_ADVANCE_TICKS, "emit_gateway_message", new=AsyncMock()),
        patch("agents.utils.traffic.compute_tick_traffic", return_value={"segments": [], "ticks_closed": {}, "overall_congestion": 0.0, "tev_impact": 0.0}),
    ):
        res = await _ADVANCE_TICKS.advance_tick(ctx)

    assert res["status"] == "success"
    assert math.isfinite(res["avg_velocity"])
    assert math.isfinite(res["avg_water"])
    assert math.isfinite(res["avg_distance"])
    assert math.isfinite(res["completion_pct"])
    assert math.isfinite(res["infrastructure"]["avg_comfort"])
    assert math.isfinite(res["resource_deltas"]["lau"]["delta"])
    assert math.isfinite(res["traffic"]["sweep_distance_mi"])

    snap = state["tick_snapshots"][-1]
    trend = state["vitals_trend_full"][-1]
    assert snap["avg_velocity"] == trend["avg_velocity"] == res["avg_velocity"]
    assert snap["avg_water"] == trend["avg_water"] == res["avg_water"]
    assert snap["avg_distance"] == trend["avg_distance"] == res["avg_distance"]
    assert math.isfinite(snap["persona_avg_progress"]["pragmatist"])

    serialized = json.dumps(res, allow_nan=False)
    assert "Infinity" not in serialized
    json.dumps(state["tick_snapshots"], allow_nan=False)
    json.dumps(state["vitals_trend_full"], allow_nan=False)


@pytest.mark.asyncio
async def test_c2_advance_tick_single_runner_1e308_percentage_multiplication_overflow():
    messages = [
        {
            "session_id": "runner-1",
            "payload": {
                "tool_name": "process_leadership_tick",
                "result": {
                    "tick": 0,
                    "effective_velocity": 5.0,
                    "water": 80.0,
                    "distance_mi": 1e308,
                    "runner_status": "running",
                    "persona": "visionary",
                },
            },
        }
    ]
    state = {
        "simulation_id": "sim-t5-r5-single",
        "current_tick": 0,
        "max_ticks": 5,
        "simulation_config": {
            "total_race_hours": 1.0,
            "tick_interval_seconds": 1.0,
            "race_distance_mi": 26.2,
        },
    }
    ctx = _ctx(state)
    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=messages)
    fake_collector.collect = AsyncMock(return_value=[])

    with (
        patch.object(_ADVANCE_TICKS.RaceCollector, "get", return_value=fake_collector),
        patch.object(_ADVANCE_TICKS, "publish_to_runners", new=AsyncMock()),
        patch.object(_ADVANCE_TICKS, "emit_gateway_message", new=AsyncMock()),
    ):
        res = await _ADVANCE_TICKS.advance_tick(ctx)

    assert math.isfinite(res["completion_pct"])
    assert math.isfinite(state["tick_snapshots"][-1]["persona_avg_progress"]["visionary"])
    json.dumps(res, allow_nan=False)


@pytest.mark.asyncio
async def test_c2_compile_results_two_runner_1e308_persona_progress_strict_json(tmp_path, monkeypatch):
    monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
    ctx = _ctx(
        {
            "simulation_id": "sim-t5-r5-compile",
            "runner_count": 2,
            "simulation_config": {"race_distance_mi": 26.2},
            "tick_snapshots": [
                {
                    "tick": 0,
                    "real_time_minutes": 1.0,
                    "avg_velocity": 5.0,
                    "avg_water": 80.0,
                    "avg_distance": 1.0,
                    "runners_reporting": 2,
                    "status_counts": {"running": 2},
                    "persona_status_counts": {"pragmatist": {"running": 2}},
                }
            ],
            "global_runner_states": {
                "r1": {"status": "running", "persona": "pragmatist", "distance": 1e308},
                "r2": {"status": "running", "persona": "pragmatist", "distance": 1e308},
            },
        }
    )

    with patch.object(_COMPLETE_RACE, "store_simulation_data", new=AsyncMock()):
        res = await _COMPLETE_RACE.compile_results(ctx)

    assert res["status"] == "success"
    assert math.isfinite(res["persona_avg_progress"]["pragmatist"])
    json.dumps(res, allow_nan=False)


def test_c2_longitudinal_peer_benchmarks_and_trajectory_1e308_strict_json():
    runs = [
        {"principle_scores": [1e308] * 7, "overall_score": 1e308, "quest_id": "q1", "quest_name": "Q1"},
        {"principle_scores": [1e308] * 7, "overall_score": 1e308, "quest_id": "q1", "quest_name": "Q1"},
    ]
    benchmarks = longitudinal.compute_peer_benchmarks(
        current_scores=[1e308] * 7,
        all_runs=runs,
    )
    assert all(math.isfinite(v) for v in benchmarks["average_scores"])
    assert all(math.isfinite(v) for v in benchmarks["p90_scores"])
    assert all(math.isfinite(v) for v in benchmarks["participant_scores"])
    json.dumps(benchmarks, allow_nan=False)

    trajectory = longitudinal.compute_learning_trajectory(runs)
    assert all(math.isfinite(item["avg_score"]) for item in trajectory["growth_areas"])
    assert all(math.isfinite(item["avg_score"]) for item in trajectory["strengths"])
    json.dumps(trajectory, allow_nan=False)
