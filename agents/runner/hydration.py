# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

import logging
import math
from google.adk.tools.tool_context import ToolContext

from agents.runner.constants import COLLAPSE_THRESHOLD, EXHAUSTION_THRESHOLD

logger = logging.getLogger(__name__)


def _finite_nonneg(value: object, default: float = 0.0) -> float:
    """Coerce numeric value to a non-negative finite float."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(num) or num < 0.0:
        return default
    return num


async def deplete_water(amount: float, tool_context: ToolContext) -> dict:
    """Deplete the runner's hydration by the given amount.

    Use this to reduce hydration based on distance run, fatigue, or other
    conditions. The agent decides how much to deplete based on the situation.

    Args:
        amount: Amount of hydration to remove (percentage points).
        tool_context: ADK tool context for state management.
    """
    if tool_context.state.get("finished", False):
        return {
            "status": "already_finished",
            "message": "Runner has already finished. No hydration changes allowed.",
            "water": min(100.0, _finite_nonneg(tool_context.state.get("water", 100.0), 100.0)),
        }
    safe_amount = _finite_nonneg(amount, 0.0)
    current_water = min(100.0, _finite_nonneg(tool_context.state.get("water", 100.0), 100.0))
    new_water = max(0.0, current_water - safe_amount)
    tool_context.state["water"] = new_water

    # Track exhaustion and collapse
    exhausted = bool(tool_context.state.get("exhausted", False))
    if new_water < EXHAUSTION_THRESHOLD:
        exhausted = True
    elif new_water >= EXHAUSTION_THRESHOLD:
        exhausted = False
    tool_context.state["exhausted"] = exhausted

    collapsed = bool(tool_context.state.get("collapsed", False))
    if exhausted and new_water < COLLAPSE_THRESHOLD:
        collapsed = True
    tool_context.state["collapsed"] = collapsed

    status = "collapsed" if collapsed else ("exhausted" if exhausted else "success")
    message = f"Hydration depleted by {safe_amount:.1f} to {new_water:.1f}%."
    if collapsed:
        message = f"COLLAPSED! Hydration critically low at {new_water:.1f}%."
    elif exhausted:
        message = f"EXHAUSTED! Hydration dangerously low at {new_water:.1f}%. Find a hydration station!"

    logger.info(f"Runner hydration updated: {message}")
    return {
        "status": status,
        "message": message,
        "water": new_water,
        "exhausted": exhausted,
        "collapsed": collapsed,
    }


async def rehydrate(amount: float, tool_context: ToolContext) -> dict:
    """Rehydrate the runner at a hydration station.

    Use this when entering a hydration station. The agent decides how much
    to rehydrate based on the situation.

    Args:
        amount: Amount of hydration to add (percentage points).
        tool_context: ADK tool context for state management.
    """
    if tool_context.state.get("finished", False):
        return {
            "status": "already_finished",
            "message": "Runner has already finished. No hydration changes allowed.",
            "water": min(100.0, _finite_nonneg(tool_context.state.get("water", 100.0), 100.0)),
        }
    safe_amount = _finite_nonneg(amount, 0.0)
    current_water = min(100.0, _finite_nonneg(tool_context.state.get("water", 100.0), 100.0))
    new_water = min(100.0, current_water + safe_amount)
    tool_context.state["water"] = new_water

    # Reset exhaustion state if hydration is restored above threshold.
    # Only clear collapse if injury_risk < 0.8 (runners collapsed from high
    # injury_risk should not be resurrected by hydration alone).
    if new_water >= EXHAUSTION_THRESHOLD:
        tool_context.state["exhausted"] = False
        injury_risk = _finite_nonneg(tool_context.state.get("injury_risk", 0.0), 0.0)
        if injury_risk < 0.8:
            tool_context.state["collapsed"] = False

    logger.info(f"Runner rehydrated: {new_water:.1f}%")
    return {
        "status": "success",
        "message": f"Rehydrated from {current_water:.1f}% to {new_water:.1f}%.",
        "water": new_water,
    }
