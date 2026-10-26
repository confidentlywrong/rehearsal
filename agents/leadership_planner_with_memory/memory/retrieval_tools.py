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

"""In-loop retrieval tool for the leadership_planner_with_memory agent.

Exposes ``retrieve_document_passages`` for semantic search.
The tool calls the gateway's endpoint
``POST /api/v1/memory/analyses/:id/retrieve-passages``
and returns relevant text passages from uploaded documents.

Why this exists:

* Steps 0.9 (``emit_simulation_report``) and 1.0
  (``emit_stakeholder_interview``) invent their JSON from thin air by
  default. When an ``analysis_id`` is bound to the session, calling
  this tool BEFORE emitting lets the agent quote real accepted
  ``:Belief`` / ``:Friction`` nodes and attach them as ``citations``
  on the outgoing artifact. That is what turns ``/memory`` from a
  write-only curation surface into a searchable knowledge source
  the user can see referenced inside the chat panel.
* The tool is **opt-in and fail-soft**: without an analysis binding
  or without a gateway auth token, it returns a ``no_*`` status with
  ``citations=[]`` so the agent proceeds unchanged.

Auth:

* The gateway requires an operator identity (``requireOperator``).
  Precedence for the bearer token this tool sends:

  1. **Per-session user token** — the SPA sent the user's own
     bearer to ``POST /api/v1/spawn``; the gateway forwarded it in
     the spawn payload; the dispatcher parked it in
     :mod:`agents.utils.user_auth_context` keyed by session id.
     This tool reads it back via ``tool_context.session.id`` so the
     gateway sees the retrieval as coming from the *end user* — the
     correctness path for multi-user cloud deployments (user B's
     scenario retrieves from user B's memory, not from a shared
     service account's).
  2. **Shared env-var token** (``MEMORY_GATEWAY_TOKEN``) — legacy
     fallback for callers running outside a spawned session (ops
      scripts, standalone retrieval callers).
  3. **No Authorization header** — local-dev loopback path. The
     gateway's ``LocalResolver`` accepts any 127.0.0.1 caller as
     ``dev@local.test`` without a token, so an unset env var and
     an empty per-session mapping still Just Work locally.

  If the gateway rejects the call with 401/403 anyway (e.g. running
  against a cloud gateway with a stale token), the tool surfaces
  ``no_auth_configured`` rather than raising.

Session-state contract:

* Reads ``state["memory_analysis_id"]`` as the default analysis
  binding when the caller omits ``analysis_id``. When neither is
  set, the tool calls ``GET /api/v1/memory/analyses`` and
  auto-selects the caller's most-recent analysis (by ``created_at``)
  — the discovered id is cached back into that state key so the
  next call is a straight hit. When the caller owns zero analyses,
  the tool returns ``no_analysis_bound`` and the run continues
  without citations.
* Writes ``state["memory_citations_available"]`` — a set-like list of
  ``source_id`` strings the emit tools may cite. The emit tools
  cross-validate ``citations[].source_id`` against this set and
  reject unknown ids so hallucinated citations can never render.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from google.adk.tools.tool_context import ToolContext

from agents.utils import user_auth_context

logger = logging.getLogger(__name__)

# Reusable HTTP client for RAG gateway queries (avoids per-call TCP overhead).
_rag_http_client: httpx.AsyncClient | None = None

def _get_rag_client() -> httpx.AsyncClient:
    global _rag_http_client
    if _rag_http_client is None or _rag_http_client.is_closed:
        _rag_http_client = httpx.AsyncClient(timeout=30.0)
    return _rag_http_client


# ---------------------------------------------------------------------
# Session-state keys (module-scope constants so the emit tools import
# the exact same strings and the two sides can never drift).
# ---------------------------------------------------------------------

MEMORY_ANALYSIS_ID_KEY = "memory_analysis_id"

# Human-friendly messages for the status strings that
# _auto_discover_analysis_id can propagate out of the tool. Kept as a
# module-level table so the two return paths (explicit-fail vs
# auto-discover-fail) render the same wording for the same status.
_AUTO_DISCOVER_MESSAGES: dict[str, str] = {
    "no_analysis_bound": (
        "The caller owns zero memory analyses, so there is nothing to "
        "cite. Ask the user to curate at least one analysis in "
        "/memory (upload documents and accept beliefs/frictions) "
        "before expecting grounded citations. Proceeding without "
        "citations is fine."
    ),
    "no_auth_configured": (
        "Gateway rejected the analysis-list call. In cloud "
        "deployments this means MEMORY_GATEWAY_TOKEN is missing or "
        "invalid; in local dev it usually means the gateway is not "
        "running in local-dev mode (IDENTITY_PROVIDER=local) or the "
        "caller is not on a loopback address."
    ),
    "gateway_error": (
        "Gateway returned an unexpected error while listing "
        "analyses. Proceeding without citations."
    ),
}


def _gateway_url() -> str:
    """Resolve the gateway base URL.

    Priority mirrors the env-var resolution used by other retrieval
    surfaces so all callers share the same env-var story:
    ``MEMORY_GATEWAY_URL`` → ``GATEWAY_ADDR`` → ``http://127.0.0.1:9101``.
    """
    url = os.environ.get("MEMORY_GATEWAY_URL", "").strip()
    if url:
        return url.rstrip("/")
    addr = os.environ.get("GATEWAY_ADDR", "").strip()
    if addr:
        scheme = "http://" if "://" not in addr else ""
        return f"{scheme}{addr}".rstrip("/")
    return "http://127.0.0.1:9101"


def _bearer_token(tool_context: ToolContext | None = None) -> str | None:
    """Return the bearer token this run should send to the gateway.

    Precedence:

    1. **Per-session user token** — pushed into
       :mod:`agents.utils.user_auth_context` by the dispatcher when
       the spawn payload carried ``user_auth_token``. This is the
       end user's *own* bearer token, so the gateway sees the run
       as that user and scopes memory reads/writes accordingly.
       This is the cloud multi-user correctness path.
    2. **Shared env-var token** (``MEMORY_GATEWAY_TOKEN``) — legacy /
       service-account fallback for callers that run outside a
       spawned session (e.g. standalone retrieval callers
       agent or ops scripts) or for local dev if someone chooses
       to configure one.
    3. **``None``** — local-dev loopback: the gateway's
       ``LocalResolver`` will accept the unauthenticated call as
       ``dev@local.test``.
    """
    # 1. Session-scoped token, keyed by tool_context.session.id.
    if tool_context is not None:
        try:
            session_id = tool_context.session.id
        except AttributeError:
            session_id = None
        if isinstance(session_id, str) and session_id:
            per_user = user_auth_context.get_session_token(session_id)
            if per_user:
                return per_user
    # 2. Env-var fallback.
    tok = os.environ.get("MEMORY_GATEWAY_TOKEN", "").strip()
    return tok or None


def _resolve_analysis_id(
    explicit: str | None,
    tool_context: ToolContext,
) -> str | None:
    """Explicit arg wins; otherwise pull from session state.

    Returns ``None`` when neither is set — the caller then falls
    through to :func:`_auto_discover_analysis_id`.
    """
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        state_val = tool_context.state.get(MEMORY_ANALYSIS_ID_KEY)
    except (AttributeError, TypeError):
        return None
    if isinstance(state_val, str) and state_val.strip():
        return state_val.strip()
    return None


def _build_headers(tool_context: ToolContext | None = None) -> dict[str, str]:
    """Standard request headers, with Authorization only when configured.

    Precedence in :func:`_bearer_token` decides which token (per-user
    session-scoped, env-var fallback, or none) ends up on the wire.
    Local dev omits Authorization entirely so the gateway's loopback
    LocalResolver takes over.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    tok = _bearer_token(tool_context)
    if tok is not None:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


# TODO: Allow operators to explicitly select an analysis_id in the
# Organizer panel instead of auto-discovering the latest one.
async def _auto_discover_analysis_id(
    client: httpx.AsyncClient,
    tool_context: ToolContext,
) -> tuple[str | None, str | None]:
    """List the caller's analyses and pick the most-recent one.

    Returns ``(analysis_id, error_status)``. Exactly one of the two
    is non-``None`` on any given call. ``error_status`` is one of the
    same ``no_*`` / ``gateway_error`` strings the top-level tool
    surfaces, so callers can proxy it straight through.

    The discovered id is cached back into
    ``state["memory_analysis_id"]`` so a second retrieve within the
    same session skips this hop.
    """
    url = f"{_gateway_url()}/api/v1/memory/analyses"
    try:
        resp = await client.get(url, headers=_build_headers(tool_context), timeout=10.0)
    except httpx.HTTPError as exc:
        logger.warning("auto-discover: transport error listing analyses: %s", exc)
        return None, "gateway_error"

    if resp.status_code in (401, 403):
        return None, "no_auth_configured"
    if resp.status_code >= 400:
        logger.warning(
            "auto-discover: gateway HTTP %s listing analyses",
            resp.status_code,
        )
        return None, "gateway_error"
    try:
        payload = resp.json()
    except ValueError:
        return None, "gateway_error"

    analyses = payload.get("analyses") if isinstance(payload, dict) else None
    if not isinstance(analyses, list) or not analyses:
        # Caller owns zero analyses — legitimate empty state, not an
        # error. Surface no_analysis_bound so the LLM proceeds without
        # citations.
        return None, "no_analysis_bound"

    # Defensive sort by created_at desc — don't depend on server
    # ordering. RFC3339 strings sort lexicographically the same way
    # they sort chronologically, so a plain string sort works.
    def _created_at(row: Any) -> str:
        if isinstance(row, dict):
            v = row.get("created_at")
            if isinstance(v, str):
                return v
        return ""

    analyses_sorted = sorted(analyses, key=_created_at, reverse=True)
    picked = analyses_sorted[0]
    if not isinstance(picked, dict):
        return None, "gateway_error"
    aid = picked.get("id")
    if not isinstance(aid, str) or not aid.strip():
        return None, "gateway_error"
    aid_clean = aid.strip()

    # Cache the pick back so subsequent retrieves in the same
    # session skip this hop.
    try:
        tool_context.state[MEMORY_ANALYSIS_ID_KEY] = aid_clean
    except (AttributeError, TypeError):
        # Read-only state (tests). Not fatal — next call will just
        # rediscover.
        pass

    logger.info(
        "auto-discover: bound session to most-recent analysis %s (%r)",
        aid_clean,
        picked.get("title", ""),
    )
    return aid_clean, None




# ---------------------------------------------------------------------------
# Document passage retrieval (RAG over uploaded documents)
# ---------------------------------------------------------------------------

_PASSAGE_DEFAULT_LIMIT = 5
_PASSAGE_HARD_LIMIT = 20


async def retrieve_document_passages(
    query: str,
    tool_context: ToolContext,
    analysis_id: str | None = None,
    limit: int = _PASSAGE_DEFAULT_LIMIT,
) -> dict:
    """Retrieve relevant passages from uploaded documents for the current analysis.

    This tool performs vector similarity search over chunked document
    text. Documents are chunked and embedded at upload time; this tool
    embeds the *query*, sends it to the gateway's ``retrieve-passages``
    endpoint, and returns ranked text passages.

    This tool returns raw document text — the planner uses it
    as grounding context when designing scenarios.

    Fail-soft: returns empty passages on any error (no analysis bound,
    gateway unreachable, no documents uploaded). Never raises.

    Args:
        query: Free-text scenario question or topic. The gateway embeds
            this and runs cosine similarity against document chunks.
        tool_context: ADK ToolContext (injected).
        analysis_id: Optional explicit analysis UUID. When omitted,
            reads from ``state["memory_analysis_id"]`` or auto-discovers.
        limit: Maximum passages to return (1–20, default 5).

    Returns:
        On success: ``{"status": "success", "analysis_id": ...,
            "passage_count": N, "passages": [{"text": ...,
            "score": ..., "document_id": ..., "chunk_index": ...}]}``.
        On soft failure: ``{"status": "no_analysis_bound"
            | "no_documents" | "gateway_error", "message": "...",
            "passages": []}`` — never raises.
    """
    clamped_limit = max(1, min(int(limit or _PASSAGE_DEFAULT_LIMIT), _PASSAGE_HARD_LIMIT))
    query_text = (query or "").strip()
    if not query_text:
        return {
            "status": "no_query",
            "message": "Empty query — nothing to search for.",
            "passages": [],
        }

    headers = _build_headers(tool_context)

    try:
        client = _get_rag_client()
        # 1. Resolve the analysis id.
        aid = _resolve_analysis_id(analysis_id, tool_context)
        if not aid:
            aid, err_status = await _auto_discover_analysis_id(
                client, tool_context
            )
            if err_status is not None:
                return {
                    "status": err_status,
                    "message": _AUTO_DISCOVER_MESSAGES.get(
                        err_status,
                        "Could not resolve analysis for document retrieval.",
                    ),
                    "passages": [],
                }
        assert aid is not None

        # 2. Call the retrieve-passages endpoint.
        url = f"{_gateway_url()}/api/v1/memory/analyses/{aid}/retrieve-passages"
        body = {"query": query_text, "limit": clamped_limit}
        resp = await client.post(url, json=body, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        logger.warning("retrieve_document_passages: transport error: %s", exc)
        return {
            "status": "gateway_error",
            "message": f"transport error contacting gateway: {exc}",
            "passages": [],
        }

    if resp.status_code == 404:
        return {
            "status": "not_found",
            "message": f"analysis {aid} not found or not owned by the caller",
            "passages": [],
        }
    if resp.status_code in (401, 403):
        return {
            "status": "no_auth_configured",
            "message": (
                f"gateway rejected the retrieve-passages call with HTTP "
                f"{resp.status_code}"
            ),
            "passages": [],
        }
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:300]
        logger.warning(
            "retrieve_document_passages: gateway HTTP %s: %s",
            resp.status_code,
            detail,
        )
        return {
            "status": "gateway_error",
            "message": f"gateway returned HTTP {resp.status_code}",
            "passages": [],
        }

    try:
        payload = resp.json()
    except ValueError:
        return {
            "status": "gateway_error",
            "message": "gateway response was not JSON",
            "passages": [],
        }
    if not isinstance(payload, dict):
        return {
            "status": "gateway_error",
            "message": "gateway response was not a JSON object",
            "passages": [],
        }

    passages = payload.get("passages")
    if not isinstance(passages, list):
        passages = []

    # Populate memory_citations_available for emit tools to validate against.
    citation_source_ids = list({p["source_id"] for p in passages if p.get("source_id")})
    tool_context.state["memory_citations_available"] = citation_source_ids

    # Normalise each passage to a consistent shape.
    cleaned: list[dict[str, Any]] = []
    for p in passages:
        if not isinstance(p, dict):
            continue
        text = (p.get("text") or "").strip()
        if not text:
            continue
        cleaned.append({
            "text": text,
            "score": float(p.get("score") or 0.0),
            "document_id": p.get("document_id", ""),
            "chunk_index": int(p.get("chunk_index") or 0),
        })

    logger.info(
        "retrieve_document_passages: analysis=%s query_len=%d -> %d passages",
        aid,
        len(query_text),
        len(cleaned),
    )
    return {
        "status": "success",
        "analysis_id": aid,
        "passage_count": len(cleaned),
        "passages": cleaned,
    }

