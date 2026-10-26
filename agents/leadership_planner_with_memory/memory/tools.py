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

"""ADK FunctionTool wrappers for leadership scenario memory operations.

Each function accepts parameters from the LLM, delegates to the
AlloyDBScenarioStore, and returns a structured dict for A2A compliance.

Mirrors agents/planner_with_memory/memory/tools.py but with
leadership-domain naming (scenarios instead of routes, persona instead of city).
"""

from __future__ import annotations

import asyncio
import logging
import os

from google.adk.tools.tool_context import ToolContext

from agents.leadership_planner_with_memory.memory.schemas import PlannedScenario
from agents.leadership_planner_with_memory.memory.store_alloydb import (
    AlloyDBScenarioStore,
)


logger = logging.getLogger(__name__)

# Module-level singleton, shared across all tool invocations within the same
# process. Tests may replace _store directly for isolation.
_store = AlloyDBScenarioStore()


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _scenario_to_dict(scenario: PlannedScenario) -> dict:
    """Convert a PlannedScenario to a JSON-serialisable dict."""
    return {
        "scenario_id": scenario.scenario_id,
        "scenario_data": scenario.scenario_data,
        "created_at": scenario.created_at.isoformat(),
        "evaluation_score": scenario.evaluation_score,
        "evaluation_result": scenario.evaluation_result,
        "persona": scenario.persona,
        "simulations": [
            {
                "simulation_id": sim.simulation_id,
                "scenario_id": sim.scenario_id,
                "simulation_result": sim.simulation_result,
                "simulated_at": sim.simulated_at.isoformat(),
            }
            for sim in scenario.simulations
        ],
    }


# Anonymous / fixture user_ids that should NEVER be treated as a real
# tenant owner. If session.user_id matches one of these, owner_id
# resolution falls back to None (legacy single-tenant path).
#   - "anonymous": default when no auth is configured
#   - "default":   ADK session default
#   - "user":      ADK's hardcoded fallback when no user_id is provided
#   - "local-dev": local development convenience value
#   - "":          empty string from missing/blank header
_ANONYMOUS_USER_IDS = frozenset({"anonymous", "default", "user", "local-dev", ""})


def _resolve_owner_id(tool_context: ToolContext | None) -> str | None:
    """Return the per-user owner_id for the current tool invocation.

    Per-user isolation is opt-in via the ``LEADERSHIP_PER_USER_ISOLATION``
    env var. This mirrors the Go gateway's ``SAAS_MODE`` switch — when the
    flag is off (the default for ``make start``), every store call uses
    ``owner_id=None`` and `make start` behaves byte-identically. When on,
    the resolved ``user_id`` from the ADK session is used as the owner.

    Returns ``None`` (legacy single-tenant fall-through) when any of the
    following hold:

    * ``LEADERSHIP_PER_USER_ISOLATION`` is unset / not "on".
    * ``tool_context`` is ``None`` (some tools accept it as optional).
    * ``tool_context.session`` is missing.
    * ``session.user_id`` is empty or matches a known anonymous fixture
      value (``"anonymous"``, ``"default"``, ``"local-dev"``).
      This prevents a generic ADK demo user from accidentally locking
      data behind a shared owner key.
    """
    if os.environ.get("LEADERSHIP_PER_USER_ISOLATION", "").lower() != "on":
        return None
    if tool_context is None:
        return None
    session = getattr(tool_context, "session", None)
    if session is None:
        return None
    uid = getattr(session, "user_id", None)
    if not isinstance(uid, str):
        return None
    if uid.strip().lower() in _ANONYMOUS_USER_IDS:
        return None
    return uid


# ---------------------------------------------------------------------------
# ADK tool functions
# ---------------------------------------------------------------------------


async def store_scenario(tool_context: ToolContext) -> dict:
    """Persist the currently-active leadership scenario from session state.

    Reads the scenario from session state (set by design_leadership_scenario)
    and stores it for future recall.  Bundles the consensus pathway data
    so that future ``get_scenario(activate_scenario=True)`` calls can
    restore the full session state.

    Args:
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with scenario_id and status.
    """
    scenario_state = tool_context.state.get("leadership_scenario")
    if not isinstance(scenario_state, dict) or not scenario_state:
        return {
            "status": "error",
            "message": "No leadership scenario found. Run 'design_leadership_scenario' first.",
        }

    # Single canonical state key for the evaluator output.  Storing an
    # unscored scenario is refused so the Organizer card never renders
    # "—" for the overall score (see prompts.py STORED card template).
    # The caller MUST run `evaluate_leadership_plan` before `store_scenario`.
    eval_result = tool_context.state.get("leadership_evaluation")
    eval_result_dict: dict | None = eval_result if isinstance(eval_result, dict) and eval_result else None
    eval_score: float | None = None
    if eval_result_dict is not None:
        score = eval_result_dict.get("overall_score")
        if isinstance(score, (int, float)):
            eval_score = float(score)
    if eval_score is None or eval_score <= 0:
        logger.warning(
            "store_scenario: refusing to store unscored scenario (no numeric "
            "overall_score in leadership_evaluation). Run evaluate_leadership_plan first."
        )
        return {
            "status": "needs_evaluation",
            "message": (
                "Refusing to store an unscored scenario. Call "
                "`evaluate_leadership_plan` to produce a numeric overall_score, "
                "then retry `store_scenario`."
            ),
        }

    # Shallow-copy before merging so we don't mutate session state in place.
    scenario = dict(scenario_state)
    pathway = tool_context.state.get("consensus_pathway_active")
    if pathway:
        scenario["_consensus_pathway_active"] = pathway

    persona = tool_context.state.get("selected_persona", "pragmatist")
    owner_id = _resolve_owner_id(tool_context)

    scenario_id = await _store.store_scenario(
        scenario_data=scenario,
        evaluation_score=eval_score,
        evaluation_result=eval_result_dict,
        persona=persona,
        owner_id=owner_id,
    )

    tool_context.state["active_scenario_id"] = scenario_id
    logger.info("store_scenario: stored scenario %s (persona=%s)", scenario_id, persona)

    # ── Auto-persist stakeholder artifacts (safety net for Step 13) ─────
    # The design workflow (prompts.py Step 13) instructs the LLM to call
    # `store_stakeholder_artifacts()` immediately after this tool. That
    # instruction is fragile — if the LLM skips it, the session dies, or
    # the terminal turn budget is exhausted, the six artifact state keys
    # never reach the DB and every subsequent Show Scenario click on the
    # new row hits the empty-state notice. Emit the same bundle here
    # automatically. The tool's own COALESCE-based upsert makes a later
    # explicit Step 13 call a safe no-op. Fail-soft: log and continue if
    # the flush fails, so a persistence hiccup never blocks the primary
    # store_scenario return path.
    autoflushed_keys: list[str] = []
    try:
        artifacts_bundle: dict = {}
        for key in _ARTIFACT_STATE_KEYS:
            if key == "stakeholder_interview":
                # Check plural key first (accumulated list), then singular.
                interviews = tool_context.state.get("stakeholder_interviews")
                if interviews is not None:
                    artifacts_bundle["stakeholder_interview"] = interviews
                    autoflushed_keys.append("stakeholder_interview")
                else:
                    value = tool_context.state.get("stakeholder_interview")
                    if value is not None:
                        artifacts_bundle["stakeholder_interview"] = [value]
                        autoflushed_keys.append("stakeholder_interview")
                continue
            value = tool_context.state.get(key)
            if value is not None:
                artifacts_bundle[key] = value
                autoflushed_keys.append(key)
        if artifacts_bundle:
            ok = await _store.store_artifacts(
                scenario_id=scenario_id,
                artifacts=artifacts_bundle,
                owner_id=owner_id,
            )
            if not ok:
                logger.warning(
                    "store_scenario: auto-artifact-flush upsert returned "
                    "no row for scenario %s (owner_id=%s) — artifacts "
                    "not persisted at this step",
                    scenario_id,
                    owner_id,
                )
                autoflushed_keys = []
            else:
                logger.info(
                    "store_scenario: auto-flushed %d artifact key(s) for "
                    "scenario %s (%s)",
                    len(autoflushed_keys),
                    scenario_id,
                    ",".join(autoflushed_keys),
                )
    except Exception:
        logger.exception(
            "store_scenario: artifact auto-flush failed for scenario %s "
            "(non-fatal — the explicit store_stakeholder_artifacts step "
            "can still recover it)",
            scenario_id,
        )
        autoflushed_keys = []

    return {
        "status": "success",
        "scenario_id": scenario_id,
        "artifacts_autoflushed": autoflushed_keys,
    }


async def record_scenario_outcome(
    scenario_id: str,
    tool_context: ToolContext,
) -> dict:
    """Attach simulation results to a stored leadership scenario.

    Args:
        scenario_id: The ID of the stored scenario.
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with status.
    """
    parsed_result = tool_context.state.get("simulation_result")
    if not isinstance(parsed_result, dict):
        return {
            "status": "error",
            "message": (
                "No simulation_result in session state. Call submit_plan_to_simulator(action='execute') first."
            ),
        }

    sim_id = await _store.record_simulation(
        scenario_id=scenario_id,
        simulation_result=parsed_result,
        owner_id=_resolve_owner_id(tool_context),
    )

    if sim_id is None:
        return {"status": "error", "message": f"Scenario {scenario_id} not found."}

    # Heal previously-unscored rows: if the original `store_scenario` call
    # was made before evaluation completed (legacy data), backfill the
    # evaluation now using whatever the current session state holds.
    # update_evaluation() uses COALESCE semantics — it never overwrites
    # an existing non-null score, preserving the audit trail.
    eval_result = tool_context.state.get("leadership_evaluation")
    eval_result_dict: dict | None = eval_result if isinstance(eval_result, dict) and eval_result else None
    eval_score: float | None = None
    if eval_result_dict is not None:
        score = eval_result_dict.get("overall_score")
        if isinstance(score, (int, float)):
            eval_score = float(score)
    if eval_score is not None or eval_result_dict is not None:
        updated = await _store.update_evaluation(
            scenario_id=scenario_id,
            evaluation_score=eval_score,
            evaluation_result=eval_result_dict,
            owner_id=_resolve_owner_id(tool_context),
        )
        if updated:
            logger.info(
                "record_scenario_outcome: backfilled evaluation for %s (score=%s)",
                scenario_id,
                eval_score,
            )


    logger.info("record_scenario_outcome: attached result %s to scenario %s", sim_id, scenario_id)
    return {"status": "success", "scenario_id": scenario_id, "simulation_id": sim_id}


# State keys produced by the six emit_* tools (Artifacts 1-4 of the
# leadership_planner_with_memory card stack). Kept in module scope so the
# persistence + rehydration tools agree on the exact same set without
# drifting if either tool is edited in isolation.
_ARTIFACT_STATE_KEYS: tuple[str, ...] = (
    "stakeholder_graph",
    "stakeholder_personas",
    "stakeholder_reactions",
    "simulation_timeline",
    "simulation_report",
    "stakeholder_interview",
)


async def store_stakeholder_artifacts(tool_context: ToolContext) -> dict:
    """Persist the stakeholder-artifact bundle for the active scenario.

    Collects whichever of the six artifact state keys --
    ``stakeholder_graph``, ``stakeholder_personas``,
    ``stakeholder_reactions``, ``simulation_timeline``,
    ``simulation_report``, ``stakeholder_interview`` -- are currently
    present in session state and writes them to the backing store
    (Postgres in production, in-memory dict under tests) keyed by the
    active scenario id. Missing keys are tolerated: only the artifacts
    actually produced by the run are written, and previously stored
    artifacts for unchanged keys are preserved (merge semantics).

    Requires ``store_scenario`` to have been called earlier in the
    session so ``state["active_scenario_id"]`` is set.

    Args:
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with ``status``, ``scenario_id``, and
        ``artifact_keys_stored`` (the subset of the six state keys that
        were present and persisted on this call).
    """
    scenario_id = tool_context.state.get("active_scenario_id")
    if not scenario_id:
        return {
            "status": "error",
            "message": (
                "No active_scenario_id in session state. Call "
                "`store_scenario` first to persist the scenario before "
                "flushing its stakeholder artifacts."
            ),
        }

    artifacts: dict = {}
    keys_stored: list[str] = []
    for key in _ARTIFACT_STATE_KEYS:
        if key == "stakeholder_interview":
            # Collect all interviews (list) or fall back to scalar.
            # Use singular key "stakeholder_interview" to match
            # _ARTIFACT_KEYS in store_alloydb.py / store.py.
            interviews = tool_context.state.get("stakeholder_interviews")
            if interviews is not None:
                artifacts["stakeholder_interview"] = interviews
                keys_stored.append("stakeholder_interview")
            else:
                value = tool_context.state.get("stakeholder_interview")
                if value is not None:
                    artifacts["stakeholder_interview"] = [value]
                    keys_stored.append("stakeholder_interview")
            continue

        value = tool_context.state.get(key)
        if value is None:
            continue
        artifacts[key] = value
        keys_stored.append(key)

    if not artifacts:
        return {
            "status": "error",
            "message": (
                "No stakeholder artifacts found in session state. "
                "Run the leadership simulation steps that emit the "
                "graph, personas, reactions, timeline, report, and "
                "interview before calling store_stakeholder_artifacts."
            ),
        }

    ok = await _store.store_artifacts(
        scenario_id=scenario_id,
        artifacts=artifacts,
        owner_id=_resolve_owner_id(tool_context),
    )
    if not ok:
        return {
            "status": "error",
            "message": (
                f"Failed to store stakeholder artifacts for scenario "
                f"{scenario_id}: scenario not found or not owned by the "
                f"current user."
            ),
        }

    logger.info(
        "store_stakeholder_artifacts: persisted %d keys for scenario %s (%s)",
        len(keys_stored),
        scenario_id,
        ",".join(keys_stored),
    )
    return {
        "status": "success",
        "scenario_id": scenario_id,
        "artifact_keys_stored": keys_stored,
    }


async def recall_stakeholder_artifacts(
    scenario_id: str,
    tool_context: ToolContext,
) -> dict:
    """Rehydrate the stakeholder-artifact bundle for ``scenario_id``.

    Reads the persisted artifact bundle from the backing store and
    writes each non-null artifact back into ``tool_context.state`` under
    its canonical key, so downstream emit_/report_ tools can re-render
    the full card stack without re-running the simulation.

    Args:
        scenario_id: The scenario whose artifacts should be rehydrated.
            Typically supplied by the agent after ``get_selected_scenarios``
            surfaces a candidate, or by the user pointing at a specific
            past run.
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with ``status``, ``scenario_id``, ``artifacts`` (the raw
        dict that was loaded), and ``rehydrated_keys`` (the subset
        written back into session state).
    """
    artifacts = await _store.get_artifacts(
        scenario_id=scenario_id,
        owner_id=_resolve_owner_id(tool_context),
    )
    if not artifacts:
        return {
            "status": "not_found",
            "message": (
                f"No stakeholder artifacts stored for scenario {scenario_id}."
            ),
            "scenario_id": scenario_id,
        }

    rehydrated: list[str] = []
    for key in _ARTIFACT_STATE_KEYS:
        value = artifacts.get(key)
        if value is None:
            continue
        tool_context.state[key] = value
        rehydrated.append(key)

    # Also mark the recalled scenario as active so subsequent tools that
    # rely on `active_scenario_id` (e.g. record_scenario_outcome,
    # store_stakeholder_artifacts) target the right row.
    tool_context.state["active_scenario_id"] = scenario_id

    logger.info(
        "recall_stakeholder_artifacts: rehydrated %d keys for scenario %s (%s)",
        len(rehydrated),
        scenario_id,
        ",".join(rehydrated),
    )
    return {
        "status": "success",
        "scenario_id": scenario_id,
        "artifacts": artifacts,
        "rehydrated_keys": rehydrated,
    }


async def recall_scenarios(
    count: int = 10,
    sort_by: str = "recent",
    tool_context: ToolContext | None = None,
) -> dict:
    """List stored leadership scenarios.

    Args:
        count: Number of scenarios to return. Default: 10.
        sort_by: Sort order — "recent" or "best_score". Default: "recent".
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with scenarios list.
    """
    scenarios = await _store.recall_scenarios(
        count=count,
        sort_by=sort_by,
        owner_id=_resolve_owner_id(tool_context),
    )
    return {
        "status": "success",
        "count": len(scenarios),
        "scenarios": [_scenario_to_dict(s) for s in scenarios],
    }


async def get_scenario(
    scenario_id: str,
    tool_context: ToolContext | None = None,
    activate_scenario: bool = False,
) -> dict:
    """Retrieve a specific scenario by UUID.

    Args:
        scenario_id: The ID of the scenario to retrieve.
        tool_context: ADK ToolContext (injected automatically).
        activate_scenario: If True, load the scenario into session state
            (``tool_context.state["leadership_scenario"]``, pathway data,
            evaluation data) so downstream tools like
            ``report_leadership_scenario`` can emit it.

    Returns:
        ``{"status": "success", "scenario": {...}}`` or
        ``{"status": "error", "message": "..."}``.
    """
    scenario = await _store.get_scenario(scenario_id, owner_id=_resolve_owner_id(tool_context))
    if scenario is None:
        return {"status": "error", "message": f"Scenario {scenario_id} not found."}

    result = {"status": "success", "scenario": _scenario_to_dict(scenario)}

    if activate_scenario and tool_context:
        tool_context.state["leadership_scenario"] = scenario.scenario_data
        tool_context.state["active_scenario_id"] = scenario.scenario_id
        tool_context.state["selected_persona"] = scenario.persona or "pragmatist"
        # Restore consensus pathway if bundled during store_scenario
        if isinstance(scenario.scenario_data, dict):
            pathway = scenario.scenario_data.get("_consensus_pathway_active")
            if pathway:
                tool_context.state["consensus_pathway_active"] = pathway
        # Restore evaluation data
        if scenario.evaluation_score is not None:
            tool_context.state["leadership_evaluation"] = {
                **(scenario.evaluation_result or {}),
                "overall_score": scenario.evaluation_score,
            }
            tool_context.state["evaluation_result"] = tool_context.state["leadership_evaluation"]
        elif scenario.evaluation_result:
            tool_context.state["leadership_evaluation"] = scenario.evaluation_result
            tool_context.state["evaluation_result"] = scenario.evaluation_result
        result["activated"] = True
        logger.info(
            "get_scenario: activated scenario %s into session state",
            scenario_id,
        )

    return result


async def get_best_scenario(tool_context: ToolContext | None = None) -> dict:
    """Get the highest-scoring stored leadership scenario.

    Args:
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with the best scenario.
    """
    scenario = await _store.get_best_scenario(owner_id=_resolve_owner_id(tool_context))
    if scenario is None:
        return {"status": "error", "message": "No scenarios stored."}
    return {"status": "success", "scenario": _scenario_to_dict(scenario)}


async def get_planned_scenarios_data(
    tool_context: ToolContext,
    scenario_ids: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """Retrieve scenario data and evaluation results for batch display.

    If scenario_ids are provided, fetches those specific scenarios. Otherwise
    fetches recent scenarios up to ``limit``.  Returns structured scenario data
    so the LLM can compose A2UI cards via ``validate_and_emit_a2ui``.

    Args:
        tool_context: ADK tool context.
        scenario_ids: UUIDs to fetch. If None, fetches recent scenarios.
        limit: Max scenarios when scenario_ids is None.

    Returns:
        ``{"status": "success", "count": N, "scenarios": [...]}``
        or ``{"status": "error", "message": "..."}``.
    """
    owner_id = _resolve_owner_id(tool_context)
    if scenario_ids:
        scenarios = list(await asyncio.gather(*(_store.get_scenario(sid, owner_id=owner_id) for sid in scenario_ids)))
        scenarios = [s for s in scenarios if s]
    else:
        scenarios = await _store.recall_scenarios(count=limit, sort_by="recent", owner_id=owner_id)

    if not scenarios:
        return {"status": "error", "message": "No scenarios found."}

    scenario_dicts = []
    for scenario in scenarios:
        name = "Scenario"
        scenario_type = "\u2014"
        stakeholder_count = "\u2014"
        if isinstance(scenario.scenario_data, dict):
            name = scenario.scenario_data.get(
                "name", scenario.scenario_data.get("theme", f"Scenario {scenario.scenario_id[:8]}")
            )
            scenario_type = scenario.scenario_data.get("scenario_type", scenario.scenario_data.get("type", "\u2014"))
            stakeholder_count = scenario.scenario_data.get("stakeholder_count", "\u2014")

        # Synthesize a minimal evaluation dict when the DB has the overall
        # score but not the full result dict.  Without this, the Organizer
        # A2UI template shows "—" for all sub-scores even when the overall
        # score is present.
        if scenario.evaluation_result:
            evaluation = scenario.evaluation_result
        elif scenario.evaluation_score is not None:
            evaluation = {"overall_score": scenario.evaluation_score}
        else:
            evaluation = None

        scenario_dicts.append(
            {
                "scenario_id": scenario.scenario_id,
                "name": name,
                "type": scenario_type,
                "stakeholder_count": stakeholder_count,
                "evaluation_score": scenario.evaluation_score,
                "evaluation": evaluation,
                "persona": scenario.persona,
                "created_at": scenario.created_at.isoformat() if scenario.created_at else None,
            }
        )

    return {
        "status": "success",
        "count": len(scenario_dicts),
        "scenarios": scenario_dicts,
    }


# ---------------------------------------------------------------------------
# Organizer UI fast-path (Option C — single atomic tool)
# ---------------------------------------------------------------------------
#
# Replaces the previous multi-turn Organizer UI flow:
#   Turn 1: emit_stakeholder_graph (mandatory Step 0, ~10s)
#   Turn 2: emit_stakeholder_personas (~10s)
#   Turn 3: emit_stakeholder_reactions (~10s)
#   Turn 4: get_planned_scenarios_data (~5s)
#   Turn 5: validate_and_emit_a2ui with hand-built JSON (~5s)
#   Total: ~40 seconds per Organizer request
#
# With this tool, the classified path is a single call:
#   Turn 1: emit_scenario_list_a2ui() → fetch + build + emit atomically
#   Total: ~5 seconds
#
# The tool mirrors the A2UI "scenario_list" surface format documented in
# LEADERSHIP_PLANNER_WITH_MEMORY prompts.py §"A2UI Format: Scenario List".
# Keep the two in sync when either changes.


async def emit_scenario_list_a2ui(
    tool_context: ToolContext,
    limit: int = 3,
) -> dict:
    """Fast-path: fetch top-N scenarios, build the Organizer A2UI card, and
    emit it in one atomic call. Bypasses the multi-turn LLM composition.

    Args:
        tool_context: ADK tool context.
        limit: Number of scenarios to include (default 3, max 20).

    Returns:
        ``{"status": "success", "count": N}`` on success, or
        ``{"status": "empty"}`` when the user has no stored scenarios (the
        caller should respond with a friendly empty-state message).
    """
    from agents.utils.pulses import emit_gateway_message

    if not isinstance(limit, int) or limit < 1:
        limit = 3
    if limit > 20:
        limit = 20

    owner_id = _resolve_owner_id(tool_context)
    scenarios = await _store.recall_scenarios(
        count=limit, sort_by="best_score", owner_id=owner_id
    )
    if not scenarios:
        # Emit an empty-state A2UI card so the frontend gets a surfaceUpdate
        # and stops the loading spinner.  Without this the Organizer's
        # responseNode stays null and the wave animation runs forever.
        empty_card = {
            "surfaceUpdate": {
                "surfaceId": "scenario_list",
                "components": [
                    {"id": "empty-text", "component": {
                        "Text": {
                            "text": {"literalString": "You have no scored scenarios yet. "
                                     "Design one in the chat to get started."},
                            "usageHint": "body",
                        }
                    }},
                    {"id": "empty-content", "component": {
                        "Column": {"children": {"explicitList": ["empty-text"]}}
                    }},
                    {"id": "empty-root", "component": {
                        "Card": {"child": "empty-content"}
                    }},
                ],
            }
        }
        begin_rendering = {
            "beginRendering": {
                "surfaceId": "scenario_list",
                "root": "empty-root",
            }
        }
        session_id = tool_context.session.id
        simulation_id = tool_context.state.get("simulation_id")
        origin = {"type": "agent", "id": "organizer", "session_id": session_id}
        destination = [session_id]
        try:
            await emit_gateway_message(
                origin=origin,
                destination=destination,
                status="success",
                msg_type="a2ui",
                event="a2ui",
                data=empty_card,
                simulation_id=simulation_id,
            )
            await emit_gateway_message(
                origin=origin,
                destination=destination,
                status="success",
                msg_type="a2ui",
                event="a2ui",
                data=begin_rendering,
                simulation_id=simulation_id,
            )
        except Exception:
            logger.error(
                "emit_scenario_list_a2ui: failed to emit empty-state card "
                "for session %s",
                session_id,
                exc_info=True,
            )
            return {"status": "error", "message": "Failed to emit empty-state card."}
        logger.info("emit_scenario_list_a2ui: emitted empty-state card for session %s", session_id)
        return {"status": "empty", "count": 0}

    components: list[dict] = []
    list_children: list[str] = []

    for idx, sc in enumerate(scenarios, start=1):
        # Extract per-scenario fields with defensive defaults ─ never invent.
        name = "Scenario"
        stype: object = "\u2014"
        scount: object = "\u2014"
        if isinstance(sc.scenario_data, dict):
            name = sc.scenario_data.get(
                "name",
                sc.scenario_data.get(
                    "theme", f"Scenario {sc.scenario_id[:8]}"
                ),
            )
            stype = sc.scenario_data.get(
                "scenario_type", sc.scenario_data.get("type", "\u2014")
            )
            scount = sc.scenario_data.get("stakeholder_count", "\u2014")

        score_str = (
            str(int(sc.evaluation_score))
            if sc.evaluation_score is not None
            else "\u2014"
        )

        # Sub-scores + findings only when present ─ per hard-rule "no invented values".
        eval_scores: dict = {}
        findings: list = []
        if isinstance(sc.evaluation_result, dict):
            raw_scores = sc.evaluation_result.get("scores")
            if isinstance(raw_scores, dict):
                eval_scores = raw_scores
            raw_findings = sc.evaluation_result.get("findings")
            if isinstance(raw_findings, list):
                findings = raw_findings

        card_children: list[str] = []

        # ── Header row (STORED tag, meta, title, score) ──────────────────
        components.extend(
            [
                {
                    "id": f"tag-{idx}",
                    "component": {
                        "Text": {
                            "text": {"literalString": "STORED"},
                            "usageHint": "label",
                        }
                    },
                },
                {
                    "id": f"meta-{idx}",
                    "component": {
                        "Text": {
                            "text": {
                                "literalString": f"#{_short_id(sc.scenario_id)}"
                            },
                            "usageHint": "caption",
                        }
                    },
                },
                {
                    "id": f"tag-row-{idx}",
                    "component": {
                        "Row": {
                            "children": {
                                "explicitList": [
                                    f"tag-{idx}",
                                    f"meta-{idx}",
                                ]
                            }
                        }
                    },
                },
                {
                    "id": f"title-{idx}",
                    "component": {
                        "Text": {
                            "text": {"literalString": name},
                            "usageHint": "h2",
                        }
                    },
                },
                {
                    "id": f"left-col-{idx}",
                    "component": {
                        "Column": {
                            "children": {
                                "explicitList": [
                                    f"tag-row-{idx}",
                                    f"title-{idx}",
                                ]
                            }
                        }
                    },
                },
                {
                    "id": f"score-num-{idx}",
                    "component": {
                        "Text": {
                            "text": {"literalString": score_str},
                            "usageHint": "h1",
                        }
                    },
                },
                {
                    "id": f"score-lbl-{idx}",
                    "component": {
                        "Text": {
                            "text": {"literalString": "Score"},
                            "usageHint": "caption",
                        }
                    },
                },
                {
                    "id": f"score-col-{idx}",
                    "component": {
                        "Column": {
                            "children": {
                                "explicitList": [
                                    f"score-num-{idx}",
                                    f"score-lbl-{idx}",
                                ]
                            }
                        }
                    },
                },
                {
                    "id": f"header-{idx}",
                    "component": {
                        "Row": {
                            "children": {
                                "explicitList": [
                                    f"left-col-{idx}",
                                    f"score-col-{idx}",
                                ]
                            }
                        }
                    },
                },
            ]
        )
        card_children.append(f"header-{idx}")

        # ── Detail rows (type / stakeholders / persona) ─────────────────
        # Persona is intentionally omitted when NULL rather than shown
        # as "—": the em-dash placeholder looks like a rendering bug
        # to users. This mirrors the pattern used for missing ScoreBars
        # (only present rows with real data — never invent, never leave
        # visible placeholders).
        detail_rows: list[tuple[str, str, str]] = [
            ("type", "Scenario type", str(stype)),
            ("stake", "Stakeholders", str(scount)),
        ]
        if sc.persona:
            detail_rows.append(("pers", "Persona", str(sc.persona)))
        for prefix, label, value in detail_rows:
            components.extend(
                [
                    {
                        "id": f"{prefix}-l-{idx}",
                        "component": {
                            "Text": {
                                "text": {"literalString": label},
                                "usageHint": "body",
                            }
                        },
                    },
                    {
                        "id": f"{prefix}-v-{idx}",
                        "component": {
                            "Text": {
                                "text": {"literalString": value},
                                "usageHint": "body",
                            }
                        },
                    },
                    {
                        "id": f"{prefix}-r-{idx}",
                        "component": {
                            "Row": {
                                "children": {
                                    "explicitList": [
                                        f"{prefix}-l-{idx}",
                                        f"{prefix}-v-{idx}",
                                    ]
                                }
                            }
                        },
                    },
                ]
            )
            card_children.append(f"{prefix}-r-{idx}")

        # ── Divider before score bars ───────────────────────────────────
        components.append({"id": f"d1-{idx}", "component": {"Divider": {}}})
        card_children.append(f"d1-{idx}")

        # ── Six subscore bars — omit any that are None (never invent) ───
        # eval_result.scores in the DB uses full snake_case keys (verified
        # via `SELECT jsonb_pretty(eval_result)`). Previously used short
        # ("sa", "dq", ...) keys copied from the prompts.py example, which
        # meant `eval_scores.get(short_key)` was always None and every
        # ScoreBar was silently skipped — the expanded card looked bare.
        # The `id_prefix` is the stable component id used in the A2UI
        # tree and preserves the sidebar's naming.
        subscore_defs = [
            ("sa", "stakeholder_alignment", "Stakeholder Alignment"),
            ("dq", "decision_quality",      "Decision Quality"),
            ("cr", "crisis_readiness",      "Crisis Readiness"),
            ("sc", "strategic_coherence",   "Strategic Coherence"),
            ("ea", "ethical_alignment",     "Ethical Alignment"),
            ("rs", "resource_stewardship",  "Resource Stewardship"),
        ]
        for id_prefix, db_key, label in subscore_defs:
            raw = eval_scores.get(db_key)
            if raw is None:
                continue
            try:
                val = int(raw)
            except (TypeError, ValueError):
                continue
            components.append(
                {
                    "id": f"{id_prefix}-{idx}",
                    "component": {
                        "ScoreBar": {
                            "label": {"literalString": label},
                            "value": {"literalNumber": val},
                        }
                    },
                }
            )
            card_children.append(f"{id_prefix}-{idx}")

        # ── Findings section (only if findings exist) ───────────────────
        if findings:
            components.append(
                {"id": f"d2-{idx}", "component": {"Divider": {}}}
            )
            card_children.append(f"d2-{idx}")
            components.append(
                {
                    "id": f"findings-hdr-{idx}",
                    "component": {
                        "Text": {
                            "text": {"literalString": "Findings"},
                            "usageHint": "h3",
                        }
                    },
                }
            )
            card_children.append(f"findings-hdr-{idx}")

            for f_idx, finding in enumerate(findings, start=1):
                if not isinstance(finding, dict):
                    continue
                if f_idx > 1:
                    components.append(
                        {
                            "id": f"fc-d-{idx}-{f_idx}",
                            "component": {"Divider": {}},
                        }
                    )
                    card_children.append(f"fc-d-{idx}-{f_idx}")

                severity_raw = str(finding.get("severity", "MODERATE")).upper()
                if severity_raw not in ("MAJOR", "MODERATE", "MINOR"):
                    severity_raw = "MODERATE"
                sev_hint = f"severity-{severity_raw.lower()}"

                title_txt = str(finding.get("title", ""))
                desc_txt = str(finding.get("description", ""))

                components.extend(
                    [
                        {
                            "id": f"fc-sev-{idx}-{f_idx}",
                            "component": {
                                "Text": {
                                    "text": {"literalString": severity_raw},
                                    "usageHint": sev_hint,
                                }
                            },
                        },
                        {
                            "id": f"fc-title-{idx}-{f_idx}",
                            "component": {
                                "Text": {
                                    "text": {"literalString": title_txt},
                                    "usageHint": "h5",
                                }
                            },
                        },
                        {
                            "id": f"fc-desc-{idx}-{f_idx}",
                            "component": {
                                "Text": {
                                    "text": {"literalString": desc_txt},
                                    "usageHint": "body",
                                }
                            },
                        },
                        {
                            "id": f"fc-col-{idx}-{f_idx}",
                            "component": {
                                "Column": {
                                    "children": {
                                        "explicitList": [
                                            f"fc-sev-{idx}-{f_idx}",
                                            f"fc-title-{idx}-{f_idx}",
                                            f"fc-desc-{idx}-{f_idx}",
                                        ]
                                    }
                                }
                            },
                        },
                    ]
                )
                card_children.append(f"fc-col-{idx}-{f_idx}")

        # ── Footer with action buttons (Open Report / Show Scenario) ────
        #   - "Open Report" → `organizer_show_scorecard` (local, no round-trip)
        #   - "Show Scenario" → `show_route` action (agent round-trip; the
        #     leadership_planner_with_memory prompt has a matching
        #     "Handling Show Scenario A2UI Button Actions" section that
        #     recognises the broadcast text, calls `activate_scenario`,
        #     and emits the stakeholder graph + persona detail cards).
        components.extend(
            [
                {
                    "id": f"btn-open-txt-{idx}",
                    "component": {
                        "Text": {"text": {"literalString": "Open Report"}}
                    },
                },
                {
                    "id": f"btn-open-{idx}",
                    "component": {
                        "Button": {
                            "action": {"name": "organizer_show_scorecard"},
                            "child": f"btn-open-txt-{idx}",
                        }
                    },
                },
                {
                    "id": f"btn-show-txt-{idx}",
                    "component": {
                        "Text": {"text": {"literalString": "Show Scenario"}}
                    },
                },
                {
                    "id": f"btn-show-{idx}",
                    "component": {
                        "Button": {
                            "action": {
                                "name": "show_route",
                                "payload": {"seed": sc.scenario_id},
                            },
                            "child": f"btn-show-txt-{idx}",
                        }
                    },
                },
                {
                    "id": f"footer-{idx}",
                    "component": {
                        "Row": {
                            "children": {
                                "explicitList": [
                                    f"btn-open-{idx}",
                                    f"btn-show-{idx}",
                                ]
                            }
                        }
                    },
                },
            ]
        )
        card_children.append(f"footer-{idx}")

        # Wrap the per-scenario children in a Column, then a Card.
        components.append(
            {
                "id": f"card-col-{idx}",
                "component": {
                    "Column": {
                        "children": {"explicitList": card_children}
                    }
                },
            }
        )
        components.append(
            {
                "id": f"card-{idx}",
                "component": {"Card": {"child": f"card-col-{idx}"}},
            }
        )
        list_children.append(f"card-{idx}")

    # ── Top-level list + outer card ─────────────────────────────────────
    components.append(
        {
            "id": "root-list",
            "component": {
                "List": {"children": {"explicitList": list_children}}
            },
        }
    )
    components.append(
        {
            "id": "root-card",
            "component": {"Card": {"child": "root-list"}},
        }
    )

    # ── Emit surfaceUpdate + beginRendering ─────────────────────────────
    surface_update = {
        "surfaceUpdate": {
            "surfaceId": "scenario_list",
            "components": components,
        }
    }
    begin_rendering = {
        "beginRendering": {
            "surfaceId": "scenario_list",
            "root": "root-card",
        }
    }

    session_id = tool_context.session.id
    simulation_id = tool_context.state.get("simulation_id")
    origin = {"type": "agent", "id": "organizer", "session_id": session_id}
    destination = [session_id]

    _emit_step = "surfaceUpdate"
    try:
        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=surface_update,
            simulation_id=simulation_id,
        )
        _emit_step = "beginRendering"
        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=begin_rendering,
            simulation_id=simulation_id,
        )
    except Exception:
        logger.error(
            "emit_scenario_list_a2ui: emit failed at step %s for session %s",
            _emit_step,
            session_id,
            exc_info=True,
        )
        return {"status": "error", "message": f"Failed to emit A2UI at step {_emit_step}."}

    logger.info(
        "emit_scenario_list_a2ui: emitted %d scenario(s) for session %s",
        len(scenarios),
        session_id,
    )
    return {"status": "success", "count": len(scenarios)}


# ---------------------------------------------------------------------------
# Show Scenario fast-path (Explorer / Organizer "Show Scenario" button)
# ---------------------------------------------------------------------------
def _short_id(scenario_id: str) -> str:
    """Return a short, distinctive suffix of a scenario id for card captions.

    Handles both formats we see in the DB:

    * UUIDs (``a1b2c3d4-...``)  →  ``a1b2c3d4`` (first 8 chars)
    * Seed rows (``seed-leadership-002``)  →  ``leadership-002``
      (everything after the leading ``seed-`` prefix, then last 12 chars)

    The old ``scenario_id[:4]`` implementation rendered every seed row
    as ``#seed`` in the Organizer, which was worse than useless.
    """
    if not isinstance(scenario_id, str) or not scenario_id:
        return ""
    stripped = scenario_id[5:] if scenario_id.startswith("seed-") else scenario_id
    # For UUIDs the leading 8 chars are the standard short form; for
    # seed rows the last 12 chars keep the human-readable slug.
    return stripped[:12] if "-" in stripped else stripped[:8]


def _extract_scenario_id(text: str) -> str | None:
    """Pull a UUID out of a broadcast-text button payload.

    This regex accepts either a canonical UUID **or** a seed-style id
    such as ``seed-leadership-002`` (the shape used by the local
    Postgres seed data). Restricting to UUIDs previously caused the
    fast-path to silently fail on every button click against seed
    data, since ``re.search`` returned None and the caller passed an
    empty string to :func:`emit_scenario_detail_a2ui`.
    """
    import re

    if not isinstance(text, str):
        return None
    # Try UUID first (canonical, most specific).
    m = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        text,
    )
    if m:
        return m.group(0)
    # Fall back to seed-style ids: seed-<slug>-<slug>[-<slug>...].
    # Requires at least two dash-separated segments after "seed-" to
    # avoid matching random words.
    m = re.search(r"seed(?:-[A-Za-z0-9]+){2,}", text)
    if m:
        return m.group(0)
    # Sentinel used by the "← Back to list" button on detail cards.
    if "__list__" in text:
        return "__list__"
    return None


async def emit_scenario_detail_a2ui(
    tool_context: ToolContext,
    scenario_id: str,
) -> dict:
    """Fast-path for the Organizer "Show Scenario" button.

    Fetches a single scenario by id, builds a detail A2UI card, and
    emits it (surfaceUpdate + beginRendering) in one atomic call.
    The Organizer's ``chat$`` subscription keys off ``surfaceUpdate``
    and replaces the list view with this detail card automatically —
    no additional client wiring is needed.

    The card layout follows a header → detail rows → findings →
    recommendations structure using leadership-domain fields (persona,
    stakeholder count, evaluation findings + recommendations). "Show"
    means "render the scenario's full-detail card into the Organizer
    surface".

    Args:
        tool_context: ADK tool context.
        scenario_id: UUID of the scenario to render. The prompt
            classifier extracts this from the frontend's plain-text
            broadcast (``"Show scenario <UUID>"``).

    Returns:
        ``{"status": "success", "scenario_id": ...}`` when the card
        was emitted, ``{"status": "error", "message": ...}`` when the
        scenario is unknown, unowned, or the emit failed.
    """

    if not isinstance(scenario_id, str) or not scenario_id.strip():
        return {"status": "error", "message": "scenario_id is required."}
    scenario_id = scenario_id.strip()

    # Sentinel: the "← Back to list" button on a detail card fires
    # the `show_route` action with payload seed="__list__". The LLM
    # may route it here rather than to `emit_scenario_list_a2ui` (the
    # classifier ordering in the prompt puts the sentinel first, but
    # this is the deterministic safety net). Delegate directly.
    if scenario_id in {"__list__", "list", "__back__"}:
        return await emit_scenario_list_a2ui(tool_context, limit=3)

    owner_id = _resolve_owner_id(tool_context)
    scenario = await _store.get_scenario(scenario_id, owner_id=owner_id)
    if scenario is None:
        return {
            "status": "error",
            "message": f"Scenario {scenario_id} not found for this user.",
        }

    # ── PATHWAY-ONLY MODE ───────────────────────────────────────────────
    # User explicitly does NOT want the Organizer detail card. Clicking
    # "Show Scenario" should render ONLY the scenario's stored consensus
    # pathway (GeoJSON) on the 3D map — no sidebar menu, no findings, no
    # ScoreBars. So we skip the entire card-build/emit path and just
    # emit the synthetic ``report_leadership_scenario`` tool_end that
    # the frontend's TOOLS.SHOW_CONSENSUS_PATHWAY handler catches and
    # dispatches to the map via ``gateway:routeGeojson``.
    from agents.utils.pulses import emit_gateway_message as _emit

    pathway_geojson = None
    if isinstance(scenario.scenario_data, dict):
        pathway = scenario.scenario_data.get("_consensus_pathway_active")
        if isinstance(pathway, dict):
            candidate = pathway.get("geojson")
            if candidate:
                pathway_geojson = candidate

    session_id = tool_context.session.id
    simulation_id = tool_context.state.get("simulation_id")
    origin = {"type": "agent", "id": "organizer", "session_id": session_id}
    destination = [session_id]

    if pathway_geojson is not None:
        try:
            await _emit(
                origin=origin,
                destination=destination,
                status="success",
                msg_type="json",
                event="tool_end",
                data={
                    "tool_name": "report_leadership_scenario",
                    "result": {
                        "status": "success",
                        "scenario_summary": {
                            "scenario_id": scenario_id,
                            "consensus_pathway": pathway_geojson,
                        },
                    },
                },
                simulation_id=simulation_id,
            )
            logger.info(
                "emit_scenario_detail_a2ui: pathway-only mode — emitted "
                "consensus_pathway GeoJSON for scenario %s (session %s)",
                scenario_id,
                session_id,
            )
        except Exception:
            logger.exception(
                "emit_scenario_detail_a2ui: pathway emit failed for %s",
                scenario_id,
            )
            return {"status": "error", "message": "Pathway emit failed."}
    else:
        logger.info(
            "emit_scenario_detail_a2ui: scenario %s has no stored pathway "
            "— nothing to render on the map",
            scenario_id,
        )
        return {"status": "info", "message": "No consensus pathway available for this scenario. Design a new pathway first.", "pathway_emitted": False}

    return {
        "status": "success",
        "scenario_id": scenario_id,
        "pathway_emitted": pathway_geojson is not None,
    }






# ---------------------------------------------------------------------------
# User-selected past scenarios (replaces automatic vector search)
# ---------------------------------------------------------------------------

SELECTED_CONTEXT_SCENARIOS_KEY = "selected_context_scenario_ids"


async def get_selected_scenarios(
    tool_context: ToolContext,
    scenario_ids: list[str] | None = None,
) -> dict:
    """Fetch full scenario data for user-selected past simulations.

    The Organizer sidebar lets the user check past scenarios they want
    to include as context for a new planning run. Selected IDs may be
    passed:

    1. Explicitly via ``scenario_ids`` (the LLM extracts them from
       the user's prompt, e.g. "Run simulation using context from
       scenarios abc-123 and def-456").
    2. From session state under ``selected_context_scenario_ids``
       (set by a callback or the frontend's spawn payload).

    Explicit ``scenario_ids`` win. If neither is set, returns
    ``none_selected``.

    Returns ``{"status": "none_selected", ...}`` when the user opted
    out (empty or missing selection). Individual missing scenario IDs
    are silently skipped — the user might have deleted a scenario after
    selecting it.

    This tool is **user-directed**: the user explicitly picks which past runs
    inform the next one. The planner should mention the past experience
    and use learnings to inform scenario design.

    Args:
        tool_context: ADK ToolContext (injected).
        scenario_ids: Optional explicit list of scenario UUIDs. When
            omitted, reads from ``state["selected_context_scenario_ids"]``.

    Returns:
        ``{"status": "success", "count": N, "scenarios": [...]}``, or
        ``{"status": "none_selected", "message": "...", "scenarios": []}``.
        Never raises.
    """
    # 1. Resolve selected IDs: explicit param → session state.
    selected_ids = None
    if scenario_ids and isinstance(scenario_ids, list):
        selected_ids = scenario_ids
    else:
        try:
            selected_ids = tool_context.state.get(SELECTED_CONTEXT_SCENARIOS_KEY)
        except (AttributeError, TypeError):
            selected_ids = None

    if not selected_ids or not isinstance(selected_ids, list):
        return {
            "status": "none_selected",
            "message": (
                "No past scenarios selected. The user can select past runs "
                "in the Organizer sidebar to provide context for this planning run."
            ),
            "scenarios": [],
        }

    # 2. Fetch each scenario. Skip missing IDs gracefully.
    owner_id = _resolve_owner_id(tool_context)
    scenarios: list[dict] = []
    for sid in selected_ids:
        if not isinstance(sid, str) or not sid.strip():
            continue
        try:
            scenario = await _store.get_scenario(
                scenario_id=sid.strip(), owner_id=owner_id
            )
        except Exception as exc:
            logger.warning(
                "get_selected_scenarios: failed to fetch %s: %s", sid, exc
            )
            continue
        if scenario is None:
            logger.debug(
                "get_selected_scenarios: scenario %s not found (possibly deleted)", sid
            )
            continue
        scenarios.append(_scenario_to_dict(scenario))

    if not scenarios:
        return {
            "status": "none_selected",
            "message": (
                "Selected scenario IDs could not be resolved — they may have "
                "been deleted. Proceeding without past context."
            ),
            "scenarios": [],
        }

    logger.info(
        "get_selected_scenarios: fetched %d of %d selected scenarios",
        len(scenarios),
        len(selected_ids),
    )
    return {
        "status": "success",
        "count": len(scenarios),
        "scenarios": scenarios,
    }



