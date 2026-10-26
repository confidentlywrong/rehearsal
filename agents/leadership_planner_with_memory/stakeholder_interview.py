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

"""Stakeholder interview tool (Artifact 4, MiroFish-style).

This is the **interview layer** of the leadership_planner_with_memory
workflow — the analogue of MiroFish's InterviewAgent that lets the user
"talk to" a single OASIS agent after a simulation run. Condensed into
one validated payload so the agent supplies a deep-dive Q&A with a
single stakeholder grounded in the prior artifacts.

Design choices (locked 2026-06-28 by the operator):

* **Same "agent IS the interviewer" pattern.** No nested LLM call
  inside the tool. The agent reads `state["stakeholder_personas"]` +
  `state["simulation_timeline"]` + `state["simulation_report"]` plus
  the user's plan, picks the most informative stakeholder to
  interview, and supplies the structured Q&A as one JSON payload. The
  tool only validates + stashes.
* **One persona at a time.** Unlike the timeline, interviews are
  focused — the agent should call this tool once per stakeholder it
  wants to surface, not bundle them all. Order of calls = render
  order in the UI.
* **No persistence yet.** Interview lives in `tool_context.state`.
* **Invented data is allowed here** — simulation layer. Default
  `inferred: true` and the UI tags the card as a synthetic interview.
* **Stakeholder id must match personas when known.** When personas
  are in state, `stakeholder_id` MUST appear in the persona set;
  unknown ids are rejected. Permissive fallback when personas are
  absent.

Downstream: nothing yet — this is the demo's final artifact.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext



logger = logging.getLogger(__name__)

# Allowed interview tones.
ALLOWED_TONES: frozenset[str] = frozenset({"candid", "guarded", "hostile", "enthusiastic", "reflective"})

# Allowed final stances — MUST stay aligned with the other Step-0.x tools.
ALLOWED_STANCES: frozenset[str] = frozenset({"supportive", "neutral", "resistant"})

# Sanity ceilings.
MIN_EXCHANGES = 1
MAX_EXCHANGES = 8
MAX_KEY_QUOTES = 3

MAX_TOPIC_CHARS = 200
MAX_QUESTION_CHARS = 300
MAX_ANSWER_CHARS = 500
MAX_FOLLOW_UP_CHARS = 200
MAX_KEY_QUOTE_CHARS = 200
MAX_SUMMARY_CHARS = 500
MAX_RATIONALE_CHARS = 500

# Session-state keys.
STATE_KEY = "stakeholder_interview"
PERSONAS_STATE_KEY = "stakeholder_personas"


def _err(message: str, *, field: str = "interview_json") -> dict[str, Any]:
    """Build a structured error response."""
    return {
        "status": "error",
        "field": field,
        "message": message,
    }


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a structured success response."""
    return {
        "status": "success",
        "stakeholder_interview": payload,
    }


def _validate_short_string(label: str, value: Any, max_chars: int) -> tuple[str | None, str | None]:
    """Validate a non-empty short string. Returns (cleaned, error)."""
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be a non-empty string"
    s = value.strip()
    if len(s) > max_chars:
        return None, f"{label} length {len(s)} > {max_chars} chars"
    return s, None


def _validate_exchange(idx: int, raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one Q&A exchange entry."""
    if not isinstance(raw, dict):
        return None, f"exchanges[{idx}] must be an object"

    question, err = _validate_short_string(f"exchanges[{idx}].question", raw.get("question"), MAX_QUESTION_CHARS)
    if err:
        return None, err

    answer, err = _validate_short_string(f"exchanges[{idx}].answer", raw.get("answer"), MAX_ANSWER_CHARS)
    if err:
        return None, err

    follow_up_raw = raw.get("follow_up")
    follow_up: str | None = None
    if follow_up_raw is not None:
        # Tolerate empty/whitespace-only follow_up by dropping it; reject
        # non-string values outright.
        if not isinstance(follow_up_raw, str):
            return None, (f"exchanges[{idx}].follow_up must be a string when present")
        if follow_up_raw.strip():
            if len(follow_up_raw.strip()) > MAX_FOLLOW_UP_CHARS:
                return None, (
                    f"exchanges[{idx}].follow_up length {len(follow_up_raw.strip())} > {MAX_FOLLOW_UP_CHARS} chars"
                )
            follow_up = follow_up_raw.strip()

    out: dict[str, Any] = {"question": question, "answer": answer}
    if follow_up:
        out["follow_up"] = follow_up
    return out, None


async def emit_stakeholder_interview(
    interview_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate a stakeholder interview and stash it in session state.

    This is **Step 1.0** of the workflow — the final artifact of the
    simulation chain. The agent picks one stakeholder from the
    persona set, grounds itself in the timeline + report, and supplies
    a 1–8 turn Q&A exchange capturing how that stakeholder would
    answer the questions the user (or the agent itself) would ask
    them after the simulation.

    Args:
        interview_json: A JSON string with shape
            ``{"stakeholder_id": "...",
              "topic": "≤200 chars",
              "tone": "candid|guarded|hostile|enthusiastic|reflective",
              "final_stance": "supportive|neutral|resistant",
              "exchanges": [
                {"question": "≤300", "answer": "≤500",
                 "follow_up"?: "≤200"},
                ...1 to 8 entries...
              ],
              "key_quotes"?: ["≤200", ...up to 3...],
              "summary"?: "≤500",
              "rationale"?: "≤500",
              "inferred"?: true}``.
            Interviews are invented synthetic dialogue belonging to the
            simulation layer.
        tool_context: ADK tool context. The validated payload is stored
            at ``tool_context.state["stakeholder_interview"]``.

    Returns:
        On success: ``{"status": "success",
            "stakeholder_interview": {...}}``.
        On failure: ``{"status": "error", "field": "...",
            "message": "..."}``.
    """
    # 1. Parse the JSON envelope (lenient: accept a dict directly).
    if isinstance(interview_json, dict):
        data = interview_json
    else:
        try:
            data = json.loads(interview_json)
        except (TypeError, ValueError) as exc:
            return _err(f"invalid JSON: {exc}")
    if not isinstance(data, dict):
        return _err("payload must be a JSON object")

    # 2. Resolve known persona ids (permissive when absent).
    known_ids: set[str] = set()
    try:
        personas_payload = tool_context.state.get(PERSONAS_STATE_KEY)
    except (AttributeError, TypeError):
        personas_payload = None
    if isinstance(personas_payload, dict):
        personas_list = personas_payload.get("personas")
        if isinstance(personas_list, list):
            for p in personas_list:
                if isinstance(p, dict):
                    pid = p.get("stakeholder_id")
                    if isinstance(pid, str) and pid.strip():
                        known_ids.add(pid.strip())

    # 3. Required top-level fields.
    sid_raw = data.get("stakeholder_id")
    if not isinstance(sid_raw, str) or not sid_raw.strip():
        return _err(
            "stakeholder_id must be a non-empty string",
            field="stakeholder_id",
        )
    sid = sid_raw.strip()
    if known_ids and sid not in known_ids:
        return _err(
            f"stakeholder_id={sid!r} does not match any persona in state['{PERSONAS_STATE_KEY}']",
            field="stakeholder_id",
        )

    topic, err = _validate_short_string("topic", data.get("topic"), MAX_TOPIC_CHARS)
    if err:
        return _err(err, field="topic")

    tone = data.get("tone")
    if tone not in ALLOWED_TONES:
        return _err(
            f"tone={tone!r} not in {sorted(ALLOWED_TONES)}",
            field="tone",
        )

    final_stance = data.get("final_stance")
    if final_stance not in ALLOWED_STANCES:
        return _err(
            f"final_stance={final_stance!r} not in {sorted(ALLOWED_STANCES)}",
            field="final_stance",
        )

    # 4. Exchanges (1-8 required).
    exchanges_raw = data.get("exchanges")
    if not isinstance(exchanges_raw, list):
        return _err("exchanges must be an array", field="exchanges")
    if len(exchanges_raw) < MIN_EXCHANGES or len(exchanges_raw) > MAX_EXCHANGES:
        return _err(
            f"exchanges count {len(exchanges_raw)} outside [{MIN_EXCHANGES}, {MAX_EXCHANGES}]",
            field="exchanges",
        )
    exchanges: list[dict[str, Any]] = []
    for i, raw in enumerate(exchanges_raw):
        normalized, err = _validate_exchange(i, raw)
        if err:
            return _err(err, field="exchanges")
        assert normalized is not None
        exchanges.append(normalized)

    # 5. Optional key quotes.
    quotes_raw = data.get("key_quotes")
    quotes: list[str] = []
    if quotes_raw is not None:
        if not isinstance(quotes_raw, list):
            return _err("key_quotes must be an array", field="key_quotes")
        if len(quotes_raw) > MAX_KEY_QUOTES:
            return _err(
                f"too many key_quotes ({len(quotes_raw)} > {MAX_KEY_QUOTES})",
                field="key_quotes",
            )
        for i, q in enumerate(quotes_raw):
            if not isinstance(q, str) or not q.strip():
                return _err(
                    f"key_quotes[{i}] must be a non-empty string",
                    field="key_quotes",
                )
            if len(q.strip()) > MAX_KEY_QUOTE_CHARS:
                return _err(
                    f"key_quotes[{i}] length {len(q.strip())} > {MAX_KEY_QUOTE_CHARS} chars",
                    field="key_quotes",
                )
            quotes.append(q.strip())

    # 6. Optional summary + rationale + inferred.
    summary_raw = data.get("summary")
    summary_clean: str | None = None
    if summary_raw is not None:
        if not isinstance(summary_raw, str):
            return _err("summary must be a string when present", field="summary")
        if summary_raw.strip():
            if len(summary_raw.strip()) > MAX_SUMMARY_CHARS:
                return _err(
                    f"summary length {len(summary_raw.strip())} > {MAX_SUMMARY_CHARS} chars",
                    field="summary",
                )
            summary_clean = summary_raw.strip()

    rationale_raw = data.get("rationale")
    rationale_clean: str | None = None
    if isinstance(rationale_raw, str) and rationale_raw.strip():
        rationale_clean = rationale_raw.strip()[:MAX_RATIONALE_CHARS]


    inferred = data.get("inferred", True)
    if not isinstance(inferred, bool):
        return _err("inferred must be a boolean", field="inferred")

    payload: dict[str, Any] = {
        "stakeholder_id": sid,
        "topic": topic,
        "tone": tone,
        "final_stance": final_stance,
        "exchanges": exchanges,
        "inferred": inferred,
    }
    if quotes:
        payload["key_quotes"] = quotes
    if summary_clean:
        payload["summary"] = summary_clean
    if rationale_clean:
        payload["rationale"] = rationale_clean
    if "citations" in data:
        payload["citations"] = data["citations"]

    # 7. Stash for the UI to render.
    try:
        tool_context.state[STATE_KEY] = payload
        interviews = tool_context.state.get("stakeholder_interviews", []) or []
        interviews.append(payload)
        tool_context.state["stakeholder_interviews"] = interviews
    except (AttributeError, TypeError):
        logger.warning(
            "emit_stakeholder_interview: could not write %s to session state",
            STATE_KEY,
        )

    return _ok(payload)
