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

"""Consultant tools for Leadership Planner with Eval.

Generates and emits the 90-Day Action Plan playbook via A2UI.
"""

import logging

from google.adk.tools.tool_context import ToolContext

from agents.utils.pulses import emit_gateway_message

logger = logging.getLogger(__name__)


async def emit_action_plan_a2ui(tool_context: ToolContext, plan_markdown: str) -> str:
    """Emits the 90-Day Action Plan playbook as an A2UI card to the frontend.

    This function parses the input markdown text roughly into A2UI components
    so the LLM doesn't have to construct the verbose A2UI JSON payload itself.

    The A2UI card is emitted as two gateway messages (surfaceUpdate then
    beginRendering), following the same protocol used by coaching cards in
    ``advance_tick``.

    Args:
        tool_context: ADK tool context.
        plan_markdown: The markdown content of the 90-day action plan.
    """
    lines = plan_markdown.strip().split("\n")

    components = []
    child_ids = []

    # Title component
    components.append(
        {
            "id": "playbook-title",
            "component": {
                "Text": {"text": {"literalString": "90-Day Action Plan (Consultant Playbook)"}, "usageHint": "h1"}
            },
        }
    )
    child_ids.append("playbook-title")

    components.append({"id": "playbook-div-1", "component": {"Divider": {}}})
    child_ids.append("playbook-div-1")

    # Simple block parsing
    block_id = 0
    current_text = []

    def _flush_block():
        nonlocal block_id, current_text
        if not current_text:
            return

        text_str = "\n".join(current_text).strip()
        if not text_str:
            return

        c_id = f"pb-block-{block_id}"
        block_id += 1

        # Check if header
        usage_hint = "body"
        if text_str.startswith("#"):
            text_str = text_str.lstrip("#").strip()
            usage_hint = "h2"

        components.append(
            {"id": c_id, "component": {"Text": {"text": {"literalString": text_str}, "usageHint": usage_hint}}}
        )
        child_ids.append(c_id)
        current_text = []

    def _is_bullet(s: str) -> bool:
        s_strip = s.lstrip()
        if not s_strip:
            return False
        if s_strip[:2] in ("- ", "* ", "• "):
            return True
        # Numbered bullet: "1. ", "12. ", etc.
        head = s_strip.split(" ", 1)[0]
        if head.endswith(".") and head[:-1].isdigit():
            return True
        return False

    # Keep consecutive bullets in a single Text body block but separate them
    # with a blank line so the markdown pipe renders them as a *loose* list
    # (each <li> wrapped in <p>) — that produces visible vertical spacing
    # without needing extra components.
    prev_was_bullet = False
    for line in lines:
        if line.startswith("#"):
            _flush_block()
            current_text.append(line)
            _flush_block()
            prev_was_bullet = False
        elif line.strip() == "":
            _flush_block()
            prev_was_bullet = False
        elif _is_bullet(line):
            if prev_was_bullet:
                current_text.append("")  # loose list separator
            current_text.append(line)
            prev_was_bullet = True
        else:
            current_text.append(line)
            prev_was_bullet = False

    _flush_block()

    components.append({"id": "playbook-content", "component": {"Column": {"children": {"explicitList": child_ids}}}})

    components.append({"id": "playbook-card", "component": {"Card": {"child": "playbook-content"}}})

    surface_update = {"surfaceUpdate": {"surfaceId": "action_plan", "components": components}}
    begin_rendering = {"beginRendering": {"surfaceId": "action_plan", "root": "playbook-card"}}

    session_id = tool_context.session.id
    simulation_id = tool_context.state.get("simulation_id")

    origin = {"type": "agent", "id": "consultant", "session_id": session_id}
    destination = [session_id]

    # Emit the A2UI card as two gateway messages (surfaceUpdate then
    # beginRendering), matching the protocol used by coaching cards
    # in advance_tick (advancing-race-ticks/tools.py L686-L703).
    # Track which step we're on so a Redis blip between the two awaits
    # surfaces as a partial-failure error — the dangerous case is
    # success-then-failure, which leaves the frontend with an orphaned
    # surface registration and no render trigger.
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
    except Exception as e:
        if _emit_step == "beginRendering":
            logger.error(
                "emit_action_plan_a2ui: partial emit for session %s — "
                "surfaceUpdate landed but beginRendering failed (%s). "
                "Frontend will hold an orphaned surface until session reset.",
                session_id,
                e,
                exc_info=True,
            )
        else:
            logger.error(
                "emit_action_plan_a2ui: surfaceUpdate emit failed for session %s: %s",
                session_id,
                e,
                exc_info=True,
            )
        raise

    logger.info("Successfully emitted 90-Day Action Plan A2UI for session %s", session_id)

    return "SUCCESS: The 90-Day Action Plan has been successfully emitted to the UI."


async def emit_sim_results_a2ui(
    tool_context: ToolContext,
    scenario_name: str,
    overall_score: int,
    stakeholder_alignment: int,
    decision_quality: int,
    crisis_readiness: int,
    coaching_kotter_title: str,
    coaching_kotter_desc: str,
    coaching_adkar_title: str,
    coaching_adkar_desc: str,
    coaching_synthesis: str,
) -> str:
    """Emits the Simulation Results dashboard as an A2UI card.

    Deterministically builds the three-card layout (Main Results, Strategy
    Overview, Coaching Insights) from structured data so the LLM does not
    need to construct A2UI JSON manually.

    Args:
        tool_context: ADK tool context.
        scenario_name: Title of the simulation scenario.
        overall_score: Overall evaluation score (0-100 integer).
        stakeholder_alignment: Stakeholder Alignment score (0-100).
        decision_quality: Decision Quality score (0-100).
        crisis_readiness: Crisis Readiness score (0-100).
        coaching_kotter_title: Kotter coaching insight title.
        coaching_kotter_desc: Kotter coaching insight description.
        coaching_adkar_title: ADKAR coaching insight title.
        coaching_adkar_desc: ADKAR coaching insight description.
        coaching_synthesis: One-sentence synthesis takeaway.
    """
    # --- Self-heal scores from session state -------------------------------
    # The LLM has to recall four integers from a much earlier
    # `evaluate_leadership_plan` turn. When recall fails it passes 0 (or the
    # tool gets called with stale args). Fall back to the canonical evaluator
    # result stored in `tool_context.state["leadership_evaluation"]` so the
    # bottom KPIs never render as blank/zero on a successful evaluation.
    eval_result = tool_context.state.get("leadership_evaluation") or tool_context.state.get("evaluation_result") or {}
    eval_scores = eval_result.get("scores") or {}

    def _coalesce(arg_value, state_value):
        """Use arg if it's a real positive int; otherwise the state value."""
        try:
            iv = int(arg_value) if arg_value is not None and str(arg_value).strip() != "" else -1
        except (TypeError, ValueError):
            iv = -1
        if iv >= 0:
            return iv

        try:
            sv = int(state_value) if state_value is not None and str(state_value).strip() != "" else -1
        except (TypeError, ValueError):
            sv = -1
        return sv if sv >= 0 else 0

    overall_score = _coalesce(overall_score, eval_result.get("overall_score"))
    stakeholder_alignment = _coalesce(stakeholder_alignment, eval_scores.get("stakeholder_alignment"))
    decision_quality = _coalesce(decision_quality, eval_scores.get("decision_quality"))
    crisis_readiness = _coalesce(crisis_readiness, eval_scores.get("crisis_readiness"))

    # Self-heal the 3 additional criteria (not LLM parameters — read from state only)
    resource_stewardship = max(0, min(100, int(_coalesce(None, eval_scores.get("resource_stewardship")))))
    strategic_coherence = max(0, min(100, int(_coalesce(None, eval_scores.get("strategic_coherence")))))
    ethical_alignment = max(0, min(100, int(_coalesce(None, eval_scores.get("ethical_alignment")))))

    logger.info(
        "CONSULTANT_DIAG: sim_results scores after state-heal: overall=%s, sa=%s, dq=%s, cr=%s (eval_state_present=%s)",
        overall_score,
        stakeholder_alignment,
        decision_quality,
        crisis_readiness,
        bool(eval_result),
    )

    # Clamp all score parameters to valid 0-100 range
    overall_score = max(0, min(100, int(overall_score or 0)))
    stakeholder_alignment = max(0, min(100, int(stakeholder_alignment or 0)))
    decision_quality = max(0, min(100, int(decision_quality or 0)))
    crisis_readiness = max(0, min(100, int(crisis_readiness or 0)))

    simulation_id = tool_context.state.get("simulation_id", "—")
    expected = tool_context.state.get("expected_stakeholder_count", 1_000)

    # --- Load structured data from Redis side-channel ---
    # compile_results stores persona_status_counts and runner_count to Redis
    # so they survive the A2A text boundary between simulator and planner.
    from agents.utils.simdata import load_simulation_data

    sim_data: dict = {}
    if simulation_id and simulation_id != "—":
        sim_data = await load_simulation_data(simulation_id)
        logger.info(
            "CONSULTANT_DIAG: load_simulation_data(%s) returned keys=%s, persona_status_counts=%s, runner_count=%s",
            simulation_id,
            list(sim_data.keys()) if sim_data else "EMPTY",
            sim_data.get("persona_status_counts"),
            sim_data.get("runner_count"),
        )
    else:
        logger.warning(
            "CONSULTANT_DIAG: simulation_id is missing or placeholder: %r",
            simulation_id,
        )

    # Extract runner_count: Redis side-channel → simulation_config → fallback
    runner_count = sim_data.get("runner_count")
    if runner_count is None:
        sim_config = tool_context.state.get("simulation_config", {})
        runner_count = sim_config.get("runner_count")

    # Format stakeholder counts with comma separators
    expected_str = f"{expected:,}"
    simulated_str = f"{runner_count:,}" if runner_count else "—"

    # --- Persona progress from Redis side-channel ---
    persona_counts: dict[str, dict[str, int]] = sim_data.get("persona_status_counts") or {}
    persona_progress: dict[str, float] = sim_data.get("persona_avg_progress") or {}

    # Fallback: try session state (works if called within the simulator session)
    if not persona_counts:
        persona_counts = tool_context.state.get("persona_status_counts", {})
    if not persona_progress:
        persona_progress = tool_context.state.get("persona_avg_progress", {})

    # Calculate persona display values
    persona_order = ["Visionary", "Diplomat", "Pragmatist", "Disruptor"]
    # Normalize keys to title case for matching
    norm_counts: dict[str, dict[str, int]] = {}
    for k, v in persona_counts.items():
        norm_counts[k.title()] = v if isinstance(v, dict) else {}
    # Preserve None to distinguish "no measurement" from "measured zero".
    # The old behavior coerced None→00.0 then later filtered with `> 0`,
    # which silently dropped a legitimately measured 0% — the exact case
    # the user most needs to see on a leadership dashboard.
    norm_progress: dict[str, float | None] = {}
    for k, v in persona_progress.items():
        norm_progress[k.title()] = float(v) if v is not None else None

    persona_values: dict[str, str] = {}
    # Parallel numeric percentages used as ScoreBar values. None means "no data"
    # — we omit the ScoreBar instead of emitting a misleading 0.
    persona_pcts: dict[str, int | None] = {}
    for persona in persona_order:
        progress_pct = norm_progress.get(persona)
        counts = norm_counts.get(persona, {})

        if progress_pct is not None:
            # Primary metric: average alignment progress. 0.0 is a valid
            # measurement and must render as a red 0% bar, not be dropped.
            pct = round(progress_pct)
            persona_values[persona] = f"{pct}% Progress"
            persona_pcts[persona] = pct
        elif counts:
            # Fallback: binary aligned count
            aligned = counts.get("finished", 0) + counts.get("aligned", 0)
            total = sum(counts.values())
            if total > 0:
                pct = round(100 * aligned / total)
                persona_values[persona] = f"{pct}% Aligned" if pct > 0 else "0% Progress"
                persona_pcts[persona] = pct
            else:
                persona_values[persona] = "—"
                persona_pcts[persona] = None
        else:
            persona_values[persona] = "—"
            persona_pcts[persona] = None

    logger.info(
        "CONSULTANT_DIAG: persona_values=%s, norm_progress=%s, norm_counts_keys=%s",
        persona_values,
        norm_progress,
        list(norm_counts.keys()),
    )

    # Determine winning strategy using progress metric (more granular than binary aligned)
    best_persona = "—"
    best_pct = 0.0
    for persona in persona_order:
        progress_pct = norm_progress.get(persona)
        if progress_pct is not None and progress_pct > best_pct:
            best_pct = progress_pct
            best_persona = persona

    # Fallback: if no progress data, try binary aligned count
    if best_persona == "—":
        for persona in persona_order:
            counts = norm_counts.get(persona, {})
            if counts:
                aligned = counts.get("finished", 0) + counts.get("aligned", 0)
                total = sum(counts.values())
                if total > 0:
                    pct = 100.0 * aligned / total
                    if pct > best_pct:
                        best_pct = pct
                        best_persona = persona

    winning_desc = f"{best_persona}" if best_persona != "—" else "No clear winner"

    # ---- Build A2UI components ----
    components: list[dict] = []

    # === Card 1: Main Results ===
    components.extend(
        [
            {"id": "tag-main", "component": {"Text": {"text": {"literalString": "SIMULATED"}, "usageHint": "label"}}},
            {
                "id": "sim-meta-main",
                "component": {"Text": {"text": {"literalString": str(simulation_id)}, "usageHint": "caption"}},
            },
            {"id": "tag-row-main", "component": {"Row": {"children": {"explicitList": ["tag-main", "sim-meta-main"]}}}},
            {"id": "title-main", "component": {"Text": {"text": {"literalString": scenario_name}, "usageHint": "h2"}}},
            {
                "id": "left-col-main",
                "component": {"Column": {"children": {"explicitList": ["tag-row-main", "title-main"]}}},
            },
            {
                "id": "score-num-main",
                "component": {"Text": {"text": {"literalString": str(overall_score)}, "usageHint": "h1"}},
            },
            {
                "id": "score-lbl-main",
                "component": {"Text": {"text": {"literalString": "Score"}, "usageHint": "caption"}},
            },
            {
                "id": "score-col-main",
                "component": {"Column": {"children": {"explicitList": ["score-num-main", "score-lbl-main"]}}},
            },
            {
                "id": "header-main",
                "component": {"Row": {"children": {"explicitList": ["left-col-main", "score-col-main"]}}},
            },
            {
                "id": "stake-l-main",
                "component": {
                    "Text": {"text": {"literalString": "Stakeholders (expected/simulated)"}, "usageHint": "body"}
                },
            },
            {
                "id": "stake-v-main",
                "component": {
                    "Text": {"text": {"literalString": f"{expected_str}/{simulated_str}"}, "usageHint": "body"}
                },
            },
            {
                "id": "stake-r-main",
                "component": {"Row": {"children": {"explicitList": ["stake-l-main", "stake-v-main"]}}},
            },
            {"id": "d1-main", "component": {"Divider": {}}},
            {
                "id": "sa-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Stakeholder Alignment"},
                        "value": {"literalNumber": int(stakeholder_alignment)},
                    }
                },
            },
            {
                "id": "dq-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Decision Quality"},
                        "value": {"literalNumber": int(decision_quality)},
                    }
                },
            },
            {
                "id": "cr-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Crisis Readiness"},
                        "value": {"literalNumber": int(crisis_readiness)},
                    }
                },
            },
            {
                "id": "rs-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Resource Stewardship"},
                        "value": {"literalNumber": int(resource_stewardship)},
                    }
                },
            },
            {
                "id": "sc-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Strategic Coherence"},
                        "value": {"literalNumber": int(strategic_coherence)},
                    }
                },
            },
            {
                "id": "ea-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Ethical Alignment"},
                        "value": {"literalNumber": int(ethical_alignment)},
                    }
                },
            },
            {"id": "d2-main", "component": {"Divider": {}}},
            {"id": "rerun-txt-main", "component": {"Text": {"text": {"literalString": "Re-run Simulation"}}}},
            {
                "id": "rerun-btn-main",
                "component": {
                    "Button": {
                        "child": "rerun-txt-main",
                        "action": {"name": "run_simulation"},
                        "primary": {"literalBoolean": True},
                    }
                },
            },
            {
                "id": "main-content",
                "component": {
                    "Column": {
                        "children": {
                            "explicitList": [
                                "header-main",
                                "stake-r-main",
                                "d1-main",
                                "sa-main",
                                "dq-main",
                                "cr-main",
                                "rs-main",
                                "sc-main",
                                "ea-main",
                                "d2-main",
                                "rerun-btn-main",
                            ]
                        }
                    }
                },
            },
            {"id": "card-main", "component": {"Card": {"child": "main-content"}}},
        ]
    )

    # === Card 2: Strategy Overview ===
    # Build persona breakdown as ScoreBars when we have numeric data,
    # otherwise fall back to a body Text saying "— (no data)" so the row
    # is still informative without faking a 0 score.
    strategy_components: list[dict] = [
        {
            "id": "strategy-title",
            "component": {
                "Text": {"text": {"literalString": "Strategy Overview (Persona Breakdown)"}, "usageHint": "h2"}
            },
        },
    ]
    persona_child_ids: list[str] = ["strategy-title"]
    persona_id_map = {
        "Visionary": "vis",
        "Diplomat": "dip",
        "Pragmatist": "prg",
        "Disruptor": "dis",
    }
    for persona, prefix in persona_id_map.items():
        pct = persona_pcts.get(persona)
        if pct is not None:
            strategy_components.append(
                {
                    "id": f"{prefix}-bar",
                    "component": {
                        "ScoreBar": {
                            "label": {"literalString": persona},
                            "value": {"literalNumber": int(pct)},
                        }
                    },
                }
            )
            persona_child_ids.append(f"{prefix}-bar")
        else:
            strategy_components.extend(
                [
                    {
                        "id": f"{prefix}-lbl",
                        "component": {"Text": {"text": {"literalString": persona}, "usageHint": "body"}},
                    },
                    {
                        "id": f"{prefix}-val",
                        "component": {
                            "Text": {"text": {"literalString": persona_values[persona]}, "usageHint": "body"}
                        },
                    },
                    {
                        "id": f"{prefix}-row",
                        "component": {"Row": {"children": {"explicitList": [f"{prefix}-lbl", f"{prefix}-val"]}}},
                    },
                ]
            )
            persona_child_ids.append(f"{prefix}-row")
    strategy_components.extend(
        [
            {"id": "d-strategy", "component": {"Divider": {}}},
            {
                "id": "winning-lbl",
                "component": {"Text": {"text": {"literalString": "Winning Strategy"}, "usageHint": "caption"}},
            },
            {
                "id": "winning-desc",
                "component": {"Text": {"text": {"literalString": winning_desc}, "usageHint": "body"}},
            },
        ]
    )
    persona_child_ids.extend(["d-strategy", "winning-lbl", "winning-desc"])
    strategy_components.append(
        {"id": "strategy-content", "component": {"Column": {"children": {"explicitList": persona_child_ids}}}}
    )
    strategy_components.append(
        {
            "id": "card-strategy",
            "component": {"Card": {"child": "strategy-content", "expandable": {"literalBoolean": True}}},
        }
    )
    components.extend(strategy_components)

    # === Card 3: Coaching Insights ===
    components.extend(
        [
            {
                "id": "coach-title-card",
                "component": {"Text": {"text": {"literalString": "Coaching Insights"}, "usageHint": "h2"}},
            },
            {
                "id": "c1-title",
                "component": {"Text": {"text": {"literalString": coaching_kotter_title}, "usageHint": "body"}},
            },
            {"id": "c1-fw", "component": {"Text": {"text": {"literalString": "KOTTER"}, "usageHint": "caption"}}},
            {"id": "c1-row", "component": {"Row": {"children": {"explicitList": ["c1-title", "c1-fw"]}}}},
            {
                "id": "c1-desc",
                "component": {"Text": {"text": {"literalString": coaching_kotter_desc}, "usageHint": "body"}},
            },
            {
                "id": "c2-title",
                "component": {"Text": {"text": {"literalString": coaching_adkar_title}, "usageHint": "body"}},
            },
            {"id": "c2-fw", "component": {"Text": {"text": {"literalString": "ADKAR"}, "usageHint": "caption"}}},
            {"id": "c2-row", "component": {"Row": {"children": {"explicitList": ["c2-title", "c2-fw"]}}}},
            {
                "id": "c2-desc",
                "component": {"Text": {"text": {"literalString": coaching_adkar_desc}, "usageHint": "body"}},
            },
            {"id": "c3-title", "component": {"Text": {"text": {"literalString": "Synthesis"}, "usageHint": "body"}}},
            {"id": "c3-fw", "component": {"Text": {"text": {"literalString": "TAKEAWAY"}, "usageHint": "caption"}}},
            {"id": "c3-row", "component": {"Row": {"children": {"explicitList": ["c3-title", "c3-fw"]}}}},
            {
                "id": "c3-desc",
                "component": {"Text": {"text": {"literalString": coaching_synthesis}, "usageHint": "body"}},
            },
            {
                "id": "coach-content",
                "component": {
                    "Column": {
                        "children": {
                            "explicitList": [
                                "coach-title-card",
                                "c1-row",
                                "c1-desc",
                                "c2-row",
                                "c2-desc",
                                "c3-row",
                                "c3-desc",
                            ]
                        }
                    }
                },
            },
            {
                "id": "card-coaching",
                "component": {"Card": {"child": "coach-content", "expandable": {"literalBoolean": True}}},
            },
        ]
    )

    # === Card 4: Transformation Status ===
    # Pull rich outcome data from the Redis side-channel that was already loaded above.
    total_ticks = len(sim_data.get("vitals_trend") or [])
    alert_history: list = sim_data.get("alert_history") or []
    alert_count = len(alert_history)
    black_swans: list = sim_data.get("triggered_black_swans") or (
        sim_data.get("quest_config") or {}
    ).get("triggered_black_swans") or []
    black_swan_count = len(black_swans)

    # Capital / resource status from session state (more reliable than redis for planner)
    resources: dict = sim_data.get("resources") or tool_context.state.get("resources") or {}
    capital_val = resources.get("leadership_capital") or resources.get("capital")
    capital_initial = None
    if capital_val is None:
        # Simulator resource pools are keyed lau/bt/tbu/owb (case varies);
        # leadership capital is the LAU (Leadership Attention Units) pool.
        lau_pool = resources.get("lau") or resources.get("LAU")
        if isinstance(lau_pool, dict):
            capital_val = lau_pool.get("current")
            capital_initial = lau_pool.get("initial")
        elif isinstance(lau_pool, (int, float)):
            capital_val = lau_pool
    try:
        capital_str = f"{int(capital_val):,}" if capital_val is not None else "—"
        if capital_val is not None and capital_initial:
            capital_str = f"{capital_str} / {int(capital_initial):,} LAU"
    except (TypeError, ValueError):
        capital_str = "—"

    # Compute overall alignment rate from persona status counts
    total_runners_all = sum(
        sum(v.values()) for v in (persona_counts or {}).values() if isinstance(v, dict)
    )
    aligned_all = sum(
        v.get("finished", 0) + v.get("aligned", 0)
        for v in (persona_counts or {}).values()
        if isinstance(v, dict)
    )
    alignment_rate_str = (
        f"{round(100.0 * aligned_all / total_runners_all)}%" if total_runners_all > 0 else "—"
    )

    # Transformation outcome label based on overall score
    if overall_score >= 80:
        outcome_label = "Transformation Successful"
    elif overall_score >= 60:
        outcome_label = "Partial Transformation"
    elif overall_score >= 40:
        outcome_label = "Transformation Stalled"
    else:
        outcome_label = "Transformation Failed"

    status_components: list[dict] = [
        {
            "id": "status-title",
            "component": {"Text": {"text": {"literalString": "Transformation Status"}, "usageHint": "h2"}},
        },
        {
            "id": "status-outcome-val",
            "component": {"Text": {"text": {"literalString": outcome_label}, "usageHint": "h3"}},
        },
        {"id": "status-d1", "component": {"Divider": {}}},
        {
            "id": "status-align-lbl",
            "component": {"Text": {"text": {"literalString": "Overall Alignment Rate"}, "usageHint": "caption"}},
        },
        {
            "id": "status-align-val",
            "component": {"Text": {"text": {"literalString": alignment_rate_str}, "usageHint": "body"}},
        },
        {
            "id": "status-align-row",
            "component": {"Row": {"children": {"explicitList": ["status-align-lbl", "status-align-val"]}}},
        },
        {
            "id": "status-capital-lbl",
            "component": {"Text": {"text": {"literalString": "Leadership Capital Remaining"}, "usageHint": "caption"}},
        },
        {
            "id": "status-capital-val",
            "component": {"Text": {"text": {"literalString": capital_str}, "usageHint": "body"}},
        },
        {
            "id": "status-capital-row",
            "component": {"Row": {"children": {"explicitList": ["status-capital-lbl", "status-capital-val"]}}},
        },
        {
            "id": "status-ticks-lbl",
            "component": {"Text": {"text": {"literalString": "Simulation Cycles Run"}, "usageHint": "caption"}},
        },
        {
            "id": "status-ticks-val",
            "component": {"Text": {"text": {"literalString": str(total_ticks) if total_ticks else "—"}, "usageHint": "body"}},
        },
        {
            "id": "status-ticks-row",
            "component": {"Row": {"children": {"explicitList": ["status-ticks-lbl", "status-ticks-val"]}}},
        },
        {"id": "status-d2", "component": {"Divider": {}}},
        {
            "id": "status-bs-lbl",
            "component": {"Text": {"text": {"literalString": "Black Swan Events Triggered"}, "usageHint": "caption"}},
        },
        {
            "id": "status-bs-val",
            "component": {"Text": {"text": {"literalString": str(black_swan_count)}, "usageHint": "body"}},
        },
        {
            "id": "status-bs-row",
            "component": {"Row": {"children": {"explicitList": ["status-bs-lbl", "status-bs-val"]}}},
        },
        {
            "id": "status-alert-lbl",
            "component": {"Text": {"text": {"literalString": "Critical Alerts Raised"}, "usageHint": "caption"}},
        },
        {
            "id": "status-alert-val",
            "component": {"Text": {"text": {"literalString": str(alert_count)}, "usageHint": "body"}},
        },
        {
            "id": "status-alert-row",
            "component": {"Row": {"children": {"explicitList": ["status-alert-lbl", "status-alert-val"]}}},
        },
        {
            "id": "status-content",
            "component": {
                "Column": {
                    "children": {
                        "explicitList": [
                            "status-title",
                            "status-outcome-val",
                            "status-d1",
                            "status-align-row",
                            "status-capital-row",
                            "status-ticks-row",
                            "status-d2",
                            "status-bs-row",
                            "status-alert-row",
                        ]
                    }
                }
            },
        },
        {
            "id": "card-status",
            "component": {"Card": {"child": "status-content", "expandable": {"literalBoolean": True}}},
        },
    ]
    components.extend(status_components)

    # === Root ===
    components.append(
        {
            "id": "root-col",
            "component": {"Column": {"children": {"explicitList": ["card-main", "card-strategy", "card-status", "card-coaching"]}}},
        }
    )

    surface_update = {
        "surfaceUpdate": {
            "surfaceId": "sim_results",
            "components": components,
        }
    }
    begin_rendering = {
        "beginRendering": {
            "surfaceId": "sim_results",
            "root": "root-col",
        }
    }

    session_id = tool_context.session.id
    origin = {"type": "agent", "id": "consultant", "session_id": session_id}
    destination = [session_id]

    # Track emit step so a mid-pair failure is reported as orphan-surface
    # (the partial-failure case) rather than swallowed.
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
    except Exception as e:
        if _emit_step == "beginRendering":
            logger.error(
                "emit_sim_results_a2ui: partial emit for session %s — "
                "surfaceUpdate landed but beginRendering failed (%s). "
                "Frontend will hold an orphaned surface until session reset.",
                session_id,
                e,
                exc_info=True,
            )
        else:
            logger.error(
                "emit_sim_results_a2ui: surfaceUpdate emit failed for session %s: %s",
                session_id,
                e,
                exc_info=True,
            )
        raise

    logger.info("Emitted Simulation Results A2UI for session %s (score=%s)", session_id, overall_score)

    return "SUCCESS: The Simulation Results dashboard has been emitted to the UI. You MUST now call emit_action_plan_a2ui to complete the 90-day action plan."
