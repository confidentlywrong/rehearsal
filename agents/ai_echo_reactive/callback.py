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

from agents.ai_echo.initialization import (
    compute_wall_severity,
    fetch_echo_config,
    initialize_echo,
)
from agents.ai_echo_autopilot.autopilot import (
    Phase,
    _finite_int,
    _pick_inner_thought,
    _strict_json_dumps,
    _text_response,
    _tool_call_response,
    detect_phase,
    extract_last_user_text,
)

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
    tick = _finite_int(event.get("tick"), 0)
    inner_thought = _pick_inner_thought(state, tick)

    wall_severity = compute_wall_severity(
        state.get("stance", "neutral"),
        state.get("sentiment", 0.0),
    )

    raw_tags = event.get("decision_tags")
    raw_res = event.get("resource_state")
    raw_deltas = event.get("influence_deltas")
    raw_cbk = event.get("collector_buffer_key")
    raw_dtext = event.get("decision_text")

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


async def reactive_echo_callback(
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

    # --- THE KEY DECISION: LLM or deterministic? ---
    raw_tags = event.get("decision_tags")
    decision_tags = raw_tags if isinstance(raw_tags, dict) else {}
    raw_dps = state.get("domain_principles")
    my_domain_principles = (
        {dp for dp in raw_dps if isinstance(dp, str)}
        if isinstance(raw_dps, (list, tuple, set))
        else set()
    )
    raw_applied = decision_tags.get("principles_applied")
    applied_set = (
        {p for p in raw_applied if isinstance(p, str)}
        if isinstance(raw_applied, (list, tuple, set))
        else set()
    )
    raw_neglected = decision_tags.get("principles_neglected")
    neglected_set = (
        {p for p in raw_neglected if isinstance(p, str)}
        if isinstance(raw_neglected, (list, tuple, set))
        else set()
    )

    # Check if any of this Echo's domain principles were applied/neglected
    relevant = my_domain_principles & (applied_set | neglected_set)

    has_contradiction = event.get("contradiction_alert") is not None

    if state.get("defected") or state.get("aligned"):
        # Terminal Echoes return deterministically in process_decision_round; skip LLM wakeups.
        return _deterministic_sentiment_update(state, event)

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

    # Inject current resources and event metadata into state for LLM fallback
    raw_res = event.get("resource_state")
    raw_deltas = event.get("influence_deltas")
    raw_cbk = event.get("collector_buffer_key")
    state["_current_resources"] = raw_res if isinstance(raw_res, dict) else {}
    state["_last_influence_deltas"] = raw_deltas if isinstance(raw_deltas, dict) else {}
    state["_last_collector_buffer_key"] = raw_cbk if isinstance(raw_cbk, str) else ""

    return None  # Falls through to LLM
