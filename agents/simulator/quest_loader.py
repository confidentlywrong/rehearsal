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

"""Quest configuration schema and loader for The Synchronicity Engine 2.0.

Reads YAML quest definitions from ``configs/quests/`` and validates them
against Pydantic models.  The loader injects the parsed quest config into
the simulator's session state during the ``pre_simulation`` phase.

No LLM dependency — this is purely deterministic configuration management.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# Default quest directory relative to the repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUEST_DIR = _REPO_ROOT / "configs" / "quests"

# ---------------------------------------------------------------------------
# Schema models
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


def _require_finite_float(value: Any, field_name: str) -> float:
    """Reject booleans, NaN, +/-Inf, and overflowing values in numeric quest schema fields."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be a boolean, got {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a finite number, got {value!r}") from exc
    if not math.isfinite(num):
        raise ValueError(f"{field_name} must be a finite number, got {value!r}")
    return num


def _validate_gatekeeper_route_entry(role: str, entry: Any) -> None:
    """Validate finite non-negative ranges for a gatekeeper_route_map entry."""
    if isinstance(entry, bool):
        raise ValueError(f"gatekeeper_route_map[{role!r}] must not be a boolean")
    if isinstance(entry, (int, float)):
        val = _require_finite_float(entry, f"gatekeeper_route_map[{role!r}]")
        if val < 0.0:
            raise ValueError(f"gatekeeper_route_map[{role!r}] must be non-negative, got {val}")
        return
    if isinstance(entry, (list, tuple)):
        nums: list[float] = []
        for idx, elem in enumerate(entry):
            if isinstance(elem, bool):
                raise ValueError(f"gatekeeper_route_map[{role!r}][{idx}] must not be a boolean")
            if isinstance(elem, (int, float)):
                val = _require_finite_float(elem, f"gatekeeper_route_map[{role!r}][{idx}]")
                if val < 0.0:
                    raise ValueError(f"gatekeeper_route_map[{role!r}][{idx}] must be non-negative, got {val}")
                nums.append(val)
        if len(entry) == 2 and len(nums) == 2 and nums[0] > nums[1]:
            raise ValueError(
                f"gatekeeper_route_map[{role!r}] start ({nums[0]}) must not exceed end ({nums[1]})"
            )
        return
    if isinstance(entry, dict):
        parsed_nums: dict[str, float] = {}
        for key, raw_val in entry.items():
            if isinstance(raw_val, bool):
                raise ValueError(f"gatekeeper_route_map[{role!r}][{key!r}] must not be a boolean")
            if isinstance(raw_val, (int, float)):
                num = _require_finite_float(raw_val, f"gatekeeper_route_map[{role!r}][{key!r}]")
                if key in ("start_mi", "end_mi", "mile") and num < 0.0:
                    raise ValueError(
                        f"gatekeeper_route_map[{role!r}][{key!r}] must be non-negative, got {num}"
                    )
                parsed_nums[key] = num
        if "start_mi" in parsed_nums and "end_mi" in parsed_nums and parsed_nums["start_mi"] > parsed_nums["end_mi"]:
            raise ValueError(
                f"gatekeeper_route_map[{role!r}] start_mi ({entry.get('start_mi')}) must not exceed end_mi ({entry.get('end_mi')})"
            )


class ResourceConfig(BaseModel):
    """Configuration for a single organizational resource pool."""

    initial: float = Field(..., ge=0, description="Starting value for the resource")
    regen_per_tick: float = Field(0.0, description="Amount regenerated (or decayed if negative) per tick")
    critical_threshold: float = Field(20.0, ge=0, description="Value below which ECSI fires a threshold breach alert")

    @field_validator("initial", "regen_per_tick", "critical_threshold", mode="before")
    @classmethod
    def validate_finite_resource_fields(cls, v: Any, info) -> float:
        return _require_finite_float(v, info.field_name or "resource_field")


class BlackSwanTrigger(BaseModel):
    """Trigger condition for a Black Swan event."""

    type: Literal["tick", "condition"] = Field(..., description="Trigger type: tick-based or condition-based")
    tick: int | None = Field(None, ge=0, description="Tick number to fire (for tick-based triggers)")
    when: str | None = Field(None, description="Condition expression, e.g. 'OWB < 30' (for condition triggers)")

    @field_validator("tick", mode="before")
    @classmethod
    def reject_bool_or_non_finite_tick(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError("tick must not be a boolean")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"tick must be finite, got {v!r}")
        return v

    @field_validator("tick")
    @classmethod
    def tick_required_for_tick_type(cls, v: int | None, info) -> int | None:
        if info.data.get("type") == "tick" and v is None:
            raise ValueError("tick is required when trigger type is 'tick'")
        return v

    @field_validator("when")
    @classmethod
    def when_required_for_condition_type(cls, v: str | None, info) -> str | None:
        if info.data.get("type") == "condition" and not v:
            raise ValueError("when is required when trigger type is 'condition'")
        return v


class BlackSwanEvent(BaseModel):
    """A disruptive event that fires during the simulation."""

    trigger: BlackSwanTrigger
    event: str = Field(..., min_length=1, description="Narrative description of the event")
    resource_impact: dict[str, float] = Field(
        default_factory=dict, description="Resource deltas, e.g. {'OWB': -15, 'BT': -50}"
    )

    @field_validator("resource_impact", mode="before")
    @classmethod
    def validate_finite_resource_impact(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        return {
            key: _require_finite_float(val, f"resource_impact[{key!r}]")
            for key, val in v.items()
        }


class EchoConfig(BaseModel):
    """Configuration for a single AI Echo stakeholder persona."""

    role: str = Field(..., min_length=1, description="Unique role identifier, e.g. 'cfo'")
    tier: int = Field(2, ge=1, le=3, description="Execution tier: 1=Autonomous, 2=Reactive, 3=Deterministic")
    persona: str = Field(..., min_length=1, description="Natural language persona description")
    domain_principles: list[str] = Field(
        default_factory=list, description="Which leadership principles this Echo is most sensitive to"
    )
    affinity: list[float] = Field(
        ..., min_length=7, max_length=7, description="7-element principle affinity vector (0.0-1.0)"
    )
    initial_sentiment: float = Field(0.5, ge=-1.0, le=1.0, description="Starting sentiment (-1 to 1)")
    initial_resistance: float = Field(0.3, ge=0.0, le=1.0, description="Starting resistance (0 to 1)")
    initial_influence: float = Field(0.5, ge=0.0, le=1.0, description="Starting influence (0 to 1)")
    has_hidden_agenda: bool = Field(False, description="Whether this Echo has a hidden agenda")
    agenda_trigger_tick: int | None = Field(None, ge=0, description="Tick at which hidden agenda activates")
    # --- Organizational Memory / Phase 3 persona payload citations ---
    # Populated by the leadership_planner when a persona is sourced from
    # the analysis graph. For template-only echoes this stays empty.
    # The simulator does not interpret the IDs — they are passthrough
    # payload for Phase 5 reflection (see
    # concepts/organizational_memory_and_learning.md §4 Phase 4).
    cited_node_ids: list[str] = Field(
        default_factory=list,
        description="Phase 3 persona-payload node_ids cited by this Echo (passthrough only)",
    )
    source_doc_ids: list[str] = Field(
        default_factory=list,
        description="Source document IDs referenced by the cited nodes (passthrough only)",
    )

    @field_validator("affinity", mode="before")
    @classmethod
    def validate_affinity_range(cls, v: Any) -> Any:
        if not isinstance(v, (list, tuple)):
            return v
        validated: list[float] = []
        for i, val in enumerate(v):
            num = _require_finite_float(val, f"affinity[{i}]")
            if not 0.0 <= num <= 1.0:
                raise ValueError(f"affinity[{i}] must be between 0.0 and 1.0, got {val}")
            validated.append(num)
        return validated

    @field_validator("initial_sentiment", "initial_resistance", "initial_influence", mode="before")
    @classmethod
    def validate_finite_echo_scalars(cls, v: Any, info) -> float:
        return _require_finite_float(v, info.field_name or "echo_scalar")


class InfluenceEdgeConfig(BaseModel):
    """Configuration for an inter-Echo influence relationship."""

    source: str = Field(..., min_length=1, description="Source Echo role")
    target: str = Field(..., min_length=1, description="Target Echo role")
    weight: float = Field(0.5, ge=0.0, le=1.0, description="Influence strength (0.0-1.0)")

    @field_validator("weight", mode="before")
    @classmethod
    def validate_finite_weight(cls, v: Any) -> float:
        return _require_finite_float(v, "weight")


class QuestConfig(BaseModel):
    """Top-level quest definition."""

    id: str = Field(..., min_length=1, description="Unique quest identifier")
    name: str = Field(..., min_length=1, description="Human-readable quest name")
    schema_version: int = Field(1, ge=1, description="Schema version for forward compatibility")
    difficulty: Literal["beginner", "intermediate", "advanced", "expert"] = "intermediate"
    max_ticks: int = Field(30, ge=5, le=100, description="Number of decision rounds")

    resources: dict[str, ResourceConfig] = Field(..., description="Resource pool definitions (LAU, BT, TBU, OWB)")
    echoes: list[EchoConfig] = Field(..., min_length=1, description="AI Echo persona definitions")
    black_swan_events: list[BlackSwanEvent] = Field(
        default_factory=list, description="Disruptive events with trigger conditions"
    )
    principles: list[str] = Field(
        default_factory=lambda: list(LEADERSHIP_PRINCIPLES),
        min_length=7,
        max_length=7,
        description="The 7 leadership principles",
    )
    influence_edges: list[InfluenceEdgeConfig] = Field(
        default_factory=list,
        description="Optional inter-Echo influence edges. Auto-generated from role heuristics if omitted.",
    )
    gatekeeper_route_map: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional mapping of gatekeeper Echo roles to route segments or mile markers.",
    )

    @field_validator("resources")
    @classmethod
    def validate_required_resources(cls, v: dict[str, ResourceConfig]) -> dict[str, ResourceConfig]:
        required = {"LAU", "BT", "TBU", "OWB"}
        missing = required - set(v.keys())
        if missing:
            raise ValueError(f"Missing required resources: {missing}")
        return v

    @field_validator("gatekeeper_route_map")
    @classmethod
    def validate_gatekeeper_route_map(cls, v: dict[str, Any]) -> dict[str, Any]:
        for role, entry in v.items():
            _validate_gatekeeper_route_entry(role, entry)
        return v


class QuestFile(BaseModel):
    """Wrapper for the quest YAML file structure."""

    quest: QuestConfig


# ---------------------------------------------------------------------------
# Loader functions
# ---------------------------------------------------------------------------


def load_quest_from_yaml(quest_id: str, quest_dir: str | Path | None = None) -> QuestConfig:
    """Load and validate a quest YAML file by ID.

    Args:
        quest_id: The quest identifier (matches filename without extension).
        quest_dir: Directory containing quest YAML files.
                   Defaults to ``configs/quests/`` relative to repo root.

    Returns:
        Validated QuestConfig instance.

    Raises:
        FileNotFoundError: If the quest file doesn't exist.
        ValueError: If the YAML is invalid or fails schema validation.
    """
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML is required for quest loading. Install with: pip install pyyaml")

    if not quest_id or not isinstance(quest_id, str) or "/" in quest_id or "\\" in quest_id or ".." in quest_id:
        raise ValueError(f"Invalid quest_id: {quest_id!r}")

    quest_path = Path(quest_dir or DEFAULT_QUEST_DIR)
    file_path = quest_path / f"{quest_id}.yaml"
    raw: Any = None

    if not file_path.exists():
        # Try .yml extension
        file_path = quest_path / f"{quest_id}.yml"
        if not file_path.exists():
            # Fallback: scan YAML files in quest_path for matching quest.id
            matched_path: Path | None = None
            available: list[str] = []
            if quest_path.exists():
                for candidate in sorted(quest_path.glob("*.y*ml")):
                    try:
                        with open(candidate, "r") as cf:
                            candidate_raw = yaml.safe_load(cf)
                    except Exception:
                        available.append(candidate.stem)
                        continue
                    candidate_quest = candidate_raw.get("quest") if isinstance(candidate_raw, dict) else None
                    candidate_id = candidate_quest.get("id") if isinstance(candidate_quest, dict) else None
                    available.append(str(candidate_id or candidate.stem))
                    if candidate_id == quest_id and matched_path is None:
                        matched_path = candidate
                        raw = candidate_raw
            if matched_path is None:
                raise FileNotFoundError(
                    f"Quest '{quest_id}' not found in {quest_path}. Available quests: {available}"
                )
            file_path = matched_path

    logger.info("Loading quest from %s", file_path)
    if raw is None:
        with open(file_path, "r") as f:
            raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "quest" not in raw:
        raise ValueError(f"Quest file {file_path} must have a top-level 'quest' key")

    quest_file = QuestFile.model_validate(raw)
    logger.info(
        "Quest '%s' loaded successfully: %d echoes, %d ticks, %d black swan events",
        quest_file.quest.name,
        len(quest_file.quest.echoes),
        quest_file.quest.max_ticks,
        len(quest_file.quest.black_swan_events),
    )
    return quest_file.quest


def inject_quest_into_state(state: Any, quest: QuestConfig) -> None:
    """Inject a validated quest configuration into simulator session state.

    This is called during ``pre_simulation`` to populate the state with
    all quest-derived values that the tick loop and Echoes will read.

    Args:
        state: ADK session state (dict-like) to populate.
        quest: Validated quest configuration.
    """
    # Store the full quest config for reference
    state["quest_id"] = quest.id
    state["quest_name"] = quest.name
    state["quest_difficulty"] = quest.difficulty
    state["max_ticks"] = quest.max_ticks
    state["current_tick"] = 0
    state["principles"] = quest.principles

    # Initialize resource pools
    resources = {}
    for name, config in quest.resources.items():
        resources[name.lower()] = {
            "current": config.initial,
            "initial": config.initial,
            "regen_per_tick": config.regen_per_tick,
            "critical_threshold": config.critical_threshold,
        }
    state["resources"] = resources

    # Store echo configs for spawning
    state["echo_configs"] = [echo.model_dump() for echo in quest.echoes]

    # Store black swan events (to be evaluated per-tick)
    state["black_swan_events"] = [event.model_dump() for event in quest.black_swan_events]
    state["triggered_black_swans"] = []

    # Initialize tracking accumulators
    state["tick_snapshots"] = []
    state["declared_commitments"] = []
    state["contradiction_log"] = []
    state["principle_scores"] = [0.0] * 7
    state["_raw_principle_scores"] = None
    state["_principle_scores_normalized"] = False
    state["alert_history"] = []
    state["revealed_agendas"] = []

    # Store influence edges and quest_config topology for advance_tick
    serialized_edges = [edge.model_dump() for edge in quest.influence_edges]
    state["influence_edges"] = serialized_edges
    state["influence_network"] = None
    state["quest_config"] = {
        "id": quest.id,
        "name": quest.name,
        "gatekeeper_route_map": dict(quest.gatekeeper_route_map),
        "influence_edges": serialized_edges,
    }

    logger.info(
        "Quest '%s' injected into session state: resources=%s, echoes=%d",
        quest.name,
        list(resources.keys()),
        len(quest.echoes),
    )
