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

"""Tier 5 Round 6 adversarial regression tests for Python hardening fixes (Challenges 1-3)."""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.simulator import ari, decision_classifier, longitudinal

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_module(name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ADVANCE_TICKS = _load_module(
    "t5_r6_advancing_race_ticks",
    "agents/simulator/skills/advancing-race-ticks/tools.py",
)


def _ctx(state: dict | None = None, session_id: str = "sess-t5-r6") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id, user_id="user-t5-r6"),
        invocation_id="inv-t5-r6",
        agent_name="test_agent",
    )


# ---------------------------------------------------------------------------
# Challenge 1: ari.compute_ari_dimensions / _compute_judgment OverflowError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "expected_j"),
    [
        ([1e200, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 0.0),
        ([1e308, -1e308, 0.0, 0.0, 0.0, 0.0, 0.0], 0.0),
        ([1e154, 1e154, -1e154, -1e154, 0.0, 0.0, 0.0], 0.0),
        ([1e308, 1e308, 0.0, 0.0, 0.0, 0.0, 0.0], 0.0),
        ([1e308] * 7, 1.0),
        ([sys.float_info.max] * 7, 1.0),
    ],
)
def test_c1_ari_extreme_large_principle_scores_no_overflow(scores, expected_j):
    res = ari.compute_ari_dimensions({"principle_scores": scores})
    assert res["j"] == expected_j
    for key in ("o", "j", "d", "s", "ari"):
        assert math.isfinite(res[key])
        assert 0.0 <= res[key] <= 1.0
    json.dumps(res, allow_nan=False)


def test_c1_ari_normal_principle_scores_preserved():
    uniform = ari.compute_ari_dimensions({"principle_scores": [80.0] * 7})
    assert uniform["j"] == 1.0

    mixed = ari.compute_ari_dimensions({"principle_scores": [100.0, 0.0, 50.0, 50.0, 50.0, 50.0, 50.0]})
    # variance = (2500 + 2500) / 7 = 714.285714... -> norm = 0.285714... -> j = 0.71
    assert mixed["j"] == 0.71
    json.dumps(mixed, allow_nan=False)


# ---------------------------------------------------------------------------
# Challenge 2: advancing-race-ticks org_state["completion_percentage"] inf guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("extreme_tick", [10**308, -(10**308)])
async def test_c2_advance_tick_extreme_current_tick_org_completion_strict_json(extreme_tick):
    state = {
        "simulation_id": "sim-t5-r6-c2",
        "current_tick": extreme_tick,
        "max_ticks": 1,
        "simulation_config": {
            "total_race_hours": 1e308,
            "tick_interval_seconds": 0.0,
            "race_distance_mi": 26.2,
        },
    }
    ctx = _ctx(state)
    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=[])

    with (
        patch.object(_ADVANCE_TICKS.RaceCollector, "get", return_value=fake_collector),
        patch.object(_ADVANCE_TICKS, "publish_to_runners", new=AsyncMock()),
        patch.object(_ADVANCE_TICKS, "emit_gateway_message", new=AsyncMock()),
    ):
        res = await _ADVANCE_TICKS.advance_tick(ctx)

    snap = state["tick_snapshots"][-1]
    org_pct = snap["organizational_state"]["completion_percentage"]
    assert math.isfinite(org_pct)
    assert org_pct == 0.0
    assert math.isfinite(res["real_time_minutes"])
    assert res["real_time_minutes"] == 0.0
    json.dumps(state["tick_snapshots"], allow_nan=False)
    json.dumps(res, allow_nan=False)


@pytest.mark.asyncio
async def test_c2_advance_tick_normal_org_completion_preserved():
    state = {
        "simulation_id": "sim-t5-r6-c2-normal",
        "current_tick": 3,
        "max_ticks": 10,
        "simulation_config": {
            "total_race_hours": 2.0,
            "tick_interval_seconds": 0.0,
            "race_distance_mi": 26.2,
        },
    }
    ctx = _ctx(state)
    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=[])

    with (
        patch.object(_ADVANCE_TICKS.RaceCollector, "get", return_value=fake_collector),
        patch.object(_ADVANCE_TICKS, "publish_to_runners", new=AsyncMock()),
        patch.object(_ADVANCE_TICKS, "emit_gateway_message", new=AsyncMock()),
    ):
        res = await _ADVANCE_TICKS.advance_tick(ctx)

    org_pct = state["tick_snapshots"][-1]["organizational_state"]["completion_percentage"]
    assert org_pct == 30.0
    json.dumps(state["tick_snapshots"], allow_nan=False)
    json.dumps(res, allow_nan=False)


# ---------------------------------------------------------------------------
# Challenge 3: longitudinal.py & decision_classifier.py finite arithmetic
# ---------------------------------------------------------------------------


def test_c3_from_session_state_1e308_principle_scores_strict_json():
    rec = longitudinal.SimulationRunRecord.from_session_state(
        {"principle_scores": [1e308] * 7}
    )
    assert math.isfinite(rec.overall_score)
    assert rec.overall_score == 0.0
    serialized = json.dumps(rec.to_dict(), allow_nan=False)
    assert "Infinity" not in serialized

    normal = longitudinal.SimulationRunRecord.from_session_state(
        {"principle_scores": [80.0, 90.0, 70.0, 80.0, 80.0, 80.0, 80.0]}
    )
    assert normal.overall_score == 80.0


def test_c3_replay_from_tick_extreme_resource_deltas_and_regen_strict_json():
    snapshots = [
        {
            "tick": 0,
            "organizational_state": {
                "resources": {
                    "lau": {"current": 1e308, "initial": 1e308, "regen_per_tick": 1e308}
                }
            },
        },
        {
            "tick": 1,
            "organizational_state": {
                "resources": {
                    "lau": {"current": -1e308, "initial": 1e308, "regen_per_tick": 0.0}
                }
            },
        },
    ]
    res = longitudinal.replay_from_tick(
        snapshots,
        fork_tick=0,
        modified_decision_tags={"resource_impact_suggestion": {"lau": 1e308}},
    )
    assert "error" not in res
    delta_lau = res["comparison"]["resource_deltas"]["lau"]
    assert math.isfinite(delta_lau)
    assert delta_lau == 0.0
    for point in res["forked_trajectory"]:
        assert math.isfinite(point["resources"]["lau"])
    json.dumps(res, allow_nan=False)


def test_c3_apply_resource_impacts_and_black_swan_extreme_floats_strict_json():
    resource_state = {
        "lau": {"current": -1e308, "initial": 1e308, "regen_per_tick": 1e308}
    }
    deltas = decision_classifier.apply_resource_impacts(resource_state, {"lau": 1e308})
    assert math.isfinite(deltas["lau"])
    assert deltas["lau"] == 0.0
    assert math.isfinite(resource_state["lau"]["current"])
    json.dumps(deltas, allow_nan=False)
    json.dumps(resource_state, allow_nan=False)

    bs_state = {"lau": {"current": 1e308, "initial": 1e308, "regen_per_tick": 0.0}}
    triggered = decision_classifier.apply_black_swan_events(
        tick=1,
        resource_state=bs_state,
        black_swan_events=[
            {
                "event": "Extreme surge",
                "trigger": {"type": "tick", "tick": 1},
                "resource_impact": {"lau": 1e308},
            }
        ],
        triggered_history=[],
    )
    assert len(triggered) == 1
    assert math.isfinite(bs_state["lau"]["current"])
    assert bs_state["lau"]["current"] == 0.0
    json.dumps(bs_state, allow_nan=False)
