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

"""Tier 5 Iteration 12 adversarial regression tests for Python multi-tenant
isolation (Gaps 1-5).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.ai_echo.initialization import fetch_echo_config
from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback
from agents.ai_echo_reactive.callback import reactive_echo_callback
from agents.leadership_planner_with_memory.memory import tools as lmem_tools
from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore
from agents.planner_with_memory.memory import tools as pmem_tools
from agents.simulator import longitudinal
from agents.utils import simdata, simulation_registry, tenant_keys, tenant_paths, user_auth_context
from agents.utils.dispatcher import RedisOrchestratorDispatcher


def _load_skill_module(name: str, rel_path: str):
    path = Path(__file__).resolve().parents[2] / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PREP_TOOLS = _load_skill_module(
    "r12_preparing_the_race_tools",
    "agents/simulator/skills/preparing-the-race/tools.py",
)
_COMP_TOOLS = _load_skill_module(
    "r12_completing_the_race_tools",
    "agents/simulator/skills/completing-the-race/tools.py",
)


@pytest.fixture(autouse=True)
def _clean_r12_env(monkeypatch):
    for var in (
        "SAAS_MODE",
        "DISPATCHER_USER_ID",
        "ACCOUNT_KEY_PREFIX",
        "RACE_RUNS_DIR",
        "LEADERSHIP_PER_USER_ISOLATION",
        "PLANNER_PER_USER_ISOLATION",
        "GATEWAY_INTERNAL_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    user_auth_context._reset_for_tests()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()
    yield
    user_auth_context._reset_for_tests()
    simulation_registry._local.clear()
    simulation_registry._context_map.clear()
    simulation_registry._inflight_session.clear()
    simulation_registry._inflight_context.clear()


def _make_fake_hash_redis():
    hashes: dict[str, dict[bytes, bytes]] = {}
    kv: dict[str, bytes] = {}
    mock_redis = AsyncMock()

    def _pipeline(transaction: bool = False):
        ops: list[tuple[str, tuple]] = []
        pipe = MagicMock()

        def _hset(key: str, mapping: dict[str, str]):
            ops.append(("hset", (key, mapping)))

        def _expire(key: str, ttl: int):
            ops.append(("expire", (key, ttl)))

        async def _execute():
            for op, args in ops:
                if op == "hset":
                    bucket = hashes.setdefault(args[0], {})
                    for k, v in args[1].items():
                        bucket[k.encode()] = v.encode()
            return [True] * len(ops)

        pipe.hset = MagicMock(side_effect=_hset)
        pipe.expire = MagicMock(side_effect=_expire)
        pipe.execute = AsyncMock(side_effect=_execute)
        return pipe

    async def _hgetall(key: str):
        return hashes.get(key, {})

    async def _delete(*keys: str):
        removed = 0
        for k in keys:
            if k in hashes:
                del hashes[k]
                removed += 1
            if k in kv:
                del kv[k]
                removed += 1
        return removed

    async def _get(key: str):
        return kv.get(key)

    async def _set(key: str, val: str | bytes, ex: int | None = None):
        kv[key] = val if isinstance(val, bytes) else str(val).encode()
        return True

    mock_redis.pipeline = MagicMock(side_effect=_pipeline)
    mock_redis.hgetall = AsyncMock(side_effect=_hgetall)
    mock_redis.delete = AsyncMock(side_effect=_delete)
    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.setex = AsyncMock(return_value=True)
    return hashes, kv, mock_redis


# ---------------------------------------------------------------------------
# Gap 1: simdata.py tenant isolation & fail-closed behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r12_gap1_simdata_isolates_tenants_and_fails_closed(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    hashes, _, mock_redis = _make_fake_hash_redis()

    with patch("agents.utils.simdata.get_shared_redis_client", return_value=mock_redis):
        with simulation_registry.user_scope("alice"):
            ok = await simdata.store_simulation_data(
                "shared-sim-id",
                quest_config={"secret": "Alice M&A plan"},
                decision_log=[{"tick": 1, "decision": "Alice choice"}],
            )
            assert ok is True
            alice_loaded = await simdata.load_simulation_data("shared-sim-id")
            assert alice_loaded["quest_config"] == {"secret": "Alice M&A plan"}

        with simulation_registry.user_scope("bob"):
            bob_loaded = await simdata.load_simulation_data("shared-sim-id")
            assert bob_loaded["quest_config"] is None
            assert bob_loaded["decision_log"] is None

            # Bob clearing shared-sim-id must NOT delete Alice's key.
            await simdata.clear_simulation_data("shared-sim-id")

        with simulation_registry.user_scope("alice"):
            still_there = await simdata.load_simulation_data("shared-sim-id")
            assert still_there["quest_config"] == {"secret": "Alice M&A plan"}

    assert list(hashes.keys()) == ["u:alice:simdata:shared-sim-id"]

    # Fail-closed when user_id is missing or anonymous under SAAS_MODE=on
    with patch("agents.utils.simdata.get_shared_redis_client", return_value=mock_redis):
        for bad_uid in (None, "", "anonymous", "default", "local-dev", "user", "simulation", "simulation_user", "a:b"):
            with pytest.raises(simdata.SimDataError):
                await simdata.store_simulation_data("shared-sim-id", runner_count=1, user_id=bad_uid)
            with pytest.raises(simdata.SimDataError):
                await simdata.load_simulation_data("shared-sim-id", user_id=bad_uid)
            with pytest.raises(simdata.SimDataError):
                await simdata.clear_simulation_data("shared-sim-id", user_id=bad_uid)

    # Even with Redis unconfigured (None), SAAS_MODE=on without user fails closed
    with patch("agents.utils.simdata.get_shared_redis_client", return_value=None):
        with pytest.raises(simdata.SimDataError):
            await simdata.load_simulation_data("shared-sim-id")


@pytest.mark.asyncio
async def test_r12_gap1_simdata_preserves_legacy_keys_when_saas_off(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "off")
    hashes, _, mock_redis = _make_fake_hash_redis()

    with patch("agents.utils.simdata.get_shared_redis_client", return_value=mock_redis):
        assert await simdata.store_simulation_data("legacy-1", runner_count=5) is True
        loaded = await simdata.load_simulation_data("legacy-1")
        assert loaded["runner_count"] == 5
        assert list(hashes.keys()) == ["simdata:legacy-1"]
        assert await simdata.clear_simulation_data("legacy-1") is True
        assert list(hashes.keys()) == []


@pytest.mark.asyncio
async def test_r12_gap1_callers_forward_user_id_from_tool_context(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    hashes, _, mock_redis = _make_fake_hash_redis()

    with patch("agents.utils.simdata.get_shared_redis_client", return_value=mock_redis):
        # Store as Alice via explicit user_id (simulating planner submit_plan_to_simulator)
        await simdata.store_simulation_data(
            "sim-collab-1",
            quest_config={
                "id": "q-alice",
                "name": "Alice Quest",
                "principles": ["P1", "P2", "P3", "P4", "P5", "P6", "P7"],
                "resources": {
                    "LAU": {"initial": 80.0},
                    "BT": {"initial": 80.0},
                    "TBU": {"initial": 80.0},
                    "OWB": {"initial": 80.0},
                },
                "echoes": [
                    {
                        "role": "cfo",
                        "tier": 2,
                        "persona": "Skeptical CFO",
                        "affinity": [0.5] * 7,
                    }
                ],
            },
            user_id="alice",
        )

        # prepare_simulation as Bob on same sim_id must NOT see Alice's quest_config
        bob_ctx = SimpleNamespace(
            state={"simulation_id": "sim-collab-1", "user_id": "bob"},
            session=SimpleNamespace(id="sim-collab-1", user_id="bob"),
        )
        res_bob = await _PREP_TOOLS.prepare_simulation(
            plan_json=json.dumps({"action": "execute", "narrative": "x"}),
            tool_context=bob_ctx,  # type: ignore[arg-type]
        )
        assert res_bob["status"] == "success"
        assert res_bob["quest_id"] == "ai_powered_disruptor"

        # prepare_simulation as Alice loads Alice's quest_config
        alice_ctx = SimpleNamespace(
            state={"simulation_id": "sim-collab-1", "user_id": "alice"},
            session=SimpleNamespace(id="sim-collab-1", user_id="alice"),
        )
        res_alice = await _PREP_TOOLS.prepare_simulation(
            plan_json=json.dumps({"action": "execute", "narrative": "x"}),
            tool_context=alice_ctx,  # type: ignore[arg-type]
        )
        assert res_alice["status"] == "success"
        assert res_alice["quest_id"] == "q-alice"

        # consultant emit_sim_results_a2ui reads only the caller's simdata namespace
        from agents.leadership_planner_with_eval.skills.consultant.tools import emit_sim_results_a2ui

        await simdata.store_simulation_data(
            "sim-collab-1",
            persona_status_counts={"Visionary": {"aligned": 4}},
            runner_count=4,
            user_id="alice",
        )
        bob_consult_ctx = SimpleNamespace(
            state={
                "simulation_id": "sim-collab-1",
                "user_id": "bob",
                "leadership_scenario": {"stakeholder_count": 4},
            },
            session=SimpleNamespace(id="sess-bob", user_id="bob"),
        )
        with patch(
            "agents.leadership_planner_with_eval.skills.consultant.tools.emit_gateway_message",
            new=AsyncMock(),
        ):
            msg = await emit_sim_results_a2ui(
                tool_context=bob_consult_ctx,  # type: ignore[arg-type]
                scenario_name="Bob Scenario",
                overall_score=50,
                stakeholder_alignment=50,
                decision_quality=50,
                crisis_readiness=50,
                coaching_kotter_title="K",
                coaching_kotter_desc="D",
                coaching_adkar_title="A",
                coaching_adkar_desc="D",
                coaching_synthesis="S",
            )
        assert "SUCCESS" in msg
        assert "u:alice:simdata:sim-collab-1" in hashes


# ---------------------------------------------------------------------------
# Gap 2: longitudinal.py, tenant_paths.py, & completing-the-race/tools.py
# ---------------------------------------------------------------------------


def test_r12_gap2_from_session_state_and_tenant_paths_saas_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))

    # State carrying user_id/acting_user_id (real ADK dispatcher format, no participant_id)
    rec = longitudinal.SimulationRunRecord.from_session_state(
        {
            "simulation_id": "sim-alice-1",
            "user_id": "alice-uid",
            "acting_user_id": "alice-uid",
            "quest_id": "q1",
        }
    )
    assert rec.participant_id == "alice-uid"

    # Anonymous participant_id is overridden by non-anonymous user_id / acting_user_id / tenant.user_id / user_scope
    rec2 = longitudinal.SimulationRunRecord.from_session_state(
        {
            "simulation_id": "sim-bob-1",
            "participant_id": "anonymous",
            "acting_user_id": "bob-uid",
        }
    )
    assert rec2.participant_id == "bob-uid"

    rec3 = longitudinal.SimulationRunRecord.from_session_state(
        {"simulation_id": "sim-carol-1", "tenant": {"user_id": "carol-uid"}}
    )
    assert rec3.participant_id == "carol-uid"

    with simulation_registry.user_scope("dave-uid"):
        rec4 = longitudinal.SimulationRunRecord.from_session_state({"simulation_id": "sim-dave-1"})
        assert rec4.participant_id == "dave-uid"

    # Missing or anonymous in SAAS_MODE=on fails closed
    for bad in ("", "anonymous", "default", "local-dev", "user", "simulation", "simulation_user"):
        with pytest.raises(ValueError):
            longitudinal.SimulationRunRecord.from_session_state({"simulation_id": "s1", "user_id": bad})
        with pytest.raises(ValueError):
            tenant_paths.resolve_tenant_id(fallback=bad)

    with pytest.raises(ValueError):
        tenant_paths.resolve_tenant_id(fallback=None)

    # user_scope resolves tenant_paths even when fallback is anonymous
    with simulation_registry.user_scope("alice-uid"):
        assert tenant_paths.resolve_tenant_id(fallback="anonymous") == "alice-uid"

    # Store writes under alice-uid/, never anonymous/
    store = longitudinal.SimulationRunStore()
    store.save_run(rec)
    assert (tmp_path / "alice-uid" / "runs.ndjson").exists()
    assert not (tmp_path / "anonymous").exists()

    # Unbound SAAS_MODE=on save_run with anonymous record fails closed
    with pytest.raises(ValueError):
        store.save_run(longitudinal.SimulationRunRecord(run_id="r-anon", participant_id="anonymous"))
    assert not (tmp_path / "anonymous").exists()


def test_r12_gap2_from_session_state_and_tenant_paths_saas_off_parity(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "off")
    rec = longitudinal.SimulationRunRecord.from_session_state({"simulation_id": "sim-off"})
    assert rec.participant_id == "anonymous"
    assert tenant_paths.resolve_tenant_id(fallback="anonymous") == "anonymous"
    assert tenant_paths.resolve_tenant_id(fallback=None) == ""


@pytest.mark.asyncio
async def test_r12_gap2_compile_results_routes_by_user_id_and_blocks_anonymous_in_saas(monkeypatch, tmp_path):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))

    _, _, mock_redis = _make_fake_hash_redis()
    ctx = SimpleNamespace(
        session=SimpleNamespace(id="sess-alice-comp", user_id="alice-uid"),
        state={
            "simulation_id": "sim-comp-1",
            "user_id": "alice-uid",
            "acting_user_id": "alice-uid",
            "quest_id": "ai_powered_disruptor",
            "tick_snapshots": [{"tick": 0, "runners_reporting": 1, "avg_velocity": 1.0, "avg_water": 1.0, "avg_distance": 1.0}],
            "runner_count": 1,
            "finished_runner_ids": ["r1"],
            "principle_scores": [1.0] * 7,
        },
    )

    with patch("agents.utils.simdata.get_shared_redis_client", return_value=mock_redis):
        res = await _COMP_TOOLS.compile_results(ctx)  # type: ignore[arg-type]

    assert res["status"] == "success"
    assert (tmp_path / "alice-uid" / "runs.ndjson").exists()
    assert not (tmp_path / "anonymous").exists()

    # Unauthenticated compile_results in SAAS_MODE=on fails closed without touching anonymous/
    anon_ctx = SimpleNamespace(
        session=SimpleNamespace(id="sess-anon-comp", user_id="anonymous"),
        state={
            "simulation_id": "sim-comp-anon",
            "tick_snapshots": [{"tick": 0, "runners_reporting": 1, "avg_velocity": 1.0, "avg_water": 1.0, "avg_distance": 1.0}],
            "runner_count": 1,
            "finished_runner_ids": ["r1"],
        },
    )
    res_anon = await _COMP_TOOLS.compile_results(anon_ctx)  # type: ignore[arg-type]
    assert res_anon["status"] == "error"
    assert not (tmp_path / "anonymous").exists()


# ---------------------------------------------------------------------------
# Gap 3: memory/tools.py _resolve_owner_id honors SAAS_MODE=on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r12_gap3_leadership_memory_isolates_under_saas_mode_without_extra_flag(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.delenv("LEADERSHIP_PER_USER_ISOLATION", raising=False)

    mem_store = ScenarioMemoryStore()
    with patch.object(lmem_tools, "_store", mem_store):
        alice_ctx = SimpleNamespace(
            session=SimpleNamespace(id="s-a", user_id="alice-uid"),
            state={
                "user_id": "alice-uid",
                "leadership_scenario": {"title": "Alice Secret Scenario"},
                "leadership_evaluation": {"overall_score": 88.0},
            },
        )
        bob_ctx = SimpleNamespace(
            session=SimpleNamespace(id="s-b", user_id="bob-uid"),
            state={
                "user_id": "bob-uid",
                "leadership_scenario": {"title": "Bob Scenario"},
                "leadership_evaluation": {"overall_score": 72.0},
            },
        )

        res_a = await lmem_tools.store_scenario(alice_ctx)  # type: ignore[arg-type]
        res_b = await lmem_tools.store_scenario(bob_ctx)  # type: ignore[arg-type]
        assert res_a["status"] == "success"
        assert res_b["status"] == "success"

        # State-only user_id (when session.user_id is absent/anonymous) also works
        alice_state_only = SimpleNamespace(session=SimpleNamespace(id="s-a2", user_id="user"), state={"acting_user_id": "alice-uid"})
        assert lmem_tools._resolve_owner_id(alice_state_only) == "alice-uid"  # type: ignore[arg-type]

        # Bob cannot recall or fetch Alice's scenario
        bob_recall = await lmem_tools.recall_scenarios(tool_context=bob_ctx)  # type: ignore[arg-type]
        assert [s["scenario_id"] for s in bob_recall["scenarios"]] == [res_b["scenario_id"]]
        bob_get = await lmem_tools.get_scenario(res_a["scenario_id"], tool_context=bob_ctx)  # type: ignore[arg-type]
        assert bob_get["status"] == "error"

        # Missing/anonymous context fails closed (PermissionError from _resolve_owner_id; error dict from tools)
        anon_ctx = SimpleNamespace(session=SimpleNamespace(id="s-anon", user_id="anonymous"), state={})
        with pytest.raises(PermissionError):
            lmem_tools._resolve_owner_id(anon_ctx)  # type: ignore[arg-type]
        with pytest.raises(PermissionError):
            lmem_tools._resolve_owner_id(None)

        err_recall = await lmem_tools.recall_scenarios(tool_context=anon_ctx)  # type: ignore[arg-type]
        assert err_recall["status"] == "error"
        assert err_recall["scenarios"] == []
        err_selected = await lmem_tools.get_selected_scenarios(
            tool_context=anon_ctx,  # type: ignore[arg-type]
            scenario_ids=[res_a["scenario_id"]],
        )
        assert err_selected["status"] == "error"
        assert err_selected["scenarios"] == []


@pytest.mark.asyncio
async def test_r12_gap3_planner_memory_isolates_under_saas_mode_without_extra_flag(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.delenv("PLANNER_PER_USER_ISOLATION", raising=False)

    routes: dict[str, object] = {}

    class _MemRouteStore:
        async def store_route(self, route_data, evaluation_score=None, evaluation_result=None, owner_id=None):
            rid = f"r-{len(routes) + 1}"
            routes[rid] = SimpleNamespace(
                route_id=rid,
                route_data=route_data,
                evaluation_score=evaluation_score,
                evaluation_result=evaluation_result,
                owner_id=owner_id,
                created_at=longitudinal.datetime.now(longitudinal.timezone.utc),
                simulations=[],
            )
            return rid

        async def recall_routes(self, count=10, sort_by="recent", owner_id=None):
            items = list(routes.values())
            if owner_id is not None:
                items = [r for r in items if getattr(r, "owner_id", None) == owner_id]
            return items[:count]

        async def get_route(self, route_id, owner_id=None):
            r = routes.get(route_id)
            if r is None or (owner_id is not None and getattr(r, "owner_id", None) != owner_id):
                return None
            return r

        async def get_best_route(self, owner_id=None):
            return None

    with patch.object(pmem_tools, "_store", _MemRouteStore()):
        alice_ctx = SimpleNamespace(
            session=SimpleNamespace(id="s-a", user_id="alice-uid"),
            state={"marathon_route": {"name": "Alice Route"}},
        )
        bob_ctx = SimpleNamespace(
            session=SimpleNamespace(id="s-b", user_id="bob-uid"),
            state={"marathon_route": {"name": "Bob Route"}},
        )
        ra = await pmem_tools.store_route(alice_ctx)  # type: ignore[arg-type]
        rb = await pmem_tools.store_route(bob_ctx)  # type: ignore[arg-type]
        assert ra["status"] == "success"
        assert rb["status"] == "success"

        bob_routes = await pmem_tools.recall_routes(bob_ctx)  # type: ignore[arg-type]
        assert [r["route_id"] for r in bob_routes["routes"]] == [rb["route_id"]]
        assert (await pmem_tools.get_route(ra["route_id"], bob_ctx))["status"] == "error"  # type: ignore[arg-type]

        anon_ctx = SimpleNamespace(session=SimpleNamespace(id="s-x", user_id="local-dev"), state={})
        with pytest.raises(PermissionError):
            pmem_tools._resolve_owner_id(anon_ctx)  # type: ignore[arg-type]
        err = await pmem_tools.recall_routes(anon_ctx)  # type: ignore[arg-type]
        assert err["status"] == "error"
        assert err["routes"] == []


# ---------------------------------------------------------------------------
# Gap 4: echo_config_key & echo initialization tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r12_gap4_echo_config_isolated_per_tenant_and_fail_closed(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "off")
    assert tenant_keys.echo_config_key("echo-1") == "echo_config:echo-1"

    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:9101")

    with pytest.raises(ValueError):
        tenant_keys.echo_config_key("echo-1")
    with pytest.raises(ValueError):
        tenant_keys.echo_config_key("echo-1", user_id="anonymous")

    _, kv, mock_redis = _make_fake_hash_redis()

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "sessions": [
                {"sessionId": "r-1", "agentType": "leadership_runner_autopilot"},
                {"sessionId": "echo-1", "agentType": "ai_echo_reactive"},
            ]
        }
    )
    post_cm = AsyncMock()
    post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    post_cm.__aexit__ = AsyncMock(return_value=False)
    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = MagicMock(return_value=post_cm)

    alice_ctx = MagicMock()
    alice_ctx.session = SimpleNamespace(id="sim-alice", user_id="alice")
    alice_ctx.state = {
        "simulation_ready": True,
        "simulation_id": "sim-alice",
        "user_id": "alice",
        "runner_type": "leadership_runner_autopilot",
        "echo_configs": [{"role": "cfo", "tier": 2, "hidden_agenda": "Alice confidential"}],
    }

    with (
        patch("aiohttp.ClientSession", return_value=mock_http),
        patch("agents.utils.auth.get_id_token", return_value=None),
        patch("agents.utils.redis_pool.get_shared_redis_client", return_value=mock_redis),
    ):
        spawn_res = await _PREP_TOOLS.spawn_runners(count=1, tool_context=alice_ctx)
    assert spawn_res["status"] == "success"
    assert list(kv.keys()) == ["u:alice:echo_config:echo-1"]

    with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=mock_redis):
        with simulation_registry.user_scope("bob"):
            assert await fetch_echo_config("echo-1") == {}

        with simulation_registry.user_scope("alice"):
            alice_cfg = await fetch_echo_config("echo-1")
            assert alice_cfg["role"] == "cfo"
            assert alice_cfg["hidden_agenda"] == "Alice confidential"

        # Callbacks (reactive & autopilot) also resolve tenant-scoped echo_config
        req = SimpleNamespace(
            contents=[SimpleNamespace(role="user", parts=[SimpleNamespace(text=json.dumps({"event": "decision_round", "tick": 1}))])],
            config=SimpleNamespace(system_instruction=""),
        )
        cb_state_bob: dict = {"user_id": "bob"}
        cb_ctx_bob = SimpleNamespace(state=cb_state_bob, session=SimpleNamespace(id="echo-1", user_id="bob"))
        await reactive_echo_callback(cb_ctx_bob, req)  # type: ignore[arg-type]
        assert cb_state_bob["role"] == "unknown"

        cb_state_alice: dict = {"user_id": "alice"}
        cb_ctx_alice = SimpleNamespace(state=cb_state_alice, session=SimpleNamespace(id="echo-1", user_id="alice"))
        await autopilot_echo_callback(cb_ctx_alice, req)  # type: ignore[arg-type]
        assert cb_state_alice["role"] == "cfo"

        # Unbound SAAS_MODE=on fails closed on fetch_echo_config
        with pytest.raises(ValueError):
            await fetch_echo_config("echo-1")


# ---------------------------------------------------------------------------
# Gap 5: dispatcher.py spawn_agent cross-tenant session collision guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r12_gap5_dispatcher_rejects_cross_tenant_spawn_session_collision(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    monkeypatch.delenv("DISPATCHER_USER_ID", raising=False)

    runner = MagicMock()
    runner.app = SimpleNamespace(name="planner_agent", root_agent=SimpleNamespace(name="planner_root"))
    disp = RedisOrchestratorDispatcher(runner=runner, dispatch_mode="callable", suppress_gateway_emission=True)
    disp._trigger_agent_run = MagicMock()
    disp._run_on_dispatcher_loop = AsyncMock()

    with patch("agents.utils.simulation_registry.get_shared_redis_client", return_value=None):
        await disp.handle_event(
            {
                "type": "spawn_agent",
                "eventId": "evt-a-1",
                "sessionId": "sess-shared",
                "payload": {
                    "agentType": "planner_agent",
                    "user_id": "alice",
                    "simulation_id": "sim-alice",
                    "user_auth_token": "alice-secret-jwt",
                },
            }
        )
        assert disp.session_user_map.get("sess-shared") == "alice"
        assert disp.session_simulation_map.get("sess-shared") == "sim-alice"
        assert user_auth_context.get_session_token("sess-shared") == "alice-secret-jwt"

        # Cross-tenant spawn_agent from Bob reusing Alice's active sessionId MUST be rejected
        await disp.handle_event(
            {
                "type": "spawn_agent",
                "eventId": "evt-b-1",
                "sessionId": "sess-shared",
                "payload": {
                    "agentType": "planner_agent",
                    "user_id": "bob",
                    "simulation_id": "sim-bob",
                    "user_auth_token": "bob-evil-jwt",
                },
            }
        )
        assert disp.session_user_map.get("sess-shared") == "alice"
        assert disp.session_simulation_map.get("sess-shared") == "sim-alice"
        assert user_auth_context.get_session_token("sess-shared") == "alice-secret-jwt"

        # Alice's subsequent broadcast on sess-shared still triggers Alice's session
        await disp.handle_event(
            {
                "type": "broadcast",
                "eventId": "evt-a-msg",
                "sessionId": "sess-shared",
                "payload": {
                    "user_id": "alice",
                    "targets": ["sess-shared"],
                    "data": "hello",
                },
            }
        )
        disp._trigger_agent_run.assert_called_once()
        assert disp._trigger_agent_run.call_args[0][0] == "sess-shared"

        # Same-tenant idempotent re-spawn by Alice remains permitted
        await disp.handle_event(
            {
                "type": "spawn_agent",
                "eventId": "evt-a-2",
                "sessionId": "sess-shared",
                "payload": {
                    "agentType": "planner_agent",
                    "user_id": "alice",
                    "simulation_id": "sim-alice",
                    "user_auth_token": "alice-refreshed-jwt",
                },
            }
        )
        assert disp.session_user_map.get("sess-shared") == "alice"
        assert user_auth_context.get_session_token("sess-shared") == "alice-refreshed-jwt"
