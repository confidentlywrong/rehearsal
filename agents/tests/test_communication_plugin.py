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

"""Tests for agents.utils.communication_plugin — A2A client lifecycle."""

import pytest
from unittest.mock import MagicMock


class TestGetClient:
    """Tests for the module-level get_client function."""

    def test_creates_new_client_on_first_call(self):
        """First call for an invocation ID should create a new client."""
        import agents.utils.communication_plugin as cp

        cp._clients.clear()

        client = cp.get_client("inv-1")
        assert client is not None
        assert "inv-1" in cp._clients

    def test_returns_cached_client_on_second_call(self):
        """Second call for same invocation ID should return the same client."""
        import agents.utils.communication_plugin as cp

        cp._clients.clear()

        c1 = cp.get_client("inv-2")
        c2 = cp.get_client("inv-2")
        assert c1 is c2

    def test_different_invocations_get_different_clients(self):
        """Different invocation IDs should get separate clients."""
        import agents.utils.communication_plugin as cp

        cp._clients.clear()

        c1 = cp.get_client("inv-a")
        c2 = cp.get_client("inv-b")
        assert c1 is not c2


class TestSimulationCommunicationPlugin:
    """Tests for the SimulationCommunicationPlugin ADK lifecycle hooks."""

    @pytest.mark.asyncio
    async def test_before_agent_prewarms_client(self, mock_callback_context):
        """before_agent_callback should pre-warm a client for the invocation."""
        import agents.utils.communication_plugin as cp

        cp._clients.clear()

        plugin = cp.SimulationCommunicationPlugin()
        mock_agent = MagicMock()

        await plugin.before_agent_callback(
            agent=mock_agent,
            callback_context=mock_callback_context,
        )

        assert mock_callback_context.invocation_id in cp._clients

    @pytest.mark.asyncio
    async def test_after_agent_cleans_up_client(self, mock_callback_context):
        """after_agent_callback should remove the client from the registry."""
        import agents.utils.communication_plugin as cp

        cp._clients.clear()

        # Pre-warm
        plugin = cp.SimulationCommunicationPlugin()
        mock_agent = MagicMock()
        await plugin.before_agent_callback(
            agent=mock_agent,
            callback_context=mock_callback_context,
        )
        assert mock_callback_context.invocation_id in cp._clients

        # Cleanup
        await plugin.after_agent_callback(
            agent=mock_agent,
            callback_context=mock_callback_context,
        )
        assert mock_callback_context.invocation_id not in cp._clients

    @pytest.mark.asyncio
    async def test_after_agent_noop_if_not_warmed(self, mock_callback_context):
        """after_agent_callback should not crash if client was never pre-warmed."""
        import agents.utils.communication_plugin as cp

        cp._clients.clear()

        plugin = cp.SimulationCommunicationPlugin()
        mock_agent = MagicMock()

        # Should not raise
        await plugin.after_agent_callback(
            agent=mock_agent,
            callback_context=mock_callback_context,
        )


class TestBoundedClientRegistry:
    """P1-5 (2026-06-24): _clients dict is bounded via FIFO eviction.

    Without this cap, every invocation whose after_agent_callback never
    runs (cancellation, crash, missing callback) leaks one
    SimulationA2AClient — the registry would grow without bound under
    the 1000-runner runner_autopilot fan-out.
    """

    def test_evicts_oldest_when_cap_exceeded(self, monkeypatch):
        """Adding a (cap+1)-th distinct invocation evicts the oldest."""
        import importlib
        import agents.utils.communication_plugin as cp

        monkeypatch.setenv("COMMUNICATION_PLUGIN_MAX_CLIENTS", "3")
        importlib.reload(cp)
        try:
            cp._clients.clear()
            cp.get_client("inv-1")
            cp.get_client("inv-2")
            cp.get_client("inv-3")
            assert len(cp._clients) == 3
            cp.get_client("inv-4")
            assert len(cp._clients) == 3
            assert "inv-1" not in cp._clients  # evicted
            assert "inv-4" in cp._clients
        finally:
            cp._clients.clear()
            monkeypatch.delenv("COMMUNICATION_PLUGIN_MAX_CLIENTS", raising=False)
            importlib.reload(cp)

    def test_repeated_get_client_does_not_evict(self, monkeypatch):
        """Re-calling get_client for an existing invocation must not count as a new entry."""
        import importlib
        import agents.utils.communication_plugin as cp

        monkeypatch.setenv("COMMUNICATION_PLUGIN_MAX_CLIENTS", "2")
        importlib.reload(cp)
        try:
            cp._clients.clear()
            c1 = cp.get_client("inv-a")
            c1_again = cp.get_client("inv-a")
            cp.get_client("inv-b")
            assert c1 is c1_again
            assert len(cp._clients) == 2
            assert "inv-a" in cp._clients
            assert "inv-b" in cp._clients
        finally:
            cp._clients.clear()
            monkeypatch.delenv("COMMUNICATION_PLUGIN_MAX_CLIENTS", raising=False)
            importlib.reload(cp)

    def test_eviction_logs_warning(self, monkeypatch, caplog):
        """Eviction must log a WARNING so operators see the leak pattern."""
        import importlib
        import logging
        import agents.utils.communication_plugin as cp

        monkeypatch.setenv("COMMUNICATION_PLUGIN_MAX_CLIENTS", "1")
        importlib.reload(cp)
        try:
            cp._clients.clear()
            cp.get_client("inv-1")
            with caplog.at_level(logging.WARNING, logger="agents.utils.communication_plugin"):
                cp.get_client("inv-2")
            assert any("evicted client for invocation inv-1" in r.message for r in caplog.records), (
                "Eviction must surface as a WARNING"
            )
        finally:
            cp._clients.clear()
            monkeypatch.delenv("COMMUNICATION_PLUGIN_MAX_CLIENTS", raising=False)
            importlib.reload(cp)
