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
def _parse_max_clients() -> int:
    raw = os.environ.get("COMMUNICATION_PLUGIN_MAX_CLIENTS", "4096").strip()
    if not raw:
        return 4096
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning(
            "A2A_PLUGIN: invalid COMMUNICATION_PLUGIN_MAX_CLIENTS=%r; using default 4096",
            raw,
        )
        return 4096
    if parsed < 1:
        logger.warning(
            "A2A_PLUGIN: non-positive COMMUNICATION_PLUGIN_MAX_CLIENTS=%d; clamping to 1",
            parsed,
        )
        return 1
    return parsed


_MAX_CLIENTS = _parse_max_clients()
_clients: "OrderedDict[str, SimulationA2AClient]" = OrderedDict()
_client_loops: dict[str, asyncio.AbstractEventLoop | None] = {}
_background_close_tasks: set[asyncio.Task[None]] = set()


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
    _client_loops.pop(stale_iid, None)
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
    task = loop.create_task(_safe_close(stale_iid, stale_client))
    _background_close_tasks.add(task)
    task.add_done_callback(_background_close_tasks.discard)


async def _safe_close(iid: str, client: SimulationA2AClient) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        logger.debug("A2A_PLUGIN: eviction close failed for %s", iid, exc_info=True)


def get_client(invocation_id: str) -> SimulationA2AClient:
    """Helper to retrieve the A2A client for a specific invocation."""
    try:
        cur_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        cur_loop = None

    if invocation_id in _clients and _client_loops.get(invocation_id) not in (None, cur_loop):
        if cur_loop is not None:
            logger.warning(
                "A2A_PLUGIN: replacing client for invocation %s bound to a different event loop",
                invocation_id,
            )
            stale_client = _clients.pop(invocation_id)
            _client_loops.pop(invocation_id, None)
            task = cur_loop.create_task(_safe_close(invocation_id, stale_client))
            _background_close_tasks.add(task)
            task.add_done_callback(_background_close_tasks.discard)

    if invocation_id not in _clients:
        logger.debug(f"A2A_PLUGIN: Initializing new client for invocation {invocation_id}")
        _evict_oldest_if_full()
        _clients[invocation_id] = SimulationA2AClient()
        _client_loops[invocation_id] = cur_loop
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
        _client_loops.pop(iid, None)
        if client:
            logger.debug(f"A2A_PLUGIN: Closing client for invocation {iid}")
            await client.close()
