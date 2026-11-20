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

"""Regression tests for user_id propagation in planner_with_eval.submit_plan_to_simulator."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import Message as A2AMessage
from a2a.types import Part, Role, TextPart

from agents.planner_with_eval.tools import submit_plan_to_simulator
from agents.utils.communication import SimulationA2AClient


@pytest.fixture(autouse=True)
def _clean_saas_env(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("ACCOUNT_KEY_PREFIX", raising=False)


def _base_state(extra: dict | None = None) -> dict:
    state = {
        "marathon_route": {"type": "FeatureCollection", "features": []},
        "simulator_session_id": "sim-pe-1",
        "_sim_token": "sim-pe-1",
    }
    if extra:
        state.update(extra)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("saas_mode", ["off", "on"])
@pytest.mark.parametrize(
    ("ctx_kwargs_factory", "expected_user"),
    [
        (lambda: {"state": _base_state({"user_id": "user-alice"})}, "user-alice"),
        (lambda: {"state": _base_state({"acting_user_id": "user-bob"})}, "user-bob"),
        (lambda: {"state": _base_state({"userId": "user-carol"})}, "user-carol"),
        (lambda: {"state": _base_state({"tenant": {"user_id": "user-dave"}})}, "user-dave"),
        (
            lambda: {
                "state": _base_state(),
                "_invocation_context": SimpleNamespace(user_id="user-erin"),
            },
            "user-erin",
        ),
    ],
)
async def test_submit_plan_propagates_user_id_to_a2a_metadata_and_payload(
    monkeypatch, saas_mode, ctx_kwargs_factory, expected_user
):
    """End-to-end submit_plan_to_simulator -> call_agent -> SimulationA2AClient attaches A2AMessage.metadata['user_id']."""
    monkeypatch.setenv("SAAS_MODE", saas_mode)

    captured_messages: list[A2AMessage] = []

    async def _send_message(msg: A2AMessage):
        captured_messages.append(msg)
        yield A2AMessage(
            message_id="resp-1",
            parts=[Part(root=TextPart(text="Simulation acknowledged"))],
            role=Role.agent,
        )

    mock_a2a = MagicMock()
    mock_a2a.send_message = _send_message
    mock_remote = MagicMock()
    mock_remote._ensure_resolved = AsyncMock()
    mock_remote._a2a_client = mock_a2a

    client = SimulationA2AClient(gateway_url="http://127.0.0.1:9101")
    client.get_agent = AsyncMock(return_value=mock_remote)

    ctx = SimpleNamespace(
        invocation_id=f"inv-pe-uid-{saas_mode}-{expected_user}",
        agent_name="planner_with_eval",
        session=SimpleNamespace(id="planner-sess-1"),
        **ctx_kwargs_factory(),
    )

    with (
        patch("agents.utils.communication_plugin.get_client", return_value=client),
        patch("agents.utils.simulation_registry.get_context_id", new=AsyncMock(return_value=None)),
        patch("agents.utils.communication.emit_inter_agent_pulse", new=AsyncMock()),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
    ):
        res = await submit_plan_to_simulator(
            action="execute",
            message="Run marathon",
            tool_context=ctx,  # type: ignore[arg-type]
        )

    assert res["status"] == "success"
    assert len(captured_messages) == 1
    msg = captured_messages[0]
    assert msg.metadata == {"user_id": expected_user}
    part_root = getattr(msg.parts[0], "root", msg.parts[0])
    sent_payload = json.loads(part_root.text)
    assert sent_payload["user_id"] == expected_user


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["execute", "verify"])
@pytest.mark.parametrize(
    ("extra_state", "session_uid"),
    [
        ({}, None),
        ({"user_id": ""}, None),
        ({"user_id": "   "}, None),
        ({"user_id": "simulation_user"}, None),
        ({"user_id": "anonymous"}, None),
        ({"user_id": "user:colon"}, None),
        ({}, "simulation_user"),
        ({}, "anonymous"),
    ],
)
async def test_submit_plan_saas_on_fails_closed_without_valid_user_id(
    monkeypatch, action, extra_state, session_uid
):
    monkeypatch.setenv("SAAS_MODE", "on")
    ctx = SimpleNamespace(
        invocation_id="inv-pe-reject",
        agent_name="planner_with_eval",
        session=SimpleNamespace(id="planner-sess-reject", user_id=session_uid),
        state=_base_state(extra_state),
    )
    mock_store = AsyncMock(return_value=True)
    mock_call = AsyncMock()

    with (
        patch("agents.utils.simdata.store_simulation_data", mock_store),
        patch("agents.utils.communication.call_agent", mock_call),
    ):
        res = await submit_plan_to_simulator(
            action=action,
            message="Run marathon",
            tool_context=ctx,  # type: ignore[arg-type]
        )

    assert res["status"] == "error"
    assert "user_id" in res["message"]
    mock_store.assert_not_awaited()
    mock_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_plan_saas_off_preserves_anonymous_compatibility(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "off")
    ctx = SimpleNamespace(
        invocation_id="inv-pe-compat",
        agent_name="planner_with_eval",
        session=SimpleNamespace(id="planner-sess-compat", user_id="anonymous"),
        state=_base_state(),
    )

    async def strict_call_agent(*, tool_context, agent_name, message, session_id):
        payload = json.loads(message)
        assert "user_id" not in payload
        return {"status": "success", "response": "ok"}

    with (
        patch("agents.utils.communication.call_agent", side_effect=strict_call_agent),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
    ):
        res = await submit_plan_to_simulator(
            action="execute",
            message="Run marathon",
            tool_context=ctx,  # type: ignore[arg-type]
        )

    assert res["status"] == "success"
