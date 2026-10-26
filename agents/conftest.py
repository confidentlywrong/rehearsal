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

"""Root conftest for all agent tests.

Prevents module-level state from leaking between tests.
"""

# IMPORTANT: These env vars MUST be set before any agent module is imported.
# agents/planner_with_memory/__init__.py eagerly imports agent.py, which calls
# create_session_service() and create_memory_service() at module level.
# If these are not set, ValueError is raised during collection.
#
# Direct assignment (not setdefault) is deliberate: the Makefile loads the
# developer's .env via `-include .env / export`, so any value exported there
# would silently win against setdefault and pollute test runs (the original
# bug behind PR #53). Each entry below documents the import-time consumer
# that motivates pinning it.
import os

# Read by agents/utils/env.py, store_alloydb's SM project resolution chain,
# memory_manager, evaluator/tools.py, simulation_executor, and others.
os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
os.environ["PROJECT_ID"] = "test-project"

# Read by simulation_executor, memory_manager, runtime, embeddings.
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

# Read by runtime.create_session_service / create_memory_service when
# agents/planner_with_memory/__init__.py imports agent.py at collection.
os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ID"] = "test-agent-engine"

# Read by store_alloydb._get_dsn(); pin so tests never accidentally point at
# a real AlloyDB instance from a developer's .env.
os.environ["ALLOYDB_HOST"] = "127.0.0.1"
# Pin password so _resolve_password() never attempts Secret Manager lookups.
os.environ["ALLOYDB_PASSWORD"] = "test"

# SECRET_MANAGER_PROJECT wins the SM project resolution chain in
# store_alloydb. Pop it so resolution falls through to GOOGLE_CLOUD_PROJECT.
os.environ.pop("SECRET_MANAGER_PROJECT", None)

# L6 fix: Prevent real Google Maps API key from leaking into tests.
os.environ.pop("GOOGLE_MAPS_API_KEY", None)

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_simulation_registry():
    """Prevent simulation_registry state from leaking between tests.

    The registry is module-level state; without this fixture, a test that
    calls ``register()`` can pollute subsequent tests' ``lookup()`` results.

    Also patches ``get_shared_redis_client`` to return None in the registry
    module, preventing unintentional Redis writes when other tests (e.g.
    test_redis_pool.py) leak a real ``_shared_client``.
    """
    from agents.utils.simulation_registry import _context_map, _local

    _local.clear()
    _context_map.clear()
    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=None):
        yield
    _local.clear()
    _context_map.clear()


@pytest.fixture(autouse=True)
def _isolate_redis(monkeypatch):
    """Prevent tests from connecting to real Redis.

    Worktree ``.env`` files set ``REDIS_ADDR`` to a real Redis instance.
    If a test triggers ``emit_gateway_message`` without mocking, the
    ``_publish_worker`` in ``pulses.py`` starts a background asyncio task
    that connects to Redis and blocks event-loop teardown indefinitely
    (``queue.get()`` in an infinite loop).

    This fixture removes ``REDIS_ADDR`` from the environment for all tests
    and resets any cached Redis client, so ``get_shared_redis_client()``
    returns ``None``.  Tests that need a real Redis connection should use
    the ``integration`` marker and set up their own mock/connection.
    """
    monkeypatch.delenv("REDIS_ADDR", raising=False)
    # Reset the cached client so it picks up the missing env var.
    from agents.utils import redis_pool
    import asyncio

    def _cleanup():
        clients = list(redis_pool._shared_clients.values())
        redis_pool._shared_clients.clear()
        if hasattr(redis_pool, "_shared_client"):
            redis_pool._shared_client = None
        if not clients:
            return
        async def _close():
            for c in clients:
                try:
                    await c.aclose()
                except Exception:
                    pass
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_close())
        except RuntimeError:
            asyncio.run(_close())

    _cleanup()
    yield
    # Clean up any workers that may have started during the test.
    from agents.utils import pulses

    pulses.reset()
    _cleanup()

@pytest.fixture(autouse=True)
def _mock_google_auth(monkeypatch):
    """Ensure all tests run fully offline without triggering real GCP auth."""
    from unittest.mock import MagicMock
    import google.auth

    mock_creds = MagicMock()
    mock_creds.token = "mock-token"
    # Provide a dummy (credentials, project) tuple
    monkeypatch.setattr(google.auth, "default", lambda *args, **kwargs: (mock_creds, "test-project"))


@pytest.fixture(autouse=True)
def _reset_maps_key_cache(monkeypatch):
    """Prevent the ``_resolved_maps_key`` module cache from leaking between tests.

    ``agents/planner/adk_tools.py`` memoises the resolved Maps API key
    in the module global ``_resolved_maps_key``. If any test (or any
    module imported during a test) re-runs ``load_dotenv()`` after
    ``agents/conftest.py`` popped ``GOOGLE_MAPS_API_KEY``, the real key
    from ``.env`` bleeds back into ``os.environ``. The next agent
    module import that runs ``root_agent = get_agent()`` at module
    top level then resolves that real key and caches it. Downstream
    tests trigger ``get_maps_tools()``, which now bypasses its
    "disabled" guard and attempts a live HTTPS call to
    ``agentregistry.googleapis.com`` using the ``MagicMock``
    credentials from ``_mock_google_auth``, producing a confusing
    ``Header value must be str or bytes, not MagicMock`` failure at
    test setup (see 2026-07-02 test-isolation audit).

    This fixture closes both leak surfaces per test: it re-pops the
    env var (in case some import path resurrected it) AND resets the
    memoised cache to ``None`` so any subsequent resolution starts
    fresh from a clean environment.
    """
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    # The cache for _fetch_maps_key_from_gcloud now properly resets if the project changes.
    from agents.planner import adk_tools as _planner_adk_tools
    _planner_adk_tools._fetch_maps_key_from_gcloud.cache_clear()
    yield
    _planner_adk_tools._fetch_maps_key_from_gcloud.cache_clear()

# Fix Vertex AI SDK attempting to convert test-project to a project ID using the RM API during collection
try:
    from google.cloud.aiplatform.utils import resource_manager_utils
    # We can't use monkeypatch at module level, so we patch it directly.
    resource_manager_utils.get_project_id = lambda project_number, **kwargs: str(project_number)
except ImportError:
    pass
