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

"""Canonical Go Gateway port and URL resolution helpers."""

from __future__ import annotations

import os

DEFAULT_GATEWAY_PORT: int = 9101
DEFAULT_GATEWAY_URL: str = f"http://127.0.0.1:{DEFAULT_GATEWAY_PORT}"


def _normalize_gateway_url(raw: str) -> str:
    url = raw.strip()
    if "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


def get_gateway_url(
    explicit: str | None = None,
    *,
    include_memory_env: bool = False,
) -> str:
    """Resolve the Go Gateway base URL cleanly without trailing slashes.

    Precedence:
      1. ``explicit`` argument (when non-empty)
      2. ``MEMORY_GATEWAY_URL`` (when ``include_memory_env=True``)
      3. ``GATEWAY_INTERNAL_URL``
      4. ``GATEWAY_URL``
      5. ``GATEWAY_ADDR`` (prepends ``http://`` if scheme is absent)
      6. ``DEFAULT_GATEWAY_URL`` (``http://127.0.0.1:9101``)
    """
    candidates: list[str | None] = [explicit]
    if include_memory_env:
        candidates.append(os.environ.get("MEMORY_GATEWAY_URL"))
    candidates.extend(
        [
            os.environ.get("GATEWAY_INTERNAL_URL"),
            os.environ.get("GATEWAY_URL"),
            os.environ.get("GATEWAY_ADDR"),
        ]
    )
    for raw in candidates:
        if raw and raw.strip():
            return _normalize_gateway_url(raw)
    return DEFAULT_GATEWAY_URL


async def register_simulation_with_gateway(
    simulation_id: str,
    *,
    tool_context: object = None,
    timeout: float = 3.0,
) -> bool:
    """Best-effort pre-registration of `simulation_id` with Gateway's session registry.

    Allows `subscribe_simulation` ownership checks to succeed immediately when
    `start_simulation` returns before `spawn_runners` invokes `/api/v1/spawn`.
    """
    if not isinstance(simulation_id, str) or not simulation_id.strip():
        return False

    import logging
    import httpx
    from agents.utils import auth as oidc_auth
    from agents.utils import user_auth_context

    logger = logging.getLogger(__name__)
    gateway_url = get_gateway_url()
    sessions_url = f"{gateway_url}/api/v1/sessions"

    headers: dict[str, str] = {}
    audience = oidc_auth.resolve_audience(gateway_url)
    oidc_tok = oidc_auth.get_id_token(audience)
    if oidc_tok:
        headers["Authorization"] = f"Bearer {oidc_tok}"

    user_token: str | None = None
    sess = getattr(tool_context, "session", None) if tool_context is not None else None
    sess_id = getattr(sess, "id", None) if sess is not None else None
    for cand in (sess_id, simulation_id):
        if isinstance(cand, str) and cand.strip():
            tok = user_auth_context.get_session_token(cand.strip())
            if tok and user_auth_context._is_valid_bearer_token(tok):
                user_token = tok
                break
    if user_token:
        if "Authorization" in headers:
            headers["X-User-Authorization"] = f"Bearer {user_token}"
        else:
            headers["Authorization"] = f"Bearer {user_token}"
        headers["Cookie"] = f"auth_session={user_token}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                sessions_url,
                json={"agentType": "simulator", "simulation_id": simulation_id.strip()},
                headers=headers or None,
            )
            return resp.status_code in (200, 201)
    except Exception as exc:  # noqa: BLE001
        logger.debug("register_simulation_with_gateway failed (best-effort): %s", exc)
        return False
