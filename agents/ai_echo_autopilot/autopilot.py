
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
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from agents.ai_echo.initialization import initialize_echo
from agents.utils.redis_pool import get_shared_redis_client

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
    """Detect if we're in the dispatch or summarize phase."""
    contents = getattr(llm_request, "contents", None) or []
    for content in contents:
        parts = getattr(content, "parts", None) or []
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


def _pick_inner_thought(state: StateLike, tick: int) -> str:
    """Pick a deterministic inner thought based on stance and tick."""
    if state.get("defected"):
        return _DEFECTED_THOUGHT
    if state.get("aligned"):
        return _ALIGNED_THOUGHT

    stance = state.get("stance", "neutral")
    thoughts = _INNER_THOUGHTS.get(stance, _INNER_THOUGHTS["neutral"])
    # Deterministic selection based on tick
    return thoughts[tick % len(thoughts)]


def autopilot_echo_callback(
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
        event = json.loads(text) if text else {}
    except json.JSONDecodeError:
        event = {}

    event_type = event.get("event", "")

    if event_type == "simulation_start":
        return _text_response("Ready.")

    if event_type not in ("decision_round", "tick"):
        return _text_response("ack")

    # --- Initialize Echo on first tick ---
    if state.get("sentiment") is None:
        session = getattr(callback_context, "session", None)
        sid = getattr(session, "id", "default") if session else "default"
        echo_config = state.get("_echo_config", {})
        if not echo_config:
            try:
                r = get_shared_redis_client()
                val = r.get(f"echo_config:{sid}") if r else None
                if val:
                    echo_config = json.loads(val)
                    state["_echo_config"] = echo_config
            except Exception as e:
                logger.warning("Failed to fetch echo config from redis: %s", e)
        initialize_echo(state, sid, echo_config)

    tick = event.get("tick", 0)
    inner_thought = _pick_inner_thought(state, tick)

    # --- Compute wall_severity deterministically from stance/sentiment ---
    # Mirrors what the Tier 1 LLM would decide:
    #   hostile/opposed → high friction (7–9), supportive/allied → low (1–3),
    #   neutral → 5.0.  Sentiment further modulates within the band.
    stance = state.get("stance", "neutral")
    sentiment = state.get("sentiment", 0.0)
    _WALL_BASE: dict[str, float] = {
        "opposed": 8.0,
        "hostile": 9.0,
        "skeptical": 6.5,
        "neutral": 5.0,
        "supportive": 3.0,
        "allied": 1.5,
        "champion": 0.5,
    }
    wall_severity = _WALL_BASE.get(stance, 5.0)
    # Sentiment in [-1, 1]: negative pushes severity up, positive pulls down.
    wall_severity = max(0.0, min(10.0, wall_severity - sentiment * 1.5))

    # Return a tool call to process_decision_round
    return _tool_call_response(
        name="process_decision_round_tool",
        args={
            "tick": tick,
            "decision_text": event.get("decision_text", ""),
            "decision_tags": json.dumps(event.get("decision_tags", {})),
            "resource_state": json.dumps(event.get("resource_state", {})),
            "contradiction_alert": json.dumps(event.get("contradiction_alert")),
            "inner_thought": inner_thought,
            "wall_severity": wall_severity,
            "collector_buffer_key": event.get("collector_buffer_key", ""),
        },
    )
