# Copyright 2026 The Confidently Wrong Authors
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

"""Tests for leadership scenario memory schemas and ScenarioMemoryStore.

Mirrors agents/planner_with_memory/tests/test_memory_store.py.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agents.leadership_planner_with_memory.memory.schemas import (
    PlannedScenario,
    SimulationRecord,
)
from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSimulationRecord:
    def test_creation_with_all_fields(self):
        now = datetime.now(tz=timezone.utc)
        record = SimulationRecord(
            simulation_id="sim-001",
            scenario_id="sc-001",
            simulation_result={"passed": True, "score": 0.95},
            simulated_at=now,
        )
        assert record.simulation_id == "sim-001"
        assert record.scenario_id == "sc-001"
        assert record.simulation_result == {"passed": True, "score": 0.95}
        assert record.simulated_at == now


class TestPlannedScenario:
    def test_creation_with_required_fields(self):
        now = datetime.now(tz=timezone.utc)
        scenario = PlannedScenario(
            scenario_id="sc-001",
            scenario_data={"title": "Budget Crisis", "stakeholders": 5},
            created_at=now,
        )
        assert scenario.scenario_id == "sc-001"
        assert scenario.scenario_data == {"title": "Budget Crisis", "stakeholders": 5}
        assert scenario.created_at == now
        assert scenario.evaluation_score is None
        assert scenario.evaluation_result is None
        assert scenario.persona is None
        assert scenario.simulations == []

    def test_creation_with_all_fields(self):
        now = datetime.now(tz=timezone.utc)
        sim = SimulationRecord(
            simulation_id="sim-001",
            scenario_id="sc-001",
            simulation_result={"passed": True},
            simulated_at=now,
        )
        scenario = PlannedScenario(
            scenario_id="sc-001",
            scenario_data={"title": "Budget Crisis"},
            created_at=now,
            evaluation_score=88.5,
            evaluation_result={"passed": True, "overall_score": 88.5},
            persona="pragmatist",
            simulations=[sim],
        )
        assert scenario.evaluation_score == 88.5
        assert scenario.persona == "pragmatist"
        assert len(scenario.simulations) == 1
        assert scenario.simulations[0].simulation_id == "sim-001"

    def test_simulations_default_factory_isolation(self):
        a = PlannedScenario(scenario_id="a", scenario_data={}, created_at=datetime.now(tz=timezone.utc))
        b = PlannedScenario(scenario_id="b", scenario_data={}, created_at=datetime.now(tz=timezone.utc))
        a.simulations.append(
            SimulationRecord(
                simulation_id="s",
                scenario_id="a",
                simulation_result={},
                simulated_at=datetime.now(tz=timezone.utc),
            )
        )
        assert b.simulations == []


# ---------------------------------------------------------------------------
# ScenarioMemoryStore tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestScenarioMemoryStore:
    async def test_store_and_get(self):
        store = ScenarioMemoryStore()
        sid = await store.store_scenario(
            scenario_data={"title": "Budget Crisis"},
            evaluation_score=80.0,
            evaluation_result={"overall_score": 80.0},
            persona="pragmatist",
        )
        assert isinstance(sid, str)

        scenario = await store.get_scenario(sid)
        assert scenario is not None
        assert scenario.scenario_id == sid
        assert scenario.scenario_data == {"title": "Budget Crisis"}
        assert scenario.evaluation_score == 80.0
        assert scenario.persona == "pragmatist"

    async def test_get_unknown_returns_none(self):
        store = ScenarioMemoryStore()
        assert await store.get_scenario("does-not-exist") is None

    async def test_record_simulation_attaches_record(self):
        store = ScenarioMemoryStore()
        sid = await store.store_scenario(scenario_data={"t": 1})
        sim_id = await store.record_simulation(sid, {"result": "ok"})
        assert isinstance(sim_id, str)

        scenario = await store.get_scenario(sid)
        assert len(scenario.simulations) == 1
        assert scenario.simulations[0].simulation_id == sim_id
        assert scenario.simulations[0].simulation_result == {"result": "ok"}

    async def test_record_simulation_unknown_scenario_returns_none(self):
        store = ScenarioMemoryStore()
        assert await store.record_simulation("missing", {"x": 1}) is None

    async def test_recall_scenarios_sort_recent(self):
        store = ScenarioMemoryStore()
        a = await store.store_scenario(scenario_data={"n": "a"})
        # Manually push back a's timestamp so b is clearly more recent.
        store._scenarios[a].created_at = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
        b = await store.store_scenario(scenario_data={"n": "b"})

        result = await store.recall_scenarios(count=10, sort_by="recent")
        assert [s.scenario_id for s in result] == [b, a]

    async def test_recall_scenarios_sort_best_score(self):
        store = ScenarioMemoryStore()
        low = await store.store_scenario(scenario_data={"n": "low"}, evaluation_score=10.0)
        high = await store.store_scenario(scenario_data={"n": "high"}, evaluation_score=90.0)
        unscored = await store.store_scenario(scenario_data={"n": "unscored"})

        result = await store.recall_scenarios(count=10, sort_by="best_score")
        assert result[0].scenario_id == high
        assert result[1].scenario_id == low
        assert result[-1].scenario_id == unscored

    async def test_recall_scenarios_count_limit(self):
        store = ScenarioMemoryStore()
        for _ in range(5):
            await store.store_scenario(scenario_data={})
        result = await store.recall_scenarios(count=2)
        assert len(result) == 2

    async def test_get_best_scenario(self):
        store = ScenarioMemoryStore()
        await store.store_scenario(scenario_data={}, evaluation_score=42.0)
        winner = await store.store_scenario(scenario_data={}, evaluation_score=99.0)
        await store.store_scenario(scenario_data={})  # unscored

        best = await store.get_best_scenario()
        assert best is not None
        assert best.scenario_id == winner

    async def test_get_best_scenario_none_when_no_scores(self):
        store = ScenarioMemoryStore()
        await store.store_scenario(scenario_data={})
        assert await store.get_best_scenario() is None

    async def test_store_scenario_idempotent_preserves_id(self):
        store = ScenarioMemoryStore()
        original_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await store.store_scenario_idempotent(
            scenario_id="fixed-id",
            scenario_data={"seeded": True},
            created_at=original_time,
            evaluation_score=55.0,
        )
        scenario = await store.get_scenario("fixed-id")
        assert scenario is not None
        assert scenario.created_at == original_time
        assert scenario.evaluation_score == 55.0

        # Second call with the same id is a no-op (does not overwrite).
        await store.store_scenario_idempotent(
            scenario_id="fixed-id",
            scenario_data={"seeded": False},
            created_at=datetime.now(tz=timezone.utc),
        )
        scenario = await store.get_scenario("fixed-id")
        assert scenario.scenario_data == {"seeded": True}


# ---------------------------------------------------------------------------
# Per-user tenancy (owner_id) isolation tests
# ---------------------------------------------------------------------------


class TestOwnerIsolation:
    """The ``owner_id`` parameter scopes reads/writes to a single user.

    Under per-user tenancy, two users sharing the same ``ScenarioMemoryStore``
    process must not see each other's data. Legacy unowned rows
    (``owner_id is None``) remain visible only to callers that pass no
    ``owner_id`` (single-tenant view).
    """

    @pytest.mark.asyncio
    async def test_recall_filters_by_owner(self):
        store = ScenarioMemoryStore()
        await store.store_scenario(scenario_data={"t": "alice-1"}, owner_id="alice")
        await store.store_scenario(scenario_data={"t": "bob-1"}, owner_id="bob")
        await store.store_scenario(scenario_data={"t": "bob-2"}, owner_id="bob")

        alice_view = await store.recall_scenarios(owner_id="alice")
        bob_view = await store.recall_scenarios(owner_id="bob")
        unscoped_view = await store.recall_scenarios()

        assert {s.scenario_data["t"] for s in alice_view} == {"alice-1"}
        assert {s.scenario_data["t"] for s in bob_view} == {"bob-1", "bob-2"}
        # No owner_id => legacy single-tenant view sees everything.
        assert len(unscoped_view) == 3

    @pytest.mark.asyncio
    async def test_get_scenario_blocks_other_owner(self):
        store = ScenarioMemoryStore()
        alice_sid = await store.store_scenario(scenario_data={"x": 1}, owner_id="alice")

        # Owner can read.
        assert await store.get_scenario(alice_sid, owner_id="alice") is not None
        # Other user gets None even though the id is valid.
        assert await store.get_scenario(alice_sid, owner_id="bob") is None
        # No-owner reader still sees it (single-tenant fallback).
        assert await store.get_scenario(alice_sid) is not None

    @pytest.mark.asyncio
    async def test_update_evaluation_blocks_other_owner(self):
        store = ScenarioMemoryStore()
        alice_sid = await store.store_scenario(scenario_data={"x": 1}, owner_id="alice")

        # Bob cannot backfill Alice's evaluation.
        ok = await store.update_evaluation(
            alice_sid,
            evaluation_score=99.0,
            evaluation_result={"hijacked": True},
            owner_id="bob",
        )
        assert ok is False
        scenario = await store.get_scenario(alice_sid)
        assert scenario.evaluation_score is None
        assert scenario.evaluation_result is None

        # Alice can.
        ok = await store.update_evaluation(
            alice_sid,
            evaluation_score=42.0,
            evaluation_result={"ok": True},
            owner_id="alice",
        )
        assert ok is True
        scenario = await store.get_scenario(alice_sid)
        assert scenario.evaluation_score == 42.0

    @pytest.mark.asyncio
    async def test_record_simulation_blocks_other_owner(self):
        store = ScenarioMemoryStore()
        alice_sid = await store.store_scenario(scenario_data={"x": 1}, owner_id="alice")

        # Bob cannot attach a sim record to Alice's scenario.
        sim_id = await store.record_simulation(alice_sid, simulation_result={"step": "bob"}, owner_id="bob")
        assert sim_id is None
        scenario = await store.get_scenario(alice_sid)
        assert scenario.simulations == []

    @pytest.mark.asyncio
    async def test_owned_recall_excludes_legacy_unowned_rows(self):
        """Strict isolation: once a caller passes owner_id, legacy
        unowned data is invisible. This forces an explicit backfill
        decision rather than silently leaking pre-tenancy data into
        every user's view."""
        store = ScenarioMemoryStore()
        await store.store_scenario(scenario_data={"t": "legacy"})  # owner_id=None
        await store.store_scenario(scenario_data={"t": "alice"}, owner_id="alice")

        alice_view = await store.recall_scenarios(owner_id="alice")
        assert {s.scenario_data["t"] for s in alice_view} == {"alice"}

    @pytest.mark.asyncio
    async def test_get_best_scenario_filters_by_owner(self):
        store = ScenarioMemoryStore()
        # Bob has a high score; Alice has a low score; isolation must hold.
        bob_sid = await store.store_scenario(scenario_data={"t": "bob"}, evaluation_score=99.0, owner_id="bob")
        alice_sid = await store.store_scenario(scenario_data={"t": "alice"}, evaluation_score=1.0, owner_id="alice")

        best_for_alice = await store.get_best_scenario(owner_id="alice")
        assert best_for_alice is not None
        assert best_for_alice.scenario_id == alice_sid

        best_for_bob = await store.get_best_scenario(owner_id="bob")
        assert best_for_bob is not None
        assert best_for_bob.scenario_id == bob_sid
