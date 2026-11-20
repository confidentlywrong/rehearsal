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

import hashlib
import inspect
import json
import logging
import math
import random
from typing import Any

from agents.utils import redis_pool
from agents.utils import tenant_keys

logger = logging.getLogger(__name__)

# State can be a plain dict (unit tests) or ADK State object (runtime).
StateLike = Any

ACRONYMS: dict[str, str] = {
    "cfo": "CFO",
    "cto": "CTO",
    "ceo": "CEO",
    "coo": "COO",
    "cpo": "CPO",
    "cio": "CIO",
    "ciso": "CISO",
    "cmo": "CMO",
    "chro": "CHRO",
    "cro": "CRO",
    "cbo": "CBO",
    "vp": "VP",
    "svp": "SVP",
    "evp": "EVP",
    "hr": "HR",
    "ai": "AI",
    "it": "IT",
    "sla": "SLA",
    "roi": "ROI",
    "wtt": "WTT",
    "pr": "PR",
    "qa": "QA",
    "r&d": "R&D",
    "ops": "Ops",
    "eng": "Eng",
}

_WALL_BASE: dict[str, float] = {
    "hostile": 9.0,
    "resistant": 7.0,
    "neutral": 5.0,
    "supportive": 3.0,
}


def format_role_display(role: str) -> str:
    """Format a role or slug into a human-readable title preserving acronyms."""
    if not role or not isinstance(role, str):
        return ""
    clean = role.replace("-", " ").replace("_", " ").strip()
    if not clean:
        return ""
    words = clean.split()
    formatted = []
    lower_words = {"of", "and", "the", "for", "in", "on", "at", "to"}
    for idx, w in enumerate(words):
        low = w.lower()
        if low in ACRONYMS:
            formatted.append(ACRONYMS[low])
        elif idx > 0 and low in lower_words:
            formatted.append(low)
        else:
            formatted.append(w.capitalize())
    return " ".join(formatted)


def compute_wall_severity(stance: object, sentiment: object) -> float:
    """Compute deterministic route friction (0.0-10.0) from stance and sentiment."""
    stance_key = stance if isinstance(stance, str) else "neutral"
    clamped_sentiment = max(-1.0, min(1.0, _finite_float(sentiment, 0.0)))
    base = _WALL_BASE.get(stance_key, 5.0)
    return max(0.0, min(10.0, base - clamped_sentiment * 1.5))


async def fetch_echo_config(session_id: str, user_id: str | None = None) -> dict[str, Any]:
    """Fetch ``echo_config:{session_id}`` (tenant-scoped under ``SAAS_MODE=on``) from Redis.

    Returns ``{}`` when Redis is unconfigured (``r is None``) or the key is
    absent (``val is None``).  Raises ``ValueError`` when ``SAAS_MODE=on``
    and tenant identity is missing/anonymous or when the stored payload is
    corrupt/non-dict JSON.  Logs ``ERROR`` and raises ``RuntimeError`` when
    Redis fails.
    """
    key = tenant_keys.echo_config_key(session_id, user_id=user_id)
    try:
        r = redis_pool.get_shared_redis_client()
        if r is None:
            return {}
        maybe_val = r.get(key)
        val = await maybe_val if inspect.isawaitable(maybe_val) else maybe_val
    except Exception as exc:
        logger.error(
            "fetch_echo_config: Redis lookup failed for %s: %s",
            session_id,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"Failed to fetch echo config from Redis for {session_id}: {exc}"
        ) from exc

    if val is None:
        return {}

    try:
        loaded_cfg = json.loads(val)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        logger.error(
            "fetch_echo_config: corrupt JSON in echo_config:%s: %s",
            session_id,
            exc,
            exc_info=True,
        )
        raise ValueError(f"Invalid JSON in echo_config:{session_id}: {exc}") from exc

    if not isinstance(loaded_cfg, dict):
        logger.error(
            "fetch_echo_config: expected dict in echo_config:%s, got %s",
            session_id,
            type(loaded_cfg).__name__,
        )
        raise ValueError(
            f"Expected dict in echo_config:{session_id}, got {type(loaded_cfg).__name__}"
        )

    return loaded_cfg


def _finite_float(value: object, default: float = 0.0) -> float:
    """Coerce numeric value to finite float, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _finite_int(value: object, default: int = 0) -> int:
    """Coerce int/float value to int, rejecting bool/None/NaN/Inf."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        try:
            num = float(value)
        except OverflowError:
            return default
        return value if math.isfinite(num) else default
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(num):
        return default
    return int(num)


def _echo_seed(session_id: str, role: str) -> int:
    """Generate a deterministic seed from session_id and role.

    Uses SHA-256 (mirroring ``runner_seed``) rather than Python's salted
    ``hash()`` so profiles remain reproducible across Python processes.
    """
    combined = f"{session_id}:{role}"
    return int(hashlib.sha256(combined.encode("utf-8")).hexdigest()[:8], 16)


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
    echo_config: dict[str, Any] | None = None,
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
    if not isinstance(echo_config, dict):
        echo_config = {}

    raw_role = echo_config.get("role")
    role = raw_role.strip() if isinstance(raw_role, str) and raw_role.strip() else "unknown"
    rng = random.Random(_echo_seed(session_id, role))

    # --- Core attributes from quest config (with seeded jitter) ---
    base_sentiment = max(-1.0, min(1.0, _finite_float(echo_config.get("initial_sentiment"), 0.5)))
    sentiment = max(-1.0, min(1.0, base_sentiment + rng.gauss(0.0, 0.05)))

    base_resistance = max(0.0, min(1.0, _finite_float(echo_config.get("initial_resistance"), 0.3)))
    resistance = max(0.0, min(1.0, base_resistance + rng.gauss(0.0, 0.03)))

    base_influence = max(0.0, min(1.0, _finite_float(echo_config.get("initial_influence"), 0.5)))
    influence = max(0.0, min(1.0, base_influence + rng.gauss(0.0, 0.03)))

    # --- Derived attributes ---
    # Urgency starts low, increases as OWB drops or deadlines approach
    urgency = max(0.0, min(1.0, 0.2 + rng.gauss(0.0, 0.05)))

    # Communication receptivity: how well this Echo responds to participant outreach
    communication_receptivity = max(0.3, min(1.0, 0.7 + rng.gauss(0.0, 0.1)))

    # Influence susceptibility: how much other Echoes affect this one
    influence_susceptibility = max(0.0, min(1.0, rng.betavariate(2, 5)))

    # --- Principle affinity vector (7 elements, with jitter) ---
    raw_affinity = echo_config.get("affinity")
    if isinstance(raw_affinity, (list, tuple)) and len(raw_affinity) > 0:
        base_affinity = [max(0.0, min(1.0, _finite_float(v, 0.5))) for v in list(raw_affinity)[:7]]
        while len(base_affinity) < 7:
            base_affinity.append(0.5)
    else:
        base_affinity = [0.5] * 7
    affinity = []
    for val in base_affinity:
        jittered = max(0.0, min(1.0, val + rng.gauss(0.0, 0.05)))
        affinity.append(round(jittered, 4))

    # --- Hidden agenda ---
    has_hidden_agenda = bool(echo_config.get("has_hidden_agenda", False))
    raw_trigger = echo_config.get("agenda_trigger_tick")
    agenda_trigger_tick = (
        _finite_int(raw_trigger)
        if raw_trigger is not None and not isinstance(raw_trigger, bool)
        else None
    )

    # --- Set state ---
    state["role"] = role
    state["tier"] = _finite_int(echo_config.get("tier"), 2)
    raw_persona = echo_config.get("persona")
    state["persona"] = raw_persona if isinstance(raw_persona, str) else ""
    raw_dps = echo_config.get("domain_principles")
    state["domain_principles"] = (
        [dp for dp in raw_dps if isinstance(dp, str)]
        if isinstance(raw_dps, (list, tuple))
        else []
    )
    if echo_config.get("label"):
        state["label"] = echo_config["label"]
    if echo_config.get("name"):
        state["name"] = echo_config["name"]
    state["role_display"] = (
        echo_config.get("label") or echo_config.get("name") or format_role_display(role)
    )
    if echo_config.get("_principles"):
        state["_principles"] = list(echo_config["_principles"])

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
