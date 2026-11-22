# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: added regression tests for leadership planner, memory, DCNS, and dispatcher bug fixes.
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

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from agents.leadership_planner_with_eval.adk_tools import get_tools as get_eval_tools
from agents.leadership_planner_with_eval.evaluator.tools import evaluate_leadership_plan
from agents.leadership_planner_with_eval.tools import (
    start_simulation,
    submit_plan_to_simulator,
)
from agents.leadership_planner_with_memory.memory import (
    embeddings,
    retrieval_tools,
    store_alloydb,
)
from agents.leadership_planner_with_memory.memory import tools as memory_tools
from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore
from agents.leadership_planner_with_memory.simulation_timeline import (
    emit_simulation_timeline,
)
from agents.leadership_planner_with_memory.stakeholder_personas import (
    emit_stakeholder_personas,
)
from agents.utils import simulation_registry, user_auth_context
from agents.utils.dispatcher import RedisOrchestratorDispatcher
from agents.utils.local_ai import LocalGemmaOllama

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCENARIO_SCRIPTS_DIR = (
    _REPO_ROOT / "agents" / "leadership_planner" / "skills" / "scenario-design" / "scripts"
)
_MAPPING_SCRIPTS_DIR = (
    _REPO_ROOT / "agents" / "leadership_planner" / "skills" / "stakeholder-mapping" / "scripts"
)


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ctx(state: dict | None = None, session_id: str = "sess-bugfix") -> SimpleNamespace:
    return SimpleNamespace(
        state=state if state is not None else {},
        invocation_id="inv-1",
        session=SimpleNamespace(id=session_id, user_id="anonymous"),
    )


def test_planner_tool_registration():
    """Verify planner tools use adk_additional_tools for on-demand activation."""
    from agents.planner.adk_tools import get_tools

    tools = get_tools()
    tool_names = [t.name if hasattr(t, "name") else type(t).__name__ for t in tools]

    # Skill tools are NOT top-level — they activate after load_skill
    skill_tool_names = [
        "plan_marathon_route",
        "report_marathon_route",
        "plan_marathon_event",
    ]
    for name in skill_tool_names:
        assert name not in tool_names, f"Tool '{name}' should not be top-level; it activates after load_skill."

    # assess_traffic_impact is NOT available in the base planner
    assert "assess_traffic_impact" not in tool_names

    # SkillToolset must be present with additional_tools configured
    st = [t for t in tools if isinstance(t, SkillToolset)][0]
    assert st._code_executor is not None
    assert len(st._provided_tools_by_name) > 0, "SkillToolset must have additional_tools"

    # Base planner tools must be in the candidate pool
    for name in skill_tool_names:
        assert name in st._provided_tools_by_name, f"Tool '{name}' missing from SkillToolset additional_tools"

    # assess_traffic_impact is NOT in the base planner's pool (only in planner_with_eval)
    assert "assess_traffic_impact" not in st._provided_tools_by_name


def test_closing_fallback_summary_without_prior_user_turn():
    """Sub-agent / programmatic sim-close turns without a user text turn must not raise UnboundLocalError."""
    req = LlmRequest(
        model="ollama_chat/gemma3:4b",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="emit_translation_diagnostic",
                            response={
                                "status": "success",
                                "translation_diagnostic": {
                                    "summary": "System metrics collided with frontline lifeworld norms."
                                },
                            },
                        )
                    ),
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="stop_race_collector",
                            response={"status": "stopped"},
                        )
                    ),
                ],
            )
        ],
    )
    summary = LocalGemmaOllama._build_closing_fallback_summary(req)
    assert "Executive Simulation Debrief" in summary
    assert "System metrics collided with frontline lifeworld norms." in summary


@pytest.mark.asyncio
async def test_eval_skill_toolset_does_not_synthesize_telemetry_in_local_gemma_mode(monkeypatch):
    """_LocalAwareSkillToolset.process_llm_request must not synthesize fake telemetry into state."""
    monkeypatch.setattr("agents.utils.local_ai.is_local_gemma_active", lambda: True)
    tools = get_eval_tools()
    skill_toolset = next(t for t in tools if isinstance(t, SkillToolset))

    ctx = _make_ctx(
        {
            "simulation_result": {"alignment_rate": 0.72},
            "leadership_scenario": {
                "id": "scen-1",
                "echoes": [{"role": "cto", "tier": 1, "persona": "CTO"}],
            },
        }
    )
    req = LlmRequest(model="ollama_chat/gemma3:4b", contents=[])
    await skill_toolset.process_llm_request(tool_context=ctx, llm_request=req)  # type: ignore[arg-type]

    # Zero fake data guarantee: process_llm_request does not invent synthetic voice logs or decisions
    assert "echo_voice_log" not in ctx.state
    assert "decision_log" not in ctx.state


@pytest.mark.asyncio
async def test_submit_plan_carries_forward_start_simulation_config():
    """submit_plan_to_simulator() with omitted args must inherit start_simulation's simulation_config."""
    ctx = _make_ctx(
        {
            "leadership_scenario": {
                "id": "scen-carry",
                "name": "Carry Forward Test",
                "stakeholder_count": 1000,
                "echoes": [{"role": "cto", "tier": 1}],
            },
            "selected_persona": "visionary",
        }
    )

    start_res = await start_simulation(
        action="execute",
        tool_context=ctx,  # type: ignore[arg-type]
        simulation_config={"stakeholder_count": 2500},
        runner_type="leadership_runner",
    )
    assert start_res["status"] == "ready"

    captured_payload: dict = {}

    async def fake_call_agent(*, tool_context, agent_name, message, session_id):
        captured_payload.update(json.loads(message))
        return {"status": "success", "response": json.dumps({"alignment_rate": 0.85})}

    with (
        patch("agents.utils.communication.call_agent", side_effect=fake_call_agent),
        patch("agents.utils.simdata.store_simulation_data", new=AsyncMock(return_value=True)),
        patch("agents.utils.simdata.load_simulation_data", new=AsyncMock(return_value={})),
    ):
        res = await submit_plan_to_simulator(action="execute", tool_context=ctx)  # type: ignore[arg-type]

    assert res["status"] == "success"
    sim_cfg = captured_payload["simulation_config"]
    assert sim_cfg["runner_count"] == 2500
    assert sim_cfg["runner_type"] == "leadership_runner"
    assert sim_cfg["persona"] == "visionary"


@pytest.mark.asyncio
async def test_dcns_zero_travel_synthetic_venues_and_pathway_schema():
    """Verify 0-minute travel friction, multi-word synthetic venue projection, and unified waypoints."""
    dcns_mod = _load_module("dcns_engine_bugfix_test", _SCENARIO_SCRIPTS_DIR / "dcns_engine.py")
    scenario_mod = _load_module("scenario_tools_bugfix_test", _SCENARIO_SCRIPTS_DIR / "tools.py")
    mapping_mod = _load_module("mapping_tools_bugfix_test", _MAPPING_SCRIPTS_DIR / "tools.py")

    src = {"role": "cto", "affinity": [0.5] * 7, "domain_principles": []}
    tgt = {
        "role": "ethics_lead",
        "affinity": [0.4] * 7,
        "domain_principles": [],
        "initial_resistance": 0.3,
        "initial_influence": 0.6,
        "initial_sentiment": 0.5,
    }
    resources = {"OWB": {"initial": 70}}
    cost_colocated = dcns_mod._edge_cost(src, tgt, resources, travel_matrix={"cto→ethics_lead": 0.0})
    cost_unknown = dcns_mod._edge_cost(src, tgt, resources, travel_matrix={"other→pair": 20.0})
    assert cost_colocated[0] == pytest.approx(cost_unknown[0] * 0.85)

    anchor = (-115.1728, 36.1147)
    stakeholders = [
        {"role": "synthetic_ethics_lead_0"},
        {"role": "synthetic_synthetic_ethics_lead_0_2"},
    ]
    projected = dcns_mod._project_to_real_venues(stakeholders, {"ethics_lead": anchor})
    for lon, lat in projected:
        assert abs(lon - anchor[0]) < 0.02
        assert abs(lat - anchor[1]) < 0.02

    pca_res = dcns_mod._generate_consensus_pathway_pca(
        stakeholders=[
            {
                "role": "cto",
                "tier": 1,
                "affinity": [0.8, 0.7, 0.5, 0.6, 0.4, 0.7, 0.6],
                "domain_principles": ["Strategic Foresight"],
            },
            {
                "role": "ethics_lead",
                "tier": 2,
                "affinity": [0.4, 0.5, 0.7, 0.9, 0.6, 0.5, 0.4],
                "domain_principles": ["Ethical Leadership & Governance"],
            },
            {
                "role": "union_rep",
                "tier": 3,
                "affinity": [0.3, 0.4, 0.6, 0.4, 0.8, 0.6, 0.3],
                "domain_principles": ["Empowerment"],
            },
        ],
        resources={"OWB": {"initial": 70}, "BT": {"initial": 500}, "TBU": {"initial": 80}},
        runner_count=20,
        seed=7,
    )
    assert len(pca_res["waypoints"]) == len(pca_res["pathway"]) > 0

    ctx = _make_ctx()
    plan_res = await scenario_mod.plan_consensus_pathway(
        scenario_type="transformation",
        stakeholder_count=2400,
        seed=11,
        tool_context=ctx,
    )
    assert plan_res["status"] == "success"
    assert plan_res["waypoint_count"] > 0
    assert ctx.state["leadership_scenario"]["stakeholder_count"] == 2400

    cached_res = await scenario_mod.design_leadership_scenario(tool_context=ctx)
    assert cached_res["stakeholder_count"] == 2400

    ctx.state["leadership_scenario"]["resources"]["BT"]["initial"] = 1
    assert scenario_mod._RESOURCE_MODELS["balanced"]["BT"]["initial"] == 500

    assert "seniority" not in ctx.state["leadership_scenario"]["echoes"][0]
    map_res = await mapping_mod.map_stakeholders(tool_context=ctx)
    assert map_res["status"] == "success"
    assert "seniority" not in ctx.state["leadership_scenario"]["echoes"][0]


@pytest.mark.asyncio
async def test_dcns_resource_pools_and_evaluator_state_enrichment():
    """Verify scenario resources expose TBU/BT/OWB pools and evaluator enriches state into LLM prompt."""
    scenario_mod = _load_module("scenario_tools_resource_test", _SCENARIO_SCRIPTS_DIR / "tools.py")
    ctx = _make_ctx()
    scen_res = await scenario_mod.design_leadership_scenario(
        scenario_type="transformation",
        budget_allocation="balanced",
        stakeholder_count=200,
        tool_context=ctx,
    )
    assert scen_res["status"] == "success"
    resources = ctx.state["leadership_scenario"]["resources"]
    assert {"TBU", "BT", "OWB"} <= set(resources.keys())
    assert resources["BT"]["initial"] == 500

    pw_res = await scenario_mod.plan_consensus_pathway(
        scenario_type="transformation",
        budget_allocation="balanced",
        stakeholder_count=200,
        tool_context=ctx,
    )
    assert pw_res["status"] == "success"

    captured_prompts: list[str] = []

    async def fake_local_json(prompt: str, **_kwargs):
        captured_prompts.append(prompt)
        if '"suggestions"' in prompt:
            return json.dumps({"suggestions": ["Monitor BT capital buffer."], "summary": "Evaluated."})
        return json.dumps(
            {
                "scores": {
                    "stakeholder_alignment": 85,
                    "decision_quality": 82,
                    "resource_stewardship": 88,
                    "crisis_readiness": 80,
                    "strategic_coherence": 84,
                    "ethical_alignment": 86,
                },
                "findings": [{"title": "Budget buffer", "severity": "MINOR", "description": "OK"}],
                "summary": "Solid.",
            }
        )

    with (
        patch("agents.leadership_planner_with_eval.evaluator.tools.is_local_gemma_active", return_value=True),
        patch("agents.leadership_planner_with_eval.evaluator.tools.generate_local_json_async", side_effect=fake_local_json),
        patch("agents.leadership_planner_with_memory.memory.tools.store_scenario", new=AsyncMock(return_value={})),
    ):
        eval_res = await evaluate_leadership_plan("Evaluate balanced plan", tool_context=ctx)

    assert eval_res["status"] == "success"
    assert eval_res["eval_method"] == "local_gemma_eval"
    assert "[Scenario Metadata (from session state)]" in captured_prompts[0]
    assert "[Consensus Pathway (from session state)]" in captured_prompts[0]


@pytest.mark.asyncio
async def test_nan_and_inf_rejected_in_personas_timeline_and_store_scenario():
    """NaN/Infinity floats must be rejected before polluting session state or JSONB."""
    ctx = _make_ctx()
    bad_persona_res = await emit_stakeholder_personas(
        json.dumps(
            {
                "personas": [
                    {
                        "stakeholder_id": "ceo",
                        "archetype": "visionary",
                        "communication_style": "narrative",
                        "stance": "supportive",
                        "sentiment_bias": "NaN",
                        "influence_weight": 0.8,
                    }
                ]
            }
        ),
        ctx,  # type: ignore[arg-type]
    )
    assert bad_persona_res["status"] == "error"

    bad_timeline_res = await emit_simulation_timeline(
        json.dumps(
            {
                "ticks": [
                    {
                        "tick": 1,
                        "sprint_label": "Sprint 1",
                        "headline": "Kickoff",
                        "states": [
                            {
                                "stakeholder_id": "ceo",
                                "stance": "supportive",
                                "sentiment": "Infinity",
                            }
                        ],
                    }
                ]
            }
        ),
        ctx,  # type: ignore[arg-type]
    )
    assert bad_timeline_res["status"] == "error"

    nan_eval_ctx = _make_ctx(
        {
            "leadership_scenario": {"id": "scen-nan", "name": "NaN Check"},
            "leadership_evaluation": {"overall_score": float("nan")},
        }
    )
    store_res = await memory_tools.store_scenario(nan_eval_ctx)  # type: ignore[arg-type]
    assert store_res["status"] == "needs_evaluation"


@pytest.mark.asyncio
async def test_recall_then_store_stakeholder_artifacts_does_not_double_wrap_interviews(monkeypatch):
    """Recalling interview list and re-storing must not nest [[interview]] in JSONB."""
    mem_store = ScenarioMemoryStore()
    monkeypatch.setattr(memory_tools, "_store", mem_store)

    sid = await mem_store.store_scenario(
        scenario_data={"id": "s1", "name": "Test"},
        evaluation_score=84.0,
        evaluation_result={"overall_score": 84.0},
    )
    interview_entry = {"persona": "ceo", "turns": [{"q": "Why now?", "a": "Market shift."}]}
    await mem_store.store_artifacts(sid, {"stakeholder_interview": [interview_entry]})

    ctx = _make_ctx()
    recalled = await memory_tools.recall_stakeholder_artifacts(sid, ctx)  # type: ignore[arg-type]
    assert recalled["status"] == "success"
    assert ctx.state["stakeholder_interviews"] == [interview_entry]

    ctx.state.pop("stakeholder_interviews", None)
    saved = await memory_tools.store_stakeholder_artifacts(ctx)  # type: ignore[arg-type]
    assert saved["status"] == "success"

    persisted = await mem_store.get_artifacts(sid)
    assert persisted is not None
    assert persisted["stakeholder_interview"] == [interview_entry]


@pytest.mark.asyncio
async def test_retrieve_document_passages_fail_soft_on_malformed_inputs_and_readonly_state(monkeypatch):
    """retrieve_document_passages must never raise on bad limit, malformed passage rows, or read-only state."""
    mock_resp = httpx.Response(
        200,
        json={
            "passages": [
                None,
                "unexpected-string-row",
                {
                    "text": "Grounded evidence passage",
                    "source_id": "belief-123",
                    "score": "not-a-float",
                    "chunk_index": "not-an-int",
                    "document_id": "doc-1",
                },
            ]
        },
    )
    fake_client = SimpleNamespace(post=AsyncMock(return_value=mock_resp))
    monkeypatch.setattr(retrieval_tools, "_get_rag_client", lambda: fake_client)

    ro_ctx = SimpleNamespace(
        state=MappingProxyType({"memory_analysis_id": "analysis-1"}),
        session=SimpleNamespace(id="sess-ro"),
    )
    result = await retrieval_tools.retrieve_document_passages(
        query="ai governance",
        tool_context=ro_ctx,  # type: ignore[arg-type]
        limit="not-an-int",  # type: ignore[arg-type]
    )
    assert result["status"] == "success"
    assert result["passage_count"] == 1
    assert result["passages"][0]["text"] == "Grounded evidence passage"
    assert result["passages"][0]["score"] == 0.0
    assert result["passages"][0]["chunk_index"] == 0


def test_rag_client_and_alloydb_pool_handle_event_loop_transitions_and_schema_validation(monkeypatch):
    """Cached async clients/locks must recreate cleanly across closed event loops."""

    async def grab_rag_client():
        return retrieval_tools._get_rag_client()

    c1 = asyncio.run(grab_rag_client())
    c2 = asyncio.run(grab_rag_client())
    assert c1 is not c2

    monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
    monkeypatch.setenv("ALLOYDB_PASSWORD", "secret")
    monkeypatch.setenv("ALLOYDB_SCHEMA", "bad; DROP TABLE x;--")

    async def try_bad_schema():
        store_alloydb._pool = None
        store_alloydb._pool_lock = None
        store_alloydb._pool_loop = None
        await store_alloydb._get_pool()

    with pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"):
        asyncio.run(try_bad_schema())


def test_embeddings_genai_client_refreshes_on_env_change(monkeypatch):
    """embeddings._get_genai_client must rebuild client when auth env vars change."""
    created_kwargs: list[dict] = []

    def fake_client(**kwargs):
        created_kwargs.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(embeddings.genai, "Client", fake_client)
    embeddings._client = None
    embeddings._client_key = None

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-a")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    c1 = embeddings._get_genai_client()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-b")
    c2 = embeddings._get_genai_client()
    assert c1 is not c2
    assert created_kwargs[0]["project"] == "proj-a"
    assert created_kwargs[1]["project"] == "proj-b"


@pytest.mark.asyncio
async def test_dispatcher_and_runtime_tls_redis_tenant_spawn_reset_and_relay(monkeypatch):
    """Verify rediss:// support, clean cross-tenant spawn rejection, reset cleanup, and non-mutating relay."""
    runner = MagicMock()
    runner.app.name = "leadership_planner"

    from agents.utils import redis_pool

    tls_dispatcher = RedisOrchestratorDispatcher(runner=runner, redis_url="rediss://tls-host:6380")
    assert tls_dispatcher.agent_type == "leadership_planner"

    with (
        patch.dict(
            os.environ,
            {
                "REDIS_ADDR": "rediss://tls-host:6380",
                "REDIS_MAX_CONNECTIONS": "50",
            },
            clear=False,
        ),
        patch.object(redis_pool, "_shared_clients", {}),
        patch("redis.asyncio.BlockingConnectionPool.from_url") as mock_pool,
        patch("redis.asyncio.Redis") as mock_redis_cls,
    ):
        client = redis_pool.get_shared_redis_client()
        assert client is mock_redis_cls.return_value
        mock_pool.assert_called_once()
        assert mock_pool.call_args.args[0] == "rediss://tls-host:6380"

    with (
        patch.dict(
            os.environ,
            {
                "SESSION_STORE_OVERRIDE": "redis",
                "REDIS_ADDR": "rediss://tls-host:6380",
                "REDIS_SESSION_MAX_CONNECTIONS": "100",
            },
            clear=False,
        ),
        patch("agents.utils.pruned_session_service.PrunedRedisSessionService") as mock_pruned,
    ):
        from agents.utils.runtime import create_services

        create_services()
        mock_pruned.assert_called_once_with(uri="rediss://tls-host:6380", max_connections=100)

    monkeypatch.setenv("DISPATCHER_USER_ID", "tenant-a")
    dispatcher = RedisOrchestratorDispatcher(runner=runner, redis_url="redis://localhost:6379")
    dispatcher._pubsub = AsyncMock()

    await dispatcher.handle_event(
        {
            "type": "spawn_agent",
            "sessionId": "sess-rogue",
            "payload": {
                "agentType": "leadership_planner",
                "simulation_id": "sim-rogue",
                "user_id": "tenant-b",
            },
        }
    )
    assert "sess-rogue" not in dispatcher.active_sessions
    assert "sess-rogue" not in dispatcher.session_simulation_map
    assert "sim-rogue" not in dispatcher._simulation_subscriptions
    assert "sess-rogue" not in simulation_registry._local
    dispatcher._pubsub.subscribe.assert_not_called()

    await dispatcher.handle_event(
        {
            "type": "spawn_agent",
            "sessionId": "sess-legit",
            "payload": {
                "agentType": "leadership_planner",
                "simulation_id": "sim-legit",
                "user_id": "tenant-a",
                "user_auth_token": "tok-secret-123",
            },
        }
    )
    assert user_auth_context.get_session_token("sess-legit") == "tok-secret-123"
    assert "sim-legit" in dispatcher._simulation_subscriptions

    await dispatcher.handle_event({"type": "environment_reset"})
    assert user_auth_context.get_session_token("sess-legit") is None
    assert len(dispatcher._simulation_subscriptions) == 0
    dispatcher._pubsub.unsubscribe.assert_called_once()

    dispatcher.active_sessions.add("sess-local")
    dispatcher._trigger_agent_run = MagicMock()  # type: ignore[method-assign]
    fake_redis = AsyncMock()
    original_targets = ["sess-local", "sess-remote"]
    broadcast_event = {
        "type": "broadcast",
        "payload": {"data": "PULSE", "targets": list(original_targets)},
    }
    with patch("agents.utils.dispatcher.get_shared_redis_client", return_value=fake_redis):
        await dispatcher.handle_event(broadcast_event)

    assert broadcast_event["payload"]["targets"] == original_targets
    fake_redis.publish.assert_awaited_once()


if __name__ == "__main__":
    test_planner_tool_registration()
    print("✅ Planner tool registration test passed!")
