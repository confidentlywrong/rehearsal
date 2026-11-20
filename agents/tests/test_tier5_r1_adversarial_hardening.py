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

"""Tier 5 Round 1 adversarial regression tests for Python agents, memory, and simulator."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.ai_echo import initialization as echo_init
from agents.ai_echo import process_decision_round as echo_round
from agents.ai_echo_autopilot import autopilot as echo_autopilot
from agents.leadership_planner_with_eval.evaluator import tools as eval_tools
from agents.leadership_planner_with_eval.evaluator.config import EVALUATION_CRITERIA
from agents.leadership_planner_with_memory.memory import retrieval_tools
from agents.leadership_planner_with_memory.memory import store as leadership_mem_store
from agents.leadership_planner_with_memory.memory import store_alloydb as leadership_alloydb
from agents.leadership_planner_with_memory.memory import tools as leadership_mem_tools
from agents.leadership_planner_with_memory.memory.schemas import PlannedScenario
from agents.leadership_runner import leadership_tick
from agents.leadership_runner_autopilot import autopilot as leadership_runner_autopilot
from agents.planner_with_memory.memory import store_alloydb as planner_alloydb
from agents.planner_with_memory.memory import tools as planner_mem_tools
from agents.runner import running as marathon_running
from agents.runner_autopilot import autopilot as runner_autopilot
from agents.simulator import ari, coaching, decision_classifier, ecsi, influence_network, live_dashboard, longitudinal
from agents.utils import communication

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_skill_module(name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CONSULTANT = _load_skill_module(
    "t5_consultant_tools",
    "agents/leadership_planner_with_eval/skills/consultant/tools.py",
)
_PRE_RACE = _load_skill_module(
    "t5_pre_race_tools",
    "agents/simulator/skills/preparing-the-race/tools.py",
)
_TICK_TOOLS = _load_skill_module(
    "t5_tick_tools",
    "agents/simulator/skills/advancing-race-ticks/tools.py",
)
_POST_RACE = _load_skill_module(
    "t5_post_race_tools",
    "agents/simulator/skills/completing-the-race/tools.py",
)


def _ctx(state: dict | None = None, session_id: str = "sess-t5") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id, user_id="user-t5"),
    )


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.calls.append((query, args))
        return []


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


# ---------------------------------------------------------------------------
# Bug 1: planner_with_memory/memory/tools.py limit clamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_limit", "expected_for_10", "expected_for_5"),
    [
        (float("nan"), 10, 5),
        (float("inf"), 10, 5),
        (float("-inf"), 10, 5),
        ("abc", 10, 5),
        (True, 10, 5),
        (False, 10, 5),
        (None, 10, 5),
        (object(), 10, 5),
        (10**400, 500, 500),
        (-5, 0, 0),
        ("10.5", 10, 10),
    ],
)
async def test_planner_recall_routes_and_get_planned_routes_data_clamp_adversarial_limits(
    monkeypatch, bad_limit, expected_for_10, expected_for_5
):
    seen_counts: list[int] = []

    async def _fake_recall_routes(*, count: int, sort_by: str, owner_id=None):
        seen_counts.append(count)
        return []

    fake_store = SimpleNamespace(recall_routes=_fake_recall_routes)
    monkeypatch.setattr(planner_mem_tools, "_store", fake_store)

    res1 = await planner_mem_tools.recall_routes(_ctx(), count=bad_limit)
    assert res1["status"] == "success"
    assert res1["routes"] == []

    res2 = await planner_mem_tools.get_planned_routes_data(_ctx(), limit=bad_limit)
    assert res2["status"] == "error"

    assert seen_counts == [expected_for_10, expected_for_5]


# ---------------------------------------------------------------------------
# Bug 2: consultant/tools.py OverflowError on >= 10**307 status counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consultant_emit_sim_results_a2ui_handles_huge_status_counts_without_overflow():
    huge = 10**308
    ctx = _ctx(
        {
            "simulation_id": "sim-huge-1",
            "leadership_scenario": {"name": "Huge Counts", "stakeholder_count": 4},
            "leadership_evaluation": {
                "overall_score": 82,
                "scores": {c: 80 for c in EVALUATION_CRITERIA},
            },
            # Exercise lines 550 (Visionary/Diplomat progress=None -> elif counts),
            # 584 (Pragmatist/Disruptor progress=0.0 -> best_persona stays "—"), and 927
            "persona_avg_progress": {
                "Visionary": None,
                "Diplomat": None,
                "Pragmatist": 0.0,
                "Disruptor": 0.0,
            },
            "persona_status_counts": {
                "Visionary": {"aligned": huge, "stalled": huge},
                "Diplomat": {"aligned": huge, "stalled": 3 * huge},
            },
        }
    )

    emitted: list[dict] = []

    async def _capture_emit(**kwargs):
        emitted.append(kwargs["data"])

    with (
        patch.object(_CONSULTANT, "emit_gateway_message", side_effect=_capture_emit),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={})),
    ):
        res = await _CONSULTANT.emit_sim_results_a2ui(ctx)

    assert "ERROR:" not in res
    assert len(emitted) == 2
    components = emitted[0]["surfaceUpdate"]["components"]
    by_id = {c["id"]: c["component"] for c in components}
    assert by_id["vis-bar"]["ScoreBar"]["value"]["literalNumber"] == 50
    assert by_id["dip-bar"]["ScoreBar"]["value"]["literalNumber"] == 25
    assert by_id["winning-desc"]["Text"]["text"]["literalString"] == "Visionary"
    assert by_id["status-align-val"]["Text"]["text"]["literalString"] == "33%"


def test_consultant_safe_ratio_helpers_exact_and_overflow_safe():
    assert _CONSULTANT._safe_ratio_pct_int(10**400, 2 * 10**400) == 50
    assert _CONSULTANT._safe_ratio_pct_int(2 * 10**400, 3 * 10**400) == 67
    assert _CONSULTANT._safe_ratio_pct_int(1, 0) == 0
    assert _CONSULTANT._safe_ratio_pct_float(10**400, 4 * 10**400) == pytest.approx(25.0)
    assert _CONSULTANT._safe_ratio_pct_float(1, 0) == 0.0


# ---------------------------------------------------------------------------
# Bug 3: retrieval_tools.py catches httpx.InvalidURL on malformed gateway URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_document_passages_malformed_gateway_url_returns_gateway_error(monkeypatch):
    monkeypatch.setenv("MEMORY_GATEWAY_URL", "http://[::1")
    ctx = _ctx({})

    res_auto = await retrieval_tools.retrieve_document_passages("budget policy", ctx)
    assert res_auto["status"] == "gateway_error"
    assert res_auto["passages"] == []

    res_explicit = await retrieval_tools.retrieve_document_passages(
        "budget policy", ctx, analysis_id="analysis-123"
    )
    assert res_explicit["status"] == "gateway_error"
    assert res_explicit["passages"] == []


# ---------------------------------------------------------------------------
# Bug 4: evaluator/tools.py filters extra score keys & hardens _build_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_leadership_plan_drops_unexpected_non_numeric_score_keys(monkeypatch):
    payload = {
        "scores": {
            "stakeholder_alignment": 70,
            "decision_quality": 75,
            "resource_stewardship": 80,
            "crisis_readiness": 85,
            "strategic_coherence": 90,
            "ethical_alignment": 95,
            "extra_bad_key": "not_an_int",
        },
        "findings": [],
        "summary": "Solid plan",
    }

    monkeypatch.setattr(eval_tools, "is_local_gemma_active", lambda: True)
    monkeypatch.setattr(
        eval_tools,
        "generate_local_json_async",
        AsyncMock(
            side_effect=[
                json.dumps(payload),
                json.dumps(
                    {
                        "summary": "Done",
                        "suggestions": ["Add executive sponsor checkpoints."],
                    }
                ),
            ]
        ),
    )

    result = await eval_tools.evaluate_leadership_plan('{"proposed_plan": "Test plan"}', _ctx({}))
    assert result["status"] == "success"
    assert set(result["scores"].keys()) == set(EVALUATION_CRITERIA)
    assert "extra_bad_key" not in result["scores"]
    assert all(isinstance(v, int) for v in result["scores"].values())


def test_build_result_hardens_non_numeric_scores_without_rescaling_small_ints():
    scores = {
        "stakeholder_alignment": "not_an_int",
        "decision_quality": True,
        "resource_stewardship": float("nan"),
        "crisis_readiness": 10**400,
        "strategic_coherence": 4,
        "ethical_alignment": 85,
    }
    details = {c: f"detail-{c}" for c in EVALUATION_CRITERIA}
    result = eval_tools._build_result(scores, details, eval_method="test", findings=None)  # type: ignore[arg-type]
    assert result["scores"]["stakeholder_alignment"] == 0
    assert result["scores"]["decision_quality"] == 0
    assert result["scores"]["resource_stewardship"] == 0
    assert result["scores"]["crisis_readiness"] == 100
    assert result["scores"]["strategic_coherence"] == 4
    assert result["scores"]["ethical_alignment"] == 85
    assert any("Strategic Coherence" in f["title"] for f in result["findings"])


# ---------------------------------------------------------------------------
# Bugs 5 & 6: store_alloydb.py & store.py limit clamping & _sanitize_score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_limit", "expected_for_10", "expected_for_100"),
    [
        (float("nan"), 10, 100),
        (float("inf"), 10, 100),
        (float("-inf"), 10, 100),
        ("abc", 10, 100),
        ("10.5", 10, 10),
        (True, 10, 100),
        (object(), 10, 100),
        (10**400, 500, 500),
    ],
)
async def test_memory_stores_clamp_adversarial_limits(
    monkeypatch, bad_limit, expected_for_10, expected_for_100
):
    conn = _FakeConn()
    fake_pool = _FakePool(conn)
    monkeypatch.setattr(leadership_alloydb, "_get_pool", AsyncMock(return_value=fake_pool))
    monkeypatch.setattr(planner_alloydb, "_get_pool", AsyncMock(return_value=fake_pool))

    l_alloy = leadership_alloydb.AlloyDBScenarioStore()
    p_alloy = planner_alloydb.AlloyDBRouteStore()
    l_mem = leadership_mem_store.ScenarioMemoryStore()
    await l_mem.store_scenario(scenario_data={"name": "S1"}, evaluation_score=80.0)

    await l_alloy.recall_scenarios(count=bad_limit)
    await l_alloy.list_scenario_bundles(limit=bad_limit)
    await p_alloy.recall_routes(count=bad_limit)

    assert conn.calls[0][1][-1] == expected_for_10
    assert conn.calls[1][1][-1] == expected_for_100
    assert conn.calls[2][1][-1] == expected_for_10

    assert len(await l_mem.recall_scenarios(count=bad_limit)) == 1
    assert len(await l_mem.list_scenario_bundles(limit=bad_limit)) == 1
    assert await l_mem.recall_scenarios(count=-1) == []
    assert await l_mem.list_scenario_bundles(limit=-1) == []


@pytest.mark.parametrize("bad_score", [10**400, float("nan"), float("inf"), float("-inf"), True, "bad", None])
def test_sanitize_score_handles_overflow_and_non_finite(bad_score):
    assert leadership_alloydb._sanitize_score(bad_score) is None
    assert planner_alloydb._sanitize_score(bad_score) is None
    assert leadership_mem_store._sanitize_score(bad_score) is None


# ---------------------------------------------------------------------------
# Bug 7: leadership_planner_with_memory/memory/tools.py subscores & None ctx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_scenario_list_a2ui_rejects_inf_nan_bool_and_overflow_scores(monkeypatch):
    bad_scenario = PlannedScenario(
        scenario_id="sc-inf-1234",
        scenario_data={"name": "Adversarial Scenario"},
        created_at=datetime.now(tz=timezone.utc),
        evaluation_score=float("inf"),
        evaluation_result={
            "scores": {
                "stakeholder_alignment": float("inf"),
                "decision_quality": float("-inf"),
                "crisis_readiness": float("nan"),
                "strategic_coherence": True,
                "ethical_alignment": "bad",
                "resource_stewardship": 85,
            }
        },
    )

    fake_store = SimpleNamespace(recall_scenarios=AsyncMock(return_value=[bad_scenario]))
    monkeypatch.setattr(leadership_mem_tools, "_store", fake_store)

    emitted: list[dict] = []

    async def _capture_emit(**kwargs):
        emitted.append(kwargs["data"])

    with patch("agents.utils.pulses.emit_gateway_message", side_effect=_capture_emit):
        res = await leadership_mem_tools.emit_scenario_list_a2ui(_ctx({}))

    assert res["status"] == "success"
    components = emitted[0]["surfaceUpdate"]["components"]
    by_id = {c["id"]: c["component"] for c in components}
    assert by_id["score-num-1"]["Text"]["text"]["literalString"] == "\u2014"
    assert "sa-1" not in by_id
    assert "dq-1" not in by_id
    assert "cr-1" not in by_id
    assert "sc-1" not in by_id
    assert "ea-1" not in by_id
    assert by_id["rs-1"]["ScoreBar"]["value"]["literalNumber"] == 85


@pytest.mark.asyncio
async def test_leadership_memory_tools_guard_none_tool_context_and_none_state(monkeypatch):
    fake_store = SimpleNamespace(
        get_artifacts=AsyncMock(return_value={"stakeholder_graph": {"nodes": []}}),
    )
    monkeypatch.setattr(leadership_mem_tools, "_store", fake_store)

    for ctx in (None, SimpleNamespace(state=None)):
        assert (await leadership_mem_tools.store_scenario(ctx))["status"] == "error"
        assert (await leadership_mem_tools.record_scenario_outcome("sc-1", ctx))["status"] == "error"
        assert (await leadership_mem_tools.store_stakeholder_artifacts(ctx))["status"] == "error"
        rehydrated = await leadership_mem_tools.recall_stakeholder_artifacts("sc-1", ctx)
        assert rehydrated["status"] == "success"
        assert rehydrated["rehydrated_keys"] == []


# ---------------------------------------------------------------------------
# Bug 8: communication._get_discovery_client loop- and closed-aware
# ---------------------------------------------------------------------------


def test_get_discovery_client_recreates_when_closed_or_loop_changes():
    communication._discovery_client = None
    communication._discovery_client_loop = None

    async def _get_and_close() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        c1 = communication._get_discovery_client()
        await c1.aclose()
        c2 = communication._get_discovery_client()
        return c1, c2

    c1, c2 = asyncio.run(_get_and_close())
    assert c1 is not c2
    assert c1.is_closed is True
    assert c2.is_closed is False

    async def _get_in_new_loop() -> httpx.AsyncClient:
        return communication._get_discovery_client()

    c3 = asyncio.run(_get_in_new_loop())
    assert c3 is not c2
    assert c3.is_closed is False

    asyncio.run(c2.aclose())
    asyncio.run(c3.aclose())
    communication._discovery_client = None
    communication._discovery_client_loop = None


# ---------------------------------------------------------------------------
# Bug 9: preparing-the-race/tools.py invalid count & echo_session_ids=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_count", [None, "5", True, False, 0, -3, 2.5])
async def test_spawn_runners_rejects_invalid_count(bad_count):
    ctx = _ctx({"simulation_ready": True, "runner_count": 4, "simulation_id": "sim-1"})
    res = await _PRE_RACE.spawn_runners(bad_count, ctx)
    assert res["status"] == "error"
    assert "Invalid runner count" in res["message"]


@pytest.mark.asyncio
async def test_fire_start_gun_handles_explicit_none_echo_session_ids():
    ctx = _ctx(
        {
            "runner_session_ids": ["r-1"],
            "echo_session_ids": None,
            "simulation_id": "sim-1",
            "simulation_config": {"race_distance_mi": 5.0},
        }
    )
    with (
        patch("agents.simulator.collector.RaceCollector.is_running", return_value=False),
        patch("agents.simulator.collector.RaceCollector.start", new=AsyncMock()) as mock_start,
        patch.object(_PRE_RACE, "wait_for_runners_ready", new=AsyncMock()) as mock_wait,
        patch.object(_PRE_RACE, "publish_to_runners", new=AsyncMock()),
    ):
        res = await _PRE_RACE.fire_start_gun(ctx)

    assert res["status"] == "success"
    assert mock_start.call_args.kwargs["runner_session_ids"] == {"r-1"}
    assert mock_wait.call_args.args[0] == ["r-1"]


# ---------------------------------------------------------------------------
# Bug 10: advancing-race-ticks/tools.py non-dict message guards
# ---------------------------------------------------------------------------


def test_sample_friction_thoughts_skips_non_dict_messages():
    assert _TICK_TOOLS._sample_friction_thoughts(["not-a-dict", None, 42]) == []


@pytest.mark.asyncio
async def test_advance_tick_skips_non_dict_messages_in_runner_loop():
    ctx = _ctx(
        {
            "simulation_ready": True,
            "current_tick": 1,
            "max_ticks": 5,
            "simulation_config": {"tick_interval_seconds": 0},
            "runner_session_ids": ["r-1"],
            "echo_session_ids": [],
            "simulation_id": "sim-1",
        }
    )
    fake_collector = MagicMock()
    fake_collector.drain = AsyncMock(return_value=["corrupt-string-msg", 123])

    with (
        patch.object(_TICK_TOOLS.RaceCollector, "get", return_value=fake_collector),
        patch.object(_TICK_TOOLS, "publish_to_runners", new=AsyncMock()),
        patch.object(_TICK_TOOLS, "emit_gateway_message", new=AsyncMock()),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock()),
    ):
        res = await _TICK_TOOLS.advance_tick(ctx)

    assert res["status"] == "success"
    assert res["tick"] == 1


# ---------------------------------------------------------------------------
# Bug 11: _finite_float OverflowError on huge ints (10**400)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn",
    [
        echo_round._finite_float,
        echo_autopilot._finite_float,
        echo_init._finite_float,
        leadership_runner_autopilot._finite_float,
        runner_autopilot._finite_float,
        ari._finite_float,
        ecsi._finite_float,
        live_dashboard._finite_float,
        longitudinal._finite_float,
        influence_network._finite_float,
        coaching._finite_float,
        decision_classifier._finite_float,
        marathon_running._finite_float,
        leadership_tick._finite_float,
        _TICK_TOOLS._finite_float,
        _POST_RACE._finite_float,
    ],
)
def test_finite_float_handles_huge_int_overflow(fn):
    assert fn(10**400, 3.5) == 3.5
    assert fn(float("inf"), 3.5) == 3.5
    assert fn(float("nan"), 3.5) == 3.5
    assert fn(True, 3.5) == 3.5
    assert fn(None, 3.5) == 3.5
    assert fn("not-a-float", 3.5) == 3.5
    assert fn(42, 3.5) == 42.0
