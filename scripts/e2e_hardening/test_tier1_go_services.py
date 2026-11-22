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

"""Tier 1 category-partition functional tests for Go Services (Features #6-#10)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg2
import pytest
import redis
from websockets.sync.client import connect as ws_connect

from agents.utils.metering import MeteringEvent, SqliteSink
from gen_proto.gateway import gateway_pb2
from scripts.e2e_hardening.conftest import MockOllamaServer
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    EphemeralGoServer,
    IsolatedPostgresSchema,
    IsolatedRedisNamespace,
    RedisUnavailableError,
    run_cmd,
)

pytestmark = pytest.mark.tier1


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
        "LOCAL_DEV_USER_EMAIL": f"t1-{uuid.uuid4().hex[:10]}@local.test",
        "AI_SETTINGS_PATH": str(tmp_path / "ai_settings.json"),
        "MEMORY_STORAGE_DIR": str(tmp_path / "mem_storage"),
        "METERING_SQLITE_PATH": str(tmp_path / "metering.db"),
    }
    if pg is not None:
        env["DATABASE_URL"] = pg.search_path_dsn
    if extra:
        env.update(extra)
    return env


def _init_local_user(base_url: str, display_name: str = "Local Tester", timezone: str = "UTC") -> httpx.Response:
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        return client.post(
            "/api/v1/me/init",
            json={"display_name": display_name, "timezone": timezone},
        )


def _stripe_sign(secret: str, payload: bytes, ts: int | None = None) -> str:
    stamp = ts if ts is not None else int(time.time())
    signed_payload = f"{stamp}.".encode("utf-8") + payload
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={sig}"


def _recv_matching_wrapper(
    ws,
    *,
    sim_id: str | None = None,
    event_type: str | None = None,
    timeout: float = 4.0,
) -> gateway_pb2.Wrapper:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        msg = ws.recv(timeout=remaining)
        if isinstance(msg, bytes):
            wrapper = gateway_pb2.Wrapper()
            wrapper.ParseFromString(msg)
            if sim_id is not None and wrapper.simulation_id != sim_id:
                continue
            if event_type is not None and wrapper.event != event_type and wrapper.type != event_type:
                continue
            return wrapper
    raise TimeoutError(f"Did not receive matching Wrapper(sim_id={sim_id!r}, event_type={event_type!r})")


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
# Feature #6: SQLite billing drainer mutex & atomic usage_rollup_monthly
# =============================================================================


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f6_t1_sqlite_drainer_happy_path_via_gateway_usage(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "metering.db"
    email = f"f6-happy-{uuid.uuid4().hex[:8]}@local.test"
    uid = _local_dev_uid(email)
    sink = SqliteSink(sqlite_path)
    now_iso = datetime.now(UTC).isoformat()
    sink.emit(
        MeteringEvent(
            user_id=uid,
            sim_id="sim-f6-1",
            agent_type="leadership_planner",
            model="gemma2:9b",
            prompt_tokens=70,
            completion_tokens=50,
            total_tokens=120,
            cost_micros=300,
            timestamp_iso=now_iso,
        )
    )
    sink.emit(
        MeteringEvent(
            user_id=uid,
            sim_id="sim-f6-1",
            agent_type="leadership_runner",
            model="gemma2:9b",
            prompt_tokens=50,
            completion_tokens=30,
            total_tokens=80,
            cost_micros=200,
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
            init_resp = _init_local_user(gw.base_url)
            assert init_resp.status_code == 201, init_resp.text

            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                usage_resp = client.get("/api/v1/usage")
                assert usage_resp.status_code == 200, usage_resp.text
                body = usage_resp.json()
                assert body["tokens_used"] == 200

    with sqlite3.connect(sqlite_path) as sconn:
        drained_vals = [row[0] for row in sconn.execute("SELECT drained FROM metering_ledger ORDER BY id")]
        assert drained_vals == [1, 1]

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as pconn, pconn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total_tokens), 0) FROM usage_ledger WHERE user_id = %s", (uid,))
        count, ledger_sum = cur.fetchone()
        assert count == 2
        assert ledger_sum == 200
        ym = datetime.now(UTC).strftime("%Y-%m")
        cur.execute(
            "SELECT total_tokens, cost_micros FROM usage_rollup_monthly WHERE user_id = %s AND year_month = %s",
            (uid, ym),
        )
        rollup_tokens, rollup_cost = cur.fetchone()
        assert rollup_tokens == 200
        assert rollup_cost == 500


@pytest.mark.requires_go
def test_f6_t1_sqlite_drainer_concurrent_drain_once_no_double_count(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "concurrent_drain.db"
    sink = SqliteSink(sqlite_path)
    now_iso = datetime.now(UTC).isoformat()
    total_rows = 40
    for idx in range(total_rows):
        sink.emit(
            MeteringEvent(
                user_id="user-conc",
                sim_id=f"sim-{idx}",
                agent_type="leadership_planner",
                model="gemma2:9b",
                prompt_tokens=10,
                completion_tokens=15,
                total_tokens=25,
                cost_micros=50,
                timestamp_iso=now_iso,
            )
        )

    ws = _setup_temp_go_workspace(tmp_path)
    go_code = f'''package e2eharness

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
)

type countingForwarder struct {{
	mu      sync.Mutex
	inserts int
	rollups int
	tokens  int64
}}

func (f *countingForwarder) InsertEvent(_ context.Context, e billing.Event) (int64, error) {{
	f.mu.Lock()
	defer f.mu.Unlock()
	f.inserts++
	return int64(f.inserts), nil
}}

func (f *countingForwarder) UpsertMonthlyRollup(_ context.Context, _ string, _ time.Time, addTokens, _ int64) error {{
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rollups++
	f.tokens += addTokens
	return nil
}}

func TestConcurrentDrainOnceNoDoubleCount(t *testing.T) {{
	fwd := &countingForwarder{{}}
	drainer := billing.NewSQLiteDrainer({json.dumps(str(sqlite_path))}, fwd, billing.WithBatchSize(10))
	const workers = 16
	var wg sync.WaitGroup
	errs := make(chan error, workers)
	for i := 0; i < workers; i++ {{
		wg.Add(1)
		go func() {{
			defer wg.Done()
			for {{
				n, err := drainer.DrainOnce(context.Background())
				if err != nil {{
					errs <- err
					return
				}}
				if n == 0 {{
					return
				}}
			}}
		}}()
	}}
	wg.Wait()
	close(errs)
	for err := range errs {{
		t.Fatalf("unexpected DrainOnce error: %v", err)
	}}
	if fwd.inserts != {total_rows} {{
		t.Fatalf("expected %d inserts, got %d", {total_rows}, fwd.inserts)
	}}
	if fwd.rollups != {total_rows} {{
		t.Fatalf("expected %d rollups, got %d", {total_rows}, fwd.rollups)
	}}
	if fwd.tokens != {total_rows * 25} {{
		t.Fatalf("expected %d tokens, got %d", {total_rows * 25}, fwd.tokens)
	}}
}}
'''
    (ws / "e2eharness" / "f6_conc_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestConcurrentDrainOnceNoDoubleCount")

    with sqlite3.connect(sqlite_path) as sconn:
        undrained = sconn.execute("SELECT COUNT(*) FROM metering_ledger WHERE drained = 0").fetchone()[0]
        assert undrained == 0


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f6_t1_sqlite_drainer_null_tokens_and_cost_micros_preserved(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "null_tokens.db"
    email = f"f6-null-{uuid.uuid4().hex[:8]}@local.test"
    uid = _local_dev_uid(email)
    sink = SqliteSink(sqlite_path)
    sink.emit(
        MeteringEvent(
            user_id=uid,
            sim_id="sim-null",
            agent_type="leadership_runner",
            model="gemma2:9b",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            cost_micros=None,
            timestamp_iso=datetime.now(UTC).isoformat(),
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
                assert resp.status_code == 200
                assert resp.json()["tokens_used"] == 0

    with sqlite3.connect(sqlite_path) as sconn:
        assert sconn.execute("SELECT drained FROM metering_ledger").fetchone()[0] == 1

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as pconn, pconn.cursor() as cur:
        cur.execute(
            "SELECT prompt_tokens, completion_tokens, total_tokens, cost_micros FROM usage_ledger WHERE user_id = %s",
            (uid,),
        )
        row = cur.fetchone()
        assert row == (None, None, None, None)


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f6_t1_sqlite_drainer_legacy_9_col_schema_supported(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "legacy_9col.db"
    email = f"f6-legacy-{uuid.uuid4().hex[:8]}@local.test"
    uid = _local_dev_uid(email)
    now_iso = datetime.now(UTC).isoformat()
    with sqlite3.connect(sqlite_path) as sconn:
        sconn.executescript(
            """
            CREATE TABLE metering_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                sim_id TEXT,
                agent_type TEXT,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                timestamp_iso TEXT NOT NULL,
                drained INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        sconn.execute(
            "INSERT INTO metering_ledger (user_id, sim_id, agent_type, model, prompt_tokens, completion_tokens, total_tokens, timestamp_iso) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, "sim-leg", "planner", "gemma2:9b", 30, 20, 50, now_iso),
        )
        sconn.execute(
            "INSERT INTO metering_ledger (user_id, sim_id, agent_type, model, prompt_tokens, completion_tokens, total_tokens, timestamp_iso) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, "sim-leg", "runner", "gemma2:9b", 45, 30, 75, now_iso),
        )
        sconn.commit()

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
                assert resp.status_code == 200
                assert resp.json()["tokens_used"] == 125

    with sqlite3.connect(sqlite_path) as sconn:
        assert [r[0] for r in sconn.execute("SELECT drained FROM metering_ledger ORDER BY id")] == [1, 1]


@pytest.mark.requires_go
def test_f6_t1_sqlite_drainer_rollup_recovery_after_transient_error(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "rollup_recover.db"
    sink = SqliteSink(sqlite_path)
    now_iso = datetime.now(UTC).isoformat()
    sink.emit(
        MeteringEvent(
            user_id="u-rec",
            sim_id="sim-r1",
            agent_type="planner",
            model="gemma2:9b",
            prompt_tokens=20,
            completion_tokens=20,
            total_tokens=40,
            cost_micros=10,
            timestamp_iso=now_iso,
        )
    )
    sink.emit(
        MeteringEvent(
            user_id="u-rec",
            sim_id="sim-r2",
            agent_type="runner",
            model="gemma2:9b",
            prompt_tokens=30,
            completion_tokens=30,
            total_tokens=60,
            cost_micros=20,
            timestamp_iso=now_iso,
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

type flakyRollupForwarder struct {{
	failOnCall int
	calls      int
	rollupSum  int64
}}

func (f *flakyRollupForwarder) InsertEvent(_ context.Context, _ billing.Event) (int64, error) {{
	return 1, nil
}}

func (f *flakyRollupForwarder) UpsertMonthlyRollup(_ context.Context, _ string, _ time.Time, addTokens, _ int64) error {{
	f.calls++
	if f.calls == f.failOnCall {{
		return errors.New("transient pg rollup deadlock")
	}}
	f.rollupSum += addTokens
	return nil
}}

func TestRollupRecoveryAfterTransientError(t *testing.T) {{
	fwd := &flakyRollupForwarder{{failOnCall: 2}}
	drainer := billing.NewSQLiteDrainer({json.dumps(str(sqlite_path))}, fwd)

	n1, err1 := drainer.DrainOnce(context.Background())
	if err1 == nil {{
		t.Fatalf("expected pass 1 error when UpsertMonthlyRollup fails on row 2")
	}}
	if n1 != 1 {{
		t.Fatalf("expected pass 1 forwarded=1, got %d", n1)
	}}

	fwd.failOnCall = 0
	n2, err2 := drainer.DrainOnce(context.Background())
	if err2 != nil {{
		t.Fatalf("expected pass 2 nil error, got %v", err2)
	}}
	if n2 != 1 {{
		t.Fatalf("expected pass 2 forwarded=1, got %d", n2)
	}}
	if fwd.rollupSum != 100 {{
		t.Fatalf("expected rollup sum 100, got %d", fwd.rollupSum)
	}}
}}
'''
    (ws / "e2eharness" / "f6_recover_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestRollupRecoveryAfterTransientError")

    with sqlite3.connect(sqlite_path) as sconn:
        assert [r[0] for r in sconn.execute("SELECT drained FROM metering_ledger ORDER BY id")] == [1, 1]


# =============================================================================
# Feature #7: Stripe redirect validator & boot arming
# =============================================================================


@pytest.mark.requires_go
def test_f7_t1_stripe_redirect_valid_same_origin_and_default_port(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
)

func TestValidateRedirectURLSameOriginAndDefaultPort(t *testing.T) {
	cases := [][2]string{
		{"https://app.example/done", "https://app.example"},
		{"https://APP.EXAMPLE:443/done?ok=1", "https://app.example"},
		{"https://app.example/done", "https://app.example:443/base"},
		{"http://127.0.0.1:9118/cb", "http://127.0.0.1:9118"},
		{"http://localhost:80/cb", "http://localhost"},
	}
	for _, tc := range cases {
		if err := billing.ValidateRedirectURL(tc[0], tc[1]); err != nil {
			t.Fatalf("expected ValidateRedirectURL(%q, %q) to succeed, got %v", tc[0], tc[1], err)
		}
	}
}
'''
    (ws / "e2eharness" / "f7_redirect_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestValidateRedirectURLSameOriginAndDefaultPort")


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f7_t1_stripe_checkout_armed_only_with_return_base_url(
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
                "STRIPE_RETURN_BASE_URL": "http://127.0.0.1:9118",
            },
        )
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            logs = gw.stderr + gw.stdout
            assert "Stripe checkout creator armed" in logs
            assert "Stripe portal creator armed" in logs

            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                portal_resp = client.post(
                    "/api/v1/billing/portal",
                    json={"return_url": "http://127.0.0.1:9118/billing"},
                )
                assert portal_resp.status_code == 409, portal_resp.text
                assert portal_resp.json()["code"] == "no_stripe_customer"


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f7_t1_stripe_portal_same_origin_allowed_and_no_customer_409(
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
                resp = client.post(
                    "/api/v1/billing/portal",
                    json={"return_url": "https://APP.EXAMPLE.COM:443/account/billing"},
                )
                assert resp.status_code == 409, resp.text
                assert resp.json()["code"] == "no_stripe_customer"


@pytest.mark.requires_go
def test_f7_t1_stripe_checkout_valid_tiers_pro_team_enterprise(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/gin-gonic/gin"
)

type fakeUserLookup struct{}

func (fakeUserLookup) GetUser(_ context.Context, id string) (account.User, error) {
	return account.User{ID: id, Email: "u@test", PlanTier: account.PlanFree}, nil
}

type fakeCheckoutCreator struct{}

func (fakeCheckoutCreator) Name() string { return "fake" }

func (fakeCheckoutCreator) Create(_ context.Context, _ account.User, tier account.PlanTier, _, _ string) (string, error) {
	return "https://checkout.stripe.test/pay/" + string(tier), nil
}

func TestCheckoutValidAndDisallowedTiers(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		ctx := account.WithContext(c.Request.Context(), account.UserContext{UserID: "u-1"})
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	})
	r.POST("/checkout", billing.CheckoutHandler(fakeUserLookup{}, fakeCheckoutCreator{}, "https://app.test"))

	for _, tier := range []string{"pro", "team", "enterprise"} {
		body, _ := json.Marshal(map[string]string{
			"tier":        tier,
			"success_url": "https://app.test/ok",
			"cancel_url":  "https://app.test/cancel",
		})
		req := httptest.NewRequest(http.MethodPost, "/checkout", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("tier %q expected 200, got %d: %s", tier, w.Code, w.Body.String())
		}
	}

	for _, badTier := range []string{"free", "comp", "gold"} {
		body, _ := json.Marshal(map[string]string{
			"tier":        badTier,
			"success_url": "https://app.test/ok",
			"cancel_url":  "https://app.test/cancel",
		})
		req := httptest.NewRequest(http.MethodPost, "/checkout", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("tier %q expected 400, got %d: %s", badTier, w.Code, w.Body.String())
		}
	}
}
'''
    (ws / "e2eharness" / "f7_tiers_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestCheckoutValidAndDisallowedTiers")


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f7_t1_stripe_billing_plans_endpoint_canonical_schema(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path, isolated_pg_schema)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                resp = client.get("/api/v1/plans")
                assert resp.status_code == 200, resp.text
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


# =============================================================================
# Feature #8: WebSocket subscribe_simulation ownership enforcement
# =============================================================================


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t1_ws_connect_and_subscribe_owned_simulation(gateway_binary: Path, tmp_path: Path) -> None:
    sim_id = f"sim-owned-{uuid.uuid4().hex[:8]}"
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                sess_resp = client.post(
                    "/api/v1/sessions",
                    json={"agentType": "leadership_planner", "simulation_id": sim_id},
                )
                assert sess_resp.status_code == 201, sess_resp.text

            rclient = redis.Redis(host=rns.host, port=rns.port, password=rns.password or None)
            try:
                with ws_connect(f"{gw.ws_url}/ws") as ws:
                    ws.send(json.dumps({"type": "subscribe_simulation", "simulation_id": sim_id}))
                    time.sleep(0.15)
                    wrapper = gateway_pb2.Wrapper(
                        timestamp=datetime.now(UTC).isoformat(),
                        type="json",
                        event="narrative",
                        simulation_id=sim_id,
                        payload=b'{"text":"owned sim ok"}',
                    )
                    rclient.publish("gateway:broadcast", wrapper.SerializeToString())
                    got = _recv_matching_wrapper(ws, sim_id=sim_id)
                    assert got.payload == b'{"text":"owned sim ok"}'
            finally:
                rclient.close()


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t1_ws_unsubscribe_simulation_stops_delivery(gateway_binary: Path, tmp_path: Path) -> None:
    sim_id = f"sim-unsub-{uuid.uuid4().hex[:8]}"
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                sess_resp = client.post(
                    "/api/v1/sessions",
                    json={"agentType": "leadership_planner", "simulation_id": sim_id},
                )
                assert sess_resp.status_code == 201

            rclient = redis.Redis(host=rns.host, port=rns.port, password=rns.password or None)
            try:
                with ws_connect(f"{gw.ws_url}/ws") as ws:
                    ws.send(json.dumps({"type": "subscribe_simulation", "simulation_id": sim_id}))
                    time.sleep(0.15)
                    w1 = gateway_pb2.Wrapper(
                        timestamp=datetime.now(UTC).isoformat(),
                        type="json",
                        event="narrative",
                        simulation_id=sim_id,
                        payload=b'{"seq":1}',
                    )
                    rclient.publish("gateway:broadcast", w1.SerializeToString())
                    assert _recv_matching_wrapper(ws, sim_id=sim_id).payload == b'{"seq":1}'

                    ws.send(json.dumps({"type": "unsubscribe_simulation", "simulation_id": sim_id}))
                    time.sleep(0.15)
                    w2 = gateway_pb2.Wrapper(
                        timestamp=datetime.now(UTC).isoformat(),
                        type="json",
                        event="narrative",
                        simulation_id=sim_id,
                        payload=b'{"seq":2}',
                    )
                    rclient.publish("gateway:broadcast", w2.SerializeToString())

                    marker = gateway_pb2.Wrapper(
                        timestamp=datetime.now(UTC).isoformat(),
                        type="system_marker",
                        event="system_marker",
                        payload=b"{}",
                    )
                    rclient.publish("gateway:broadcast", marker.SerializeToString())

                    seen_seq2 = False
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        msg = ws.recv(timeout=max(0.05, deadline - time.monotonic()))
                        if isinstance(msg, bytes):
                            got = gateway_pb2.Wrapper()
                            got.ParseFromString(msg)
                            if got.simulation_id == sim_id and got.payload == b'{"seq":2}':
                                seen_seq2 = True
                                break
                            if got.type == "system_marker":
                                break
                    assert not seen_seq2, "Unsubscribed client still received simulation broadcast"
            finally:
                rclient.close()


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t1_ws_connect_with_query_session_and_simulation_owned(gateway_binary: Path, tmp_path: Path) -> None:
    sim_id = f"sim-q-{uuid.uuid4().hex[:8]}"
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                sess_resp = client.post(
                    "/api/v1/sessions",
                    json={"agentType": "leadership_planner", "simulation_id": sim_id},
                )
                assert sess_resp.status_code == 201
                sid = sess_resp.json()["sessionId"]

            rclient = redis.Redis(host=rns.host, port=rns.port, password=rns.password or None)
            try:
                with ws_connect(f"{gw.ws_url}/ws?sessionId={sid}&simulationId={sim_id}") as ws:
                    time.sleep(0.15)
                    wrapper = gateway_pb2.Wrapper(
                        timestamp=datetime.now(UTC).isoformat(),
                        type="json",
                        event="narrative",
                        simulation_id=sim_id,
                        session_id=sid,
                        destination=[sid],
                        payload=b'{"targeted":true}',
                    )
                    rclient.publish("gateway:broadcast", wrapper.SerializeToString())
                    got = _recv_matching_wrapper(ws, sim_id=sim_id)
                    assert got.payload == b'{"targeted":true}'
            finally:
                rclient.close()


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t1_ws_global_observer_connect_receives_system_broadcast(gateway_binary: Path, tmp_path: Path) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with ws_connect(f"{gw.ws_url}/ws") as ws:
                with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                    reset_resp = client.post("/api/v1/environment/reset", json={"targets": ["sessions"]})
                    assert reset_resp.status_code == 200, reset_resp.text
                got = _recv_matching_wrapper(ws, event_type="environment_reset")
                assert got.type == "environment_reset"
                assert got.event == "environment_reset"


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f8_t1_ws_malformed_binary_proto_emits_gateway_error_frame(gateway_binary: Path, tmp_path: Path) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                sess_resp = client.post(
                    "/api/v1/sessions",
                    json={"agentType": "leadership_planner"},
                )
                assert sess_resp.status_code == 201
                sid = sess_resp.json()["sessionId"]

            with ws_connect(f"{gw.ws_url}/ws?sessionId={sid}") as ws:
                ws.send(b"\xff\xff\xff")
                frame = _recv_json_error_frame(ws)
                assert frame["origin"] == {"type": "system", "id": "gateway"}
                assert frame["status"] == "error"
                assert frame["type"] == "error"
                assert frame["event"] == "bad_request"


# =============================================================================
# Feature #9: Go handler explicit error propagation & 9101 port
# =============================================================================


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f9_t1_me_and_me_init_and_export_lifecycle(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    email = f"f9-lifecycle-{uuid.uuid4().hex[:8]}@local.test"
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path, isolated_pg_schema, {"LOCAL_DEV_USER_EMAIL": email})
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                me1 = client.get("/api/v1/me")
                assert me1.status_code == 200
                assert me1.json()["needs_init"] is True

                init1 = client.post("/api/v1/me/init", json={"display_name": "Ada Lovelace", "timezone": "UTC"})
                assert init1.status_code == 201, init1.text
                assert init1.json()["created"] is True
                assert init1.json()["display_name"] == "Ada Lovelace"

                init2 = client.post("/api/v1/me/init", json={"display_name": "Ignored", "timezone": "UTC"})
                assert init2.status_code == 200
                assert init2.json()["created"] is False

                me2 = client.get("/api/v1/me")
                assert me2.status_code == 200
                assert me2.json()["needs_init"] is False

                exp = client.get("/api/v1/me/export")
                assert exp.status_code == 200, exp.text
                assert "attachment;" in exp.headers.get("content-disposition", "")
                exp_json = exp.json()
                assert exp_json["user"]["Email"] == email
                assert isinstance(exp_json["audit"], list)


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f9_t1_admin_superadmin_gdpr_export_and_usage(
    gateway_binary: Path,
    admin_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    email = f"f9-admin-target-{uuid.uuid4().hex[:8]}@local.test"
    uid = _local_dev_uid(email)
    with _isolated_redis() as rns:
        gw_env = _gateway_env(rns, tmp_path, isolated_pg_schema, {"LOCAL_DEV_USER_EMAIL": email})
        admin_env = {
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "REDIS_ADDR": rns.addr,
            "REDIS_PASSWORD": rns.password,
        }
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw:
            assert _init_local_user(gw.base_url, display_name="Target User").status_code == 201
            admin_env["GATEWAY_INTERNAL_URL"] = gw.base_url
            with EphemeralGoServer(admin_binary, env=admin_env) as adm:
                headers = {"X-Local-Dev-Superadmin": "1"}
                with httpx.Client(base_url=adm.base_url, timeout=10.0) as client:
                    exp = client.get(f"/admin/users/{uid}/export", headers=headers)
                    assert exp.status_code == 200, exp.text
                    exp_json = exp.json()
                    assert exp_json["user"]["ID"] == uid
                    assert "exported_at" in exp_json
                    assert isinstance(exp_json["audit"], list)

                    usage = client.get(f"/admin/users/{uid}/usage", headers=headers)
                    assert usage.status_code == 200, usage.text
                    usage_json = usage.json()
                    assert usage_json["user_id"] == uid
                    assert usage_json["total_tokens"] == 0
                    assert usage_json["agents"] == []


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f9_t1_admin_environment_reset_bff_proxies_to_gateway(
    gateway_binary: Path,
    admin_binary: Path,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        gw_env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw:
            admin_env = {
                "REDIS_ADDR": rns.addr,
                "REDIS_PASSWORD": rns.password,
                "GATEWAY_INTERNAL_URL": gw.base_url,
            }
            with EphemeralGoServer(admin_binary, env=admin_env) as adm:
                with httpx.Client(base_url=adm.base_url, timeout=10.0) as client:
                    resp = client.post(
                        "/api/admin/environment/reset",
                        headers={"X-Local-Dev-Superadmin": "1"},
                        json={"targets": ["sessions", "queues"]},
                    )
                    assert resp.status_code == 200, resp.text
                    body = resp.json()
                    assert body["status"] == "reset"
                    assert body["results"]["sessions"]["flushed"] is True
                    assert body["results"]["queues"]["flushed"] is True


@pytest.mark.requires_go
@pytest.mark.requires_postgres
@pytest.mark.requires_redis
def test_f9_t1_memory_config_and_small_upload_chunk_retrieve(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path, isolated_pg_schema)
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            assert _init_local_user(gw.base_url).status_code == 201
            with httpx.Client(base_url=gw.base_url, timeout=10.0) as client:
                cfg_resp = client.get("/api/v1/memory/config")
                assert cfg_resp.status_code == 200, cfg_resp.text
                cfg = cfg_resp.json()
                assert cfg["text_readable_kinds"] == ["memo", "note", "plan", "postmortem", "transcript"]
                assert cfg["max_upload_bytes"] > 0

                an_resp = client.post(
                    "/api/v1/memory/analyses",
                    json={"title": "Q4 Strategy", "org_name": "Acme", "description": "Initial"},
                )
                assert an_resp.status_code == 201, an_resp.text
                analysis_id = an_resp.json()["id"]

                up_resp = client.post(
                    f"/api/v1/memory/analyses/{analysis_id}/documents",
                    data={"kind": "memo"},
                    files={"file": ("memo.txt", b"Executive alignment memo content.", "text/plain")},
                )
                assert up_resp.status_code == 201, up_resp.text
                doc_id = up_resp.json()["document"]["id"]

                list_resp = client.get(f"/api/v1/memory/analyses/{analysis_id}/documents")
                assert list_resp.status_code == 200
                assert any(d["id"] == doc_id for d in list_resp.json()["documents"])


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f9_t1_ai_settings_get_put_test_against_mock_ollama(
    gateway_binary: Path,
    mock_ollama: MockOllamaServer,
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "ai_settings.json"
    with _isolated_redis() as rns:
        env = _gateway_env(rns, tmp_path, extra={"AI_SETTINGS_PATH": str(settings_path)})
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
                assert put_resp.status_code == 200, put_resp.text

                test_resp = client.post("/api/v1/settings/ai/test", json={})
                assert test_resp.status_code == 200, test_resp.text
                tbody = test_resp.json()
                assert tbody["reachable"] is True
                assert "gemma2:9b" in tbody["installed_models"]

                get_resp = client.get("/api/v1/settings/ai")
                assert get_resp.status_code == 200
                assert get_resp.json()["endpoint"] == mock_ollama.base_url

    assert settings_path.is_file()


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f9_t1_frontend_and_tester_proxies_forward_api_and_ws(
    gateway_binary: Path,
    frontend_binary: Path,
    tester_binary: Path,
    tmp_path: Path,
) -> None:
    with _isolated_redis() as rns:
        gw_env = _gateway_env(rns, tmp_path)
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw:
            proxy_env = {"GATEWAY_INTERNAL_URL": gw.base_url}
            with (
                EphemeralGoServer(frontend_binary, env=proxy_env) as fe,
                EphemeralGoServer(tester_binary, env=proxy_env) as te,
            ):
                for proxy in (fe, te):
                    with httpx.Client(base_url=proxy.base_url, timeout=10.0) as client:
                        me_resp = client.get("/api/v1/me")
                        assert me_resp.status_code == 200, f"{proxy.binary_path.name} /api/v1/me: {me_resp.text}"
                        assert "needs_init" in me_resp.json()

                    with ws_connect(f"{proxy.ws_url}/ws") as ws:
                        with httpx.Client(base_url=gw.base_url, timeout=10.0) as gw_client:
                            assert gw_client.post("/api/v1/environment/reset", json={"targets": ["queues"]}).status_code == 200
                        got = _recv_matching_wrapper(ws, event_type="environment_reset")
                        assert got.event == "environment_reset"


@pytest.mark.requires_go
@pytest.mark.requires_postgres
def test_f9_t1_billing_webhook_applied_and_deduped_and_healthz_200(
    go_bin_dir: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
) -> None:
    billing_bin = _build_billing_binary(go_bin_dir)
    secret = "whsec_e2e_test_secret"
    uid = _local_dev_uid("f9-billing@local.test")

    env = {
        "DATABASE_URL": isolated_pg_schema.search_path_dsn,
        "BILLING_PROVIDER": "stripe",
        "STRIPE_WEBHOOK_SECRET": secret,
        "STRIPE_PRICE_MAP": "price_pro=pro",
    }
    with EphemeralGoServer(billing_bin, env=env, health_path="/healthz") as bill:
        with psycopg2.connect(isolated_pg_schema.search_path_dsn) as pconn, pconn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, display_name, plan_tier, status) VALUES (%s, %s, %s, 'free', 'approved')",
                (uid, "f9-billing@local.test", "Billing User"),
            )
            pconn.commit()

        event_payload = json.dumps(
            {
                "id": "evt_test_checkout_1",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "customer": "cus_test_1",
                        "metadata": {"user_id": uid, "price_id": "price_pro"},
                    }
                },
            }
        ).encode("utf-8")
        sig = _stripe_sign(secret, event_payload)

        with httpx.Client(base_url=bill.base_url, timeout=10.0) as client:
            hz = client.get("/healthz")
            assert hz.status_code == 200

            r1 = client.post("/webhook", content=event_payload, headers={"Stripe-Signature": sig})
            assert r1.status_code == 200, r1.text
            assert r1.json()["status"] == "applied"
            assert r1.json()["plan_tier"] == "pro"

            r2 = client.post("/webhook", content=event_payload, headers={"Stripe-Signature": sig})
            assert r2.status_code == 200, r2.text
            assert r2.json()["status"] == "deduped"


# =============================================================================
# Feature #10: Go internal stores, hub & middleware contracts
# =============================================================================


@pytest.mark.requires_go
@pytest.mark.requires_postgres
def test_f10_t1_account_store_audit_json_roundtrip_and_superadmin_guard(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"os"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestAccountStoreAuditAndSuperadminGuard(t *testing.T) {
	ctx := context.Background()
	dsn := os.Getenv("TEST_DSN")
	migConn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("connect mig: %v", err)
	}
	if err := account.ApplyMigration(ctx, migConn); err != nil {
		_ = migConn.Close(ctx)
		t.Fatalf("ApplyMigration: %v", err)
	}
	_ = migConn.Close(ctx)

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("pgxpool: %v", err)
	}
	defer pool.Close()

	store := account.NewStore(pool)
	if _, _, err := store.CreateUser(ctx, "u1", "a1@test", "Admin One"); err != nil {
		t.Fatalf("CreateUser u1: %v", err)
	}
	if _, _, err := store.CreateUser(ctx, "u2", "a2@test", "Admin Two"); err != nil {
		t.Fatalf("CreateUser u2: %v", err)
	}
	if err := store.RecordAudit(ctx, account.AuditEntry{
		UserID: "u1",
		Action: "test.action",
		Target: "target-1",
		Metadata: map[string]any{
			"nested": map[string]any{"count": float64(3)},
			"flag":   true,
		},
	}); err != nil {
		t.Fatalf("RecordAudit: %v", err)
	}
	audits, err := store.ListAudit(ctx, "u1", "test.action", 10, 0)
	if err != nil || len(audits) != 1 {
		t.Fatalf("ListAudit got len=%d err=%v", len(audits), err)
	}
	nested, ok := audits[0].Metadata["nested"].(map[string]any)
	if !ok || nested["count"] != float64(3) {
		t.Fatalf("unexpected audit metadata: %#v", audits[0].Metadata)
	}

	if err := store.SetSuperadmin(ctx, "a1@test", true); err != nil {
		t.Fatalf("promote a1: %v", err)
	}
	if err := store.SetSuperadmin(ctx, "a2@test", true); err != nil {
		t.Fatalf("promote a2: %v", err)
	}
	if err := store.SetSuperadmin(ctx, "a1@test", false); err != nil {
		t.Fatalf("demote a1 while a2 remains superadmin: %v", err)
	}
}
'''
    (ws / "e2eharness" / "f10_acct_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestAccountStoreAuditAndSuperadminGuard", env={"TEST_DSN": isolated_pg_schema.search_path_dsn})


@pytest.mark.requires_go
@pytest.mark.requires_postgres
def test_f10_t1_memory_store_update_analysis_clears_optional_fields(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"os"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/memory"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestMemoryStoreUpdateAnalysisClearsOptionalFields(t *testing.T) {
	ctx := context.Background()
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

	acct := account.NewStore(pool)
	if _, _, err := acct.CreateUser(ctx, "op-1", "op1@test", "Op 1"); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}

	store := memory.NewStore(pool)
	created, err := store.CreateAnalysis(ctx, memory.CreateAnalysisInput{
		OperatorID:  "op-1",
		OrgName:     "Acme Corp",
		Title:       "Initial Title",
		Description: "Initial Desc",
	})
	if err != nil {
		t.Fatalf("CreateAnalysis: %v", err)
	}

	empty := ""
	updated, err := store.UpdateAnalysis(ctx, "op-1", created.ID, memory.UpdateAnalysisInput{
		Description: &empty,
		OrgName:     &empty,
	})
	if err != nil {
		t.Fatalf("UpdateAnalysis: %v", err)
	}
	if updated.OrgName != "" || updated.Description != "" {
		t.Fatalf("expected cleared fields, got org=%q desc=%q", updated.OrgName, updated.Description)
	}

	var orgIsNull, descIsNull bool
	if err := pool.QueryRow(ctx, "SELECT org_name IS NULL, description IS NULL FROM analyses WHERE id = $1", created.ID).Scan(&orgIsNull, &descIsNull); err != nil {
		t.Fatalf("null check query: %v", err)
	}
	if !orgIsNull || !descIsNull {
		t.Fatalf("expected SQL NULLs, got orgIsNull=%v descIsNull=%v", orgIsNull, descIsNull)
	}
}
'''
    (ws / "e2eharness" / "f10_mem_update_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestMemoryStoreUpdateAnalysisClearsOptionalFields", env={"TEST_DSN": isolated_pg_schema.search_path_dsn})


@pytest.mark.requires_go
@pytest.mark.requires_postgres
def test_f10_t1_memory_store_3072_vector_store_and_retrieve(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"os"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/memory"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestMemoryStore3072VectorRetrieve(t *testing.T) {
	ctx := context.Background()
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

	acct := account.NewStore(pool)
	if _, _, err := acct.CreateUser(ctx, "op-vec", "opvec@test", "Op Vec"); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}

	store := memory.NewStore(pool)
	an, err := store.CreateAnalysis(ctx, memory.CreateAnalysisInput{OperatorID: "op-vec", Title: "Vec Test"})
	if err != nil {
		t.Fatalf("CreateAnalysis: %v", err)
	}
	doc, err := store.AddDocument(ctx, "op-vec", an.ID, memory.AddDocumentInput{
		Kind: "memo", Filename: "doc.txt", SHA256: "abc", StorageURI: "file:///tmp/doc.txt", ByteSize: 10,
	})
	if err != nil {
		t.Fatalf("AddDocument: %v", err)
	}

	v0 := make([]float32, memory.TargetVectorDimension)
	v0[0] = 1.0
	v1 := make([]float32, memory.TargetVectorDimension)
	v1[1] = 1.0
	if err := store.ReplaceChunksForDocument(ctx, doc.ID, []memory.StoreChunkInput{
		{AnalysisID: an.ID, DocumentID: doc.ID, ChunkIndex: 0, ChunkText: "primary match", Embedding: v0},
		{AnalysisID: an.ID, DocumentID: doc.ID, ChunkIndex: 1, ChunkText: "orthogonal", Embedding: v1},
	}); err != nil {
		t.Fatalf("ReplaceChunksForDocument: %v", err)
	}

	res, err := store.RetrieveChunks(ctx, an.ID, v0, 2)
	if err != nil {
		t.Fatalf("RetrieveChunks: %v", err)
	}
	if len(res) != 2 || res[0].ChunkText != "primary match" {
		t.Fatalf("unexpected chunk results: %#v", res)
	}
}
'''
    (ws / "e2eharness" / "f10_mem_vec_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestMemoryStore3072VectorRetrieve", env={"TEST_DSN": isolated_pg_schema.search_path_dsn})


@pytest.mark.requires_go
@pytest.mark.requires_postgres
def test_f10_t1_billing_store_sargable_agent_breakdown_and_quota(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/billing"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestBillingStoreSargableBreakdownAndQuota(t *testing.T) {
	ctx := context.Background()
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

	acct := account.NewStore(pool)
	if _, _, err := acct.CreateUser(ctx, "u-quota", "q@test", "Quota User"); err != nil {
		t.Fatalf("CreateUser: %v", err)
	}

	bstore := billing.NewStore(pool)
	agentA := "leadership_planner"
	tokNov := int64(300)
	tokDec := int64(900)
	novEnd := time.Date(2026, 11, 30, 23, 59, 59, 0, time.UTC)
	decStart := time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)

	if _, err := bstore.InsertEvent(ctx, billing.Event{
		UserID: "u-quota", AgentType: &agentA, TotalTokens: &tokNov, Timestamp: novEnd,
	}); err != nil {
		t.Fatalf("InsertEvent nov: %v", err)
	}
	if _, err := bstore.InsertEvent(ctx, billing.Event{
		UserID: "u-quota", AgentType: &agentA, TotalTokens: &tokDec, Timestamp: decStart,
	}); err != nil {
		t.Fatalf("InsertEvent dec: %v", err)
	}

	bd, err := bstore.GetAgentBreakdown(ctx, "u-quota", "2026-11")
	if err != nil {
		t.Fatalf("GetAgentBreakdown: %v", err)
	}
	if len(bd) != 1 || bd[0].TotalTokens != 300 || bd[0].EventCount != 1 {
		t.Fatalf("expected only Nov 300 tokens, got %#v", bd)
	}

	now := time.Now().UTC()
	plan, err := bstore.GetUserPlan(ctx, "u-quota")
	if err != nil {
		t.Fatalf("GetUserPlan: %v", err)
	}
	if err := bstore.UpsertMonthlyRollup(ctx, "u-quota", now, (plan.MonthlyTokenCap*85)/100, 10); err != nil {
		t.Fatalf("UpsertMonthlyRollup 85%%: %v", err)
	}
	st1, err := bstore.CheckQuota(ctx, "u-quota")
	if err != nil || !st1.WarnHigh || st1.Exhausted {
		t.Fatalf("expected WarnHigh=true Exhausted=false, got %#v err=%v", st1, err)
	}
	if err := bstore.UpsertMonthlyRollup(ctx, "u-quota", now, plan.MonthlyTokenCap, 10); err != nil {
		t.Fatalf("UpsertMonthlyRollup 100%%: %v", err)
	}
	st2, err := bstore.CheckQuota(ctx, "u-quota")
	if err != nil || !st2.Exhausted {
		t.Fatalf("expected Exhausted=true, got %#v err=%v", st2, err)
	}
}
'''
    (ws / "e2eharness" / "f10_billing_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestBillingStoreSargableBreakdownAndQuota", env={"TEST_DSN": isolated_pg_schema.search_path_dsn})


@pytest.mark.requires_go
def test_f10_t1_switchboard_spawn_queue_namespace_per_user(tmp_path: Path) -> None:
    ws = _setup_temp_go_workspace(tmp_path)
    go_code = '''package e2eharness

import (
	"context"
	"strings"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/hub"
)

func TestSwitchboardSpawnQueueNamespacePerUser(t *testing.T) {
	t.Setenv("SAAS_MODE", "on")
	t.Setenv("ACCOUNT_KEY_PREFIX", "u")

	noUserQ := hub.SpawnQueueNameForContext(context.Background(), "planner", "sess-1", 8)
	if !strings.HasPrefix(noUserQ, "simulation:spawns:planner:") {
		t.Fatalf("expected legacy queue prefix without UserContext, got %q", noUserQ)
	}

	userCtx := account.WithContext(context.Background(), account.UserContext{UserID: "user-42"})
	userQ := hub.SpawnQueueNameForContext(userCtx, "planner", "sess-1", 8)
	if !strings.HasPrefix(userQ, "u:user-42:simulation:spawns:planner:") {
		t.Fatalf("expected user-scoped queue prefix, got %q", userQ)
	}
	if strings.TrimPrefix(userQ, "u:user-42:") != noUserQ {
		t.Fatalf("expected identical shard suffix: userQ=%q noUserQ=%q", userQ, noUserQ)
	}
}
'''
    (ws / "e2eharness" / "f10_spawn_q_test.go").write_text(go_code, encoding="utf-8")
    _run_temp_go_test(ws, "e2eharness", "TestSwitchboardSpawnQueueNamespacePerUser")
