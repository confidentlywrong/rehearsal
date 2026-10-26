# Copyright 2026 The Confidently Wrong Authors
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

"""Final-response relay in SimulationExecutor.

The simulator wraps its pipeline in ``AgentTool(skip_summarization=True)``.
ADK then marks the *function-response* event as the final response, so the
event carries a ``function_response`` part instead of text. The executor must
relay that tool result; otherwise every execute run reports
"simulator failed to generate response".
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.types import TaskState
from google.adk.events import Event, EventActions
from google.genai import types

from agents.utils.simulation_executor import SimulationExecutor, _final_response_text


def _skip_summarization_event(result) -> Event:
    return Event(
        author="simulator",
        invocation_id="inv-1",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="simulation_pipeline",
                        response={"result": result},
                    )
                )
            ],
        ),
        actions=EventActions(skip_summarization=True),
    )


def _text_event(text: str) -> Event:
    return Event(
        author="simulator",
        invocation_id="inv-1",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


def test_skip_summarization_event_is_final_response():
    """Guards the ADK behaviour the executor relies on."""
    assert _skip_summarization_event("debrief").is_final_response()


def test_text_parts_are_returned():
    assert _final_response_text(_text_event("Simulation done.")) == "Simulation done."


def test_function_response_result_is_returned():
    assert _final_response_text(_skip_summarization_event("# Debrief\nAll good")) == "# Debrief\nAll good"


def test_structured_function_response_result_is_json():
    assert _final_response_text(_skip_summarization_event({"status": "ok"})) == '{"status": "ok"}'


@pytest.mark.parametrize("result", ["", "   ", None])
def test_empty_function_response_result_yields_empty_text(result):
    assert _final_response_text(_skip_summarization_event(result)) == ""


def test_event_without_content_yields_empty_text():
    assert _final_response_text(Event(author="simulator", invocation_id="inv-1")) == ""


async def _run_executor_with_final_event(final_event: Event):
    runner = MagicMock()
    runner.app_name = "simulator"

    async def _run(*args, **kwargs):
        yield final_event

    runner.run_async = MagicMock(side_effect=_run)
    executor = SimulationExecutor(agent_getter=lambda: MagicMock(), agent_name="simulator", runner=runner)
    executor._session_manager = AsyncMock()
    executor._session_manager.get_or_create_session.return_value = "ctx-1"

    context = MagicMock(spec=RequestContext)
    context.context_id = "ctx-1"
    context.task_id = "task-1"
    context.get_user_input.return_value = "plain request"

    with (
        patch("agents.utils.simulation_executor.TaskUpdater") as MockUpdater,
        patch("agents.utils.simulation_executor.pulses") as mock_pulses,
        patch("agents.utils.simulation_executor.simulation_registry") as mock_registry,
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
    ):
        updater = MockUpdater.return_value
        updater.submit = AsyncMock()
        updater.start_work = AsyncMock()
        updater.update_status = AsyncMock()
        updater.complete = AsyncMock()
        updater.add_artifact = AsyncMock()
        mock_pulses.emit_gateway_message = AsyncMock()
        mock_registry.lookup = AsyncMock(return_value=None)
        mock_registry.register = AsyncMock()
        mock_registry.register_context = AsyncMock()
        await executor.execute(context, MagicMock())
    return updater


@pytest.mark.asyncio
async def test_execute_relays_pipeline_result_as_artifact():
    updater = await _run_executor_with_final_event(_skip_summarization_event("Enterprise Leadership Debrief"))

    updater.add_artifact.assert_awaited_once()
    parts = updater.add_artifact.call_args.args[0]
    assert parts[0].text == "Enterprise Leadership Debrief"
    updater.complete.assert_awaited_once()
    failed_calls = [c for c in updater.update_status.call_args_list if c.args and c.args[0] == TaskState.failed]
    assert failed_calls == []


@pytest.mark.asyncio
async def test_execute_reports_failure_for_empty_pipeline_result():
    updater = await _run_executor_with_final_event(_skip_summarization_event(""))

    updater.add_artifact.assert_not_called()
    failed_calls = [c for c in updater.update_status.call_args_list if c.args and c.args[0] == TaskState.failed]
    assert len(failed_calls) == 1
