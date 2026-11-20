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

"""Tier 5 Round 7 adversarial regression tests for Python hardening fixes (Challenges 1-3)."""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.leadership_runner import leadership_tick
from agents.runner import running
from agents.simulator import coaching, influence_network, live_dashboard
from agents.utils import runner_protocol

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_module(name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ADVANCE_TICKS = _load_module(
    "t5_r7_advancing_race_ticks",
    "agents/simulator/skills/advancing-race-ticks/tools.py",
)


def _ctx(state: dict | None = None, session_id: str = "sess-t5-r7") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id, user_id="user-t5-r7"),
        invocation_id="inv-t5-r7",
        agent_name="test_agent",
    )


def _strict_parse_json(raw: str) -> dict:
    def _reject_nonfinite(tok: str) -> None:
        raise ValueError(f"Non-RFC-8259 JSON token emitted: {tok}")

    parsed = json.loads(raw, parse_constant=_reject_nonfinite)
    json.dumps(parsed, allow_nan=False)
    return parsed


# ---------------------------------------------------------------------------
# Challenge 1: runner_protocol.build_tick_event / serialize_runner_event
# ---------------------------------------------------------------------------


def test_c1_build_tick_event_rejects_overflow_bool_and_nonfinite_inputs():
    ev_1e308 = runner_protocol.build_tick_event(
        tick=10**308,
        max_ticks=1,
        total_race_hours=1e308,
    )
    assert math.isfinite(ev_1e308.data["minutes_per_tick"])
    assert math.isfinite(ev_1e308.data["elapsed_minutes"])
    assert ev_1e308.data["minutes_per_tick"] == 0.0
    assert ev_1e308.data["elapsed_minutes"] == 0.0
    _strict_parse_json(runner_protocol.serialize_runner_event(ev_1e308))

    ev_400 = runner_protocol.build_tick_event(
        tick=10**400,
        max_ticks=10**400,
        total_race_hours=6.0,
        runner_count=10**400,
    )
    assert ev_400.data["tick"] == 0
    assert ev_400.data["max_ticks"] == 0
    assert ev_400.data["runner_count"] == 0
    assert ev_400.data["minutes_per_tick"] == 0.0
    assert ev_400.data["elapsed_minutes"] == 0.0
    _strict_parse_json(runner_protocol.serialize_runner_event(ev_400))

    ev_bool = runner_protocol.build_tick_event(
        tick=True,  # type: ignore[arg-type]
        max_ticks=True,  # type: ignore[arg-type]
        total_race_hours=True,  # type: ignore[arg-type]
        race_distance_mi=False,  # type: ignore[arg-type]
        runner_count=True,  # type: ignore[arg-type]
    )
    assert type(ev_bool.data["tick"]) is int and ev_bool.data["tick"] == 0
    assert type(ev_bool.data["max_ticks"]) is int and ev_bool.data["max_ticks"] == 0
    assert type(ev_bool.data["runner_count"]) is int and ev_bool.data["runner_count"] == 0
    assert ev_bool.data["minutes_per_tick"] == 0.0
    assert ev_bool.data["race_distance_mi"] > 0.0
    _strict_parse_json(runner_protocol.serialize_runner_event(ev_bool))

    ev_nan = runner_protocol.build_tick_event(
        tick=2,
        max_ticks=10,
        total_race_hours=float("nan"),
        race_distance_mi=float("inf"),
    )
    assert ev_nan.data["minutes_per_tick"] == 0.0
    assert ev_nan.data["elapsed_minutes"] == 0.0
    assert math.isfinite(ev_nan.data["race_distance_mi"])
    _strict_parse_json(runner_protocol.serialize_runner_event(ev_nan))


def test_c1_serialize_runner_event_strips_nested_nonfinite_floats():
    event = runner_protocol.RunnerEvent(
        event=runner_protocol.RunnerEventType.TICK,
        data={
            "minutes_per_tick": float("inf"),
            "nested": {"nan_val": float("nan"), "neg_inf": float("-inf"), "ok": 3.5},
            "arr": [1.0, float("inf"), float("nan")],
        },
    )
    serialized = runner_protocol.serialize_runner_event(event)
    parsed = _strict_parse_json(serialized)
    assert parsed["minutes_per_tick"] is None
    assert parsed["nested"] == {"nan_val": None, "neg_inf": None, "ok": 3.5}
    assert parsed["arr"] == [1.0, None, None]


def test_c1_build_tick_event_normal_behavior_preserved():
    ev0 = runner_protocol.build_tick_event(tick=0, max_ticks=12, total_race_hours=6.0)
    assert ev0.data["minutes_per_tick"] == 0.0
    assert ev0.data["elapsed_minutes"] == 0.0

    ev5 = runner_protocol.build_tick_event(tick=5, max_ticks=24, total_race_hours=6.0, runner_count=8)
    assert ev5.data["minutes_per_tick"] == 15.0
    assert ev5.data["elapsed_minutes"] == 75.0
    assert ev5.data["runner_count"] == 8
    _strict_parse_json(runner_protocol.serialize_runner_event(ev5))


@pytest.mark.asyncio
async def test_c1_advance_tick_publishes_strict_json_under_extreme_inputs():
    state = {
        "simulation_id": "sim-t5-r7-c1",
        "current_tick": 10**308,
        "max_ticks": 1,
        "simulation_config": {
            "total_race_hours": 1e308,
            "tick_interval_seconds": 0.0,
            "race_distance_mi": 26.2,
        },
        "route_friction_map": {"cfo": float("inf")},
    }
    ctx = _ctx(state)
    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=[])
    published_payloads: list[str] = []

    async def _capture_publish(payload: str, **_kwargs):
        published_payloads.append(payload)

    with (
        patch.object(_ADVANCE_TICKS.RaceCollector, "get", return_value=fake_collector),
        patch.object(_ADVANCE_TICKS, "publish_to_runners", side_effect=_capture_publish),
        patch.object(_ADVANCE_TICKS, "emit_gateway_message", new=AsyncMock()),
    ):
        await _ADVANCE_TICKS.advance_tick(ctx)

    assert len(published_payloads) == 1
    parsed = _strict_parse_json(published_payloads[0])
    assert parsed["minutes_per_tick"] == 0.0
    assert parsed["elapsed_minutes"] == 0.0


# ---------------------------------------------------------------------------
# Challenge 2: live_dashboard & coaching extreme float formatting
# ---------------------------------------------------------------------------


def test_c2_live_dashboard_extreme_critical_threshold_no_inf_label():
    payload = live_dashboard.build_live_dashboard(
        tick=2,
        max_ticks=10,
        completion_pct=20.0,
        resource_state={
            "lau": {"current": 50.0, "initial": 0.0, "critical_threshold": 1e308},
            "bt": {"current": 1.2e308, "initial": 0.0, "critical_threshold": 1e308},
        },
        resource_deltas={"lau": {"delta": -10.0}},
        status_counts={"running": 4},
        persona_status_counts={},
        infrastructure={},
        active_alerts_count=0,
        avg_velocity=1.0,
        avg_water=80.0,
    )
    serialized = json.dumps(payload, allow_nan=False)
    assert re.search(r"(?<![a-z])(inf|infinity|nan)(?![a-z])", serialized.lower()) is None
    comps = {c["id"]: c["component"] for c in payload["surfaceUpdate"]["components"]}
    lau_label = comps["dash-res-lau"]["ScoreBar"]["label"]["literalString"]
    bt_label = comps["dash-res-bt"]["ScoreBar"]["label"]["literalString"]
    assert "/inf" not in lau_label.lower()
    assert "/inf" not in bt_label.lower()
    assert math.isfinite(comps["dash-res-lau"]["ScoreBar"]["maxValue"]["literalNumber"])
    assert math.isfinite(comps["dash-res-bt"]["ScoreBar"]["maxValue"]["literalNumber"])


def test_c2_live_dashboard_normal_behavior_preserved():
    payload = live_dashboard.build_live_dashboard(
        tick=1,
        max_ticks=10,
        completion_pct=10.0,
        resource_state={"lau": {"current": 50.0, "initial": 0.0, "critical_threshold": 20.0}},
        resource_deltas={"lau": {"delta": 5.0}},
        status_counts={"running": 2},
        persona_status_counts={},
        infrastructure={},
        active_alerts_count=0,
        avg_velocity=1.0,
        avg_water=80.0,
    )
    comps = {c["id"]: c["component"] for c in payload["surfaceUpdate"]["components"]}
    label = comps["dash-res-lau"]["ScoreBar"]["label"]["literalString"]
    assert label == "Attention 50/100 ▲5"
    json.dumps(payload, allow_nan=False)


def test_c2_coaching_card_extreme_delta_no_inf_text():
    card = coaching.build_coaching_card(
        resource_state={"lau": {"current": 1e308, "critical_threshold": 1e308}},
        decision_tags={"principles_neglected": ["Trust & Candor"]},
        alerts=[],
        tick=3,
        max_ticks=10,
        previous_resource_state={"lau": {"current": -1e308}},
    )
    assert card is not None
    serialized = json.dumps(card, allow_nan=False)
    assert "▲ inf" not in serialized
    assert "▼ inf" not in serialized
    assert "inf" not in serialized.lower()


def test_c2_coaching_card_normal_delta_preserved():
    card = coaching.build_coaching_card(
        resource_state={"lau": {"current": 25.0, "critical_threshold": 20.0}},
        decision_tags={"principles_neglected": []},
        alerts=[],
        tick=3,
        max_ticks=10,
        previous_resource_state={"lau": {"current": 40.0}},
    )
    assert card is not None
    comps = {c["id"]: c["component"] for c in card["surfaceUpdate"]["components"]}
    cause_text = comps["coach-cause"]["Text"]["text"]["literalString"]
    assert "Leadership Attention ▼ 15 since last tick (40 → 25)." in cause_text
    json.dumps(card, allow_nan=False)


# ---------------------------------------------------------------------------
# Challenge 3: leadership_tick, running, and influence_network overflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c3_leadership_tick_extreme_velocity_and_minutes_strict_json():
    state = {
        "persona": "pragmatist",
        "velocity": 1e308,
        "distance": 0.0,
        "water": 100.0,
        "black_swan_multiplier": 1e308,
        "wave_number": 10**400,
        "medical_saves": 10**400,
    }
    ctx = _ctx(state, session_id="lead-t5-r7")
    mock_redis = MagicMock()
    mock_redis.rpush = AsyncMock()
    mock_redis.expire = AsyncMock()

    with patch.object(leadership_tick, "get_shared_redis_client", return_value=mock_redis):
        res = await leadership_tick.process_leadership_tick(
            ctx,  # type: ignore[arg-type]
            inner_thought="Pushing forward",
            minutes_per_tick=1e308,
            elapsed_minutes=1e308,
            race_distance_mi=1e-10,
            tick=1,
            collector_buffer_key="buf:lead-r7",
        )

    assert res["status"] == "success"
    assert math.isfinite(res["effective_velocity"])
    assert math.isfinite(res["mi_this_tick"])
    assert math.isfinite(res["progress_this_tick"])
    assert res["mi_this_tick"] == 0.0
    assert res["progress_this_tick"] == 0.0
    json.dumps(res, allow_nan=False)

    mock_redis.rpush.assert_awaited_once()
    _, pushed_raw = mock_redis.rpush.await_args.args
    _strict_parse_json(pushed_raw)


@pytest.mark.asyncio
async def test_c3_runner_process_tick_extreme_velocity_and_minutes_strict_json():
    state = {
        "velocity": 1e308,
        "distance": 0.0,
        "water": 100.0,
        "wave_number": 10**400,
    }
    ctx = _ctx(state, session_id="run-t5-r7")
    mock_redis = MagicMock()
    mock_redis.rpush = AsyncMock()
    mock_redis.expire = AsyncMock()

    with patch.object(running, "get_shared_redis_client", return_value=mock_redis):
        res = await running.process_tick(
            ctx,  # type: ignore[arg-type]
            inner_thought="Steady pace",
            minutes_per_tick=1e308,
            elapsed_minutes=1e308,
            race_distance_mi=1e-10,
            tick=1,
            collector_buffer_key="buf:run-r7",
        )

    assert res["status"] == "success"
    assert math.isfinite(res["effective_velocity"])
    assert math.isfinite(res["mi_this_tick"])
    assert res["mi_this_tick"] == 0.0
    json.dumps(res, allow_nan=False)

    mock_redis.rpush.assert_awaited_once()
    _, pushed_raw = mock_redis.rpush.await_args.args
    _strict_parse_json(pushed_raw)


@pytest.mark.asyncio
async def test_c3_runner_and_leadership_normal_tick_preserved():
    l_ctx = _ctx({"persona": "pragmatist", "velocity": 1.0, "distance": 0.0, "water": 95.0})
    l_res = await leadership_tick.process_leadership_tick(
        l_ctx,  # type: ignore[arg-type]
        inner_thought="Normal tick",
        minutes_per_tick=5.0,
        elapsed_minutes=5.0,
        race_distance_mi=26.2188,
        tick=1,
    )
    assert l_res["status"] == "success"
    assert l_res["mi_this_tick"] > 0.0
    json.dumps(l_res, allow_nan=False)

    r_ctx = _ctx({"velocity": 1.0, "distance": 0.0, "water": 95.0})
    r_res = await running.process_tick(
        r_ctx,  # type: ignore[arg-type]
        inner_thought="Normal tick",
        minutes_per_tick=5.0,
        elapsed_minutes=5.0,
        race_distance_mi=26.2188,
        tick=1,
    )
    assert r_res["status"] == "success"
    assert r_res["mi_this_tick"] > 0.0
    json.dumps(r_res, allow_nan=False)


def test_c3_influence_network_propagate_extreme_sentiments_strict_json():
    net = influence_network.InfluenceNetwork(
        [
            influence_network.InfluenceEdge("cto", "cfo", 1.0),
            influence_network.InfluenceEdge("coo", "cfo", 1.0),
        ]
    )
    deltas = net.propagate({"cto": 1e308, "coo": 1e308, "cfo": -1e308}, propagation_rate=1.0)
    assert "cfo" in deltas
    assert math.isfinite(deltas["cfo"])
    assert deltas["cfo"] == 0.0
    json.dumps(deltas, allow_nan=False)

    # Accumulation overflow when individual raw_deltas are finite (1e308 + 1e308)
    deltas_acc = net.propagate({"cto": 1e308, "coo": 1e308, "cfo": 0.0}, propagation_rate=1.0)
    assert math.isfinite(deltas_acc["cfo"])
    assert deltas_acc["cfo"] == 0.0
    json.dumps(deltas_acc, allow_nan=False)


def test_c3_influence_network_propagate_normal_behavior_preserved():
    net = influence_network.InfluenceNetwork(
        [influence_network.InfluenceEdge("cto", "cfo", 0.5)]
    )
    deltas = net.propagate({"cto": 0.8, "cfo": 0.2}, propagation_rate=0.1)
    assert deltas["cfo"] == pytest.approx(0.03)
    json.dumps(deltas, allow_nan=False)


@pytest.mark.asyncio
async def test_c3_wave_number_1e308_tick1_no_gateway_delay_inf():
    lead_ctx = _ctx(
        {
            "persona": "pragmatist",
            "velocity": 1.0,
            "distance": 0.0,
            "water": 100.0,
            "wave_number": 10**308,
        },
        session_id="lead-wave-1e308",
    )
    lead_redis = MagicMock()
    lead_redis.rpush = AsyncMock()
    lead_redis.expire = AsyncMock()

    with patch.object(leadership_tick, "get_shared_redis_client", return_value=lead_redis):
        l_res = await leadership_tick.process_leadership_tick(
            lead_ctx,  # type: ignore[arg-type]
            inner_thought="Wave overflow check",
            minutes_per_tick=5.0,
            elapsed_minutes=5.0,
            race_distance_mi=26.2188,
            tick=1,
            collector_buffer_key="buf:lead-wave-r7",
        )

    assert l_res["status"] == "success"
    assert l_res["wave_number"] == 10**308
    assert "gateway_delay_seconds" not in l_res
    json.dumps(l_res, allow_nan=False)
    lead_redis.rpush.assert_awaited_once()
    _, lead_pushed = lead_redis.rpush.await_args.args
    _strict_parse_json(lead_pushed)

    run_ctx = _ctx(
        {
            "velocity": 1.0,
            "distance": 0.0,
            "water": 100.0,
            "wave_number": 10**308,
        },
        session_id="run-wave-1e308",
    )
    run_redis = MagicMock()
    run_redis.rpush = AsyncMock()
    run_redis.expire = AsyncMock()

    with patch.object(running, "get_shared_redis_client", return_value=run_redis):
        r_res = await running.process_tick(
            run_ctx,  # type: ignore[arg-type]
            inner_thought="Wave overflow check",
            minutes_per_tick=5.0,
            elapsed_minutes=5.0,
            race_distance_mi=26.2188,
            tick=1,
            collector_buffer_key="buf:run-wave-r7",
        )

    assert r_res["status"] == "success"
    assert r_res["wave_number"] == 10**308
    assert "gateway_delay_seconds" not in r_res
    json.dumps(r_res, allow_nan=False)
    run_redis.rpush.assert_awaited_once()
    _, run_pushed = run_redis.rpush.await_args.args
    _strict_parse_json(run_pushed)

    # Normal wave_number=3 parity check
    l_norm_ctx = _ctx({"persona": "pragmatist", "velocity": 1.0, "distance": 0.0, "water": 100.0, "wave_number": 3})
    l_norm = await leadership_tick.process_leadership_tick(
        l_norm_ctx,  # type: ignore[arg-type]
        inner_thought="Normal wave",
        minutes_per_tick=5.0,
        elapsed_minutes=5.0,
        race_distance_mi=26.2188,
        tick=1,
    )
    assert l_norm["gateway_delay_seconds"] == pytest.approx(6.0)
    json.dumps(l_norm, allow_nan=False)

    r_norm_ctx = _ctx({"velocity": 1.0, "distance": 0.0, "water": 100.0, "wave_number": 3})
    r_norm = await running.process_tick(
        r_norm_ctx,  # type: ignore[arg-type]
        inner_thought="Normal wave",
        minutes_per_tick=5.0,
        elapsed_minutes=5.0,
        race_distance_mi=26.2188,
        tick=1,
    )
    assert r_norm["gateway_delay_seconds"] == pytest.approx(6.0)
    json.dumps(r_norm, allow_nan=False)


@pytest.mark.asyncio
async def test_c3_runner_process_tick_large_water_station_no_overflow():
    ctx = _ctx(
        {
            "velocity": 1.0,
            "distance": 0.0,
            "water": 95.0,
            "_tick_params": {
                "station_index": {"water_stations": [1e307]},
            },
        },
        session_id="run-station-1e307",
    )
    res = await running.process_tick(
        ctx,  # type: ignore[arg-type]
        inner_thought="Large station mile marker",
        minutes_per_tick=1e308,
        elapsed_minutes=1e308,
        race_distance_mi=1e308,
        tick=1,
    )
    assert res["status"] == "success"
    assert res["distance_mi"] >= 1e307
    assert math.isfinite(res["water"])
    json.dumps(res, allow_nan=False)

    # Normal water_stations parity check
    norm_ctx = _ctx(
        {
            "velocity": 1.0,
            "distance": 0.0,
            "water": 35.0,
            "_tick_params": {
                "station_index": {"water_stations": [0.1]},
            },
        },
        session_id="run-station-norm",
    )
    norm_res = await running.process_tick(
        norm_ctx,  # type: ignore[arg-type]
        inner_thought="Normal station",
        minutes_per_tick=5.0,
        elapsed_minutes=5.0,
        race_distance_mi=26.2188,
        tick=1,
    )
    assert norm_res["status"] == "success"
    assert norm_res["water"] > 35.0
    json.dumps(norm_res, allow_nan=False)
