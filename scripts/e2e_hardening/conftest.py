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

"""Shared pytest fixtures and marker registration for scripts/e2e_hardening/."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    IsolatedPostgresSchema,
    MockOllamaServer,
    PostgresUnavailableError,
    build_go_binary,
    get_git_status_snapshot,
)


def pytest_configure(config: pytest.Config) -> None:
    """Register E2E tier and capability markers without mutating pyproject.toml."""
    markers = [
        "tier1: Tier 1 category-partition functional tests",
        "tier2: Tier 2 boundary value and negative error-path tests",
        "tier3: Tier 3 pairwise cross-feature interaction tests",
        "tier4: Tier 4 full compliance acceptance and end-to-end workload tests",
        "tier4_slow: Tier 4 heavy full-repo build, race, and typecheck gates",
        "requires_postgres: Test requires a reachable PostgreSQL instance",
        "requires_redis: Test requires a reachable Redis instance",
        "requires_go: Test requires Go toolchain",
        "requires_node: Test requires Node.js / npm toolchain",
    ]
    for marker in markers:
        config.addinivalue_line("markers", marker)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def go_bin_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Temporary directory outside the workspace for compiled Go binaries."""
    return tmp_path_factory.mktemp("e2e_go_bins")


@pytest.fixture(scope="session")
def gateway_binary(go_bin_dir: Path) -> Path:
    """Compiled cmd/gateway binary in temporary directory."""
    return build_go_binary("./cmd/gateway", go_bin_dir, "gateway")


@pytest.fixture(scope="session")
def admin_binary(go_bin_dir: Path) -> Path:
    """Compiled cmd/admin binary in temporary directory."""
    return build_go_binary("./cmd/admin", go_bin_dir, "admin")


@pytest.fixture(scope="session")
def billing_binary(go_bin_dir: Path) -> Path:
    """Compiled cmd/billing binary in temporary directory."""
    return build_go_binary("./cmd/billing", go_bin_dir, "billing")


@pytest.fixture(scope="session")
def frontend_binary(go_bin_dir: Path) -> Path:
    """Compiled cmd/frontend binary in temporary directory."""
    return build_go_binary("./cmd/frontend", go_bin_dir, "frontend")


@pytest.fixture(scope="session")
def tester_binary(go_bin_dir: Path) -> Path:
    """Compiled cmd/tester binary in temporary directory."""
    return build_go_binary("./cmd/tester", go_bin_dir, "tester")


@pytest.fixture()
def mock_ollama() -> Iterator[MockOllamaServer]:
    """Ephemeral local MockOllamaServer (never port 11434)."""
    with MockOllamaServer() as server:
        yield server


@pytest.fixture()
def isolated_pg_schema() -> Iterator[IsolatedPostgresSchema]:
    """Isolated temporary PostgreSQL schema dropped cleanly on teardown."""
    try:
        with IsolatedPostgresSchema(require_available=True) as pg:
            yield pg
    except PostgresUnavailableError as exc:
        pytest.skip(f"PostgreSQL unreachable for live DB test: {exc}")


@pytest.fixture(scope="session")
def baseline_git_snapshot() -> dict[str, str]:
    """Baseline git status snapshot captured once at session start."""
    return get_git_status_snapshot(REPO_ROOT)
