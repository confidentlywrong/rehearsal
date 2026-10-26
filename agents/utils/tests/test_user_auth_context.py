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

"""Unit tests for the session-scoped user auth token store
(Deferral A — per-user memory-gateway auth passthrough)."""

from __future__ import annotations

import pytest

from agents.utils import user_auth_context


@pytest.fixture(autouse=True)
def _reset_store():
    user_auth_context._reset_for_tests()
    yield
    user_auth_context._reset_for_tests()


class TestSetAndGet:
    def test_roundtrip(self) -> None:
        user_auth_context.set_session_token("s1", "tok-a")
        assert user_auth_context.get_session_token("s1") == "tok-a"

    def test_missing_returns_none(self) -> None:
        assert user_auth_context.get_session_token("nope") is None

    def test_overwrite_replaces(self) -> None:
        user_auth_context.set_session_token("s1", "first")
        user_auth_context.set_session_token("s1", "second")
        assert user_auth_context.get_session_token("s1") == "second"

    def test_two_sessions_isolated(self) -> None:
        user_auth_context.set_session_token("s1", "alice")
        user_auth_context.set_session_token("s2", "bob")
        assert user_auth_context.get_session_token("s1") == "alice"
        assert user_auth_context.get_session_token("s2") == "bob"


class TestEmptyAndNoneHandling:
    def test_empty_token_is_noop(self) -> None:
        user_auth_context.set_session_token("s1", "")
        assert user_auth_context.get_session_token("s1") is None

    def test_none_token_is_noop(self) -> None:
        user_auth_context.set_session_token("s1", None)
        assert user_auth_context.get_session_token("s1") is None

    def test_whitespace_token_is_noop(self) -> None:
        """Prevents a malformed spawn payload from parking a
        blank-string token that shadows the env-var fallback."""
        user_auth_context.set_session_token("s1", "   \t\n  ")
        assert user_auth_context.get_session_token("s1") is None

    def test_empty_session_id_is_noop(self) -> None:
        user_auth_context.set_session_token("", "tok")
        assert user_auth_context.get_session_token("") is None

    def test_get_with_empty_session_id(self) -> None:
        assert user_auth_context.get_session_token("") is None


class TestClear:
    def test_clear_removes_token(self) -> None:
        user_auth_context.set_session_token("s1", "tok")
        user_auth_context.clear_session_token("s1")
        assert user_auth_context.get_session_token("s1") is None

    def test_clear_missing_is_idempotent(self) -> None:
        # Must not raise.
        user_auth_context.clear_session_token("never-set")
        user_auth_context.clear_session_token("never-set")

    def test_clear_empty_session_id_is_noop(self) -> None:
        user_auth_context.clear_session_token("")

    def test_clear_one_does_not_touch_others(self) -> None:
        user_auth_context.set_session_token("s1", "alice")
        user_auth_context.set_session_token("s2", "bob")
        user_auth_context.clear_session_token("s1")
        assert user_auth_context.get_session_token("s1") is None
        assert user_auth_context.get_session_token("s2") == "bob"


class TestCapEviction:
    def test_eviction_when_over_cap(self, monkeypatch) -> None:
        """When the map is over ``_MAX_ENTRIES``, the OLDEST entry is
        evicted so long-running processes cannot leak unbounded
        memory even if ``clear_session_token`` is skipped somewhere."""
        # Shrink the cap for a fast test.
        monkeypatch.setattr(user_auth_context, "_MAX_ENTRIES", 3)
        user_auth_context.set_session_token("s1", "t1")
        user_auth_context.set_session_token("s2", "t2")
        user_auth_context.set_session_token("s3", "t3")
        user_auth_context.set_session_token("s4", "t4")

        # s1 is the oldest and should have been evicted.
        assert user_auth_context.get_session_token("s1") is None
        assert user_auth_context.get_session_token("s2") == "t2"
        assert user_auth_context.get_session_token("s3") == "t3"
        assert user_auth_context.get_session_token("s4") == "t4"

    def test_overwrite_bumps_recency(self, monkeypatch) -> None:
        """Overwriting a key moves it to the end, so it survives the
        next eviction. Documents the LRU-ish semantics."""
        monkeypatch.setattr(user_auth_context, "_MAX_ENTRIES", 3)
        user_auth_context.set_session_token("s1", "t1")
        user_auth_context.set_session_token("s2", "t2")
        user_auth_context.set_session_token("s3", "t3")
        # Refresh s1 → it becomes the most recent.
        user_auth_context.set_session_token("s1", "t1-refreshed")
        # Adding s4 should evict s2 (now the oldest), not s1.
        user_auth_context.set_session_token("s4", "t4")

        assert user_auth_context.get_session_token("s1") == "t1-refreshed"
        assert user_auth_context.get_session_token("s2") is None
        assert user_auth_context.get_session_token("s3") == "t3"
        assert user_auth_context.get_session_token("s4") == "t4"
