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

"""AI Echo initialization — generates deterministic stakeholder profiles.

Direct extension of ``agents/runner/initialization.py``.  Uses the same
seeded-RNG pattern to produce per-Echo profiles that are deterministic
across identical sessions but varied across different Echoes.

State mapping from marathon → leadership:
    velocity         → sentiment (float, -1 to 1)
    water            → resistance (float, 0 to 1)
    distance         → influence (float, 0 to 1)
    exhausted        → urgency (float, 0 to 1)
    collapsed        → defected (bool)
    finished         → aligned (bool)
    runner_status    → stance (enum: supportive/neutral/resistant/hostile)
    will_hit_wall    → has_hidden_agenda (bool)
    wall_mi          → agenda_trigger_tick (int)
    hydration_eff    → communication_receptivity (float)
    target_finish    → principle_affinity[7] (float array)
    crowd_resp       → influence_susceptibility (float)
"""

import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

# State can be a plain dict (unit tests) or ADK State object (runtime).
StateLike = Any


def _echo_seed(session_id: str, role: str) -> int:
    """Generate a deterministic seed from session_id and role.

    Uses the same approach as ``runner_seed`` but adds role to ensure
    each Echo within the same simulation gets a unique but reproducible profile.
    """
    combined = f"{session_id}:{role}"
    return hash(combined) & 0xFFFFFFFF


def sentiment_to_stance(sentiment: float) -> str:
    """Map a sentiment value to a stance label."""
    if sentiment < -0.4:
        return "hostile"
    elif sentiment < 0.0:
        return "resistant"
    elif sentiment < 0.3:
        return "neutral"
    else:
        return "supportive"


def initialize_echo(
    state: StateLike,
    session_id: str,
    echo_config: dict = None,
) -> None:
    """Initialize an AI Echo's characteristics from quest config + seeded RNG.

    Called once during the first decision round (tick 0) via the Echo's
    ``before_agent_callback``.  Uses a deterministic RNG seeded from
    ``session_id + role`` so the same session always produces the same
    Echo profile, with controlled jitter for natural variation.

    Args:
        state: ADK session state (dict-like) to populate.
        session_id: The Echo's ADK session ID.
        echo_config: Echo configuration dict from the quest YAML.
    """
    if not echo_config:
        echo_config = {}

    role = echo_config.get("role", "unknown")
    rng = random.Random(_echo_seed(session_id, role))

    # --- Core attributes from quest config (with seeded jitter) ---
    base_sentiment = echo_config.get("initial_sentiment", 0.5)
    sentiment = max(-1.0, min(1.0, base_sentiment + rng.gauss(0.0, 0.05)))

    base_resistance = echo_config.get("initial_resistance", 0.3)
    resistance = max(0.0, min(1.0, base_resistance + rng.gauss(0.0, 0.03)))

    base_influence = echo_config.get("initial_influence", 0.5)
    influence = max(0.0, min(1.0, base_influence + rng.gauss(0.0, 0.03)))

    # --- Derived attributes ---
    # Urgency starts low, increases as OWB drops or deadlines approach
    urgency = max(0.0, min(1.0, 0.2 + rng.gauss(0.0, 0.05)))

    # Communication receptivity: how well this Echo responds to participant outreach
    communication_receptivity = max(0.3, min(1.0, 0.7 + rng.gauss(0.0, 0.1)))

    # Influence susceptibility: how much other Echoes affect this one
    influence_susceptibility = max(0.0, min(1.0, rng.betavariate(2, 5)))

    # --- Principle affinity vector (7 elements, with jitter) ---
    base_affinity = echo_config.get("affinity", [0.5] * 7)
    affinity = []
    for val in base_affinity:
        jittered = max(0.0, min(1.0, val + rng.gauss(0.0, 0.05)))
        affinity.append(round(jittered, 4))

    # --- Hidden agenda ---
    has_hidden_agenda = echo_config.get("has_hidden_agenda", False)
    agenda_trigger_tick = echo_config.get("agenda_trigger_tick")

    # --- Set state ---
    state["role"] = role
    state["tier"] = echo_config.get("tier", 2)
    state["persona"] = echo_config.get("persona", "")
    state["domain_principles"] = echo_config.get("domain_principles", [])

    state["sentiment"] = round(sentiment, 4)
    state["resistance"] = round(resistance, 4)
    state["influence"] = round(influence, 4)
    state["urgency"] = round(urgency, 4)
    state["defected"] = False
    state["aligned"] = False
    state["stance"] = sentiment_to_stance(sentiment)

    state["has_hidden_agenda"] = has_hidden_agenda
    state["agenda_trigger_tick"] = agenda_trigger_tick
    state["agenda_revealed"] = False

    state["communication_receptivity"] = round(communication_receptivity, 4)
    state["influence_susceptibility"] = round(influence_susceptibility, 4)
    state["principle_affinity"] = affinity

    # Echo status for the dynamic instruction template
    state["echo_status"] = "active"

    logger.info(
        "Echo initialized: role=%s, tier=%d, sentiment=%.2f, resistance=%.2f, influence=%.2f, stance=%s, affinity=%s",
        role,
        state["tier"],
        sentiment,
        resistance,
        influence,
        state["stance"],
        affinity,
    )
