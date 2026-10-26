# Copyright 2026 The Confidently Wrong Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Phase 2.5b cross-tenant isolation tests for the dispatcher helpers.

These tests prove three guarantees:

1. SAAS_MODE=off (default) → spawn queue names and broadcast channel
   names are byte-identical to today.  Existing single-tenant
   deployments continue to BLPOP / PUB-SUB on exactly the same keys.
2. SAAS_MODE=on + DISPATCHER_USER_ID=user1 → every name the dispatcher
   touches is prefixed with ``u:user1:`` so user A's dispatcher never
   reads user B's queues or channels.
3. The simulator's per-simulation broadcast channel
   (``simulation:{sim}:broadcast``) is also tenant-scoped, so two users
   that happen to pick the same simulation id never cross-leak.

These are unit tests over the pure helper functions — no Redis needed.
"""

from __future__ import annotations

import pytest

from agents.simulator import broadcast as sim_broadcast
from agents.utils import dispatcher


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Clean SAAS_MODE / DISPATCHER_USER_ID between tests."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)
    monkeypatch.delenv("ACCOUNT_KEY_PREFIX", raising=False)
    yield


class TestLegacyByteIdentical:
    """SAAS_MODE=off must emit today's exact strings."""

    def test_spawn_queue_names_unchanged(self):
        got = dispatcher.spawn_queue_names("runner_autopilot", num_shards=4)
        assert got == [
            "simulation:spawns:runner_autopilot:0",
            "simulation:spawns:runner_autopilot:1",
            "simulation:spawns:runner_autopilot:2",
            "simulation:spawns:runner_autopilot:3",
        ]

    def test_global_broadcast_channel_unchanged(self):
        assert dispatcher._global_broadcast_channel() == "simulation:broadcast"

    def test_scoped_broadcast_channel_unchanged(self):
        assert dispatcher._scoped_broadcast_channel("sim-42") == "simulation:sim-42:broadcast"
        assert sim_broadcast._scoped_broadcast_channel("sim-42") == "simulation:sim-42:broadcast"

    def test_saas_off_with_user_id_set_still_legacy(self, monkeypatch):
        # Tripwire: even if someone leaks DISPATCHER_USER_ID into a
        # SAAS_MODE=off environment, names stay legacy. This protects
        # the off path from misconfigured env files.
        monkeypatch.setenv("DISPATCHER_USER_ID", "user1")
        assert dispatcher._global_broadcast_channel() == "simulation:broadcast"
        assert dispatcher.spawn_queue_names("runner")[0] == "simulation:spawns:runner:0"


class TestSaaSOnTenantPrefixed:
    """SAAS_MODE=on + DISPATCHER_USER_ID → keys carry the tenant prefix."""

    def test_spawn_queue_names_prefixed(self, monkeypatch):
        monkeypatch.setenv("SAAS_MODE", "on")
        monkeypatch.setenv("DISPATCHER_USER_ID", "user1")
        monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

        got = dispatcher.spawn_queue_names("runner_autopilot", num_shards=2)
        assert got == [
            "u:user1:simulation:spawns:runner_autopilot:0",
            "u:user1:simulation:spawns:runner_autopilot:1",
        ]

    def test_global_broadcast_channel_prefixed(self, monkeypatch):
        monkeypatch.setenv("SAAS_MODE", "on")
        monkeypatch.setenv("DISPATCHER_USER_ID", "user1")
        monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
        assert dispatcher._global_broadcast_channel() == "u:user1:simulation:broadcast"

    def test_scoped_broadcast_channel_prefixed(self, monkeypatch):
        monkeypatch.setenv("SAAS_MODE", "on")
        monkeypatch.setenv("DISPATCHER_USER_ID", "globex")
        monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
        # Both helpers (dispatcher and simulator) must agree on the
        # exact channel string, otherwise publisher and subscriber miss
        # each other.
        d = dispatcher._scoped_broadcast_channel("sim-7")
        s = sim_broadcast._scoped_broadcast_channel("sim-7")
        assert d == "u:globex:simulation:sim-7:broadcast"
        assert s == "u:globex:simulation:sim-7:broadcast"

    def test_saas_on_without_user_id_returns_legacy(self, monkeypatch):
        monkeypatch.setenv("SAAS_MODE", "on")
        assert dispatcher._global_broadcast_channel() == "simulation:broadcast"
        assert dispatcher.spawn_queue_names("runner_autopilot", num_shards=1) == [
            "simulation:spawns:runner_autopilot:0"
        ]

    def test_respects_custom_tenant_prefix(self, monkeypatch):
        monkeypatch.setenv("SAAS_MODE", "on")
        monkeypatch.setenv("DISPATCHER_USER_ID", "user1")
        monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "rc")

        got = dispatcher.spawn_queue_names("runner_autopilot", num_shards=2)
        assert got == [
            "rc:user1:simulation:spawns:runner_autopilot:0",
            "rc:user1:simulation:spawns:runner_autopilot:1",
        ]
        assert dispatcher._global_broadcast_channel() == "rc:user1:simulation:broadcast"
        assert dispatcher._scoped_broadcast_channel("sim-7") == "rc:user1:simulation:sim-7:broadcast"
