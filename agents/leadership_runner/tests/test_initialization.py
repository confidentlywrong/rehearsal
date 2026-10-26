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

"""Tests for agents.leadership_runner.initialization."""

from agents.leadership_runner.constants import (
    CONSENSUS_DISTANCE,
    MAX_FINISH_MIN,
    MIN_FINISH_MIN,
    MOMENTUM_SCALE,
)
from agents.leadership_runner.initialization import initialize_leadership_stakeholder


class TestInitializeLeadershipStakeholder:
    """Verify that initialization produces valid stakeholder profiles."""

    def _init(self, session_id: str = "test-session", runner_count: int = 100) -> dict:
        state: dict = {}
        initialize_leadership_stakeholder(state, session_id, runner_count)
        return state

    def test_all_required_keys_present(self):
        state = self._init()
        required = [
            "velocity",
            "distance",
            "water",
            "exhausted",
            "collapsed",
            "finished",
            "runner_status",
            "will_hit_wall",
            "wall_mi",
            "wall_severity",
            "hydration_efficiency",
            "target_finish_minutes",
            "crowd_responsiveness",
            "wave_number",
            "start_delay_minutes",
        ]
        for key in required:
            assert key in state, f"Missing key: {key}"

    def test_initial_state_values(self):
        state = self._init()
        assert state["distance"] == 0.0
        assert state["exhausted"] is False
        assert state["collapsed"] is False
        assert state["finished"] is False
        assert state["runner_status"] == "running"

    def test_velocity_positive(self):
        state = self._init()
        assert state["velocity"] > 0

    def test_political_capital_in_range(self):
        """Starting political capital should be 85–100%."""
        state = self._init()
        assert 85.0 <= state["water"] <= 100.0

    def test_target_finish_in_range(self):
        state = self._init()
        assert MIN_FINISH_MIN <= state["target_finish_minutes"] <= MAX_FINISH_MIN

    def test_wall_severity_in_range(self):
        state = self._init()
        assert 0.0 <= state["wall_severity"] <= 1.0

    def test_hydration_efficiency_in_range(self):
        state = self._init()
        assert 0.3 <= state["hydration_efficiency"] <= 1.5

    def test_crowd_responsiveness_in_range(self):
        state = self._init()
        assert 0.0 <= state["crowd_responsiveness"] <= 1.0

    def test_deterministic(self):
        """Same session_id → identical profile."""
        s1 = self._init("session-42")
        s2 = self._init("session-42")
        for key in s1:
            assert s1[key] == s2[key], f"Key {key} differs: {s1[key]} != {s2[key]}"

    def test_different_sessions_differ(self):
        """Different session_ids → different profiles."""
        s1 = self._init("session-1")
        s2 = self._init("session-2")
        assert s1["velocity"] != s2["velocity"]

    def test_velocity_scale(self):
        """Verify velocity is in a reasonable range for MOMENTUM_SCALE."""
        state = self._init()
        effective_rate = state["velocity"] * MOMENTUM_SCALE
        # Should produce a finish between MIN and MAX finish minutes
        estimated_minutes = (CONSENSUS_DISTANCE / effective_rate) * 60.0
        assert MIN_FINISH_MIN * 0.8 <= estimated_minutes <= MAX_FINISH_MIN * 1.2

    def test_wave_assignment_small_count(self):
        """With <=50 runners, everyone gets wave 0."""
        state = self._init(runner_count=10)
        assert state["wave_number"] == 0
        assert state["start_delay_minutes"] == 0.0
