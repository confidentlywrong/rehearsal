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

"""Round 4 unit tests for Python ADK planners, MiroFish validation, and utils."""

from __future__ import annotations

import ast
import json
import pathlib
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.leadership_planner_with_memory.memory import retrieval_tools
from agents.leadership_planner_with_memory.simulation_report import emit_simulation_report
from agents.leadership_planner_with_memory.simulation_timeline import emit_simulation_timeline
from agents.leadership_planner_with_memory.stakeholder_graph import emit_stakeholder_graph
from agents.leadership_planner_with_memory.stakeholder_interview import emit_stakeholder_interview
from agents.leadership_planner_with_memory.stakeholder_personas import emit_stakeholder_personas
from agents.leadership_planner_with_memory.stakeholder_reactions import emit_stakeholder_reactions
from agents.utils import auth, env, load_agent_skills, tenant_keys, user_auth_context
from agents.utils.global_gemini import GlobalGemini
from agents.utils.prompt_builder import PromptBuilder
from agents.utils.simulation_executor import SimulationExecutor
from agents.utils.simulation_plugin import SimulationNetworkPlugin

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _tool_ctx(state: dict | None = None, *, session_id: str = "sess-1") -> SimpleNamespace:
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id),
    )


def _valid_report() -> dict:
    return {
        "headline": "Pilot holds after CFO guardrail revision",
        "outcome": "partial",
        "executive_summary": "Support consolidates once risk controls are added.",
        "key_findings": [
            {"title": "Ops bandwidth", "body": "Floor leads need backfill.", "severity": "high"},
            {"title": "Finance gate", "body": "Monthly ROI review unblocks CFO.", "severity": "medium"},
        ],
    }


def _valid_interview() -> dict:
    return {
        "stakeholder_id": "cfo",
        "topic": "Rollout budget controls",
        "tone": "guarded",
        "final_stance": "neutral",
        "exchanges": [
            {"question": "What blocks Q3 sign-off?", "answer": "Unbounded compute spend."},
        ],
    }


def _valid_graph() -> dict:
    return {
        "nodes": [
            {"id": "ceo", "label": "CEO", "kind": "Actor"},
            {"id": "ops", "label": "Operations", "kind": "Group"},
        ],
        "edges": [{"from": "ceo", "to": "ops", "label": "aligns"}],
    }


def _valid_personas() -> dict:
    return {
        "personas": [
            {
                "stakeholder_id": "ceo",
                "archetype": "visionary",
                "communication_style": "directive",
                "stance": "supportive",
                "sentiment_bias": 0.4,
                "influence_weight": 0.9,
                "response_delay_sprints": 0,
                "primary_concerns": ["growth"],
            }
        ]
    }


def _valid_reactions() -> dict:
    return {
        "reactions": [
            {"stakeholder_id": "ceo", "stance": "supportive", "reaction_text": "Let us pilot in Q3."}
        ]
    }


def _valid_timeline() -> dict:
    return {
        "ticks": [
            {
                "tick": 1,
                "headline": "Kickoff alignment",
                "states": [
                    {"stakeholder_id": "ceo", "stance": "supportive", "sentiment": 0.5}
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# 1. agents/utils/auth.py
# ---------------------------------------------------------------------------


class TestAuthResolutionAndTimeout:
    def test_resolve_audience_strips_iap_and_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IAP_CLIENT_ID", "  12345.apps.googleusercontent.com\n ")
        assert auth.resolve_audience("http://localhost:9101") == "12345.apps.googleusercontent.com"

        monkeypatch.setenv("IAP_CLIENT_ID", "   \n")
        assert auth.resolve_audience("  https://gateway.run.app:8443/a2a \n") == "https://gateway.run.app:8443/a2a"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "http://gateway.run.app",
            "http://localhost:9101",
            "http://127.0.0.1:9101",
            "https://localhost",
            "https://localhost:9101/a2a",
            "https://sub.localhost./a2a",
            "https://127.0.0.1:9101",
            "https://127.2.3.4/path",
            "https://[::1]:9101",
            "https://[::ffff:127.0.0.1]:9101",
            "https://0.0.0.0:9101",
            "https://[::]:9101",
            "https://example.com:notaport",
            "https://example.com:99999",
            "https://example.com\nX-Inject: 1",
            "https://example.com/has space",
            "https:///missing-host",
        ],
    )
    def test_resolve_audience_rejects_cleartext_loopback_and_malformed_urls(
        self,
        monkeypatch: pytest.MonkeyPatch,
        url: str,
    ) -> None:
        monkeypatch.delenv("IAP_CLIENT_ID", raising=False)
        assert auth.resolve_audience(url) == ""

    def test_get_id_token_skips_metadata_fetch_for_whitespace_or_local_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("IAP_CLIENT_ID", raising=False)
        with patch("agents.utils.auth.id_token.fetch_id_token") as mock_fetch:
            aud = auth.resolve_audience("http://localhost:9101")
            assert aud == ""
            assert auth.get_id_token(aud) is None
            assert auth.get_id_token("   \n\t ") is None
        mock_fetch.assert_not_called()

    def test_get_id_token_strips_valid_audience(self) -> None:
        with patch("agents.utils.auth.id_token.fetch_id_token", return_value="tok-123") as mock_fetch:
            got = auth.get_id_token("  https://gateway.run.app  ")
        assert got == "tok-123"
        assert mock_fetch.call_args.args[1] == "https://gateway.run.app"

    def test_request_caps_session_timeout_without_breaking_mocks(self) -> None:
        auth._request.cache_clear()
        try:
            req = auth._request()
            captured_timeouts: list[object] = []
            assert hasattr(req, "session")
            assert callable(req.session.request)
        finally:
            auth._request.cache_clear()

        # Verify timeout clamping helper & mocked Request() compatibility.
        assert auth._clamp_timeout(None) == auth._DEFAULT_METADATA_REQUEST_TIMEOUT_SECS
        assert auth._clamp_timeout(120) == auth._DEFAULT_METADATA_REQUEST_TIMEOUT_SECS
        assert auth._clamp_timeout(3) == 3
        assert auth._clamp_timeout((120, 2.5)) == (auth._DEFAULT_METADATA_REQUEST_TIMEOUT_SECS, 2.5)
        assert auth._clamp_timeout(float("nan")) == auth._DEFAULT_METADATA_REQUEST_TIMEOUT_SECS

        auth._request.cache_clear()
        try:
            mock_req = MagicMock()
            with patch("agents.utils.auth.google.auth.transport.requests.Request", return_value=mock_req):
                assert auth._request() is mock_req
        finally:
            auth._request.cache_clear()

        # Verify wrapper intercepts real requests.Session.request timeout=120 -> <=5.0s.
        auth._request.cache_clear()
        try:
            with patch("requests.Session.request", side_effect=lambda *a, **kw: captured_timeouts.append(kw.get("timeout"))):
                wrapped_req = auth._request()
                wrapped_req.session.request("GET", "http://169.254.169.254", timeout=120)
                wrapped_req.session.request("GET", "http://169.254.169.254", timeout=3)
                wrapped_req.session.request("GET", "http://169.254.169.254")
            assert captured_timeouts == [
                auth._DEFAULT_METADATA_REQUEST_TIMEOUT_SECS,
                3,
                auth._DEFAULT_METADATA_REQUEST_TIMEOUT_SECS,
            ]
        finally:
            auth._request.cache_clear()


# ---------------------------------------------------------------------------
# 2. MiroFish tools validation, citations, NaN/Inf, rationale, assert removal
# ---------------------------------------------------------------------------


class TestMiroFishValidation:
    @pytest.mark.asyncio
    async def test_simulation_report_citations_valid_strings_dicts_empty_and_none(self) -> None:
        ctx = _tool_ctx()
        payload = _valid_report()
        payload["citations"] = [
            "  Q3 Operating Review, p. 4  ",
            {
                "quote": "  Margins compressed 2%  ",
                "source_kind": "Belief",
                "source_id": "belief:q3",
                "score": 0.91,
            },
        ]
        res = await emit_simulation_report(payload, ctx)
        assert res["status"] == "success", res
        saved = ctx.state["simulation_report"]
        assert saved["citations"] == [
            "Q3 Operating Review, p. 4",
            {
                "quote": "Margins compressed 2%",
                "source_kind": "Belief",
                "source_id": "belief:q3",
                "score": 0.91,
            },
        ]

        # None omits citations rather than leaking None into state/JSONB.
        ctx_none = _tool_ctx()
        p_none = _valid_report()
        p_none["citations"] = None
        res_none = await emit_simulation_report(p_none, ctx_none)
        assert res_none["status"] == "success"
        assert "citations" not in ctx_none.state["simulation_report"]

        # Explicit [] is preserved safely.
        ctx_empty = _tool_ctx()
        p_empty = _valid_report()
        p_empty["citations"] = []
        res_empty = await emit_simulation_report(p_empty, ctx_empty)
        assert res_empty["status"] == "success"
        assert ctx_empty.state["simulation_report"]["citations"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_citations",
        [
            "scalar-string",
            123,
            True,
            {"quote": "not-a-list"},
            [""],
            ["   "],
            [None],
            [123],
            [{}],
            [{"": "missing-key"}],
            [{"quote": ""}],
            [{"quote": 123}],
            [{"source_id": "   "}],
            [{"source_kind": 42}],
            [{"score": float("nan")}],
            [{"score": float("inf")}],
            [{"nested": [{"v": float("-inf")}]}],
            [{"bad_type": object()}],
        ],
    )
    async def test_simulation_report_and_interview_reject_bad_citations(self, bad_citations: object) -> None:
        for fn, base_fn, state_key in (
            (emit_simulation_report, _valid_report, "simulation_report"),
            (emit_stakeholder_interview, _valid_interview, "stakeholder_interview"),
        ):
            ctx = _tool_ctx()
            payload = base_fn()
            payload["citations"] = bad_citations
            res = await fn(payload, ctx)  # type: ignore[arg-type]
            assert res["status"] == "error", f"expected error for {bad_citations!r}"
            assert res["field"] == "citations"
            assert state_key not in ctx.state

    @pytest.mark.asyncio
    async def test_all_mirofish_tools_reject_json_nan_inf_and_non_string_rationale(self) -> None:
        tools = [
            (emit_stakeholder_graph, _valid_graph),
            (emit_stakeholder_personas, _valid_personas),
            (emit_stakeholder_reactions, _valid_reactions),
            (emit_simulation_timeline, _valid_timeline),
            (emit_simulation_report, _valid_report),
            (emit_stakeholder_interview, _valid_interview),
        ]
        for fn, builder in tools:
            # Non-string rationale rejected explicitly.
            ctx = _tool_ctx()
            bad_rat = builder()
            bad_rat["rationale"] = 42
            res = await fn(bad_rat, ctx)  # type: ignore[arg-type]
            assert res["status"] == "error"
            assert res["field"] == "rationale"

            # JSON NaN / Infinity constants rejected cleanly.
            for const in ("NaN", "Infinity", "-Infinity"):
                raw = json.dumps(builder())[:-1] + f', "nan_probe": {const}}}'
                res_nan = await fn(raw, _tool_ctx())
                assert res_nan["status"] == "error"
                assert "invalid JSON" in res_nan["message"]

        # Non-string node.note rejected in stakeholder_graph.
        g = _valid_graph()
        g["nodes"][0]["note"] = 999
        res_note = await emit_stakeholder_graph(g, _tool_ctx())  # type: ignore[arg-type]
        assert res_note["status"] == "error"
        assert res_note["field"] == "nodes"

    @pytest.mark.asyncio
    async def test_retrieval_tools_sanitizes_nan_score_bool_and_unsafe_analysis_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MEMORY_GATEWAY_TOKEN", "bad\r\nX-Inject: 1")
        monkeypatch.setenv("MEMORY_GATEWAY_URL", "http://gateway.example.test:9101")
        captured_urls: list[str] = []
        captured_headers: list[dict[str, str]] = []

        async def _fake_post(url: str, *, json: dict, headers: dict, timeout: float) -> MagicMock:
            captured_urls.append(url)
            captured_headers.append(dict(headers))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "passages": [
                    {
                        "text": "Q3 operating margin risk",
                        "score": float("nan"),
                        "chunk_index": True,
                        "document_id": "doc-1",
                        "source_id": "src-1",
                    },
                    {
                        "text": "Valid passage",
                        "score": 0.85,
                        "chunk_index": 2,
                        "document_id": "doc-2",
                        "source_id": "src-2",
                    },
                ]
            }
            return resp

        fake_client = SimpleNamespace(post=AsyncMock(side_effect=_fake_post))
        ctx = _tool_ctx(state={"memory_analysis_id": "../evil/path"})
        with (
            patch.object(retrieval_tools, "_get_rag_client", return_value=fake_client),
            patch.object(retrieval_tools, "_auto_discover_analysis_id", new=AsyncMock(return_value=("analysis-ok", None))),
        ):
            res = await retrieval_tools.retrieve_document_passages(
                "operating margin",
                ctx,
                analysis_id="../../admin?x=1",
            )
        assert res["status"] == "success"
        assert captured_urls == ["http://gateway.example.test:9101/api/v1/memory/analyses/analysis-ok/retrieve-passages"]
        assert "Authorization" not in captured_headers[0]
        assert res["passages"][0]["score"] == 0.0
        assert res["passages"][0]["chunk_index"] == 0
        assert res["passages"][1]["score"] == 0.85

        # _auto_discover_analysis_id returning (None, None) must fail cleanly without AssertionError even under -O.
        with (
            patch.object(retrieval_tools, "_get_rag_client", return_value=fake_client),
            patch.object(retrieval_tools, "_auto_discover_analysis_id", new=AsyncMock(return_value=(None, None))),
        ):
            res_empty = await retrieval_tools.retrieve_document_passages("operating margin", _tool_ctx())
        assert res_empty["status"] == "no_analysis_bound"

    def test_no_bare_asserts_in_production_modules(self) -> None:
        targets = [
            "agents/leadership_planner_with_memory/_validation.py",
            "agents/leadership_planner_with_memory/simulation_report.py",
            "agents/leadership_planner_with_memory/stakeholder_interview.py",
            "agents/leadership_planner_with_memory/stakeholder_reactions.py",
            "agents/leadership_planner_with_memory/stakeholder_graph.py",
            "agents/leadership_planner_with_memory/stakeholder_personas.py",
            "agents/leadership_planner_with_memory/simulation_timeline.py",
            "agents/leadership_planner_with_memory/memory/retrieval_tools.py",
            "agents/utils/simulation_executor.py",
            "agents/utils/__init__.py",
        ]
        for rel in targets:
            tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
            asserts = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
            assert not asserts, f"bare assert statement(s) in {rel}: lines {asserts}"


# ---------------------------------------------------------------------------
# 3. user_auth_context, simulation_plugin, tenant_keys, env, prompt_builder, global_gemini, utils
# ---------------------------------------------------------------------------


class TestUtilsBugfixes:
    def test_user_auth_context_rejects_control_chars_whitespace_and_non_ascii(self) -> None:
        user_auth_context._reset_for_tests()
        try:
            user_auth_context.set_session_token("s1", "  valid.jwt.token  ")
            assert user_auth_context.get_session_token("s1") == "valid.jwt.token"

            for bad in (
                "tok\r\nX-Inject: 1",
                "tok\nnext",
                "tok with space",
                "tok\x00null",
                "tok\x7fdel",
                "tok-nonascii-\u00e9",
                12345,
            ):
                user_auth_context.set_session_token("s-bad", bad)  # type: ignore[arg-type]
                assert user_auth_context.get_session_token("s-bad") is None
                # Also must not overwrite existing valid token on s1.
                user_auth_context.set_session_token("s1", bad)  # type: ignore[arg-type]
                assert user_auth_context.get_session_token("s1") == "valid.jwt.token"

            # Non-string session_id does not crash.
            user_auth_context.set_session_token(None, "tok")  # type: ignore[arg-type]
            assert user_auth_context.get_session_token(123) is None  # type: ignore[arg-type]
            user_auth_context.clear_session_token(123)  # type: ignore[arg-type]
        finally:
            user_auth_context._reset_for_tests()

    @pytest.mark.asyncio
    async def test_simulation_plugin_close_idempotent_and_exception_safe(self) -> None:
        plugin = SimulationNetworkPlugin()
        fake_dispatcher = MagicMock()
        fake_dispatcher.stop.side_effect = [RuntimeError("stop boom"), None]
        plugin.dispatcher = fake_dispatcher
        plugin.runner = MagicMock()

        with patch("google.adk.plugins.base_plugin.BasePlugin.close", new=AsyncMock()) as super_close:
            await plugin.close()
            assert plugin.dispatcher is None
            assert plugin.runner is None
            assert fake_dispatcher.stop.call_count == 1
            assert super_close.await_count == 1

            # Second close is a safe no-op for dispatcher.stop().
            await plugin.close()
            assert fake_dispatcher.stop.call_count == 1
            assert super_close.await_count == 2

    def test_simulation_plugin_set_runner_resets_safely_on_stop_or_start_failure(self) -> None:
        plugin = SimulationNetworkPlugin()
        old_disp = MagicMock()
        old_disp.stop.side_effect = RuntimeError("old stop failed")
        plugin.dispatcher = old_disp

        new_disp = MagicMock()
        new_disp.start.side_effect = RuntimeError("redis unreachable")
        with (
            patch("agents.utils.simulation_plugin.RedisOrchestratorDispatcher", return_value=new_disp),
            pytest.raises(RuntimeError, match="redis unreachable"),
        ):
            plugin.set_runner(MagicMock())

        assert old_disp.stop.call_count == 1
        assert plugin.dispatcher is None

    def test_tenant_keys_handles_whitespace_non_strings_and_delimiter_collisions(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SAAS_MODE", " on ")
        monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "   ")
        assert tenant_keys.key({"user_id": "   "}, "session", "abc") == "session:abc"
        assert tenant_keys.key({"user_id": 123}, "session", "abc") == "session:abc"  # type: ignore[dict-item]
        assert tenant_keys.key("not-a-mapping", "session", "abc") == "session:abc"  # type: ignore[arg-type]
        assert tenant_keys.key({"user_id": "user-1"}, "session", "  ", "abc") == "u:user-1:session:abc"

        with pytest.raises(ValueError, match="user_id"):
            tenant_keys.key({"user_id": "alice:session"}, "abc")
        with pytest.raises(ValueError, match="user_id"):
            tenant_keys.key({"user_id": "alice\nbob"}, "abc")
        with pytest.raises(ValueError, match="segment"):
            tenant_keys.key({"user_id": "alice"}, "session:hijack")
        monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u:bad")
        with pytest.raises(ValueError, match="ACCOUNT_KEY_PREFIX"):
            tenant_keys.key({"user_id": "alice"}, "session", "abc")

    def test_env_prompt_builder_and_global_gemini_fixes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PROJECT_ID", "   ")
        monkeypatch.setenv("VERTEXAI_PROJECT", "  fallback-proj  ")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        env.configure_project_env("planner")
        assert env.os.environ["GOOGLE_CLOUD_PROJECT"] == "fallback-proj"
        assert env.os.environ["VERTEXAI_PROJECT"] == "fallback-proj"

        pb = PromptBuilder(OrderedDict([("a", "intro"), ("empty", ""), ("b", "body")]))
        assert pb.static("a", "empty", "b") == "intro\n\nbody"

        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "   ")
        monkeypatch.setenv("PROJECT_ID", "   ")
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", " false ")
        monkeypatch.setenv("GEMINI_API_KEY", "  ai-key-123  ")
        model = GlobalGemini(model="gemini-3.1-pro-preview")
        with patch("google.genai.Client") as mock_client:
            _ = model.api_client
        assert mock_client.call_args.kwargs["vertexai"] is False
        assert mock_client.call_args.kwargs["api_key"] == "ai-key-123"

    def test_simulation_executor_and_skill_loader_fail_without_assert(self, tmp_path: pathlib.Path) -> None:
        executor = SimulationExecutor(agent_getter=None, agent_name="planner")
        with pytest.raises(RuntimeError, match="agent_getter"):
            executor._init_runner()

        skill_dir = tmp_path / "demo-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: d\n---\nBody\n", encoding="utf-8")
        (skill_dir / "tools.py").write_text("def hello(): return 1\n", encoding="utf-8")
        with (
            patch("agents.utils._SHARED_SKILLS_DIR", tmp_path),
            patch("importlib.util.spec_from_file_location", return_value=None),
            pytest.raises(ImportError, match="module spec/loader"),
        ):
            load_agent_skills(str(tmp_path))
