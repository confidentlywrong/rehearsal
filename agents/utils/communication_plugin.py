# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
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
import logging
import os
from collections import OrderedDict
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.agents.callback_context import CallbackContext
from agents.utils.communication import SimulationA2AClient

logger = logging.getLogger(__name__)

# Registry for non-serializable A2A clients
# Key: invocation_id, Value: SimulationA2AClient.
#
# Bounded (2026-06-24): the registry used to be a plain dict that grew
# unboundedly. ``after_agent_callback`` removes entries on the happy
# path, but ADK does not guarantee the callback runs after a cancelled
# or crashed invocation — under the 1000-runner runner_autopilot fan-out
# a 1% callback-skip rate already leaks 10 SimulationA2AClient instances
# per orchestration tick, each holding an httpx connection pool.
#
# OrderedDict + FIFO eviction caps the leak. Cap is configurable via
# ``COMMUNICATION_PLUGIN_MAX_CLIENTS`` (default 4096) so an operator
# can raise it without a redeploy if the warning fires legitimately.
# When eviction happens we schedule a best-effort ``client.close()``
# on the running loop — the client may still be in use, but the choice
# is between (a) eventual OOM and (b) closing what's almost certainly
# a leaked entry.
_MAX_CLIENTS = max(1, int(os.environ.get("COMMUNICATION_PLUGIN_MAX_CLIENTS", "4096")))
_clients: "OrderedDict[str, SimulationA2AClient]" = OrderedDict()


def _evict_oldest_if_full() -> None:
    """Evict the oldest entry if the registry has grown past the cap.

    Fires a single WARNING per eviction so the operator notices the
    leak pattern. The evicted client is closed best-effort on the
    running loop (if any) — if no loop is available we drop the
    reference and rely on GC + httpx's destructor.
    """
    if len(_clients) < _MAX_CLIENTS:
        return
    stale_iid, stale_client = _clients.popitem(last=False)
    logger.warning(
        "A2A_PLUGIN: evicted client for invocation %s (registry hit cap=%d); "
        "after_agent_callback likely never ran for this invocation. "
        "If this fires repeatedly, raise COMMUNICATION_PLUGIN_MAX_CLIENTS.",
        stale_iid,
        _MAX_CLIENTS,
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — best-effort close gives up; the httpx
        # destructor will reclaim sockets when the client is GC'd.
        return
    loop.create_task(_safe_close(stale_iid, stale_client))


async def _safe_close(iid: str, client: SimulationA2AClient) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        logger.debug("A2A_PLUGIN: eviction close failed for %s", iid, exc_info=True)


def get_client(invocation_id: str) -> SimulationA2AClient:
    """Helper to retrieve the A2A client for a specific invocation."""
    if invocation_id not in _clients:
        logger.debug(f"A2A_PLUGIN: Initializing new client for invocation {invocation_id}")
        _evict_oldest_if_full()
        _clients[invocation_id] = SimulationA2AClient()
    return _clients[invocation_id]


class SimulationCommunicationPlugin(BasePlugin):
    """ADK Plugin that standardizes A2A client management for simulation agents."""

    def __init__(self, name="communication"):
        super().__init__(name=name)

    async def before_agent_callback(self, *, agent, callback_context: CallbackContext) -> None:
        """Pre-warm the A2A client for the current invocation."""
        iid = callback_context.invocation_id
        get_client(iid)

    async def after_agent_callback(self, *, agent, callback_context: CallbackContext) -> None:
        """Cleanup client after the agent run is complete."""
        iid = callback_context.invocation_id
        client = _clients.pop(iid, None)
        if client:
            logger.debug(f"A2A_PLUGIN: Closing client for invocation {iid}")
            await client.close()
