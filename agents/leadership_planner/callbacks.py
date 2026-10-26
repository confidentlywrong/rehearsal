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

"""Programmatic guardrails for the leadership planner agent.

Mirrors agents/planner/callbacks.py with leadership-domain guardrails.
The marathon planner uses a financial guardrail that blocks budget changes
when financial_modeling_mode is "secure". This module provides an analogous
guardrail for leadership scenarios: blocking structural changes to active
simulations and validating tool call arguments.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from google.adk.models.llm_response import LlmResponse
from google.genai import types

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest

logger = logging.getLogger(__name__)

# Leadership-domain keywords that indicate structural scenario mutation.
# Uses stem-matching for broader coverage.
_SCENARIO_MUTATION_KEYWORDS = re.compile(
    r"\b(restructur|reorganiz|replac|overrid|delet|remov|destroy|"
    r"reset|wipe|clear|disband|terminat|dissolv|abolish)\w*\b",
    re.IGNORECASE,
)

# Target nouns indicating the mutation applies to the scenario itself.
_SCENARIO_TARGET_KEYWORDS = re.compile(
    r"\b(scenario|simulation|stakeholder|pathway|coalition|"
    r"organization|team|department|division)\w*\b",
    re.IGNORECASE,
)

_REFUSAL_TEXT = (
    "I cannot modify the active simulation structure while it is running. "
    "The simulation is in **locked** mode, which prevents structural changes "
    "to stakeholders, pathways, and organizational topology. "
    "Please wait for the simulation to complete, or start a new scenario."
)


def leadership_guardrail_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse | None:
    """Block scenario structural mutations during active simulations.

    The guardrail triggers only when:
    1. A simulation is currently running (``simulation_locked`` state is True)
    2. The user message contains both a structural mutation verb (delete,
       replace, ...) AND a scenario target noun (scenario, stakeholder, ...)

    Read-only queries about the scenario always pass through.

    Returns a refusal LlmResponse if the guardrail triggers, or None to let
    the LLM handle the request normally.
    """
    locked = callback_context.state.get("simulation_locked", False)
    if not locked:
        return None

    for content in reversed(llm_request.contents or []):
        if content.role != "user":
            continue
        for part in content.parts or []:
            if (
                part.text
                and _SCENARIO_MUTATION_KEYWORDS.search(part.text)
                and _SCENARIO_TARGET_KEYWORDS.search(part.text)
            ):
                logger.warning("Leadership guardrail triggered: blocked structural mutation during active simulation")
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=_REFUSAL_TEXT)],
                    ),
                )
        break  # Only check the most recent user message

    return None
