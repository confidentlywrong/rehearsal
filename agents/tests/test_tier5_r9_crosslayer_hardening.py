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

"""Tier 5 Iteration 9 adversarial regression tests for Go<->Python cross-layer
tenant isolation, simreg keyspace parity, callable broadcast payload handling,
and RFC-8259 JSON serialization boundaries.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.types import Message as A2AMessage
from a2a.types import MessageSendParams, Part, Role, TaskState, TextPart
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from agents.leadership_planner_with_eval.tools import (
    submit_plan_to_simulator as leadership_submit_plan,
)
from agents.planner_with_eval.tools import (
    submit_plan_to_simulator as marathon_submit_plan,
)
from agents.utils import communication, simulation_registry
from agents.utils.communication import (
    SimulationA2AClient,
    _resolve_a2a_user_id,
    resolve_tool_context_user_id,
)
from agents.utils.dispatcher import (
    RedisOrchestratorDispatcher,
    ThreadSafeDict,
    ThreadSafeSet,
)
from agents.utils.session_manager import SessionManager
from agents.utils.simulation_executor import SimulationExecutor


@pytest.fixture(autouse=True)
def _clean_env_and_simreg(monkeypatch):
    for var in ("SAAS_MODE", "DISPATCHER_USER_ID", "ACCOUNT_KEY_PREFIX", "DISPATCH_MODE"):
        monkeypatch.delenv(var, raising=False)
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()
    yield
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

    async def _scan_iter(match: str = "*", count: int = 100):
        import fnmatch

        for k in list(store.keys()):
            if fnmatch.fnmatch(k, match):
                yield k.encode()

    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.setex = AsyncMock(side_effect=_setex)
    mock_redis.delete = AsyncMock(side_effect=_delete)
    mock_redis.scan_iter = _scan_iter
    return store, mock_redis


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
# Challenger 2 Gap 1: Gateway spawn_agent user_id parity in Dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_user_fields",
    [
        {"acting_user_id": "user-alice", "userId": "user-alice"},
        {"user_id": "user-alice", "acting_user_id": "user-alice", "userId": "user-alice"},
        {"userId": "user-alice"},
    ],
)
async def test_r9_dispatcher_accepts_gateway_spawn_payload_shapes(monkeypatch, payload_user_fields):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCHER_USER_ID", "user-alice")

    agent = LlmAgent(name="lp_agent", instruction="Hi")
    runner = InMemoryRunner(app=App(name="leadership_planner", root_agent=agent))
    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=None):
        await dispatcher.handle_event(
            {
                "type": "spawn_agent",
                "eventId": "evt-gw-1",
                "sessionId": "sess-gw-1",
                "payload": {
                    "agentType": "leadership_planner",
                    "simulation_id": "sim-gw-1",
                    **payload_user_fields,
                },
            }
        )

    assert "sess-gw-1" in dispatcher.active_sessions
    assert dispatcher.session_user_map.get("sess-gw-1") == "user-alice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bound_user", "extra_payload"),
    [
        ("user-alice", {"acting_user_id": "user-bob", "userId": "user-bob"}),
        ("user-alice", {"user_id": "user-alice", "acting_user_id": "user-bob"}),
        ("user-alice", {"user_id": 12345}),
        ("user-alice", {"user_id": ["user-alice"]}),
        ("", {}),
        ("", {"user_id": "simulation"}),
        ("", {"user_id": "simulation_user"}),
        ("", {"user_id": "anonymous"}),
        ("", {"user_id": "bad:colon"}),
    ],
)
async def test_r9_dispatcher_rejects_mismatched_conflicting_or_anon_spawn_in_saas(
    monkeypatch, bound_user, extra_payload
):
    monkeypatch.setenv("SAAS_MODE", "on")
    if bound_user:
        monkeypatch.setenv("DISPATCHER_USER_ID", bound_user)

    agent = LlmAgent(name="lp_agent", instruction="Hi")
    runner = InMemoryRunner(app=App(name="leadership_planner", root_agent=agent))
    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    await dispatcher.handle_event(
        {
            "type": "spawn_agent",
            "eventId": "evt-bad-spawn",
            "sessionId": "sess-bad-spawn",
            "payload": {
                "agentType": "leadership_planner",
                "simulation_id": "sim-bad",
                **extra_payload,
            },
        }
    )

    assert "sess-bad-spawn" not in dispatcher.active_sessions
    assert "sess-bad-spawn" not in dispatcher.session_user_map


# ---------------------------------------------------------------------------
# Challenger 2 Gap 2: End-to-end tenant propagation (no "simulation" collapse)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r9_cross_layer_gateway_spawn_to_dispatcher_to_submit_plan_to_executor(monkeypatch):
    """Contract test: Gateway spawn_agent -> Dispatcher -> Runner -> submit_plan -> A2A -> Executor."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCHER_USER_ID", "user-alice")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

    redis_store, mock_redis = _make_fake_redis()

    planner_agent = LlmAgent(name="lp_agent", instruction="Respond")
    planner_runner = InMemoryRunner(app=App(name="leadership_planner_with_eval", root_agent=planner_agent))
    planner_runner.auto_create_session = True
    dispatcher = RedisOrchestratorDispatcher(runner=planner_runner)

    # 1. Feed Gateway's spawn_agent event shape into Dispatcher
    gateway_spawn_event = {
        "type": "spawn_agent",
        "sessionId": "planner-sess-e2e",
        "eventId": "evt-e2e-spawn",
        "payload": {
            "agentType": "leadership_planner_with_eval",
            "userId": "user-alice",
            "user_id": "user-alice",
            "simulation_id": "sim-e2e-parent",
            "acting_user_id": "user-alice",
            "user_auth_token": "",
        },
    }
    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
        await dispatcher.handle_event(gateway_spawn_event)

    assert "planner-sess-e2e" in dispatcher.active_sessions
    assert redis_store.get("u:user-alice:simreg:session:planner-sess-e2e") == b"sim-e2e-parent"

    # 2. Run dispatcher._trigger_agent_run_logic and verify Runner.run_async user_id + session.state
    observed_run_users: list[str] = []
    captured_tool_contexts: list[SimpleNamespace] = []

    mock_response = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(parts=[types.Part.from_text(text="Planned.")]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1, candidates_token_count=1, total_token_count=2
        ),
    )

    orig_run_async = planner_runner.run_async

    async def _wrapped_run_async(**kwargs):
        observed_run_users.append(kwargs["user_id"])
        async for ev in orig_run_async(**kwargs):
            yield ev
        sess = await planner_runner.session_service.get_session(
            app_name="leadership_planner_with_eval",
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
        )
        assert sess is not None
        captured_tool_contexts.append(
            SimpleNamespace(
                invocation_id="inv-e2e-1",
                agent_name="leadership_planner_with_eval",
                session=sess,
                state=dict(sess.state)
                | {
                    "leadership_scenario": {"title": "Board Pivot", "stakeholder_count": 3},
                    "simulator_session_id": "sim-exec-e2e",
                    "_sim_token": "sim-exec-e2e",
                },
            )
        )

    mock_genai_client = MagicMock()
    mock_genai_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    async def _mock_stream(*args, **kwargs):
        yield mock_response

    mock_genai_client.aio.models.generate_content_stream = _mock_stream

    with (
        patch("google.genai.Client", return_value=mock_genai_client),
        patch.object(planner_runner, "run_async", side_effect=_wrapped_run_async),
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
    ):
        pulses: list[str] = []
        await dispatcher._trigger_agent_run_logic(
            "planner-sess-e2e",
            types.Content(role="user", parts=[types.Part.from_text(text="Go")]),
            pulses_collector=pulses,
        )

    assert observed_run_users == ["user-alice"]
    assert len(captured_tool_contexts) == 1
    tool_ctx = captured_tool_contexts[0]
    assert tool_ctx.state["user_id"] == "user-alice"
    assert tool_ctx.state["acting_user_id"] == "user-alice"
    assert resolve_tool_context_user_id(tool_ctx) == "user-alice"

    # 3. Execute submit_plan_to_simulator -> call_agent -> SimulationA2AClient -> SimulationExecutor
    sim_executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator")
    sim_executor._dispatch_mode = "callable"
    sim_observed_users: list[str] = []

    final_sim_event = MagicMock()
    final_sim_event.is_final_response.return_value = True
    final_sim_event.author = "agent"
    final_sim_event.content.parts = [MagicMock(text="Simulation complete.")]

    async def _sim_run_async(*, session_id, user_id, new_message):
        sim_observed_users.append(user_id)
        yield final_sim_event

    sim_runner = MagicMock()
    sim_runner.app_name = "simulator"
    sim_runner.app.plugins = []
    sim_runner.run_async = _sim_run_async
    sim_executor._runner = sim_runner

    mock_session_service = AsyncMock()
    mock_session_service.create_session = AsyncMock(return_value=SimpleNamespace(id="vertex-sim-1"))
    sim_executor._session_manager = SessionManager(session_service=mock_session_service)

    async def _bridge_send(a2a_msg: A2AMessage):
        req_ctx = RequestContext(
            request=MessageSendParams(message=a2a_msg),
            context_id=a2a_msg.context_id,
            task_id="task-sim-e2e",
        )
        eq = AsyncMock()
        await sim_executor.execute(req_ctx, eq)
        statuses = [
            c.args[0].status.state for c in eq.enqueue_event.await_args_list if c.args and hasattr(c.args[0], "status")
        ]
        assert TaskState.failed not in statuses
        yield A2AMessage(
            message_id="resp-sim-e2e",
            parts=[Part(root=TextPart(text="Simulation complete."))],
            role=Role.agent,
        )

    mock_a2a = MagicMock()
    mock_a2a.send_message = _bridge_send
    mock_remote = MagicMock()
    mock_remote._ensure_resolved = AsyncMock()
    mock_remote._a2a_client = mock_a2a

    a2a_client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    a2a_client.get_agent = AsyncMock(return_value=mock_remote)

    with (
        patch("agents.utils.communication_plugin.get_client", return_value=a2a_client),
        patch("agents.utils.communication.emit_inter_agent_pulse", new=AsyncMock()),
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.simulation_executor.pulses.emit_gateway_message", new=AsyncMock()),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={"vitals_trend": []})),
    ):
        res = await leadership_submit_plan(
            action="execute",
            message="Run scenario",
            tool_context=tool_ctx,  # type: ignore[arg-type]
        )

    assert res["status"] == "success"
    assert sim_observed_users == ["user-alice"]
    assert "session_map:user-alice:sim-exec-e2e" in redis_store
    assert redis_store.get("u:user-alice:simreg:session:sim-exec-e2e") == b"sim-exec-e2e"
    assert redis_store.get("u:user-alice:simreg:context:vertex-sim-1") == b"sim-exec-e2e"


@pytest.mark.asyncio
async def test_r9_dispatcher_trigger_fails_closed_without_user_in_saas_and_keeps_off_compat(monkeypatch):
    agent = LlmAgent(name="test_agent", instruction="Hi")
    runner = InMemoryRunner(app=App(name="test_app", root_agent=agent))
    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    monkeypatch.setenv("SAAS_MODE", "on")
    with pytest.raises(ValueError, match="user_id"):
        await dispatcher._trigger_agent_run_logic(
            "unspawned-sess",
            types.Content(role="user", parts=[types.Part.from_text(text="hi")]),
            pulses_collector=[],
        )

    # SAAS_MODE=off still falls back to "simulation" for legacy compatibility
    monkeypatch.setenv("SAAS_MODE", "off")
    observed_users: list[str] = []

    async def _mock_run_async(**kwargs):
        observed_users.append(kwargs["user_id"])
        if False:
            yield None

    with patch.object(runner, "run_async", side_effect=_mock_run_async):
        await dispatcher._trigger_agent_run_logic(
            "legacy-sess",
            types.Content(role="user", parts=[types.Part.from_text(text="hi")]),
            pulses_collector=[],
        )
    assert observed_users == ["simulation"]


@pytest.mark.asyncio
async def test_r9_new_instantiated_dispatcher_trigger_saas_off_and_on(monkeypatch):
    """__new__-instantiated dispatcher (test_dispatcher.py shape) handles SAAS off/on safely."""
    observed_calls: list[dict] = []

    async def _run_async(*, user_id, session_id, new_message, state_delta=None):
        observed_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "new_message": new_message,
                "state_delta": state_delta,
            }
        )
        if False:
            yield None

    mock_runner = MagicMock()
    mock_runner.app_name = "test_agent"
    mock_runner.run_async = _run_async

    dispatcher = RedisOrchestratorDispatcher.__new__(RedisOrchestratorDispatcher)
    dispatcher.agent_type = "test_agent"
    dispatcher.runner = mock_runner
    dispatcher._background_tasks = set()
    dispatcher._run_semaphore = None
    assert not hasattr(dispatcher, "session_user_map")

    # 1. SAAS_MODE=off: falls back to "simulation" without AttributeError
    monkeypatch.setenv("SAAS_MODE", "off")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)
    await dispatcher._trigger_agent_run_logic("sess-new-off", "hello")
    assert observed_calls[-1]["user_id"] == "simulation"
    assert observed_calls[-1]["state_delta"] is None

    # 2. SAAS_MODE=on with DISPATCHER_USER_ID="": fails closed with ValueError (not AttributeError)
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCHER_USER_ID", "")
    with pytest.raises(ValueError, match="user_id"):
        await dispatcher._trigger_agent_run_logic("sess-new-on-empty", "hello")

    # 3. SAAS_MODE=on with DISPATCHER_USER_ID="user-alice": propagates user_id + state_delta
    monkeypatch.setenv("DISPATCHER_USER_ID", "user-alice")
    await dispatcher._trigger_agent_run_logic("sess-new-on-alice", "hello")
    assert observed_calls[-1]["user_id"] == "user-alice"
    assert observed_calls[-1]["state_delta"] == {
        "user_id": "user-alice",
        "acting_user_id": "user-alice",
    }


@pytest.mark.asyncio
async def test_r9_new_instantiated_dispatcher_lifecycle_without_session_user_map(monkeypatch, caplog):
    """__new__ dispatcher omitting session_user_map and _loop runs reset, remove_session, and spawn cleanly."""
    observed_calls: list[dict] = []

    async def _run_async(*, user_id, session_id, new_message, state_delta=None):
        observed_calls.append({"user_id": user_id, "session_id": session_id, "state_delta": state_delta})
        if False:
            yield None

    mock_runner = MagicMock()
    mock_runner.app_name = "test_agent"
    mock_runner.run_async = _run_async

    dispatcher = RedisOrchestratorDispatcher.__new__(RedisOrchestratorDispatcher)
    dispatcher.agent_type = "test_agent"
    dispatcher.runner = mock_runner
    dispatcher._background_tasks = set()
    dispatcher._session_locks = {"sess-stale": asyncio.Lock()}
    dispatcher.session_simulation_map = ThreadSafeDict()
    dispatcher.session_simulation_map["sess-stale"] = "sim-stale"
    dispatcher.active_sessions = ThreadSafeSet()
    dispatcher.active_sessions.add("sess-stale")
    dispatcher._seen_events = OrderedDict([("evt-1", None)])
    dispatcher._simulation_subscriptions = ThreadSafeSet()
    dispatcher._pubsub = None
    dispatcher._run_semaphore = None
    dispatcher.allowed_authors = {"agent", "tool", "model", "test_agent"}
    # Deliberately omit _loop, _active_session_tasks, and session_user_map

    redis_store, mock_redis = _make_fake_redis()
    monkeypatch.setenv("SAAS_MODE", "off")

    with (
        caplog.at_level(logging.ERROR, logger="agents.utils.dispatcher"),
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
    ):
        await dispatcher._process_event({"type": "environment_reset"})

    assert "ORCHESTRATION_ERROR" not in caplog.text
    assert "AttributeError" not in caplog.text
    assert len(dispatcher._session_locks) == 0
    assert len(dispatcher.session_simulation_map) == 0
    assert len(dispatcher._seen_events) == 0

    # remove_session works when session_user_map and _loop are absent in both SAAS modes
    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
        dispatcher.remove_session("sess-uninit-off")
        monkeypatch.setenv("SAAS_MODE", "on")
        monkeypatch.setenv("DISPATCHER_USER_ID", "")
        dispatcher.remove_session("sess-uninit-on")

    # spawn_agent lazily initializes session_user_map and feeds _trigger_agent_run_logic
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCHER_USER_ID", "")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    with (
        caplog.at_level(logging.ERROR, logger="agents.utils.dispatcher"),
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
    ):
        await dispatcher._process_event(
            {
                "type": "spawn_agent",
                "eventId": "evt-new-spawn",
                "sessionId": "sess-new",
                "payload": {
                    "agentType": "test_agent",
                    "simulation_id": "sim-new",
                    "user_id": "user-alice",
                },
            }
        )
        assert isinstance(dispatcher.session_user_map, ThreadSafeDict)
        assert dispatcher.session_user_map.get("sess-new") == "user-alice"
        assert redis_store.get("u:user-alice:simreg:session:sess-new") == b"sim-new"

        await dispatcher._trigger_agent_run_logic("sess-new", "hello")
        assert observed_calls[-1]["user_id"] == "user-alice"
        assert observed_calls[-1]["state_delta"] == {
            "user_id": "user-alice",
            "acting_user_id": "user-alice",
        }

        dispatcher.remove_session("sess-new")
        assert "sess-new" not in dispatcher.session_user_map
        assert "ORCHESTRATION_ERROR" not in caplog.text
        assert "AttributeError" not in caplog.text


@pytest.mark.asyncio
async def test_r9_init_dispatcher_remove_session_and_environment_reset_tenant_cleanup(monkeypatch):
    """Normal __init__ dispatcher cleans per-tenant simreg:session and session_user_map across remove_session and reset."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCHER_USER_ID", "")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

    redis_store, mock_redis = _make_fake_redis()
    agent = LlmAgent(name="lp_agent", instruction="Hi")
    runner = InMemoryRunner(app=App(name="leadership_planner", root_agent=agent))
    dispatcher = RedisOrchestratorDispatcher(runner=runner)
    dispatcher._loop = asyncio.get_running_loop()

    try:
        with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-a1",
                    "sessionId": "sess-a1",
                    "payload": {
                        "agentType": "leadership_planner",
                        "simulation_id": "sim-a",
                        "user_id": "user-alice",
                    },
                }
            )
            await dispatcher.handle_event(
                {
                    "type": "spawn_agent",
                    "eventId": "evt-b1",
                    "sessionId": "sess-b1",
                    "payload": {
                        "agentType": "leadership_planner",
                        "simulation_id": "sim-b",
                        "user_id": "user-bob",
                    },
                }
            )

            assert dispatcher.session_user_map.get("sess-a1") == "user-alice"
            assert dispatcher.session_user_map.get("sess-b1") == "user-bob"
            assert redis_store.get("u:user-alice:simreg:session:sess-a1") == b"sim-a"
            assert redis_store.get("u:user-bob:simreg:session:sess-b1") == b"sim-b"

            dispatcher.remove_session("sess-a1")
            if dispatcher._background_tasks:
                await asyncio.gather(*list(dispatcher._background_tasks))

            assert "sess-a1" not in dispatcher.session_user_map
            assert "u:user-alice:simreg:session:sess-a1" not in redis_store
            assert dispatcher.session_user_map.get("sess-b1") == "user-bob"
            assert redis_store.get("u:user-bob:simreg:session:sess-b1") == b"sim-b"

            await dispatcher.handle_event(
                {
                    "type": "environment_reset",
                    "payload": {"user_id": "user-bob"},
                }
            )

            assert len(dispatcher.session_user_map) == 0
            assert "u:user-bob:simreg:session:sess-b1" not in redis_store
    finally:
        dispatcher._loop = None


def test_r9_communication_rejects_simulation_literal_in_saas_on_only(monkeypatch):
    ctx = SimpleNamespace(
        state={"user_id": "simulation"},
        session=SimpleNamespace(id="s1", user_id="simulation"),
    )

    monkeypatch.setenv("SAAS_MODE", "on")
    with pytest.raises(ValueError, match="user_id"):
        resolve_tool_context_user_id(ctx)
    with pytest.raises(ValueError, match="user_id"):
        _resolve_a2a_user_id("hello", user_id="simulation")
    with pytest.raises(ValueError, match="user_id"):
        _resolve_a2a_user_id({"action": "execute", "user_id": "simulation"})

    monkeypatch.setenv("SAAS_MODE", "off")
    assert resolve_tool_context_user_id(ctx) is None
    assert _resolve_a2a_user_id("hello", user_id="simulation") is None


@pytest.mark.asyncio
async def test_r9_simulation_executor_rejects_simulation_literal_in_saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCH_MODE", "callable")

    mock_runner = MagicMock()
    mock_runner.app_name = "simulator"
    mock_runner.app.plugins = []
    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator", runner=mock_runner)
    executor._session_manager = AsyncMock()

    ctx = _make_exec_context(
        json.dumps({"action": "execute", "simulation_id": "sim-1", "user_id": "simulation"}),
        context_id="ctx-sim-lit",
        metadata={"user_id": "simulation"},
    )
    eq = AsyncMock()
    with patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None):
        await executor.execute(ctx, eq)

    executor._session_manager.get_or_create_session.assert_not_called()
    statuses = [
        c.args[0].status.state for c in eq.enqueue_event.await_args_list if c.args and hasattr(c.args[0], "status")
    ]
    assert TaskState.failed in statuses


# ---------------------------------------------------------------------------
# Challenger 2 Gap 3: simreg keyspace parity & multi-tenant callable mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r9_simreg_multitenant_callable_isolation_and_key_parity(monkeypatch):
    redis_store, mock_redis = _make_fake_redis()

    monkeypatch.setenv("SAAS_MODE", "off")
    assert simulation_registry._session_key("sess-1", user_id="user-alice") == "simreg:session:sess-1"
    assert simulation_registry._context_key("vsess-1", user_id="user-alice") == "simreg:context:vsess-1"

    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    assert (
        simulation_registry._session_key("sess-1", user_id="user-alice")
        == "u:user-alice:simreg:session:sess-1"
    )
    assert (
        simulation_registry._context_key("vsess-1", user_id="user-alice")
        == "u:user-alice:simreg:context:vsess-1"
    )

    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis):
        # Missing or anonymous user_id fails closed in SAAS_MODE=on when DISPATCHER_USER_ID is unset
        for bad_uid in (None, "", "simulation", "simulation_user"):
            with pytest.raises(ValueError, match="user_id"):
                await simulation_registry.register("sess-x", "sim-x", user_id=bad_uid)
            with pytest.raises(ValueError, match="user_id"):
                await simulation_registry.lookup("sess-x", user_id=bad_uid)
            with pytest.raises(ValueError, match="user_id"):
                await simulation_registry.register_context("vsess-x", "ctx-x", user_id=bad_uid)
            with pytest.raises(ValueError, match="user_id"):
                await simulation_registry.get_context_id("vsess-x", user_id=bad_uid)
            with pytest.raises(ValueError, match="user_id"):
                await simulation_registry.unregister("sess-x", user_id=bad_uid)

        # Explicit user_id and user_scope isolate Alice & Bob in Redis and L1
        await simulation_registry.register("shared-sess", "sim-alice", user_id="user-alice")
        with simulation_registry.user_scope("user-bob"):
            await simulation_registry.register("shared-sess", "sim-bob")
            assert await simulation_registry.lookup("shared-sess") == "sim-bob"

        assert await simulation_registry.lookup("shared-sess", user_id="user-alice") == "sim-alice"
        assert redis_store.get("u:user-alice:simreg:session:shared-sess") == b"sim-alice"
        assert redis_store.get("u:user-bob:simreg:session:shared-sess") == b"sim-bob"
        assert "simreg:session:shared-sess" not in redis_store

        # Tenant-scoped clear removes only Alice's entries
        await simulation_registry.clear(user_id="user-alice")
        assert await simulation_registry.lookup("shared-sess", user_id="user-alice") is None
        assert await simulation_registry.lookup("shared-sess", user_id="user-bob") == "sim-bob"


@pytest.mark.asyncio
async def test_r9_executor_callable_mode_reads_go_scoped_simreg_without_dispatcher_env(monkeypatch):
    """Callable mode with DISPATCHER_USER_ID='' reads u:<uid>:simreg:session:<ctx> written by Go."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCH_MODE", "callable")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

    redis_store, mock_redis = _make_fake_redis()
    # Pre-seed Go Switchboard's scoped simreg:session key
    redis_store["u:user-alice:simreg:session:ctx-ae-1"] = b"sim-from-go"

    final_event = MagicMock()
    final_event.is_final_response.return_value = True
    final_event.author = "agent"
    final_event.content.parts = [MagicMock(text="OK")]

    async def _run_async(**kwargs):
        # Inside runner, plugins/tools using user_scope resolve simreg cleanly without user_id arg
        assert await simulation_registry.lookup("ctx-ae-1") == "sim-from-go"
        assert await simulation_registry.get_context_id("vertex-ae-1") == "ctx-ae-1"
        yield final_event

    mock_runner = MagicMock()
    mock_runner.app_name = "simulator"
    mock_runner.app.plugins = []
    mock_runner.run_async = _run_async

    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator", runner=mock_runner)
    mock_session_service = AsyncMock()
    mock_session_service.create_session = AsyncMock(return_value=SimpleNamespace(id="vertex-ae-1"))
    executor._session_manager = SessionManager(session_service=mock_session_service)

    ctx = _make_exec_context(
        json.dumps(
            {
                "type": "broadcast",
                "sessionId": "ctx-ae-1",
                "payload": {"data": json.dumps({"text": "tick"}), "targets": ["ctx-ae-1"]},
            }
        ),
        context_id="ctx-ae-1",
        metadata={"user_id": "user-alice"},
    )
    eq = AsyncMock()
    with (
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.simulation_executor.pulses.emit_gateway_message", new=AsyncMock()),
    ):
        await executor.execute(ctx, eq)

    assert redis_store.get("u:user-alice:simreg:context:vertex-ae-1") == b"ctx-ae-1"
    assert redis_store.get("u:user-alice:simreg:session:vertex-ae-1") == b"sim-from-go"
    assert "simreg:session:vertex-ae-1" not in redis_store
    assert "simreg:context:vertex-ae-1" not in redis_store


# ---------------------------------------------------------------------------
# Challenger 1 Gap 2: Callable broadcast non-string text / dict data handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data_field", "expected_text"),
    [
        (json.dumps({"text": {"nested": 1}}), '{"nested": 1}'),
        (json.dumps({"text": 12345}), "12345"),
        ({"event": "tick"}, '{"event": "tick"}'),
        ({"event": "tick", "score": float("nan")}, '{"event": "tick", "score": null}'),
    ],
)
async def test_r9_executor_callable_broadcast_handles_non_string_payloads_safely(
    monkeypatch, data_field, expected_text
):
    monkeypatch.setenv("DISPATCH_MODE", "callable")
    monkeypatch.setenv("SAAS_MODE", "off")

    seen_texts: list[str] = []
    final_event = MagicMock()
    final_event.is_final_response.return_value = True
    final_event.author = "agent"
    final_event.content.parts = [MagicMock(text="done")]

    async def _run_async(*, session_id, user_id, new_message):
        seen_texts.append(new_message.parts[0].text)
        yield final_event

    mock_runner = MagicMock()
    mock_runner.app_name = "simulator"
    mock_runner.app.plugins = []
    mock_runner.run_async = _run_async

    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator", runner=mock_runner)
    executor._session_manager = AsyncMock()
    executor._session_manager.get_or_create_session = AsyncMock(return_value="ctx-bcast")

    # Serialize manually only if data_field is dict with NaN (raw dict in parsed_event)
    raw_event = json.dumps(
        {"type": "broadcast", "payload": {"simulation_id": "s1", "data": data_field}},
        allow_nan=True,
    )
    ctx = _make_exec_context(raw_event, context_id="ctx-bcast")
    eq = AsyncMock()

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_executor.pulses.emit_gateway_message", new=AsyncMock()),
    ):
        await executor.execute(ctx, eq)

    assert seen_texts == [expected_text]
    statuses = [
        c.args[0].status.state for c in eq.enqueue_event.await_args_list if c.args and hasattr(c.args[0], "status")
    ]
    assert TaskState.failed not in statuses


@pytest.mark.asyncio
async def test_r9_executor_callable_broadcast_rejects_unserializable_or_nan_scalar(monkeypatch):
    monkeypatch.setenv("DISPATCH_MODE", "callable")
    monkeypatch.setenv("SAAS_MODE", "off")

    mock_runner = MagicMock()
    mock_runner.app_name = "simulator"
    mock_runner.app.plugins = []
    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator", runner=mock_runner)
    executor._session_manager = AsyncMock()

    # Use parsed dict with NaN float scalar inside inner_event["text"] via json.loads
    nan_payload = '{"type": "broadcast", "payload": {"simulation_id": "s1", "data": "{\\"text\\": NaN}"}}'
    ctx = _make_exec_context(nan_payload, context_id="ctx-nan")
    eq = AsyncMock()

    with patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None):
        await executor.execute(ctx, eq)

    executor._session_manager.get_or_create_session.assert_not_called()
    statuses = [
        c.args[0].status.state for c in eq.enqueue_event.await_args_list if c.args and hasattr(c.args[0], "status")
    ]
    assert TaskState.failed in statuses


# ---------------------------------------------------------------------------
# Challenger 1 Gap 4: RFC-8259 JSON serialization (communication + tools)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r9_a2a_client_sanitizes_nonfinite_floats_in_dict_messages():
    captured: list[A2AMessage] = []

    async def _send_message(msg: A2AMessage):
        captured.append(msg)
        yield A2AMessage(
            message_id="resp-rfc",
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

    res = await client.call_agent(
        "simulator",
        {"action": "execute", "score": float("nan"), "nested": [float("inf"), -float("inf"), 1.5]},
    )
    assert res == "ok"
    part_root = getattr(captured[0].parts[0], "root", captured[0].parts[0])
    raw_text = part_root.text
    assert "NaN" not in raw_text
    assert "Infinity" not in raw_text
    parsed = json.loads(raw_text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert parsed == {"action": "execute", "score": None, "nested": [None, None, 1.5]}


@pytest.mark.asyncio
async def test_r9_submit_plan_to_simulator_emits_strict_rfc8259_json_on_fallback():
    captured_messages: list[str] = []

    async def _fake_call_agent(*, tool_context, agent_name, message, session_id, **kwargs):
        captured_messages.append(message)
        return {"status": "success", "response": "ok"}

    # 1. leadership_planner_with_eval
    lp_ctx = SimpleNamespace(
        invocation_id="inv-lp-nan",
        agent_name="leadership_planner_with_eval",
        session=SimpleNamespace(id="s-lp-nan"),
        state={
            "leadership_scenario": {"title": "Crunch", "stakeholder_count": 4, "drift": float("nan")},
            "simulator_session_id": "sim-lp-nan",
            "_sim_token": "sim-lp-nan",
            "simulation_config": {"stakeholder_count": 4, "weight": float("inf")},
        },
    )
    with (
        patch("agents.utils.communication.call_agent", side_effect=_fake_call_agent),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=False)),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={})),
    ):
        lp_res = await leadership_submit_plan(action="execute", message="Run", tool_context=lp_ctx)  # type: ignore[arg-type]

    assert lp_res["status"] == "success"

    # 2. planner_with_eval
    mp_ctx = SimpleNamespace(
        invocation_id="inv-mp-nan",
        agent_name="planner_with_eval",
        session=SimpleNamespace(id="s-mp-nan"),
        state={
            "marathon_route": {"type": "FeatureCollection", "features": [], "score": float("nan")},
            "traffic_assessment": {"congestion": -float("inf")},
            "simulator_session_id": "sim-mp-nan",
            "_sim_token": "sim-mp-nan",
        },
    )
    with (
        patch("agents.utils.communication.call_agent", side_effect=_fake_call_agent),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=False)),
    ):
        mp_res = await marathon_submit_plan(action="execute", message="Run", tool_context=mp_ctx)  # type: ignore[arg-type]

    assert mp_res["status"] == "success"
    assert len(captured_messages) == 2
    for raw in captured_messages:
        assert "NaN" not in raw
        assert "Infinity" not in raw
        parsed = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        assert isinstance(parsed, dict)
    assert communication.strict_json_dumps({"x": float("nan")}) == '{"x": null}'
