# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

"""Tests for dispatcher suppress_gateway_emission flag."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.utils.dispatcher import RedisOrchestratorDispatcher


def _make_mock_runner(agent_name: str = "test_runner") -> MagicMock:
    runner = MagicMock()
    runner.app = MagicMock()
    runner.app.name = agent_name
    runner.app.root_agent = MagicMock()
    runner.app.root_agent.name = agent_name
    return runner


def test_dispatcher_default_does_not_suppress():
    """By default, suppress_gateway_emission should be False."""
    runner = _make_mock_runner()
    d = RedisOrchestratorDispatcher(runner=runner, dispatch_mode="callable")
    assert d.suppress_gateway_emission is False


def test_dispatcher_accepts_suppress_flag():
    """Dispatcher should accept suppress_gateway_emission=True."""
    runner = _make_mock_runner()
    d = RedisOrchestratorDispatcher(runner=runner, dispatch_mode="callable", suppress_gateway_emission=True)
    assert d.suppress_gateway_emission is True


@pytest.mark.asyncio
async def test_dispatcher_suppresses_gateway_emission():
    """When suppress_gateway_emission=True, no gateway messages should be emitted."""
    runner = _make_mock_runner("runner_test")

    # Create a mock event with a function_response part (simulating process_tick result)
    mock_event = MagicMock()
    mock_event.author = "runner_test"
    mock_part = MagicMock()
    mock_part.text = None
    mock_part.function_call = None
    mock_part.function_response = MagicMock()
    mock_part.function_response.response = {"status": "success", "tick": 1}
    mock_event.content = MagicMock()
    mock_event.content.parts = [mock_part]

    # Make run_async return our mock event
    async def mock_run_async(**kwargs):
        yield mock_event

    runner.run_async = mock_run_async

    d = RedisOrchestratorDispatcher(runner=runner, dispatch_mode="callable", suppress_gateway_emission=True)
    d.session_simulation_map["test-session"] = "sim-001"

    with patch("agents.utils.pulses.emit_gateway_message", new_callable=AsyncMock) as mock_emit:
        content = MagicMock()
        await d._trigger_agent_run_logic("test-session", content)
        # Gateway emission should NOT have been called
        mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_emits_when_not_suppressed():
    """When suppress_gateway_emission=False, gateway messages should be emitted."""
    runner = _make_mock_runner("runner_test")

    mock_event = MagicMock()
    mock_event.author = "runner_test"
    mock_part = MagicMock()
    mock_part.text = "I finished the race."
    mock_part.function_call = None
    mock_part.function_response = None
    mock_event.content = MagicMock()
    mock_event.content.parts = [mock_part]

    async def mock_run_async(**kwargs):
        yield mock_event

    runner.run_async = mock_run_async

    d = RedisOrchestratorDispatcher(runner=runner, dispatch_mode="callable", suppress_gateway_emission=False)
    d.session_simulation_map["test-session"] = "sim-001"

    with patch("agents.utils.pulses.emit_gateway_message", new_callable=AsyncMock) as mock_emit:
        content = MagicMock()
        await d._trigger_agent_run_logic("test-session", content)
        # Gateway emission SHOULD have been called
        mock_emit.assert_called()


def test_dispatcher_prepares_pulses_suppresses_raw_json_tool_calls():
    """Verify _prepare_pulses suppresses raw function call JSON like validate_and_emit_a2ui."""
    runner = _make_mock_runner("planner_test")
    d = RedisOrchestratorDispatcher(runner=runner, dispatch_mode="callable")
    d.allowed_authors.add("planner_test")

    # 1. Raw JSON tool call should be suppressed
    raw_tool_json = '{"name": "validate_and_emit_a2ui", "parameters": {"payload": "dashboard"}}'
    mock_part_tool = MagicMock()
    mock_part_tool.text = raw_tool_json
    mock_part_tool.function_call = None
    mock_part_tool.function_response = None

    mock_event_tool = MagicMock()
    mock_event_tool.author = "planner_test"
    mock_event_tool.content = MagicMock()
    mock_event_tool.content.parts = [mock_part_tool]

    payloads = d._prepare_pulses("test-session", mock_event_tool)
    assert payloads == [], f"Expected raw tool call JSON to be suppressed, got: {payloads}"

    # 2. Markdown-wrapped JSON tool call should also be suppressed
    wrapped_json = '```json\n{"function": "validate_and_emit_a2ui", "args": {}}\n```'
    mock_part_wrapped = MagicMock()
    mock_part_wrapped.text = wrapped_json
    mock_part_wrapped.function_call = None
    mock_part_wrapped.function_response = None

    mock_event_wrapped = MagicMock()
    mock_event_wrapped.author = "planner_test"
    mock_event_wrapped.content = MagicMock()
    mock_event_wrapped.content.parts = [mock_part_wrapped]

    payloads_wrapped = d._prepare_pulses("test-session", mock_event_wrapped)
    assert payloads_wrapped == [], f"Expected wrapped tool call JSON to be suppressed, got: {payloads_wrapped}"

    # 3. Single-quoted JSON tool call should also be suppressed
    single_quoted_json = "{'name': 'validate_and_emit_a2ui', 'parameters': {}}"
    mock_part_sq = MagicMock()
    mock_part_sq.text = single_quoted_json
    mock_part_sq.function_call = None
    mock_part_sq.function_response = None

    mock_event_sq = MagicMock()
    mock_event_sq.author = "planner_test"
    mock_event_sq.content = MagicMock()
    mock_event_sq.content.parts = [mock_part_sq]

    payloads_sq = d._prepare_pulses("test-session", mock_event_sq)
    assert payloads_sq == [], f"Expected single-quoted tool call JSON to be suppressed, got: {payloads_sq}"

    # 4. Unquoted key JSON tool call should also be suppressed
    unquoted_json = '{function: "validate_and_emit_a2ui", args: {}}'
    mock_part_uq = MagicMock()
    mock_part_uq.text = unquoted_json
    mock_part_uq.function_call = None
    mock_part_uq.function_response = None

    mock_event_uq = MagicMock()
    mock_event_uq.author = "planner_test"
    mock_event_uq.content = MagicMock()
    mock_event_uq.content.parts = [mock_part_uq]

    payloads_uq = d._prepare_pulses("test-session", mock_event_uq)
    assert payloads_uq == [], f"Expected unquoted key tool call JSON to be suppressed, got: {payloads_uq}"

    # 5. Conversational prose should NOT be suppressed
    mock_part_prose = MagicMock()
    mock_part_prose.text = "I have developed the stakeholder strategy."
    mock_part_prose.function_call = None
    mock_part_prose.function_response = None

    mock_event_prose = MagicMock()
    mock_event_prose.author = "planner_test"
    mock_event_prose.content = MagicMock()
    mock_event_prose.content.parts = [mock_part_prose]

    payloads_prose = d._prepare_pulses("test-session", mock_event_prose)
    assert payloads_prose == ["I have developed the stakeholder strategy."]

