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

"""Tier 1 category-partition functional tests for Features #1–#5 (Postgres/AlloyDB/Migrations/Seeds)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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
    _row_to_scenario,
)
from agents.planner_with_memory.memory import embeddings as planner_embeddings_mod
from agents.planner_with_memory.memory import store_alloydb as planner_store_mod
from agents.planner_with_memory.memory import tools as planner_tools
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    EphemeralGoServer,
    IsolatedPostgresSchema,
    run_cmd,
)

pytestmark = pytest.mark.tier1


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


def _make_tool_context(state: dict[str, Any] | None = None, user_id: str | None = None) -> Any:
    session = SimpleNamespace(id="e2e-session-1", user_id=user_id) if user_id is not None else SimpleNamespace(id="e2e-session-1")
    return SimpleNamespace(state=state if state is not None else {}, session=session)


class _MinimalRecordNoGet:
    """Stub mimicking asyncpg.Record key/index protocol WITHOUT .get()."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def keys(self) -> list[str]:
        return list(self._data.keys())


# ---------------------------------------------------------------------------
# Feature #1: asyncpg.Record.get() runtime crash fix & AlloyDBScenarioStore
# ---------------------------------------------------------------------------


def test_f1_row_to_scenario_with_minimal_record_stub() -> None:
    created = datetime(2026, 6, 1, 12, 0, 0)  # naive UTC -> must normalize to timezone.utc
    row_full = _MinimalRecordNoGet(
        {
            "scenario_id": "scn-001",
            "scenario_data": '{"name": "Board Pivot", "stakeholder_count": 6}',
            "created_at": created,
            "eval_score": 84.5,
            "eval_result": '{"overall_score": 84.5}',
            "persona": "visionary",
            "owner_id": "uid-123",
        }
    )
    assert not hasattr(row_full, "get")
    parsed_full = _row_to_scenario(row_full)  # type: ignore[arg-type]
    assert parsed_full.scenario_id == "scn-001"
    assert parsed_full.scenario_data == {"name": "Board Pivot", "stakeholder_count": 6}
    assert parsed_full.created_at == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parsed_full.evaluation_score == 84.5
    assert parsed_full.evaluation_result == {"overall_score": 84.5}
    assert parsed_full.persona == "visionary"
    assert parsed_full.owner_id == "uid-123"

    # Also verify missing optional keys (persona, owner_id) do not raise AttributeError/KeyError.
    row_min = _MinimalRecordNoGet(
        {
            "scenario_id": "scn-002",
            "scenario_data": {"name": "Legacy Row"},
            "created_at": datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc),
            "eval_score": None,
            "eval_result": None,
        }
    )
    parsed_min = _row_to_scenario(row_min)  # type: ignore[arg-type]
    assert parsed_min.persona is None
    assert parsed_min.owner_id is None
    assert parsed_min.evaluation_score is None
    assert parsed_min.evaluation_result is None


@pytest.mark.requires_postgres
async def test_f1_live_store_and_get_scenario_real_asyncpg_record(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)

    store = AlloyDBScenarioStore()
    try:
        sid = await store.store_scenario(
            scenario_data={"name": "Crisis Rehearsal", "stakeholder_count": 4},
            evaluation_score=88.0,
            evaluation_result={"overall_score": 88.0, "tier": "strong"},
            persona="pragmatist",
            owner_id="user-alpha",
        )
        loaded = await store.get_scenario(sid, owner_id="user-alpha")
        assert loaded is not None
        assert loaded.scenario_id == sid
        assert loaded.scenario_data["name"] == "Crisis Rehearsal"
        assert loaded.evaluation_score == 88.0
        assert loaded.evaluation_result == {"overall_score": 88.0, "tier": "strong"}
        assert loaded.persona == "pragmatist"
        assert loaded.owner_id == "user-alpha"
        assert loaded.simulations == []
        assert loaded.created_at.tzinfo is not None
    finally:
        await store.close()


@pytest.mark.requires_postgres
async def test_f1_live_recall_scenarios_sort_recent_and_best_score(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)

    store = AlloyDBScenarioStore()
    try:
        s1 = await store.store_scenario({"name": "Low"}, evaluation_score=62.0, owner_id="u1")
        s2 = await store.store_scenario({"name": "High"}, evaluation_score=93.0, owner_id="u1")
        s3 = await store.store_scenario({"name": "Mid"}, evaluation_score=77.0, owner_id="u1")
        _other = await store.store_scenario({"name": "OtherTenant"}, evaluation_score=99.0, owner_id="u2")

        by_score = await store.recall_scenarios(count=10, sort_by="best_score", owner_id="u1")
        assert [s.scenario_id for s in by_score] == [s2, s3, s1]
        assert all(s.owner_id == "u1" for s in by_score)

        best = await store.get_best_scenario(owner_id="u1")
        assert best is not None
        assert best.scenario_id == s2
        assert best.evaluation_score == 93.0
    finally:
        await store.close()


@pytest.mark.requires_postgres
async def test_f1_live_record_simulation_and_update_evaluation_coalesce(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)

    store = AlloyDBScenarioStore()
    try:
        sid = await store.store_scenario(
            scenario_data={"name": "Unscored Plan"},
            evaluation_score=None,
            evaluation_result=None,
            owner_id="u-eval",
        )
        sim_id = await store.record_simulation(sid, {"outcome": "aligned", "turns": 5}, owner_id="u-eval")
        assert sim_id is not None

        first_update = await store.update_evaluation(
            sid, evaluation_score=81.5, evaluation_result={"overall_score": 81.5}, owner_id="u-eval"
        )
        assert first_update is True

        # Second update must be a no-op (COALESCE preserves original evaluation audit trail).
        second_update = await store.update_evaluation(
            sid, evaluation_score=40.0, evaluation_result={"overall_score": 40.0}, owner_id="u-eval"
        )
        assert second_update is False

        loaded = await store.get_scenario(sid, owner_id="u-eval")
        assert loaded is not None
        assert loaded.evaluation_score == 81.5
        assert loaded.evaluation_result == {"overall_score": 81.5}
        assert len(loaded.simulations) == 1
        assert loaded.simulations[0].simulation_id == sim_id
        assert loaded.simulations[0].simulation_result == {"outcome": "aligned", "turns": 5}
    finally:
        await store.close()


@pytest.mark.requires_postgres
async def test_f1_live_artifacts_and_list_scenario_bundles(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)

    store = AlloyDBScenarioStore()
    try:
        sid = await store.store_scenario({"name": "Bundle Test"}, evaluation_score=86.0, owner_id="u-art")
        ok1 = await store.store_artifacts(
            sid,
            {"stakeholder_graph": {"nodes": [{"id": "ceo"}]}, "stakeholder_personas": [{"id": "ceo"}]},
            owner_id="u-art",
        )
        ok2 = await store.store_artifacts(
            sid,
            {"simulation_report": {"summary": "consensus reached"}},
            owner_id="u-art",
        )
        assert ok1 is True and ok2 is True

        artifacts = await store.get_artifacts(sid, owner_id="u-art")
        assert artifacts is not None
        assert artifacts["stakeholder_graph"] == {"nodes": [{"id": "ceo"}]}
        assert artifacts["stakeholder_personas"] == [{"id": "ceo"}]
        assert artifacts["simulation_report"] == {"summary": "consensus reached"}

        bundles = await store.list_scenario_bundles(owner_id="u-art", limit=10)
        assert len(bundles) == 1
        assert bundles[0]["scenario_id"] == sid
        assert bundles[0]["eval_score"] == 86.0
        assert bundles[0]["stakeholder_graph"] == {"nodes": [{"id": "ceo"}]}
        assert bundles[0]["simulation_report"] == {"summary": "consensus reached"}
        assert bundles[0]["simulation_timeline"] is None
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Feature #2: AlloyDB & Postgres schema parity
# ---------------------------------------------------------------------------


def test_f2_marathon_schema_files_parity() -> None:
    files = [
        REPO_ROOT / "agents/planner_with_memory/alloydb/schema.sql",
        REPO_ROOT / "agents/planner_with_memory/alloydb/schema_local.sql",
        REPO_ROOT / "scripts/postgres-init/01_schema.sql",
        REPO_ROOT / "infra/modules/cloud-sql-postgres/schema_local.sql",
    ]
    for path in files:
        assert path.is_file(), f"Missing schema file: {path}"
        txt = path.read_text(encoding="utf-8")
        for required_token in (
            "CREATE TABLE IF NOT EXISTS rules",
            "VECTOR(3072)",
            "uq_rules_source_chunk",
            "CREATE TABLE IF NOT EXISTS planned_routes",
            "idx_planned_routes_owner_created",
            "CREATE TABLE IF NOT EXISTS simulation_records",
            "idx_simulation_records_route_id",
            "CREATE TABLE IF NOT EXISTS simulation_summaries",
            "idx_simulation_summaries_owner_created",
            "owner_id",
        ):
            assert required_token in txt, f"{path.name} missing {required_token!r}"


def test_f2_leadership_schema_files_parity() -> None:
    files = [
        REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/schema.sql",
        REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/schema_local.sql",
        REPO_ROOT / "scripts/postgres-init/03_leadership_schema.sql",
        REPO_ROOT / "infra/modules/cloud-sql-postgres/schema_local.sql",
    ]
    for path in files:
        assert path.is_file(), f"Missing leadership schema file: {path}"
        txt = path.read_text(encoding="utf-8")
        for required_token in (
            "CREATE TABLE IF NOT EXISTS leadership_scenarios",
            "CREATE TABLE IF NOT EXISTS leadership_simulation_records",
            "idx_leadership_sim_records_scenario_id",
            "persona",
            "owner_id",
            "idx_leadership_scenarios_owner_created",
            "CREATE TABLE IF NOT EXISTS leadership_stakeholder_artifacts",
            "idx_leadership_artifacts_owner_updated",
        ):
            assert required_token in txt, f"{path} missing {required_token!r}"


def test_f2_alloydb_schema_defaults_to_public_across_modules() -> None:
    assert leadership_store_mod._validate_schema_name(None) == "public"
    assert planner_store_mod._validate_schema_name(None) == "public"

    deploy_sh = (REPO_ROOT / "agents/planner_with_memory/alloydb/deploy_alloydb.sh").read_text(encoding="utf-8")
    assert 'ALLOYDB_SCHEMA="${ALLOYDB_SCHEMA:-public}"' in deploy_sh

    seed_py = (REPO_ROOT / "agents/planner_with_memory/alloydb/seed_routes.py").read_text(encoding="utf-8")
    assert '_validate_schema_name(os.environ.get("ALLOYDB_SCHEMA"), default="public")' in seed_py


@pytest.mark.requires_postgres
def test_f2_live_local_schemas_idempotent_double_apply(isolated_pg_schema: IsolatedPostgresSchema) -> None:
    _exec_sql_files(
        isolated_pg_schema,
        "scripts/postgres-init/01_schema.sql",
        "scripts/postgres-init/03_leadership_schema.sql",
        "infra/modules/cloud-sql-postgres/schema_local.sql",
        "infra/modules/cloud-sql-postgres/schema_local.sql",
    )
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                """,
                (isolated_pg_schema.schema_name,),
            )
            tables = {r[0] for r in cur.fetchall()}
            expected_tables = {
                "rules",
                "planned_routes",
                "simulation_records",
                "simulation_summaries",
                "leadership_scenarios",
                "leadership_simulation_records",
                "leadership_stakeholder_artifacts",
            }
            assert expected_tables.issubset(tables)

            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND column_name = 'owner_id'
                """,
                (isolated_pg_schema.schema_name,),
            )
            owner_tables = {r[0] for r in cur.fetchall()}
            assert {
                "planned_routes",
                "simulation_summaries",
                "leadership_scenarios",
                "leadership_stakeholder_artifacts",
            }.issubset(owner_tables)

            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
                (isolated_pg_schema.schema_name,),
            )
            indexes = {r[0] for r in cur.fetchall()}
            assert {
                "uq_rules_source_chunk",
                "idx_planned_routes_owner_created",
                "idx_simulation_records_route_id",
                "idx_simulation_summaries_owner_created",
                "idx_leadership_sim_records_scenario_id",
                "idx_leadership_scenarios_owner_created",
                "idx_leadership_artifacts_owner_updated",
            }.issubset(indexes)

            cur.execute(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON a.attrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = %s AND c.relname = 'rules' AND a.attname = 'embedding'
                """,
                (isolated_pg_schema.schema_name,),
            )
            assert cur.fetchone() == ("vector(3072)",)


@pytest.mark.requires_postgres
async def test_f2_live_uq_rules_source_chunk_enforced(isolated_pg_schema: IsolatedPostgresSchema) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/01_schema.sql")

    conn = await asyncpg.connect(
        isolated_pg_schema.dsn,
        server_settings={"search_path": f"{isolated_pg_schema.schema_name}, public"},
    )
    try:
        await conn.execute(
            "INSERT INTO rules (source_file, chunk_id, city, text) VALUES ($1, $2, $3, $4)",
            "DOC.txt",
            1,
            "Zurich",
            "Rule 1",
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO rules (source_file, chunk_id, city, text) VALUES ($1, $2, $3, $4)",
                "DOC.txt",
                1,
                "Zurich",
                "Rule 1 duplicate",
            )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Feature #3: Local Postgres seed owner_id='__seed__' preservation
# ---------------------------------------------------------------------------


def test_f3_dockerfile_local_init_copy_order() -> None:
    dockerfile = (REPO_ROOT / "infra/postgres/Dockerfile.local").read_text(encoding="utf-8")
    init_targets = re.findall(r"/docker-entrypoint-initdb\.d/([^\s]*)", dockerfile)
    assert "05_memory_schema.sql" in init_targets
    assert "06_memory_seed.sql" in init_targets
    assert "91_users.sql" in init_targets
    assert "97_drop_legacy_tables.sql" in init_targets
    assert init_targets.index("05_memory_schema.sql") < init_targets.index("06_memory_seed.sql")
    assert init_targets.index("06_memory_seed.sql") < init_targets.index("91_users.sql")


@pytest.mark.requires_postgres
def test_f3_live_dockerfile_init_sequence_preserves_seed_owner_id(
    isolated_pg_schema: IsolatedPostgresSchema,
) -> None:
    _exec_sql_files(
        isolated_pg_schema,
        "scripts/postgres-init/01_schema.sql",
        "scripts/postgres-init/03_leadership_schema.sql",
        "scripts/postgres-init/04_leadership_demo_fixtures.sql",
        "agents/leadership_planner_with_memory/alloydb/schema_local.sql",
        "agents/leadership_planner_with_memory/alloydb/seed_local.sql",
    )
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT scenario_id, owner_id FROM leadership_scenarios ORDER BY scenario_id")
            scn_rows = cur.fetchall()
            assert scn_rows == [(f"seed-leadership-00{i}", "__seed__") for i in range(1, 5)]

            cur.execute("SELECT scenario_id, owner_id FROM leadership_stakeholder_artifacts ORDER BY scenario_id")
            art_rows = cur.fetchall()
            assert art_rows == [(f"seed-leadership-00{i}", "__seed__") for i in range(1, 5)]


@pytest.mark.requires_postgres
def test_f3_live_seed_conflict_upgrades_null_owner_but_preserves_explicit_owner(
    isolated_pg_schema: IsolatedPostgresSchema,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO leadership_scenarios (scenario_id, scenario_data, owner_id)
                VALUES
                  ('seed-leadership-001', '{"name": "Preexisting Null"}', NULL),
                  ('seed-leadership-002', '{"name": "Owned By User"}', 'custom-user')
                """
            )
            cur.execute(
                """
                INSERT INTO leadership_stakeholder_artifacts (scenario_id, stakeholder_graph, owner_id)
                VALUES
                  ('seed-leadership-001', '{"nodes": []}', NULL),
                  ('seed-leadership-002', '{"nodes": []}', 'custom-user')
                """
            )

    _exec_sql_files(
        isolated_pg_schema,
        "scripts/postgres-init/04_leadership_demo_fixtures.sql",
        "agents/leadership_planner_with_memory/alloydb/seed_local.sql",
    )
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scenario_id, owner_id FROM leadership_scenarios WHERE scenario_id IN ('seed-leadership-001', 'seed-leadership-002') ORDER BY scenario_id"
            )
            assert cur.fetchall() == [
                ("seed-leadership-001", "__seed__"),
                ("seed-leadership-002", "custom-user"),
            ]
            cur.execute(
                "SELECT scenario_id, owner_id FROM leadership_stakeholder_artifacts WHERE scenario_id IN ('seed-leadership-001', 'seed-leadership-002') ORDER BY scenario_id"
            )
            assert cur.fetchall() == [
                ("seed-leadership-001", "__seed__"),
                ("seed-leadership-002", "custom-user"),
            ]


@pytest.mark.requires_postgres
async def test_f3_live_seed_rows_excluded_from_scoped_user_reads_visible_unscoped(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(
        isolated_pg_schema,
        "scripts/postgres-init/03_leadership_schema.sql",
        "scripts/postgres-init/04_leadership_demo_fixtures.sql",
    )
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)

    store = AlloyDBScenarioStore()
    try:
        scoped = await store.recall_scenarios(count=10, owner_id="real-user-123")
        assert scoped == []

        unscoped = await store.recall_scenarios(count=10, owner_id=None)
        assert len(unscoped) == 4
        assert all(s.owner_id == "__seed__" for s in unscoped)
    finally:
        await store.close()


@pytest.mark.requires_postgres
def test_f3_live_marathon_seed_local_and_seed_routes_owner_id(
    isolated_pg_schema: IsolatedPostgresSchema,
) -> None:
    _exec_sql_files(
        isolated_pg_schema,
        "scripts/postgres-init/01_schema.sql",
        "agents/planner_with_memory/alloydb/seed_local.sql",
    )
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE owner_id = '__seed__') FROM planned_routes")
            total, seeded = cur.fetchone()
            assert total >= 4
            assert total == seeded

            cur.execute("SELECT COUNT(*) FROM rules")
            assert cur.fetchone()[0] >= 1


# ---------------------------------------------------------------------------
# Feature #4: AlloyDB deploy credentials & explicit tool errors
# ---------------------------------------------------------------------------


def test_f4_deploy_alloydb_no_hardcoded_password_literal() -> None:
    script = (REPO_ROOT / "agents/planner_with_memory/alloydb/deploy_alloydb.sh").read_text(encoding="utf-8")
    assert "DevPassword123!" not in script
    assert "gcloud secrets versions access latest" in script
    assert "ALLOYDB_PASSWORD" in script


@pytest.mark.requires_postgres
async def test_f4_planner_rules_and_recall_with_vertex_ai_mock_embeddings_live(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/01_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)
    monkeypatch.setenv("AI_SETTINGS_PATH", str(tmp_path / "no_ai_settings.json"))
    monkeypatch.setenv("AI_PROVIDER", "cloud_gemini")
    monkeypatch.setenv("USE_ALLOYDB", "false")
    monkeypatch.setenv("EMBEDDING_BACKEND", "vertex_ai")
    monkeypatch.delenv("LOCAL_AI_MODE", raising=False)
    monkeypatch.delenv("USE_LOCAL_AI", raising=False)
    monkeypatch.delenv("PLANNER_PER_USER_ISOLATION", raising=False)

    vec = "[" + ",".join(["0.1"] * 3072) + "]"
    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rules (source_file, chunk_id, city, text, embedding) VALUES (%s, %s, %s, %s, %s::vector)",
                ("RULES.txt", 1, "Las Vegas", "Marathon runners keep right lane.", vec),
            )
            cur.execute(
                "INSERT INTO simulation_summaries (summary_id, city, prompt, summary, embedding, owner_id) "
                "VALUES (%s, %s, %s, %s, %s::vector, %s)",
                ("sum-1", "Las Vegas", "10k strip run", "Completed smoothly", vec, None),
            )

    async def _fake_compute_embedding(_text: str, *, dimension: int = 3072) -> list[float]:
        return [0.1] * dimension

    monkeypatch.setattr(planner_embeddings_mod, "compute_embedding", _fake_compute_embedding)

    ctx = _make_tool_context()
    rules_res = await planner_tools.get_local_and_traffic_rules(
        query="lane rules", tool_context=ctx, city="Las Vegas", limit=5
    )
    assert rules_res["status"] == "success"
    assert rules_res["count"] == 1
    assert rules_res["rules"] == [{"city": "Las Vegas", "text": "Marathon runners keep right lane."}]

    sim_res = await planner_tools.recall_past_simulations(
        query="strip run", tool_context=ctx, city="Las Vegas", limit=2
    )
    assert sim_res["status"] == "success"
    assert sim_res["count"] == 1
    assert sim_res["simulations"][0]["summary"] == "Completed smoothly"


@pytest.mark.requires_postgres
async def test_f4_leadership_tools_store_recall_and_artifacts_roundtrip(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)
    monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")

    store = AlloyDBScenarioStore()
    monkeypatch.setattr(leadership_tools, "_store", store)
    try:
        state = {
            "leadership_scenario": {"name": "Restructuring", "stakeholder_count": 5},
            "leadership_evaluation": {"overall_score": 87, "clarity": 90},
            "selected_persona": "strategist",
            "consensus_pathway_active": {"step": 1},
            "stakeholder_graph": {"nodes": [{"id": "cfo"}]},
        }
        ctx = _make_tool_context(state=state, user_id="exec-user-1")

        stored = await leadership_tools.store_scenario(ctx)
        assert stored["status"] == "success"
        sid = stored["scenario_id"]
        assert "stakeholder_graph" in stored["artifacts_autoflushed"]

        ctx.state["simulation_result"] = {"consensus": 0.88}
        outcome = await leadership_tools.record_scenario_outcome(sid, ctx)
        assert outcome["status"] == "success"

        ctx.state["simulation_report"] = {"headline": "Strong buy-in"}
        art_store = await leadership_tools.store_stakeholder_artifacts(ctx)
        assert art_store["status"] == "success"
        assert "simulation_report" in art_store["artifact_keys_stored"]

        fresh_ctx = _make_tool_context(state={}, user_id="exec-user-1")
        recalled_art = await leadership_tools.recall_stakeholder_artifacts(sid, fresh_ctx)
        assert recalled_art["status"] == "success"
        assert set(recalled_art["rehydrated_keys"]) == {"stakeholder_graph", "simulation_report"}
        assert fresh_ctx.state["stakeholder_graph"] == {"nodes": [{"id": "cfo"}]}

        recalled_scn = await leadership_tools.get_scenario(sid, fresh_ctx, activate_scenario=True)
        assert recalled_scn["status"] == "success"
        assert recalled_scn["activated"] is True
        assert fresh_ctx.state["active_scenario_id"] == sid
        assert fresh_ctx.state["selected_persona"] == "strategist"
        assert fresh_ctx.state["consensus_pathway_active"] == {"step": 1}
    finally:
        await store.close()


@pytest.mark.requires_postgres
async def test_f4_leadership_get_selected_scenarios_success(
    isolated_pg_schema: IsolatedPostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _exec_sql_files(isolated_pg_schema, "scripts/postgres-init/03_leadership_schema.sql")
    _configure_alloydb_env(monkeypatch, isolated_pg_schema)
    monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")

    store = AlloyDBScenarioStore()
    monkeypatch.setattr(leadership_tools, "_store", store)
    try:
        id1 = await store.store_scenario({"name": "One"}, evaluation_score=80.0, owner_id="u-sel")
        id2 = await store.store_scenario({"name": "Two"}, evaluation_score=85.0, owner_id="u-sel")
        ctx = _make_tool_context(state={}, user_id="u-sel")
        res = await leadership_tools.get_selected_scenarios(ctx, scenario_ids=[id1, id2])
        assert res["status"] == "success"
        assert res["count"] == 2
        assert {s["scenario_id"] for s in res["scenarios"]} == {id1, id2}
    finally:
        await store.close()


def test_f4_deploy_alloydb_resolves_with_explicit_env_hermetic_shims(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    psql_log = tmp_path / "psql_calls.log"
    uv_log = tmp_path / "uv_calls.log"

    def _mk_shim(name: str, body: str) -> None:
        p = bin_dir / name
        p.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    _mk_shim(
        "psql",
        f'echo "PGPASSWORD=${{PGPASSWORD:-}} PGOPTIONS=${{PGOPTIONS:-}} ARGS=$*" >> "{psql_log}"\n'
        'if [[ "$*" == *"COUNT(*)"* ]]; then echo "4"; fi\n',
    )
    _mk_shim("gcloud", 'echo "unexpected-gcloud-call" >&2; exit 1\n')
    _mk_shim("uv", f'echo "ARGS=$*" >> "{uv_log}"\n')

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "ALLOYDB_HOST": "10.10.0.5",
        "ALLOYDB_PASSWORD": "explicit-secret-pw",
        "ALLOYDB_SCHEMA": "custom_schema",
    }
    proc = run_cmd(
        ["bash", str(REPO_ROOT / "agents/planner_with_memory/alloydb/deploy_alloydb.sh")],
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"STDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
    psql_calls = psql_log.read_text(encoding="utf-8")
    assert "DevPassword123!" not in psql_calls
    assert "PGPASSWORD=explicit-secret-pw" in psql_calls
    assert "PGOPTIONS=-c search_path=custom_schema,public" in psql_calls
    assert "schema.sql" in psql_calls and "seed_rules.sql" in psql_calls
    assert "seed_routes" in uv_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Feature #5: Go schema_migrations tracking & fail-closed advisory lock
# ---------------------------------------------------------------------------


@pytest.mark.requires_go
def test_f5_go_unit_tests_internal_account_migrations() -> None:
    proc = run_cmd(
        ["go", "test", "-v", "-count=1", "-run", "TestApplyMigration", "./internal/account"],
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, f"go test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_live_ephemeral_billing_applies_all_7_migrations_and_records_versions(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path, "billing")
    with EphemeralGoServer(
        billing_bin,
        env={"DATABASE_URL": isolated_pg_schema.search_path_dsn, "BILLING_PROVIDER": "stub"},
    ) as srv:
        resp = httpx.get(f"{srv.base_url}/healthz", timeout=3.0)
        assert resp.status_code == 200
        assert resp.json().get("mode") != "degraded"

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = [r[0] for r in cur.fetchall()]
            assert versions == [
                "0001_users.sql",
                "0002_analyses.sql",
                "0003_node_fts.sql",
                "0004_analysis_proposals.sql",
                "0005_analyses_org_name_nullable.sql",
                "0006_document_chunks.sql",
                "0007_drop_legacy_tables.sql",
            ]

            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (isolated_pg_schema.schema_name,),
            )
            tables = {r[0] for r in cur.fetchall()}
            assert {
                "schema_migrations",
                "users",
                "plans",
                "usage_ledger",
                "usage_rollup_monthly",
                "audit_log",
                "processed_webhook_events",
                "analyses",
                "analysis_documents",
                "document_chunks",
            }.issubset(tables)
            for legacy in ("analysis_nodes", "analysis_edges", "analysis_proposals"):
                assert legacy not in tables

            cur.execute(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON a.attrelid = c.oid
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = %s AND c.relname = 'document_chunks' AND a.attname = 'embedding'
                """,
                (isolated_pg_schema.schema_name,),
            )
            assert cur.fetchone() == ("vector(3072)",)


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_live_second_startup_skips_already_applied_migrations(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path, "billing")
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for sql_file in sorted((REPO_ROOT / "migrations").glob("*.sql")):
        shutil.copy2(sql_file, mig_dir / sql_file.name)
    (mig_dir / "0008_non_idempotent_probe.sql").write_text(
        "CREATE TABLE e2e_apply_once_probe (id INT);\n",
        encoding="utf-8",
    )

    billing_env = {
        "DATABASE_URL": isolated_pg_schema.search_path_dsn,
        "BILLING_PROVIDER": "stub",
        "USERS_MIGRATIONS_DIR": str(mig_dir),
        "USERS_MIGRATION_PATH": "",
    }
    with EphemeralGoServer(
        billing_bin,
        env=billing_env,
        ready_statuses=(200, 503),
    ) as srv1:
        resp1 = httpx.get(f"{srv1.base_url}/healthz", timeout=3.0)
        assert resp1.status_code == 200
        assert resp1.json().get("mode") != "degraded"

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")
            first_snapshot = cur.fetchall()
    assert len(first_snapshot) == 8

    with EphemeralGoServer(
        billing_bin,
        env=billing_env,
        ready_statuses=(200, 503),
    ) as srv2:
        resp2 = httpx.get(f"{srv2.base_url}/healthz", timeout=3.0)
        assert resp2.status_code == 200
        assert resp2.json().get("mode") != "degraded"

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")
            second_snapshot = cur.fetchall()
    assert second_snapshot == first_snapshot


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_live_concurrent_cold_starts_serialize_on_advisory_lock(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path / "b", "billing")
    start_barrier = threading.Barrier(2)

    def _boot_billing() -> tuple[int, str | None]:
        start_barrier.wait(timeout=10.0)
        with EphemeralGoServer(
            billing_bin,
            env={"DATABASE_URL": isolated_pg_schema.search_path_dsn, "BILLING_PROVIDER": "stub"},
            ready_statuses=(200, 503),
            startup_timeout=20.0,
        ) as srv:
            resp = httpx.get(f"{srv.base_url}/healthz", timeout=3.0)
            return resp.status_code, resp.json().get("mode")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_boot_billing)
        f2 = pool.submit(_boot_billing)
        status1, mode1 = f1.result()
        status2, mode2 = f2.result()
        assert status1 == 200 and mode1 != "degraded"
        assert status2 == 200 and mode2 != "degraded"

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            assert len(cur.fetchall()) == 7


@pytest.mark.requires_postgres
@pytest.mark.requires_go
def test_f5_migrations_0001_plans_and_partial_email_index_verified(
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
) -> None:
    billing_bin = _build_go_binary_checked("./cmd/billing", tmp_path, "billing")
    with EphemeralGoServer(
        billing_bin,
        env={"DATABASE_URL": isolated_pg_schema.search_path_dsn, "BILLING_PROVIDER": "stub"},
    ):
        pass

    with psycopg2.connect(isolated_pg_schema.search_path_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT tier FROM plans ORDER BY tier")
            tiers = [r[0] for r in cur.fetchall()]
            assert tiers == ["comp", "enterprise", "free", "pro", "team"]

            # Soft-deleted user frees the email for re-signup under users_email_live_uniq.
            cur.execute(
                "INSERT INTO users (id, email, deleted_at) VALUES ('u-del', 'reuse@example.com', now())"
            )
            cur.execute("INSERT INTO users (id, email) VALUES ('u-live-1', 'reuse@example.com')")
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute("INSERT INTO users (id, email) VALUES ('u-live-2', 'reuse@example.com')")
