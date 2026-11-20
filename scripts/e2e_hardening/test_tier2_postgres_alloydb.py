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

"""Tier 2 boundary, negative, and error-path tests for Features #1–#5 (Postgres/AlloyDB/Migrations/Seeds)."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import psycopg2
import pytest

from agents.leadership_planner_with_memory.memory import store_alloydb as leadership_store_mod
from agents.leadership_planner_with_memory.memory import tools as leadership_tools
from agents.leadership_planner_with_memory.memory.store_alloydb import (
    AlloyDBScenarioStore,
    _get_dsn,
    _json_dumps,
    _parse_int_env,
    _row_to_scenario,
    _sanitize_score,
    _validate_schema_name,
)
from agents.planner_with_memory.alloydb.seed_routes import seed_routes
from agents.planner_with_memory.memory import embeddings as planner_embeddings_mod
from agents.planner_with_memory.memory import tools as planner_tools
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    EphemeralGoServer,
    IsolatedPostgresSchema,
    free_port,
    run_cmd,
)

pytestmark = pytest.mark.tier2

_MIGRATION_LOCK_KEY = 1431520594  # 0x55534552 ("USER")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_pg_dsn(dsn: str) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(dsn)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": str(parsed.port or 5432),
        "user": urllib.parse.unquote(parsed.username or "postgres"),
        "password": urllib.parse.unquote(parsed.password or ""),
        "database": urllib.parse.unquote((parsed.path or "/postgres").lstrip("/") or "postgres"),
    }


def _configure_alloydb_env(monkeypatch: pytest.MonkeyPatch, pg: IsolatedPostgresSchema) -> None:
    info = _parse_pg_dsn(pg.dsn)
    monkeypatch.setenv("ALLOYDB_HOST", info["host"])
    monkeypatch.setenv("ALLOYDB_PORT", info["port"])
    monkeypatch.setenv("ALLOYDB_USER", info["user"])
    monkeypatch.setenv("ALLOYDB_PASSWORD", info["password"])
    monkeypatch.setenv("ALLOYDB_DATABASE", info["database"])
    monkeypatch.setenv("ALLOYDB_SCHEMA", pg.schema_name)
    monkeypatch.setenv("ALLOYDB_POOL_MIN", "1")
    monkeypatch.setenv("ALLOYDB_POOL_MAX", "2")


def _exec_sql_files(pg: IsolatedPostgresSchema, *rel_paths: str) -> None:
    with psycopg2.connect(pg.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for rel_path in rel_paths:
                sql = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
                cur.execute(sql)


def _build_go_binary_checked(pkg_path: str, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / name
    proc = run_cmd(["go", "build", "-o", str(out_path), pkg_path], cwd=REPO_ROOT, timeout=300)
    assert proc.returncode == 0, (
        f"go build {pkg_path} failed (exit {proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert out_path.is_file() and os.access(out_path, os.X_OK)
    return out_path


def _build_options_dsn(base_dsn: str, schema_name: str, extra_option: str = "") -> str:
    parts = urllib.parse.urlsplit(base_dsn)
    params = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "options"]
    opt_val = f"-csearch_path={schema_name},public"
    if extra_option:
        opt_val = f"{opt_val} {extra_option}"
    params.append(("options", opt_val))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(params), parts.fragment))


def _boot_billing_and_capture(
    billing_bin: Path,
    extra_env: dict[str, str],
    *,
    timeout: float = 8.0,
) -> tuple[int | None, str, str]:
    """Start billing binary with isolated env; return (exit_code_or_None, stdout, stderr)."""
    port = free_port()
    clean_env: dict[str, str] = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP", "GOPATH", "GOMODCACHE", "GOCACHE", "GOROOT")
        if k in os.environ
    }
    for blocked in (
        "DATABASE_URL",
        "ALLOYDB_HOST",
        "ALLOYDB_PORT",
        "ALLOYDB_USER",
        "ALLOYDB_PASSWORD",
        "ALLOYDB_DATABASE",
        "ALLOYDB_SCHEMA",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_MAP",
        "USERS_MIGRATION_PATH",
        "USERS_MIGRATIONS_DIR",
    ):
        clean_env[blocked] = ""
    clean_env["PORT"] = str(port)
    clean_env["IDENTITY_PROVIDER"] = "local"
    clean_env["BILLING_PROVIDER"] = "stub"
    clean_env.update(extra_env)

    proc = subprocess.Popen(
        [str(billing_bin)],
        cwd=str(REPO_ROOT),
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout
    exit_code: int | None = None
    try:
        with httpx.Client(timeout=0.5) as client:
            while time.monotonic() < deadline:
                exit_code = proc.poll()
                if exit_code is not None:
                    break
                try:
                    client.get(f"http://127.0.0.1:{port}/healthz")
                    break
                except httpx.HTTPError:
                    time.sleep(0.1)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
        else:
            exit_code = proc.returncode
        stdout, stderr = proc.communicate(timeout=3.0)
    return exit_code, stdout, stderr


class _MinimalRecordNoGet:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def keys(self) -> list[str]:
        return list(self._data.keys())


def _make_tool_context(state: dict[str, Any] | None = None, user_id: str | None = None) -> Any:
    session = SimpleNamespace(id="e2e-t2-session", user_id=user_id) if user_id is not None else SimpleNamespace(id="e2e-t2-session")
    return SimpleNamespace(state=state if state is not None else {}, session=session)


# ---------------------------------------------------------------------------
# Feature #1: Boundary & error handling for asyncpg.Record & AlloyDBScenarioStore
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_data",
    [None, "{bad-json", '["not-a-dict"]'],
    ids=["null", "invalid-json", "non-dict-json"],
)
def test_f1_row_to_scenario_null_and_corrupt_scenario_data_raises(bad_data: Any) -> None:
    row = _MinimalRecordNoGet(
        {
            "scenario_id": "scn-bad",
            "scenario_data": bad_data,
            "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "eval_score": 50.0,
            "eval_result": None,
        }
    )
    with pytest.raises(ValueError):
        _row_to_scenario(row)  # type: ignore[arg-type]


def test_f1_sanitize_score_and_json_dumps_handle_nan_inf_bool() -> None:
    assert _sanitize_score(float("nan")) is None
    assert _sanitize_score(float("inf")) is None
    assert _sanitize_score(float("-inf")) is None
    assert _sanitize_score(True) is None
    assert _sanitize_score("not-a-number") is None  # type: ignore[arg-type]
    assert _sanitize_score(88) == 88.0

    dumped = _json_dumps({"x": float("nan"), "y": [float("-inf"), 1.5, float("inf")]})
    assert "NaN" not in dumped and "Infinity" not in dumped
    assert json.loads(dumped) == {"x": None, "y": [None, 1.5, None]}


def test_f1_invalid_alloydb_schema_and_missing_host_raise_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOYDB_HOST", raising=False)
    with pytest.raises(ValueError, match="ALLOYDB_HOST"):
        _get_dsn()

    for bad_schema in ("bad; DROP SCHEMA public;--", "1starts_with_digit", "a-b", "x y"):
        with pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"):
            _validate_schema_name(bad_schema)

    monkeypatch.setenv("ALLOYDB_PORT", "99999")
    assert _parse_int_env("ALLOYDB_PORT", 5432, min_value=1, max_value=65535) == 5432
    monkeypatch.setenv("ALLOYDB_PORT", "abc")
    assert _parse_int_env("ALLOYDB_PORT", 5432, min_value=1, max_value=65535) == 5432


@pytest.mark.requires_postgres
async def test_f1_live_cross_tenant_isolation_denies_other_owner(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)

    store = AlloyDBScenarioStore()
    try:
        sid = await store.store_scenario(
            scenario_data={"name": "Private Scenario"},
            evaluation_score=None,
            owner_id="user-a",
        )
        assert await store.get_scenario(sid, owner_id="user-b") is None
        assert await store.record_simulation(sid, {"ok": True}, owner_id="user-b") is None
        assert await store.update_evaluation(sid, 90.0, {"overall_score": 90.0}, owner_id="user-b") is False
        assert await store.store_artifacts(sid, {"stakeholder_graph": {"nodes": []}}, owner_id="user-b") is False
        assert await store.get_artifacts(sid, owner_id="user-b") is None
        assert await store.recall_scenarios(owner_id="user-b") == []
        assert await store.get_best_scenario(owner_id="user-b") is None
    finally:
        await store.close()


@pytest.mark.requires_postgres
async def test_f1_live_record_simulation_nonexistent_parent_returns_none(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)

    store = AlloyDBScenarioStore()
    try:
        res = await store.record_simulation("00000000-0000-0000-0000-000000000000", {"ok": True})
        assert res is None
        assert await store.recall_scenarios(count=0) == []
        assert await store.list_scenario_bundles(limit=0) == []
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Feature #2: Schema drift, pgvector dims, FK cascades, pre-connect validation
# ---------------------------------------------------------------------------


def test_f2_no_missing_owner_id_or_uq_rules_drift_in_schemas() -> None:
    marathon_schemas = [
        REPO_ROOT / "agents/planner_with_memory/alloydb/schema.sql",
        REPO_ROOT / "agents/planner_with_memory/alloydb/schema_local.sql",
        REPO_ROOT / "scripts/postgres-init/01_schema.sql",
        REPO_ROOT / "infra/modules/cloud-sql-postgres/schema_local.sql",
    ]
    for path in marathon_schemas:
        txt = path.read_text(encoding="utf-8")
        assert re.search(r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+uq_rules_source_chunk", txt), path.name
        assert re.search(r"planned_routes[\s\S]*owner_id\s+VARCHAR\(64\)", txt), path.name
        assert re.search(r"simulation_summaries[\s\S]*owner_id\s+VARCHAR\(64\)", txt), path.name

    leadership_schemas = [
        REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/schema.sql",
        REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/schema_local.sql",
        REPO_ROOT / "scripts/postgres-init/03_leadership_schema.sql",
        REPO_ROOT / "infra/modules/cloud-sql-postgres/schema_local.sql",
    ]
    for path in leadership_schemas:
        txt = path.read_text(encoding="utf-8")
        assert re.search(r"leadership_scenarios[\s\S]*owner_id\s+VARCHAR\(64\)", txt), path.name
        assert re.search(r"leadership_stakeholder_artifacts[\s\S]*owner_id\s+VARCHAR\(64\)", txt), path.name


@pytest.mark.requires_postgres
async def test_f2_live_wrong_vector_dimension_rejected_by_pgvector(
    isolated_pg_schema: IsolatedPostgresSchema,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/01_schema.sql")
    bad_vec_768 = "[" + ",".join(["0.1"] * 768) + "]"
    conn = await asyncpg.connect(
        isolated_pg_schema.dsn,
        server_settings={"search_path": f"{isolated_pg_schema.schema_name}, public"},
    )
    try:
        with pytest.raises(asyncpg.DataError):
            await conn.execute(
                "INSERT INTO rules (source_file, chunk_id, city, text, embedding) VALUES ($1, $2, $3, $4, $5::vector)",
                "BAD.txt",
                1,
                "Las Vegas",
                "Wrong dims",
                bad_vec_768,
            )
    finally:
        await conn.close()


@pytest.mark.requires_postgres
def test_f2_live_foreign_key_cascade_deletes_children(isolated_pg_schema: IsolatedPostgresSchema) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO leadership_scenarios (scenario_id, scenario_data) VALUES ('scn-cas', '{\"name\":\"C\"}')"
            )
            cur.execute(
                "INSERT INTO leadership_simulation_records (simulation_id, scenario_id, sim_result) "
                "VALUES ('sim-cas', 'scn-cas', '{\"ok\":true}')"
            )
            cur.execute(
                "INSERT INTO leadership_stakeholder_artifacts (scenario_id, stakeholder_graph) "
                "VALUES ('scn-cas', '{\"nodes\":[]}')"
            )
            cur.execute("DELETE FROM leadership_scenarios WHERE scenario_id = 'scn-cas'")
            cur.execute("SELECT COUNT(*) FROM leadership_simulation_records WHERE scenario_id = 'scn-cas'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM leadership_stakeholder_artifacts WHERE scenario_id = 'scn-cas'")
            assert cur.fetchone()[0] == 0


async def test_f2_invalid_schema_name_rejected_before_pool_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
    monkeypatch.setenv("ALLOYDB_PASSWORD", "pw")
    monkeypatch.setenv("ALLOYDB_SCHEMA", "public; DROP TABLE users")
    monkeypatch.setattr(leadership_store_mod, "_pool", None)
    monkeypatch.setattr(leadership_store_mod, "_pool_lock", None)
    monkeypatch.setattr(leadership_store_mod, "_pool_loop", None)

    store = AlloyDBScenarioStore()
    with pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"):
        await store.recall_scenarios()


@pytest.mark.requires_postgres
def test_f2_rules_table_rejects_duplicate_source_chunk_without_conflict_clause(
    isolated_pg_schema: IsolatedPostgresSchema,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/01_schema.sql")
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rules (source_file, chunk_id, city, text) VALUES ('R.txt', 42, 'NYC', 'First')"
            )
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO rules (source_file, chunk_id, city, text) VALUES ('R.txt', 42, 'NYC', 'Dup')"
                )


# ---------------------------------------------------------------------------
# Feature #3: Seed owner_id conflict, repair, idempotency & FK boundaries
# ---------------------------------------------------------------------------


def test_f3_seed_files_have_no_null_owner_id_inserts() -> None:
    for rel in (
        "scripts/postgres-init/04_leadership_demo_fixtures.sql",
        "agents/leadership_planner_with_memory/alloydb/seed_local.sql",
    ):
        txt = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "INSERT INTO leadership_scenarios (scenario_id, scenario_data, created_at, eval_score, eval_result, owner_id)" in txt
        assert "'__seed__'" in txt
        assert "COALESCE(leadership_scenarios.owner_id, EXCLUDED.owner_id)" in txt

    fixtures = (REPO_ROOT / "scripts/postgres-init/04_leadership_demo_fixtures.sql").read_text(encoding="utf-8")
    assert "INSERT INTO leadership_stakeholder_artifacts (scenario_id, stakeholder_graph, owner_id)" in fixtures
    assert "COALESCE(leadership_stakeholder_artifacts.owner_id, EXCLUDED.owner_id)" in fixtures


@pytest.mark.requires_postgres
def test_f3_live_preexisting_null_artifact_owner_repaired_by_06_seed_local(
    isolated_pg_schema: IsolatedPostgresSchema,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for i in range(1, 5):
                sid = f"seed-leadership-00{i}"
                cur.execute(
                    "INSERT INTO leadership_scenarios (scenario_id, scenario_data, owner_id) VALUES (%s, %s::jsonb, NULL)",
                    (sid, json.dumps({"name": f"Legacy {i}"})),
                )
                cur.execute(
                    "INSERT INTO leadership_stakeholder_artifacts (scenario_id, stakeholder_graph, owner_id) "
                    "VALUES (%s, '{\"nodes\":[]}'::jsonb, NULL)",
                    (sid,),
                )

    _exec_sql_files(isolated_pg_schema, "agents/leadership_planner_with_memory/alloydb/seed_local.sql")
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM leadership_scenarios WHERE owner_id = '__seed__'")
            assert cur.fetchone()[0] == 4
            cur.execute("SELECT COUNT(*) FROM leadership_stakeholder_artifacts WHERE owner_id = '__seed__'")
            assert cur.fetchone()[0] == 4


@pytest.mark.requires_postgres
def test_f3_live_reapplying_seed_sql_is_idempotent_no_duplicate_rows(
    isolated_pg_schema: IsolatedPostgresSchema,
) -> None:
    _exec_sql_files(
        isolated_pg_schema,
        "scripts/postgres-init/03_leadership_schema.sql",
        "scripts/postgres-init/04_leadership_demo_fixtures.sql",
        "agents/leadership_planner_with_memory/alloydb/seed_local.sql",
        "scripts/postgres-init/04_leadership_demo_fixtures.sql",
        "agents/leadership_planner_with_memory/alloydb/seed_local.sql",
    )
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM leadership_scenarios")
            assert cur.fetchone()[0] == 4
            cur.execute("SELECT COUNT(*) FROM leadership_stakeholder_artifacts")
            assert cur.fetchone()[0] == 4


@pytest.mark.requires_postgres
async def test_f3_seed_routes_py_rejects_bad_schema_and_tags_seed_owner(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/01_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)

    monkeypatch.setenv("ALLOYDB_SCHEMA", "bad;drop")
    with pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"):
        await seed_routes()

    monkeypatch.setenv("ALLOYDB_SCHEMA", isolated_pg_schema.schema_name)
    loaded = await seed_routes()
    assert loaded >= 1
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE owner_id = '__seed__') FROM planned_routes")
            total, seeded = cur.fetchone()
            assert total == loaded and total == seeded


@pytest.mark.requires_postgres
def test_f3_live_fk_blocks_orphan_seed_artifact_insert(isolated_pg_schema: IsolatedPostgresSchema) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur.execute(
                    "INSERT INTO leadership_stakeholder_artifacts (scenario_id, stakeholder_graph, owner_id) "
                    "VALUES ('missing-seed-id', '{\"nodes\":[]}'::jsonb, '__seed__')"
                )


# ---------------------------------------------------------------------------
# Feature #4: Fail-closed deploy_alloydb.sh & explicit tool errors
# ---------------------------------------------------------------------------


def test_f4_deploy_alloydb_fails_closed_on_missing_or_whitespace_password(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("psql", "exit 0\n"),
        ("uv", "exit 0\n"),
        ("gcloud", 'if [[ "$1" == "config" ]]; then echo ""; exit 0; fi\nexit 1\n'),
    ):
        p = bin_dir / name
        p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    base_env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "ALLOYDB_HOST": "10.0.0.1"}

    # Missing password + empty gcloud project -> fail closed
    proc_missing = run_cmd(
        ["bash", str(REPO_ROOT / "agents/planner_with_memory/alloydb/deploy_alloydb.sh")],
        env=base_env,
        timeout=30,
    )
    assert proc_missing.returncode != 0
    assert "DevPassword123!" not in proc_missing.stderr + proc_missing.stdout
    assert "ALLOYDB_PASSWORD" in proc_missing.stderr

    # Whitespace-only password -> fail closed
    proc_ws = run_cmd(
        ["bash", str(REPO_ROOT / "agents/planner_with_memory/alloydb/deploy_alloydb.sh")],
        env={**base_env, "ALLOYDB_PASSWORD": "   \t  "},
        timeout=30,
    )
    assert proc_ws.returncode != 0
    assert "whitespace" in proc_ws.stderr.lower()


def test_f4_deploy_alloydb_fails_closed_on_invalid_schema_or_whitespace_host(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("psql", "gcloud", "uv"):
        p = bin_dir / name
        p.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    base_env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "ALLOYDB_PASSWORD": "pw"}

    proc_bad_schema = run_cmd(
        ["bash", str(REPO_ROOT / "agents/planner_with_memory/alloydb/deploy_alloydb.sh")],
        env={**base_env, "ALLOYDB_HOST": "10.0.0.1", "ALLOYDB_SCHEMA": "bad;schema"},
        timeout=30,
    )
    assert proc_bad_schema.returncode != 0
    assert "Invalid ALLOYDB_SCHEMA" in proc_bad_schema.stderr

    proc_ws_host = run_cmd(
        ["bash", str(REPO_ROOT / "agents/planner_with_memory/alloydb/deploy_alloydb.sh")],
        env={**base_env, "ALLOYDB_HOST": "   "},
        timeout=30,
    )
    assert proc_ws_host.returncode != 0
    assert "ALLOYDB_HOST" in proc_ws_host.stderr


async def test_f4_planner_tools_missing_host_and_local_mode_return_explicit_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = _make_tool_context(state={})
    monkeypatch.setenv("AI_SETTINGS_PATH", str(tmp_path / "no_ai_settings.json"))
    monkeypatch.setenv("AI_PROVIDER", "cloud_gemini")
    monkeypatch.delenv("ALLOYDB_HOST", raising=False)
    monkeypatch.delenv("LOCAL_AI_MODE", raising=False)
    monkeypatch.delenv("USE_LOCAL_AI", raising=False)

    r1 = await planner_tools.get_local_and_traffic_rules(query="permits", tool_context=ctx)
    r2 = await planner_tools.recall_past_simulations(query="vegas", tool_context=ctx)
    assert r1["status"] == "error" and "ALLOYDB_HOST" in r1["message"]
    assert r2["status"] == "error" and "ALLOYDB_HOST" in r2["message"]
    assert "rules" not in r1 and "simulations" not in r2

    monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
    monkeypatch.setenv("ALLOYDB_PASSWORD", "test-pw")
    monkeypatch.setenv("USE_ALLOYDB", "false")
    monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")
    r3 = await planner_tools.get_local_and_traffic_rules(query="permits", tool_context=ctx)
    r4 = await planner_tools.recall_past_simulations(query="vegas", tool_context=ctx)
    assert r3["status"] == "error" and "ai.embedding()" in r3["message"]
    assert r4["status"] == "error" and "ai.embedding()" in r4["message"]


async def test_f4_planner_tools_unreachable_db_returns_explicit_error_not_sample(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed_port = free_port()
    monkeypatch.setenv("AI_SETTINGS_PATH", str(tmp_path / "no_ai_settings.json"))
    monkeypatch.setenv("AI_PROVIDER", "cloud_gemini")
    monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
    monkeypatch.setenv("ALLOYDB_PORT", str(closed_port))
    monkeypatch.setenv("ALLOYDB_USER", "postgres")
    monkeypatch.setenv("ALLOYDB_PASSWORD", "x")
    monkeypatch.setenv("ALLOYDB_DATABASE", "postgres")
    monkeypatch.setenv("USE_ALLOYDB", "true")
    monkeypatch.setenv("EMBEDDING_BACKEND", "vertex_ai")
    monkeypatch.delenv("LOCAL_AI_MODE", raising=False)
    monkeypatch.delenv("USE_LOCAL_AI", raising=False)

    async def _fake_embed(_text: str, *, dimension: int = 3072) -> list[float]:
        return [0.1] * dimension

    monkeypatch.setattr(planner_embeddings_mod, "compute_embedding", _fake_embed)

    ctx = _make_tool_context(state={})
    r1 = await planner_tools.get_local_and_traffic_rules(query="permits", tool_context=ctx)
    r2 = await planner_tools.recall_past_simulations(query="vegas", tool_context=ctx)
    assert r1["status"] == "error"
    assert r2["status"] == "error"
    assert "Sample" not in r1["message"]
    assert "simulations" not in r2


async def test_f4_leadership_tools_refuse_unscored_and_surface_store_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unscored / zero / NaN evaluation must return needs_evaluation
    for bad_eval in (None, {}, {"overall_score": 0}, {"overall_score": float("nan")}):
        ctx = _make_tool_context(
            state={"leadership_scenario": {"name": "Draft"}, "leadership_evaluation": bad_eval}
        )
        res = await leadership_tools.store_scenario(ctx)
        assert res["status"] == "needs_evaluation"

    # Store exception in get_selected_scenarios must surface status="error" + failed_ids (not "none_selected")
    class _FailingStore:
        async def get_scenario(self, scenario_id: str, owner_id: str | None = None) -> None:
            raise RuntimeError(f"boom for {scenario_id}")

    monkeypatch.setattr(leadership_tools, "_store", _FailingStore())
    ctx2 = _make_tool_context(state={})
    sel = await leadership_tools.get_selected_scenarios(ctx2, scenario_ids=["scn-1", "scn-2"])
    assert sel["status"] == "error"
    assert sel["failed_ids"] == ["scn-1", "scn-2"]
    assert sel["scenarios"] == []


# ---------------------------------------------------------------------------
# Feature #5: Go ApplyMigration fail-closed error handling & boundaries
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_billing_fails_migration_on_empty_migrations_dir(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path / "bin", "billing")
    empty_dir = tmp_path / "empty_migrations"
    empty_dir.mkdir()

    _, _, stderr = _boot_billing_and_capture(
        billing_bin,
        {
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "USERS_MIGRATIONS_DIR": str(empty_dir),
            "USERS_MIGRATION_PATH": "",
        },
    )
    assert "no *.sql files in migrations dir" in stderr

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'schema_migrations'",
                (isolated_pg_schema.schema_name,),
            )
            assert cur.fetchone() is None


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_billing_stops_on_broken_migration_and_releases_advisory_lock(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path / "bin", "billing")
    mig_dir = tmp_path / "broken_migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_ok.sql").write_text("CREATE TABLE IF NOT EXISTS m_probe (id INT);", encoding="utf-8")
    (mig_dir / "0002_bad.sql").write_text("INVALID SQL SYNTAX HERE;", encoding="utf-8")

    _, _, stderr = _boot_billing_and_capture(
        billing_bin,
        {
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "USERS_MIGRATIONS_DIR": str(mig_dir),
            "USERS_MIGRATION_PATH": "",
        },
    )
    assert "0002_bad.sql" in stderr

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            assert [r[0] for r in cur.fetchall()] == ["0001_ok.sql"]

            # Advisory lock must not be stranded after migration failure.
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
            acquired = cur.fetchone()[0]
            assert acquired is True
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_live_advisory_lock_blocks_and_fails_closed_on_timeout(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path / "bin", "billing")
    timeout_dsn = _build_options_dsn(
        isolated_pg_schema.dsn,
        isolated_pg_schema.schema_name,
        extra_option="-cstatement_timeout=200ms",
    )

    with psycopg2.connect(isolated_pg_schema.dsn) as lock_conn:
        lock_conn.autocommit = True
        with lock_conn.cursor() as lock_cur:
            lock_cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
            try:
                _, _, stderr = _boot_billing_and_capture(
                    billing_bin,
                    {
                        "DATABASE_URL": timeout_dsn,
                        "USERS_MIGRATIONS_DIR": str(REPO_ROOT / "migrations"),
                        "USERS_MIGRATION_PATH": "",
                    },
                    timeout=8.0,
                )
                assert "acquire advisory lock" in stderr
            finally:
                lock_cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'schema_migrations'",
                (isolated_pg_schema.schema_name,),
            )
            assert cur.fetchone() is None


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_document_chunks_rejects_non_3072_vector_and_cascades_on_user_delete(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path / "bin", "billing")
    with EphemeralGoServer(
        billing_bin,
        env={"DATABASE_URL": isolated_pg_schema.search_path_dsn, "BILLING_PROVIDER": "stub"},
    ):
        pass

    analysis_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    vec_3072 = "[" + ",".join(["0.05"] * 3072) + "]"
    vec_768 = "[" + ",".join(["0.05"] * 768) + "]"

    conn = psycopg2.connect(isolated_pg_schema.search_path_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (id, email) VALUES ('u-cas', 'cascade@example.com')")
            cur.execute(
                "INSERT INTO analyses (id, operator_id, title) VALUES (%s, 'u-cas', 'Cascade Check')",
                (analysis_id,),
            )
            cur.execute(
                "INSERT INTO analysis_documents (id, analysis_id, kind, filename, sha256, storage_uri) "
                "VALUES (%s, %s, 'memo', 'memo.txt', 'abc123', 'local://memo.txt')",
                (doc_id, analysis_id),
            )
            with pytest.raises(psycopg2.errors.DataException):
                cur.execute(
                    "INSERT INTO document_chunks (document_id, analysis_id, chunk_index, chunk_text, embedding) "
                    "VALUES (%s, %s, 0, 'bad dim', %s::vector)",
                    (doc_id, analysis_id, vec_768),
                )

            cur.execute(
                "INSERT INTO document_chunks (document_id, analysis_id, chunk_index, chunk_text, embedding) "
                "VALUES (%s, %s, 0, 'good dim', %s::vector)",
                (doc_id, analysis_id, vec_3072),
            )
            cur.execute("DELETE FROM users WHERE id = 'u-cas'")
            cur.execute("SELECT COUNT(*) FROM document_chunks WHERE analysis_id = %s", (analysis_id,))
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_migrations_reject_whitespace_only_sql_file(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path / "bin", "billing")
    mig_dir = tmp_path / "ws_migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_blank.sql").write_text("   \n\t  \n", encoding="utf-8")

    _, _, stderr = _boot_billing_and_capture(
        billing_bin,
        {
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "USERS_MIGRATIONS_DIR": str(mig_dir),
            "USERS_MIGRATION_PATH": "",
        },
    )
    assert "is empty" in stderr

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            assert cur.fetchone()[0] == 0
