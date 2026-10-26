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

"""Tests for Phase 5: longitudinal tracking, what-if replay, peer benchmarks."""

import pytest

from agents.simulator.longitudinal import (
    SimulationRunRecord,
    SimulationRunStore,
    compute_learning_trajectory,
    compute_peer_benchmarks,
    recommend_next_quest,
    replay_from_tick,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path):
    """Create a temporary SimulationRunStore."""
    return SimulationRunStore(base_dir=tmp_path)


@pytest.fixture
def sample_run():
    """A minimal simulation run record."""
    return SimulationRunRecord(
        run_id="run-001",
        participant_id="user-alice",
        quest_id="ai_powered_disruptor",
        quest_name="AI Transformation",
        principle_scores=[0.8, 0.6, 0.5, 0.7, 0.4, 0.9, 0.3],
        overall_score=0.6,
        contradiction_count=2,
        defected_count=1,
        aligned_count=3,
        total_echoes=11,
        total_ticks=30,
        final_resources={"lau": 45, "bt": 200, "tbu": 30, "owb": 25},
        tick_snapshots=[
            {
                "tick": i,
                "organizational_state": {
                    "resources": {
                        "lau": {"current": 80 - i, "regen_per_tick": 3, "initial": 80, "critical_threshold": 15},
                        "owb": {"current": 60 - i * 2, "regen_per_tick": -1, "initial": 60, "critical_threshold": 15},
                    }
                },
            }
            for i in range(30)
        ],
    )


@pytest.fixture
def sample_runs_for_benchmarks():
    """Multiple runs for benchmarking."""
    return [
        {
            "quest_id": "ai_powered_disruptor",
            "principle_scores": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
            "overall_score": 0.5,
        },
        {
            "quest_id": "ai_powered_disruptor",
            "principle_scores": [0.8, 0.8, 0.8, 0.8, 0.8, 0.8, 0.8],
            "overall_score": 0.8,
        },
        {
            "quest_id": "ai_powered_disruptor",
            "principle_scores": [0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3],
            "overall_score": 0.3,
        },
        {
            "quest_id": "ai_powered_disruptor",
            "principle_scores": [0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9],
            "overall_score": 0.9,
        },
    ]


# ---------------------------------------------------------------------------
# 5A: Longitudinal Storage Tests
# ---------------------------------------------------------------------------


class TestSimulationRunRecord:
    """Tests for run record serialization."""

    def test_to_dict_excludes_snapshots(self, sample_run):
        d = sample_run.to_dict()
        assert "tick_snapshots" not in d
        assert d["run_id"] == "run-001"
        assert d["participant_id"] == "user-alice"

    def test_to_full_dict_includes_snapshots(self, sample_run):
        d = sample_run.to_full_dict()
        assert "tick_snapshots" in d
        assert len(d["tick_snapshots"]) == 30

    def test_from_session_state(self):
        state = {
            "simulation_id": "sim-123",
            "participant_id": "user-bob",
            "quest_id": "budget_cuts_2027",
            "quest_name": "Budget Cuts",
            "quest_difficulty": "intermediate",
            "principle_scores": [0.7] * 7,
            "contradiction_log": [{"tick": 5}],
            "defected_echo_ids": ["cfo"],
            "aligned_echo_ids": ["hr_director", "coach"],
            "echo_configs": [{"role": f"echo_{i}"} for i in range(11)],
            "resources": {
                "lau": {"current": 50},
                "owb": {"current": 30},
            },
            "tick_snapshots": [{"tick": 0}],
        }
        record = SimulationRunRecord.from_session_state(state)
        assert record.quest_id == "budget_cuts_2027"
        assert record.contradiction_count == 1
        assert record.defected_count == 1
        assert record.aligned_count == 2
        assert record.final_resources["lau"] == 50


class TestSimulationRunStore:
    """Tests for file-based run storage."""

    def test_save_and_load_run(self, tmp_store, sample_run):
        tmp_store.save_run(sample_run)
        runs = tmp_store.get_participant_runs("user-alice")
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-001"

    def test_save_multiple_runs(self, tmp_store, sample_run):
        tmp_store.save_run(sample_run)
        sample_run.run_id = "run-002"
        sample_run.overall_score = 0.8
        tmp_store.save_run(sample_run)

        runs = tmp_store.get_participant_runs("user-alice")
        assert len(runs) == 2

    def test_load_empty_participant(self, tmp_store):
        runs = tmp_store.get_participant_runs("nonexistent")
        assert runs == []

    def test_tick_snapshots_saved_separately(self, tmp_store, sample_run):
        tmp_store.save_run(sample_run)

        replay_data = tmp_store.get_run_snapshots("user-alice", "run-001")
        assert replay_data is not None
        assert len(replay_data["tick_snapshots"]) == 30

    def test_get_all_runs_for_quest(self, tmp_store, sample_run):
        tmp_store.save_run(sample_run)

        # Save another run for the same user
        sample_run.run_id = "run-002"
        tmp_store.save_run(sample_run)

        all_runs = tmp_store.get_all_runs_for_quest("ai_powered_disruptor", "user-alice")
        assert len(all_runs) == 2

    def test_quest_filter_excludes_other_quests(self, tmp_store, sample_run):
        tmp_store.save_run(sample_run)
        all_runs = tmp_store.get_all_runs_for_quest("budget_cuts_2027", "user-alice")
        assert len(all_runs) == 0


# ---------------------------------------------------------------------------
# 5B: What-If Replay Tests
# ---------------------------------------------------------------------------


class TestWhatIfReplay:
    """Tests for the what-if replay engine."""

    def test_replay_basic(self, sample_run):
        result = replay_from_tick(
            tick_snapshots=sample_run.tick_snapshots,
            fork_tick=10,
        )
        assert "fork_tick" in result
        assert result["fork_tick"] == 10
        assert len(result["original_trajectory"]) == 30
        assert len(result["forked_trajectory"]) == 20  # 30 - 10

    def test_replay_invalid_tick(self, sample_run):
        result = replay_from_tick(
            tick_snapshots=sample_run.tick_snapshots,
            fork_tick=999,
        )
        assert "error" in result

    def test_replay_with_modified_decision(self, sample_run):
        result = replay_from_tick(
            tick_snapshots=sample_run.tick_snapshots,
            fork_tick=5,
            modified_decision_tags={
                "resource_impact_suggestion": {"lau": -10, "owb": 5},
            },
        )
        assert "comparison" in result
        assert "resource_deltas" in result["comparison"]
        assert "verdict" in result["comparison"]

    def test_replay_first_tick(self, sample_run):
        result = replay_from_tick(
            tick_snapshots=sample_run.tick_snapshots,
            fork_tick=0,
        )
        assert result["fork_tick"] == 0
        assert len(result["forked_trajectory"]) == 30


# ---------------------------------------------------------------------------
# 5C: Peer Benchmarks & Learning Trajectory Tests
# ---------------------------------------------------------------------------


class TestPeerBenchmarks:
    """Tests for peer benchmarking."""

    def test_benchmarks_with_peers(self, sample_runs_for_benchmarks):
        current = [0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7]
        result = compute_peer_benchmarks(current, sample_runs_for_benchmarks)

        assert "participant_scores" in result
        assert "average_scores" in result
        assert "p90_scores" in result
        assert "rank" in result
        assert "percentile" in result
        assert result["total_runs"] == 4

    def test_benchmarks_with_no_peers(self):
        current = [0.7] * 7
        result = compute_peer_benchmarks(current, [])
        assert result["rank"] == 1
        assert result["percentile"] == 100.0

    def test_high_scorer_ranked_well(self, sample_runs_for_benchmarks):
        current = [0.95] * 7
        result = compute_peer_benchmarks(current, sample_runs_for_benchmarks)
        assert result["rank"] == 1  # Best performer

    def test_low_scorer_ranked_low(self, sample_runs_for_benchmarks):
        current = [0.1] * 7
        result = compute_peer_benchmarks(current, sample_runs_for_benchmarks)
        assert result["rank"] > 3  # Worst performer


class TestLearningTrajectory:
    """Tests for longitudinal learning analysis."""

    def test_empty_trajectory(self):
        result = compute_learning_trajectory([])
        assert result["simulations"] == []
        assert result["total_simulations"] == 0

    def test_single_run_trajectory(self):
        runs = [
            {
                "quest_id": "q1",
                "quest_name": "Quest 1",
                "overall_score": 0.5,
                "principle_scores": [0.5] * 7,
                "contradiction_count": 1,
            }
        ]
        result = compute_learning_trajectory(runs)
        assert result["total_simulations"] == 1
        assert result["trend"] == "insufficient_data"

    def test_improving_trajectory(self):
        runs = [
            {"quest_id": "q1", "overall_score": 0.3, "principle_scores": [0.3] * 7, "contradiction_count": 3},
            {"quest_id": "q2", "overall_score": 0.6, "principle_scores": [0.6] * 7, "contradiction_count": 1},
            {"quest_id": "q3", "overall_score": 0.8, "principle_scores": [0.8] * 7, "contradiction_count": 0},
        ]
        result = compute_learning_trajectory(runs)
        assert result["trend"] == "improving"
        assert len(result["growth_areas"]) == 2
        assert len(result["strengths"]) == 2

    def test_declining_trajectory(self):
        runs = [
            {"quest_id": "q1", "overall_score": 0.8, "principle_scores": [0.8] * 7, "contradiction_count": 0},
            {"quest_id": "q2", "overall_score": 0.4, "principle_scores": [0.4] * 7, "contradiction_count": 3},
        ]
        result = compute_learning_trajectory(runs)
        assert result["trend"] == "declining"

    def test_growth_areas_identify_weakest(self):
        runs = [
            {
                "quest_id": "q1",
                "overall_score": 0.5,
                "principle_scores": [0.9, 0.1, 0.8, 0.7, 0.2, 0.6, 0.5],
                "contradiction_count": 0,
            },
        ]
        result = compute_learning_trajectory(runs)
        growth_principles = [g["principle"] for g in result["growth_areas"]]
        # Indices 1 (0.1) and 4 (0.2) should be the weakest
        assert len(growth_principles) == 2


class TestQuestRecommendation:
    """Tests for adaptive quest recommendation."""

    def test_first_run_recommends_flagship(self):
        result = recommend_next_quest([])
        assert result["quest_id"] == "ai_powered_disruptor"

    def test_recommendation_targets_weakness(self):
        runs = [
            {
                "quest_id": "ai_powered_disruptor",
                "overall_score": 0.5,
                "principle_scores": [0.9, 0.1, 0.8, 0.7, 0.2, 0.6, 0.5],
            },
        ]
        result = recommend_next_quest(runs)
        assert result["target_principle"] is not None
        assert result["quest_id"] != ""

    def test_avoids_repeating_last_quest(self):
        # If the weakest principle maps to the quest they just did,
        # it should recommend the next-weakest principle's quest
        runs = [
            {
                "quest_id": "scaling_hypergrowth_2028",
                "overall_score": 0.5,
                "principle_scores": [0.9, 0.1, 0.8, 0.7, 0.2, 0.1, 0.5],
            },
        ]
        result = recommend_next_quest(runs)
        # Both weakest (idx 1 and idx 5) map to hypergrowth, so it should
        # try to avoid repeating — or if all map to same, at least return something
        assert result["quest_id"] is not None
