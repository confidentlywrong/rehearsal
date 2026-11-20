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

"""Tier 5 Round 4 adversarial regression tests for Python hardening fixes (Challenges 1-4)."""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.ai_echo import initialization as echo_init
from agents.ai_echo import process_decision_round as echo_pdr
from agents.ai_echo_autopilot import autopilot as echo_autopilot
from agents.leadership_runner import leadership_tick
from agents.leadership_runner_autopilot import autopilot as leadership_autopilot
from agents.runner import running
from agents.runner_autopilot import autopilot as runner_autopilot
from agents.simulator import ari, decision_classifier, live_dashboard, tick_callback, truth_engine
from agents.utils import auth, local_ai

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_module(name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ADVANCE_TICKS = _load_module(
    "t5_r4_advancing_race_ticks",
    "agents/simulator/skills/advancing-race-ticks/tools.py",
)
_COMPLETE_RACE = _load_module(
    "t5_r4_completing_the_race",
    "agents/simulator/skills/completing-the-race/tools.py",
)
_TRANSLATION_DIAG = _load_module(
    "t5_r4_translation_diag_tools",
    "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
)
_EMBEDDING_BACKFILL = _load_module(
    "t5_r4_embedding_backfill",
    "infra/modules/cloud-sql-postgres/embedding_backfill.py",
)


def _ctx(state: dict | None = None, session_id: str = "sess-t5-r4") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id, user_id="user-t5-r4"),
        invocation_id="inv-t5-r4",
        agent_name="test_agent",
    )


# ---------------------------------------------------------------------------
# Challenge 1: _finite_int / _safe_int / _safe_tick & route_friction_map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "helper,default_val",
    [
        (_ADVANCE_TICKS._finite_int, 7),
        (_COMPLETE_RACE._finite_int, 7),
        (live_dashboard._finite_int, 7),
        (leadership_tick._finite_int, 7),
        (running._finite_int, 7),
        (echo_autopilot._finite_int, 7),
        (runner_autopilot._finite_int, 7),
        (leadership_autopilot._finite_int, 7),
        (echo_init._finite_int, 7),
        (echo_pdr._finite_int, 7),
        (tick_callback._safe_int, 7),
        (truth_engine._safe_tick, 7),
        (_TRANSLATION_DIAG._safe_int, 7),
    ],
)
def test_c1_finite_int_helpers_reject_overflow_and_non_finite(helper, default_val):
    for bad in (10**400, -(10**400), float("inf"), float("-inf"), float("nan"), True, False, None, "bad"):
        assert helper(bad, default_val) == default_val
    assert helper(42, default_val) == 42
    assert helper(42.0, default_val) == 42


@pytest.mark.asyncio
async def test_c1_advance_tick_handles_overflow_tick_and_route_friction_map():
    ctx = _ctx(
        {
            "simulation_id": "sim-t5-r4",
            "current_tick": 10**400,
            "max_ticks": 10,
            "simulation_config": {"total_race_hours": 1.0, "tick_interval_seconds": 1.0},
            "route_friction_map": {"cfo": 10**400, "cto": 7.5, "coo": True},
        }
    )
    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=[])
    fake_collector.collect = AsyncMock(return_value=[])
    with (
        patch.object(_ADVANCE_TICKS.RaceCollector, "get", return_value=fake_collector),
        patch.object(_ADVANCE_TICKS, "publish_to_runners", AsyncMock(return_value=1)),
        patch.object(_ADVANCE_TICKS, "emit_gateway_message", AsyncMock()),
    ):
        res = await _ADVANCE_TICKS.advance_tick(ctx)  # type: ignore[arg-type]
    assert res["tick"] == 0
    assert res["real_time_minutes"] == 0.0
    assert res["infrastructure"]["friction_level"] == "moderate"


@pytest.mark.asyncio
async def test_c1_compile_results_handles_overflow_runners_reporting():
    ctx = _ctx(
        {
            "simulation_id": "sim-t5-r4",
            "runner_count": 10,
            "finished_runner_ids": [],
            "tick_snapshots": [
                {"tick": 1, "runners_reporting": 10**400},
                {"tick": 10**400, "runners_reporting": 10**308},
                {"tick": 3, "runners_reporting": 10**308},
            ],
        }
    )
    res = await _COMPLETE_RACE.compile_results(ctx)  # type: ignore[arg-type]
    assert res["status"] == "success"
    assert isinstance(res["avg_runners_reporting"], (int, float))


def test_c1_build_live_dashboard_handles_overflow_status_counts():
    payload = live_dashboard.build_live_dashboard(
        tick=10**400,
        max_ticks=10**400,
        completion_pct=50.0,
        resource_state={},
        resource_deltas={},
        status_counts={
            "finished": 10**400,
            "running": 10**308,
            "exhausted": 10**308,
            "collapsed": 2,
        },
        persona_status_counts={},
        infrastructure={},
        active_alerts_count=10**400,
        avg_velocity=1.0,
        avg_water=80.0,
    )
    assert "surfaceUpdate" in payload
    assert "beginRendering" in payload


@pytest.mark.asyncio
async def test_c1_leadership_tick_and_runner_tick_handle_overflow_tick():
    l_ctx = _ctx({"runner_id": "echo-1", "velocity": 2.0, "water": 80.0, "distance": 1.0})
    l_res = await leadership_tick.process_leadership_tick(
        l_ctx,  # type: ignore[arg-type]
        inner_thought="Aligning team",
        minutes_per_tick=1.0,
        elapsed_minutes=1.0,
        race_distance_mi=26.2,
        tick=10**400,
    )
    assert l_res["status"] == "error"

    r_ctx = _ctx({"runner_id": "run-1", "velocity": 2.0, "water": 80.0, "distance": 1.0})
    r_res = await running.process_tick(
        r_ctx,  # type: ignore[arg-type]
        inner_thought="Pacing steady",
        minutes_per_tick=1.0,
        elapsed_minutes=1.0,
        race_distance_mi=26.2,
        tick=10**400,
    )
    assert r_res["status"] == "error"


# ---------------------------------------------------------------------------
# Challenge 2: ari.py, decision_classifier.py, truth_engine.py OverflowError
# ---------------------------------------------------------------------------


def test_c2_ari_handles_overflow_ticks_in_decision_log_snapshots_and_voice_log():
    state = {
        "tick_snapshots": [
            {"tick": 10**400, "resources": {"lau": {"current": 10.0, "critical_threshold": 20.0}}},
            {"tick": 1, "resources": {"lau": {"current": 10.0, "critical_threshold": 20.0}}},
            {"tick": 2, "resources": {"lau": {"current": 50.0, "critical_threshold": 20.0}}},
        ],
        "decision_log": [
            {"tick": 10**400, "decision_tags": {"resource_impact_suggestion": {"lau": 15.0}}},
            {"tick": 2, "decision_tags": {"resource_impact_suggestion": {"lau": 10.0}}},
        ],
        "echo_voice_log": [
            {"tick": 10**400, "stance": "hostile", "role": "cfo"},
            {"tick": 2, "stance": "supportive", "role": "cto"},
            {"tick": 2, "stance": "opposed", "role": "coo"},
        ],
    }
    res = ari.compute_ari_dimensions(state)
    assert 0.0 <= res["ari"] <= 1.0
    assert res["zone"] in {"Dead Zone", "Transition", "Compounding"}


def test_c2_decision_classifier_black_swan_ignores_overflow_trigger_tick():
    events = [
        {
            "event": "Overflow Event",
            "trigger": {"type": "tick", "tick": 10**400},
            "resource_impact": {"lau": -25.0},
        },
        {
            "event": "Normal Event",
            "trigger": {"type": "tick", "tick": 3},
            "resource_impact": {"lau": -10.0},
        },
    ]
    resources = {"lau": {"current": 80.0}}
    triggered = decision_classifier.apply_black_swan_events(
        tick=3,
        resource_state=resources,
        black_swan_events=events,
        triggered_history=[],
    )
    assert [e["event"] for e in triggered] == ["Normal Event"]
    assert resources["lau"]["current"] == 70.0


def test_c2_truth_engine_find_best_responder_skips_overflow_affinity():
    contradictions = [
        truth_engine.Contradiction(
            commitment_text="Protect budget",
            violated_principle="Resource Optimization",
            violated_principle_index=0,
            evidence="Overspent",
            tick_detected=2,
            tick_declared=1,
        )
    ]
    echo_configs = [
        {"role": "cfo", "affinity": [10**400]},
        {"role": "coo", "affinity": [0.8]},
    ]
    assert truth_engine.find_best_responder(contradictions, echo_configs) == "coo"


# ---------------------------------------------------------------------------
# Challenge 3: local_ai._repair_malformed_tool_call >4300-digit ints
# ---------------------------------------------------------------------------


def test_c3_repair_malformed_tool_call_handles_oversized_digit_strings():
    huge_digits = "9" * 5000
    assert (
        local_ai.LocalGemmaOllama._repair_malformed_tool_call(
            f'{{"name": "design_leadership_scenario", "arguments": {{"stakeholder_count": {huge_digits}}}'
        )
        is None
    )
    assert (
        local_ai.LocalGemmaOllama._repair_malformed_tool_call(
            f'{{"name": "emit_scenario_list_a2ui", "arguments": {{"limit": {huge_digits}}}'
        )
        is None
    )
    assert (
        local_ai.LocalGemmaOllama._repair_malformed_tool_call(
            f'{{"name": "start_simulation", "arguments": {{"stakeholder_count": {huge_digits}}}'
        )
        is None
    )
    # Valid scalar fields are still recovered when oversized integer count is ignored
    res_design = local_ai.LocalGemmaOllama._repair_malformed_tool_call(
        f'{{"name": "design_leadership_scenario", "arguments": {{"scenario_type": "crisis", "stakeholder_count": {huge_digits}}}'
    )
    assert res_design == ("design_leadership_scenario", {"scenario_type": "crisis"})

    # Normal positive limit and stakeholder_count still recover cleanly
    assert local_ai.LocalGemmaOllama._repair_malformed_tool_call(
        '{"name": "emit_scenario_list_a2ui", "arguments": {"limit": 12'
    ) == ("emit_scenario_list_a2ui", {"limit": 12})


# ---------------------------------------------------------------------------
# Challenge 4: auth._clamp_timeout & embedding_backfill._to_pgvector_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_timeout", [10**400, -(10**400), float("inf"), float("nan"), True, -1, 0])
def test_c4_auth_clamp_timeout_returns_cap_on_overflow_and_invalid(bad_timeout):
    assert auth._clamp_timeout(bad_timeout, cap=5.0) == 5.0
    assert auth._clamp_timeout((bad_timeout, 2.0), cap=5.0) == (5.0, 2.0)


@pytest.mark.parametrize("bad_vec", [[10**400], [True], [False], ["not-a-number"], [float("nan")], [float("inf")]])
def test_c4_embedding_backfill_to_pgvector_text_rejects_overflow_bool_and_non_finite(bad_vec):
    with pytest.raises(ValueError):
        _EMBEDDING_BACKFILL._to_pgvector_text(bad_vec)
