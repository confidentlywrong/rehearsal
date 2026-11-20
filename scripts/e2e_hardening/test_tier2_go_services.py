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

"""Tier 2 boundary-value and negative error-path tests for Go Services (Features #6-#10)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psycopg2
import pytest
import redis
from websockets.sync.client import connect as ws_connect

from agents.utils.metering import MeteringEvent, SqliteSink
from gen_proto.gateway import gateway_pb2
from scripts.e2e_hardening.harness import (
    _DOTENV_BLOCKED_KEYS,
    _INHERITED_ENV_KEYS,
    REPO_ROOT,
    EphemeralGoServer,
    IsolatedPostgresSchema,
    IsolatedRedisNamespace,
    RedisUnavailableError,
    free_port,
    run_cmd,
)

pytestmark = pytest.mark.tier2


def _local_dev_uid(email: str = "dev@local.test") -> str:
    norm = email.strip().lower() or "dev@local.test"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    return f"local-dev-{digest}"


@contextmanager
def _isolated_redis() -> Iterator[IsolatedRedisNamespace]:
    try:
        with IsolatedRedisNamespace(require_available=True) as rns:
            yield rns
    except RedisUnavailableError as exc:
        pytest.skip(f"Redis unreachable for live Go service test: {exc}")


def _build_billing_binary(go_bin_dir: Path) -> Path:
    out_path = go_bin_dir / "billing"
    if not out_path.exists():
        proc = run_cmd(["go", "build", "-o", str(out_path), "./cmd/billing"], cwd=REPO_ROOT, timeout=300)
        assert proc.returncode == 0, f"cmd/billing failed to build:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return out_path


def _setup_temp_go_workspace(tmp_path: Path, pkg_name: str = "e2eharness") -> Path:
    ws = tmp_path / "go_ws"
    ws.mkdir(parents=True, exist_ok=True)
    for name in ("go.mod", "go.sum", "internal", "cmd", "gen_proto", "migrations"):
        link = ws / name
        if not link.exists():
            link.symlink_to(REPO_ROOT / name)
    pkg_dir = ws / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    return ws


def _symlink_non_test_go_files(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for go_file in src_dir.glob("*.go"):
        if go_file.name.endswith("_test.go"):
            continue
        link = dst_dir / go_file.name
        if not link.exists():
            link.symlink_to(go_file)


def _run_temp_go_test(ws: Path, pkg_name: str, test_name: str, env: dict[str, str] | None = None) -> None:
    merged_env = {**os.environ, **env} if env is not None else None
    proc = run_cmd(
        ["go", "test", "-count=1", "-v", "-run", f"^{test_name}$", f"./{pkg_name}"],
        cwd=ws,
        env=merged_env,
        timeout=180,
    )
    assert proc.returncode == 0, f"Go harness {test_name} failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def _gateway_env(
    rns: IsolatedRedisNamespace,
    tmp_path: Path,
    pg: IsolatedPostgresSchema | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "REDIS_ADDR": rns.addr,
        "REDIS_PASSWORD": rns.password,
        "REDIS_PREFIX": rns.prefix,
        "LOCAL_DEV_USER_EMAIL": f"t2-{uuid.uuid4().hex[:10]}@local.test",
        "AI_SETTINGS_PATH": str(tmp_path / "ai_settings.json"),
        "MEMORY_STORAGE_DIR": str(tmp_path / "mem_storage"),
        "METERING_SQLITE_PATH": str(tmp_path / "metering.db"),
    }
    if pg is not None:
        env["DATABASE_URL"] = pg.search_path_dsn
    if extra:
        env.update(extra)
    return env


def _init_local_user(base_url: str) -> httpx.Response:
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        return client.post("/api/v1/me/init", json={"display_name": "Tier2 Tester", "timezone": "UTC"})


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _recv_json_error_frame(ws, *, timeout: float = 4.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        msg = ws.recv(timeout=remaining)
        if isinstance(msg, str):
            data = json.loads(msg)
            if data.get("status") == "error" or data.get("type") == "error":
                return data
    raise TimeoutError("Did not receive JSON error frame from WebSocket")


# =============================================================================
# Feature #6: SQLite billing drainer negative/boundary cases
# =============================================================================


@pytest.mark.requires_go
def test_f6_t2_sqlite_drainer_rollup_failure_keeps_drained_zero(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "rollup_fail.db"
    sink = SqliteSink(sqlite_path)
    sink.emit(
        MeteringEvent(
            user_id="u-fail",
            sim_id="sim-1",
            agent_type="planner",
            model="gemma2:9b",
            prompt_tokens=60,
            completion_tokens=51,
            total_tokens=111,
            cost_micros=10,
            timestamp_iso=datetime.now(UTC).isoformat(),
        )
    )

    ws = _setup_temp_go_workspace(tmp_path)
    go_code = f'''package e2eharness

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
)

type rollupFailForwarder struct{{}}

func (rollupFailForwarder) InsertEvent(_ context.Context, _ billing.Event) (int64, error) {{
	return 1, nil
}}

func (rollupFailForwarder) UpsertMonthlyRollup(_ context.Context, _ string, _ time.Time, _, _ int64) error {{
	return errors.New("rollup failure")
}}

func TestRollupFailureKeepsDrainedZero(t *testing.T) {{
	d := billing.NewSQLiteDrainer({json.dumps(str(sqlite_path))}, rollupFailForwarder{{}})
	n, err := d.DrainOnce(context.Background())
	if err == nil {{
		t.Fatalf("expected non-nil error on rollup failure")
	}}
	if n != 0 {{
		t.Fatalf("expected 0 forwarded rows, got %d", n)
	}}
}}
'''
    (ws / "e2eharness" / "f6_rollup_fail_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestRollupFailureKeepsDrainedZero")

    with sqlite3.connect(sqlite_path) as sconn:
        assert sconn.execute("SELECT drained FROM metering_ledger").fetchone()[0] == 0


@pytest.mark.requires_go
def test_f6_t2_sqlite_drainer_transient_pg_insert_error_keeps_drained_zero(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "insert_fail.db"
    sink = SqliteSink(sqlite_path)
    sink.emit(
        MeteringEvent(
            user_id="u-trans",
            sim_id="sim-2",
            agent_type="runner",
            model="gemma2:9b",
            prompt_tokens=25,
            completion_tokens=25,
            total_tokens=50,
            timestamp_iso=datetime.now(UTC).isoformat(),
        )
    )

    ws = _setup_temp_go_workspace(tmp_path)
    go_code = f'''package e2eharness

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
)

type transientInsertForwarder struct{{}}

func (transientInsertForwarder) InsertEvent(_ context.Context, _ billing.Event) (int64, error) {{
	return 0, errors.New("connection reset by peer")
}}

func (transientInsertForwarder) UpsertMonthlyRollup(_ context.Context, _ string, _ time.Time, _, _ int64) error {{
	return nil
}}

func TestTransientInsertErrorKeepsDrainedZero(t *testing.T) {{
	d := billing.NewSQLiteDrainer({json.dumps(str(sqlite_path))}, transientInsertForwarder{{}})
	n, err := d.DrainOnce(context.Background())
	if err == nil {{
		t.Fatalf("expected transient error to propagate")
	}}
	if n != 0 {{
		t.Fatalf("expected 0 forwarded rows, got %d", n)
	}}
}}
'''
    (ws / "e2eharness" / "f6_trans_fail_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestTransientInsertErrorKeepsDrainedZero")

    with sqlite3.connect(sqlite_path) as sconn:
        assert sconn.execute("SELECT drained FROM metering_ledger").fetchone()[0] == 0


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f6_t2_sqlite_drainer_permanent_23503_fk_drops_poison_row(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "poison_fk.db"
    email = f"f6-poison-{uuid.uuid4().hex[:8]}@local.test"
    uid = _local_dev_uid(email)
    sink = SqliteSink(sqlite_path)
    now_iso = datetime.now(UTC).isoformat()

    sink.emit(
        MeteringEvent(
            user_id="nonexistent-poison-user",
            sim_id="sim-poison",
            agent_type="planner",
            model="gemma2:9b",
            prompt_tokens=500,
            completion_tokens=499,
            total_tokens=999,
            timestamp_iso=now_iso,
        )
    )
    sink.emit(
        MeteringEvent(
            user_id=uid,
            sim_id="sim-valid",
            agent_type="runner",
            model="gemma2:9b",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            timestamp_iso=now_iso,
        )
    )

    with _isolated_redis() as rns:
        env = _gateway_env(
            rns,
            tmp_path,
            isolated_pg_schema,
            {
                "LOCAL_DEV_USER_EMAIL": email,
                "METERING_SINK": "sqlite",
                "METERING_SQLITE_PATH": str(sqlite_path),
            },
        )
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                resp = client.get("/api/v1/usage")
                assert resp.status_code == 200, resp.text
                assert resp.json()["tokens_used"] == 150

    with sqlite3.connect(sqlite_path) as sconn:
        drained_vals = [row[0] for row in sconn.execute("SELECT drained FROM metering_ledger ORDER BY id")]
        assert drained_vals == [1, 1]

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as pconn, pconn.cursor() as cur:
        cur.execute("SELECT user_id, total_tokens FROM usage_ledger")
        rows = cur.fetchall()
        assert rows == [(uid, 150)]


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f6_t2_sqlite_drainer_missing_file_and_missing_table_noop(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    missing_db = tmp_path / "does_not_exist.db"
    with _isolated_redis() as rns:
        env = _gateway_env(
            rns,
            tmp_path,
            isolated_pg_schema,
            {
                "METERING_SINK": "sqlite",
                "METERING_SQLITE_PATH": str(missing_db),
            },
        )
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                resp = client.get("/api/v1/usage")
                assert resp.status_code == 200, resp.text
                assert resp.json()["tokens_used"] == 0

    empty_db = tmp_path / "empty_no_table.db"
    sqlite3.connect(empty_db).close()

    ws = _setup_temp_go_workspace(tmp_path)
    go_code = f'''package e2eharness

import (
	"context"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
)

type noopForwarder struct{{}}

func (noopForwarder) InsertEvent(_ context.Context, _ billing.Event) (int64, error) {{ return 0, nil }}
func (noopForwarder) UpsertMonthlyRollup(_ context.Context, _ string, _ time.Time, _, _ int64) error {{ return nil }}

func TestMissingTableNoop(t *testing.T) {{
	d := billing.NewSQLiteDrainer({json.dumps(str(empty_db))}, noopForwarder{{}})
	n, err := d.DrainOnce(context.Background())
	if err != nil {{
		t.Fatalf("expected nil error on missing table, got %v", err)
	}}
	if n != 0 {{
		t.Fatalf("expected 0 rows, got %d", n)
	}}
}}
'''
    (ws / "e2eharness" / "f6_missing_table_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestMissingTableNoop")


@pytest.mark.requires_go
def test_f6_t2_sqlite_drainer_malformed_timestamp_skipped_undrained(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "bad_ts.db"
    sink = SqliteSink(sqlite_path)
    sink.emit(
        MeteringEvent(
            user_id="u-ts",
            sim_id="sim-bad",
            agent_type="planner",
            model="gemma2:9b",
            prompt_tokens=5,
            completion_tokens=6,
            total_tokens=11,
            timestamp_iso="not-a-date",
        )
    )
    sink.emit(
        MeteringEvent(
            user_id="u-ts",
            sim_id="sim-good",
            agent_type="planner",
            model="gemma2:9b",
            prompt_tokens=11,
            completion_tokens=11,
            total_tokens=22,
            timestamp_iso=datetime.now(UTC).isoformat(),
        )
    )

    ws = _setup_temp_go_workspace(tmp_path)
    go_code = f'''package e2eharness

import (
	"context"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
)

type recForwarder struct{{ count int }}

func (r *recForwarder) InsertEvent(_ context.Context, _ billing.Event) (int64, error) {{
	r.count++
	return 1, nil
}}

func (r *recForwarder) UpsertMonthlyRollup(_ context.Context, _ string, _ time.Time, _, _ int64) error {{
	return nil
}}

func TestMalformedTimestampSkippedUndrained(t *testing.T) {{
	fwd := &recForwarder{{}}
	d := billing.NewSQLiteDrainer({json.dumps(str(sqlite_path))}, fwd)
	n, err := d.DrainOnce(context.Background())
	if err != nil {{
		t.Fatalf("expected nil error when skipping bad timestamp row, got %v", err)
	}}
	if n != 1 || fwd.count != 1 {{
		t.Fatalf("expected 1 forwarded row, got n=%d count=%d", n, fwd.count)
	}}
}}
'''
    (ws / "e2eharness" / "f6_bad_ts_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestMalformedTimestampSkippedUndrained")

    with sqlite3.connect(sqlite_path) as sconn:
        rows = sconn.execute("SELECT sim_id, drained FROM metering_ledger ORDER BY id").fetchall()
        assert rows == [("sim-bad", 0), ("sim-good", 1)]


# =============================================================================
# Feature #7: Stripe redirect validator negative/boundary cases
# =============================================================================


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f7_t2_stripe_empty_return_base_url_refuses_arming_and_503s(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(
            rns,
            tmp_path,
            isolated_pg_schema,
            {
                "STRIPE_API_KEY": "sk_test_dummy_123",
                "STRIPE_PRICE_MAP": "price_pro=pro",
                "STRIPE_RETURN_BASE_URL": "",
            },
        )
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            logs = gw.stderr + gw.stdout
            assert "Stripe checkout creator armed" not in logs, (
                "Gateway must NOT arm StripeCheckoutCreator when STRIPE_RETURN_BASE_URL is empty"
            )
            assert "Stripe portal creator armed" not in logs, (
                "Gateway must NOT arm StripePortalCreator when STRIPE_RETURN_BASE_URL is empty"
            )

            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                c_resp = client.post(
                    "/api/v1/billing/checkout",
                    json={
                        "tier": "pro",
                        "success_url": "https://app.example.com/ok",
                        "cancel_url": "https://app.example.com/cancel",
                    },
                )
                assert c_resp.status_code == 503, c_resp.text
                assert c_resp.json()["code"] == "checkout_unavailable"

                p_resp = client.post(
                    "/api/v1/billing/portal",
                    json={"return_url": "https://app.example.com/billing"},
                )
                assert p_resp.status_code == 503, p_resp.text
                assert p_resp.json()["code"] == "portal_unavailable"


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f7_t2_stripe_checkout_rejects_cross_origin_and_subdomain_trick(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(
            rns,
            tmp_path,
            isolated_pg_schema,
            {
                "STRIPE_API_KEY": "sk_test_dummy_123",
                "STRIPE_PRICE_MAP": "price_pro=pro",
                "STRIPE_RETURN_BASE_URL": "https://app.example.com",
            },
        )
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            bad_urls = [
                "https://evil.com/ok",
                "https://app.example.com.evil.com/ok",
                "http://app.example.com/ok",
                "https://app.example.com:8443/ok",
            ]
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                for bad in bad_urls:
                    r1 = client.post(
                        "/api/v1/billing/checkout",
                        json={
                            "tier": "pro",
                            "success_url": bad,
                            "cancel_url": "https://app.example.com/cancel",
                        },
                    )
                    assert r1.status_code == 400, f"Expected 400 for success_url={bad!r}, got {r1.status_code}"
                    r2 = client.post(
                        "/api/v1/billing/checkout",
                        json={
                            "tier": "pro",
                            "success_url": "https://app.example.com/ok",
                            "cancel_url": bad,
                        },
                    )
                    assert r2.status_code == 400, f"Expected 400 for cancel_url={bad!r}, got {r2.status_code}"


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f7_t2_stripe_validator_rejects_userinfo_backslash_opaque_ctrl(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(
            rns,
            tmp_path,
            isolated_pg_schema,
            {
                "STRIPE_API_KEY": "sk_test_dummy_123",
                "STRIPE_PRICE_MAP": "price_pro=pro",
                "STRIPE_RETURN_BASE_URL": "https://app.example.com",
            },
        )
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            evil_urls = [
                "https://app.example.com@evil.com/ok",
                "https://evil.com\\@app.example.com/ok",
                "javascript:alert(1)",
                "https://app.example.com/\x00",
                "https://app.example.com/\nLocation:https://evil.com",
            ]
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                for evil in evil_urls:
                    resp = client.post(
                        "/api/v1/billing/checkout",
                        json={
                            "tier": "pro",
                            "success_url": evil,
                            "cancel_url": "https://app.example.com/cancel",
                        },
                    )
                    assert resp.status_code == 400, f"Expected 400 for {evil!r}, got {resp.status_code}"


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f7_t2_stripe_portal_rejects_cross_origin_and_oversized_body(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(
            rns,
            tmp_path,
            isolated_pg_schema,
            {
                "STRIPE_API_KEY": "sk_test_dummy_123",
                "STRIPE_PRICE_MAP": "price_pro=pro",
                "STRIPE_RETURN_BASE_URL": "https://app.example.com",
            },
        )
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                bad = client.post(
                    "/api/v1/billing/portal",
                    json={"return_url": "https://evil.example.org/phish"},
                )
                assert bad.status_code == 400
                assert bad.json()["error"] == "return_url not allowed"

                oversize = json.dumps({"return_url": "https://app.example.com/" + ("a" * ((1 << 20) + 1024))})
                big = client.post(
                    "/api/v1/billing/portal",
                    content=oversize.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                assert big.status_code == 413, big.text


@pytest.mark.requires_go
def test_f7_t2_stripe_price_map_rejects_comp_and_malformed_entries(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
)

func TestLoadPriceMapRejectsCompAndMalformed(t *testing.T) {
	badInputs := []string{
		"price_1=comp",
		"badpair",
		"=pro",
		"price_1=",
		"price_1=unknown_tier",
		"p1=pro,p1=team",
	}
	for _, raw := range badInputs {
		if _, err := billing.LoadPriceMapFromEnv(raw); err == nil {
			t.Fatalf("expected error for STRIPE_PRICE_MAP=%q, got nil", raw)
		}
	}
}
'''
    (ws / "e2eharness" / "f7_pricemap_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestLoadPriceMapRejectsCompAndMalformed")


# =============================================================================
# Feature #8: WebSocket subscribe_simulation ownership & negative cases
# =============================================================================


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t2_ws_subscribe_simulation_foreign_sim_rejected_with_error_frame(
    gateway_binary: Path,
    tmp_path: Path,
) -> None:
    foreign_sim = f"sim-foreign-{uuid.uuid4().hex[:8]}"
    user_a_uid = _local_dev_uid(f"usera-{uuid.uuid4().hex[:8]}@local.test")
    user_b_email = f"userb-{uuid.uuid4().hex[:8]}@local.test"

    with _isolated_redis() as rns:
        rclient = redis.Redis(host=rns.host, port=rns.port, password=rns.password or None)
        try:
            rclient.sadd(f"u:{user_a_uid}:{rns.prefix}:active-simulations", foreign_sim)
            rclient.sadd(f"u:{user_a_uid}:{rns.prefix}:simulation:{foreign_sim}:sessions", "sess-user-a")

            env = _gateway_env(
                rns,
                tmp_path,
                extra={
                    "SAAS_MODE": "on",
                    "LOCAL_DEV_USER_EMAIL": user_b_email,
                },
            )
            with EphemeralGoServer(gateway_binary, env=env) as gw:
                with ws_connect(f"{gw.ws_url}/ws") as ws:
                    ws.send(json.dumps({"type": "subscribe_simulation", "simulation_id": foreign_sim}))
                    err_frame = _recv_json_error_frame(ws)
                    assert err_frame["status"] == "error"
                    assert err_frame["type"] == "error"
                    assert err_frame["event"] == "forbidden"
                    assert err_frame["data"]["error"] == "simulation_not_owned"

                    leak_wrapper = gateway_pb2.Wrapper(
                        timestamp=datetime.now(UTC).isoformat(),
                        type="json",
                        event="narrative",
                        simulation_id=foreign_sim,
                        payload=b'{"secret":"must_not_leak"}',
                    )
                    rclient.publish("gateway:broadcast", leak_wrapper.SerializeToString())
                    marker = gateway_pb2.Wrapper(
                        timestamp=datetime.now(UTC).isoformat(),
                        type="system_marker",
                        event="system_marker",
                        payload=b"{}",
                    )
                    rclient.publish("gateway:broadcast", marker.SerializeToString())

                    leaked = False
                    deadline = time.monotonic() + 2.5
                    while time.monotonic() < deadline:
                        msg = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
                        if isinstance(msg, bytes):
                            got = gateway_pb2.Wrapper()
                            got.ParseFromString(msg)
                            if got.simulation_id == foreign_sim:
                                leaked = True
                                break
                            if got.type == "system_marker":
                                break
                    assert not leaked, "Unauthorized user received foreign simulation broadcast"
        finally:
            rclient.close()


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t2_ws_upgrade_foreign_session_or_sim_query_returns_403(
    gateway_binary: Path,
    tmp_path: Path,
) -> None:
    foreign_sid = f"sess-foreign-{uuid.uuid4().hex[:8]}"
    foreign_sim = f"sim-foreign-{uuid.uuid4().hex[:8]}"
    user_a_uid = _local_dev_uid(f"usera-{uuid.uuid4().hex[:8]}@local.test")
    user_b_email = f"userb-{uuid.uuid4().hex[:8]}@local.test"

    with _isolated_redis() as rns:
        rclient = redis.Redis(host=rns.host, port=rns.port, password=rns.password or None)
        try:
            rclient.set(f"u:{user_a_uid}:{rns.prefix}:session:{foreign_sid}", "leadership_planner")
            rclient.sadd(f"u:{user_a_uid}:{rns.prefix}:active-simulations", foreign_sim)

            env = _gateway_env(
                rns,
                tmp_path,
                extra={
                    "SAAS_MODE": "on",
                    "LOCAL_DEV_USER_EMAIL": user_b_email,
                },
            )
            with EphemeralGoServer(gateway_binary, env=env) as gw:
                with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                    r_sess = client.get(f"/ws?sessionId={foreign_sid}")
                    assert r_sess.status_code == 403, r_sess.text
                    assert r_sess.json()["error"] == "session_not_owned"

                    r_sim = client.get(f"/ws?simulationId={foreign_sim}")
                    assert r_sim.status_code == 403, r_sim.text
                    assert r_sim.json()["error"] == "simulation_not_owned"
        finally:
            rclient.close()


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t2_ws_invalid_session_or_sim_id_format_returns_400(
    gateway_binary: Path,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                r1 = client.get("/ws?sessionId=bad$id")
                assert r1.status_code == 400
                assert "invalid sessionId format" in r1.json()["error"]

                r2 = client.get("/ws?simulationId=../etc")
                assert r2.status_code == 400
                assert "invalid simulationId format" in r2.json()["error"]


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t2_ws_malformed_json_empty_sim_id_unknown_type_error_frames(
    gateway_binary: Path,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with ws_connect(f"{gw.ws_url}/ws") as ws:
                ws.send("{bad")
                f1 = _recv_json_error_frame(ws)
                assert f1["data"]["error"] == "invalid_json"

                ws.send(json.dumps({"type": "subscribe_simulation", "simulation_id": ""}))
                f2 = _recv_json_error_frame(ws)
                assert f2["data"]["error"] == "missing_simulation_id"

                ws.send(json.dumps({"type": "bogus_message_type"}))
                f3 = _recv_json_error_frame(ws)
                assert f3["data"]["error"] == "unknown_message_type"


@pytest.mark.requires_go
def test_f8_t2_hub_validator_error_fails_closed_service_unavailable(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/gen_proto/gateway"
	"github.com/GoogleCloudPlatform/race-condition/internal/hub"
	"github.com/gorilla/websocket"
)

func TestHubValidatorErrorFailsClosed(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	h := hub.NewHub(
		hub.WithSimulationOwnershipValidator(func(_ context.Context, _, _ string) (bool, error) {
			return false, errors.New("redis down")
		}),
		hub.WithRequireSimulationOwnership(true),
	)
	go h.Run(ctx)

	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		h.RegisterWithUser("sess-1", conn, "user-1")
		for {
			mType, msg, err := conn.ReadMessage()
			if err != nil {
				return
			}
			if mType == websocket.TextMessage {
				h.HandleTextMessage(conn, msg)
			}
		}
	}))
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	clientConn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer clientConn.Close()

	if err := clientConn.WriteMessage(websocket.TextMessage, []byte(`{"type":"subscribe_simulation","simulation_id":"sim-x"}`)); err != nil {
		t.Fatalf("write: %v", err)
	}
	_ = clientConn.SetReadDeadline(time.Now().Add(3 * time.Second))
	mType, raw, err := clientConn.ReadMessage()
	if err != nil || mType != websocket.TextMessage {
		t.Fatalf("expected error text frame, got mType=%d err=%v", mType, err)
	}
	var gm hub.GatewayMessage
	if err := json.Unmarshal(raw, &gm); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	dataMap, _ := gm.Data.(map[string]any)
	if gm.Event != "service_unavailable" || dataMap["error"] != "ownership_check_failed" {
		t.Fatalf("unexpected error frame: %#v", gm)
	}

	h.HandleRemoteMessage(&gateway.Wrapper{SimulationId: "sim-x", Type: "json", Event: "narrative", Payload: []byte("nope")})
	_ = clientConn.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
	if _, _, err := clientConn.ReadMessage(); err == nil {
		t.Fatalf("client should NOT receive simulation traffic after validator failure")
	}
}
'''
    (ws / "e2eharness" / "f8_validator_err_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestHubValidatorErrorFailsClosed")


# =============================================================================
# Feature #9: Go handler error propagation & canonical port 9101
# =============================================================================


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f9_t2_me_init_malformed_json_400_and_oversize_413(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path, isolated_pg_schema)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                bad = client.post(
                    "/api/v1/me/init",
                    content=b"{bad",
                    headers={"Content-Type": "application/json"},
                )
                assert bad.status_code == 400, f"Malformed JSON on /me/init must return 400, got {bad.status_code}: {bad.text}"

                oversize = json.dumps({"display_name": "x" * ((1 << 20) + 1024)}).encode("utf-8")
                big = client.post(
                    "/api/v1/me/init",
                    content=oversize,
                    headers={"Content-Type": "application/json"},
                )
                assert big.status_code == 413, big.text

                empty = client.post("/api/v1/me/init")
                assert empty.status_code == 201, empty.text


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f9_t2_memory_upload_exceeding_max_bytes_returns_413(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(
            rns,
            tmp_path,
            isolated_pg_schema,
            {"MEMORY_MAX_UPLOAD_MB": "1"},
        )
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            with httpx.Client(base_url=gw.base_url, timeout=15.0) as client:
                an = client.post("/api/v1/memory/analyses", json={"title": "Upload Limit Test"})
                assert an.status_code == 201
                aid = an.json()["id"]

                big_payload = b"A" * (1_500_000)
                up = client.post(
                    f"/api/v1/memory/analyses/{aid}/documents",
                    data={"kind": "memo"},
                    files={"file": ("big.txt", big_payload, "text/plain")},
                )
                assert up.status_code == 413, f"Expected 413 on >MEMORY_MAX_UPLOAD_MB upload, got {up.status_code}: {up.text}"
                assert up.json()["error"] == "request_body_too_large"


@pytest.mark.requires_go
def test_f9_t2_billing_healthz_returns_503_when_db_unavailable(go_bin_dir: Path) -> None:
    billing_bin = _build_billing_binary(go_bin_dir)
    with EphemeralGoServer(
        billing_bin,
        env={"DATABASE_URL": ""},
        health_path="/healthz",
        ready_statuses=(200, 503),
    ) as bill:
        with httpx.Client(base_url=bill.base_url, timeout=10.0) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 503, f"Expected 503 when DB unavailable, got {resp.status_code}: {resp.text}"


@pytest.mark.requires_go
def test_f9_t2_export_and_usage_propagate_db_errors_as_500(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    gw_pkg = ws / "cmd_gw_fault"
    adm_pkg = ws / "cmd_adm_fault"
    _symlink_non_test_go_files(REPO_ROOT / "cmd" / "gateway", gw_pkg)
    _symlink_non_test_go_files(REPO_ROOT / "cmd" / "admin", adm_pkg)

    gw_test_code = '''package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/gin-gonic/gin"
)

type faultMeExportStore struct{}

func (faultMeExportStore) GetUser(_ context.Context, id string) (account.User, error) {
	return account.User{ID: id, Email: "u@test", PlanTier: account.PlanFree}, nil
}

func (faultMeExportStore) ListAudit(_ context.Context, _, _ string, _, _ int) ([]account.AuditEntry, error) {
	return nil, errors.New("audit db failure")
}

func TestExportMePropagatesListAuditError(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		ctx := account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-1"})
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	})
	r.GET("/api/v1/me/export", exportMeHandler(faultMeExportStore{}))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/me/export", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500 when ListAudit fails, got %d: %s", w.Code, w.Body.String())
	}
}
'''
    (gw_pkg / "e2e_fault_test.go").write_text(gw_test_code, encoding="utf-8")
    _run_temp_go_test(ws, "cmd_gw_fault", "TestExportMePropagatesListAuditError")

    adm_test_code = '''package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type fakeUserRow struct{}

func (fakeUserRow) Scan(dest ...any) error {
	now := time.Now().UTC()
	*(dest[0].(*string)) = "u-1"
	*(dest[1].(*string)) = "u1@test"
	*(dest[2].(*string)) = "User One"
	*(dest[3].(*string)) = "UTC"
	*(dest[4].(*string)) = ""
	*(dest[5].(*string)) = "free"
	*(dest[6].(**time.Time)) = nil
	*(dest[7].(*bool)) = false
	*(dest[8].(*time.Time)) = now
	*(dest[9].(**time.Time)) = nil
	*(dest[10].(**time.Time)) = nil
	*(dest[11].(*string)) = "approved"
	*(dest[12].(**time.Time)) = nil
	*(dest[13].(*string)) = ""
	return nil
}

type faultAuditConn struct{}

func (faultAuditConn) Exec(_ context.Context, _ string, _ ...any) (pgconn.CommandTag, error) {
	return pgconn.CommandTag{}, nil
}

func (faultAuditConn) Query(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
	return nil, errors.New("audit query failure")
}

func (faultAuditConn) QueryRow(_ context.Context, _ string, _ ...any) pgx.Row {
	return fakeUserRow{}
}

type faultBreakdownReader struct{}

func (faultBreakdownReader) GetMonthlyUsage(_ context.Context, userID, ym string) (billing.MonthlyUsage, error) {
	return billing.MonthlyUsage{UserID: userID, YearMonth: ym, TotalTokens: 10}, nil
}

func (faultBreakdownReader) GetAgentBreakdown(_ context.Context, _, _ string) ([]billing.AgentBreakdown, error) {
	return nil, errors.New("breakdown query failure")
}

func TestAdminExportAndUsagePropagateErrors(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/admin/users/:id/export", exportUserHandler(account.NewStore(faultAuditConn{})))
	r.GET("/admin/users/:id/usage", usageUserHandler(faultBreakdownReader{}, nil))

	w1 := httptest.NewRecorder()
	r.ServeHTTP(w1, httptest.NewRequest(http.MethodGet, "/admin/users/u-1/export", nil))
	if w1.Code != http.StatusInternalServerError {
		t.Fatalf("exportUserHandler expected 500 on ListAudit failure, got %d: %s", w1.Code, w1.Body.String())
	}

	w2 := httptest.NewRecorder()
	r.ServeHTTP(w2, httptest.NewRequest(http.MethodGet, "/admin/users/u-1/usage", nil))
	if w2.Code != http.StatusInternalServerError {
		t.Fatalf("usageUserHandler expected 500 on GetAgentBreakdown failure, got %d: %s", w2.Code, w2.Body.String())
	}
}
'''
    (adm_pkg / "e2e_fault_test.go").write_text(adm_test_code, encoding="utf-8")
    _run_temp_go_test(ws, "cmd_adm_fault", "TestAdminExportAndUsagePropagateErrors")


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f9_t2_ai_settings_malformed_json_400_and_autostart_failure(
    gateway_binary: Path,
    tmp_path: Path,
) -> None:
    dead_port = free_port()
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                bad_test = client.post(
                    "/api/v1/settings/ai/test",
                    content=b"{bad",
                    headers={"Content-Type": "application/json"},
                )
                assert bad_test.status_code == 400, f"Expected 400 for malformed JSON on /ai/test, got {bad_test.status_code}"

                bad_start = client.post(
                    "/api/v1/settings/ai/start",
                    content=b"{bad",
                    headers={"Content-Type": "application/json"},
                )
                assert bad_start.status_code == 400, f"Expected 400 for malformed JSON on /ai/start, got {bad_start.status_code}"

                auto_resp = client.post(
                    "/api/v1/settings/ai/test",
                    json={"endpoint": f"http://127.0.0.1:{dead_port}", "auto_start": True},
                )
                if auto_resp.status_code in (502, 503):
                    assert auto_resp.json().get("error")
                else:
                    assert auto_resp.status_code == 200
                    body = auto_resp.json()
                    assert body["reachable"] is False
                    err_txt = f"{body.get('error', '')} {body.get('start_error', '')}".lower()
                    assert "auto-start" in err_txt or "auto_start" in err_txt, (
                        f"EnsureDaemonRunning startErr was silently swallowed: {body}"
                    )


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f9_t2_admin_authz_and_reset_bff_and_default_port_9101(
    gateway_binary: Path,
    admin_binary: Path,
    frontend_binary: Path,
    tester_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        adm_env = {
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "REDIS_ADDR": rns.addr,
            "REDIS_PASSWORD": rns.password,
        }
        with EphemeralGoServer(admin_binary, env=adm_env) as adm:
            with httpx.Client(base_url=adm.base_url, timeout=10.0) as client:
                r_users = client.get("/admin/users")
                assert r_users.status_code == 403
                assert r_users.json()["error"] == "superadmin_required"

                r_reset = client.post("/api/admin/environment/reset", json={"targets": ["sessions"]})
                assert r_reset.status_code == 403, f"Expected 403 superadmin_required on BFF reset, got {r_reset.status_code}: {r_reset.text}"
                assert r_reset.json()["error"] == "superadmin_required"

    # Verify canonical default Gateway port 9101 across cmd/{gateway,admin,frontend,tester}
    for rel in ("cmd/gateway/main.go", "cmd/admin/main.go", "cmd/frontend/main.go", "cmd/tester/main.go"):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "8101" not in src, f"Stale port 8101 still referenced in {rel}"

    admin_src = (REPO_ROOT / "cmd/admin/main.go").read_text(encoding="utf-8")
    for agent_name in ("leadership_planner", "leadership_runner", "ai_echo"):
        assert re.search(rf'"{agent_name}"\s*:', admin_src), f"{agent_name} missing from defaultPortMap in cmd/admin/main.go"

    if _is_port_free(9101):
        hits: list[str] = []

        class _Mock9101(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                hits.append(self.path)
                payload = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        httpd = ThreadingHTTPServer(("127.0.0.1", 9101), _Mock9101)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with EphemeralGoServer(frontend_binary, env={}) as fe, EphemeralGoServer(tester_binary, env={}) as te:
                with httpx.Client(base_url=fe.base_url, timeout=5.0) as client:
                    assert client.get("/api/v1/agent-types").status_code == 200
                with httpx.Client(base_url=te.base_url, timeout=5.0) as client:
                    assert client.get("/api/v1/agent-types").status_code == 200
                assert len(hits) >= 2
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2.0)

        # Also boot gateway binary without PORT/GATEWAY_PORT to verify it listens on 9101.
        with _isolated_redis() as rns:
            gw_env = {k: os.environ[k] for k in _INHERITED_ENV_KEYS if k in os.environ}
            for k in _DOTENV_BLOCKED_KEYS:
                gw_env[k] = ""
            gw_env.update(
                {
                    "IDENTITY_PROVIDER": "local",
                    "REDIS_ADDR": rns.addr,
                    "REDIS_PASSWORD": rns.password,
                    "REDIS_PREFIX": rns.prefix,
                    "AI_SETTINGS_PATH": str(tmp_path / "gw9101_ai.json"),
                }
            )
            with (
                tempfile.TemporaryFile(mode="w+b") as out_f,
                tempfile.TemporaryFile(mode="w+b") as err_f,
            ):
                proc = subprocess.Popen([str(gateway_binary)], cwd=str(REPO_ROOT), env=gw_env, stdout=out_f, stderr=err_f)
                try:
                    deadline = time.monotonic() + 10.0
                    ready = False
                    with httpx.Client(timeout=1.0) as client:
                        while time.monotonic() < deadline:
                            if proc.poll() is not None:
                                break
                            try:
                                if client.get("http://127.0.0.1:9101/health").status_code == 200:
                                    ready = True
                                    break
                            except httpx.HTTPError:
                                pass
                            time.sleep(0.1)
                    assert ready, "cmd/gateway did not listen on canonical default port 9101"
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=5.0)
    else:
        warnings.warn(
            "Host port 9101 is already occupied; skipping live default-port-9101 proxy/boot assertions "
            "(static 8101-absence and defaultPortMap checks still ran).",
            UserWarning,
            stacklevel=1,
        )


@pytest.mark.requires_go
def test_f9_t2_tester_require_auth_401_without_bearer(tester_binary: Path) -> None:
    with EphemeralGoServer(tester_binary, env={"TESTER_REQUIRE_AUTH": "true"}) as te:
        with httpx.Client(base_url=te.base_url, timeout=10.0) as client:
            api_resp = client.get("/api/v1/agent-types")
            assert api_resp.status_code == 401
            assert api_resp.json()["error"] == "operator_auth_required"

            health_resp = client.get("/health")
            assert health_resp.status_code == 200


# =============================================================================
# Feature #10: Go internal stores, hub & middleware fail-closed contracts
# =============================================================================


@pytest.mark.requires_go
def test_f10_t2_memory_store_rejects_empty_wrong_dim_zero_nan_embeddings(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"math"
	"strings"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/memory"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type panicConn struct{}

func (panicConn) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	panic("SQL should not be executed for invalid embeddings")
}
func (panicConn) Query(context.Context, string, ...any) (pgx.Rows, error) {
	panic("SQL should not be executed for invalid embeddings")
}
func (panicConn) QueryRow(context.Context, string, ...any) pgx.Row {
	panic("SQL should not be executed for invalid embeddings")
}
func (panicConn) Begin(context.Context) (pgx.Tx, error) {
	panic("SQL should not be executed for invalid embeddings")
}

func TestMemoryStoreRejectsBadEmbeddingsBeforeSQL(t *testing.T) {
	s := memory.NewStore(panicConn{})
	aid := uuid.New()

	zeroVec := make([]float32, memory.TargetVectorDimension)
	nanVec := make([]float32, memory.TargetVectorDimension)
	nanVec[0] = float32(math.NaN())

	badCases := [][]float32{
		{},
		make([]float32, 768),
		zeroVec,
		nanVec,
	}
	for i, emb := range badCases {
		_, err := s.RetrieveChunks(context.Background(), aid, emb, 5)
		if err == nil || !strings.Contains(err.Error(), "memory:") {
			t.Fatalf("case %d: expected explicit memory: error, got %v", i, err)
		}
	}
}
'''
    (ws / "e2eharness" / "f10_bad_emb_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestMemoryStoreRejectsBadEmbeddingsBeforeSQL")


@pytest.mark.requires_go
@pytest.mark.requires_postgres
def test_f10_t2_account_store_typed_nil_last_superadmin_and_corrupt_audit(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestAccountStoreNilLastSuperadminAndCorruptAudit(t *testing.T) {
	ctx := context.Background()

	var typedNil *pgxpool.Pool
	nilStore := account.NewStore(typedNil)
	if _, err := nilStore.GetUser(ctx, "u1"); !errors.Is(err, account.ErrNilStore) {
		t.Fatalf("expected ErrNilStore on typed nil pool, got %v", err)
	}

	dsn := os.Getenv("TEST_DSN")
	migConn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	if err := account.ApplyMigration(ctx, migConn); err != nil {
		_ = migConn.Close(ctx)
		t.Fatalf("migrate: %v", err)
	}
	_ = migConn.Close(ctx)

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	defer pool.Close()

	s := account.NewStore(pool)
	if _, _, err := s.CreateUser(ctx, "sole-admin", "sole@test", "Sole"); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	if err := s.SetSuperadmin(ctx, "sole@test", true); err != nil {
		t.Fatalf("promote sole: %v", err)
	}
	if err := s.SetSuperadmin(ctx, "sole@test", false); !errors.Is(err, account.ErrLastSuperadmin) {
		t.Fatalf("expected ErrLastSuperadmin, got %v", err)
	}

	if _, err := pool.Exec(ctx, "INSERT INTO audit_log (user_id, action, metadata) VALUES ($1, $2, $3::jsonb)", "sole-admin", "corrupt.evt", `["not","an","object"]`); err != nil {
		t.Fatalf("insert non-object audit: %v", err)
	}
	if _, err := s.ListAudit(ctx, "sole-admin", "corrupt.evt", 10, 0); err == nil {
		t.Fatalf("expected decode error for corrupt audit metadata JSON")
	}
}
'''
    (ws / "e2eharness" / "f10_acct_edge_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestAccountStoreNilLastSuperadminAndCorruptAudit", env={"TEST_DSN": isolated_pg_schema.search_path_dsn})


@pytest.mark.requires_go
def test_f10_t2_billing_dedup_fail_closed_on_nil_zero_and_db_error(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"errors"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type failingExecer struct{}

func (failingExecer) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	return pgconn.CommandTag{}, errors.New("db connection dropped")
}

func TestBillingDedupFailClosed(t *testing.T) {
	var nilMem *billing.InMemoryEventDedup
	if seen, err := nilMem.SeenOrMark("evt_1"); err == nil || seen {
		t.Fatalf("nil InMemoryEventDedup must fail closed, got seen=%v err=%v", seen, err)
	}
	zeroMem := &billing.InMemoryEventDedup{}
	if seen, err := zeroMem.SeenOrMark("evt_1"); err == nil || seen {
		t.Fatalf("zero InMemoryEventDedup must fail closed, got seen=%v err=%v", seen, err)
	}

	var nilPool *pgxpool.Pool
	if _, err := billing.NewPostgresEventDedup(nilPool); !errors.Is(err, billing.ErrDedupUnavailable) {
		t.Fatalf("expected ErrDedupUnavailable on typed nil pool, got %v", err)
	}

	pgDedup, err := billing.NewPostgresEventDedup(failingExecer{})
	if err != nil {
		t.Fatalf("NewPostgresEventDedup: %v", err)
	}
	if seen, err := pgDedup.SeenOrMark("evt_2"); err == nil || seen {
		t.Fatalf("failing DB must fail closed, got seen=%v err=%v", seen, err)
	}
}
'''
    (ws / "e2eharness" / "f10_dedup_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestBillingDedupFailClosed")


@pytest.mark.requires_go
def test_f10_t2_middleware_appcheck_and_cors_fail_closed_misconfig(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/middleware"
	"github.com/gin-gonic/gin"
)

func TestMiddlewareAppCheckAndCORSFailClosed(t *testing.T) {
	gin.SetMode(gin.TestMode)

	opts, err := middleware.AppCheckFromEnvE(func(k string) string {
		if k == "APPCHECK_ENFORCE" {
			return "enforce"
		}
		return ""
	})
	if err == nil || opts.InitErr == nil {
		t.Fatalf("expected InitErr when APPCHECK_ENFORCE=enforce without project")
	}

	if _, badErr := middleware.AppCheckFromEnvE(func(k string) string {
		if k == "APPCHECK_ENFORCE" {
			return "enabled"
		}
		return ""
	}); badErr == nil {
		t.Fatalf("expected error for unknown APPCHECK_ENFORCE mode")
	}

	r := gin.New()
	r.Use(middleware.AppCheck(opts))
	r.GET("/api/v1/me", func(c *gin.Context) { c.Status(http.StatusOK) })
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/me", nil))
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 appcheck_unavailable, got %d: %s", w.Code, w.Body.String())
	}

	t.Setenv("CORS_REFUSE_WILDCARD", "true")
	rc := gin.New()
	rc.Use(middleware.CORS("*"))
	rc.GET("/ping", func(c *gin.Context) { c.Status(http.StatusOK) })
	req := httptest.NewRequest(http.MethodGet, "/ping", nil)
	req.Header.Set("Origin", "https://evil.example.com")
	wc := httptest.NewRecorder()
	rc.ServeHTTP(wc, req)
	if got := wc.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Fatalf("expected empty Access-Control-Allow-Origin under CORS_REFUSE_WILDCARD, got %q", got)
	}
}
'''
    (ws / "e2eharness" / "f10_mw_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestMiddlewareAppCheckAndCORSFailClosed")


@pytest.mark.requires_go
def test_f10_t2_local_resolver_refuses_non_loopback_and_gcp_runtime(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"net/http/httptest"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

func TestLocalResolverRefusesNonLoopbackAndGCPRuntime(t *testing.T) {
	lr := account.NewLocalResolver()

	req := httptest.NewRequest("GET", "/api/v1/me", nil)
	req.RemoteAddr = "198.51.100.10:1234"
	if _, err := lr.Resolve(req); err == nil {
		t.Fatalf("expected error for non-loopback RemoteAddr")
	}

	t.Setenv("K_SERVICE", "race-gateway")
	loopReq := httptest.NewRequest("GET", "/api/v1/me", nil)
	loopReq.RemoteAddr = "127.0.0.1:1234"
	if _, err := lr.Resolve(loopReq); err == nil {
		t.Fatalf("expected error under K_SERVICE without ALLOW_LOCAL_AUTH=1")
	}
}
'''
    (ws / "e2eharness" / "f10_local_resolver_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestLocalResolverRefusesNonLoopbackAndGCPRuntime")


@pytest.mark.requires_go
def test_f10_t2_switchboard_publish_and_auth_fail_closed_on_errors(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/agent"
	"github.com/GoogleCloudPlatform/race-condition/internal/hub"
	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestSwitchboardPublishAndAuthFailClosed(t *testing.T) {
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", filepath.Join(t.TempDir(), "missing-sa.json"))

	mr, err := miniredis.Run()
	if err != nil {
		t.Fatalf("miniredis: %v", err)
	}
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	defer rdb.Close()

	h := hub.NewHub()
	sb := hub.NewSwitchboardWithRegistry(rdb, "gw-test", h, agent.NewCatalog(nil), nil)

	mr.Close()
	if err := sb.PublishOrchestration(context.Background(), "simulation:broadcast", map[string]string{"type": "ping"}); err == nil {
		t.Fatalf("expected joined error when Redis is closed on PublishOrchestration")
	}

	if err := sb.DispatchToAgent(context.Background(), "ae_agent", map[string]string{"type": "ping"}); err == nil {
		t.Fatalf("expected DispatchToAgent error for missing agent / unauthenticated AE")
	}
}
'''
    (ws / "e2eharness" / "f10_switchboard_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestSwitchboardPublishAndAuthFailClosed")
