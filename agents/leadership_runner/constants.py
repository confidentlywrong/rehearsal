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

"""Constants and utilities for Leadership Stakeholder runner agents.

Single source of truth for leadership simulation parameters used by
the leadership_runner agent and process_leadership_tick tool.

Each constant mirrors its Marathon counterpart in agents/runner/constants.py
but is tuned for organizational dynamics: political capital depletes faster,
resistance cliffs are more common, and organizational entropy compounds
more aggressively than physical fatigue.
"""

import hashlib
import os

# --- Model Configuration ---
DEFAULT_MODEL = "gemini-3.5-flash-lite"
LEADERSHIP_RUNNER_MODEL = (
    (os.environ.get("LEADERSHIP_RUNNER_MODEL") or "").strip()
    or (os.environ.get("RUNNER_MODEL") or "").strip()
    or DEFAULT_MODEL
)
RUNNER_MODEL = LEADERSHIP_RUNNER_MODEL

# --- Momentum / Distance ---
MOMENTUM_SCALE = 4.0  # momentum=1.0 maps to 4.0 "alignment units"/hour
CONSENSUS_DISTANCE = 26.2188  # Same as MARATHON_MI — route length compatibility

# --- Stakeholder distribution ---
# Wider spread than marathon: champions align fast, resistors stall.
LOGNORMAL_MU = 5.8  # ln(minutes), median target ~330 ticks
LOGNORMAL_SIGMA = 0.45  # Wider spread: champions (~90) to resistors (~450)
MIN_FINISH_MIN = 90.0  # Champions align quickly
MAX_FINISH_MIN = 450.0  # Resistors can stall indefinitely

# --- Resistance cliff (replaces marathon "wall") ---
RESISTANCE_CLIFF_PROB = 0.55  # 55% of stakeholders hit a hidden resistance pocket
# Default cliff point is ~60% through the alignment journey (mile ~16)

# --- Political capital (replaces "water"/hydration) ---
CHECKPOINT_INTERVAL = 2.5  # Alignment checkpoints less frequent than water stops
CHECKPOINT_REFILL = 20.0  # Checkpoints restore less capital (political capital is scarce)
CAPITAL_DEPLETION_RATE = 4.5  # Capital% per "mile" at baseline (faster than water)
FRICTION_GROWTH = 0.05  # Additional depletion per unit of progress (compounds faster)
STALL_THRESHOLD = 35.0  # Political capital below this → stalled
DEADLOCK_THRESHOLD = 15.0  # Political capital below this while stalled → deadlocked

# --- Tool-based capital management limits ---
MAX_TOOL_CAPITAL_CHANGE = 15.0  # Max capital% the LLM can change per tool call
TOOL_RESTORE_WINDOW_TICKS = 5  # Sliding window size for restore cooldown
TOOL_RESTORE_WINDOW_MAX = 30.0  # Max total capital restored within the window

# --- Organizational entropy (replaces "fatigue") ---
ENTROPY_RATE = 0.003  # Momentum loss per tick (0.3% — faster than marathon's 0.2%)
MIN_ENTROPY_FACTOR = 0.65  # Floor for entropy degradation (deeper than marathon's 0.75)


def stakeholder_seed(session_id: str, salt: int = 0) -> int:
    """Deterministic seed from session_id for reproducible per-stakeholder RNG.

    Mirrors runner_seed() from the Marathon runner.

    Args:
        session_id: The stakeholder's ADK session ID.
        salt: Additional salt for per-event RNG.

    Returns:
        Integer seed for random.Random().
    """
    return int(hashlib.sha256(f"{session_id}:{salt}".encode()).hexdigest()[:8], 16)


# ---------------------------------------------------------------------------
# Persona profiles — each defines a distinct leadership simulation style.
# The module-level constants above remain the "pragmatist" defaults for
# backward compatibility.  At runtime, process_leadership_tick reads the
# active persona from session state and calls get_persona_profile().
# ---------------------------------------------------------------------------

PERSONA_PROFILES: dict[str, dict[str, float]] = {
    "pragmatist": {
        "momentum_scale": MOMENTUM_SCALE,
        "capital_depletion_rate": CAPITAL_DEPLETION_RATE,
        "resistance_cliff_prob": RESISTANCE_CLIFF_PROB,
        "entropy_rate": ENTROPY_RATE,
        "min_entropy_factor": MIN_ENTROPY_FACTOR,
        "checkpoint_refill": CHECKPOINT_REFILL,
        "friction_growth": FRICTION_GROWTH,
        "stall_threshold": STALL_THRESHOLD,
        "deadlock_threshold": DEADLOCK_THRESHOLD,
    },
    # Visionary: fast momentum, burns capital aggressively, lower resistance
    # but deep entropy decay. Moves quickly but risks burnout.
    "visionary": {
        "momentum_scale": 5.2,
        "capital_depletion_rate": 6.0,
        "resistance_cliff_prob": 0.35,
        "entropy_rate": 0.004,
        "min_entropy_factor": 0.55,
        "checkpoint_refill": 15.0,
        "friction_growth": 0.07,
        "stall_threshold": 30.0,
        "deadlock_threshold": 12.0,
    },
    # Diplomat: slow and steady, excellent capital efficiency, high
    # resistance tolerance. Rarely stalls but takes longer to align.
    "diplomat": {
        "momentum_scale": 3.2,
        "capital_depletion_rate": 3.0,
        "resistance_cliff_prob": 0.70,
        "entropy_rate": 0.002,
        "min_entropy_factor": 0.75,
        "checkpoint_refill": 28.0,
        "friction_growth": 0.03,
        "stall_threshold": 25.0,
        "deadlock_threshold": 10.0,
    },
    # Disruptor: chaotic profile with extreme variance — high momentum
    # but fragile capital, very likely to hit resistance cliffs.
    "disruptor": {
        "momentum_scale": 5.8,
        "capital_depletion_rate": 7.5,
        "resistance_cliff_prob": 0.80,
        "entropy_rate": 0.005,
        "min_entropy_factor": 0.50,
        "checkpoint_refill": 12.0,
        "friction_growth": 0.08,
        "stall_threshold": 40.0,
        "deadlock_threshold": 18.0,
    },
}

# Ordered list of all persona keys for deterministic random assignment.
ALL_PERSONAS: list[str] = sorted(PERSONA_PROFILES.keys())


def get_persona_profile(name: str) -> dict[str, float]:
    """Return the parameter profile for a leadership persona.

    Falls back to ``"pragmatist"`` if the name is unknown.

    Args:
        name: Persona key (one of: pragmatist, visionary, diplomat, disruptor).

    Returns:
        Dict of simulation parameters for the given persona.
    """
    return PERSONA_PROFILES.get(name.lower(), PERSONA_PROFILES["pragmatist"])
