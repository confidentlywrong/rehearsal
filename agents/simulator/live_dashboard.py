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

"""Live dashboard A2UI surface builder.

Constructs a persistent A2UI surface card (surfaceId: "live-dashboard") that
updates on every tick with live resource gauges, stakeholder mix, and
infrastructure impact indicators.

Zero LLM cost — pure Python string formatting.
"""

import logging
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


def _bar(value: float, max_val: float = 100.0, width: int = 10) -> str:
    """Create a text-based progress bar using Unicode block characters.

    Retained for backwards compatibility with any caller that still wants a
    Unicode bar; the dashboard itself now emits A2UI ``ScoreBar`` components
    instead.
    """
    if max_val <= 0:
        return "░" * width
    ratio = min(max(value / max_val, 0.0), 1.0)
    filled = round(ratio * width)
    empty = width - filled
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
    bar_props: dict[str, Any] = {
        "label": {"literalString": label},
        "value": {"literalNumber": float(value)},
    }
    if max_value and abs(max_value - 100.0) > 0.001:
        bar_props["maxValue"] = {"literalNumber": float(max_value)}
    if band in ("high", "mid", "low"):
        bar_props["colorBand"] = {"literalString": band}
    return {
        "id": comp_id,
        "component": {"ScoreBar": bar_props},
    }


def _resource_status_emoji(current: float, critical: float) -> str:
    """Return status emoji based on resource health."""
    if current <= critical:
        return "🔴"
    if current <= critical * 1.5:
        return "⚠️"
    return ""


def _delta_str(delta: float) -> str:
    """Format a resource delta as a compact string."""
    if delta == 0:
        return ""
    sign = "▲" if delta > 0 else "▼"
    return f" {sign}{abs(delta):.0f}"


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

    # ── Progress overview ──
    components.append(
        _score_bar(
            "dash-progress",
            f"Sprint {tick}/{max_ticks} — {completion_pct:.0f}% aligned",
            completion_pct,
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
        pool = resource_state.get(key, {})
        if not isinstance(pool, dict):
            continue
        current = pool.get("current", 0)
        critical = pool.get("critical_threshold", 0)
        # Use the pool's initial value as the bar's maxValue so each resource
        # is shown relative to its own scale (budget peaks at 500–800, OWB
        # peaks at 40–70, etc.). Fall back to a sensible default.
        initial = pool.get("initial")
        if not isinstance(initial, (int, float)) or initial <= 0:
            initial = max(float(current), critical * 4.0, 100.0)
        name = _RESOURCE_SHORT.get(key, key)
        emoji = _resource_status_emoji(current, critical)

        # Get delta from resource_deltas dict
        rd = resource_deltas.get(key, {})
        delta = rd.get("delta", 0) if isinstance(rd, dict) else 0
        delta_s = _delta_str(delta)

        # Compose label: "Attention  82/100 ▲5 ⚠️" — including the absolute
        # value/initial disambiguates the bar (a full green bar on tick 1
        # otherwise reads as "nothing has happened" rather than "full
        # headroom").
        label_bits = [name, f"{float(current):.0f}/{float(initial):.0f}"]
        if delta_s:
            label_bits.append(delta_s.strip())
        if emoji:
            label_bits.append(emoji)
        label = " ".join(label_bits)

        comp_id = f"dash-res-{key}"
        # Status-driven band: critical → red, ≤2× critical → amber, else green.
        if critical and current <= critical:
            res_band = "low"
        elif critical and current <= critical * 1.5:
            res_band = "mid"
        else:
            res_band = "high"
        components.append(
            _score_bar(
                comp_id,
                label,
                float(current),
                max_value=float(initial),
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
    aligned = status_counts.get("finished", 0)
    active = status_counts.get("running", 0)
    stalled = status_counts.get("exhausted", 0)
    lost = status_counts.get("collapsed", 0)
    total = aligned + active + stalled + lost

    # Four ScoreBars instead of a single status text line. Each bar uses
    # ``total`` as its denominator so the fills are directly comparable.
    # Bands are *forced* per metric so the colors carry semantic meaning:
    #   Aligned → "more = good"  (green when high)
    #   Active  → neutral; always amber/mid (people in motion is normal)
    #   Stalled → "more = bad"   (low/red when share >25 %, amber 10–25 %)
    #   Lost    → "more = bad"   (low/red when share >10 %, amber 5–10 %)
    if total > 0:

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

        aligned_share = aligned / total
        stalled_share = stalled / total
        lost_share = lost / total

        components.append(
            _score_bar(
                "dash-stake-aligned",
                f"Aligned {aligned}/{total}",
                float(aligned),
                max_value=float(total),
                band=_good_band(aligned_share),
            )
        )
        content_children.append("dash-stake-aligned")

        components.append(
            _score_bar(
                "dash-stake-active",
                f"Active {active}/{total}",
                float(active),
                max_value=float(total),
                band="mid",
            )
        )
        content_children.append("dash-stake-active")

        components.append(
            _score_bar(
                "dash-stake-stalled",
                f"Stalled {stalled}/{total}",
                float(stalled),
                max_value=float(total),
                band=_bad_band(stalled_share, warn=0.10, crit=0.25),
            )
        )
        content_children.append("dash-stake-stalled")

        components.append(
            _score_bar(
                "dash-stake-lost",
                f"Lost {lost}/{total}",
                float(lost),
                max_value=float(total),
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
        counts = persona_status_counts.get(persona, {})
        if not counts:
            continue
        n = sum(counts.values())
        done = counts.get("finished", 0)
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

    infra = infrastructure or {}

    # Active Resistors — leadership-domain replacement for the marathon
    # "medical interventions" metric. Counts gatekeepers actively resisting
    # change this sprint.
    active_gatekeepers = infra.get("active_gatekeepers", 0)
    medical_saves = infra.get("medical_saves_this_tick", 0)
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
    comfort_raw = infra.get("avg_comfort", 100)
    try:
        comfort = float(comfort_raw)
    except (TypeError, ValueError):
        comfort = 0.0
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
    friction_level = infra.get("friction_level", "none")
    friction_emoji = {"high": "🔴", "moderate": "⚠️", "low": "🟢", "none": "✅"}.get(friction_level, "")
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
    if active_alerts_count > 0:
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
                            "literalString": f"{active_alerts_count} active alert{'s' if active_alerts_count != 1 else ''}"
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
