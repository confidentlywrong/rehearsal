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

"""Post-tick coaching engine for the Leadership Crucible.

Provides deterministic, zero-LLM micro-feedback and A2UI coaching cards
that are injected into the ``advance_tick`` result dict.  The frontend
reads ``micro_feedback`` (string) and ``coaching_card`` (optional A2UI
payload dict) from the tick result.

No LLM dependency — pure Python logic using the coaching_prompts library.
"""

import logging

try:
    from agents.ai_echo.coaching_prompts import (
        get_coaching_prompt,
        get_contradiction_prompt,
        get_resource_crisis_prompt,
    )
except ImportError:
    # Graceful degradation: if ai_echo is removed or renamed, the simulator
    # continues to function with generic coaching prompts instead of crashing.
    logger = logging.getLogger(__name__)
    logger.warning("ai_echo.coaching_prompts unavailable; using generic coaching fallbacks")

    def get_coaching_prompt(principle: str, tick: int, affected_echo: str = "") -> str:  # type: ignore[misc]
        return f"Consider how {principle} applies to your current situation (tick {tick})."

    def get_contradiction_prompt(commitment: str, tick: int) -> str:  # type: ignore[misc]
        return f"Your actions may conflict with your commitment to {commitment} (tick {tick})."

    def get_resource_crisis_prompt(resource: str, tick: int) -> str:  # type: ignore[misc]
        return f"Resource {resource} is critically low — consider reallocating (tick {tick})."


logger = logging.getLogger(__name__)

# Resource names for display
_RESOURCE_NAMES = {
    "lau": "Leadership Attention",
    "bt": "Budget",
    "tbu": "Team Bandwidth",
    "owb": "Org Well-Being",
}


def generate_micro_feedback(
    decision_tags: dict,
    resource_state: dict,
    alerts: list,
    tick: int,
    contradiction_alert: dict | None = None,
) -> str:
    """Generate a concise 1-line feedback string for the participant.

    Called after each tick's aggregation to provide immediate, actionable
    micro-feedback without LLM cost.

    Args:
        decision_tags: Output from decision classifier.
        resource_state: Current resource pools.
        alerts: ECSI alerts from this tick.
        tick: Current tick number.
        contradiction_alert: If set, a Say-Do gap was detected.

    Returns:
        A single-line feedback string with emoji indicators.
    """
    parts: list[str] = []

    # 1. Acknowledge applied principles (strength-based)
    applied = decision_tags.get("principles_applied", [])
    if applied:
        # Show the first applied principle, shortened
        principle_short = applied[0].split("&")[0].strip()
        parts.append(f"✅ Strong: {principle_short}")

    # 2. Flag neglected principles
    neglected = decision_tags.get("principles_neglected", [])
    if neglected:
        neglected_short = neglected[0].split("&")[0].strip()
        parts.append(f"💭 Consider: {neglected_short}")

    # 3. Contradiction warning
    if contradiction_alert:
        parts.append("⚡ Say-Do gap detected")

    # 4. Resource warnings (lowest resource approaching threshold)
    lowest_name = ""
    lowest_ratio = float("inf")
    for name, pool in resource_state.items():
        current = pool.get("current", 100)
        threshold = pool.get("critical_threshold", 20)
        if threshold > 0:
            ratio = current / threshold
            if ratio < lowest_ratio:
                lowest_ratio = ratio
                lowest_name = name

    if lowest_ratio < 1.0:
        display_name = _RESOURCE_NAMES.get(lowest_name, lowest_name.upper())
        current_val = resource_state[lowest_name].get("current", 0)
        parts.append(f"🔴 CRITICAL: {display_name} at {current_val:.0f}")
    elif lowest_ratio < 1.5:
        display_name = _RESOURCE_NAMES.get(lowest_name, lowest_name.upper())
        current_val = resource_state[lowest_name].get("current", 0)
        parts.append(f"⚠️ Watch: {display_name} at {current_val:.0f}")

    # 5. Active ECSI alerts
    if alerts:
        parts.append(f"🔔 {len(alerts)} alert(s)")

    if not parts:
        parts.append("📊 Simulation progressing normally")

    return " | ".join(parts)


def _severity_bucket(current: float, threshold: float) -> str:
    """Categorize a resource value into a coarse severity bucket.

    Used for coaching-card deduplication so we re-emit only when the
    situation materially changes, not every tick.
    """
    if current <= 0:
        return "depleted"
    if threshold <= 0:
        return "ok"
    if current <= threshold:
        return "critical"
    if current <= threshold * 1.5:
        return "warning"
    return "ok"


def build_coaching_card(
    resource_state: dict,
    decision_tags: dict,
    alerts: list,
    tick: int,
    max_ticks: int,
    contradiction_alert: dict | None = None,
    previous_resource_state: dict | None = None,
    last_coaching_signature: tuple | None = None,
) -> dict | None:
    """Build an A2UI coaching card when resources hit early-warning thresholds.

    Returns an A2UI ``surfaceUpdate`` + ``beginRendering`` payload dict
    when coaching is warranted, or ``None`` if the situation is nominal
    *or* unchanged since the last emission.

    Deduplication: the card is keyed by a ``signature`` tuple of
    ``(trigger_resource, severity_bucket, trigger_principle,
    has_contradiction)``. If the new signature matches
    ``last_coaching_signature``, the function returns ``None`` so the
    coach stops re-broadcasting the same advice every tick.

    When ``previous_resource_state`` is supplied, the card body embeds a
    causal context line (delta vs. previous tick + the principle(s) the
    last decision applied or neglected) so the participant can see *why*
    the advisory fired now.

    Args:
        resource_state: Current resource pools.
        decision_tags: Output from decision classifier.
        alerts: ECSI alerts from this tick.
        tick: Current tick number.
        max_ticks: Total ticks in the quest.
        contradiction_alert: If set, a Say-Do gap was detected.
        previous_resource_state: Resource pools from the previous tick
            (used to compute deltas for the causal-context line).
        last_coaching_signature: Signature of the last emitted card; if
            equal to the freshly-computed signature, suppress emission.

    Returns:
        A2UI card payload dict (with an extra ``signature`` key the
        caller should persist), or ``None`` when no card should be
        emitted.
    """
    # Determine the coaching trigger
    trigger_resource = None
    trigger_principle = None
    coaching_text = ""

    # Priority 1: Contradiction coaching
    if contradiction_alert:
        commitment = contradiction_alert.get("commitment_text", "a prior commitment")
        coaching_text = get_contradiction_prompt(commitment, tick)
        trigger_principle = contradiction_alert.get("violated_principle", "")

    # Priority 2: Resource crisis coaching
    if not coaching_text:
        for name, pool in resource_state.items():
            current = pool.get("current", 100)
            threshold = pool.get("critical_threshold", 20)
            # Early warning zone: between threshold and threshold * 1.5
            if current < threshold * 1.5 and current > 0:
                trigger_resource = name
                coaching_text = get_resource_crisis_prompt(name, tick)
                break

    # Priority 3: Principle coaching (if neglected principles detected)
    if not coaching_text:
        neglected = decision_tags.get("principles_neglected", [])
        if neglected:
            trigger_principle = neglected[0]
            # Find which echo is most affected
            coaching_text = get_coaching_prompt(trigger_principle, tick)

    if not coaching_text:
        return None

    # --------------------------------------------------------------
    # Compute dedup signature. The signature deliberately ignores
    # `tick` and exact resource values so warning-zone ticks don't
    # spam the surface with three near-identical cards in a row.
    # --------------------------------------------------------------
    severity_bucket = "info"
    if trigger_resource:
        pool = resource_state.get(trigger_resource, {})
        severity_bucket = _severity_bucket(pool.get("current", 100), pool.get("critical_threshold", 20))
    elif contradiction_alert:
        severity_bucket = "contradiction"
    elif trigger_principle:
        severity_bucket = "principle"

    signature = (
        trigger_resource,
        severity_bucket,
        trigger_principle,
        bool(contradiction_alert),
    )

    if last_coaching_signature is not None and signature == last_coaching_signature:
        # Nothing material has changed since the last card — stay quiet.
        return None

    # Determine card title, severity text, severity pill usageHint, and
    # optional resource ScoreBar payload. Pill colour mirrors the
    # _severity_bucket scale: critical/contradiction -> red, warning/principle
    # -> amber, info -> neutral.
    _SEVERITY_PILL = {
        "critical": "severity-major",
        "contradiction": "severity-major",
        "warning": "severity-moderate",
        "principle": "severity-moderate",
        "info": "severity-minor",
    }
    severity_pill_hint = _SEVERITY_PILL.get(severity_bucket, "severity-minor")
    resource_score_value: int | None = None
    resource_score_label: str | None = None
    if contradiction_alert:
        card_title = "💡 Integrity Check"
        severity = "Say-Do Gap Detected"
        severity_pill_hint = "severity-major"
    elif trigger_resource:
        display_name = _RESOURCE_NAMES.get(trigger_resource, trigger_resource.upper())
        current_val = resource_state[trigger_resource].get("current", 0)
        card_title = "💡 Resource Advisory"
        # Pill carries only the severity word; the numeric value moves to
        # a ScoreBar below so the user sees the bar fill at a glance.
        severity = severity_bucket.upper()
        resource_score_value = int(round(float(current_val)))
        resource_score_label = display_name
    else:
        card_title = "💡 Leadership Insight"
        severity = f"Tick {tick}/{max_ticks}"
        severity_pill_hint = "severity-minor"

    # --------------------------------------------------------------
    # Causal context: what changed and why we're surfacing this now.
    # --------------------------------------------------------------
    cause_lines: list[str] = []
    if trigger_resource and previous_resource_state:
        prev_pool = previous_resource_state.get(trigger_resource, {})
        prev_val = prev_pool.get("current")
        if prev_val is not None:
            curr_val = resource_state[trigger_resource].get("current", 0)
            delta = curr_val - prev_val
            if abs(delta) >= 0.5:
                arrow = "▼" if delta < 0 else "▲"
                display_name = _RESOURCE_NAMES.get(trigger_resource, trigger_resource.upper())
                cause_lines.append(
                    f"{display_name} {arrow} {abs(delta):.0f} since last tick ({prev_val:.0f} → {curr_val:.0f})."
                )

    applied = decision_tags.get("principles_applied", []) or []
    neglected = decision_tags.get("principles_neglected", []) or []
    if neglected:
        neglected_short = neglected[0].split("&")[0].strip()
        cause_lines.append(f"Last decision underweighted: {neglected_short}.")
    elif applied and trigger_resource:
        applied_short = applied[0].split("&")[0].strip()
        cause_lines.append(
            f"Last decision leaned into {applied_short} — at a cost to "
            f"{_RESOURCE_NAMES.get(trigger_resource, trigger_resource.upper())}."
        )

    cause_text = " ".join(cause_lines).strip()

    # Build A2UI flat component structure
    components = [
        {
            "id": "coach-title",
            "component": {"Text": {"text": {"literalString": card_title}, "usageHint": "h3"}},
        },
        {
            "id": "coach-severity",
            "component": {"Text": {"text": {"literalString": severity}, "usageHint": severity_pill_hint}},
        },
        {
            "id": "coach-header",
            "component": {"Row": {"children": {"explicitList": ["coach-title", "coach-severity"]}}},
        },
    ]

    content_children = ["coach-header"]

    if resource_score_value is not None and resource_score_label is not None:
        components.append(
            {
                "id": "coach-resource-bar",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": resource_score_label},
                        "value": {"literalNumber": resource_score_value},
                    }
                },
            }
        )
        content_children.append("coach-resource-bar")

    if cause_text:
        components.append(
            {
                "id": "coach-cause",
                "component": {"Text": {"text": {"literalString": cause_text}, "usageHint": "caption"}},
            }
        )
        content_children.append("coach-cause")

    components.extend(
        [
            {
                "id": "coach-text",
                "component": {"Text": {"text": {"literalString": coaching_text}, "usageHint": "body"}},
            },
            {
                "id": "coach-divider",
                "component": {"Divider": {}},
            },
        ]
    )
    content_children.extend(["coach-text", "coach-divider"])

    components.append(
        {
            "id": "coach-content",
            "component": {"Column": {"children": {"explicitList": content_children}}},
        }
    )
    components.append(
        {
            "id": "coach-card",
            "component": {"Card": {"child": "coach-content"}},
        }
    )

    return {
        "surfaceUpdate": {
            "surfaceId": "coaching",
            "components": components,
        },
        "beginRendering": {
            "surfaceId": "coaching",
            "root": "coach-card",
        },
        "signature": signature,
    }
