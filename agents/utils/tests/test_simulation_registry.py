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

"""Tests for the distributed simulation registry."""

from unittest.mock import AsyncMock, patch

import pytest

from agents.utils.simulation_registry import (
    clear,
    get_context_id,
    lookup,
    register,
    register_context,
    unregister,
    _context_map,
    _local,
)


class TestSimulationRegistry:
    """Tests for the simulation_registry module.

    When Redis is unavailable (tests), the registry falls back to the
    process-local L1 cache.  These tests verify the local-only path.
    """

    @pytest.fixture(autouse=True)
    async def _clear(self):
        """Clear registry before each test."""
        _local.clear()
        yield
        _local.clear()

    @pytest.mark.asyncio
    async def test_register_and_lookup(self):
        """register() should store and lookup() should retrieve."""
        await register("session-1", "sim-abc")
        assert await lookup("session-1") == "sim-abc"

    @pytest.mark.asyncio
    async def test_lookup_unknown_returns_none(self):
        """lookup() on an unknown session should return None."""
        assert await lookup("nonexistent") is None

    @pytest.mark.asyncio
    async def test_unregister(self):
        """unregister() should remove the entry."""
        await register("session-1", "sim-abc")
        await unregister("session-1")
        assert await lookup("session-1") is None

    @pytest.mark.asyncio
    async def test_unregister_unknown_is_noop(self):
        """unregister() on an unknown session should not raise."""
        await unregister("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_clear(self):
        """clear() should remove all entries."""
        await register("s1", "sim-1")
        await register("s2", "sim-2")
        await clear()
        assert await lookup("s1") is None
        assert await lookup("s2") is None

    @pytest.mark.asyncio
    async def test_multiple_sessions_same_simulation(self):
        """Multiple sessions can map to the same simulation_id."""
        await register("s1", "sim-shared")
        await register("s2", "sim-shared")
        await register("s3", "sim-shared")
        assert await lookup("s1") == "sim-shared"
        assert await lookup("s2") == "sim-shared"
        assert await lookup("s3") == "sim-shared"

    @pytest.mark.asyncio
    async def test_overwrite_existing(self):
        """Re-registering a session should overwrite the previous value."""
        await register("session-1", "sim-old")
        await register("session-1", "sim-new")
        assert await lookup("session-1") == "sim-new"


class TestSimulationRegistryRedis:
    """Tests for the Redis-backed cross-instance behavior.

    After the HASH→STRING migration, each session/context gets its own
    Redis key with a 10-minute TTL (SETEX) instead of living in a
    shared hash.
    """

    @pytest.fixture(autouse=True)
    async def _clear(self):
        """Clear registry before each test."""
        _local.clear()
        _context_map.clear()
        yield
        _local.clear()
        _context_map.clear()

    @pytest.mark.asyncio
    async def test_lookup_falls_back_to_redis(self):
        """lookup() should GET per-key STRING when L1 cache is empty."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b"sim-cross-uuid")

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            result = await lookup("session-from-other-instance")

        assert result == "sim-cross-uuid"
        mock_redis.get.assert_awaited_once_with("simreg:session:session-from-other-instance")
        # L1 should be warmed
        assert _local.get("session-from-other-instance") == "sim-cross-uuid"

    @pytest.mark.asyncio
    async def test_register_writes_to_redis(self):
        """register() should SETEX per-key STRING with 7200s TTL (2 hours)."""
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            await register("sess-1", "sim-1")

        mock_redis.setex.assert_awaited_once_with("simreg:session:sess-1", 7200, "sim-1")
        assert _local["sess-1"] == "sim-1"

    @pytest.mark.asyncio
    async def test_clear_deletes_redis_keys(self):
        """clear() should SCAN both prefixes and DELETE matching keys."""
        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock()

        async def mock_scan_session(*args, **kwargs):
            for key in [b"simreg:session:s1", b"simreg:session:s2"]:
                yield key

        async def mock_scan_context(*args, **kwargs):
            for key in [b"simreg:context:v1"]:
                yield key

        # scan_iter returns different results based on match pattern
        call_count = 0

        def scan_iter_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            match = kwargs.get("match", args[0] if args else "")
            if "session" in match:
                return mock_scan_session()
            return mock_scan_context()

        mock_redis.scan_iter = scan_iter_side_effect

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            _local["a"] = "b"
            _context_map["c"] = "d"
            await clear()

        assert len(_local) == 0
        assert len(_context_map) == 0
        # Batched deletes: 2 session keys in one call, 1 context key in another
        assert mock_redis.delete.await_count == 2
        # Verify all keys were included across the batched calls
        all_deleted_keys = []
        for call in mock_redis.delete.await_args_list:
            all_deleted_keys.extend(call.args)
        assert set(all_deleted_keys) == {
            b"simreg:session:s1",
            b"simreg:session:s2",
            b"simreg:context:v1",
        }

    @pytest.mark.asyncio
    async def test_lookup_prefers_l1_over_redis(self):
        """lookup() should return L1 value without hitting Redis."""
        mock_redis = AsyncMock()

        _local["cached-sess"] = "cached-sim"

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            result = await lookup("cached-sess")

        assert result == "cached-sim"
        # Should NOT have called Redis
        mock_redis.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregister_deletes_redis_key(self):
        """unregister() should DELETE both session and context keys from Redis."""
        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock()

        _local["sess-1"] = "sim-1"
        _context_map["sess-1"] = "ctx-1"

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            await unregister("sess-1")

        # M12: now deletes both keys in one call
        mock_redis.delete.assert_awaited_once_with("simreg:session:sess-1", "simreg:context:sess-1")
        assert "sess-1" not in _local
        assert "sess-1" not in _context_map

    @pytest.mark.asyncio
    async def test_register_context_uses_setex(self):
        """register_context() should SETEX per-key STRING with 7200s TTL (2 hours)."""
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            await register_context("vsess-1", "ctx-1")

        mock_redis.setex.assert_awaited_once_with("simreg:context:vsess-1", 7200, "ctx-1")
        assert _context_map["vsess-1"] == "ctx-1"

    @pytest.mark.asyncio
    async def test_get_context_id_uses_get(self):
        """get_context_id() should GET per-key STRING from Redis."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b"ctx-1")

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            result = await get_context_id("vsess-1")

        assert result == "ctx-1"
        mock_redis.get.assert_awaited_once_with("simreg:context:vsess-1")
        # L1 should be warmed
        assert _context_map.get("vsess-1") == "ctx-1"


class TestSingleFlightCoalesce:
    """Verify concurrent L1 misses for the same key collapse to 1 Redis read.

    This is the hot path for DashLogPlugin under 1000-runner bursts: every
    runner event resolves a context_id, so without single-flight a cold cache
    causes 1000 concurrent GETs that saturate the Redis pool.
    """

    @pytest.fixture(autouse=True)
    async def _clear(self):
        from agents.utils.simulation_registry import _inflight_context, _inflight_session

        _local.clear()
        _context_map.clear()
        _inflight_context.clear()
        _inflight_session.clear()
        yield
        _local.clear()
        _context_map.clear()
        _inflight_context.clear()
        _inflight_session.clear()

    @pytest.mark.asyncio
    async def test_concurrent_get_context_id_coalesces_to_single_redis_read(self):
        """100 concurrent get_context_id(same key) on a cold cache → 1 Redis GET."""
        import asyncio

        get_calls = 0

        async def slow_get(key):
            nonlocal get_calls
            get_calls += 1
            # Hold the connection long enough that all 100 callers pile up.
            await asyncio.sleep(0.05)
            return b"ctx-coalesced"

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=slow_get)

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            results = await asyncio.gather(*[get_context_id("vsess-burst") for _ in range(100)])

        assert all(r == "ctx-coalesced" for r in results), "All callers see same value"
        assert get_calls == 1, f"Single-flight should collapse 100 concurrent calls to 1 Redis GET, got {get_calls}"
        assert mock_redis.get.await_count == 1
        # L1 is warmed for subsequent (post-burst) calls.
        assert _context_map.get("vsess-burst") == "ctx-coalesced"

    @pytest.mark.asyncio
    async def test_concurrent_lookup_coalesces_to_single_redis_read(self):
        """100 concurrent lookup(same key) on a cold cache → 1 Redis GET."""
        import asyncio

        async def slow_get(key):
            await asyncio.sleep(0.05)
            return b"sim-coalesced"

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=slow_get)

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            results = await asyncio.gather(*[lookup("sess-burst") for _ in range(100)])

        assert all(r == "sim-coalesced" for r in results)
        assert mock_redis.get.await_count == 1
        assert _local.get("sess-burst") == "sim-coalesced"

    @pytest.mark.asyncio
    async def test_inflight_entry_cleared_after_completion(self):
        """After a coalesced lookup completes, the in-flight map is empty."""
        from agents.utils.simulation_registry import _inflight_context

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b"ctx-1")

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            await get_context_id("vsess-clean")

        assert "vsess-clean" not in _inflight_context, "Single-flight Future must be removed in finally block"

    @pytest.mark.asyncio
    async def test_redis_exception_propagates_to_all_awaiters(self):
        """If the lead Redis read raises, awaiting coalesced callers must not hang.

        Behaviour: the lead caller catches the exception (already-existing
        try/except) and resolves the Future with None. Coalesced callers
        therefore see None too, never deadlock.
        """
        import asyncio

        async def boom(key):
            await asyncio.sleep(0.02)
            raise ConnectionError("simulated redis outage")

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=boom)

        with patch(
            "agents.utils.simulation_registry.get_shared_redis_client",
            return_value=mock_redis,
        ):
            results = await asyncio.gather(*[get_context_id("vsess-err") for _ in range(10)])

        assert all(r is None for r in results)
        # One Redis call — the other 9 awaited the same Future.
        assert mock_redis.get.await_count == 1


class TestL1CacheBound:
    """Verify the L1 caches cap at ``SIMREG_L1_MAX_ENTRIES``.

    Without this cap, a long-lived runner_autopilot instance accumulates
    one entry per (session_id, context_id) it observes, eventually OOM'ing.
    """

    @pytest.fixture(autouse=True)
    async def _clear(self):
        _local.clear()
        _context_map.clear()
        yield
        _local.clear()
        _context_map.clear()

    @pytest.mark.asyncio
    async def test_local_cache_evicts_oldest_when_over_cap(self, monkeypatch):
        """register() past the cap evicts FIFO; cap is observable via _local len."""
        import importlib
        import agents.utils.simulation_registry as sr

        monkeypatch.setenv("SIMREG_L1_MAX_ENTRIES", "3")
        importlib.reload(sr)
        try:
            await sr.register("s1", "sim-1")
            await sr.register("s2", "sim-2")
            await sr.register("s3", "sim-3")
            assert len(sr._local) == 3
            await sr.register("s4", "sim-4")
            assert len(sr._local) == 3
            # s1 should have been evicted (oldest).
            assert "s1" not in sr._local
            assert "s4" in sr._local
            assert sr._local["s2"] == "sim-2"
        finally:
            sr._local.clear()
            sr._context_map.clear()
            monkeypatch.delenv("SIMREG_L1_MAX_ENTRIES", raising=False)
            importlib.reload(sr)

    @pytest.mark.asyncio
    async def test_context_map_evicts_oldest_when_over_cap(self, monkeypatch):
        """register_context() past the cap evicts FIFO."""
        import importlib
        import agents.utils.simulation_registry as sr

        monkeypatch.setenv("SIMREG_L1_MAX_ENTRIES", "2")
        importlib.reload(sr)
        try:
            await sr.register_context("v1", "ctx-1")
            await sr.register_context("v2", "ctx-2")
            await sr.register_context("v3", "ctx-3")
            assert len(sr._context_map) == 2
            assert "v1" not in sr._context_map
            assert "v3" in sr._context_map
        finally:
            sr._local.clear()
            sr._context_map.clear()
            monkeypatch.delenv("SIMREG_L1_MAX_ENTRIES", raising=False)
            importlib.reload(sr)

    @pytest.mark.asyncio
    async def test_re_register_renews_eviction_order(self, monkeypatch):
        """Re-registering the same key moves it to most-recent, surviving the next eviction."""
        import importlib
        import agents.utils.simulation_registry as sr

        monkeypatch.setenv("SIMREG_L1_MAX_ENTRIES", "3")
        importlib.reload(sr)
        try:
            await sr.register("s1", "sim-1")
            await sr.register("s2", "sim-2")
            await sr.register("s3", "sim-3")
            # Renew s1 — it should now be most-recent, so s2 is oldest.
            await sr.register("s1", "sim-1-v2")
            await sr.register("s4", "sim-4")
            assert "s1" in sr._local
            assert sr._local["s1"] == "sim-1-v2"
            assert "s2" not in sr._local  # evicted instead of s1
        finally:
            sr._local.clear()
            sr._context_map.clear()
            monkeypatch.delenv("SIMREG_L1_MAX_ENTRIES", raising=False)
            importlib.reload(sr)
