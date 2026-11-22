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

"""Tests for SessionManager — cross-worker session mapping via Redis L2 cache.

Verifies that the context_id → session_id mapping is shared across workers
via Redis, with graceful fallback to in-process TTLCache when Redis is
unavailable.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clean_saas_env(monkeypatch):
    """Ensure SAAS_MODE / ACCOUNT_KEY_PREFIX never leak between tests."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("ACCOUNT_KEY_PREFIX", raising=False)


@pytest.fixture()
def mock_session_service():
    """Mock session service that returns sessions with incrementing IDs."""
    service = AsyncMock()
    call_count = 0

    async def create_session(*, app_name, user_id):
        nonlocal call_count
        call_count += 1
        return SimpleNamespace(id=f"vertex-session-{call_count}")

    service.create_session = AsyncMock(side_effect=create_session)
    return service


@pytest.fixture()
def mock_redis():
    """Mock async Redis client with in-memory dict backing."""
    store: dict[str, bytes] = {}
    client = AsyncMock()

    async def redis_get(key):
        return store.get(key)

    async def redis_set(key, value, ex=None):
        store[key] = value if isinstance(value, bytes) else value.encode()

    client.get = AsyncMock(side_effect=redis_get)
    client.set = AsyncMock(side_effect=redis_set)
    client._test_store = store  # expose for assertions
    return client


class TestSessionManagerRedisL2:
    """When Redis is available, SessionManager shares mappings across workers."""

    @pytest.mark.asyncio
    async def test_stores_mapping_in_redis_on_create(self, mock_session_service, mock_redis):
        """New session mapping is stored in both L1 (TTLCache) and L2 (Redis)."""
        from agents.utils.session_manager import SessionManager

        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)

            session_id = await mgr.get_or_create_session(context_id="ctx-001", app_name="test", user_id="user1")

            assert session_id == "vertex-session-1"
            # L1 cache populated
            assert mgr.session_cache.get("ctx-001") == "vertex-session-1"
            # L2 Redis populated
            mock_redis.set.assert_called_once()
            call_args = mock_redis.set.call_args
            assert call_args[0][0] == "session_map:ctx-001"
            assert call_args[0][1] == "vertex-session-1"

    @pytest.mark.asyncio
    async def test_redis_hit_avoids_session_creation(self, mock_session_service, mock_redis):
        """When L1 misses but Redis has the mapping, no new session is created."""
        from agents.utils.session_manager import SessionManager

        # Pre-populate Redis with existing mapping
        mock_redis._test_store["session_map:ctx-002"] = b"existing-vertex-session"

        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)

            session_id = await mgr.get_or_create_session(context_id="ctx-002", app_name="test", user_id="user1")

            assert session_id == "existing-vertex-session"
            mock_session_service.create_session.assert_not_called()
            # L1 cache should be populated from Redis hit
            assert mgr.session_cache.get("ctx-002") == "existing-vertex-session"

    @pytest.mark.asyncio
    async def test_cross_worker_session_continuity(self, mock_session_service, mock_redis):
        """Two SessionManager instances (simulating workers) share via Redis."""
        from agents.utils.session_manager import SessionManager

        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            worker1 = SessionManager(session_service=mock_session_service)
            worker2 = SessionManager(session_service=mock_session_service)

            # Worker 1 creates the session
            sid1 = await worker1.get_or_create_session(context_id="ctx-003", app_name="test", user_id="user1")

            # Worker 2 resolves the SAME session via Redis
            sid2 = await worker2.get_or_create_session(context_id="ctx-003", app_name="test", user_id="user1")

            assert sid1 == sid2 == "vertex-session-1"
            # Only ONE session was created (not two)
            assert mock_session_service.create_session.call_count == 1


class TestSessionManagerDefaultTTL:
    """Verify default TTL is 7200 seconds (2 hours)."""

    @pytest.mark.asyncio
    async def test_default_redis_ttl_is_7200_seconds(self, mock_session_service, mock_redis):
        """SETEX should be called with TTL=7200 (2 hours) by default."""
        from agents.utils.session_manager import SessionManager

        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)

            await mgr.get_or_create_session(context_id="ctx-ttl", app_name="test", user_id="user1")

            mock_redis.set.assert_called_once()
            call_args = mock_redis.set.call_args
            # ex kwarg is the TTL
            assert call_args.kwargs["ex"] == 7200, f"Expected TTL=7200, got {call_args.kwargs.get('ex')}"

    @pytest.mark.asyncio
    async def test_default_ttl_cache_ttl_is_7200(self, mock_session_service, mock_redis):
        """TTLCache should default to 7200s TTL (2 hours)."""
        from agents.utils.session_manager import SessionManager

        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)

            assert mgr.session_cache._ttl == 7200, f"Expected TTLCache TTL=7200, got {mgr.session_cache._ttl}"


class TestSessionManagerNoRedis:
    """When Redis is unavailable, SessionManager falls back to L1-only."""

    @pytest.mark.asyncio
    async def test_no_redis_still_works(self, mock_session_service):
        """Without Redis, sessions are created normally (L1-only)."""
        from agents.utils.session_manager import SessionManager

        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=None):
            mgr = SessionManager(session_service=mock_session_service)

            session_id = await mgr.get_or_create_session(context_id="ctx-004", app_name="test", user_id="user1")

            assert session_id == "vertex-session-1"
            assert mgr.session_cache.get("ctx-004") == "vertex-session-1"

    @pytest.mark.asyncio
    async def test_redis_error_degrades_gracefully(self, mock_session_service):
        """Redis connection errors don't crash — fall back to creating a session."""
        from agents.utils.session_manager import SessionManager

        broken_redis = AsyncMock()
        broken_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
        broken_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))

        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=broken_redis):
            mgr = SessionManager(session_service=mock_session_service)

            session_id = await mgr.get_or_create_session(context_id="ctx-005", app_name="test", user_id="user1")

            # Should still work — just without Redis L2
            assert session_id == "vertex-session-1"
            assert mgr.session_cache.get("ctx-005") == "vertex-session-1"


class TestSessionManagerTenantScoping:
    """Verify SAAS_MODE=off vs SAAS_MODE=on Redis and L1 key schema parity."""

    @pytest.mark.asyncio
    async def test_saas_off_ignores_user_id_for_key(self, mock_session_service, mock_redis, monkeypatch):
        from agents.utils.session_manager import SessionManager

        monkeypatch.setenv("SAAS_MODE", "off")
        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)
            session_id = await mgr.get_or_create_session(
                context_id="spawn-ctx-abc",
                app_name="leadership_planner",
                user_id="local-dev-user",
            )

        assert session_id == "vertex-session-1"
        assert "session_map:spawn-ctx-abc" in mock_redis._test_store
        assert mock_redis._test_store["session_map:spawn-ctx-abc"] == b"vertex-session-1"
        assert mgr.get_session_id("spawn-ctx-abc") == "vertex-session-1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("saas_val", ["on", "true", "1", "yes", " ON "])
    async def test_saas_on_namespaces_redis_key_by_user_id(
        self, mock_session_service, mock_redis, monkeypatch, saas_val
    ):
        from agents.utils.session_manager import SessionManager

        monkeypatch.setenv("SAAS_MODE", saas_val)
        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)
            session_id = await mgr.get_or_create_session(
                context_id="spawn-ctx-1",
                app_name="leadership_planner",
                user_id="user-alice",
            )

        assert session_id == "vertex-session-1"
        assert "session_map:user-alice:spawn-ctx-1" in mock_redis._test_store
        assert "session_map:spawn-ctx-1" not in mock_redis._test_store
        assert mock_redis._test_store["session_map:user-alice:spawn-ctx-1"] == b"vertex-session-1"

    @pytest.mark.asyncio
    async def test_saas_on_redis_hit_only_matches_same_tenant(self, mock_session_service, mock_redis, monkeypatch):
        from agents.utils.session_manager import SessionManager

        monkeypatch.setenv("SAAS_MODE", "on")
        mock_redis._test_store["session_map:ctx-shared"] = b"legacy-session"
        mock_redis._test_store["session_map:user-bob:ctx-shared"] = b"bob-session"
        mock_redis._test_store["session_map:user-alice:ctx-shared"] = b"alice-session"

        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)
            alice_sid = await mgr.get_or_create_session(
                context_id="ctx-shared",
                app_name="leadership_planner",
                user_id="user-alice",
            )
            bob_sid = await mgr.get_or_create_session(
                context_id="ctx-shared",
                app_name="leadership_planner",
                user_id="user-bob",
            )

        assert alice_sid == "alice-session"
        assert bob_sid == "bob-session"
        mock_session_service.create_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_saas_on_l1_cache_isolated_across_tenants(self, mock_session_service, mock_redis, monkeypatch):
        from agents.utils.session_manager import SessionManager

        monkeypatch.setenv("SAAS_MODE", "on")
        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)
            alice_sid = await mgr.get_or_create_session(
                context_id="same-ctx",
                app_name="test",
                user_id="user-alice",
            )
            bob_sid = await mgr.get_or_create_session(
                context_id="same-ctx",
                app_name="test",
                user_id="user-bob",
            )

        assert alice_sid == "vertex-session-1"
        assert bob_sid == "vertex-session-2"
        assert alice_sid != bob_sid
        assert mock_redis._test_store["session_map:user-alice:same-ctx"] == b"vertex-session-1"
        assert mock_redis._test_store["session_map:user-bob:same-ctx"] == b"vertex-session-2"
        assert mgr.get_session_id("same-ctx", user_id="user-alice") == "vertex-session-1"
        assert mgr.get_session_id("same-ctx", user_id="user-bob") == "vertex-session-2"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_user_id", ["", "   ", "alice:bob", "alice\nbob", "alice bob"])
    async def test_saas_on_rejects_missing_or_invalid_user_id(
        self, mock_session_service, mock_redis, monkeypatch, bad_user_id
    ):
        from agents.utils.session_manager import SessionManager

        monkeypatch.setenv("SAAS_MODE", "on")
        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)
            with pytest.raises(ValueError):
                await mgr.get_or_create_session(
                    context_id="ctx-bad-user",
                    app_name="test",
                    user_id=bad_user_id,
                )

        mock_session_service.create_session.assert_not_called()
        mock_redis.get.assert_not_called()
        mock_redis.set.assert_not_called()
        assert mock_redis._test_store == {}

    @pytest.mark.asyncio
    async def test_rejects_empty_context_id(self, mock_session_service, mock_redis, monkeypatch):
        from agents.utils.session_manager import SessionManager

        monkeypatch.setenv("SAAS_MODE", "off")
        with patch("agents.utils.session_manager.get_shared_redis_client", return_value=mock_redis):
            mgr = SessionManager(session_service=mock_session_service)
            with pytest.raises(ValueError, match="context_id"):
                await mgr.get_or_create_session(context_id="   ", app_name="test", user_id="user1")

        mock_session_service.create_session.assert_not_called()
