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

"""Tenant-namespacing helper for on-disk simulation-run storage (Phase 3).

Sibling of :mod:`agents.utils.tenant_keys`. That module namespaces
Redis keys; this one namespaces filesystem paths under
``data/simulation_runs/``.

USAGE RULES (mirror the Redis-side guarantees):

* The storage layout relies entirely on the caller's fallback (e.g. the
  ``participant_id`` which acts as the ``user_id`` tenant boundary).
* The layout is strictly per-tenant ``data/simulation_runs/{tenant_id}/...``.

This module reads env vars on every call so test setup can mutate
``RACE_RUNS_DIR`` without re-importing.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.utils import simulation_registry
from agents.utils import tenant_keys

# Default base directory: ``<repo-root>/data/simulation_runs``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BASE_DIR = _REPO_ROOT / "data" / "simulation_runs"

_ANONYMOUS_USER_IDS = frozenset(
    {"anonymous", "default", "local-dev", "user", "simulation", "simulation_user", ""}
)


def runs_base_dir() -> Path:
    """Return the base directory holding every tenant's simulation runs.

    Honours ``RACE_RUNS_DIR`` for test/sandbox overrides; otherwise
    points at ``data/simulation_runs/`` next to the repo root.
    """
    override = os.environ.get("RACE_RUNS_DIR", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_BASE_DIR


def resolve_tenant_id(fallback: str | None = None) -> str:
    """Return the tenant id that should own a write.

    When ``SAAS_MODE=off``, preserves legacy behavior (returns ``fallback or ""``).
    When ``SAAS_MODE=on``, resolves a non-anonymous, path-safe tenant id from
    ``simulation_registry.get_current_user_id()`` or *fallback*, failing closed
    with ``ValueError`` if missing or anonymous.
    """
    if not tenant_keys._saas_mode_enabled():
        return fallback or ""

    active_uid = simulation_registry.get_current_user_id()
    if active_uid:
        if active_uid.lower() in _ANONYMOUS_USER_IDS:
            raise ValueError(
                f"tenant_paths: authenticated tenant_id is required when SAAS_MODE=on (got {active_uid!r})"
            )
        _validate_single_segment(active_uid, "tenant_id")
        tenant_keys._validate_component(active_uid, "tenant_id")
        return active_uid

    fb = fallback.strip() if isinstance(fallback, str) else ""
    if not fb or fb.lower() in _ANONYMOUS_USER_IDS:
        raise ValueError(
            f"tenant_paths: authenticated tenant_id is required when SAAS_MODE=on (got {fallback!r})"
        )
    _validate_single_segment(fb, "tenant_id")
    tenant_keys._validate_component(fb, "tenant_id")
    return fb


def _validate_single_segment(value: str, label: str) -> str:
    """Reject empty or path-traversing path components."""
    if not value or not value.strip():
        raise ValueError(f"tenant_paths: {label} must be non-empty")
    if value in (".", "..") or "/" in value or "\\" in value or "\x00" in value or ".." in value:
        raise ValueError(f"tenant_paths: unsafe {label} segment: {value!r}")
    return value


def tenant_runs_dir(tenant_id: str) -> Path:
    """Return ``data/simulation_runs/{tenant_id}/``.

    Does NOT create the directory — callers do that with the same
    ``mkdir(parents=True, exist_ok=True)`` pattern used today, so
    failure modes stay identical.
    """
    _validate_single_segment(tenant_id, "tenant_id")
    return runs_base_dir() / tenant_id


def run_path(tenant_id: str, sim_id: str) -> Path:
    """Return ``data/simulation_runs/{tenant_id}/{sim_id}.json``."""
    _validate_single_segment(sim_id, "sim_id")
    return tenant_runs_dir(tenant_id) / f"{sim_id}.json"


def summary_path(tenant_id: str) -> Path:
    """Return ``data/simulation_runs/{tenant_id}/runs.ndjson``.

    This is the append-only longitudinal summary file. One per tenant.
    """
    return tenant_runs_dir(tenant_id) / "runs.ndjson"
