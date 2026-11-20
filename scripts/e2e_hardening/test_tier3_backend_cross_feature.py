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

"""Tier 3 pairwise cross-feature backend integration tests (Features #1-#15)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg2
import pytest
import redis
import websockets

from agents.ai_echo.initialization import fetch_echo_config
from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback
from agents.ai_echo_reactive.callback import reactive_echo_callback
from agents.leadership_planner_with_eval.evaluator import tools as evaluator_tools
from agents.leadership_planner_with_eval.skills.consultant import tools as consultant_tools
from agents.leadership_planner_with_memory.memory import retrieval_tools, store_alloydb
from agents.leadership_planner_with_memory.memory import tools as memory_tools
from agents.simulator import broadcast, collector as collector_mod
from agents.simulator.collector import RaceCollector
from agents.utils import local_ai, metering, pulses, redis_pool, simdata, tenant_keys
from gen_proto.gateway import gateway_pb2
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    EphemeralGoServer,
    IsolatedPostgresSchema,
    IsolatedRedisNamespace,
    MockOllamaServer,
    PostgresUnavailableError,
    RedisUnavailableError,
    free_port,
    run_cmd,
)

pytestmark = pytest.mark.tier3

_BUILT_BINARIES: dict[str, tuple[int, Path, str, str]] = {}


def _require_go_binary(pkg_path: str, go_bin_dir: Path, name: str) -> Path:
    """Build a Go binary once per session into go_bin_dir and assert cleanly inside test."""
    if pkg_path not in _BUILT_BINARIES:
        go_bin_dir.mkdir(parents=True, exist_ok=True)
        out_path = go_bin_dir / name
        proc = run_cmd(["go", "build", "-o", str(out_path), pkg_path], cwd=REPO_ROOT, timeout=180)
        _BUILT_BINARIES[pkg_path] = (proc.returncode, out_path, proc.stdout, proc.stderr)
    rc, out_path, stdout, stderr = _BUILT_BINARIES[pkg_path]
    assert rc == 0, f"{pkg_path} failed to build (M2 in progress):\n{stdout}\n{stderr}"
    return out_path


def _load_module(name: str, rel_path: str) -> Any:
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_pg_dsn_env(monkeypatch: pytest.MonkeyPatch, pg: IsolatedPostgresSchema) -> None:
    parsed = urllib.parse.urlsplit(pg.dsn)
    monkeypatch.setenv("ALLOYDB_HOST", parsed.hostname or "127.0.0.1")
    monkeypatch.setenv("ALLOYDB_PORT", str(parsed.port or 5432))
    monkeypatch.setenv("ALLOYDB_USER", urllib.parse.unquote(parsed.username or "postgres"))
    monkeypatch.setenv("ALLOYDB_PASSWORD", urllib.parse.unquote(parsed.password or ""))
    monkeypatch.setenv("ALLOYDB_DATABASE", (parsed.path or "/postgres").lstrip("/") or "postgres")
    monkeypatch.setenv("ALLOYDB_SCHEMA", pg.schema_name)


def _bind_redis(monkeypatch: pytest.MonkeyPatch, rns: IsolatedRedisNamespace) -> None:
    monkeypatch.setenv("REDIS_ADDR", rns.url)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_TLS_ENABLED", raising=False)


class _MockA2AAgentServer:
    """Minimal HTTP server serving /.well-known/agent-card.json for Go catalog discovery."""

    def __init__(self, agent_names: list[str]) -> None:
        self.agent_names = agent_names
        self._servers: list[ThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []
        self.urls: list[str] = []

    def __enter__(self) -> _MockA2AAgentServer:
        for name in self.agent_names:
            class _Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"
                agent_name = name

                def log_message(self, format: str, *args: Any) -> None:
                    return

                def do_GET(self) -> None:  # noqa: N802
                    port = self.server.server_address[1]
                    payload = json.dumps(
                        {
                            "name": self.agent_name,
                            "description": f"Mock {self.agent_name}",
                            "version": "1.0.0",
                            "url": f"http://127.0.0.1:{port}",
                            "capabilities": {},
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            port = int(srv.server_address[1])
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            self._servers.append(srv)
            self._threads.append(t)
            self.urls.append(f"http://127.0.0.1:{port}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        for t in self._threads:
            t.join(timeout=2.0)


def _gateway_env(
    pg: IsolatedPostgresSchema | None,
    rns: IsolatedRedisNamespace,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "REDIS_ADDR": rns.addr,
        "REDIS_PASSWORD": rns.password,
        "REDIS_PREFIX": rns.prefix,
        "SAAS_MODE": "off",
    }
    if pg is not None:
        env["DATABASE_URL"] = pg.search_path_dsn
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# T3-01: Python SqliteSink -> Go SQLiteDrainer -> Postgres & /api/v1/usage
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_redis
@pytest.mark.requires_go
def test_t3_01_python_sqlite_sink_to_go_drainer_to_postgres_and_usage(
    isolated_pg_schema: IsolatedPostgresSchema,
    go_bin_dir: Path,
    tmp_path: Path,
) -> None:
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    sqlite_path = tmp_path / "metering.db"
    try:
        with EphemeralGoServer(
            gateway_binary,
            env=_gateway_env(
                isolated_pg_schema,
                rns,
                extra={
                    "METERING_SINK": "sqlite",
                    "METERING_SQLITE_PATH": str(sqlite_path),
                    "AI_SETTINGS_PATH": str(tmp_path / "ai.json"),
                    "MEMORY_STORAGE_DIR": str(tmp_path / "mem"),
                    "LOCAL_DEV_USER_EMAIL": f"t301-{rns.prefix}@test.local",
                },
            ),
        ) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                init_resp = client.post("/api/v1/me/init", json={"display_name": "Meter User", "timezone": "UTC"})
                assert init_resp.status_code in (200, 201), init_resp.text
                user_id = init_resp.json()["id"]

                sink = metering.SqliteSink(sqlite_path)
                now_iso = datetime.now(timezone.utc).isoformat()
                sink.emit(
                    metering.MeteringEvent(
                        user_id=user_id,
                        sim_id="sim-t3-01",
                        agent_type="leadership_planner",
                        model="gemini-3-flash-preview",
                        prompt_tokens=100,
                        completion_tokens=50,
                        total_tokens=150,
                        cost_micros=500,
                        timestamp_iso=now_iso,
                    )
                )
                sink.emit(
                    metering.MeteringEvent(
                        user_id=user_id,
                        sim_id="sim-t3-01",
                        agent_type="leadership_planner",
                        model="gemini-3-flash-preview",
                        prompt_tokens=None,
                        completion_tokens=None,
                        total_tokens=None,
                        cost_micros=None,
                        timestamp_iso=now_iso,
                    )
                )

                usage_resp = client.get("/api/v1/usage")
                assert usage_resp.status_code == 200, usage_resp.text
                assert usage_resp.json()["tokens_used"] == 150

        with sqlite3.connect(sqlite_path) as sconn:
            drained_vals = [row[0] for row in sconn.execute("SELECT drained FROM metering_ledger ORDER BY id")]
            assert drained_vals == [1, 1]

        with psycopg2.connect(isolated_pg_schema.search_path_dsn) as pconn:
            with pconn.cursor() as cur:
                cur.execute(
                    "SELECT total_tokens FROM usage_ledger WHERE user_id = %s ORDER BY id",
                    (user_id,),
                )
                rows = [r[0] for r in cur.fetchall()]
                assert rows == [150, None]
                cur.execute("SELECT total_tokens FROM usage_rollup_monthly WHERE user_id = %s", (user_id,))
                assert cur.fetchone()[0] == 150

        # Verify failed UpsertMonthlyRollup keeps drained=0 via Go drainer regression test.
        go_res = run_cmd(
            [
                "go",
                "test",
                "-v",
                "-count=1",
                "-run",
                "TestSQLiteDrainer_RollupFailureDoesNotMarkDrainedAndPropagatesError",
                "./internal/billing",
            ],
            cwd=REPO_ROOT,
            timeout=180,
        )
        assert go_res.returncode == 0, f"Go rollup failure check failed:\n{go_res.stdout}\n{go_res.stderr}"
    finally:
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-02: Postgres Init Fixtures + Seed + Go Migrations + AlloyDBScenarioStore
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_redis
@pytest.mark.requires_go
@pytest.mark.asyncio
async def test_t3_02_pg_init_fixtures_seed_migrations_and_alloydb_store(
    isolated_pg_schema: IsolatedPostgresSchema,
    go_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        sql_files = [
            REPO_ROOT / "scripts/postgres-init/01_schema.sql",
            REPO_ROOT / "scripts/postgres-init/03_leadership_schema.sql",
            REPO_ROOT / "scripts/postgres-init/04_leadership_demo_fixtures.sql",
            REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/seed_local.sql",
        ]
        with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                for sf in sql_files:
                    cur.execute(sf.read_text(encoding="utf-8"))

        # Boot compiled Go gateway twice against the same isolated schema to verify
        # idempotent ApplyMigration + schema_migrations tracking.
        gw_env = _gateway_env(
            isolated_pg_schema,
            rns,
            extra={
                "AI_SETTINGS_PATH": str(tmp_path / "ai.json"),
                "MEMORY_STORAGE_DIR": str(tmp_path / "mem"),
            },
        )
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw1:
            assert httpx.get(f"{gw1.base_url}/healthz", timeout=5.0).status_code == 200
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw2:
            assert httpx.get(f"{gw2.base_url}/healthz", timeout=5.0).status_code == 200

        with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM schema_migrations")
                assert cur.fetchone()[0] == 7
                cur.execute(
                    "SELECT scenario_id, owner_id FROM leadership_scenarios "
                    "WHERE scenario_id LIKE 'seed-leadership-00%' ORDER BY scenario_id"
                )
                seed_rows = cur.fetchall()
                assert len(seed_rows) == 4
                assert all(owner == "__seed__" for _, owner in seed_rows)

        _parse_pg_dsn_env(monkeypatch, isolated_pg_schema)
        store = store_alloydb.AlloyDBScenarioStore()
        try:
            sc = await store.get_scenario("seed-leadership-001")
            assert sc is not None
            assert sc.owner_id == "__seed__"
            listed = await store.recall_scenarios(count=10)
            assert len(listed) == 4
        finally:
            await store.close()
    finally:
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-03: Gateway WS Hub + Registry + Spawn Quota
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_redis
@pytest.mark.requires_go
@pytest.mark.asyncio
async def test_t3_03_gateway_ws_hub_registry_and_spawn_quota(
    isolated_pg_schema: IsolatedPostgresSchema,
    go_bin_dir: Path,
    tmp_path: Path,
) -> None:
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        with _MockA2AAgentServer(["leadership_runner_autopilot"]) as mock_agent:
            common_extra = {
                "SAAS_MODE": "on",
                "AGENT_URLS": ",".join(mock_agent.urls),
                "MAX_RUNNERS": "10",
                "AI_SETTINGS_PATH": str(tmp_path / "ai.json"),
                "MEMORY_STORAGE_DIR": str(tmp_path / "mem"),
            }
            with (
                EphemeralGoServer(
                    gateway_binary,
                    env=_gateway_env(
                        isolated_pg_schema,
                        rns,
                        extra={**common_extra, "LOCAL_DEV_USER_EMAIL": f"usera-{rns.prefix}@test.local"},
                    ),
                ) as gw_a,
                EphemeralGoServer(
                    gateway_binary,
                    env=_gateway_env(
                        isolated_pg_schema,
                        rns,
                        extra={**common_extra, "LOCAL_DEV_USER_EMAIL": f"userb-{rns.prefix}@test.local"},
                    ),
                ) as gw_b,
            ):
                async with httpx.AsyncClient(timeout=10.0) as client:
                    for base in (gw_a.base_url, gw_b.base_url):
                        r_init = await client.post(f"{base}/api/v1/me/init", json={"display_name": "Tester"})
                        assert r_init.status_code in (200, 201), r_init.text

                    # Approve both users under SAAS_MODE=on so /spawn and /ws are permitted.
                    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
                        conn.autocommit = True
                        with conn.cursor() as cur:
                            cur.execute("UPDATE users SET status = 'approved'")

                    spawn_ok = await client.post(
                        f"{gw_a.base_url}/api/v1/spawn",
                        json={
                            "agents": [{"agentType": "leadership_runner_autopilot", "count": 1}],
                            "simulation_id": "sim-owned-a",
                        },
                    )
                    assert spawn_ok.status_code in (200, 201), spawn_ok.text

                    # Free tier max_concurrent_runs = 1 -> second distinct simulation rejected with 402.
                    spawn_cap = await client.post(
                        f"{gw_a.base_url}/api/v1/spawn",
                        json={
                            "agents": [{"agentType": "leadership_runner_autopilot", "count": 1}],
                            "simulation_id": "sim-second-a",
                        },
                    )
                    assert spawn_cap.status_code == 402, spawn_cap.text

                rclient = redis.Redis(host=rns.host, port=rns.port, password=rns.password or None)
                try:
                    async with (
                        websockets.connect(f"{gw_a.ws_url}/ws") as ws_a,
                        websockets.connect(f"{gw_b.ws_url}/ws") as ws_b,
                    ):
                        await ws_a.send(json.dumps({"type": "subscribe_simulation", "simulation_id": "sim-owned-a"}))
                        await asyncio.sleep(0.15)

                        await ws_b.send(json.dumps({"type": "subscribe_simulation", "simulation_id": "sim-owned-a"}))
                        raw_msg = await asyncio.wait_for(ws_b.recv(), timeout=3.0)
                        err_frame = json.loads(raw_msg)
                        assert err_frame.get("status") == "error"
                        assert err_frame.get("data", {}).get("error") == "simulation_not_owned"

                        wrapper = gateway_pb2.Wrapper(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            type="json",
                            event="narrative",
                            simulation_id="sim-owned-a",
                            payload=b'{"t3":"owned_delivery"}',
                        )
                        rclient.publish("gateway:broadcast", wrapper.SerializeToString())

                        delivered_a: gateway_pb2.Wrapper | None = None
                        deadline = time.monotonic() + 3.0
                        while time.monotonic() < deadline:
                            frame = await asyncio.wait_for(
                                ws_a.recv(),
                                timeout=max(0.1, deadline - time.monotonic()),
                            )
                            if isinstance(frame, str):
                                raise AssertionError(f"Owner ws_a received unexpected text frame: {frame}")
                            parsed = gateway_pb2.Wrapper()
                            parsed.ParseFromString(frame)
                            if parsed.simulation_id == "sim-owned-a" and parsed.payload == b'{"t3":"owned_delivery"}':
                                delivered_a = parsed
                                break
                        assert delivered_a is not None, "Owner ws_a did not receive sim-owned-a broadcast"

                        await asyncio.sleep(0.1)
                        marker = gateway_pb2.Wrapper(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            type="system_marker",
                            event="system_marker",
                            payload=b"{}",
                        )
                        rclient.publish("gateway:broadcast", marker.SerializeToString())

                        leaked_b = False
                        deadline_b = time.monotonic() + 3.0
                        while time.monotonic() < deadline_b:
                            frame_b = await asyncio.wait_for(
                                ws_b.recv(),
                                timeout=max(0.1, deadline_b - time.monotonic()),
                            )
                            if isinstance(frame_b, bytes):
                                parsed_b = gateway_pb2.Wrapper()
                                parsed_b.ParseFromString(frame_b)
                                if parsed_b.simulation_id == "sim-owned-a":
                                    leaked_b = True
                                    break
                                if parsed_b.type == "system_marker":
                                    break
                        assert not leaked_b, "Non-owner ws_b received sim-owned-a broadcast"
                finally:
                    rclient.close()
    finally:
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-04: Simulator Prepare + Broadcast + Redis -> AI Echo Hydration
# ---------------------------------------------------------------------------


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_t3_04_simulator_prepare_broadcast_redis_to_echo_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    prep_mod = _load_module("prep_tools_t3_04", "agents/simulator/skills/preparing-the-race/tools.py")
    sim_id = f"{rns.prefix}-t304-sim"
    coll_sid = f"{rns.prefix}-t304-coll"
    runner_sid = f"{rns.prefix}-t304-runner"
    echo_auto_sid = f"{rns.prefix}-t304-echo-auto"
    echo_react_sid = f"{rns.prefix}-t304-echo-react"
    pubsub = None
    try:
        _bind_redis(monkeypatch, rns)
        monkeypatch.setenv("SAAS_MODE", "off")
        assert rns._client is not None

        rns._client.set(f"simreg:session:{runner_sid}", sim_id, ex=120)
        rns._client.set(
            f"echo_config:{echo_auto_sid}",
            json.dumps({"role": "ops_lead", "tier": 3, "initial_sentiment": -0.3}),
            ex=120,
        )
        rns._client.set(
            f"echo_config:{echo_react_sid}",
            json.dumps({"role": "cfo", "tier": 2, "domain_principles": ["Governance"]}),
            ex=120,
        )

        pubsub = rns._client.pubsub()
        pubsub.subscribe(f"simulation:{sim_id}:broadcast")
        pubsub.get_message(timeout=1.0)

        ctx = SimpleNamespace(
            state={
                "simulation_id": sim_id,
                "runner_session_ids": [runner_sid],
                "max_ticks": 6,
                "simulation_config": {"runner_type": "leadership_runner_autopilot", "race_distance_mi": 5.0},
            },
            session=SimpleNamespace(id=coll_sid),
        )
        gun_res = await prep_mod.fire_start_gun(ctx)
        assert gun_res["status"] == "success"

        msg = pubsub.get_message(timeout=2.0)
        while msg and msg.get("type") != "message":
            msg = pubsub.get_message(timeout=2.0)
        assert msg is not None
        assert json.loads(msg["data"])["type"] == "broadcast"

        loaded_cfg = await fetch_echo_config(echo_auto_sid)
        assert loaded_cfg["role"] == "ops_lead"

        auto_state: dict[str, Any] = {}
        auto_req = SimpleNamespace(
            contents=[
                SimpleNamespace(
                    role="user",
                    parts=[SimpleNamespace(text=json.dumps({"event": "decision_round", "tick": 1}), function_response=None)],
                )
            ]
        )
        auto_resp = await autopilot_echo_callback(
            SimpleNamespace(state=auto_state, session=SimpleNamespace(id=echo_auto_sid)),
            auto_req,
        )
        assert auto_resp is not None
        assert auto_state["role"] == "ops_lead"

        react_state: dict[str, Any] = {}
        react_resp = await reactive_echo_callback(
            SimpleNamespace(state=react_state, session=SimpleNamespace(id=echo_react_sid)),
            auto_req,
        )
        assert react_resp is not None
        assert react_state["domain_principles"] == ["Governance"]

        await broadcast.publish_end_simulation(sim_id)
        end_msg = pubsub.get_message(timeout=2.0)
        while end_msg and end_msg.get("type") != "message":
            end_msg = pubsub.get_message(timeout=2.0)
        assert end_msg is not None
        assert json.loads(end_msg["data"])["type"] == "end_simulation"
    finally:
        if pubsub is not None:
            pubsub.close()
        coll = RaceCollector.get(coll_sid)
        if coll is not None:
            await coll.stop()
        if rns._client is not None:
            rns._client.delete(
                f"collector:active:{coll_sid}",
                f"collector:buffer:{coll_sid}",
                f"simreg:session:{runner_sid}",
            )
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-05: Python retrieval_tools -> Go Gateway 9101 Memory RAG + MockOllamaServer
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_redis
@pytest.mark.requires_go
@pytest.mark.asyncio
async def test_t3_05_python_retrieval_tools_to_go_gateway_9101_memory_rag(
    isolated_pg_schema: IsolatedPostgresSchema,
    go_bin_dir: Path,
    mock_ollama: MockOllamaServer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    settings_path = tmp_path / "ai_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "provider": "local_gemma",
                "endpoint": mock_ollama.base_url,
                "text_model": "gemma2:9b",
                "embed_model": "embeddinggemma:latest",
            }
        ),
        encoding="utf-8",
    )
    try:
        with EphemeralGoServer(
            gateway_binary,
            env=_gateway_env(
                isolated_pg_schema,
                rns,
                extra={
                    "AI_SETTINGS_PATH": str(settings_path),
                    "MEMORY_STORAGE_DIR": str(tmp_path / "mem_store"),
                    "LOCAL_DEV_USER_EMAIL": f"t305-{rns.prefix}@test.local",
                },
            ),
        ) as gw:
            async with httpx.AsyncClient(base_url=gw.base_url, timeout=15.0) as client:
                r_init = await client.post("/api/v1/me/init", json={"display_name": "RAG User"})
                assert r_init.status_code in (200, 201), r_init.text

                r_an = await client.post(
                    "/api/v1/memory/analyses",
                    json={"org_name": "Acme Corp", "title": "Cloud Cutover"},
                )
                assert r_an.status_code == 201, r_an.text
                analysis_id = r_an.json()["id"]

                memo_bytes = b"Frontline engineering teams need clear phased cutover milestones and rollback buffers."
                r_doc = await client.post(
                    f"/api/v1/memory/analyses/{analysis_id}/documents",
                    data={"kind": "memo"},
                    files={"file": ("cutover_memo.txt", memo_bytes, "text/plain")},
                )
                assert r_doc.status_code == 201, r_doc.text

            monkeypatch.setenv("MEMORY_GATEWAY_URL", gw.base_url)
            monkeypatch.delenv("MEMORY_GATEWAY_TOKEN", raising=False)

            ctx_explicit = SimpleNamespace(state={}, session=SimpleNamespace(id="sess-rag-1"))
            res_exp = await retrieval_tools.retrieve_document_passages(
                "phased cutover milestones",
                tool_context=ctx_explicit,
                analysis_id=analysis_id,
            )
            assert res_exp["status"] == "success"
            assert res_exp["passage_count"] >= 1
            assert "phased cutover" in res_exp["passages"][0]["text"]
            assert "memory_citations_available" in ctx_explicit.state

            ctx_auto = SimpleNamespace(state={}, session=SimpleNamespace(id="sess-rag-2"))
            res_auto = await retrieval_tools.retrieve_document_passages(
                "rollback buffers",
                tool_context=ctx_auto,
            )
            assert res_auto["status"] == "success"
            assert ctx_auto.state[retrieval_tools.MEMORY_ANALYSIS_ID_KEY] == analysis_id
    finally:
        if retrieval_tools._rag_http_client is not None and not retrieval_tools._rag_http_client.is_closed:
            await retrieval_tools._rag_http_client.aclose()
            retrieval_tools._rag_http_client = None
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-06: Billing Webhook Dedup -> Gateway Usage & Origin Guard
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_redis
@pytest.mark.requires_go
def test_t3_06_billing_webhook_dedup_to_gateway_usage_and_origin_guard(
    isolated_pg_schema: IsolatedPostgresSchema,
    go_bin_dir: Path,
    tmp_path: Path,
) -> None:
    billing_bin = _require_go_binary("./cmd/billing", go_bin_dir, "billing")
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")

    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    secret = "whsec_t3_06_secret"
    try:
        with (
            EphemeralGoServer(
                billing_bin,
                health_path="/healthz",
                env={
                    "DATABASE_URL": isolated_pg_schema.search_path_dsn,
                    "BILLING_PROVIDER": "stripe",
                    "STRIPE_WEBHOOK_SECRET": secret,
                    "STRIPE_PRICE_MAP": "price_pro=pro",
                },
            ) as bill,
            EphemeralGoServer(
                gateway_binary,
                env=_gateway_env(
                    isolated_pg_schema,
                    rns,
                    extra={
                        "STRIPE_API_KEY": "sk_test_123",
                        "STRIPE_PRICE_MAP": "price_pro=pro",
                        "STRIPE_RETURN_BASE_URL": "",
                        "AI_SETTINGS_PATH": str(tmp_path / "ai.json"),
                        "MEMORY_STORAGE_DIR": str(tmp_path / "mem"),
                        "LOCAL_DEV_USER_EMAIL": f"t306-{rns.prefix}@test.local",
                    },
                ),
            ) as gw_unarmed,
        ):
            with httpx.Client(timeout=10.0) as client:
                r_init = client.post(f"{gw_unarmed.base_url}/api/v1/me/init", json={"display_name": "Bill User"})
                assert r_init.status_code in (200, 201), r_init.text
                user_id = r_init.json()["id"]

                logs_unarmed = gw_unarmed.stderr + gw_unarmed.stdout
                assert "Stripe checkout creator armed" not in logs_unarmed, logs_unarmed
                assert "Stripe portal creator armed" not in logs_unarmed, logs_unarmed

                # Unset STRIPE_RETURN_BASE_URL -> 503 checkout_unavailable BEFORE tier/origin 400 checks or Stripe API calls
                for payload in (
                    {"tier": "pro", "success_url": "https://app.example.com/ok", "cancel_url": "https://app.example.com/no"},
                    {"tier": "bogus", "success_url": "https://app.example.com/ok", "cancel_url": "https://app.example.com/no"},
                    {"tier": "pro", "success_url": "https://evil.com/ok", "cancel_url": "https://app.example.com/no"},
                ):
                    r_chk = client.post(f"{gw_unarmed.base_url}/api/v1/billing/checkout", json=payload)
                    assert r_chk.status_code == 503, f"Expected 503 for {payload}, got {r_chk.status_code}: {r_chk.text}"
                    assert r_chk.json().get("code") == "checkout_unavailable", r_chk.text

                event_payload = json.dumps(
                    {
                        "id": "evt_t3_06_001",
                        "type": "checkout.session.completed",
                        "data": {"object": {"metadata": {"user_id": user_id, "price_id": "price_pro"}}},
                    }
                ).encode("utf-8")
                ts = str(int(time.time()))
                sig = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + event_payload, hashlib.sha256).hexdigest()
                headers = {"Stripe-Signature": f"t={ts},v1={sig}"}

                r_wh1 = client.post(f"{bill.base_url}/webhook", content=event_payload, headers=headers)
                assert r_wh1.status_code == 200 and r_wh1.json()["status"] == "applied", r_wh1.text

                r_wh2 = client.post(f"{bill.base_url}/webhook", content=event_payload, headers=headers)
                assert r_wh2.status_code == 200 and r_wh2.json()["status"] == "deduped", r_wh2.text

                r_usage = client.get(f"{gw_unarmed.base_url}/api/v1/usage")
                assert r_usage.status_code == 200
                assert r_usage.json()["plan_tier"] == "pro"

        # With STRIPE_RETURN_BASE_URL configured, cross-origin redirect must return 400.
        with EphemeralGoServer(
            gateway_binary,
            env=_gateway_env(
                isolated_pg_schema,
                rns,
                extra={
                    "STRIPE_API_KEY": "sk_test_123",
                    "STRIPE_PRICE_MAP": "price_pro=pro",
                    "STRIPE_RETURN_BASE_URL": "https://app.example.com",
                    "AI_SETTINGS_PATH": str(tmp_path / "ai2.json"),
                    "MEMORY_STORAGE_DIR": str(tmp_path / "mem2"),
                },
            ),
        ) as gw_armed:
            assert "Stripe checkout creator armed" in (gw_armed.stderr + gw_armed.stdout)
            with httpx.Client(base_url=gw_armed.base_url, timeout=10.0) as client:
                r_bad_origin = client.post(
                    "/api/v1/billing/checkout",
                    json={"tier": "pro", "success_url": "https://evil.com/ok", "cancel_url": "https://app.example.com/no"},
                )
                assert r_bad_origin.status_code == 400, r_bad_origin.text
                assert "success_url not allowed" in r_bad_origin.json().get("error", ""), r_bad_origin.text
    finally:
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-07: Migrations + /me/init + /me + /me/export + GDPR Delete -> 410
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_redis
@pytest.mark.requires_go
def test_t3_07_migrations_advisory_lock_plus_me_init_export_gdpr_delete(
    isolated_pg_schema: IsolatedPostgresSchema,
    go_bin_dir: Path,
    tmp_path: Path,
) -> None:
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        with EphemeralGoServer(
            gateway_binary,
            env=_gateway_env(
                isolated_pg_schema,
                rns,
                extra={
                    "AI_SETTINGS_PATH": str(tmp_path / "ai.json"),
                    "MEMORY_STORAGE_DIR": str(tmp_path / "mem"),
                    "LOCAL_DEV_USER_EMAIL": f"t307-{rns.prefix}@test.local",
                },
            ),
        ) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                bad_init = client.post(
                    "/api/v1/me/init",
                    content="{malformed-json",
                    headers={"Content-Type": "application/json"},
                )
                assert bad_init.status_code == 400, bad_init.text

                ok_init = client.post("/api/v1/me/init", json={"display_name": "Alice", "timezone": "UTC"})
                assert ok_init.status_code == 201, ok_init.text

                me_resp = client.get("/api/v1/me")
                assert me_resp.status_code == 200
                assert me_resp.json()["needs_init"] is False

                exp_resp = client.get("/api/v1/me/export")
                assert exp_resp.status_code == 200, exp_resp.text
                exp_data = exp_resp.json()
                assert "user" in exp_data and "audit" in exp_data

                del_resp = client.delete("/api/v1/me")
                assert del_resp.status_code in (200, 204), del_resp.text

                after_del = client.post("/api/v1/me/init", json={"display_name": "Alice"})
                assert after_del.status_code == 410, after_del.text
    finally:
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-08: Go AI Settings Endpoints -> Python local_ai + MockOllamaServer
# ---------------------------------------------------------------------------


@pytest.mark.requires_redis
@pytest.mark.requires_go
@pytest.mark.asyncio
async def test_t3_08_go_ai_settings_endpoints_to_python_local_ai_mock_ollama(
    go_bin_dir: Path,
    mock_ollama: MockOllamaServer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    settings_path = tmp_path / "shared_ai_settings.json"
    try:
        with EphemeralGoServer(
            gateway_binary,
            env=_gateway_env(
                None,
                rns,
                extra={
                    "AI_SETTINGS_PATH": str(settings_path),
                    "MEMORY_STORAGE_DIR": str(tmp_path / "mem"),
                },
            ),
        ) as gw:
            async with httpx.AsyncClient(base_url=gw.base_url, timeout=10.0) as client:
                put_resp = await client.put(
                    "/api/v1/settings/ai",
                    json={
                        "provider": "local_gemma",
                        "endpoint": mock_ollama.base_url,
                        "text_model": "gemma2:9b",
                        "embed_model": "embeddinggemma:latest",
                    },
                )
                assert put_resp.status_code == 200, put_resp.text

                test_resp = await client.post("/api/v1/settings/ai/test", json={})
                assert test_resp.status_code == 200, test_resp.text
                body = test_resp.json()
                assert body["reachable"] is True
                assert "gemma2:9b" in body["installed_models"]

            monkeypatch.setenv("AI_SETTINGS_PATH", str(settings_path))
            monkeypatch.setattr(local_ai, "_cached_settings", None)
            monkeypatch.setattr(local_ai, "_cached_path", None)
            monkeypatch.setattr(local_ai, "_cached_mtime", None)

            cfg = local_ai.get_ai_settings()
            assert cfg.endpoint == mock_ollama.base_url
            assert local_ai.is_local_gemma_active() is True
            assert json.loads(await local_ai.generate_local_json_async("ping")) == {"status": "ok"}
            vec = await local_ai.compute_local_embedding_async("hello world")
            assert vec is not None and len(vec) == 3072
    finally:
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-09: Planner Eval -> Consultant -> A2UI -> Translation Diagnostic Chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_09_planner_eval_consultant_a2ui_non_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    a2ui_mod = _load_module("a2ui_tools_t3_09", "agents/skills/a2ui-rendering/tools.py")
    td_mod = _load_module(
        "td_tools_t3_09",
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
    )

    settings_path = tmp_path / "ai_cloud.json"
    settings_path.write_text(json.dumps({"provider": "cloud_gemini"}), encoding="utf-8")
    monkeypatch.setenv("AI_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr(local_ai, "_cached_settings", None)
    monkeypatch.setattr(local_ai, "_cached_path", None)
    monkeypatch.setattr(local_ai, "_cached_mtime", None)

    emitted: list[dict[str, Any]] = []

    async def _capture_emit(**kwargs: Any) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(consultant_tools, "emit_gateway_message", _capture_emit)
    monkeypatch.delenv("REDIS_ADDR", raising=False)

    # Happy path with mock cloud LLM clients.
    eval_json = json.dumps(
        {
            "scores": {
                "stakeholder_alignment": 88,
                "decision_quality": 84,
                "resource_stewardship": 82,
                "crisis_readiness": 80,
                "strategic_coherence": 86,
                "ethical_alignment": 90,
            },
            "findings": [{"title": "Strong Coalition", "severity": "MINOR", "description": "Good cadence."}],
            "summary": "Balanced executive roadmap.",
        }
    )
    td_json = json.dumps(
        {
            "gaps": [{"stakeholder": "cfo", "tick": 2, "system_framing": "ROI", "lifeworld_impact": "Risk"}],
            "summary": "Translate financial jargon.",
            "actionable_corrections": ["Clarify risk budget"],
        }
    )

    class _GoodEvalClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(**kwargs: Any) -> Any:
                    return SimpleNamespace(text=eval_json, candidates=[])

    class _GoodTdClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(**kwargs: Any) -> Any:
                    return SimpleNamespace(text=td_json, candidates=[])

    monkeypatch.setattr(evaluator_tools, "_get_client", lambda: _GoodEvalClient())
    monkeypatch.setattr(
        evaluator_tools,
        "_generate_feedback",
        lambda **kwargs: __import__("asyncio").sleep(0, result=(["Engage CFO early"], "Strong alignment")),
    )
    monkeypatch.setattr(td_mod, "_get_client", lambda: _GoodTdClient())

    ctx = SimpleNamespace(
        state={
            "leadership_scenario": {"name": "Platform Shift", "stakeholder_count": 15},
            "echo_voice_log": [
                {"tick": 1, "role": "cfo", "sentiment": 0.4, "stance": "supportive"},
                {"tick": 2, "role": "cfo", "sentiment": -0.2, "stance": "resistant", "inner_thought": "Too risky"},
            ],
            "decision_log": [{"tick": 2, "decision_text": "Cut legacy support"}],
            "stakeholder_map": [{"role": "cfo", "tier": 1, "domain_principles": ["Governance"]}],
            "persona_status_counts": {"Visionary": {"aligned": 3}},
            "persona_avg_progress": {"Visionary": 75.0},
        },
        session=SimpleNamespace(id=None),
    )

    ev_res = await evaluator_tools.evaluate_leadership_plan("Phased migration plan", tool_context=ctx)
    assert ev_res["status"] == "success"
    sr_res = await consultant_tools.emit_sim_results_a2ui(ctx)
    assert sr_res.startswith("SUCCESS:")
    ap_res = await consultant_tools.emit_action_plan_a2ui(ctx, plan_markdown="# Days 1-30\n- Align stakeholders")
    assert ap_res.startswith("SUCCESS:")
    dash_res = await a2ui_mod.validate_and_emit_a2ui("dashboard", tool_context=ctx)
    assert dash_res["status"] == "success"
    diag_res = await td_mod.emit_translation_diagnostic(ctx)
    assert diag_res["status"] == "success"
    psych_res = await td_mod.emit_stakeholder_psychology(ctx)
    assert psych_res["status"] == "success"

    # Fault path: invalid A2UI JSON must error even with valid leadership_evaluation in state;
    # empty markdown and cloud timeout must error cleanly without synthetic fallbacks.
    bad_a2ui = await a2ui_mod.validate_and_emit_a2ui("{broken", tool_context=ctx)
    assert bad_a2ui["status"] == "error"
    bad_ap = await consultant_tools.emit_action_plan_a2ui(ctx, plan_markdown="   ")
    assert bad_ap.startswith("ERROR:")

    class _FailEvalClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(**kwargs: Any) -> Any:
                    raise TimeoutError("timeout")

    monkeypatch.setattr(evaluator_tools, "_get_client", lambda: _FailEvalClient())
    fail_ev = await evaluator_tools.evaluate_leadership_plan("Plan", tool_context=SimpleNamespace(state={}))
    assert fail_ev["status"] == "error" and fail_ev["eval_method"] != "dcns_heuristic"


# ---------------------------------------------------------------------------
# T3-10: Go account.KeyForUser <-> Python tenant_keys Parity (SAAS_MODE off/on)
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_redis
@pytest.mark.requires_go
def test_t3_10_go_python_tenant_key_parity_saas_on_off(
    isolated_pg_schema: IsolatedPostgresSchema,
    go_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    try:
        with _MockA2AAgentServer(["leadership_runner_autopilot"]) as mock_agent:
            # 1. SAAS_MODE=off: Go gateway writes {prefix}:session:{sid}
            with EphemeralGoServer(
                gateway_binary,
                env=_gateway_env(
                    isolated_pg_schema,
                    rns,
                    extra={
                        "SAAS_MODE": "off",
                        "ACCOUNT_KEY_PREFIX": "u",
                        "AGENT_URLS": ",".join(mock_agent.urls),
                        "LOCAL_DEV_USER_EMAIL": f"t310-off-{rns.prefix}@test.local",
                        "AI_SETTINGS_PATH": str(tmp_path / "ai_off.json"),
                        "MEMORY_STORAGE_DIR": str(tmp_path / "mem_off"),
                    },
                ),
            ) as gw_off:
                with httpx.Client(base_url=gw_off.base_url, timeout=10.0) as client:
                    r_init = client.post("/api/v1/me/init", json={"display_name": "Parity Off"})
                    assert r_init.status_code in (200, 201), r_init.text
                    uid_off = r_init.json()["id"]
                    r_sp = client.post(
                        "/api/v1/spawn",
                        json={
                            "agents": [{"agentType": "leadership_runner_autopilot", "count": 1}],
                            "simulation_id": f"{rns.prefix}-sim-off",
                        },
                    )
                    assert r_sp.status_code in (200, 201), r_sp.text
                    sid_off = r_sp.json()["sessions"][0]["sessionId"]

            # 2. SAAS_MODE=on: Go gateway writes u:{uid}:{prefix}:session:{sid}
            with EphemeralGoServer(
                gateway_binary,
                env=_gateway_env(
                    isolated_pg_schema,
                    rns,
                    extra={
                        "SAAS_MODE": "on",
                        "ACCOUNT_KEY_PREFIX": "u",
                        "AGENT_URLS": ",".join(mock_agent.urls),
                        "LOCAL_DEV_USER_EMAIL": f"t310-on-{rns.prefix}@test.local",
                        "AI_SETTINGS_PATH": str(tmp_path / "ai_on.json"),
                        "MEMORY_STORAGE_DIR": str(tmp_path / "mem_on"),
                    },
                ),
            ) as gw_on:
                with httpx.Client(base_url=gw_on.base_url, timeout=10.0) as client:
                    r_init_on = client.post("/api/v1/me/init", json={"display_name": "Parity On"})
                    assert r_init_on.status_code in (200, 201), r_init_on.text
                    uid_on = r_init_on.json()["id"]
                    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
                        conn.autocommit = True
                        with conn.cursor() as cur:
                            cur.execute("UPDATE users SET status = 'approved' WHERE id = %s", (uid_on,))
                    r_sp_on = client.post(
                        "/api/v1/spawn",
                        json={
                            "agents": [{"agentType": "leadership_runner_autopilot", "count": 1}],
                            "simulation_id": f"{rns.prefix}-sim-on",
                        },
                    )
                    assert r_sp_on.status_code in (200, 201), r_sp_on.text
                    sid_on = r_sp_on.json()["sessions"][0]["sessionId"]

        assert rns._client is not None
        monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")

        monkeypatch.setenv("SAAS_MODE", "off")
        py_key_off = tenant_keys.key_for_user(uid_off, rns.prefix, "session", sid_off)
        assert rns._client.get(py_key_off) == "leadership_runner_autopilot"
        monkeypatch.setenv("DISPATCHER_USER_ID", uid_off)
        assert broadcast._scoped_broadcast_channel("sim-9") == "simulation:sim-9:broadcast"
        assert collector_mod._gateway_broadcast_channel() == "gateway:broadcast"

        monkeypatch.setenv("SAAS_MODE", "on")
        py_key_on = tenant_keys.key_for_user(uid_on, rns.prefix, "session", sid_on)
        assert rns._client.get(py_key_on) == "leadership_runner_autopilot"
        monkeypatch.setenv("DISPATCHER_USER_ID", uid_on)
        assert broadcast._scoped_broadcast_channel("sim-9") == f"u:{uid_on}:simulation:sim-9:broadcast"
        assert collector_mod._gateway_broadcast_channel() == f"u:{uid_on}:gateway:broadcast"
    finally:
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-11: AlloyDBScenarioStore + compile_results + Longitudinal NDJSON
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_t3_11_alloydb_scenario_store_plus_compile_results_and_longitudinal(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    schema_sql = (REPO_ROOT / "scripts/postgres-init/03_leadership_schema.sql").read_text(encoding="utf-8")
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(schema_sql)

    comp_mod = _load_module("comp_tools_t3_11", "agents/simulator/skills/completing-the-race/tools.py")
    _parse_pg_dsn_env(monkeypatch, isolated_pg_schema)
    monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("REDIS_ADDR", raising=False)

    store = store_alloydb.AlloyDBScenarioStore()
    monkeypatch.setattr(memory_tools, "_store", store)
    try:
        sid = await store.store_scenario(
            scenario_data={"name": "Zero Downtime", "stakeholder_count": 5},
            evaluation_score=86.0,
            evaluation_result={"overall_score": 86, "nan_probe": float("nan")},
            persona="pragmatist",
            owner_id="user-long-1",
        )
        loaded = await store.get_scenario(sid, owner_id="user-long-1")
        assert loaded is not None
        assert loaded.evaluation_result is not None
        assert loaded.evaluation_result["nan_probe"] is None

        ctx = SimpleNamespace(
            state={
                "simulation_id": "sim-t3-11",
                "participant_id": "user-long-1",
                "quest_id": "ai_powered_disruptor",
                "runner_count": 5,
                "finished_runner_ids": ["r1", "r2"],
                "principle_scores": [4.0] * 7,
                "tick_snapshots": [{"tick": 1, "runners_reporting": 5, "status_counts": {"aligned": 2}}],
            },
            session=SimpleNamespace(id="sess-t3-11"),
        )
        comp_res = await comp_mod.compile_results(ctx)
        assert comp_res["status"] == "success"

        ndjson_path = tmp_path / "runs" / "user-long-1" / "runs.ndjson"
        assert ndjson_path.is_file() and ndjson_path.read_text(encoding="utf-8").strip()

        ctx.state["simulation_result"] = comp_res
        ctx.state["leadership_evaluation"] = loaded.evaluation_result
        monkeypatch.setattr(memory_tools, "_resolve_owner_id", lambda _tc: "user-long-1")
        rec_res = await memory_tools.record_scenario_outcome(sid, ctx)
        assert rec_res["status"] == "success"

        after = await store.get_scenario(sid, owner_id="user-long-1")
        assert after is not None and len(after.simulations) == 1
    finally:
        await store.close()
        await redis_pool.aclose()


# ---------------------------------------------------------------------------
# T3-12: Gateway /spawn + Python spawn_runners + /environment/reset
# ---------------------------------------------------------------------------


@pytest.mark.requires_redis
@pytest.mark.requires_go
@pytest.mark.asyncio
async def test_t3_12_gateway_spawn_plus_python_spawn_runners_plus_env_reset(
    go_bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway_binary = _require_go_binary("./cmd/gateway", go_bin_dir, "gateway")
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    prep_mod = _load_module("prep_tools_t3_12", "agents/simulator/skills/preparing-the-race/tools.py")
    echo_sids: list[str] = []
    try:
        with _MockA2AAgentServer(
            ["leadership_runner_autopilot", "ai_echo", "ai_echo_reactive", "ai_echo_autopilot"]
        ) as mock_agents:
            with EphemeralGoServer(
                gateway_binary,
                env=_gateway_env(
                    None,
                    rns,
                    extra={
                        "AGENT_URLS": ",".join(mock_agents.urls),
                        "MAX_RUNNERS": "50",
                        "AI_SETTINGS_PATH": str(tmp_path / "ai.json"),
                        "MEMORY_STORAGE_DIR": str(tmp_path / "mem"),
                    },
                ),
            ) as gw:
                _bind_redis(monkeypatch, rns)
                monkeypatch.setenv("SAAS_MODE", "off")
                monkeypatch.setenv("GATEWAY_INTERNAL_URL", gw.base_url)

                sim_id = f"{rns.prefix}-t312-sim"
                ctx = SimpleNamespace(
                    state={
                        "simulation_id": sim_id,
                    },
                    session=SimpleNamespace(id=f"{rns.prefix}-t312-sess"),
                )
                prep_res = await prep_mod.prepare_simulation(
                    json.dumps(
                        {
                            "action": "execute",
                            "simulation_config": {
                                "duration_seconds": 20,
                                "tick_interval_seconds": 5,
                                "runner_count": 2,
                                "runner_type": "leadership_runner_autopilot",
                            },
                        }
                    ),
                    ctx,
                )
                assert prep_res["status"] == "success"

                spawn_res = await prep_mod.spawn_runners(2, ctx)
                assert spawn_res["status"] == "success", spawn_res
                assert spawn_res["count"] == 2
                echo_sids = list(ctx.state["echo_session_ids"])
                assert len(echo_sids) >= 1
                assert rns._client is not None
                for esid in echo_sids:
                    assert rns._client.get(f"echo_config:{esid}") is not None

                async with httpx.AsyncClient(base_url=gw.base_url, timeout=10.0) as client:
                    rst = await client.post("/api/v1/environment/reset", json={"targets": ["sessions", "queues"]})
                    assert rst.status_code == 200, rst.text
                    assert rst.json()["results"]["sessions"]["flushed"] is True
                    assert rst.json()["results"]["queues"]["flushed"] is True

                for rsid in spawn_res["session_ids"]:
                    assert rns._client.get(f"{rns.prefix}:session:{rsid}") is None
                    assert rns._client.get(f"simreg:session:{rsid}") is None
    finally:
        if rns._client is not None and echo_sids:
            rns._client.delete(*(f"echo_config:{esid}" for esid in echo_sids))
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-13: simdata Redis + compile_results + consultant emit_sim_results_a2ui
# ---------------------------------------------------------------------------


@pytest.mark.requires_redis
@pytest.mark.asyncio
async def test_t3_13_simdata_redis_plus_compile_results_plus_consultant_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    try:
        rns_ctx = IsolatedRedisNamespace(require_available=True)
        rns = rns_ctx.__enter__()
    except RedisUnavailableError as exc:
        pytest.skip(str(exc))

    comp_mod = _load_module("comp_tools_t3_13", "agents/simulator/skills/completing-the-race/tools.py")
    sim_id = f"{rns.prefix}-t313-sim"
    emitted: list[dict[str, Any]] = []

    async def _capture_emit(**kwargs: Any) -> None:
        emitted.append(kwargs)

    try:
        _bind_redis(monkeypatch, rns)
        monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path / "runs"))
        monkeypatch.setattr(consultant_tools, "emit_gateway_message", _capture_emit)

        comp_ctx = SimpleNamespace(
            state={
                "simulation_id": sim_id,
                "participant_id": "u-t313",
                "quest_id": "ai_powered_disruptor",
                "runner_count": 2,
                "finished_runner_ids": ["r1"],
                "simulation_config": {"race_distance_mi": 10.0},
                "global_runner_states": {
                    "r1": {"status": "aligned", "persona": "Visionary", "distance": 10.0},
                    "r2": {"status": "neutral", "persona": "Disruptor", "distance": 0.0},
                },
                "tick_snapshots": [{"tick": 1, "runners_reporting": 2}],
            },
            session=SimpleNamespace(id="sess-t313-comp"),
        )
        comp_res = await comp_mod.compile_results(comp_ctx)
        assert comp_res["status"] == "success"
        assert comp_res["persona_avg_progress"]["Disruptor"] == 0.0
        persisted = await simdata.load_simulation_data(sim_id)
        assert persisted["persona_avg_progress"]["Disruptor"] == 0.0

        # Consultant context intentionally omits persona_avg_progress/runner_count in state
        # so it must load them from Redis simdata.
        cons_ctx = SimpleNamespace(
            state={
                "simulation_id": sim_id,
                "leadership_scenario": {"name": "Zero-Progress Probe", "stakeholder_count": 2},
                "leadership_evaluation": {
                    "status": "success",
                    "overall_score": 81,
                    "scores": {"stakeholder_alignment": 80, "decision_quality": 82, "crisis_readiness": 81},
                    "summary": "Measured zero preserved.",
                },
            },
            session=SimpleNamespace(id="sess-t313-cons"),
        )
        sr_res = await consultant_tools.emit_sim_results_a2ui(cons_ctx)
        assert sr_res.startswith("SUCCESS:")
        surface_msgs = [e["data"]["surfaceUpdate"] for e in emitted if "surfaceUpdate" in e.get("data", {})]
        assert len(surface_msgs) == 1
        by_id = {c["id"]: c["component"] for c in surface_msgs[0]["components"]}
        assert by_id["dis-bar"]["ScoreBar"]["label"]["literalString"] == "Disruptor"
        assert by_id["dis-bar"]["ScoreBar"]["value"]["literalNumber"] == 0
        assert by_id["vis-bar"]["ScoreBar"]["value"]["literalNumber"] == 100
        assert by_id["dip-val"]["Text"]["text"]["literalString"] == "—"
    finally:
        pulses.reset()
        await redis_pool.aclose()
        rns_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# T3-14: cmd/billing /healthz 503 when DB down vs 200 when Postgres up
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_t3_14_billing_healthz_503_when_db_down_vs_200_when_pg_up(
    go_bin_dir: Path,
) -> None:
    billing_bin = _require_go_binary("./cmd/billing", go_bin_dir, "billing")

    bad_port = free_port()
    with EphemeralGoServer(
        billing_bin,
        health_path="/healthz",
        ready_statuses=(200, 503),
        env={"DATABASE_URL": f"postgresql://bad:bad@127.0.0.1:{bad_port}/bad?sslmode=disable"},
    ) as bill_down:
        r_down = httpx.get(f"{bill_down.base_url}/healthz", timeout=5.0)
        assert r_down.status_code == 503, f"Expected 503 when DB down, got {r_down.status_code}: {r_down.text}"

    try:
        with IsolatedPostgresSchema(require_available=True) as pg:
            with EphemeralGoServer(
                billing_bin,
                health_path="/healthz",
                env={"DATABASE_URL": pg.search_path_dsn},
            ) as bill_up:
                r_up = httpx.get(f"{bill_up.base_url}/healthz", timeout=5.0)
                assert r_up.status_code == 200
                assert r_up.json()["status"] == "ok"
    except PostgresUnavailableError as exc:
        pytest.skip(str(exc))
