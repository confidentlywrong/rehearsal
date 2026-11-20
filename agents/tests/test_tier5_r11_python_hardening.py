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

"""Tier 5 Iteration 11 adversarial regression tests for Python multi-tenant
isolation and plugin delay resilience (Gaps 1-5).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.runners import InMemoryRunner

from agents.leadership_runner import leadership_tick
from agents.runner import running
from agents.simulator import broadcast as sim_broadcast
from agents.simulator import collector as collector_mod
from agents.utils import plugins as plugins_mod
from agents.utils import pulses as pulses_mod
from agents.utils import simulation_registry
from agents.utils import tenant_keys
from agents.utils.communication import call_agent
from agents.utils.dispatcher import RedisOrchestratorDispatcher


@pytest.fixture(autouse=True)
def _clean_r11_state(monkeypatch):
    for var in (
        "SAAS_MODE",
        "DISPATCHER_USER_ID",
        "ACCOUNT_KEY_PREFIX",
        "DISPATCH_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    pulses_mod.reset()
    collector_mod._instances.clear()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()
    yield
    pulses_mod.reset()
    collector_mod._instances.clear()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()


def _make_fake_redis() -> tuple[dict[str, bytes], dict[str, list[bytes]], AsyncMock]:
    kv_store: dict[str, bytes] = {}
    list_store: dict[str, list[bytes]] = {}
    published: list[tuple[str, bytes]] = []
    mock_redis = AsyncMock()

    async def _get(key: str):
        return kv_store.get(key)

    async def _set(key: str, value: str | bytes, ex: int | None = None, nx: bool = False):
        if nx and key in kv_store:
            return False
        kv_store[key] = value if isinstance(value, bytes) else str(value).encode()
        return True

    async def _setex(key: str, ttl: int, value: str | bytes):
        kv_store[key] = value if isinstance(value, bytes) else str(value).encode()
        return True

    async def _delete(*keys: str):
        removed = 0
        for k in keys:
            ks = k.decode() if isinstance(k, bytes) else k
            if ks in kv_store:
                del kv_store[ks]
                removed += 1
            if ks in list_store:
                del list_store[ks]
                removed += 1
        return removed

    async def _rpush(key: str, *values: str | bytes):
        bucket = list_store.setdefault(key, [])
        for v in values:
            bucket.append(v if isinstance(v, bytes) else str(v).encode())
        return len(bucket)

    async def _scan_iter(match: str = "*", count: int = 100):
        prefix = match[:-1] if match.endswith("*") else match
        for k in list(kv_store.keys()):
            if k.startswith(prefix):
                yield k

    def _pipeline(transaction: bool = True):
        ops: list[tuple[str, tuple]] = []
        pipe = AsyncMock()

        async def _aenter():
            return pipe

        async def _aexit(exc_type, exc, tb):
            return False

        def _publish(channel: str, msg: bytes):
            ops.append(("publish", (channel, msg)))

        def _lrange(key: str, start: int, end: int):
            ops.append(("lrange", (key, start, end)))

        def _pipe_delete(key: str):
            ops.append(("delete", (key,)))

        async def _execute():
            results = []
            for op, args in ops:
                if op == "publish":
                    published.append((args[0], args[1]))
                    results.append(1)
                elif op == "lrange":
                    key = args[0]
                    results.append(list(list_store.get(key, [])))
                elif op == "delete":
                    key = args[0]
                    existed = 1 if key in list_store or key in kv_store else 0
                    list_store.pop(key, None)
                    kv_store.pop(key, None)
                    results.append(existed)
            return results

        pipe.__aenter__ = AsyncMock(side_effect=_aenter)
        pipe.__aexit__ = AsyncMock(side_effect=_aexit)
        pipe.publish = MagicMock(side_effect=_publish)
        pipe.lrange = MagicMock(side_effect=_lrange)
        pipe.delete = MagicMock(side_effect=_pipe_delete)
        pipe.execute = AsyncMock(side_effect=_execute)
        return pipe

    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.setex = AsyncMock(side_effect=_setex)
    mock_redis.delete = AsyncMock(side_effect=_delete)
    mock_redis.rpush = AsyncMock(side_effect=_rpush)
    mock_redis.scan_iter = _scan_iter
    mock_redis.pipeline = MagicMock(side_effect=_pipeline)
    mock_redis.publish = AsyncMock(side_effect=lambda ch, msg: published.append((ch, msg)) or 1)
    mock_redis._published = published  # type: ignore[attr-defined]
    return kv_store, list_store, mock_redis


# ---------------------------------------------------------------------------
# Gap 1: call_agent() inter-agent pulse tenant routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatcher_user", [None, "bob"])
async def test_r11_gap1_call_agent_routes_inter_agent_pulses_to_resolved_user(monkeypatch, dispatcher_user):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    if dispatcher_user is None:
        monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)
    else:
        monkeypatch.setenv("DISPATCHER_USER_ID", dispatcher_user)

    _, _, mock_redis = _make_fake_redis()
    mock_a2a = AsyncMock()
    mock_a2a.call_agent = AsyncMock(return_value="acknowledged by runner")

    tc = SimpleNamespace(
        invocation_id="inv-r11-g1",
        agent_name="simulator",
        session=SimpleNamespace(id="sess-alice-1"),
        state={"user_id": "alice", "simulation_id": "sim-alice-1"},
    )

    with (
        patch("agents.utils.communication_plugin.get_client", return_value=mock_a2a),
        patch("agents.utils.simulation_registry.get_context_id", new=AsyncMock(return_value="ctx-alice-1")),
        patch("agents.utils.pulses._get_redis_client", return_value=mock_redis),
    ):
        res = await call_agent("runner", "Run tick 1", tc)  # type: ignore[arg-type]
        await asyncio.sleep(0.12)

    assert res["status"] == "success"
    channels = [ch for ch, _ in mock_redis._published]
    assert channels == ["u:alice:gateway:broadcast", "u:alice:gateway:broadcast"]
    assert "gateway:broadcast" not in channels
    assert "u:bob:gateway:broadcast" not in channels


# ---------------------------------------------------------------------------
# Gap 2: RaceCollector._instances tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r11_gap2_race_collector_instances_isolated_across_tenants(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    _, list_store, mock_redis = _make_fake_redis()

    with patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis):
        c_alice = await collector_mod.RaceCollector.start(
            "shared-sim-id",
            {"runner-alice"},
            skip_pubsub=True,
            user_id="alice",
        )
        await mock_redis.rpush(
            "u:alice:collector:buffer:shared-sim-id",
            json.dumps({"payload": {"secret": "alice_data"}}),
        )

        # Bob must not see Alice's local collector or drain Alice's buffer.
        assert collector_mod.RaceCollector.is_running("shared-sim-id", user_id="alice") is True
        assert collector_mod.RaceCollector.is_running("shared-sim-id", user_id="bob") is False
        c_bob_proxy = collector_mod.RaceCollector.get("shared-sim-id", user_id="bob")
        assert c_bob_proxy is not c_alice
        assert isinstance(c_bob_proxy, collector_mod._DrainProxy)
        assert c_bob_proxy._buffer_key == "u:bob:collector:buffer:shared-sim-id"
        assert await c_bob_proxy.drain() == []

        # Bob starting his own collector with the identical session_id stays isolated.
        c_bob = await collector_mod.RaceCollector.start(
            "shared-sim-id",
            {"runner-bob"},
            skip_pubsub=True,
            user_id="bob",
        )
        assert c_bob is not c_alice
        assert collector_mod.RaceCollector.get("shared-sim-id", user_id="alice") is c_alice
        assert collector_mod.RaceCollector.get("shared-sim-id", user_id="bob") is c_bob

        # Alice's buffer is still intact and drains only Alice's data.
        alice_drained = await c_alice.drain()
        assert alice_drained == [{"payload": {"secret": "alice_data"}}]
        assert "u:alice:collector:buffer:shared-sim-id" not in list_store

        # Stopping Alice outside user_scope removes only Alice's instance.
        await c_alice.stop()
        assert collector_mod.RaceCollector.is_running("shared-sim-id", user_id="alice") is False
        assert collector_mod.RaceCollector.is_running("shared-sim-id", user_id="bob") is True
        assert collector_mod.RaceCollector.get("shared-sim-id", user_id="bob") is c_bob

        await c_bob.stop()
        assert collector_mod.RaceCollector.is_running("shared-sim-id", user_id="bob") is False


# ---------------------------------------------------------------------------
# Gap 3: RedisOrchestratorDispatcher multi-tenant isolation
# ---------------------------------------------------------------------------


def _make_dispatcher(agent_type: str = "runner_autopilot") -> RedisOrchestratorDispatcher:
    agent = LlmAgent(name=f"{agent_type}_agent", instruction="Hi")
    runner = InMemoryRunner(app=App(name=agent_type, root_agent=agent))
    d = RedisOrchestratorDispatcher(runner=runner)
    d._loop = asyncio.get_running_loop()
    return d


@pytest.mark.asyncio
async def test_r11_gap3_dispatcher_shared_sim_id_subscriptions_broadcast_end_and_reset(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    kv_store, _, mock_redis = _make_fake_redis()
    dispatcher = _make_dispatcher("runner_autopilot")
    mock_pubsub = AsyncMock()
    dispatcher._pubsub = mock_pubsub
    triggered: list[str] = []
    dispatcher._trigger_agent_run = lambda sid, content: triggered.append(sid)

    try:
        with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
            # 1. Spawn Alice and Bob with identical simulation_id="sim-shared"
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-spawn-a",
                    "sessionId": "alice-sess-1",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "alice",
                    },
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-spawn-b",
                    "sessionId": "bob-sess-1",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "bob",
                    },
                }
            )

            sub_channels = [call.args[0] for call in mock_pubsub.subscribe.await_args_list]
            assert sub_channels == [
                "u:alice:simulation:sim-shared:broadcast",
                "u:bob:simulation:sim-shared:broadcast",
            ]
            assert dispatcher._unpack_sim_scope_key("u:alice:sim-shared") == (
                "sim-shared",
                "alice",
            )
            assert dispatcher._unpack_sim_scope_key("u:bob:sim-shared") == ("sim-shared", "bob")

            # 2. Scoped broadcast for Alice must ONLY trigger alice-sess-1
            await dispatcher.handle_event(
                {
                    "type": "broadcast",
                    "eventId": "evt-bc-a",
                    "simulation_id": "sim-shared",
                    "payload": {"data": "TICK", "user_id": "alice"},
                }
            )
            assert triggered == ["alice-sess-1"]
            triggered.clear()

            # 3. Cross-tenant a2ui_action from Alice targeting Bob's session must be rejected
            await dispatcher.handle_event(
                {
                    "type": "a2ui_action",
                    "eventId": "evt-a2ui-spoof",
                    "sessionId": "bob-sess-1",
                    "payload": {"actionName": "boost", "user_id": "alice"},
                }
            )
            assert triggered == []

            # 4. Alice environment_reset must leave Bob's session, simreg, and subscription intact
            await dispatcher.handle_event(
                {
                    "type": "environment_reset",
                    "eventId": "evt-reset-a",
                    "payload": {"user_id": "alice"},
                }
            )
            assert "alice-sess-1" not in dispatcher.active_sessions
            assert "bob-sess-1" in dispatcher.active_sessions
            assert dispatcher.session_user_map.get("bob-sess-1") == "bob"
            assert "u:alice:simreg:session:alice-sess-1" not in kv_store
            assert kv_store.get("u:bob:simreg:session:bob-sess-1") == b"sim-shared"
            unsub_channels = [call.args[0] for call in mock_pubsub.unsubscribe.await_args_list]
            assert unsub_channels == ["u:alice:simulation:sim-shared:broadcast"]
            assert "u:bob:sim-shared" in dispatcher._simulation_subscriptions

            # Bob still receives his broadcasts cleanly
            await dispatcher.handle_event(
                {
                    "type": "broadcast",
                    "eventId": "evt-bc-b",
                    "simulation_id": "sim-shared",
                    "payload": {"data": "TICK", "user_id": "bob"},
                }
            )
            assert triggered == ["bob-sess-1"]
            triggered.clear()

            # 5. Re-spawn Alice on sim-shared, then end_simulation for Alice only
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-spawn-a2",
                    "sessionId": "alice-sess-2",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "alice",
                    },
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "end_simulation",
                    "eventId": "evt-end-a",
                    "simulation_id": "sim-shared",
                    "payload": {"user_id": "alice"},
                }
            )
            if dispatcher._background_tasks:
                await asyncio.gather(*list(dispatcher._background_tasks))

            assert "alice-sess-2" not in dispatcher.active_sessions
            assert "bob-sess-1" in dispatcher.active_sessions
            assert "u:bob:sim-shared" in dispatcher._simulation_subscriptions
    finally:
        dispatcher._loop = None


@pytest.mark.asyncio
async def test_r11_gap3_bound_dispatcher_rejects_cross_tenant_control_events(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.setenv("DISPATCHER_USER_ID", "bob")

    _, _, mock_redis = _make_fake_redis()
    dispatcher = _make_dispatcher("runner_autopilot")
    dispatcher._pubsub = AsyncMock()
    triggered: list[str] = []
    dispatcher._trigger_agent_run = lambda sid, content: triggered.append(sid)

    try:
        with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-bob-spawn",
                    "sessionId": "bob-sess-bound",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-bob",
                        "user_id": "bob",
                    },
                }
            )

            # Cross-tenant events from alice must all be rejected by bob's bound dispatcher.
            await dispatcher.handle_event(
                {
                    "type": "broadcast",
                    "eventId": "evt-alice-bc",
                    "simulation_id": "sim-bob",
                    "payload": {"data": "TICK", "user_id": "alice"},
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "a2ui_action",
                    "eventId": "evt-alice-a2ui",
                    "sessionId": "bob-sess-bound",
                    "payload": {"actionName": "hack", "user_id": "alice"},
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "end_simulation",
                    "eventId": "evt-alice-end",
                    "simulation_id": "sim-bob",
                    "payload": {"user_id": "alice"},
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "environment_reset",
                    "eventId": "evt-alice-reset",
                    "payload": {"user_id": "alice"},
                }
            )

            assert triggered == []
            assert "bob-sess-bound" in dispatcher.active_sessions
            assert "u:bob:sim-bob" in dispatcher._simulation_subscriptions
    finally:
        dispatcher._loop = None


# ---------------------------------------------------------------------------
# Gap 4: simulation_registry.clear() with user_scope preserves other tenants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r11_gap4_simulation_registry_user_scoped_clear_preserves_other_tenants(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    kv_store, _, mock_redis = _make_fake_redis()
    loop = asyncio.get_running_loop()
    alice_fut: asyncio.Future[str | None] = loop.create_future()
    bob_fut: asyncio.Future[str | None] = loop.create_future()

    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
        await simulation_registry.register("s-alice", "sim-alice", user_id="alice")
        await simulation_registry.register_context("v-alice", "ctx-alice", user_id="alice")
        await simulation_registry.register("s-bob", "sim-bob", user_id="bob")
        await simulation_registry.register_context("v-bob", "ctx-bob", user_id="bob")

        simulation_registry._inflight_session[(id(loop), "u:alice:s-inflight")] = alice_fut
        simulation_registry._inflight_session[(id(loop), "u:bob:s-inflight")] = bob_fut

        with simulation_registry.user_scope("alice"):
            await simulation_registry.clear()

        assert "u:alice:s-alice" not in simulation_registry._local
        assert "u:alice:v-alice" not in simulation_registry._context_map
        assert "u:alice:simreg:session:s-alice" not in kv_store
        assert alice_fut.cancelled() is True

        assert simulation_registry._local.get("u:bob:s-bob") == "sim-bob"
        assert simulation_registry._context_map.get("u:bob:v-bob") == "ctx-bob"
        assert kv_store.get("u:bob:simreg:session:s-bob") == b"sim-bob"
        assert bob_fut.cancelled() is False
        assert await simulation_registry.lookup("s-bob", user_id="bob") == "sim-bob"
        assert await simulation_registry.get_context_id("v-bob", user_id="bob") == "ctx-bob"


# ---------------------------------------------------------------------------
# Gap 5: gateway_delay_seconds clamping in plugins, running, leadership_tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r11_gap5_plugins_clamps_extreme_gateway_delay_seconds():
    plugin = plugins_mod.BaseDashLogPlugin(name="test_dash")
    emitted: list[dict] = []

    async def _capture_emit(**kwargs):
        emitted.append(kwargs)

    with (
        patch("agents.utils.pulses.emit_gateway_message", side_effect=_capture_emit),
        patch("agents.utils.plugins.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        t0 = time.monotonic()
        await asyncio.wait_for(
            plugin._emit_narrative(
                session_id="sess-g5",
                payload={
                    "type": "tool_end",
                    "agent": "runner",
                    "tool": "process_tick",
                    "result": {"status": "ok", "gateway_delay_seconds": 1e300},
                },
                simulation_id="sim-g5",
            ),
            timeout=0.1,
        )
        elapsed = time.monotonic() - t0

    assert elapsed < 0.1
    mock_sleep.assert_awaited_once()
    slept_seconds = mock_sleep.await_args.args[0]
    assert slept_seconds <= 30.0
    assert slept_seconds == plugins_mod._MAX_GATEWAY_DELAY_SECONDS == 30.0
    assert len(emitted) == 1
    assert emitted[0]["data"]["result"] == {"status": "ok"}


@pytest.mark.asyncio
async def test_r11_gap5_runner_and_leadership_tick_cap_wave_gateway_delay_seconds():
    lead_ctx = SimpleNamespace(
        state={
            "persona": "pragmatist",
            "velocity": 1.0,
            "distance": 0.0,
            "water": 100.0,
            "wave_number": 10**299,
        },
        session=SimpleNamespace(id="lead-huge-wave", user_id="u1"),
        invocation_id="inv-lead-g5",
        agent_name="leadership_runner",
    )
    l_res = await leadership_tick.process_leadership_tick(
        lead_ctx,  # type: ignore[arg-type]
        inner_thought="Huge wave",
        minutes_per_tick=5.0,
        elapsed_minutes=5.0,
        race_distance_mi=26.2188,
        tick=1,
    )
    assert l_res["gateway_delay_seconds"] == pytest.approx(30.0)
    json.dumps(l_res, allow_nan=False)

    run_ctx = SimpleNamespace(
        state={
            "velocity": 1.0,
            "distance": 0.0,
            "water": 100.0,
            "wave_number": 10**299,
        },
        session=SimpleNamespace(id="run-huge-wave", user_id="u1"),
        invocation_id="inv-run-g5",
        agent_name="runner",
    )
    r_res = await running.process_tick(
        run_ctx,  # type: ignore[arg-type]
        inner_thought="Huge wave",
        minutes_per_tick=5.0,
        elapsed_minutes=5.0,
        race_distance_mi=26.2188,
        tick=1,
    )
    assert r_res["gateway_delay_seconds"] == pytest.approx(30.0)
    json.dumps(r_res, allow_nan=False)


# ---------------------------------------------------------------------------
# Iteration 11 Review Remediation: Fail-Closed Multi-Tenant Attribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r11_rev2_f1_simulation_registry_clear_without_valid_user_preserves_l1_and_futures(
    monkeypatch,
):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    kv_store, _, mock_redis = _make_fake_redis()
    loop = asyncio.get_running_loop()
    bob_sess_fut: asyncio.Future[str | None] = loop.create_future()
    bob_ctx_fut: asyncio.Future[str | None] = loop.create_future()

    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
        await simulation_registry.register("s-bob", "sim-bob", user_id="bob")
        await simulation_registry.register_context("v-bob", "ctx-bob", user_id="bob")
        simulation_registry._inflight_session[(id(loop), "u:bob:s-inflight")] = bob_sess_fut
        simulation_registry._inflight_context[(id(loop), "u:bob:v-inflight")] = bob_ctx_fut

        for bad_uid in (None, "", "simulation", "simulation_user", "anonymous", "default", "user"):
            await simulation_registry.clear(user_id=bad_uid)
            assert simulation_registry._local.get("u:bob:s-bob") == "sim-bob"
            assert simulation_registry._context_map.get("u:bob:v-bob") == "ctx-bob"
            assert kv_store.get("u:bob:simreg:session:s-bob") == b"sim-bob"
            assert kv_store.get("u:bob:simreg:context:v-bob") == b"ctx-bob"
            assert bob_sess_fut.cancelled() is False
            assert bob_ctx_fut.cancelled() is False
            assert (id(loop), "u:bob:s-inflight") in simulation_registry._inflight_session
            assert (id(loop), "u:bob:v-inflight") in simulation_registry._inflight_context


@pytest.mark.asyncio
async def test_r11_rev2_f2_environment_reset_fail_closed_and_bound_gateway_reset(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    kv_store, _, mock_redis = _make_fake_redis()
    dispatcher = _make_dispatcher("runner_autopilot")
    mock_pubsub = AsyncMock()
    dispatcher._pubsub = mock_pubsub

    try:
        with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-f2-spawn-a",
                    "sessionId": "alice-s1",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "alice",
                    },
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-f2-spawn-b",
                    "sessionId": "bob-s1",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "bob",
                    },
                }
            )
            await simulation_registry.register_context("v-bob", "ctx-bob", user_id="bob")

            invalid_resets = [
                {"type": "environment_reset", "eventId": "evt-rst-missing"},
                {"type": "environment_reset", "eventId": "evt-rst-sim", "payload": {"user_id": "simulation"}},
                {
                    "type": "environment_reset",
                    "eventId": "evt-rst-sim-user",
                    "payload": {"user_id": "simulation_user"},
                },
                {"type": "environment_reset", "eventId": "evt-rst-anon", "payload": {"user_id": "anonymous"}},
                {"type": "environment_reset", "eventId": "evt-rst-def", "payload": {"user_id": "default"}},
                {"type": "environment_reset", "eventId": "evt-rst-user", "payload": {"user_id": "user"}},
                {"type": "environment_reset", "eventId": "evt-rst-bad", "payload": {"user_id": "bad:uid"}},
                {
                    "type": "environment_reset",
                    "eventId": "evt-rst-conflict-top",
                    "user_id": "bob",
                    "payload": {"user_id": "alice"},
                },
                {
                    "type": "environment_reset",
                    "eventId": "evt-rst-conflict-inner",
                    "payload": {"user_id": "alice", "acting_user_id": "bob"},
                },
            ]
            for evt in invalid_resets:
                await dispatcher.handle_event(evt)

            assert "alice-s1" in dispatcher.active_sessions
            assert "bob-s1" in dispatcher.active_sessions
            assert dispatcher.session_user_map.get("alice-s1") == "alice"
            assert dispatcher.session_user_map.get("bob-s1") == "bob"
            assert "u:alice:sim-shared" in dispatcher._simulation_subscriptions
            assert "u:bob:sim-shared" in dispatcher._simulation_subscriptions
            mock_pubsub.unsubscribe.assert_not_awaited()
            assert simulation_registry._local.get("u:alice:alice-s1") == "sim-shared"
            assert simulation_registry._local.get("u:bob:bob-s1") == "sim-shared"
            assert simulation_registry._context_map.get("u:bob:v-bob") == "ctx-bob"
            assert kv_store.get("u:alice:simreg:session:alice-s1") == b"sim-shared"
            assert kv_store.get("u:bob:simreg:session:bob-s1") == b"sim-shared"

            # Bound dispatcher still processes Go Gateway environment_reset without payload user_id.
            monkeypatch.setenv("DISPATCHER_USER_ID", "bob")
            await dispatcher.handle_event({"type": "environment_reset", "eventId": "evt-gw-bound-reset"})
            assert "bob-s1" not in dispatcher.active_sessions
            assert "alice-s1" in dispatcher.active_sessions
            assert "u:bob:sim-shared" not in dispatcher._simulation_subscriptions
            assert "u:bob:simreg:session:bob-s1" not in kv_store
            assert kv_store.get("u:alice:simreg:session:alice-s1") == b"sim-shared"
    finally:
        dispatcher._loop = None


@pytest.mark.asyncio
async def test_r11_rev2_f3_pubsub_channel_tenant_attribution_and_unprefixed_rejection(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "off")
    assert tenant_keys.user_from_simulation_channel("u:alice:simulation:sim-1:broadcast") == ("", None)

    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    assert tenant_keys.user_from_simulation_channel("u:alice:simulation:broadcast") == ("alice", None)
    assert tenant_keys.user_from_simulation_channel(b"u:alice:simulation:sim-1:broadcast") == ("alice", "sim-1")
    assert tenant_keys.user_from_simulation_channel("simulation:broadcast") == ("", None)
    assert tenant_keys.user_from_simulation_channel("simulation:sim-1:broadcast") == ("", None)
    assert tenant_keys.user_from_simulation_channel("u::simulation:sim-1:broadcast") == ("", None)
    assert tenant_keys.user_from_simulation_channel("u:alice :simulation:sim-1:broadcast") == ("", None)

    _, _, mock_redis = _make_fake_redis()
    dispatcher = _make_dispatcher("runner_autopilot")
    dispatcher._pubsub = AsyncMock()
    triggered: list[str] = []
    dispatcher._trigger_agent_run = lambda sid, content: triggered.append(sid)

    try:
        with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-f3-spawn-a",
                    "sessionId": "a1",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "alice",
                    },
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-f3-spawn-b",
                    "sessionId": "b1",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "bob",
                    },
                }
            )

            # 1. Scoped Pub/Sub message WITHOUT user_id in JSON attributes solely from channel.
            await dispatcher._handle_message(
                mock_redis,
                json.dumps({"type": "broadcast", "eventId": "evt-ch-bc-a", "payload": {"data": "TICK"}}).encode(),
                channel=b"u:alice:simulation:sim-shared:broadcast",
            )
            assert triggered == ["a1"]
            triggered.clear()

            # 1b. Verify _pubsub_listener itself threads message["channel"] into _handle_message.
            listener_msgs = [
                {
                    "type": "message",
                    "channel": b"u:alice:simulation:sim-shared:broadcast",
                    "data": json.dumps(
                        {"type": "broadcast", "eventId": "evt-listener-ch", "payload": {"data": "TICK"}}
                    ).encode(),
                }
            ]

            class _ListenerPubSub:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

                async def subscribe(self, *channels: str) -> None:
                    return None

                async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 1.0):
                    if listener_msgs:
                        return listener_msgs.pop(0)
                    dispatcher._stop_event.set()
                    return None

            fake_listener_redis = MagicMock()
            fake_listener_redis.pubsub = MagicMock(return_value=_ListenerPubSub())
            dispatcher._stop_event.clear()
            await dispatcher._pubsub_listener(fake_listener_redis)
            dispatcher._stop_event.clear()
            dispatcher._pubsub = AsyncMock()
            assert triggered == ["a1"]
            triggered.clear()

            # 2. Unprefixed Pub/Sub channels in SAAS_MODE=on must be rejected (even with payload user_id).
            for unprefixed in ("simulation:broadcast", b"simulation:sim-shared:broadcast"):
                await dispatcher._handle_message(
                    mock_redis,
                    json.dumps(
                        {
                            "type": "broadcast",
                            "eventId": f"evt-unpref-{unprefixed}",
                            "simulation_id": "sim-shared",
                            "payload": {"data": "TICK", "user_id": "alice"},
                        }
                    ).encode(),
                    channel=unprefixed,
                )
            assert triggered == []

            # 3. Channel vs payload user_id mismatch or simulation_id mismatch must be rejected.
            await dispatcher._handle_message(
                mock_redis,
                json.dumps(
                    {
                        "type": "broadcast",
                        "eventId": "evt-ch-user-mismatch",
                        "payload": {"data": "TICK", "user_id": "bob"},
                    }
                ).encode(),
                channel="u:alice:simulation:sim-shared:broadcast",
            )
            await dispatcher._handle_message(
                mock_redis,
                json.dumps(
                    {
                        "type": "broadcast",
                        "eventId": "evt-ch-sim-mismatch",
                        "simulation_id": "sim-other",
                        "payload": {"data": "TICK"},
                    }
                ).encode(),
                channel="u:alice:simulation:sim-shared:broadcast",
            )
            assert triggered == []

            # 4. Scoped end_simulation via channel without payload user_id removes ONLY Alice's session.
            await dispatcher._handle_message(
                mock_redis,
                json.dumps({"type": "end_simulation", "eventId": "evt-ch-end-a"}).encode(),
                channel=b"u:alice:simulation:sim-shared:broadcast",
            )
            if dispatcher._background_tasks:
                await asyncio.gather(*list(dispatcher._background_tasks))
            assert "a1" not in dispatcher.active_sessions
            assert "b1" in dispatcher.active_sessions
            assert "u:bob:sim-shared" in dispatcher._simulation_subscriptions
    finally:
        dispatcher._loop = None


@pytest.mark.asyncio
async def test_r11_rev2_f3_unbound_handle_event_fail_closed_and_scoped_relay(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    _, _, mock_redis = _make_fake_redis()
    dispatcher = _make_dispatcher("runner_autopilot")
    dispatcher._pubsub = AsyncMock()
    triggered: list[str] = []
    dispatcher._trigger_agent_run = lambda sid, content: triggered.append(sid)

    try:
        with (
            patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
            patch("agents.utils.dispatcher.get_shared_redis_client", return_value=mock_redis),
        ):
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-f3u-spawn-a",
                    "sessionId": "a1",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "alice",
                    },
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-f3u-spawn-b",
                    "sessionId": "b1",
                    "payload": {
                        "agentType": "runner_autopilot",
                        "simulation_id": "sim-shared",
                        "user_id": "bob",
                    },
                }
            )

            # Unbound HTTP events without user_id or with anonymous/invalid/conflicting user_id fail closed.
            bad_events = [
                {"type": "broadcast", "eventId": "evt-bc-nouser", "simulation_id": "sim-shared", "payload": {"data": "X"}},
                {
                    "type": "broadcast",
                    "eventId": "evt-bc-simuser",
                    "simulation_id": "sim-shared",
                    "payload": {"data": "X", "user_id": "simulation"},
                },
                {
                    "type": "broadcast",
                    "eventId": "evt-bc-baduid",
                    "simulation_id": "sim-shared",
                    "payload": {"data": "X", "user_id": "bad:uid"},
                },
                {
                    "type": "end_simulation",
                    "eventId": "evt-end-nouser",
                    "simulation_id": "sim-shared",
                    "payload": {},
                },
                {
                    "type": "end_simulation",
                    "eventId": "evt-end-simuser",
                    "simulation_id": "sim-shared",
                    "payload": {"user_id": "simulation"},
                },
                {
                    "type": "a2ui_action",
                    "eventId": "evt-a2ui-nouser",
                    "sessionId": "b1",
                    "payload": {"actionName": "boost"},
                },
                {
                    "type": "a2ui_action",
                    "eventId": "evt-a2ui-simuser",
                    "sessionId": "b1",
                    "payload": {"actionName": "boost", "user_id": "simulation"},
                },
            ]
            for evt in bad_events:
                await dispatcher.handle_event(evt)

            assert triggered == []
            assert "a1" in dispatcher.active_sessions
            assert "b1" in dispatcher.active_sessions
            mock_redis.publish.assert_not_awaited()

            # Scoped relay WITHOUT simulation_id must publish to u:alice:simulation:broadcast, not global!
            await dispatcher.handle_event(
                {
                    "type": "broadcast",
                    "eventId": "evt-bc-relay-nosim",
                    "payload": {
                        "data": "TICK",
                        "targets": ["a1", "a-remote"],
                        "user_id": "alice",
                    },
                }
            )
            assert triggered == ["a1"]
            mock_redis.publish.assert_awaited_once()
            relay_ch, relay_raw = mock_redis.publish.await_args.args
            assert relay_ch == "u:alice:simulation:broadcast"
            relay_parsed = json.loads(relay_raw)
            assert relay_parsed["payload"]["user_id"] == "alice"
            assert relay_parsed["payload"]["targets"] == ["a-remote"]
    finally:
        dispatcher._loop = None


@pytest.mark.asyncio
async def test_r11_rev2_f3_broadcast_py_includes_user_id_in_saas_on_only(monkeypatch):
    _, _, mock_redis = _make_fake_redis()

    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    with (
        patch("agents.simulator.broadcast.get_shared_redis_client", return_value=mock_redis),
        simulation_registry.user_scope("alice"),
    ):
        await sim_broadcast.publish_to_runners('{"event":"tick"}', "sim-1")
        await sim_broadcast.publish_end_simulation("sim-1")

    assert len(mock_redis._published) == 2
    ch1, raw1 = mock_redis._published[0]
    ch2, raw2 = mock_redis._published[1]
    assert ch1 == "u:alice:simulation:sim-1:broadcast"
    assert ch2 == "u:alice:simulation:sim-1:broadcast"
    assert json.loads(raw1)["user_id"] == "alice"
    assert json.loads(raw2)["user_id"] == "alice"

    mock_redis._published.clear()
    monkeypatch.setenv("SAAS_MODE", "off")
    with (
        patch("agents.simulator.broadcast.get_shared_redis_client", return_value=mock_redis),
        simulation_registry.user_scope("alice"),
    ):
        await sim_broadcast.publish_to_runners('{"event":"tick"}', "sim-1")
        await sim_broadcast.publish_end_simulation("sim-1")

    ch3, raw3 = mock_redis._published[0]
    ch4, raw4 = mock_redis._published[1]
    assert ch3 == "simulation:sim-1:broadcast"
    assert ch4 == "simulation:sim-1:broadcast"
    assert "user_id" not in json.loads(raw3)
    assert "user_id" not in json.loads(raw4)
