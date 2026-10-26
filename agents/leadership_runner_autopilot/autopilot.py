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

"""Deterministic callback for the leadership_runner_autopilot agent.

Direct analogue of ``agents/runner_autopilot/autopilot.py`` but calls
``process_leadership_tick`` (leadership physics: momentum, political capital,
resistance cliff) instead of ``process_tick`` (marathon physics: velocity,
hydration, fatigue).

Implements a ``before_model_callback`` that always returns an ``LlmResponse``,
preventing any LLM model calls.  Zero cost per tick.

This module is pure logic — independently testable with mock state dicts.
"""

import logging
from collections.abc import Callable
from enum import Enum
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from agents.leadership_runner.initialization import initialize_leadership_stakeholder
from agents.utils.runner_protocol import RunnerEventType, parse_runner_event
from agents.utils.sim_defaults import DEFAULT_RACE_DISTANCE_MI

logger = logging.getLogger(__name__)

# State can be a plain dict (unit tests) or ADK State object (runtime).
StateLike = Any

# Deterministic inner thought templates by persona.
# Keys MUST match PERSONA_PROFILES in agents/leadership_runner/constants.py:
#   pragmatist, visionary, diplomat, disruptor
_INNER_THOUGHTS: dict[str, list[str]] = {
    "pragmatist": [
        # Early — cautious assessment
        "Budget concerns remain.",
        "Need clear ROI metrics.",
        "Timeline is reasonable.",
        "Watching the numbers.",
        # Mid — growing pressure
        "Where's the cost-benefit analysis?",
        "Resources are stretched thin.",
        "Show me the data before I commit.",
        "Pilot results would help here.",
        # Late — fatigue or resolution
        "We've spent too much already to stop.",
        "The numbers are starting to make sense.",
        "Sunk cost or real progress?",
        "Cautiously optimistic now.",
        # Friction variants
        "This feels rushed without proper vetting.",
        "Who approved this budget line?",
        "I need a revised forecast.",
        "Incremental wins matter more than vision.",
    ],
    "visionary": [
        # Early — inspiring momentum
        "Vision is compelling.",
        "People deserve better.",
        "Purpose drives progress.",
        "Transformation is essential.",
        # Mid — rallying through resistance
        "We can't let fear stop us now.",
        "The future is worth fighting for.",
        "Every great change met resistance.",
        "Stay focused on the bigger picture.",
        # Late — reflective urgency
        "Are we moving fast enough?",
        "The window of opportunity is closing.",
        "We've come too far to turn back.",
        "Legacy is built in moments like these.",
        # Friction variants
        "Why is leadership wavering?",
        "Bold moves require bold leadership.",
        "The status quo is the real risk.",
        "Inspire them — don't manage them.",
    ],
    "diplomat": [
        # Early — coalition building
        "Building consensus slowly.",
        "All voices must be heard.",
        "Finding common ground here.",
        "Patience keeps coalitions intact.",
        # Mid — navigating tensions
        "Both sides have valid concerns.",
        "A compromise is within reach.",
        "Trust takes time to build.",
        "Let's not alienate key allies.",
        # Late — holding the center
        "The coalition is fragile but holding.",
        "One wrong move could fracture everything.",
        "Steady hands needed right now.",
        "Almost there — keep the dialogue open.",
        # Friction variants
        "That decision bypassed consultation.",
        "We're losing stakeholder trust.",
        "Slow down — bring everyone along.",
        "Relationships matter more than speed.",
    ],
    "disruptor": [
        # Early — aggressive push
        "Move faster on this.",
        "Disruption creates opportunity.",
        "Old ways must go.",
        "Momentum is building.",
        # Mid — impatient escalation
        "Bureaucracy is killing our velocity.",
        "Break the bottleneck now.",
        "If they won't adapt, replace them.",
        "Half measures got us nowhere.",
        # Late — burnout or breakthrough
        "Running out of political capital here.",
        "The resistance is crumbling — push harder.",
        "Either we win big or flame out.",
        "No point in playing it safe now.",
        # Friction variants
        "This pace is unsustainable.",
        "We've burned too many bridges.",
        "Who's still blocking progress?",
        "Radical change demands radical commitment.",
    ],
}

_DEFAULT_THOUGHTS = [
    "Observing the situation.",
    "Reserving judgment for now.",
    "Need more information.",
    "Watching leadership closely.",
    "Waiting for clarity.",
    "Something feels off here.",
    "Let's see how this unfolds.",
    "Keeping my options open.",
]


class Phase(str, Enum):
    """Which phase of the callback loop we're in."""

    DECIDE = "decide"
    SUMMARIZE = "summarize"


def detect_phase(llm_request: LlmRequest) -> Phase:
    """Determine whether we're deciding tool calls or summarizing results.

    After ADK executes tool calls, it appends function_response parts and
    calls before_model_callback again. We detect this by checking the last
    content entry for function_response parts.
    """
    if not llm_request.contents:
        return Phase.DECIDE

    last = llm_request.contents[-1]
    if last.parts:
        for part in last.parts:
            if part.function_response is not None:
                return Phase.SUMMARIZE

    return Phase.DECIDE


def extract_last_user_text(llm_request: LlmRequest) -> str:
    """Extract the text from the last user message in the request.

    Walks backwards through contents to find the last content with role='user'
    that has text parts, skipping model responses and function_response entries.
    """
    for content in reversed(llm_request.contents):
        if content.role != "user":
            continue
        if content.parts:
            for part in content.parts:
                if part.text is not None:
                    return part.text
    return ""


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _function_call_response(name: str, args: dict) -> LlmResponse:
    """Build an LlmResponse containing a single FunctionCall."""
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
        ),
    )


def _text_response(text: str) -> LlmResponse:
    """Build an LlmResponse containing a text part."""
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=text)],
        ),
    )


# ---------------------------------------------------------------------------
# Event handlers — pure functions: (state, data) -> LlmResponse
# ---------------------------------------------------------------------------


def _pick_inner_thought(state: StateLike, tick: int) -> str:
    """Pick a deterministic inner thought based on persona and tick."""
    persona = state.get("persona", "pragmatist")
    thoughts = _INNER_THOUGHTS.get(persona, _DEFAULT_THOUGHTS)
    return thoughts[tick % len(thoughts)]


def handle_start_gun(state: StateLike, data: dict) -> LlmResponse:
    """Simulation started. Acknowledge the start gun.

    Stakeholder initialization is deferred to tick 0 (via handle_tick) so
    that the first tick reports initial momentum at distance=0 without advancing.
    """
    return _text_response("Start gun heard. Awaiting first tick.")


def handle_crowd_boost(state: StateLike, data: dict) -> LlmResponse:
    """Coalition zone boost. Accelerate with provided or default intensity."""
    intensity = data.get("intensity", 0.5)
    return _function_call_response("accelerate", {"intensity": float(intensity)})


def handle_distance_update(state: StateLike, data: dict) -> LlmResponse:
    """No-op for autopilot — process_leadership_tick handles all capital depletion.

    The frontend sends distance_update events on alignment boundary crossings,
    but process_leadership_tick already depletes political capital with its own
    calibrated physics and handles auto-refill at checkpoints.
    """
    return _text_response("Distance update acknowledged (depletion handled by process_leadership_tick).")


def handle_tick(state: StateLike, data: dict) -> LlmResponse:
    """Per-tick update. Initializes stakeholder on first tick, then process_leadership_tick.

    On tick 0 (minutes_per_tick=0), initializes the stakeholder profile from a
    seeded log-normal distribution and reports initial momentum at distance=0.
    On subsequent ticks, advances alignment progress normally.

    Finished and deadlocked stakeholders still call process_leadership_tick to
    emit a tool_end event with their preserved state.  The process_leadership_tick
    function handles these cases as no-ops (progress_this_tick=0, effective_momentum=0)
    while keeping momentum/distance/capital in the telemetry payload.
    Without this, advance_tick aggregation cannot see the stakeholder and
    runners_reporting drops to zero once all stakeholders align.
    """
    # Initialize on first tick if not yet done.
    if state.get("velocity") is None:
        session_id = data.get("_session_id", "default")
        runner_count = data.get("runner_count")
        if runner_count is None:
            raise ValueError("CRITICAL: runner_count is missing from autopilot state data. Cannot safely spawn.")
        # NOTE: persona is NOT extracted from tick broadcast data.
        # Each runner self-assigns via seeded random in
        # initialize_leadership_stakeholder(), ensuring a diverse persona mix.
        initialize_leadership_stakeholder(state, session_id, runner_count=runner_count)

    tick = data.get("tick", 0)
    inner_thought = _pick_inner_thought(state, tick)

    args: dict = {
        "minutes_per_tick": data.get("minutes_per_tick", 15.0),
        "elapsed_minutes": data.get("elapsed_minutes", 0.0),
        "race_distance_mi": data.get("race_distance_mi", DEFAULT_RACE_DISTANCE_MI),
        "tick": tick,
        "inner_thought": inner_thought,
        "route_friction_map": data.get("route_friction_map", {}),
        "gatekeeper_route_map": data.get("gatekeeper_route_map", {}),
    }
    collector_key = data.get("collector_buffer_key", "")
    if collector_key:
        args["collector_buffer_key"] = collector_key
    return _function_call_response("process_leadership_tick", args)


def build_summary(state: StateLike) -> LlmResponse:
    """Build a one-sentence summary after tool execution."""
    momentum = state.get("velocity", 0.0)
    capital = state.get("water", 100)
    progress = state.get("distance", 0.0)
    stalled = state.get("exhausted", False)
    aligned = state.get("finished", False)
    deadlocked = state.get("collapsed", False)
    if aligned:
        status = "aligned"
    elif deadlocked:
        status = "deadlocked"
    elif stalled:
        status = "stalled"
    else:
        status = "advancing"
    return _text_response(f"Status: {status}, momentum={momentum:.1f}, capital={capital:.0f}%, progress={progress:.1f}")


# ---------------------------------------------------------------------------
# Handler dispatch table
# ---------------------------------------------------------------------------

HANDLERS: dict[RunnerEventType, Callable[[StateLike, dict], LlmResponse]] = {
    RunnerEventType.START_GUN: handle_start_gun,
    RunnerEventType.CROWD_BOOST: handle_crowd_boost,
    RunnerEventType.DISTANCE_UPDATE: handle_distance_update,
    RunnerEventType.TICK: handle_tick,
}

# Also handle HYDRATION_STATION as a no-op (political capital refill is
# handled internally by process_leadership_tick's checkpoint logic)
if hasattr(RunnerEventType, "HYDRATION_STATION"):
    HANDLERS[RunnerEventType.HYDRATION_STATION] = lambda state, data: _text_response(
        "Capital checkpoint handled by process_leadership_tick."
    )


# ---------------------------------------------------------------------------
# The callback — wired to LlmAgent.before_model_callback
# ---------------------------------------------------------------------------


def autopilot_leadership_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse:
    """Deterministic before_model_callback for leadership_runner_autopilot.

    Called by ADK's BaseLlmFlow in a loop:
    1. First call: parse user message, return tool calls or text
    2. After tools execute: called again with function_response in contents,
       return summary text

    Returns LlmResponse always — the model is never invoked.
    """
    phase = detect_phase(llm_request)
    state = callback_context.state

    if phase == Phase.SUMMARIZE:
        return build_summary(state)

    text = extract_last_user_text(llm_request)
    event = parse_runner_event(text)

    handler = HANDLERS.get(event.event)
    if handler is None:
        logger.debug("Unknown event type %s, returning no-op text", event.event)
        return _text_response(f"Received unknown event: {text[:100]}")

    # Inject session_id for handlers that need it (START_GUN for ack,
    # TICK for initialize_leadership_stakeholder on tick 0).
    if event.event in (RunnerEventType.START_GUN, RunnerEventType.TICK):
        event.data["_session_id"] = getattr(getattr(callback_context, "session", None), "id", "default")

    return handler(state, event.data)
