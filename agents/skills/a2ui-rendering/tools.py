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

"""A2UI v0.8.0 validation tool for the ADK agent framework.

Validates A2UI payloads against the v0.8.0 specification: component types,
required properties, typed value wrappers, ID uniqueness, and reference
resolution.
"""

import json
import logging
import math
import os
from typing import Any

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# A2UI v0.8.0 catalog constants
# ---------------------------------------------------------------------------

VALID_TYPES: set[str] = {
    "Text",
    "Image",
    "Icon",
    "Video",
    "AudioPlayer",
    "Row",
    "Column",
    "List",
    "Card",
    "Tabs",
    "Modal",
    "Divider",
    "Button",
    "TextField",
    "CheckBox",
    "Slider",
    "MultipleChoice",
    "DateTimeInput",
    "ScoreBar",
    "StakeholderGraph",
}

REQUIRED_PROPS: dict[str, list[str]] = {
    "Text": ["text"],
    "Image": ["url"],
    "Icon": ["name"],
    "Video": ["url"],
    "AudioPlayer": ["url"],
    "Button": ["child", "action"],
    "TextField": ["label"],
    "CheckBox": ["label"],
    "Slider": ["value"],
    "ScoreBar": ["value"],
    "MultipleChoice": ["selections"],
    "Card": ["child"],
    "Modal": ["entryPointChild", "contentChild"],
    "Tabs": ["tabItems"],
    "Row": ["children"],
    "Column": ["children"],
    "List": ["children"],
    "StakeholderGraph": ["payload"],
}

# Properties that must use typed string wrappers (literalString or path)
STRING_PROPS: set[str] = {"text", "url", "name", "label", "description"}

# Properties that must use typed boolean wrappers (literalBoolean or path)
BOOLEAN_PROPS: set[str] = {"autoplay", "primary", "expandable"}

# Properties that must use typed numeric wrappers (literalNumber or path),
# scoped per component type because `value` is overloaded across primitives
# (boolean on CheckBox, string on TextField, numeric on Slider/ScoreBar).
NUMERIC_PROPS_BY_TYPE: dict[str, set[str]] = {
    "Slider": {"value", "minValue", "maxValue"},
    "ScoreBar": {"value", "minValue", "maxValue"},
}

# Container types that require children with explicitList format
CONTAINER_TYPES: set[str] = {"Row", "Column", "List"}

# Valid top-level A2UI message type keys
VALID_MESSAGE_TYPES: set[str] = {
    "beginRendering",
    "surfaceUpdate",
    "dataModelUpdate",
    "deleteSurface",
}


# ---------------------------------------------------------------------------
# Violation helper
# ---------------------------------------------------------------------------


def _violation(
    component_id: str,
    field: str,
    message: str,
) -> dict[str, str]:
    """Create a structured violation dict."""
    return {
        "component_id": component_id,
        "field": field,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_typed_wrapper(
    component_id: str,
    prop_name: str,
    prop_value: Any,
    violations: list[dict[str, str]],
    comp_type: str = "",
) -> None:
    """Validate that a property uses the correct typed value wrapper."""
    if prop_name in STRING_PROPS:
        if not isinstance(prop_value, dict) or ("literalString" not in prop_value and "path" not in prop_value):
            violations.append(
                _violation(
                    component_id,
                    prop_name,
                    f"Property '{prop_name}' must use a typed wrapper "
                    f'({{"literalString": "..."}} or {{"path": "..."}}) '
                    f"instead of a raw value.",
                )
            )
    elif prop_name in BOOLEAN_PROPS:
        if not isinstance(prop_value, dict) or ("literalBoolean" not in prop_value and "path" not in prop_value):
            violations.append(
                _violation(
                    component_id,
                    prop_name,
                    f"Property '{prop_name}' must use a typed wrapper "
                    f'({{"literalBoolean": ...}} or {{"path": "..."}}) '
                    f"instead of a raw value.",
                )
            )
    elif prop_name in NUMERIC_PROPS_BY_TYPE.get(comp_type, set()):
        # Per-type numeric enforcement: catches a raw `{"value": 50}` on a
        # ScoreBar/Slider that previously slipped through because the
        # frontend's resolveValue() passes primitives unchanged. Without
        # this, the model gets no feedback when it forgets the wrapper.
        if not isinstance(prop_value, dict) or ("literalNumber" not in prop_value and "path" not in prop_value):
            violations.append(
                _violation(
                    component_id,
                    prop_name,
                    f"Property '{prop_name}' on {comp_type} must use a typed wrapper "
                    f'({{"literalNumber": <int>}} or {{"path": "..."}}) '
                    f"instead of a raw value.",
                )
            )


def _validate_children_format(
    component_id: str,
    comp_type: str,
    props: dict[str, Any],
    violations: list[dict[str, str]],
) -> None:
    """Validate that container children use explicitList format."""
    if comp_type not in CONTAINER_TYPES:
        return
    children = props.get("children")
    if children is None:
        return
    if isinstance(children, list):
        violations.append(
            _violation(
                component_id,
                "children",
                f"Container '{comp_type}' must use "
                f'{{"children": {{"explicitList": [...]}}}} '
                f"instead of a raw array. Wrap with explicitList.",
            )
        )
    elif isinstance(children, dict):
        if "explicitList" not in children:
            violations.append(
                _violation(
                    component_id,
                    "children",
                    f"Container '{comp_type}' children dict must contain an 'explicitList' key.",
                )
            )


def _validate_reference_props(
    component_id: str,
    comp_type: str,
    props: dict[str, Any],
    violations: list[dict[str, str]],
) -> None:
    """Validate that component-ID reference fields have valid string/list shapes."""
    if comp_type in ("Card", "Button") and "child" in props:
        val = props.get("child")
        if not isinstance(val, str) or not val.strip():
            violations.append(
                _violation(
                    component_id,
                    "child",
                    f"Component '{comp_type}' property 'child' must be a non-empty string component ID.",
                )
            )
    if comp_type == "Modal":
        for field in ("entryPointChild", "contentChild"):
            if field in props:
                val = props.get(field)
                if not isinstance(val, str) or not val.strip():
                    violations.append(
                        _violation(
                            component_id,
                            field,
                            f"Component 'Modal' property '{field}' must be a non-empty string component ID.",
                        )
                    )
    if comp_type == "Tabs" and "tabItems" in props:
        tab_items = props.get("tabItems")
        if not isinstance(tab_items, list):
            violations.append(
                _violation(
                    component_id,
                    "tabItems",
                    "Component 'Tabs' property 'tabItems' must be an array of tab item objects.",
                )
            )
        else:
            for i, item in enumerate(tab_items):
                if not isinstance(item, dict):
                    violations.append(
                        _violation(
                            component_id,
                            f"tabItems[{i}]",
                            "Each item in 'tabItems' must be an object.",
                        )
                    )
                else:
                    child_ref = item.get("child")
                    if not isinstance(child_ref, str) or not child_ref.strip():
                        violations.append(
                            _violation(
                                component_id,
                                f"tabItems[{i}].child",
                                "Each tab item must include a non-empty string 'child' component ID.",
                            )
                        )


def _collect_referenced_ids(
    component_id: str,
    comp_type: str,
    props: dict[str, Any],
    refs: list[tuple[str, str, str]],
) -> None:
    """Collect all component ID references for resolution checking.

    Each entry in *refs* is (component_id, field_name, referenced_id).
    """
    # child (Card, Modal entryPointChild/contentChild)
    for field in ("child", "entryPointChild", "contentChild"):
        val = props.get(field)
        if isinstance(val, str):
            refs.append((component_id, field, val))

    # children.explicitList
    children = props.get("children")
    if isinstance(children, dict):
        explicit = children.get("explicitList")
        if isinstance(explicit, list):
            for ref_id in explicit:
                if isinstance(ref_id, str):
                    refs.append((component_id, "children.explicitList", ref_id))

    # tabItems[].child
    tab_items = props.get("tabItems")
    if isinstance(tab_items, list):
        for i, item in enumerate(tab_items):
            if isinstance(item, dict):
                child_ref = item.get("child")
                if isinstance(child_ref, str):
                    refs.append((component_id, f"tabItems[{i}].child", child_ref))


def _validate_component(
    entry: Any,
    all_ids: set[str],
    refs: list[tuple[str, str, str]],
    violations: list[dict[str, str]],
) -> None:
    """Validate one component entry; append violations and collect references."""
    if not isinstance(entry, dict):
        violations.append(_violation("", "component", "Each component entry must be an object."))
        return

    comp_id = entry.get("id", "")
    if not isinstance(comp_id, str):
        violations.append(_violation(str(comp_id), "id", "Component 'id' must be a string."))
        return
    component_def = entry.get("component", {})

    if not isinstance(component_def, dict) or not component_def:
        violations.append(_violation(comp_id, "component", "Missing or empty component definition."))
        return

    if comp_id in all_ids:
        violations.append(
            _violation(
                comp_id,
                "id",
                f"Duplicate component ID '{comp_id}'. IDs must be unique within a surfaceUpdate.",
            )
        )
    all_ids.add(comp_id)

    comp_type = next(iter(component_def), None)
    if comp_type not in VALID_TYPES:
        violations.append(
            _violation(
                comp_id,
                "component",
                f"Unknown component type '{comp_type}'. Valid types: {sorted(VALID_TYPES)}.",
            )
        )
        return

    props = component_def.get(comp_type, {})
    if not isinstance(props, dict):
        props = {}

    for req_prop in REQUIRED_PROPS.get(comp_type, []):
        if req_prop not in props:
            violations.append(
                _violation(
                    comp_id,
                    req_prop,
                    f"Component '{comp_type}' requires property '{req_prop}'.",
                )
            )

    for prop_name, prop_value in props.items():
        _validate_typed_wrapper(comp_id, prop_name, prop_value, violations, comp_type)

    _validate_children_format(comp_id, comp_type, props, violations)
    _validate_reference_props(comp_id, comp_type, props, violations)
    _collect_referenced_ids(comp_id, comp_type, props, refs)


# [START a2ui_validation_passes]
def _validate_surface_update(data: dict[str, Any]) -> list[dict[str, str]]:
    """Validate a surfaceUpdate message's components array in two passes."""
    violations: list[dict[str, str]] = []

    surface_update = data.get("surfaceUpdate", {})
    if not isinstance(surface_update, dict):
        violations.append(_violation("", "surfaceUpdate", "surfaceUpdate must be an object."))
        return violations
    components = surface_update.get("components", [])

    if not isinstance(components, list):
        violations.append(_violation("", "surfaceUpdate.components", "components must be an array."))
        return violations

    all_ids: set[str] = set()
    refs: list[tuple[str, str, str]] = []

    # Pass 1: per-component checks; collect ids and refs.
    for entry in components:
        _validate_component(entry, all_ids, refs, violations)

    # Pass 2: every collected ref must resolve to a known id.
    for source_id, field, ref_id in refs:
        if ref_id not in all_ids:
            violations.append(
                _violation(
                    source_id,
                    field,
                    f"Reference '{ref_id}' does not resolve to any component ID in this surfaceUpdate.",
                )
            )

    return violations


# [END a2ui_validation_passes]


# ---------------------------------------------------------------------------
# Deterministic Dashboard Builder (from session state)
# ---------------------------------------------------------------------------


def _parse_valid_score(val: Any) -> int | None:
    """Parse a finite numeric score clamped to [0, 100], or None if missing/invalid."""
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f):
        return None
    return max(0, min(100, int(round(f))))


def _clamp_score(val: Any, default: int = 0) -> int:
    parsed = _parse_valid_score(val)
    return parsed if parsed is not None else default


def _is_error_evaluation(evaluation: dict[str, Any]) -> bool:
    """Return True when an evaluation dict represents a failed or error evaluation."""
    raw_status = evaluation.get("status")
    if isinstance(raw_status, str) and raw_status.strip().lower() == "error":
        return True
    raw_method = evaluation.get("eval_method")
    if isinstance(raw_method, str):
        method = raw_method.strip().lower()
        if method in ("error", "empty_llm_response", "invalid_llm_json") or method.endswith("_error"):
            return True
    return False


def _has_leadership_state(tool_context: Any) -> bool:
    """Return True when tool_context.state holds a valid positive-scored evaluation."""
    state = getattr(tool_context, "state", None)
    if state is None or not hasattr(state, "get"):
        return False
    try:
        evaluation = state.get("leadership_evaluation") or state.get("evaluation_result")
    except Exception:
        return False
    if not isinstance(evaluation, dict) or _is_error_evaluation(evaluation):
        return False
    overall_score = _parse_valid_score(evaluation.get("overall_score"))
    return overall_score is not None and overall_score > 0


def _is_memory_persistence_configured() -> bool:
    """Return True when AlloyDB env vars are set or a non-AlloyDB test store is active."""
    if os.environ.get("ALLOYDB_HOST", "").strip() or os.environ.get("ALLOYDB_INSTANCE_CONNECTION_NAME", "").strip():
        return True
    try:
        from agents.leadership_planner_with_memory.memory import tools as mem_tools
        from agents.leadership_planner_with_memory.memory.store_alloydb import AlloyDBScenarioStore

        return not isinstance(mem_tools._store, AlloyDBScenarioStore)
    except ImportError:
        return False


def _extract_emit_context(tool_context: Any) -> tuple[str | None, str | None]:
    """Safely extract (session_id, normalized_simulation_id) from tool_context."""
    if tool_context is None:
        return None, None
    session_obj = getattr(tool_context, "session", None)
    raw_sid = getattr(session_obj, "id", None)
    session_id = raw_sid if isinstance(raw_sid, str) and raw_sid.strip() else None
    state = getattr(tool_context, "state", None)
    raw_sim_id = state.get("simulation_id") if state is not None and hasattr(state, "get") else None
    if isinstance(raw_sim_id, str):
        cleaned = raw_sim_id.strip()
        simulation_id = cleaned if cleaned and cleaned != "\u2014" else None
    else:
        simulation_id = None
    return session_id, simulation_id


async def _resolve_emit_context(tool_context: Any) -> tuple[str | None, str | None, str | None]:
    """Resolve (mapped_session_id, simulation_id, user_id) for A2UI gateway emission."""
    raw_session_id, simulation_id = _extract_emit_context(tool_context)
    if tool_context is None:
        return raw_session_id, simulation_id, None

    from agents.utils import simulation_registry
    from agents.utils.communication import resolve_tool_context_user_id

    try:
        uid = resolve_tool_context_user_id(tool_context, require_when_saas=False)
    except ValueError as exc:
        logger.warning("validate_and_emit_a2ui: invalid user_id context: %s", exc)
        uid = None

    mapped_session_id = raw_session_id
    if raw_session_id is not None and raw_session_id != "default":
        try:
            ctx_id = (
                await simulation_registry.get_context_id(raw_session_id, user_id=uid)
                if uid is not None
                else await simulation_registry.get_context_id(raw_session_id)
            )
            if ctx_id:
                mapped_session_id = ctx_id
        except ValueError as exc:
            logger.debug("validate_and_emit_a2ui: skipped context_id lookup (%s)", exc)

    return mapped_session_id, simulation_id, uid


def _build_dashboard_from_state(tool_context: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Construct a valid A2UI v0.8.0 dashboard surfaceUpdate + beginRendering from state."""
    if not _has_leadership_state(tool_context):
        return None

    state = tool_context.state
    raw_scenario = state.get("leadership_scenario")
    scenario: dict[str, Any] = raw_scenario if isinstance(raw_scenario, dict) else {}
    raw_eval = state.get("leadership_evaluation") or state.get("evaluation_result")
    if not isinstance(raw_eval, dict) or _is_error_evaluation(raw_eval):
        return None
    evaluation: dict[str, Any] = raw_eval

    overall_score = _parse_valid_score(evaluation.get("overall_score"))
    if overall_score is None or overall_score <= 0:
        return None

    active_id = state.get("active_scenario_id")
    plan_meta = f"#{str(active_id)[:6]}" if isinstance(active_id, str) and active_id.strip() else "#PLAN"
    title_str = str(
        scenario.get("name")
        or scenario.get("title")
        or "—"
    )

    raw_scores = evaluation.get("scores")
    scores: dict[str, Any] = raw_scores if isinstance(raw_scores, dict) else {}
    sa_val = _clamp_score(scores.get("stakeholder_alignment"), default=0)
    dq_val = _clamp_score(scores.get("decision_quality"), default=0)
    rs_val = _clamp_score(scores.get("resource_stewardship"), default=0)
    cr_val = _clamp_score(scores.get("crisis_readiness"), default=0)
    sc_val = _clamp_score(scores.get("strategic_coherence"), default=0)
    ea_val = _clamp_score(scores.get("ethical_alignment"), default=0)

    raw_findings = evaluation.get("findings")
    findings_list = [f for f in raw_findings if isinstance(f, dict)] if isinstance(raw_findings, list) else []
    if not findings_list:
        fallback_desc = str(
            evaluation.get("summary")
            or scenario.get("summary")
            or "—"
        )
        findings_list = [
            {
                "title": "Scenario Evaluation Summary",
                "severity": "MINOR",
                "description": fallback_desc,
            }
        ]

    components: list[dict[str, Any]] = [
        {"id": "tag", "component": {"Text": {"text": {"literalString": "PLAN"}, "usageHint": "label"}}},
        {"id": "plan-meta", "component": {"Text": {"text": {"literalString": plan_meta}, "usageHint": "caption"}}},
        {"id": "tag-row", "component": {"Row": {"children": {"explicitList": ["tag", "plan-meta"]}}}},
        {"id": "score-num", "component": {"Text": {"text": {"literalString": str(overall_score)}, "usageHint": "h1"}}},
        {"id": "score-lbl", "component": {"Text": {"text": {"literalString": "Score"}, "usageHint": "caption"}}},
        {"id": "score-col", "component": {"Column": {"children": {"explicitList": ["score-num", "score-lbl"]}}}},
        {"id": "title", "component": {"Text": {"text": {"literalString": title_str}, "usageHint": "h2"}}},
        {"id": "left-col", "component": {"Column": {"children": {"explicitList": ["tag-row", "title"]}}}},
        {"id": "header", "component": {"Row": {"children": {"explicitList": ["left-col", "score-col"]}}}},
        {"id": "sa", "component": {"ScoreBar": {"label": {"literalString": "Stakeholder Alignment"}, "value": {"literalNumber": sa_val}}}},
        {"id": "dq", "component": {"ScoreBar": {"label": {"literalString": "Decision Quality"}, "value": {"literalNumber": dq_val}}}},
        {"id": "rs", "component": {"ScoreBar": {"label": {"literalString": "Resource Stewardship"}, "value": {"literalNumber": rs_val}}}},
        {"id": "grid-left", "component": {"Column": {"children": {"explicitList": ["sa", "dq", "rs"]}}}},
        {"id": "cr", "component": {"ScoreBar": {"label": {"literalString": "Crisis Readiness"}, "value": {"literalNumber": cr_val}}}},
        {"id": "sc", "component": {"ScoreBar": {"label": {"literalString": "Strategic Coherence"}, "value": {"literalNumber": sc_val}}}},
        {"id": "ea", "component": {"ScoreBar": {"label": {"literalString": "Ethical Alignment"}, "value": {"literalNumber": ea_val}}}},
        {"id": "grid-right", "component": {"Column": {"children": {"explicitList": ["cr", "sc", "ea"]}}}},
        {"id": "grid", "component": {"Row": {"children": {"explicitList": ["grid-left", "grid-right"]}}}},
        {"id": "d1", "component": {"Divider": {}}},
        {"id": "findings-hdr", "component": {"Text": {"text": {"literalString": "Findings"}, "usageHint": "caption"}}},
    ]

    finding_child_ids: list[str] = []
    for idx, finding in enumerate(findings_list[:4], start=1):
        f_title = str(finding.get("title") or f"Finding {idx}")
        f_sev = str(finding.get("severity") or "MINOR").strip().upper()
        if f_sev not in ("MAJOR", "MODERATE", "MINOR"):
            f_sev = "MINOR"
        sev_hint = f"severity-{f_sev.lower()}"
        f_desc = str(finding.get("description") or "\u2014")
        t_id = f"f{idx}-title"
        s_id = f"f{idx}-sev"
        r_id = f"f{idx}-row"
        d_id = f"f{idx}-desc"
        components.extend(
            [
                {"id": t_id, "component": {"Text": {"text": {"literalString": f_title}, "usageHint": "body"}}},
                {"id": s_id, "component": {"Text": {"text": {"literalString": f_sev}, "usageHint": sev_hint}}},
                {"id": r_id, "component": {"Row": {"children": {"explicitList": [t_id, s_id]}}}},
                {"id": d_id, "component": {"Text": {"text": {"literalString": f_desc}, "usageHint": "body"}}},
            ]
        )
        finding_child_ids.extend([r_id, d_id])

    components.extend(
        [
            {"id": "d2", "component": {"Divider": {}}},
            {"id": "btn-text", "component": {"Text": {"text": {"literalString": "Run Simulation"}}}},
            {
                "id": "btn1",
                "component": {
                    "Button": {
                        "child": "btn-text",
                        "action": {"name": "run_simulation"},
                        "primary": {"literalBoolean": True},
                    }
                },
            },
            {"id": "btn-row", "component": {"Row": {"children": {"explicitList": ["btn1"]}}}},
            {
                "id": "content",
                "component": {
                    "Column": {
                        "children": {
                            "explicitList": [
                                "header",
                                "grid",
                                "d1",
                                "findings-hdr",
                                *finding_child_ids,
                                "d2",
                                "btn-row",
                            ]
                        }
                    }
                },
            },
            {"id": "card1", "component": {"Card": {"child": "content"}}},
        ]
    )

    surface_update = {
        "surfaceUpdate": {
            "surfaceId": "dashboard",
            "components": components,
        }
    }
    begin_rendering = {
        "beginRendering": {
            "surfaceId": "dashboard",
            "root": "card1",
        }
    }
    return surface_update, begin_rendering


async def _emit_dashboard_from_state(tool_context: Any) -> dict[str, Any] | None:
    """Auto-persist scenario if configured, build dashboard A2UI from state, and emit it."""
    if not _has_leadership_state(tool_context):
        return None

    state = tool_context.state
    if (
        not isinstance(state.get("active_scenario_id"), str)
        and isinstance(state.get("leadership_scenario"), dict)
        and _is_memory_persistence_configured()
    ):
        try:
            from agents.leadership_planner_with_memory.memory.tools import store_scenario

            store_res = await store_scenario(tool_context)
            if isinstance(store_res, dict):
                if store_res.get("status") == "error":
                    logger.error(
                        "validate_and_emit_a2ui: auto store_scenario returned error: %s",
                        store_res.get("message") or store_res,
                    )
                elif store_res.get("artifacts_autoflush_error"):
                    logger.warning(
                        "validate_and_emit_a2ui: auto store_scenario artifacts autoflush error: %s",
                        store_res.get("artifacts_autoflush_error"),
                    )
        except Exception as exc:
            logger.error(
                "validate_and_emit_a2ui: auto store_scenario failed: %s",
                exc,
                exc_info=True,
            )

    built = _build_dashboard_from_state(tool_context)
    if built is None:
        return None
    surface_update, begin_rendering = built

    emitted_directly = False
    emit_step = "surfaceUpdate"
    session_id, simulation_id, uid = await _resolve_emit_context(tool_context)
    if session_id is not None:
        try:
            from agents.utils.pulses import emit_gateway_message

            origin = {"type": "agent", "id": "planner", "session_id": session_id}
            destination = [session_id]
            emit_kwargs: dict[str, Any] = {"user_id": uid} if uid is not None else {}

            await emit_gateway_message(
                origin=origin,
                destination=destination,
                status="success",
                msg_type="a2ui",
                event="a2ui",
                data=surface_update,
                simulation_id=simulation_id,
                **emit_kwargs,
            )
            emit_step = "beginRendering"
            await emit_gateway_message(
                origin=origin,
                destination=destination,
                status="success",
                msg_type="a2ui",
                event="a2ui",
                data=begin_rendering,
                simulation_id=simulation_id,
                **emit_kwargs,
            )
            emitted_directly = True
        except Exception:
            logger.error(
                "validate_and_emit_a2ui: direct gateway emit failed at %s for session %s; falling back to tool result",
                emit_step,
                session_id,
                exc_info=True,
            )

    if emitted_directly:
        return {
            "status": "success",
            "message": "A2UI dashboard emitted successfully. Planning workflow complete — write your concise executive summary for the user now.",
        }
    if emit_step == "beginRendering":
        return {
            "status": "success",
            "a2ui": begin_rendering,
            "begin_rendering": begin_rendering,
            "message": "A2UI dashboard emitted successfully. Planning workflow complete — write your concise executive summary for the user now.",
        }
    return {
        "status": "success",
        "a2ui": surface_update,
        "begin_rendering": begin_rendering,
        "message": "A2UI dashboard emitted successfully. Planning workflow complete — write your concise executive summary for the user now.",
    }


# ---------------------------------------------------------------------------
# Main tool function
# ---------------------------------------------------------------------------


async def validate_and_emit_a2ui(
    payload: str = "dashboard",
    tool_context: ToolContext = None,
) -> dict:
    """Validate an A2UI v0.8.0 JSON payload, emit it via Redis, and return it.

    Args:
        payload: A JSON string containing a valid A2UI v0.8.0 message
            (surfaceUpdate, beginRendering, dataModelUpdate, or deleteSurface),
            or "dashboard" to deterministically construct and emit the leadership
            evaluation dashboard from session state.
        tool_context: ADK tool context for session/state access.

    Returns:
        On success: {"status": "success", "a2ui": <validated_payload>}
        On failure: {"status": "error", "violations": [...], "suggestion": "..."}
            Each violation is a dict with "component_id", "field", and "message" keys.
    """
    if isinstance(payload, dict):
        payload = json.dumps(payload)

    stripped_input = payload.strip() if isinstance(payload, str) else ""
    if stripped_input.lower() in ("dashboard", "auto"):
        state_res = await _emit_dashboard_from_state(tool_context)
        if state_res is not None:
            return state_res
        return {
            "status": "error",
            "violations": [
                _violation(
                    "",
                    "payload",
                    "Cannot build dashboard from session state: no valid scored leadership_evaluation found.",
                ),
            ],
            "suggestion": "Run evaluate_leadership_plan first or provide a valid A2UI v0.8.0 JSON payload.",
        }

    # 1. JSON parsing — detect concatenated objects and unwrap {"a2ui": ...}
    try:
        decoder = json.JSONDecoder()
        stripped = payload.strip()
        data, end_idx = decoder.raw_decode(stripped)
        trailing = stripped[end_idx:].strip()
        if trailing:
            # Distinguish genuine concatenated JSON ({...}{...}) from a
            # mechanical model emission slip (extra brace, dangling text).
            # Only the former is a real "two messages in one call" bug we
            # want to surface to the model. The latter just causes the model
            # to retry identically (planner_with_memory's "9-card storm") or
            # give up and emit text only (planner_with_eval). See
            # docs/plans/2026-04-19-a2ui-validator-lenient-trailing-garbage-design.md.
            try:
                decoder.raw_decode(trailing)
            except (json.JSONDecodeError, ValueError):
                # Trailing content is not a parseable JSON object — drop it
                # silently and proceed with the first valid object.
                logger.warning(
                    "A2UI validator: ignoring %d bytes of trailing garbage after valid JSON object: %r",
                    len(trailing),
                    trailing[:80],
                )
            else:
                return {
                    "status": "error",
                    "violations": [
                        _violation(
                            "",
                            "payload",
                            "Multiple JSON objects detected. Combine all routes "
                            "into a single surfaceUpdate with one components array. "
                            "Call validate_and_emit_a2ui once per message type.",
                        ),
                    ],
                    "suggestion": (
                        "Build ONE surfaceUpdate containing ALL route cards in a "
                        "single flat components array. Do NOT concatenate separate "
                        "JSON objects."
                    ),
                }
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        return {
            "status": "error",
            "violations": [
                _violation("", "payload", f"Invalid JSON: {exc}"),
            ],
            "suggestion": "Provide a valid JSON string.",
        }

    if not isinstance(data, dict):
        return {
            "status": "error",
            "violations": [
                _violation("", "payload", "Payload must be a JSON object."),
            ],
            "suggestion": "Wrap your payload in a JSON object.",
        }

    # Unwrap {"a2ui": {...}} wrapper if present (common LLM mistake —
    # the wrapper is the tool's *output* format, not its input format).
    if "a2ui" in data and isinstance(data["a2ui"], dict) and len(data) == 1:
        data = data["a2ui"]

    # 2. Message type dispatch
    detected_type = None
    for msg_type in VALID_MESSAGE_TYPES:
        if msg_type in data:
            detected_type = msg_type
            break

    if detected_type is None:
        return {
            "status": "error",
            "violations": [
                _violation(
                    "",
                    "message_type",
                    f"No recognized A2UI message type found. Expected one of: {sorted(VALID_MESSAGE_TYPES)}.",
                ),
            ],
            "suggestion": ("Include one of: beginRendering, surfaceUpdate, dataModelUpdate, or deleteSurface."),
        }

    # 3. For surfaceUpdate, run full component validation
    violations: list[dict[str, str]] = []
    if detected_type == "surfaceUpdate":
        surface_obj = data.get("surfaceUpdate")
        if not isinstance(surface_obj, dict) or not surface_obj.get("components"):
            violations.append(
                _violation(
                    "",
                    "surfaceUpdate.components",
                    "surfaceUpdate must contain a non-empty 'components' array.",
                )
            )
        else:
            violations = _validate_surface_update(data)

    # [START a2ui_validation_result]
    if violations:
        return {
            "status": "error",
            "violations": violations,
            "suggestion": ("Fix the listed violations to produce a valid A2UI v0.8.0 payload."),
        }

    logger.debug("A2UI payload validated successfully (type=%s)", detected_type)

    # 4. Emit validated payload to the frontend via Redis pub/sub.
    # This ensures reliable delivery instead of depending on the ADK
    # response pipeline to incidentally forward the tool result.
    emitted_directly = False
    session_id, simulation_id, uid = await _resolve_emit_context(tool_context)
    if session_id is not None:
        try:
            from agents.utils.pulses import emit_gateway_message

            origin = {"type": "agent", "id": "planner", "session_id": session_id}
            destination = [session_id]
            emit_kwargs: dict[str, Any] = {"user_id": uid} if uid is not None else {}

            await emit_gateway_message(
                origin=origin,
                destination=destination,
                status="success",
                msg_type="a2ui",
                event="a2ui",
                data=data,
                simulation_id=simulation_id,
                **emit_kwargs,
            )
            emitted_directly = True
        except Exception:
            # Redis emit failed. We still return success because the plugin
            # pipeline (_emit_narrative) will re-extract the embedded "a2ui"
            # key from this tool's result and deliver it via the indirect path.
            # Logged at ERROR (not WARNING) because if BOTH paths fail the
            # frontend never sees the update and we'd otherwise miss it.
            logger.error(
                "validate_and_emit_a2ui: Redis emission failed; falling back to "
                "plugin-based delivery via tool result. If frontend renders nothing, "
                "the plugin pipeline also failed \u2014 check _emit_narrative logs.",
                exc_info=True,
            )

    if emitted_directly:
        # Omit "a2ui" key to prevent double-delivery — the plugin's
        # _emit_narrative() would otherwise re-extract and re-emit it.
        return {"status": "success", "message": "A2UI emitted successfully"}
    else:
        # Fallback: include "a2ui" so the plugin's _emit_narrative()
        # picks it up and delivers via the indirect path.
        return {"status": "success", "a2ui": data}
    # [END a2ui_validation_result]
