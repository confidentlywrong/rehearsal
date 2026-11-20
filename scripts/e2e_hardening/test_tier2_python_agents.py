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

"""Tier 2 boundary, negative, and fault-injection tests for Python agents (Features #11-#15)."""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from a2a.types import TaskState

from agents.ai_echo.initialization import fetch_echo_config
from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback
from agents.ai_echo_reactive.callback import reactive_echo_callback
from agents.leadership_planner_with_eval.evaluator import tools as evaluator_tools
from agents.leadership_planner_with_eval.skills.consultant import tools as consultant_tools
from agents.simulator import broadcast
from agents.utils import gateway as gateway_utils
from agents.utils import local_ai, models, plugins, pulses, redis_pool, simdata, tenant_keys, tenant_paths
from agents.utils.simulation_executor import SimulationExecutor
from scripts.core import agent_dash
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    IsolatedRedisNamespace,
    MockOllamaServer,
    RedisUnavailableError,
    free_port,
    run_cmd,
)

pytestmark = pytest.mark.tier2


def _load_module(name: str, rel_path: str) -> Any:
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_llm_request(event_dict: dict[str, Any] | str) -> Any:
    raw_text = event_dict if isinstance(event_dict, str) else json.dumps(event_dict)
    return SimpleNamespace(
        contents=[
            SimpleNamespace(
                role="user",
                parts=[SimpleNamespace(text=raw_text, function_response=None)],
            )
        ]
    )


def _make_tool_context(state: dict[str, Any] | None = None, session_id: str | None = "sess-t2") -> Any:
    sess = SimpleNamespace(id=session_id) if session_id is not None else None
    return SimpleNamespace(state=state if state is not None else {}, session=sess)


def _set_ai_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    provider: str = "cloud_gemini",
    endpoint: str | None = None,
) -> Path:
    if endpoint is None:
        if provider != "cloud_gemini":
            raise ValueError(f"explicit endpoint is required when provider={provider!r}")
        endpoint = f"http://127.0.0.1:{free_port()}"
    settings_path = tmp_path / "ai_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "provider": provider,
                "endpoint": endpoint,
                "text_model": "gemma2:9b",
                "embed_model": "embeddinggemma:latest",
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
# Feature #11 — Lint/Typecheck & Boundary Hygiene (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f11_translation_diagnostic_broken_state_attr_and_missing_keys() -> None:
    td_mod = _load_module(
        "td_tools_t2_broken",
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
    )

    class _RaisingStateCtx:
        @property
        def state(self) -> Any:
            raise AttributeError("state unavailable")

    res_psych = await td_mod.emit_stakeholder_psychology(_RaisingStateCtx())
    assert res_psych["status"] == "error"
    assert res_psych["error"] == "no_telemetry"

    res_diag = await td_mod.emit_translation_diagnostic(_RaisingStateCtx())
    assert res_diag["status"] == "error"
    assert res_diag["error"] == "no_telemetry"

    non_mapping_ctx = SimpleNamespace(state="not-a-dict")
    res_psych2 = await td_mod.emit_stakeholder_psychology(non_mapping_ctx)
    assert res_psych2["status"] == "error"
    assert res_psych2["error"] == "no_telemetry"


@pytest.mark.asyncio
async def test_f11_translation_diagnostic_nan_inf_bool_telemetry_coercion() -> None:
    td_mod = _load_module(
        "td_tools_t2_nan",
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
    )
    ctx = _make_tool_context(
        state={
            "echo_voice_log": [
                {"tick": True, "role": "cfo", "sentiment": float("nan"), "resistance": "bad", "stance": "neutral"},
                {"tick": 2, "role": "cfo", "sentiment": float("inf"), "resistance": float("-inf"), "stance": "neutral"},
            ],
            "stakeholder_map": [{"role": "cfo", "tier": 1, "domain_principles": ["Governance"]}],
        }
    )
    res = await td_mod.emit_stakeholder_psychology(ctx)
    assert res["status"] == "success"
    p = res["stakeholder_psychology"]["profiles"][0]
    assert isinstance(p["wtt_score"], int) and 0 <= p["wtt_score"] <= 100
    assert math.isfinite(p["sentiment_trajectory"]["final"])


def test_f11_ruff_noqa_free_adk_tools() -> None:
    res = run_cmd(
        [
            "uv",
            "run",
            "ruff",
            "check",
            "--select",
            "F841,F401",
            "agents/leadership_planner_with_eval/adk_tools.py",
        ],
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert res.returncode == 0, f"ruff F841/F401 failed on adk_tools.py:\n{res.stdout}\n{res.stderr}"
    src = (REPO_ROOT / "agents/leadership_planner_with_eval/adk_tools.py").read_text(encoding="utf-8")
    assert "td_mod" not in src


def test_f11_pyright_strict_boundary_on_translation_diagnostic() -> None:
    res = run_cmd(
        [
            "npx",
            "--yes",
            "pyright@1.1.365",
            "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
            "agents/leadership_planner_with_eval/adk_tools.py",
        ],
        cwd=REPO_ROOT,
        timeout=180,
    )
    assert res.returncode == 0, f"pyright reported diagnostics:\n{res.stdout}\n{res.stderr}"


def test_f11_safe_json_sanitize_handles_circular_and_nan_inf() -> None:
    cyclic: dict[str, Any] = {"nan": float("nan"), "inf": float("inf"), "neg_inf": float("-inf")}
    cyclic["self"] = cyclic
    dumped = plugins._safe_json_dumps(cyclic)
    parsed = json.loads(dumped)
    assert parsed["nan"] is None
    assert parsed["inf"] is None
    assert parsed["neg_inf"] is None
    assert "CircularReference" in parsed["self"]


# ---------------------------------------------------------------------------
# Feature #12 — Redis Echo Config Error & Boundary Paths (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f12_fetch_echo_config_corrupt_json_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        _bind_redis(monkeypatch, rns)
        sid = f"{rns.prefix}-corrupt"
        assert rns._client is not None
        rns._client.set(f"echo_config:{sid}", "{corrupt-json", ex=120)
        with pytest.raises(ValueError, match="Invalid JSON"):
            await fetch_echo_config(sid)
    finally:
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f12_fetch_echo_config_non_dict_json_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        _bind_redis(monkeypatch, rns)
        sid = f"{rns.prefix}-list"
        assert rns._client is not None
        rns._client.set(f"echo_config:{sid}", "[1, 2, 3]", ex=120)
        with pytest.raises(ValueError, match="Expected dict"):
            await fetch_echo_config(sid)
    finally:
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_f12_fetch_echo_config_dead_redis_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    dead_port = free_port()
    await redis_pool.aclose()
    monkeypatch.setenv("REDIS_ADDR", f"redis://127.0.0.1:{dead_port}/0")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TLS_ENABLED", raising=False)
    try:
        with pytest.raises(RuntimeError, match="Failed to fetch echo config"):
            await fetch_echo_config("dead-redis-sid")
    finally:
        await redis_pool.aclose()


@pytest.mark.asyncio
async def test_f12_reactive_echo_callback_malformed_event_and_terminal_echo() -> None:
    ctx_ack = SimpleNamespace(state={}, session=SimpleNamespace(id="sess-ack"))
    ack_resp = await reactive_echo_callback(ctx_ack, _make_llm_request("{not-json"))
    assert ack_resp is not None
    assert ack_resp.content.parts[0].text == "ack"

    ctx_def = SimpleNamespace(
        state={
            "role": "cfo",
            "sentiment": -0.8,
            "stance": "hostile",
            "defected": True,
            "domain_principles": ["Governance"],
        },
        session=SimpleNamespace(id="sess-defected"),
    )
    def_resp = await reactive_echo_callback(
        ctx_def,
        _make_llm_request(
            {
                "event": "decision_round",
                "tick": 4,
                "decision_tags": {"principles_applied": ["Governance"]},
            }
        ),
    )
    assert def_resp is not None
    assert def_resp.content.parts[0].function_call.name == "process_decision_round_tool"


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_f12_autopilot_echo_callback_corrupt_redis_propagates_error(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        _bind_redis(monkeypatch, rns)
        sid = f"{rns.prefix}-auto-bad"
        assert rns._client is not None
        rns._client.set(f"echo_config:{sid}", "{bad-json", ex=120)
        ctx = SimpleNamespace(state={}, session=SimpleNamespace(id=sid))
        req = _make_llm_request({"event": "decision_round", "tick": 1})
        with pytest.raises(ValueError, match="Invalid JSON"):
            await autopilot_echo_callback(ctx, req)
    finally:
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Feature #13 — Simulator Start Gun & Broadcast Failure Paths (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f13_fire_start_gun_dead_redis_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    prep_mod = _load_module(
        "prep_tools_t2_dead",
        "agents/simulator/skills/preparing-the-race/tools.py",
    )
    dead_port = free_port()
    await redis_pool.aclose()
    monkeypatch.setenv("REDIS_ADDR", f"redis://127.0.0.1:{dead_port}/0")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TLS_ENABLED", raising=False)
    try:
        ctx = _make_tool_context(
            state={
                "simulation_id": "sim-dead-gun",
                "runner_session_ids": ["r-dead-1"],
                "simulation_config": {"runner_type": "leadership_runner_autopilot"},
            },
            session_id="sess-dead-gun",
        )
        res = await prep_mod.fire_start_gun(ctx)
        assert res["status"] == "error"
    finally:
        await redis_pool.aclose()


@pytest.mark.asyncio
async def test_f13_publish_to_runners_dead_redis_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    dead_port = free_port()
    await redis_pool.aclose()
    monkeypatch.setenv("REDIS_ADDR", f"redis://127.0.0.1:{dead_port}/0")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TLS_ENABLED", raising=False)
    try:
        with pytest.raises(Exception):
            await broadcast.publish_to_runners('{"event":"tick"}', "sim-dead-pub")
    finally:
        await redis_pool.aclose()


@pytest.mark.asyncio
async def test_f13_publish_end_simulation_dead_redis_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    dead_port = free_port()
    await redis_pool.aclose()
    monkeypatch.setenv("REDIS_ADDR", f"redis://127.0.0.1:{dead_port}/0")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TLS_ENABLED", raising=False)
    try:
        with pytest.raises(Exception):
            await broadcast.publish_end_simulation("sim-dead-end")
    finally:
        await redis_pool.aclose()


@pytest.mark.asyncio
async def test_f13_prepare_simulation_invalid_numeric_boundaries_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    prep_mod = _load_module(
        "prep_tools_t2_bounds",
        "agents/simulator/skills/preparing-the-race/tools.py",
    )
    monkeypatch.delenv("REDIS_ADDR", raising=False)
    await redis_pool.aclose()

    try:
        res_arr = await prep_mod.prepare_simulation("[1, 2, 3]", _make_tool_context(state={}))
        assert res_arr["status"] == "error"

        bad_configs = [
            {"tick_interval_seconds": 0},
            {"duration_seconds": float("nan")},
            {"runner_count": True},
            {"runner_count": 0},
        ]
        for cfg in bad_configs:
            payload = {"action": "execute", "simulation_config": cfg}
            res = await prep_mod.prepare_simulation(
                json.dumps(payload, allow_nan=True),
                _make_tool_context(state={"simulation_id": "sim-bad-num"}),
            )
            assert res["status"] == "error", f"Expected error for {cfg!r}, got {res!r}"
    finally:
        await redis_pool.aclose()


@pytest.mark.asyncio
async def test_f13_spawn_runners_without_prepare_or_gateway_500_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    prep_mod = _load_module(
        "prep_tools_t2_spawn",
        "agents/simulator/skills/preparing-the-race/tools.py",
    )
    res_unready = await prep_mod.spawn_runners(2, _make_tool_context(state={}))
    assert res_unready["status"] == "error"
    assert "prepare_simulation" in res_unready["message"]

    dead_gw = free_port()
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", f"http://127.0.0.1:{dead_gw}")
    ctx_ready = _make_tool_context(
        state={
            "simulation_ready": True,
            "simulation_id": "sim-spawn-err",
            "runner_count": 2,
            "runner_type": "leadership_runner_autopilot",
        }
    )
    res_gw = await prep_mod.spawn_runners(2, ctx_ready)
    assert res_gw["status"] == "error"


# ---------------------------------------------------------------------------
# Feature #14 — Non-Fallback Error Semantics Across Tools (8 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f14_a2ui_invalid_json_with_leadership_state_returns_error() -> None:
    a2ui_mod = _load_module("a2ui_tools_t2_json", "agents/skills/a2ui-rendering/tools.py")
    ctx = _make_tool_context(
        state={
            "leadership_scenario": {"name": "Q4 Transformation"},
            "leadership_evaluation": {"status": "success", "overall_score": 88},
        },
        session_id=None,
    )
    res = await a2ui_mod.validate_and_emit_a2ui("{invalid-json", tool_context=ctx)
    assert res["status"] == "error"
    assert res["violations"]


@pytest.mark.asyncio
async def test_f14_a2ui_schema_violations_with_leadership_state_returns_error() -> None:
    a2ui_mod = _load_module("a2ui_tools_t2_schema", "agents/skills/a2ui-rendering/tools.py")
    ctx = _make_tool_context(
        state={
            "leadership_scenario": {"name": "Q4 Transformation"},
            "leadership_evaluation": {"status": "success", "overall_score": 88},
        },
        session_id=None,
    )
    bad_payload = {
        "surfaceUpdate": {
            "surfaceId": "bad",
            "components": [
                {"id": "txt", "component": {"Text": {"text": "raw-string-missing-literalString"}}},
                {"id": "c1", "component": {"Card": {"child": "nonexistent-child"}}},
            ],
        }
    }
    res = await a2ui_mod.validate_and_emit_a2ui(json.dumps(bad_payload), tool_context=ctx)
    assert res["status"] == "error"
    assert len(res["violations"]) >= 1


def test_f14_a2ui_clamp_score_rejects_invalid_no_75_fallback() -> None:
    a2ui_mod = _load_module("a2ui_tools_t2_score", "agents/skills/a2ui-rendering/tools.py")
    for invalid in (None, float("nan"), float("inf"), True, "not-a-num"):
        assert a2ui_mod._parse_valid_score(invalid) is None
        clamped = a2ui_mod._clamp_score(invalid)
        assert clamped != 75
        assert clamped == 0


@pytest.mark.asyncio
async def test_f14_evaluator_cloud_failure_returns_explicit_error_no_75_95(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_ai_settings(monkeypatch, tmp_path, provider="cloud_gemini")
    ctx = _make_tool_context(
        state={
            "leadership_scenario": {"name": "Org Restructure", "stakeholder_count": 50},
            "consensus_pathway_active": {"consensus_score": 82, "waypoints": [{"type": "hub"}]},
        }
    )

    class _TimeoutClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(**kwargs: Any) -> Any:
                    raise TimeoutError("cloud LLM timed out")

    monkeypatch.setattr(evaluator_tools, "_get_client", lambda: _TimeoutClient())
    res_timeout = await evaluator_tools.evaluate_leadership_plan("Evaluate this plan", tool_context=ctx)
    assert res_timeout["status"] == "error"
    assert res_timeout["eval_method"] != "dcns_heuristic"
    assert res_timeout["overall_score"] == 0

    class _BadJsonClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(**kwargs: Any) -> Any:
                    return SimpleNamespace(text="NOT_VALID_JSON", candidates=[])

    monkeypatch.setattr(evaluator_tools, "_get_client", lambda: _BadJsonClient())
    res_bad_json = await evaluator_tools.evaluate_leadership_plan("Evaluate this plan", tool_context=ctx)
    assert res_bad_json["status"] == "error"
    assert res_bad_json["eval_method"] == "invalid_llm_json"
    assert res_bad_json["overall_score"] == 0


@pytest.mark.asyncio
async def test_f14_translation_diagnostic_empty_telemetry_or_llm_failure_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    td_mod = _load_module(
        "td_tools_t2_fail",
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
    )
    _set_ai_settings(monkeypatch, tmp_path, provider="cloud_gemini")

    res_empty = await td_mod.emit_translation_diagnostic(_make_tool_context(state={"echo_voice_log": []}))
    assert res_empty["status"] == "error"
    assert res_empty["error"] == "no_telemetry"

    for empty_state in ({"echo_voice_log": [], "stakeholder_map": []}, {}):
        res_psych_empty = await td_mod.emit_stakeholder_psychology(_make_tool_context(state=empty_state))
        assert res_psych_empty["status"] == "error"
        assert res_psych_empty["error"] == "no_telemetry"
        assert res_psych_empty["stakeholder_psychology"]["profiles"] == []

    class _FailingClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(**kwargs: Any) -> Any:
                    raise RuntimeError("vertex unavailable")

    monkeypatch.setattr(td_mod, "_get_client", lambda: _FailingClient())
    ctx_live = _make_tool_context(
        state={
            "echo_voice_log": [
                {"tick": 1, "role": "ops", "sentiment": 0.4, "stance": "supportive"},
                {"tick": 2, "role": "ops", "sentiment": -0.3, "stance": "resistant", "inner_thought": "Unclear"},
            ],
            "decision_log": [{"tick": 2, "decision_text": "Mandate cutover"}],
            "stakeholder_map": [{"role": "ops", "tier": 2}],
        }
    )
    res_fail = await td_mod.emit_translation_diagnostic(ctx_live)
    assert res_fail["status"] == "error"
    assert res_fail["error"] == "llm_failed"
    assert res_fail["translation_diagnostic"]["gaps"] == []


@pytest.mark.asyncio
async def test_f14_consultant_empty_markdown_or_missing_eval_returns_error() -> None:
    ctx = _make_tool_context(state={"leadership_scenario": {"name": "Test"}}, session_id="sess-cons-err")
    ap_err = await consultant_tools.emit_action_plan_a2ui(ctx, plan_markdown="   \n\t ")
    assert ap_err.startswith("ERROR:")
    assert "Days 1-30" not in ap_err

    sr_err = await consultant_tools.emit_sim_results_a2ui(ctx)
    assert sr_err.startswith("ERROR:")


@pytest.mark.asyncio
async def test_f14_consultant_omitted_stakeholder_count_renders_dash_not_1000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []

    async def _capture_emit(**kwargs: Any) -> dict[str, Any]:
        emitted.append(kwargs)
        return {"status": "sent"}

    monkeypatch.setattr(consultant_tools, "emit_gateway_message", _capture_emit)
    ctx = _make_tool_context(
        state={
            "leadership_scenario": {"name": "Missing Count"},
            "leadership_evaluation": {
                "status": "success",
                "overall_score": 80,
                "scores": {
                    "stakeholder_alignment": 80,
                    "decision_quality": 80,
                    "crisis_readiness": 80,
                },
            },
        },
        session_id="sess-cons-missing-cnt",
    )
    try:
        res = await consultant_tools.emit_sim_results_a2ui(ctx)
    finally:
        pulses.reset()

    assert res.startswith("SUCCESS:"), res
    surface_msgs = [e["data"]["surfaceUpdate"] for e in emitted if "surfaceUpdate" in e.get("data", {})]
    assert len(surface_msgs) == 1
    by_id = {c["id"]: c["component"] for c in surface_msgs[0]["components"]}
    stake_text = by_id["stake-v-main"]["Text"]["text"]["literalString"]
    assert stake_text == "—/—"
    assert "1,000" not in stake_text and "1000" not in stake_text


@pytest.mark.asyncio
async def test_f14_local_ai_rejects_non_loopback_and_corrupt_settings_and_empty_embed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        local_ai._validate_local_endpoint("http://169.254.169.254")

    bad_path = tmp_path / "corrupt_ai.json"
    bad_path.write_text("{bad-json", encoding="utf-8")
    monkeypatch.setenv("AI_SETTINGS_PATH", str(bad_path))
    monkeypatch.setattr(local_ai, "_cached_settings", None)
    monkeypatch.setattr(local_ai, "_cached_path", None)
    monkeypatch.setattr(local_ai, "_cached_mtime", None)
    with pytest.raises(ValueError, match="Invalid AI settings file"):
        local_ai.get_ai_settings()

    with MockOllamaServer(empty_embed=True, fail_chat=True) as mock_srv:
        _set_ai_settings(monkeypatch, tmp_path, provider="local_gemma", endpoint=mock_srv.base_url)
        assert await local_ai.compute_local_embedding_async("hello world") is None
        with pytest.raises(httpx.HTTPStatusError):
            await local_ai.generate_local_json_async("test prompt")

    with MockOllamaServer(fail_embed=True) as mock_fail:
        _set_ai_settings(monkeypatch, tmp_path, provider="local_gemma", endpoint=mock_fail.base_url)
        assert await local_ai.compute_local_embedding_async("hello world") is None


@pytest.mark.asyncio
async def test_f14_simdata_dead_redis_raises_simdata_error_and_compile_0_ticks_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp_mod = _load_module("comp_tools_t2", "agents/simulator/skills/completing-the-race/tools.py")
    res_zero = await comp_mod.compile_results(_make_tool_context(state={"tick_snapshots": [], "runner_count": 3}))
    assert res_zero["status"] == "error"
    assert res_zero["total_ticks"] == 0

    dead_port = free_port()
    await redis_pool.aclose()
    monkeypatch.setenv("REDIS_ADDR", f"redis://127.0.0.1:{dead_port}/0")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TLS_ENABLED", raising=False)
    try:
        with pytest.raises(simdata.SimDataError):
            await simdata.store_simulation_data("sim-dead", runner_count=1)
        with pytest.raises(simdata.SimDataError):
            await simdata.load_simulation_data("sim-dead")
    finally:
        await redis_pool.aclose()


# ---------------------------------------------------------------------------
# Feature #15 — Env Parsing, Traversal Guards & Fail-Closed Pool/Executor (5 tests)
# ---------------------------------------------------------------------------


def test_f15_gateway_url_whitespace_and_explicit_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_GATEWAY_URL", "   ")
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "  ")
    monkeypatch.setenv("GATEWAY_URL", "")
    monkeypatch.setenv("GATEWAY_ADDR", "127.0.0.1:9199/")
    assert gateway_utils.get_gateway_url() == "http://127.0.0.1:9199"

    monkeypatch.setenv("MEMORY_GATEWAY_URL", "http://127.0.0.1:9255/")
    assert gateway_utils.get_gateway_url(include_memory_env=False) == "http://127.0.0.1:9199"
    assert gateway_utils.get_gateway_url(include_memory_env=True) == "http://127.0.0.1:9255"
    assert gateway_utils.get_gateway_url("https://explicit.local:9443/") == "https://explicit.local:9443"


def test_f15_tenant_keys_and_paths_reject_traversal_and_delimiters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SAAS_MODE", "on")
    with pytest.raises(ValueError):
        tenant_keys.key_for_user("user:bad", "session", "1")
    with pytest.raises(ValueError):
        tenant_keys.key_for_user("ok-user", "bad:seg")

    monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        tenant_paths.tenant_runs_dir("../etc")
    with pytest.raises(ValueError):
        tenant_paths.run_path("u1", "a/b")


@pytest.mark.asyncio
async def test_f15_redis_pool_invalid_max_connections_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    await redis_pool.aclose()
    monkeypatch.setenv("REDIS_ADDR", "redis://127.0.0.1:9102/0")
    try:
        monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "0")
        with pytest.raises(ValueError, match="REDIS_MAX_CONNECTIONS"):
            redis_pool.get_shared_redis_client()

        monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "abc")
        with pytest.raises(ValueError, match="REDIS_MAX_CONNECTIONS"):
            redis_pool.get_shared_redis_client()
    finally:
        await redis_pool.aclose()


@pytest.mark.asyncio
async def test_f15_simulation_executor_dedup_fail_closed_when_redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    dead_port = free_port()
    await redis_pool.aclose()
    monkeypatch.setenv("REDIS_ADDR", f"redis://127.0.0.1:{dead_port}/0")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TLS_ENABLED", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.delenv("VERTEXAI_LOCATION", raising=False)

    events: list[Any] = []

    class _RecordingQueue:
        async def enqueue_event(self, event: Any) -> None:
            events.append(event)

    try:
        fake_runner = SimpleNamespace(
            app_name="test-app",
            app=SimpleNamespace(plugins=[]),
            session_service=object(),
        )
        executor = SimulationExecutor(agent_name="leadership_planner", runner=fake_runner)
        context = SimpleNamespace(
            message=SimpleNamespace(metadata={"user_id": "u1"}, context_id="ctx-1"),
            task_id="task-1",
            context_id="ctx-1",
            current_task=None,
            get_user_input=lambda: "Design a migration plan",
        )
        await executor.execute(context, _RecordingQueue())

        failed_events = [
            ev
            for ev in events
            if getattr(getattr(ev, "status", None), "state", None) == TaskState.failed
        ]
        assert failed_events, f"Expected TaskState.failed event, got {events}"
        msg_parts = failed_events[-1].status.message.parts
        text = "".join(getattr(getattr(p, "root", p), "text", "") for p in msg_parts)
        assert "Execution lock unavailable" in text
    finally:
        pulses.reset()
        await redis_pool.aclose()


def test_f15_models_get_model_skips_whitespace_and_agent_dash_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_WHITESPACE", "   \t  ")
    assert models.get_model("MODEL_WHITESPACE", models.DEFAULT_PLANNER_MODEL) == models.DEFAULT_PLANNER_MODEL

    for k in ("PUBSUB_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "PROJECT_ID", "PUBSUB_EMULATOR_HOST"):
        monkeypatch.delenv(k, raising=False)
    assert agent_dash._resolve_project_id() == ""

    monkeypatch.setenv("PUBSUB_EMULATOR_HOST", "127.0.0.1:8085")
    assert agent_dash._resolve_project_id() == "test-project"
