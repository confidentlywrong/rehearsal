# Copyright 2026 Christian Haeussinger
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

"""Tenant-routing tests for :class:`SimulationRunStore` (Phase 3).

These tests complement ``test_phase5_longitudinal.py`` by proving that
tenant isolation correctly buckets files by participant_id in a user-id
multi-tenant system.
"""

from __future__ import annotations

import json

import pytest

from agents.simulator.longitudinal import (
    SimulationRunRecord,
    SimulationRunStore,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with the Phase-3 env vars cleared."""
    for var in ("SAAS_MODE", "RACE_RUNS_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield


def _record(run_id: str, participant_id: str) -> SimulationRunRecord:
    return SimulationRunRecord(
        run_id=run_id,
        participant_id=participant_id,
        quest_id="ai_powered_disruptor",
        quest_name="AI Transformation",
        principle_scores=[0.5] * 7,
        overall_score=0.5,
        total_ticks=10,
        tick_snapshots=[{"tick": i, "organizational_state": {"resources": {}}} for i in range(2)],
    )


class TestSaaSRoutesByParticipant:
    """Storage logic strictly writes to the participant's tenant dir."""

    def test_save_uses_participant_subdir(self, tmp_path):
        store = SimulationRunStore(base_dir=tmp_path)
        store.save_run(_record("run-1", "user-alice"))

        assert (tmp_path / "user-alice" / "runs.ndjson").exists()
        assert (tmp_path / "user-alice" / "run-1.json").exists()

    def test_read_uses_participant_subdir(self, tmp_path):
        store = SimulationRunStore(base_dir=tmp_path)
        store.save_run(_record("run-1", "user-alice"))

        runs = store.get_participant_runs("user-alice")
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-1"


class TestCrossTenantIsolation:
    """Two participants writing under the same base dir must not see each other."""

    def test_disjoint_directories(self, tmp_path):
        base = tmp_path

        # Alice writes one record.
        SimulationRunStore(base_dir=base).save_run(_record("run-1", "user-alice"))

        # Bob writes a different record.
        SimulationRunStore(base_dir=base).save_run(_record("run-2", "user-bob"))

        alice_runs = json.loads((base / "user-alice" / "runs.ndjson").read_text().splitlines()[0])
        bob_runs = json.loads((base / "user-bob" / "runs.ndjson").read_text().splitlines()[0])
        assert alice_runs["run_id"] == "run-1"
        assert bob_runs["run_id"] == "run-2"

        # Snapshot files also disjoint.
        assert (base / "user-alice" / "run-1.json").exists()
        assert not (base / "user-bob" / "run-1.json").exists()
        assert (base / "user-bob" / "run-2.json").exists()
        assert not (base / "user-alice" / "run-2.json").exists()

    def test_get_all_runs_for_quest_is_user_scoped(self, tmp_path):
        """Proves get_all_runs_for_quest is scoped to the participant to prevent data leakage."""
        base = tmp_path

        SimulationRunStore(base_dir=base).save_run(_record("run-1", "user-alice"))
        SimulationRunStore(base_dir=base).save_run(_record("run-2", "user-bob"))

        store = SimulationRunStore(base_dir=base)
        all_runs = store.get_all_runs_for_quest("ai_powered_disruptor", "user-alice")

        # Only Alice's runs should be loaded, Bob's runs are isolated
        run_ids = sorted(r["run_id"] for r in all_runs)
        assert run_ids == ["run-1"]
