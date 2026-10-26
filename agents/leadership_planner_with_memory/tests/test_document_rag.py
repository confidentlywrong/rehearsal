# Copyright 2026 The Confidently Wrong Authors
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
