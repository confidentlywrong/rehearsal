# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

import json
import math
import os
import time
import httpx
import logging
import asyncio
import re
from collections.abc import Mapping
from typing import Optional, Dict, Any, Union
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from a2a.client.client import ClientConfig as A2AClientConfig
from a2a.client.client_factory import ClientFactory as A2AClientFactory
from a2a.types import (
    AgentCard,
    TransportProtocol,
    Part,
    TextPart,
    Role,
    Message as A2AMessage,
    AgentCapabilities,
)
from agents.utils import auth as oidc_auth
from agents.utils import config
from agents.utils import tenant_keys
from agents.utils import user_auth_context
from agents.utils.gateway import get_gateway_url
from google.adk.tools.tool_context import ToolContext
from agents.utils.pulses import emit_inter_agent_pulse

logger = logging.getLogger(__name__)

_ANONYMOUS_USER_IDS = frozenset({"simulation", "simulation_user", "anonymous", "default", "user"})


def _strip_nonfinite(obj: object) -> object:
    """Recursively replace non-finite floats (NaN/Inf) with None for RFC 8259 JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_nonfinite(v) for v in obj]
    return obj


def strict_json_dumps(obj: object, **kwargs: Any) -> str:
    """Serialize *obj* to strict RFC-8259 JSON, replacing non-finite floats with null."""
    try:
        return json.dumps(obj, allow_nan=False, **kwargs)
    except ValueError:
        return json.dumps(_strip_nonfinite(obj), allow_nan=False, **kwargs)


def _extract_str_field(mapping: Any, *keys: str) -> Optional[str]:
    """Extract the first non-empty stripped string value for *keys* from *mapping*."""
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        val = mapping.get(key)
        if isinstance(val, str):
            stripped = val.strip()
            if stripped:
                return stripped
    return None


def resolve_tool_context_user_id(
    tool_context: Any,
    *,
    require_when_saas: bool = True,
) -> Optional[str]:
    """Resolve the acting user_id from a ToolContext.

    Inspection order:
      1. ``tool_context.state``: ``user_id``, ``acting_user_id``, ``userId``,
         or ``state["tenant"]["user_id"]``.
      2. ``tool_context._invocation_context.user_id`` (if present).
      3. ``tool_context.user_id`` (ADK ReadonlyContext property).
      4. ``tool_context.session.user_id`` (if present).

    When ``SAAS_MODE=on``, raises ``ValueError`` if an explicit anonymous
    ``user_id`` (e.g. ``"simulation_user"``) is encountered in ``state``, if
    ``tenant_keys._validate_component`` fails, or (when ``require_when_saas``
    is True) if no valid authenticated ``user_id`` can be resolved.
    When ``SAAS_MODE=off``, returns ``None`` if absent or anonymous so legacy
    single-tenant workflows remain backward-compatible.
    """
    saas_on = tenant_keys._saas_mode_enabled()
    if tool_context is None:
        if saas_on and require_when_saas:
            raise ValueError("Authenticated user_id is required when SAAS_MODE=on")
        return None

    candidates: list[tuple[str, Any]] = []
    state = getattr(tool_context, "state", None)
    if state is not None and hasattr(state, "get"):
        for key in ("user_id", "acting_user_id", "userId"):
            candidates.append((f"state.{key}", state.get(key)))
        tenant = state.get("tenant")
        if isinstance(tenant, Mapping):
            candidates.append(("state.tenant.user_id", tenant.get("user_id")))

    inv_ctx = getattr(tool_context, "_invocation_context", None)
    if inv_ctx is not None:
        candidates.append(("_invocation_context.user_id", getattr(inv_ctx, "user_id", None)))

    candidates.append(("tool_context.user_id", getattr(tool_context, "user_id", None)))

    session = getattr(tool_context, "session", None)
    if session is not None:
        candidates.append(("session.user_id", getattr(session, "user_id", None)))

    for source, raw in candidates:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        if cleaned.lower() in _ANONYMOUS_USER_IDS:
            if saas_on and source.startswith("state."):
                raise ValueError(f"Authenticated user_id is required when SAAS_MODE=on (got {cleaned!r})")
            continue
        if saas_on:
            return tenant_keys._validate_component(cleaned, "user_id")
        return cleaned

    if saas_on and require_when_saas:
        raise ValueError("Authenticated user_id is required when SAAS_MODE=on")
    return None


def _resolve_a2a_user_id(
    message: Union[str, Dict[str, Any]],
    user_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve user_id from explicit argument or JSON payload for A2AMessage.metadata."""
    saas_on = tenant_keys._saas_mode_enabled()

    raw_uid: Optional[str] = None
    if user_id is not None:
        if not isinstance(user_id, str):
            raise ValueError(f"Invalid user_id type: {type(user_id).__name__}")
        cleaned = user_id.strip()
        if not cleaned or cleaned.lower() in _ANONYMOUS_USER_IDS:
            if saas_on:
                raise ValueError("Authenticated user_id is required when SAAS_MODE=on")
            return None
        raw_uid = cleaned
    else:
        data: Optional[Mapping[str, Any]] = None
        if isinstance(message, Mapping):
            data = message
        elif isinstance(message, str) and message.lstrip().startswith("{"):
            try:
                parsed = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, Mapping):
                data = parsed

        if data is not None:
            candidate = _extract_str_field(data, "user_id", "acting_user_id", "userId")
            if not candidate:
                candidate = _extract_str_field(data.get("payload"), "acting_user_id", "user_id", "userId")
            if candidate:
                if candidate.lower() in _ANONYMOUS_USER_IDS:
                    if saas_on:
                        raise ValueError("Authenticated user_id is required when SAAS_MODE=on")
                    return None
                raw_uid = candidate

    if not raw_uid:
        if saas_on:
            raise ValueError("Authenticated user_id is required when SAAS_MODE=on")
        return None

    if saas_on:
        return tenant_keys._validate_component(raw_uid, "user_id")
    return raw_uid

# Module-level discovery cache (shared across all SimulationA2AClient instances).
# M4: keyed by gateway_url so processes that serve multiple tenants with
# distinct GATEWAY_INTERNAL_URL values (or test setups that mutate the env
# between calls) cannot conflate one tenant's registry with another's.
_discovery_cache: dict[str, dict[str, Any]] = {}
_discovery_cache_ts: dict[str, float] = {}
_DISCOVERY_CACHE_TTL: float = 30.0  # Match gateway's 30s TTL

# Shared httpx client for agent discovery (scoped to the running event loop)
_discovery_client: httpx.AsyncClient | None = None
_discovery_client_loop: asyncio.AbstractEventLoop | None = None


def _get_discovery_client() -> httpx.AsyncClient:
    """Get or create a shared httpx client for discovery calls.

    Recreates the client if it has been closed or if the running asyncio
    event loop has changed (or the previously recorded loop was closed).
    """
    global _discovery_client, _discovery_client_loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if (
        _discovery_client is None
        or getattr(_discovery_client, "is_closed", False)
        or (_discovery_client_loop is not None and _discovery_client_loop.is_closed())
        or (
            _discovery_client_loop is not None
            and current_loop is not None
            and _discovery_client_loop is not current_loop
        )
    ):
        _discovery_client = httpx.AsyncClient(timeout=5.0)
        _discovery_client_loop = current_loop
    return _discovery_client


def _is_agent_engine_url(url: str) -> bool:
    """Return True if the URL points to a Vertex AI Agent Engine resource."""
    return "aiplatform.googleapis.com" in url and "reasoningEngines" in url


class _GCPAuth(httpx.Auth):
    """httpx auth that injects a refreshed GCP OAuth2 token on every request.

    Used for Agent Engine, which accepts cloud-platform OAuth2 tokens.
    Refresh-per-request avoids expired-token errors during long simulations.
    """

    def __init__(self):
        import google.auth
        import google.auth.transport.requests

        self._credentials, _ = google.auth.default()
        self._request = google.auth.transport.requests.Request()

    async def async_auth_flow(self, request):
        if not self._credentials.valid:
            import asyncio
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._credentials.refresh, self._request)
        request.headers["Authorization"] = f"Bearer {self._credentials.token}"
        yield request


class _OIDCAuth(httpx.Auth):
    """httpx auth that injects a Google-signed OIDC ID token for Cloud Run
    service-to-service calls. Audience must equal the target URL (or
    IAP_CLIENT_ID when IAP-fronted). No header when get_id_token returns
    None (matches middleware.AttachOIDCToken's no-op contract on the Go side).
    """

    def __init__(self, audience: str):
        self._audience = audience

    def auth_flow(self, request):
        token = oidc_auth.get_id_token(self._audience)
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


class SimulationA2AClient:
    """
    Standardized client for inter-agent communication in the Leadership Simulation.
    Handles discovery via the Gateway and resilience for scale-to-zero.
    """

    def __init__(self, gateway_url: Optional[str] = None):
        config.load_env()
        # Prefer GATEWAY_INTERNAL_URL (Cloud Run internal, bypasses IAP) over
        # GATEWAY_URL (public/IAP-fronted). AE agents can reach the internal
        # URL via VPC/PSC but cannot authenticate through IAP.
        self.gateway_url = get_gateway_url(gateway_url)
        self._registry_cache: Dict[str, AgentCard] = {}
        self._agents: Dict[str, RemoteA2aAgent] = {}
        self._http_clients: list[httpx.AsyncClient] = []

    # [START a2a_discovery]
    async def _discover_agents(self) -> Dict[str, AgentCard]:
        """Fetch all available agent types from the Gateway registry.

        Uses a module-level cache with a 30s TTL to avoid redundant HTTP calls
        across SimulationA2AClient instances within the same process. The
        cache is keyed by ``gateway_url`` (M4) so a process serving multiple
        tenants with distinct gateway URLs keeps their registries isolated.
        """
        global _discovery_cache, _discovery_cache_ts
        now = time.monotonic()
        cached = _discovery_cache.get(self.gateway_url)
        cached_ts = _discovery_cache_ts.get(self.gateway_url, 0.0)
        if cached and (now - cached_ts) < _DISCOVERY_CACHE_TTL:
            self._registry_cache = cached
            return cached

        url = f"{self.gateway_url}/api/v1/agent-types"
        try:
            client = _get_discovery_client()
            # Attach OIDC token for OSS Cloud Run IAM mode (no-op when
            # get_id_token returns None: local dev / no ADC).
            headers: dict[str, str] = {}
            audience = oidc_auth.resolve_audience(self.gateway_url)
            token = oidc_auth.get_id_token(audience)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            resp = await client.get(url, timeout=5.0, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            cards = {}
            for name, card_data in data.items():
                # Use model_validate to handle camelCase→snake_case alias
                # resolution (e.g., additionalInterfaces, preferredTransport)
                cards[name] = AgentCard.model_validate(card_data)

            _discovery_cache[self.gateway_url] = cards
            _discovery_cache_ts[self.gateway_url] = now
            self._registry_cache = cards
            logger.info(f"A2A_DISCOVERY: Discovered {len(cards)} agent types from Gateway.")
            return cards
        except Exception as e:
            logger.error(f"A2A_DISCOVERY_ERROR: Failed to fetch agent types from {url}: {e}")
            if self._registry_cache:
                logger.warning("A2A_DISCOVERY: Returning stale registry cache after Gateway fetch failure")
                return self._registry_cache
            raise RuntimeError(f"Agent discovery failed from {url}: {e}") from e

    # [END a2a_discovery]

    async def get_agent(self, name: str, force_refresh: bool = False) -> RemoteA2aAgent:
        """Resolve an agent name to a RemoteA2aAgent instance."""
        if name in self._agents and not force_refresh:
            return self._agents[name]

        registry = await self._discover_agents()
        if name not in registry:
            # Fallback to env var if not in registry.
            # Check both {NAME}_URL and {NAME}_INTERNAL_URL (AE deployments
            # use INTERNAL_URL format).
            url = (
                (os.environ.get(f"{name.upper()}_URL") or "").strip()
                or (os.environ.get(f"{name.upper()}_INTERNAL_URL") or "").strip()
            )
            if not url:
                raise ValueError(f"Agent '{name}' not found in registry and no {name.upper()}_URL provided.")

            logger.warning(f"A2A_DISCOVERY: Agent '{name}' not in registry, using env URL: {url}")
            # Determine URL and transport based on whether this is an AE agent.
            is_ae = _is_agent_engine_url(url)
            if is_ae:
                # AE agents: append /a2a so RestTransport constructs the
                # correct path: {base}/a2a/v1/message:send (not /v1/message:send).
                a2a_url = f"{url.rstrip('/')}/a2a"
                transport = TransportProtocol.http_json
            else:
                # Local/Cloud Run: append the A2A mount path convention.
                # This matches the server's mount point in agents/utils/serve.py.
                a2a_url = f"{url.rstrip('/')}/a2a/{name}/"
                transport = TransportProtocol.jsonrpc
            card = AgentCard(
                name=name,
                url=a2a_url,
                description=f"Fallback for {name}",
                version="1.0.0",
                capabilities=AgentCapabilities(),
                default_input_modes=["text/plain"],
                default_output_modes=["text/plain"],
                skills=[],
                preferred_transport=transport,
            )
        else:
            card = registry[name]

        # Local/Cloud Run agents use JSON-RPC protocol (A2AStarletteApplication).
        # The Vertex AI SDK forces http_json on AgentCards, but that's the REST
        # transport which appends /v1/message:send — local servers don't serve that.
        # Only normalize to jsonrpc for non-AE agents. AE agents need http_json
        # because Agent Engine serves the REST transport at /a2a/v1/message:send.
        if _is_agent_engine_url(card.url or ""):
            # Ensure AE card URLs include the /a2a prefix so RestTransport
            # constructs {base}/a2a/v1/message:send (not /v1/message:send).
            if not (card.url or "").rstrip("/").endswith("/a2a"):
                card.url = f"{(card.url or '').rstrip('/')}/a2a"
        else:
            card.preferred_transport = TransportProtocol.jsonrpc

        # Build a ClientFactory that supports both jsonrpc and http_json
        # so that any server transport preference is compatible.
        # Simulation pipelines (e.g., simulator execute) can run for minutes.
        # 300s covers 24 ticks * 5s sleep + LLM overhead with margin.
        is_ae = _is_agent_engine_url(card.url or "")
        if is_ae:
            client_auth = _GCPAuth()
        else:
            # Cloud Run agent: attach OIDC ID token with audience derived
            # from the target service URL (or IAP_CLIENT_ID when set, for
            # IAP-fronted dev/prod). This is what unblocks OSS deploys
            # where the cloud-run-iam module enforces roles/run.invoker.
            client_auth = _OIDCAuth(audience=oidc_auth.resolve_audience(card.url or ""))
        raw_a2a_timeout = (os.environ.get("A2A_CALL_TIMEOUT_SECONDS") or "").strip()
        try:
            a2a_timeout_secs = float(raw_a2a_timeout) if raw_a2a_timeout else 900.0
        except ValueError:
            a2a_timeout_secs = 900.0
        if not math.isfinite(a2a_timeout_secs) or a2a_timeout_secs <= 0:
            a2a_timeout_secs = 900.0

        httpx_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout=a2a_timeout_secs, connect=30.0),
            auth=client_auth,
        )
        self._http_clients.append(httpx_client)
        # AE agents MUST use http_json (REST) transport — AE's RESTHandler
        # rejects JSON-RPC payloads with a protobuf ParseError on the "id"
        # field, returning a sporadic 400 FAILED_PRECONDITION.
        # Local/Cloud Run agents use jsonrpc (A2AStarletteApplication).
        if is_ae:
            transports = [TransportProtocol.http_json]
        else:
            transports = [TransportProtocol.jsonrpc, TransportProtocol.http_json]

        client_config = A2AClientConfig(
            httpx_client=httpx_client,
            streaming=False,
            polling=False,
            supported_transports=list(transports),  # type: ignore[arg-type]
        )
        factory = A2AClientFactory(config=client_config)

        agent = RemoteA2aAgent(
            name=name,
            agent_card=card,
            a2a_client_factory=factory,
            timeout=a2a_timeout_secs,  # Long timeout: simulation pipelines take minutes
        )
        self._agents[name] = agent
        return agent

    async def call_agent(
        self,
        agent_name: str,
        message: Union[str, Dict[str, Any]],
        retries: int = 3,
        backoff: float = 1.5,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        user_auth_token: Optional[str] = None,
    ) -> Any:
        """
        Call a remote agent with retry logic for scale-to-zero resilience.

        Args:
            agent_name: Name of the agent to call (e.g., 'runner').
            message: The message string or JSON dict to send.
            retries: Number of retries for 5xx or connection errors.
            backoff: Backoff multiplier for retries.
            session_id: Optional A2A context_id.
            user_id: Optional authenticated user_id to attach as A2A message metadata.
            user_auth_token: Optional per-user bearer token to attach as A2A message metadata.
        """
        resolved_user_id = _resolve_a2a_user_id(message, user_id=user_id)
        resolved_token: Optional[str] = None
        if isinstance(user_auth_token, str):
            cleaned_tok = user_auth_token.strip()
            if cleaned_tok and user_auth_context._is_valid_bearer_token(cleaned_tok):
                resolved_token = cleaned_tok
        if resolved_token is None and session_id:
            resolved_token = user_auth_context.get_session_token(session_id)

        agent = await self.get_agent(agent_name)

        # Prepare parts
        if isinstance(message, dict):
            text = strict_json_dumps(message)
        else:
            text = str(message)

        attempt = 0
        last_error = None

        while attempt <= retries:
            try:
                # We use a simple message exchange pattern for simulation tools.
                # Standard A2A flow via run_async is preferred for full history.
                # However, for simple tool-base probes, we use send_message directly.

                # ADK RemoteA2aAgent manages history internally if we use its higher level APIs.
                # For this utility, we'll provide a simplified 'request-response' wrapper.

                # Setup A2AMessage for a direct call
                from uuid import uuid4

                msg_id = str(uuid4())
                msg_metadata: dict[str, Any] = {}
                if resolved_user_id:
                    msg_metadata["user_id"] = resolved_user_id
                if resolved_token:
                    msg_metadata["user_auth_token"] = resolved_token

                a2a_msg = A2AMessage(
                    message_id=msg_id,
                    parts=[Part(root=TextPart(text=text))],
                    role=Role.user,
                    context_id=session_id,
                    metadata=msg_metadata or None,
                )

                # DIAGNOSTIC: Log the context_id being sent (never log raw tokens).
                logger.info(
                    "A2A_CALL_IDS: sending to %s msg_id=%s context_id=%s session_id=%s user_id=%s has_auth_token=%s",
                    agent_name,
                    msg_id,
                    a2a_msg.context_id,
                    session_id,
                    resolved_user_id,
                    bool(resolved_token),
                )

                # Ensure resolved (resolves HTTPS client etc)
                await agent._ensure_resolved()

                # ADK 2.x types `_a2a_client` as Optional and only assigns it
                # when a client factory is present, so resolution succeeding
                # does not by itself guarantee a client.  Fail loudly rather
                # than raise an opaque AttributeError mid-stream.
                a2a_client = agent._a2a_client
                if a2a_client is None:
                    raise RuntimeError(
                        f"A2A_CALL_FAILED: no A2A client available for {agent_name} "
                        "after resolution (missing client factory)"
                    )

                full_res_text = ""
                async for event in a2a_client.send_message(a2a_msg):
                    # Technical telemetry is now fired directly by the callee agent
                    # using the RedisDashLogPlugin. We no longer relay it here.

                    # Handle both Message response (direct) and tuple (task, update) response (task-based)
                    msg = None
                    if isinstance(event, tuple):
                        task, update = event
                        if update is None:
                            # Initial task state, might have a Message in status
                            status = getattr(task, "status", None)
                            if status and getattr(status, "message", None):
                                msg = getattr(status, "message", None)
                            elif hasattr(task, "history") and task.history:
                                # Fallback: last agent message in history
                                agent_msgs = [
                                    m for m in task.history if hasattr(m, "role") and getattr(m, "role", "") == "agent"
                                ]
                                if agent_msgs:
                                    msg = agent_msgs[-1]

                            # Fallback: check task artifacts (A2aAgentExecutor
                            # puts the final response here, not in status.message)
                            if not msg and hasattr(task, "artifacts") and task.artifacts:
                                for artifact in task.artifacts:
                                    if hasattr(artifact, "parts") and artifact.parts:
                                        for p in artifact.parts:
                                            res_text = ""
                                            if hasattr(p, "root") and isinstance(p.root, TextPart):
                                                res_text = p.root.text
                                            elif isinstance(p, TextPart):
                                                res_text = p.text
                                            if isinstance(res_text, str) and res_text:
                                                full_res_text += res_text
                        else:
                            status = getattr(update, "status", None)
                            if status and getattr(status, "message", None):
                                msg = getattr(status, "message", None)
                    elif isinstance(event, A2AMessage):
                        msg = event

                    if msg and msg.parts:
                        for p in msg.parts:
                            # Part is a RootModel[TextPart | FilePart | DataPart]
                            # Extract text from the root discriminated union.
                            res_text = ""
                            if hasattr(p, "root") and isinstance(p.root, TextPart):
                                res_text = p.root.text
                            elif isinstance(p, TextPart):
                                res_text = p.text

                            if isinstance(res_text, str) and res_text:
                                full_res_text += res_text

                if full_res_text:
                    return full_res_text

                logger.warning("A2A_CALL: No response text found from %s", agent_name)
                return None

            except Exception as e:
                attempt += 1
                last_error = e

                # Classify the error to decide retry behavior.
                err_str = str(e)
                status_code = getattr(e, "status_code", 0)
                if not status_code:
                    for code in (400, 401, 403, 404, 422, 500, 502, 503):
                        if re.search(rf"\b{code}\b", err_str):
                            status_code = code
                            break

                # Extract response body for diagnostics.
                response_body = ""
                cause = e.__cause__ if hasattr(e, "__cause__") else None
                while cause:
                    resp_obj = getattr(cause, "response", None)
                    if resp_obj is not None and hasattr(resp_obj, "text"):
                        response_body = resp_obj.text[:500]
                        break
                    cause = getattr(cause, "__cause__", None)

                # 400 from Agent Engine is a known transient routing error
                # (FAILED_PRECONDITION).  AE has two handler paths: a
                # RESTHandler (uses protobuf Parse with a2a.v1 protos
                # whose field names don't match the a2a-sdk JSON model)
                # and the normal A2A Starlette path.  When the request
                # hits the RESTHandler it fails with ParseError on fields
                # like "parts" or "id".  Retrying routes to a different
                # handler/instance and typically succeeds.
                #
                # 401/403/404/422 are genuine client errors -- don't retry.
                is_non_retryable = status_code in (401, 403, 404, 422)

                if is_non_retryable:
                    logger.error(
                        "A2A_CALL_ERROR: Failed to call %s (non-retryable %d): %s | body=%s",
                        agent_name,
                        status_code,
                        e,
                        response_body or "(n/a)",
                    )
                    raise

                logger.warning(f"A2A_CALL_RETRY: Attempt {attempt} failed for {agent_name}: {e}")
                if attempt <= retries:
                    wait_time = backoff**attempt
                    await asyncio.sleep(wait_time)
                else:
                    break

        logger.error(f"A2A_CALL_FAILED: All {attempt} attempts failed for {agent_name}: {last_error}")
        if last_error:
            raise last_error
        raise RuntimeError(f"A2A_CALL_FAILED: All {attempt} attempts failed for {agent_name}")

    async def close(self):
        """Close cached RemoteA2aAgent instances and underlying httpx connection pools."""
        for name, agent in list(self._agents.items()):
            try:
                if hasattr(agent, "cleanup"):
                    await agent.cleanup()
            except Exception as exc:
                logger.warning("A2A_CLIENT_CLOSE: agent cleanup failed for %s: %s", name, exc)
        self._agents.clear()
        for hc in self._http_clients:
            try:
                await hc.aclose()
            except Exception as exc:
                logger.warning("A2A_CLIENT_CLOSE: httpx client close failed: %s", exc)
        self._http_clients.clear()


async def call_agent(
    agent_name: str,
    message: str,
    tool_context: ToolContext,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> dict:
    """Proactively send a message or instruction to another agent via A2A.

    This is a standardized tool intended to be imported by ANY agent in the simulation.

    Args:
        agent_name: The name of the target agent (e.g., 'runner').
        message: The instruction or query to send.
        tool_context: ADK tool context for A2A client access.
        session_id: Optional explicit session ID for the target agent. If not
            provided, a fresh UUID is generated per call to avoid session reuse
            between caller and callee.
        user_id: Optional authenticated user_id for tenant-scoped A2A metadata.
    """
    logger.info(f"A2A_CALL: Agent calling {agent_name} with: {message}")

    try:
        ctx_user_id = resolve_tool_context_user_id(tool_context, require_when_saas=False)
        resolved_user_id = _resolve_a2a_user_id(message, user_id=ctx_user_id or user_id)
    except ValueError as e:
        logger.error(f"A2A_CALL_ERROR: Invalid or missing user_id calling {agent_name}: {e}")
        return {"status": "error", "message": str(e)}

    # Access the shared client via the plugin's registry
    from agents.utils.communication_plugin import get_client

    iid = tool_context.invocation_id
    a2a_client = get_client(iid)

    try:
        # On AE, tool_context.session.id is the Vertex AI-generated session ID
        # which differs from the gateway's spawn session UUID.  Map back to the
        # original context_id so the frontend's session-based filters match.
        from agents.utils.simulation_registry import get_context_id

        raw_session = getattr(tool_context, "session", None)
        raw_session_id = getattr(raw_session, "id", None)
        origin_session = str(raw_session_id or "default")
        mapped = (
            await get_context_id(origin_session, user_id=resolved_user_id)
            if resolved_user_id is not None
            else await get_context_id(origin_session)
        )
        if mapped:
            origin_session = mapped

        caller_token: Optional[str] = None
        if isinstance(raw_session_id, str) and raw_session_id:
            caller_token = user_auth_context.get_session_token(raw_session_id)
        if (
            caller_token is None
            and origin_session
            and origin_session != "default"
            and origin_session != raw_session_id
        ):
            caller_token = user_auth_context.get_session_token(origin_session)

        state_obj = getattr(tool_context, "state", None)
        simulation_id = state_obj.get("simulation_id") if state_obj is not None and hasattr(state_obj, "get") else None

        # Emit a pulse to show inter-agent communication (Request)
        await emit_inter_agent_pulse(
            session_id=origin_session,
            from_agent=tool_context.agent_name,
            to_agent=agent_name,
            message=message,
            simulation_id=simulation_id,
            user_id=resolved_user_id,
        )

        # Use explicit session_id if provided, otherwise generate a fresh UUID
        # to isolate the callee's session from the caller's.
        from uuid import uuid4

        target_session_id = session_id or str(uuid4())
        call_kwargs: dict[str, Any] = {"session_id": target_session_id}
        if resolved_user_id is not None:
            call_kwargs["user_id"] = resolved_user_id
        if caller_token is not None:
            call_kwargs["user_auth_token"] = caller_token
        response = await a2a_client.call_agent(
            agent_name,
            message,
            **call_kwargs,
        )

        # Emit a pulse for the response
        resp_prefix = str(response)[:100] + ("..." if len(str(response)) > 100 else "")
        await emit_inter_agent_pulse(
            session_id=origin_session,
            from_agent=tool_context.agent_name,
            to_agent=agent_name,
            message=resp_prefix,
            direction="response",
            simulation_id=simulation_id,
            user_id=resolved_user_id,
        )
        return {"status": "success", "agent": agent_name, "response": response}
    except Exception as e:
        logger.error(f"A2A_CALL_ERROR: Failed to call {agent_name}: {e}")
        return {"status": "error", "message": str(e)}
