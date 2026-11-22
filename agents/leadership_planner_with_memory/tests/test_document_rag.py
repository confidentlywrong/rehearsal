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

"""Tests for the ``retrieve_document_passages`` RAG tool.

The tool is a thin async HTTP client over the gateway's
``/api/v1/memory/analyses/:id/retrieve-passages`` endpoint.
Every network call is mocked via ``respx`` so these tests stay
offline like the rest of the leadership_planner_with_memory suite.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx

from agents.leadership_planner_with_memory.memory import retrieval_tools
from agents.utils import user_auth_context


@pytest.fixture
def _fake_ctx() -> SimpleNamespace:
    session_id = f"test-session-{id(object())}"
    return SimpleNamespace(state={}, session=SimpleNamespace(id=session_id))


@pytest.fixture(autouse=True)
def _pin_gateway_env(monkeypatch):
    """Force a stable gateway URL + token for every test."""
    monkeypatch.setenv("MEMORY_GATEWAY_URL", "http://gateway.test")
    monkeypatch.setenv("MEMORY_GATEWAY_TOKEN", "test-token")


@pytest.fixture(autouse=True)
def _reset_user_auth_context():
    user_auth_context._reset_for_tests()
    yield
    user_auth_context._reset_for_tests()


# ── Happy path ──────────────────────────────────────────────────────


class TestRetrievePassagesHappyPath:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_formatted_passages(self, _fake_ctx):
        """Gateway returns passages → tool formats them correctly."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "passages": [
                        {"text": "The Q3 report shows revenue growth of 15%.", "score": 0.92, "document_id": "doc-1", "chunk_index": 0},
                        {"text": "Team morale improved after the offsite.", "score": 0.85, "document_id": "doc-2", "chunk_index": 3},
                    ]
                },
            )
        )

        result = await retrieval_tools.retrieve_document_passages(
            query="What is the revenue outlook?",
            tool_context=_fake_ctx,
        )

        assert result["status"] == "success"
        assert result["analysis_id"] == "analysis-abc"
        assert result["passage_count"] == 2
        assert len(result["passages"]) == 2
        assert result["passages"][0]["text"] == "The Q3 report shows revenue growth of 15%."
        assert result["passages"][0]["score"] == 0.92
        assert result["passages"][1]["document_id"] == "doc-2"


# ── No analysis bound ──────────────────────────────────────────────


class TestRetrievePassagesNoAnalysis:
    @pytest.mark.asyncio
    @respx.mock
    async def test_no_analysis_bound(self, _fake_ctx):
        """No analysis in state, auto-discover finds none → fail-soft."""
        respx.get("http://gateway.test/api/v1/memory/analyses").mock(
            return_value=httpx.Response(200, json=[])
        )

        result = await retrieval_tools.retrieve_document_passages(
            query="Tell me about org structure",
            tool_context=_fake_ctx,
        )

        assert result["status"] == "no_analysis_bound"
        assert result["passages"] == []


# ── Empty query ────────────────────────────────────────────────────


class TestRetrievePassagesEmptyQuery:
    @pytest.mark.asyncio
    async def test_empty_query(self, _fake_ctx):
        """Empty query returns no_query status without hitting the gateway."""
        result = await retrieval_tools.retrieve_document_passages(
            query="",
            tool_context=_fake_ctx,
        )

        assert result["status"] == "no_query"
        assert result["passages"] == []


# ── Gateway errors ─────────────────────────────────────────────────


class TestRetrievePassagesGatewayErrors:
    @pytest.mark.asyncio
    @respx.mock
    async def test_gateway_500(self, _fake_ctx):
        """Gateway 500 → fail-soft with gateway_error status."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(
            return_value=httpx.Response(500, json={"error": "internal"})
        )

        result = await retrieval_tools.retrieve_document_passages(
            query="budget forecast",
            tool_context=_fake_ctx,
        )

        assert result["status"] == "gateway_error"
        assert result["passages"] == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_not_found(self, _fake_ctx):
        """Gateway 404 → analysis not found or not owned."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-missing"

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-missing/retrieve-passages"
        ).mock(
            return_value=httpx.Response(404, json={"error": "not_found"})
        )

        result = await retrieval_tools.retrieve_document_passages(
            query="budget",
            tool_context=_fake_ctx,
        )

        assert result["status"] == "not_found"
        assert result["passages"] == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_auth_rejected(self, _fake_ctx):
        """Gateway 401 → fail-soft with no_auth_configured."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(
            return_value=httpx.Response(401, text="unauthorized")
        )

        result = await retrieval_tools.retrieve_document_passages(
            query="budget",
            tool_context=_fake_ctx,
        )

        assert result["status"] == "no_auth_configured"
        assert result["passages"] == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_transport_error(self, _fake_ctx):
        """Network failure → fail-soft with gateway_error status."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        result = await retrieval_tools.retrieve_document_passages(
            query="org chart",
            tool_context=_fake_ctx,
        )

        assert result["status"] == "gateway_error"
        assert result["passages"] == []


# ── Empty passages from gateway ────────────────────────────────────


class TestRetrievePassagesEmpty:
    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_results(self, _fake_ctx):
        """Gateway returns empty passages (no docs uploaded) → success with 0."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(
            return_value=httpx.Response(200, json={"passages": []})
        )

        result = await retrieval_tools.retrieve_document_passages(
            query="anything",
            tool_context=_fake_ctx,
        )

        assert result["status"] == "success"
        assert result["passage_count"] == 0
        assert result["passages"] == []


# ── Limit clamping ─────────────────────────────────────────────────


class TestRetrievePassagesLimits:
    @pytest.mark.asyncio
    @respx.mock
    async def test_limit_clamped_high(self, _fake_ctx):
        """Limit > 20 is clamped to 20."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"

        captured_bodies: list[dict] = []

        def capture_request(request):
            import json
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"passages": []})

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(side_effect=capture_request)

        await retrieval_tools.retrieve_document_passages(
            query="test", tool_context=_fake_ctx, limit=50
        )
        assert captured_bodies[-1]["limit"] == 20

    @pytest.mark.asyncio
    @respx.mock
    async def test_limit_clamped_low(self, _fake_ctx):
        """Limit 0 → treated as falsy, falls back to default (5)."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"

        captured_bodies: list[dict] = []

        def capture_request(request):
            import json
            captured_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"passages": []})

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(side_effect=capture_request)

        await retrieval_tools.retrieve_document_passages(
            query="test", tool_context=_fake_ctx, limit=0
        )
        # 0 is falsy, so `int(0 or 5)` = 5 (default), clamped to [1,20].
        assert captured_bodies[-1]["limit"] == 5


class TestGatewayUrlAndAutoDiscover:
    def test_gateway_url_precedence_and_default(self, monkeypatch):
        """_gateway_url follows MEMORY_GATEWAY_URL > GATEWAY_INTERNAL_URL > GATEWAY_URL > GATEWAY_ADDR > 9101."""
        for var in ("MEMORY_GATEWAY_URL", "GATEWAY_INTERNAL_URL", "GATEWAY_URL", "GATEWAY_ADDR"):
            monkeypatch.delenv(var, raising=False)
        assert retrieval_tools._gateway_url() == "http://127.0.0.1:9101"

        monkeypatch.setenv("GATEWAY_ADDR", "10.0.0.5:9101")
        assert retrieval_tools._gateway_url() == "http://10.0.0.5:9101"

        monkeypatch.setenv("GATEWAY_URL", "https://gw.example.com/")
        assert retrieval_tools._gateway_url() == "https://gw.example.com"

        monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://internal-gw:9101/")
        assert retrieval_tools._gateway_url() == "http://internal-gw:9101"

        monkeypatch.setenv("MEMORY_GATEWAY_URL", "   ")
        assert retrieval_tools._gateway_url() == "http://internal-gw:9101"

        monkeypatch.setenv("MEMORY_GATEWAY_URL", "http://memory-gw:9101/")
        assert retrieval_tools._gateway_url() == "http://memory-gw:9101"

    @pytest.mark.asyncio
    @respx.mock
    async def test_auto_discover_analysis_id_picks_latest_dict_row(self, _fake_ctx):
        respx.get("http://gateway.test/api/v1/memory/analyses").mock(
            return_value=httpx.Response(
                200,
                json={
                    "analyses": [
                        {"id": "older", "created_at": "2026-01-01T00:00:00Z"},
                        {"id": "newest", "created_at": "2026-06-01T00:00:00Z"},
                        "non-dict-ignored",
                    ]
                },
            )
        )
        async with httpx.AsyncClient() as client:
            aid, err = await retrieval_tools._auto_discover_analysis_id(client, _fake_ctx)
        assert err is None
        assert aid == "newest"
        assert _fake_ctx.state["memory_analysis_id"] == "newest"

    @pytest.mark.asyncio
    @respx.mock
    @pytest.mark.parametrize("bad_id", [".", "..", "../../admin", "a/b?x=1", "bad#frag", "id\\slash"])
    async def test_auto_discover_rejects_unsafe_path_traversal_id(self, _fake_ctx, bad_id):
        """Regression: unsafe gateway analysis id must be rejected with gateway_error."""
        respx.get("http://gateway.test/api/v1/memory/analyses").mock(
            return_value=httpx.Response(
                200,
                json={
                    "analyses": [
                        {"id": bad_id, "created_at": "2026-06-01T00:00:00Z"},
                    ]
                },
            )
        )
        async with httpx.AsyncClient() as client:
            aid, err = await retrieval_tools._auto_discover_analysis_id(client, _fake_ctx)
        assert aid is None
        assert err == "gateway_error"
        assert "memory_analysis_id" not in _fake_ctx.state


class TestRetrievePassagesOverflowAndNonFinite:
    @pytest.mark.asyncio
    @respx.mock
    async def test_non_finite_and_bool_limits_fail_soft(self, _fake_ctx):
        """Regression: limit=float('inf'), float('nan'), and bool must not raise OverflowError."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"
        captured_limits: list[int] = []

        def capture_request(request):
            import json

            captured_limits.append(json.loads(request.content)["limit"])
            return httpx.Response(200, json={"passages": []})

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(side_effect=capture_request)

        for bad_limit in (float("inf"), float("nan"), True):
            res = await retrieval_tools.retrieve_document_passages(
                query="test", tool_context=_fake_ctx, limit=bad_limit  # type: ignore[arg-type]
            )
            assert res["status"] == "success"
            assert res["passages"] == []
            assert captured_limits[-1] == 5

    @pytest.mark.asyncio
    @respx.mock
    async def test_overflow_score_and_chunk_index_in_gateway_payload_never_raise(self, _fake_ctx):
        """Regression: score=10**400 and chunk_index=10**400/inf must normalize cleanly without OverflowError."""
        _fake_ctx.state["memory_analysis_id"] = "analysis-abc"

        respx.post(
            "http://gateway.test/api/v1/memory/analyses/analysis-abc/retrieve-passages"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "passages": [
                        {
                            "text": "Valid passage with overflow telemetry.",
                            "score": 10**400,
                            "document_id": "doc-ovf",
                            "chunk_index": 10**400,
                        },
                        {
                            "text": "Valid passage with string infinity.",
                            "score": "inf",
                            "document_id": "doc-inf",
                            "chunk_index": "inf",
                        },
                    ]
                },
            )
        )

        res = await retrieval_tools.retrieve_document_passages(
            query="test", tool_context=_fake_ctx
        )
        assert res["status"] == "success"
        assert res["passage_count"] == 2
        assert res["passages"][0]["score"] == 0.0
        assert res["passages"][0]["chunk_index"] == 10**400
        assert res["passages"][1]["score"] == 0.0
        assert res["passages"][1]["chunk_index"] == 0

