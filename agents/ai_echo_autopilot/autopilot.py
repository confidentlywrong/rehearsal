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

"""Deterministic callback for AI Echo Autopilot (Tier 3) — zero LLM cost.

Direct analogue of ``agents/runner_autopilot/autopilot.py``.  Implements
a ``before_model_callback`` that always returns an ``LlmResponse``,
preventing any LLM calls.  The callback generates deterministic
inner_thoughts based on the Echo's state transitions.

This module is pure logic — independently testable with mock state dicts.
"""

from enum import Enum
import json
import logging
import math
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from agents.ai_echo.initialization import (
    compute_wall_severity,
    fetch_echo_config,
    initialize_echo,
)

logger = logging.getLogger(__name__)

StateLike = Any

# Deterministic inner thought templates by stance
_INNER_THOUGHTS: dict[str, list[str]] = {
    "hostile": [
        "This leadership is incompetent.",
        "They're not listening to anyone.",
        "I've had enough of this.",
        "Time to escalate externally.",
    ],
    "resistant": [
        "They need to hear our concerns.",
        "This direction worries me deeply.",
        "Where is the stakeholder input?",
        "Not convinced this will work.",
    ],
    "neutral": [
        "Let's see how this plays out.",
        "Reserving judgment for now.",
        "Need more data before committing.",
        "Watching the leadership closely.",
    ],
    "supportive": [
        "This is moving in the right direction.",
        "Good decision, well communicated.",
        "Leadership is showing they care.",
        "Feeling more confident about this.",
    ],
}

# Deterministic thoughts for terminal states
_DEFECTED_THOUGHT = "I no longer support this organization."
_ALIGNED_THOUGHT = "Fully committed to this leadership vision."


class Phase(str, Enum):
    """Which phase of the callback loop we're in."""

    DISPATCH = "dispatch"
    SUMMARIZE = "summarize"


def detect_phase(llm_request: LlmRequest) -> Phase:
    """Detect if we're in the dispatch or summarize phase by checking the last content entry."""
    contents = getattr(llm_request, "contents", None) or []
    if not contents:
        return Phase.DISPATCH
    last = contents[-1]
    parts = getattr(last, "parts", None) or []
    for part in parts:
        if hasattr(part, "function_response") and part.function_response is not None:
            return Phase.SUMMARIZE
    return Phase.DISPATCH


def extract_last_user_text(llm_request: LlmRequest) -> str:
    """Extract the last user message text from the LLM request."""
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(contents):
        role = getattr(content, "role", "")
        if role != "user":
            continue
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                return text
    return ""


def _text_response(text: str) -> LlmResponse:
    """Build a simple text LlmResponse."""
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=text)],
        ),
    )


def _tool_call_response(
    name: str,
    args: dict,
) -> LlmResponse:
    """Build an LlmResponse with a single tool call."""
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name=name,
                        args=args,
                    )
                )
            ],
        ),
    )


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


def _strip_nonfinite(obj: object) -> object:
    """Recursively replace non-finite floats (NaN/Inf) with None for RFC 8259 JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_nonfinite(v) for v in obj]
    return obj


def _strict_json_dumps(obj: object) -> str:
    """Serialize *obj* to strict RFC-8259 JSON, replacing non-finite floats with null."""
    try:
        return json.dumps(obj, allow_nan=False)
    except ValueError:
        return json.dumps(_strip_nonfinite(obj), allow_nan=False)


def _pick_inner_thought(state: StateLike, tick: int) -> str:
    """Pick a deterministic inner thought based on stance and tick."""
    if state.get("defected"):
        return _DEFECTED_THOUGHT
    if state.get("aligned"):
        return _ALIGNED_THOUGHT

    raw_stance = state.get("stance", "neutral")
    stance = raw_stance if isinstance(raw_stance, str) else "neutral"
    thoughts = _INNER_THOUGHTS.get(stance, _INNER_THOUGHTS["neutral"])
    # Deterministic selection based on tick
    return thoughts[_finite_int(tick, 0) % len(thoughts)]


async def autopilot_echo_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse:
    """Deterministic before_model_callback for Tier 3 Echoes.

    Always returns an LlmResponse — the LLM model is never invoked.
    Zero cost per tick.
    """
    phase = detect_phase(llm_request)
    state = callback_context.state

    if phase == Phase.SUMMARIZE:
        role = state.get("role", "echo")
        stance = state.get("stance", "neutral")
        return _text_response(f"{role} [{stance}]: processed decision round")

    text = extract_last_user_text(llm_request)

    # Parse the event
    try:
        parsed = json.loads(text) if text else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    event = parsed if isinstance(parsed, dict) else {}

    event_type = event.get("event", "")

    if event_type == "simulation_start":
        return _text_response("Ready.")

    if event_type not in ("decision_round", "tick"):
        return _text_response("ack")

    # --- Initialize Echo on first tick ---
    if state.get("sentiment") is None:
        session = getattr(callback_context, "session", None)
        sid = (getattr(session, "id", None) or "unknown") if session else "unknown"
        raw_cfg = state.get("_echo_config")
        echo_config = raw_cfg if isinstance(raw_cfg, dict) else {}
        if not echo_config:
            from agents.utils.communication import resolve_tool_context_user_id

            cb_uid = resolve_tool_context_user_id(callback_context, require_when_saas=False)
            echo_config = (
                await fetch_echo_config(sid, user_id=cb_uid)
                if cb_uid is not None
                else await fetch_echo_config(sid)
            )
            if echo_config:
                state["_echo_config"] = echo_config
        initialize_echo(state, sid, echo_config)

    tick = _finite_int(event.get("tick"), 0)
    inner_thought = _pick_inner_thought(state, tick)

    # --- Compute wall_severity deterministically from stance/sentiment ---
    wall_severity = compute_wall_severity(
        state.get("stance", "neutral"),
        state.get("sentiment", 0.0),
    )

    raw_tags = event.get("decision_tags")
    raw_res = event.get("resource_state")
    raw_deltas = event.get("influence_deltas")
    raw_cbk = event.get("collector_buffer_key")
    raw_dtext = event.get("decision_text")

    # Return a tool call to process_decision_round
    return _tool_call_response(
        name="process_decision_round_tool",
        args={
            "tick": tick,
            "decision_text": raw_dtext if isinstance(raw_dtext, str) else "",
            "decision_tags": _strict_json_dumps(raw_tags if isinstance(raw_tags, dict) else {}),
            "resource_state": _strict_json_dumps(raw_res if isinstance(raw_res, dict) else {}),
            "contradiction_alert": _strict_json_dumps(event.get("contradiction_alert")),
            "inner_thought": inner_thought,
            "wall_severity": wall_severity,
            "collector_buffer_key": raw_cbk if isinstance(raw_cbk, str) else "",
            "influence_deltas": _strict_json_dumps(raw_deltas if isinstance(raw_deltas, dict) else {}),
        },
    )
