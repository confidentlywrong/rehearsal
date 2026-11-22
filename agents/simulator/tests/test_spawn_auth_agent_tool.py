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

"""Adversarial regression tests for spawn_runners auth propagation across ADK AgentTool child sessions."""

from __future__ import annotations

import importlib.util
import json
import pathlib
from types import SimpleNamespace
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.types import Message as A2AMessage
from a2a.types import MessageSendParams, Part, Role, TextPart
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from agents.utils import simulation_registry, user_auth_context
from agents.utils.session_manager import SessionManager
from agents.utils.simulation_executor import SimulationExecutor

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _load_preparing_tools():
    path = _REPO_ROOT / "agents" / "simulator" / "skills" / "preparing-the-race" / "tools.py"
    spec = importlib.util.spec_from_file_location("test_spawn_auth_preparing_tools", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PREP_TOOLS = _load_preparing_tools()
prepare_simulation = _PREP_TOOLS.prepare_simulation
spawn_runners = _PREP_TOOLS.spawn_runners


@pytest.fixture(autouse=True)
def _clean_auth_and_reg(monkeypatch):
    for var in ("SAAS_MODE", "DISPATCHER_USER_ID", "ACCOUNT_KEY_PREFIX", "GATEWAY_INTERNAL_URL"):
        monkeypatch.delenv(var, raising=False)
    user_auth_context._reset_for_tests()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()
    yield
    user_auth_context._reset_for_tests()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()


def _make_aiohttp_spawn_mock(captured_posts: list[dict]):
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={"sessions": [{"sessionId": "r-child-1", "agentType": "leadership_runner_autopilot"}]}
    )
    post_cm = AsyncMock()
    post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    post_cm.__aexit__ = AsyncMock(return_value=False)

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    def _post(url, json=None, headers=None):
        captured_posts.append({"url": url, "json": json, "headers": dict(headers or {})})
        return post_cm

    mock_http.post = MagicMock(side_effect=_post)
    return mock_http


class _ChildPipelineAgent(BaseAgent):
    """Sub-agent run inside ADK AgentTool that calls prepare_simulation + spawn_runners."""

    captured_child_sids: list[str] = []
    plan_json: str = "{}"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        tc = ToolContext(ctx)
        self.captured_child_sids.append(str(tc.session.id))
        prep_res = await prepare_simulation(self.plan_json, tc)
        assert prep_res["status"] == "success", prep_res
        spawn_res = await spawn_runners(1, tc)
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part.from_text(text=json.dumps(spawn_res))]),
        )


class _ParentSimulatorAgent(BaseAgent):
    """Parent agent that invokes _ChildPipelineAgent via real google.adk.tools.agent_tool.AgentTool."""

    child_agent: BaseAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        tc = ToolContext(ctx)
        agent_tool = AgentTool(agent=self.child_agent, skip_summarization=True)
        result = await agent_tool.run_async(args={"request": "run"}, tool_context=tc)
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part.from_text(text=str(result))]),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("oidc_token", "expected_headers"),
    [
        (
            None,
            {
                "Authorization": "Bearer firebase-jwt-alice-abcdef",
                "Cookie": "__session=firebase-jwt-alice-abcdef",
            },
        ),
        (
            "oidc-service-tok",
            {
                "Authorization": "Bearer oidc-service-tok",
                "X-User-Authorization": "Bearer firebase-jwt-alice-abcdef",
                "Cookie": "__session=firebase-jwt-alice-abcdef",
            },
        ),
    ],
)
async def test_spawn_runners_forwards_user_token_across_real_agent_tool_child_session(
    monkeypatch,
    oidc_token,
    expected_headers,
):
    """Real AgentTool creates child_sid != parent_sid; spawn_runners still attaches user auth headers."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:9101")

    parent_sid = "parent-vertex-session-123"
    context_sid = "parent-spawn-context-456"
    user_token = "firebase-jwt-alice-abcdef"
    user_auth_context.set_session_token(parent_sid, user_token)
    user_auth_context.set_session_token(context_sid, user_token)

    plan_json = json.dumps(
        {
            "action": "execute",
            "narrative": "Launch AI governance rollout",
            "quest_id": "ai_powered_disruptor",
            "simulation_config": {"runner_count": 1, "duration_seconds": 30, "tick_interval_seconds": 5},
        }
    )
    child_agent = _ChildPipelineAgent(name="simulation_pipeline", plan_json=plan_json, captured_child_sids=[])
    parent_agent = _ParentSimulatorAgent(name="simulator", child_agent=child_agent)

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="simulator",
        user_id="alice",
        session_id=parent_sid,
        state={"simulation_id": context_sid, "user_id": "alice"},
    )
    runner = Runner(app_name="simulator", agent=parent_agent, session_service=session_service)

    captured_posts: list[dict] = []
    mock_http = _make_aiohttp_spawn_mock(captured_posts)

    final_texts: list[str] = []
    with (
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={})),
        patch("aiohttp.ClientSession", return_value=mock_http),
        patch("agents.utils.auth.get_id_token", return_value=oidc_token),
        patch("agents.utils.auth.resolve_audience", return_value="https://gateway.run.app" if oidc_token else None),
    ):
        async for ev in runner.run_async(
            user_id="alice",
            session_id=parent_sid,
            new_message=types.Content(role="user", parts=[types.Part.from_text(text="start")]),
        ):
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if part.text:
                        final_texts.append(part.text)

    assert len(child_agent.captured_child_sids) == 1
    child_sid = child_agent.captured_child_sids[0]
    assert child_sid != parent_sid
    assert child_sid != context_sid
    assert user_auth_context.get_session_token(child_sid) is None

    assert len(captured_posts) == 1
    assert captured_posts[0]["url"] == "http://127.0.0.1:9101/api/v1/spawn"
    assert captured_posts[0]["headers"] == expected_headers
    assert json.loads(final_texts[-1])["status"] == "success"


@pytest.mark.asyncio
async def test_spawn_runners_resolves_token_via_registry_context_and_respects_tenant_isolation(monkeypatch):
    """spawn_runners resolves token via simulation_registry.get_context_id(simulation_id) per tenant."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:9101")

    # Token only bound on Bob's context should NOT leak to Alice; Alice's vertex->context mapping wins.
    await simulation_registry.register_context("ae-sim-40765115", "ctx-bob-only", user_id="bob")
    user_auth_context.set_session_token("ctx-bob-only", "tok-bob-secret")

    alice_ctx = MagicMock()
    alice_ctx.session = SimpleNamespace(id="agent-tool-random-child-uuid", user_id="alice")
    alice_ctx.state = {
        "simulation_ready": True,
        "simulation_id": "ae-sim-40765115",
        "user_id": "alice",
    }

    captured_posts: list[dict] = []
    mock_http = _make_aiohttp_spawn_mock(captured_posts)
    with (
        patch("aiohttp.ClientSession", return_value=mock_http),
        patch("agents.utils.auth.get_id_token", return_value=None),
    ):
        res_no_alice_ctx = await spawn_runners(1, alice_ctx)
    assert res_no_alice_ctx["status"] == "success"
    assert captured_posts[-1]["headers"] == {}

    # Now register Alice's context mapping and bind Alice's token on the resolved spawn context UUID.
    await simulation_registry.register_context("ae-sim-40765115", "ctx-alice-spawn", user_id="alice")
    user_auth_context.set_session_token("ctx-alice-spawn", "tok-alice-jwt")

    captured_posts.clear()
    with (
        patch("aiohttp.ClientSession", return_value=mock_http),
        patch("agents.utils.auth.get_id_token", return_value=None),
    ):
        res_with_alice_ctx = await spawn_runners(1, alice_ctx)
    assert res_with_alice_ctx["status"] == "success"
    assert captured_posts[-1]["headers"] == {
        "Authorization": "Bearer tok-alice-jwt",
        "Cookie": "__session=tok-alice-jwt",
    }


@pytest.mark.asyncio
async def test_simulation_executor_binds_and_restores_simulation_id_token_across_agent_tool(monkeypatch):
    """SimulationExecutor binds distinct simulation_id during run_async and restores/clears in finally."""
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("DISPATCH_MODE", "callable")
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:9101")

    user_auth_context.set_session_token("sim-existing-prev", "tok-previous")

    captured_posts: list[dict] = []
    mock_http = _make_aiohttp_spawn_mock(captured_posts)

    async def _run_async_ok(*, session_id, user_id, new_message):
        # Simulate AgentTool child session where session.id is random, state["simulation_id"] is sim-distinct-1
        child_ctx = MagicMock()
        child_ctx.session = SimpleNamespace(id="random-child-session-uuid", user_id=user_id)
        child_ctx.state = {
            "simulation_ready": True,
            "simulation_id": "sim-distinct-1",
            "user_id": user_id,
        }
        with (
            patch("aiohttp.ClientSession", return_value=mock_http),
            patch("agents.utils.auth.get_id_token", return_value=None),
        ):
            res = await spawn_runners(1, child_ctx)
        assert res["status"] == "success"
        assert user_auth_context.get_session_token("sim-distinct-1") == "tok-alice-exec"

        ev = MagicMock()
        ev.is_final_response.return_value = True
        ev.author = "agent"
        ev.content.parts = [MagicMock(text="Simulation complete")]
        yield ev

    mock_runner = MagicMock()
    mock_runner.app_name = "simulator"
    mock_runner.app.plugins = []
    mock_runner.run_async = _run_async_ok

    mock_session_service = AsyncMock()
    mock_session_service.create_session = AsyncMock(return_value=SimpleNamespace(id="vertex-exec-sim"))

    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator", runner=mock_runner)
    executor._session_manager = SessionManager(session_service=mock_session_service)

    msg = A2AMessage(
        message_id="m-exec-1",
        parts=[Part(root=TextPart(text='{"action":"execute","simulation_id":"sim-distinct-1"}'))],
        role=Role.user,
        context_id="ctx-exec-sim",
        metadata={"user_id": "alice", "user_auth_token": "tok-alice-exec"},
    )
    req_ctx = RequestContext(request=MessageSendParams(message=msg), context_id="ctx-exec-sim", task_id="task-1")

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
        patch("agents.utils.session_manager.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_executor.simulation_registry.register", new=AsyncMock()),
        patch("agents.utils.simulation_executor.simulation_registry.lookup", new=AsyncMock(return_value=None)),
        patch("agents.utils.simulation_executor.simulation_registry.register_context", new=AsyncMock()),
        patch("agents.utils.simulation_executor.pulses.emit_gateway_message", new=AsyncMock()),
    ):
        await executor.execute(req_ctx, AsyncMock())

    assert captured_posts[0]["headers"] == {
        "Authorization": "Bearer tok-alice-exec",
        "Cookie": "__session=tok-alice-exec",
    }
    assert user_auth_context.get_session_token("sim-distinct-1") is None
    assert user_auth_context.get_session_token("vertex-exec-sim") is None
    assert user_auth_context.get_session_token("ctx-exec-sim") is None

    # Also verify pre-existing simulation_id token is restored (not deleted) on error
    async def _run_async_fail(*, session_id, user_id, new_message):
        assert user_auth_context.get_session_token("sim-existing-prev") == "tok-new-run"
        raise RuntimeError("boom")
        yield  # pragma: no cover

    mock_runner.run_async = _run_async_fail
    msg_err = A2AMessage(
        message_id="m-exec-2",
        parts=[Part(root=TextPart(text='{"action":"execute","simulation_id":"sim-existing-prev"}'))],
        role=Role.user,
        context_id="ctx-exec-err2",
        metadata={"user_id": "alice", "user_auth_token": "tok-new-run"},
    )
    req_ctx_err = RequestContext(
        request=MessageSendParams(message=msg_err), context_id="ctx-exec-err2", task_id="task-2"
    )
    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
        patch("agents.utils.session_manager.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_executor.simulation_registry.register", new=AsyncMock()),
        patch("agents.utils.simulation_executor.simulation_registry.lookup", new=AsyncMock(return_value=None)),
        patch("agents.utils.simulation_executor.simulation_registry.register_context", new=AsyncMock()),
    ):
        await executor.execute(req_ctx_err, AsyncMock())

    assert user_auth_context.get_session_token("sim-existing-prev") == "tok-previous"
    assert user_auth_context.get_session_token("ctx-exec-err2") is None
