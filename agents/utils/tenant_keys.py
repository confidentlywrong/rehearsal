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

"""Tenant-namespacing helper for Redis keys (Phase 2).

This is the Python mirror of ``internal/account/keys.go``. Both must
produce IDENTICAL key strings for the same inputs — the gateway
(Go) and the agents (Python) read and write the same Redis keys.

PHASE 2.5 STATUS: integrated into ``agents/utils/simulation_registry.py``,
``agents/utils/dispatcher.py``, ``agents/utils/pulses.py``,
``agents/simulator/collector.py``, and ``agents/simulator/broadcast.py``.

USAGE RULES (mirrored from the Go side):

* SAAS_MODE=off (default): ``key(ctx, ...)`` returns the legacy
  un-prefixed key. Behavior is byte-identical to the single-tenant
  app that exists today.
* SAAS_MODE=on, tenant present: prepends ``{ACCOUNT_KEY_PREFIX}:{user_id}``.
* SAAS_MODE=on, tenant missing: falls through to the legacy key so
  the Phase 2.5 compatibility shim can still find pre-migration data.

This module reads env vars on every call so test setup can mutate
``SAAS_MODE`` and ``ACCOUNT_KEY_PREFIX`` freely without re-importing.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional


def _saas_mode_enabled() -> bool:
    """Mirror of ``tenant.SAASModeEnabled`` in Go.

    Accepts ``on``/``true``/``1``/``yes`` (case-insensitive).
    """
    return os.environ.get("SAAS_MODE", "off").strip().lower() in ("on", "true", "1", "yes")


def _validate_component(value: str, label: str) -> str:
    """Reject delimiter collisions (':'), whitespace, or control chars in key parts."""
    if ":" in value or any(ord(ch) <= 0x20 or ord(ch) >= 0x7F for ch in value):
        raise ValueError(f"tenant_keys: invalid {label} {value!r}")
    return value


def _key_prefix_for_user(user_id: str) -> str:
    if not isinstance(user_id, str):
        return ""
    cleaned = user_id.strip()
    if not cleaned:
        return ""
    _validate_component(cleaned, "user_id")
    root = os.environ.get("ACCOUNT_KEY_PREFIX", "").strip() or "u"
    _validate_component(root, "ACCOUNT_KEY_PREFIX")
    return f"{root}:{cleaned}"


def key_prefix(tenant_state: Optional[Mapping[str, object]]) -> str:
    """Return the namespace prefix for the given tenant state.

    ``tenant_state`` is the dict the gateway adds to session state
    under the key ``tenant``. It must look like ``{"user_id": ..., ...}``.
    None or a missing ``user_id`` means "no tenant" — caller decides
    whether to fall through to the legacy key or reject.

    Returns the empty string in SAAS_MODE=off or when no user is set.
    """
    if not _saas_mode_enabled():
        return ""
    return _key_prefix_for_user(_user_from_state(tenant_state))


def key(tenant_state: Optional[Mapping[str, object]], *segments: str) -> str:
    """Compose a Redis key, prepending the per-tenant namespace if any.

    Examples (with ACCOUNT_KEY_PREFIX=u):

        # SAAS_MODE=off
        key(None, "session", "abc")        -> "session:abc"

        # SAAS_MODE=on, user=local-dev
        key({"user_id": "local-dev"}, "session", "abc")
                                           -> "u:local-dev:session:abc"

        # SAAS_MODE=on, no tenant (compat fallback)
        key(None, "session", "abc")        -> "session:abc"

    Empty segments are dropped so callers don't have to special-case
    a missing trailing ID.
    """
    return key_for_user(
        _user_from_state(tenant_state),
        *segments,
    )


def key_for_user(user_id: str, *segments: str) -> str:
    """User-explicit form of :func:`key`. Pass ``""`` to suppress prefix."""
    parts: list[str] = []
    if _saas_mode_enabled():
        prefix = _key_prefix_for_user(user_id)
        if prefix:
            parts.append(prefix)
    for seg in segments:
        if not isinstance(seg, str):
            raise ValueError(f"tenant_keys: segment must be a string, got {type(seg).__name__}")
        if not seg or not seg.strip():
            continue
        _validate_component(seg, "segment")
        parts.append(seg)
    return ":".join(parts)


def user_from_simulation_channel(channel: str | bytes | None) -> tuple[str, Optional[str]]:
    """Extract ``(user_id, simulation_id)`` from a tenant-prefixed simulation channel.

    Supports:
    - ``{ACCOUNT_KEY_PREFIX}:{user_id}:simulation:broadcast`` -> ``(user_id, None)``
    - ``{ACCOUNT_KEY_PREFIX}:{user_id}:simulation:{sim_id}:broadcast`` -> ``(user_id, sim_id)``

    Returns ``("", None)`` in ``SAAS_MODE=off`` or if *channel* is unprefixed/malformed.
    """
    if not _saas_mode_enabled() or channel is None:
        return "", None
    if isinstance(channel, bytes):
        try:
            ch = channel.decode("utf-8")
        except UnicodeDecodeError:
            return "", None
    elif isinstance(channel, str):
        ch = channel
    else:
        return "", None
    root = os.environ.get("ACCOUNT_KEY_PREFIX", "").strip() or "u"
    parts = ch.split(":")
    if len(parts) not in (4, 5):
        return "", None
    if parts[0] != root or parts[2] != "simulation" or parts[-1] != "broadcast":
        return "", None
    raw_user = parts[1]
    if not raw_user or not raw_user.strip():
        return "", None
    try:
        uid = _validate_component(raw_user, "user_id")
    except ValueError:
        return "", None
    if len(parts) == 5:
        raw_sim = parts[3]
        if not raw_sim or not raw_sim.strip():
            return "", None
        try:
            sim_id = _validate_component(raw_sim, "simulation_id")
        except ValueError:
            return "", None
        return uid, sim_id
    return uid, None


def _user_from_state(tenant_state: Optional[Mapping[str, object]]) -> str:
    if not isinstance(tenant_state, Mapping):
        return ""
    user_id = tenant_state.get("user_id")
    if not isinstance(user_id, str):
        return ""
    return user_id.strip()


_ECHO_ANONYMOUS_USER_IDS = frozenset(
    {"simulation", "simulation_user", "anonymous", "default", "user", "local-dev", ""}
)


def echo_config_key(session_id: str, user_id: str | None = None) -> str:
    """Return the Redis key for an Echo config, tenant-scoped when ``SAAS_MODE=on``.

    When ``SAAS_MODE=off``, returns legacy ``echo_config:{session_id}``.
    When ``SAAS_MODE=on``, resolves a non-anonymous ``user_id`` from
    *user_id*, ``simulation_registry.get_current_user_id()``, or
    ``DISPATCHER_USER_ID`` and returns
    ``key_for_user(uid, "echo_config", session_id)``.
    """
    if not _saas_mode_enabled():
        return f"echo_config:{session_id}"
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("echo_config_key: session_id must be a non-empty string")
    if user_id is not None:
        if not isinstance(user_id, str):
            raise ValueError(f"echo_config_key: user_id must be a string, got {type(user_id).__name__}")
        uid = user_id.strip()
    else:
        from agents.utils import simulation_registry

        uid = simulation_registry.get_current_user_id()
        if not uid:
            uid = os.environ.get("DISPATCHER_USER_ID", "").strip()
    if not uid or uid.lower() in _ECHO_ANONYMOUS_USER_IDS:
        raise ValueError(
            f"Authenticated user_id is required for echo_config when SAAS_MODE=on (got {uid!r})"
        )
    uid = _validate_component(uid, "user_id")
    return key_for_user(uid, "echo_config", session_id.strip())
