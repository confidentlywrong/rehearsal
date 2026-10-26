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

"""Longitudinal tracking, what-if replay, and peer benchmarking.

Provides cross-simulation tracking for leadership development.
Uses file-based NDJSON storage as a portable default (no DB dependency),
with optional PostgreSQL/AlloyDB support when ``USE_ALLOYDB=true``.

No LLM dependency — pure Python data operations.
"""

import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
import fcntl

from agents.utils import tenant_paths

logger = logging.getLogger(__name__)

# Default storage directory relative to repo root.
# Kept as a module-level constant for backward compatibility with code
# that imports it directly; new call sites should go through
# :func:`agents.utils.tenant_paths.runs_base_dir` so that test overrides
# via ``RACE_RUNS_DIR`` take effect.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_DIR = _REPO_ROOT / "data" / "simulation_runs"


# ---------------------------------------------------------------------------
# 5A: Simulation Run Record
# ---------------------------------------------------------------------------


class SimulationRunRecord:
    """A single simulation run record for longitudinal tracking."""

    def __init__(
        self,
        run_id: str | None = None,
        participant_id: str = "anonymous",
        quest_id: str = "",
        quest_name: str = "",
        difficulty: str = "intermediate",
        principle_scores: list[float] | None = None,
        overall_score: float = 0.0,
        contradiction_count: int = 0,
        defected_count: int = 0,
        aligned_count: int = 0,
        total_echoes: int = 0,
        total_ticks: int = 0,
        final_resources: dict[str, float] | None = None,
        completed_at: str | None = None,
        tick_snapshots: list[dict] | None = None,
    ):
        self.run_id = run_id or str(uuid.uuid4())
        self.participant_id = participant_id
        self.quest_id = quest_id
        self.quest_name = quest_name
        self.difficulty = difficulty
        self.principle_scores = principle_scores or [0.0] * 7
        self.overall_score = overall_score
        self.contradiction_count = contradiction_count
        self.defected_count = defected_count
        self.aligned_count = aligned_count
        self.total_echoes = total_echoes
        self.total_ticks = total_ticks
        self.final_resources = final_resources or {}
        self.completed_at = completed_at or datetime.now(timezone.utc).isoformat()
        self.tick_snapshots = tick_snapshots  # Full snapshots for what-if replay

    def to_dict(self) -> dict:
        """Serialize to dict (tick_snapshots excluded for summary use)."""
        return {
            "run_id": self.run_id,
            "participant_id": self.participant_id,
            "quest_id": self.quest_id,
            "quest_name": self.quest_name,
            "difficulty": self.difficulty,
            "principle_scores": self.principle_scores,
            "overall_score": self.overall_score,
            "contradiction_count": self.contradiction_count,
            "defected_count": self.defected_count,
            "aligned_count": self.aligned_count,
            "total_echoes": self.total_echoes,
            "total_ticks": self.total_ticks,
            "final_resources": self.final_resources,
            "completed_at": self.completed_at,
        }

    def to_full_dict(self) -> dict:
        """Serialize to dict including tick_snapshots for replay."""
        d = self.to_dict()
        d["tick_snapshots"] = self.tick_snapshots or []
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SimulationRunRecord":
        params = set(inspect.signature(cls.__init__).parameters) - {"self"}
        return cls(**{k: v for k, v in d.items() if k in params})

    @classmethod
    def from_session_state(cls, state: dict) -> "SimulationRunRecord":
        """Build a run record from ADK session state after simulation completes."""
        principle_scores = state.get("principle_scores", [0.0] * 7)
        overall_score = sum(principle_scores) / len(principle_scores) if principle_scores else 0.0

        final_resources = {}
        for name, pool in state.get("resources", {}).items():
            final_resources[name] = pool.get("current", 0)

        return cls(
            run_id=state.get("simulation_id", str(uuid.uuid4())),
            participant_id=state.get("participant_id", "anonymous"),
            quest_id=state.get("quest_id", ""),
            quest_name=state.get("quest_name", ""),
            difficulty=state.get("quest_difficulty", "intermediate"),
            principle_scores=principle_scores,
            overall_score=round(overall_score, 2),
            contradiction_count=len(state.get("contradiction_log", [])),
            defected_count=len(state.get("defected_echo_ids", [])),
            aligned_count=len(state.get("aligned_echo_ids", [])),
            total_echoes=len(state.get("echo_configs", [])),
            total_ticks=len(state.get("tick_snapshots", [])),
            final_resources=final_resources,
            tick_snapshots=state.get("tick_snapshots"),
        )


# ---------------------------------------------------------------------------
# 5A: File-Based Storage
# ---------------------------------------------------------------------------


class SimulationRunStore:
    """File-based storage for simulation run records.

    Layout (legacy, SAAS_MODE=off — unchanged from the single-tenant
    app): each participant gets a directory under
    ``data/simulation_runs/``.  Run summaries are appended to
    ``{participant_id}/runs.ndjson``; full tick snapshots are stored
    as ``{participant_id}/{run_id}.json`` for what-if replay.

    Layout (SaaS-on, ``SAAS_MODE`` truthy and ``DISPATCHER_USER_ID``
    set): the subdirectory becomes the bound user id rather than the
    participant id, so all of a tenant's runs cohabit one tree:
    ``{user_id}/runs.ndjson`` + ``{user_id}/{run_id}.json``.  Phase 3
    routes both modes through :mod:`agents.utils.tenant_paths` so
    the off-path is byte-identical to today.

    No on-disk write ever falls back to a synthetic tenant — under
    SaaS-on, if ``DISPATCHER_USER_ID`` is unset the call defaults to
    the participant id, exactly as in legacy mode, and the dispatcher
    tripwire (Phase 2.5b) is responsible for rejecting cross-tenant
    payloads upstream.
    """

    def __init__(self, base_dir: Path | str | None = None):
        # ``base_dir`` wins when supplied (used by tests via tmp_path).
        # Otherwise we defer to tenant_paths so the ``RACE_RUNS_DIR``
        # env var can redirect production writes without touching code.
        self._explicit_base_dir = Path(base_dir) if base_dir else None

    @property
    def base_dir(self) -> Path:
        """Resolve the runs directory at every access.

        Defined as a property so tests that change ``RACE_RUNS_DIR``
        between fixtures see the new value without rebuilding the
        store.
        """
        if self._explicit_base_dir is not None:
            return self._explicit_base_dir
        return tenant_paths.runs_base_dir()

    def _subdir_for_record(self, record: SimulationRunRecord) -> Path:
        """Pick the on-disk subdirectory for a record.

        SAAS_MODE off → ``participant_id`` (byte-identical legacy).
        SAAS_MODE on  → ``user_id`` if available, else fall through to
                        ``participant_id`` so behavior matches the
                        dispatcher's tripwire policy.
        """
        tenant_id = tenant_paths.resolve_tenant_id(fallback=record.participant_id)
        return self.base_dir / tenant_id

    def _subdir_for_participant(self, participant_id: str) -> Path:
        """Pick the on-disk subdirectory for a read.

        Mirrors :meth:`_subdir_for_record`. Reads never invent a tenant;
        a missing ``DISPATCHER_USER_ID`` keeps the caller pointed at
        the legacy ``participant_id`` subdirectory.
        """
        tenant_id = tenant_paths.resolve_tenant_id(fallback=participant_id)
        return self.base_dir / tenant_id

    def save_run(self, record: SimulationRunRecord) -> Path:
        """Save a simulation run record.

        Args:
            record: The run record to save.

        Returns:
            Path to the saved summary file.
        """
        target_dir = self._subdir_for_record(record)
        target_dir.mkdir(parents=True, exist_ok=True)

        # Append summary to runs.ndjson
        summary_path = target_dir / "runs.ndjson"
        with open(summary_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(record.to_dict()) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        # Save full tick snapshots for replay
        if record.tick_snapshots:
            replay_path = target_dir / f"{record.run_id}.json"
            with open(replay_path, "w") as f:
                json.dump(record.to_full_dict(), f, indent=2)

        logger.info(
            "Saved simulation run %s for participant %s under %s (quest=%s, score=%.1f)",
            record.run_id,
            record.participant_id,
            target_dir.name,
            record.quest_id,
            record.overall_score,
        )
        return summary_path

    def get_participant_runs(self, participant_id: str) -> list[dict]:
        """Load all run summaries for a participant.

        Args:
            participant_id: The participant identifier.

        Returns:
            List of run summary dicts, ordered chronologically.
        """
        summary_path = self._subdir_for_participant(participant_id) / "runs.ndjson"
        if not summary_path.exists():
            return []

        runs = []
        with open(summary_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        runs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return runs

    def get_run_snapshots(self, participant_id: str, run_id: str) -> dict | None:
        """Load full tick snapshots for what-if replay.

        Args:
            participant_id: The participant identifier.
            run_id: The simulation run identifier.

        Returns:
            Full run dict including tick_snapshots, or None.
        """
        replay_path = self._subdir_for_participant(participant_id) / f"{run_id}.json"
        if not replay_path.exists():
            return None
        with open(replay_path) as f:
            return json.load(f)

    def get_all_runs_for_quest(self, quest_id: str, participant_id: str) -> list[dict]:
        """Load all runs for a specific quest, scoped to the user.

        Used for peer benchmarking percentile calculations. Since the system
        migrated to a user_id multi-tenant model, this only scans the specific
        participant's directory to prevent O(N) disk scans across the entire global platform.

        Args:
            quest_id: The quest identifier.
            participant_id: The participant identifier.

        Returns:
            List of run summary dicts matching the quest.
        """
        all_runs = []
        if not self.base_dir.exists():
            return all_runs

        participant_dir = self._subdir_for_participant(participant_id)
        if not participant_dir.is_dir():
            return all_runs

        summary_path = participant_dir / "runs.ndjson"
        if not summary_path.exists():
            return all_runs

        with open(summary_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        run = json.loads(line)
                        if run.get("quest_id") == quest_id:
                            all_runs.append(run)
                    except json.JSONDecodeError:
                        continue
        return all_runs


# ---------------------------------------------------------------------------
# 5B: What-If Replay Engine
# ---------------------------------------------------------------------------


def replay_from_tick(
    tick_snapshots: list[dict],
    fork_tick: int,
    modified_decision_tags: dict | None = None,
) -> dict:
    """Simulate a what-if replay from a given tick.

    Replays the deterministic resource and contention logic from
    ``fork_tick`` onward with modified decision tags.  This shows how
    the simulation *would have* evolved if the participant made a
    different decision at the fork point.

    Only replays the deterministic resource tracking — LLM-generated
    Echo responses are not re-simulated (they would require full agent
    invocation).

    Args:
        tick_snapshots: Full list of tick snapshots from the original run.
        fork_tick: The tick number to fork from.
        modified_decision_tags: Alternative decision classification to
            apply at the fork tick.

    Returns:
        Dict with forked resource trajectories and comparison data.
    """
    from agents.simulator.decision_classifier import (
        apply_resource_impacts,
    )

    if fork_tick < 0 or fork_tick >= len(tick_snapshots):
        return {"error": f"Invalid fork_tick: {fork_tick}, must be 0-{len(tick_snapshots) - 1}"}

    # Get the resource state at the fork point
    fork_snapshot = tick_snapshots[fork_tick]
    org_state = fork_snapshot.get("organizational_state", {})
    resources = org_state.get("resources", {})

    # Deep copy resources for the what-if branch
    import copy

    forked_resources = copy.deepcopy(resources)

    # Apply modified decision at fork point
    if modified_decision_tags:
        apply_resource_impacts(
            forked_resources,
            modified_decision_tags.get("resource_impact_suggestion", {}),
        )

    # Replay remaining ticks with the original tick data but forked resources
    original_trajectory: list[dict] = []
    forked_trajectory: list[dict] = []

    for i, snap in enumerate(tick_snapshots):
        original_resources = snap.get("organizational_state", {}).get("resources", {})
        original_trajectory.append(
            {
                "tick": snap.get("tick", i),
                "resources": {k: v.get("current", 0) for k, v in original_resources.items()},
            }
        )

    for i in range(fork_tick, len(tick_snapshots)):
        forked_trajectory.append(
            {
                "tick": tick_snapshots[i].get("tick", i),
                "resources": {k: v.get("current", 0) for k, v in forked_resources.items()},
            }
        )

        # Apply regen for next tick (simulating the ongoing decay/regen)
        if i < len(tick_snapshots) - 1:
            for name, pool in forked_resources.items():
                regen = pool.get("regen_per_tick", 0)
                current = pool.get("current", 0)
                initial = pool.get("initial", 100)
                pool["current"] = max(0, min(initial, round(current + regen, 1)))

    return {
        "fork_tick": fork_tick,
        "original_trajectory": original_trajectory,
        "forked_trajectory": forked_trajectory,
        "comparison": _compute_trajectory_comparison(
            original_trajectory,
            forked_trajectory,
            fork_tick,
        ),
    }


def _compute_trajectory_comparison(
    original: list[dict],
    forked: list[dict],
    fork_tick: int,
) -> dict:
    """Compute summary comparison between original and forked trajectories."""
    if not original or not forked:
        return {}

    orig_final = original[-1]["resources"]
    fork_final = forked[-1]["resources"]

    deltas = {}
    for resource in orig_final:
        orig_val = orig_final.get(resource, 0)
        fork_val = fork_final.get(resource, 0)
        deltas[resource] = round(fork_val - orig_val, 1)

    return {
        "resource_deltas": deltas,
        "ticks_replayed": len(forked),
        "verdict": _verdict_from_deltas(deltas),
    }


def _verdict_from_deltas(deltas: dict) -> str:
    """Generate a human-readable verdict from resource deltas."""
    positive = sum(1 for v in deltas.values() if v > 0)
    negative = sum(1 for v in deltas.values() if v < 0)

    if positive > negative:
        return "This alternative decision would have improved your resource position."
    elif negative > positive:
        return "This alternative decision would have worsened your resource position."
    else:
        return "This alternative decision would have had a mixed impact on resources."


# ---------------------------------------------------------------------------
# 5C: Peer Benchmarking & Principle Radar
# ---------------------------------------------------------------------------


def compute_peer_benchmarks(
    current_scores: list[float],
    all_runs: list[dict],
    principles: list[str] | None = None,
) -> dict:
    """Compute peer benchmarks for principle radar chart.

    Args:
        current_scores: The current participant's 7 principle scores.
        all_runs: All runs for the same quest (from SimulationRunStore).
        principles: The 7 principle names.

    Returns:
        Benchmark dict with per-principle participant score, average, and
        p90 values, plus a ranked position.
    """
    if principles is None:
        from agents.simulator.quest_loader import LEADERSHIP_PRINCIPLES

        principles = LEADERSHIP_PRINCIPLES

    if not all_runs:
        return {
            "participant_scores": current_scores,
            "average_scores": current_scores,
            "p90_scores": current_scores,
            "total_runs": 0,
            "rank": 1,
            "rank_total": 1,
            "percentile": 100.0,
        }

    # Collect all principle scores
    all_scores = [r.get("principle_scores", [0.0] * 7) for r in all_runs]

    # Compute per-principle averages and p90
    avg_scores = []
    p90_scores = []
    for i in range(7):
        values = sorted([s[i] for s in all_scores if len(s) > i])
        if values:
            avg_scores.append(round(sum(values) / len(values), 2))
            p90_idx = min(int(len(values) * 0.9), len(values) - 1)
            p90_scores.append(round(values[p90_idx], 2))
        else:
            avg_scores.append(0.0)
            p90_scores.append(0.0)

    # Compute overall ranking
    current_overall = sum(current_scores) / len(current_scores) if current_scores else 0
    all_overalls = sorted([sum(s) / len(s) for s in all_scores if s])

    rank = sum(1 for o in all_overalls if o > current_overall) + 1
    total = len(all_overalls) + 1  # Include current run

    return {
        "participant_scores": [round(s, 2) for s in current_scores],
        "average_scores": avg_scores,
        "p90_scores": p90_scores,
        "total_runs": len(all_runs),
        "rank": rank,
        "rank_total": total,
        "percentile": round((1 - (rank - 1) / total) * 100, 1) if total > 0 else 100.0,
        "principles": principles,
    }


def compute_learning_trajectory(runs: list[dict]) -> dict:
    """Compute longitudinal learning trajectory across simulations.

    Args:
        runs: Chronological list of run summaries for a participant.

    Returns:
        Trajectory dict with per-simulation scores, trends, and
        growth recommendations.
    """
    if not runs:
        return {
            "simulations": [],
            "growth_areas": [],
            "strengths": [],
            "trend": "insufficient_data",
            "total_simulations": 0,
        }

    from agents.simulator.quest_loader import LEADERSHIP_PRINCIPLES

    trajectory = []
    for run in runs:
        scores = run.get("principle_scores", [0.0] * 7)
        trajectory.append(
            {
                "quest_id": run.get("quest_id"),
                "quest_name": run.get("quest_name"),
                "overall_score": run.get("overall_score", 0),
                "principle_scores": scores,
                "contradiction_count": run.get("contradiction_count", 0),
                "completed_at": run.get("completed_at"),
            }
        )

    # Identify growth areas (weakest principles across last 3 runs)
    recent = runs[-3:] if len(runs) >= 3 else runs
    recent_scores = [r.get("principle_scores", [0.0] * 7) for r in recent]

    avg_per_principle = []
    for i in range(7):
        vals = [s[i] for s in recent_scores if len(s) > i]
        avg_per_principle.append(sum(vals) / len(vals) if vals else 0)

    # Sort by score ascending to find weakest
    indexed = list(enumerate(avg_per_principle))
    indexed.sort(key=lambda x: x[1])

    growth_areas = [
        {"principle": LEADERSHIP_PRINCIPLES[i], "avg_score": round(s, 2)}
        for i, s in indexed[:2]  # Bottom 2
    ]

    strengths = [
        {"principle": LEADERSHIP_PRINCIPLES[i], "avg_score": round(s, 2)}
        for i, s in indexed[-2:]  # Top 2
    ]

    # Compute trend (is the participant improving?)
    if len(runs) >= 2:
        first_overall = runs[0].get("overall_score", 0)
        last_overall = runs[-1].get("overall_score", 0)
        trend = (
            "improving" if last_overall > first_overall else ("declining" if last_overall < first_overall else "stable")
        )
    else:
        trend = "insufficient_data"

    return {
        "simulations": trajectory,
        "growth_areas": growth_areas,
        "strengths": strengths,
        "trend": trend,
        "total_simulations": len(runs),
    }


# ---------------------------------------------------------------------------
# Quest Recommendation
# ---------------------------------------------------------------------------

# Maps each principle to the quest that best exercises it
QUEST_PRINCIPLE_MAP = {
    "Strategic Foresight & Vision Cascading": "ai_powered_disruptor",
    "Adaptive Decision-Making & Agility": "scaling_hypergrowth_2028",
    "Stakeholder Engagement & Inclusive Communication": "budget_cuts_2027",
    "Ethical Leadership & Governance": "ai_powered_disruptor",
    "Empowerment & Cultivating Distributed Leadership": "budget_cuts_2027",
    "Organizational Resilience & Change Agility": "scaling_hypergrowth_2028",
    "Resource Optimization & Strategic Allocation": "budget_cuts_2027",
}


def recommend_next_quest(runs: list[dict]) -> dict:
    """Recommend the next quest based on the participant's weakest principles.

    Args:
        runs: All run summaries for the participant.

    Returns:
        Recommendation dict with quest_id, rationale, and target principle.
    """
    if not runs:
        return {
            "quest_id": "ai_powered_disruptor",
            "rationale": "Start with the flagship scenario to establish your leadership baseline.",
            "target_principle": None,
        }

    trajectory = compute_learning_trajectory(runs)
    growth_areas = trajectory.get("growth_areas", [])

    if not growth_areas:
        return {
            "quest_id": "ai_powered_disruptor",
            "rationale": "Re-run the flagship scenario to improve your overall score.",
            "target_principle": None,
        }

    weakest = growth_areas[0]
    target_principle = weakest["principle"]
    quest_id = QUEST_PRINCIPLE_MAP.get(target_principle, "ai_powered_disruptor")

    # Don't recommend the same quest they just completed
    last_quest = runs[-1].get("quest_id", "")
    if quest_id == last_quest and len(growth_areas) > 1:
        target_principle = growth_areas[1]["principle"]
        quest_id = QUEST_PRINCIPLE_MAP.get(target_principle, "ai_powered_disruptor")

    return {
        "quest_id": quest_id,
        "rationale": f"Your weakest area is '{target_principle}' "
        f"(avg score: {weakest['avg_score']}). "
        f"This quest specifically exercises that principle.",
        "target_principle": target_principle,
    }
