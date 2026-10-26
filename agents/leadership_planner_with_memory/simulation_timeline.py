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

"""Stakeholder simulation timeline tool (Artifact 2, MiroFish-style).

This is the **multi-tick simulation** layer of the
leadership_planner_with_memory workflow — the missing layer between a
single first-tick reaction (Artifact 0.5) and a final after-action
report (future Artifact 3). Modelled after MiroFish's OASIS subprocess
that writes `actions.jsonl` across many simulation steps, but condensed
into a single agent-supplied artifact:

* Personas from Step 0.25 are the simulation actors.
* Each "tick" represents one sprint (organizational time-step, not
  seconds). The agent chooses 3–8 ticks.
* Per tick, every persona gets a `(stance, sentiment)` snapshot plus an
  optional one-line `note`.
* Each tick also gets a `headline` and optional `events[]` describing
  what happened to drive that tick's shifts.

Design choices (locked 2026-06-28 by the operator):

* **Same "agent IS the simulator" pattern.** No nested LLM call inside
  the tool. The agent reads `state["stakeholder_personas"]` (and the
  graph, and the user's plan) and supplies the timeline as one JSON
  payload. The tool only validates + stashes.
* **No persistence yet.** Timeline lives in `tool_context.state`. No
  Neo4j, no Postgres rows. Persistence comes when Artifact 3 (report)
  starts needing to look back across past simulations.
* **Invented data is allowed here** — simulation layer. Every state row
  defaults to `inferred: true`. The UI tags the entire timeline as a
  synthetic projection.
* **One state per persona per tick.** When personas are present in
  state, the timeline must cover exactly that set of stakeholder_ids on
  each tick — no omissions, no extras. This keeps the visualization
  honest: every persona has a continuous trace across all sprints.

Downstream consumers:
* Artifact 3's report-builder will read this timeline + the personas to
  produce the after-action summary ("by sprint 3, mid-management
  resistance had spread to operations").
* Artifact 4's interview tool will read this timeline + a persona to
  ground its Q&A on what that persona "experienced" across the sprints.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# Allowed stances — MUST stay aligned with stakeholder_personas /
# stakeholder_reactions so the colored stance dot in the UI keeps the
# same meaning across Artifacts 0, 0.25, 0.5, 2.
ALLOWED_STANCES: frozenset[str] = frozenset({"supportive", "neutral", "resistant"})

# Sanity ceilings.
MIN_TICKS = 1
MAX_TICKS = 8
MAX_EVENTS_PER_TICK = 5
MAX_EVENT_CHARS = 160
MAX_NOTE_CHARS = 160
MAX_HEADLINE_CHARS = 200
MAX_SPRINT_LABEL_CHARS = 40
MAX_RATIONALE_CHARS = 500

# Session-state keys.
STATE_KEY = "simulation_timeline"
PERSONAS_STATE_KEY = "stakeholder_personas"


def _err(message: str, *, field: str = "timeline_json") -> dict[str, Any]:
    """Build a structured error response."""
    return {
        "status": "error",
        "field": field,
        "message": message,
    }


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a structured success response."""
    return {
        "status": "success",
        "simulation_timeline": payload,
    }


def _validate_float_range(
    tick_idx: int,
    state_idx: int,
    field: str,
    value: Any,
    lo: float,
    hi: float,
) -> tuple[float | None, str | None]:
    """Validate a numeric field falls in [lo, hi]. Accept int as float."""
    if isinstance(value, str):
        try:
            value = float(value)
        except (ValueError, TypeError):
            pass
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, (f"ticks[{tick_idx}].states[{state_idx}].{field} must be a number in [{lo}, {hi}]")
    v = float(value)
    if v < lo or v > hi:
        return None, (f"ticks[{tick_idx}].states[{state_idx}].{field}={v} outside [{lo}, {hi}]")
    return v, None


def _validate_short_string(label: str, value: Any, max_chars: int) -> tuple[str | None, str | None]:
    """Validate a non-empty short string. Returns (cleaned, error)."""
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be a non-empty string"
    s = value.strip()
    if len(s) > max_chars:
        return None, f"{label} length {len(s)} > {max_chars} chars"
    return s, None


def _validate_events(tick_idx: int, raw: Any) -> tuple[list[str] | None, str | None]:
    """Validate an optional events list."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, f"ticks[{tick_idx}].events must be a list"
    if len(raw) > MAX_EVENTS_PER_TICK:
        return None, (f"ticks[{tick_idx}].events has {len(raw)} entries (max {MAX_EVENTS_PER_TICK})")
    out: list[str] = []
    for j, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            return None, (f"ticks[{tick_idx}].events[{j}] must be a non-empty string")
        s = item.strip()
        if len(s) > MAX_EVENT_CHARS:
            return None, (f"ticks[{tick_idx}].events[{j}] length {len(s)} > {MAX_EVENT_CHARS} chars")
        out.append(s)
    return out, None


def _validate_state(
    tick_idx: int,
    state_idx: int,
    raw: Any,
    known_ids: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one per-persona state row inside a tick."""
    if not isinstance(raw, dict):
        return None, f"ticks[{tick_idx}].states[{state_idx}] must be an object"

    sid = raw.get("stakeholder_id")
    stance = raw.get("stance")
    sentiment = raw.get("sentiment")
    note = raw.get("note")
    inferred = raw.get("inferred", True)

    if not isinstance(sid, str) or not sid.strip():
        return None, (f"ticks[{tick_idx}].states[{state_idx}].stakeholder_id must be a non-empty string")
    sid_clean = sid.strip()
    if known_ids and sid_clean not in known_ids:
        return None, (
            f"ticks[{tick_idx}].states[{state_idx}].stakeholder_id="
            f"{sid_clean!r} does not match any persona in "
            f"state['{PERSONAS_STATE_KEY}']"
        )

    if stance not in ALLOWED_STANCES:
        return None, (f"ticks[{tick_idx}].states[{state_idx}].stance={stance!r} not in {sorted(ALLOWED_STANCES)}")

    sent_val, err = _validate_float_range(tick_idx, state_idx, "sentiment", sentiment, -1.0, 1.0)
    if err:
        return None, err

    note_clean: str | None = None
    if note is not None:
        if not isinstance(note, str):
            return None, (f"ticks[{tick_idx}].states[{state_idx}].note must be a string")
        s = note.strip()
        if s:
            if len(s) > MAX_NOTE_CHARS:
                return None, (f"ticks[{tick_idx}].states[{state_idx}].note length {len(s)} > {MAX_NOTE_CHARS} chars")
            note_clean = s

    if not isinstance(inferred, bool):
        return None, (f"ticks[{tick_idx}].states[{state_idx}].inferred must be a boolean")

    out: dict[str, Any] = {
        "stakeholder_id": sid_clean,
        "stance": stance,
        "sentiment": sent_val,
        "inferred": inferred,
    }
    if note_clean:
        out["note"] = note_clean
    return out, None


def _validate_tick(
    tick_idx: int,
    expected_tick_number: int,
    raw: Any,
    known_ids: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a single tick block."""
    if not isinstance(raw, dict):
        return None, f"ticks[{tick_idx}] must be an object"

    tick_num = raw.get("tick")
    if isinstance(tick_num, bool) or not isinstance(tick_num, int):
        return None, f"ticks[{tick_idx}].tick must be an integer"
    if tick_num != expected_tick_number:
        return None, (
            f"ticks[{tick_idx}].tick={tick_num} does not equal expected "
            f"sequential value {expected_tick_number}; ticks must be "
            "1-indexed and contiguous (1, 2, 3, ...)"
        )

    headline_clean, err = _validate_short_string(f"ticks[{tick_idx}].headline", raw.get("headline"), MAX_HEADLINE_CHARS)
    if err:
        return None, err

    sprint_label_raw = raw.get("sprint_label")
    sprint_label_clean: str | None = None
    if sprint_label_raw is not None:
        if not isinstance(sprint_label_raw, str):
            return None, f"ticks[{tick_idx}].sprint_label must be a string"
        s = sprint_label_raw.strip()
        if s:
            if len(s) > MAX_SPRINT_LABEL_CHARS:
                return None, (f"ticks[{tick_idx}].sprint_label length {len(s)} > {MAX_SPRINT_LABEL_CHARS} chars")
            sprint_label_clean = s

    events, err = _validate_events(tick_idx, raw.get("events"))
    if err:
        return None, err
    assert events is not None

    states_raw = raw.get("states")
    if not isinstance(states_raw, list) or not states_raw:
        return None, f"ticks[{tick_idx}].states must be a non-empty array"

    states: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    for j, sraw in enumerate(states_raw):
        normalized, err = _validate_state(tick_idx, j, sraw, known_ids)
        if err:
            return None, err
        assert normalized is not None
        sid = normalized["stakeholder_id"]
        if sid in seen_sids:
            return None, (
                f"ticks[{tick_idx}].states[{j}].stakeholder_id={sid!r} duplicates an earlier state on the same tick"
            )
        seen_sids.add(sid)
        states.append(normalized)

    # When personas are known, every persona MUST appear on every tick.
    # This keeps the visualization honest: every persona has a
    # continuous trace across all sprints.
    if known_ids and seen_sids != known_ids:
        missing = sorted(known_ids - seen_sids)
        extra = sorted(seen_sids - known_ids)
        details: list[str] = []
        if missing:
            details.append(f"missing personas: {missing}")
        if extra:
            details.append(f"unknown personas: {extra}")
        return None, (
            f"ticks[{tick_idx}].states must cover exactly the persona ids "
            f"from state['{PERSONAS_STATE_KEY}']; " + "; ".join(details)
        )

    out: dict[str, Any] = {
        "tick": tick_num,
        "headline": headline_clean,
        "states": states,
    }
    if sprint_label_clean:
        out["sprint_label"] = sprint_label_clean
    if events:
        out["events"] = events
    return out, None


async def emit_simulation_timeline(
    timeline_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate a multi-tick simulation timeline and stash it in session state.

    This is **Step 0.75** of the workflow, sitting between the Step-0.5
    first-tick reactions and the future after-action report. The agent
    supplies a 1–8 tick projection of how persona stance + sentiment
    shifts across sprints, optionally explaining each tick with a
    headline + driving events.

    Args:
        timeline_json: A JSON string with shape
            ``{"ticks": [
                {"tick": 1,
                 "sprint_label": "Sprint 1 (week 2)",
                 "headline": "Sponsorship holds; mid-mgmt friction emerges.",
                 "events": ["Town hall surfaces bandwidth fears"],
                 "states": [
                   {"stakeholder_id": "ceo",
                    "stance": "supportive",
                    "sentiment": 0.5,
                    "note": "Reaffirms timeline.",
                    "inferred": true},
                   ...one state per persona...
                 ]},
                ...up to 8 ticks...
              ],
              "rationale": "one-sentence why this projection"}``.
            Timeline is invented synthetic data belonging to the
            simulation layer.
        tool_context: ADK tool context. The validated payload is stored
            at ``tool_context.state["simulation_timeline"]``.

    Returns:
        On success: ``{"status": "success", "simulation_timeline": {...}}``.
        On failure: ``{"status": "error", "field": "...", "message": "..."}``.
    """
    # 1. Parse the JSON envelope (lenient: accept a dict directly).
    if isinstance(timeline_json, dict):
        data = timeline_json
    else:
        try:
            data = json.loads(timeline_json)
        except (TypeError, ValueError) as exc:
            return _err(f"invalid JSON: {exc}")
    if not isinstance(data, dict):
        return _err("payload must be a JSON object")

    # 2. Validate top-level shape.
    ticks_raw = data.get("ticks")
    if not isinstance(ticks_raw, list) or not ticks_raw:
        return _err("ticks must be a non-empty array")
    if len(ticks_raw) < MIN_TICKS or len(ticks_raw) > MAX_TICKS:
        return _err(f"ticks count {len(ticks_raw)} outside [{MIN_TICKS}, {MAX_TICKS}]")

    # 3. Resolve known persona ids. Permissive fallback: if no personas in
    #    state, accept any id (lets the tool run standalone in tests).
    known_ids: set[str] = set()
    try:
        personas_payload = tool_context.state.get(PERSONAS_STATE_KEY)
    except (AttributeError, TypeError):
        personas_payload = None
    if isinstance(personas_payload, dict):
        personas_list = personas_payload.get("personas")
        if isinstance(personas_list, list):
            for p in personas_list:
                if isinstance(p, dict):
                    pid = p.get("stakeholder_id")
                    if isinstance(pid, str) and pid.strip():
                        known_ids.add(pid.strip())

    # 4. Validate each tick. Ticks must be 1-indexed and contiguous.
    ticks: list[dict[str, Any]] = []
    for i, raw in enumerate(ticks_raw):
        normalized, err = _validate_tick(i, i + 1, raw, known_ids)
        if err:
            return _err(err, field="ticks")
        assert normalized is not None
        ticks.append(normalized)

    # 5. Optional rationale.
    rationale = data.get("rationale")
    rationale_clean: str | None = None
    if isinstance(rationale, str) and rationale.strip():
        rationale_clean = rationale.strip()[:MAX_RATIONALE_CHARS]

    payload: dict[str, Any] = {"ticks": ticks}
    if rationale_clean:
        payload["rationale"] = rationale_clean

    # 6. Stash for downstream report / interview tools.
    try:
        tool_context.state[STATE_KEY] = payload
    except (AttributeError, TypeError):
        logger.warning(
            "emit_simulation_timeline: could not write %s to session state",
            STATE_KEY,
        )

    return _ok(payload)
