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

"""Live dashboard A2UI surface builder.

Constructs a persistent A2UI surface card (surfaceId: "live-dashboard") that
updates on every tick with live resource gauges, stakeholder mix, and
infrastructure impact indicators.

Zero LLM cost — pure Python string formatting.
"""

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Resource display names
_RESOURCE_NAMES = {
    "lau": "Leadership Attention",
    "bt": "Budget",
    "tbu": "Team Bandwidth",
    "owb": "Org Well-Being",
}

# Resource short names for compact display
_RESOURCE_SHORT = {
    "lau": "Attention",
    "bt": "Budget",
    "tbu": "Bandwidth",
    "owb": "Well-Being",
}


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce numeric input to a finite float, rejecting None, bool, NaN, and Inf."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _finite_int(value: Any, default: int = 0) -> int:
    """Coerce numeric input to an int safely, rejecting None, bool, NaN, and Inf."""
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
    if not math.isfinite(num):
        return default
    return int(round(num))


def _bar(value: float, max_val: float = 100.0, width: int = 10) -> str:
    """Create a text-based progress bar using Unicode block characters.

    Retained for backwards compatibility with any caller that still wants a
    Unicode bar; the dashboard itself now emits A2UI ``ScoreBar`` components
    instead.
    """
    safe_width = max(0, _finite_int(width, 10))
    if safe_width == 0:
        return ""
    safe_max = _finite_float(max_val, 0.0)
    if safe_max <= 0:
        return "░" * safe_width
    safe_value = _finite_float(value, 0.0)
    ratio = min(max(safe_value / safe_max, 0.0), 1.0)
    filled = round(ratio * safe_width)
    empty = safe_width - filled
    return "█" * filled + "░" * empty


def _score_bar(
    comp_id: str,
    label: str,
    value: float,
    max_value: float = 100.0,
    band: str | None = None,
) -> dict:
    """Build an A2UI ScoreBar component dict.

    Args:
        band: Optional explicit color band ("high"|"mid"|"low"). When
            supplied it overrides the frontend's auto-band logic — required
            for metrics where "high = bad" (e.g. count of Lost
            stakeholders should be RED when high, not green).
    """
    # ScoreBar default range is 0..100. Override maxValue when the metric uses
    # a different scale (e.g. budget pools that start at 500+).
    safe_value = _finite_float(value, 0.0)
    safe_max = _finite_float(max_value, 100.0)
    bar_props: dict[str, Any] = {
        "label": {"literalString": label},
        "value": {"literalNumber": safe_value},
    }
    if safe_max > 0 and abs(safe_max - 100.0) > 0.001:
        bar_props["maxValue"] = {"literalNumber": safe_max}
    if band in ("high", "mid", "low"):
        bar_props["colorBand"] = {"literalString": band}
    return {
        "id": comp_id,
        "component": {"ScoreBar": bar_props},
    }


def _resource_status_emoji(current: float, critical: float) -> str:
    """Return status emoji based on resource health."""
    safe_current = _finite_float(current, 0.0)
    safe_critical = _finite_float(critical, 0.0)
    if safe_current <= safe_critical:
        return "🔴"
    warn_limit = safe_critical * 1.5
    is_warning = (
        safe_current <= warn_limit
        if math.isfinite(warn_limit)
        else (safe_critical > 0 and (safe_current / 1.5) <= safe_critical)
    )
    if is_warning:
        return "⚠️"
    return ""


def _delta_str(delta: float) -> str:
    """Format a resource delta as a compact string."""
    safe_delta = _finite_float(delta, 0.0)
    if safe_delta == 0:
        return ""
    sign = "▲" if safe_delta > 0 else "▼"
    return f" {sign}{abs(safe_delta):.0f}"


def build_live_dashboard(
    *,
    tick: int,
    max_ticks: int,
    completion_pct: float,
    resource_state: dict,
    resource_deltas: dict,
    status_counts: dict[str, int],
    persona_status_counts: dict[str, dict[str, int]],
    infrastructure: dict,
    active_alerts_count: int,
    avg_velocity: float,
    avg_water: float,
    voices: list[str] | None = None,
) -> dict:
    """Build the live dashboard A2UI surface payload.

    Returns a dict with ``surfaceUpdate`` and ``beginRendering`` keys,
    ready to be emitted via ``emit_gateway_message``.
    """
    components: list[dict] = []
    content_children: list[str] = []

    # ── Header ──
    components.append(
        {
            "id": "dash-title",
            "component": {
                "Text": {
                    "text": {"literalString": "TRANSFORMATION DASHBOARD"},
                    "usageHint": "h3",
                }
            },
        }
    )
    content_children.append("dash-title")

    safe_tick = _finite_int(tick, 0)
    safe_max_ticks = _finite_int(max_ticks, 0)
    safe_completion_pct = _finite_float(completion_pct, 0.0)
    safe_resource_state = resource_state if isinstance(resource_state, dict) else {}
    safe_resource_deltas = resource_deltas if isinstance(resource_deltas, dict) else {}
    safe_status_counts = status_counts if isinstance(status_counts, dict) else {}
    safe_persona_status_counts = persona_status_counts if isinstance(persona_status_counts, dict) else {}

    # ── Progress overview ──
    components.append(
        _score_bar(
            "dash-progress",
            f"Sprint {safe_tick}/{safe_max_ticks} — {safe_completion_pct:.0f}% aligned",
            safe_completion_pct,
            max_value=100.0,
        )
    )
    content_children.append("dash-progress")

    components.append(
        {
            "id": "dash-div1",
            "component": {"Divider": {}},
        }
    )
    content_children.append("dash-div1")

    # ── Resources section ──
    components.append(
        {
            "id": "dash-res-title",
            "component": {
                "Text": {
                    "text": {"literalString": "RESOURCES"},
                    "usageHint": "caption",
                }
            },
        }
    )
    content_children.append("dash-res-title")

    for key in ["lau", "bt", "tbu", "owb"]:
        pool = safe_resource_state.get(key, {})
        if not isinstance(pool, dict):
            continue
        current = _finite_float(pool.get("current"), 0.0)
        critical = _finite_float(pool.get("critical_threshold"), 0.0)
        # Use the pool's initial value as the bar's maxValue so each resource
        # is shown relative to its own scale (budget peaks at 500–800, OWB
        # peaks at 40–70, etc.). Fall back to a sensible default.
        initial = _finite_float(pool.get("initial"), 0.0)
        if initial <= 0:
            scaled_critical = critical * 4.0
            fallback_max = scaled_critical if math.isfinite(scaled_critical) else critical
            initial = max(current, fallback_max, 100.0)
        name = _RESOURCE_SHORT.get(key, key)
        emoji = _resource_status_emoji(current, critical)

        # Get delta from resource_deltas dict
        rd = safe_resource_deltas.get(key, {})
        delta = _finite_float(rd.get("delta"), 0.0) if isinstance(rd, dict) else 0.0
        delta_s = _delta_str(delta)

        # Compose label: "Attention  82/100 ▲5 ⚠️" — including the absolute
        # value/initial disambiguates the bar (a full green bar on tick 1
        # otherwise reads as "nothing has happened" rather than "full
        # headroom").
        label_bits = [name, f"{current:.0f}/{initial:.0f}"]
        if delta_s:
            label_bits.append(delta_s.strip())
        if emoji:
            label_bits.append(emoji)
        label = " ".join(label_bits)

        comp_id = f"dash-res-{key}"
        # Status-driven band: critical → red, ≤2× critical → amber, else green.
        warn_limit = critical * 1.5
        is_warn_band = (
            current <= warn_limit
            if math.isfinite(warn_limit)
            else (critical > 0 and (current / 1.5) <= critical)
        )
        if critical and current <= critical:
            res_band = "low"
        elif critical and is_warn_band:
            res_band = "mid"
        else:
            res_band = "high"
        components.append(
            _score_bar(
                comp_id,
                label,
                current,
                max_value=initial,
                band=res_band,
            )
        )
        content_children.append(comp_id)

    components.append(
        {
            "id": "dash-div2",
            "component": {"Divider": {}},
        }
    )
    content_children.append("dash-div2")

    # ── Stakeholder Mix ──
    components.append(
        {
            "id": "dash-stake-title",
            "component": {
                "Text": {
                    "text": {"literalString": "STAKEHOLDERS"},
                    "usageHint": "caption",
                }
            },
        }
    )
    content_children.append("dash-stake-title")

    # Status summary line
    aligned = max(0, _finite_int(safe_status_counts.get("finished"), 0))
    active = max(0, _finite_int(safe_status_counts.get("running"), 0))
    stalled = max(0, _finite_int(safe_status_counts.get("exhausted"), 0))
    lost = max(0, _finite_int(safe_status_counts.get("collapsed"), 0))
    total = aligned + active + stalled + lost

    # Four ScoreBars instead of a single status text line. Each bar uses
    # ``total`` as its denominator so the fills are directly comparable.
    # Bands are *forced* per metric so the colors carry semantic meaning:
    #   Aligned → "more = good"  (green when high)
    #   Active  → neutral; always amber/mid (people in motion is normal)
    #   Stalled → "more = bad"   (low/red when share >25 %, amber 10–25 %)
    #   Lost    → "more = bad"   (low/red when share >10 %, amber 5–10 %)
    total_f = _finite_float(total, 0.0)
    if total > 0 and total_f > 0.0:

        def _bad_band(share: float, warn: float, crit: float) -> str:
            if share >= crit:
                return "low"
            if share >= warn:
                return "mid"
            return "high"

        def _good_band(share: float) -> str:
            if share >= 0.5:
                return "high"
            if share >= 0.25:
                return "mid"
            return "low"

        aligned_f = _finite_float(aligned, 0.0)
        active_f = _finite_float(active, 0.0)
        stalled_f = _finite_float(stalled, 0.0)
        lost_f = _finite_float(lost, 0.0)

        aligned_share = aligned_f / total_f
        stalled_share = stalled_f / total_f
        lost_share = lost_f / total_f

        components.append(
            _score_bar(
                "dash-stake-aligned",
                f"Aligned {aligned}/{total}",
                aligned_f,
                max_value=total_f,
                band=_good_band(aligned_share),
            )
        )
        content_children.append("dash-stake-aligned")

        components.append(
            _score_bar(
                "dash-stake-active",
                f"Active {active}/{total}",
                active_f,
                max_value=total_f,
                band="mid",
            )
        )
        content_children.append("dash-stake-active")

        components.append(
            _score_bar(
                "dash-stake-stalled",
                f"Stalled {stalled}/{total}",
                stalled_f,
                max_value=total_f,
                band=_bad_band(stalled_share, warn=0.10, crit=0.25),
            )
        )
        content_children.append("dash-stake-stalled")

        components.append(
            _score_bar(
                "dash-stake-lost",
                f"Lost {lost}/{total}",
                lost_f,
                max_value=total_f,
                band=_bad_band(lost_share, warn=0.05, crit=0.10),
            )
        )
        content_children.append("dash-stake-lost")

    # Per-persona breakdown (compact)
    _PERSONA_ABBR = {
        "pragmatist": "P",
        "visionary": "V",
        "diplomat": "Di",
        "disruptor": "Du",
    }
    persona_parts = []
    for persona in ["pragmatist", "visionary", "diplomat", "disruptor"]:
        counts = safe_persona_status_counts.get(persona, {})
        if not isinstance(counts, dict) or not counts:
            continue
        n = sum(max(0, _finite_int(v, 0)) for v in counts.values())
        done = max(0, _finite_int(counts.get("finished"), 0))
        abbr = _PERSONA_ABBR.get(persona, persona[0].upper())
        if done > 0:
            persona_parts.append(f"{abbr}:{done}/{n}")
        else:
            persona_parts.append(f"{abbr}:{n}")

    if persona_parts:
        components.append(
            {
                "id": "dash-persona-mix",
                "component": {
                    "Text": {
                        "text": {"literalString": "Mix: " + "  ".join(persona_parts)},
                        "usageHint": "caption",
                    }
                },
            }
        )
        content_children.append("dash-persona-mix")

    # ── Stakeholder Voices (optional) ──
    if voices:
        components.append(
            {
                "id": "dash-div-voices",
                "component": {"Divider": {}},
            }
        )
        content_children.append("dash-div-voices")

        components.append(
            {
                "id": "dash-voices-title",
                "component": {
                    "Text": {
                        "text": {"literalString": "VOICES"},
                        "usageHint": "caption",
                    }
                },
            }
        )
        content_children.append("dash-voices-title")

        for idx, voice in enumerate(voices[:3]):
            if not isinstance(voice, str):
                voice = "" if voice is None else str(voice)
            # Strip leading thought-bubble emoji if present
            clean = voice.lstrip("\U0001f4ad ").strip()
            comp_id = f"dash-voice-{idx}"
            components.append(
                {
                    "id": comp_id,
                    "component": {
                        "Text": {
                            "text": {"literalString": f"💬 {clean}"},
                            "usageHint": "caption",
                        }
                    },
                }
            )
            content_children.append(comp_id)

    components.append(
        {
            "id": "dash-div3",
            "component": {"Divider": {}},
        }
    )
    content_children.append("dash-div3")

    # ── Infrastructure Impact ──
    components.append(
        {
            "id": "dash-infra-title",
            "component": {
                "Text": {
                    "text": {"literalString": "CHANGE ENABLEMENT"},
                    "usageHint": "caption",
                }
            },
        }
    )
    content_children.append("dash-infra-title")

    infra = infrastructure if isinstance(infrastructure, dict) else {}

    # Active Resistors — leadership-domain replacement for the marathon
    # "medical interventions" metric. Counts gatekeepers actively resisting
    # change this sprint.
    active_gatekeepers = max(0, _finite_int(infra.get("active_gatekeepers"), 0))
    medical_saves = max(0, _finite_int(infra.get("medical_saves_this_tick"), 0))
    resistor_bits = [f"Active Resistors: {active_gatekeepers}"]
    if medical_saves > 0:
        resistor_bits.append(f"{medical_saves} intervention{'s' if medical_saves != 1 else ''} this sprint")
    components.append(
        {
            "id": "dash-infra-resistors",
            "component": {
                "Text": {
                    "text": {"literalString": " · ".join(resistor_bits)},
                    "usageHint": "body",
                }
            },
        }
    )
    content_children.append("dash-infra-resistors")

    # Psych. Safety — render as a ScoreBar (it's already 0–100).
    comfort = _finite_float(infra.get("avg_comfort"), 100.0)
    comfort_emoji = "🔴" if comfort < 20 else ("⚠️" if comfort < 40 else "✅")
    components.append(
        _score_bar(
            "dash-infra-psych",
            f"Psych. Safety {comfort_emoji}",
            comfort,
            max_value=100.0,
        )
    )
    content_children.append("dash-infra-psych")

    # Org. Drag — categorical, keep as Text but on its own line.
    friction_raw = infra.get("friction_level")
    friction_level = str(friction_raw).strip() if isinstance(friction_raw, str) and friction_raw.strip() else "none"
    friction_emoji = {"high": "🔴", "moderate": "⚠️", "low": "🟢", "none": "✅"}.get(friction_level.lower(), "")
    components.append(
        {
            "id": "dash-infra-drag",
            "component": {
                "Text": {
                    "text": {"literalString": f"Org. Drag: {friction_level.title()} {friction_emoji}".rstrip()},
                    "usageHint": "body",
                }
            },
        }
    )
    content_children.append("dash-infra-drag")

    # Backwards-compat alias: keep ``dash-infra-status`` pointing at the first
    # of the three rows so existing tests/consumers continue to resolve it.
    # (No new component added — older code paths that look up the ID by name
    #  will still find a body row, just one of three instead of one combined
    #  string.)

    # ── Active Alerts ──
    safe_alerts = max(0, _finite_int(active_alerts_count, 0))
    if safe_alerts > 0:
        components.append(
            {
                "id": "dash-div4",
                "component": {"Divider": {}},
            }
        )
        content_children.append("dash-div4")

        components.append(
            {
                "id": "dash-alerts",
                "component": {
                    "Text": {
                        "text": {
                            "literalString": f"{safe_alerts} active alert{'s' if safe_alerts != 1 else ''}"
                        },
                        "usageHint": "caption",
                    }
                },
            }
        )
        content_children.append("dash-alerts")

    # ── Assemble Card ──
    components.append(
        {
            "id": "dash-content",
            "component": {"Column": {"children": {"explicitList": content_children}}},
        }
    )
    components.append(
        {
            "id": "dash-card",
            "component": {"Card": {"child": "dash-content"}},
        }
    )

    return {
        "surfaceUpdate": {
            "surfaceId": "live-dashboard",
            "components": components,
        },
        "beginRendering": {
            "surfaceId": "live-dashboard",
            "root": "dash-card",
        },
    }
