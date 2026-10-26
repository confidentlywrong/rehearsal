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

"""Leadership stakeholder initialization logic.

Mirrors agents/runner/initialization.py but generates organizational
stakeholder profiles instead of marathon runner profiles.  Uses the
same state keys so the frontend visualization pipeline works unchanged.
"""

import logging
import random
from typing import Any

from agents.leadership_runner.constants import (
    ALL_PERSONAS,
    CONSENSUS_DISTANCE,
    LOGNORMAL_MU,
    LOGNORMAL_SIGMA,
    MAX_FINISH_MIN,
    MIN_FINISH_MIN,
    get_persona_profile,
    stakeholder_seed,
)
from agents.runner.waves import compute_wave

logger = logging.getLogger(__name__)

# State can be a plain dict (unit tests) or ADK State object (runtime).
StateLike = Any


def initialize_leadership_stakeholder(
    state: StateLike,
    session_id: str,
    runner_count: int = 1,
) -> None:
    """Initialize leadership stakeholder characteristics from seeded distributions.

    Called once during the first tick (tick 0).  Uses a deterministic RNG
    seeded from the session_id so the same session always produces the
    same stakeholder profile.

    State keys are intentionally identical to the Marathon runner so the
    frontend 3D viewport, HUD, and Redis telemetry pipeline work unchanged.

    Args:
        state: Mutable state dict (ADK State or plain dict).
        session_id: The stakeholder's ADK session ID.
        runner_count: Total number of stakeholders in the simulation.
    """
    # Resolve persona: if not pre-set, assign randomly from the
    # available profiles using the session_id seed.  This mirrors
    # Marathon's approach of per-session statistical variance but
    # applies it through distinct physics profiles.
    persona = state.get("persona")
    if not persona:
        persona_rng = random.Random(stakeholder_seed(session_id, salt=999))
        persona = persona_rng.choice(ALL_PERSONAS)
    state["persona"] = persona
    profile = get_persona_profile(persona)

    rng = random.Random(stakeholder_seed(session_id))

    # Target alignment time from log-normal distribution (wider than marathon)
    target_finish = rng.lognormvariate(LOGNORMAL_MU, LOGNORMAL_SIGMA)
    target_finish = max(MIN_FINISH_MIN, min(MAX_FINISH_MIN, target_finish))

    # Derive initial momentum (= velocity state key)
    target_rate = CONSENSUS_DISTANCE / (target_finish / 60.0)
    initial_momentum = target_rate / profile["momentum_scale"]

    # Resistance cliff parameters (replaces marathon "wall")
    will_hit_cliff = rng.random() < profile["resistance_cliff_prob"]
    cliff_point = rng.gauss(16.0, 3.0)  # ~60% through alignment journey
    cliff_severity = rng.betavariate(3, 4)  # Heavier than marathon wall

    # --- Capital efficiency (mirrors hydration_efficiency) ---
    # Fast aligners: efficient capital use.  Slow aligners: wasteful but steady.
    ability_ratio = target_finish / 300.0  # 1.0 = median stakeholder
    if ability_ratio <= 1.0:
        base_efficiency = 0.5 + 0.5 * ability_ratio  # 0.5 → 1.0
    else:
        overshoot = ability_ratio - 1.0
        base_efficiency = max(0.5, 1.0 - 0.3 * overshoot)  # 1.0 → 0.5
    capital_efficiency = max(0.3, base_efficiency * max(0.6, rng.gauss(1.0, 0.12)))

    # Coalition responsiveness: 60% ignore coalition zones, 40% respond
    # (more responsive than marathon's 25% crowd effect)
    if rng.random() < 0.60:
        coalition_resp = 0.0
    else:
        coalition_resp = rng.betavariate(3, 4)

    # Wave start: assign corral by alignment speed (faster stakeholders start first)
    ability_pct = (target_finish - MIN_FINISH_MIN) / (MAX_FINISH_MIN - MIN_FINISH_MIN)
    runner_index = int(ability_pct * max(runner_count - 1, 0))
    wave_number, start_delay_minutes = compute_wave(runner_index, runner_count)

    # Starting political capital: 85–100% (leaders start with capital to spend)
    base_capital = 100.0 - 10.0 * max(0.0, ability_ratio - 0.5)
    noisy_capital = base_capital + rng.gauss(0.0, 2.0)
    initial_capital = max(85.0, min(100.0, noisy_capital))

    # ── Write state (SAME KEYS as marathon runner) ──────────────────────
    state["velocity"] = round(initial_momentum, 4)
    state["distance"] = 0.0
    state["water"] = round(initial_capital, 4)  # "water" = political capital
    state["exhausted"] = False  # = stalled
    state["collapsed"] = False  # = deadlocked
    state["finished"] = False  # = aligned
    state["runner_status"] = "running"  # = advancing
    state["will_hit_wall"] = will_hit_cliff
    state["wall_mi"] = round(cliff_point, 4)
    state["wall_severity"] = round(cliff_severity, 4)
    state["hydration_efficiency"] = round(capital_efficiency, 4)
    state["target_finish_minutes"] = round(target_finish, 1)
    state["crowd_responsiveness"] = round(coalition_resp, 4)
    state["wave_number"] = wave_number
    state["start_delay_minutes"] = start_delay_minutes
