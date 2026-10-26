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

"""Tests for the shared Redis connection pool."""

import os
from unittest.mock import patch

import redis.asyncio as redis

import agents.utils.redis_pool as pool_mod


class TestGetSharedRedisClient:
    def setup_method(self):
        # ``redis_pool`` now keys its cache per-loop (dict) instead of
        # holding a single global, so reset by clearing the dict rather
        # than nulling a nonexistent ``_shared_client`` attribute (the
        # previous form silently created a NEW attribute that the
        # implementation never read, leaving the real cache untouched).
        pool_mod._shared_clients.clear()
        # Clear any leaked REDIS_MAX_CONNECTIONS from other test files
        os.environ.pop("REDIS_MAX_CONNECTIONS", None)

    def teardown_method(self):
        pool_mod._shared_clients.clear()

    def test_returns_redis_client_with_capped_pool(self):
        """Shared client should have max_connections <= 50 (default)."""
        with patch.dict("os.environ", {"REDIS_ADDR": "127.0.0.1:6379"}):
            client = pool_mod.get_shared_redis_client()
            assert client is not None
            assert client.connection_pool.max_connections <= 50

    def test_pool_class_is_blocking(self):
        """Pool must be BlockingConnectionPool to queue callers instead of raising."""
        with patch.dict("os.environ", {"REDIS_ADDR": "127.0.0.1:6379"}):
            client = pool_mod.get_shared_redis_client()
            assert client is not None
            pool = client.connection_pool
            assert isinstance(pool, redis.BlockingConnectionPool), (
                f"Expected BlockingConnectionPool, got {type(pool).__name__}"
            )

    def test_pool_timeout_is_set(self):
        """BlockingConnectionPool should have a timeout so callers don't block forever."""
        with patch.dict("os.environ", {"REDIS_ADDR": "127.0.0.1:6379"}):
            client = pool_mod.get_shared_redis_client()
            assert client is not None
            pool = client.connection_pool
            assert isinstance(pool, redis.BlockingConnectionPool)
            # Raised 5→30 in 2026-06-24 to survive 1000-runner burst (see redis_pool.py docstring).
            assert pool.timeout == 30, f"Expected timeout=30, got {pool.timeout}"

    def test_max_connections_configurable_via_env(self):
        """REDIS_MAX_CONNECTIONS env var should override the default pool size."""
        with patch.dict(
            "os.environ",
            {"REDIS_ADDR": "127.0.0.1:6379", "REDIS_MAX_CONNECTIONS": "42"},
        ):
            client = pool_mod.get_shared_redis_client()
            assert client is not None
            assert client.connection_pool.max_connections == 42

    def test_returns_same_instance_on_repeated_calls(self):
        """Should return the exact same client object (singleton)."""
        with patch.dict("os.environ", {"REDIS_ADDR": "127.0.0.1:6379"}):
            a = pool_mod.get_shared_redis_client()
            b = pool_mod.get_shared_redis_client()
            assert a is b

    def test_returns_none_when_redis_addr_not_set(self):
        """Should return None if REDIS_ADDR is not configured."""
        with patch.dict("os.environ", {}, clear=True):
            client = pool_mod.get_shared_redis_client()
            assert client is None

    def test_bare_host_defaults_to_plaintext(self):
        """A bare host without a scheme should get ``redis://`` when TLS is not enabled."""
        with patch.dict("os.environ", {"REDIS_ADDR": "127.0.0.1:6379"}, clear=True):
            client = pool_mod.get_shared_redis_client()
            assert client is not None
            # SSLConnection is used only for TLS; without opt-in we should
            # be on the plaintext Connection class.
            assert client.connection_pool.connection_class is not redis.SSLConnection

    def test_rediss_scheme_is_preserved(self):
        """An explicit ``rediss://`` URL must NOT be downgraded to plaintext.

        Regression: the previous code force-prefixed with ``redis://``
        which mangled operator-supplied ``rediss://host`` into
        ``redis://rediss://host`` — a plaintext connection attempt to a
        nonsense host. Now an explicit TLS URL is honoured as-is.
        """
        with patch.dict(
            "os.environ", {"REDIS_ADDR": "rediss://127.0.0.1:6379"}, clear=True
        ):
            client = pool_mod.get_shared_redis_client()
            assert client is not None
            assert client.connection_pool.connection_class is redis.SSLConnection

    def test_tls_env_flag_upgrades_bare_host(self):
        """REDIS_TLS_ENABLED=true should upgrade a bare host to ``rediss://``."""
        with patch.dict(
            "os.environ",
            {"REDIS_ADDR": "127.0.0.1:6379", "REDIS_TLS_ENABLED": "true"},
            clear=True,
        ):
            client = pool_mod.get_shared_redis_client()
            assert client is not None
            assert client.connection_pool.connection_class is redis.SSLConnection

    def test_tls_env_flag_upgrades_plaintext_url(self):
        """REDIS_TLS_ENABLED=true should upgrade a ``redis://`` URL to TLS."""
        with patch.dict(
            "os.environ",
            {
                "REDIS_ADDR": "redis://127.0.0.1:6379",
                "REDIS_TLS_ENABLED": "true",
            },
            clear=True,
        ):
            client = pool_mod.get_shared_redis_client()
            assert client is not None
            assert client.connection_pool.connection_class is redis.SSLConnection


@patch.dict(os.environ, {"REDIS_ADDR": "127.0.0.1:6379"}, clear=False)
def test_default_pool_size_is_50():
    """Default REDIS_MAX_CONNECTIONS should be 50 (right-sized for local dev and AE agents)."""
    import agents.utils.redis_pool as rp

    # Reset per-loop singleton cache
    rp._shared_clients.clear()
    os.environ.pop("REDIS_MAX_CONNECTIONS", None)

    client = rp.get_shared_redis_client()
    assert client is not None  # guaranteed when REDIS_ADDR is set
    pool = client.connection_pool  # type: ignore[union-attr]
    assert pool.max_connections == 50

    # Cleanup
    rp._shared_clients.clear()
