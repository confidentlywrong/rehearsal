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

"""Algorithmic Readiness Index (ARI) — post-simulation summary.

Computes the ARI = O × J × D × S from simulation state accumulated across
all ticks.  Called once at the end of the simulation by ``compile_results``.

The four dimensions measure:
  O (Ontology)  — cross-resource decision integration
  J (Judgment)  — principle consistency and codified rules
  D (Decision)  — response latency to critical events
  S (Scale)     — stakeholder-specific response differentiation

Each dimension is scored 0.0–1.0.  The multiplicative formula ensures that
a single weak dimension dominates the total — matching the "Sequence"
dependency model from the Leadership Crucible framework.

ARI zones:
  < 0.10  → Dead Zone   — one or more dimensions are collapsed
  0.10–0.30 → Transition — architecturally coherent, still improving
  > 0.30  → Compounding — all four dimensions reinforce each other

Zero LLM cost — pure Python.
"""

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce numeric value to finite float, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _finite_int(value: Any, default: int = 0) -> int:
    """Coerce non-bool int/float value to finite int, falling back to default."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return int(value) if math.isfinite(num) else default


def compute_ari_dimensions(state: dict) -> dict:
    """Compute the four ARI dimensions from accumulated simulation state.

    Args:
        state: Session state dict with tick_snapshots, decision_log,
               principle_scores, resources, echo_voice_log, etc.

    Returns:
        Dict with keys: o, j, d, s, ari, zone, dimension_details.
    """
    safe_state = state if hasattr(state, "get") else {}
    o = min(1.0, max(0.0, _finite_float(_compute_ontology(safe_state), 0.2)))
    j = min(1.0, max(0.0, _finite_float(_compute_judgment(safe_state), 0.2)))
    d = min(1.0, max(0.0, _finite_float(_compute_decision(safe_state), 0.4)))
    s = min(1.0, max(0.0, _finite_float(_compute_scale(safe_state), 0.2)))

    ari = round(o * j * d * s, 4)

    if ari < 0.10:
        zone = "Dead Zone"
    elif ari <= 0.30:
        zone = "Transition"
    else:
        zone = "Compounding"

    result = {
        "o": round(o, 2),
        "j": round(j, 2),
        "d": round(d, 2),
        "s": round(s, 2),
        "ari": ari,
        "zone": zone,
        "dimension_details": {
            "ontology": _ontology_detail(safe_state, o),
            "judgment": _judgment_detail(safe_state, j),
            "decision": _decision_detail(safe_state, d),
            "scale": _scale_detail(safe_state, s),
        },
    }

    logger.info(
        "ARI computed: O=%.2f J=%.2f D=%.2f S=%.2f → ARI=%.4f (%s)",
        o, j, d, s, ari, zone,
    )

    return result


def _compute_ontology(state: dict) -> float:
    """O: How well the leader integrates data across resource domains.

    Measured by how many resource pools the leader's decisions impact
    simultaneously across the simulation.  A leader who only touches
    one resource at a time has siloed thinking (low O); one who
    considers cross-resource trade-offs has integrated thinking (high O).
    """
    decision_log = state.get("decision_log") or []
    if not isinstance(decision_log, list) or not decision_log:
        return 0.2  # ad hoc baseline

    resource_keys = {"lau", "bt", "tbu", "owb"}
    cross_impact_ticks = 0
    total_decision_ticks = 0

    for entry in decision_log:
        if not isinstance(entry, dict):
            continue
        tags = entry.get("decision_tags") or {}
        if not isinstance(tags, dict):
            continue
        impacts = tags.get("resource_impact_suggestion") or {}
        if not isinstance(impacts, dict) or not impacts:
            continue
        total_decision_ticks += 1
        touched = {
            k
            for k, val in impacts.items()
            if k in resource_keys and _finite_float(val, 0.0) != 0.0
        }
        if len(touched) >= 2:
            cross_impact_ticks += 1

    if total_decision_ticks == 0:
        return 0.2

    cross_ratio = cross_impact_ticks / total_decision_ticks
    # Map: 0% cross-impact → 0.2, 50%+ → 0.8, 100% → 1.0
    return min(1.0, 0.2 + cross_ratio * 0.8)


def _compute_judgment(state: dict) -> float:
    """J: Consistency of principle application (the Codex).

    Measured by principle_scores variance and contradiction count.
    Consistent principle application = high J.  Erratic usage or
    many contradictions = low J.
    """
    raw_scores = state.get("principle_scores")
    principle_scores = (
        [_finite_float(sc, 0.0) for sc in raw_scores]
        if isinstance(raw_scores, (list, tuple)) and raw_scores
        else [0.0] * 7
    )
    contradiction_log = state.get("contradiction_log") or []
    if not isinstance(contradiction_log, list):
        contradiction_log = []

    # Principle consistency: low variance = high consistency
    if principle_scores and any(sc > 0 for sc in principle_scores):
        if min(principle_scores) == max(principle_scores):
            norm_variance = 0.0
        else:
            n = len(principle_scores)
            raw_sum = sum(principle_scores)
            if math.isfinite(raw_sum):
                mean_score = raw_sum / n
                scale = 1.0
            else:
                scaled_sum = sum(sc / n for sc in principle_scores)
                if math.isfinite(scaled_sum):
                    mean_score = scaled_sum
                    scale = 1.0
                else:
                    mean_score = sum((sc * 0.5) / n for sc in principle_scores)
                    scale = 0.5

            overflowed = False
            sq_diffs: list[float] = []
            for sc in principle_scores:
                diff = (sc * scale) - mean_score
                if not math.isfinite(diff):
                    overflowed = True
                    break
                try:
                    sq = diff ** 2
                    if scale != 1.0:
                        sq = sq * 4.0
                except OverflowError:
                    overflowed = True
                    break
                if not math.isfinite(sq):
                    overflowed = True
                    break
                sq_diffs.append(sq)

            if overflowed:
                norm_variance = 1.0
            else:
                var_sum = sum(sq_diffs)
                if not math.isfinite(var_sum):
                    norm_variance = 1.0
                else:
                    variance = var_sum / n
                    norm_variance = min(variance / 2500.0, 1.0) if math.isfinite(variance) else 1.0
        consistency = 1.0 - norm_variance
    else:
        consistency = 0.2  # No data → ad hoc

    # Contradiction penalty: each contradiction reduces J
    contradiction_penalty = min(len(contradiction_log) * 0.05, 0.4)

    return max(0.0, min(1.0, consistency - contradiction_penalty))


def _compute_decision(state: dict) -> float:
    """D: How quickly the leader responds to critical resource drops.

    Measured by tracking resource threshold breaches and how many ticks
    pass before the leader's decisions provide positive impact to the
    critical resource.
    """
    snapshots = state.get("tick_snapshots") or []
    if not isinstance(snapshots, list) or len(snapshots) < 3:
        return 0.4  # structured baseline (insufficient data)

    # Track resource criticality events and response
    decision_log = state.get("decision_log") or []
    if not isinstance(decision_log, list):
        decision_log = []
    critical_events = 0
    fast_responses = 0

    # Build a map of which ticks had positive resource impacts
    tick_impacts: dict[int, dict] = {}
    for entry in decision_log:
        if not isinstance(entry, dict):
            continue
        tick = _finite_int(entry.get("tick", -1), -1)
        tags = entry.get("decision_tags") or {}
        if not isinstance(tags, dict):
            continue
        impacts = tags.get("resource_impact_suggestion") or {}
        if isinstance(impacts, dict):
            tick_impacts[tick] = impacts

    # Detect when resources went critical and if the next decision addressed it
    for i, snap in enumerate(snapshots):
        if not isinstance(snap, dict):
            continue
        org_state = snap.get("organizational_state")
        org_resources = org_state.get("resources", {}) if isinstance(org_state, dict) else {}
        resources = snap.get("resources") or snap.get("resource_state") or org_resources
        if not isinstance(resources, dict):
            continue
        snap_tick = _finite_int(snap.get("tick"), i)
        for key in ["lau", "bt", "tbu", "owb"]:
            pool = resources.get(key, {})
            if not isinstance(pool, dict):
                continue
            current = _finite_float(pool.get("current"), 100.0)
            threshold = _finite_float(pool.get("critical_threshold"), 20.0)
            if current <= threshold:
                critical_events += 1
                # Check if next 1-2 ticks have positive impact for this resource
                for offset in range(1, 3):
                    future_tick = snap_tick + offset
                    future_impacts = tick_impacts.get(future_tick, {})
                    if _finite_float(future_impacts.get(key), 0.0) > 0.0:
                        fast_responses += 1
                        break

    if critical_events == 0:
        return 0.6  # systematic baseline (no crises = competent)

    response_ratio = fast_responses / critical_events
    # Map: 0% response → 0.2, 50% → 0.6, 100% → 1.0
    return min(1.0, 0.2 + response_ratio * 0.8)


def _compute_scale(state: dict) -> float:
    """S: Whether decisions differentiate across stakeholder personas.

    Measured by stance diversity in the echo population.  If all echoes
    converge to the same stance, the leader is applying uniform approaches
    (low S).  If echoes diverge based on the leader's targeted engagement,
    the leader is scaling individualized response (high S).
    """
    echo_states = state.get("_echo_states") or {}
    echo_voice_log = state.get("echo_voice_log") or []

    if not echo_states and not echo_voice_log:
        return 0.2  # ad hoc baseline

    # Use final echo states if available
    if isinstance(echo_states, dict) and echo_states:
        stances = [str(es.get("stance") or "neutral") for es in echo_states.values() if isinstance(es, dict)]
    elif isinstance(echo_voice_log, list) and echo_voice_log:
        # Fall back to last tick of voice log
        def _safe_entry_tick(entry: dict) -> int:
            return _finite_int(entry.get("tick"), 0)

        valid_entries = [e for e in echo_voice_log if isinstance(e, dict)]
        max_tick = max((_safe_entry_tick(e) for e in valid_entries), default=0)
        stances = [
            str(e.get("stance") or "neutral")
            for e in valid_entries
            if _safe_entry_tick(e) == max_tick
        ]
    else:
        return 0.2

    if len(stances) < 2:
        return 0.2

    # Stance diversity: how many distinct stances exist
    unique_stances = len(set(stances))
    max_possible = min(len(stances), 7)  # max 7 stance types

    # Also measure if some are positive and some negative (true differentiation)
    positive = sum(1 for s in stances if s in ("supportive", "allied", "champion"))
    negative = sum(1 for s in stances if s in ("hostile", "opposed", "skeptical", "resistant"))

    diversity = unique_stances / max_possible
    spread = 1.0 if (positive > 0 and negative > 0) else 0.5 if (positive > 0 or negative > 0) else 0.2

    # Combine: diversity of stances + presence of spread
    return min(1.0, diversity * 0.5 + spread * 0.5)


# --- Dimension detail strings ---

def _ontology_detail(state: dict, score: float) -> str:
    if score >= 0.8:
        return "Decisions consistently address cross-resource trade-offs."
    elif score >= 0.6:
        return "Some decisions integrate multiple resource domains."
    elif score >= 0.4:
        return "Decisions tend to address resources in isolation."
    else:
        return "Data silos: decisions rarely connect resource domains."


def _judgment_detail(state: dict, score: float) -> str:
    log = state.get("contradiction_log") or []
    n = len(log) if isinstance(log, list) else 0
    if score >= 0.8:
        return "Principles applied consistently across decisions."
    elif score >= 0.6:
        return f"Mostly consistent, {n} contradiction(s) detected."
    elif score >= 0.4:
        return f"Principle application is erratic. {n} contradiction(s)."
    else:
        return f"Significant inconsistency. {n} contradiction(s) detected."


def _decision_detail(state: dict, score: float) -> str:
    if score >= 0.8:
        return "Critical resource drops addressed within 1-2 rounds."
    elif score >= 0.6:
        return "Adequate response latency to resource crises."
    elif score >= 0.4:
        return "Delayed responses to some critical events."
    else:
        return "Resource crises frequently go unaddressed for multiple rounds."


def _scale_detail(state: dict, score: float) -> str:
    echo_states = state.get("_echo_states") or {}
    n = len(echo_states) if isinstance(echo_states, dict) else 0
    if n == 0:
        voice_log = state.get("echo_voice_log") or []
        n = len({e.get("role") for e in voice_log if isinstance(e, dict) and e.get("role")}) if isinstance(voice_log, list) else 0
    if score >= 0.8:
        return f"Stakeholder responses are differentiated across {n} personas."
    elif score >= 0.6:
        return "Some differentiation in stakeholder engagement."
    elif score >= 0.4:
        return "Stakeholders respond uniformly — limited individualization."
    else:
        return "All stakeholders converge — decisions lack personalization."


def build_ari_a2ui_card(ari_result: dict) -> dict:
    """Build a single A2UI surface card for the ARI summary.

    Returns a dict with ``surfaceUpdate`` and ``beginRendering`` keys.
    """
    safe_result = ari_result if isinstance(ari_result, dict) else {}
    o = min(1.0, max(0.0, _finite_float(safe_result.get("o"), 0.0)))
    j = min(1.0, max(0.0, _finite_float(safe_result.get("j"), 0.0)))
    d = min(1.0, max(0.0, _finite_float(safe_result.get("d"), 0.0)))
    s = min(1.0, max(0.0, _finite_float(safe_result.get("s"), 0.0)))
    ari = min(1.0, max(0.0, _finite_float(safe_result.get("ari"), 0.0)))
    zone = str(safe_result.get("zone") or "Dead Zone")
    details = safe_result.get("dimension_details") or {}
    if not isinstance(details, dict):
        details = {}

    zone_emoji = "🔴" if zone == "Dead Zone" else "🟡" if zone == "Transition" else "🟢"

    components: list[dict] = []
    children: list[str] = []

    # Title
    components.append({
        "id": "ari-title",
        "component": {
            "Text": {
                "text": {"literalString": "ALGORITHMIC READINESS INDEX"},
                "usageHint": "h3",
            }
        },
    })
    children.append("ari-title")

    # Formula
    components.append({
        "id": "ari-formula",
        "component": {
            "Text": {
                "text": {"literalString": f"ARI = O × J × D × S = {ari:.3f}  {zone_emoji} {zone}"},
                "usageHint": "body",
            }
        },
    })
    children.append("ari-formula")

    components.append({"id": "ari-div1", "component": {"Divider": {}}})
    children.append("ari-div1")

    # Dimension bars
    dims = [
        ("O", "Ontology", o, details.get("ontology", "")),
        ("J", "Judgment (Codex)", j, details.get("judgment", "")),
        ("D", "Decision", d, details.get("decision", "")),
        ("S", "Scale", s, details.get("scale", "")),
    ]

    for letter, name, value, detail in dims:
        dim_id = f"ari-dim-{letter.lower()}"
        components.append({
            "id": dim_id,
            "component": {
                "ScoreBar": {
                    "label": {"literalString": f"{letter}  {name}"},
                    "value": {"literalNumber": round(value * 100)},
                    "maxValue": {"literalNumber": 100},
                }
            },
        })
        children.append(dim_id)

        detail_id = f"ari-detail-{letter.lower()}"
        components.append({
            "id": detail_id,
            "component": {
                "Text": {
                    "text": {"literalString": detail},
                    "usageHint": "caption",
                }
            },
        })
        children.append(detail_id)

    components.append({"id": "ari-div2", "component": {"Divider": {}}})
    children.append("ari-div2")

    # Weakest dimension callout
    dim_scores = {"o": o, "j": j, "d": d, "s": s}
    weakest_letter, weakest_name = min(
        [("O", "Ontology"), ("J", "Judgment"), ("D", "Decision"), ("S", "Scale")],
        key=lambda x: dim_scores[x[0].lower()],
    )
    weakest_val = dim_scores[weakest_letter.lower()]

    callout = (
        f"⚠️  {weakest_name} ({weakest_letter}={weakest_val:.2f}) is the ceiling on the total. "
        f"Three strong dimensions cannot compensate for one broken one."
    )
    components.append({
        "id": "ari-callout",
        "component": {
            "Text": {
                "text": {"literalString": callout},
                "usageHint": "body",
            }
        },
    })
    children.append("ari-callout")

    components.append({
        "id": "ari-content",
        "component": {
            "Column": {
                "children": {"explicitList": children},
            }
        },
    })

    components.append({
        "id": "ari-card",
        "component": {
            "Card": {
                "expandable": {"literalBoolean": True},
                "child": "ari-content",
            }
        },
    })

    surface_id = "ari-summary"
    return {
        "surfaceUpdate": {
            "surfaceId": surface_id,
            "components": components,
        },
        "beginRendering": {
            "surfaceId": surface_id,
            "root": "ari-card",
        },
    }
