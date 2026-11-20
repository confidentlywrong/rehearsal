# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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

"""Tests for the shared OIDC helper used by AE / Cloud Run callers."""

from __future__ import annotations

from unittest.mock import patch

from agents.utils import auth


def test_resolve_audience_prefers_iap_client_id(monkeypatch):
    monkeypatch.setenv("IAP_CLIENT_ID", "iap-aud-12345")
    assert auth.resolve_audience("https://gateway.run.app") == "iap-aud-12345"


def test_resolve_audience_falls_back_to_url(monkeypatch):
    monkeypatch.delenv("IAP_CLIENT_ID", raising=False)
    assert auth.resolve_audience("https://gateway.run.app") == "https://gateway.run.app"


def test_resolve_audience_returns_empty_when_both_unset(monkeypatch):
    monkeypatch.delenv("IAP_CLIENT_ID", raising=False)
    assert auth.resolve_audience("") == ""


def test_get_id_token_returns_none_when_audience_empty():
    """Empty audience means 'don't attach a token'; helper short-circuits."""
    assert auth.get_id_token("") is None


def test_get_id_token_fetches_via_google_oauth2(monkeypatch):
    """When audience is non-empty, helper delegates to google.oauth2.id_token."""
    fake_token = "eyJfaketoken"
    with patch("agents.utils.auth.id_token.fetch_id_token", return_value=fake_token) as mock_fetch:
        token = auth.get_id_token("https://gateway.run.app")

    assert token == fake_token
    # Called with (request, audience) -- request comes from the cached factory
    assert mock_fetch.call_count == 1
    call_args = mock_fetch.call_args
    assert call_args.args[1] == "https://gateway.run.app"


def test_get_id_token_returns_none_on_failure(monkeypatch, caplog):
    """If google.oauth2 raises (no ADC, network error, etc.), helper logs warning and returns None."""
    import logging

    with (
        patch(
            "agents.utils.auth.id_token.fetch_id_token",
            side_effect=RuntimeError("no ADC"),
        ),
        caplog.at_level(logging.WARNING, logger="agents.utils.auth"),
    ):
        assert auth.get_id_token("https://gateway.run.app") is None

    assert any("failed to fetch ID token" in r.message for r in caplog.records)


def test_is_remote_https_url_rejects_non_https_loopback_and_control_chars():
    """Verify _is_remote_https_url accepts valid https hosts and rejects unsafe/local inputs."""
    assert auth._is_remote_https_url("https://gateway.run.app") is True
    assert auth._is_remote_https_url("http://gateway.run.app") is False
    assert auth._is_remote_https_url("https://localhost:9101") is False
    assert auth._is_remote_https_url("https://sub.localhost") is False
    assert auth._is_remote_https_url("https://127.0.0.1:9101") is False
    assert auth._is_remote_https_url("https://[::1]:9101") is False
    assert auth._is_remote_https_url("https://gateway.run.app\nX-Evil: 1") is False
    assert auth._is_remote_https_url("") is False
