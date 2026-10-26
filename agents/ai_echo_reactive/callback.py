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

"""Reactive callback for AI Echo (Tier 2) — selective LLM activation.

The key cost-control innovation.  This ``before_model_callback`` intercepts
all ticks and decides whether to use the LLM or respond deterministically.

Decision logic:
    - If the participant's decision touches this Echo's domain principles
      (applied OR neglected), fall through to LLM for nuanced response.
    - If a contradiction_alert targets this Echo, fall through to LLM.
    - Otherwise, respond deterministically (zero LLM cost).

This is a hybrid of ``runner/agent.py`` (LLM) and ``runner_autopilot``
(deterministic): most ticks are deterministic, but domain-relevant ticks
use the LLM.
"""

import json
import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from agents.ai_echo.initialization import initialize_echo
from agents.ai_echo_autopilot.autopilot import (
    Phase,
    _pick_inner_thought,
    _text_response,
    _tool_call_response,
    detect_phase,
    extract_last_user_text,
)
from agents.utils.redis_pool import get_shared_redis_client

logger = logging.getLogger(__name__)

StateLike = Any


def _deterministic_sentiment_update(
    state: StateLike,
    event: dict,
) -> LlmResponse:
    """Generate a deterministic response when decision is not domain-relevant.

    Runs process_decision_round with a deterministic inner_thought,
    returns the result as a tool call.
    """
    tick = event.get("tick", 0)
    inner_thought = _pick_inner_thought(state, tick)

    return _tool_call_response(
        name="process_decision_round_tool",
        args={
            "tick": tick,
            "decision_text": event.get("decision_text", ""),
            "decision_tags": json.dumps(event.get("decision_tags", {})),
            "resource_state": json.dumps(event.get("resource_state", {})),
            "contradiction_alert": json.dumps(event.get("contradiction_alert")),
            "inner_thought": inner_thought,
            "collector_buffer_key": event.get("collector_buffer_key", ""),
        },
    )


def reactive_echo_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse | None:
    """Selective before_model_callback for Tier 2 Echoes.

    Returns LlmResponse (deterministic) when the decision is not relevant
    to this Echo's domain.  Returns None (falls through to LLM) when the
    decision IS relevant, allowing a nuanced, persona-driven response.
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

    # --- THE KEY DECISION: LLM or deterministic? ---
    decision_tags = event.get("decision_tags", {})
    my_domain_principles = set(state.get("domain_principles", []))

    # Check if any of this Echo's domain principles were applied/neglected
    relevant = my_domain_principles & (
        set(decision_tags.get("principles_applied", [])) | set(decision_tags.get("principles_neglected", []))
    )

    has_contradiction = event.get("contradiction_alert") is not None

    if not relevant and not has_contradiction:
        # NOT relevant — deterministic response, zero LLM cost
        logger.debug(
            "Reactive Echo %s: decision not domain-relevant, using deterministic path",
            state.get("role", "?"),
        )
        return _deterministic_sentiment_update(state, event)

    # RELEVANT — fall through to LLM for nuanced response
    logger.info(
        "Reactive Echo %s: decision IS domain-relevant (principles=%s, contradiction=%s), activating LLM",
        state.get("role", "?"),
        relevant,
        has_contradiction,
    )

    # Inject current resources into state for ground truth
    state["_current_resources"] = event.get("resource_state", {})

    return None  # Falls through to LLM
