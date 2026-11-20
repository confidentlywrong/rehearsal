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

"""Tier 5 Iteration 13 adversarial regression tests for A2UI context_id & user_id forwarding."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from agents.leadership_planner_with_memory.memory import tools as _LMEM_TOOLS
from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore
from agents.simulator import pre_race_callback as _PRE_RACE_CB
from agents.utils import pulses, simulation_registry
from gen_proto.gateway import gateway_pb2

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_module(name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_A2UI = _load_module("r13_a2ui_tools", "agents/skills/a2ui-rendering/tools.py")
_CONSULTANT = _load_module("r13_consultant_tools", "agents/leadership_planner_with_eval/skills/consultant/tools.py")
_ADVANCE_TICKS = _load_module("r13_advance_tick_tools", "agents/simulator/skills/advancing-race-ticks/tools.py")


@pytest.fixture(autouse=True)
def _clean_r13_state(monkeypatch):
    for var in ("SAAS_MODE", "DISPATCHER_USER_ID", "ACCOUNT_KEY_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    pulses.reset()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()
    yield
    pulses.reset()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()


def _make_publishing_redis():
    published: list[tuple[str, bytes]] = []

    class _FakePipe:
        def __init__(self):
            self._items: list[tuple[str, bytes]] = []

        def publish(self, channel: str, msg: bytes):
            self._items.append((channel, msg))

        async def execute(self):
            published.extend(self._items)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    fake_redis = MagicMock()
    fake_redis.pipeline = MagicMock(return_value=_FakePipe())
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.setex = AsyncMock(return_value=True)
    return published, fake_redis


@pytest.mark.asyncio
async def test_a2ui_emitters_resolve_context_id_and_forward_user_id_under_saas_mode(monkeypatch):
    """All four A2UI emitters map Agent Engine Vertex session_id -> spawn_ctx_uuid and forward user_id."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

    await simulation_registry.register_context("ae-40765115", "spawn-ctx-uuid", user_id="alice")
    # Register another tenant with the same Vertex ID to prove tenant isolation.
    await simulation_registry.register_context("ae-40765115", "bob-spawn-ctx-uuid", user_id="bob")

    ctx = SimpleNamespace(
        session=SimpleNamespace(id="ae-40765115", user_id="alice"),
        state={
            "user_id": "alice",
            "simulation_id": "sim-r13-1",
            "leadership_scenario": {"name": "AI Governance Pivot"},
            "leadership_evaluation": {
                "status": "success",
                "overall_score": 88,
                "scores": {
                    "stakeholder_alignment": 86,
                    "decision_quality": 90,
                    "resource_stewardship": 84,
                    "crisis_readiness": 82,
                },
            },
        },
    )

    published, fake_redis = _make_publishing_redis()
    a2ui_captured: list[dict] = []
    consultant_captured: list[dict] = []
    orig_emit = pulses.emit_gateway_message

    async def _capture_a2ui(**kwargs):
        a2ui_captured.append(kwargs)
        await orig_emit(**kwargs)

    async def _capture_consultant(**kwargs):
        consultant_captured.append(kwargs)
        await orig_emit(**kwargs)

    with (
        patch("agents.utils.pulses.emit_gateway_message", side_effect=_capture_a2ui),
        patch.object(_CONSULTANT, "emit_gateway_message", side_effect=_capture_consultant),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={})),
        patch("agents.utils.pulses._get_redis_client", return_value=fake_redis),
    ):
        # 1. validate_and_emit_a2ui("dashboard")
        res_dash = await _A2UI.validate_and_emit_a2ui("dashboard", tool_context=ctx)
        assert res_dash["status"] == "success"

        # 2. validate_and_emit_a2ui(raw_json)
        res_raw = await _A2UI.validate_and_emit_a2ui(
            json.dumps({"beginRendering": {"surfaceId": "s1", "root": "c1"}}),
            tool_context=ctx,
        )
        assert res_raw["status"] == "success"

        # 3. emit_sim_results_a2ui
        res_sim = await _CONSULTANT.emit_sim_results_a2ui(ctx)
        assert res_sim.startswith("SUCCESS:")

        # 4. emit_action_plan_a2ui
        res_plan = await _CONSULTANT.emit_action_plan_a2ui(ctx, plan_markdown="# Days 1-30\n- Align leadership")
        assert res_plan.startswith("SUCCESS:")

        # Flush async publish worker queue
        queue = pulses._get_queue()
        while not queue.empty() or len(published) < 7:
            await pulses.asyncio.sleep(0.01)

    assert len(a2ui_captured) == 3  # 2 for dashboard + 1 for raw beginRendering
    assert len(consultant_captured) == 4  # 2 for sim_results + 2 for action_plan

    for call in a2ui_captured + consultant_captured:
        assert call["origin"]["session_id"] == "spawn-ctx-uuid"
        assert call["destination"] == ["spawn-ctx-uuid"]
        assert call["user_id"] == "alice"

    assert len(published) == 7
    for channel, raw_bytes in published:
        assert channel == "u:alice:gateway:broadcast"
        wrapper = gateway_pb2.Wrapper()
        wrapper.ParseFromString(raw_bytes)
        assert wrapper.origin.session_id == "spawn-ctx-uuid"
        assert list(wrapper.destination) == ["spawn-ctx-uuid"]


@pytest.mark.asyncio
async def test_a2ui_emitters_fail_safe_when_saas_mode_on_without_user_id(monkeypatch):
    """Under SAAS_MODE=on with no user_id on context, emitters fall back to raw session_id without crashing."""
    monkeypatch.setenv("SAAS_MODE", "on")
    await simulation_registry.register_context("ae-999", "bob-ctx-should-not-leak", user_id="bob")

    ctx = SimpleNamespace(
        session=SimpleNamespace(id="ae-999"),
        state={
            "leadership_evaluation": {"status": "success", "overall_score": 81, "scores": {}},
        },
    )

    a2ui_captured: list[dict] = []
    consultant_captured: list[dict] = []

    async def _capture_a2ui(**kwargs):
        a2ui_captured.append(kwargs)

    async def _capture_consultant(**kwargs):
        consultant_captured.append(kwargs)

    with (
        patch("agents.utils.pulses.emit_gateway_message", side_effect=_capture_a2ui),
        patch.object(_CONSULTANT, "emit_gateway_message", side_effect=_capture_consultant),
    ):
        res_dash = await _A2UI.validate_and_emit_a2ui("dashboard", tool_context=ctx)
        res_raw = await _A2UI.validate_and_emit_a2ui(
            json.dumps({"beginRendering": {"surfaceId": "s1", "root": "c1"}}),
            tool_context=ctx,
        )
        res_sim = await _CONSULTANT.emit_sim_results_a2ui(ctx)
        res_plan = await _CONSULTANT.emit_action_plan_a2ui(ctx, plan_markdown="# Days 1-30\n- Step 1")

    assert res_dash["status"] == "success"
    assert res_raw["status"] == "success"
    assert res_sim.startswith("SUCCESS:")
    assert res_plan.startswith("SUCCESS:")

    for call in a2ui_captured + consultant_captured:
        assert call["origin"]["session_id"] == "ae-999"
        assert call["destination"] == ["ae-999"]
        assert "user_id" not in call


@pytest.mark.asyncio
async def test_memory_a2ui_emitters_resolve_context_id_and_forward_user_id_under_saas_mode(monkeypatch):
    """emit_scenario_list_a2ui (empty + non-empty) and emit_scenario_detail_a2ui resolve context_id & user_id."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

    await simulation_registry.register_context("ae-40765115", "spawn-ctx-uuid", user_id="alice")
    await simulation_registry.register_context("ae-40765115", "bob-spawn-ctx-uuid", user_id="bob")

    mem_store = ScenarioMemoryStore()
    alice_sid = await mem_store.store_scenario(
        scenario_data={
            "name": "Alice AI Governance",
            "scenario_type": "leadership",
            "stakeholder_count": 6,
            "_consensus_pathway_active": {
                "geojson": {"type": "FeatureCollection", "features": []},
            },
        },
        evaluation_score=91.0,
        evaluation_result={"scores": {"stakeholder_alignment": 90}},
        owner_id="alice",
    )
    await mem_store.store_scenario(
        scenario_data={"name": "Bob Private"},
        evaluation_score=80.0,
        owner_id="bob",
    )

    alice_ctx = SimpleNamespace(
        session=SimpleNamespace(id="ae-40765115", user_id="alice"),
        state={"user_id": "alice", "simulation_id": "sim-mem-1"},
    )
    carol_empty_ctx = SimpleNamespace(
        session=SimpleNamespace(id="ae-40765115", user_id="carol"),
        state={"user_id": "carol", "simulation_id": "sim-mem-empty"},
    )
    await simulation_registry.register_context("ae-40765115", "carol-spawn-ctx-uuid", user_id="carol")

    published, fake_redis = _make_publishing_redis()
    captured: list[dict] = []
    orig_emit = pulses.emit_gateway_message

    async def _capture_emit(**kwargs):
        captured.append(kwargs)
        await orig_emit(**kwargs)

    with (
        patch.object(_LMEM_TOOLS, "_store", mem_store),
        patch("agents.utils.pulses.emit_gateway_message", side_effect=_capture_emit),
        patch("agents.utils.pulses._get_redis_client", return_value=fake_redis),
    ):
        # 1. Non-empty list for Alice
        res_list = await _LMEM_TOOLS.emit_scenario_list_a2ui(alice_ctx, limit=3)  # type: ignore[arg-type]
        assert res_list == {"status": "success", "count": 1}

        # 2. Pathway detail emit for Alice
        res_detail = await _LMEM_TOOLS.emit_scenario_detail_a2ui(alice_ctx, scenario_id=alice_sid)  # type: ignore[arg-type]
        assert res_detail["status"] == "success"
        assert res_detail["pathway_emitted"] is True

        # 3. Empty-state list emit for Carol
        res_empty = await _LMEM_TOOLS.emit_scenario_list_a2ui(carol_empty_ctx, limit=3)  # type: ignore[arg-type]
        assert res_empty == {"status": "empty", "count": 0}

        queue = pulses._get_queue()
        while not queue.empty() or len(published) < 5:
            await pulses.asyncio.sleep(0.01)

    assert len(captured) == 5  # 2 list + 1 detail + 2 empty-state list
    for call in captured[:3]:
        assert call["origin"]["session_id"] == "spawn-ctx-uuid"
        assert call["destination"] == ["spawn-ctx-uuid"]
        assert call["user_id"] == "alice"
    for call in captured[3:]:
        assert call["origin"]["session_id"] == "carol-spawn-ctx-uuid"
        assert call["destination"] == ["carol-spawn-ctx-uuid"]
        assert call["user_id"] == "carol"

    assert len(published) == 5
    for idx, (channel, raw_bytes) in enumerate(published):
        expected_user = "alice" if idx < 3 else "carol"
        expected_ctx = "spawn-ctx-uuid" if idx < 3 else "carol-spawn-ctx-uuid"
        assert channel == f"u:{expected_user}:gateway:broadcast"
        wrapper = gateway_pb2.Wrapper()
        wrapper.ParseFromString(raw_bytes)
        assert wrapper.origin.session_id == expected_ctx
        assert list(wrapper.destination) == [expected_ctx]


@pytest.mark.asyncio
async def test_memory_a2ui_emitters_fail_safe_without_user_id(monkeypatch):
    """Under SAAS_MODE=on when _resolve_owner_id is bypassed and uid is None, memory A2UI falls back safely."""
    monkeypatch.setenv("SAAS_MODE", "on")
    await simulation_registry.register_context("ae-999", "bob-ctx-should-not-leak", user_id="bob")

    mem_store = ScenarioMemoryStore()
    sid = await mem_store.store_scenario(
        scenario_data={
            "name": "Legacy Test",
            "_consensus_pathway_active": {"geojson": {"type": "FeatureCollection", "features": []}},
        },
        evaluation_score=85.0,
    )
    ctx = SimpleNamespace(session=SimpleNamespace(id="ae-999"), state={})
    captured: list[dict] = []

    async def _capture_emit(**kwargs):
        captured.append(kwargs)

    with (
        patch.object(_LMEM_TOOLS, "_store", mem_store),
        patch.object(_LMEM_TOOLS, "_resolve_owner_id", return_value=None),
        patch("agents.utils.pulses.emit_gateway_message", side_effect=_capture_emit),
    ):
        res_list = await _LMEM_TOOLS.emit_scenario_list_a2ui(ctx, limit=3)  # type: ignore[arg-type]
        res_detail = await _LMEM_TOOLS.emit_scenario_detail_a2ui(ctx, scenario_id=sid)  # type: ignore[arg-type]

    assert res_list == {"status": "success", "count": 1}
    assert res_detail["status"] == "success"
    assert len(captured) == 3
    for call in captured:
        assert call["origin"]["session_id"] == "ae-999"
        assert call["destination"] == ["ae-999"]
        assert "user_id" not in call


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "simulation_id"),
    [
        ("ae-sim-40765115", "sim-r13-direct"),
        ("adk-child-random-uuid", "ae-sim-40765115"),
        ("adk-child-random-uuid", "spawn-sim-ctx-uuid"),
    ],
)
async def test_advance_tick_resolves_context_id_and_user_id_across_ae_and_agent_tool(
    monkeypatch,
    session_id,
    simulation_id,
):
    """advance_tick emits coaching + dashboard A2UI with spawn context_id and user_id, keeping collector key raw."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

    await simulation_registry.register_context("ae-sim-40765115", "spawn-sim-ctx-uuid", user_id="alice")
    await simulation_registry.register_context("ae-sim-40765115", "bob-sim-ctx-uuid", user_id="bob")

    state = {
        "simulation_id": simulation_id,
        "user_id": "alice",
        "current_tick": 2,
        "max_ticks": 10,
        "simulation_config": {
            "total_race_hours": 2.0,
            "tick_interval_seconds": 0.0,
            "race_distance_mi": 26.2,
        },
        # Depleted resource forces coaching_card emission alongside live_dashboard.
        "resources": {"owb": {"current": 0.0, "critical_threshold": 20.0, "initial": 100.0}},
        "principles": ["Ethical Leadership & Governance"],
    }
    ctx = SimpleNamespace(
        session=SimpleNamespace(id=session_id, user_id="alice"),
        state=state,
    )

    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=[])
    collector_get_calls: list[str] = []

    def _fake_collector_get(sid: str):
        collector_get_calls.append(sid)
        return fake_collector

    published, fake_redis = _make_publishing_redis()
    captured: list[dict] = []
    orig_emit = pulses.emit_gateway_message

    async def _capture_emit(**kwargs):
        captured.append(kwargs)
        await orig_emit(**kwargs)

    with (
        patch.object(_ADVANCE_TICKS.RaceCollector, "get", side_effect=_fake_collector_get),
        patch.object(_ADVANCE_TICKS, "publish_to_runners", new=AsyncMock()),
        patch.object(_ADVANCE_TICKS, "emit_gateway_message", side_effect=_capture_emit),
        patch("agents.utils.pulses._get_redis_client", return_value=fake_redis),
    ):
        res = await _ADVANCE_TICKS.advance_tick(ctx)
        queue = pulses._get_queue()
        while not queue.empty() or len(published) < 4:
            await pulses.asyncio.sleep(0.01)

    assert res["status"] == "success"
    assert res["coaching_card_emitted"] is True
    # RaceCollector lookup must continue using the raw session_id (matches start_race_collector).
    assert collector_get_calls == [session_id]

    assert len(captured) == 4  # 2 coaching card + 2 live dashboard
    for call in captured:
        assert call["origin"]["session_id"] == "spawn-sim-ctx-uuid"
        assert call["destination"] == ["spawn-sim-ctx-uuid"]
        assert call["user_id"] == "alice"

    assert len(published) == 4
    for channel, raw_bytes in published:
        assert channel == "u:alice:gateway:broadcast"
        wrapper = gateway_pb2.Wrapper()
        wrapper.ParseFromString(raw_bytes)
        assert wrapper.origin.session_id == "spawn-sim-ctx-uuid"
        assert list(wrapper.destination) == ["spawn-sim-ctx-uuid"]


@pytest.mark.asyncio
async def test_advance_tick_fail_safe_without_user_id_under_saas_mode(monkeypatch):
    """Under SAAS_MODE=on with no user_id on context, advance_tick falls back without leaking Bob's context."""
    monkeypatch.setenv("SAAS_MODE", "on")
    await simulation_registry.register_context("ae-999", "bob-ctx-should-not-leak", user_id="bob")

    ctx = SimpleNamespace(
        session=SimpleNamespace(id="ae-999"),
        state={
            "simulation_id": "sim-anon-1",
            "current_tick": 0,
            "max_ticks": 5,
            "simulation_config": {"total_race_hours": 1.0, "tick_interval_seconds": 0.0},
            "resources": {"owb": {"current": 0.0, "critical_threshold": 20.0, "initial": 100.0}},
        },
    )
    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=[])
    captured: list[dict] = []

    async def _capture_emit(**kwargs):
        captured.append(kwargs)

    with (
        patch.object(_ADVANCE_TICKS.RaceCollector, "get", return_value=fake_collector),
        patch.object(_ADVANCE_TICKS, "publish_to_runners", new=AsyncMock()),
        patch.object(_ADVANCE_TICKS, "emit_gateway_message", side_effect=_capture_emit),
    ):
        res = await _ADVANCE_TICKS.advance_tick(ctx)

    assert res["status"] == "success"
    assert len(captured) == 4
    for call in captured:
        assert call["origin"]["session_id"] == "ae-999"
        assert call["destination"] == ["ae-999"]
        assert "user_id" not in call


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "simulation_id"),
    [
        ("ae-pre-40765115", "sim-pre-direct"),
        ("adk-child-pre-uuid", "ae-pre-40765115"),
        ("adk-child-pre-uuid", "spawn-pre-ctx-uuid"),
    ],
)
async def test_pre_race_callback_error_resolves_context_id_and_user_id(
    monkeypatch,
    session_id,
    simulation_id,
):
    """_broadcast_pre_race_error resolves spawn context_id and forwards user_id on SAAS_MODE=on."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

    await simulation_registry.register_context("ae-pre-40765115", "spawn-pre-ctx-uuid", user_id="alice")
    await simulation_registry.register_context("ae-pre-40765115", "bob-pre-ctx-uuid", user_id="bob")

    ctx = SimpleNamespace(
        session=SimpleNamespace(id=session_id, user_id="alice"),
        state={"simulation_id": simulation_id, "user_id": "alice"},
    )
    req = LlmRequest(
        model="test-model",
        contents=[
            types.Content(
                role="tool",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="spawn_runners",
                            response={"status": "error", "message": "gateway unavailable"},
                        )
                    )
                ],
            )
        ],
    )

    published, fake_redis = _make_publishing_redis()
    captured: list[dict] = []
    orig_emit = pulses.emit_gateway_message

    async def _capture_emit(**kwargs):
        captured.append(kwargs)
        await orig_emit(**kwargs)

    with (
        patch.object(_PRE_RACE_CB, "emit_gateway_message", side_effect=_capture_emit),
        patch("agents.utils.pulses._get_redis_client", return_value=fake_redis),
    ):
        resp = _PRE_RACE_CB.pre_race_callback(ctx, req)  # type: ignore[arg-type]
        assert resp.content is not None and "aborted" in (resp.content.parts[0].text or "")  # type: ignore[index]
        pending = list(_PRE_RACE_CB._background_tasks)
        if pending:
            await asyncio.gather(*pending)
        queue = pulses._get_queue()
        while not queue.empty() or len(published) < 1:
            await pulses.asyncio.sleep(0.01)

    assert len(captured) == 1
    call = captured[0]
    assert call["origin"]["session_id"] == "spawn-pre-ctx-uuid"
    assert call["destination"] == ["spawn-pre-ctx-uuid"]
    assert call["user_id"] == "alice"
    assert call["event"] == "pre_race_aborted"

    assert len(published) == 1
    channel, raw_bytes = published[0]
    assert channel == "u:alice:gateway:broadcast"
    wrapper = gateway_pb2.Wrapper()
    wrapper.ParseFromString(raw_bytes)
    assert wrapper.origin.session_id == "spawn-pre-ctx-uuid"
    assert list(wrapper.destination) == ["spawn-pre-ctx-uuid"]


@pytest.mark.asyncio
async def test_pre_race_callback_error_fail_safe_without_user_id(monkeypatch):
    """Under SAAS_MODE=on with no user_id on context, _broadcast_pre_race_error does not crash or leak."""
    monkeypatch.setenv("SAAS_MODE", "on")
    await simulation_registry.register_context("ae-999", "bob-ctx-should-not-leak", user_id="bob")

    ctx = SimpleNamespace(
        session=SimpleNamespace(id="ae-999"),
        state={"simulation_id": "sim-anon-pre"},
    )
    req = LlmRequest(
        model="test-model",
        contents=[
            types.Content(
                role="tool",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="prepare_simulation",
                            response={"status": "error", "message": "bad plan"},
                        )
                    )
                ],
            )
        ],
    )
    captured: list[dict] = []

    async def _capture_emit(**kwargs):
        captured.append(kwargs)

    with patch.object(_PRE_RACE_CB, "emit_gateway_message", side_effect=_capture_emit):
        _PRE_RACE_CB.pre_race_callback(ctx, req)  # type: ignore[arg-type]
        pending = list(_PRE_RACE_CB._background_tasks)
        if pending:
            await asyncio.gather(*pending)

    assert len(captured) == 1
    assert captured[0]["origin"]["session_id"] == "ae-999"
    assert captured[0]["destination"] == ["ae-999"]
    assert "user_id" not in captured[0]
