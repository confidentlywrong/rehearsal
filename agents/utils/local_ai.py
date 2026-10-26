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

"""Local Google AI Engine adapter (Gemma text models + EmbeddingGemma vectors).

Provides runtime hot-swapping between Cloud Google Gemini (Vertex AI) and
local Google models served via Ollama (`http://localhost:11434`):
- Text / Agent reasoning: `gemma2:2b`, `gemma2:9b`, `gemma2:27b` (or `gemma3`)
  via `LocalGemmaOllama(GemmaFunctionCallingMixin, LiteLlm)`.
- Vector embeddings: `embeddinggemma:latest` (768 native dimensions, L2-normalized
  and zero-padded to 3072 dimensions for pgvector `VECTOR(3072)` compatibility).
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any, AsyncGenerator
from urllib.parse import urlparse
import uuid

from google.adk.models.gemma_llm import (
    GemmaFunctionCallingMixin,
    _get_last_valid_json_substring,
)
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
import httpx

logger = logging.getLogger(__name__)

PROVIDER_CLOUD_GEMINI = "cloud_gemini"
PROVIDER_LOCAL_GEMMA = "local_gemma"

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_TEXT_MODEL = "gemma2:9b"
DEFAULT_EMBED_MODEL = "embeddinggemma:latest"
TARGET_EMBED_DIMENSION = 3072


@dataclass(frozen=True)
class AiSettings:
    """Resolved AI Engine configuration shared between Go and Python."""

    provider: str = PROVIDER_CLOUD_GEMINI
    endpoint: str = DEFAULT_ENDPOINT
    text_model: str = DEFAULT_TEXT_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL


_cached_mtime: float | None = None
_cached_path: str | None = None
_cached_settings: AiSettings | None = None
_cached_litellm_key: tuple[str, str] | None = None
_cached_litellm_model: LocalGemmaOllama | None = None


_SAFE_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


def _strip_ollama_prefix(model: str) -> str:
    """Strip optional LiteLLM `ollama_chat/` or `ollama/` prefix from a model tag."""
    trimmed = (model or "").strip()
    lower = trimmed.lower()
    if lower.startswith("ollama_chat/"):
        return trimmed[len("ollama_chat/") :].strip()
    if lower.startswith("ollama/"):
        return trimmed[len("ollama/") :].strip()
    return trimmed


def _validate_text_model(raw: str) -> str:
    """Validate a configured text model tag, falling back to DEFAULT_TEXT_MODEL."""
    cleaned = _strip_ollama_prefix(raw)
    if not cleaned or len(cleaned) > 128 or ".." in cleaned:
        return DEFAULT_TEXT_MODEL
    if not _SAFE_MODEL_NAME_RE.match(cleaned):
        return DEFAULT_TEXT_MODEL
    lower = cleaned.lower()
    if lower == "bundle" or lower.startswith("bundle:") or "embed" in lower:
        return DEFAULT_TEXT_MODEL
    return cleaned


def _validate_embed_model(raw: str) -> str:
    """Validate a configured embedding model tag, falling back to DEFAULT_EMBED_MODEL."""
    cleaned = _strip_ollama_prefix(raw)
    if not cleaned or len(cleaned) > 128 or ".." in cleaned:
        return DEFAULT_EMBED_MODEL
    if not _SAFE_MODEL_NAME_RE.match(cleaned):
        return DEFAULT_EMBED_MODEL
    lower = cleaned.lower()
    if lower == "bundle" or lower.startswith("bundle:"):
        return DEFAULT_EMBED_MODEL
    return cleaned


def _validate_local_endpoint(raw: str) -> str:
    """Validate that `raw` is an http/https loopback URL, falling back to DEFAULT_ENDPOINT."""
    trimmed = (raw or "").strip().rstrip("/")
    if not trimmed:
        return DEFAULT_ENDPOINT
    try:
        parsed = urlparse(trimmed)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("unsupported scheme")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.params:
            raise ValueError("credentials, query, or fragment not allowed")
        port = parsed.port
        if port is not None and not (1 <= port <= 65535):
            raise ValueError("invalid port")
        clean_path = (parsed.path or "").rstrip("/")
        if clean_path.lower() in ("/v1", "/api"):
            clean_path = ""
        if clean_path != "":
            raise ValueError("unexpected URL path")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError("missing host")
        is_allowed_host = host in (
            "localhost",
            "127.0.0.1",
            "::1",
            "host.docker.internal",
        )
        if not is_allowed_host:
            ip = ipaddress.ip_address(host)
            is_allowed_host = ip.is_loopback
        if is_allowed_host:
            fmt_host = f"[{host}]" if ":" in host else host
            netloc = f"{fmt_host}:{port}" if port is not None else fmt_host
            return f"{parsed.scheme}://{netloc}"
    except ValueError:
        pass
    logger.warning(
        "LOCAL_AI: Rejecting non-loopback endpoint %r; falling back to %s",
        raw,
        DEFAULT_ENDPOINT,
    )
    return DEFAULT_ENDPOINT


def _resolve_settings_path() -> Path:
    env_path = (os.environ.get("AI_SETTINGS_PATH") or "").strip()
    if env_path:
        return Path(env_path)
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data" / "ai_settings.json"


def _default_settings_from_env() -> AiSettings:
    provider = (
        (os.environ.get("AI_PROVIDER") or PROVIDER_CLOUD_GEMINI).strip().lower()
    )
    if provider not in (PROVIDER_CLOUD_GEMINI, PROVIDER_LOCAL_GEMMA):
        provider = PROVIDER_CLOUD_GEMINI
    raw_endpoint = (
        os.environ.get("OLLAMA_API_BASE")
        or os.environ.get("OLLAMA_ENDPOINT")
        or DEFAULT_ENDPOINT
    )
    endpoint = _validate_local_endpoint(raw_endpoint)
    text_model = _validate_text_model(os.environ.get("GEMMA_TEXT_MODEL") or "")
    embed_model = _validate_embed_model(
        os.environ.get("GEMMA_EMBED_MODEL") or ""
    )
    return AiSettings(
        provider=provider,
        endpoint=endpoint,
        text_model=text_model,
        embed_model=embed_model,
    )


def get_ai_settings() -> AiSettings:
    """Return the active AI settings from `data/ai_settings.json` (mtime-cached) or env."""
    global _cached_mtime, _cached_path, _cached_settings

    path = _resolve_settings_path()
    path_str = str(path)
    try:
        st = path.stat()
        mtime = st.st_mtime
        if (
            _cached_settings is not None
            and _cached_path == path_str
            and _cached_mtime == mtime
        ):
            return _cached_settings

        raw = json.loads(path.read_text(encoding="utf-8"))
        provider = (
            str(raw.get("provider") or PROVIDER_CLOUD_GEMINI).strip().lower()
        )
        if provider not in (PROVIDER_CLOUD_GEMINI, PROVIDER_LOCAL_GEMMA):
            provider = PROVIDER_CLOUD_GEMINI
        endpoint = _validate_local_endpoint(
            str(
                raw.get("endpoint")
                or raw.get("ollama_endpoint")
                or DEFAULT_ENDPOINT
            )
        )
        text_model = _validate_text_model(str(raw.get("text_model") or ""))
        embed_model = _validate_embed_model(str(raw.get("embed_model") or ""))

        settings = AiSettings(
            provider=provider,
            endpoint=endpoint,
            text_model=text_model,
            embed_model=embed_model,
        )
        _cached_path = path_str
        _cached_mtime = mtime
        _cached_settings = settings
        return settings
    except FileNotFoundError:
        _cached_path = path_str
        _cached_mtime = None
        _cached_settings = None
        return _default_settings_from_env()
    except Exception as exc:  # noqa: BLE001
        logger.debug("LOCAL_AI: Failed reading %s (%s); using env defaults", path, exc)
        return _default_settings_from_env()


def is_local_gemma_active() -> bool:
    """Return True when the user has selected Local Google Gemma (`local_gemma`)."""
    return get_ai_settings().provider == PROVIDER_LOCAL_GEMMA


def _ipv4_endpoint(endpoint: str) -> str:
    """Rewrite `localhost` to `127.0.0.1` to avoid macOS IPv6 `::1` delays."""
    return _validate_local_endpoint(endpoint).replace(
        "://localhost", "://127.0.0.1", 1
    )


def _is_transient_memory_content(content: types.Content | None) -> bool:
    """Return True if `content` is a transient `<PAST_CONVERSATIONS>` block injected by `PreloadMemoryTool`."""
    if not content or content.role != "user" or not content.parts:
        return False
    if any(
        p.function_call is not None or p.function_response is not None
        for p in content.parts
    ):
        return False
    texts = [p.text for p in content.parts if p.text]
    if not texts:
        return False
    return "<PAST_CONVERSATIONS>" in " ".join(texts)


def _last_non_transient_content(
    llm_request: LlmRequest,
) -> types.Content | None:
    """Return the last content item in `llm_request.contents` that is not a transient memory block."""
    for content in reversed(llm_request.contents or []):
        if not content or not content.parts:
            continue
        if _is_transient_memory_content(content):
            continue
        return content
    return None


def _is_simulation_execution_request(llm_request: LlmRequest) -> bool:
    """Return True if the active user turn is a simulation execution workflow."""
    sim_tool_names = {
        "start_simulation",
        "submit_plan_to_simulator",
        "record_scenario_outcome",
        "emit_sim_results_a2ui",
        "emit_action_plan_a2ui",
        "emit_translation_diagnostic",
        "emit_stakeholder_psychology",
    }
    if not llm_request.contents:
        return False

    # Inspect the latest user text prompt and any tool calls/responses in the active turn
    for content in reversed(llm_request.contents):
        if not content or not content.parts:
            continue
        if _is_transient_memory_content(content):
            continue
        has_user_text = False
        for part in content.parts:
            if part.function_call and part.function_call.name in sim_tool_names:
                return True
            if (
                part.function_response
                and part.function_response.name in sim_tool_names
            ):
                return True
            if content.role == "user" and part.text:
                has_user_text = True
                lower_t = part.text.strip().lower()
                try:
                    parsed_t = json.loads(part.text)
                    if isinstance(parsed_t, dict):
                        act = str(parsed_t.get("action") or parsed_t.get("a2ui_action") or "").lower()
                        if act in ("execute", "run_simulation", "run_route", "start_simulation"):
                            return True
                except Exception:
                    pass
                if (
                    "run the simulation" in lower_t
                    or "run simulation" in lower_t
                    or "run_simulation" in lower_t
                    or "run_route" in lower_t
                    or "start the simulation" in lower_t
                    or "start simulation" in lower_t
                    or "execute the simulation" in lower_t
                    or "execute simulation" in lower_t
                    or "simulate the leadership scenario" in lower_t
                    or "simulate leadership scenario" in lower_t
                    or "simulate scenario" in lower_t
                    or "simulate the scenario" in lower_t
                    or "run the scenario" in lower_t
                    or "run scenario" in lower_t
                    or bool(re.search(r'["\'](?:a2ui_)?action["\']\s*:\s*["\'](?:execute|run_simulation|run_route)["\']', lower_t))
                ):
                    return True
        if has_user_text:
            break
    return False


def sanitize_request_for_local_gemma(
    llm_request: LlmRequest,
    target_model: str,
) -> LlmRequest:
    """Clone `llm_request`, overwrite `req.model`, and strip Gemini-only config fields.

    Why `req.model` MUST be overwritten:
    `LiteLlm.generate_content_async` computes `effective_model = llm_request.model or self.model`.
    Because ADK pre-populates `llm_request.model = "gemini-3-flash-preview"` from
    `GlobalGemini.model`, leaving `req.model` untouched causes LiteLLM to call
    Vertex AI Gemini instead of Ollama.
    """
    req = llm_request.model_copy(deep=True)
    req.model = target_model
    if hasattr(req, "cache_config"):
        req.cache_config = None
    if req.config is None:
        req.config = types.GenerateContentConfig()
    if hasattr(req.config, "thinking_config"):
        req.config.thinking_config = None
    if hasattr(req.config, "cached_content"):
        req.config.cached_content = None

    # Ensure Ollama 8K context allocation
    extra_options = {"options": {"num_ctx": 8192}, "num_ctx": 8192}
    try:
        req.config.extra_body = extra_options
    except (AttributeError, ValueError):
        object.__setattr__(req.config, "extra_body", extra_options)

    is_sim_exec = _is_simulation_execution_request(req)

    # Condense system instruction if long leadership prompt is detected
    if req.config.system_instruction:
        if isinstance(req.config.system_instruction, types.Content):
            raw_sys = "\n".join(p.text for p in req.config.system_instruction.parts if p.text)
        elif isinstance(req.config.system_instruction, str):
            raw_sys = req.config.system_instruction
        else:
            raw_sys = str(req.config.system_instruction)

        from agents.leadership_planner_with_memory.prompts import LOCAL_GEMMA_INSTRUCTION

        if raw_sys != LOCAL_GEMMA_INSTRUCTION and len(raw_sys) > 3000:
            lower_sys = raw_sys.lower()
            if "leadership" in lower_sys or "scenario" in lower_sys or "planner" in lower_sys:
                req.config.system_instruction = LOCAL_GEMMA_INSTRUCTION

    if req.config.tools:
        # If this is the leadership planner agent, curate tools for local Gemma
        has_leadership_tools = any(
            isinstance(t, types.Tool)
            and t.function_declarations
            and any(fd.name == "design_leadership_scenario" for fd in t.function_declarations if fd)
            for t in req.config.tools
        )
        if has_leadership_tools:
            if is_sim_exec:
                core_names: set[str] | None = {
                    "start_simulation",
                    "submit_plan_to_simulator",
                    "record_scenario_outcome",
                    "emit_sim_results_a2ui",
                    "emit_action_plan_a2ui",
                    "emit_translation_diagnostic",
                    "emit_stakeholder_psychology",
                    "validate_and_emit_a2ui",
                }
            else:
                core_names = {
                    "design_leadership_scenario",
                    "plan_consensus_pathway",
                    "report_leadership_scenario",
                    "map_stakeholders",
                    "evaluate_leadership_plan",
                    "validate_and_emit_a2ui",
                    "store_scenario",
                    "emit_scenario_list_a2ui",
                    "emit_scenario_detail_a2ui",
                }
        else:
            core_names = None

        cleaned_tools = []
        for t in req.config.tools:
            if isinstance(t, types.Tool):
                if t.function_declarations:
                    decls = t.function_declarations
                    if core_names is not None:
                        decls = [fd for fd in decls if fd and fd.name in core_names]
                    if decls:
                        cleaned_tools.append(
                            types.Tool(function_declarations=decls)
                        )
            else:
                cleaned_tools.append(t)
        req.config.tools = cleaned_tools
    return req


_JSON_STRING_PARAM_KEYS: tuple[str, ...] = (
    "payload",
    "evaluation_request",
    "graph_json",
    "request",
    "plan_json",
    "decision_tags",
    "resource_state",
    "contradiction_alert",
    "influence_deltas",
)


class LocalGemmaOllama(GemmaFunctionCallingMixin, LiteLlm):
    """LiteLLM Ollama model with ADK's Gemma prompt-based function-calling mixin.

    Standard `gemma2:2b`, `gemma2:9b`, `gemma2:27b`, and `gemma3` Ollama templates
    do not declare a `{{ .Tools }}` block, causing Ollama to return HTTP 400
    (`does not support tools`) if native OpenAI tool definitions are sent.
    `GemmaFunctionCallingMixin` converts ADK `FunctionDeclaration` schemas into
    system-prompt instructions, converts prior `function_call`/`function_response`
    history parts into text, clears `req.config.tools`, and extracts structured
    `types.Part(function_call=...)` objects from the model's JSON response.
    """

    follow_settings: bool = False

    def __init__(self, model: str, **kwargs: Any) -> None:
        follow_settings = kwargs.pop("follow_settings", False)
        # Ensure LiteLLM / Ollama uses 8K context (num_ctx=8192)
        options = dict(kwargs.pop("options", {}) or {})
        options.setdefault("num_ctx", 8192)
        kwargs["options"] = options
        kwargs.setdefault("num_ctx", 8192)
        super().__init__(model=model, **kwargs)
        self.follow_settings = follow_settings
        self._additional_args.pop("follow_settings", None)
        self._additional_args["options"] = options
        self._additional_args["num_ctx"] = 8192

    @staticmethod
    def _normalize_tool_args(
        fn_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize dict/list tool arguments for parameters typed as JSON strings."""
        normalized = dict(args or {})
        for str_key in _JSON_STRING_PARAM_KEYS:
            if str_key in normalized and isinstance(
                normalized[str_key], (dict, list)
            ):
                normalized[str_key] = json.dumps(normalized[str_key], default=str)
        if fn_name == "validate_and_emit_a2ui" and not normalized.get("payload"):
            normalized["payload"] = "dashboard"
        return normalized

    @classmethod
    def _repair_malformed_tool_call(
        cls,
        response_text: str,
        allowed_tool_names: set[str] | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """Recover tool calls when local Gemma emits unescaped nested quotes or truncated JSON.

        For example, when `gemma2:9b` emits:
        `{"name": "validate_and_emit_a2ui", "parameters": {"payload": "{"surfaceUpdate": {"card": ...`
        standard `json.loads` fails due to unescaped inner double quotes, which would
        otherwise cause ADK to leak the raw JSON tool-call text to the chat UI and stall.
        """
        matches = list(
            re.finditer(
                r'\{\s*["\']?(?:name|function)["\']?\s*:\s*["\']?([A-Za-z0-9_]+)["\']?',
                response_text,
            )
        )
        for match in reversed(matches):
            fn_name = match.group(1).strip()
            tail = response_text[match.start() :].strip()
            if allowed_tool_names and fn_name not in allowed_tool_names:
                continue

            # Unconditionally return valid dashboard payload for validate_and_emit_a2ui
            if fn_name == "validate_and_emit_a2ui":
                return ("validate_and_emit_a2ui", {"payload": "dashboard"})

            if not allowed_tool_names and not re.search(
                r'["\']?(?:parameters|args|arguments)["\']?\s*:', tail
            ):
                continue

            # 1. Try repairing unescaped JSON-in-string for known JSON string parameters
            for str_key in _JSON_STRING_PARAM_KEYS:
                inner_match = re.search(
                    rf'"{str_key}"\s*:\s*"(\{{[\s\S]*)"\s*\}}\s*\}}',
                    tail,
                )
                if inner_match:
                    raw_inner = inner_match.group(1).replace('\\"', '"')
                    try:
                        parsed_inner = json.loads(raw_inner)
                        return (
                            fn_name,
                            {str_key: json.dumps(parsed_inner, default=str)},
                        )
                    except Exception:  # noqa: BLE001
                        pass

            # 2. Tool-specific recovery when nested JSON is truncated or invalid
            if fn_name == "evaluate_leadership_plan":
                return (
                    "evaluate_leadership_plan",
                    {"evaluation_request": "Enterprise transformation plan"},
                )
            if fn_name in ("start_simulation", "submit_plan_to_simulator"):
                res_args: dict[str, Any] = {
                    "action": "execute",
                    "message": "Execute simulation",
                }
                sc_m = re.search(r'"(?:stakeholder|runner)_count"\s*:\s*(\d+)', tail)
                if sc_m:
                    try:
                        cnt = int(sc_m.group(1))
                        res_args["simulation_config"] = {"stakeholder_count": cnt, "runner_count": cnt}
                    except ValueError:
                        pass
                return (fn_name, res_args)
            if fn_name in (
                "report_leadership_scenario",
                "map_stakeholders",
                "store_scenario",
                "get_best_scenario",
                "store_stakeholder_artifacts",
                "record_scenario_outcome",
                "emit_sim_results_a2ui",
                "emit_action_plan_a2ui",
                "emit_translation_diagnostic",
                "emit_stakeholder_psychology",
                "compile_results",
                "stop_race_collector",
            ):
                return (fn_name, {})
            if fn_name in ("design_leadership_scenario", "plan_consensus_pathway"):
                recovered: dict[str, Any] = {}
                for str_field in (
                    "scenario_type",
                    "budget_allocation",
                    "complexity",
                ):
                    m = re.search(rf'"{str_field}"\s*:\s*"([^"]+)"', tail)
                    if m:
                        recovered[str_field] = m.group(1).strip()
                sc_m = re.search(r'"stakeholder_count"\s*:\s*(\d+)', tail)
                if sc_m:
                    recovered["stakeholder_count"] = int(sc_m.group(1))
                return (fn_name, recovered)
            if fn_name == "emit_scenario_list_a2ui":
                lim_m = re.search(r'"limit"\s*:\s*(\d+)', tail)
                return (
                    "emit_scenario_list_a2ui",
                    {"limit": int(lim_m.group(1)) if lim_m else 3},
                )
            if fn_name == "emit_scenario_detail_a2ui":
                sid_m = re.search(r'"scenario_id"\s*:\s*"([^"]+)"', tail)
                if sid_m:
                    return (
                        "emit_scenario_detail_a2ui",
                        {"scenario_id": sid_m.group(1).strip()},
                    )
        if not allowed_tool_names or "validate_and_emit_a2ui" in allowed_tool_names:
            if re.search(
                r'["\']?(?:name|function)["\']?\s*:\s*["\']?validate_and_emit_a2ui\b',
                response_text,
            ):
                return ("validate_and_emit_a2ui", {"payload": "dashboard"})
        return None

    @staticmethod
    def _latest_user_prompt(llm_request: LlmRequest) -> str:
        """Return the latest non-tool user prompt text in `llm_request.contents`."""
        for content in reversed(llm_request.contents or []):
            if not content or content.role != "user" or not content.parts:
                continue
            if _is_transient_memory_content(content):
                continue
            if any(p.function_response is not None for p in content.parts):
                continue
            text = " ".join(p.text for p in content.parts if p.text).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _called_in_current_turn(
        llm_request: LlmRequest,
        tool_name: str,
    ) -> bool:
        """Return True if `tool_name` was already called or returned in the current user turn."""
        for content in reversed(llm_request.contents or []):
            if not content or not content.parts:
                continue
            if _is_transient_memory_content(content):
                continue
            has_user_text = False
            for part in content.parts:
                if part.function_call and part.function_call.name == tool_name:
                    return True
                if (
                    part.function_response
                    and part.function_response.name == tool_name
                ):
                    return True
                if content.role == "user" and part.text:
                    has_user_text = True
            if has_user_text:
                break
        return False

    @staticmethod
    def _last_call_args_in_current_turn(
        llm_request: LlmRequest,
        tool_name: str,
    ) -> dict[str, Any]:
        """Return the `args` dict of the most recent `FunctionCall(name=tool_name)` in the turn."""
        for content in reversed(llm_request.contents or []):
            if not content or not content.parts:
                continue
            if _is_transient_memory_content(content):
                continue
            has_user_text = False
            for part in content.parts:
                if (
                    part.function_call
                    and part.function_call.name == tool_name
                    and isinstance(part.function_call.args, dict)
                ):
                    return dict(part.function_call.args)
                if content.role == "user" and part.text:
                    has_user_text = True
            if has_user_text:
                break
        return {}

    @classmethod
    def _try_fast_path_response(
        cls,
        llm_request: LlmRequest,
        allowed_tool_names: set[str],
    ) -> LlmResponse | None:
        """Deterministically handle Organizer UI, 6-step Prozesskette, and Simulation fast-paths."""
        last_content = _last_non_transient_content(llm_request)
        if not last_content or not last_content.parts:
            return None

        def _make_fc_response(
            name: str,
            args: dict[str, Any] | None = None,
        ) -> LlmResponse:
            fc = types.FunctionCall(
                id=f"call_{uuid.uuid4().hex[:8]}",
                name=name,
                args=dict(args or {}),
            )
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=fc)],
                )
            )

        fn_responses = [
            p.function_response
            for p in last_content.parts
            if p.function_response is not None
        ]
        if len(fn_responses) == 1:
            fr = fn_responses[0]
            fr_name = (fr.name or "").strip()
            fr_resp = fr.response if isinstance(fr.response, dict) else {}

            if fr_name == "emit_scenario_list_a2ui":
                if fr_resp.get("status") == "empty":
                    text = "You have no stored scenarios yet — design one first."
                else:
                    count = fr_resp.get("count", 3)
                    text = f"Here are your top {count} scenarios."
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=text)],
                    )
                )

            if fr_name == "emit_scenario_detail_a2ui":
                if fr_resp.get("status") == "error":
                    text = str(
                        fr_resp.get("message")
                        or "Unable to load scenario details."
                    )
                else:
                    text = "Showing scenario details."
                return LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=text)],
                    )
                )

            # Planning Prozesskette deterministic step transitions:
            # Step 1 -> Step 2: design_leadership_scenario -> plan_consensus_pathway
            if (
                fr_name == "design_leadership_scenario"
                and fr_resp.get("status") in ("ready", "success")
                and "error" not in fr_resp
                and "plan_consensus_pathway" in allowed_tool_names
                and not cls._called_in_current_turn(
                    llm_request, "plan_consensus_pathway"
                )
            ):
                design_args = cls._last_call_args_in_current_turn(
                    llm_request, "design_leadership_scenario"
                )
                pathway_args: dict[str, Any] = {
                    "scenario_type": str(
                        design_args.get("scenario_type")
                        or fr_resp.get("scenario_type")
                        or "transformation"
                    ),
                    "budget_allocation": str(
                        design_args.get("budget_allocation") or "balanced"
                    ),
                    "complexity": str(
                        design_args.get("complexity")
                        or fr_resp.get("complexity")
                        or "high"
                    ),
                    "stakeholder_count": int(
                        design_args.get("stakeholder_count")
                        or fr_resp.get("stakeholder_count")
                        or 1000
                    ),
                }
                return _make_fc_response("plan_consensus_pathway", pathway_args)

            # Step 2 -> Step 3: plan_consensus_pathway -> report_leadership_scenario
            if (
                fr_name == "plan_consensus_pathway"
                and fr_resp.get("status") in ("ready", "success")
                and "error" not in fr_resp
                and "report_leadership_scenario" in allowed_tool_names
                and not cls._called_in_current_turn(
                    llm_request, "report_leadership_scenario"
                )
            ):
                return _make_fc_response("report_leadership_scenario", {})

            # Step 3 -> Step 4: report_leadership_scenario -> map_stakeholders
            if (
                fr_name == "report_leadership_scenario"
                and fr_resp.get("status") in ("ready", "success")
                and "error" not in fr_resp
                and "map_stakeholders" in allowed_tool_names
                and not cls._called_in_current_turn(
                    llm_request, "map_stakeholders"
                )
            ):
                return _make_fc_response("map_stakeholders", {})

            # Step 4 -> Step 5 (or direct dashboard if no evaluator):
            # map_stakeholders -> evaluate_leadership_plan (or validate_and_emit_a2ui)
            if (
                fr_name == "map_stakeholders"
                and fr_resp.get("status") in ("ready", "success")
                and "error" not in fr_resp
            ):
                if (
                    "evaluate_leadership_plan" in allowed_tool_names
                    and not cls._called_in_current_turn(
                        llm_request, "evaluate_leadership_plan"
                    )
                ):
                    user_prompt = (
                        cls._latest_user_prompt(llm_request)
                        or "Enterprise transformation plan"
                    )
                    return _make_fc_response(
                        "evaluate_leadership_plan",
                        {"evaluation_request": user_prompt},
                    )
                if "validate_and_emit_a2ui" in allowed_tool_names:
                    return _make_fc_response(
                        "validate_and_emit_a2ui",
                        {"payload": "dashboard"},
                    )

            # Step 5 -> Step 6: evaluate_leadership_plan -> validate_and_emit_a2ui
            if (
                fr_name == "evaluate_leadership_plan"
                and (
                    fr_resp.get("status") in ("ready", "success")
                    or fr_resp.get("passed") is True
                    or fr_resp.get("overall_score", 0) > 0
                )
                and "error" not in fr_resp
                and "validate_and_emit_a2ui" in allowed_tool_names
            ):
                return _make_fc_response(
                    "validate_and_emit_a2ui",
                    {"payload": "dashboard"},
                )

            # Simulation execution chain deterministic transitions:
            # 1. start_simulation -> submit_plan_to_simulator
            if (
                fr_name == "start_simulation"
                and (
                    fr_resp.get("status") in ("ready", "success")
                    or fr_resp.get("result") in ("SUCCESS", "success")
                )
                and "error" not in fr_resp
                and "submit_plan_to_simulator" in allowed_tool_names
            ):
                return _make_fc_response(
                    "submit_plan_to_simulator",
                    {
                        "action": "execute",
                        "message": "Submit leadership scenario plan to simulator",
                    },
                )

            # 2. submit_plan_to_simulator -> emit_sim_results_a2ui
            if (
                fr_name == "submit_plan_to_simulator"
                and (
                    fr_resp.get("status") in ("ready", "success")
                    or fr_resp.get("result") in ("SUCCESS", "success")
                )
                and "error" not in fr_resp
                and "emit_sim_results_a2ui" in allowed_tool_names
            ):
                return _make_fc_response("emit_sim_results_a2ui", {})

            # 3. emit_sim_results_a2ui -> emit_action_plan_a2ui
            if (
                fr_name == "emit_sim_results_a2ui"
                and (
                    fr_resp.get("status") in ("ready", "success")
                    or fr_resp.get("result") in ("SUCCESS", "success")
                    or "SUCCESS" in str(fr.response)
                )
                and "error" not in fr_resp
                and "emit_action_plan_a2ui" in allowed_tool_names
            ):
                return _make_fc_response("emit_action_plan_a2ui", {})

            # 4. emit_action_plan_a2ui -> emit_translation_diagnostic
            if (
                fr_name == "emit_action_plan_a2ui"
                and (
                    fr_resp.get("status") in ("ready", "success")
                    or fr_resp.get("result") in ("SUCCESS", "success")
                    or "SUCCESS" in str(fr.response)
                )
                and "error" not in fr_resp
                and "emit_translation_diagnostic" in allowed_tool_names
            ):
                return _make_fc_response("emit_translation_diagnostic", {})

            # 5. emit_translation_diagnostic -> emit_stakeholder_psychology
            if (
                fr_name == "emit_translation_diagnostic"
                and (
                    fr_resp.get("status") in ("ready", "success")
                    or fr_resp.get("result") in ("SUCCESS", "success")
                    or "SUCCESS" in str(fr.response)
                )
                and "error" not in fr_resp
                and "emit_stakeholder_psychology" in allowed_tool_names
            ):
                return _make_fc_response("emit_stakeholder_psychology", {})

            # Simulator post_race agent transitions:
            # compile_results -> stop_race_collector
            if (
                fr_name == "compile_results"
                and "stop_race_collector" in allowed_tool_names
            ):
                return _make_fc_response("stop_race_collector", {})

            return None

        if last_content.role == "user" and not fn_responses:
            user_text = " ".join(
                p.text for p in last_content.parts if p.text
            ).strip()
            lower_user = user_text.lower()
            if "emit_scenario_list_a2ui" in allowed_tool_names and lower_user in {
                "list the top 3 best scenarios",
                "list scenarios",
                "show my scenarios",
                "feed the organizer",
                "list scenarios for the organizer ui",
                "show scenario __list__",
                "get the route for the seed __list__",
            }:
                return _make_fc_response("emit_scenario_list_a2ui", {"limit": 3})

            if "emit_scenario_detail_a2ui" in allowed_tool_names:
                detail_match = re.match(
                    r"^(?:Show scenario|Get the route for the seed)\s+(\S+)\s*$",
                    user_text,
                    re.IGNORECASE,
                )
                if (
                    detail_match
                    and detail_match.group(1).strip().lower() != "__list__"
                ):
                    return _make_fc_response(
                        "emit_scenario_detail_a2ui",
                        {"scenario_id": detail_match.group(1).strip()},
                    )

            # Simulation execution user trigger ("Run the simulation for the leadership scenario...")
            if (
                "start_simulation" in allowed_tool_names
                and _is_simulation_execution_request(llm_request)
                and not cls._called_in_current_turn(
                    llm_request, "start_simulation"
                )
            ):
                count = 1000
                found_count = False

                # First, check the active user text directly
                sc_m = (
                    re.search(
                        r"(?:stakeholder|runner)[\s_]*count\s*\*?\*?\s*:\s*`?\*?\*?\s*([\d,]+)",
                        lower_user,
                    )
                    or re.search(
                        r"\b([\d,]+)\s+(?:stakeholders?|executives?|personas?|leaders?|participants?|runners?)\b",
                        lower_user,
                    )
                    or re.search(
                        r"\bfor\s+([\d,]+)\b",
                        lower_user,
                    )
                )
                if sc_m:
                    try:
                        parsed_sc = int(sc_m.group(1).replace(",", ""))
                        if 1 <= parsed_sc <= 100000:
                            count = parsed_sc
                            found_count = True
                    except ValueError:
                        pass

                # If not found in active text, check JSON payload of active user turn
                if not found_count:
                    try:
                        parsed_user_json = json.loads(user_text)
                        if isinstance(parsed_user_json, dict):
                            cfg = parsed_user_json.get("simulation_config") or {}
                            c = cfg.get("stakeholder_count") or cfg.get("runner_count") or parsed_user_json.get("stakeholder_count") or parsed_user_json.get("runner_count")
                            if isinstance(c, (int, str)):
                                parsed_c = int(str(c).replace(",", ""))
                                if 1 <= parsed_c <= 100000:
                                    count = parsed_c
                                    found_count = True
                    except Exception:
                        pass

                # If still not found, search conversational history in llm_request.contents
                if not found_count:
                    for content in llm_request.contents or []:
                        for p in content.parts or []:
                            if content.role == "user" and p.text:
                                h_m = re.search(
                                    r"\b([\d,]+)\s+(?:stakeholders?|executives?|personas?|leaders?|participants?|runners?)\b",
                                    p.text.lower(),
                                )
                                if h_m:
                                    try:
                                        parsed_h = int(h_m.group(1).replace(",", ""))
                                        if parsed_h >= 10:
                                            count = parsed_h
                                            found_count = True
                                            break
                                    except ValueError:
                                        pass
                            elif p.function_call and isinstance(p.function_call.args, dict):
                                args = p.function_call.args
                                c = args.get("stakeholder_count") or args.get("runner_count")
                                if c:
                                    try:
                                        parsed_c = int(str(c).replace(",", ""))
                                        if parsed_c >= 10:
                                            count = parsed_c
                                            found_count = True
                                            break
                                    except (ValueError, TypeError):
                                        pass
                            elif p.function_response and isinstance(p.function_response.response, dict):
                                resp = p.function_response.response
                                c = resp.get("stakeholder_count") or resp.get("runner_count")
                                if isinstance(c, int) and c >= 10:
                                    count = c
                                    found_count = True
                        if found_count:
                            break

                return _make_fc_response(
                    "start_simulation",
                    {
                        "action": "execute",
                        "message": f"Run leadership simulation for {count} stakeholders",
                        "simulation_config": {
                            "stakeholder_count": count,
                            "runner_count": count,
                        },
                    },
                )

            # Step 1 (design_leadership_scenario) deterministic user trigger
            if (
                "design_leadership_scenario" in allowed_tool_names
                and not _is_simulation_execution_request(llm_request)
                and not cls._called_in_current_turn(
                    llm_request, "design_leadership_scenario"
                )
            ):
                design_args = cls._extract_scenario_design_args(user_text)
                if design_args is not None:
                    return _make_fc_response(
                        "design_leadership_scenario",
                        design_args,
                    )

            # Simulator root agent fast-path (verify_plan vs simulation_pipeline)
            if "simulation_pipeline" in allowed_tool_names:
                is_execute = False
                try:
                    parsed_user = json.loads(user_text)
                    if isinstance(parsed_user, dict):
                        act = str(parsed_user.get("action") or "").lower()
                        if act == "execute":
                            is_execute = True
                except Exception:
                    pass
                if (
                    is_execute
                    or "start the race" in lower_user
                    or "run simulation" in lower_user
                    or "run_simulation" in lower_user
                    or "run_route" in lower_user
                    or "start the simulation" in lower_user
                    or "start simulation" in lower_user
                    or "execute the simulation" in lower_user
                    or "execute simulation" in lower_user
                    or "execute" in lower_user
                    or bool(
                        re.search(
                            r'["\']action["\']\s*:\s*["\']execute["\']',
                            lower_user,
                        )
                    )
                ):
                    if not cls._called_in_current_turn(
                        llm_request, "simulation_pipeline"
                    ):
                        return _make_fc_response(
                            "simulation_pipeline",
                            {"request": user_text or "Start the simulation"},
                        )
                elif "verify_plan" in allowed_tool_names and not cls._called_in_current_turn(
                    llm_request, "verify_plan"
                ):
                    return _make_fc_response(
                        "verify_plan",
                        {"request": user_text},
                    )

            # Simulator post_race agent initial turn -> compile_results
            if (
                {"compile_results", "stop_race_collector"}.issubset(
                    allowed_tool_names
                )
                and not cls._called_in_current_turn(
                    llm_request, "compile_results"
                )
            ):
                return _make_fc_response("compile_results", {})

        return None

    @staticmethod
    def _extract_scenario_design_args(user_text: str) -> dict[str, Any] | None:
        """Deterministically extract `design_leadership_scenario` args when `user_text` requests scenario design."""
        lower_user = (user_text or "").strip().lower()
        if not lower_user:
            return None

        if "design_leadership_scenario" not in lower_user:
            if any(
                phrase in lower_user
                for phrase in (
                    "list the top",
                    "list scenarios",
                    "show my scenarios",
                    "show me my scenarios",
                    "feed the organizer",
                    "show scenario",
                    "get the route",
                    "recall ",
                    "replay ",
                    "revisit ",
                    "run the best scenario",
                    "run the simulation",
                    "start the simulation",
                    "execute the simulation",
                    "simulate the leadership scenario",
                )
            ):
                return None

        has_explicit_tool = "design_leadership_scenario" in lower_user
        has_metadata = (
            "scenario type" in lower_user
            or "stakeholder count" in lower_user
            or "budget allocation" in lower_user
        )
        has_design_verb = bool(
            re.search(
                r"\b(?:design|architect|create|plan|build|draft|model)\b",
                lower_user,
            )
            and re.search(
                r"\b(?:scenario|simulation|transformation|merger|crisis|rehearsal|leadership)\b",
                lower_user,
            )
        )
        if not (has_explicit_tool or has_metadata or has_design_verb):
            return None

        type_m = re.search(
            r"scenario[\s_]*type\s*\*?\*?\s*:\s*`?\*?\*?\s*(transformation|crisis|merger)",
            lower_user,
        )
        if type_m:
            scenario_type = type_m.group(1)
        elif "crisis" in lower_user and "transformation" not in lower_user:
            scenario_type = "crisis"
        elif (
            re.search(r"\b(?:merger|acquisition|m&a)\b", lower_user)
            and "transformation" not in lower_user
        ):
            scenario_type = "merger"
        else:
            scenario_type = "transformation"

        budget_m = re.search(
            r"budget(?:[\s_]*allocation)?\s*\*?\*?\s*:\s*`?\*?\*?\s*(balanced|aggressive|conservative)",
            lower_user,
        )
        if budget_m:
            budget_allocation = budget_m.group(1)
        elif "aggressive" in lower_user and "balanced" not in lower_user:
            budget_allocation = "aggressive"
        elif "conservative" in lower_user and "balanced" not in lower_user:
            budget_allocation = "conservative"
        else:
            budget_allocation = "balanced"

        comp_m = re.search(
            r"complexity\s*\*?\*?\s*:\s*`?\*?\*?\s*(simple|moderate|complex|high)",
            lower_user,
        )
        if comp_m:
            raw_comp = comp_m.group(1)
            complexity = "complex" if raw_comp == "high" else raw_comp
        elif (
            "complex" in lower_user
            or "advanced" in lower_user
            or "45 ticks" in lower_user
        ):
            complexity = "complex"
        elif "simple" in lower_user or "beginner" in lower_user:
            complexity = "simple"
        else:
            complexity = "moderate"

        stakeholder_count = 1000
        sc_m = re.search(
            r"stakeholder[\s_]*count\s*\*?\*?\s*:\s*`?\*?\*?\s*([\d,]+)",
            lower_user,
        ) or re.search(
            r"\b([\d,]+)\s+(?:stakeholders?|executives?|personas?|leaders?|participants?)\b",
            lower_user,
        )
        if sc_m:
            try:
                parsed_sc = int(sc_m.group(1).replace(",", ""))
                if 1 <= parsed_sc <= 100000:
                    stakeholder_count = parsed_sc
            except ValueError:
                pass

        return {
            "scenario_type": scenario_type,
            "budget_allocation": budget_allocation,
            "complexity": complexity,
            "stakeholder_count": stakeholder_count,
        }

    @classmethod
    def _build_closing_fallback_summary(cls, llm_request: LlmRequest) -> str:
        """Build a deterministic 2-3 paragraph plain-Markdown executive summary from `llm_request.contents`."""
        scenario_name = "Enterprise Leadership Transformation"
        scenario_type = "transformation"
        complexity = "complex"
        stakeholder_count = 1000
        overall_score: int | float | None = None
        eval_summary = ""
        findings: list[dict[str, Any]] = []
        suggestions: list[str] = []
        is_sim_close = False

        for content in llm_request.contents or []:
            if not content or not content.parts:
                continue
            if _is_transient_memory_content(content):
                continue
            if (
                content.role == "user"
                and not any(
                    p.function_response is not None for p in content.parts
                )
                and any(p.text for p in content.parts)
            ):
                # New user turn boundary: reset turn-scoped evaluation and simulation flags
                # while keeping scenario metadata in case the turn runs a simulation on the active scenario.
                overall_score = None
                eval_summary = ""
                findings = []
                suggestions = []
                td_summary = ""
                sp_summary = ""
                is_sim_close = False
            for part in content.parts:
                if part.function_call is not None:
                    fc_name = part.function_call.name or ""
                    fc_args = (
                        part.function_call.args
                        if isinstance(part.function_call.args, dict)
                        else {}
                    )
                    if fc_name == "design_leadership_scenario":
                        is_sim_close = False
                        scenario_type = str(
                            fc_args.get("scenario_type") or scenario_type
                        )
                        complexity = str(
                            fc_args.get("complexity") or complexity
                        )
                        if isinstance(fc_args.get("stakeholder_count"), int):
                            stakeholder_count = int(
                                fc_args["stakeholder_count"]
                            )
                elif part.function_response is not None:
                    fr_name = part.function_response.name or ""
                    fr_raw = part.function_response.response
                    if isinstance(fr_raw, dict):
                        fr_resp = fr_raw
                    elif isinstance(fr_raw, str):
                        try:
                            loaded = json.loads(fr_raw)
                            fr_resp = loaded if isinstance(loaded, dict) else {}
                        except Exception:
                            fr_resp = {}
                    else:
                        fr_resp = {}
                    if fr_name == "design_leadership_scenario":
                        is_sim_close = False
                        if fr_resp.get("name"):
                            scenario_name = str(fr_resp["name"])
                        if fr_resp.get("scenario_type"):
                            scenario_type = str(fr_resp["scenario_type"])
                    elif fr_name == "report_leadership_scenario":
                        s_sum = fr_resp.get("scenario_summary")
                        if isinstance(s_sum, dict) and s_sum.get(
                            "scenario_name"
                        ):
                            scenario_name = str(s_sum["scenario_name"])
                    elif fr_name == "evaluate_leadership_plan":
                        if isinstance(
                            fr_resp.get("overall_score"), (int, float)
                        ):
                            overall_score = fr_resp["overall_score"]
                        if fr_resp.get("summary"):
                            eval_summary = str(fr_resp["summary"]).strip()
                        if isinstance(fr_resp.get("findings"), list):
                            findings = [
                                f
                                for f in fr_resp["findings"]
                                if isinstance(f, dict)
                            ]
                        if isinstance(
                            fr_resp.get("improvement_suggestions"), list
                        ):
                            suggestions = [
                                str(s).strip()
                                for s in fr_resp["improvement_suggestions"]
                                if str(s).strip()
                            ]
                    elif fr_name == "emit_translation_diagnostic":
                        is_sim_close = True
                        td = fr_resp.get("translation_diagnostic")
                        if isinstance(td, dict) and td.get("summary"):
                            td_summary = str(td["summary"]).strip()
                    elif fr_name == "emit_stakeholder_psychology":
                        is_sim_close = True
                        sp = fr_resp.get("stakeholder_psychology")
                        if isinstance(sp, dict) and sp.get("summary"):
                            sp_summary = str(sp["summary"]).strip()
                    elif fr_name in {
                        "start_simulation",
                        "emit_sim_results_a2ui",
                        "emit_action_plan_a2ui",
                        "submit_plan_to_simulator",
                        "stop_race_collector",
                        "simulation_pipeline",
                    }:
                        is_sim_close = True

        if is_sim_close:
            p1 = (
                f"### Executive Simulation Debrief: {scenario_name}\n\n"
                f"The multi-agent leadership simulation across **{stakeholder_count:,} stakeholders** "
                f"({scenario_type} scenario, {complexity} complexity) has completed execution, and all "
                "telemetry, translation diagnostics, and stakeholder psychology artifacts have been "
                "rendered to the dashboard."
            )
            p_insights = []
            if sp_summary:
                p_insights.append(f"**Stakeholder Psychology:** {sp_summary}")
            if td_summary:
                p_insights.append(f"**Translation Diagnostic:** {td_summary}")
            p2 = "\n\n".join(p_insights) if p_insights else ""

            p3 = (
                "Review the interactive **Simulation Results**, **Action Plan**, and **Stakeholder "
                "Psychology** cards in the dashboard to inspect coalition alignment, resistance shifts, "
                "and recommended follow-up interventions."
            )
            return f"{p1}\n\n{p2}\n\n{p3}" if p2 else f"{p1}\n\n{p3}"

        p1 = (
            f"### Executive Summary: {scenario_name}\n\n"
            f"The **{scenario_name}** rehearsal architecture has been configured as a **{complexity}** "
            f"`{scenario_type}` scenario modeling **{stakeholder_count:,} stakeholders**, complete with "
            "a Dynamic Consensus Network (DCNS) pathway and enriched stakeholder persona topology."
        )
        if overall_score is not None:
            p2 = (
                f"**Readiness Evaluation ({overall_score}/100):** "
                f"{eval_summary or 'The leadership plan demonstrates structured governance milestones across key stakeholder tiers.'}"
            )
            if findings:
                finding_items = "; ".join(
                    f"**{f.get('title', 'Risk Finding')}** ({f.get('severity', 'MODERATE')})"
                    for f in findings[:3]
                )
                p2 += f" Key diagnostic areas identified: {finding_items}."
        else:
            p2 = (
                "The consensus pathway and stakeholder influence topology have been mapped across "
                "executive decision-makers, operational champions, and technical gatekeepers to "
                "surface potential friction points before execution."
            )

        if suggestions:
            rec_text = " ".join(suggestions[:2])
            p3 = (
                f"**Strategic Recommendations:** {rec_text} "
                "The interactive A2UI evaluation dashboard is now live in the workspace."
            )
        else:
            p3 = (
                "**Next Steps:** Review the interactive A2UI dashboard for detailed criterion scores "
                "and stakeholder alignment metrics, or launch the simulation to stress-test the plan "
                "against Black Swan disruptions."
            )
        return f"{p1}\n\n{p2}\n\n{p3}"

    @classmethod
    def _is_leaked_json_or_empty_closing(cls, response_text: str) -> bool:
        """Return True if a closing-turn response is empty or contains leaked tool/A2UI JSON, code blocks, or brief step echoes."""
        stripped = (response_text or "").strip()
        if not stripped:
            return True
        if len(stripped) < 80:
            return True
        if stripped.startswith(("{", "[")):
            return True
        if "```" in stripped:
            return True
        if re.search(r"^\s*Completed workflow step:.*$", stripped, flags=re.MULTILINE):
            return True
        if re.search(r"\bCompleted workflow step\b", stripped, re.IGNORECASE):
            return True
        if re.search(
            r"\b(?:Completed|Finished|Executing)\s+(?:workflow\s+)?step\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        if re.search(
            r"\b(?:workflow\s+step|step\s+completed|tool\s+call)\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        if re.search(r"\bemit_scenario_narrative\b", stripped, re.IGNORECASE):
            return True
        if re.search(
            r"\b(?:profile stakeholder psychology|profile_stakeholder_psychology)\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        if re.search(
            r'\{\s*"[A-Za-z0-9_]+"\s*:',
            stripped,
        ):
            return True
        if re.search(
            r"\b(?:design_leadership_scenario|plan_consensus_pathway|report_leadership_scenario|map_stakeholders|evaluate_leadership_plan|validate_and_emit_a2ui|emit_sim_results_a2ui|emit_action_plan_a2ui|emit_translation_diagnostic|emit_stakeholder_psychology|emit_scenario_narrative)\s*\(",
            stripped,
        ):
            return True
        if cls._repair_malformed_tool_call(stripped) is not None:
            return True
        return False

    @classmethod
    def _sanitize_closing_response(
        cls,
        llm_response: LlmResponse,
        fallback_summary: str,
    ) -> None:
        """Replace leaked tool-call JSON or empty text on a closing turn with `fallback_summary`."""
        if not llm_response.content or not llm_response.content.parts:
            llm_response.content = types.Content(
                role="model",
                parts=[types.Part.from_text(text=fallback_summary)],
            )
            return

        thought_parts = [
            p for p in llm_response.content.parts if getattr(p, "thought", False)
        ]
        non_thought_parts = [
            p
            for p in llm_response.content.parts
            if not getattr(p, "thought", False)
        ]
        has_fc = any(p.function_call is not None for p in non_thought_parts)
        combined_text = "\n".join(
            p.text for p in non_thought_parts if p.text
        ).strip()

        if has_fc or cls._is_leaked_json_or_empty_closing(combined_text):
            llm_response.content.parts = [
                *thought_parts,
                types.Part.from_text(text=fallback_summary),
            ]

    def _move_function_calls_into_system_instruction(
        self,
        llm_request: LlmRequest,
        *,
        is_closing_step: bool = False,
    ) -> None:
        # Pre-convert function_call and function_response history parts using
        # json.dumps(..., default=str) so non-JSON-native tool outputs (e.g.
        # datetime, UUID) never raise TypeError inside ADK's mixin, and prior
        # function_call parts match the system prompt's {"name", "parameters"} schema.
        filtered_contents: list[types.Content] = []
        for content in llm_request.contents or []:
            if not content or not content.parts:
                continue
            new_parts: list[types.Part] = []
            has_function_call = False
            has_function_response = False
            for part in content.parts:
                if part.function_call is not None:
                    if not is_closing_step:
                        fc_payload = {
                            "name": part.function_call.name,
                            "parameters": dict(part.function_call.args or {}),
                        }
                        new_parts.append(
                            types.Part.from_text(
                                text=json.dumps(fc_payload, default=str)
                            )
                        )
                    has_function_call = True
                elif part.function_response is not None:
                    resp_payload = part.function_response.response
                    if isinstance(resp_payload, dict):
                        if "scenario_summary" in resp_payload:
                            s_sum = resp_payload.get("scenario_summary")
                            if isinstance(s_sum, dict) and "consensus_pathway" in s_sum:
                                resp_payload = dict(resp_payload)
                                s_copy = dict(s_sum)
                                s_copy["consensus_pathway"] = "[GeoJSON mapped to visualization layer]"
                                resp_payload["scenario_summary"] = s_copy
                        if "a2ui" in resp_payload:
                            resp_payload = dict(resp_payload)
                            resp_payload["a2ui"] = "[A2UI rendered in dashboard]"

                    fr_text = json.dumps(
                        resp_payload,
                        default=str,
                    )
                    new_parts.append(
                        types.Part.from_text(
                            text=(
                                f"Observed result of tool `{part.function_response.name}`"
                                f" execution:\n{fr_text}"
                            )
                        )
                    )
                    has_function_response = True
                else:
                    new_parts.append(part)
            if has_function_call or has_function_response:
                content.parts = new_parts
                content.role = "user" if has_function_response else "model"
            if content.parts:
                filtered_contents.append(content)
        llm_request.contents = filtered_contents
        super()._move_function_calls_into_system_instruction(llm_request)

    def _extract_function_calls_from_response(
        self,
        llm_response: LlmResponse,
        allowed_tool_names: set[str] | None = None,
    ) -> None:
        if not llm_response.content or not llm_response.content.parts:
            return

        original_parts = list(llm_response.content.parts)
        super()._extract_function_calls_from_response(llm_response)
        if not llm_response.content or not llm_response.content.parts:
            return

        for part in llm_response.content.parts:
            if part.function_call is not None:
                clean_fc_name = (part.function_call.name or "").strip("`'\" \t\n\r")
                part.function_call.name = clean_fc_name
                if (
                    allowed_tool_names
                    and clean_fc_name not in allowed_tool_names
                ):
                    # Reject false-positive tool call whose name is not a declared tool.
                    llm_response.content.parts = original_parts
                    return
                if isinstance(part.function_call.args, dict):
                    part.function_call.args = self._normalize_tool_args(
                        clean_fc_name,
                        part.function_call.args,
                    )
                if not part.function_call.id:
                    part.function_call.id = f"call_{uuid.uuid4().hex[:8]}"
                return

        # Fallback parser for zero-arg tool calls (`{"name": "tool"}` without
        # `"parameters"`), OpenAI-style `"arguments"` keys, responses with
        # leading `thought` parts, multi-block responses where the tool call
        # is followed by another JSON/brace block, string-typed parameters,
        # or malformed/unescaped nested quotes inside JSON string arguments.
        if llm_response.partial:
            return
        thought_parts = [
            p for p in llm_response.content.parts if getattr(p, "thought", False)
        ]
        non_thought_parts = [
            p
            for p in llm_response.content.parts
            if not getattr(p, "thought", False)
        ]
        if not non_thought_parts:
            return
        if any(p.function_call is not None for p in non_thought_parts):
            return
        response_text = "\n".join(p.text for p in non_thought_parts if p.text).strip()
        if not response_text:
            return
        try:
            candidates: list[str] = []
            block_matches = re.findall(
                r"```(?:json|tool_code|tool_call)?\s*(.*?)\s*```",
                response_text,
                re.DOTALL,
            )
            for block in reversed(block_matches):
                stripped = block.strip()
                if stripped:
                    candidates.append(stripped)
            found, json_text = _get_last_valid_json_substring(response_text)
            if found and json_text:
                candidates.append(json_text.strip())

            decoder = json.JSONDecoder()
            raw_json_slices: list[str] = []
            for match in re.finditer(r"\{", response_text):
                start_idx = match.start()
                try:
                    obj, end_idx = decoder.raw_decode(response_text, start_idx)
                    if isinstance(obj, dict):
                        raw_json_slices.append(
                            response_text[start_idx:end_idx].strip()
                        )
                except ValueError:
                    continue
            for raw_slice in reversed(raw_json_slices):
                if raw_slice not in candidates:
                    candidates.append(raw_slice)

            for json_candidate in candidates:
                try:
                    parsed = json.loads(json_candidate)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(parsed, dict):
                    continue
                fn_name = parsed.get("name") or parsed.get("function")
                if not isinstance(fn_name, str) or not fn_name.strip():
                    continue
                fn_name = fn_name.strip("`'\" \t\n\r")
                if allowed_tool_names and fn_name not in allowed_tool_names:
                    continue

                has_param_key = False
                raw_params: Any = None
                for key in ("parameters", "args", "arguments"):
                    if key in parsed:
                        has_param_key = True
                        raw_params = parsed[key]
                        break
                if not has_param_key:
                    extra_keys = set(parsed.keys()) - {
                        "name",
                        "function",
                        "id",
                        "type",
                        "tool_call_id",
                    }
                    if not extra_keys:
                        params = {}
                    else:
                        params = {k: parsed[k] for k in extra_keys}
                elif isinstance(raw_params, dict):
                    params = raw_params
                elif isinstance(raw_params, str):
                    try:
                        loaded = json.loads(raw_params)
                        if isinstance(loaded, dict):
                            params = loaded
                        else:
                            params = {"payload": raw_params}
                    except Exception:
                        params = {"payload": raw_params}
                else:
                    continue

                if fn_name == "validate_and_emit_a2ui":
                    if not params or not params.get("payload"):
                        if params:
                            params = {"payload": json.dumps(params, default=str)}
                        else:
                            params = {"payload": "dashboard"}

                params = self._normalize_tool_args(fn_name, params)
                fc = types.FunctionCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name=fn_name,
                    args=params,
                )
                llm_response.content.parts = [
                    *thought_parts,
                    types.Part(function_call=fc),
                ]
                return

            repaired = self._repair_malformed_tool_call(
                response_text,
                allowed_tool_names=allowed_tool_names,
            )
            if repaired is not None:
                rep_name, rep_args = repaired
                fc = types.FunctionCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name=rep_name,
                    args=self._normalize_tool_args(rep_name, rep_args),
                )
                llm_response.content.parts = [
                    *thought_parts,
                    types.Part(function_call=fc),
                ]
                return
        except Exception:  # noqa: BLE001
            pass

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        if self.follow_settings and is_local_gemma_active():
            local_llm = get_local_litellm()
            async for resp in local_llm.generate_content_async(
                llm_request,
                stream=stream,
            ):
                yield resp
            return

        if self.follow_settings:
            endpoint = _ipv4_endpoint(get_ai_settings().endpoint)
            os.environ["OLLAMA_API_BASE"] = endpoint
            object.__setattr__(self, "api_base", endpoint)

        req = sanitize_request_for_local_gemma(llm_request, self.model)
        allowed_tool_names: set[str] = set()
        if req.config and req.config.tools:
            for t in req.config.tools:
                if isinstance(t, types.Tool) and t.function_declarations:
                    for fd in t.function_declarations:
                        if fd and fd.name:
                            allowed_tool_names.add(fd.name)

        fast_resp = self._try_fast_path_response(req, allowed_tool_names)
        if fast_resp is not None:
            yield fast_resp
            return

        # If the final step of a workflow just succeeded, clear tools and
        # replace tool-calling instructions with a prose-only closing prompt
        # so Gemma writes a clean Markdown summary without re-calling tools.
        is_closing_step = False
        closing_fallback_summary = ""
        last_effective = _last_non_transient_content(req)
        if last_effective and last_effective.parts:
            last_frs = [
                p.function_response
                for p in last_effective.parts
                if p.function_response is not None
            ]
            if last_frs:
                lfr = last_frs[-1]
                lfr_name = (lfr.name or "").strip()
                if isinstance(lfr.response, dict):
                    lfr_resp = lfr.response
                elif isinstance(lfr.response, str):
                    try:
                        loaded = json.loads(lfr.response)
                        lfr_resp = loaded if isinstance(loaded, dict) else {}
                    except Exception:
                        lfr_resp = {}
                else:
                    lfr_resp = {}

                is_closing_step = (
                    (
                        lfr_name == "validate_and_emit_a2ui"
                        and (
                            lfr_resp.get("status") in ("ready", "success")
                            or lfr_resp.get("result") in ("SUCCESS", "success")
                        )
                        and "error" not in lfr_resp
                    )
                    or (
                        lfr_name == "map_stakeholders"
                        and "evaluate_leadership_plan" not in allowed_tool_names
                        and "validate_and_emit_a2ui" not in allowed_tool_names
                        and (
                            lfr_resp.get("status") in ("ready", "success")
                            or lfr_resp.get("result") in ("SUCCESS", "success")
                        )
                        and "error" not in lfr_resp
                    )
                    or (
                        lfr_name == "emit_stakeholder_psychology"
                        and (
                            lfr_resp.get("status") in ("ready", "success")
                            or lfr_resp.get("result") in ("SUCCESS", "success")
                        )
                        and "error" not in lfr_resp
                    )
                    or (
                        lfr_name == "emit_translation_diagnostic"
                        and "emit_stakeholder_psychology" not in allowed_tool_names
                        and (
                            lfr_resp.get("status") in ("ready", "success")
                            or lfr_resp.get("result") in ("SUCCESS", "success")
                        )
                        and "error" not in lfr_resp
                    )
                    or (
                        lfr_name == "emit_action_plan_a2ui"
                        and "emit_translation_diagnostic" not in allowed_tool_names
                        and "emit_stakeholder_psychology" not in allowed_tool_names
                        and (
                            lfr_resp.get("status") in ("ready", "success")
                            or lfr_resp.get("result") in ("SUCCESS", "success")
                            or "SUCCESS" in str(lfr.response)
                        )
                        and "error" not in lfr_resp
                    )
                    or lfr_name
                    in {
                        "stop_race_collector",
                        "verify_plan",
                        "simulation_pipeline",
                    }
                )
                if is_closing_step:
                    closing_fallback_summary = (
                        self._build_closing_fallback_summary(req)
                    )
                    # Directly yield the comprehensive executive debrief on the closing step
                    # rather than dispatching a slow Ollama call that risks autoregressive hallucination.
                    yield LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[types.Part.from_text(text=closing_fallback_summary)],
                        )
                    )
                    return

        had_tools = bool(req.config and req.config.tools)
        # Always run _move_function_calls_into_system_instruction so multi-turn
        # function_call / function_response parts in req.contents are converted to
        # text messages even if req.config.tools is empty on a follow-up turn.
        self._move_function_calls_into_system_instruction(
            req,
            is_closing_step=is_closing_step,
        )
        # When tools are active or on a closing turn, disable token streaming so
        # raw JSON tool-call payloads are never flushed to the user's chat UI.
        effective_stream = False if (had_tools or is_closing_step) else stream
        async for resp in super().generate_content_async(
            req,
            stream=effective_stream,
        ):
            if had_tools:
                self._extract_function_calls_from_response(
                    resp,
                    allowed_tool_names=allowed_tool_names or None,
                )
            if is_closing_step:
                self._sanitize_closing_response(
                    resp,
                    closing_fallback_summary,
                )
            yield resp


class DynamicNonGeminiLiteLlm(LiteLlm):
    """LiteLLM wrapper for non-Gemini models that still honors runtime `local_gemma` switches."""

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        if is_local_gemma_active():
            local_llm = get_local_litellm()
            async for resp in local_llm.generate_content_async(
                llm_request,
                stream=stream,
            ):
                yield resp
            return
        req = sanitize_request_for_local_gemma(llm_request, self.model)
        async for resp in super().generate_content_async(req, stream=stream):
            yield resp


def get_local_litellm() -> LocalGemmaOllama:
    """Return a cached `LocalGemmaOllama` instance configured for the active Ollama model."""
    global _cached_litellm_key, _cached_litellm_model

    settings = get_ai_settings()
    endpoint = _ipv4_endpoint(settings.endpoint)
    clean_model = _validate_text_model(settings.text_model)
    target_model = f"ollama_chat/{clean_model}"

    os.environ["OLLAMA_API_BASE"] = endpoint
    key = (endpoint, target_model)
    if _cached_litellm_model is not None and _cached_litellm_key == key:
        return _cached_litellm_model

    _cached_litellm_model = LocalGemmaOllama(
        model=target_model,
        api_base=endpoint,
        follow_settings=False,
    )
    _cached_litellm_key = key
    return _cached_litellm_model


def build_non_gemini_litellm(model_str: str) -> LiteLlm:
    """Build a LiteLlm backend for non-Gemini model strings.

    Uses `LocalGemmaOllama` (with `GemmaFunctionCallingMixin`) when `model_str`
    targets an Ollama `gemma2` or `gemma3` model, and `DynamicNonGeminiLiteLlm`
    for models with native tool calling (e.g. `ollama_chat/gemma4:e2b`, `openai/...`).
    Both dynamically delegate to `get_local_litellm()` whenever the user activates
    `local_gemma` in Settings.
    """
    raw = (model_str or "").strip()
    clean = _strip_ollama_prefix(raw)
    if not raw.lower().startswith("openai/") and clean.lower().startswith(
        ("gemma2", "gemma3")
    ):
        endpoint = _ipv4_endpoint(get_ai_settings().endpoint)
        os.environ["OLLAMA_API_BASE"] = endpoint
        target = raw if raw.lower().startswith("ollama") else f"ollama_chat/{clean}"
        return LocalGemmaOllama(
            model=target,
            api_base=endpoint,
            follow_settings=True,
        )
    return DynamicNonGeminiLiteLlm(model=raw)


def _strip_markdown_json_fence(text: str) -> str:
    """Strip optional markdown code fences or surrounding prose around a JSON payload."""
    stripped = (text or "").strip()
    if not stripped:
        return stripped
    if "```" in stripped:
        match = re.search(
            r"```(?:json)?\s*(.*?)\s*```",
            stripped,
            re.DOTALL | re.IGNORECASE,
        )
        if match and match.group(1).strip():
            stripped = match.group(1).strip()
    try:
        json.loads(stripped)
        return stripped
    except Exception:  # noqa: BLE001
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", stripped):
        start_idx = match.start()
        try:
            _, end_idx = decoder.raw_decode(stripped, start_idx)
            return stripped[start_idx:end_idx].strip()
        except ValueError:
            continue
    return stripped


async def generate_local_json_async(
    prompt: str,
    *,
    system_instruction: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 20.0,
) -> str:
    """Generate a JSON string from the local Google Gemma model via Ollama `/api/chat`.

    Used by direct `genai.Client` callers (such as the post-conversation evaluator
    and translation-diagnostic skill) when `local_gemma` is active.
    """
    if timeout <= 0:
        timeout = 20.0
    env_to = os.environ.get("LOCAL_AI_TIMEOUT") or os.environ.get("LOCAL_GEMMA_TIMEOUT")
    if env_to:
        try:
            timeout = float(env_to)
        except ValueError:
            pass

    settings = get_ai_settings()
    endpoint = _ipv4_endpoint(settings.endpoint)
    model_name = _validate_text_model(settings.text_model)
    messages: list[dict[str, str]] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_name,
        "messages": messages,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx": 8192,
        },
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{endpoint}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return _strip_markdown_json_fence(content)
        response_text = data.get("response")
        if isinstance(response_text, str) and response_text.strip():
            return _strip_markdown_json_fence(response_text)
        raise ValueError("Empty JSON response from local Gemma model")


def _has_valid_embedding_norm(
    vec: list[float],
    target_dim: int = TARGET_EMBED_DIMENSION,
) -> bool:
    """Return True if `vec[:target_dim]` has finite values and a non-zero L2 norm."""
    if target_dim <= 0 or not vec:
        return False
    limit = min(len(vec), target_dim)
    sum_sq = 0.0
    for i in range(limit):
        try:
            v = float(vec[i])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(v):
            return False
        sum_sq += v * v
    return math.isfinite(sum_sq) and math.sqrt(sum_sq) > 1e-12


def normalize_and_pad_embedding(
    vec: list[float],
    target_dim: int = TARGET_EMBED_DIMENSION,
) -> list[float]:
    """L2-normalize `vec` and zero-pad (or truncate) it to `target_dim` floats.

    Zero-padding an L2-normalized 768-dim `embeddinggemma:latest` vector to 3072
    dimensions preserves exact cosine similarity (`1 - (a <=> b)`) in pgvector's
    `VECTOR(3072)` column without requiring a database schema migration.
    """
    if target_dim <= 0:
        return []
    if not vec:
        return [0.0] * target_dim

    limit = min(len(vec), target_dim)
    out = [0.0] * target_dim
    sum_sq = 0.0
    for i in range(limit):
        v = float(vec[i])
        if not math.isfinite(v):
            return [0.0] * target_dim
        sum_sq += v * v
    if not math.isfinite(sum_sq):
        return [0.0] * target_dim
    norm = math.sqrt(sum_sq)
    if norm > 1e-12:
        for i in range(limit):
            out[i] = float(vec[i]) / norm
    return out


async def compute_local_embedding_async(
    text: str,
    dimension: int = TARGET_EMBED_DIMENSION,
) -> list[float] | None:
    """Compute a vector embedding using the local Google `embeddinggemma` engine."""
    if not text or not text.strip() or dimension <= 0:
        return None
    settings = get_ai_settings()
    endpoint = _ipv4_endpoint(settings.endpoint)
    model = _validate_embed_model(settings.embed_model)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{endpoint}/api/embed",
                json={"model": model, "input": [text]},
            )
            if resp.status_code == 404:
                legacy = await client.post(
                    f"{endpoint}/api/embeddings",
                    json={"model": model, "prompt": text},
                )
                legacy.raise_for_status()
                raw_vec = legacy.json().get("embedding") or []
                if not _has_valid_embedding_norm(raw_vec, dimension):
                    logger.warning(
                        "LOCAL_AI: Rejecting empty or zero-norm legacy embedding from %s",
                        model,
                    )
                    return None
                return normalize_and_pad_embedding(raw_vec, dimension)

            resp.raise_for_status()
            embeddings = resp.json().get("embeddings") or []
            if not embeddings or not _has_valid_embedding_norm(
                embeddings[0], dimension
            ):
                logger.warning(
                    "LOCAL_AI: Rejecting empty or zero-norm embedding from %s",
                    model,
                )
                return None
            return normalize_and_pad_embedding(embeddings[0], dimension)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "LOCAL_AI: Local embedding (%s at %s) failed: %s",
            model,
            endpoint,
            exc,
        )
        return None
