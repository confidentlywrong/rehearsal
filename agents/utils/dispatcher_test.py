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

import asyncio
import json

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from google.genai import types
from agents.utils.dispatcher import RedisOrchestratorDispatcher
from google.adk.runners import InMemoryRunner
from google.adk.agents import LlmAgent
from google.adk.apps import App


@pytest.fixture(autouse=True)
def mock_genai():
    # Patch the genai Client itself to be safe
    with patch("google.genai.Client") as m:
        mock_client = MagicMock()
        # Create a real response object to avoid Pydantic validation errors
        mock_response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(content=types.Content(parts=[types.Part.from_text(text="Hello from mock LLM")]))
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10, candidates_token_count=10, total_token_count=20
            ),
        )

        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

        async def mock_stream(*args, **kwargs):
            yield mock_response

        mock_client.aio.models.generate_content_stream = mock_stream

        m.return_value = mock_client
        yield m


@pytest.mark.asyncio
async def test_dispatcher_agent_run_smoke():
    """Smoke test for the dispatcher triggering an agent run directly."""

    # Setup a dummy agent and runner
    agent = LlmAgent(name="test_agent", instruction="Echo back: Hello")
    app = App(name="test_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    # Initialize dispatcher
    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    session_id = "smoke_session"
    content = "Hello Dispatcher"

    # Pre-create session (in production, _ensure_session does this during spawn)
    await runner.session_service.create_session(
        app_name="test_app",
        user_id="simulation",
        session_id=session_id,
    )

    # Trigger the run logic directly with a collector to avoid Redis
    pulses = []
    content_obj = types.Content(role="user", parts=[types.Part.from_text(text=content)])
    await dispatcher._trigger_agent_run_logic(session_id, content_obj, pulses_collector=pulses)

    # Check the session store via runner
    session = await runner.session_service.get_session(app_name="test_app", user_id="simulation", session_id=session_id)
    assert session and len(session.events) > 1, (
        f"Agent run did not complete (events: {len(session.events) if session else 'None'})"
    )
    assert len(pulses) > 0, "No pulses were emitted"


@pytest.mark.asyncio
async def test_dispatcher_handle_event_push():
    """Verifies that _process_event (HTTP Push logic) correctly triggers agent runs."""
    agent = LlmAgent(name="push_agent", instruction="Echo: OK")
    app = App(name="push_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    session_id = "push_session_1"
    event_data = {
        "type": "spawn_agent",
        "sessionId": session_id,
        "payload": {"agentType": "push_app"},
    }

    # Mock _trigger_agent_run to verify spawn doesn't trigger a run
    with patch.object(dispatcher, "_trigger_agent_run") as mock_trigger:
        # Handle event (Push logic)
        await dispatcher._process_event(event_data)

        # Verify it DID NOT trigger a run (passive spawn — session creation
        # is deferred to the first run_async via auto_create_session=True)
        mock_trigger.assert_not_called()
        assert session_id in dispatcher.active_sessions


@pytest.mark.asyncio
async def test_dispatcher_broadcast_pulse():
    """Verifies that a broadcast pulse logic triggers active sessions."""
    agent = LlmAgent(name="runner_agent", instruction="Run!")
    app = App(name="runner_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    # Manually add active sessions
    dispatcher.active_sessions.add("runner_1")
    dispatcher.active_sessions.add("runner_2")

    broadcast_data = {"type": "broadcast", "payload": {"data": "GO_GO_GO"}}

    # Mock _trigger_agent_run
    with patch.object(dispatcher, "_trigger_agent_run") as mock_trigger:
        await dispatcher._process_event(broadcast_data)

        # Verify both sessions were triggered
        assert mock_trigger.call_count == 2
        called_sids = [args[0] for args, _ in mock_trigger.call_args_list]
        assert "runner_1" in called_sids
        assert "runner_2" in called_sids


@pytest.mark.asyncio
async def test_callable_dispatcher_skips_redis():
    """Callable agents skip Redis listeners, rely on HTTP-only orchestration."""
    agent = LlmAgent(name="callable_agent", instruction="I am callable")
    app = App(name="callable_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    # dispatch_mode="callable" should cause Redis listeners to be skipped
    dispatcher = RedisOrchestratorDispatcher(runner=runner, dispatch_mode="callable")
    assert dispatcher.dispatch_mode == "callable"

    # Start the dispatcher and give it a moment to run
    with (
        patch.object(dispatcher, "_pubsub_listener", new_callable=AsyncMock) as mock_pubsub,
        patch.object(dispatcher, "_queue_listener", new_callable=AsyncMock) as mock_queue,
    ):
        # _listen_loop should NOT call pubsub/queue listeners in callable mode
        # We can't easily test the full loop, but we can test that
        # the dispatcher knows it's callable
        dispatcher.start()
        await asyncio.sleep(0.05)
        dispatcher.stop()

        # In callable mode, Redis listeners should never be called
        mock_pubsub.assert_not_called()
        mock_queue.assert_not_called()


@pytest.mark.asyncio
async def test_subscriber_dispatcher_starts_redis():
    """Subscriber agents start Redis Pub/Sub and Queue listeners normally."""
    agent = LlmAgent(name="sub_agent", instruction="I am subscriber")
    app = App(name="sub_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    # Default dispatch_mode should be "subscriber"
    dispatcher = RedisOrchestratorDispatcher(runner=runner)
    assert dispatcher.dispatch_mode == "subscriber"


@pytest.mark.asyncio
async def test_broadcast_skips_without_active_sessions():
    """Broadcast events are ignored when the dispatcher has no active sessions."""
    agent = LlmAgent(name="empty_agent", instruction="I have no sessions")
    app = App(name="empty_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    # No active sessions
    assert len(dispatcher.active_sessions) == 0

    broadcast_data = {"type": "broadcast", "payload": {"data": "PULSE"}}

    with patch.object(dispatcher, "_trigger_agent_run") as mock_trigger:
        await dispatcher._process_event(broadcast_data)

        # Should NOT trigger any runs — no active sessions
        mock_trigger.assert_not_called()


@pytest.mark.asyncio
async def test_process_event_a2ui_action():
    """A2UI action events trigger an agent run for the target session."""
    agent = LlmAgent(name="a2ui_agent", instruction="Handle A2UI")
    app = App(name="a2ui_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    # Manually register an active session
    session_id = "session-abc"
    dispatcher.active_sessions.add(session_id)

    event = {
        "type": "a2ui_action",
        "eventId": "evt-001",
        "sessionId": session_id,
        "payload": {
            "actionName": "run_simulation",
            "sessionId": session_id,
        },
    }

    with patch.object(dispatcher, "_trigger_agent_run") as mock_trigger:
        await dispatcher._process_event(event)

        # Verify _trigger_agent_run was called once for the active session
        mock_trigger.assert_called_once()
        call_args = mock_trigger.call_args
        assert call_args is not None
        assert call_args[0][0] == session_id

        # Verify the content is a types.Content with the action as JSON
        content = call_args[0][1]
        assert isinstance(content, types.Content)
        assert content.role == "user"
        assert content.parts is not None
        part_text = content.parts[0].text
        assert part_text is not None

        action_data = json.loads(part_text)
        assert action_data["a2ui_action"] == "run_simulation"
        assert action_data["source"] == "a2ui_button"


@pytest.mark.asyncio
async def test_process_event_a2ui_action_inactive_session():
    """A2UI action for inactive sessions is ignored."""
    agent = LlmAgent(name="a2ui_agent2", instruction="Handle A2UI")
    app = App(name="a2ui_app2", root_agent=agent)
    runner = InMemoryRunner(app=app)

    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    # Session NOT in active_sessions
    event = {
        "type": "a2ui_action",
        "eventId": "evt-002",
        "sessionId": "unknown-session",
        "payload": {
            "actionName": "run_simulation",
            "sessionId": "unknown-session",
        },
    }

    with patch.object(dispatcher, "_trigger_agent_run") as mock_trigger:
        await dispatcher._process_event(event)

        # Should NOT trigger any runs — session is not active
        mock_trigger.assert_not_called()


@pytest.mark.asyncio
async def test_process_event_a2ui_action_session_id_from_payload():
    """A2UI action uses sessionId from payload if not at top level."""
    agent = LlmAgent(name="a2ui_agent3", instruction="Handle A2UI")
    app = App(name="a2ui_app3", root_agent=agent)
    runner = InMemoryRunner(app=app)

    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    session_id = "payload-session"
    dispatcher.active_sessions.add(session_id)

    # sessionId only in payload, not at top level
    event = {
        "type": "a2ui_action",
        "eventId": "evt-003",
        "payload": {
            "actionName": "approve_budget",
            "sessionId": session_id,
        },
    }

    with patch.object(dispatcher, "_trigger_agent_run") as mock_trigger:
        await dispatcher._process_event(event)

        mock_trigger.assert_called_once()
        call_args = mock_trigger.call_args
        assert call_args[0][0] == session_id


@pytest.mark.asyncio
async def test_environment_reset_clears_sessions_and_tasks():
    """Reset event clears active_sessions, background tasks, and seen events."""
    agent = LlmAgent(name="reset_agent", instruction="Reset me")
    app = App(name="reset_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    # Seed some sessions
    for sid in ("s1", "s2", "s3"):
        dispatcher.active_sessions.add(sid)
    from collections import OrderedDict

    dispatcher._seen_events = OrderedDict([("evt1", None), ("evt2", None)])

    import asyncio
    dispatcher._loop = asyncio.get_running_loop()

    # Seed mock background tasks (concurrent.futures.Future objects)
    mock_future1 = MagicMock()
    mock_future2 = MagicMock()
    dispatcher._background_tasks = {mock_future1, mock_future2}

    # Process reset event
    await dispatcher._process_event({"type": "environment_reset", "eventId": "reset-123"})
    await asyncio.sleep(0.01)

    assert len(dispatcher.active_sessions) == 0
    assert len(dispatcher._seen_events) == 0
    # Verify cancel was called on each future
    mock_future1.cancel.assert_called_once()
    mock_future2.cancel.assert_called_once()
    assert len(dispatcher._background_tasks) == 0


@pytest.mark.asyncio
async def test_broadcast_skips_a2ui_tool_results():
    """Dispatcher broadcast must NOT re-emit A2UI tool results as JSON.

    The plugin's _emit_narrative already emits A2UI payloads with the correct
    msg_type='a2ui'.  When the dispatcher sees a function_response whose JSON
    contains an 'a2ui' key, it must skip emitting it to avoid a duplicate
    message with the wrong wrapperType ('json' instead of 'a2ui').
    """
    agent = LlmAgent(name="a2ui_skip_agent", instruction="Emit A2UI")
    app = App(name="a2ui_skip_app", root_agent=agent)
    runner = InMemoryRunner(app=app)

    dispatcher = RedisOrchestratorDispatcher(runner=runner)
    session_id = "a2ui_skip_session"

    # Build a mock event whose function_response contains an A2UI result
    mock_part = MagicMock()
    mock_part.text = None
    mock_part.function_response = MagicMock()
    mock_part.function_response.response = {
        "status": "success",
        "a2ui": {"surfaceUpdate": {"surfaceId": "sim_results"}},
    }

    mock_event = MagicMock()
    mock_event.author = "test_agent"  # Not in default allowed_authors, so patch that
    mock_event.content.parts = [mock_part]

    # Allow the mock event's author through
    dispatcher.allowed_authors = {"test_agent"}

    # Mock run_async to yield our crafted event
    async def mock_run_async(**kwargs):
        yield mock_event

    # Pre-create session
    await runner.session_service.create_session(
        app_name="a2ui_skip_app",
        user_id="simulation",
        session_id=session_id,
    )

    content = types.Content(role="user", parts=[types.Part.from_text(text="run")])

    with (
        patch.object(runner, "run_async", side_effect=mock_run_async),
        patch("agents.utils.pulses.emit_gateway_message", new_callable=AsyncMock) as mock_emit,
    ):
        # pulses_collector=None makes broadcast_to_redis=True
        await dispatcher._trigger_agent_run_logic(session_id, content, pulses_collector=None)

        # The A2UI tool result must NOT be emitted as msg_type="json"
        for call in mock_emit.call_args_list:
            kwargs = call.kwargs if call.kwargs else {}
            # Also check positional via call signature
            assert kwargs.get("msg_type") != "json" or "a2ui" not in json.dumps(kwargs.get("data", {})), (
                f"Dispatcher emitted A2UI tool result as msg_type='json': {kwargs}"
            )


@pytest.mark.asyncio
async def test_spawn_agent_rejects_empty_user_id_when_bound(monkeypatch):
    """When DISPATCHER_USER_ID is set, spawn_agent without user_id must be rejected."""
    monkeypatch.setenv("DISPATCHER_USER_ID", "alice")

    agent = LlmAgent(name="tenant_agent", instruction="Test")
    app = App(name="tenant_app", root_agent=agent)
    runner = InMemoryRunner(app=app)
    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    await dispatcher._process_event(
        {
            "type": "spawn_agent",
            "eventId": "evt-missing-uid",
            "sessionId": "sess-missing-uid",
            "payload": {
                "agentType": "tenant_app",
                "simulation_id": "sim-1",
            },
        }
    )
    assert "sess-missing-uid" not in dispatcher.active_sessions


@pytest.mark.asyncio
async def test_handle_event_marshals_pubsub_across_loops():
    """HTTP handle_event() on caller loop marshals _pubsub subscribe/unsubscribe onto self._loop."""
    import threading

    agent = LlmAgent(name="loop_agent", instruction="Test")
    app = App(name="loop_app", root_agent=agent)
    runner = InMemoryRunner(app=app)
    dispatcher = RedisOrchestratorDispatcher(runner=runner, dispatch_mode="callable")

    loop_ready = threading.Event()
    sub_loops: list[asyncio.AbstractEventLoop] = []

    class FakePubSub:
        async def subscribe(self, channel: str) -> None:
            sub_loops.append(asyncio.get_running_loop())

        async def unsubscribe(self, channel: str) -> None:
            sub_loops.append(asyncio.get_running_loop())

    bg_loop = asyncio.new_event_loop()

    def _bg() -> None:
        asyncio.set_event_loop(bg_loop)
        dispatcher._loop = bg_loop
        dispatcher._pubsub = FakePubSub()
        loop_ready.set()
        bg_loop.run_forever()

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    assert loop_ready.wait(timeout=2.0)
    try:
        await dispatcher.handle_event(
            {
                "type": "spawn_agent",
                "eventId": "evt-cross-loop",
                "sessionId": "sess-cross",
                "payload": {"agentType": "loop_app", "simulation_id": "sim-cross"},
            }
        )
        assert sub_loops == [bg_loop]
        dispatcher.remove_session("sess-cross")
        await asyncio.sleep(0.05)
        assert sub_loops == [bg_loop, bg_loop]
    finally:
        bg_loop.call_soon_threadsafe(bg_loop.stop)
        t.join(timeout=2.0)
        bg_loop.close()


def test_prepare_pulses_preserves_empty_dict_function_response():
    """Empty dict {} function_response must not be dropped by truthiness check."""
    agent = LlmAgent(name="fr_agent", instruction="Test")
    app = App(name="fr_app", root_agent=agent)
    runner = InMemoryRunner(app=app)
    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    mock_part = MagicMock()
    mock_part.text = None
    mock_part.function_call = None
    mock_part.function_response = MagicMock()
    mock_part.function_response.response = {}

    mock_event = MagicMock()
    mock_event.author = "agent"
    mock_event.content.parts = [mock_part]

    assert dispatcher._prepare_pulses("sess-1", mock_event) == ["{}"]


@pytest.mark.asyncio
async def test_trigger_agent_run_logic_rejects_invalid_redis_max_connections(monkeypatch):
    """Invalid REDIS_MAX_CONNECTIONS raises ValueError instead of silently falling back."""
    monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "not-an-int")
    agent = LlmAgent(name="cfg_agent", instruction="Test")
    app = App(name="cfg_app", root_agent=agent)
    runner = InMemoryRunner(app=app)
    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    content = types.Content(role="user", parts=[types.Part.from_text(text="hi")])
    with pytest.raises(ValueError, match="REDIS_MAX_CONNECTIONS"):
        await dispatcher._trigger_agent_run_logic("sess-cfg", content)


@pytest.mark.asyncio
async def test_process_event_handles_null_payload_without_attribute_error(caplog):
    """Explicit null payload must not raise AttributeError inside _process_event."""
    import logging

    agent = LlmAgent(name="null_payload_agent", instruction="Test")
    app = App(name="null_payload_app", root_agent=agent)
    runner = InMemoryRunner(app=app)
    dispatcher = RedisOrchestratorDispatcher(runner=runner)

    with caplog.at_level(logging.ERROR):
        await dispatcher._process_event(
            {
                "type": "spawn_agent",
                "eventId": "evt-null-payload",
                "sessionId": "sess-null",
                "payload": None,
            }
        )

    assert "sess-null" not in dispatcher.active_sessions
    assert "AttributeError" not in caplog.text
