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

"""Shared OIDC helper for AE / Cloud Run service-to-service calls.

Two auth modes:
  - IAP (dev/prod): audience = IAP_CLIENT_ID.
  - Cloud Run IAM (OSS): audience = target service's .run.app URL.

Python mirror of internal/auth/idtoken.go.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import google.auth.transport.requests
import requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

# Bound outbound metadata/OIDC HTTP fetches so unreachable 169.254.169.254 never
# stalls a call for google-auth's 120s default timeout.
_DEFAULT_METADATA_REQUEST_TIMEOUT_SECS = 5.0


def _clamp_timeout(timeout: Any, cap: float = _DEFAULT_METADATA_REQUEST_TIMEOUT_SECS) -> Any:
    """Cap scalar or (connect, read) timeouts at ``cap`` seconds."""
    if timeout is None:
        return cap
    if isinstance(timeout, bool):
        return cap
    if isinstance(timeout, (int, float)):
        try:
            v = float(timeout)
        except (TypeError, ValueError, OverflowError):
            return cap
        return cap if (not math.isfinite(v) or v <= 0 or v > cap) else timeout
    if isinstance(timeout, tuple):
        return tuple(_clamp_timeout(part, cap) for part in timeout)
    return timeout


@lru_cache(maxsize=1)
def _request() -> google.auth.transport.requests.Request:
    """Lazy-build the urllib3-backed transport (cached). Lazy so importing
    this module has no side effects -- helps tests that don't want a
    connection pool spinning up at collection time.
    """
    req = google.auth.transport.requests.Request()
    session = getattr(req, "session", None)
    if isinstance(session, requests.Session):
        orig_request = session.request

        def _bounded_request(*args: Any, **kwargs: Any) -> Any:
            kwargs["timeout"] = _clamp_timeout(kwargs.get("timeout"))
            return orig_request(*args, **kwargs)

        setattr(session, "request", _bounded_request)
    return req


def _is_remote_https_url(raw_url: str) -> bool:
    """True if ``raw_url`` is a syntactically valid non-loopback https:// URL.

    Mirrors internal/auth/idtoken.go ``IsCloudRunURL``: prevents sending OIDC
    tokens over cleartext http:// or stalling on GCE metadata probes during local
    dev against http://localhost / 127.0.0.1 / ::1.
    """
    if not raw_url or any(ord(ch) <= 0x20 or ord(ch) >= 0x7F for ch in raw_url):
        return False
    try:
        parsed = urlsplit(raw_url)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = parsed.hostname
    if not host:
        return False
    host_norm = host.rstrip(".").split("%", 1)[0].lower()
    if not host_norm or host_norm == "localhost" or host_norm.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host_norm)
    except ValueError:
        return True
    if ip.is_loopback or ip.is_unspecified:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and (mapped.is_loopback or mapped.is_unspecified):
        return False
    return True


def resolve_audience(fallback_url: str) -> str:
    """OIDC audience for a downstream service: IAP_CLIENT_ID if set, else fallback_url.

    Strips whitespace on both inputs. When IAP_CLIENT_ID is unset/blank, only
    returns ``fallback_url`` if it is a valid non-loopback ``https://`` URL;
    otherwise returns ``""`` (meaning "do not attach a token" or hit metadata).
    """
    iap_client_id = os.environ.get("IAP_CLIENT_ID", "").strip()
    if iap_client_id:
        return iap_client_id
    if not isinstance(fallback_url, str):
        return ""
    candidate = fallback_url.strip()
    if not _is_remote_https_url(candidate):
        return ""
    return candidate


def get_id_token(audience: str) -> str | None:
    """Mint a Google-signed OIDC ID token for ``audience``.

    Returns None if the audience is empty/whitespace or the underlying library
    raises (no ADC, network error, etc.). Callers treat None as "skip the
    Authorization header".
    """
    if not isinstance(audience, str):
        return None
    aud = audience.strip()
    if not aud:
        return None
    try:
        return id_token.fetch_id_token(_request(), aud)
    except Exception as exc:  # noqa: BLE001 -- any failure -> log warning, no token, caller decides
        logger.warning("OIDC: failed to fetch ID token for audience %r: %s", aud, exc)
        return None
