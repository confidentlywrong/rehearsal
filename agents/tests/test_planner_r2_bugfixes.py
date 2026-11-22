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

"""Round-2 regression tests for leadership planners and shared agent utilities."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.sessions.state import State


def _load_script_module(rel_path: str, module_name: str):
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    full_path = repo_root / rel_path
    spec = importlib.util.spec_from_file_location(module_name, full_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_adk_tool_context(initial_state: dict | None = None, session_id: str | None = "sess-r2"):
    """Build a lightweight ToolContext stand-in backed by a REAL ADK State instance."""
    adk_state = State(value=dict(initial_state or {}), delta={})
    session = SimpleNamespace(id=session_id) if session_id is not None else None
    return SimpleNamespace(
        state=adk_state,
        session=session,
        invocation_id="inv-r2",
        agent_name="test_agent",
    )


# ---------------------------------------------------------------------------
# 1. agents/leadership_planner/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_stakeholders_preserves_behavioral_patterns_dict():
    sd_tools = _load_script_module(
        "agents/leadership_planner/skills/scenario-design/scripts/tools.py",
        "r2_sd_for_map",
    )
    sm_tools = _load_script_module(
        "agents/leadership_planner/skills/stakeholder-mapping/scripts/tools.py",
        "r2_stakeholder_mapping_tools",
    )
    ctx = _make_adk_tool_context()
    await sd_tools.design_leadership_scenario(scenario_type="transformation", stakeholder_count=500, tool_context=ctx)
    res = await sm_tools.map_stakeholders(tool_context=ctx)
    assert res["status"] in ("ready", "success")
    stakeholders = ctx.state.get("stakeholder_map")
    assert isinstance(stakeholders, list) and stakeholders
    for sh in stakeholders:
        bp = sh["behavioral_patterns"]
        assert isinstance(bp, dict)
        assert {"decision_style", "communication", "risk_tolerance"} <= set(bp.keys())

    # Verify downstream emit_stakeholder_psychology preserves the mapped dict values
    td_tools = _load_script_module(
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
        "r2_td_tools_from_map",
    )
    psych_res = await td_tools.emit_stakeholder_psychology(ctx)  # type: ignore[arg-type]
    assert psych_res["status"] == "success"
    profiles = psych_res["stakeholder_psychology"]["profiles"]
    assert profiles
    # Tier-1 C-Suite mapped pattern ("strategic" / "top-down directive") must override the "data-driven" fallback
    assert any(
        p["decision_style"] == "strategic" and p["communication_style"] == "top-down directive"
        for p in profiles
    )


@pytest.mark.asyncio
async def test_scenario_design_coerces_none_strings_dicts_and_clamps_negative_resistance():
    sd_tools = _load_script_module(
        "agents/leadership_planner/skills/scenario-design/scripts/tools.py",
        "r2_scenario_design_tools",
    )
    ctx = _make_adk_tool_context(
        {
            "venue_coords": {
                "HQ Tower": {"lon": -115.1728, "lat": 36.1147},
                "Bad Venue": [float("nan"), 36.1],
                "Short Venue": [-115.1],
            },
            "travel_matrix": {
                "HQ Tower->Ops Hub": "14 mins",
                "Bad Leg": None,
                "Bool Leg": True,
            },
            "scenario_weather": {"resistance_delta": -0.95},
        }
    )
    scen = await sd_tools.design_leadership_scenario(
        scenario_type=None,  # type: ignore[arg-type]
        stakeholder_count="1,250",  # type: ignore[arg-type]
        complexity=None,  # type: ignore[arg-type]
        tool_context=ctx,
    )
    assert scen["status"] in ("ready", "success")
    assert scen["stakeholder_count"] == 1250

    pw = await sd_tools.plan_consensus_pathway(
        scenario_type=None,  # type: ignore[arg-type]
        budget_allocation=None,  # type: ignore[arg-type]
        complexity=None,  # type: ignore[arg-type]
        stakeholder_count="1,250",  # type: ignore[arg-type]
        tool_context=ctx,
    )
    assert pw["status"] in ("ready", "success")
    active_pw = ctx.state.get("consensus_pathway_active")
    assert isinstance(active_pw, dict)
    # Negative weather delta must never create negative resistance or negative total cost
    for v in active_pw["cost"].values():
        assert v >= 0.0


def test_dcns_project_to_real_venues_deterministic_sha256_seed():
    dcns = _load_script_module(
        "agents/leadership_planner/skills/scenario-design/scripts/dcns_engine.py",
        "r2_dcns_engine",
    )
    venue_coords = {
        "cfo": (-115.17, 36.11),
        "cto": (-115.16, 36.12),
        "coo": (-115.15, 36.13),
    }
    stakeholders = [{"role": "cfo_t2_0", "tier": 2}, {"role": "cto_t2_1", "tier": 2}]
    pts1 = dcns._project_to_real_venues(stakeholders, venue_coords)
    pts2 = dcns._project_to_real_venues(stakeholders, dict(reversed(list(venue_coords.items()))))
    assert pts1 == pts2


# ---------------------------------------------------------------------------
# 2. agents/leadership_planner_with_eval/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluator_reads_real_adk_state_and_normalizes_scores_and_findings():
    from agents.leadership_planner_with_eval.evaluator.tools import (
        _build_result,
        _normalize_score,
        evaluate_leadership_plan,
    )

    # Real ADK State is NOT an instance of dict; evaluate_leadership_plan must still read it.
    ctx = _make_adk_tool_context(
        {
            "leadership_scenario": {
                "name": "ADK State Scenario",
                "dilemmas": [{"id": "d1"}, {"id": "d2"}],
                "budget_allocation": "conservative",
                "black_swan_events": [{"id": "bs1"}, {"id": "bs2"}],
            },
            "consensus_pathway_active": {
                "consensus_score": 0.94,
                "waypoints": [{"venue_type": "boardroom"}, {"venue_type": "townhall"}],
                "milestones": ["m1", "m2", "m3"],
            },
        }
    )
    assert not isinstance(ctx.state, dict)
    captured_prompts: list[str] = []

    async def fake_local_json(prompt: str, **_kwargs):
        captured_prompts.append(prompt)
        if '"suggestions"' in prompt:
            return json.dumps({"suggestions": ["Align executive sponsors."], "summary": "Strong plan."})
        return json.dumps(
            {
                "scores": {
                    "stakeholder_alignment": 0.94,
                    "decision_quality": 85,
                    "resource_stewardship": 85,
                    "crisis_readiness": 85,
                    "strategic_coherence": 85,
                    "ethical_alignment": 85,
                },
                "findings": [{"title": "Review cadence", "severity": "MINOR", "description": "Keep cadence."}],
                "summary": "Enriched via ADK State.",
            }
        )

    with (
        patch("agents.leadership_planner_with_eval.evaluator.tools.is_local_gemma_active", return_value=True),
        patch("agents.leadership_planner_with_eval.evaluator.tools.generate_local_json_async", side_effect=fake_local_json),
        patch("agents.leadership_planner_with_memory.memory.tools.store_scenario", new=AsyncMock(return_value={})),
    ):
        eval_res = await evaluate_leadership_plan(evaluation_request="Plan", tool_context=ctx)

    assert eval_res["status"] == "success"
    assert eval_res["eval_method"] == "local_gemma_eval"
    assert eval_res["scores"]["stakeholder_alignment"] == 94
    assert "ADK State Scenario" in captured_prompts[0]
    assert ctx.state["leadership_evaluation"] == eval_res

    # _normalize_score edge cases
    assert _normalize_score(True) == 0
    assert _normalize_score(False) == 0
    assert _normalize_score(float("nan")) == 0
    assert _normalize_score(float("inf")) == 0
    assert _normalize_score({"score": float("-inf")}) == 0
    assert _normalize_score(0.85) == 85

    # Lowercase "major" finding must block passing; non-dict items are ignored safely.
    high_scores = {
        "stakeholder_alignment": 90,
        "decision_quality": 90,
        "resource_stewardship": 90,
        "crisis_readiness": 90,
        "strategic_coherence": 90,
        "ethical_alignment": 90,
    }
    built = _build_result(
        scores=high_scores,
        details={},
        eval_method="llm_judge",
        findings=[{"title": "Critical flaw", "severity": "major", "description": "Bad"}, "junk"],  # type: ignore[list-item]
    )
    assert built["passed"] is False
    assert len(built["findings"]) == 1
    assert built["findings"][0]["severity"] == "MAJOR"


def test_eval_planner_sim_config_coerces_string_counts_and_rejects_bool():
    from agents.leadership_planner_with_eval.tools import (
        _ensure_runner_count_from_scenario,
        _normalize_sim_config,
    )

    cfg = _normalize_sim_config({"stakeholder_count": "1,500"}, None)
    assert cfg["runner_count"] == 1500
    assert cfg["runner_type"] == "leadership_runner_autopilot"

    cfg_bool = _normalize_sim_config({"runner_count": True}, "custom_runner")
    assert "runner_count" not in cfg_bool
    ensured = _ensure_runner_count_from_scenario(cfg_bool, {"stakeholder_count": "2,000"})
    assert ensured["runner_count"] == 2000


@pytest.mark.asyncio
async def test_consultant_emit_sim_results_a2ui_no_placeholder_sim_id_and_zero_lau():
    from agents.leadership_planner_with_eval.skills.consultant.tools import emit_sim_results_a2ui

    ctx = _make_adk_tool_context(
        {
            "simulation_id": "—",
            "leadership_scenario": {"name": "Zero LAU Test", "stakeholder_count": "1,200"},
            "simulation_config": {"runner_count": "1,200"},
            "persona_avg_progress": {"visionary": float("nan"), "diplomat": 0.0},
            "resources": {"lau": {"current": 0, "initial": 100}},
        },
        session_id=None,
    )
    captured_calls: list[dict] = []

    async def _fake_emit(**kwargs):
        captured_calls.append(kwargs)

    with patch(
        "agents.leadership_planner_with_eval.skills.consultant.tools.emit_gateway_message",
        side_effect=_fake_emit,
    ):
        msg = await emit_sim_results_a2ui(ctx, overall_score=82)  # type: ignore[arg-type]

    assert "SUCCESS" in msg
    assert len(captured_calls) == 2
    # Placeholder "—" must NEVER leak onto the wire as simulation_id
    for call in captured_calls:
        assert call["simulation_id"] is None
        assert call["origin"]["session_id"] == "default"

    components = captured_calls[0]["data"]["surfaceUpdate"]["components"]
    by_id = {c["id"]: c["component"] for c in components}
    assert by_id["sim-meta-main"]["Text"]["text"]["literalString"] == "—"
    assert by_id["stake-v-main"]["Text"]["text"]["literalString"] == "1,200/1,200"
    assert by_id["status-capital-val"]["Text"]["text"]["literalString"] == "0 / 100 LAU"
    # Diplomat 0.0% renders ScoreBar 0; Visionary NaN falls back to "—" text row
    assert by_id["dip-bar"]["ScoreBar"]["value"]["literalNumber"] == 0
    assert by_id["vis-val"]["Text"]["text"]["literalString"] == "—"


@pytest.mark.asyncio
async def test_translation_diagnostic_fence_stripping_and_psychology_json_state(monkeypatch):
    td_tools = _load_script_module(
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
        "r2_td_tools",
    )
    monkeypatch.setattr("agents.utils.local_ai.is_local_gemma_active", lambda: False)

    voice_log = [
        {"tick": "3", "role": "cfo", "sentiment": "-0.4", "resistance": float("nan"), "stance": "skeptical"},
        {"tick": "1", "role": "cfo", "sentiment": "0.3", "resistance": 0.2, "stance": "supportive"},
    ]
    decision_log = [{"tick": "3", "decision_text": "Accelerate synergy efficiencies immediately."}]
    ctx = _make_adk_tool_context(
        {
            "echo_voice_log": json.dumps(voice_log),
            "decision_log": json.dumps(decision_log),
            "route_friction_map": None,
            "contradiction_log": None,
        }
    )

    fenced_llm_json = (
        "```json\n"
        + json.dumps(
            {
                "gaps": [
                    {
                        "stakeholder_id": "cfo",
                        "trigger_phrases": [
                            {
                                "phrase": "synergy efficiencies",
                                "tick": "3",
                                "leader_intent": "Cost discipline",
                                "stakeholder_decoded_as": "Unfunded cuts",
                                "gap_type": "efficiency -> cuts",
                                "severity": "MAJOR",
                            }
                        ],
                        "suggested_reframing": "Specify protected capital buffers.",
                        "overall_gap_severity": "CRITICAL",
                    }
                ],
                "summary": "High translation friction.",
            }
        )
        + "\n```\n\n"
    )
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(
        return_value=SimpleNamespace(text=fenced_llm_json)
    )
    monkeypatch.setattr(td_tools, "_get_client", lambda: fake_client)

    diag_res = await td_tools.emit_translation_diagnostic(ctx)  # type: ignore[arg-type]
    assert diag_res["status"] == "success"
    diag = diag_res["translation_diagnostic"]
    assert diag["eval_method"] == "llm"
    assert diag["gaps"][0]["overall_gap_severity"] == "high"
    assert diag["gaps"][0]["trigger_phrases"][0]["severity"] == "high"
    assert diag["gaps"][0]["trigger_phrases"][0]["tick"] == 3

    # Psychology handles JSON-encoded echo_voice_log, route_friction_map=None, unsorted ticks, and NaN
    psych_res = await td_tools.emit_stakeholder_psychology(ctx)  # type: ignore[arg-type]
    assert psych_res["status"] == "success"
    prof = psych_res["stakeholder_psychology"]["profiles"][0]
    # Chronological sort: tick 1 (0.3) -> tick 3 (-0.4) => declining
    assert prof["sentiment_trajectory"]["initial"] == 0.3
    assert prof["sentiment_trajectory"]["final"] == -0.4
    assert prof["sentiment_trajectory"]["trend"] == "declining"


# ---------------------------------------------------------------------------
# 3. agents/leadership_planner_with_memory/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_tools_hydrate_adk_state_and_clamp_limits(monkeypatch):
    from agents.leadership_planner_with_memory.memory import store_alloydb, tools as mem_tools
    from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore
    from agents.leadership_planner_with_memory.stakeholder_graph import _validate_edge
    from agents.leadership_planner_with_memory.stakeholder_interview import emit_stakeholder_interview

    mem_store = ScenarioMemoryStore()
    sid = await mem_store.store_scenario(
        scenario_data={
            "name": "Board Alignment",
            "_consensus_pathway_active": {"geojson": {"type": "FeatureCollection", "features": []}},
        },
        evaluation_score=88.0,
        evaluation_result={"scores": {"stakeholder_alignment": 90}},
        owner_id="user-1",
    )
    # Negative/zero limits clamp cleanly in ScenarioMemoryStore
    assert await mem_store.recall_scenarios(count=0, owner_id="user-1") == []
    assert await mem_store.recall_scenarios(count=-5, owner_id="user-1") == []
    assert await mem_store.list_scenario_bundles(owner_id="user-1", limit=-1) == []

    monkeypatch.setattr(mem_tools, "_store", mem_store)
    monkeypatch.setattr(mem_tools, "_resolve_owner_id", lambda _ctx: "user-1")

    recalled = await mem_tools.recall_scenarios(count=-3, tool_context=None)
    assert recalled["count"] == 1

    ctx = _make_adk_tool_context({}, session_id=None)
    with patch("agents.utils.pulses.emit_gateway_message", new=AsyncMock()):
        detail_res = await mem_tools.emit_scenario_detail_a2ui(ctx, scenario_id=sid)  # type: ignore[arg-type]
    assert detail_res["status"] == "success"
    assert ctx.state.get("active_scenario_id") == sid
    assert ctx.state.get("consensus_pathway_active") is not None
    assert ctx.state.get("leadership_evaluation")["overall_score"] == 88.0

    # DSN URL-encodes special password characters and brackets IPv6 hosts
    monkeypatch.setenv("ALLOYDB_HOST", "fd00::1")
    monkeypatch.setenv("ALLOYDB_USER", "admin@corp")
    monkeypatch.setenv("ALLOYDB_PASSWORD", "p@ss:w/rd#1%2")
    monkeypatch.setenv("ALLOYDB_DATABASE", "rehearsal_db")
    dsn = store_alloydb._get_dsn()
    assert "admin%40corp:p%40ss%3Aw%2Frd%231%252@[fd00::1]:5432/rehearsal_db" == dsn[len("postgresql://") :]

    # stakeholder_graph _validate_edge strips endpoint whitespace
    edge, err = _validate_edge(0, {"from": " CFO ", "to": " CTO\n", "label": "aligns"}, {"cfo", "cto"})
    assert err is None and edge == {"from": "cfo", "to": "cto", "label": "aligns"}

    # stakeholder_interview does not mutate pre-existing list in place
    original_list: list = []
    interview_ctx = _make_adk_tool_context({"stakeholder_interviews": original_list})
    iv_res = await emit_stakeholder_interview(
        json.dumps(
            {
                "stakeholder_id": "cfo",
                "topic": "Stage-gated capital discipline",
                "tone": "candid",
                "final_stance": "supportive",
                "exchanges": [
                    {
                        "question": "What unlocks your alignment?",
                        "answer": "Stage-gated budget checkpoints.",
                    }
                ],
            }
        ),
        interview_ctx,  # type: ignore[arg-type]
    )
    assert iv_res["status"] == "success"
    assert original_list == []
    assert len(interview_ctx.state.get("stakeholder_interviews")) == 1


# ---------------------------------------------------------------------------
# 4. agents/utils/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_ai_fast_path_none_score_and_negative_timeout(monkeypatch):
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    from agents.utils import local_ai

    req = LlmRequest(
        model="gemma4:26b",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_function_response(
                        name="evaluate_leadership_plan",
                        response={"status": "failed", "passed": False, "overall_score": None},
                    )
                ],
            )
        ],
    )
    # Must not raise TypeError: '>' not supported between instances of 'NoneType' and 'int'
    fast = local_ai.LocalGemmaOllama._try_fast_path_response(
        req, {"validate_and_emit_a2ui"}
    )
    assert fast is None

    # Negative LOCAL_AI_TIMEOUT falls back to 20.0 and non-dict response raises ValueError
    monkeypatch.setenv("LOCAL_AI_TIMEOUT", "-5")
    seen_timeouts: list = []

    class _FakeClient:
        def __init__(self, timeout=None, **kwargs):
            seen_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = lambda: ["not-a-dict"]
            return resp

    monkeypatch.setattr(local_ai.httpx, "AsyncClient", _FakeClient)
    with pytest.raises(ValueError, match="Non-object JSON"):
        await local_ai.generate_local_json_async("Return JSON")
    assert seen_timeouts == [20.0]


@pytest.mark.asyncio
async def test_dispatcher_scoped_targeted_broadcast_and_excluded_relay():
    from agents.utils.dispatcher import RedisOrchestratorDispatcher

    disp = object.__new__(RedisOrchestratorDispatcher)
    disp.agent_type = "runner_autopilot"
    disp.active_sessions = {"s1", "s2"}
    disp.session_simulation_map = {"s1": "sim-1", "s2": "sim-1"}
    disp._simulation_subscriptions = {"sim-1"}
    disp._seen_events = {}
    disp._pubsub = None
    triggered: list[str] = []
    disp._trigger_agent_run = lambda sid, _content: triggered.append(sid)  # type: ignore[method-assign]
    disp._record_seen_event = lambda _eid: None  # type: ignore[method-assign]

    fake_redis = MagicMock()
    fake_redis.publish = AsyncMock()

    with patch("agents.utils.dispatcher.get_shared_redis_client", return_value=fake_redis):
        # 1. Relayed/targeted scoped broadcast for ONLY ["s2"] must NOT re-trigger "s1"
        await disp._process_event(
            {
                "type": "broadcast",
                "simulation_id": "sim-1",
                "_relayed": True,
                "payload": {"data": "TICK", "targets": ["s2"]},
            }
        )
        assert triggered == ["s2"]

        # 2. Excluded runner "s3" in targets must NOT trigger a spurious Redis relay
        triggered.clear()
        await disp._process_event(
            {
                "type": "broadcast",
                "simulation_id": "sim-1",
                "payload": {
                    "data": "TICK",
                    "targets": ["s1", "s3"],
                    "exclude_runner_ids": ["s3"],
                },
            }
        )
        assert triggered == ["s1"]
        fake_redis.publish.assert_not_called()

        # 3. payload=None and empty spawn sessionId do not raise
        await disp._process_event({"type": "spawn_agent", "sessionId": "", "payload": None})
        await disp._process_event({"type": "a2ui_action", "payload": None})


@pytest.mark.asyncio
async def test_simulation_registry_cross_event_loop_single_flight():
    from agents.utils import simulation_registry

    simulation_registry._local.clear()
    simulation_registry._inflight_session.clear()

    class _SlowRedis:
        async def get(self, _key: str):
            await asyncio.sleep(0.05)
            return b"sim-cross-loop"

    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=_SlowRedis()):
        thread_result: list[str | None] = []
        thread_err: list[BaseException] = []

        def _bg_lookup():
            bg_loop = asyncio.new_event_loop()
            try:
                res = bg_loop.run_until_complete(simulation_registry.lookup("shared-sess"))
                thread_result.append(res)
            except BaseException as exc:  # pragma: no cover
                thread_err.append(exc)
            finally:
                bg_loop.close()

        t = threading.Thread(target=_bg_lookup)
        t.start()
        fg_res = await simulation_registry.lookup("shared-sess")
        t.join(timeout=5)

    assert not thread_err
    assert fg_res == "sim-cross-loop"
    assert thread_result == ["sim-cross-loop"]
    simulation_registry._local.clear()


@pytest.mark.asyncio
async def test_simdata_default_str_and_corrupted_field_isolation():
    from agents.utils import simdata

    stored_mapping: dict[str, str] = {}

    class _FakePipe:
        def hset(self, _key, mapping):
            stored_mapping.update(mapping)

        def expire(self, _key, _ttl):
            pass

        async def execute(self):
            return [1, True]

    fake_redis = MagicMock()
    fake_redis.pipeline.return_value = _FakePipe()
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    uid = uuid.uuid4()

    with patch("agents.utils.simdata.get_shared_redis_client", return_value=fake_redis):
        ok = await simdata.store_simulation_data(
            "sim-r2",
            resources={"updated_at": now, "token": uid},
            runner_count=500,
        )
        assert ok is True
        assert str(uid) in stored_mapping["resources"]

        # Corrupt one field in Redis and ensure valid fields still load
        fake_redis.hgetall = AsyncMock(
            return_value={
                b"runner_count": b"500",
                b"resources": b"{corrupted-json",
            }
        )
        loaded = await simdata.load_simulation_data("sim-r2")
        assert loaded["runner_count"] == 500
        assert loaded["resources"] is None


@pytest.mark.asyncio
async def test_plugins_nan_sanitization_runtime_redaction_comm_close_metering_paths_and_traffic(monkeypatch):
    from agents.utils import communication, metering, plugins, runtime, station_index, tenant_paths, traffic

    # 1. plugins._safe_json_dumps converts NaN / Inf to strict JSON null
    dumped = plugins._safe_json_dumps({"a": float("nan"), "b": float("inf"), "c": 1.5})
    assert "NaN" not in dumped and "Infinity" not in dumped
    assert json.loads(dumped) == {"a": None, "b": None, "c": 1.5}

    # 2. runtime._redact_redis_addr & IPv6 host/port parsing
    assert runtime._redact_redis_addr("rediss://default:s3cr3t@10.0.0.5:6379/0") == "rediss://default:***@10.0.0.5:6379/0"
    assert runtime._parse_redis_host_port("[::1]:6380") == ("::1", 6380)
    assert runtime._parse_redis_host_port("::1") == ("::1", 6379)

    # 3. SimulationA2AClient.close() closes tracked httpx.AsyncClient instances
    client = communication.SimulationA2AClient(gateway_url="http://localhost:9101")
    mock_hc = MagicMock()
    mock_hc.aclose = AsyncMock()
    client._http_clients.append(mock_hc)
    await client.close()
    mock_hc.aclose.assert_awaited_once()
    assert client._http_clients == []

    # 4. MeteringPlugin bounds _model_for_invocation, cleans up on model error, and tolerates non-dict tenant
    mp = metering.MeteringPlugin(sink=metering.StdoutSink(stream=MagicMock()))
    mp._MAX_TRACKED_INVOCATIONS = 3
    for i in range(5):
        await mp.before_model_callback(
            callback_context=SimpleNamespace(invocation_id=f"inv-{i}"),
            llm_request=SimpleNamespace(model="gemini-test"),
        )
    assert len(mp._model_for_invocation) == 3
    await mp.on_model_error_callback(
        callback_context=SimpleNamespace(invocation_id="inv-4"),
        llm_request=SimpleNamespace(model="gemini-test"),
        error=RuntimeError("boom"),
    )
    assert "inv-4" not in mp._model_for_invocation

    evt = metering.build_event(
        SimpleNamespace(state={"tenant": "corrupted-string"}, user_id="u-fallback", agent_name="planner"),
        SimpleNamespace(usage_metadata=None),
    )
    assert evt.user_id == "u-fallback"

    # 5. tenant_paths rejects path traversal segments
    with pytest.raises(ValueError, match="unsafe tenant_id"):
        tenant_paths.tenant_runs_dir("../escape")
    with pytest.raises(ValueError, match="unsafe sim_id"):
        tenant_paths.run_path("tenant-a", "../../etc/passwd")

    # 6. traffic.compute_tick_traffic handles max_congestion_ticks=0 without ZeroDivisionError
    t_res = traffic.compute_tick_traffic(
        segment_index=[{"road_name": "Main St", "start_mi": 1.0, "end_mi": 2.0, "coordinates": [[0.0, 0.0], [1.0, 1.0]]}],
        current_tick=1,
        sweep_distance_mi=0.5,
        ticks_closed={},
        max_congestion_ticks=0,
    )
    assert t_res["segments"][0]["congestion_level"] == 1.0

    # 7. station_index skips None properties, bools, and NaN mile markers
    idx = station_index.extract_station_index(
        {
            "features": [
                None,
                {"properties": None},
                {"properties": {"type": "water_station", "mi": True}},
                {"properties": {"type": "water_station", "mi": float("nan")}},
                {"properties": {"type": "water_station", "mi": "3.5"}},
            ]
        }
    )
    assert idx["water_stations"] == [3.5]
