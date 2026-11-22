# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: fix zero/negative max_events pruning and disconnect replaced Redis connection pool.
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

"""RedisSessionService subclass that prunes events to prevent blob growth.

The community RedisSessionService serializes the entire Session (including all
accumulated events) as a single Redis blob on every append_event().  At 1000
runners x 4 events/tick, the blob grows linearly and causes progressive
degradation via serialization + network overhead.

This subclass prunes session.events to the most recent ``max_events`` BEFORE
the parent writes the blob, keeping the stored payload constant regardless of
how many ticks have elapsed.  State (app_state/user_state) is unaffected --
it's stored in separate Redis HASHes.
"""

import asyncio
import inspect
import logging

import redis.asyncio as redis

from google.adk.events.event import Event
from google.adk.sessions.session import Session
from google.adk_community.sessions import RedisSessionService

logger = logging.getLogger(__name__)
_background_cleanup_tasks: set[asyncio.Task[object]] = set()


def _disconnect_replaced_pool(old_pool: object) -> None:
    """Best-effort disconnect of a replaced Redis connection pool."""
    disconnect = getattr(old_pool, "disconnect", None)
    if not callable(disconnect):
        return
    try:
        try:
            result = disconnect(inuse_connections=False)
        except TypeError:
            result = disconnect()
    except Exception:
        logger.debug("PrunedRedisSessionService: failed to disconnect replaced pool", exc_info=True)
        return

    if not inspect.isawaitable(result):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if inspect.iscoroutine(result):
            result.close()
        return

    task: asyncio.Task[object] = loop.create_task(result)  # type: ignore[arg-type]
    _background_cleanup_tasks.add(task)

    def _on_done(done_task: asyncio.Task[object]) -> None:
        _background_cleanup_tasks.discard(done_task)
        if not done_task.cancelled():
            exc = done_task.exception()
            if exc is not None:
                logger.warning("PrunedRedisSessionService: pool disconnect failed: %s", exc)

    task.add_done_callback(_on_done)


class PrunedRedisSessionService(RedisSessionService):
    """RedisSessionService that caps stored events to prevent blob growth.

    Also replaces the community library's regular ``ConnectionPool`` with a
    ``BlockingConnectionPool`` so that callers queue for a connection instead
    of receiving an immediate ``ConnectionError("Too many connections")``
    when the pool is exhausted during broadcast bursts.
    """

    def __init__(self, *args, max_events: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_events = max_events

        # The community RedisSessionService creates a plain ConnectionPool
        # via Redis.from_url() or Redis(). Replace it with a blocking pool
        # so 50+ concurrent runners queue for connections instead of crashing.
        cache = getattr(self, "cache", None)
        if cache is not None and hasattr(cache, "connection_pool"):
            old_pool = cache.connection_pool
            if not isinstance(old_pool, redis.BlockingConnectionPool):
                max_conn = old_pool.max_connections
                pool_kwargs = old_pool.connection_kwargs.copy()
                new_pool = redis.BlockingConnectionPool(
                    max_connections=max_conn,
                    timeout=10,  # seconds to wait for a connection
                    **pool_kwargs,
                )
                cache.connection_pool = new_pool
                _disconnect_replaced_pool(old_pool)
                logger.info(
                    "PrunedRedisSessionService: replaced ConnectionPool with "
                    "BlockingConnectionPool (max_connections=%d, timeout=10s)",
                    max_conn,
                )

    async def append_event(self, session: Session, event: Event) -> Event:
        """Prune old events before the parent serializes the session blob."""
        if self.max_events <= 0:
            session.events = []
        elif len(session.events) > self.max_events:
            session.events = session.events[-self.max_events :]
        return await super().append_event(session=session, event=event)
