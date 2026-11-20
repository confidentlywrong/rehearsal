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

"""Tier 3 pairwise cross-layer tests exercising compiled Go binaries <-> UI client contracts."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect as ws_connect

from agents.utils.metering import MeteringEvent, SqliteSink
from gen_proto.gateway import gateway_pb2
from scripts.e2e_hardening.harness import (
    EphemeralGoServer,
    IsolatedPostgresSchema,
    IsolatedRedisNamespace,
    MockOllamaServer,
    free_port,
)

pytestmark = pytest.mark.tier3


def _gateway_env(
    redis_ns: IsolatedRedisNamespace,
    tmp_path: Path,
    pg: IsolatedPostgresSchema | None = None,
    **extra: str,
) -> dict[str, str]:
    env: dict[str, str] = {
        "REDIS_ADDR": redis_ns.addr,
        "REDIS_PASSWORD": redis_ns.password,
        "REDIS_PREFIX": redis_ns.prefix,
        "SAAS_MODE": "on",
        "AI_SETTINGS_PATH": str(tmp_path / "ai_settings.json"),
        "LOCAL_DEV_USER_EMAIL": f"t3-{uuid.uuid4().hex[:10]}@local.test",
    }
    if pg is not None:
        env["DATABASE_URL"] = pg.search_path_dsn
    env.update(extra)
    return env


def _recv_json_frame(ws: Any, timeout: float = 3.0) -> dict[str, Any]:
    deadline_msg = f"Expected JSON error frame from WebSocket within {timeout}s"
    try:
        for _ in range(5):
            frame = ws.recv(timeout=timeout)
            if isinstance(frame, str):
                return json.loads(frame)
    except TimeoutError as exc:
        pytest.fail(f"{deadline_msg}: {exc}")
    pytest.fail(f"{deadline_msg}: only binary frames received")


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


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_plans_schema_parity_gateway_and_frontend_billing(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        with EphemeralGoServer(gateway_binary, env=_gateway_env(redis_ns, tmp_path, isolated_pg_schema)) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=5.0) as client:
                resp = client.get("/api/v1/plans")
            assert resp.status_code == 200
            plans = resp.json()["plans"]
            tiers = {p["tier"] for p in plans}
            assert {"free", "pro", "team", "enterprise", "comp"}.issubset(tiers)
            for p in plans:
                assert set(p.keys()) == {
                    "tier",
                    "monthly_token_cap",
                    "max_concurrent_runs",
                    "description",
                }
                assert "max_users" not in p

    billing_svc = (repo_root / "web/frontend/src/app/usage/billing.service.ts").read_text(encoding="utf-8")
    iface_match = re.search(r"export\s+interface\s+BillingPlan\s*\{([^}]*)\}", billing_svc)
    assert iface_match is not None
    assert "max_users" not in iface_match.group(1)
    billing_page = (repo_root / "web/frontend/src/app/settings/billing/billing.page.ts").read_text(
        encoding="utf-8"
    )
    assert "p.max_users" not in billing_page


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_checkout_and_portal_payload_parity_gateway_and_frontend(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        env = _gateway_env(redis_ns, tmp_path, isolated_pg_schema, BILLING_PROVIDER="stub")
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=5.0) as client:
                init_resp = client.post("/api/v1/me/init", json={})
                assert init_resp.status_code in (200, 201)

                bad_checkout = client.post("/api/v1/billing/checkout", json={"plan_tier": "pro"})
                assert bad_checkout.status_code in (400, 503)

                stub_checkout = client.post(
                    "/api/v1/billing/checkout",
                    json={
                        "tier": "pro",
                        "success_url": "http://127.0.0.1:9118/billing?checkout=success",
                        "cancel_url": "http://127.0.0.1:9118/billing?checkout=cancel",
                    },
                )
                assert stub_checkout.status_code == 503
                assert "url" not in stub_checkout.json()
                assert stub_checkout.json().get("code") in {"checkout_unavailable", "billing_unavailable"} or stub_checkout.json().get("error")

                stub_portal = client.post(
                    "/api/v1/billing/portal",
                    json={"return_url": "http://127.0.0.1:9118/billing"},
                )
                assert stub_portal.status_code == 503
                assert "url" not in stub_portal.json()

    billing_svc = (repo_root / "web/frontend/src/app/usage/billing.service.ts").read_text(encoding="utf-8")
    assert "success_url" in billing_svc and "cancel_url" in billing_svc, (
        "BillingService.createCheckoutSession must send tier, success_url, and cancel_url matching Go CheckoutHandler"
    )
    assert re.search(r"\btier\s*:", billing_svc), (
        "BillingService.createCheckoutSession must send canonical 'tier' field (not 'plan_tier' alone)"
    )

    portal_svc = (repo_root / "web/frontend/src/app/settings/billing/portal.service.ts").read_text(
        encoding="utf-8"
    )
    assert "return_url" in portal_svc, "PortalService.openPortal must send return_url matching Go PortalHandler"


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_memory_config_and_upload_kind_parity_gateway_and_documents_panel(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    mock_ollama: MockOllamaServer,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    storage_dir = tmp_path / "mem_store"
    storage_dir.mkdir()
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        env = _gateway_env(redis_ns, tmp_path, isolated_pg_schema, MEMORY_STORAGE_DIR=str(storage_dir))
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
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

                assert client.post("/api/v1/me/init", json={}).status_code in (200, 201)

                cfg_resp = client.get("/api/v1/memory/config")
                assert cfg_resp.status_code == 200
                cfg = cfg_resp.json()
                assert cfg["text_readable_kinds"] == ["memo", "note", "plan", "postmortem", "transcript"]
                assert "text" not in cfg["text_readable_kinds"]
                assert cfg["max_upload_bytes"] == 33554432

                ana_resp = client.post(
                    "/api/v1/memory/analyses",
                    json={"title": "Q4 Leadership Audit", "org_name": "Acme", "description": "Test"},
                )
                assert ana_resp.status_code == 201
                analysis_id = ana_resp.json()["id"]

                up_ok = client.post(
                    f"/api/v1/memory/analyses/{analysis_id}/documents",
                    data={"kind": "memo"},
                    files={"file": ("memo.txt", b"Executive decision memo content.", "text/plain")},
                )
                assert up_ok.status_code == 201, f"Valid kind=memo upload failed: {up_ok.status_code} {up_ok.text}"
                doc = up_ok.json().get("document", {})
                assert {"id", "analysis_id", "kind", "filename", "sha256", "storage_uri", "byte_size", "ingested_at"}.issubset(
                    doc.keys()
                )

                up_missing_kind = client.post(
                    f"/api/v1/memory/analyses/{analysis_id}/documents",
                    data={"kind": ""},
                    files={"file": ("note.txt", b"Missing kind probe.", "text/plain")},
                )
                assert up_missing_kind.status_code == 400, (
                    f"Gateway must reject empty document kind='' with 400 missing_kind, got {up_missing_kind.status_code}: {up_missing_kind.text}"
                )
                assert up_missing_kind.json().get("error") == "missing_kind"

    docs_panel = (
        repo_root / "web/frontend/src/app/memory/components/documents-panel.component.ts"
    ).read_text(encoding="utf-8")
    assert not re.search(r"\[\s*['\"]memo['\"]\s*,\s*['\"]text['\"]\s*\]", docs_panel)


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_memory_oversize_and_cross_tenant_error_parity(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    storage_dir = tmp_path / "mem_store_bva"
    storage_dir.mkdir()
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        alice_env = _gateway_env(
            redis_ns,
            tmp_path,
            isolated_pg_schema,
            MEMORY_STORAGE_DIR=str(storage_dir),
            MEMORY_MAX_UPLOAD_MB="1",
            LOCAL_DEV_USER_EMAIL=f"t3-alice-{uuid.uuid4().hex[:8]}@local.test",
        )
        bob_env = _gateway_env(
            redis_ns,
            tmp_path,
            isolated_pg_schema,
            MEMORY_STORAGE_DIR=str(storage_dir),
            MEMORY_MAX_UPLOAD_MB="1",
            LOCAL_DEV_USER_EMAIL=f"t3-bob-{uuid.uuid4().hex[:8]}@local.test",
        )
        with (
            EphemeralGoServer(gateway_binary, env=alice_env) as gw_alice,
            EphemeralGoServer(gateway_binary, env=bob_env) as gw_bob,
        ):
            with (
                httpx.Client(base_url=gw_alice.base_url, timeout=10.0) as alice,
                httpx.Client(base_url=gw_bob.base_url, timeout=10.0) as bob,
            ):
                assert alice.post("/api/v1/me/init", json={}).status_code in (200, 201)
                assert bob.post("/api/v1/me/init", json={}).status_code in (200, 201)

                ana_resp = alice.post(
                    "/api/v1/memory/analyses",
                    json={"title": "Alice Confidential", "org_name": "Alpha"},
                )
                assert ana_resp.status_code == 201
                alice_analysis_id = ana_resp.json()["id"]

                oversize_bytes = b"A" * (1500 * 1024)
                over_resp = alice.post(
                    f"/api/v1/memory/analyses/{alice_analysis_id}/documents",
                    data={"kind": "memo"},
                    files={"file": ("big.txt", oversize_bytes, "text/plain")},
                )
                assert over_resp.status_code == 413, (
                    f"Oversize upload must return 413 request_body_too_large, got {over_resp.status_code}: {over_resp.text}"
                )
                assert over_resp.json().get("error") == "request_body_too_large"

                missing_resp = alice.get("/api/v1/memory/analyses/00000000-0000-0000-0000-000000000099")
                assert missing_resp.status_code == 404
                assert missing_resp.json().get("error") == "analysis_not_found"

                cross_resp = bob.get(f"/api/v1/memory/analyses/{alice_analysis_id}")
                assert cross_resp.status_code == 404
                assert cross_resp.json().get("error") == "analysis_not_found"

    mem_svc = (repo_root / "web/frontend/src/app/memory/memory.service.ts").read_text(encoding="utf-8")
    assert "class MemoryApiError" in mem_svc
    assert "body.error" in mem_svc or "body?.error" in mem_svc
    assert "xhr.status >= 200 && xhr.status < 300" in mem_svc
    assert "encodeURIComponent(analysisId)" in mem_svc

    docs_panel = (
        repo_root / "web/frontend/src/app/memory/components/documents-panel.component.ts"
    ).read_text(encoding="utf-8")
    assert "file.size > this.maxUploadBytes()" in docs_panel
    assert "hasFileError.set(true)" in docs_panel
    assert "instanceof MemoryApiError" in docs_panel
    assert "uploadError.set(" in docs_panel


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_t3_admin_bff_environment_reset_and_gateway_roundtrip(
    gateway_binary: Path,
    admin_binary: Path,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        with EphemeralGoServer(gateway_binary, env=_gateway_env(redis_ns, tmp_path)) as gw:
            admin_env = {
                "GATEWAY_INTERNAL_URL": gw.base_url,
                "GATEWAY_URL": gw.base_url,
                "REDIS_ADDR": redis_ns.addr,
                "REDIS_PASSWORD": redis_ns.password,
            }
            with EphemeralGoServer(admin_binary, env=admin_env) as admin:
                with (
                    httpx.Client(base_url=gw.base_url, timeout=5.0) as gw_client,
                    httpx.Client(base_url=admin.base_url, timeout=5.0) as admin_client,
                ):
                    sess_resp = gw_client.post("/api/v1/sessions", json={"agentType": "planner"})
                    assert sess_resp.status_code == 201

                    unpriv = admin_client.post(
                        "/api/admin/environment/reset",
                        json={"targets": ["sessions", "queues", "maps"]},
                    )
                    assert unpriv.status_code == 403, (
                        f"Expected 403 superadmin_required without superadmin header, got {unpriv.status_code}: {unpriv.text}"
                    )

                    priv = admin_client.post(
                        "/api/admin/environment/reset",
                        headers={"X-Local-Dev-Superadmin": "1"},
                        json={"targets": ["sessions", "queues", "maps"]},
                    )
                    assert priv.status_code == 200, (
                        f"Admin BFF reset failed: {priv.status_code} {priv.text}"
                    )
                    body = priv.json()
                    assert body.get("status") == "reset"
                    assert body.get("results", {}).get("sessions", {}).get("flushed") is True
                    assert body["results"]["sessions"].get("count", 0) >= 1

                    after_sessions = gw_client.get("/api/v1/sessions")
                    assert after_sessions.status_code == 200
                    assert not after_sessions.json()

    main_js = (repo_root / "web/admin-dash/main.js").read_text(encoding="utf-8")
    assert "8101" not in main_js
    assert "/api/admin/environment/reset" in main_js
    api_js = (repo_root / "web/admin-dash/api.js").read_text(encoding="utf-8")
    assert "LOCAL_DEV" in api_js or "hostname" in api_js, (
        "web/admin-dash/api.js must gate X-Local-Dev-Superadmin header on local dev environment"
    )


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_admin_services_health_and_users_schema_parity(
    gateway_binary: Path,
    admin_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        with EphemeralGoServer(gateway_binary, env=_gateway_env(redis_ns, tmp_path, isolated_pg_schema)) as gw:
            admin_port = free_port()
            admin_env = {
                "ADMIN_URL": f"http://127.0.0.1:{admin_port}",
                "GATEWAY_INTERNAL_URL": gw.base_url,
                "GATEWAY_URL": gw.base_url,
                "REDIS_ADDR": redis_ns.addr,
                "REDIS_PASSWORD": redis_ns.password,
                "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            }
            with EphemeralGoServer(admin_binary, port=admin_port, env=admin_env) as admin:
                with (
                    httpx.Client(base_url=gw.base_url, timeout=5.0) as gw_client,
                    httpx.Client(base_url=admin.base_url, timeout=5.0) as admin_client,
                ):
                    init_resp = gw_client.post("/api/v1/me/init", json={})
                    assert init_resp.status_code in (200, 201)
                    uid = init_resp.json()["id"]

                    svcs_resp = admin_client.get("/api/v1/services")
                    assert svcs_resp.status_code == 200
                    svcs = svcs_resp.json()
                    assert isinstance(svcs.get("categories"), list)
                    gw_entry = next(s for s in svcs["services"] if s.get("id") == "gateway")
                    assert gw_entry["url"] == gw.base_url

                    hs_resp = admin_client.get("/api/v1/health/services")
                    assert hs_resp.status_code == 200
                    hs = hs_resp.json()
                    assert hs.get("admin") == "online"
                    assert hs.get("gateway") == "online"

                    hi_resp = admin_client.get("/api/v1/health/infra")
                    assert hi_resp.status_code == 200
                    hi = hi_resp.json()
                    assert hi.get("redis") == "online"
                    assert hi.get("alloydb") == "online"

                    users_resp = admin_client.get("/admin/users", headers={"X-Local-Dev-Superadmin": "1"})
                    assert users_resp.status_code == 200
                    users = users_resp.json().get("users", [])
                    row = next(u for u in users if u.get("id") == uid)
                    for key in ("id", "email", "status", "plan_tier", "is_superadmin", "created_at"):
                        assert key in row

    admin_go = (repo_root / "cmd/admin/main.go").read_text(encoding="utf-8")
    assert "8101" not in admin_go, "cmd/admin/main.go still defaults GATEWAY_URL to stale port 8101"

    admin_main_js = (repo_root / "web/admin-dash/main.js").read_text(encoding="utf-8")
    for token in (
        "/api/v1/services",
        "/api/v1/health/services",
        "/api/v1/health/infra",
        "categories",
        "services",
        '=== "online"',
    ):
        assert token in admin_main_js, f"web/admin-dash/main.js missing expected contract token {token!r}"

    admin_users_js = (repo_root / "web/admin-dash/users.js").read_text(encoding="utf-8")
    for field in ("u.id", "u.email", "u.status", "u.plan_tier", "u.is_superadmin", "u.created_at"):
        assert field in admin_users_js, f"web/admin-dash/users.js missing expected user schema field {field!r}"
    for tier in ('"free"', '"pro"', '"enterprise"', '"comp"'):
        assert tier in admin_users_js, f"web/admin-dash/users.js PLAN_TIERS missing {tier}"


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_t3_tester_proxy_config_and_sessions_contract_parity(
    gateway_binary: Path,
    tester_binary: Path,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        with EphemeralGoServer(gateway_binary, env=_gateway_env(redis_ns, tmp_path, SAAS_MODE="off")) as gw:
            tester_env = {
                "GATEWAY_INTERNAL_URL": gw.base_url,
                "GATEWAY_URL": gw.base_url,
            }
            with EphemeralGoServer(tester_binary, env=tester_env) as tester:
                with httpx.Client(base_url=tester.base_url, timeout=5.0) as client:
                    cfg_resp = client.get("/config.js")
                    assert cfg_resp.status_code == 200
                    assert 'window.ENV = {"VITE_GATEWAY_URL":"/ws"}' in cfg_resp.text

                    create_resp = client.post(
                        "/api/v1/sessions",
                        json={"agentType": "planner", "simulation_id": f"sim-t3-{uuid.uuid4().hex[:8]}"},
                    )
                    assert create_resp.status_code == 201
                    sid = create_resp.json()["sessionId"]

                    list_resp = client.get("/api/v1/sessions")
                    assert list_resp.status_code == 200
                    listed = list_resp.json()
                    assert isinstance(listed, list) and sid in listed

                    flush_resp = client.post("/api/v1/sessions/flush")
                    assert flush_resp.status_code == 200
                    assert flush_resp.json().get("status") == "flushed"
                    assert flush_resp.json().get("count", 0) >= 1

            with EphemeralGoServer(
                tester_binary,
                env={**tester_env, "TESTER_REQUIRE_AUTH": "true"},
            ) as auth_tester:
                with httpx.Client(base_url=auth_tester.base_url, timeout=5.0) as auth_client:
                    unauth = auth_client.get("/api/v1/sessions")
                    assert unauth.status_code == 401

    tester_go = (repo_root / "cmd/tester/main.go").read_text(encoding="utf-8")
    assert "8101" not in tester_go, "cmd/tester/main.go still defaults GATEWAY_URL to 8101"
    vite_cfg = (repo_root / "web/tester/vite.config.ts").read_text(encoding="utf-8")
    assert "8101" not in vite_cfg and "/ws" in vite_cfg

    tester_main = (repo_root / "web/tester/src/main.ts").read_text(encoding="utf-8")
    assert re.search(r'typeof\s+s\s*===\s*["\']string["\']', tester_main), (
        "web/tester/src/main.ts must handle string[] returned by Gateway GET /api/v1/sessions"
    )


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_tester_ws_proxy_and_gateway_hub_ownership_relay(
    gateway_binary: Path,
    tester_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    sim_owned = f"sim-owned-{uuid.uuid4().hex[:8]}"
    sim_foreign = f"sim-foreign-{uuid.uuid4().hex[:8]}"
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        gw_env = _gateway_env(
            redis_ns,
            tmp_path,
            isolated_pg_schema,
            LOCAL_DEV_USER_EMAIL=f"t3-alice-{uuid.uuid4().hex[:8]}@local.test",
        )
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw:
            with EphemeralGoServer(
                tester_binary,
                env={"GATEWAY_INTERNAL_URL": gw.base_url, "GATEWAY_URL": gw.base_url},
            ) as tester:
                with httpx.Client(base_url=tester.base_url, timeout=5.0) as client:
                    assert client.post("/api/v1/me/init", json={}).status_code in (200, 201)
                    s_resp = client.post(
                        "/api/v1/sessions",
                        json={"agentType": "planner", "simulation_id": sim_owned},
                    )
                    assert s_resp.status_code == 201
                    sid = s_resp.json()["sessionId"]

                with pytest.raises(InvalidStatus) as bad_exc:
                    with ws_connect(f"{tester.ws_url}/ws?sessionId=bad$id", open_timeout=3.0):
                        pass
                assert bad_exc.value.response.status_code == 400

                with ws_connect(f"{tester.ws_url}/ws?sessionId={sid}", open_timeout=5.0, close_timeout=2.0) as ws:
                    ws.send("{malformed-json")
                    err1 = _recv_json_frame(ws, timeout=3.0)
                    assert err1.get("status") == "error"
                    assert err1.get("data", {}).get("error") == "invalid_json"

                    ws.send(json.dumps({"type": "subscribe_simulation", "simulation_id": sim_foreign}))
                    err2 = _recv_json_frame(ws, timeout=3.0)
                    assert err2.get("status") == "error"
                    assert err2.get("data", {}).get("error") == "simulation_not_owned", (
                        f"Expected simulation_not_owned error frame, got {err2}"
                    )

                    breq = gateway_pb2.BroadcastRequest(
                        payload=b'{"ping":"ok"}',
                        target_session_ids=[sid],
                    )
                    wrapper = gateway_pb2.Wrapper()
                    wrapper.type = "broadcast"
                    wrapper.session_id = sid
                    wrapper.simulation_id = sim_owned
                    wrapper.payload = breq.SerializeToString()
                    ws.send(wrapper.SerializeToString())
                    _assert_no_ws_error_frame(ws, timeout=0.5)

    tester_main_ts = (repo_root / "web/tester/src/main.ts").read_text(encoding="utf-8")
    assert 'root.lookupType("gateway.Wrapper")' in tester_main_ts
    assert 'root.lookupType("gateway.BroadcastRequest")' in tester_main_ts
    assert "BroadcastRequest.encode(" in tester_main_ts
    assert 'type: "broadcast"' in tester_main_ts
    assert 'ws.binaryType = "arraybuffer"' in tester_main_ts
    assert "VITE_GATEWAY_URL" in tester_main_ts

    tester_url_ts = (repo_root / "web/tester/src/url.ts").read_text(encoding="utf-8")
    assert "export function wsToHttp" in tester_url_ts
    assert "/ws" in tester_url_ts


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_t3_ai_settings_get_put_test_pull_with_mock_ollama(
    gateway_binary: Path,
    mock_ollama: MockOllamaServer,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    settings_path = tmp_path / "ai_settings.json"
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        env = _gateway_env(redis_ns, tmp_path, AI_SETTINGS_PATH=str(settings_path))
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                superadmin_headers = {"X-Local-Dev-Superadmin": "1"}
                non_admin_get = client.get("/api/v1/settings/ai")
                assert non_admin_get.status_code == 200
                assert non_admin_get.json().get("can_configure") is False, (
                    f"Non-superadmin GET /api/v1/settings/ai in SAAS_MODE=on must report can_configure=False, "
                    f"got {non_admin_get.json().get('can_configure')!r}"
                )

                put_payload = {
                    "provider": "local_gemma",
                    "endpoint": mock_ollama.base_url,
                    "text_model": "gemma2:9b",
                    "embed_model": "embeddinggemma:latest",
                }
                for method, path, body in (
                    ("PUT", "/api/v1/settings/ai", put_payload),
                    ("POST", "/api/v1/settings/ai/test", {"endpoint": mock_ollama.base_url, "auto_start": False}),
                    ("POST", "/api/v1/settings/ai/start", {"endpoint": mock_ollama.base_url}),
                    ("POST", "/api/v1/settings/ai/pull", {"model": "gemma3:4b", "endpoint": mock_ollama.base_url}),
                ):
                    denied = client.request(method, path, json=body)
                    assert denied.status_code == 403, (
                        f"Non-superadmin {method} {path} in SAAS_MODE=on must return 403, "
                        f"got {denied.status_code}: {denied.text}"
                    )
                    assert denied.json().get("error") == "superadmin_required", (
                        f"Expected superadmin_required on {method} {path}, got {denied.json()}"
                    )
                assert not settings_path.exists()

                put_resp = client.put(
                    "/api/v1/settings/ai",
                    json=put_payload,
                    headers=superadmin_headers,
                )
                assert put_resp.status_code == 200
                put_body = put_resp.json()
                assert put_body.get("can_configure") is True
                assert put_body["endpoint"] == mock_ollama.base_url
                assert put_body["connected"] is True
                assert "gemma2:9b" in put_body.get("installed_models", [])

                admin_get_resp = client.get("/api/v1/settings/ai", headers=superadmin_headers)
                assert admin_get_resp.status_code == 200
                assert admin_get_resp.json().get("can_configure") is True

                get_resp = client.get("/api/v1/settings/ai")
                assert get_resp.status_code == 200
                assert get_resp.json()["endpoint"] == mock_ollama.base_url
                assert get_resp.json().get("can_configure") is False
                assert settings_path.is_file()

                test_resp = client.post(
                    "/api/v1/settings/ai/test",
                    json={"endpoint": mock_ollama.base_url, "auto_start": False},
                    headers=superadmin_headers,
                )
                assert test_resp.status_code == 200
                assert test_resp.json()["reachable"] is True
                assert test_resp.json()["endpoint"] == mock_ollama.base_url
                assert test_resp.json().get("status", {}).get("can_configure") is True

                bad_test_resp = client.post(
                    "/api/v1/settings/ai/test",
                    content="{not-json",
                    headers={"Content-Type": "application/json", **superadmin_headers},
                )
                assert bad_test_resp.status_code == 400, (
                    f"Malformed JSON on POST /api/v1/settings/ai/test must return 400, got {bad_test_resp.status_code}"
                )

                pull_resp = client.post(
                    "/api/v1/settings/ai/pull",
                    json={"model": "gemma3:4b", "endpoint": mock_ollama.base_url},
                    headers=superadmin_headers,
                )
                assert pull_resp.status_code == 200
                assert pull_resp.json().get("pull_progress", {}).get("done") is True
                assert pull_resp.json().get("status", {}).get("can_configure") is True

    ai_service = (repo_root / "web/frontend/src/app/settings/ai-engine/ai-engine.service.ts").read_text(
        encoding="utf-8"
    )
    status_iface = re.search(r"export\s+interface\s+AiEngineStatus\s*\{([^}]*)\}", ai_service)
    assert status_iface is not None, "AiEngineStatus interface not found in ai-engine.service.ts"
    assert "can_configure" in status_iface.group(1), (
        "AiEngineStatus interface in ai-engine.service.ts must declare can_configure"
    )
    assert "Only workspace administrators can modify AI Engine settings." in ai_service

    ai_page = (repo_root / "web/frontend/src/app/settings/ai-engine/ai-engine.page.ts").read_text(
        encoding="utf-8"
    )
    assert "toastMessage" not in ai_page
    assert "endpoint = 'http://localhost:11434'" not in ai_page
    assert "canConfigure" in ai_page
    assert "ai-readonly-notice" in ai_page


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_frontend_bff_configjs_proxy_and_local_login_redirect_guard(
    gateway_binary: Path,
    frontend_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        with EphemeralGoServer(gateway_binary, env=_gateway_env(redis_ns, tmp_path, isolated_pg_schema)) as gw:
            fe_env = {"GATEWAY_INTERNAL_URL": gw.base_url, "GATEWAY_URL": gw.base_url}
            with EphemeralGoServer(frontend_binary, env=fe_env) as fe:
                with httpx.Client(base_url=fe.base_url, timeout=5.0, follow_redirects=False) as client:
                    cfg = client.get("/config.js")
                    assert cfg.status_code == 200
                    assert '"NG_APP_GATEWAY_URL":"/ws"' in cfg.text
                    assert "SAAS_MODE" not in cfg.text

                    evil_redir = client.get("/login?redirect=//evil.com")
                    assert evil_redir.status_code == 302
                    assert evil_redir.headers.get("location") == "/"

                    safe_redir = client.get("/login?redirect=/settings/billing")
                    assert safe_redir.status_code == 302
                    assert safe_redir.headers.get("location") == "/settings/billing"

                    plans_proxy = client.get("/api/v1/plans", headers={"Accept": "application/json"})
                    assert plans_proxy.status_code == 200
                    assert len(plans_proxy.json().get("plans", [])) >= 5

    proxy_conf = json.loads((repo_root / "web/frontend/proxy.conf.json").read_text(encoding="utf-8"))
    for path_key in ("/api", "/ws", "/config.js"):
        assert "9118" in proxy_conf[path_key]["target"]

    fe_go = (repo_root / "cmd/frontend/main.go").read_text(encoding="utf-8")
    assert "8101" not in fe_go, "cmd/frontend/main.go still defaults GATEWAY_URL to 8101"


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_user_init_profile_and_gdpr_export_cross_layer(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        with EphemeralGoServer(gateway_binary, env=_gateway_env(redis_ns, tmp_path, isolated_pg_schema)) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=5.0) as client:
                pre_me = client.get("/api/v1/me")
                assert pre_me.status_code == 200
                assert pre_me.json().get("needs_init") is True

                bad_init = client.post(
                    "/api/v1/me/init",
                    content="{bad",
                    headers={"Content-Type": "application/json"},
                )
                assert bad_init.status_code == 400, (
                    f"Malformed JSON on POST /api/v1/me/init must return 400, got {bad_init.status_code}"
                )

                ok_init = client.post("/api/v1/me/init", json={})
                assert ok_init.status_code == 201
                assert ok_init.json().get("created") is True

                post_me = client.get("/api/v1/me")
                assert post_me.status_code == 200
                me_body = post_me.json()
                assert me_body.get("needs_init") is False
                assert {"id", "email", "plan_tier", "is_superadmin", "status"}.issubset(
                    me_body["user"].keys()
                )

                prof_ok = client.patch(
                    "/api/v1/me/profile",
                    json={"display_name": "Exec User", "timezone": "Europe/Berlin"},
                )
                assert prof_ok.status_code == 200
                assert prof_ok.json()["display_name"] == "Exec User"
                assert prof_ok.json()["timezone"] == "Europe/Berlin"

                prof_bad_tz = client.patch(
                    "/api/v1/me/profile",
                    json={"display_name": "Exec User", "timezone": "Mars/Olympus"},
                )
                assert prof_bad_tz.status_code == 400
                assert prof_bad_tz.json().get("error") == "timezone_invalid"

                exp_resp = client.get("/api/v1/me/export")
                assert exp_resp.status_code == 200
                assert "attachment" in exp_resp.headers.get("content-disposition", "")
                exp_json = exp_resp.json()
                assert {"user", "audit", "exported_at"}.issubset(exp_json.keys())

                del_resp = client.delete("/api/v1/me")
                assert del_resp.status_code == 204

                reinit_resp = client.post("/api/v1/me/init", json={})
                assert reinit_resp.status_code == 410
                assert reinit_resp.json().get("error") == "account_deleted"

    identity_svc = (repo_root / "web/frontend/src/app/auth/identity.service.ts").read_text(encoding="utf-8")
    for field in ("id:", "email:", "display_name:", "timezone:", "plan_tier:", "is_superadmin:", "status?:"):
        assert field in identity_svc, f"IdentityService UserDTO missing field {field}"
    assert "needs_init" in identity_svc
    assert "/api/v1/me/init" in identity_svc

    user_menu = (repo_root / "web/frontend/src/app/auth/user-menu.component.ts").read_text(encoding="utf-8")
    assert "this.identity.ensureInitialized()" in user_menu

    profile_svc = (repo_root / "web/frontend/src/app/settings/profile/profile.service.ts").read_text(
        encoding="utf-8"
    )
    assert "/api/v1/me/profile" in profile_svc
    assert "method: 'PATCH'" in profile_svc
    assert "method: 'DELETE'" in profile_svc

    auth_svc = (repo_root / "web/frontend/src/app/auth/auth.service.ts").read_text(encoding="utf-8")
    assert "res.status === 401 || res.status === 410" in auth_svc


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_t3_usage_sqlite_drain_and_frontend_usage_tile_contract(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    sqlite_path = tmp_path / "metering.db"
    sink = SqliteSink(path=sqlite_path)
    try:
        with IsolatedRedisNamespace(require_available=True) as redis_ns:
            env = _gateway_env(
                redis_ns,
                tmp_path,
                isolated_pg_schema,
                METERING_SINK="sqlite",
                METERING_SQLITE_PATH=str(sqlite_path),
            )
            with EphemeralGoServer(gateway_binary, env=env) as gw:
                with httpx.Client(base_url=gw.base_url, timeout=5.0) as client:
                    init_resp = client.post("/api/v1/me/init", json={})
                    assert init_resp.status_code in (200, 201)
                    uid = init_resp.json()["id"]

                    sink.emit(
                        MeteringEvent(
                            user_id=uid,
                            sim_id="sim-t3-usage",
                            agent_type="leadership_planner_with_eval",
                            model="gemini-3.5-flash-lite",
                            prompt_tokens=100000,
                            completion_tokens=50000,
                            total_tokens=150000,
                            timestamp_iso=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                            cost_micros=500,
                        )
                    )

                    usage_resp = client.get("/api/v1/usage")
                    assert usage_resp.status_code == 200
                    usage = usage_resp.json()
                    expected_keys = {
                        "user_id",
                        "year_month",
                        "plan_tier",
                        "tokens_used",
                        "tokens_cap",
                        "percent_used",
                        "exhausted",
                        "warn_high",
                        "max_concurrent_runs",
                    }
                    assert expected_keys.issubset(usage.keys())
                    assert usage["user_id"] == uid
                    assert usage["plan_tier"] == "free"
                    assert usage["tokens_used"] == 150000
                    assert usage["tokens_cap"] == 300000
                    assert usage["percent_used"] == pytest.approx(50.0)
                    assert usage["exhausted"] is False
                    assert usage["warn_high"] is False
                    assert usage["max_concurrent_runs"] == 1
    finally:
        sink.close()

    with contextlib.closing(sqlite3.connect(sqlite_path)) as conn:
        drained_rows = conn.execute("SELECT drained FROM metering_ledger").fetchall()
    assert drained_rows == [(1,)], f"Expected SQLite metering row drained=1, got {drained_rows}"

    usage_svc = (repo_root / "web/frontend/src/app/usage/usage.service.ts").read_text(encoding="utf-8")
    assert "export interface UsageResponse" in usage_svc
    for field in (
        "user_id",
        "year_month",
        "plan_tier",
        "tokens_used",
        "tokens_cap",
        "percent_used",
        "exhausted",
        "warn_high",
        "max_concurrent_runs",
    ):
        assert field in usage_svc
