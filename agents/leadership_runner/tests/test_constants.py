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

"""Tests for agents.leadership_runner.constants."""

from agents.leadership_runner.constants import (
    CAPITAL_DEPLETION_RATE,
    CHECKPOINT_INTERVAL,
    CHECKPOINT_REFILL,
    CONSENSUS_DISTANCE,
    DEADLOCK_THRESHOLD,
    ENTROPY_RATE,
    FRICTION_GROWTH,
    LOGNORMAL_MU,
    LOGNORMAL_SIGMA,
    MAX_FINISH_MIN,
    MIN_ENTROPY_FACTOR,
    MIN_FINISH_MIN,
    MOMENTUM_SCALE,
    RESISTANCE_CLIFF_PROB,
    STALL_THRESHOLD,
    stakeholder_seed,
)


class TestConstantsValues:
    """Verify that leadership constants are within sensible ranges."""

    def test_momentum_scale_positive(self):
        assert MOMENTUM_SCALE > 0

    def test_consensus_distance_matches_marathon(self):
        """Must equal MARATHON_MI (26.2188) for route compatibility."""
        assert CONSENSUS_DISTANCE == 26.2188

    def test_distribution_bounds_valid(self):
        assert MIN_FINISH_MIN < MAX_FINISH_MIN
        assert LOGNORMAL_MU > 0
        assert LOGNORMAL_SIGMA > 0

    def test_resistance_cliff_probability_in_range(self):
        assert 0.0 < RESISTANCE_CLIFF_PROB <= 1.0

    def test_capital_depletion_faster_than_marathon(self):
        """Leadership capital depletes faster than marathon hydration (3.2187)."""
        assert CAPITAL_DEPLETION_RATE > 3.2187

    def test_friction_growth_positive(self):
        assert FRICTION_GROWTH > 0

    def test_checkpoint_interval_positive(self):
        assert CHECKPOINT_INTERVAL > 0
        assert CHECKPOINT_REFILL > 0

    def test_thresholds_ordered(self):
        """Stall triggers before deadlock."""
        assert STALL_THRESHOLD > DEADLOCK_THRESHOLD

    def test_entropy_positive(self):
        assert ENTROPY_RATE > 0
        assert 0 < MIN_ENTROPY_FACTOR < 1.0


class TestStakeholderSeed:
    """Verify deterministic seeding."""

    def test_deterministic(self):
        s1 = stakeholder_seed("session-abc", 0)
        s2 = stakeholder_seed("session-abc", 0)
        assert s1 == s2

    def test_different_sessions_different_seeds(self):
        s1 = stakeholder_seed("session-1", 0)
        s2 = stakeholder_seed("session-2", 0)
        assert s1 != s2

    def test_salt_varies_seed(self):
        s1 = stakeholder_seed("session-abc", 0)
        s2 = stakeholder_seed("session-abc", 1)
        assert s1 != s2
