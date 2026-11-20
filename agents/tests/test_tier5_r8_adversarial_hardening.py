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

"""Tier 5 Round 8 adversarial regression tests for Python hardening fixes."""

from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from gen_proto.gateway import gateway_pb2

from agents.ai_echo import agent as ai_echo_agent
from agents.ai_echo import process_decision_round as pdr
from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback
from agents.ai_echo_reactive.callback import reactive_echo_callback
from agents.leadership_runner import capital
from agents.simulator.collector import RaceCollector
from agents.utils.dispatcher import RedisOrchestratorDispatcher
from agents.utils.plugins import BaseDashLogPlugin
from agents.utils.traffic import (
    _find_network_junctions,
    build_segment_distance_index,
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


def _ctx(state: dict | None = None, session_id: str = "sess-t5-r8") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id, user_id="user-t5-r8"),
        invocation_id="inv-t5-r8",
        agent_name="test_agent",
    )


def _echo_request(event_obj: dict[str, Any] | str) -> LlmRequest:
    text = event_obj if isinstance(event_obj, str) else json.dumps(event_obj)
    return LlmRequest(
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=text)],
            )
        ]
    )


# ---------------------------------------------------------------------------
# Task 1: AI Echo decision round & callbacks strict RFC-8259 JSON
# ---------------------------------------------------------------------------


def test_r8_process_decision_round_active_and_terminal_sanitize_nonfinite_and_overflow():
    bad_state_active = {
        "role": "cfo",
        "tier": 10**400,
        "sentiment": float("nan"),
        "resistance": float("inf"),
        "influence": float("nan"),
        "urgency": float("-inf"),
        "principle_affinity": [float("nan"), float("inf"), 0.4],
    }
    res_active = pdr.process_decision_round(
        state=bad_state_active,
        tick=10**400,  # type: ignore[arg-type]
        decision_tags={"principles_applied": ["Ethical Leadership & Governance"]},
        resource_state={"owb": {"current": float("nan")}},
        wall_severity=float("inf"),
        influence_deltas={"cfo": float("nan")},
    )
    serialized_active = json.dumps(res_active, allow_nan=False)
    _strict_parse_json(serialized_active)
    assert math.isfinite(res_active["influence"])
    assert res_active["influence"] == 0.5
    assert res_active["tick"] == 0
    assert res_active["tier"] == 3
    assert all(math.isfinite(v) for v in res_active["principle_affinity"])

    bad_state_terminal = {
        "role": "cto",
        "defected": True,
        "tier": True,
        "sentiment": float("nan"),
        "resistance": float("inf"),
        "influence": float("-inf"),
        "urgency": float("nan"),
        "principle_affinity": [float("nan"), 0.8],
    }
    res_terminal = pdr.process_decision_round(
        state=bad_state_terminal,
        tick=10**400,  # type: ignore[arg-type]
        wall_severity=float("nan"),
    )
    _strict_parse_json(json.dumps(res_terminal, allow_nan=False))
    assert res_terminal["echo_status"] == "defected"
    assert res_terminal["tick"] == 0
    assert res_terminal["tier"] == 3
    assert math.isfinite(res_terminal["sentiment"])
    assert math.isfinite(res_terminal["resistance"])
    assert math.isfinite(res_terminal["influence"])
    assert math.isfinite(res_terminal["urgency"])
    assert all(math.isfinite(v) for v in res_terminal["principle_affinity"])

    direct_terminal = pdr._terminal_result(
        state=bad_state_terminal,
        tick=10**400,  # type: ignore[arg-type]
        status="aligned",
        inner_thought="ok",
        wall_severity=float("inf"),
    )
    _strict_parse_json(json.dumps(direct_terminal, allow_nan=False))
    assert math.isfinite(direct_terminal["wall_severity"])


def test_r8_process_decision_round_normal_parity():
    state_active = {
        "role": "cfo",
        "tier": 2,
        "sentiment": 0.2,
        "resistance": 0.3,
        "influence": 0.75,
        "urgency": 0.2,
        "communication_receptivity": 0.8,
        "domain_principles": ["Ethical Leadership & Governance"],
    }
    res = pdr.process_decision_round(
        state=state_active,
        tick=4,
        decision_tags={"principles_applied": ["Ethical Leadership & Governance"]},
        resource_state={"owb": {"current": 70.0}},
        wall_severity=3.5,
    )
    assert res["tick"] == 4
    assert res["tier"] == 2
    assert res["influence"] == 0.75
    assert res["wall_severity"] == 3.5
    assert res["sentiment"] > 0.2
    assert res["resistance"] < 0.3

    state_term = {
        "role": "cfo",
        "tier": 1,
        "aligned": True,
        "sentiment": 0.9,
        "resistance": 0.05,
        "influence": 0.85,
        "urgency": 0.1,
        "stance": "supportive",
        "principle_affinity": [0.7] * 7,
    }
    term = pdr.process_decision_round(state=state_term, tick=7)
    assert term["tick"] == 7
    assert term["tier"] == 1
    assert term["sentiment"] == 0.9
    assert term["resistance"] == 0.05
    assert term["influence"] == 0.85
    assert term["urgency"] == 0.1
    assert term["wall_severity"] == 0.0


@pytest.mark.asyncio
async def test_r8_ai_echo_agent_direct_write_strict_rfc8259_json():
    mock_redis = MagicMock()
    mock_redis.rpush = AsyncMock(return_value=1)
    mock_redis.expire = AsyncMock(return_value=True)

    ctx = _ctx(
        state={
            "role": "cfo",
            "sentiment": 0.1,
            "resistance": 0.2,
            "influence": 0.6,
            "urgency": 0.2,
        },
        session_id="echo-sess-1",
    )

    # Simulate process_decision_round returning a dict containing NaN to verify
    # process_decision_round_tool itself enforces strict RFC-8259 serialization.
    corrupt_result = {
        "tick": 1,
        "role": "cfo",
        "sentiment": 0.1,
        "influence": float("nan"),
        "nested": [float("inf"), 0.5],
    }
    with (
        patch("agents.utils.redis_pool.get_shared_redis_client", return_value=mock_redis),
        patch("agents.ai_echo.process_decision_round.process_decision_round", return_value=corrupt_result),
    ):
        await ai_echo_agent.process_decision_round_tool(
            tool_context=ctx,
            tick=1,
            collector_buffer_key="collector:buffer:sim1",
        )

    assert mock_redis.rpush.await_count == 1
    _, pushed_raw = mock_redis.rpush.call_args.args
    parsed = _strict_parse_json(pushed_raw)
    assert parsed["payload"]["result"]["influence"] is None
    assert parsed["payload"]["result"]["nested"] == [None, 0.5]

    # Normal parity
    mock_redis.rpush.reset_mock()
    with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=mock_redis):
        res_normal = await ai_echo_agent.process_decision_round_tool(
            tool_context=ctx,
            tick=2,
            decision_tags='{"principles_applied": []}',
            resource_state='{"owb": {"current": 70.0}}',
            collector_buffer_key="collector:buffer:sim1",
        )
    assert res_normal["tick"] == 2
    _, normal_raw = mock_redis.rpush.call_args.args
    normal_parsed = _strict_parse_json(normal_raw)
    assert normal_parsed["payload"]["result"]["influence"] == 0.6


@pytest.mark.asyncio
async def test_r8_autopilot_and_reactive_callbacks_emit_strict_rfc8259_json_args():
    bad_event_json = (
        '{"event": "decision_round", "tick": 3, "decision_text": "Act", '
        '"decision_tags": {"score": NaN, "principles_applied": []}, '
        '"resource_state": {"owb": {"current": Infinity}}, '
        '"contradiction_alert": {"severity": -Infinity}, '
        '"influence_deltas": {"cfo": NaN}, '
        '"collector_buffer_key": "buf:1"}'
    )

    state_ap = {
        "role": "cfo",
        "sentiment": 0.1,
        "stance": "neutral",
        "domain_principles": ["Ethical Leadership & Governance"],
    }
    resp_ap = await autopilot_echo_callback(
        _ctx(state=state_ap),  # type: ignore[arg-type]
        _echo_request(bad_event_json),
    )
    assert resp_ap.content is not None and resp_ap.content.parts
    fc_ap = resp_ap.content.parts[0].function_call
    assert fc_ap is not None and fc_ap.args is not None
    for key in ("decision_tags", "resource_state", "contradiction_alert", "influence_deltas"):
        parsed = _strict_parse_json(fc_ap.args[key])
        assert isinstance(parsed, dict)
    assert _strict_parse_json(fc_ap.args["resource_state"]) == {"owb": {"current": None}}

    # Reactive callback: non-relevant + terminal paths both go through _deterministic_sentiment_update
    bad_event_reactive = (
        '{"event": "decision_round", "tick": 3, "decision_text": "Act", '
        '"decision_tags": {"score": NaN, "principles_applied": []}, '
        '"resource_state": {"owb": {"current": NaN}}, '
        '"contradiction_alert": null, '
        '"influence_deltas": {"cfo": Infinity}}'
    )
    state_rx = {
        "role": "cfo",
        "sentiment": 0.1,
        "stance": "neutral",
        "domain_principles": ["Ethical Leadership & Governance"],
    }
    resp_rx = await reactive_echo_callback(
        _ctx(state=state_rx),  # type: ignore[arg-type]
        _echo_request(bad_event_reactive),
    )
    assert resp_rx is not None and resp_rx.content is not None and resp_rx.content.parts
    fc_rx = resp_rx.content.parts[0].function_call
    assert fc_rx is not None and fc_rx.args is not None
    for key in ("decision_tags", "resource_state", "contradiction_alert", "influence_deltas"):
        _strict_parse_json(fc_rx.args[key])

    # Normal parity for autopilot and reactive
    normal_event = {
        "event": "decision_round",
        "tick": 5,
        "decision_text": "Align team",
        "decision_tags": {"principles_applied": ["Adaptive Decision-Making & Agility"]},
        "resource_state": {"owb": {"current": 65.0}},
        "contradiction_alert": None,
        "influence_deltas": {"cfo": 0.02},
    }
    resp_ap_ok = await autopilot_echo_callback(_ctx(state=state_ap), _echo_request(normal_event))  # type: ignore[arg-type]
    assert resp_ap_ok.content is not None and resp_ap_ok.content.parts
    fc_ap_ok = resp_ap_ok.content.parts[0].function_call
    assert fc_ap_ok is not None and fc_ap_ok.args is not None
    assert _strict_parse_json(fc_ap_ok.args["resource_state"]) == {"owb": {"current": 65.0}}
    assert _strict_parse_json(fc_ap_ok.args["influence_deltas"]) == {"cfo": 0.02}

    resp_rx_ok = await reactive_echo_callback(_ctx(state=state_rx), _echo_request(normal_event))  # type: ignore[arg-type]
    assert resp_rx_ok is not None and resp_rx_ok.content is not None and resp_rx_ok.content.parts
    fc_rx_ok = resp_rx_ok.content.parts[0].function_call
    assert fc_rx_ok is not None and fc_rx_ok.args is not None
    assert _strict_parse_json(fc_rx_ok.args["resource_state"]) == {"owb": {"current": 65.0}}


# ---------------------------------------------------------------------------
# Task 2: restore_capital OverflowError on huge tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_tick", [10**400, -10**400, True, float("inf"), float("nan")])
async def test_r8_restore_capital_huge_or_nonfinite_tick_no_overflow(bad_tick):
    ctx = _ctx(
        state={
            "water": 50.0,
            "persona": "pragmatist",
            "_tick_params": {"tick": bad_tick},
            "_capital_restored_window": [[0.0, 10.0], [3.0, 10.0]],
        }
    )
    res = await capital.restore_capital(10.0, ctx)  # type: ignore[arg-type]
    assert res["status"] == "success"
    assert res["water"] == 60.0
    assert math.isfinite(res["water"])


@pytest.mark.asyncio
async def test_r8_restore_capital_normal_cooldown_window_parity():
    ctx = _ctx(
        state={
            "water": 40.0,
            "persona": "pragmatist",
            "_tick_params": {"tick": 10},
            "_capital_restored_window": [[1.0, 25.0], [8.0, 20.0]],
        }
    )
    # tick=10: [1.0, 25.0] is outside 5-tick window (10 - 1 = 9 > 5), [8.0, 20.0] stays (10 - 8 = 2 <= 5)
    # Remaining budget in window = 30.0 - 20.0 = 10.0 (requested 25.0 -> clamped to 15.0 -> capped to 10.0)
    res = await capital.restore_capital(25.0, ctx)  # type: ignore[arg-type]
    assert res["status"] == "success"
    assert res["water"] == 50.0

    # Now window sum is 30.0 (TOOL_RESTORE_WINDOW_MAX) -> cooldown
    res2 = await capital.restore_capital(5.0, ctx)  # type: ignore[arg-type]
    assert res2["status"] == "cooldown"
    assert res2["water"] == 50.0


# ---------------------------------------------------------------------------
# Task 3: BaseDashLogPlugin._emit_narrative silent JSON-to-text downgrade
# ---------------------------------------------------------------------------


class _DummyDashPlugin(BaseDashLogPlugin):
    async def _do_publish(self, context: Any, payload: dict[str, Any]) -> None:
        return None


@pytest.mark.asyncio
async def test_r8_plugins_emit_narrative_huge_delay_does_not_downgrade_to_text():
    plugin = _DummyDashPlugin(name="dummy")
    emitted: list[dict[str, Any]] = []

    async def fake_emit_gateway_message(**kwargs: Any) -> None:
        emitted.append(kwargs)

    with patch("agents.utils.pulses.emit_gateway_message", side_effect=fake_emit_gateway_message):
        # 1) Full _publish pipeline with gateway_delay_seconds=10**400 and NaN field
        await plugin._publish(
            _ctx(state={"simulation_id": "sim-1"}),
            {
                "type": "tool_end",
                "agent": "leadership_runner",
                "tool": "process_leadership_tick",
                "result": {
                    "status": "success",
                    "gateway_delay_seconds": 10**400,
                    "bad_float": float("nan"),
                },
            },
        )
        # 2) Direct _emit_narrative call bypassing _publish sanitization
        await plugin._emit_narrative(
            session_id="sess-direct",
            payload={
                "type": "tool_end",
                "agent": "leadership_runner",
                "tool": "process_leadership_tick",
                "result": {
                    "status": "success",
                    "gateway_delay_seconds": 10**400,
                    "bad_float": float("inf"),
                },
            },
            simulation_id="sim-1",
        )

    assert len(emitted) == 2
    for call in emitted:
        assert call["msg_type"] == "json"
        assert call["event"] == "tool_end"
        _strict_parse_json(json.dumps(call["data"], allow_nan=False))
        assert call["data"]["result"]["bad_float"] is None


@pytest.mark.asyncio
async def test_r8_plugins_emit_narrative_normal_positive_delay_parity():
    plugin = _DummyDashPlugin(name="dummy")
    emitted: list[dict[str, Any]] = []

    async def fake_emit_gateway_message(**kwargs: Any) -> None:
        emitted.append(kwargs)

    with (
        patch("agents.utils.pulses.emit_gateway_message", side_effect=fake_emit_gateway_message),
        patch("agents.utils.plugins.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        await plugin._emit_narrative(
            session_id="sess-delay",
            payload={
                "type": "tool_end",
                "agent": "runner",
                "tool": "process_tick",
                "result": {"status": "success", "gateway_delay_seconds": 0.25},
            },
            simulation_id="sim-1",
        )

    mock_sleep.assert_awaited_once_with(0.25)
    assert len(emitted) == 1
    assert emitted[0]["msg_type"] == "json"
    assert emitted[0]["data"]["result"] == {"status": "success"}


# ---------------------------------------------------------------------------
# Task 4: GeoJSON validation contract & consumer safety in traffic.py
# ---------------------------------------------------------------------------


def test_r8_traffic_geojson_validator_and_consumers_reject_and_guard_null_geometry():
    bad_null_geom = {"features": [{"geometry": None, "properties": {"name": "Road A"}}]}
    ok, msg = validate_route_geojson(bad_null_geom)
    assert ok is False
    assert "geometry" in msg.lower()

    bad_str_geom = {"features": [{"geometry": "LineString", "properties": {"name": "Road A"}}]}
    ok2, msg2 = validate_route_geojson(bad_str_geom)
    assert ok2 is False
    assert "geometry" in msg2.lower()

    # Downstream consumers must not raise AttributeError on geometry=None or properties=None
    mixed_fc = {
        "type": "FeatureCollection",
        "features": [
            {"geometry": None, "properties": {"name": "Road A"}},
            {"geometry": None, "properties": None},
            {
                "type": "Feature",
                "properties": None,
                "geometry": {"type": "LineString", "coordinates": [[0.0, 0.0], [0.0, 1.0]]},
            },
        ],
    }
    seg_index = build_segment_distance_index(mixed_fc)
    assert len(seg_index) == 1
    assert seg_index[0]["road_name"] == ""
    assert seg_index[0]["end_mi"] > 60.0

    junctions = _find_network_junctions(mixed_fc)
    assert junctions == set()

    closure_res = identify_closed_segments(mixed_fc, mixed_fc)
    assert "closed" in closure_res and "route_closures" in closure_res


def test_r8_traffic_geojson_normal_parity():
    good_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Main St"},
                "geometry": {"type": "LineString", "coordinates": [[-87.63, 41.87], [-87.63, 41.88]]},
            }
        ],
    }
    ok, msg = validate_route_geojson(good_fc)
    assert ok is True
    assert msg == ""
    idx = build_segment_distance_index(good_fc)
    assert len(idx) == 1
    assert idx[0]["road_name"] == "Main St"
    assert 0.5 < idx[0]["end_mi"] < 1.0


def test_r8_traffic_stringified_route_features_fail_loudly():
    bad_stringified_route = {
        "type": "FeatureCollection",
        "features": [
            '{"type": "Feature", "properties": {"name": "Main St"}, '
            '"geometry": {"type": "LineString", "coordinates": [[-87.63, 41.87], [-87.63, 41.88]]}}'
        ],
    }
    ok, msg = validate_route_geojson(bad_stringified_route)
    assert ok is False
    assert "str" in msg.lower()

    empty_network = {"type": "FeatureCollection", "features": []}
    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        identify_closed_segments(bad_stringified_route, empty_network)

    with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
        build_segment_distance_index(bad_stringified_route)


def test_r8_traffic_empty_coordinates_and_malformed_network_features_safe():
    route_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Empty Route"},
                "geometry": {"type": "LineString", "coordinates": []},
            },
            {
                "type": "Feature",
                "properties": {"name": "Main St"},
                "geometry": {"type": "LineString", "coordinates": [[-87.63, 41.87], [-87.63, 41.88]]},
            },
        ],
    }
    network_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Empty Net"},
                "geometry": {"type": "LineString", "coordinates": []},
            },
            {"type": "Feature", "geometry": None, "properties": {"name": "NullGeom"}},
            {"type": "Feature", "geometry": "LineString", "properties": {"name": "StrGeom"}},
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": None},
                "properties": "bad_props",
            },
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[-87.62, 41.89], [-87.62, 41.90]]},
                "properties": None,
            },
            {
                "type": "Feature",
                "properties": {"name": "Main St"},
                "geometry": {"type": "LineString", "coordinates": [[-87.63, 41.87], [-87.63, 41.88]]},
            },
            {
                "type": "Feature",
                "properties": {"name": "Cross Ave"},
                "geometry": {"type": "LineString", "coordinates": [[-87.63, 41.87], [-87.64, 41.87]]},
            },
        ],
    }

    seg_index = build_segment_distance_index(route_fc)
    assert len(seg_index) == 2
    assert seg_index[0]["end_mi"] == 0.0
    assert seg_index[1]["road_name"] == "Main St"
    assert 0.5 < seg_index[1]["end_mi"] < 1.0

    junctions = _find_network_junctions(network_fc)
    assert junctions == {(-87.63, 41.87)}

    closure_res = identify_closed_segments(route_fc, network_fc)
    assert [f["properties"]["name"] for f in closure_res["route_closures"]] == ["Main St"]
    assert [f["properties"]["name"] for f in closure_res["affected"]] == ["Cross Ave"]
    assert closure_res["closed"] == []
    assert len(closure_res["intersections"]) == 1
    assert set(closure_res["intersections"][0]["cross_streets"]) == {"Main St", "Cross Ave"}


# ---------------------------------------------------------------------------
# Task 5: Dispatcher _prepare_pulses & RaceCollector._collect_loop RFC-8259 JSON
# ---------------------------------------------------------------------------


def test_r8_dispatcher_prepare_pulses_strict_rfc8259_json():
    mock_runner = MagicMock()
    mock_runner.app.name = "test_agent"
    dispatcher = RedisOrchestratorDispatcher(runner=mock_runner, redis_url="redis://dummy")

    part_bad = SimpleNamespace(
        text=None,
        function_call=None,
        function_response=SimpleNamespace(
            response={"influence": float("nan"), "vals": [float("inf"), float("-inf"), 1.25]}
        ),
    )
    event_bad = SimpleNamespace(
        author="test_agent",
        content=SimpleNamespace(parts=[part_bad]),
    )
    pulses = dispatcher._prepare_pulses("sess-1", event_bad)
    assert len(pulses) == 1
    parsed = _strict_parse_json(pulses[0])
    assert parsed == {"influence": None, "vals": [None, None, 1.25]}

    # Normal parity
    part_ok = SimpleNamespace(
        text=None,
        function_call=None,
        function_response=SimpleNamespace(response={"status": "success", "count": 3}),
    )
    event_ok = SimpleNamespace(author="test_agent", content=SimpleNamespace(parts=[part_ok]))
    pulses_ok = dispatcher._prepare_pulses("sess-1", event_ok)
    assert len(pulses_ok) == 1
    assert _strict_parse_json(pulses_ok[0]) == {"status": "success", "count": 3}


@pytest.mark.asyncio
async def test_r8_collector_collect_loop_pushes_strict_rfc8259_json():
    collector = RaceCollector(session_id="sim-r8", runner_session_ids={"r1"})
    mock_redis = MagicMock()
    mock_redis.rpush = AsyncMock(return_value=1)
    mock_redis.ltrim = AsyncMock(return_value=True)
    mock_redis.expire = AsyncMock(return_value=True)

    w_bad = gateway_pb2.Wrapper()
    w_bad.origin.session_id = "r1"
    w_bad.origin.id = "echo_1"
    w_bad.event = "tool_end"
    w_bad.type = "json"
    w_bad.timestamp = "2026-11-19T00:00:00Z"
    w_bad.payload = b'{"tool_name": "process_decision_round", "result": {"influence": NaN, "vals": [Infinity, -Infinity, 0.5]}}'

    w_ok = gateway_pb2.Wrapper()
    w_ok.origin.session_id = "r1"
    w_ok.origin.id = "echo_1"
    w_ok.event = "tool_end"
    w_ok.type = "json"
    w_ok.timestamp = "2026-11-19T00:00:01Z"
    w_ok.payload = b'{"tool_name": "process_decision_round", "result": {"influence": 0.8}}'

    messages = [
        {"type": "message", "data": w_bad.SerializeToString()},
        {"type": "message", "data": w_ok.SerializeToString()},
    ]

    async def fake_get_message(**kwargs: Any) -> dict[str, Any] | None:
        if messages:
            return messages.pop(0)
        raise asyncio.CancelledError()

    mock_pubsub = MagicMock()
    mock_pubsub.get_message = AsyncMock(side_effect=fake_get_message)

    collector._redis = mock_redis
    collector._pubsub = mock_pubsub

    await collector._collect_loop()

    assert mock_redis.rpush.await_count == 2
    bad_raw = mock_redis.rpush.call_args_list[0].args[1]
    ok_raw = mock_redis.rpush.call_args_list[1].args[1]

    bad_parsed = _strict_parse_json(bad_raw)
    assert bad_parsed["payload"]["result"]["influence"] is None
    assert bad_parsed["payload"]["result"]["vals"] == [None, None, 0.5]

    ok_parsed = _strict_parse_json(ok_raw)
    assert ok_parsed["payload"]["result"]["influence"] == 0.8
