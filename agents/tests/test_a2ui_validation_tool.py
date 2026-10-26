# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
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

"""Tests for the A2UI v0.8.0 validation tool.

Validates that validate_and_emit_a2ui correctly accepts valid A2UI payloads
and rejects invalid ones with detailed violation reports.
"""

import importlib.util
import json
import pathlib
import re
from unittest.mock import MagicMock

import pytest

# Load the tool module from hyphenated skill directory
_tools_path = pathlib.Path(__file__).parent.parent / "skills" / "a2ui-rendering" / "tools.py"
_spec = importlib.util.spec_from_file_location("a2ui_rendering_tools", _tools_path)
assert _spec is not None, f"Could not find module spec for {_tools_path}"
assert _spec.loader is not None, f"Module spec has no loader for {_tools_path}"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
validate_and_emit_a2ui = _mod.validate_and_emit_a2ui
VALID_TYPES = _mod.VALID_TYPES


@pytest.fixture
def tool_context():
    """Pre-built ToolContext mock."""
    return MagicMock()


@pytest.mark.asyncio
async def test_valid_single_text_component(tool_context):
    """Valid Text component passes validation."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-1",
                        "component": {
                            "Text": {
                                "text": {"literalString": "Hello, World!"},
                            },
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success"
    assert "a2ui" in result


@pytest.mark.asyncio
async def test_valid_card_with_column(tool_context):
    """Card > Column > 2 Texts passes validation."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-1",
                        "component": {
                            "Text": {"text": {"literalString": "Line 1"}},
                        },
                    },
                    {
                        "id": "text-2",
                        "component": {
                            "Text": {"text": {"literalString": "Line 2"}},
                        },
                    },
                    {
                        "id": "col-1",
                        "component": {
                            "Column": {
                                "children": {"explicitList": ["text-1", "text-2"]},
                            },
                        },
                    },
                    {
                        "id": "card-1",
                        "component": {
                            "Card": {"child": "col-1"},
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success"
    assert "a2ui" in result


@pytest.mark.asyncio
async def test_invalid_component_type(tool_context):
    """Unknown component type 'FancyWidget' fails with violation."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "fw-1",
                        "component": {
                            "FancyWidget": {"text": {"literalString": "Nope"}},
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error"
    assert any("FancyWidget" in v["message"] for v in result["violations"])


@pytest.mark.asyncio
async def test_duplicate_component_ids(tool_context):
    """Duplicate component IDs fail with 'duplicate' in message."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "dup",
                        "component": {
                            "Text": {"text": {"literalString": "First"}},
                        },
                    },
                    {
                        "id": "dup",
                        "component": {
                            "Text": {"text": {"literalString": "Second"}},
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error"
    assert any("duplicate" in v["message"].lower() for v in result["violations"])


@pytest.mark.asyncio
async def test_dangling_child_reference(tool_context):
    """Card with child referencing 'nonexistent' ID fails."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "card-1",
                        "component": {
                            "Card": {"child": "nonexistent"},
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error"
    assert any("nonexistent" in v["message"] for v in result["violations"])


@pytest.mark.asyncio
async def test_raw_string_instead_of_literal_wrapper(tool_context):
    """Text with raw string 'text' value fails with wrapper hint."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-1",
                        "component": {
                            "Text": {"text": "raw string"},
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error"
    violations_text = " ".join(v["message"] for v in result["violations"])
    assert "literalString" in violations_text or "typed wrapper" in violations_text


@pytest.mark.asyncio
async def test_invalid_json_input(tool_context):
    """Non-JSON input fails gracefully."""
    result = await validate_and_emit_a2ui(payload="not json at all", tool_context=tool_context)
    assert result["status"] == "error"
    assert "violations" in result


@pytest.mark.asyncio
async def test_begin_rendering_valid(tool_context):
    """Valid beginRendering message passes."""
    payload = json.dumps(
        {
            "beginRendering": {"root": "card-1"},
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success"
    assert "a2ui" in result


@pytest.mark.asyncio
async def test_missing_surface_update_structure(tool_context):
    """Payload with no recognized message type fails."""
    payload = json.dumps({"random": "data"})
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_children_without_explicit_list(tool_context):
    """Column with raw array children fails with 'explicitList' hint."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "t1",
                        "component": {
                            "Text": {"text": {"literalString": "Hi"}},
                        },
                    },
                    {
                        "id": "col-1",
                        "component": {
                            "Column": {"children": ["t1"]},
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error"
    assert any("explicitList" in v["message"] for v in result["violations"])


@pytest.mark.asyncio
async def test_concatenated_json_objects_detected(tool_context):
    """Two JSON objects concatenated without separator are detected with actionable message."""
    obj1 = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-1",
                        "component": {"Text": {"text": {"literalString": "Card 1"}}},
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    obj2 = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-2",
                        "component": {"Text": {"text": {"literalString": "Card 2"}}},
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    payload = obj1 + obj2  # concatenated -- the exact bug
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error"
    assert any("single" in v["message"].lower() or "combine" in v["message"].lower() for v in result["violations"]), (
        f"Expected actionable 'combine into single' guidance, got: {result['violations']}"
    )


@pytest.mark.asyncio
async def test_a2ui_wrapper_stripped_gracefully(tool_context):
    """Payload wrapped in {"a2ui": {...}} is unwrapped and validated successfully."""
    payload = json.dumps(
        {
            "a2ui": {
                "surfaceUpdate": {
                    "components": [
                        {
                            "id": "text-1",
                            "component": {"Text": {"text": {"literalString": "Hello"}}},
                        },
                    ],
                },
                "surfaceId": "test",
            },
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success"
    assert "a2ui" in result


@pytest.mark.asyncio
async def test_concatenated_json_with_newline_detected(tool_context):
    """JSONL-style concatenation (newline-separated) is also detected."""
    obj1 = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-1",
                        "component": {"Text": {"text": {"literalString": "Card 1"}}},
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    obj2 = json.dumps(
        {
            "beginRendering": {"root": "text-1"},
            "surfaceId": "test",
        }
    )
    payload = obj1 + "\n" + obj2
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error"
    assert any("single" in v["message"].lower() or "one call" in v["message"].lower() for v in result["violations"])


@pytest.mark.asyncio
async def test_trailing_extra_brace_accepted_with_warning(tool_context):
    """A valid surfaceUpdate followed by a stray closing brace is accepted.

    Captures the planner_with_eval failure observed during PR #312 QA
    (session 97f6768d, /tmp/a2ui_rejected_0fed72dd54c0.json). The model
    emitted a syntactically valid surfaceUpdate ending in `]}}` plus one
    extra `}`. The validator's old behaviour rejected with "Multiple JSON
    objects detected"; the new behaviour drops the garbage and succeeds.
    """
    valid = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-1",
                        "component": {"Text": {"text": {"literalString": "Hello"}}},
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    payload = valid + "}"
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success", f"Expected lenient acceptance of trailing extra brace, got: {result}"
    assert "a2ui" in result


@pytest.mark.asyncio
async def test_trailing_garbage_text_accepted_with_warning(tool_context):
    """Non-JSON trailing text (e.g. accidental commentary) is dropped, payload accepted."""
    valid = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-1",
                        "component": {"Text": {"text": {"literalString": "Hi"}}},
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    payload = valid + " // accidental trailing note"
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success", f"Expected lenient acceptance of trailing non-JSON text, got: {result}"


@pytest.mark.asyncio
async def test_trailing_partial_object_accepted_with_warning(tool_context):
    """A partial second object that won't parse is treated as garbage, payload accepted."""
    valid = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "text-1",
                        "component": {"Text": {"text": {"literalString": "Hi"}}},
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    payload = valid + '{"foo"'  # opens object, no value, no close — JSONDecodeError
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success", f"Expected lenient acceptance of unparseable trailing fragment, got: {result}"


# ---------------------------------------------------------------------------
# Registry-drift regression test
# ---------------------------------------------------------------------------
#
# The agent-side `VALID_TYPES` set (this file's `_mod.VALID_TYPES`) and the
# frontend-side `A2UI_COMPONENT_TYPES` array
# (web/frontend/src/app/components/a2ui/a2ui-tree.service.ts) MUST agree.
#
# When they drift, the agent happily emits a component the frontend's
# `getType()` switch falls through on, rendering a literal "Unknown component
# type" placeholder. This bit us when `ScoreBar` was added agent-side without
# updating the frontend registry. This test scans the TS file and asserts
# both registries hold the same set of type names.


def test_frontend_registry_matches_agent_valid_types():
    """A2UI_COMPONENT_TYPES (frontend) must match VALID_TYPES (agent).

    Parses the Angular service file with a regex (no node_modules / tsc
    needed in the Python test env) and compares the extracted set against
    the Python VALID_TYPES set.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    ts_path = repo_root / "web" / "frontend" / "src" / "app" / "components" / "a2ui" / "a2ui-tree.service.ts"
    assert ts_path.exists(), f"Frontend registry not found at {ts_path}"

    src = ts_path.read_text(encoding="utf-8")
    match = re.search(
        r"A2UI_COMPONENT_TYPES\s*=\s*\[(?P<body>[^\]]*)\]\s*as\s+const",
        src,
        re.DOTALL,
    )
    assert match, (
        f"Could not locate `A2UI_COMPONENT_TYPES = [...] as const` in {ts_path}. "
        "If the declaration changed shape, update this regex too."
    )
    frontend_types = set(re.findall(r"'([^']+)'", match.group("body")))

    missing_in_frontend = VALID_TYPES - frontend_types
    extra_in_frontend = frontend_types - VALID_TYPES

    assert not missing_in_frontend and not extra_in_frontend, (
        "A2UI registry drift detected — agent VALID_TYPES and frontend "
        "A2UI_COMPONENT_TYPES are out of sync.\n"
        f"  Missing from frontend (add to a2ui-tree.service.ts): {sorted(missing_in_frontend)}\n"
        f"  Extra in frontend (remove from a2ui-tree.service.ts or add to agent VALID_TYPES): {sorted(extra_in_frontend)}"
    )


# ---------------------------------------------------------------------------
# Numeric wrapper enforcement regression tests
# ---------------------------------------------------------------------------
#
# ScoreBar / Slider `value` (and minValue / maxValue) must be wrapped as
# {"literalNumber": N} or {"path": "..."}. Raw integers used to slip
# through because the validator only checked STRING_PROPS / BOOLEAN_PROPS.
# These tests pin the new NUMERIC_PROPS_BY_TYPE enforcement so the gap
# can't reopen silently.


@pytest.mark.asyncio
async def test_scorebar_with_raw_numeric_value_rejected(tool_context):
    """Raw `{"value": 50}` on a ScoreBar must be rejected with a typed-wrapper violation."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "score-1",
                        "component": {
                            "ScoreBar": {
                                "label": {"literalString": "Alignment"},
                                "value": 50,  # raw integer — must be rejected
                            },
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "error", f"Raw numeric value on ScoreBar must trigger a validation error; got {result}"
    msgs = [v["message"] for v in result["violations"]]
    assert any("literalNumber" in m for m in msgs), (
        f"Expected a 'literalNumber' wrapper violation; got messages: {msgs}"
    )


@pytest.mark.asyncio
async def test_scorebar_with_wrapped_numeric_value_accepted(tool_context):
    """Properly-wrapped `{"value": {"literalNumber": 50}}` on a ScoreBar must pass."""
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "score-1",
                        "component": {
                            "ScoreBar": {
                                "label": {"literalString": "Alignment"},
                                "value": {"literalNumber": 50},
                            },
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success", f"Wrapped numeric ScoreBar must pass; got {result}"


@pytest.mark.asyncio
async def test_checkbox_value_still_unrestricted(tool_context):
    """CheckBox `value` overload (boolean, not numeric) must not be caught by the new NUMERIC check.

    The NUMERIC_PROPS_BY_TYPE map is scoped per-component-type precisely so
    CheckBox / TextField / other primitives whose `value` is non-numeric
    aren't accidentally rejected.
    """
    payload = json.dumps(
        {
            "surfaceUpdate": {
                "components": [
                    {
                        "id": "cb-1",
                        "component": {
                            "CheckBox": {
                                "label": {"literalString": "Agree"},
                                "value": {"literalBoolean": True},
                            },
                        },
                    },
                ],
            },
            "surfaceId": "test",
        }
    )
    result = await validate_and_emit_a2ui(payload=payload, tool_context=tool_context)
    assert result["status"] == "success", f"CheckBox boolean value must pass; got {result}"
