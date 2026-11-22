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

"""Targeted M1 regression tests for planner AlloyDB store, tools, and SQL assets."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.planner_with_memory.memory import store_alloydb as pl_db
from agents.planner_with_memory.memory import tools as pl_tools


_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestPlannerStoreAlloyDBM1:
    def test_validate_schema_name_defaults_to_public_and_handles_blank(self):
        assert pl_db._validate_schema_name(None) == "public"
        assert pl_db._validate_schema_name("") == "public"
        assert pl_db._validate_schema_name("   ") == "public"
        assert pl_db._validate_schema_name("planner_memory") == "planner_memory"
        with pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"):
            pl_db._validate_schema_name("bad;schema")

    def test_resolve_password_honors_alloydb_secret_name(self, monkeypatch):
        monkeypatch.delenv("ALLOYDB_PASSWORD", raising=False)
        monkeypatch.setenv("ALLOYDB_SECRET_NAME", "custom-planner-secret")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-2")
        monkeypatch.setattr(pl_db, "_cached", None)

        mock_client = MagicMock()
        mock_client.access_secret_version.return_value = SimpleNamespace(
            payload=SimpleNamespace(data=b"planner-pw\n")
        )
        with patch.object(pl_db, "_get_sm_client", return_value=mock_client):
            pw = pl_db._resolve_password()

        assert pw == "planner-pw"
        mock_client.access_secret_version.assert_called_once_with(
            name="projects/proj-2/secrets/custom-planner-secret/versions/latest"
        )
        monkeypatch.setattr(pl_db, "_cached", None)

    @pytest.mark.asyncio
    async def test_get_pool_uses_public_schema_default(self, monkeypatch):
        monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
        monkeypatch.setenv("ALLOYDB_PASSWORD", "pw")
        monkeypatch.setenv("ALLOYDB_SCHEMA", "")
        monkeypatch.setattr(pl_db, "_pool", None)
        monkeypatch.setattr(pl_db, "_pool_lock", None)
        monkeypatch.setattr(pl_db, "_pool_loop", None)

        fake_pool = MagicMock()
        fake_pool.is_closing.return_value = False
        fake_pool.terminate = MagicMock()

        with patch("asyncpg.create_pool", AsyncMock(return_value=fake_pool)) as mock_create:
            pool = await pl_db._get_pool()

        assert pool is fake_pool
        assert mock_create.await_args.kwargs["server_settings"] == {
            "search_path": "public, public"
        }
        monkeypatch.setattr(pl_db, "_pool", None)
        monkeypatch.setattr(pl_db, "_pool_lock", None)
        monkeypatch.setattr(pl_db, "_pool_loop", None)

    def test_get_dsn_rejects_whitespace_host_and_strips_valid_host(self, monkeypatch):
        monkeypatch.setenv("ALLOYDB_PASSWORD", "pw")
        monkeypatch.setenv("ALLOYDB_HOST", "   ")
        with pytest.raises(ValueError, match="ALLOYDB_HOST"):
            pl_db._get_dsn()

        monkeypatch.setenv("ALLOYDB_HOST", " 127.0.0.1 ")
        assert pl_db._get_dsn() == "postgresql://postgres:pw@127.0.0.1:5432/postgres"

    def test_row_to_route_handles_bytes_corrupt_eval_and_invalid_route_data(self, caplog):
        now = datetime(2026, 3, 1, 10, 0, 0)
        good_row = {
            "route_id": "r-1",
            "route_data": b'{"name": "BytesRoute"}',
            "created_at": now,
            "eval_score": 85.0,
            "eval_result": bytearray(b'{"overall_score": 85}'),
            "owner_id": "u1",
        }
        route = pl_db._row_to_route(good_row)  # type: ignore[arg-type]
        assert route.route_data == {"name": "BytesRoute"}
        assert route.evaluation_result == {"overall_score": 85}
        assert route.created_at.tzinfo == timezone.utc

        corrupt_eval_row = {
            "route_id": "r-2",
            "route_data": bytearray(b'{"name": "BytearrayRoute"}'),
            "created_at": now,
            "eval_score": None,
            "eval_result": "{corrupt_eval",
        }
        route2 = pl_db._row_to_route(corrupt_eval_row)  # type: ignore[arg-type]
        assert route2.route_data == {"name": "BytearrayRoute"}
        assert route2.evaluation_result is None
        assert route2.owner_id is None
        assert "JSONB decode failed" in caplog.text

        for bad_rd in (None, "{corrupt_route", b"{corrupt_bytes", "null", "[]", b"[]", [], "42"):
            bad_row = {
                "route_id": "r-bad",
                "route_data": bad_rd,
                "created_at": now,
                "eval_score": None,
                "eval_result": None,
            }
            with pytest.raises(ValueError):
                pl_db._row_to_route(bad_row)  # type: ignore[arg-type]

    def test_row_to_route_ignores_non_dict_eval_result(self, caplog):
        now = datetime(2026, 4, 1, tzinfo=timezone.utc)
        for bad_eval in ("[1, 2]", '"oops"', "123", "true", b"[1, 2]"):
            caplog.clear()
            row = {
                "route_id": "r-nondict",
                "route_data": '{"name": "Strip"}',
                "created_at": now,
                "eval_score": 85.0,
                "eval_result": bad_eval,
            }
            route = pl_db._row_to_route(row)  # type: ignore[arg-type]
            assert route.evaluation_result is None
            assert "Ignoring non-dict eval_result JSONB" in caplog.text

    @pytest.mark.asyncio
    async def test_load_simulations_handles_bytes_and_corrupt_jsonb(self, caplog):
        now = datetime(2026, 4, 1, 12, 0, 0)
        conn = MagicMock()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "simulation_id": "sim-1",
                    "route_id": "r-1",
                    "sim_result": b'{"k": 1}',
                    "simulated_at": now,
                },
                {
                    "simulation_id": "sim-2",
                    "route_id": "r-1",
                    "sim_result": bytearray(b'{"k": 2}'),
                    "simulated_at": now,
                },
                {
                    "simulation_id": "sim-3",
                    "route_id": "r-1",
                    "sim_result": "{corrupt_sim_json",
                    "simulated_at": now,
                },
                {
                    "simulation_id": "sim-4",
                    "route_id": "r-1",
                    "sim_result": "[]",
                    "simulated_at": now,
                },
            ]
        )
        records = await pl_db._load_simulations(conn, "r-1")
        assert [r.simulation_result for r in records] == [{"k": 1}, {"k": 2}, {}, {}]
        assert all(r.simulated_at.tzinfo == timezone.utc for r in records)
        assert "JSONB decode failed" in caplog.text


class TestPlannerToolsM1:
    def test_resolve_embedding_backend_accepts_vertexai_alias_and_rejects_invalid(self, monkeypatch):
        monkeypatch.setenv("USE_ALLOYDB", "true")
        with patch.object(pl_tools, "is_local_gemma_active", return_value=False):
            monkeypatch.setenv("EMBEDDING_BACKEND", "vertexai")
            assert pl_tools._resolve_embedding_backend() == "vertex_ai"

            monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")
            assert pl_tools._resolve_embedding_backend() == "alloydb_ai"

            monkeypatch.setenv("EMBEDDING_BACKEND", "unknown_backend")
            with pytest.raises(ValueError, match="Invalid EMBEDDING_BACKEND"):
                pl_tools._resolve_embedding_backend()

    @pytest.mark.asyncio
    async def test_recall_past_simulations_errors_and_decodes_json_string(self, monkeypatch):
        monkeypatch.delenv("ALLOYDB_HOST", raising=False)
        res_no_host = await pl_tools.recall_past_simulations(query="test", tool_context=MagicMock())
        assert res_no_host["status"] == "error"

        monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
        monkeypatch.setenv("ALLOYDB_PASSWORD", "pw")
        monkeypatch.setenv("USE_ALLOYDB", "false")
        monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")
        with patch.object(pl_tools, "is_local_gemma_active", return_value=False):
            res_local = await pl_tools.recall_past_simulations(query="test", tool_context=MagicMock())
        assert res_local["status"] == "error"

        mock_conn = MagicMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "city": "Las Vegas",
                    "prompt": "Plan",
                    "summary": "Summary",
                    "sim_result": json.dumps({"completion": 0.99}),
                    "created_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
                }
            ]
        )
        mock_conn.close = AsyncMock()
        monkeypatch.setenv("USE_ALLOYDB", "true")
        monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")
        with (
            patch("asyncpg.connect", AsyncMock(return_value=mock_conn)) as mock_connect,
            patch.object(pl_tools, "is_local_gemma_active", return_value=False),
        ):
            res_ok = await pl_tools.recall_past_simulations(query="test", tool_context=MagicMock())

        assert res_ok["status"] == "success"
        assert res_ok["simulations"][0]["sim_result"] == {"completion": 0.99}
        assert mock_connect.await_args.kwargs["server_settings"] == {
            "search_path": "public, public"
        }

    @pytest.mark.asyncio
    async def test_recall_past_simulations_handles_bytes_and_corrupt_sim_result(self, monkeypatch, caplog):
        monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
        monkeypatch.setenv("ALLOYDB_PASSWORD", "pw")
        monkeypatch.setenv("USE_ALLOYDB", "true")
        monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")

        mock_conn = MagicMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "city": "Las Vegas",
                    "prompt": "P1",
                    "summary": "S1",
                    "sim_result": b'{"k": 3}',
                    "created_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
                },
                {
                    "city": "Las Vegas",
                    "prompt": "P2",
                    "summary": "S2",
                    "sim_result": "{bad_json",
                    "created_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
                },
                {
                    "city": "Las Vegas",
                    "prompt": "P3",
                    "summary": "S3",
                    "sim_result": b"{bad_bytes",
                    "created_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
                },
            ]
        )
        mock_conn.close = AsyncMock()
        with (
            patch("asyncpg.connect", AsyncMock(return_value=mock_conn)),
            patch.object(pl_tools, "is_local_gemma_active", return_value=False),
        ):
            res = await pl_tools.recall_past_simulations(query="test", tool_context=MagicMock())

        assert res["status"] == "success"
        assert res["count"] == 3
        assert res["simulations"][0]["sim_result"] == {"k": 3}
        assert res["simulations"][1]["sim_result"] is None
        assert res["simulations"][2]["sim_result"] is None
        assert "JSONB decode failed" in caplog.text

    @pytest.mark.asyncio
    async def test_limit_zero_runs_config_and_schema_checks_first(self, monkeypatch):
        for fn in (pl_tools.get_local_and_traffic_rules, pl_tools.recall_past_simulations):
            # 1. Whitespace or unset ALLOYDB_HOST must error even when limit <= 0
            monkeypatch.setenv("ALLOYDB_HOST", "   ")
            res_ws = await fn(query="q", tool_context=MagicMock(), limit=0)
            assert res_ws["status"] == "error"

            monkeypatch.delenv("ALLOYDB_HOST", raising=False)
            res_unset = await fn(query="q", tool_context=MagicMock(), limit=-5)
            assert res_unset["status"] == "error"

            # 2. Misconfigured local mode must error even when limit <= 0
            monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
            monkeypatch.setenv("USE_ALLOYDB", "false")
            monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")
            with patch.object(pl_tools, "is_local_gemma_active", return_value=False):
                res_local = await fn(query="q", tool_context=MagicMock(), limit=0)
            assert res_local["status"] == "error"

            # 3. Invalid ALLOYDB_SCHEMA must raise ValueError even when limit <= 0
            monkeypatch.setenv("USE_ALLOYDB", "true")
            monkeypatch.setenv("ALLOYDB_SCHEMA", "bad;schema")
            with (
                patch.object(pl_tools, "is_local_gemma_active", return_value=False),
                pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"),
            ):
                await fn(query="q", tool_context=MagicMock(), limit=0)

            # 4. Valid config with limit=0 short-circuits cleanly without DB connect
            monkeypatch.setenv("ALLOYDB_SCHEMA", "public")
            with (
                patch.object(pl_tools, "is_local_gemma_active", return_value=False),
                patch("asyncpg.connect", side_effect=AssertionError("DB must not be called")),
            ):
                res_ok = await fn(query="q", tool_context=MagicMock(), limit=0)
            assert res_ok["status"] == "success"
            assert res_ok["count"] == 0

    @pytest.mark.asyncio
    async def test_use_alloydb_whitespace_and_invalid_nan_inf_limit_fallback(self, monkeypatch):
        monkeypatch.setenv("ALLOYDB_HOST", "127.0.0.1")
        monkeypatch.setenv("ALLOYDB_PASSWORD", "pw")
        monkeypatch.setenv("ALLOYDB_SCHEMA", "public")
        monkeypatch.setenv("USE_ALLOYDB", " false ")
        monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")

        with patch.object(pl_tools, "is_local_gemma_active", return_value=False):
            assert pl_tools._use_alloydb_flag() == "false"
            res_rules = await pl_tools.get_local_and_traffic_rules(query="q", tool_context=MagicMock())
            res_sims = await pl_tools.recall_past_simulations(query="q", tool_context=MagicMock())
        assert res_rules["status"] == "error"
        assert "USE_ALLOYDB=false" in res_rules["message"]
        assert res_sims["status"] == "error"
        assert "USE_ALLOYDB=false" in res_sims["message"]

        monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
        with patch.object(pl_tools, "is_local_gemma_active", return_value=False):
            assert pl_tools._resolve_embedding_backend() == "vertex_ai"

        monkeypatch.setenv("USE_ALLOYDB", "true")
        monkeypatch.setenv("EMBEDDING_BACKEND", "alloydb_ai")
        for fn, expected_default in (
            (pl_tools.get_local_and_traffic_rules, 5),
            (pl_tools.recall_past_simulations, 2),
        ):
            for bad_limit in ("bad", float("nan"), float("inf"), float("-inf"), True, False):
                mock_conn = MagicMock()
                mock_conn.fetch = AsyncMock(return_value=[])
                mock_conn.close = AsyncMock()
                with (
                    patch("asyncpg.connect", AsyncMock(return_value=mock_conn)),
                    patch.object(pl_tools, "is_local_gemma_active", return_value=False),
                ):
                    res = await fn(query="q", tool_context=MagicMock(), limit=bad_limit)  # type: ignore[arg-type]
                assert res["status"] == "success"
                assert mock_conn.fetch.await_args.args[-1] == expected_default


class TestPlannerSQLAndScriptAssets:
    def test_schema_and_seed_sql_parity(self):
        schema_sql = (_REPO_ROOT / "agents/planner_with_memory/alloydb/schema.sql").read_text()
        schema_local = (_REPO_ROOT / "agents/planner_with_memory/alloydb/schema_local.sql").read_text()
        seed_rules = (_REPO_ROOT / "agents/planner_with_memory/alloydb/seed_rules.sql").read_text()
        cloudsql_local = (_REPO_ROOT / "infra/modules/cloud-sql-postgres/schema_local.sql").read_text()
        init_seed = (_REPO_ROOT / "scripts/postgres-init/02_seed.sql").read_text()

        assert "Modifications copyright 2026 Christian Haeussinger" in schema_sql
        assert "Modifications copyright 2026 Christian Haeussinger" in seed_rules

        for sql_text in (schema_sql, schema_local, cloudsql_local):
            assert "uq_rules_source_chunk" in sql_text
            assert "idx_planned_routes_owner_created" in sql_text
            assert "idx_simulation_summaries_owner_created" in sql_text

        assert "ON CONFLICT (source_file, chunk_id) DO NOTHING;" in seed_rules
        assert "'__seed__'" in init_seed
        assert "COALESCE(planned_routes.owner_id, EXCLUDED.owner_id)" in init_seed

        # Check planner seed_local.sql head and tail without touching giant vectors unnecessarily
        seed_local_lines = (_REPO_ROOT / "agents/planner_with_memory/alloydb/seed_local.sql").read_text().splitlines()
        head = "\n".join(seed_local_lines[:5])
        tail = "\n".join(seed_local_lines[-35:])
        assert "Modifications copyright 2026 Christian Haeussinger" in head
        assert "ON CONFLICT (source_file, chunk_id) DO NOTHING;" in "\n".join(seed_local_lines[35:62])
        assert "'__seed__'" in tail
        assert "COALESCE(planned_routes.owner_id, EXCLUDED.owner_id)" in tail

    def test_deploy_alloydb_script_fails_closed_without_hardcoded_password(self, tmp_path):
        script_path = _REPO_ROOT / "agents/planner_with_memory/alloydb/deploy_alloydb.sh"
        script_text = script_path.read_text()
        assert "DevPassword123!" not in script_text
        assert "ALLOYDB_SCHEMA" in script_text
        assert "PGOPTIONS" in script_text

        # Create fake bin dir with stub psql, gcloud, uv where gcloud fails to provide project/secret
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool, body in {
            "psql": "#!/usr/bin/env bash\nexit 0\n",
            "uv": "#!/usr/bin/env bash\nexit 0\n",
            "gcloud": "#!/usr/bin/env bash\nexit 1\n",
        }.items():
            p = bin_dir / tool
            p.write_text(body)
            p.chmod(0o755)

        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ALLOYDB_HOST": "127.0.0.1",
        }
        proc = subprocess.run(
            ["bash", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        assert "ALLOYDB_PASSWORD" in proc.stderr or "Secret Manager" in proc.stderr

        # Also check invalid ALLOYDB_SCHEMA is rejected
        env_bad_schema = {**env, "ALLOYDB_PASSWORD": "testpw", "ALLOYDB_SCHEMA": "bad;drop"}
        proc_bad = subprocess.run(
            ["bash", str(script_path)],
            env=env_bad_schema,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_bad.returncode != 0
        assert "Invalid ALLOYDB_SCHEMA" in proc_bad.stderr

        # Also check ALLOYDB_PASSWORD set + ALLOYDB_HOST unset + failing gcloud reaches explicit die message
        env_no_host = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ALLOYDB_PASSWORD": "testpw",
        }
        proc_no_host = subprocess.run(
            ["bash", str(script_path)],
            env=env_no_host,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_no_host.returncode != 0
        assert "Could not determine ALLOYDB_HOST" in proc_no_host.stderr

        # Whitespace-only ALLOYDB_HOST must fail closed with explicit die error
        env_ws_host = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ALLOYDB_PASSWORD": "testpw",
            "ALLOYDB_HOST": "   ",
        }
        proc_ws_host = subprocess.run(
            ["bash", str(script_path)],
            env=env_ws_host,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_ws_host.returncode != 0
        assert "ALLOYDB_HOST" in proc_ws_host.stderr

        # Whitespace-only ALLOYDB_PASSWORD must also fail closed with explicit die error
        env_ws_pw = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ALLOYDB_PASSWORD": "   ",
            "ALLOYDB_HOST": "127.0.0.1",
        }
        proc_ws_pw = subprocess.run(
            ["bash", str(script_path)],
            env=env_ws_pw,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_ws_pw.returncode != 0
        assert "ALLOYDB_PASSWORD" in proc_ws_pw.stderr
