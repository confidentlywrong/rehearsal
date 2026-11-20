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

"""Targeted regression tests for ai_echo, ai_echo_reactive, and ai_echo_autopilot."""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.genai import types

from agents.ai_echo import agent as echo_agent
from agents.ai_echo.initialization import compute_wall_severity, fetch_echo_config, format_role_display
from agents.ai_echo_autopilot import agent as echo_auto_agent
from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback
from agents.ai_echo_reactive import agent as echo_react_agent
from agents.ai_echo_reactive.callback import reactive_echo_callback
from agents.utils.models import DEFAULT_FLASH_LITE_MODEL


def _make_ctx(state=None, session_id="sess-test"):
    ctx = MagicMock()
    ctx.state = state if state is not None else {}
    ctx.session = SimpleNamespace(id=session_id)
    ctx.user_content = None
    ctx._user_content = None
    return ctx


def _make_tick_request(tick=1, applied=None, neglected=None):
    event = {
        "event": "tick",
        "tick": tick,
        "decision_text": "Invest in platform reliability",
        "decision_tags": {
            "principles_applied": applied or [],
            "principles_neglected": neglected or [],
        },
        "resource_state": {"lau": {"current": 65}},
        "contradiction_alert": None,
        "collector_buffer_key": "collector:buf:test",
        "influence_deltas": {},
    }
    req = MagicMock()
    req.contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=json.dumps(event))])
    ]
    return req


class _FakeAsyncRedis:
    def __init__(self, values=None, exc=None):
        self._values = values or {}
        self._exc = exc
        self.calls: list[str] = []

    async def get(self, key: str):
        self.calls.append(key)
        if self._exc is not None:
            raise self._exc
        return self._values.get(key)


class TestAsyncEchoConfigLookup:
    @pytest.mark.asyncio
    async def test_reactive_echo_callback_awaits_async_redis_and_routes_by_domain_principle(self):
        fake_redis = _FakeAsyncRedis(
            {
                "echo_config:sess-r": json.dumps(
                    {
                        "role": "vp_engineering",
                        "tier": 2,
                        "domain_principles": ["P2"],
                        "initial_sentiment": -0.2,
                    }
                )
            }
        )
        with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=fake_redis):
            state_irrelevant: dict = {}
            resp = await reactive_echo_callback(
                _make_ctx(state=state_irrelevant, session_id="sess-r"),
                _make_tick_request(tick=1, applied=["P1"]),
            )
            assert fake_redis.calls == ["echo_config:sess-r"]
            assert state_irrelevant["role"] == "vp_engineering"
            assert state_irrelevant["domain_principles"] == ["P2"]
            assert state_irrelevant["_echo_config"]["role"] == "vp_engineering"
            assert resp is not None
            assert resp.content.parts[0].function_call.name == "process_decision_round_tool"

            state_relevant: dict = {}
            resp_llm = await reactive_echo_callback(
                _make_ctx(state=state_relevant, session_id="sess-r"),
                _make_tick_request(tick=2, applied=["P2"]),
            )
            assert resp_llm is None

    @pytest.mark.asyncio
    async def test_autopilot_echo_callback_awaits_async_redis_and_emits_tool_call(self):
        fake_redis = _FakeAsyncRedis(
            {
                "echo_config:sess-a": json.dumps(
                    {
                        "role": "cfo",
                        "tier": 3,
                        "initial_sentiment": -0.6,
                    }
                )
            }
        )
        with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=fake_redis):
            state: dict = {}
            resp = await autopilot_echo_callback(
                _make_ctx(state=state, session_id="sess-a"),
                _make_tick_request(tick=1),
            )
        assert fake_redis.calls == ["echo_config:sess-a"]
        assert state["role"] == "cfo"
        assert state["role_display"] == "CFO"
        assert state["sentiment"] is not None
        assert resp.content.parts[0].function_call.name == "process_decision_round_tool"

    @pytest.mark.asyncio
    async def test_none_redis_and_missing_key_initialize_default_config(self):
        with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=None):
            assert await fetch_echo_config("sess-none") == {}
            state_r: dict = {}
            await reactive_echo_callback(
                _make_ctx(state=state_r, session_id="sess-none"),
                _make_tick_request(tick=1),
            )
            assert state_r["role"] == "unknown"

        fake_redis = _FakeAsyncRedis({})
        with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=fake_redis):
            state_a: dict = {}
            await autopilot_echo_callback(
                _make_ctx(state=state_a, session_id="sess-missing"),
                _make_tick_request(tick=1),
            )
            assert state_a["role"] == "unknown"

    @pytest.mark.asyncio
    async def test_redis_error_and_corrupt_json_raise_explicitly(self):
        err_redis = _FakeAsyncRedis(exc=ConnectionError("redis down"))
        with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=err_redis):
            with pytest.raises(RuntimeError, match="Failed to fetch echo config"):
                await reactive_echo_callback(
                    _make_ctx(state={}, session_id="sess-err"),
                    _make_tick_request(tick=1),
                )

        bad_json_redis = _FakeAsyncRedis({"echo_config:sess-bad": "{not-json"})
        with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=bad_json_redis):
            with pytest.raises(ValueError, match="Invalid JSON"):
                await autopilot_echo_callback(
                    _make_ctx(state={}, session_id="sess-bad"),
                    _make_tick_request(tick=1),
                )

        non_dict_redis = _FakeAsyncRedis({"echo_config:sess-list": '["not", "dict"]'})
        with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=non_dict_redis):
            with pytest.raises(ValueError, match="Expected dict"):
                await fetch_echo_config("sess-list")


class TestEchoModelAndPortConfig:
    def test_blank_echo_model_falls_back_to_default_flash_lite_with_is_gemini(self, monkeypatch):
        try:
            for blank in ("", "   "):
                monkeypatch.setenv("ECHO_MODEL", blank)
                monkeypatch.delenv("RUNNER_MODEL", raising=False)
                reloaded = importlib.reload(echo_agent)
                assert reloaded.DEFAULT_MODEL == DEFAULT_FLASH_LITE_MODEL
                assert reloaded.ECHO_MODEL == DEFAULT_FLASH_LITE_MODEL
                assert reloaded._is_gemini is True
        finally:
            monkeypatch.delenv("ECHO_MODEL", raising=False)
            monkeypatch.delenv("RUNNER_MODEL", raising=False)
            importlib.reload(echo_agent)

    def test_canonical_ports_and_empty_port_guard(self, monkeypatch):
        monkeypatch.setenv("PORT", "")
        monkeypatch.delenv("AI_ECHO_PORT", raising=False)
        monkeypatch.delenv("AI_ECHO_REACTIVE_PORT", raising=False)
        monkeypatch.delenv("AI_ECHO_AUTOPILOT_PORT", raising=False)

        assert echo_agent._resolve_port() == 9123
        assert echo_react_agent._resolve_port() == 9124
        assert echo_auto_agent._resolve_port() == 9125

        monkeypatch.setenv("PORT", "9299")
        assert echo_agent._resolve_port() == 9299

    def test_shared_helpers_compute_wall_severity_and_format_role(self):
        assert compute_wall_severity("hostile", -1.0) == 10.0
        assert compute_wall_severity("supportive", 1.0) == 1.5
        assert format_role_display("vp_eng") == "VP Eng"
