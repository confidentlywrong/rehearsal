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

"""Tier 4 multi-step end-to-end application workflow tests (Scenarios 4-10)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import psycopg2
import pytest
from websockets.sync.client import connect as ws_connect

from agents.leadership_planner_with_eval.evaluator import tools as eval_tools
from agents.leadership_planner_with_eval.skills.consultant.tools import (
    emit_action_plan_a2ui,
    emit_sim_results_a2ui,
)
from agents.leadership_planner_with_memory.memory import store_alloydb
from agents.utils.metering import MeteringEvent, SqliteSink
from gen_proto.gateway import gateway_pb2
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    EphemeralGoServer,
    IsolatedPostgresSchema,
    IsolatedRedisNamespace,
    MockOllamaServer,
    free_port,
)

pytestmark = pytest.mark.tier4


def _isolated_ai_settings(tmp_path: Path, name: str = "ai_settings.json") -> str:
    cfg = tmp_path / name
    cfg.write_text(
        json.dumps({"provider": "cloud_gemini", "endpoint": f"http://127.0.0.1:{free_port()}"}),
        encoding="utf-8",
    )
    return str(cfg)


class _MockAgentCardServer:
    """Thread-safe ephemeral HTTP server serving an A2A AgentCard on a free port."""

    def __init__(self, agent_name: str = "planner") -> None:
        self.agent_name = agent_name
        self.port: int = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _MockAgentCardServer:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if urllib.parse.urlsplit(self.path).path == "/.well-known/agent-card.json":
                    body = json.dumps(
                        {
                            "name": parent.agent_name,
                            "description": "Ephemeral mock planner agent",
                            "version": "1.0.0",
                            "url": parent.base_url,
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                raw_len = int(self.headers.get("Content-Length") or "0")
                if raw_len > 0:
                    _ = self.rfile.read(raw_len)
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class _Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        httpd = _Server(("127.0.0.1", 0), _Handler)
        self.port = int(httpd.server_address[1])
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _load_hyphenated_module(rel_path: str, mod_name: str) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(mod_name, REPO_ROOT / rel_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recv_json_frame(ws: Any, timeout: float = 3.0) -> dict[str, Any]:
    try:
        for _ in range(5):
            frame = ws.recv(timeout=timeout)
            if isinstance(frame, str):
                return json.loads(frame)
    except TimeoutError as exc:
        pytest.fail(f"Expected JSON error frame from WebSocket within {timeout}s: {exc}")
    pytest.fail("Expected JSON error frame from WebSocket, only binary frames received")


def _assert_no_ws_error_frame(ws: Any, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            frame = ws.recv(timeout=remaining)
        except TimeoutError:
            return
        if isinstance(frame, str):
            payload = json.loads(frame)
            assert payload.get("status") != "error" and payload.get("event") != "bad_request", (
                f"Unexpected WebSocket error frame after BroadcastRequest: {payload}"
            )
        elif isinstance(frame, bytes):
            echoed = gateway_pb2.Wrapper()
            echoed.ParseFromString(frame)
            assert echoed.status != "error", (
                f"Unexpected WebSocket binary error wrapper after BroadcastRequest: {echoed}"
            )


def _stripe_sig_header(secret: str, raw_body: bytes, ts: int | None = None) -> str:
    unix_ts = ts if ts is not None else int(time.time())
    signed_payload = f"{unix_ts}.".encode("utf-8") + raw_body
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={unix_ts},v1={sig}"


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t4_workflow_scenario4_user_lifecycle_and_rag_memory(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    mock_ollama: MockOllamaServer,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "rag_storage"
    storage_dir.mkdir()
    settings_path = tmp_path / "ai_settings.json"
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "SAAS_MODE": "on",
            "MEMORY_STORAGE_DIR": str(storage_dir),
            "AI_SETTINGS_PATH": str(settings_path),
            "LOCAL_DEV_USER_EMAIL": f"t4-s4-{uuid.uuid4().hex[:8]}@local.test",
        }
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=15.0) as client:
                ai_payload = {
                    "provider": "local_gemma",
                    "endpoint": mock_ollama.base_url,
                    "text_model": "gemma2:9b",
                    "embed_model": "embeddinggemma:latest",
                }
                non_admin_ai = client.put("/api/v1/settings/ai", json=ai_payload)
                assert non_admin_ai.status_code == 403, (
                    f"Non-superadmin PUT /api/v1/settings/ai in SAAS_MODE=on must return 403, "
                    f"got {non_admin_ai.status_code}: {non_admin_ai.text}"
                )
                assert non_admin_ai.json().get("error") == "superadmin_required"

                ai_resp = client.put(
                    "/api/v1/settings/ai",
                    json=ai_payload,
                    headers={"X-Local-Dev-Superadmin": "1"},
                )
                assert ai_resp.status_code == 200
                assert ai_resp.json()["connected"] is True

                bad_init = client.post(
                    "/api/v1/me/init",
                    content="{bad-json",
                    headers={"Content-Type": "application/json"},
                )
                assert bad_init.status_code == 400

                init_resp = client.post("/api/v1/me/init", json={})
                assert init_resp.status_code == 201
                assert init_resp.json()["created"] is True

                ana_resp = client.post(
                    "/api/v1/memory/analyses",
                    json={"title": "RAG Lifecycle Scenario", "org_name": "Acme Corp", "description": "E2E"},
                )
                assert ana_resp.status_code == 201
                analysis_id = ana_resp.json()["id"]

                doc_text = (
                    b"Executive Rehearsal Postmortem: Stakeholder alignment improved by 35 percent "
                    b"after deploying structured weekly governance checkpoints."
                )
                up_resp = client.post(
                    f"/api/v1/memory/analyses/{analysis_id}/documents",
                    data={"kind": "memo"},
                    files={"file": ("governance_memo.txt", doc_text, "text/plain")},
                )
                assert up_resp.status_code == 201
                doc_id = up_resp.json()["document"]["id"]

                chunk_resp = client.post(f"/api/v1/memory/analyses/{analysis_id}/documents/{doc_id}/chunk")
                assert chunk_resp.status_code == 200
                assert chunk_resp.json().get("chunks_stored", 0) >= 1

                ret_resp = client.post(
                    f"/api/v1/memory/analyses/{analysis_id}/retrieve-passages",
                    json={"query": "governance checkpoints alignment", "limit": 5},
                )
                assert ret_resp.status_code == 200
                passages = ret_resp.json().get("passages", [])
                assert len(passages) >= 1
                assert any(p.get("document_id") == doc_id for p in passages)

                exp_resp = client.get("/api/v1/me/export")
                assert exp_resp.status_code == 200
                exp_json = exp_resp.json()
                assert {"user", "audit", "exported_at"}.issubset(exp_json.keys())

                del_resp = client.delete("/api/v1/me")
                assert del_resp.status_code == 204

                reinit_resp = client.post("/api/v1/me/init", json={})
                assert reinit_resp.status_code == 410
                assert reinit_resp.json().get("error") == "account_deleted"

                post_del_me = client.get("/api/v1/me")
                assert post_del_me.status_code == 410 or (
                    post_del_me.status_code == 200 and post_del_me.json().get("needs_init") is True
                )


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t4_workflow_scenario5_simulation_ws_ownership_and_metering_rollup(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "metering_s5.db"
    sink = SqliteSink(path=sqlite_path)
    try:
        with (
            IsolatedRedisNamespace(require_available=True) as redis_ns,
            _MockAgentCardServer("planner") as card_srv,
        ):
            common_env = {
                "REDIS_ADDR": redis_ns.addr,
                "REDIS_PASSWORD": redis_ns.password,
                "REDIS_PREFIX": redis_ns.prefix,
                "DATABASE_URL": isolated_pg_schema.search_path_dsn,
                "SAAS_MODE": "on",
                "AGENT_URLS": card_srv.base_url,
                "METERING_SINK": "sqlite",
                "METERING_SQLITE_PATH": str(sqlite_path),
            }
            with (
                EphemeralGoServer(
                    gateway_binary,
                    env={
                        **common_env,
                        "AI_SETTINGS_PATH": _isolated_ai_settings(tmp_path, "ai_settings_s5_alice.json"),
                        "LOCAL_DEV_USER_EMAIL": f"alice-{uuid.uuid4().hex[:8]}@local.test",
                    },
                ) as gw_alice,
                EphemeralGoServer(
                    gateway_binary,
                    env={
                        **common_env,
                        "AI_SETTINGS_PATH": _isolated_ai_settings(tmp_path, "ai_settings_s5_bob.json"),
                        "LOCAL_DEV_USER_EMAIL": f"bob-{uuid.uuid4().hex[:8]}@local.test",
                    },
                ) as gw_bob,
            ):
                with (
                    httpx.Client(base_url=gw_alice.base_url, timeout=10.0) as alice,
                    httpx.Client(base_url=gw_bob.base_url, timeout=10.0) as bob,
                ):
                    alice_init = alice.post("/api/v1/me/init", json={})
                    assert alice_init.status_code in (200, 201)
                    alice_uid = alice_init.json()["id"]

                    bob_init = bob.post("/api/v1/me/init", json={})
                    assert bob_init.status_code in (200, 201)

                    sim_id = f"sim-e2e-{uuid.uuid4().hex[:8]}"
                    bob_sim_id = f"sim-bob-{uuid.uuid4().hex[:8]}"

                    spawn_resp = alice.post(
                        "/api/v1/spawn",
                        json={"agents": [{"agentType": "planner", "count": 1}], "simulation_id": sim_id},
                    )
                    assert spawn_resp.status_code in (200, 201), (
                        f"POST /api/v1/spawn failed: {spawn_resp.status_code} {spawn_resp.text}"
                    )
                    spawn_sessions = spawn_resp.json().get("sessions", [])
                    assert len(spawn_sessions) >= 1
                    alice_sid = spawn_sessions[0]["sessionId"]

                    bob_sess = bob.post(
                        "/api/v1/sessions",
                        json={"agentType": "planner", "simulation_id": bob_sim_id},
                    )
                    assert bob_sess.status_code == 201
                    bob_sid = bob_sess.json()["sessionId"]

                    with ws_connect(
                        f"{gw_bob.ws_url}/ws?sessionId={bob_sid}",
                        open_timeout=5.0,
                        close_timeout=2.0,
                    ) as bob_ws:
                        bob_ws.send(json.dumps({"type": "subscribe_simulation", "simulation_id": sim_id}))
                        err_frame = _recv_json_frame(bob_ws, timeout=3.0)
                        assert err_frame.get("status") == "error"
                        assert err_frame.get("data", {}).get("error") == "simulation_not_owned"

                    with ws_connect(
                        f"{gw_alice.ws_url}/ws?sessionId={alice_sid}",
                        open_timeout=5.0,
                        close_timeout=2.0,
                    ) as alice_ws:
                        alice_ws.send(json.dumps({"type": "subscribe_simulation", "simulation_id": sim_id}))
                        breq = gateway_pb2.BroadcastRequest(
                            payload=b'{"event":"tick"}',
                            target_session_ids=[alice_sid],
                        )
                        wrapper = gateway_pb2.Wrapper()
                        wrapper.type = "broadcast"
                        wrapper.session_id = alice_sid
                        wrapper.simulation_id = sim_id
                        wrapper.payload = breq.SerializeToString()
                        alice_ws.send(wrapper.SerializeToString())
                        _assert_no_ws_error_frame(alice_ws, timeout=0.5)

                    sink.emit(
                        MeteringEvent(
                            user_id=alice_uid,
                            sim_id=sim_id,
                            agent_type="planner",
                            model="gemini-3.5-flash-lite",
                            prompt_tokens=80000,
                            completion_tokens=40000,
                            total_tokens=120000,
                            timestamp_iso=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                            cost_micros=400,
                        )
                    )

                    usage_resp = alice.get("/api/v1/usage")
                    assert usage_resp.status_code == 200
                    assert usage_resp.json()["tokens_used"] >= 120000

                    plans_resp = alice.get("/api/v1/plans")
                    assert plans_resp.status_code == 200
                    for plan in plans_resp.json().get("plans", []):
                        assert "max_users" not in plan
    finally:
        sink.close()


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t4_workflow_scenario6_postgres_migrations_seed_and_alloydb_store(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "AI_SETTINGS_PATH": _isolated_ai_settings(tmp_path, "ai_settings_s6.json"),
        }
        with EphemeralGoServer(gateway_binary, env=env):
            pass
        with EphemeralGoServer(gateway_binary, env=env):
            pass

    with contextlib.closing(psycopg2.connect(isolated_pg_schema.search_path_dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = [row[0] for row in cur.fetchall()]
            assert len(versions) == 7, f"Expected 7 schema_migrations entries, got {versions}"

            for legacy in ("analysis_nodes", "analysis_edges", "analysis_proposals"):
                cur.execute(
                    "SELECT to_regclass(%s)",
                    (f"{isolated_pg_schema.schema_name}.{legacy}",),
                )
                assert cur.fetchone()[0] is None, f"Legacy table {legacy} should be dropped by 0007"

            for sql_rel in (
                "scripts/postgres-init/03_leadership_schema.sql",
                "scripts/postgres-init/04_leadership_demo_fixtures.sql",
                "agents/leadership_planner_with_memory/alloydb/seed_local.sql",
                "scripts/postgres-init/04_leadership_demo_fixtures.sql",
                "agents/leadership_planner_with_memory/alloydb/seed_local.sql",
            ):
                cur.execute((REPO_ROOT / sql_rel).read_text(encoding="utf-8"))

            cur.execute(
                "SELECT scenario_id, owner_id FROM leadership_scenarios "
                "WHERE scenario_id LIKE 'seed-leadership-%' ORDER BY scenario_id"
            )
            seed_rows = cur.fetchall()
            assert len(seed_rows) == 4
            assert all(owner == "__seed__" for _, owner in seed_rows), f"Unexpected seed owners: {seed_rows}"

    parsed = urllib.parse.urlsplit(isolated_pg_schema.dsn)
    monkeypatch.setenv("ALLOYDB_HOST", parsed.hostname or "127.0.0.1")
    monkeypatch.setenv("ALLOYDB_PORT", str(parsed.port or 5432))
    monkeypatch.setenv("ALLOYDB_USER", urllib.parse.unquote(parsed.username or "postgres"))
    monkeypatch.setenv("ALLOYDB_PASSWORD", urllib.parse.unquote(parsed.password or ""))
    monkeypatch.setenv("ALLOYDB_DATABASE", parsed.path.lstrip("/") or "postgres")
    monkeypatch.setenv("ALLOYDB_SCHEMA", isolated_pg_schema.schema_name)

    async def _run_store_reads() -> None:
        with store_alloydb._state_lock:
            store_alloydb._pools.clear()
            store_alloydb._pool_locks.clear()
            store_alloydb._pool = None
            store_alloydb._pool_lock = None
            store_alloydb._pool_loop = None
        try:
            store = store_alloydb.AlloyDBScenarioStore()
            s1 = await store.get_scenario("seed-leadership-001")
            assert s1 is not None
            assert s1.scenario_id == "seed-leadership-001"
            assert s1.owner_id == "__seed__"
            recalled = await store.recall_scenarios(count=10)
            assert len(recalled) >= 4
        finally:
            pool_to_close = store_alloydb._pool
            with store_alloydb._state_lock:
                store_alloydb._pools.clear()
                store_alloydb._pool_locks.clear()
                store_alloydb._pool = None
                store_alloydb._pool_lock = None
                store_alloydb._pool_loop = None
            if pool_to_close is not None:
                await pool_to_close.close()

    asyncio.run(_run_store_reads())


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t4_workflow_scenario7_admin_control_plane_and_environment_reset_bff(
    gateway_binary: Path,
    admin_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        gw_env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "SAAS_MODE": "on",
            "AI_SETTINGS_PATH": _isolated_ai_settings(tmp_path, "ai_settings_s7.json"),
            "LOCAL_DEV_USER_EMAIL": f"t4-s7-{uuid.uuid4().hex[:8]}@local.test",
        }
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw:
            admin_env = {
                "GATEWAY_INTERNAL_URL": gw.base_url,
                "GATEWAY_URL": gw.base_url,
                "REDIS_ADDR": redis_ns.addr,
                "REDIS_PASSWORD": redis_ns.password,
                "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            }
            with EphemeralGoServer(admin_binary, env=admin_env) as admin:
                with (
                    httpx.Client(base_url=gw.base_url, timeout=5.0) as gw_client,
                    httpx.Client(base_url=admin.base_url, timeout=5.0) as admin_client,
                ):
                    init_resp = gw_client.post("/api/v1/me/init", json={})
                    assert init_resp.status_code in (200, 201)
                    uid = init_resp.json()["id"]

                    sess_resp = gw_client.post("/api/v1/sessions", json={"agentType": "planner"})
                    assert sess_resp.status_code == 201

                    assert admin_client.post(
                        "/api/admin/environment/reset",
                        json={"targets": ["sessions"]},
                    ).status_code == 403
                    assert admin_client.get(f"/admin/users/{uid}/usage").status_code == 403

                    super_headers = {"X-Local-Dev-Superadmin": "1"}
                    hs = admin_client.get("/api/v1/health/services")
                    assert hs.status_code == 200
                    assert hs.json().get("gateway") == "online"

                    reset_resp = admin_client.post(
                        "/api/admin/environment/reset",
                        headers=super_headers,
                        json={"targets": ["sessions", "queues", "maps"]},
                    )
                    assert reset_resp.status_code == 200
                    assert reset_resp.json().get("status") == "reset"
                    assert reset_resp.json()["results"]["sessions"]["flushed"] is True

                    usage_resp = admin_client.get(f"/admin/users/{uid}/usage", headers=super_headers)
                    assert usage_resp.status_code == 200

                    exp_resp = admin_client.get(f"/admin/users/{uid}/export", headers=super_headers)
                    assert exp_resp.status_code == 200
                    assert "attachment" in exp_resp.headers.get("content-disposition", "")
                    assert {"user", "audit"}.issubset(exp_resp.json().keys())


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_t4_workflow_scenario8_local_ai_settings_and_fail_loud_python_eval(
    gateway_binary: Path,
    mock_ollama: MockOllamaServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = tmp_path / "ai_settings_s8.json"
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "AI_SETTINGS_PATH": str(settings_path),
        }
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                put_resp = client.put(
                    "/api/v1/settings/ai",
                    json={
                        "provider": "local_gemma",
                        "endpoint": mock_ollama.base_url,
                        "text_model": "gemma2:9b",
                        "embed_model": "embeddinggemma:latest",
                    },
                )
                assert put_resp.status_code == 200
                get_resp = client.get("/api/v1/settings/ai")
                assert get_resp.status_code == 200
                assert get_resp.json()["endpoint"] == mock_ollama.base_url

                test_ok = client.post(
                    "/api/v1/settings/ai/test",
                    json={"endpoint": mock_ollama.base_url, "auto_start": False},
                )
                assert test_ok.status_code == 200
                assert test_ok.json()["reachable"] is True

                bad_test = client.post(
                    "/api/v1/settings/ai/test",
                    content="{malformed",
                    headers={"Content-Type": "application/json"},
                )
                assert bad_test.status_code == 400

    a2ui_mod = _load_hyphenated_module("agents/skills/a2ui-rendering/tools.py", "e2e_a2ui_rendering_tools")
    diag_mod = _load_hyphenated_module(
        "agents/leadership_planner_with_eval/skills/translation-diagnostic/tools.py",
        "e2e_translation_diagnostic_tools",
    )

    class _Ctx:
        def __init__(self, state: dict[str, Any] | None = None) -> None:
            self.state: dict[str, Any] = state if state is not None else {}

    class _BadStateCtx:
        @property
        def state(self) -> Any:
            raise AttributeError("simulated broken ToolContext.state")

    async def _run_python_checks() -> None:
        ctx_with_eval = _Ctx(
            {
                "leadership_evaluation": {
                    "status": "success",
                    "overall_score": 82,
                    "scores": {
                        "stakeholder_alignment": 80,
                        "decision_quality": 85,
                        "resource_stewardship": 80,
                        "crisis_readiness": 82,
                        "strategic_coherence": 83,
                        "ethical_alignment": 81,
                    },
                }
            }
        )
        a2ui_res = await a2ui_mod.validate_and_emit_a2ui("{bad-json", ctx_with_eval)
        assert a2ui_res.get("status") == "error"

        diag_res = await diag_mod.emit_translation_diagnostic(_Ctx({}))
        assert diag_res.get("status") == "error"

        psych_res = await diag_mod.emit_stakeholder_psychology(_BadStateCtx())
        assert psych_res.get("status") == "error"

        action_res = await emit_action_plan_a2ui(_Ctx({}), "")
        assert isinstance(action_res, str) and action_res.startswith("ERROR:")

        sim_res = await emit_sim_results_a2ui(_Ctx({}))
        assert isinstance(sim_res, str) and sim_res.startswith("ERROR:")

    asyncio.run(_run_python_checks())

    with MockOllamaServer(fail_chat=True) as failing_ollama:
        monkeypatch.setenv("AI_SETTINGS_PATH", str(settings_path))
        monkeypatch.setenv("LOCAL_AI_ENDPOINT", failing_ollama.base_url)
        monkeypatch.setattr(eval_tools, "is_local_gemma_active", lambda: True)

        class _FailEvalCtx:
            def __init__(self) -> None:
                self.state: dict[str, Any] = {}

        eval_res = asyncio.run(eval_tools.evaluate_leadership_plan("Evaluate this plan", _FailEvalCtx()))
        assert eval_res.get("status") == "error", (
            f"evaluate_leadership_plan must fail explicitly rather than return heuristic scores: {eval_res}"
        )


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t4_workflow_scenario9_stripe_webhook_and_checkout_origin_guard(
    gateway_binary: Path,
    billing_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    webhook_secret = "whsec_test_secret"
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        gw_guarded_env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "SAAS_MODE": "on",
            "BILLING_PROVIDER": "stripe",
            "STRIPE_API_KEY": "sk_test_dummy",
            "STRIPE_PRICE_MAP": "price_pro=pro,price_team=team",
            "STRIPE_RETURN_BASE_URL": "http://127.0.0.1:9118",
            "AI_SETTINGS_PATH": _isolated_ai_settings(tmp_path, "ai_settings_s9.json"),
            "LOCAL_DEV_USER_EMAIL": f"t4-s9-{uuid.uuid4().hex[:8]}@local.test",
        }
        with EphemeralGoServer(gateway_binary, env=gw_guarded_env) as gw_guarded:
            with httpx.Client(base_url=gw_guarded.base_url, timeout=5.0) as client:
                init_resp = client.post("/api/v1/me/init", json={})
                assert init_resp.status_code in (200, 201)
                uid = init_resp.json()["id"]

                evil_checkout = client.post(
                    "/api/v1/billing/checkout",
                    json={
                        "tier": "pro",
                        "success_url": "https://evil.example/steal",
                        "cancel_url": "http://127.0.0.1:9118/billing?checkout=cancel",
                    },
                )
                assert evil_checkout.status_code == 400

        gw_unconfigured_env = {
            **gw_guarded_env,
            "STRIPE_RETURN_BASE_URL": "",
        }
        with EphemeralGoServer(gateway_binary, env=gw_unconfigured_env) as gw_unconfigured:
            with httpx.Client(base_url=gw_unconfigured.base_url, timeout=5.0) as client:
                no_base = client.post(
                    "/api/v1/billing/checkout",
                    json={
                        "tier": "pro",
                        "success_url": "http://127.0.0.1:9118/billing?checkout=success",
                        "cancel_url": "http://127.0.0.1:9118/billing?checkout=cancel",
                    },
                )
                assert no_base.status_code == 503, (
                    f"Stripe checkout without STRIPE_RETURN_BASE_URL must fail closed with 503, got {no_base.status_code}"
                )

    billing_env = {
        "DATABASE_URL": isolated_pg_schema.search_path_dsn,
        "BILLING_PROVIDER": "stripe",
        "STRIPE_WEBHOOK_SECRET": webhook_secret,
        "STRIPE_PRICE_MAP": "price_pro=pro,price_team=team",
    }
    with EphemeralGoServer(billing_binary, env=billing_env) as billing:
        event_body = json.dumps(
            {
                "id": "evt_t4_001",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "customer": "cus_t4_001",
                        "metadata": {"user_id": uid, "price_id": "price_pro"},
                        "line_items": {"data": [{"price": {"id": "price_pro"}}]},
                    }
                },
            }
        ).encode("utf-8")
        valid_sig = _stripe_sig_header(webhook_secret, event_body)
        bad_sig = f"t={int(time.time())},v1=deadbeef"

        with httpx.Client(base_url=billing.base_url, timeout=5.0) as client:
            r_bad = client.post("/webhook", content=event_body, headers={"Stripe-Signature": bad_sig})
            assert r_bad.status_code == 400

            r_ok = client.post("/webhook", content=event_body, headers={"Stripe-Signature": valid_sig})
            assert r_ok.status_code == 200
            assert r_ok.json().get("status") == "applied"

            r_replay = client.post("/webhook", content=event_body, headers={"Stripe-Signature": valid_sig})
            assert r_replay.status_code == 200
            assert r_replay.json().get("status") == "deduped"

    with EphemeralGoServer(
        billing_binary,
        env={"BILLING_PROVIDER": "stub"},
        ready_statuses=(200, 503),
    ) as billing_no_db:
        with httpx.Client(base_url=billing_no_db.base_url, timeout=5.0) as client:
            hz = client.get("/healthz")
            assert hz.status_code == 503, (
                f"cmd/billing /healthz without DB must return 503, got {hz.status_code}: {hz.text}"
            )


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_t4_workflow_scenario10_tester_proxy_and_session_log_management(
    gateway_binary: Path,
    tester_binary: Path,
    tmp_path: Path,
) -> None:
    tester_go = (REPO_ROOT / "cmd/tester/main.go").read_text(encoding="utf-8")
    assert "8101" not in tester_go, "cmd/tester/main.go still defaults GATEWAY_URL to 8101"
    vite_cfg = (REPO_ROOT / "web/tester/vite.config.ts").read_text(encoding="utf-8")
    assert "8101" not in vite_cfg and "/ws" in vite_cfg

    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        gw_env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "SAAS_MODE": "off",
            "AI_SETTINGS_PATH": _isolated_ai_settings(tmp_path, "ai_settings_s10.json"),
            "LOCAL_DEV_USER_EMAIL": f"t4-s10-{uuid.uuid4().hex[:8]}@local.test",
        }
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw:
            tester_env = {"GATEWAY_INTERNAL_URL": gw.base_url, "GATEWAY_URL": gw.base_url}
            with EphemeralGoServer(tester_binary, env=tester_env) as tester:
                with httpx.Client(base_url=tester.base_url, timeout=5.0) as client:
                    cfg = client.get("/config.js")
                    assert cfg.status_code == 200
                    assert "VITE_GATEWAY_URL" in cfg.text

                    initial = client.get("/api/v1/sessions")
                    assert initial.status_code == 200

                    created = client.post(
                        "/api/v1/sessions",
                        json={"agentType": "planner", "simulation_id": f"sim-t4-tester-{uuid.uuid4().hex[:8]}"},
                    )
                    assert created.status_code == 201
                    sid = created.json()["sessionId"]

                    with ws_connect(
                        f"{tester.ws_url}/ws?sessionId={sid}",
                        open_timeout=5.0,
                        close_timeout=2.0,
                    ) as ws:
                        ws.send("{bad-json")
                        err = _recv_json_frame(ws, timeout=3.0)
                        assert err.get("status") == "error"

                        breq = gateway_pb2.BroadcastRequest(
                            payload=b'{"ping":true}',
                            target_session_ids=[sid],
                        )
                        wrapper = gateway_pb2.Wrapper()
                        wrapper.type = "broadcast"
                        wrapper.session_id = sid
                        wrapper.payload = breq.SerializeToString()
                        ws.send(wrapper.SerializeToString())
                        _assert_no_ws_error_frame(ws, timeout=0.5)

                    reset_resp = client.post(
                        "/api/v1/environment/reset",
                        json={"targets": ["sessions", "queues", "maps"]},
                    )
                    assert reset_resp.status_code == 200
                    assert reset_resp.json().get("status") == "reset"
