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

"""Unit tests for ``agents.utils.tenant_keys``.

These tests MUST produce the same outputs as the Go-side keys_test.go
for the same inputs (TestKey_LegacyFormatStableUnderRefactor is the
canary).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agents.utils import tenant_keys


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip SAAS_MODE / ACCOUNT_KEY_PREFIX from the env so each test
    sets exactly what it needs. We re-read on every call so this is
    sufficient.
    """
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("ACCOUNT_KEY_PREFIX", raising=False)
    yield


def test_key_saas_mode_off_always_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAAS_MODE", "off")
    state = {"user_id": "u-1"}
    assert tenant_keys.key_prefix(state) == ""
    assert tenant_keys.key(state, "session", "abc") == "session:abc"


def test_key_saas_mode_on_with_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    state = {"user_id": "user-1"}
    assert tenant_keys.key_prefix(state) == "u:user-1"
    assert tenant_keys.key(state, "session", "abc") == "u:user-1:session:abc"


def test_key_saas_mode_on_no_tenant_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAAS_MODE", "on")
    assert tenant_keys.key_prefix(None) == ""
    # Compat-shim contract: still produces a usable legacy key so
    # the registry can fall back to pre-migration data.
    assert tenant_keys.key(None, "sim", "x") == "sim:x"


def test_key_custom_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "rc")
    state = {"user_id": "user-1"}
    assert tenant_keys.key(state, "session", "abc") == "rc:user-1:session:abc"


def test_key_skips_empty_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    state = {"user_id": "user-1"}
    assert tenant_keys.key(state, "session", "", "abc", "") == "u:user-1:session:abc"


def test_key_for_user_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    got = tenant_keys.key_for_user("user-1", "simulation", "spawns", "runner")
    assert got == "u:user-1:simulation:spawns:runner"


def test_key_for_user_empty_no_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAAS_MODE", "on")
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    got = tenant_keys.key_for_user("", "simulation", "spawns", "runner")
    assert got == "simulation:spawns:runner"


def test_key_for_user_saas_off_never_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAAS_MODE", "off")
    got = tenant_keys.key_for_user("user-1", "simulation", "spawns", "runner")
    assert got == "simulation:spawns:runner"


def test_legacy_format_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    """This MUST match the Go-side TestKey_LegacyFormatStableUnderRefactor."""
    monkeypatch.setenv("SAAS_MODE", "off")
    cases = {
        "session:abc-123": ("session", "abc-123"),
        "agent-sessions:runner": ("agent-sessions", "runner"),
        "simulation:spawns:runner_autopilot:7": (
            "simulation",
            "spawns",
            "runner_autopilot",
            "7",
        ),
        "simulation:broadcast": ("simulation", "broadcast"),
        "simulation:sim-42:sessions": ("simulation", "sim-42", "sessions"),
    }
    for expected, segments in cases.items():
        assert tenant_keys.key(None, *segments) == expected, f"legacy mismatch: segments={segments!r}"


def test_saas_mode_truthy_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror of Go SAASModeEnabled accepted values."""
    monkeypatch.setenv("ACCOUNT_KEY_PREFIX", "u")
    state = {"user_id": "user-1"}
    for truthy in ("on", "true", "1", "yes"):
        monkeypatch.setenv("SAAS_MODE", truthy)
        assert tenant_keys.key(state, "session") == "u:user-1:session", f"SAAS_MODE={truthy!r} should be truthy"
    for falsy in ("off", "false", "0", "no", ""):
        monkeypatch.setenv("SAAS_MODE", falsy)
        assert tenant_keys.key(state, "session") == "session", f"SAAS_MODE={falsy!r} should be falsy"
