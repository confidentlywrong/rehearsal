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

"""Shared JSON/payload validation helpers for MiroFish simulation tools."""

from __future__ import annotations

import json
import math
from typing import Any

MAX_RATIONALE_CHARS = 500
MAX_CITATIONS = 50
MAX_CITATION_STR_CHARS = 2000
_MAX_JSON_DEPTH = 12


def _reject_non_finite_constant(constant: str) -> Any:
    """Reject non-standard JSON constants (NaN, Infinity, -Infinity) at parse time."""
    raise ValueError(f"non-finite JSON constant {constant!r} is not allowed")


def parse_json_envelope(raw_payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a JSON string or dict envelope, rejecting non-finite constants."""
    if isinstance(raw_payload, dict):
        data: Any = raw_payload
    else:
        try:
            data = json.loads(raw_payload, parse_constant=_reject_non_finite_constant)
        except (TypeError, ValueError) as exc:
            return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "payload must be a JSON object"
    return data, None


def sanitize_json_value(value: Any, *, path: str = "value", depth: int = 0) -> tuple[Any, str | None]:
    """Validate and copy a JSON-safe structure, rejecting non-finite floats.

    Allows ``None``, ``bool``, ``int``, finite ``float``, ``str``, ``list``,
    and ``dict`` with string keys. Rejects ``NaN`` / ``Inf`` / ``-Inf`` and
    non-JSON Python objects so payloads serialize cleanly to PostgreSQL JSONB.
    """
    if depth > _MAX_JSON_DEPTH:
        return None, f"{path} exceeds maximum nesting depth ({_MAX_JSON_DEPTH})"
    if value is None or isinstance(value, (bool, int, str)):
        return value, None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None, f"{path} contains non-finite float {value!r}"
        return value, None
    if isinstance(value, list):
        out_list: list[Any] = []
        for idx, item in enumerate(value):
            clean_item, err = sanitize_json_value(item, path=f"{path}[{idx}]", depth=depth + 1)
            if err:
                return None, err
            out_list.append(clean_item)
        return out_list, None
    if isinstance(value, dict):
        out_dict: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str) or not k.strip():
                return None, f"{path} contains an empty or non-string key"
            k_clean = k.strip()
            if k_clean in out_dict:
                return None, f"{path} contains duplicate key {k_clean!r} after stripping"
            clean_val, err = sanitize_json_value(v, path=f"{path}.{k_clean}", depth=depth + 1)
            if err:
                return None, err
            out_dict[k_clean] = clean_val
        return out_dict, None
    return None, f"{path} has unsupported type {type(value).__name__}"


def validate_optional_rationale(raw: Any, *, max_chars: int = MAX_RATIONALE_CHARS) -> tuple[str | None, str | None]:
    """Validate an optional ``rationale`` string field.

    ``None`` or whitespace-only strings omit the field; non-string values fail.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "rationale must be a string when present"
    cleaned = raw.strip()
    if not cleaned:
        return None, None
    return cleaned[:max_chars], None


def validate_citations(
    raw: Any,
    *,
    max_items: int = MAX_CITATIONS,
    max_str_chars: int = MAX_CITATION_STR_CHARS,
) -> tuple[list[Any] | None, str | None]:
    """Validate optional ``citations`` as a list of non-empty strings or dicts.

    Returns ``(None, None)`` when ``raw`` is ``None`` so callers omit the key
    rather than leaking ``None`` into session state or JSONB.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        return None, "citations must be a list"
    if len(raw) > max_items:
        return None, f"too many citations ({len(raw)} > {max_items})"

    cleaned: list[Any] = []
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            s = item.strip()
            if not s:
                return None, f"citations[{idx}] string must not be empty"
            if len(s) > max_str_chars:
                return None, f"citations[{idx}] length {len(s)} > {max_str_chars} chars"
            cleaned.append(s)
        elif isinstance(item, dict):
            if not item:
                return None, f"citations[{idx}] object must not be empty"
            clean_obj, err = sanitize_json_value(item, path=f"citations[{idx}]")
            if err:
                return None, err
            for req_if_present in ("quote", "source_id", "source_kind"):
                if req_if_present in clean_obj:
                    val = clean_obj[req_if_present]
                    if not isinstance(val, str) or not val.strip():
                        return None, f"citations[{idx}].{req_if_present} must be a non-empty string"
                    s = val.strip()
                    if req_if_present == "quote" and len(s) > max_str_chars:
                        return None, f"citations[{idx}].quote length {len(s)} > {max_str_chars} chars"
                    clean_obj[req_if_present] = s
            cleaned.append(clean_obj)
        else:
            return None, f"citations[{idx}] must be a non-empty string or object"

    return cleaned, None
