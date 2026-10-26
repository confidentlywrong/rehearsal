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

"""Scenario design tools for the Leadership Planner agent.

Mirrors agents/planner/skills/gis-spatial-engineering/scripts/tools.py
but generates leadership simulation scenarios instead of marathon routes.

Output format is QuestConfig-compatible (see agents/simulator/quest_loader.py)
so the simulator can consume it directly via inject_quest_into_state().
"""

import logging
import uuid
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The 7 Leadership Principles (canonical order — must match quest_loader.py)
# ---------------------------------------------------------------------------

LEADERSHIP_PRINCIPLES = [
    "Strategic Foresight & Vision Cascading",
    "Adaptive Decision-Making & Agility",
    "Stakeholder Engagement & Inclusive Communication",
    "Ethical Leadership & Governance",
    "Empowerment & Cultivating Distributed Leadership",
    "Organizational Resilience & Change Agility",
    "Resource Optimization & Strategic Allocation",
]

# ---------------------------------------------------------------------------
# Echo persona templates by scenario type
# Output format: QuestConfig EchoConfig schema
#
# Reference: configs/quests/ai_powered_disruptor.yaml for affinity vector
# calibration and persona writing style.
# ---------------------------------------------------------------------------

_ECHO_TEMPLATES: Dict[str, List[Dict[str, Any]]] = {
    "transformation": [
        {
            "role": "cto",
            "tier": 1,
            "persona": (
                "Visionary CTO who champions emerging technology but often underestimates "
                "organizational resistance. Pushes for aggressive AI adoption timelines "
                "and views every delay as a competitive threat."
            ),
            "domain_principles": [
                "Strategic Foresight & Vision Cascading",
                "Adaptive Decision-Making & Agility",
            ],
            "affinity": [0.9, 0.8, 0.3, 0.4, 0.3, 0.5, 0.5],
            "initial_sentiment": 0.8,
            "initial_resistance": 0.1,
            "initial_influence": 0.7,
        },
        {
            "role": "head_ai_ethics",
            "tier": 1,
            "persona": (
                "Principled AI Ethics Lead who advocates for responsible innovation. "
                "Insists on thorough bias audits, transparency reports, and stakeholder "
                "consent before any AI deployment."
            ),
            "domain_principles": ["Ethical Leadership & Governance"],
            "affinity": [0.5, 0.4, 0.7, 0.95, 0.6, 0.5, 0.3],
            "initial_sentiment": 0.7,
            "initial_resistance": 0.2,
            "initial_influence": 0.6,
        },
        {
            "role": "cfo",
            "tier": 2,
            "persona": (
                "Conservative CFO laser-focused on ROI and fiscal discipline. Demands "
                "rigorous cost-benefit analysis for every initiative. Controls budget "
                "allocation authority."
            ),
            "domain_principles": ["Resource Optimization & Strategic Allocation"],
            "affinity": [0.3, 0.5, 0.4, 0.6, 0.2, 0.3, 0.9],
            "initial_sentiment": 0.4,
            "initial_resistance": 0.5,
            "initial_influence": 0.8,
        },
        {
            "role": "hr_director",
            "tier": 2,
            "persona": (
                "Empathetic HR Director focused on workforce development and retention. "
                "Concerned about AI displacing employees without adequate reskilling. "
                "Champions inclusive communication and transparent change management."
            ),
            "domain_principles": [
                "Empowerment & Cultivating Distributed Leadership",
                "Stakeholder Engagement & Inclusive Communication",
            ],
            "affinity": [0.3, 0.4, 0.8, 0.6, 0.9, 0.7, 0.3],
            "initial_sentiment": 0.5,
            "initial_resistance": 0.4,
            "initial_influence": 0.6,
        },
        {
            "role": "vp_operations",
            "tier": 2,
            "persona": (
                "Pragmatic VP of Operations managing legacy system dependencies. "
                "Supports AI adoption but demands realistic integration timelines. "
                "Raises operational risks executives tend to overlook."
            ),
            "domain_principles": [
                "Organizational Resilience & Change Agility",
                "Adaptive Decision-Making & Agility",
            ],
            "affinity": [0.4, 0.7, 0.5, 0.5, 0.4, 0.9, 0.6],
            "initial_sentiment": 0.5,
            "initial_resistance": 0.3,
            "initial_influence": 0.5,
        },
        {
            "role": "board_member",
            "tier": 2,
            "persona": (
                "Influential board member representing shareholder interests. Demands "
                "quarterly progress reports and measurable KPIs. Withdraws support "
                "rapidly if execution stumbles."
            ),
            "domain_principles": [
                "Strategic Foresight & Vision Cascading",
                "Resource Optimization & Strategic Allocation",
            ],
            "affinity": [0.8, 0.6, 0.4, 0.5, 0.3, 0.5, 0.8],
            "initial_sentiment": 0.6,
            "initial_resistance": 0.3,
            "initial_influence": 0.9,
            "has_hidden_agenda": True,
            "agenda_trigger_tick": 20,
        },
        {
            "role": "union_rep",
            "tier": 3,
            "persona": (
                "Protective union representative advocating for workforce rights. "
                "Concerned about job displacement, surveillance, and algorithmic "
                "management. Will organize resistance if workers feel unheard."
            ),
            "domain_principles": [
                "Empowerment & Cultivating Distributed Leadership",
                "Ethical Leadership & Governance",
            ],
            "affinity": [0.2, 0.3, 0.5, 0.7, 0.9, 0.6, 0.2],
            "initial_sentiment": 0.3,
            "initial_resistance": 0.7,
            "initial_influence": 0.5,
        },
        {
            "role": "data_scientist_lead",
            "tier": 3,
            "persona": (
                "Senior Data Scientist excited about AI potential but frustrated by "
                "organizational bottlenecks. Has critical technical skills the "
                "organization needs but is considering external offers."
            ),
            "domain_principles": ["Adaptive Decision-Making & Agility"],
            "affinity": [0.6, 0.8, 0.4, 0.5, 0.5, 0.4, 0.3],
            "initial_sentiment": 0.6,
            "initial_resistance": 0.2,
            "initial_influence": 0.4,
        },
        {
            "role": "middle_manager",
            "tier": 3,
            "persona": (
                "Mid-level manager caught between executive directives and team "
                "concerns. Worried about losing authority as AI automates decision-making "
                "in their domain."
            ),
            "domain_principles": [
                "Empowerment & Cultivating Distributed Leadership",
                "Organizational Resilience & Change Agility",
            ],
            "affinity": [0.3, 0.4, 0.6, 0.4, 0.7, 0.6, 0.4],
            "initial_sentiment": 0.4,
            "initial_resistance": 0.5,
            "initial_influence": 0.3,
        },
        {
            "role": "external_consultant",
            "tier": 3,
            "persona": (
                "External AI strategy consultant brought in for objectivity. Has "
                "industry benchmarks and best practices but lacks context on "
                "organizational culture. May be technically sound but culturally tone-deaf."
            ),
            "domain_principles": [
                "Strategic Foresight & Vision Cascading",
                "Adaptive Decision-Making & Agility",
            ],
            "affinity": [0.7, 0.7, 0.3, 0.4, 0.3, 0.5, 0.7],
            "initial_sentiment": 0.7,
            "initial_resistance": 0.1,
            "initial_influence": 0.3,
        },
    ],
    "crisis": [
        {
            "role": "ceo",
            "tier": 1,
            "persona": (
                "CEO facing existential organizational crisis. Must balance immediate "
                "survival actions with long-term strategic positioning. Under intense "
                "board and media scrutiny."
            ),
            "domain_principles": [
                "Strategic Foresight & Vision Cascading",
                "Organizational Resilience & Change Agility",
            ],
            "affinity": [0.7, 0.8, 0.6, 0.5, 0.4, 0.9, 0.6],
            "initial_sentiment": 0.3,
            "initial_resistance": 0.2,
            "initial_influence": 0.9,
        },
        {
            "role": "cfo",
            "tier": 1,
            "persona": (
                "CFO focused on cash preservation and runway extension. Demands "
                "immediate cost-cutting measures and rigorous financial controls."
            ),
            "domain_principles": ["Resource Optimization & Strategic Allocation"],
            "affinity": [0.3, 0.5, 0.3, 0.5, 0.2, 0.4, 0.95],
            "initial_sentiment": 0.2,
            "initial_resistance": 0.4,
            "initial_influence": 0.8,
        },
        {
            "role": "general_counsel",
            "tier": 2,
            "persona": (
                "General Counsel managing legal exposure and regulatory risk. "
                "Focused on liability reduction, compliance, and damage control."
            ),
            "domain_principles": ["Ethical Leadership & Governance"],
            "affinity": [0.3, 0.3, 0.5, 0.9, 0.3, 0.6, 0.4],
            "initial_sentiment": 0.2,
            "initial_resistance": 0.3,
            "initial_influence": 0.7,
        },
        {
            "role": "vp_communications",
            "tier": 2,
            "persona": (
                "VP Communications managing narrative and reputation. Advocates for "
                "transparency while balancing stakeholder panic."
            ),
            "domain_principles": ["Stakeholder Engagement & Inclusive Communication"],
            "affinity": [0.4, 0.5, 0.9, 0.6, 0.4, 0.5, 0.3],
            "initial_sentiment": 0.35,
            "initial_resistance": 0.3,
            "initial_influence": 0.6,
        },
        {
            "role": "cto",
            "tier": 2,
            "persona": (
                "CTO focused on system stability, incident response, and recovery. "
                "Needs time and resources for root cause analysis."
            ),
            "domain_principles": [
                "Adaptive Decision-Making & Agility",
                "Organizational Resilience & Change Agility",
            ],
            "affinity": [0.4, 0.7, 0.4, 0.5, 0.3, 0.8, 0.5],
            "initial_sentiment": 0.4,
            "initial_resistance": 0.2,
            "initial_influence": 0.6,
        },
    ],
    "merger": [
        {
            "role": "ceo_acquirer",
            "tier": 1,
            "persona": (
                "CEO of the acquiring company driving synergy capture and market "
                "dominance. Focused on integration speed and shareholder value."
            ),
            "domain_principles": [
                "Strategic Foresight & Vision Cascading",
                "Resource Optimization & Strategic Allocation",
            ],
            "affinity": [0.9, 0.6, 0.5, 0.4, 0.3, 0.5, 0.8],
            "initial_sentiment": 0.75,
            "initial_resistance": 0.1,
            "initial_influence": 0.9,
        },
        {
            "role": "ceo_target",
            "tier": 1,
            "persona": (
                "CEO of the acquired company fighting for team preservation, "
                "culture protection, and fair treatment of loyal employees."
            ),
            "domain_principles": [
                "Empowerment & Cultivating Distributed Leadership",
                "Stakeholder Engagement & Inclusive Communication",
            ],
            "affinity": [0.4, 0.5, 0.8, 0.6, 0.9, 0.7, 0.3],
            "initial_sentiment": 0.4,
            "initial_resistance": 0.5,
            "initial_influence": 0.7,
        },
        {
            "role": "integration_lead",
            "tier": 2,
            "persona": (
                "Integration Lead managing the PMO. Focused on timeline adherence, "
                "minimal disruption, and stakeholder alignment across both organizations."
            ),
            "domain_principles": [
                "Organizational Resilience & Change Agility",
                "Adaptive Decision-Making & Agility",
            ],
            "affinity": [0.5, 0.7, 0.6, 0.4, 0.5, 0.8, 0.6],
            "initial_sentiment": 0.5,
            "initial_resistance": 0.3,
            "initial_influence": 0.6,
        },
    ],
}

# ---------------------------------------------------------------------------
# Resource model templates (QuestConfig ResourceConfig format)
# ---------------------------------------------------------------------------

_RESOURCE_MODELS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "balanced": {
        "LAU": {"initial": 100, "regen_per_tick": 5, "critical_threshold": 20},
        "BT": {"initial": 500, "regen_per_tick": 0, "critical_threshold": 50},
        "TBU": {"initial": 80, "regen_per_tick": 2, "critical_threshold": 15},
        "OWB": {"initial": 70, "regen_per_tick": -1, "critical_threshold": 25},
    },
    "aggressive": {
        "LAU": {"initial": 150, "regen_per_tick": 8, "critical_threshold": 25},
        "BT": {"initial": 800, "regen_per_tick": 0, "critical_threshold": 80},
        "TBU": {"initial": 60, "regen_per_tick": 1, "critical_threshold": 10},
        "OWB": {"initial": 40, "regen_per_tick": -2, "critical_threshold": 15},
    },
    "conservative": {
        "LAU": {"initial": 80, "regen_per_tick": 3, "critical_threshold": 15},
        "BT": {"initial": 400, "regen_per_tick": 0, "critical_threshold": 40},
        "TBU": {"initial": 90, "regen_per_tick": 3, "critical_threshold": 20},
        "OWB": {"initial": 65, "regen_per_tick": 0, "critical_threshold": 20},
    },
}

# ---------------------------------------------------------------------------
# Black swan event templates
# ---------------------------------------------------------------------------

_BLACK_SWAN_TEMPLATES: Dict[str, List[Dict[str, Any]]] = {
    "transformation": [
        {
            "trigger": {"type": "tick", "tick": 8},
            "event": (
                "Breaking news: A major competitor announces an AI-powered product "
                "that directly threatens your market position. The board demands an "
                "immediate strategic response."
            ),
            "resource_impact": {"OWB": -10, "LAU": -15},
        },
        {
            "trigger": {"type": "tick", "tick": 15},
            "event": (
                "Data breach discovered in a pilot AI system. Customer data may have "
                "been exposed through an inadequately tested ML pipeline. Regulatory "
                "investigation imminent."
            ),
            "resource_impact": {"OWB": -20, "BT": -80, "TBU": -10},
        },
        {
            "trigger": {"type": "tick", "tick": 22},
            "event": (
                "A viral social media post from a departing employee criticizes the "
                "organization's AI adoption as 'reckless and employee-hostile.' "
                "Media coverage intensifies."
            ),
            "resource_impact": {"OWB": -15, "TBU": -5},
        },
        {
            "trigger": {"type": "condition", "when": "OWB < 30"},
            "event": (
                "Key talent begins exodus — 3 senior engineers resign citing 'lack "
                "of organizational commitment to responsible AI.' Remaining team "
                "morale drops sharply."
            ),
            "resource_impact": {"TBU": -20, "OWB": -10},
        },
        {
            "trigger": {"type": "condition", "when": "BT < 100"},
            "event": (
                "Emergency board meeting called. The CFO presents alarming burn rate "
                "projections. Budget freeze on all non-essential AI initiatives "
                "unless immediate ROI can be demonstrated."
            ),
            "resource_impact": {"LAU": -10, "BT": -30},
        },
    ],
    "crisis": [
        {
            "trigger": {"type": "tick", "tick": 5},
            "event": (
                "A whistleblower leaks internal documents to the press revealing "
                "the full extent of the crisis. Public trust plummets."
            ),
            "resource_impact": {"OWB": -25, "TBU": -15},
        },
        {
            "trigger": {"type": "tick", "tick": 12},
            "event": (
                "Regulatory body announces formal investigation. Legal costs "
                "projected to exceed initial estimates by 300%."
            ),
            "resource_impact": {"BT": -120, "LAU": -10},
        },
        {
            "trigger": {"type": "condition", "when": "OWB < 20"},
            "event": (
                "Mass resignation wave. 15% of mid-level management submits notice. "
                "Remaining staff morale reaches critical low."
            ),
            "resource_impact": {"TBU": -25, "OWB": -5},
        },
    ],
    "merger": [
        {
            "trigger": {"type": "tick", "tick": 10},
            "event": (
                "Key executive from the target company publicly criticizes the "
                "merger, rallying employees against integration plans."
            ),
            "resource_impact": {"TBU": -20, "OWB": -10},
        },
        {
            "trigger": {"type": "tick", "tick": 18},
            "event": (
                "Antitrust regulator demands additional concessions before approving "
                "the merger. Delays integration timeline by 3 months."
            ),
            "resource_impact": {"BT": -100, "LAU": -15},
        },
        {
            "trigger": {"type": "condition", "when": "TBU < 25"},
            "event": (
                "Target company's top engineering talent mass-exits to competitor. "
                "Core IP knowledge walking out the door."
            ),
            "resource_impact": {"TBU": -15, "OWB": -20},
        },
    ],
}

# Complexity → max_ticks mapping
_COMPLEXITY_TICKS: Dict[str, int] = {
    "simple": 15,
    "moderate": 30,
    "complex": 45,
}

# Complexity → difficulty mapping
_COMPLEXITY_DIFFICULTY: Dict[str, str] = {
    "simple": "beginner",
    "moderate": "intermediate",
    "complex": "advanced",
}





def _slug_role(name: str) -> str:
    """Lower-snake_case a persona name into a stable echo role id.

    Used only when the retrieved persona has no canonical role field —
    keeps the echo schema's `role` requirement satisfied without
    inventing semantic content. Falls back to a stable hash if the
    name is empty.
    """
    cleaned = "".join(ch if ch.isalnum() or ch in (" ", "_", "-") else " " for ch in (name or ""))
    parts = [p for p in cleaned.lower().replace("-", " ").split() if p]
    if not parts:
        return f"persona_{uuid.uuid4().hex[:6]}"
    return "_".join(parts)[:48]


def _collect_cited_node_ids(persona: Dict[str, Any]) -> tuple[list[str], list[str]]:
    """Pull the persona's full citation set (node_ids + source_doc_ids).

    Returns (cited_node_ids, source_doc_ids) — order-preserving,
    deduplicated. Citations are passthrough metadata for Phase 5; this
    helper does not interpret them.
    """
    cited: list[str] = []
    docs: list[str] = []
    seen_nodes: set[str] = set()
    seen_docs: set[str] = set()

    actor_id = persona.get("actor_node_id")
    if isinstance(actor_id, str) and actor_id and actor_id not in seen_nodes:
        cited.append(actor_id)
        seen_nodes.add(actor_id)

    for bucket in ("beliefs", "frictions", "past_outcomes"):
        for ref in persona.get(bucket) or []:
            if not isinstance(ref, dict):
                continue
            nid = ref.get("node_id")
            if isinstance(nid, str) and nid and nid not in seen_nodes:
                cited.append(nid)
                seen_nodes.add(nid)
            sdid = ref.get("source_doc_id")
            if isinstance(sdid, str) and sdid and sdid not in seen_docs:
                docs.append(sdid)
                seen_docs.add(sdid)

    return cited, docs


def _persona_to_echo(persona: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a retrieved Persona dict to a QuestConfig echo dict.

    Returns None when the input is structurally unusable. Never invents
    sentiment / resistance / influence values — leaves them at the
    EchoConfig defaults (0.5 / 0.3 / 0.5). Beliefs / frictions are
    surfaced into the persona string verbatim so the simulator's
    persona-prompt has them in plain text.
    """
    if not isinstance(persona, dict):
        return None

    name = (persona.get("name") or "").strip()
    role_label = (persona.get("role") or "").strip()
    team = (persona.get("team") or "").strip()
    role_id = _slug_role(role_label or name)
    if not role_id:
        return None

    beliefs = persona.get("beliefs") or []
    frictions = persona.get("frictions") or []

    persona_lines: list[str] = []
    header = name or role_label or role_id
    if role_label and role_label.lower() != role_id:
        header = f"{header} ({role_label})"
    if team:
        header = f"{header} — {team}"
    persona_lines.append(header)

    for belief in beliefs[:5]:
        if not isinstance(belief, dict):
            continue
        statement = (belief.get("statement") or belief.get("label") or "").strip()
        if not statement:
            continue
        marker = "Governing belief" if belief.get("governing") else "Belief"
        persona_lines.append(f"- {marker}: {statement}")

    for friction in frictions[:5]:
        if not isinstance(friction, dict):
            continue
        label = (friction.get("label") or friction.get("statement") or "").strip()
        if not label:
            continue
        persona_lines.append(f"- Friction: {label}")

    persona_text = "\n".join(persona_lines).strip()
    if not persona_text:
        persona_text = role_id

    cited, source_docs = _collect_cited_node_ids(persona)

    return {
        "role": role_id,
        "tier": 2,
        "persona": persona_text,
        "domain_principles": [],
        # 7-element neutral affinity; EchoConfig validates length==7 with
        # values in [0, 1]. Neutral 0.5 means "no opinion expressed yet"
        # rather than inventing a stance.
        "affinity": [0.5] * 7,
        "cited_node_ids": cited,
        "source_doc_ids": source_docs,
    }


def _merge_memory_personas_into_echoes(
    persona_payload: Dict[str, Any],
    template_echoes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge retrieved personas with template echoes (Step 7, Q2=Merge).

    Retrieved personas come first (preserving their `cited_node_ids`).
    Template echoes fill role slots not already covered, so the
    simulator's role-keyed logic (black swans, decision rounds) keeps
    working when the analysis graph is sparse.
    """
    personas = persona_payload.get("personas") or []
    if not isinstance(personas, list):
        return template_echoes

    merged: List[Dict[str, Any]] = []
    seen_roles: set[str] = set()

    for persona in personas:
        echo = _persona_to_echo(persona)
        if echo is None:
            continue
        role = echo["role"]
        if role in seen_roles:
            continue
        merged.append(echo)
        seen_roles.add(role)

    for template in template_echoes:
        role = (template.get("role") or "").strip()
        if not role or role in seen_roles:
            continue
        # Templates carry no graph citations.
        merged.append(template)
        seen_roles.add(role)

    return merged or template_echoes




# ---------------------------------------------------------------------------
# Tool: design_leadership_scenario
# ---------------------------------------------------------------------------


async def design_leadership_scenario(
    scenario_type: str = "transformation",
    budget_allocation: str = "balanced",
    complexity: str = "moderate",
    stakeholder_count: int = 1000,
    gatekeeper_route_map: dict | None = None,
    dependency_graph: list | None = None,
    tool_context=None,
) -> Dict[str, Any]:
    """Design a leadership simulation scenario with stakeholder topology.

    Generates a QuestConfig-compatible scenario with AI Echo personas,
    resource pools, black swan events, and leadership principles. The output
    can be injected directly into the simulator's session state via
    inject_quest_into_state().

    Results are stored in session state for downstream tools
    (report_leadership_scenario, submit_plan_to_simulator).

    This tool is idempotent: calling it twice returns the cached result.

    Args:
        scenario_type: Type of scenario (transformation, crisis, merger).
            Default: "transformation".
        budget_allocation: Resource strategy (balanced, aggressive,
            conservative). Default: "balanced".
        complexity: Scenario complexity (simple, moderate, complex).
            Default: "moderate".
        stakeholder_count: Number of stakeholders. Default: 10.
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with QuestConfig-compatible format: id, name, resources,
        echoes, black_swan_events, principles, max_ticks, difficulty.
    """
    # Idempotent: return cached result if already generated
    if tool_context and tool_context.state.get("leadership_scenario"):
        logger.info("design_leadership_scenario: returning cached scenario")
        return tool_context.state["leadership_scenario"]

    scenario_id = f"dynamic_{uuid.uuid4().hex[:8]}"

    # Select echo templates for the scenario type
    templates = _ECHO_TEMPLATES.get(scenario_type, _ECHO_TEMPLATES["transformation"])

    # Load base templates exactly once (to avoid context overflow).
    # Scaling to 10,000+ stakeholders is handled purely mathematically
    # via the simulation_config passed to submit_plan_to_simulator.
    echoes = [template.copy() for template in templates]

    # ------------------------------------------------------------------
    # Phase 3 memory merge (Q2=Merge).
    # If persona payload data is present in session state (set
    # externally), convert each retrieved persona into an echo carrying
    # `cited_node_ids` and put them FIRST. Template echoes then fill any
    # role gaps so black-swan / decision-round logic keeps working.
    # Never invent data: if the payload is empty or absent, the
    # templates stand alone unchanged.
    # ------------------------------------------------------------------
    if tool_context is not None:
        persona_payload = tool_context.state.get("memory_persona_payload")
        if persona_payload:
            echoes = _merge_memory_personas_into_echoes(persona_payload, echoes)

    # Build resources (QuestConfig ResourceConfig format)
    resources = _RESOURCE_MODELS.get(budget_allocation, _RESOURCE_MODELS["balanced"])

    # Build black swan events
    black_swan_events = _BLACK_SWAN_TEMPLATES.get(scenario_type, _BLACK_SWAN_TEMPLATES["transformation"])

    # Determine ticks and difficulty from complexity
    max_ticks = _COMPLEXITY_TICKS.get(complexity, 30)
    difficulty = _COMPLEXITY_DIFFICULTY.get(complexity, "intermediate")

    # Scale black swan tick triggers to fit max_ticks
    scaled_events = []
    for event in black_swan_events:
        scaled = event.copy()
        trigger = event["trigger"].copy()
        if trigger["type"] == "tick" and trigger.get("tick") is not None:
            # Proportionally scale tick to max_ticks
            original_tick = trigger["tick"]
            trigger["tick"] = max(1, int(original_tick * max_ticks / 30))
        scaled["trigger"] = trigger
        scaled_events.append(scaled)

    # Build the scenario name
    type_names = {
        "transformation": "Enterprise AI Transformation",
        "crisis": "Organizational Crisis Response",
        "merger": "Strategic Merger Integration",
    }
    scenario_name = type_names.get(scenario_type, f"{scenario_type.title()} Leadership Challenge")

    # Assemble QuestConfig-compatible output
    quest_config = {
        "id": scenario_id,
        "name": scenario_name,
        "schema_version": 1,
        "scenario_type": scenario_type,
        "complexity": complexity,
        "difficulty": difficulty,
        "max_ticks": max_ticks,
        "resources": resources,
        "echoes": echoes,
        "black_swan_events": scaled_events,
        "principles": list(LEADERSHIP_PRINCIPLES),
        "stakeholder_count": stakeholder_count,
        "gatekeeper_route_map": gatekeeper_route_map or {},
        "influence_edges": dependency_graph or [],
    }

    # Store in session state for downstream tools
    if tool_context:
        tool_context.state["leadership_scenario"] = quest_config
        tool_context.state["scenario_id"] = scenario_id

    logger.info(
        "design_leadership_scenario: created scenario %s (%s, %d echoes, %d ticks, %d black swans, difficulty=%s)",
        scenario_id,
        scenario_type,
        len(echoes),
        max_ticks,
        len(scaled_events),
        difficulty,
    )

    return quest_config


# ---------------------------------------------------------------------------
# Tool: report_leadership_scenario
# ---------------------------------------------------------------------------


async def report_leadership_scenario(tool_context=None) -> Dict[str, Any]:
    """Emit the leadership scenario to the gateway for visualization.

    Reads the scenario from session state (set by design_leadership_scenario).
    Publishes via the communication plugin for frontend rendering.

    Args:
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with status and scenario summary.
    """
    if not tool_context:
        return {"status": "error", "message": "No tool context available."}

    scenario = tool_context.state.get("leadership_scenario")
    if not scenario:
        return {
            "status": "error",
            "message": "No leadership scenario found. Run 'design_leadership_scenario' first.",
        }

    # Build a summary for the gateway
    summary = {
        "scenario_id": scenario["id"],
        "scenario_name": scenario["name"],
        "difficulty": scenario["difficulty"],
        "max_ticks": scenario["max_ticks"],
        "echo_count": len(scenario["echoes"]),
        "black_swan_count": len(scenario["black_swan_events"]),
        "resource_pools": list(scenario["resources"].keys()),
        "echo_roles": [e["role"] for e in scenario["echoes"]],
    }

    logger.info(
        "report_leadership_scenario: emitting scenario %s to gateway",
        scenario["id"],
    )

    # Include consensus pathway GeoJSON if available
    pathway = tool_context.state.get("consensus_pathway_active")
    if pathway and "geojson" in pathway:
        summary["consensus_pathway"] = pathway["geojson"]

    return {
        "status": "success",
        # NOTE: This `message` is the immediate context the model sees right
        # before its terminal turn -- it overrides the ambient prompt
        # instruction. Wording that signals completion makes the model treat
        # the tool emission as the user-facing delivery and return an empty
        # terminal turn. Cue the summary explicitly here.
        "message": (
            "Scenario published to the visualization layer. "
            "Now complete your workflow (map stakeholders, evaluate, store, "
            "and MUST emit A2UI dashboard via validate_and_emit_a2ui). "
            "Then write a 2-3 paragraph natural-language summary for the user "
            "covering scenario highlights, stakeholder dynamics, and how the "
            "plan addresses the core leadership pillars (Alignment, Decision Quality, "
            "Crisis Readiness)."
        ),
        "scenario_summary": summary,
    }


# ---------------------------------------------------------------------------
# Tool: plan_consensus_pathway  (DCNS — mirrors plan_marathon_route)
# ---------------------------------------------------------------------------


async def plan_consensus_pathway(
    scenario_type: str = "transformation",
    budget_allocation: str = "balanced",
    complexity: str = "moderate",
    stakeholder_count: int = 1000,
    seed: Optional[int] = None,
    tool_context=None,
) -> Dict[str, Any]:
    """Generate a DCNS consensus pathway for the leadership scenario.

    Mirrors plan_marathon_route() but operates on organizational influence
    graphs instead of road networks.  Outputs GeoJSON compatible with the
    existing 3D viewport via hud:importPath.

    IMPORTANT: This tool is idempotent. If a pathway has already been planned
    in this session, it returns status "already_planned" with the cached
    pathway instead of regenerating. Call this tool only once per planning
    pass.

    When Google Maps MCP tools are available, the agent populates
    ``tool_context.state["venue_coords"]`` with a dict mapping stakeholder
    roles to (lon, lat) tuples.  If present, stakeholders are anchored to
    real Las Vegas venues instead of synthetic PCA-projected coordinates.

    Args:
        scenario_type: Type of scenario (transformation, crisis, merger).
        budget_allocation: Resource strategy (balanced, aggressive, conservative).
        complexity: Scenario complexity (simple, moderate, complex).
        stakeholder_count: Number of stakeholders to simulate (scales the graph).
        seed: Random seed for reproducible pathway generation. If None,
            a random seed is generated (mirrors plan_marathon_route).
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with status and geojson FeatureCollection.
    """
    # ── Hard idempotency guard (mirrors plan_marathon_route) ──────────
    # If the pathway has already been planned in this session, return
    # "already_planned".  The LLM must use report_leadership_scenario
    # to emit it.
    if tool_context and tool_context.state.get("consensus_pathway_active"):
        logger.info("plan_consensus_pathway: already planned, returning cached status.")
        return {
            "status": "already_planned",
            "message": "Consensus pathway was already planned in this session. Use report_leadership_scenario to emit it.",
            "geojson": tool_context.state["consensus_pathway_active"].get("geojson", {}),
        }

    import importlib.util as _ilu
    import pathlib as _pl

    _dcns_path = _pl.Path(__file__).parent / "dcns_engine.py"
    _spec = _ilu.spec_from_file_location("dcns_engine", _dcns_path)
    dcns_engine = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(dcns_engine)  # type: ignore[union-attr]

    # Step 1: Ensure scenario is designed (reuses cached if available)
    scenario = await design_leadership_scenario(
        scenario_type=scenario_type,
        budget_allocation=budget_allocation,
        complexity=complexity,
        tool_context=tool_context,
    )

    echoes = scenario.get("echoes", [])
    resources = scenario.get("resources", {})

    if not echoes:
        return {"status": "error", "message": "No echo templates in scenario."}

    # Step 2: Generate single consensus pathway (pragmatist strategy)
    # Read optional venue coordinates from session state (populated by Maps
    # search_places when Google Maps MCP tools are available).
    venue_coords = None
    venue_types = None
    travel_matrix = None
    weather_modifier = None
    if tool_context:
        raw_venue_coords = tool_context.state.get("venue_coords")
        if isinstance(raw_venue_coords, dict) and raw_venue_coords:
            # Ensure values are tuples
            venue_coords = {k: tuple(v) if isinstance(v, (list, tuple)) else v for k, v in raw_venue_coords.items()}
            logger.info(
                "plan_consensus_pathway: using %d real venue coordinates from Maps",
                len(venue_coords),
            )

        # Organizational Terrain: read venue types, travel matrix, and weather
        # modifier from session state (populated by the agent in workflow
        # steps 2, 2b, and 3 respectively).
        raw_venue_types = tool_context.state.get("venue_types")
        if isinstance(raw_venue_types, dict) and raw_venue_types:
            venue_types = raw_venue_types
            logger.info(
                "plan_consensus_pathway: using %d venue types from Places API",
                len(venue_types),
            )

        raw_travel_matrix = tool_context.state.get("travel_matrix")
        if isinstance(raw_travel_matrix, dict) and raw_travel_matrix:
            travel_matrix = {k: float(v) for k, v in raw_travel_matrix.items()}
            logger.info(
                "plan_consensus_pathway: using %d travel durations from Routes API",
                len(travel_matrix),
            )

        raw_weather = tool_context.state.get("weather_modifier")
        if isinstance(raw_weather, dict) and raw_weather:
            weather_modifier = raw_weather
            logger.info(
                "plan_consensus_pathway: using weather modifier (%s)",
                weather_modifier.get("condition", "unknown"),
            )

    # Pass network.json if available
    skill_dir = _pl.Path(__file__).parent.parent
    network_path = str(skill_dir / "assets" / "network.json")

    selected = dcns_engine.generate_consensus_pathway(
        stakeholders=echoes,
        resources=resources,
        scenario_type=scenario_type,
        runner_count=stakeholder_count,
        seed=seed,
        venue_coords=venue_coords,
        network_path=network_path,
        venue_types=venue_types,
        travel_matrix=travel_matrix,
        weather_modifier=weather_modifier,
    )

    # Step 3: Cache in session state — single pathway only
    if tool_context:
        tool_context.state["consensus_pathway_active"] = selected
        tool_context.state["selected_persona"] = "pragmatist"

    logger.info(
        "plan_consensus_pathway: generated pathway (consensus=%.3f, stakeholders=%d)",
        selected.get("consensus_score", 0),
        len(echoes),
    )

    return {
        "status": "success",
        "persona": "pragmatist",
        "geojson": selected["geojson"],
        "consensus_score": round(selected.get("consensus_score", 0), 3),
        "cost": selected.get("cost", {}),
    }
