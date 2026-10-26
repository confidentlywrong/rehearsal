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

"""Shared Redis connection pool for the entire process.

Every module that needs Redis should call ``get_shared_redis_client()``
instead of creating its own ``redis.from_url()`` client.  This ensures a
single connection pool is shared across pulses, broadcast, dispatcher,
and race collector -- preventing connection exhaustion under load.

Tuning history
--------------
* 2026-06-24 (Redis pool exhaustion under 1000-runner burst): default
  ``REDIS_MAX_CONNECTIONS`` raised 20 → 50 and pool ``timeout`` raised
  5s → 30s. The in-process runner_autopilot dispatcher fans out up to
  1000 concurrent tickers and the previous defaults left no head-room
  for the burst; raising both buys survivability without changing the
  pool topology. Cloud Run runner services additionally override the
  env to 250 via Terraform (infra/modules/cloud-run-services/main.tf).
"""

import asyncio
import logging
import os

import redis.asyncio as redis

logger = logging.getLogger(__name__)

_shared_clients: dict[asyncio.AbstractEventLoop | None, redis.Redis] = {}


def get_shared_redis_client() -> redis.Redis | None:
    """Return the process-wide Redis client, creating it on first call.

    Uses ``BlockingConnectionPool`` so that callers queue for a connection
    instead of receiving an immediate ``MaxConnectionsError`` when the pool
    is exhausted.  Pool size is configurable via the ``REDIS_MAX_CONNECTIONS``
    environment variable (default 50).  Cloud Run runner services override
    this to 250 via Terraform; Agent Engine planner agents override to 10
    via deploy.py.

    Returns ``None`` if ``REDIS_ADDR`` is not set in the environment.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop in _shared_clients:
        return _shared_clients[loop]

    redis_url = os.environ.get("REDIS_ADDR")
    if not redis_url:
        return None
    # TLS handling: previously the URL was force-prefixed with
    # ``redis://`` which (a) mangled operator-supplied ``rediss://``
    # URLs into ``redis://rediss://host`` and (b) silently downgraded
    # traffic to plaintext. Now:
    #
    # * ``rediss://...`` is honoured as-is (TLS on).
    # * ``redis://...`` is honoured as-is UNLESS the operator opts
    #   into TLS via ``REDIS_TLS_ENABLED``, in which case the scheme
    #   is upgraded to ``rediss://``.
    # * A bare host is prefixed with either ``rediss://`` or
    #   ``redis://`` depending on the same env flag.
    #
    # The env flag is a soft opt-in for existing deployments that
    # set ``REDIS_ADDR=host:port`` and expect the pool to add the
    # scheme; explicit URLs always win.
    tls_enabled = os.environ.get("REDIS_TLS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if redis_url.startswith("rediss://"):
        pass  # explicit TLS URL — leave alone
    elif redis_url.startswith("redis://"):
        if tls_enabled:
            redis_url = "rediss://" + redis_url[len("redis://") :]
    else:
        redis_url = ("rediss://" if tls_enabled else "redis://") + redis_url
    max_conn = int(os.environ.get("REDIS_MAX_CONNECTIONS", "50"))
    redis_password = os.environ.get("REDIS_PASSWORD")
    pool = redis.BlockingConnectionPool.from_url(
        redis_url,
        password=redis_password,
        decode_responses=False,
        max_connections=max_conn,
        timeout=30,  # max seconds to wait for a connection (was 5s; raised 2026-06-24 to survive 1000-runner bursts)
    )
    client = redis.Redis(connection_pool=pool)
    _shared_clients[loop] = client
    return client


async def aclose() -> None:
    """Gracefully close the shared Redis client and its connection pool.

    Safe to call multiple times or when no client was ever created.
    After this call, ``get_shared_redis_client()`` will re-create the pool
    on the next invocation.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    client = _shared_clients.pop(loop, None)
    if client is not None:
        await client.aclose()
