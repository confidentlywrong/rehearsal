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

"""Unit tests for Local Google Gemma + EmbeddingGemma integration."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import AsyncGenerator

from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
import pytest

from agents.utils.global_gemini import GlobalGemini
from agents.utils.local_ai import (
    LocalGemmaOllama,
    get_ai_settings,
    is_local_gemma_active,
    normalize_and_pad_embedding,
    sanitize_request_for_local_gemma,
)
from agents.utils.retry import resilient_model


def test_get_ai_settings_reads_and_reloads_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "ai_settings.json"
    monkeypatch.setenv("AI_SETTINGS_PATH", str(cfg_file))

    # Missing file falls back to cloud_gemini
    s1 = get_ai_settings()
    assert s1.provider == "cloud_gemini"
    assert not is_local_gemma_active()

    # Write local_gemma settings
    cfg_file.write_text(
        json.dumps(
            {
                "provider": "local_gemma",
                "endpoint": "http://localhost:11434/",
                "text_model": "gemma2:9b",
                "embed_model": "embeddinggemma:latest",
            }
        ),
        encoding="utf-8",
    )
    s2 = get_ai_settings()
    assert s2.provider == "local_gemma"
    assert s2.endpoint == "http://localhost:11434"
    assert s2.text_model == "gemma2:9b"
    assert s2.embed_model == "embeddinggemma:latest"
    assert is_local_gemma_active()


def test_sanitize_request_overwrites_model_and_strips_gemini_only_fields() -> None:
    orig = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Hello")],
            )
        ],
        config=types.GenerateContentConfig(
            temperature=0.4,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
            cached_content="projects/test/locations/global/cachedContents/123",
        ),
    )

    sanitized = sanitize_request_for_local_gemma(orig, "ollama_chat/gemma2:9b")
    assert sanitized.model == "ollama_chat/gemma2:9b"
    assert orig.model == "gemini-3-flash-preview"
    assert sanitized.config.thinking_config is None
    assert sanitized.config.cached_content is None
    assert orig.config.thinking_config is not None


@pytest.mark.asyncio
async def test_local_gemma_ollama_prompt_based_function_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_requests: list[tuple[LlmRequest, bool]] = []

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        captured_requests.append((llm_request, stream))
        tool_json = json.dumps(
            {
                "name": "emit_decision_event",
                "parameters": {"decision": "Pause rollout", "turn": 2},
            }
        )
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=tool_json)],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)

    model = LocalGemmaOllama(
        model="ollama_chat/gemma2:9b",
        api_base="http://127.0.0.1:11434",
    )
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Log this decision")],
            )
        ],
        config=types.GenerateContentConfig(
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="emit_decision_event",
                            description="Emit a decision event",
                            parameters=types.Schema(
                                type=types.Type.OBJECT,
                                properties={
                                    "decision": types.Schema(type=types.Type.STRING),
                                    "turn": types.Schema(type=types.Type.INTEGER),
                                },
                                required=["decision"],
                            ),
                        )
                    ]
                )
            ]
        ),
    )

    responses = [
        resp async for resp in model.generate_content_async(req, stream=True)
    ]
    assert len(responses) == 1
    assert len(captured_requests) == 1
    forwarded_req, forwarded_stream = captured_requests[0]
    # Native tools must be stripped so Ollama gemma2:9b doesn't return HTTP 400
    assert not forwarded_req.config.tools
    # Stream must be forced False when tools are active
    assert forwarded_stream is False
    # Overwritten model must be ollama_chat/gemma2:9b
    assert forwarded_req.model == "ollama_chat/gemma2:9b"

    # Response must have extracted Part(function_call=...)
    parts = responses[0].content.parts
    assert len(parts) == 1
    assert parts[0].function_call is not None
    assert parts[0].function_call.name == "emit_decision_event"
    assert parts[0].function_call.args == {
        "decision": "Pause rollout",
        "turn": 2,
    }


@pytest.mark.asyncio
async def test_global_gemini_delegates_to_local_gemma_when_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "ai_settings.json"
    cfg_file.write_text(
        json.dumps(
            {
                "provider": "local_gemma",
                "endpoint": "http://127.0.0.1:11434",
                "text_model": "gemma2:9b",
                "embed_model": "embeddinggemma:latest",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_SETTINGS_PATH", str(cfg_file))

    async def fake_local_generate(
        self: LocalGemmaOllama,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Local Gemma response")],
            )
        )

    monkeypatch.setattr(
        LocalGemmaOllama,
        "generate_content_async",
        fake_local_generate,
    )

    m = resilient_model("gemini-3-flash-preview")
    assert isinstance(m, GlobalGemini)

    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Test")],
            )
        ],
    )
    outputs = [r async for r in m.generate_content_async(req, stream=False)]
    assert len(outputs) == 1
    assert outputs[0].content.parts[0].text == "Local Gemma response"


def test_normalize_and_pad_embedding_preserves_cosine_similarity() -> None:
    v1 = [float(i + 1) * 0.01 for i in range(768)]
    v2 = [float(768 - i) * 0.02 for i in range(768)]

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb)

    orig_sim = cosine(v1, v2)
    p1 = normalize_and_pad_embedding(v1, 3072)
    p2 = normalize_and_pad_embedding(v2, 3072)

    assert len(p1) == 3072
    assert len(p2) == 3072
    assert all(x == 0.0 for x in p1[768:])
    assert math.isclose(orig_sim, cosine(p1, p2), rel_tol=1e-6)


def test_settings_ssrf_guard_and_prefix_stripping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "ai_settings.json"
    monkeypatch.setenv("AI_SETTINGS_PATH", str(cfg_file))
    cfg_file.write_text(
        json.dumps(
            {
                "provider": "local_gemma",
                "endpoint": "http://169.254.169.254:11434",
                "text_model": "ollama_chat/gemma3:12b",
                "embed_model": "ollama/embeddinggemma:latest",
            }
        ),
        encoding="utf-8",
    )
    s = get_ai_settings()
    assert s.endpoint == "http://localhost:11434"
    assert s.text_model == "gemma3:12b"
    assert s.embed_model == "embeddinggemma:latest"


@pytest.mark.asyncio
async def test_local_gemma_zero_arg_tool_and_multiturn_history_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_requests: list[LlmRequest] = []

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        captured_requests.append(llm_request)
        # Zero-arg tool call JSON without "parameters" key
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text='{"name": "list_stations"}')],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)

    model = LocalGemmaOllama(
        model="ollama_chat/gemma3:12b",
        api_base="http://127.0.0.1:11434",
    )
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="List stations")],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="call_prev",
                            name="prev_tool",
                            args={"x": 1},
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="call_prev",
                            name="prev_tool",
                            response={"ok": True},
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="list_stations",
                            description="List stations",
                        )
                    ]
                )
            ]
        ),
    )

    responses = [resp async for resp in model.generate_content_async(req, stream=False)]
    assert len(responses) == 1
    fc = responses[0].content.parts[0].function_call
    assert fc is not None
    assert fc.name == "list_stations"
    assert fc.args == {}
    assert fc.id

    # Verify prior function_call / function_response in contents were converted to text
    # AND that assistant function_call turns preserve role="model" while function_response
    # turns have role="user".
    forwarded = captured_requests[0]
    assert forwarded.contents[1].role == "model"
    assert forwarded.contents[2].role == "user"
    for content in forwarded.contents:
        for p in content.parts:
            assert p.function_call is None
            assert p.function_response is None


@pytest.mark.asyncio
async def test_compute_embedding_raises_instead_of_silent_cloud_fallback_when_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.leadership_planner_with_memory.memory import embeddings as lead_emb
    import agents.utils.local_ai as local_ai_mod

    cfg_file = tmp_path / "ai_settings.json"
    cfg_file.write_text(
        json.dumps(
            {
                "provider": "local_gemma",
                "endpoint": "http://127.0.0.1:11434",
                "text_model": "gemma2:9b",
                "embed_model": "embeddinggemma:latest",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_SETTINGS_PATH", str(cfg_file))

    async def fake_none_embedding(text: str, dimension: int = 3072):
        return None

    monkeypatch.setattr(local_ai_mod, "compute_local_embedding_async", fake_none_embedding)

    with pytest.raises(RuntimeError, match="Local Google embeddinggemma failed"):
        await lead_emb.compute_embedding("test query")


@pytest.mark.asyncio
async def test_local_gemma_does_not_misclassify_domain_json_as_tool_call_and_supports_multiblock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses_to_yield = [
        # Turn 1: Normal prose with domain JSON containing a "name" key (NOT a declared tool)
        'Here is the scenario summary:\n```json\n{"name": "Project Phoenix", "stakeholders": ["VP Eng"]}\n```',
        # Turn 2: Multi-block response with prose code block first and valid tool call second
        'Context:\n```json\n{"note": "preparing call"}\n```\nNow calling tool:\n```json\n{"name": "list_stations", "arguments": {"limit": 3}}\n```',
    ]

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        text = responses_to_yield.pop(0)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=text)],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)

    model = LocalGemmaOllama(
        model="ollama_chat/gemma3:12b",
        api_base="http://127.0.0.1:11434",
    )

    def make_req() -> LlmRequest:
        return LlmRequest(
            model="gemini-3-flash-preview",
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text="Test")],
                )
            ],
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name="list_stations",
                                description="List stations",
                            )
                        ]
                    )
                ]
            ),
        )

    # 1. Domain JSON with "name": "Project Phoenix" must remain text, NOT become a FunctionCall
    r1 = [resp async for resp in model.generate_content_async(make_req(), stream=False)]
    assert r1[0].content.parts[0].function_call is None
    assert "Project Phoenix" in (r1[0].content.parts[0].text or "")

    # 2. Multi-block response extracts the second block's valid tool call
    r2 = [resp async for resp in model.generate_content_async(make_req(), stream=False)]
    fc = r2[0].content.parts[0].function_call
    assert fc is not None
    assert fc.name == "list_stations"
    assert fc.args == {"limit": 3}


def test_build_non_gemini_litellm_routes_gemma2_gemma3_to_local_gemma_ollama() -> None:
    from agents.utils.local_ai import build_non_gemini_litellm

    m_gemma3 = build_non_gemini_litellm("ollama_chat/gemma3:4b")
    assert isinstance(m_gemma3, LocalGemmaOllama)
    assert m_gemma3.model == "ollama_chat/gemma3:4b"

    m_gemma4 = build_non_gemini_litellm("ollama_chat/gemma4:e2b")
    assert isinstance(m_gemma4, LiteLlm)
    assert not isinstance(m_gemma4, LocalGemmaOllama)
    assert m_gemma4.model == "ollama_chat/gemma4:e2b"


def test_validate_local_endpoint_canonicalizes_and_rejects_malformed() -> None:
    from agents.utils.local_ai import (
        _strip_markdown_json_fence,
        _validate_local_endpoint,
        _validate_text_model,
    )

    assert _validate_local_endpoint("http://localhost:11434/v1") == "http://localhost:11434"
    assert _validate_local_endpoint("http://127.0.0.1:11434/api/") == "http://127.0.0.1:11434"
    assert _validate_local_endpoint("http://localhost:11434?x=1") == "http://localhost:11434"
    assert _validate_local_endpoint("http://user:pass@localhost:11434") == "http://localhost:11434"
    assert _validate_local_endpoint("http://localhost:11434/custom/path") == "http://localhost:11434"

    fenced = '```json\n{"scores": {"clarity": 85}}\n```'
    assert _strip_markdown_json_fence(fenced) == '{"scores": {"clarity": 85}}'
    prose_wrapped = 'Here is your evaluation:\n{"scores": {"clarity": 85}}\nThank you!'
    assert _strip_markdown_json_fence(prose_wrapped) == '{"scores": {"clarity": 85}}'

    assert _validate_text_model("bundle:gemma2:9b") == "gemma2:9b"
    assert _validate_text_model("embeddinggemma:latest") == "gemma2:9b"
    assert _validate_text_model("gemma3:12b;rm") == "gemma2:9b"


@pytest.mark.asyncio
async def test_local_gemma_serializes_datetime_and_uuid_tool_responses_without_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone
    import uuid

    captured: list[LlmRequest] = []

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        captured.append(llm_request)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Done")],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)

    model = LocalGemmaOllama(
        model="ollama_chat/gemma2:9b",
        api_base="http://127.0.0.1:11434",
    )
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="call_1",
                            name="recall_past_simulations",
                            response={
                                "created_at": datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc),
                                "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
                            },
                        )
                    )
                ],
            )
        ],
    )
    out = [r async for r in model.generate_content_async(req, stream=False)]
    assert len(out) == 1
    assert len(captured) == 1
    assert "2026-11-05" in (captured[0].contents[0].parts[0].text or "")


@pytest.mark.asyncio
async def test_local_gemma_extracts_tool_call_with_thought_part_and_trailing_braces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(text="Thinking about calling tool...", thought=True),
                    types.Part.from_text(
                        text='{"name": "list_stations", "parameters": {"city": "Boston"}}\nNote: {"status": "pending"}'
                    ),
                ],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)

    model = LocalGemmaOllama(
        model="ollama_chat/gemma3:12b",
        api_base="http://127.0.0.1:11434",
    )
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="List Boston stations")],
            )
        ],
        config=types.GenerateContentConfig(
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="list_stations",
                            description="List stations",
                        )
                    ]
                )
            ]
        ),
    )
    out = [r async for r in model.generate_content_async(req, stream=False)]
    assert len(out) == 1
    parts = out[0].content.parts
    assert len(parts) == 2
    assert getattr(parts[0], "thought", False) is True
    assert parts[1].function_call is not None
    assert parts[1].function_call.name == "list_stations"
    assert parts[1].function_call.args == {"city": "Boston"}


@pytest.mark.asyncio
async def test_marathon_evaluate_plan_with_local_gemma_does_not_raise_key_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.planner_with_eval.evaluator.tools import evaluate_plan
    import agents.utils.local_ai as local_ai_mod

    cfg_file = tmp_path / "ai_settings.json"
    cfg_file.write_text(
        json.dumps(
            {
                "provider": "local_gemma",
                "endpoint": "http://127.0.0.1:11434",
                "text_model": "gemma2:9b",
                "embed_model": "embeddinggemma:latest",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_SETTINGS_PATH", str(cfg_file))

    calls: list[str] = []

    async def fake_generate_local_json(
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps(
                {
                    "score": 4,
                    "explanation": {
                        "safety_compliance": 4,
                        "logistics_completeness": 4,
                        "participant_experience": 4,
                        "community_impact": 4,
                        "financial_viability": 4,
                        "intent_alignment": 5,
                    },
                }
            )
        return json.dumps(
            {
                "suggestions": ["Add medical tent at mile 18."],
                "summary": "Strong 26.2 mile marathon plan.",
            }
        )

    monkeypatch.setattr(local_ai_mod, "generate_local_json_async", fake_generate_local_json)

    res = await evaluate_plan(
        json.dumps(
            {
                "user_intent": "Plan a Boston marathon",
                "proposed_plan": "26.2 miles loop with medical tents and water stations.",
            }
        )
    )
    assert res["eval_method"] == "local_gemma_eval"
    assert res["passed"] is True
    assert res["scores"]["intent_alignment"] == 100


@pytest.mark.asyncio
async def test_echo_before_agent_callback_awaits_async_redis_and_handles_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from agents.ai_echo.agent import _echo_before_agent_callback
    import agents.utils.redis_pool as redis_pool_mod

    class FakeAsyncRedis:
        async def get(self, key: str) -> str | None:
            if key == "echo_config:sess-42":
                return json.dumps(
                    {
                        "role": "CFO",
                        "tier": 1,
                        "initial_sentiment": -0.4,
                        "initial_stance": "skeptical",
                    }
                )
            return None

    monkeypatch.setattr(redis_pool_mod, "get_shared_redis_client", lambda: FakeAsyncRedis())
    ctx = SimpleNamespace(
        state={},
        session=SimpleNamespace(id="sess-42", events=[]),
    )
    await _echo_before_agent_callback(ctx)
    assert ctx.state["role"] == "CFO"
    assert ctx.state["tier"] == 1
    assert -0.55 <= ctx.state["sentiment"] <= -0.25
    assert ctx.state["_echo_config"]["initial_stance"] == "skeptical"
    assert ctx.state["stance"] in ("resistant", "hostile", "skeptical")

    # Also verify r=None does not raise AttributeError
    monkeypatch.setattr(redis_pool_mod, "get_shared_redis_client", lambda: None)
    ctx_none = SimpleNamespace(
        state={},
        session=SimpleNamespace(id="sess-none", events=[]),
    )
    await _echo_before_agent_callback(ctx_none)
    assert ctx_none.state["sentiment"] is not None


def test_sync_echo_callbacks_handle_async_redis_coroutine_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    import agents.ai_echo_autopilot.autopilot as autopilot_mod
    import agents.ai_echo_reactive.callback as reactive_mod

    class FakeAsyncRedis:
        async def get(self, key: str) -> str | None:
            return '{"role": "VP"}'

    monkeypatch.setattr(autopilot_mod, "get_shared_redis_client", lambda: FakeAsyncRedis())
    monkeypatch.setattr(reactive_mod, "get_shared_redis_client", lambda: FakeAsyncRedis())

    event_json = json.dumps({"event": "tick", "tick": 1, "decision_tags": {}})
    llm_req = SimpleNamespace(
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=event_json)])]
    )
    ctx_auto = SimpleNamespace(
        state={},
        session=SimpleNamespace(id="sess-auto"),
    )
    resp_auto = autopilot_mod.autopilot_echo_callback(ctx_auto, llm_req)
    assert resp_auto is not None
    assert ctx_auto.state.get("sentiment") is not None

    ctx_react = SimpleNamespace(
        state={},
        session=SimpleNamespace(id="sess-react"),
    )
    resp_react = reactive_mod.reactive_echo_callback(ctx_react, llm_req)
    assert resp_react is not None
    assert ctx_react.state.get("sentiment") is not None


@pytest.mark.asyncio
async def test_leadership_evaluator_markdown_fence_and_deterministic_suggestions_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import agents.leadership_planner_with_eval.evaluator.tools as eval_tools
    import agents.utils.local_ai as local_ai_mod

    # 1. _generate_feedback when local gemma is active returns deterministic suggestions without Ollama
    called_local = []

    async def fake_local_json_fenced(prompt: str, **kwargs: object) -> str:
        called_local.append(prompt)
        return '```json\n{"suggestions": ["Clarify the Q3 budget cap."], "summary": "Solid structure."}\n```'

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)
    monkeypatch.setattr(local_ai_mod, "generate_local_json_async", fake_local_json_fenced)

    suggs, summary = await eval_tools._generate_feedback(
        scores={"decision_quality": 70},
        details={},
        user_intent="Test intent",
        proposed_plan="Test plan",
    )
    assert len(called_local) == 0  # Bypassed directly
    assert any("dilemma complexity" in s.lower() for s in suggs)
    assert "Leadership scenario evaluation complete" in summary

    # 2. When local gemma is inactive (cloud mode), markdown fences around JSON are stripped
    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: False)
    mock_client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(
                    return_value=SimpleNamespace(
                        text='```json\n{"suggestions": ["Clarify the Q3 budget cap."], "summary": "Solid structure."}\n```'
                    )
                )
            )
        )
    )
    monkeypatch.setattr(eval_tools, "_get_client", lambda: mock_client)
    suggs_cloud, summary_cloud = await eval_tools._generate_feedback(
        scores={"decision_quality": 70},
        details={},
        user_intent="Test intent",
        proposed_plan="Test plan",
    )
    assert suggs_cloud == ["Clarify the Q3 budget cap."]
    assert summary_cloud == "Solid structure."

    # 3. evaluate_leadership_plan in cloud mode falls back to _DETERMINISTIC_SUGGESTIONS when
    # _generate_feedback fails and findings is empty but a criterion is < 80
    mock_eval_client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(
                    return_value=SimpleNamespace(
                        text=json.dumps(
                            {
                                "scores": {
                                    "stakeholder_alignment": 85,
                                    "decision_quality": 60,
                                    "resource_stewardship": 85,
                                    "crisis_readiness": 85,
                                    "strategic_coherence": 85,
                                    "ethical_alignment": 85,
                                },
                                "findings": [],
                                "summary": "Judge summary.",
                            }
                        )
                    )
                )
            )
        )
    )
    async def fake_feedback_boom(**kwargs: object) -> tuple[list[str], str]:
        raise RuntimeError("feedback call failed")

    monkeypatch.setattr(eval_tools, "_get_client", lambda: mock_eval_client)
    monkeypatch.setattr(eval_tools, "_generate_feedback", fake_feedback_boom)

    res = await eval_tools.evaluate_leadership_plan(
        json.dumps({"user_intent": "Test", "proposed_plan": "Plan with weak decision quality"})
    )
    assert any("dilemma complexity" in s.lower() for s in res["improvement_suggestions"])


def test_genai_client_vertex_and_api_key_modes_across_evaluator_and_global_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util
    from types import SimpleNamespace
    import agents.leadership_planner_with_eval.evaluator.tools as lead_eval_tools
    import agents.leadership_planner_with_memory.memory.embeddings as lead_emb
    import agents.planner_with_memory.memory.embeddings as plan_emb
    import agents.planner_with_memory.memory.tools as plan_mem_tools

    td_path = (
        Path(__file__).resolve().parent.parent
        / "leadership_planner_with_eval"
        / "skills"
        / "translation-diagnostic"
        / "tools.py"
    )
    td_spec = importlib.util.spec_from_file_location("td_tools_test", td_path)
    assert td_spec and td_spec.loader
    td_tools = importlib.util.module_from_spec(td_spec)
    td_spec.loader.exec_module(td_tools)

    # Reset cached clients
    lead_eval_tools._client = None
    lead_eval_tools._client_key = None
    td_tools._client = None
    td_tools._client_key = None
    lead_emb._get_genai_client.cache_clear()
    plan_emb._get_genai_client.cache_clear()

    # 1. API-key mode (no GCP project, GEMINI_API_KEY set) must NOT pass project/location
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-api-key")

    c_eval_api = lead_eval_tools._get_client()
    assert c_eval_api.vertexai is False
    c_td_api = td_tools._get_client()
    assert c_td_api.vertexai is False

    g_api = GlobalGemini(model="gemini-3-flash-preview")
    assert g_api.api_client.vertexai is False

    # 2. Vertex AI mode (PROJECT_ID set, GOOGLE_GENAI_USE_VERTEXAI unset) must pass vertexai=True
    monkeypatch.setenv("PROJECT_ID", "test-gcp-project")
    c_eval_vtx = lead_eval_tools._get_client()
    assert c_eval_vtx.vertexai is True
    c_td_vtx = td_tools._get_client()
    assert c_td_vtx.vertexai is True

    g_vtx = GlobalGemini(model="gemini-3-flash-preview")
    assert g_vtx.api_client.vertexai is True

    # 3. Embeddings _get_genai_client honors GOOGLE_GENAI_USE_VERTEXAI=false + GOOGLE_API_KEY
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-api-key")
    lead_emb._get_genai_client.cache_clear()
    plan_emb._get_genai_client.cache_clear()
    assert lead_emb._get_genai_client().vertexai is False
    assert plan_emb._get_genai_client().vertexai is False
    lead_emb._get_genai_client.cache_clear()
    plan_emb._get_genai_client.cache_clear()

    # 4. _resolve_owner_id in planner_with_memory treats ADK default "user" as anonymous
    monkeypatch.setenv("PLANNER_PER_USER_ISOLATION", "on")
    ctx_user = SimpleNamespace(session=SimpleNamespace(user_id="user"))
    assert plan_mem_tools._resolve_owner_id(ctx_user) is None
    ctx_real = SimpleNamespace(session=SimpleNamespace(user_id="alice@example.com"))
    assert plan_mem_tools._resolve_owner_id(ctx_real) == "alice@example.com"


@pytest.mark.asyncio
async def test_compact_tool_returns_in_prozesskette() -> None:
    from types import SimpleNamespace
    import importlib.util

    # 1. design_leadership_scenario
    spec = importlib.util.spec_from_file_location(
        "sd_tools",
        Path(__file__).resolve().parent.parent / "leadership_planner" / "skills" / "scenario-design" / "scripts" / "tools.py",
    )
    sd_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sd_tools)

    ctx = SimpleNamespace(state={})
    res = await sd_tools.design_leadership_scenario(tool_context=ctx)
    assert res["status"] == "success"
    assert "NEXT STEP: Call plan_consensus_pathway()" in res["message"]
    assert "leadership_scenario" in ctx.state
    assert "echoes" not in res  # Full echoes must not be in the LLM return receipt
    assert len(json.dumps(res)) < 400  # Compact summary (<60 tokens)

    # 2. plan_consensus_pathway
    pathway_res = await sd_tools.plan_consensus_pathway(tool_context=ctx)
    assert pathway_res["status"] == "success"
    assert "geojson" not in pathway_res  # GeoJSON stripped from LLM return
    assert "NEXT STEP: Call report_leadership_scenario()" in pathway_res["message"]
    assert "consensus_pathway_active" in ctx.state
    assert "geojson" in ctx.state["consensus_pathway_active"]

    # 3. map_stakeholders
    m_spec = importlib.util.spec_from_file_location(
        "sm_tools",
        Path(__file__).resolve().parent.parent / "leadership_planner" / "skills" / "stakeholder-mapping" / "scripts" / "tools.py",
    )
    sm_tools = importlib.util.module_from_spec(m_spec)
    m_spec.loader.exec_module(sm_tools)

    map_res = await sm_tools.map_stakeholders(tool_context=ctx)
    assert map_res["status"] == "success"
    assert "stakeholder_map" not in map_res  # Full persona list stripped from LLM return
    assert "roles" in map_res
    assert "NEXT STEP: Call evaluate_leadership_plan()" in map_res["message"]
    assert "stakeholder_map" in ctx.state


def test_sanitize_request_for_local_gemma_8k_and_curated_tools() -> None:
    from agents.leadership_planner_with_memory.prompts import LOCAL_GEMMA_INSTRUCTION

    long_leadership_prompt = "Leadership Planner System Instructions:\n" + ("rule\n" * 1000)
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[types.Content(role="user", parts=[types.Part.from_text(text="Plan scenario")])],
        config=types.GenerateContentConfig(
            system_instruction=long_leadership_prompt,
            tools=[
                types.Tool(function_declarations=[
                    types.FunctionDeclaration(name="design_leadership_scenario"),
                    types.FunctionDeclaration(name="unneeded_tool_xyz"),
                ])
            ],
        ),
    )

    sanitized = sanitize_request_for_local_gemma(req, "ollama_chat/gemma2:9b")
    # Verify 8K context options
    assert sanitized.config.extra_body.get("options", {}).get("num_ctx") == 8192
    assert sanitized.config.extra_body.get("num_ctx") == 8192
    # Verify system instruction was condensed
    assert sanitized.config.system_instruction == LOCAL_GEMMA_INSTRUCTION
    # Verify tools were curated down to core tools
    decls = sanitized.config.tools[0].function_declarations
    tool_names = [d.name for d in decls]
    assert "design_leadership_scenario" in tool_names
    assert "unneeded_tool_xyz" not in tool_names


@pytest.mark.asyncio
async def test_local_gemma_ollama_history_compacts_geojson() -> None:
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")
    assert llm._additional_args.get("options", {}).get("num_ctx") == 8192
    assert llm._additional_args.get("num_ctx") == 8192

    # Simulate tool response with large GeoJSON in report_leadership_scenario
    heavy_geojson = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"coordinates": [1, 2] * 500}}]}
    req = LlmRequest(
        model="ollama_chat/gemma2:9b",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="report_leadership_scenario",
                            response={"status": "success", "scenario_summary": {"consensus_pathway": heavy_geojson}},
                        )
                    )
                ],
            )
        ],
        config=types.GenerateContentConfig(),
    )

    llm._move_function_calls_into_system_instruction(req)
    # The text of the converted part must NOT contain the 500 coordinates
    converted_text = req.contents[0].parts[0].text
    assert "[GeoJSON mapped to visualization layer]" in converted_text
    assert "coordinates" not in converted_text


@pytest.mark.asyncio
async def test_dispatcher_cancels_stale_session_task() -> None:
    import asyncio
    from agents.utils.dispatcher import RedisOrchestratorDispatcher

    disp = RedisOrchestratorDispatcher(runner=None)
    disp._loop = asyncio.get_running_loop()

    session_id = "test_sess_cancel"
    started_first = asyncio.Event()
    cancelled_first = False

    async def mock_run_logic(sid, content, pulses_collector=None):
        nonlocal cancelled_first
        try:
            started_first.set()
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            cancelled_first = True
            raise

    disp._trigger_agent_run_logic = mock_run_logic

    # Trigger first run
    disp._trigger_agent_run(session_id, {"msg": 1})
    await started_first.wait()
    assert session_id in disp._active_session_tasks
    first_task = disp._active_session_tasks[session_id]

    # Trigger second run for same session -> should cancel first task
    disp._trigger_agent_run(session_id, {"msg": 2})
    # Yield control to let event loop process cancellation
    await asyncio.sleep(0.05)

    assert first_task.cancelled() or first_task.done()
    assert cancelled_first is True


@pytest.mark.asyncio
async def test_local_gemma_repairs_unescaped_nested_quotes_and_truncated_a2ui_tool_call() -> None:
    """Reproduces the exact Kraft Heinz screenshot bug where Gemma outputs unescaped JSON inside payload."""
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")
    allowed = {
        "design_leadership_scenario",
        "evaluate_leadership_plan",
        "validate_and_emit_a2ui",
    }

    # 1. Exact malformed / truncated tool call from screenshot (unescaped quotes inside payload)
    broken_screenshot_text = (
        '{"name": "validate_and_emit_a2ui", "parameters": {"payload": '
        '"{"surfaceUpdate": {"card": {"title": "Enterprise AI Transformation", '
        '"summary": "Kraft Heinz is undergoing a complex global business split...'
    )
    resp1 = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=broken_screenshot_text)],
        )
    )
    llm._extract_function_calls_from_response(resp1, allowed_tool_names=allowed)
    assert len(resp1.content.parts) == 1
    fc1 = resp1.content.parts[0].function_call
    assert fc1 is not None
    assert fc1.name == "validate_and_emit_a2ui"
    assert fc1.args == {"payload": "dashboard"}
    assert resp1.content.parts[0].text is None  # Raw JSON must NOT remain as text

    # 2. Dict-typed payload parameter (when Gemma omits outer string quotes around payload)
    dict_payload_text = json.dumps(
        {
            "name": "validate_and_emit_a2ui",
            "parameters": {
                "payload": {"surfaceUpdate": {"card": {"title": "Kraft Heinz"}}}
            },
        }
    )
    resp2 = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=dict_payload_text)],
        )
    )
    llm._extract_function_calls_from_response(resp2, allowed_tool_names=allowed)
    fc2 = resp2.content.parts[0].function_call
    assert fc2 is not None
    assert fc2.name == "validate_and_emit_a2ui"
    assert isinstance(fc2.args["payload"], str)

    # 3. Unescaped quotes inside evaluate_leadership_plan
    broken_eval_text = (
        '{"name": "evaluate_leadership_plan", "parameters": {"evaluation_request": '
        '"{"user_intent": "Kraft Heinz split", "proposed_plan": "DCNS"}"}}'
    )
    resp3 = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=broken_eval_text)],
        )
    )
    llm._extract_function_calls_from_response(resp3, allowed_tool_names=allowed)
    fc3 = resp3.content.parts[0].function_call
    assert fc3 is not None
    assert fc3.name == "evaluate_leadership_plan"
    assert isinstance(fc3.args.get("evaluation_request"), str)

    # 4. Truncated design_leadership_scenario call recovers scalar parameters
    truncated_design = (
        '{"name": "design_leadership_scenario", "parameters": '
        '{"scenario_type": "transformation", "complexity": "high", "stakeholder_count": 1000'
    )
    resp4 = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=truncated_design)],
        )
    )
    llm._extract_function_calls_from_response(resp4, allowed_tool_names=allowed)
    fc4 = resp4.content.parts[0].function_call
    assert fc4 is not None
    assert fc4.name == "design_leadership_scenario"
    assert fc4.args["scenario_type"] == "transformation"
    assert fc4.args["complexity"] == "high"
    assert fc4.args["stakeholder_count"] == 1000


@pytest.mark.asyncio
async def test_validate_and_emit_a2ui_builds_dashboard_from_session_state_and_self_heals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify validate_and_emit_a2ui deterministically builds a valid A2UI v0.8.0 dashboard from state."""
    import importlib.util
    from types import SimpleNamespace
    import agents.leadership_planner_with_memory.memory.tools as mem_tools
    from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore

    monkeypatch.setattr(mem_tools, "_store", ScenarioMemoryStore())

    a2ui_path = Path(__file__).resolve().parent.parent / "skills" / "a2ui-rendering" / "tools.py"
    spec = importlib.util.spec_from_file_location("a2ui_tools_test", a2ui_path)
    assert spec and spec.loader
    a2ui_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a2ui_mod)

    ctx = SimpleNamespace(
        state={
            "leadership_scenario": {
                "name": "Kraft Heinz Enterprise AI Transformation",
                "scenario_type": "transformation",
                "stakeholder_count": 1000,
                "complexity": "high",
                "summary": "Navigating the global business split while scaling AI.",
            },
            "leadership_evaluation": {
                "overall_score": 82,
                "scores": {
                    "stakeholder_alignment": 85,
                    "decision_quality": 78,
                    "resource_stewardship": 84,
                    "crisis_readiness": 76,
                    "strategic_coherence": 86,
                    "ethical_alignment": 83,
                },
                "findings": [
                    {
                        "title": "Shadow IT Resistance in Regional Silos",
                        "severity": "MODERATE",
                        "description": "Mid-level managers in regional units show high defection risk during the split.",
                    }
                ],
                "summary": "Strong strategic coherence with moderate regional resistance.",
            },
        }
    )

    # 1. Calling with shorthand payload="dashboard"
    res1 = await a2ui_mod.validate_and_emit_a2ui(payload="dashboard", tool_context=ctx)
    assert res1["status"] == "success"
    assert "a2ui" in res1
    violations = a2ui_mod._validate_surface_update(res1["a2ui"])
    assert violations == []

    # 2. Calling with the hallucinated nested card schema from the user's screenshot
    hallucinated_payload = json.dumps(
        {
            "surfaceUpdate": {
                "card": {
                    "title": "Enterprise AI Transformation",
                    "summary": "Kraft Heinz is undergoing a complex global business split...",
                }
            }
        }
    )
    res2 = await a2ui_mod.validate_and_emit_a2ui(payload=hallucinated_payload, tool_context=ctx)
    assert res2["status"] == "success"
    assert "a2ui" in res2
    assert a2ui_mod._validate_surface_update(res2["a2ui"]) == []


@pytest.mark.asyncio
async def test_local_gemma_organizer_fast_paths_and_post_eval_dashboard_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Organizer UI requests and Step 5->6 dashboard transition bypass redundant Ollama calls."""
    super_calls: list[LlmRequest] = []

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        super_calls.append(llm_request)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Executive summary of the scenario.")],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")

    planner_tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="design_leadership_scenario"),
                types.FunctionDeclaration(name="evaluate_leadership_plan"),
                types.FunctionDeclaration(name="validate_and_emit_a2ui"),
                types.FunctionDeclaration(name="emit_scenario_list_a2ui"),
                types.FunctionDeclaration(name="emit_scenario_detail_a2ui"),
            ]
        )
    ]

    # 1. Organizer list request -> immediate FunctionCall(emit_scenario_list_a2ui), 0 Ollama calls
    req_list = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="list the top 3 best scenarios")],
            )
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out_list = [r async for r in llm.generate_content_async(req_list)]
    assert len(super_calls) == 0
    assert out_list[0].content.parts[0].function_call.name == "emit_scenario_list_a2ui"
    assert out_list[0].content.parts[0].function_call.args == {"limit": 3}

    # 2. Organizer detail request -> immediate FunctionCall(emit_scenario_detail_a2ui), 0 Ollama calls
    req_detail = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Get the route for the seed abc-123")],
            )
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out_detail = [r async for r in llm.generate_content_async(req_detail)]
    assert len(super_calls) == 0
    assert out_detail[0].content.parts[0].function_call.name == "emit_scenario_detail_a2ui"
    assert out_detail[0].content.parts[0].function_call.args == {"scenario_id": "abc-123"}

    # 3. Post-evaluate_leadership_plan turn -> immediate FunctionCall(validate_and_emit_a2ui(payload="dashboard"))
    req_post_eval = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Design transformation scenario")],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="call_eval",
                            name="evaluate_leadership_plan",
                            response={"overall_score": 82, "passed": True},
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out_post_eval = [r async for r in llm.generate_content_async(req_post_eval)]
    assert len(super_calls) == 0
    assert out_post_eval[0].content.parts[0].function_call.name == "validate_and_emit_a2ui"
    assert out_post_eval[0].content.parts[0].function_call.args == {"payload": "dashboard"}

    # 4. Post-validate_and_emit_a2ui turn -> directly yields executive summary with 0 Ollama calls
    req_post_a2ui = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Design transformation scenario")],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="call_a2ui",
                            name="validate_and_emit_a2ui",
                            response={"status": "success", "message": "A2UI dashboard emitted successfully."},
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out_summary = [r async for r in llm.generate_content_async(req_post_a2ui)]
    assert len(super_calls) == 0
    assert out_summary[0].content.parts[0].function_call is None
    assert "Executive Summary" in (out_summary[0].content.parts[0].text or "")


@pytest.mark.asyncio
async def test_full_6_step_local_gemma_leadership_prozesskette_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute all 6 steps of the Local Gemma planning Prozesskette and verify state and A2UI output."""
    import importlib.util
    from types import SimpleNamespace
    import agents.leadership_planner_with_eval.evaluator.tools as eval_tools
    import agents.leadership_planner_with_memory.memory.tools as mem_tools
    from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore
    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)
    monkeypatch.setattr(mem_tools, "_store", ScenarioMemoryStore())

    ollama_eval_calls: list[str] = []

    async def fake_local_json(prompt: str, **kwargs: object) -> str:
        ollama_eval_calls.append(prompt)
        return json.dumps(
            {
                "scores": {
                    "stakeholder_alignment": 84,
                    "decision_quality": 79,
                    "resource_stewardship": 88,
                    "crisis_readiness": 75,
                    "strategic_coherence": 86,
                    "ethical_alignment": 90,
                },
                "findings": [
                    {
                        "title": "Regional Business Split Friction",
                        "severity": "MODERATE",
                        "description": "Shared service dependencies require phased transition gates.",
                    }
                ],
                "summary": "Balanced consensus pathway with clear governance milestones.",
            }
        )

    monkeypatch.setattr(local_ai_mod, "generate_local_json_async", fake_local_json)

    sd_spec = importlib.util.spec_from_file_location(
        "sd_tools_e2e",
        Path(__file__).resolve().parent.parent / "leadership_planner" / "skills" / "scenario-design" / "scripts" / "tools.py",
    )
    sd_tools = importlib.util.module_from_spec(sd_spec)
    sd_spec.loader.exec_module(sd_tools)

    sm_spec = importlib.util.spec_from_file_location(
        "sm_tools_e2e",
        Path(__file__).resolve().parent.parent / "leadership_planner" / "skills" / "stakeholder-mapping" / "scripts" / "tools.py",
    )
    sm_tools = importlib.util.module_from_spec(sm_spec)
    sm_spec.loader.exec_module(sm_tools)

    a2ui_spec = importlib.util.spec_from_file_location(
        "a2ui_tools_e2e",
        Path(__file__).resolve().parent.parent / "skills" / "a2ui-rendering" / "tools.py",
    )
    a2ui_tools = importlib.util.module_from_spec(a2ui_spec)
    a2ui_spec.loader.exec_module(a2ui_tools)

    ctx = SimpleNamespace(state={})

    # Step 1: design_leadership_scenario
    r1 = await sd_tools.design_leadership_scenario(
        scenario_type="transformation",
        budget_allocation="balanced",
        complexity="high",
        stakeholder_count=1000,
        tool_context=ctx,
    )
    assert r1["status"] == "success"

    # Step 2: plan_consensus_pathway
    r2 = await sd_tools.plan_consensus_pathway(
        scenario_type="transformation",
        budget_allocation="balanced",
        complexity="high",
        stakeholder_count=1000,
        tool_context=ctx,
    )
    assert r2["status"] == "success"

    # Step 3: report_leadership_scenario
    r3 = await sd_tools.report_leadership_scenario(tool_context=ctx)
    assert r3["status"] == "success"

    # Step 4: map_stakeholders
    r4 = await sm_tools.map_stakeholders(tool_context=ctx)
    assert r4["status"] == "success"

    # Step 5: evaluate_leadership_plan (bypasses CPU Ollama and uses DCNS heuristic scoring)
    r5 = await eval_tools.evaluate_leadership_plan(
        evaluation_request="Enterprise transformation plan",
        tool_context=ctx,
    )
    assert r5["overall_score"] > 0
    assert r5["eval_method"] == "dcns_heuristic"
    assert len(ollama_eval_calls) == 0  # CPU Ollama call bypassed completely
    assert 'validate_and_emit_a2ui(payload="dashboard")' in r5["message"]

    # Step 6: validate_and_emit_a2ui
    r6 = await a2ui_tools.validate_and_emit_a2ui(payload="dashboard", tool_context=ctx)
    assert r6["status"] == "success"
    assert a2ui_tools._validate_surface_update(r6["a2ui"]) == []


@pytest.mark.asyncio
async def test_local_aware_skill_toolset_dynamic_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _LocalAwareSkillToolset gates tools behind SkillToolset in cloud mode and exposes them directly in local_gemma mode."""
    import agents.leadership_planner_with_eval.adk_tools as eval_adk_tools
    import agents.leadership_planner_with_memory.adk_tools as mem_adk_tools
    import agents.utils.local_ai as local_ai_mod
    from google.adk.tools.skill_toolset import SkillToolset

    for mod in (eval_adk_tools, mem_adk_tools):
        tools = mod.get_tools()
        top_names = [
            getattr(t, "name", None)
            for t in tools
            if not isinstance(t, SkillToolset)
        ]
        # Top-level tools must contain validate_and_emit_a2ui, but NOT consultant skill tools
        assert "validate_and_emit_a2ui" in top_names
        assert "emit_sim_results_a2ui" not in top_names
        assert "emit_action_plan_a2ui" not in top_names

        skill_ts = next(t for t in tools if isinstance(t, SkillToolset))

        monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: False)
        cloud_tools = await skill_ts.get_tools()
        cloud_names = {getattr(t, "name", None) for t in cloud_tools}
        assert "load_skill" in cloud_names
        assert "emit_sim_results_a2ui" not in cloud_names

        monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)
        local_tools = await skill_ts.get_tools()
        local_names = {getattr(t, "name", None) for t in local_tools}
        assert "design_leadership_scenario" in local_names
        assert "plan_consensus_pathway" in local_names
        assert "report_leadership_scenario" in local_names
        assert "map_stakeholders" in local_names
        assert "emit_sim_results_a2ui" in local_names
        assert "emit_action_plan_a2ui" in local_names


@pytest.mark.asyncio
async def test_deterministic_prozesskette_all_steps_and_second_scenario_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Steps 1->2->3->4->5->6 transition deterministically and support a 2nd scenario in the same session."""
    super_calls: list[LlmRequest] = []

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        super_calls.append(llm_request)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Executive summary.")],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")

    planner_tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="design_leadership_scenario"),
                types.FunctionDeclaration(name="plan_consensus_pathway"),
                types.FunctionDeclaration(name="report_leadership_scenario"),
                types.FunctionDeclaration(name="map_stakeholders"),
                types.FunctionDeclaration(name="evaluate_leadership_plan"),
                types.FunctionDeclaration(name="validate_and_emit_a2ui"),
            ]
        )
    ]

    user_prompt = "Kraft Heinz is undergoing a complex global business split while scaling AI."
    history: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)]),
        types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="c1",
                        name="design_leadership_scenario",
                        args={
                            "scenario_type": "transformation",
                            "budget_allocation": "balanced",
                            "complexity": "high",
                            "stakeholder_count": 1000,
                        },
                    )
                )
            ],
        ),
        types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="c1",
                        name="design_leadership_scenario",
                        response={"status": "success"},
                    )
                )
            ],
        ),
    ]

    expected_chain = [
        "plan_consensus_pathway",
        "report_leadership_scenario",
        "map_stakeholders",
        "evaluate_leadership_plan",
        "validate_and_emit_a2ui",
    ]
    for idx, expected_tool in enumerate(expected_chain):
        req = LlmRequest(
            model="gemini-3-flash-preview",
            contents=list(history),
            config=types.GenerateContentConfig(tools=planner_tools),
        )
        out = [r async for r in llm.generate_content_async(req)]
        fc = out[0].content.parts[0].function_call
        assert fc is not None
        assert fc.name == expected_tool
        if expected_tool == "evaluate_leadership_plan":
            assert fc.args["evaluation_request"] == user_prompt
        if expected_tool == "validate_and_emit_a2ui":
            assert fc.args == {"payload": "dashboard"}
        history.append(out[0].content)
        history.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=fc.id or f"c_{idx}",
                            name=expected_tool,
                            response={"status": "success", "overall_score": 85},
                        )
                    )
                ],
            )
        )

    # Zero Ollama calls were needed for the 5 intermediate transitions
    assert len(super_calls) == 0

    # Second scenario in the same session must still emit validate_and_emit_a2ui
    history.extend(
        [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Now design a crisis turnaround scenario")],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="eval_2",
                            name="evaluate_leadership_plan",
                            response={"status": "success", "overall_score": 78},
                        )
                    )
                ],
            ),
        ]
    )
    req2 = LlmRequest(
        model="gemini-3-flash-preview",
        contents=list(history),
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out2 = [r async for r in llm.generate_content_async(req2)]
    assert out2[0].content.parts[0].function_call is not None
    assert out2[0].content.parts[0].function_call.name == "validate_and_emit_a2ui"


@pytest.mark.asyncio
async def test_simulation_execution_fast_path_and_zero_arg_a2ui_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify 'Run the simulation...' curates simulation tools, fast-paths the execution chain, and self-heals zero-arg A2UI calls."""
    from types import SimpleNamespace
    import agents.leadership_planner_with_eval.skills.consultant.tools as consultant_tools

    super_calls: list[LlmRequest] = []

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        super_calls.append(llm_request)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Simulation debrief complete.")],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")

    all_tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="design_leadership_scenario"),
                types.FunctionDeclaration(name="start_simulation"),
                types.FunctionDeclaration(name="submit_plan_to_simulator"),
                types.FunctionDeclaration(name="emit_sim_results_a2ui"),
                types.FunctionDeclaration(name="emit_action_plan_a2ui"),
                types.FunctionDeclaration(name="emit_translation_diagnostic"),
                types.FunctionDeclaration(name="emit_stakeholder_psychology"),
                types.FunctionDeclaration(name="validate_and_emit_a2ui"),
            ]
        )
    ]

    history: list[types.Content] = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text="Run the simulation for the leadership scenario with 1,000 stakeholders"
                )
            ],
        )
    ]
    sim_chain = [
        "start_simulation",
        "submit_plan_to_simulator",
        "emit_sim_results_a2ui",
        "emit_action_plan_a2ui",
        "emit_translation_diagnostic",
        "emit_stakeholder_psychology",
    ]
    for step_name in sim_chain:
        req = LlmRequest(
            model="gemini-3-flash-preview",
            contents=list(history),
            config=types.GenerateContentConfig(tools=all_tools),
        )
        out = [r async for r in llm.generate_content_async(req)]
        fc = out[0].content.parts[0].function_call
        assert fc is not None
        assert fc.name == step_name
        history.append(out[0].content)
        history.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=fc.id or "sim_call",
                            name=step_name,
                            response={"result": "SUCCESS"},
                        )
                    )
                ],
            )
        )

    assert len(super_calls) == 0

    # Closing turn after emit_stakeholder_psychology directly yields simulation debrief with 0 Ollama calls
    req_close = LlmRequest(
        model="gemini-3-flash-preview",
        contents=list(history),
        config=types.GenerateContentConfig(tools=all_tools),
    )
    out_close = [r async for r in llm.generate_content_async(req_close)]
    assert len(super_calls) == 0
    assert out_close[0].content.parts[0].function_call is None
    assert "Executive Simulation Debrief" in (out_close[0].content.parts[0].text or "")

    # Verify zero-arg self-healing on emit_sim_results_a2ui and emit_action_plan_a2ui
    emitted_msgs: list[dict[str, object]] = []

    async def fake_emit_gateway(**kwargs: object) -> None:
        emitted_msgs.append(dict(kwargs))

    monkeypatch.setattr(consultant_tools, "emit_gateway_message", fake_emit_gateway)

    ctx = SimpleNamespace(
        session=SimpleNamespace(id="test-session"),
        state={
            "leadership_scenario": {
                "name": "Kraft Heinz Enterprise AI Transformation",
                "scenario_type": "transformation",
                "stakeholder_count": 1000,
                "complexity": "high",
            },
            "leadership_evaluation": {
                "overall_score": 82,
                "scores": {
                    "stakeholder_alignment": 85,
                    "decision_quality": 78,
                    "resource_stewardship": 84,
                    "crisis_readiness": 76,
                    "strategic_coherence": 86,
                    "ethical_alignment": 83,
                },
                "findings": [
                    {
                        "title": "Regional Split Friction",
                        "severity": "MODERATE",
                        "description": "Shared service dependencies require phased gates.",
                    }
                ],
            },
            "simulation_result": {
                "raw_response": "820 aligned, 180 defected across 4 stages."
            },
        },
    )
    sim_res = await consultant_tools.emit_sim_results_a2ui(tool_context=ctx)
    assert "SUCCESS" in sim_res

    plan_res = await consultant_tools.emit_action_plan_a2ui(tool_context=ctx)
    assert "SUCCESS" in plan_res
    assert len(emitted_msgs) == 4


@pytest.mark.asyncio
async def test_kraft_heinz_step1_fast_path_and_closing_step_anti_json_leak_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Step 1 fast-path extracts Kraft Heinz metadata and closing turn never leaks raw tool-call JSON."""
    captured: list[tuple[LlmRequest, bool]] = []
    leaked_closing_json = (
        '{"name": "validate_and_emit_a2ui", "parameters": {"payload": '
        '"{"surfaceUpdate": {"card": {"title": "Enterprise AI Transformation", '
        '"summary": "Kraft Heinz is undergoing a complex global business split...'
    )

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        captured.append((llm_request, stream))
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=leaked_closing_json)],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")

    planner_tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="design_leadership_scenario"),
                types.FunctionDeclaration(name="plan_consensus_pathway"),
                types.FunctionDeclaration(name="report_leadership_scenario"),
                types.FunctionDeclaration(name="map_stakeholders"),
                types.FunctionDeclaration(name="evaluate_leadership_plan"),
                types.FunctionDeclaration(name="validate_and_emit_a2ui"),
            ]
        )
    ]

    kraft_heinz_prompt = (
        "Use the `design_leadership_scenario` tool to architect a comprehensive "
        "enterprise leadership simulation scenario based on the following Kraft Heinz "
        "Foods Company business split and BlackLine software integration case study:\n\n"
        "### Scenario Metadata & Settings\n"
        "- **Scenario Type**: transformation\n"
        "- **Complexity**: complex (max 45 ticks, advanced difficulty)\n"
        "- **Stakeholder Count**: 1000\n"
        "- **Budget Allocation**: balanced\n"
    )

    # 1. Step 1 fast-path extracts exact metadata from the Kraft Heinz prompt with 0 Ollama calls
    req_step1 = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=kraft_heinz_prompt)],
            )
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out_step1 = [r async for r in llm.generate_content_async(req_step1, stream=True)]
    assert len(captured) == 0
    fc1 = out_step1[0].content.parts[0].function_call
    assert fc1 is not None
    assert fc1.name == "design_leadership_scenario"
    assert fc1.args == {
        "scenario_type": "transformation",
        "budget_allocation": "balanced",
        "complexity": "complex",
        "stakeholder_count": 1000,
    }

    # 2.Closing step after validate_and_emit_a2ui:
    # Even if gemma2:9b outputs raw malformed tool-call JSON on the 7th turn,
    # effective_stream is forced False, system_instruction is prose-only, and
    # the leaked JSON is replaced with a deterministic Markdown executive summary.
    req_closing = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=kraft_heinz_prompt)],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="c1",
                            name="design_leadership_scenario",
                            args=fc1.args,
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="c1",
                            name="design_leadership_scenario",
                            response={
                                "status": "success",
                                "name": "Enterprise AI Transformation",
                                "scenario_type": "transformation",
                            },
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="c5",
                            name="evaluate_leadership_plan",
                            response={
                                "overall_score": 82,
                                "summary": "Strong governance structure with moderate carve-out friction.",
                                "findings": [
                                    {
                                        "title": "Controller Black-Box Resistance",
                                        "severity": "MODERATE",
                                    }
                                ],
                                "improvement_suggestions": [
                                    "Deploy BlackLine AI in assisted pilot mode before auto-approval."
                                ],
                            },
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="c6",
                            name="validate_and_emit_a2ui",
                            response={
                                "status": "success",
                                "a2ui": {"surfaceUpdate": {"surfaceId": "dashboard"}},
                                "message": "A2UI dashboard emitted successfully.",
                            },
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )

    out_closing = [
        r async for r in llm.generate_content_async(req_closing, stream=True)
    ]
    # Closing step directly yields executive summary with 0 Ollama calls
    assert len(captured) == 0

    final_text = out_closing[0].content.parts[0].text or ""
    assert '{"name":' not in final_text
    assert "surfaceUpdate" not in final_text
    assert "Enterprise AI Transformation" in final_text
    assert "**Readiness Evaluation (82/100):**" in final_text
    assert "Controller Black-Box Resistance" in final_text


@pytest.mark.asyncio
async def test_preload_memory_transient_content_and_closing_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify PreloadMemoryTool <PAST_CONVERSATIONS>, 4-step base planner close, recall exclusions, and mixed prose+JSON leaks."""
    super_responses: list[str] = []

    async def fake_super_generate(
        self: LiteLlm,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        text = (
            super_responses.pop(0)
            if super_responses
            else 'Here is the summary:\n{"parameters": {"payload": "dashboard"}, "name": "validate_and_emit_a2ui"}'
        )
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=text)],
            )
        )

    monkeypatch.setattr(LiteLlm, "generate_content_async", fake_super_generate)
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")

    # 1. Recall / replay prompts must NOT falsely trigger Step 1 design_leadership_scenario
    assert (
        llm._extract_scenario_design_args(
            "Recall the best leadership scenario we designed"
        )
        is None
    )
    assert (
        llm._extract_scenario_design_args(
            "Replay the Budget Crisis leadership scenario"
        )
        is None
    )
    assert llm._extract_scenario_design_args(
        "Create a crisis management scenario with 5 executives"
    ) == {
        "scenario_type": "crisis",
        "budget_allocation": "balanced",
        "complexity": "moderate",
        "stakeholder_count": 5,
    }

    # 2. PreloadMemoryTool transient <PAST_CONVERSATIONS> appended after function_response
    # must NOT break Step 1->2 fast-path or _called_in_current_turn
    transient_memory = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=(
                    "The following content is from your previous conversations with the user.\n"
                    "<PAST_CONVERSATIONS>\nuser: Design a scenario\n</PAST_CONVERSATIONS>"
                )
            )
        ],
    )
    planner_tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="design_leadership_scenario"),
                types.FunctionDeclaration(name="plan_consensus_pathway"),
                types.FunctionDeclaration(name="report_leadership_scenario"),
                types.FunctionDeclaration(name="map_stakeholders"),
            ]
        )
    ]
    req_with_transient = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text="Create a crisis management scenario with 5 executives"
                    )
                ],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="c1",
                            name="design_leadership_scenario",
                            args={
                                "scenario_type": "crisis",
                                "budget_allocation": "balanced",
                                "complexity": "moderate",
                                "stakeholder_count": 5,
                            },
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="c1",
                            name="design_leadership_scenario",
                            response={
                                "status": "success",
                                "name": "Organizational Crisis Response",
                                "scenario_type": "crisis",
                            },
                        )
                    )
                ],
            ),
            transient_memory,
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out_step2 = [r async for r in llm.generate_content_async(req_with_transient)]
    fc2 = out_step2[0].content.parts[0].function_call
    assert fc2 is not None
    assert fc2.name == "plan_consensus_pathway"
    assert fc2.args["scenario_type"] == "crisis"
    assert fc2.args["stakeholder_count"] == 5

    # 3. Base leadership_planner (4 tools, no evaluator/A2UI) enters is_closing_step after map_stakeholders
    # even when trailing <PAST_CONVERSATIONS> is present and Gemma outputs prose + reversed-key JSON or plain fence
    req_base_close = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            *req_with_transient.contents[:-1],
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="c4",
                            name="map_stakeholders",
                            response={"status": "success", "stakeholder_count": 5},
                        )
                    )
                ],
            ),
            transient_memory,
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out_base_close = [
        r async for r in llm.generate_content_async(req_base_close, stream=True)
    ]
    base_text = out_base_close[0].content.parts[0].text or ""
    assert "### Executive Summary: Organizational Crisis Response" in base_text
    assert "validate_and_emit_a2ui" not in base_text

    # 4. Multi-turn session: simulation close in Turn 1 followed by new scenario design close in Turn 2
    # must NOT bleed is_sim_close into Turn 2's planning executive summary
    super_responses.append("```\nvalidate_and_emit_a2ui(payload='dashboard')\n```")
    req_multiturn_close = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Run the simulation")],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="sim_end",
                            name="emit_stakeholder_psychology",
                            response={"status": "success"},
                        )
                    )
                ],
            ),
            types.Content(
                role="model",
                parts=[types.Part.from_text(text="Simulation debrief.")],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text="Design a strategic merger scenario with 12 stakeholders"
                    )
                ],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="m1",
                            name="design_leadership_scenario",
                            args={
                                "scenario_type": "merger",
                                "budget_allocation": "balanced",
                                "complexity": "moderate",
                                "stakeholder_count": 12,
                            },
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="m1",
                            name="design_leadership_scenario",
                            response={
                                "status": "success",
                                "name": "Strategic Merger Integration",
                                "scenario_type": "merger",
                            },
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="m4",
                            name="map_stakeholders",
                            response={"status": "success", "stakeholder_count": 12},
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out_mt = [
        r async for r in llm.generate_content_async(req_multiturn_close, stream=True)
    ]
    mt_text = out_mt[0].content.parts[0].text or ""
    assert "### Executive Summary: Strategic Merger Integration" in mt_text
    assert "Executive Simulation Debrief" not in mt_text
    assert "12 stakeholders" in mt_text


@pytest.mark.asyncio
async def test_fast_path_evaluate_to_validate_and_emit_a2ui_not_blocked_by_prior_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify evaluate_leadership_plan -> validate_and_emit_a2ui transitions even if called earlier."""
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")
    tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="evaluate_leadership_plan"),
                types.FunctionDeclaration(name="validate_and_emit_a2ui"),
            ]
        )
    ]
    # Request containing a prior call to validate_and_emit_a2ui earlier in the turn history
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="prior_call",
                            name="validate_and_emit_a2ui",
                            args={"payload": "dashboard"},
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="eval_resp",
                            name="evaluate_leadership_plan",
                            response={"overall_score": 90, "status": "success"},
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(tools=tools),
    )
    responses = [r async for r in llm.generate_content_async(req)]
    assert len(responses) == 1
    fc = responses[0].content.parts[0].function_call
    assert fc is not None
    assert fc.name == "validate_and_emit_a2ui"
    assert fc.args == {"payload": "dashboard"}


def test_repair_malformed_tool_call_validate_and_emit_a2ui_unconditional():
    """Verify _repair_malformed_tool_call unconditionally returns dashboard payload for validate_and_emit_a2ui."""
    # 1. Truncated without parameters
    res1 = LocalGemmaOllama._repair_malformed_tool_call('{"name": "validate_and_emit_a2ui"}')
    assert res1 == ("validate_and_emit_a2ui", {"payload": "dashboard"})

    # 2. Corrupted inner payload with nested unescaped quotes
    res2 = LocalGemmaOllama._repair_malformed_tool_call(
        '{"name": "validate_and_emit_a2ui", "parameters": {"payload": "{"broken": "unescaped "quotes""}}'
    )
    assert res2 == ("validate_and_emit_a2ui", {"payload": "dashboard"})

    # 3. Direct function name match in json-like text
    res3 = LocalGemmaOllama._repair_malformed_tool_call(
        '```json\n{"function": "validate_and_emit_a2ui", "arguments": "corrupted"}\n```'
    )
    assert res3 == ("validate_and_emit_a2ui", {"payload": "dashboard"})


def test_extract_function_calls_string_raw_params_and_turn_complete():
    """Verify _extract_function_calls_from_response handles string raw_params and turn_complete=True."""
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")
    allowed = {"validate_and_emit_a2ui"}

    # 1. String raw_params with plain string
    resp_str = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text='{"name": "validate_and_emit_a2ui", "parameters": "dashboard"}'
                )
            ],
        ),
        turn_complete=True,
    )
    llm._extract_function_calls_from_response(resp_str, allowed_tool_names=allowed)
    fc1 = resp_str.content.parts[0].function_call
    assert fc1 is not None
    assert fc1.name == "validate_and_emit_a2ui"
    assert fc1.args == {"payload": "dashboard"}

    # 2. String raw_params with JSON dict inside
    json_str_payload = json.dumps({"payload": "dashboard", "view": "grid"})
    resp_json = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f'{{"name": "validate_and_emit_a2ui", "parameters": {json.dumps(json_str_payload)}}}'
                )
            ],
        ),
        turn_complete=True,
    )
    llm._extract_function_calls_from_response(resp_json, allowed_tool_names=allowed)
    fc2 = resp_json.content.parts[0].function_call
    assert fc2 is not None
    assert fc2.name == "validate_and_emit_a2ui"
    assert fc2.args.get("payload") == "dashboard"

    # 3. Multi-part response where one part is text and the other is raw JSON tool call
    resp_multipart = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(text="Emitting the executive dashboard."),
                types.Part.from_text(
                    text='{"name": "validate_and_emit_a2ui", "parameters": {"payload": "dashboard"}}'
                ),
            ],
        )
    )
    llm._extract_function_calls_from_response(resp_multipart, allowed_tool_names=allowed)
    fc3 = resp_multipart.content.parts[0].function_call
    assert fc3 is not None
    assert fc3.name == "validate_and_emit_a2ui"
    assert fc3.args == {"payload": "dashboard"}

    # 4. Valid custom payload preserved without being clobbered by default dashboard
    custom_surface = json.dumps({"surfaceUpdate": {"surfaceId": "custom_board"}})
    resp_custom = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f'{{"name": "validate_and_emit_a2ui", "parameters": {{"payload": {json.dumps(custom_surface)}}}}}'
                )
            ],
        ),
        turn_complete=True,
    )
    llm._extract_function_calls_from_response(resp_custom, allowed_tool_names=allowed)
    fc4 = resp_custom.content.parts[0].function_call
    assert fc4 is not None
    assert fc4.name == "validate_and_emit_a2ui"
    assert fc4.args == {"payload": custom_surface}

    # 5. Malformed unescaped quotes in payload repaired to dashboard
    raw_unescaped = '{"name": "validate_and_emit_a2ui", "parameters": {"payload": "{"surfaceUpdate": {"card": 123}}"}}'
    resp_unescaped = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=raw_unescaped)],
        ),
        turn_complete=True,
    )
    llm._extract_function_calls_from_response(resp_unescaped, allowed_tool_names=allowed)
    fc5 = resp_unescaped.content.parts[0].function_call
    assert fc5 is not None
    assert fc5.name == "validate_and_emit_a2ui"
    assert fc5.args == {"payload": "dashboard"}


def test_repair_malformed_tool_call_supports_single_quotes_and_unquoted_keys():
    """Verify _repair_malformed_tool_call handles single quotes and unquoted key styles."""
    # Single quotes
    res1 = LocalGemmaOllama._repair_malformed_tool_call(
        "{'name': 'validate_and_emit_a2ui', 'parameters': 'corrupted'}"
    )
    assert res1 == ("validate_and_emit_a2ui", {"payload": "dashboard"})

    # Unquoted name key
    res2 = LocalGemmaOllama._repair_malformed_tool_call(
        '{name: "validate_and_emit_a2ui", parameters: "corrupted"}'
    )
    assert res2 == ("validate_and_emit_a2ui", {"payload": "dashboard"})


@pytest.mark.asyncio
async def test_fast_path_map_stakeholders_to_validate_and_emit_a2ui_not_blocked():
    """Verify base leadership planner transitions from map_stakeholders to validate_and_emit_a2ui even if called before."""
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")
    planner_tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="design_leadership_scenario"),
                types.FunctionDeclaration(name="plan_consensus_pathway"),
                types.FunctionDeclaration(name="report_leadership_scenario"),
                types.FunctionDeclaration(name="map_stakeholders"),
                types.FunctionDeclaration(name="validate_and_emit_a2ui"),
            ]
        )
    ]
    # History includes an earlier validate_and_emit_a2ui in the turn
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Plan an enterprise restructuring")],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc_prev",
                            name="validate_and_emit_a2ui",
                            args={"payload": "dashboard"},
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fr_map",
                            name="map_stakeholders",
                            response={"status": "success", "stakeholder_count": 50},
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(tools=planner_tools),
    )
    out = [r async for r in llm.generate_content_async(req)]
    assert out[0].content.parts[0].function_call is not None
    assert out[0].content.parts[0].function_call.name == "validate_and_emit_a2ui"
    assert out[0].content.parts[0].function_call.args == {"payload": "dashboard"}


@pytest.mark.asyncio
async def test_fast_path_simulation_iteration_not_blocked_by_called_in_current_turn():
    """Verify simulation execution steps transition cleanly in multi-iteration turns."""
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")
    sim_tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="start_simulation"),
                types.FunctionDeclaration(name="submit_plan_to_simulator"),
                types.FunctionDeclaration(name="emit_sim_results_a2ui"),
            ]
        )
    ]
    # Request where submit_plan_to_simulator was already called earlier in this turn
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Run round 2 of the simulation")],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc_prev_sub",
                            name="submit_plan_to_simulator",
                            args={},
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fr_start",
                            name="start_simulation",
                            response={"status": "success"},
                        )
                    )
                ],
            ),
        ],
        config=types.GenerateContentConfig(tools=sim_tools),
    )
    out = [r async for r in llm.generate_content_async(req)]
    assert out[0].content.parts[0].function_call is not None
    assert out[0].content.parts[0].function_call.name == "submit_plan_to_simulator"


def test_closing_step_anti_hallucination_and_sanitizer_guards() -> None:
    """Verify _is_leaked_json_or_empty_closing, _sanitize_closing_response, and function call history rewriting on closing steps."""
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")

    # 1. _is_leaked_json_or_empty_closing rejects hallucinated step echoes and short text
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("Completed workflow step: emit_scenario_narrative.") is True
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("   Completed workflow step: `emit_scenario_narrative`.  ") is True
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("Completed workflow step: emit_stakeholder_psychology.") is True
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("Short debrief.") is True
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("") is True
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("   ") is True

    # Proper debrief over 80 characters without tool JSON is accepted
    valid_debrief = (
        "### Executive Simulation Debrief\n\n"
        "The multi-agent leadership simulation has completed execution with all telemetry and "
        "stakeholder psychology artifacts rendered to the interactive dashboard. "
        "Review the action plan and coalition metrics."
    )
    assert len(valid_debrief) >= 80
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing(valid_debrief) is False

    # 2. _sanitize_closing_response replaces leaked/hallucinated one-liner with fallback
    fallback_summary = "### Executive Debrief Fallback\n\nAll tools completed."
    leaked_resp = LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Completed workflow step: emit_scenario_narrative.")],
        )
    )
    LocalGemmaOllama._sanitize_closing_response(leaked_resp, fallback_summary)
    assert leaked_resp.content.parts[0].text == fallback_summary

    # 3. _move_function_calls_into_system_instruction does NOT inject 'Completed workflow step:' on closing turns
    req_closing = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Run simulation")],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc_sim",
                            name="emit_stakeholder_psychology",
                            args={},
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fc_sim",
                            name="emit_stakeholder_psychology",
                            response={"status": "success"},
                        )
                    )
                ],
            ),
        ],
    )
    llm._move_function_calls_into_system_instruction(req_closing, is_closing_step=True)
    all_text = " ".join(
        (p.text or "")
        for c in (req_closing.contents or [])
        for p in (c.parts or [])
    )
    assert "Completed workflow step:" not in all_text
    assert "emit_scenario_narrative" not in all_text


@pytest.mark.asyncio
async def test_closing_step_robustness_with_multipart_and_string_json() -> None:
    """Verify closing turn recognizes multi-part content, string JSON responses, and catches hallucinated phrases."""
    # 1. Leak detection on new edge case phrases
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("Next step: emit_scenario_narrative.") is True
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("Executing workflow step: profile stakeholder psychology.") is True
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("Step completed: emit_action_plan_a2ui.") is True
    assert LocalGemmaOllama._is_leaked_json_or_empty_closing("Tool call: profile stakeholder psychology") is True

    # 2. Multi-part last content and string-encoded JSON response
    llm = LocalGemmaOllama(model="ollama_chat/gemma2:9b")
    req = LlmRequest(
        model="gemini-3-flash-preview",
        config=types.GenerateContentConfig(
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name="emit_translation_diagnostic"),
                types.FunctionDeclaration(name="emit_stakeholder_psychology"),
            ])]
        ),
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Run simulation for Digital Health Transformation")],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text="Observed result:"),
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fc_sp",
                            name="emit_stakeholder_psychology",
                            response={
                                "status": "success",
                                "stakeholder_psychology": {
                                    "summary": "High alignment across clinical champions. Average WTT score: 82/100.",
                                    "profiles": [{"stakeholder_id": "clinical_lead", "wtt_score": 82}],
                                },
                            },
                        )
                    ),
                ],
            ),
        ],
    )

    responses = [r async for r in llm.generate_content_async(req)]
    assert len(responses) == 1
    text = responses[0].content.parts[0].text or ""
    assert "### Executive Simulation Debrief" in text
    assert "High alignment across clinical champions" in text
    assert "82/100" in text


def test_generate_local_json_async_timeout_default():
    """Verify generate_local_json_async has configurable timeout defaulting to 20.0s."""
    import inspect

    from agents.utils.local_ai import generate_local_json_async

    sig = inspect.signature(generate_local_json_async)
    assert "timeout" in sig.parameters
    assert sig.parameters["timeout"].default == 20.0


@pytest.mark.asyncio
async def test_generate_local_json_async_timeout_env_override(monkeypatch):
    """Verify generate_local_json_async respects LOCAL_AI_TIMEOUT env var when timeout <= 0."""
    import httpx
    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setenv("LOCAL_AI_TIMEOUT", "12.5")
    captured_timeout = []

    class DummyClient:
        def __init__(self, timeout=None, **kwargs):
            captured_timeout.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            class DummyResp:
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"message": {"content": "{\"status\": \"ok\"}"}}
            return DummyResp()

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)

    res = await local_ai_mod.generate_local_json_async("test prompt", timeout=0.0)
    assert res == '{"status": "ok"}'
    assert captured_timeout == [12.5]


def test_start_simulation_fast_path_extracts_stakeholder_count() -> None:
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text="Run the simulation for the leadership scenario with 10,000 stakeholders"
                    )
                ],
            )
        ],
    )
    resp = LocalGemmaOllama._try_fast_path_response(
        req,
        {"start_simulation", "submit_plan_to_simulator"},
    )
    assert resp is not None
    fc = resp.content.parts[0].function_call
    assert fc.name == "start_simulation"
    assert fc.args["action"] == "execute"
    assert "10000" in fc.args["message"]
    assert fc.args["simulation_config"]["stakeholder_count"] == 10000
    assert fc.args["simulation_config"]["runner_count"] == 10000


def test_submit_plan_to_simulator_transition_args() -> None:
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Run simulation")],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc1",
                            name="start_simulation",
                            args={"action": "execute"},
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fc1",
                            name="start_simulation",
                            response={"status": "ready", "simulation_id": "sim-123"},
                        )
                    )
                ],
            ),
        ],
    )
    resp = LocalGemmaOllama._try_fast_path_response(
        req,
        {"start_simulation", "submit_plan_to_simulator"},
    )
    assert resp is not None
    fc = resp.content.parts[0].function_call
    assert fc.name == "submit_plan_to_simulator"
    assert fc.args == {
        "action": "execute",
        "message": "Submit leadership scenario plan to simulator",
    }


def test_start_simulation_error_blocks_transition() -> None:
    # Error response from start_simulation must NOT cascade to submit_plan_to_simulator
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Run simulation")],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc1",
                            name="start_simulation",
                            args={},
                        )
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fc1",
                            name="start_simulation",
                            response={"error": "Missing required argument: action"},
                        )
                    )
                ],
            ),
        ],
    )
    resp = LocalGemmaOllama._try_fast_path_response(
        req,
        {"start_simulation", "submit_plan_to_simulator"},
    )
    # Must NOT transition to submit_plan_to_simulator on error
    assert resp is None


def test_repair_malformed_tool_call_for_simulation_tools() -> None:
    res_start = LocalGemmaOllama._repair_malformed_tool_call(
        '{"name": "start_simulation", "parameters": {}}',
        {"start_simulation"},
    )
    assert res_start is not None
    assert res_start[0] == "start_simulation"
    assert res_start[1] == {"action": "execute", "message": "Execute simulation"}

    res_submit = LocalGemmaOllama._repair_malformed_tool_call(
        '{"name": "submit_plan_to_simulator", "parameters": {}}',
        {"submit_plan_to_simulator"},
    )
    assert res_submit is not None
    assert res_submit[0] == "submit_plan_to_simulator"
    assert res_submit[1] == {"action": "execute", "message": "Execute simulation"}


def test_simulator_action_execute_routes_to_simulation_pipeline() -> None:
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text='{"action": "execute", "narrative": "Run simulation", "simulation_id": "sim-99"}'
                    )
                ],
            )
        ],
    )
    resp = LocalGemmaOllama._try_fast_path_response(
        req,
        {"verify_plan", "simulation_pipeline"},
    )
    assert resp is not None
    fc = resp.content.parts[0].function_call
    assert fc.name == "simulation_pipeline"


def test_is_simulation_execution_request_matches_execute_simulation_and_json() -> None:
    from agents.utils.local_ai import _is_simulation_execution_request

    for user_msg in [
        "execute simulation",
        "execute the simulation",
        "start simulation",
        "simulate scenario",
        '{"action": "execute"}',
        '{"a2ui_action": "run_simulation"}',
    ]:
        req = LlmRequest(
            model="gemini-3-flash-preview",
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_msg)])],
        )
        assert _is_simulation_execution_request(req) is True, f"Failed for {user_msg}"


def test_start_simulation_extracts_stakeholder_count_from_history_and_ignores_small_archetypes() -> None:
    # Simulates: Turn 1: user designs 10,000 stakeholders; Step 4 map_stakeholders returns 4 archetypes;
    # Turn 2: user clicks "Run simulation" button (no count in active turn).
    req = LlmRequest(
        model="gemini-3-flash-preview",
        contents=[
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="Design AI transformation scenario for 10,000 stakeholders")],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part.from_function_call(
                        name="design_leadership_scenario",
                        args={"stakeholder_count": 10000},
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part.from_function_response(
                        name="map_stakeholders",
                        response={"status": "success", "stakeholder_count": 4, "archetype_count": 4},
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[types.Part.from_text(text='{"a2ui_action": "run_simulation"}')],
            ),
        ],
    )
    resp = LocalGemmaOllama._try_fast_path_response(
        req,
        {"start_simulation"},
    )
    assert resp is not None
    fc = resp.content.parts[0].function_call
    assert fc.name == "start_simulation"
    assert fc.args["simulation_config"]["stakeholder_count"] == 10000
    assert fc.args["simulation_config"]["runner_count"] == 10000
    assert "10000" in fc.args["message"]








