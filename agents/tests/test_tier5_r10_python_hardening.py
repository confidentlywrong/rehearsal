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

"""Tier 5 Iteration 10 regression tests for Python cross-layer auth,
tenant-scoped Redis channels/keys, strict RFC-8259 JSON serialization,
RedisDashLogPlugin tenant resolution, and non-fatal memory summary storage.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.types import Message as A2AMessage
from a2a.types import MessageSendParams, Part, Role, TextPart
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.runners import InMemoryRunner

from agents.planner_with_memory.memory import tools as planner_memory_tools
from agents.simulator import agent as simulator_agent
from agents.simulator import broadcast as sim_broadcast
from agents.simulator.collector import RaceCollector, _DrainProxy
from agents.utils import pulses as pulses_mod
from agents.utils import simulation_registry, user_auth_context
from agents.utils.communication import SimulationA2AClient, call_agent
from agents.utils.dispatcher import RedisOrchestratorDispatcher
from agents.utils.plugins import RedisDashLogPlugin
from agents.utils.session_manager import SessionManager
from agents.utils.simulation_executor import SimulationExecutor
from gen_proto.gateway import gateway_pb2

_PRE_RACE_TOOLS_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "simulator" / "skills" / "preparing-the-race" / "tools.py"
)
_pre_race_spec = importlib.util.spec_from_file_location("tier5_r10_pre_race_tools", _PRE_RACE_TOOLS_PATH)
assert _pre_race_spec is not None and _pre_race_spec.loader is not None
_pre_race_tools = importlib.util.module_from_spec(_pre_race_spec)
_pre_race_spec.loader.exec_module(_pre_race_tools)
spawn_runners = _pre_race_tools.spawn_runners


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for var in (
        "SAAS_MODE",
        "DISPATCHER_USER_ID",
        "ACCOUNT_KEY_PREFIX",
        "DISPATCH_MODE",
        "GATEWAY_URL",
        "GATEWAY_INTERNAL_URL",
        "ALLOYDB_HOST",
        "ALLOYDB_SCHEMA",
        "EMBEDDING_BACKEND",
    ):
        monkeypatch.delenv(var, raising=False)
    pulses_mod.reset()
    user_auth_context._reset_for_tests()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()
    yield
    pulses_mod.reset()
    user_auth_context._reset_for_tests()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()


def _make_fake_redis() -> tuple[dict[str, bytes], AsyncMock]:
    store: dict[str, bytes] = {}
    mock_redis = AsyncMock()

    async def _get(key: str):
        return store.get(key)

    async def _set(key: str, value: str | bytes, ex: int | None = None, nx: bool = False):
        if nx and key in store:
            return False
        store[key] = value if isinstance(value, bytes) else str(value).encode()
        return True

    async def _setex(key: str, ttl: int, value: str | bytes):
        store[key] = value if isinstance(value, bytes) else str(value).encode()
        return True

    async def _delete(*keys: str):
        deleted = 0
        for k in keys:
            key_str = k.decode() if isinstance(k, bytes) else k
            if key_str in store:
                del store[key_str]
                deleted += 1
        return deleted

    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.setex = AsyncMock(side_effect=_setex)
    mock_redis.delete = AsyncMock(side_effect=_delete)
    mock_redis.publish = AsyncMock(return_value=1)
    return store, mock_redis


def _make_aiohttp_spawn_mock(status: int, body: object) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status = status
    if status == 200:
        mock_resp.json = AsyncMock(return_value=body)
    else:
        mock_resp.text = AsyncMock(return_value=str(body))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _make_exec_context(query: str, *, context_id: str = "ctx-1", metadata: dict | None = None) -> RequestContext:
    msg = A2AMessage(
        message_id="msg-1",
        parts=[Part(root=TextPart(text=query))],
        role=Role.user,
        context_id=context_id,
        metadata=metadata,
    )
    return RequestContext(
        request=MessageSendParams(message=msg),
        context_id=context_id,
        task_id="task-1",
    )


# ---------------------------------------------------------------------------
# Gap 1: End-to-end user_auth_token forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap1_call_agent_forwards_user_auth_token_without_logging_secret(caplog):
    captured: list[A2AMessage] = []

    async def _send_message(msg: A2AMessage):
        captured.append(msg)
        yield A2AMessage(
            message_id="resp-1",
            parts=[Part(root=TextPart(text="ok"))],
            role=Role.agent,
        )

    mock_a2a = MagicMock()
    mock_a2a.send_message = _send_message
    mock_remote = MagicMock()
    mock_remote._ensure_resolved = AsyncMock()
    mock_remote._a2a_client = mock_a2a

    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock(return_value=mock_remote)

    secret_token = "firebase-jwt-secret-alice-999"
    user_auth_context.set_session_token("planner-sess-1", secret_token)

    ctx = SimpleNamespace(
        invocation_id="iid-1",
        agent_name="planner",
        session=SimpleNamespace(id="planner-sess-1", user_id="user-alice"),
        state={"user_id": "user-alice", "simulation_id": "sim-1"},
    )

    with (
        caplog.at_level(logging.DEBUG, logger="agents.utils.communication"),
        patch("agents.utils.communication_plugin.get_client", return_value=client),
        patch("agents.utils.simulation_registry.get_context_id", new=AsyncMock(return_value=None)),
        patch("agents.utils.communication.emit_inter_agent_pulse", new=AsyncMock()),
    ):
        res = await call_agent("simulator", '{"action":"execute"}', ctx, session_id="sim-ctx-1")

    assert res["status"] == "success"
    assert len(captured) == 1
    assert captured[0].metadata == {
        "user_id": "user-alice",
        "user_auth_token": secret_token,
    }
    assert secret_token not in caplog.text


@pytest.mark.asyncio
async def test_gap1_executor_binds_and_cleans_session_token_across_run_and_error(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCH_MODE", "callable")

    observed_tokens: list[tuple[str | None, str | None]] = []

    final_event = MagicMock()
    final_event.is_final_response.return_value = True
    final_event.author = "agent"
    final_event.content.parts = [MagicMock(text="Done")]

    async def _run_async(*, session_id, user_id, new_message):
        observed_tokens.append(
            (
                user_auth_context.get_session_token(session_id),
                user_auth_context.get_session_token("ctx-exec-1"),
            )
        )
        yield final_event

    mock_runner = MagicMock()
    mock_runner.app_name = "simulator"
    mock_runner.app.plugins = []
    mock_runner.run_async = _run_async

    mock_session_service = AsyncMock()
    mock_session_service.create_session = AsyncMock(return_value=SimpleNamespace(id="vertex-exec-1"))

    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator", runner=mock_runner)
    executor._session_manager = SessionManager(session_service=mock_session_service)

    ctx = _make_exec_context(
        '{"action":"execute","simulation_id":"sim-1"}',
        context_id="ctx-exec-1",
        metadata={"user_id": "user-alice", "user_auth_token": "tok-exec-alice"},
    )
    eq = AsyncMock()

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
        patch("agents.utils.session_manager.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_executor.simulation_registry.register", new=AsyncMock()),
        patch("agents.utils.simulation_executor.simulation_registry.lookup", new=AsyncMock(return_value=None)),
        patch("agents.utils.simulation_executor.simulation_registry.register_context", new=AsyncMock()),
        patch("agents.utils.simulation_executor.pulses.emit_gateway_message", new=AsyncMock()),
    ):
        await executor.execute(ctx, eq)

    assert observed_tokens == [("tok-exec-alice", "tok-exec-alice")]
    assert user_auth_context.get_session_token("vertex-exec-1") is None
    assert user_auth_context.get_session_token("ctx-exec-1") is None

    # Error path must also clean up bound tokens via finally
    async def _failing_run_async(*, session_id, user_id, new_message):
        assert user_auth_context.get_session_token(session_id) == "tok-exec-err"
        raise RuntimeError("boom")
        yield  # pragma: no cover

    mock_runner.run_async = _failing_run_async
    ctx_err = _make_exec_context(
        '{"action":"execute","simulation_id":"sim-2"}',
        context_id="ctx-exec-err",
        metadata={"user_id": "user-alice", "user_auth_token": "tok-exec-err"},
    )
    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
        patch("agents.utils.session_manager.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_executor.simulation_registry.register", new=AsyncMock()),
        patch("agents.utils.simulation_executor.simulation_registry.lookup", new=AsyncMock(return_value=None)),
        patch("agents.utils.simulation_executor.simulation_registry.register_context", new=AsyncMock()),
    ):
        await executor.execute(ctx_err, AsyncMock())

    assert user_auth_context.get_session_token("vertex-exec-1") is None
    assert user_auth_context.get_session_token("ctx-exec-err") is None


@pytest.mark.asyncio
async def test_gap1_spawn_runners_headers_internal_http_and_cloud_run_oidc(monkeypatch):
    user_auth_context.set_session_token("sim-sess-http", "firebase-tok-alice")
    ctx_http = MagicMock()
    ctx_http.session = SimpleNamespace(id="sim-sess-http")
    ctx_http.state = {"simulation_ready": True, "simulation_id": "sim-1"}

    # 1. Internal HTTP URL (no OIDC audience): Authorization + Cookie carry user token
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:9101")
    mock_http = _make_aiohttp_spawn_mock(200, {"sessions": [{"sessionId": "r-1", "agentType": "runner"}]})
    with (
        patch("aiohttp.ClientSession", return_value=mock_http),
        patch("agents.utils.auth.get_id_token", return_value=None),
    ):
        res_http = await spawn_runners(count=1, tool_context=ctx_http)

    assert res_http["status"] == "success"
    http_headers = mock_http.post.call_args[1]["headers"]
    assert http_headers == {
        "Authorization": "Bearer firebase-tok-alice",
        "Cookie": "__session=firebase-tok-alice",
    }

    # 2. HTTPS Cloud Run URL (OIDC audience present): Authorization=OIDC, X-User-Authorization + Cookie=user
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "https://gateway-xyz.run.app")
    mock_https = _make_aiohttp_spawn_mock(200, {"sessions": [{"sessionId": "r-2", "agentType": "runner"}]})
    with (
        patch("aiohttp.ClientSession", return_value=mock_https),
        patch("agents.utils.auth.get_id_token", return_value="google-oidc-jwt"),
    ):
        res_https = await spawn_runners(count=1, tool_context=ctx_http)

    assert res_https["status"] == "success"
    https_headers = mock_https.post.call_args[1]["headers"]
    assert https_headers == {
        "Authorization": "Bearer google-oidc-jwt",
        "X-User-Authorization": "Bearer firebase-tok-alice",
        "Cookie": "__session=firebase-tok-alice",
    }


@pytest.mark.asyncio
async def test_gap1_e2e_planner_to_executor_to_spawn_runners_mocked_gateway(monkeypatch):
    """End-to-end: planner session token -> call_agent -> SimulationExecutor -> spawn_runners -> gateway."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCH_MODE", "callable")
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:9101")

    user_auth_context.set_session_token("planner-sess-e2e", "valid-firebase-alice")

    # Mocked gateway enforcing user token on /api/v1/spawn
    def _gateway_post(url, json=None, headers=None):
        auth_hdr = (headers or {}).get("Authorization", "")
        cookie_hdr = (headers or {}).get("Cookie", "")
        if auth_hdr == "Bearer valid-firebase-alice" and "__session=valid-firebase-alice" in cookie_hdr:
            return _make_aiohttp_spawn_mock(
                200, {"sessions": [{"sessionId": "r-e2e-1", "agentType": "leadership_runner_autopilot"}]}
            ).post(url, json=json, headers=headers)
        return _make_aiohttp_spawn_mock(401, '{"error":"authentication_required"}').post(
            url, json=json, headers=headers
        )

    mock_aiohttp_session = MagicMock()
    mock_aiohttp_session.post = MagicMock(side_effect=_gateway_post)
    mock_aiohttp_session.__aenter__ = AsyncMock(return_value=mock_aiohttp_session)
    mock_aiohttp_session.__aexit__ = AsyncMock(return_value=False)

    spawn_results: list[dict] = []

    async def _sim_run_async(*, session_id, user_id, new_message):
        sim_tool_ctx = MagicMock()
        sim_tool_ctx.session = SimpleNamespace(id=session_id)
        sim_tool_ctx.state = {"simulation_ready": True, "simulation_id": "sim-e2e-auth"}
        with (
            patch("aiohttp.ClientSession", return_value=mock_aiohttp_session),
            patch("agents.utils.auth.get_id_token", return_value=None),
        ):
            spawn_res = await spawn_runners(count=1, tool_context=sim_tool_ctx)
        spawn_results.append(spawn_res)
        ev = MagicMock()
        ev.is_final_response.return_value = True
        ev.author = "agent"
        ev.content.parts = [MagicMock(text=json.dumps(spawn_res))]
        yield ev

    mock_runner = MagicMock()
    mock_runner.app_name = "simulator"
    mock_runner.app.plugins = []
    mock_runner.run_async = _sim_run_async

    mock_session_service = AsyncMock()
    mock_session_service.create_session = AsyncMock(return_value=SimpleNamespace(id="vertex-sim-auth"))

    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator", runner=mock_runner)
    executor._session_manager = SessionManager(session_service=mock_session_service)

    async def _bridge_send(a2a_msg: A2AMessage):
        req_ctx = RequestContext(
            request=MessageSendParams(message=a2a_msg),
            context_id=a2a_msg.context_id,
            task_id="task-e2e-auth",
        )
        eq = AsyncMock()
        with (
            patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
            patch("agents.utils.session_manager.get_shared_redis_client", return_value=None),
            patch("agents.utils.simulation_executor.simulation_registry.register", new=AsyncMock()),
            patch("agents.utils.simulation_executor.simulation_registry.lookup", new=AsyncMock(return_value=None)),
            patch("agents.utils.simulation_executor.simulation_registry.register_context", new=AsyncMock()),
            patch("agents.utils.simulation_executor.pulses.emit_gateway_message", new=AsyncMock()),
        ):
            await executor.execute(req_ctx, eq)
        yield A2AMessage(
            message_id="resp-e2e-auth",
            parts=[Part(root=TextPart(text=json.dumps(spawn_results[-1])))],
            role=Role.agent,
        )

    mock_a2a = MagicMock()
    mock_a2a.send_message = _bridge_send
    mock_remote = MagicMock()
    mock_remote._ensure_resolved = AsyncMock()
    mock_remote._a2a_client = mock_a2a

    a2a_client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    a2a_client.get_agent = AsyncMock(return_value=mock_remote)

    planner_ctx = SimpleNamespace(
        invocation_id="iid-e2e-auth",
        agent_name="leadership_planner",
        session=SimpleNamespace(id="planner-sess-e2e", user_id="user-alice"),
        state={"user_id": "user-alice", "simulation_id": "sim-e2e-auth"},
    )
    with (
        patch("agents.utils.communication_plugin.get_client", return_value=a2a_client),
        patch("agents.utils.simulation_registry.get_context_id", new=AsyncMock(return_value=None)),
        patch("agents.utils.communication.emit_inter_agent_pulse", new=AsyncMock()),
    ):
        call_res = await call_agent("simulator", '{"action":"execute"}', planner_ctx, session_id="sim-ctx-e2e")

    assert call_res["status"] == "success"
    assert len(spawn_results) == 1
    assert spawn_results[0]["status"] == "success"
    assert spawn_results[0]["session_ids"] == ["r-e2e-1"]
    assert user_auth_context.get_session_token("vertex-sim-auth") is None


# ---------------------------------------------------------------------------
# Gap 2: Honor simulation_registry.user_scope / per-session user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap2_broadcast_and_collector_honor_user_scope_without_dispatcher_env(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    redis_store, mock_redis = _make_fake_redis()
    mock_pubsub = AsyncMock()
    mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

    with simulation_registry.user_scope("user-alice"):
        with (
            patch("agents.simulator.broadcast.get_shared_redis_client", return_value=mock_redis),
            patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis),
        ):
            await sim_broadcast.publish_to_runners('{"event":"start_gun"}', simulation_id="sim-alice")
            await sim_broadcast.publish_end_simulation("sim-alice")
            collector = await RaceCollector.start(
                session_id="sim-sess-alice",
                runner_session_ids={"r-alice"},
            )
            proxy = _DrainProxy("sim-sess-alice", mock_redis)

    published_channels = [call.args[0] for call in mock_redis.publish.call_args_list]
    assert published_channels == [
        "u:user-alice:simulation:sim-alice:broadcast",
        "u:user-alice:simulation:sim-alice:broadcast",
    ]
    mock_pubsub.subscribe.assert_awaited_once_with("u:user-alice:gateway:broadcast")
    assert "u:user-alice:collector:active:sim-sess-alice" in redis_store
    assert collector._buffer_key == "u:user-alice:collector:buffer:sim-sess-alice"
    assert proxy._buffer_key == "u:user-alice:collector:buffer:sim-sess-alice"

    # Stopping outside user_scope still cleans up Alice's scoped active key
    with patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis):
        await collector.stop()
    assert "u:user-alice:collector:active:sim-sess-alice" not in redis_store

    # SAAS_MODE=off remains byte-identical
    monkeypatch.setenv("SAAS_MODE", "off")
    with simulation_registry.user_scope("user-alice"):
        assert sim_broadcast._scoped_broadcast_channel("sim-alice") == "simulation:sim-alice:broadcast"


@pytest.mark.asyncio
async def test_gap2_dispatcher_subscribes_relays_and_unsubscribes_scoped_channels(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    redis_store, mock_redis = _make_fake_redis()
    agent = LlmAgent(name="runner_agent", instruction="Hi")
    runner = InMemoryRunner(app=App(name="runner_autopilot", root_agent=agent))
    disp = RedisOrchestratorDispatcher(runner=runner)
    disp._pubsub = AsyncMock()
    disp._trigger_agent_run = MagicMock()  # type: ignore[method-assign]

    with (
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.dispatcher.get_shared_redis_client", return_value=mock_redis),
    ):
        await disp._process_event(
            {
                "type": "spawn_agent",
                "eventId": "evt-s1",
                "sessionId": "r-sess-1",
                "payload": {
                    "agentType": "runner_autopilot",
                    "simulation_id": "sim-alice",
                    "user_id": "user-alice",
                },
            }
        )
        disp._pubsub.subscribe.assert_awaited_once_with("u:user-alice:simulation:sim-alice:broadcast")
        assert redis_store.get("u:user-alice:simreg:session:r-sess-1") == b"sim-alice"

        # Broadcast relay for unmatched session targets publishes to Alice's scoped channel
        await disp._process_event(
            {
                "type": "broadcast",
                "eventId": "evt-b1",
                "simulation_id": "sim-alice",
                "payload": {
                    "data": "TICK",
                    "targets": ["r-sess-1", "r-sess-remote"],
                    "user_id": "user-alice",
                },
            }
        )
        mock_redis.publish.assert_awaited_once()
        assert mock_redis.publish.call_args[0][0] == "u:user-alice:simulation:sim-alice:broadcast"

        # Removing last session unsubscribes from Alice's scoped channel (test inline via _loop)
        disp._loop = None
        await disp._unsubscribe_scoped_channel("sim-alice")
        disp._pubsub.unsubscribe.assert_awaited_once_with("u:user-alice:simulation:sim-alice:broadcast")


# ---------------------------------------------------------------------------
# Gap 3: Strict RFC-8259 JSON for echo_config:{sid}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap3_spawn_runners_writes_strict_rfc8259_json_for_echo_config(monkeypatch):
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:9101")
    redis_store, mock_redis = _make_fake_redis()

    ctx = MagicMock()
    ctx.session = SimpleNamespace(id="sim-sess-nan")
    ctx.state = {
        "simulation_ready": True,
        "simulation_id": "sim-nan",
        "runner_type": "leadership_runner_autopilot",
        "echo_configs": [
            {
                "role": "cfo",
                "tier": 2,
                "initial_sentiment": float("nan"),
                "bounds": [float("inf"), -float("inf"), 0.75],
            }
        ],
    }

    mock_session = _make_aiohttp_spawn_mock(
        200,
        {
            "sessions": [
                {"sessionId": "r-1", "agentType": "leadership_runner_autopilot"},
                {"sessionId": "e-1", "agentType": "ai_echo_reactive"},
            ]
        },
    )

    with (
        patch("aiohttp.ClientSession", return_value=mock_session),
        patch("agents.utils.auth.get_id_token", return_value=None),
        patch("agents.utils.redis_pool.get_shared_redis_client", return_value=mock_redis),
    ):
        res = await spawn_runners(count=1, tool_context=ctx)

    assert res["status"] == "success"
    raw_bytes = redis_store.get("echo_config:e-1")
    assert raw_bytes is not None
    raw_str = raw_bytes.decode("utf-8")
    assert "NaN" not in raw_str
    assert "Infinity" not in raw_str

    def _reject_non_rfc8259(const: str):
        raise ValueError(f"Non-RFC8259: {const}")

    parsed = json.loads(raw_str, parse_constant=_reject_non_rfc8259)
    assert parsed["initial_sentiment"] is None
    assert parsed["bounds"] == [None, None, 0.75]


# ---------------------------------------------------------------------------
# Gap 4: RedisDashLogPlugin._publish tenant resolution & graceful guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap4_plugins_publish_resolves_tenant_and_guards_missing_user_in_saas(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    redis_store, mock_redis = _make_fake_redis()
    redis_store["u:user-alice:simreg:context:vertex-p1"] = b"spawn-p1"
    redis_store["u:user-alice:simreg:session:spawn-p1"] = b"sim-p1"

    with patch("agents.utils.plugins.pubsub_v1"):
        plugin = RedisDashLogPlugin(topic_id="test-topic")
    plugin._do_publish = AsyncMock()

    # 1. Context carrying user_id in state resolves both context_id and simulation_id
    ctx_alice = MagicMock()
    ctx_alice.session = SimpleNamespace(id="vertex-p1", user_id="user-alice", state={})
    ctx_alice.state = {"user_id": "user-alice"}
    ctx_alice.invocation_id = "inv-1"

    with (
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
        patch.object(plugin, "_emit_narrative", new=AsyncMock()) as mock_narr,
    ):
        await plugin._publish(
            ctx_alice,
            {"type": "tool_end", "agent": "runner", "tool": "step", "result": {"ok": True}},
        )
        assert mock_narr.await_args is not None
        assert mock_narr.await_args.args[0] == "spawn-p1"
        assert mock_narr.await_args.kwargs["simulation_id"] == "sim-p1"

    # 2. Context without any user_id (or invalid user_id) does NOT crash in SAAS_MODE=on
    ctx_anon = MagicMock()
    ctx_anon.session = SimpleNamespace(id="unknown-v", user_id=None, state={})
    ctx_anon.state = {}
    ctx_anon.user_id = None
    ctx_anon._invocation_context = None
    ctx_anon.invocation_id = "inv-2"

    with (
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
        patch.object(plugin, "_emit_narrative", new=AsyncMock()) as mock_narr_anon,
    ):
        await plugin._publish(
            ctx_anon,
            {"type": "tool_end", "agent": "runner", "tool": "step", "result": {"ok": True}},
        )
        mock_narr_anon.assert_awaited_once()
        assert mock_narr_anon.await_args is not None
        assert mock_narr_anon.await_args.kwargs["simulation_id"] is None


# ---------------------------------------------------------------------------
# Gap 5: _auto_store_summary handles pre-flight config/schema errors safely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_overrides",),
    [
        ({"ALLOYDB_HOST": "127.0.0.1", "ALLOYDB_SCHEMA": "bad; DROP TABLE rules;--"},),
        ({"ALLOYDB_HOST": "127.0.0.1", "EMBEDDING_BACKEND": "bogus_backend"},),
    ],
)
async def test_gap5_auto_store_summary_returns_false_on_invalid_config(monkeypatch, env_overrides):
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)

    ctx = MagicMock()
    ctx.state = {"user_prompt": "plan route", "city": "Berlin"}
    ok = await planner_memory_tools._auto_store_summary("route-1", {"score": 90}, ctx)
    assert ok is False


@pytest.mark.asyncio
async def test_gap5_auto_store_summary_returns_false_when_get_dsn_raises(monkeypatch):
    monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
    ctx = MagicMock()
    ctx.state = {"user_prompt": "plan route", "city": "Berlin"}
    with patch.object(planner_memory_tools, "_get_dsn", side_effect=RuntimeError("missing dsn")):
        ok = await planner_memory_tools._auto_store_summary("route-2", {"score": 88}, ctx)
    assert ok is False


# ---------------------------------------------------------------------------
# Iteration 10 Follow-up: Enqueue-time pulses.py tenant scoping, RaceCollector
# round-trip, simulator _capture_root_simulation_id, dispatcher exception
# path user_scope, and plugins.py direct invocation user_scope propagation.
# ---------------------------------------------------------------------------


def _make_pubsub_capable_redis() -> tuple[dict[str, bytes], list[tuple[str, bytes]], MagicMock]:
    """Fake async Redis supporting get/set/delete/list/pipeline/pubsub."""
    kv_store: dict[str, bytes] = {}
    list_store: dict[str, list[bytes]] = {}
    published: list[tuple[str, bytes]] = []
    subscribers: dict[str, list[asyncio.Queue[dict[str, object]]]] = {}

    class _FakePubSub:
        def __init__(self) -> None:
            self._channels: list[str] = []
            self._inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def subscribe(self, *channels: str) -> None:
            for ch in channels:
                self._channels.append(ch)
                subscribers.setdefault(ch, []).append(self._inbox)

        async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 1.0):
            try:
                return await asyncio.wait_for(self._inbox.get(), timeout=min(timeout, 0.05))
            except asyncio.TimeoutError:
                return None

        async def close(self) -> None:
            for ch in self._channels:
                lst = subscribers.get(ch, [])
                if self._inbox in lst:
                    lst.remove(self._inbox)
            self._channels.clear()

    def _deliver(channel: str, data: bytes) -> int:
        published.append((channel, data))
        for q in subscribers.get(channel, []):
            q.put_nowait({"type": "message", "channel": channel, "data": data})
        return len(subscribers.get(channel, []))

    class _FakePipeline:
        def __init__(self, transaction: bool = False) -> None:
            self._ops: list[tuple[str, tuple[object, ...]]] = []

        async def __aenter__(self) -> "_FakePipeline":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        def publish(self, channel: str, data: bytes) -> "_FakePipeline":
            self._ops.append(("publish", (channel, data)))
            return self

        def lrange(self, key: str, start: int, end: int) -> "_FakePipeline":
            self._ops.append(("lrange", (key, start, end)))
            return self

        def delete(self, key: str) -> "_FakePipeline":
            self._ops.append(("delete", (key,)))
            return self

        async def execute(self) -> list[object]:
            results: list[object] = []
            for op, args in self._ops:
                if op == "publish":
                    ch, data = args
                    results.append(_deliver(str(ch), data if isinstance(data, bytes) else str(data).encode()))
                elif op == "lrange":
                    key, start, end = args
                    items = list(list_store.get(str(key), []))
                    if end == -1:
                        results.append(items[int(start) :])  # type: ignore[arg-type]
                    else:
                        results.append(items[int(start) : int(end) + 1])  # type: ignore[arg-type]
                elif op == "delete":
                    key = str(args[0])
                    existed = 1 if (key in kv_store or key in list_store) else 0
                    kv_store.pop(key, None)
                    list_store.pop(key, None)
                    results.append(existed)
            self._ops.clear()
            return results

    mock_redis = MagicMock()
    mock_redis.pubsub = MagicMock(side_effect=lambda: _FakePubSub())
    mock_redis.pipeline = MagicMock(side_effect=lambda transaction=False: _FakePipeline(transaction=transaction))

    async def _get(key: str):
        return kv_store.get(key)

    async def _set(key: str, value: str | bytes, ex: int | None = None, nx: bool = False):
        if nx and key in kv_store:
            return False
        kv_store[key] = value if isinstance(value, bytes) else str(value).encode()
        return True

    async def _delete(*keys: str):
        deleted = 0
        for k in keys:
            ks = k.decode() if isinstance(k, bytes) else k
            if ks in kv_store:
                del kv_store[ks]
                deleted += 1
            if ks in list_store:
                del list_store[ks]
                deleted += 1
        return deleted

    async def _rpush(key: str, *values: str | bytes):
        bucket = list_store.setdefault(key, [])
        for v in values:
            bucket.append(v if isinstance(v, bytes) else str(v).encode())
        return len(bucket)

    async def _ltrim(key: str, start: int, end: int):
        return True

    async def _expire(key: str, ttl: int):
        return True

    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.delete = AsyncMock(side_effect=_delete)
    mock_redis.rpush = AsyncMock(side_effect=_rpush)
    mock_redis.ltrim = AsyncMock(side_effect=_ltrim)
    mock_redis.expire = AsyncMock(side_effect=_expire)
    mock_redis.publish = AsyncMock(side_effect=lambda ch, msg: _deliver(ch, msg if isinstance(msg, bytes) else str(msg).encode()))
    return kv_store, published, mock_redis


@pytest.mark.asyncio
async def test_r10_pulses_enqueue_time_tenant_isolation_and_zero_worker_bleed(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    pulses_mod.reset()
    _, published, mock_redis = _make_pubsub_capable_redis()
    try:
        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            # Tenant Alice triggers creation of the 16-worker pool while Alice's ContextVar is active.
            with simulation_registry.user_scope("user-alice"):
                await pulses_mod.emit_gateway_message(
                    origin={"type": "agent", "id": "runner-a", "session_id": "s-alice"},
                    destination=["s-alice"],
                    status="success",
                    msg_type="json",
                    event="text",
                    data={"text": "alice-msg"},
                )
            await asyncio.sleep(0.05)

            # Tenant Bob and Tenant Alice enqueue synchronously into the SAME batch
            # on the SAME loop / worker pool before yielding to workers.
            with simulation_registry.user_scope("user-bob"):
                await pulses_mod.emit_gateway_message(
                    origin={"type": "agent", "id": "runner-b", "session_id": "s-bob"},
                    destination=["s-bob"],
                    status="success",
                    msg_type="json",
                    event="text",
                    data={"text": "bob-msg"},
                )
            with simulation_registry.user_scope("user-alice"):
                await pulses_mod.emit_gateway_message(
                    origin={"type": "agent", "id": "runner-a", "session_id": "s-alice"},
                    destination=["s-alice"],
                    status="success",
                    msg_type="json",
                    event="text",
                    data={"text": "alice-msg-2"},
                )

            # Explicit user_id outside any user_scope across public wrappers
            await pulses_mod.emit_narrative_pulse("s-charlie", "charlie-narr", user_id="user-charlie")
            await pulses_mod.emit_inter_agent_pulse("s-charlie", "a", "b", "hi", user_id="user-charlie")
            await pulses_mod.emit_telemetry_pulse("s-charlie", {"agent": "r", "type": "tick"}, user_id="user-charlie")

            await asyncio.sleep(0.15)

        by_channel: dict[str, list[str]] = {}
        for ch, raw in published:
            w = gateway_pb2.Wrapper()
            w.ParseFromString(raw)
            by_channel.setdefault(ch, []).append(w.origin.session_id)

        assert "gateway:broadcast" not in by_channel
        assert by_channel.get("u:user-alice:gateway:broadcast") == ["s-alice", "s-alice"]
        assert by_channel.get("u:user-bob:gateway:broadcast") == ["s-bob"]
        assert by_channel.get("u:user-charlie:gateway:broadcast") == ["s-charlie", "s-charlie", "s-charlie"]
    finally:
        pulses_mod.reset()


@pytest.mark.asyncio
async def test_r10_pulses_precedence_fail_closed_and_retry_channel_preservation(monkeypatch):
    pulses_mod.reset()
    try:
        # 1. SAAS_MODE=off collapses to literal "gateway:broadcast" regardless of scope/env/explicit
        monkeypatch.setenv("SAAS_MODE", "off")
        monkeypatch.setenv("DISPATCHER_USER_ID", "alice")
        with simulation_registry.user_scope("bob"):
            assert pulses_mod._gateway_broadcast_channel() == "gateway:broadcast"
            assert pulses_mod._gateway_broadcast_channel("carol") == "gateway:broadcast"

        # 2. SAAS_MODE=on precedence: explicit > user_scope > DISPATCHER_USER_ID; anonymous -> unscoped
        monkeypatch.setenv("SAAS_MODE", "on")
        monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
        assert pulses_mod._gateway_broadcast_channel() == "u:alice:gateway:broadcast"
        with simulation_registry.user_scope("bob"):
            assert pulses_mod._gateway_broadcast_channel() == "u:bob:gateway:broadcast"
            assert pulses_mod._gateway_broadcast_channel("carol") == "u:carol:gateway:broadcast"
        monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)
        with simulation_registry.user_scope("simulation"):
            assert pulses_mod._gateway_broadcast_channel() == "gateway:broadcast"

        # 3. Invalid user_id fails fast at enqueue time without spawning workers
        with pytest.raises(ValueError, match="user_id"):
            await pulses_mod._publish_to_gateway(b"bad", user_id="bad:uid")
        assert pulses_mod._loop_state == {}

        # 4. Retry path republishes mixed-tenant batch to each item's enqueued channel
        attempts = 0
        published_after_retry: list[tuple[str, bytes]] = []

        class _RetryPipe:
            def __init__(self) -> None:
                self._ops: list[tuple[str, bytes]] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def publish(self, ch: str, msg: bytes):
                self._ops.append((ch, msg))

            async def execute(self):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionError("transient blip")
                published_after_retry.extend(self._ops)

        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(side_effect=lambda transaction=False: _RetryPipe())

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            with simulation_registry.user_scope("user-alice"):
                await pulses_mod._publish_to_gateway(b"payload-a")
            with simulation_registry.user_scope("user-bob"):
                await pulses_mod._publish_to_gateway(b"payload-b")
            await asyncio.sleep(0.35)

        assert attempts >= 2
        assert ("u:user-alice:gateway:broadcast", b"payload-a") in published_after_retry
        assert ("u:user-bob:gateway:broadcast", b"payload-b") in published_after_retry
    finally:
        pulses_mod.reset()


@pytest.mark.asyncio
async def test_r10_pulses_to_race_collector_skip_pubsub_false_round_trip(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    pulses_mod.reset()
    _, _, mock_redis = _make_pubsub_capable_redis()
    collector: RaceCollector | None = None
    try:
        with (
            patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis),
            patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis),
        ):
            with simulation_registry.user_scope("user-alice"):
                collector = await RaceCollector.start(
                    session_id="sim-sess-alice",
                    runner_session_ids={"r-alice-1", "r-bob-1"},
                    skip_pubsub=False,
                )

            # Emit Alice's telemetry under Alice's scope and Bob's telemetry under Bob's scope
            with simulation_registry.user_scope("user-alice"):
                await pulses_mod.emit_gateway_message(
                    origin={"type": "agent", "id": "runner_alice", "session_id": "r-alice-1"},
                    destination=["sim-sess-alice"],
                    status="info",
                    msg_type="telemetry",
                    event="tick",
                    data={"tick": 1, "tenant": "alice"},
                )
            with simulation_registry.user_scope("user-bob"):
                await pulses_mod.emit_gateway_message(
                    origin={"type": "agent", "id": "runner_bob", "session_id": "r-bob-1"},
                    destination=["sim-sess-alice"],
                    status="info",
                    msg_type="telemetry",
                    event="tick",
                    data={"tick": 1, "tenant": "bob"},
                )

            await asyncio.sleep(0.2)
            drained = await collector.drain()

        assert len(drained) == 1
        assert drained[0]["session_id"] == "r-alice-1"
        assert drained[0]["payload"]["tenant"] == "alice"
    finally:
        if collector is not None:
            with patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis):
                await collector.stop()
        pulses_mod.reset()


@pytest.mark.asyncio
async def test_r10_simulator_capture_root_simulation_id_saas_modes(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    _, mock_redis = _make_fake_redis()
    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
        await simulation_registry.register_context("v-alice", "ctx-alice-orig", user_id="user-alice")
        await simulation_registry.register_context("v-alice", "ctx-bob-other", user_id="user-bob")

        # (a) Outside user_scope with MagicMock attrs and no str user_id: does NOT raise ValueError
        ctx_mock = MagicMock()
        ctx_mock.state = {}
        ctx_mock.session.id = "v-alice"
        await simulator_agent._capture_root_simulation_id(ctx_mock)
        assert ctx_mock.state["simulation_id"] == "v-alice"

        # (a2) Outside user_scope with anonymous user_id: does NOT raise ValueError
        ctx_anon = SimpleNamespace(
            state={"user_id": "simulation_user"},
            session=SimpleNamespace(id="v-alice", user_id="simulation_user"),
        )
        await simulator_agent._capture_root_simulation_id(ctx_anon)
        assert ctx_anon.state["simulation_id"] == "v-alice"

        # (b) Outside user_scope with session.user_id / state user_id: resolves Alice's mapping only
        ctx_sess = SimpleNamespace(
            state={},
            session=SimpleNamespace(id="v-alice", user_id="user-alice"),
        )
        await simulator_agent._capture_root_simulation_id(ctx_sess)
        assert ctx_sess.state["simulation_id"] == "ctx-alice-orig"

        ctx_tenant_state = SimpleNamespace(
            state={"tenant": {"user_id": "user-bob"}},
            session=SimpleNamespace(id="v-alice", user_id=""),
        )
        await simulator_agent._capture_root_simulation_id(ctx_tenant_state)
        assert ctx_tenant_state.state["simulation_id"] == "ctx-bob-other"

        # (c) Inside user_scope with no user_id on callback_context: resolves Alice's mapping
        ctx_scoped = SimpleNamespace(
            state={},
            session=SimpleNamespace(id="v-alice", user_id=None),
        )
        with simulation_registry.user_scope("user-alice"):
            await simulator_agent._capture_root_simulation_id(ctx_scoped)
        assert ctx_scoped.state["simulation_id"] == "ctx-alice-orig"


@pytest.mark.asyncio
async def test_r10_dispatcher_exception_path_preserves_user_scope(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    pulses_mod.reset()
    _, published, mock_redis = _make_pubsub_capable_redis()
    try:
        agent = LlmAgent(name="runner_agent", instruction="Hi")
        runner = InMemoryRunner(app=App(name="runner_autopilot", root_agent=agent))

        async def _boom_run_async(**kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        runner.run_async = _boom_run_async  # type: ignore[method-assign]

        disp = RedisOrchestratorDispatcher(runner=runner)
        disp.session_user_map["sess-alice"] = "user-alice"
        disp.session_simulation_map["sess-alice"] = "sim-alice"

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            await disp._trigger_agent_run_logic("sess-alice", "hello")
            await asyncio.sleep(0.15)

        assert len(published) == 2
        channels = [ch for ch, _ in published]
        assert channels == ["u:user-alice:gateway:broadcast", "u:user-alice:gateway:broadcast"]
        events = []
        for _, raw in published:
            w = gateway_pb2.Wrapper()
            w.ParseFromString(raw)
            events.append((w.event, w.status, w.simulation_id))
        assert events == [
            ("text", "error", "sim-alice"),
            ("run_end", "success", "sim-alice"),
        ]
    finally:
        pulses_mod.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("fire_and_forget", [False, True])
async def test_r10_plugins_direct_publish_propagates_plugin_user_id_to_pulses(monkeypatch, fire_and_forget):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    pulses_mod.reset()
    kv_store, published, mock_redis = _make_pubsub_capable_redis()
    kv_store["u:user-alice:simreg:context:vertex-p2"] = b"spawn-p2"
    kv_store["u:user-alice:simreg:session:spawn-p2"] = b"sim-p2"

    try:
        with patch("agents.utils.plugins.pubsub_v1"):
            plugin = RedisDashLogPlugin(topic_id="test-topic", fire_and_forget=fire_and_forget)
        plugin._do_publish = AsyncMock()

        ctx_alice = MagicMock()
        ctx_alice.session = SimpleNamespace(id="vertex-p2", user_id="user-alice", state={})
        ctx_alice.state = {"user_id": "user-alice"}
        ctx_alice.invocation_id = "inv-p2"

        with (
            patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
            patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis),
        ):
            await plugin._publish(
                ctx_alice,
                {"type": "tool_start", "agent": "runner", "tool": "step", "args": {}},
            )
            await plugin._publish(
                ctx_alice,
                {"type": "tool_end", "agent": "runner", "tool": "step", "result": {"ok": True}},
            )
            if plugin._background_tasks:
                await asyncio.gather(*list(plugin._background_tasks))
            await asyncio.sleep(0.15)

        assert len(published) == 2
        assert all(ch == "u:user-alice:gateway:broadcast" for ch, _ in published)
        decoded = []
        for _, raw in published:
            w = gateway_pb2.Wrapper()
            w.ParseFromString(raw)
            decoded.append((w.event, w.simulation_id, w.origin.session_id))
        assert set(decoded) == {
            ("tool_start", "sim-p2", "spawn-p2"),
            ("tool_end", "sim-p2", "spawn-p2"),
        }
    finally:
        pulses_mod.reset()
