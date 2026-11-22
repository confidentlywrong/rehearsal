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

"""Truth Engine — Contradiction Detector (Assumption 2).

Compares a participant's stated commitments against their enacted decisions
to detect integrity gaps.  Implemented as a stateful accumulator inside the
simulator's session state, evaluated per-tick.

Follows the ``finished_runner_ids`` / ``collapsed_runner_ids`` accumulator
pattern in ``agents/simulator/skills/advancing-race-ticks/tools.py:681-696``.

No additional LLM calls — reuses the Decision Classification Engine output.
"""

import logging
import math
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

StateLike = Any

# Commitment extraction patterns (keyword-matching heuristic)
_COMMITMENT_PATTERNS = [
    re.compile(r"(?:i|we)\s+(?:commit|pledge|promise)\s+(?:to\s+)?(.+)", re.IGNORECASE),
    re.compile(r"(?:my|our)\s+(?:priority|focus|commitment)\s+(?:is|will be)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:i|we)\s+(?:believe|value)\s+(?:in\s+)?(.+)", re.IGNORECASE),
    re.compile(r"(?:i|we)\s+(?:will|shall)\s+(?:always|ensure|make sure)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:i|we)\s+(?:stand for|champion|advocate for)\s+(.+)", re.IGNORECASE),
]

# Mapping from commitment keywords to principle indices
# Principle order:
#   0: Strategic Foresight & Vision Cascading
#   1: Adaptive Decision-Making & Agility
#   2: Stakeholder Engagement & Inclusive Communication
#   3: Ethical Leadership & Governance
#   4: Empowerment & Cultivating Distributed Leadership
#   5: Organizational Resilience & Change Agility
#   6: Resource Optimization & Strategic Allocation
_KEYWORD_TO_PRINCIPLE: dict[str, int] = {
    # Principle 0: Strategic Foresight
    "vision": 0,
    "strategy": 0,
    "foresight": 0,
    "long-term": 0,
    "roadmap": 0,
    "future": 0,
    # Principle 1: Adaptive Decision-Making
    "agility": 1,
    "adaptability": 1,
    "flexibility": 1,
    "pivot": 1,
    "iterate": 1,
    "experiment": 1,
    # Principle 2: Stakeholder Engagement
    "stakeholder": 2,
    "communication": 2,
    "transparency": 2,
    "inclusion": 2,
    "dialogue": 2,
    "listen": 2,
    "engage": 2,
    "inclusive": 2,
    # Principle 3: Ethical Leadership
    "ethics": 3,
    "ethical": 3,
    "responsible": 3,
    "governance": 3,
    "fairness": 3,
    "bias": 3,
    "trust": 3,
    "integrity": 3,
    "privacy": 3,
    "accountability": 3,
    # Principle 4: Empowerment
    "empowerment": 4,
    "empower": 4,
    "autonomy": 4,
    "delegation": 4,
    "distributed": 4,
    "team": 4,
    "talent": 4,
    "people": 4,
    "workforce": 4,
    "employee": 4,
    # Principle 5: Organizational Resilience
    "resilience": 5,
    "resilient": 5,
    "change": 5,
    "transformation": 5,
    "adapt": 5,
    "sustain": 5,
    "stability": 5,
    # Principle 6: Resource Optimization
    "resource": 6,
    "budget": 6,
    "cost": 6,
    "efficiency": 6,
    "roi": 6,
    "optimize": 6,
    "allocation": 6,
    "invest": 6,
}


@dataclass
class Commitment:
    """A detected commitment from participant's communication."""

    text: str
    principle_index: int
    principle_name: str
    tick_declared: int
    active: bool = True


@dataclass
class Contradiction:
    """A detected contradiction between stated and enacted principles."""

    commitment_text: str
    violated_principle: str
    violated_principle_index: int
    evidence: str  # The decision that contradicted the commitment
    tick_detected: int
    tick_declared: int
    severity: str = "warning"  # "warning" or "critical"

    def to_dict(self) -> dict:
        return {
            "commitment_text": self.commitment_text,
            "violated_principle": self.violated_principle,
            "violated_principle_index": self.violated_principle_index,
            "evidence": self.evidence,
            "tick_detected": self.tick_detected,
            "tick_declared": self.tick_declared,
            "severity": self.severity,
        }


def _safe_tick(value: Any, default: int = 0) -> int:
    """Coerce a tick value to a finite int, rejecting booleans and NaN/Inf."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        try:
            num = float(value)
        except OverflowError:
            return default
        return value if math.isfinite(num) else default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return int(num) if math.isfinite(num) else default


def extract_commitments(
    text: str,
    current_tick: int,
    principles: list[str],
) -> list[Commitment]:
    """Extract leadership commitments from participant text.

    Uses keyword-matching heuristics to identify when participants
    express commitment to specific principles.

    Args:
        text: Participant's text input.
        current_tick: Current simulation tick.
        principles: List of 7 principle names.

    Returns:
        List of detected Commitment instances.
    """
    if not text or not isinstance(text, str):
        return []
    safe_principles = principles if isinstance(principles, list) else []
    safe_tick = _safe_tick(current_tick, 0)
    commitments: list[Commitment] = []

    for pattern in _COMMITMENT_PATTERNS:
        match = pattern.search(text)
        if match:
            commitment_text = match.group(1).strip().rstrip(".")
            # Map to principle via keyword matching
            principle_idx = _map_text_to_principle(commitment_text)
            if principle_idx is not None:
                commitments.append(
                    Commitment(
                        text=commitment_text,
                        principle_index=principle_idx,
                        principle_name=(
                            safe_principles[principle_idx]
                            if 0 <= principle_idx < len(safe_principles)
                            else "Unknown"
                        ),
                        tick_declared=safe_tick,
                    )
                )

    if commitments:
        logger.info(
            "Truth Engine: Extracted %d commitments from tick %d: %s",
            len(commitments),
            safe_tick,
            ", ".join(f"'{c.text}' → {c.principle_name}" for c in commitments),
        )

    return commitments


def _map_text_to_principle(text: str) -> int | None:
    """Map text to a principle index using keyword frequency."""
    text_lower = text.lower()
    scores: dict[int, int] = {}

    for keyword, idx in _KEYWORD_TO_PRINCIPLE.items():
        if keyword in text_lower:
            scores[idx] = scores.get(idx, 0) + 1

    if not scores:
        return None

    return max(scores, key=lambda idx: scores[idx])


def detect_contradictions(
    commitments: list[dict],
    decision_tags: dict,
    current_tick: int,
    principles: list[str],
) -> list[Contradiction]:
    """Detect contradictions between stated commitments and enacted decisions.

    Called after the Decision Classification Engine has tagged the current
    decision, but BEFORE broadcasting to Echoes. This allows the contradiction
    to be included in the broadcast payload, triggering principle-based pushback.

    Args:
        commitments: List of commitment dicts from ``state["declared_commitments"]``.
        decision_tags: Output from the Decision Classification Engine:
            ``{ "principles_applied": [...], "principles_neglected": [...] }``
        current_tick: Current simulation tick.
        principles: List of 7 principle names.

    Returns:
        List of Contradiction instances (may be empty).
    """
    if not commitments or not isinstance(decision_tags, dict):
        return []

    raw_neglected = decision_tags.get("principles_neglected") or []
    neglected = {p for p in raw_neglected if isinstance(p, str)} if isinstance(raw_neglected, (list, tuple, set)) else set()
    if not neglected:
        return []

    safe_principles = principles if isinstance(principles, list) else []
    safe_tick = _safe_tick(current_tick, 0)
    contradictions: list[Contradiction] = []

    for commitment in commitments:
        if not isinstance(commitment, dict) or not commitment.get("active", True):
            continue

        principle_name = str(commitment.get("principle_name") or "")
        raw_idx = commitment.get("principle_index", -1)
        principle_idx = int(raw_idx) if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) else -1

        # Check if the committed principle is being neglected
        if principle_name in neglected or (
            0 <= principle_idx < len(safe_principles) and safe_principles[principle_idx] in neglected
        ):
            # Compute severity based on commitment age and repeat violations
            tick_declared = _safe_tick(commitment.get("tick_declared"), 0)
            commitment_age = max(0, safe_tick - tick_declared)
            severity = _compute_severity(commitment_age)

            contradictions.append(
                Contradiction(
                    commitment_text=str(commitment.get("text") or ""),
                    violated_principle=principle_name,
                    violated_principle_index=principle_idx,
                    evidence=str(decision_tags.get("raw_decision") or ""),
                    tick_detected=safe_tick,
                    tick_declared=tick_declared,
                    severity=severity,
                )
            )

    if contradictions:
        logger.info(
            "Truth Engine: %d contradictions detected at tick %d — %s",
            len(contradictions),
            safe_tick,
            ", ".join(f"'{c.violated_principle}' (severity={c.severity})" for c in contradictions),
        )

    return contradictions


def _compute_severity(commitment_age: int) -> str:
    """Compute contradiction severity based on how recently commitment was made.

    Recent commitments (within 5 ticks) are "critical" — the participant
    is actively contradicting something they just said.  Older commitments
    are "warning" — the participant may have legitimately shifted priorities.
    """
    if commitment_age <= 5:
        return "critical"
    return "warning"


def find_best_responder(
    contradictions: list[Contradiction],
    echo_configs: list[dict],
) -> str | None:
    """Find the Echo best suited to respond to the contradiction.

    Returns the role of the Echo with the highest affinity to the
    violated principle.

    Args:
        contradictions: List of detected contradictions.
        echo_configs: List of Echo configuration dicts.

    Returns:
        Role string of the best responder, or None if no match.
    """
    if not contradictions or not echo_configs:
        return None

    # Use the highest-severity contradiction (critical before warning)
    primary = min(contradictions, key=lambda c: 0 if c.severity == "critical" else 1)
    target_idx = primary.violated_principle_index
    if not isinstance(target_idx, int) or target_idx < 0:
        return None

    best_role = None
    best_affinity = -1.0

    for config in echo_configs:
        if not isinstance(config, dict):
            continue
        affinity = config.get("affinity") or []
        if not isinstance(affinity, (list, tuple)) or target_idx >= len(affinity):
            continue
        val = affinity[target_idx]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        try:
            val_f = float(val)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(val_f):
            continue
        if val_f > best_affinity:
            best_affinity = val_f
            best_role = config.get("role")

    return best_role


def update_state_with_commitments(
    state: StateLike,
    new_commitments: list[Commitment],
) -> None:
    """Append new commitments to the session state accumulator."""
    raw = state.get("declared_commitments")
    existing = list(raw) if isinstance(raw, list) else []
    for c in new_commitments or ():
        existing.append(
            {
                "text": c.text,
                "principle_index": c.principle_index,
                "principle_name": c.principle_name,
                "tick_declared": c.tick_declared,
                "active": True,
            }
        )
    state["declared_commitments"] = existing


def update_state_with_contradictions(
    state: StateLike,
    new_contradictions: list[Contradiction],
) -> None:
    """Append contradictions to the session state log."""
    raw = state.get("contradiction_log")
    existing = list(raw) if isinstance(raw, list) else []
    for c in new_contradictions or ():
        existing.append(c.to_dict())
    state["contradiction_log"] = existing
