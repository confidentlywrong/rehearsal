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

"""Tests for leadership_planner_with_memory ADK tool wrappers.

Uses the in-memory ``ScenarioMemoryStore`` (via the ``in_memory_store``
fixture in conftest.py) as the ``tools._store`` backend so no AlloyDB is
required.  Mirrors agents/planner_with_memory/tests/test_memory_tools.py.
"""

from types import SimpleNamespace

import pytest

from agents.leadership_planner_with_memory.memory import tools


def _make_ctx(state: dict | None = None) -> SimpleNamespace:
    """Build a minimal ToolContext stand-in with a mutable ``state`` dict."""
    return SimpleNamespace(state=state if state is not None else {})


@pytest.fixture(autouse=True)
def _stub_auto_summary(monkeypatch):
    """No-op — _auto_store_summary was removed in Phase 2D."""
    pass


# ---------------------------------------------------------------------------
# store_scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStoreScenario:
    async def test_error_when_no_scenario_in_state(self, in_memory_store):
        ctx = _make_ctx({})
        result = await tools.store_scenario(ctx)
        assert result["status"] == "error"
        assert "design_leadership_scenario" in result["message"]

    async def test_persists_scenario_from_state(self, in_memory_store):
        ctx = _make_ctx(
            {
                "leadership_scenario": {"title": "Budget Crisis"},
                "leadership_evaluation": {"overall_score": 82.5, "passed": True},
                "selected_persona": "pragmatist",
            }
        )
        result = await tools.store_scenario(ctx)
        assert result["status"] == "success"
        sid = result["scenario_id"]
        assert ctx.state["active_scenario_id"] == sid

        scenario = await in_memory_store.get_scenario(sid)
        assert scenario is not None
        assert scenario.scenario_data["title"] == "Budget Crisis"
        assert scenario.evaluation_score == 82.5
        assert scenario.persona == "pragmatist"

    async def test_bundles_consensus_pathway(self, in_memory_store):
        ctx = _make_ctx(
            {
                "leadership_scenario": {"title": "Crisis"},
                "leadership_evaluation": {"overall_score": 70.0},
                "consensus_pathway_active": {"nodes": [1, 2, 3]},
            }
        )
        result = await tools.store_scenario(ctx)
        scenario = await in_memory_store.get_scenario(result["scenario_id"])
        assert scenario.scenario_data["_consensus_pathway_active"] == {"nodes": [1, 2, 3]}

    async def test_refuses_to_store_when_evaluation_missing(self, in_memory_store):
        # An unscored row would render as "—" in the Organizer STORED card.
        # store_scenario must refuse so the caller runs evaluate_leadership_plan first.
        ctx = _make_ctx({"leadership_scenario": {"title": "x"}})
        result = await tools.store_scenario(ctx)
        assert result["status"] == "needs_evaluation"
        # Nothing was persisted.
        listed = await in_memory_store.recall_scenarios()
        assert listed == []

    async def test_refuses_to_store_when_overall_score_non_numeric(self, in_memory_store):
        ctx = _make_ctx(
            {
                "leadership_scenario": {"title": "x"},
                "leadership_evaluation": {"overall_score": "—", "passed": False},
            }
        )
        result = await tools.store_scenario(ctx)
        assert result["status"] == "needs_evaluation"
        listed = await in_memory_store.recall_scenarios()
        assert listed == []


# ---------------------------------------------------------------------------
# record_scenario_outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecordScenarioOutcome:
    async def test_error_when_no_simulation_result(self, in_memory_store):
        sid = await in_memory_store.store_scenario(scenario_data={"t": 1})
        ctx = _make_ctx({})
        result = await tools.record_scenario_outcome(sid, ctx)
        assert result["status"] == "error"
        assert "simulation_result" in result["message"]

    async def test_error_when_scenario_missing(self, in_memory_store):
        ctx = _make_ctx({"simulation_result": {"passed": True}})
        result = await tools.record_scenario_outcome("missing-id", ctx)
        assert result["status"] == "error"

    async def test_attaches_simulation_record(self, in_memory_store):
        sid = await in_memory_store.store_scenario(scenario_data={"t": 1})
        ctx = _make_ctx({"simulation_result": {"passed": True, "score": 0.9}})
        result = await tools.record_scenario_outcome(sid, ctx)
        assert result["status"] == "success"
        assert result["scenario_id"] == sid
        assert "simulation_id" in result

        scenario = await in_memory_store.get_scenario(sid)
        assert len(scenario.simulations) == 1
        assert scenario.simulations[0].simulation_result == {"passed": True, "score": 0.9}

    async def test_backfills_evaluation_when_originally_unscored(self, in_memory_store):
        # Legacy row stored before evaluation completed (eval_score=None).
        sid = await in_memory_store.store_scenario(scenario_data={"t": 1})
        ctx = _make_ctx(
            {
                "simulation_result": {"passed": True},
                "leadership_evaluation": {"overall_score": 88.0, "passed": True},
            }
        )
        result = await tools.record_scenario_outcome(sid, ctx)
        assert result["status"] == "success"

        scenario = await in_memory_store.get_scenario(sid)
        assert scenario.evaluation_score == 88.0
        assert scenario.evaluation_result == {"overall_score": 88.0, "passed": True}

    async def test_backfill_never_overwrites_existing_score(self, in_memory_store):
        # Existing score must be preserved (audit trail) even when the
        # caller offers a different number.
        sid = await in_memory_store.store_scenario(
            scenario_data={"t": 1},
            evaluation_score=42.0,
            evaluation_result={"overall_score": 42.0, "original": True},
        )
        ctx = _make_ctx(
            {
                "simulation_result": {"passed": True},
                "leadership_evaluation": {"overall_score": 99.0, "original": False},
            }
        )
        await tools.record_scenario_outcome(sid, ctx)

        scenario = await in_memory_store.get_scenario(sid)
        assert scenario.evaluation_score == 42.0
        assert scenario.evaluation_result == {"overall_score": 42.0, "original": True}


# ---------------------------------------------------------------------------
# recall_scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecallScenarios:
    async def test_returns_stored_scenarios(self, in_memory_store):
        await in_memory_store.store_scenario(scenario_data={"title": "A"}, evaluation_score=10.0)
        await in_memory_store.store_scenario(scenario_data={"title": "B"}, evaluation_score=90.0)
        result = await tools.recall_scenarios(count=10, sort_by="best_score")
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["scenarios"][0]["evaluation_score"] == 90.0

    async def test_empty_store(self, in_memory_store):
        result = await tools.recall_scenarios()
        assert result["status"] == "success"
        assert result["count"] == 0
        assert result["scenarios"] == []


# ---------------------------------------------------------------------------
# get_scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetScenario:
    async def test_not_found(self, in_memory_store):
        result = await tools.get_scenario("missing-id")
        assert result["status"] == "error"

    async def test_returns_scenario_dict(self, in_memory_store):
        sid = await in_memory_store.store_scenario(scenario_data={"title": "Crisis"}, persona="bold_visionary")
        result = await tools.get_scenario(sid)
        assert result["status"] == "success"
        assert result["scenario"]["scenario_id"] == sid
        assert result["scenario"]["persona"] == "bold_visionary"

    async def test_activate_scenario_loads_state(self, in_memory_store):
        sid = await in_memory_store.store_scenario(
            scenario_data={"title": "Crisis", "_consensus_pathway_active": {"n": 1}},
            evaluation_score=77.0,
            evaluation_result={"overall_score": 77.0, "passed": True},
            persona="pragmatist",
        )
        ctx = _make_ctx({})
        result = await tools.get_scenario(sid, tool_context=ctx, activate_scenario=True)
        assert result["status"] == "success"
        assert result.get("activated") is True
        assert ctx.state["active_scenario_id"] == sid
        assert ctx.state["leadership_scenario"]["title"] == "Crisis"
        assert ctx.state["consensus_pathway_active"] == {"n": 1}
        assert ctx.state["selected_persona"] == "pragmatist"
        assert ctx.state["leadership_evaluation"]["overall_score"] == 77.0


# ---------------------------------------------------------------------------
# get_best_scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetBestScenario:
    async def test_empty(self, in_memory_store):
        result = await tools.get_best_scenario()
        assert result["status"] == "error"

    async def test_returns_highest_score(self, in_memory_store):
        await in_memory_store.store_scenario(scenario_data={}, evaluation_score=40.0)
        winner = await in_memory_store.store_scenario(scenario_data={}, evaluation_score=95.0)
        await in_memory_store.store_scenario(scenario_data={})  # unscored
        result = await tools.get_best_scenario()
        assert result["status"] == "success"
        assert result["scenario"]["scenario_id"] == winner


# ---------------------------------------------------------------------------
# Per-user owner_id resolution + propagation
# ---------------------------------------------------------------------------


def _make_ctx_with_user(user_id: str | None, state: dict | None = None) -> SimpleNamespace:
    """Build a ToolContext stand-in with a session.user_id attribute."""
    session = SimpleNamespace(user_id=user_id) if user_id is not None else None
    return SimpleNamespace(state=state if state is not None else {}, session=session)


class TestResolveOwnerId:
    def test_returns_none_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("LEADERSHIP_PER_USER_ISOLATION", raising=False)
        ctx = _make_ctx_with_user("alice@example.com")
        assert tools._resolve_owner_id(ctx) is None

    def test_returns_user_id_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        ctx = _make_ctx_with_user("alice@example.com")
        assert tools._resolve_owner_id(ctx) == "alice@example.com"

    def test_none_when_session_missing(self, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        ctx = SimpleNamespace(state={})
        assert tools._resolve_owner_id(ctx) is None

    def test_none_when_tool_context_is_none(self, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        assert tools._resolve_owner_id(None) is None

    @pytest.mark.parametrize("uid", ["", "anonymous", "Default", "LOCAL-DEV"])
    def test_none_for_anonymous_user_ids(self, monkeypatch, uid):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        ctx = _make_ctx_with_user(uid)
        assert tools._resolve_owner_id(ctx) is None

    def test_none_for_non_string_user_id(self, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        ctx = _make_ctx_with_user(12345)  # type: ignore[arg-type]
        assert tools._resolve_owner_id(ctx) is None


@pytest.mark.asyncio
class TestOwnerIdPropagation:
    """Verify owner_id flows from ToolContext.session.user_id into the store."""

    async def test_store_scenario_tags_owner_when_flag_on(self, in_memory_store, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        ctx = _make_ctx_with_user(
            "alice@example.com",
            {
                "leadership_scenario": {"title": "Budget Crisis"},
                "leadership_evaluation": {"overall_score": 80.0, "passed": True},
            },
        )
        result = await tools.store_scenario(ctx)
        assert result["status"] == "success"
        scenario = await in_memory_store.get_scenario(result["scenario_id"])
        assert scenario.owner_id == "alice@example.com"

    async def test_store_scenario_leaves_owner_none_when_flag_off(self, in_memory_store, monkeypatch):
        monkeypatch.delenv("LEADERSHIP_PER_USER_ISOLATION", raising=False)
        ctx = _make_ctx_with_user(
            "alice@example.com",
            {
                "leadership_scenario": {"title": "Budget Crisis"},
                "leadership_evaluation": {"overall_score": 80.0, "passed": True},
            },
        )
        result = await tools.store_scenario(ctx)
        assert result["status"] == "success"
        scenario = await in_memory_store.get_scenario(result["scenario_id"])
        assert scenario.owner_id is None

    async def test_recall_scenarios_isolates_users(self, in_memory_store, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        # Seed two owners directly via the store.
        await in_memory_store.store_scenario(
            scenario_data={"t": "alice"}, evaluation_score=90.0, owner_id="alice@example.com"
        )
        await in_memory_store.store_scenario(
            scenario_data={"t": "bob"}, evaluation_score=70.0, owner_id="bob@example.com"
        )
        await in_memory_store.store_scenario(scenario_data={"t": "legacy"}, evaluation_score=50.0)  # unowned legacy row

        alice_ctx = _make_ctx_with_user("alice@example.com")
        result = await tools.recall_scenarios(tool_context=alice_ctx)
        titles = {s["scenario_data"]["t"] for s in result["scenarios"]}
        assert titles == {"alice"}

    async def test_get_best_scenario_is_owner_scoped(self, in_memory_store, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        # Bob has the globally-best score, but Alice should still see her own best.
        await in_memory_store.store_scenario(
            scenario_data={"t": "alice"}, evaluation_score=60.0, owner_id="alice@example.com"
        )
        await in_memory_store.store_scenario(
            scenario_data={"t": "bob"}, evaluation_score=99.0, owner_id="bob@example.com"
        )
        alice_ctx = _make_ctx_with_user("alice@example.com")
        result = await tools.get_best_scenario(tool_context=alice_ctx)
        assert result["status"] == "success"
        assert result["scenario"]["scenario_data"]["t"] == "alice"

    async def test_get_scenario_blocks_cross_user_read(self, in_memory_store, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        sid = await in_memory_store.store_scenario(
            scenario_data={"t": "bob"}, evaluation_score=90.0, owner_id="bob@example.com"
        )
        alice_ctx = _make_ctx_with_user("alice@example.com")
        result = await tools.get_scenario(scenario_id=sid, tool_context=alice_ctx)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# store_stakeholder_artifacts / recall_stakeholder_artifacts
# ---------------------------------------------------------------------------


def _make_full_state(scenario_id: str) -> dict:
    """Session state populated with all six artifact keys + active id."""
    return {
        "active_scenario_id": scenario_id,
        "stakeholder_graph": {"nodes": ["ceo", "vp_eng"], "edges": []},
        "stakeholder_personas": [{"id": "ceo", "stance": "champion"}],
        "stakeholder_reactions": [{"persona": "ceo", "delta": +0.4}],
        "simulation_timeline": [{"tick": 1, "event": "mandate"}],
        "simulation_report": {"overall_score": 75, "summary": "ok"},
        "stakeholder_interview": [{"persona": "ceo", "qna": [{"q": "?", "a": "yes"}]}],
    }


@pytest.mark.asyncio
class TestStakeholderArtifacts:
    async def _seed_scenario(self, store, owner_id: str | None = None) -> str:
        return await store.store_scenario(
            scenario_data={"title": "demo"},
            evaluation_score=80.0,
            owner_id=owner_id,
        )

    async def test_store_success_full_bundle(self, in_memory_store):
        sid = await self._seed_scenario(in_memory_store)
        ctx = _make_ctx(_make_full_state(sid))

        result = await tools.store_stakeholder_artifacts(ctx)

        assert result["status"] == "success"
        assert result["scenario_id"] == sid
        assert set(result["artifact_keys_stored"]) == {
            "stakeholder_graph",
            "stakeholder_personas",
            "stakeholder_reactions",
            "simulation_timeline",
            "simulation_report",
            "stakeholder_interview",
        }
        bundle = await in_memory_store.get_artifacts(sid)
        assert bundle is not None
        assert bundle["simulation_report"]["overall_score"] == 75

    async def test_store_error_when_no_active_scenario_id(self, in_memory_store):
        ctx = _make_ctx({"stakeholder_graph": {"nodes": []}})
        result = await tools.store_stakeholder_artifacts(ctx)
        assert result["status"] == "error"
        assert "store_scenario" in result["message"]

    async def test_store_error_when_no_artifacts_present(self, in_memory_store):
        sid = await self._seed_scenario(in_memory_store)
        ctx = _make_ctx({"active_scenario_id": sid})
        result = await tools.store_stakeholder_artifacts(ctx)
        assert result["status"] == "error"

    async def test_store_partial_only_persists_present_keys(self, in_memory_store):
        sid = await self._seed_scenario(in_memory_store)
        ctx = _make_ctx(
            {
                "active_scenario_id": sid,
                "stakeholder_graph": {"nodes": ["ceo"]},
                "simulation_report": {"overall_score": 50},
            }
        )
        result = await tools.store_stakeholder_artifacts(ctx)
        assert result["status"] == "success"
        assert set(result["artifact_keys_stored"]) == {
            "stakeholder_graph",
            "simulation_report",
        }
        bundle = await in_memory_store.get_artifacts(sid)
        assert set(bundle.keys()) == {"stakeholder_graph", "simulation_report"}

    async def test_store_merge_preserves_earlier_keys(self, in_memory_store):
        # Two consecutive partial flushes should yield a merged bundle.
        sid = await self._seed_scenario(in_memory_store)

        first = _make_ctx(
            {
                "active_scenario_id": sid,
                "stakeholder_graph": {"nodes": ["ceo"]},
            }
        )
        await tools.store_stakeholder_artifacts(first)

        second = _make_ctx(
            {
                "active_scenario_id": sid,
                "simulation_report": {"overall_score": 90},
            }
        )
        result = await tools.store_stakeholder_artifacts(second)
        assert result["status"] == "success"

        bundle = await in_memory_store.get_artifacts(sid)
        assert bundle["stakeholder_graph"] == {"nodes": ["ceo"]}
        assert bundle["simulation_report"] == {"overall_score": 90}

    async def test_store_error_when_scenario_unknown(self, in_memory_store):
        ctx = _make_ctx(_make_full_state("does-not-exist"))
        result = await tools.store_stakeholder_artifacts(ctx)
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    async def test_recall_rehydrates_state_keys(self, in_memory_store):
        sid = await self._seed_scenario(in_memory_store)
        write_ctx = _make_ctx(_make_full_state(sid))
        await tools.store_stakeholder_artifacts(write_ctx)

        # Fresh context — none of the state keys are present.
        read_ctx = _make_ctx({})
        result = await tools.recall_stakeholder_artifacts(
            scenario_id=sid, tool_context=read_ctx
        )

        assert result["status"] == "success"
        assert result["scenario_id"] == sid
        assert set(result["rehydrated_keys"]) == {
            "stakeholder_graph",
            "stakeholder_personas",
            "stakeholder_reactions",
            "simulation_timeline",
            "simulation_report",
            "stakeholder_interview",
        }
        # State has been rehydrated.
        assert read_ctx.state["simulation_report"]["overall_score"] == 75
        # And the recalled scenario is marked active for downstream tools.
        assert read_ctx.state["active_scenario_id"] == sid

    async def test_recall_not_found(self, in_memory_store):
        ctx = _make_ctx({})
        result = await tools.recall_stakeholder_artifacts(
            scenario_id="missing-sid", tool_context=ctx
        )
        assert result["status"] == "not_found"
        assert "missing-sid" in result["message"]
        # State must not be polluted on a miss.
        assert "active_scenario_id" not in ctx.state
        assert "stakeholder_graph" not in ctx.state

    async def test_store_owner_isolation(self, in_memory_store, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        sid = await self._seed_scenario(in_memory_store, owner_id="bob@example.com")

        alice_ctx = _make_ctx_with_user("alice@example.com", _make_full_state(sid))
        result = await tools.store_stakeholder_artifacts(alice_ctx)
        assert result["status"] == "error"
        assert await in_memory_store.get_artifacts(sid) is None

    async def test_recall_owner_isolation(self, in_memory_store, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        sid = await self._seed_scenario(in_memory_store, owner_id="bob@example.com")

        bob_ctx = _make_ctx_with_user("bob@example.com", _make_full_state(sid))
        ok = await tools.store_stakeholder_artifacts(bob_ctx)
        assert ok["status"] == "success"

        alice_ctx = _make_ctx_with_user("alice@example.com", {})
        result = await tools.recall_stakeholder_artifacts(
            scenario_id=sid, tool_context=alice_ctx
        )
        assert result["status"] == "not_found"
