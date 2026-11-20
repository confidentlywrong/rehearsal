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

"""Tier 1 category-partition functional tests for Python agents (Features #11-#15)."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.ai_echo.initialization import fetch_echo_config
from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback
from agents.ai_echo_reactive.callback import reactive_echo_callback
from agents.leadership_planner_with_eval import adk_tools
from agents.leadership_planner_with_eval.evaluator import config as eval_config
from agents.leadership_planner_with_eval.skills.consultant import tools as consultant_tools
from agents.leadership_planner_with_memory.memory import retrieval_tools
from agents.simulator import broadcast, decision_classifier, longitudinal
from agents.simulator.collector import RaceCollector
from agents.utils import gateway as gateway_utils
from agents.utils import local_ai, models, pulses, redis_pool, simdata, tenant_keys, tenant_paths
from agents.utils.simulation_executor import _final_response_text
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    IsolatedRedisNamespace,
    MockOllamaServer,
    RedisUnavailableError,
    run_cmd,
)

pytestmark = pytest.mark.tier1


def _load_module(name: str, rel_path: str) -> Any:
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_llm_request(event_dict: dict[str, Any]) -> Any:
    return SimpleNamespace(
        contents=[
            SimpleNamespace(
                role="user",
                parts=[SimpleNamespace(text=json.dumps(event_dict), function_response=None)],
            )
        ]
    )


def _make_tool_context(state: dict[str, Any] | None = None, session_id: str | None = "sess-t1") -> Any:
    sess = SimpleNamespace(id=session_id) if session_id is not None else None
    return SimpleNamespace(state=state if state is not None else {}, session=sess)


def _set_local_ai_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    provider: str = "local_gemma",
    endpoint: str,
    text_model: str = "gemma2:9b",
    embed_model: str = "embeddinggemma:latest",
) -> Path:
    settings_path = tmp_path / "ai_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "provider": provider,
                "endpoint": endpoint,
                "text_model": text_model,
                "embed_model": embed_model,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr(local_ai, "_cached_settings", None)
    monkeypatch.setattr(local_ai, "_cached_path", None)
    monkeypatch.setattr(local_ai, "_cached_mtime", None)
    return settings_path


def _bind_redis(monkeypatch: pytest.MonkeyPatch, rns: IsolatedRedisNamespace) -> None:
    monkeypatch.setenv("REDIS_ADDR", rns.url)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TLS_ENABLED", raising=False)


# ---------------------------------------------------------------------------
# Feature #11 — Python Lint (ruff) & Typecheck (pyright) Cleanliness (5 tests)
# ---------------------------------------------------------------------------


def test_f11_ruff_clean_agents_and_scripts() -> None:
    res = run_cmd(["uv", "run", "ruff", "check", "agents/", "scripts/"], cwd=REPO_ROOT, timeout=180)
    assert res.returncode == 0, f"ruff check failed:\n{res.stdout}\n{res.stderr}"


def test_f11_pyright_clean_agents() -> None:
    res = run_cmd(["npx", "--yes", "pyright@1.1.365", "agents/"], cwd=REPO_ROOT, timeout=300)
    assert res.returncode == 0, f"pyright failed:\n{res.stdout}\n{res.stderr}"


def test_f11_pyproject_ruff_pyright_config_valid() -> None:
    res = run_cmd(["uv", "run", "ruff", "check", "--show-settings", "agents/"], cwd=REPO_ROOT, timeout=60)
    assert res.returncode == 0, f"ruff --show-settings failed:\n{res.stdout}\n{res.stderr}"
    assert "3.13" in res.stdout

    py_cfg = json.loads(
        json.dumps(
            __import__("tomllib").loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pyright"]
        )
    )
    assert py_cfg.get("pythonVersion") == "3.13"


def test_f11_adk_tools_loads_without_unused_or_import_errors() -> None:
    tools = adk_tools.get_tools()
    names = []
    for t in tools:
        func = getattr(t, "func", None)
        if func is not None and hasattr(func, "__name__"):
            names.append(func.__name__)
    for expected in (
        "evaluate_leadership_plan",
        "start_simulation",
        "submit_plan_to_simulator",
        "validate_and_emit_a2ui",
    ):
        assert expected in names, f"Expected tool {expected!r} in {names}"


@pytest.mark.asyncio
async def test_f11_translation_diagnostic_state_obj_safe_on_broken_state() -> None:
    td_mod = _load_module(
        "td_tools_t1",
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
    )

    class _BrokenStateCtx:
        @property
        def state(self) -> Any:
            raise AttributeError("broken state property")

    res = await td_mod.emit_stakeholder_psychology(_BrokenStateCtx())
    assert res["status"] == "error"
    assert res["error"] == "no_telemetry"


# ---------------------------------------------------------------------------
# Feature #12 — Async Redis Coroutine Hydration in AI Echoes (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f12_fetch_echo_config_hydrates_from_live_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        _bind_redis(monkeypatch, rns)
        sid = f"{rns.prefix}-echo-cfg"
        expected = {
            "role": "ops_director",
            "tier": 2,
            "domain_principles": ["Governance", "Agility"],
            "initial_sentiment": 0.25,
        }
        assert rns._client is not None
        rns._client.set(f"echo_config:{sid}", json.dumps(expected), ex=120)

        loaded = await fetch_echo_config(sid)
        assert loaded == expected
    finally:
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f12_reactive_echo_callback_hydrates_and_routes_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        _bind_redis(monkeypatch, rns)
        sid = f"{rns.prefix}-reactive-det"
        cfg = {
            "role": "compliance_lead",
            "tier": 2,
            "domain_principles": ["Ethical Leadership & Governance"],
            "initial_sentiment": 0.4,
        }
        assert rns._client is not None
        rns._client.set(f"echo_config:{sid}", json.dumps(cfg), ex=120)

        state: dict[str, Any] = {}
        ctx = SimpleNamespace(state=state, session=SimpleNamespace(id=sid))
        req = _make_llm_request(
            {
                "event": "decision_round",
                "tick": 1,
                "decision_text": "Speed up release",
                "decision_tags": {"principles_applied": ["Adaptive Decision-Making & Agility"]},
            }
        )
        resp = await reactive_echo_callback(ctx, req)
        assert resp is not None
        assert state["role"] == "compliance_lead"
        assert state["domain_principles"] == ["Ethical Leadership & Governance"]
        fc = resp.content.parts[0].function_call
        assert fc is not None and fc.name == "process_decision_round_tool"
    finally:
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f12_reactive_echo_callback_falls_through_to_llm_on_relevant_principle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        _bind_redis(monkeypatch, rns)
        sid = f"{rns.prefix}-reactive-llm"
        cfg = {
            "role": "ethics_officer",
            "tier": 2,
            "domain_principles": ["Governance"],
            "initial_sentiment": 0.1,
        }
        assert rns._client is not None
        rns._client.set(f"echo_config:{sid}", json.dumps(cfg), ex=120)

        state: dict[str, Any] = {}
        ctx = SimpleNamespace(state=state, session=SimpleNamespace(id=sid))
        req = _make_llm_request(
            {
                "event": "decision_round",
                "tick": 2,
                "decision_text": "Revise governance charter",
                "decision_tags": {"principles_applied": ["Governance"]},
            }
        )
        resp = await reactive_echo_callback(ctx, req)
        assert resp is None
        assert state["role"] == "ethics_officer"
    finally:
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f12_autopilot_echo_callback_hydrates_from_redis_and_returns_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        _bind_redis(monkeypatch, rns)
        sid = f"{rns.prefix}-autopilot"
        cfg = {
            "role": "ops_lead",
            "tier": 3,
            "initial_sentiment": -0.5,
            "initial_resistance": 0.6,
        }
        assert rns._client is not None
        rns._client.set(f"echo_config:{sid}", json.dumps(cfg), ex=120)

        state: dict[str, Any] = {}
        ctx = SimpleNamespace(state=state, session=SimpleNamespace(id=sid))
        req = _make_llm_request(
            {
                "event": "decision_round",
                "tick": 3,
                "decision_text": "Enforce immediate cutover",
                "decision_tags": {},
            }
        )
        resp = await autopilot_echo_callback(ctx, req)
        assert resp is not None
        assert state["role"] == "ops_lead"
        assert state["tier"] == 3
        fc = resp.content.parts[0].function_call
        assert fc is not None and fc.name == "process_decision_round_tool"
        assert "wall_severity" in fc.args
        assert float(fc.args["wall_severity"]) > 0.0
    finally:
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.requires_redis
def test_f12_shared_redis_client_loop_isolation_and_aclose(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    async def _ping_and_close() -> bool:
        try:
            client = redis_pool.get_shared_redis_client()
            assert client is not None
            return bool(await client.ping())
        finally:
            await redis_pool.aclose()

    try:
        _bind_redis(monkeypatch, rns)
        assert asyncio.run(_ping_and_close()) is True
        assert asyncio.run(_ping_and_close()) is True
    finally:
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Feature #13 — Simulator Start Gun skip_pubsub & Broadcast Propagation (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f13_prepare_simulation_valid_plan_configures_state(monkeypatch: pytest.MonkeyPatch) -> None:
    prep_mod = _load_module(
        "prep_tools_t1",
        "agents/simulator/skills/preparing-the-race/tools.py",
    )
    monkeypatch.delenv("REDIS_ADDR", raising=False)
    try:
        await redis_pool.aclose()
        ctx = _make_tool_context(state={"simulation_id": "sim-t1-prep"}, session_id="sess-t1-prep")
        plan = {
            "action": "execute",
            "narrative": "Phased platform migration",
            "route": {"type": "FeatureCollection", "features": []},
            "simulation_config": {
                "duration_seconds": 30,
                "tick_interval_seconds": 5,
                "runner_count": 4,
                "runner_type": "leadership_runner_autopilot",
            },
        }
        res = await prep_mod.prepare_simulation(json.dumps(plan), ctx)
        assert res["status"] == "success"
        assert ctx.state["simulation_ready"] is True
        assert ctx.state["max_ticks"] == 6
        assert ctx.state["runner_count"] == 4
        assert ctx.state["simulation_config"]["runner_type"] == "leadership_runner_autopilot"
    finally:
        await redis_pool.aclose()


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f13_start_race_collector_autopilot_sets_skip_pubsub_true(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    prep_mod = _load_module(
        "prep_tools_t1_start",
        "agents/simulator/skills/preparing-the-race/tools.py",
    )
    sid = f"{rns.prefix}-collector-sess"
    try:
        _bind_redis(monkeypatch, rns)
        monkeypatch.setenv("SAAS_MODE", "off")
        ctx = _make_tool_context(
            state={
                "simulation_ready": True,
                "simulation_id": f"{rns.prefix}-sim",
                "runner_session_ids": [f"{rns.prefix}-r1", f"{rns.prefix}-r2"],
                "simulation_config": {"runner_type": "leadership_runner_autopilot"},
            },
            session_id=sid,
        )
        res = await prep_mod.start_race_collector(ctx)
        assert res["status"] == "success"
        collector = RaceCollector.get(sid)
        assert isinstance(collector, RaceCollector)
        assert collector.skip_pubsub is True
        assert collector._task is None
        assert rns._client is not None
        assert rns._client.get(f"collector:active:{sid}") == "1"
    finally:
        collector = RaceCollector.get(sid)
        if collector is not None:
            await collector.stop()
        rns._client.delete(f"collector:active:{sid}", f"collector:buffer:{sid}") if rns._client else None
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f13_fire_start_gun_autopilot_forwards_skip_pubsub_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    prep_mod = _load_module(
        "prep_tools_t1_gun",
        "agents/simulator/skills/preparing-the-race/tools.py",
    )
    sid = f"{rns.prefix}-gun-sess"
    sim_id = f"{rns.prefix}-gun-sim"
    runner_id = f"{rns.prefix}-runner-1"
    pubsub = None
    try:
        _bind_redis(monkeypatch, rns)
        monkeypatch.setenv("SAAS_MODE", "off")
        assert rns._client is not None
        rns._client.set(f"simreg:session:{runner_id}", sim_id, ex=120)

        pubsub = rns._client.pubsub()
        pubsub.subscribe(f"simulation:{sim_id}:broadcast")
        # Drain subscription confirmation frame.
        pubsub.get_message(timeout=1.0)

        ctx = _make_tool_context(
            state={
                "simulation_id": sim_id,
                "runner_session_ids": [runner_id],
                "max_ticks": 8,
                "simulation_config": {
                    "runner_type": "leadership_runner_autopilot",
                    "race_distance_mi": 10.0,
                },
            },
            session_id=sid,
        )
        res = await prep_mod.fire_start_gun(ctx)
        assert res["status"] == "success"
        collector = RaceCollector.get(sid)
        assert isinstance(collector, RaceCollector)
        assert collector.skip_pubsub is True

        msg = pubsub.get_message(timeout=2.0)
        while msg and msg.get("type") != "message":
            msg = pubsub.get_message(timeout=2.0)
        assert msg is not None and msg.get("type") == "message"
        envelope = json.loads(msg["data"])
        assert envelope["type"] == "broadcast"
        assert envelope["simulation_id"] == sim_id
        inner = json.loads(envelope["payload"]["data"])
        assert inner["event"] == "start_gun"
    finally:
        if pubsub is not None:
            pubsub.close()
        collector = RaceCollector.get(sid)
        if collector is not None:
            await collector.stop()
        if rns._client is not None:
            rns._client.delete(f"collector:active:{sid}", f"collector:buffer:{sid}", f"simreg:session:{runner_id}")
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f13_publish_to_runners_delivers_envelope_with_exclusions(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    pubsub = None
    try:
        _bind_redis(monkeypatch, rns)
        monkeypatch.setenv("SAAS_MODE", "off")
        sim_id = f"{rns.prefix}-pub-sim"
        assert rns._client is not None
        pubsub = rns._client.pubsub()
        pubsub.subscribe(f"simulation:{sim_id}:broadcast")
        pubsub.get_message(timeout=1.0)

        await broadcast.publish_to_runners('{"event":"tick","tick":1}', sim_id, exclude_runner_ids=["r1"])

        msg = pubsub.get_message(timeout=2.0)
        while msg and msg.get("type") != "message":
            msg = pubsub.get_message(timeout=2.0)
        assert msg is not None
        env = json.loads(msg["data"])
        assert env["type"] == "broadcast"
        assert env["simulation_id"] == sim_id
        assert env["payload"]["data"] == '{"event":"tick","tick":1}'
        assert env["payload"]["exclude_runner_ids"] == ["r1"]
    finally:
        if pubsub is not None:
            pubsub.close()
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f13_publish_end_simulation_delivers_end_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    pubsub = None
    try:
        _bind_redis(monkeypatch, rns)
        monkeypatch.setenv("SAAS_MODE", "off")
        sim_id = f"{rns.prefix}-end-sim"
        assert rns._client is not None
        pubsub = rns._client.pubsub()
        pubsub.subscribe(f"simulation:{sim_id}:broadcast")
        pubsub.get_message(timeout=1.0)

        await broadcast.publish_end_simulation(sim_id)

        msg = pubsub.get_message(timeout=2.0)
        while msg and msg.get("type") != "message":
            msg = pubsub.get_message(timeout=2.0)
        assert msg is not None
        env = json.loads(msg["data"])
        assert env["type"] == "end_simulation"
        assert env["simulation_id"] == sim_id
        assert env["eventId"]
    finally:
        if pubsub is not None:
            pubsub.close()
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Feature #14 — Python Explicit Contracts & Non-Fallback Behavior (6 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f14_a2ui_valid_surface_update_succeeds() -> None:
    a2ui_mod = _load_module("a2ui_tools_t1", "agents/skills/a2ui-rendering/tools.py")
    payload = {
        "surfaceUpdate": {
            "surfaceId": "main",
            "components": [
                {"id": "lbl", "component": {"Text": {"text": {"literalString": "Ready"}, "usageHint": "h2"}}},
                {"id": "card1", "component": {"Card": {"child": "lbl"}}},
            ],
        }
    }
    ctx = _make_tool_context(state={}, session_id=None)
    res = await a2ui_mod.validate_and_emit_a2ui(json.dumps(payload), tool_context=ctx)
    assert res["status"] == "success"
    assert res["a2ui"]["surfaceUpdate"]["surfaceId"] == "main"


@pytest.mark.asyncio
async def test_f14_local_ai_settings_and_mock_ollama_json_and_embed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_ollama: MockOllamaServer,
) -> None:
    _set_local_ai_settings(monkeypatch, tmp_path, endpoint=mock_ollama.base_url)
    settings = local_ai.get_ai_settings()
    assert settings.provider == "local_gemma"
    assert local_ai.is_local_gemma_active() is True

    raw_json = await local_ai.generate_local_json_async("Return status json")
    assert json.loads(raw_json) == {"status": "ok"}

    vec = await local_ai.compute_local_embedding_async("organizational alignment memo")
    assert vec is not None
    assert len(vec) == 3072
    norm = math.sqrt(sum(v * v for v in vec))
    assert math.isclose(norm, 1.0, rel_tol=1e-4)


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f14_simdata_store_and_load_roundtrip_live_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        _bind_redis(monkeypatch, rns)
        sim_id = f"{rns.prefix}-simdata"
        ok = await simdata.store_simulation_data(
            simulation_id=sim_id,
            quest_config={"id": "q1", "name": "Cloud Migration"},
            echo_voice_log=[{"tick": 1, "role": "cfo", "sentiment": 0.4}],
            runner_count=12,
        )
        assert ok is True
        loaded = await simdata.load_simulation_data(sim_id)
        assert loaded["quest_config"]["id"] == "q1"
        assert loaded["runner_count"] == 12
        assert len(loaded["echo_voice_log"]) == 1

        cleared = await simdata.clear_simulation_data(sim_id)
        assert cleared is True
        after = await simdata.load_simulation_data(sim_id)
        assert after["runner_count"] is None
    finally:
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_f14_compile_results_valid_snapshots_and_idempotent_normalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    comp_mod = _load_module("comp_tools_t1", "agents/simulator/skills/completing-the-race/tools.py")
    monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("REDIS_ADDR", raising=False)
    try:
        await redis_pool.aclose()
        ctx = _make_tool_context(
            state={
                "simulation_id": "sim-comp-1",
                "participant_id": "user-comp-1",
                "quest_id": "ai_powered_disruptor",
                "runner_count": 2,
                "finished_runner_ids": ["r1"],
                "principle_scores": [5.0, 2.5, 0.0, 1.0, 4.0, 3.0, 5.0],
                "tick_snapshots": [
                    {
                        "tick": 1,
                        "real_time_minutes": 10.0,
                        "avg_velocity": 0.5,
                        "avg_water": 80.0,
                        "avg_distance": 1.0,
                        "runners_reporting": 2,
                        "status_counts": {"aligned": 1, "neutral": 1},
                    },
                    {
                        "tick": 2,
                        "real_time_minutes": 20.0,
                        "avg_velocity": 0.6,
                        "avg_water": 85.0,
                        "avg_distance": 2.0,
                        "runners_reporting": 2,
                        "status_counts": {"aligned": 1, "neutral": 1},
                    },
                    {"tick": 3, "runners_reporting": 2, "status_counts": {"aligned": 1, "neutral": 1}},
                    {"tick": 4, "runners_reporting": 2, "status_counts": {"aligned": 1, "neutral": 1}},
                    {"tick": 5, "runners_reporting": 2, "status_counts": {"aligned": 1, "neutral": 1}},
                ],
            },
            session_id="sess-comp-1",
        )
        res1 = await comp_mod.compile_results(ctx)
        assert res1["status"] == "success"
        first_scores = list(ctx.state["principle_scores"])
        assert first_scores == [100.0, 50.0, 0.0, 20.0, 80.0, 60.0, 100.0]

        res2 = await comp_mod.compile_results(ctx)
        assert res2["status"] == "success"
        assert ctx.state["principle_scores"] == first_scores
    finally:
        await redis_pool.aclose()


@pytest.mark.asyncio
async def test_f14_stakeholder_psychology_real_telemetry_computes_wtt() -> None:
    td_mod = _load_module(
        "td_tools_t1_psych",
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
    )
    ctx = _make_tool_context(
        state={
            "echo_voice_log": [
                {"tick": 1, "role": "cfo", "sentiment": 0.2, "resistance": 0.3, "stance": "neutral", "wall_severity": 4.0},
                {"tick": 2, "role": "cfo", "sentiment": 0.6, "resistance": 0.1, "stance": "supportive", "wall_severity": 2.5},
            ],
            "decision_log": [
                {"tick": 1, "principles_applied": ["Resource Optimization & Strategic Allocation"]},
                {"tick": 2, "principles_applied": ["Resource Optimization & Strategic Allocation"]},
            ],
            "stakeholder_map": [
                {
                    "role": "cfo",
                    "tier": 1,
                    "domain_principles": ["Resource Optimization & Strategic Allocation"],
                }
            ],
        }
    )
    res = await td_mod.emit_stakeholder_psychology(ctx)
    assert res["status"] == "success"
    profiles = res["stakeholder_psychology"]["profiles"]
    assert len(profiles) == 1
    p = profiles[0]
    assert 0 <= p["wtt_score"] <= 100
    assert p["malicious_compliance_risk"] in {"low", "medium", "high"}
    assert p["sentiment_trajectory"]["trend"] == "rising"


@pytest.mark.asyncio
async def test_f14_consultant_valid_markdown_and_scores_emit_a2ui(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[dict[str, Any]] = []

    async def _capture_emit(**kwargs: Any) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(consultant_tools, "emit_gateway_message", _capture_emit)
    monkeypatch.delenv("REDIS_ADDR", raising=False)
    try:
        await redis_pool.aclose()
        ctx = _make_tool_context(
            state={
                "simulation_id": "sim-cons-1",
                "leadership_scenario": {"name": "ERP Rollout", "stakeholder_count": 20},
                "leadership_evaluation": {
                    "status": "success",
                    "overall_score": 84,
                    "scores": {
                        "stakeholder_alignment": 85,
                        "decision_quality": 82,
                        "crisis_readiness": 80,
                        "resource_stewardship": 86,
                        "strategic_coherence": 84,
                        "ethical_alignment": 87,
                    },
                    "summary": "Strong coalition sequence.",
                },
                "persona_status_counts": {"Visionary": {"aligned": 4, "neutral": 1}},
                "persona_avg_progress": {"Visionary": 80.0},
            },
            session_id="sess-cons-1",
        )
        ap_res = await consultant_tools.emit_action_plan_a2ui(ctx, plan_markdown="# Phase 1\n- Align CFO")
        assert ap_res.startswith("SUCCESS:")
        sr_res = await consultant_tools.emit_sim_results_a2ui(
            ctx,
            coaching_kotter_title="Guiding Coalition",
            coaching_kotter_desc="Engage operations early",
            coaching_adkar_title="Awareness",
            coaching_adkar_desc="Clarify rollout milestones",
        )
        assert sr_res.startswith("SUCCESS:")
        assert any("surfaceUpdate" in e["data"] for e in emitted)
        assert any("beginRendering" in e["data"] for e in emitted)
    finally:
        pulses.reset()
        await redis_pool.aclose()


# ---------------------------------------------------------------------------
# Feature #15 — Canonical Model/URL/Port Constants & Docstrings (6 tests)
# ---------------------------------------------------------------------------


def test_f15_canonical_models_and_get_model_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    assert models.DEFAULT_PLANNER_MODEL == "gemini-3-flash-preview"
    assert models.DEFAULT_EVALUATOR_MODEL == "gemini-3.1-pro-preview"
    assert models.DEFAULT_FLASH_LITE_MODEL == "gemini-3.5-flash-lite"
    assert models.DEFAULT_EMBEDDING_MODEL == "gemini-embedding-001"

    monkeypatch.delenv("TEST_PRIMARY_MODEL", raising=False)
    monkeypatch.setenv("TEST_FALLBACK_MODEL", "custom-fallback")
    assert models.get_model("TEST_PRIMARY_MODEL", "default-m", fallback_env_vars=("TEST_FALLBACK_MODEL",)) == "custom-fallback"
    monkeypatch.setenv("TEST_PRIMARY_MODEL", "  custom-primary  ")
    assert models.get_model("TEST_PRIMARY_MODEL", "default-m", fallback_env_vars=("TEST_FALLBACK_MODEL",)) == "custom-primary"

    assert eval_config.MODEL  # resolves via models.get_model
    runner_const_src = (REPO_ROOT / "agents/runner/constants.py").read_text(encoding="utf-8")
    assert "8101" not in runner_const_src
    assert models.DEFAULT_FLASH_LITE_MODEL in runner_const_src or "DEFAULT_FLASH_LITE_MODEL" in runner_const_src


def test_f15_canonical_gateway_url_port_9101_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("MEMORY_GATEWAY_URL", "GATEWAY_INTERNAL_URL", "GATEWAY_URL", "GATEWAY_ADDR"):
        monkeypatch.delenv(key, raising=False)

    assert gateway_utils.DEFAULT_GATEWAY_PORT == 9101
    assert gateway_utils.DEFAULT_GATEWAY_URL == "http://127.0.0.1:9101"
    assert gateway_utils.get_gateway_url() == "http://127.0.0.1:9101"
    assert retrieval_tools._gateway_url() == "http://127.0.0.1:9101"

    # Scan non-test Python files in agents/ and scripts/ (excluding e2e_hardening) for stale 8101 defaults.
    stale_8101: list[str] = []
    for root_rel in ("agents", "scripts"):
        for py_file in (REPO_ROOT / root_rel).rglob("*.py"):
            rel = str(py_file.relative_to(REPO_ROOT))
            if rel.startswith("scripts/e2e_hardening/") or "/tests/" in rel or py_file.name.startswith("test_"):
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "8101" in node.value:
                    stale_8101.append(f"{rel}:{node.lineno}:{node.value!r}")
    assert stale_8101 == [], f"Stale 8101 references found: {stale_8101}"


def test_f15_tenant_keys_and_tenant_paths_saas_on_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SAAS_MODE", "off")
    assert tenant_keys.key_for_user("user-1", "simulation", "s1", "broadcast") == "simulation:s1:broadcast"

    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    assert tenant_keys.key_for_user("user-1", "simulation", "s1", "broadcast") == "u:user-1:simulation:s1:broadcast"

    monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
    assert tenant_paths.summary_path("user-1") == tmp_path / "user-1" / "runs.ndjson"


def test_f15_agent_dash_health_and_root_from_any_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from scripts.core import agent_dash

    monkeypatch.chdir(tmp_path)
    client = TestClient(agent_dash.app)
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json() == {"status": "ok", "service": "agent_dash"}

    root_resp = client.get("/")
    assert root_resp.status_code == 200
    assert "<html" in root_resp.text.lower()


def test_f15_docstrings_match_runtime_behavior() -> None:
    dc_doc = (decision_classifier.__doc__ or "").lower()
    assert "heuristic" not in dc_doc
    assert "deterministic" in dc_doc

    long_doc = (longitudinal.__doc__ or "").lower()
    assert "use_alloydb" not in long_doc
    assert "ndjson" in long_doc

    tk_doc = (tenant_keys.__doc__ or "").lower()
    assert "saas_mode=off" in tk_doc


def test_f15_simulation_executor_valid_execution_and_final_response() -> None:
    text_event = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text="Final leadership summary", function_response=None)])
    )
    assert _final_response_text(text_event) == "Final leadership summary"

    fn_event = SimpleNamespace(
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(
                    text=None,
                    function_response=SimpleNamespace(response={"result": {"status": "success", "ticks": 5}}),
                )
            ]
        )
    )
    assert json.loads(_final_response_text(fn_event)) == {"status": "success", "ticks": 5}
