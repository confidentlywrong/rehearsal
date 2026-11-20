# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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

"""Tests for SimulationExecutor simulation_id bridging.

Validates that callable-mode AE instances correctly bridge simulation_id
from orchestration event payloads to the simulation registry, so that
DashLogPlugin can resolve simulation_id for events emitted by callable
agents.

This is the Python-side counterpart to the Go-side fix in
switchboard.go dispatchCallable (which writes simreg:sessions for the
A2A context_id).  The executor must re-register under the Vertex AI
session_id so DashLogPlugin (which looks up by session.id) can find it.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clean_saas_env(monkeypatch):
    """Ensure SAAS_MODE / ACCOUNT_KEY_PREFIX never leak between tests."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("ACCOUNT_KEY_PREFIX", raising=False)


@pytest.fixture()
def _callable_mode(monkeypatch):
    """Set DISPATCH_MODE=callable to simulate Agent Engine environment."""
    monkeypatch.setenv("DISPATCH_MODE", "callable")


@pytest.fixture()
def _patch_simulation_registry():
    """Provide isolated simulation_registry with no Redis."""
    from agents.utils.simulation_registry import _context_map, _local, user_scope

    _local.clear()
    _context_map.clear()
    with patch("agents.utils.simulation_executor.simulation_registry") as mock_reg:
        # Wire through to the real dicts for register/lookup/context
        async def _register(sid, sim_id, user_id=None):
            _local[sid] = sim_id

        async def _lookup(sid, user_id=None):
            return _local.get(sid)

        async def _register_context(vertex_sid, ctx_id, user_id=None):
            _context_map[vertex_sid] = ctx_id

        async def _get_context_id(vertex_sid, user_id=None):
            return _context_map.get(vertex_sid)

        mock_reg.register = AsyncMock(side_effect=_register)
        mock_reg.lookup = AsyncMock(side_effect=_lookup)
        mock_reg.register_context = AsyncMock(side_effect=_register_context)
        mock_reg.get_context_id = AsyncMock(side_effect=_get_context_id)
        mock_reg.user_scope = user_scope
        yield mock_reg
    _local.clear()
    _context_map.clear()


def _make_executor(agent_name="test_agent"):
    """Create a SimulationExecutor with mocked internals."""
    from agents.utils.simulation_executor import SimulationExecutor

    executor = SimulationExecutor(
        agent_getter=lambda: MagicMock(),
        agent_name=agent_name,
    )
    # Pre-set dispatch mode (normally from env)
    executor._dispatch_mode = "callable"

    # Mock the runner and session manager so _init_runner() is a no-op
    mock_runner = MagicMock()
    mock_runner.app_name = "test_app"
    mock_runner.app.plugins = []

    # Mock run_async to return a simple final event
    final_event = MagicMock()
    final_event.is_final_response.return_value = True
    final_event.author = "agent"
    final_event.content.parts = [MagicMock(text="test response")]

    async def _run_async(**kwargs):
        yield final_event

    mock_runner.run_async = _run_async

    executor._runner = mock_runner

    mock_session_mgr = AsyncMock()
    executor._session_manager = mock_session_mgr

    return executor, mock_session_mgr


def _make_request_context(query: str, context_id: str = "ctx-123"):
    """Create a minimal A2A RequestContext."""
    ctx = MagicMock()
    ctx.task_id = "task-1"
    ctx.context_id = context_id
    ctx.current_task = None

    msg = MagicMock()
    msg.metadata = {}
    ctx.message = msg
    ctx.get_user_input.return_value = query

    return ctx


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_spawn_registers_simulation_id(_patch_simulation_registry):
    """spawn_agent event should register context_id → simulation_id."""
    reg = _patch_simulation_registry
    executor, _ = _make_executor(agent_name="planner_with_eval")

    spawn_event = json.dumps(
        {
            "type": "spawn_agent",
            "sessionId": "sess-abc",
            "payload": {
                "agentType": "planner_with_eval",
                "simulation_id": "sim-xyz",
            },
        }
    )

    ctx = _make_request_context(spawn_event, context_id="sess-abc")
    event_queue = AsyncMock()

    await executor.execute(ctx, event_queue)

    # Should have registered context_id → simulation_id
    reg.register.assert_called_with("sess-abc", "sim-xyz")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_broadcast_bridges_simulation_id_to_vertex_session(
    _patch_simulation_registry,
):
    """broadcast event should bridge simulation_id from context_id to vertex session_id."""
    reg = _patch_simulation_registry

    executor, mock_session_mgr = _make_executor(agent_name="planner_with_eval")

    # Simulate: spawn already registered context_id → simulation_id
    from agents.utils.simulation_registry import _local

    _local["ctx-broadcast"] = "sim-broadcast-1"

    # SessionManager returns a DIFFERENT vertex session ID
    mock_session_mgr.get_or_create_session.return_value = "vertex-uuid-999"

    broadcast_event = json.dumps(
        {
            "type": "broadcast",
            "sessionId": "ctx-broadcast",
            "payload": {
                "targets": ["ctx-broadcast"],
                "data": json.dumps({"text": "tick advance"}),
            },
        }
    )

    ctx = _make_request_context(broadcast_event, context_id="ctx-broadcast")
    event_queue = AsyncMock()

    with patch("agents.utils.simulation_executor.pulses") as mock_pulses:
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor.execute(ctx, event_queue)

    # Should have looked up simulation_id via context_id
    reg.lookup.assert_called_with("ctx-broadcast")
    # Should have re-registered under the vertex session ID
    reg.register.assert_called_with("vertex-uuid-999", "sim-broadcast-1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_broadcast_with_simulation_id_in_payload(_patch_simulation_registry):
    """broadcast event with simulation_id in payload should use it directly."""
    reg = _patch_simulation_registry

    executor, mock_session_mgr = _make_executor(agent_name="simulator")
    mock_session_mgr.get_or_create_session.return_value = "vertex-uuid-888"

    broadcast_event = json.dumps(
        {
            "type": "broadcast",
            "sessionId": "ctx-sim",
            "payload": {
                "targets": ["ctx-sim"],
                "data": json.dumps({"text": "run simulation"}),
                "simulation_id": "sim-direct-payload",
            },
        }
    )

    ctx = _make_request_context(broadcast_event, context_id="ctx-sim")
    event_queue = AsyncMock()

    with patch("agents.utils.simulation_executor.pulses") as mock_pulses:
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor.execute(ctx, event_queue)

    # Should have registered using the payload's simulation_id directly
    # (no need to fall back to registry lookup)
    reg.register.assert_called_with("vertex-uuid-888", "sim-direct-payload")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_telemetry_bridge_passes_simulation_id(_patch_simulation_registry):
    """Telemetry bridge emit_gateway_message should include simulation_id."""
    from agents.utils.simulation_registry import _local

    _local["ctx-telem"] = "sim-telem-1"

    executor, mock_session_mgr = _make_executor(agent_name="planner_with_eval")
    mock_session_mgr.get_or_create_session.return_value = "vertex-telem"

    broadcast_event = json.dumps(
        {
            "type": "broadcast",
            "payload": {
                "targets": ["ctx-telem"],
                "data": json.dumps({"text": "test telemetry"}),
            },
        }
    )

    ctx = _make_request_context(broadcast_event, context_id="ctx-telem")
    event_queue = AsyncMock()

    with patch("agents.utils.simulation_executor.pulses") as mock_pulses:
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor.execute(ctx, event_queue)

        # The telemetry bridge should have passed simulation_id
        if mock_pulses.emit_gateway_message.called:
            call_kwargs = mock_pulses.emit_gateway_message.call_args
            assert call_kwargs.kwargs.get("simulation_id") == "sim-telem-1" or (
                len(call_kwargs.args) > 7 and call_kwargs.args[7] == "sim-telem-1"
            ), "emit_gateway_message must pass simulation_id from registry"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_no_simulation_id_still_works(_patch_simulation_registry):
    """Events without simulation_id should not crash and should NOT register a fake simulation_id."""
    reg = _patch_simulation_registry

    executor, mock_session_mgr = _make_executor(agent_name="simulator")
    mock_session_mgr.get_or_create_session.return_value = "vertex-no-sim"

    # Plain user message, not an orchestration event
    ctx = _make_request_context("Hello, simulator", context_id="ctx-plain")
    event_queue = AsyncMock()

    with patch("agents.utils.simulation_executor.pulses") as mock_pulses:
        mock_pulses.emit_gateway_message = AsyncMock()
        # Should not raise
        await executor.execute(ctx, event_queue)

    # Without a simulation_id from an orchestration event or registry
    # lookup, register should NOT be called.  Falling back to context_id
    # would pollute DashLogPlugin events and break Hub subscription
    # filters (see simulation_executor.py lines 416-419).
    reg.register.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_context_id_mapping_registered(_patch_simulation_registry):
    """SimulationExecutor should register vertex_session_id → context_id mapping."""
    reg = _patch_simulation_registry

    executor, mock_session_mgr = _make_executor(agent_name="planner_with_memory")
    # SessionManager returns a DIFFERENT vertex session ID
    mock_session_mgr.get_or_create_session.return_value = "vertex-99887766"

    # Plain user message (not an orchestration event)
    ctx = _make_request_context("Plan a marathon", context_id="spawn-uuid-abcd")
    event_queue = AsyncMock()

    with patch("agents.utils.simulation_executor.pulses") as mock_pulses:
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor.execute(ctx, event_queue)

    # Should have registered the context mapping so DashLogPlugin can
    # map vertex session IDs back to spawn session UUIDs
    reg.register_context.assert_called_with("vertex-99887766", "spawn-uuid-abcd")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_telemetry_bridge_uses_context_id(_patch_simulation_registry):
    """Telemetry bridge should use context_id (spawn UUID) in origin, not vertex session ID."""
    executor, mock_session_mgr = _make_executor(agent_name="planner_with_memory")
    mock_session_mgr.get_or_create_session.return_value = "vertex-numeric-id"

    ctx = _make_request_context("Plan a marathon", context_id="spawn-uuid-1234")
    event_queue = AsyncMock()

    with patch("agents.utils.simulation_executor.pulses") as mock_pulses:
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor.execute(ctx, event_queue)

        if mock_pulses.emit_gateway_message.called:
            call_kwargs = mock_pulses.emit_gateway_message.call_args
            origin = call_kwargs.kwargs.get("origin") or call_kwargs.args[0]
            # Origin session_id should be the spawn UUID, not the vertex ID
            assert origin["session_id"] == "spawn-uuid-1234", (
                f"Telemetry bridge should use context_id 'spawn-uuid-1234' "
                f"in origin.session_id, got '{origin['session_id']}'"
            )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_dedup_blocks_second_execution(_patch_simulation_registry):
    """Second execute() with same context_id should be dropped via Redis NX lock."""
    executor, mock_session_mgr = _make_executor(agent_name="simulator")
    mock_session_mgr.get_or_create_session.return_value = "vertex-session-1"

    ctx1 = _make_request_context('{"action":"execute","narrative":"test"}', context_id="sim-ctx-1")
    ctx2 = _make_request_context('{"action":"execute","narrative":"test"}', context_id="sim-ctx-1")

    mock_redis = MagicMock()
    # First call: NX succeeds (lock acquired)
    # Second call: NX fails (lock already held)
    mock_redis.set = AsyncMock(side_effect=[True, False])

    with patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=mock_redis):
        eq1 = AsyncMock()
        await executor.execute(ctx1, eq1)

        eq2 = AsyncMock()
        await executor.execute(ctx2, eq2)

    # runner.run_async should only have been invoked for the first call
    actual = mock_session_mgr.get_or_create_session.call_count
    assert actual == 1, f"Expected 1 session creation (second should be deduped), got {actual}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_dedup_allows_different_context_ids(_patch_simulation_registry):
    """Different context_ids should NOT be deduped."""
    executor, mock_session_mgr = _make_executor(agent_name="simulator")
    mock_session_mgr.get_or_create_session.return_value = "vertex-session-1"

    ctx1 = _make_request_context('{"action":"execute","narrative":"test"}', context_id="sim-ctx-1")
    ctx2 = _make_request_context('{"action":"execute","narrative":"test"}', context_id="sim-ctx-2")

    mock_redis = MagicMock()
    # Both NX calls succeed (different keys)
    mock_redis.set = AsyncMock(return_value=True)

    with patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=mock_redis):
        eq1 = AsyncMock()
        await executor.execute(ctx1, eq1)

        eq2 = AsyncMock()
        await executor.execute(ctx2, eq2)

    assert mock_session_mgr.get_or_create_session.call_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_dedup_graceful_without_redis(_patch_simulation_registry):
    """When Redis is unavailable, dedup should be skipped (not crash)."""
    executor, mock_session_mgr = _make_executor(agent_name="simulator")
    mock_session_mgr.get_or_create_session.return_value = "vertex-session-1"

    ctx = _make_request_context('{"action":"execute","narrative":"test"}', context_id="sim-ctx-1")

    with patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None):
        eq = AsyncMock()
        await executor.execute(ctx, eq)

    # Should still proceed without Redis
    assert mock_session_mgr.get_or_create_session.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_simulation_executor_dedup_fails_closed_on_redis_error(_patch_simulation_registry, caplog):
    """When Redis is configured and set(..., nx=True) raises, execute() fails closed."""
    import logging
    from a2a.types import TaskState

    executor, mock_session_mgr = _make_executor(agent_name="simulator")
    ctx = _make_request_context('{"action":"execute","narrative":"test"}', context_id="sim-ctx-err")

    mock_redis = MagicMock()
    mock_redis.set = AsyncMock(side_effect=ConnectionError("redis unavailable"))
    eq = AsyncMock()

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=mock_redis),
        caplog.at_level(logging.ERROR, logger="agents.utils.simulation_executor"),
    ):
        await executor.execute(ctx, eq)

    # Must NOT execute the runner
    mock_session_mgr.get_or_create_session.assert_not_called()
    assert any("DEDUP_ERROR" in r.message for r in caplog.records)
    # Verify TaskState.failed was emitted on the event queue
    statuses = [
        call.args[0].status.state
        for call in eq.enqueue_event.await_args_list
        if call.args and hasattr(call.args[0], "status")
    ]
    assert TaskState.failed in statuses


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_user_id_from_metadata_propagates_to_session_manager_and_runner(_patch_simulation_registry):
    """A2A message.metadata['user_id'] must propagate to SessionManager and Runner.run_async."""
    executor, mock_session_mgr = _make_executor(agent_name="planner_with_eval")
    mock_session_mgr.get_or_create_session.return_value = "vertex-alice-1"

    observed_run_kwargs: list[dict] = []
    orig_run_async = executor._runner.run_async  # type: ignore[union-attr]

    async def _capturing_run_async(**kwargs):
        observed_run_kwargs.append(kwargs)
        async for ev in orig_run_async(**kwargs):
            yield ev

    executor._runner.run_async = _capturing_run_async  # type: ignore[union-attr]

    ctx = _make_request_context("Run leadership rehearsal", context_id="ctx-alice-1")
    ctx.message.metadata = {"user_id": "user-alice"}
    eq = AsyncMock()

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_executor.pulses") as mock_pulses,
    ):
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor.execute(ctx, eq)

    mock_session_mgr.get_or_create_session.assert_awaited_once_with(
        context_id="ctx-alice-1",
        app_name="test_app",
        user_id="user-alice",
    )
    assert len(observed_run_kwargs) == 1
    assert observed_run_kwargs[0]["user_id"] == "user-alice"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
@pytest.mark.parametrize(
    ("query_obj", "expected_user"),
    [
        (
            {
                "type": "broadcast",
                "payload": {"data": json.dumps({"text": "tick"}), "acting_user_id": "user-bob"},
            },
            "user-bob",
        ),
        (
            {
                "type": "broadcast",
                "payload": {"data": json.dumps({"text": "tick"}), "userId": "user-carol"},
            },
            "user-carol",
        ),
        (
            {"action": "execute", "simulation_id": "sim-1", "user_id": "user-dave"},
            "user-dave",
        ),
        (
            {"action": "execute", "simulation_id": "sim-2", "userId": "user-erin"},
            "user-erin",
        ),
    ],
)
async def test_user_id_extracted_from_json_payload_when_metadata_missing(
    _patch_simulation_registry, query_obj, expected_user
):
    """When message.metadata lacks user_id, JSON payload acting_user_id/user_id/userId propagates."""
    executor, mock_session_mgr = _make_executor(agent_name="simulator")
    mock_session_mgr.get_or_create_session.return_value = "vertex-payload-uid"

    ctx = _make_request_context(json.dumps(query_obj), context_id="ctx-payload-uid")
    ctx.message.metadata = {}
    eq = AsyncMock()

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_executor.pulses") as mock_pulses,
    ):
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor.execute(ctx, eq)

    mock_session_mgr.get_or_create_session.assert_awaited_once_with(
        context_id="ctx-payload-uid",
        app_name="test_app",
        user_id=expected_user,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_metadata_user_id_takes_precedence_over_payload(_patch_simulation_registry):
    """Authenticated message.metadata['user_id'] must override any userId in the event payload."""
    executor, mock_session_mgr = _make_executor(agent_name="planner_with_eval")
    mock_session_mgr.get_or_create_session.return_value = "vertex-prec-1"

    broadcast_event = json.dumps(
        {
            "type": "broadcast",
            "payload": {
                "data": json.dumps({"text": "Run"}),
                "userId": "spoofed-user",
                "acting_user_id": "spoofed-user",
            },
        }
    )
    ctx = _make_request_context(broadcast_event, context_id="ctx-prec-1")
    ctx.message.metadata = {"user_id": "verified-user"}
    eq = AsyncMock()

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None),
        patch("agents.utils.simulation_executor.pulses") as mock_pulses,
    ):
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor.execute(ctx, eq)

    mock_session_mgr.get_or_create_session.assert_awaited_once_with(
        context_id="ctx-prec-1",
        app_name="test_app",
        user_id="verified-user",
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
@pytest.mark.parametrize(
    ("meta", "query"),
    [
        ({}, "Plain message without user_id"),
        ({"user_id": "   "}, '{"type":"broadcast","payload":{"data":"{\\"text\\":\\"hi\\"}"}}'),
        ({"user_id": "simulation_user"}, '{"action":"execute","simulation_id":"sim-x"}'),
        ({"user_id": "user:colon"}, "Plain message"),
        ({}, json.dumps({"type": "spawn_agent", "sessionId": "s1", "payload": {"agentType": "simulator"}})),
    ],
)
async def test_saas_mode_on_fails_closed_without_valid_user_id(
    monkeypatch, _patch_simulation_registry, meta, query
):
    """When SAAS_MODE=on, missing/anonymous/invalid user_id must fail closed and never collapse to simulation_user."""
    from a2a.types import TaskState

    monkeypatch.setenv("SAAS_MODE", "on")
    executor, mock_session_mgr = _make_executor(agent_name="simulator")
    ctx = _make_request_context(query, context_id="ctx-saas-reject")
    ctx.message.metadata = meta
    eq = AsyncMock()

    with patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=None):
        await executor.execute(ctx, eq)

    mock_session_mgr.get_or_create_session.assert_not_called()
    statuses = [
        call.args[0].status.state
        for call in eq.enqueue_event.await_args_list
        if call.args and hasattr(call.args[0], "status")
    ]
    assert TaskState.failed in statuses


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_saas_mode_on_and_off_real_session_manager_redis_keys(monkeypatch, _patch_simulation_registry):
    """End-to-end SimulationExecutor -> real SessionManager writes matching Go reset Redis keys."""
    from agents.utils.session_manager import SessionManager

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

    mock_session_service = AsyncMock()
    created_users: list[str] = []

    async def _create_session(*, app_name, user_id):
        created_users.append(user_id)
        return SimpleNamespace(id=f"vertex-{user_id}-{len(created_users)}")

    mock_session_service.create_session = AsyncMock(side_effect=_create_session)

    # 1. SAAS_MODE=on with metadata user_id -> session_map:user-alice:ctx-e2e-1
    monkeypatch.setenv("SAAS_MODE", "on")
    executor_saas, _ = _make_executor(agent_name="planner_with_eval")
    executor_saas._session_manager = SessionManager(session_service=mock_session_service)
    ctx_saas = _make_request_context(
        json.dumps(
            {
                "type": "broadcast",
                "payload": {"data": json.dumps({"text": "Plan leadership scenario"}), "targets": ["ctx-e2e-1"]},
            }
        ),
        context_id="ctx-e2e-1",
    )
    ctx_saas.message.metadata = {"user_id": "user-alice"}

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.simulation_executor.pulses") as mock_pulses,
    ):
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor_saas.execute(ctx_saas, AsyncMock())

    assert "session_map:user-alice:ctx-e2e-1" in redis_store
    assert "session_map:simulation_user:ctx-e2e-1" not in redis_store

    # 2. SAAS_MODE=off without user_id -> backward-compatible session_map:ctx-e2e-2 with simulation_user
    monkeypatch.setenv("SAAS_MODE", "off")
    executor_local, _ = _make_executor(agent_name="planner_with_eval")
    executor_local._session_manager = SessionManager(session_service=mock_session_service)
    ctx_local = _make_request_context("Plain local request", context_id="ctx-e2e-2")
    ctx_local.message.metadata = {}

    with (
        patch("agents.utils.simulation_executor.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis),
        patch("agents.utils.simulation_executor.pulses") as mock_pulses,
    ):
        mock_pulses.emit_gateway_message = AsyncMock()
        await executor_local.execute(ctx_local, AsyncMock())

    assert "session_map:ctx-e2e-2" in redis_store
    assert created_users == ["user-alice", "simulation_user"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_callable_mode")
async def test_saas_mode_on_spawn_agent_with_payload_user_id_succeeds(monkeypatch, _patch_simulation_registry):
    """spawn_agent carrying acting_user_id in payload succeeds when SAAS_MODE=on."""
    monkeypatch.setenv("SAAS_MODE", "on")
    reg = _patch_simulation_registry
    executor, _ = _make_executor(agent_name="planner_with_eval")

    spawn_event = json.dumps(
        {
            "type": "spawn_agent",
            "sessionId": "sess-saas-spawn",
            "payload": {
                "agentType": "planner_with_eval",
                "simulation_id": "sim-saas-1",
                "acting_user_id": "user-alice",
                "userId": "user-alice",
            },
        }
    )
    ctx = _make_request_context(spawn_event, context_id="sess-saas-spawn")
    await executor.execute(ctx, AsyncMock())

    reg.register.assert_called_with("sess-saas-spawn", "sim-saas-1", user_id="user-alice")
