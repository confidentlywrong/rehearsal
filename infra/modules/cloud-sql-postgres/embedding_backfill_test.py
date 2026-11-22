# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: add unit tests for empty vectors, dimension mismatch, missing env vars, _embed(), and main().
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

"""Self-contained tests for embedding_backfill.

Pinned invariant: asyncpg cannot encode a Python list as TEXT, so the
embedding vector must be passed to UPDATE ... = $1::vector as the
pgvector text format ('[v1,v2,...]').

Runs with no external test framework or via pytest:
  python3 infra/modules/cloud-sql-postgres/embedding_backfill_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Import directly from the sibling file. embedding_backfill.py defers
# asyncpg and google-genai imports into main()/_client() so this module
# is import-safe in environments without those installed.
sys.path.insert(0, str(Path(__file__).parent))
import embedding_backfill  # noqa: E402
from embedding_backfill import EMBED_DIMENSION, _embed, _require_env, _to_pgvector_text  # noqa: E402


def test_short_vector_is_bracketed_csv() -> None:
    got = _to_pgvector_text([0.1, 0.2, 0.3])
    assert got.startswith("[") and got.endswith("]"), got
    assert "," in got, got
    assert " " not in got, "pgvector accepts spaces but we omit them for compactness"


def test_single_element() -> None:
    assert _to_pgvector_text([1.0]) == "[1.0]"


def test_three_elements_exact_format() -> None:
    assert _to_pgvector_text([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"


def test_negative_and_scientific() -> None:
    # repr(float(...)) keeps full precision; pgvector accepts scientific notation.
    got = _to_pgvector_text([-1.5e-10, 2.0])
    assert got.startswith("[-1.5e-10,") or got.startswith("[-1.5e-010,"), got
    assert got.endswith(",2.0]"), got


def test_full_dimension_3072() -> None:
    # Real-world size: gemini-embedding-001 returns 3072-dim vectors.
    vec = [0.001 * i for i in range(3072)]
    got = _to_pgvector_text(vec)
    assert got.startswith("[0.0,0.001,0.002,"), got[:60]
    # 3072 elements + 3071 commas + 2 brackets = at least 6145 chars.
    assert len(got) > 6000, len(got)


def test_rejects_empty_vector() -> None:
    try:
        _to_pgvector_text([])
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for empty vector")


def test_rejects_nan_and_inf() -> None:
    for bad in ([0.1, float("nan")], [float("inf")], [float("-inf")]):
        try:
            _to_pgvector_text(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected ValueError for non-finite vector {bad!r}")


def test_require_env_raises_clear_error_when_missing() -> None:
    with patch.dict(os.environ, {}, clear=True):
        try:
            _require_env("PROJECT_ID")
        except ValueError as exc:
            assert "PROJECT_ID" in str(exc)
        else:
            raise AssertionError("Expected ValueError for missing PROJECT_ID")


def test_embed_validates_dimension_and_empty_response() -> None:
    async def _run() -> None:
        # 1. Empty embeddings response -> RuntimeError
        client = MagicMock()
        client.aio.models.embed_content = AsyncMock(return_value=MagicMock(embeddings=[]))
        try:
            await _embed(client, "some rule text")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected RuntimeError when Vertex returns no embeddings")

        # 2. Wrong dimension -> ValueError
        bad_vec = [0.1] * (EMBED_DIMENSION - 1)
        client.aio.models.embed_content = AsyncMock(
            return_value=MagicMock(embeddings=[MagicMock(values=bad_vec)])
        )
        try:
            await _embed(client, "some rule text")
        except ValueError as exc:
            assert str(EMBED_DIMENSION) in str(exc)
        else:
            raise AssertionError("Expected ValueError for dimension mismatch")

        # 3. Exact dimension -> returns vector
        good_vec = [0.25] * EMBED_DIMENSION
        client.aio.models.embed_content = AsyncMock(
            return_value=MagicMock(embeddings=[MagicMock(values=good_vec)])
        )
        got = await _embed(client, "some rule text")
        assert len(got) == EMBED_DIMENSION
        assert got[0] == 0.25

    asyncio.run(_run())


def test_main_requires_db_env_and_updates_rules() -> None:
    async def _run() -> None:
        fake_asyncpg = types.ModuleType("asyncpg")
        fake_asyncpg.connect = AsyncMock()  # type: ignore[attr-defined]

        # 1. Missing DATABASE_HOST fails with ValueError before connecting
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            try:
                await embedding_backfill.main()
            except ValueError as exc:
                assert "DATABASE_HOST" in str(exc)
            else:
                raise AssertionError("Expected ValueError when DATABASE_HOST is unset")

        # 2. Valid env updates NULL-embedding rows via $1::vector
        fake_conn = MagicMock()
        fake_conn.fetch = AsyncMock(
            return_value=[{"source_file": "LEGISLATION.txt", "chunk_id": 7, "text": "Rule text"}]
        )
        fake_conn.execute = AsyncMock()
        fake_conn.close = AsyncMock()
        fake_asyncpg.connect = AsyncMock(return_value=fake_conn)  # type: ignore[attr-defined]

        fake_client = MagicMock()
        vec = [0.5] * EMBED_DIMENSION

        with (
            patch.dict(
                os.environ,
                {
                    "DATABASE_HOST": "127.0.0.1",
                    "DATABASE_PASSWORD": "secret",
                    "PROJECT_ID": "proj-1",
                },
                clear=True,
            ),
            patch.dict(sys.modules, {"asyncpg": fake_asyncpg}),
            patch.object(embedding_backfill, "_client", return_value=fake_client),
            patch.object(embedding_backfill, "_embed", AsyncMock(return_value=vec)),
        ):
            rc = await embedding_backfill.main()

        assert rc == 0
        fake_conn.execute.assert_awaited_once()
        sql_arg, vec_arg, src_arg, chunk_arg = fake_conn.execute.call_args.args
        assert "UPDATE rules SET embedding = $1::vector" in sql_arg
        assert vec_arg.startswith("[0.5,")
        assert src_arg == "LEGISLATION.txt"
        assert chunk_arg == 7
        fake_conn.close.assert_awaited_once()

    asyncio.run(_run())


if __name__ == "__main__":
    tests = [name for name in dir() if name.startswith("test_")]
    fails: list[str] = []
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            fails.append(name)
    if fails:
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")
