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

"""Regression tests for Round-3 bug fixes across planner, eval, memory, skills, and utils."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.events.event import Event
from google.adk.sessions.session import Session
from google.adk.sessions.state import State
from starlette.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(module_name: str, relative_path: str):
    path = _REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# planner_with_eval/evaluator/tools.py & planner_with_eval/tools.py
# ---------------------------------------------------------------------------


def test_normalize_score_handles_nan_inf_bool_and_non_numeric():
    from agents.planner_with_eval.evaluator.tools import _normalize_score

    assert _normalize_score(float("nan")) == 0
    assert _normalize_score(float("inf")) == 0
    assert _normalize_score(float("-inf")) == 0
    assert _normalize_score(True) == 0  # type: ignore[arg-type]
    assert _normalize_score(False) == 0  # type: ignore[arg-type]
    assert _normalize_score("not-a-number") == 0  # type: ignore[arg-type]
    assert _normalize_score(4.5) == 90
    assert _normalize_score(0.85) == 85


@pytest.mark.asyncio
async def test_evaluate_plan_handles_non_dict_and_none_without_crashing():
    from agents.planner_with_eval.evaluator.tools import evaluate_plan

    ctx = SimpleNamespace(state=State({}, {}))

    for bad_input in ("null", "[]", "42", "{not json", None):
        res = await evaluate_plan(bad_input, tool_context=ctx)  # type: ignore[arg-type]
        assert res["passed"] is False
        assert res["eval_method"] == "error"
        assert res["overall_score"] == 0
        assert res["findings"][0]["criterion"] == "parse_error"

    assert ctx.state["evaluation_result"]["eval_method"] == "error"


@pytest.mark.asyncio
async def test_evaluate_plan_does_not_mutate_caller_dict_and_single_pass_prompt():
    from agents.planner_with_eval.evaluator.tools import evaluate_plan

    caller_plan = {"distance": 26.2, "note": "Keep this plan intact"}
    caller_plan_before = copy.deepcopy(caller_plan)
    request = {
        "user_intent": "Must include {response} verbatim token",
        "proposed_plan": caller_plan,
    }
    ctx = SimpleNamespace(state=State({"marathon_route": {"type": "FeatureCollection"}}, {}))
    captured_prompts: list[str] = []

    async def fake_local_json_with_nan(prompt, **_kwargs):
        captured_prompts.append(prompt)
        return json.dumps(
            {
                "explanation": {
                    "safety_compliance": 4,
                    "logistics_completeness": 4.5,
                    "participant_experience": 4,
                    "community_impact": 4,
                    "financial_viability": "nan",
                    "intent_alignment": 5,
                }
            }
        )

    with (
        patch("agents.utils.local_ai.is_local_gemma_active", return_value=True),
        patch("agents.utils.local_ai.generate_local_json_async", side_effect=fake_local_json_with_nan),
    ):
        result = await evaluate_plan(request, tool_context=ctx)  # type: ignore[arg-type]

    # Caller dict must remain untouched even though state enriched the evaluation payload.
    assert caller_plan == caller_plan_before
    assert "route_geometry_details" not in caller_plan

    # Prompt injection check: literal {response} in user_intent must NOT be substituted again.
    assert len(captured_prompts) == 1
    assert "Must include {response} verbatim token" in captured_prompts[0]
    assert "Keep this plan intact" in captured_prompts[0]

    # NaN score criterion must be ignored cleanly rather than raising ValueError on round(nan).
    assert "financial_viability" not in result["scores"]
    assert result["scores"]["safety_compliance"] == 80
    assert result["eval_method"] == "error"


@pytest.mark.asyncio
async def test_generate_feedback_rejects_non_dict_json():
    from agents.planner_with_eval.evaluator.tools import _generate_feedback

    mock_response = SimpleNamespace(text='["not", "a", "dict"]')
    mock_client = SimpleNamespace(
        models=SimpleNamespace(generate_content=MagicMock(return_value=mock_response))
    )
    with (
        patch("agents.utils.local_ai.is_local_gemma_active", return_value=False),
        patch("google.genai.Client", return_value=mock_client),
        pytest.raises(ValueError, match="Expected JSON object for feedback"),
    ):
        await _generate_feedback(
            {"distance_compliance": 95},
            {"distance_compliance": "Good"},
            "Design 26.2 mile race",
            "Route summary",
        )


@pytest.mark.asyncio
async def test_planner_with_eval_tools_do_not_mutate_simulation_config():
    from agents.planner_with_eval.tools import start_simulation, submit_plan_to_simulator

    original_config = {"duration_seconds": 60, "runner_count": 200}
    original_snapshot = copy.deepcopy(original_config)
    state = State({"marathon_route": {"type": "FeatureCollection"}}, {})
    ctx = SimpleNamespace(state=state, invocation_id="inv-1")

    res = await start_simulation(
        action="execute",
        tool_context=ctx,  # type: ignore[arg-type]
        simulation_config=original_config,
        runner_type="runner",
    )
    assert res["status"] == "ready"
    assert original_config == original_snapshot
    assert "runner_type" not in original_config
    assert state["simulation_config"]["runner_type"] == "runner"

    state_submit = State(
        {
            "marathon_route": {"type": "FeatureCollection"},
            "_sim_token": "tok-1",
            "simulator_session_id": "tok-1",
        },
        {},
    )
    submit_ctx = SimpleNamespace(state=state_submit, invocation_id="inv-2")
    submit_config = {"duration_seconds": 30}
    submit_snapshot = copy.deepcopy(submit_config)

    with (
        patch("agents.utils.simdata.store_simulation_data", AsyncMock(return_value=True)),
        patch("agents.utils.communication.call_agent", AsyncMock(return_value="ok")) as mocked_call,
    ):
        res_verify = await submit_plan_to_simulator(
            action="verify",
            tool_context=submit_ctx,  # type: ignore[arg-type]
            simulation_config=submit_config,
            runner_type="runner_gke",
        )
    assert res_verify["status"] == "success"
    assert submit_config == submit_snapshot
    assert "runner_type" not in submit_config
    sent_payload = json.loads(mocked_call.call_args.kwargs["message"])
    assert sent_payload["simulation_config"]["runner_type"] == "runner_gke"


# ---------------------------------------------------------------------------
# leadership_planner_with_eval/evaluator/tools.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leadership_evaluator_rejects_bool_and_handles_zero_initial_budget_state():
    from agents.leadership_planner_with_eval.evaluator.tools import (
        _normalize_score,
        evaluate_leadership_plan,
    )

    assert _normalize_score(True) == 0
    assert _normalize_score(False) == 0

    state = State(
        {
            "leadership_scenario": {
                "name": "Zero Budget Scenario",
                "stakeholders": [{"name": "Ops", "tier": 1}],
                "resources": {"budget": {"initial": 0}},
                "black_swan_events": [],
            },
            "consensus_pathway_active": {
                "waypoints": [{"decision": "Emergency spend"}],
                "cost": {"budget": 10},
                "milestones": ["M1"],
            },
        },
        {},
    )
    ctx = SimpleNamespace(state=state)

    async def fake_local_json(prompt: str, **_kwargs):
        if '"suggestions"' in prompt:
            return json.dumps({"suggestions": ["Protect contingency reserve."], "summary": "Checked."})
        return json.dumps(
            {
                "scores": {
                    "stakeholder_alignment": 80,
                    "decision_quality": 80,
                    "resource_stewardship": True,  # bool must normalize to 0, never 100
                    "crisis_readiness": 80,
                    "strategic_coherence": 80,
                    "ethical_alignment": 80,
                },
                "findings": [],
                "summary": "Bool score guard.",
            }
        )

    with (
        patch("agents.leadership_planner_with_eval.evaluator.tools.is_local_gemma_active", return_value=True),
        patch("agents.leadership_planner_with_eval.evaluator.tools.generate_local_json_async", side_effect=fake_local_json),
        patch("agents.leadership_planner_with_memory.memory.tools.store_scenario", new=AsyncMock(return_value={})),
    ):
        res = await evaluate_leadership_plan("Plan", tool_context=ctx)

    assert res["status"] == "success"
    assert res["eval_method"] == "local_gemma_eval"
    assert res["scores"]["resource_stewardship"] == 0


@pytest.mark.asyncio
async def test_evaluate_leadership_plan_handles_none_fields_and_adk_state():
    from agents.leadership_planner_with_eval.evaluator.tools import evaluate_leadership_plan

    ctx = SimpleNamespace(
        state=State(
            {
                "leadership_scenario": {"stakeholders": [{"tier": 1}]},
                "leadership_pathway": {"waypoints": [], "milestones": ["M1"]},
            },
            {},
        )
    )

    async def fake_local_json(prompt: str, **_kwargs):
        if '"suggestions"' in prompt:
            return json.dumps({"suggestions": ["Clarify user intent."], "summary": "Completed."})
        return json.dumps(
            {
                "scores": {
                    "stakeholder_alignment": 78,
                    "decision_quality": 76,
                    "resource_stewardship": 80,
                    "crisis_readiness": 74,
                    "strategic_coherence": 79,
                    "ethical_alignment": 82,
                },
                "findings": [{"title": "Intent clarity", "severity": "MINOR", "description": "Clarify goals."}],
                "summary": "Handled None fields.",
            }
        )

    with (
        patch("agents.leadership_planner_with_eval.evaluator.tools.is_local_gemma_active", return_value=True),
        patch("agents.leadership_planner_with_eval.evaluator.tools.generate_local_json_async", side_effect=fake_local_json),
        patch("agents.leadership_planner_with_memory.memory.tools.store_scenario", new=AsyncMock(return_value={})),
    ):
        res = await evaluate_leadership_plan(
            json.dumps({"user_intent": None, "proposed_plan": None}),
            tool_context=ctx,
        )

    assert res["status"] == "success"
    assert res["eval_method"] == "local_gemma_eval"
    assert "overall_score" in res
    assert isinstance(ctx.state["leadership_evaluation"], dict)


# ---------------------------------------------------------------------------
# planner_with_memory & leadership_planner_with_memory (store_alloydb/store/embeddings/tools)
# ---------------------------------------------------------------------------


def test_store_alloydb_get_dsn_url_encodes_credentials_ipv6_and_ports(monkeypatch):
    from agents.leadership_planner_with_memory.memory import store_alloydb as leadership_db
    from agents.planner_with_memory.memory import store_alloydb as planner_db

    for mod in (planner_db, leadership_db):
        monkeypatch.setenv("ALLOYDB_HOST", "::1")
        monkeypatch.setenv("ALLOYDB_PORT", "invalid-port")
        monkeypatch.setenv("ALLOYDB_USER", "user@domain:1")
        monkeypatch.setenv("ALLOYDB_PASSWORD", "p@ss/w:d#100%")
        monkeypatch.setenv("ALLOYDB_DATABASE", "db/name?v=1")
        monkeypatch.setenv("ALLOYDB_SECRET_NAME", "")

        dsn = mod._get_dsn()
        assert dsn == (
            "postgresql://user%40domain%3A1:p%40ss%2Fw%3Ad%23100%25@[::1]:5432/db%2Fname%3Fv%3D1"
        )

        monkeypatch.setenv("ALLOYDB_PORT", "99999")
        assert ":5432/" in mod._get_dsn()


@pytest.mark.asyncio
async def test_store_alloydb_schema_validation_rejects_injection(monkeypatch):
    from agents.planner_with_memory.memory import store_alloydb as planner_db
    from agents.planner_with_memory.memory import tools as planner_memory_tools

    assert planner_db._validate_schema_name("planner_memory", default="planner_memory") == "planner_memory"
    with pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"):
        planner_db._validate_schema_name("public; DROP TABLE routes--", default="planner_memory")

    monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
    monkeypatch.setenv("USE_ALLOYDB", "true")
    monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")
    monkeypatch.setenv("ALLOYDB_SCHEMA", "bad-schema!")
    with (
        patch("agents.planner_with_memory.memory.tools.is_local_gemma_active", return_value=False),
        pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"),
    ):
        await planner_memory_tools.recall_past_simulations(
            "marathon",
            tool_context=SimpleNamespace(state=State({}, {}), session=None),  # type: ignore[arg-type]
            limit=2,
        )


@pytest.mark.asyncio
async def test_planner_and_leadership_alloydb_pools_isolate_loops_and_cleanup(monkeypatch):
    from agents.leadership_planner_with_memory.memory import store_alloydb as leadership_db
    from agents.planner_with_memory.memory import store_alloydb as planner_db

    for mod, cls in (
        (planner_db, planner_db.AlloyDBRouteStore),
        (leadership_db, leadership_db.AlloyDBScenarioStore),
    ):
        monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
        monkeypatch.setenv("ALLOYDB_POOL_MIN", "bad")
        monkeypatch.setenv("ALLOYDB_POOL_MAX", "0")

        created_pools: list[MagicMock] = []

        async def fake_create_pool(_dsn, **kwargs):
            assert kwargs["min_size"] == 2
            assert kwargs["max_size"] >= 2
            pool = MagicMock()
            pool.is_closing.return_value = False
            pool.close = AsyncMock()
            pool.terminate = MagicMock()
            created_pools.append(pool)
            return pool

        with mod._state_lock:
            mod._pools.clear()
            mod._pool_locks.clear()
            mod._pool = None
            mod._pool_lock = None
            mod._pool_loop = None

        with patch.object(mod.asyncpg, "create_pool", side_effect=fake_create_pool):
            p1 = await asyncio.to_thread(lambda: asyncio.run(mod._get_pool()))
            p2 = await mod._get_pool()

        assert p1 is not p2
        assert len(created_pools) == 2
        # Pool tied to the finished thread event loop must be terminated cleanly during cleanup.
        created_pools[0].terminate.assert_called_once()

        # Verify manual test reset compatibility (_pool = None; _pool_lock = None; _pool_loop = None).
        mod._pool = None
        mod._pool_lock = None
        mod._pool_loop = None
        with patch.object(mod.asyncpg, "create_pool", side_effect=fake_create_pool):
            p3 = await mod._get_pool()
        assert len(created_pools) == 3
        await cls().close()
        p3.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_alloydb_sanitizes_nan_and_clamps_count():
    from agents.leadership_planner_with_memory.memory import store_alloydb as leadership_db
    from agents.planner_with_memory.memory import store_alloydb as planner_db

    for mod in (planner_db, leadership_db):
        assert mod._sanitize_score(float("nan")) is None
        assert mod._sanitize_score(float("inf")) is None
        assert mod._sanitize_score(True) is None
        assert mod._sanitize_score(88.5) == 88.5

        encoded = mod._json_dumps({"score": float("nan"), "nested": [float("inf"), 42]})
        assert json.loads(encoded) == {"score": None, "nested": [None, 42]}

    route_store = planner_db.AlloyDBRouteStore()
    scenario_store = leadership_db.AlloyDBScenarioStore()
    with (
        patch.object(planner_db, "_get_pool", side_effect=AssertionError("pool should not be acquired")),
        patch.object(leadership_db, "_get_pool", side_effect=AssertionError("pool should not be acquired")),
    ):
        assert await route_store.recall_routes(count=0) == []
        assert await route_store.recall_routes(count=-3) == []
        assert await scenario_store.recall_scenarios(count=0) == []
        assert await scenario_store.recall_scenarios(count=-5) == []


@pytest.mark.asyncio
async def test_store_alloydb_record_simulation_artifacts_and_auto_store_summary_strip_nan(monkeypatch):
    from agents.leadership_planner_with_memory.memory import store_alloydb as leadership_db
    from agents.planner_with_memory.memory import store_alloydb as planner_db
    from agents.planner_with_memory.memory import tools as planner_memory_tools

    executed_calls: list[tuple[str, tuple]] = []

    class _FakeConn:
        async def execute(self, sql: str, *args):
            executed_calls.append((sql, args))

        async def fetchrow(self, sql: str, *args):
            executed_calls.append((sql, args))
            return {"scenario_id": "s-1"}

        async def close(self):
            return None

    class _AcquireCtx:
        def __init__(self, conn: _FakeConn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *_exc):
            return False

    fake_conn = _FakeConn()
    fake_pool = SimpleNamespace(acquire=lambda: _AcquireCtx(fake_conn))

    # 1. planner AlloyDBRouteStore.record_simulation
    with patch.object(planner_db, "_get_pool", AsyncMock(return_value=fake_pool)):
        sim_id_1 = await planner_db.AlloyDBRouteStore().record_simulation(
            "r-1", {"latency": float("nan"), "peak": float("inf")}
        )
    assert sim_id_1 is not None
    route_sim_json = executed_calls[-1][1][2]
    assert "NaN" not in route_sim_json and "Infinity" not in route_sim_json
    assert json.loads(route_sim_json) == {"latency": None, "peak": None}

    # 2. leadership AlloyDBScenarioStore.record_simulation & store_artifacts
    with patch.object(leadership_db, "_get_pool", AsyncMock(return_value=fake_pool)):
        sim_id_2 = await leadership_db.AlloyDBScenarioStore().record_simulation(
            "s-1", {"morale_delta": float("nan"), "ratio": float("-inf")}
        )
        ok_art = await leadership_db.AlloyDBScenarioStore().store_artifacts(
            "s-1", {"simulation_report": {"metric": float("nan"), "score": 92}}
        )
    assert sim_id_2 is not None and ok_art is True
    lead_sim_json = executed_calls[-2][1][2]
    assert "NaN" not in lead_sim_json and "Infinity" not in lead_sim_json
    assert json.loads(lead_sim_json) == {"morale_delta": None, "ratio": None}
    art_json = executed_calls[-1][1][5]
    assert "NaN" not in art_json
    assert json.loads(art_json) == {"metric": None, "score": 92}

    # 3. planner_with_memory tools._auto_store_summary (embedding + non-embedding paths, preserving default=str)
    monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
    monkeypatch.setenv("ALLOYDB_SCHEMA", "local_dev")
    ctx = SimpleNamespace(state=State({"user_prompt": "Test", "city": "Vegas"}, {}), session=None)
    payload = {"nan_val": float("nan"), "inf_val": float("inf"), "artifact_path": Path("/tmp/run")}

    with patch("asyncpg.connect", AsyncMock(return_value=fake_conn)):
        with (
            patch.object(planner_memory_tools, "_resolve_embedding_backend", return_value="vertex_ai"),
            patch.object(
                planner_memory_tools._embeddings,
                "compute_embedding",
                AsyncMock(return_value=[0.1, 0.2]),
            ),
        ):
            assert await planner_memory_tools._auto_store_summary("r-1", payload, ctx) is True  # type: ignore[arg-type]
        embed_sql, embed_args = executed_calls[-1]
        assert "embedding" in embed_sql
        assert "NaN" not in embed_args[3] and "Infinity" not in embed_args[3]
        assert "NaN" not in embed_args[5] and "Infinity" not in embed_args[5]
        assert json.loads(embed_args[5]) == {"nan_val": None, "inf_val": None, "artifact_path": "/tmp/run"}

        with patch.object(planner_memory_tools, "_resolve_embedding_backend", return_value="alloydb_ai"):
            assert await planner_memory_tools._auto_store_summary("r-1", payload, ctx) is True  # type: ignore[arg-type]
        no_embed_args = executed_calls[-1][1]
        assert "NaN" not in no_embed_args[5] and "Infinity" not in no_embed_args[5]
        assert json.loads(no_embed_args[5]) == {"nan_val": None, "inf_val": None, "artifact_path": "/tmp/run"}


@pytest.mark.asyncio
async def test_inmemory_stores_clamp_count_and_sanitize_nan_bool_scores():
    from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore
    from agents.planner_with_memory.memory.store import RouteMemoryStore

    r_store = RouteMemoryStore()
    id_good = r_store.store_route({"distance_miles": 26.2}, evaluation_score=91.0)
    id_nan = r_store.store_route({"distance_miles": 26.2}, evaluation_score=float("nan"))
    id_bool = r_store.store_route({"distance_miles": 26.2}, evaluation_score=True)  # type: ignore[arg-type]

    assert r_store.get_route(id_nan).evaluation_score is None  # type: ignore[union-attr]
    assert r_store.get_route(id_bool).evaluation_score is None  # type: ignore[union-attr]
    assert r_store.get_best_route().route_id == id_good  # type: ignore[union-attr]
    assert r_store.recall_routes(count=0) == []
    assert r_store.recall_routes(count=-2) == []

    s_store = ScenarioMemoryStore()
    s_good = await s_store.store_scenario({"title": "A"}, evaluation_score=87.0)
    s_nan = await s_store.store_scenario({"title": "B"}, evaluation_score=float("inf"))
    assert (await s_store.get_scenario(s_nan)).evaluation_score is None  # type: ignore[union-attr]
    assert (await s_store.get_best_scenario()).scenario_id == s_good  # type: ignore[union-attr]
    assert await s_store.recall_scenarios(count=0) == []


@pytest.mark.asyncio
async def test_embeddings_validate_dimension_nonfinite_vectors_and_cache_key(monkeypatch):
    from agents.leadership_planner_with_memory.memory import embeddings as lead_emb
    from agents.planner_with_memory.memory import embeddings as plan_emb

    for mod in (plan_emb, lead_emb):
        with pytest.raises(ValueError, match="positive integer"):
            await mod.compute_embedding("hello", dimension=0)
        with pytest.raises(ValueError, match="positive integer"):
            await mod.compute_embedding("hello", dimension=True)  # type: ignore[arg-type]

        bad_response = SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1, float("nan")])])
        mock_client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(embed_content=AsyncMock(return_value=bad_response))
            )
        )
        with (
            patch("agents.utils.local_ai.is_local_gemma_active", return_value=False),
            patch.object(mod, "_get_genai_client", return_value=mock_client),
        ):
            with pytest.raises(ValueError, match="non-finite"):
                await mod.compute_embedding("hello", dimension=2)

    # Verify planner_with_memory embeddings client cache invalidates when location changes.
    plan_emb._clear_genai_client_cache()
    created_configs: list[dict] = []

    def fake_client(**kwargs):
        created_configs.append(kwargs)
        return SimpleNamespace(**kwargs)

    with patch.object(plan_emb.genai, "Client", side_effect=fake_client):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-a")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
        c1 = plan_emb._get_genai_client()
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        c2 = plan_emb._get_genai_client()
        plan_emb._get_genai_client.cache_clear()

    assert c1 is not c2
    assert [cfg["location"] for cfg in created_configs] == ["global", "us-central1"]


@pytest.mark.asyncio
async def test_memory_tools_pgvector_and_store_route_nan_guard():
    from agents.planner_with_memory.memory import tools as mem_tools

    with pytest.raises(ValueError, match="must not be empty"):
        mem_tools._to_pgvector([])
    with pytest.raises(ValueError, match="got bool"):
        mem_tools._to_pgvector([True])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="non-finite"):
        mem_tools._to_pgvector([0.2, float("nan")])
    assert mem_tools._to_pgvector([1, 2.5]) == "[1.0,2.5]"

    state = State(
        {
            "marathon_route": {"distance_miles": 26.2},
            "evaluation_result": {"overall_score": float("nan")},
        },
        {},
    )
    ctx = SimpleNamespace(state=state, session=SimpleNamespace(user_id="alice"))
    captured_scores: list[float | None] = []

    async def fake_store_route(**kwargs):
        captured_scores.append(kwargs["evaluation_score"])
        return "route-123"

    with patch.object(mem_tools._store, "store_route", side_effect=fake_store_route):
        res = await mem_tools.store_route(tool_context=ctx)  # type: ignore[arg-type]

    assert res == {"status": "success", "route_id": "route-123"}
    assert captured_scores == [None]
    assert state["active_route_id"] == "route-123"

    # Clamped non-positive count returns empty immediately without querying DB.
    recall_res = await mem_tools.recall_routes(tool_context=ctx, count=0)  # type: ignore[arg-type]
    assert recall_res == {"status": "success", "count": 0, "routes": []}


# ---------------------------------------------------------------------------
# utils/pruned_session_service.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pruned_redis_session_service_zero_max_events_and_pool_disconnect():
    from agents.utils.pruned_session_service import (
        PrunedRedisSessionService,
        _disconnect_replaced_pool,
    )

    class _FakeBlockingPool:
        def __init__(self, **_kwargs):
            pass

    old_pool = MagicMock()
    old_pool.max_connections = 5
    old_pool.connection_kwargs = {"host": "localhost"}
    old_pool.disconnect = AsyncMock()
    fake_cache = MagicMock()
    fake_cache.connection_pool = old_pool

    def fake_sync_parent_init(self, *args, **kwargs):
        self.cache = fake_cache

    with (
        patch("agents.utils.pruned_session_service.RedisSessionService.__init__", fake_sync_parent_init),
        patch("agents.utils.pruned_session_service.RedisSessionService.append_event", new_callable=AsyncMock) as parent_append,
        patch("agents.utils.pruned_session_service.redis.BlockingConnectionPool", _FakeBlockingPool),
    ):
        svc = PrunedRedisSessionService(max_events=0)
        # Allow scheduled disconnect task on the running loop to execute.
        await asyncio.sleep(0)
        old_pool.disconnect.assert_awaited_once_with(inuse_connections=False)

        session = Session(id="s1", app_name="app", user_id="u1", events=[Event(author="user")])
        await svc.append_event(session, Event(author="model"))
        assert session.events == []
        parent_append.assert_awaited_once()

    # Without an active event loop, async disconnect coroutines are closed instead of leaking warnings.
    async_only_pool = SimpleNamespace(disconnect=AsyncMock())
    _disconnect_replaced_pool(async_only_pool)
    async_only_pool.disconnect.assert_called_once_with(inuse_connections=False)


# ---------------------------------------------------------------------------
# skills/a2ui-rendering/tools.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2ui_clamp_score_validation_and_missing_context():
    a2ui_tools = _load_module("a2ui_rendering_tools_r3", "agents/skills/a2ui-rendering/tools.py")

    assert a2ui_tools._clamp_score(float("nan"), 50) == 50
    assert a2ui_tools._clamp_score(float("inf"), 50) == 50
    assert a2ui_tools._clamp_score(True, 50) == 50
    assert a2ui_tools._clamp_score(120) == 100
    assert a2ui_tools._clamp_score(-5) == 0

    violations: list[dict[str, str]] = []
    a2ui_tools._validate_component({"id": ["unhashable"], "component": {"Text": {}}}, set(), [], violations)
    assert violations and violations[0]["field"] == "id"

    bad_surface_violations = a2ui_tools._validate_surface_update({"surfaceUpdate": ["not-a-dict"]})
    assert bad_surface_violations and bad_surface_violations[0]["field"] == "surfaceUpdate"

    ctx_weird = SimpleNamespace(session=None, state=State({"simulation_id": "—"}, {}))
    assert a2ui_tools._extract_emit_context(ctx_weird) == (None, None)

    # validate_and_emit_a2ui must validate cleanly even without tool_context.
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {"id": "t1", "component": {"Text": {"text": {"literalString": "Hello"}}}}
                ]
            }
        }
    )
    res = await a2ui_tools.validate_and_emit_a2ui(payload=payload, tool_context=None)
    assert res["status"] == "success"
    assert res["a2ui"]["surfaceUpdate"]["components"][0]["id"] == "t1"


# ---------------------------------------------------------------------------
# planner/skills/gis-spatial-engineering, adk_tools.py & callbacks.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gis_geometry_and_infrastructure_edge_cases():
    gis = _load_module(
        "gis_tools_r3",
        "agents/planner/skills/gis-spatial-engineering/scripts/tools.py",
    )

    # Identical coordinates and near-antipodal rounding must stay real/finite.
    assert math.isfinite(gis._haversine((-115.17, 36.11), (-115.17, 36.11)))
    assert math.isfinite(gis._haversine((0.0, 0.0), (180.0, 0.0)))

    # Malformed road network features are ignored without raising ValueError/TypeError.
    adj, landmarks, road_names, strip_nodes = gis._build_graph(
        {
            "features": [
                "not-a-dict",
                {"geometry": {"type": "LineString", "coordinates": [[-115.17, 36.11]]}},
                {"geometry": {"type": "LineString", "coordinates": [[-115.17, 36.11], [-115.17]]}},
            ]
        }
    )
    assert adj == {} and landmarks == {} and road_names == {} and strip_nodes == set()
    assert gis._split_route_by_road([], {}, 0.0) == []

    bad_route = await gis.plan_marathon_route(geojson_data="not-a-dict")  # type: ignore[arg-type]
    assert bad_route["status"] == "error"

    # Zero-length and tiny (<0.5 mi) routes must never emit negative mile markers.
    zero_index = [((-115.17, 36.11), 0.0)]
    assert gis._place_portable_toilets(zero_index, runner_count=5000) == []

    tiny_index = [((-115.17, 36.11), 0.0), ((-115.17, 36.113), 0.2)]
    tiny_toilets = gis._place_portable_toilets(tiny_index, runner_count=5000)
    assert all(f["properties"]["mi"] >= 0.0 for f in tiny_toilets)

    # add_course_infrastructure must not mutate caller route_geojson in place.
    caller_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-115.17, 36.10], [-115.17, 36.15], [-115.17, 36.25]],
                },
                "properties": {"type": "marathon_route"},
            }
        ],
    }
    caller_snapshot = copy.deepcopy(caller_geojson)
    infra_res = await gis.add_course_infrastructure(
        caller_geojson,
        runner_count=True,  # type: ignore[arg-type]
    )
    assert infra_res["status"] == "success"
    assert caller_geojson == caller_snapshot


def test_get_maps_tools_non_dict_servers_and_guardrail_callbacks_no_state(monkeypatch):
    from agents.leadership_planner.callbacks import leadership_guardrail_callback
    from agents.planner import adk_tools
    from agents.planner.callbacks import financial_guardrail_callback

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-proj")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    mock_registry = SimpleNamespace(list_mcp_servers=lambda: ["unexpected-list"])
    with patch.object(adk_tools, "AgentRegistry", return_value=mock_registry):
        assert adk_tools.get_maps_tools() == []

    req = SimpleNamespace(contents=[SimpleNamespace(role="user", parts=[SimpleNamespace(text="change budget and delete stakeholder")])])
    assert financial_guardrail_callback(SimpleNamespace(state=None), req) is None  # type: ignore[arg-type]
    assert leadership_guardrail_callback(SimpleNamespace(state=None), req) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# agents/utils/* bug fixes
# ---------------------------------------------------------------------------


def test_utils_retry_loop_protocol_types_cache_a2ui_defaults_runtime(monkeypatch):
    from agents.utils.a2ui_helpers import parse_markdown_blocks
    from agents.utils.loop_agent_ext import TenantSafeLoopAgent
    from agents.utils.retry import _env_float
    from agents.utils.runner_protocol import RunnerEventType, parse_runner_event
    from agents.utils.runner_types import cap_for_runner_type
    from agents.utils.runtime import _int_env
    from agents.utils.session_manager import TTLCache
    from agents.utils.sim_defaults import _safe_positive_int_env

    # retry._env_float rejects NaN and Inf
    monkeypatch.setenv("RETRY_TEST_FLOAT", "nan")
    with pytest.raises(ValueError, match="finite"):
        _env_float("RETRY_TEST_FLOAT", 1.0)
    monkeypatch.setenv("RETRY_TEST_FLOAT", "inf")
    with pytest.raises(ValueError, match="finite"):
        _env_float("RETRY_TEST_FLOAT", 1.0)

    # TenantSafeLoopAgent ignores bool and <= 0 overrides
    agent = TenantSafeLoopAgent(name="loop", max_iterations=4)
    for bad in (True, False, -3, "0", "not-an-int"):
        ctx = SimpleNamespace(session=SimpleNamespace(state={"_loop_max_iterations_loop": bad}))
        assert agent._effective_max_iterations(ctx) == 4  # type: ignore[arg-type]
    ctx_good = SimpleNamespace(session=SimpleNamespace(state={"_loop_max_iterations_loop": "7"}))
    assert agent._effective_max_iterations(ctx_good) == 7  # type: ignore[arg-type]

    # runner_protocol handles unhashable event payload cleanly
    ev = parse_runner_event(json.dumps({"event": ["unhashable"], "x": 1}))
    assert ev.event is RunnerEventType.UNKNOWN

    # runner_types clamps invalid / non-positive env vars
    monkeypatch.setenv("MAX_RUNNERS_AUTOPILOT", "not-an-int")
    assert cap_for_runner_type("runner_autopilot") == 1000
    monkeypatch.setenv("MAX_RUNNERS_LLM", "0")
    assert cap_for_runner_type("runner") == 1

    # sim_defaults & runtime helpers validate/clamp explicitly
    monkeypatch.setenv("BAD_POS", "-10")
    assert _safe_positive_int_env("BAD_POS", 120) == 120
    monkeypatch.setenv("BAD_INT", "abc")
    with pytest.raises(ValueError, match="Invalid integer for BAD_INT"):
        _int_env("BAD_INT", 20)

    # TTLCache with maxsize <= 0 never stores entries
    zero_cache = TTLCache(maxsize=0, ttl=60)
    zero_cache.set("k", "v")
    assert len(zero_cache) == 0
    assert zero_cache.get("k") is None

    # parse_markdown_blocks returns empty on whitespace-only text
    assert parse_markdown_blocks("   \n  ", "p") == ([], [])


def test_a2a_handle_orchestration_invalid_json_and_cors_whitespace(monkeypatch):
    from fastapi import FastAPI
    from google.adk.agents import BaseAgent
    from google.adk.apps.app import App

    from agents.utils.a2a import register_a2a_routes
    from agents.utils.serve import create_agent_app
    from agents.utils.simulation_plugin import SimulationNetworkPlugin

    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", " https://a.example , , https://b.example ")
    api = FastAPI()
    sub_app = FastAPI()

    @sub_app.get("/health")
    def _health():
        return {"ok": True}

    a2a_stub = MagicMock()
    a2a_stub._handle_get_agent_card = lambda _req: None
    a2a_stub.build.return_value = sub_app

    plugin = SimulationNetworkPlugin()
    adk_app = App(root_agent=BaseAgent(name="test_agent"), name="test_agent", plugins=[plugin])
    card = MagicMock()
    card.name = "test_agent"

    with (
        patch("agents.utils.simulation_plugin.RedisOrchestratorDispatcher") as disp_cls,
        patch("agents.utils.simulation_executor.SimulationExecutor"),
        patch("agents.utils.a2a.A2AStarletteApplication", return_value=a2a_stub),
    ):
        disp_cls.return_value.handle_event = AsyncMock()
        register_a2a_routes(
            app=api,
            agent_or_app=adk_app,
            agent_card=card,
            path_prefix="/a2a",
            simulation_runner=MagicMock(),
        )

    client = TestClient(api)
    assert client.post("/orchestration", content="{invalid-json").status_code == 400
    assert client.post("/a2a/orchestration", json=["list-not-dict"]).status_code == 400

    # Trimmed CORS origins allow exact configured origin without wildcard or blanks
    cors_mw = [m for m in sub_app.user_middleware if "CORS" in str(m.cls)]
    assert cors_mw and cors_mw[0].kwargs["allow_origins"] == ["https://a.example", "https://b.example"]

    with (
        patch("agents.utils.serve.get_fast_api_app", return_value=FastAPI()),
        patch("agents.utils.serve._register_a2a"),
    ):
        app_serve = create_agent_app("demo", agents_dir=".", adk_app=adk_app, agent_card=card)
    serve_cors = [m for m in app_serve.user_middleware if "CORS" in str(m.cls)]
    assert serve_cors and serve_cors[0].kwargs["allow_origins"] == ["https://a.example", "https://b.example"]
