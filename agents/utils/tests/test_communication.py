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

"""Regression tests for A2A user_id propagation in agents/utils/communication.py."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import Message as A2AMessage
from a2a.types import Part, Role, TextPart

from agents.utils.communication import SimulationA2AClient, call_agent


@pytest.fixture(autouse=True)
def _clean_saas_env(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("ACCOUNT_KEY_PREFIX", raising=False)


def _mock_remote_agent(captured_messages: list[A2AMessage]) -> MagicMock:
    async def _send_message(msg: A2AMessage):
        captured_messages.append(msg)
        yield A2AMessage(
            message_id="resp-1",
            parts=[Part(root=TextPart(text="ok"))],
            role=Role.agent,
        )

    mock_a2a = MagicMock()
    mock_a2a.send_message = _send_message

    remote = MagicMock()
    remote._ensure_resolved = AsyncMock()
    remote._a2a_client = mock_a2a
    return remote


@pytest.mark.asyncio
async def test_client_call_agent_attaches_metadata_from_explicit_user_id():
    captured: list[A2AMessage] = []
    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock(return_value=_mock_remote_agent(captured))

    res = await client.call_agent("simulator", "hello", user_id="  user-alice  ")
    assert res == "ok"
    assert len(captured) == 1
    assert captured[0].metadata == {"user_id": "user-alice"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_user"),
    [
        ({"action": "execute", "user_id": "user-bob"}, "user-bob"),
        ({"action": "execute", "acting_user_id": "user-carol"}, "user-carol"),
        ({"action": "execute", "userId": "user-dave"}, "user-dave"),
        ({"type": "broadcast", "payload": {"acting_user_id": "user-erin"}}, "user-erin"),
    ],
)
async def test_client_call_agent_extracts_user_id_from_dict_and_json_str(payload, expected_user):
    # Dict payload
    captured_dict: list[A2AMessage] = []
    client_dict = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client_dict.get_agent = AsyncMock(return_value=_mock_remote_agent(captured_dict))
    await client_dict.call_agent("simulator", payload)
    assert captured_dict[0].metadata == {"user_id": expected_user}

    # JSON string payload
    captured_str: list[A2AMessage] = []
    client_str = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client_str.get_agent = AsyncMock(return_value=_mock_remote_agent(captured_str))
    await client_str.call_agent("simulator", json.dumps(payload))
    assert captured_str[0].metadata == {"user_id": expected_user}


@pytest.mark.asyncio
async def test_client_call_agent_saas_off_omits_metadata_when_unauthenticated():
    captured: list[A2AMessage] = []
    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock(return_value=_mock_remote_agent(captured))

    res = await client.call_agent("simulator", "plain message")
    assert res == "ok"
    assert captured[0].metadata is None

    # Anonymous / whitespace values in SAAS_MODE=off also remain omitted
    captured2: list[A2AMessage] = []
    client.get_agent = AsyncMock(return_value=_mock_remote_agent(captured2))
    await client.call_agent("simulator", "plain message", user_id="simulation_user")
    assert captured2[0].metadata is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("msg", "uid"),
    [
        ("plain message", None),
        ("plain message", ""),
        ("plain message", "   "),
        ("plain message", "simulation_user"),
        ("plain message", "anonymous"),
        ("plain message", "user:colon"),
        ({"action": "execute", "user_id": "simulation_user"}, None),
        ({"action": "execute", "user_id": "bad:user"}, None),
    ],
)
async def test_client_call_agent_saas_on_fails_closed_on_missing_or_invalid_user_id(monkeypatch, msg, uid):
    monkeypatch.setenv("SAAS_MODE", "on")
    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock()

    with pytest.raises(ValueError, match="user_id"):
        await client.call_agent("simulator", msg, user_id=uid)
    client.get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_call_agent_saas_on_with_valid_user_id_succeeds(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    captured: list[A2AMessage] = []
    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock(return_value=_mock_remote_agent(captured))

    res = await client.call_agent("simulator", '{"action":"execute"}', user_id="user-alice")
    assert res == "ok"
    assert captured[0].metadata == {"user_id": "user-alice"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ctx_factory", "expected_user"),
    [
        (
            lambda: SimpleNamespace(
                invocation_id="iid-1",
                agent_name="planner",
                session=SimpleNamespace(id="s1"),
                state={"user_id": "user-state"},
            ),
            "user-state",
        ),
        (
            lambda: SimpleNamespace(
                invocation_id="iid-2",
                agent_name="planner",
                session=SimpleNamespace(id="s2"),
                state={"acting_user_id": "user-acting"},
            ),
            "user-acting",
        ),
        (
            lambda: SimpleNamespace(
                invocation_id="iid-3",
                agent_name="planner",
                session=SimpleNamespace(id="s3"),
                state={"userId": "user-camel"},
            ),
            "user-camel",
        ),
        (
            lambda: SimpleNamespace(
                invocation_id="iid-4",
                agent_name="planner",
                session=SimpleNamespace(id="s4"),
                state={"tenant": {"user_id": "user-tenant"}},
            ),
            "user-tenant",
        ),
        (
            lambda: SimpleNamespace(
                invocation_id="iid-5",
                agent_name="planner",
                session=SimpleNamespace(id="s5"),
                state={},
                _invocation_context=SimpleNamespace(user_id="user-inv-ctx"),
            ),
            "user-inv-ctx",
        ),
        (
            lambda: SimpleNamespace(
                invocation_id="iid-6",
                agent_name="planner",
                session=SimpleNamespace(id="s6", user_id="user-sess"),
                state={},
            ),
            "user-sess",
        ),
    ],
)
async def test_tool_call_agent_propagates_user_id_from_tool_context(monkeypatch, ctx_factory, expected_user):
    monkeypatch.setenv("SAAS_MODE", "on")
    captured: list[A2AMessage] = []
    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock(return_value=_mock_remote_agent(captured))

    ctx = ctx_factory()
    with (
        patch("agents.utils.communication_plugin.get_client", return_value=client),
        patch("agents.utils.simulation_registry.get_context_id", new=AsyncMock(return_value=None)),
        patch("agents.utils.communication.emit_inter_agent_pulse", new=AsyncMock()),
    ):
        res = await call_agent("simulator", '{"action":"execute"}', ctx, session_id="sim-1")

    assert res["status"] == "success"
    assert len(captured) == 1
    assert captured[0].metadata == {"user_id": expected_user}


@pytest.mark.asyncio
async def test_tool_call_agent_saas_on_rejects_missing_or_anonymous_user_id(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock()

    ctx = SimpleNamespace(
        invocation_id="iid-err",
        agent_name="planner",
        session=SimpleNamespace(id="s-err", user_id="anonymous"),
        state={"user_id": "simulation_user"},
    )
    with (
        patch("agents.utils.communication_plugin.get_client", return_value=client),
        patch("agents.utils.communication.emit_inter_agent_pulse", new=AsyncMock()) as mock_pulse,
    ):
        res = await call_agent("simulator", "hello", ctx)

    assert res["status"] == "error"
    assert "user_id" in res["message"]
    client.get_agent.assert_not_awaited()
    mock_pulse.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_call_agent_context_user_id_prevents_kwarg_spoofing(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    captured: list[A2AMessage] = []
    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock(return_value=_mock_remote_agent(captured))

    ctx = SimpleNamespace(
        invocation_id="iid-auth",
        agent_name="planner",
        session=SimpleNamespace(id="s-auth", user_id="verified-user"),
        state={},
    )
    with (
        patch("agents.utils.communication_plugin.get_client", return_value=client),
        patch("agents.utils.simulation_registry.get_context_id", new=AsyncMock(return_value=None)),
        patch("agents.utils.communication.emit_inter_agent_pulse", new=AsyncMock()),
    ):
        res = await call_agent("simulator", "hello", ctx, user_id="spoofed-user")

    assert res["status"] == "success"
    assert captured[0].metadata == {"user_id": "verified-user"}


@pytest.mark.asyncio
async def test_e2e_a2a_client_to_simulation_executor_and_session_manager(monkeypatch):
    """End-to-end Python-to-Python A2A propagation writes tenant-scoped session_map keys in SAAS_MODE=on."""
    from a2a.server.agent_execution import RequestContext
    from a2a.types import MessageSendParams, TaskState
    from agents.utils.session_manager import SessionManager
    from agents.utils.simulation_executor import SimulationExecutor

    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCH_MODE", "callable")

    redis_store: dict[str, bytes] = {}
    mock_redis = AsyncMock()

    async def _redis_get(key):
        return redis_store.get(key)

    async def _redis_set(key, value, ex=None, nx=False):
        if nx and key in redis_store:
            return False
        redis_store[key] = value if isinstance(value, bytes) else value.encode()
        return True

    mock_redis.get = AsyncMock(side_effect=_redis_get)
    mock_redis.set = AsyncMock(side_effect=_redis_set)

    created_sessions: list[tuple[str, str]] = []

    async def _create_session(*, app_name, user_id):
        sid = f"vertex-{user_id}-{len(created_sessions) + 1}"
        created_sessions.append((app_name, user_id))
        return SimpleNamespace(id=sid)

    mock_session_service = AsyncMock()
    mock_session_service.create_session = AsyncMock(side_effect=_create_session)

    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator")
    executor._dispatch_mode = "callable"

    final_event = MagicMock()
    final_event.is_final_response.return_value = True
    final_event.author = "agent"
    final_event.content.parts = [MagicMock(text="Simulation complete.")]

    observed_run_users: list[str] = []

    async def _run_async(*, session_id, user_id, new_message):
        observed_run_users.append(user_id)
        yield final_event

    mock_runner = MagicMock()
    mock_runner.app_name = "simulator"
    mock_runner.app.plugins = []
    mock_runner.run_async = _run_async
    executor._runner = mock_runner
    executor._session_manager = SessionManager(session_service=mock_session_service)

    async def _bridge_send_message(a2a_msg: A2AMessage):
        req_ctx = RequestContext(
            request=MessageSendParams(message=a2a_msg),
            context_id=a2a_msg.context_id,
            task_id="task-e2e-1",
        )
        eq = AsyncMock()
        with (
            patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=mock_redis),
            patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis),
            patch("agents.utils.simulation_executor.simulation_registry.register", new=AsyncMock()),
            patch("agents.utils.simulation_executor.simulation_registry.lookup", new=AsyncMock(return_value=None)),
            patch("agents.utils.simulation_executor.simulation_registry.register_context", new=AsyncMock()),
            patch("agents.utils.simulation_executor.pulses.emit_gateway_message", new=AsyncMock()),
        ):
            await executor.execute(req_ctx, eq)

        statuses = [
            call.args[0].status.state
            for call in eq.enqueue_event.await_args_list
            if call.args and hasattr(call.args[0], "status")
        ]
        assert TaskState.failed not in statuses
        yield A2AMessage(
            message_id="resp-e2e-1",
            parts=[Part(root=TextPart(text="Simulation complete."))],
            role=Role.agent,
        )

    mock_a2a = MagicMock()
    mock_a2a.send_message = _bridge_send_message
    mock_remote = MagicMock()
    mock_remote._ensure_resolved = AsyncMock()
    mock_remote._a2a_client = mock_a2a

    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock(return_value=mock_remote)

    res = await client.call_agent(
        "simulator",
        {"action": "execute", "simulation_id": "sim-e2e-1"},
        session_id="ctx-e2e-py",
        user_id="user-alice",
    )

    assert res == "Simulation complete."
    assert "session_map:user-alice:ctx-e2e-py" in redis_store
    assert "session_map:simulation_user:ctx-e2e-py" not in redis_store
    assert created_sessions == [("simulator", "user-alice")]
    assert observed_run_users == ["user-alice"]
