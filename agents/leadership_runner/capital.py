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

"""Political capital management tools for leadership stakeholders.

Mirrors agents/runner/hydration.py but operates on political capital
instead of hydration.  Uses the same state keys (water, exhausted,
collapsed) for frontend pipeline compatibility.

Thresholds are persona-aware: the active persona's ``stall_threshold``
and ``deadlock_threshold`` are read from the persona profile so that
Disruptors, Diplomats, etc. behave consistently with
``process_leadership_tick``.

.. note:: **L10 — Tool Exposure Gap**

   These tools are exposed to the LLM-powered leadership runner, which
   can call ``deplete_capital`` / ``restore_capital`` with arbitrary
   ``amount`` values.  This bypasses the calibrated physics in
   ``process_leadership_tick`` (which derives depletion from velocity,
   wall severity, and friction coefficients).  The gap is *intentional*:
   it gives the LLM creative latitude for narrative-driven capital
   management (e.g. "I'm spending 20 capital to push through this
   stakeholder's resistance").  The autopilot runner never calls these
   tools — all its capital management flows through
   ``process_leadership_tick``.
"""

import logging

from google.adk.tools.tool_context import ToolContext

from agents.leadership_runner.constants import (
    MAX_TOOL_CAPITAL_CHANGE,
    TOOL_RESTORE_WINDOW_MAX,
    TOOL_RESTORE_WINDOW_TICKS,
    get_persona_profile,
)

logger = logging.getLogger(__name__)


async def deplete_capital(amount: float, tool_context: ToolContext) -> dict:
    """Deplete the stakeholder's political capital by the given amount.

    Use this to reduce capital based on influence actions, negotiations,
    or resistance encounters.

    Args:
        amount: Amount of political capital to remove (percentage points).
        tool_context: ADK tool context for state management.
    """
    if tool_context.state.get("finished", False):
        return {
            "status": "already_finished",
            "message": "Stakeholder has already aligned. No capital changes allowed.",
            "water": tool_context.state.get("water", 100.0),
        }
    # Resolve persona-specific thresholds (matches process_leadership_tick)
    persona = tool_context.state.get("persona", "pragmatist")
    profile = get_persona_profile(persona)
    stall_threshold = profile["stall_threshold"]
    deadlock_threshold = profile["deadlock_threshold"]

    amount = min(max(0.0, amount), MAX_TOOL_CAPITAL_CHANGE)  # Reject negative inputs

    current_capital = tool_context.state.get("water", 100.0)
    new_capital = max(0.0, current_capital - amount)
    tool_context.state["water"] = new_capital

    # Track stall and deadlock using persona-specific thresholds
    stalled = tool_context.state.get("exhausted", False)
    if new_capital < stall_threshold:
        stalled = True
    elif new_capital >= stall_threshold:
        stalled = False
    tool_context.state["exhausted"] = stalled

    deadlocked = tool_context.state.get("collapsed", False)
    if stalled and new_capital < deadlock_threshold:
        deadlocked = True
    tool_context.state["collapsed"] = deadlocked

    status = "collapsed" if deadlocked else ("exhausted" if stalled else "success")
    message = f"Political capital depleted by {amount:.1f} to {new_capital:.1f}%."
    if deadlocked:
        message = f"DEADLOCKED! Political capital critically low at {new_capital:.1f}%."
    elif stalled:
        message = f"STALLED! Political capital dangerously low at {new_capital:.1f}%. Seek coalition support!"

    logger.info("Stakeholder capital updated: %s", message)
    return {
        "status": status,
        "message": message,
        "water": new_capital,
        "exhausted": stalled,
        "collapsed": deadlocked,
    }


async def restore_capital(amount: float, tool_context: ToolContext) -> dict:
    """Restore the stakeholder's political capital at an alignment checkpoint.

    Use this when reaching a coalition zone or alignment checkpoint.

    Args:
        amount: Amount of political capital to add (percentage points).
        tool_context: ADK tool context for state management.
    """
    if tool_context.state.get("finished", False):
        return {
            "status": "already_finished",
            "message": "Stakeholder has already aligned. No capital changes allowed.",
            "water": tool_context.state.get("water", 100.0),
        }
    # Resolve persona-specific thresholds (matches process_leadership_tick)
    persona = tool_context.state.get("persona", "pragmatist")
    profile = get_persona_profile(persona)
    stall_threshold = profile["stall_threshold"]

    amount = min(max(0.0, amount), MAX_TOOL_CAPITAL_CHANGE)  # Reject negative inputs

    # Cooldown: limit total restores within a sliding tick window.
    tick = tool_context.state.get("_tick_params", {}).get("tick", 0)
    recent_restores: list = tool_context.state.get("_capital_restored_window", [])
    # Prune entries outside the window
    recent_restores = [r for r in recent_restores if tick - r[0] <= TOOL_RESTORE_WINDOW_TICKS]
    total_recent = sum(r[1] for r in recent_restores)
    if total_recent + amount > TOOL_RESTORE_WINDOW_MAX:
        amount = max(0.0, TOOL_RESTORE_WINDOW_MAX - total_recent)
    if amount <= 0:
        return {
            "status": "cooldown",
            "message": f"Capital restore on cooldown. {total_recent:.1f}% restored in last {TOOL_RESTORE_WINDOW_TICKS} ticks (max {TOOL_RESTORE_WINDOW_MAX:.0f}%).",
            "water": tool_context.state.get("water", 100.0),
        }
    recent_restores.append([tick, amount])
    tool_context.state["_capital_restored_window"] = recent_restores

    current_capital = tool_context.state.get("water", 100.0)
    new_capital = min(100.0, current_capital + amount)
    tool_context.state["water"] = new_capital

    # Reset stall state if capital is restored above persona-specific threshold
    if new_capital >= stall_threshold:
        tool_context.state["exhausted"] = False
        # H8 fix: Only clear deadlock if it was capital-based. If burnout_risk
        # is still above 0.8, the stakeholder deadlocked from burnout and
        # restoring capital alone shouldn't resurrect them.
        burnout_risk = tool_context.state.get("injury_risk", 0.0)
        if burnout_risk < 0.8:
            tool_context.state["collapsed"] = False

    logger.info("Stakeholder capital restored: %.1f%%", new_capital)
    return {
        "status": "success",
        "message": f"Capital restored from {current_capital:.1f}% to {new_capital:.1f}%.",
        "water": new_capital,
    }

async def executive_intervention(tool_context: ToolContext) -> dict:
    """Emergency one-time recovery from deadlock.

    Use this as a last resort when a stakeholder has deadlocked and all
    other options are exhausted. Can only be used once per simulation.
    Restores the stakeholder to just above stall threshold.

    Args:
        tool_context: ADK tool context for state management.
    """
    if tool_context.state.get("finished", False):
        return {
            "status": "already_finished",
            "message": "Stakeholder has already aligned. No capital changes allowed.",
            "water": tool_context.state.get("water", 100.0),
        }
    if not tool_context.state.get("collapsed", False):
        return {
            "status": "not_deadlocked",
            "message": "Stakeholder is not deadlocked. No intervention needed.",
        }
    if tool_context.state.get("_intervention_used", False):
        return {
            "status": "already_used",
            "message": "Executive intervention has already been used for this stakeholder.",
        }

    persona = tool_context.state.get("persona", "pragmatist")
    profile = get_persona_profile(persona)
    restore_to = profile["stall_threshold"] + 5.0

    tool_context.state["water"] = restore_to
    tool_context.state["collapsed"] = False
    tool_context.state["exhausted"] = False
    tool_context.state["_intervention_used"] = True
    # Reset burnout to prevent immediate re-deadlock from high injury_risk.
    tool_context.state["injury_risk"] = 0.4  # Residual risk, not fully healed
    tool_context.state["recovering"] = True
    tool_context.state["recovery_ticks_left"] = 3  # Recovery period after intervention

    logger.info("Executive intervention for [%s]: capital restored to %.1f%%", persona, restore_to)
    return {
        "status": "success",
        "message": (
            f"Executive intervention activated! Capital restored to {restore_to:.1f}%. "
            f"This is a one-time recovery — no further interventions available."
        ),
        "water": restore_to,
        "exhausted": False,
        "collapsed": False,
        "injury_risk": 0.4,
        "recovering": True,
    }
