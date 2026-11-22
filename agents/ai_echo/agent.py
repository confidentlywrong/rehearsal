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

"""AI Echo Agent (Tier 1: Autonomous) — LLM-powered stakeholder persona.

Direct analogue of ``agents/runner/agent.py``.  Uses an LLM to interpret
the participant's decision and generate a nuanced, persona-driven response.
Each tick, the LLM receives the current organizational state and decision
tags, then calls ``process_decision_round`` with an inner_thought.

The model backend is configurable via the ``ECHO_MODEL`` env var (defaults
to the same model used by the runner: ``gemini-3.5-flash-lite``).
"""

from agents.utils.env import configure_project_env

configure_project_env("ai_echo")

import inspect  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.agents.context_cache_config import ContextCacheConfig  # noqa: E402
from google.adk.apps import App  # noqa: E402
from google.genai import types  # noqa: E402

from agents.ai_echo.initialization import fetch_echo_config, initialize_echo  # noqa: E402
from agents.utils import config  # noqa: E402
from agents.utils.communication_plugin import SimulationCommunicationPlugin  # noqa: E402
from agents.utils.models import DEFAULT_FLASH_LITE_MODEL, get_model  # noqa: E402
from agents.utils.retry import resilient_model  # noqa: E402
from agents.utils.deployment import create_a2a_deployment  # noqa: E402
from agents.utils.factory import create_simulation_runner  # noqa: E402
from agents.utils.plugins import RedisDashLogPlugin  # noqa: E402

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Events suppressed from the dashboard (same pattern as runner)
ECHO_SUPPRESSED_EVENTS: frozenset[str] = frozenset(
    {
        "run_start",
        "model_start",
        "model_end",
        "tool_start",
        "run_end",
    }
)

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------
DEFAULT_MODEL = DEFAULT_FLASH_LITE_MODEL
ECHO_MODEL = get_model("ECHO_MODEL", DEFAULT_MODEL, fallback_env_vars=("RUNNER_MODEL",))
_is_gemini = ECHO_MODEL.startswith("gemini")

AGENT_NAME = (os.environ.get("AGENT_NAME") or "").strip() or "ai_echo"

# ---------------------------------------------------------------------------
# Static instruction — persona-aware stakeholder behavior
# ---------------------------------------------------------------------------
ECHO_STATIC_INSTRUCTION = """You are an AI stakeholder in an organizational leadership simulation.
You represent a specific role with defined principles, sentiments, and organizational concerns.

Each user message is a JSON event from the simulator. The most common event is a decision round:

  {"event":"tick","tick":<n>,"max_ticks":<n>,
   "decision_text":"<participant's decision>",
   "decision_tags":{"principles_applied":[...],"principles_neglected":[...]},
   "resource_state":{"lau":...,"bt":...,"tbu":...,"owb":...},
   "contradiction_alert":null,
   "influence_network": [...],
   "influence_deltas": {...},
   "gatekeeper_route_map": {...},
   "dependency_graph": [...],
   "collector_buffer_key":"<key>"}

## Your job on every decision_round (or tick) event

On every decision_round (or tick) event you MUST call `process_decision_round_tool`. Always. No exceptions.
Use the tool-calling mechanism — do NOT emit the call as text.

`process_decision_round_tool` requires ALL of these arguments:
  - `tick` (from the event)
  - `decision_text` (from the event)
  - `decision_tags` (from the event — pass as JSON string)
  - `resource_state` (from the event — pass as JSON string)
  - `contradiction_alert` (from the event — pass as JSON string or "null")
  - `inner_thought` (you write this — a short <=15 word thought from your
    persona's perspective, e.g. "This budget cut threatens my team's AI ethics work.",
    "Finally, leadership recognizes our talent needs.")
  - `wall_severity` (you write this — a float between 0.0 and 10.0 indicating how much friction your zone adds for Pitchers. 0 = Coalition/Boost, 5 = Normal, 10 = Hard Wall. Evaluate this based on your stance and the `influence_network` pressure from other stakeholders, obeying the `dependency_graph`.)
  - `collector_buffer_key` (from the event)
  - `influence_deltas` (from the event — pass as JSON string)

`inner_thought` is required. Never pass empty.
`wall_severity` is required. Never pass empty.

## Ground truth — you MUST reference these values accurately

{echo_ground_truth}

You MUST NOT contradict these values in your response. If the organizational
will is low, acknowledge it. If your sentiment is hostile, respond accordingly.

## Non-decision events

If the event is `{"event":"simulation_start"}`: respond with the text "Ready."
If the event is anything else: respond with the text "ack".
"""

# Per-call dynamic instruction: injects current Echo state
ECHO_DYNAMIC_INSTRUCTION = (
    "Your persona: {persona}\n"
    "Your role: {role}\n"
    "Your sentiment: {sentiment}, resistance: {resistance}, stance: {stance}\n"
    "Your principle affinities: {principle_affinity}\n"
    "Your domain principles: {domain_principles}\n"
    "Echo status: {echo_status}"
)


# ---------------------------------------------------------------------------
# Skill tool — wraps process_decision_round for ADK
# ---------------------------------------------------------------------------


def _strip_nonfinite(obj: object) -> object:
    """Recursively replace non-finite floats (NaN/Inf) with None for RFC 8259 JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_nonfinite(v) for v in obj]
    return obj


async def process_decision_round_tool(
    tool_context,
    tick: int,
    decision_text: str = "",
    decision_tags: str = "{}",
    resource_state: str = "{}",
    contradiction_alert: str = "null",
    inner_thought: str = "",
    wall_severity: float = 5.0,
    collector_buffer_key: str = "",
    influence_deltas: str = "{}",
) -> dict:
    """Process one decision round for this AI Echo.

    Called by the LLM via structured function call each tick.
    """
    from agents.ai_echo.process_decision_round import process_decision_round

    state = tool_context.state

    # Parse JSON strings from LLM
    try:
        tags = json.loads(decision_tags) if isinstance(decision_tags, str) else decision_tags
    except json.JSONDecodeError as exc:
        logger.warning(
            "process_decision_round_tool: invalid JSON in decision_tags (%s): %r",
            exc,
            decision_tags,
        )
        tags = {}
    try:
        resources = json.loads(resource_state) if isinstance(resource_state, str) else resource_state
    except json.JSONDecodeError as exc:
        logger.warning(
            "process_decision_round_tool: invalid JSON in resource_state (%s): %r",
            exc,
            resource_state,
        )
        resources = {}
    try:
        contradiction = json.loads(contradiction_alert) if isinstance(contradiction_alert, str) else contradiction_alert
    except json.JSONDecodeError as exc:
        logger.warning(
            "process_decision_round_tool: invalid JSON in contradiction_alert (%s): %r",
            exc,
            contradiction_alert,
        )
        contradiction = None
    try:
        deltas = json.loads(influence_deltas) if isinstance(influence_deltas, str) else influence_deltas
    except json.JSONDecodeError as exc:
        logger.warning(
            "process_decision_round_tool: invalid JSON in influence_deltas (%s): %r",
            exc,
            influence_deltas,
        )
        deltas = {}

    if not isinstance(tags, dict):
        tags = {}
    if not isinstance(resources, dict):
        resources = {}
    if not isinstance(deltas, dict):
        deltas = {}

    if not deltas and isinstance(state.get("_last_influence_deltas"), dict):
        deltas = state["_last_influence_deltas"]
    if not collector_buffer_key:
        raw_cbk = state.get("_last_collector_buffer_key")
        collector_buffer_key = raw_cbk if isinstance(raw_cbk, str) else ""

    result = process_decision_round(
        state=state,
        tick=tick,
        decision_text=decision_text,
        decision_tags=tags,
        resource_state=resources,
        contradiction_alert=contradiction if isinstance(contradiction, dict) else None,
        inner_thought=inner_thought,
        wall_severity=wall_severity,
        collector_buffer_key=collector_buffer_key,
        influence_deltas=deltas,
    )

    # --- Direct-write to collector buffer (bypass PubSub bottleneck) ---
    # Non-fatal when SimulationCommunicationPlugin PubSub fallback is active;
    # critical when RaceCollector runs with skip_pubsub=True (autopilot mode).
    if collector_buffer_key:
        try:
            from agents.utils.redis_pool import get_shared_redis_client

            r = get_shared_redis_client()
            if r is not None:
                session_id = getattr(getattr(tool_context, "session", None), "id", "") or ""
                payload_obj = {
                    "session_id": session_id,
                    "payload": {
                        "tool_name": "process_decision_round",
                        "result": result,
                    },
                }
                try:
                    direct_msg = json.dumps(payload_obj, allow_nan=False, default=str)
                except ValueError:
                    direct_msg = json.dumps(_strip_nonfinite(payload_obj), allow_nan=False, default=str)
                maybe_rpush = r.rpush(collector_buffer_key, direct_msg)  # type: ignore[misc]
                if inspect.isawaitable(maybe_rpush):
                    await maybe_rpush
                maybe_expire = r.expire(collector_buffer_key, 7200)
                if inspect.isawaitable(maybe_expire):
                    await maybe_expire
        except Exception:
            logger.error(
                "process_decision_round_tool: direct-write RPUSH failed for %s "
                "(tick telemetry dropped if skip_pubsub=True)",
                collector_buffer_key,
                exc_info=True,
            )

    return result


# ---------------------------------------------------------------------------
# Before-agent callback — initialization
# ---------------------------------------------------------------------------


async def _echo_before_agent_callback(callback_context):
    """Initialize Echo profile and extract decision params from incoming message.

    On first tick (sentiment is None), initializes the Echo from quest config.
    On every decision_round event, extracts params into state so
    process_decision_round can read them.
    """
    state = callback_context.state

    # --- Initialize Echo on first tick ---
    if state.get("sentiment") is None:
        session = getattr(callback_context, "session", None)
        sid = (getattr(session, "id", None) or "unknown") if session else "unknown"
        raw_cfg = state.get("_echo_config")
        echo_config = raw_cfg if isinstance(raw_cfg, dict) else {}
        if not echo_config:
            from agents.utils.communication import resolve_tool_context_user_id

            cb_uid = resolve_tool_context_user_id(callback_context, require_when_saas=False)
            echo_config = (
                await fetch_echo_config(sid, user_id=cb_uid)
                if cb_uid is not None
                else await fetch_echo_config(sid)
            )
            if echo_config:
                state["_echo_config"] = echo_config
        initialize_echo(state, sid, echo_config)
        logger.info(
            "Echo initialized: role=%s tier=%s sentiment=%.2f stance=%s",
            state.get("role"),
            state.get("tier"),
            float(state.get("sentiment", 0.0) or 0.0),
            state.get("stance"),
        )

    # --- Populate resources from incoming event ---
    # The LLM receives the event JSON as user text; extract resource_state
    # into state so the ground truth string reflects actual resource levels.
    # (Mirrors ai_echo_reactive/callback.py:154.)
    try:
        last_text = ""
        user_content = getattr(callback_context, "user_content", None) or getattr(
            callback_context, "_user_content", None
        )
        if user_content is not None and hasattr(user_content, "parts") and user_content.parts:
            for part in user_content.parts:
                if hasattr(part, "text") and part.text:
                    last_text = part.text
                    break
        if not last_text:
            # Traverse the invocation context's session events for the last
            # user message — fallback ADK path.
            session = getattr(callback_context, "session", None)
            if session and hasattr(session, "events"):
                for evt in reversed(session.events or []):
                    if getattr(evt, "author", None) == "user" and evt.content and evt.content.parts:
                        for part in evt.content.parts:
                            if hasattr(part, "text") and part.text:
                                last_text = part.text
                                break
                        if last_text:
                            break
        if last_text:
            try:
                parsed_event = json.loads(last_text)
                if isinstance(parsed_event, dict):
                    if "resource_state" in parsed_event:
                        state["_current_resources"] = parsed_event["resource_state"]
                    if "influence_deltas" in parsed_event:
                        state["_last_influence_deltas"] = parsed_event["influence_deltas"]
                    if "collector_buffer_key" in parsed_event:
                        state["_last_collector_buffer_key"] = parsed_event["collector_buffer_key"]
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception:
        logging.debug("ai_echo: state extraction failed", exc_info=True)

    # --- Build ground truth string for instruction substitution ---
    raw_resources = state.get("_current_resources")
    resources = raw_resources if isinstance(raw_resources, dict) else {}

    def _res_cur(key: str) -> object:
        pool = resources.get(key)
        return pool.get("current", "?") if isinstance(pool, dict) else "?"

    state["echo_ground_truth"] = (
        f"Organization Resources: "
        f"LAU={_res_cur('lau')}, "
        f"BT={_res_cur('bt')}, "
        f"TBU={_res_cur('tbu')}, "
        f"OWB={_res_cur('owb')}\n"
        f"Your sentiment: {state.get('sentiment', 0)}, "
        f"Your resistance: {state.get('resistance', 0)}, "
        f"Your stance: {state.get('stance', 'neutral')}"
    )

    return None  # Continue to LLM


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def get_agent():
    """Create a Tier 1 (Autonomous) AI Echo agent."""
    from agents.utils.local_ai import build_non_gemini_litellm

    effective_model = ECHO_MODEL or DEFAULT_MODEL
    agent_model = resilient_model(effective_model) if _is_gemini else build_non_gemini_litellm(effective_model)
    return LlmAgent(
        name=AGENT_NAME,
        model=agent_model,
        description="An AI stakeholder persona powered by an LLM.",
        static_instruction=ECHO_STATIC_INSTRUCTION,
        instruction=ECHO_DYNAMIC_INSTRUCTION,
        include_contents="none",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=512,
            thinking_config=types.ThinkingConfig(thinking_budget=0) if _is_gemini else None,
        ),
        tools=[process_decision_round_tool],
        before_agent_callback=_echo_before_agent_callback,
    )


root_agent = get_agent()

_echo_app: "App | None" = None


def _get_echo():
    global _echo_app
    runner, app, _ = create_simulation_runner(
        name="ai_echo",
        root_agent=root_agent,
        extra_plugins=[SimulationCommunicationPlugin()],
        dash_log_plugin=RedisDashLogPlugin(
            fire_and_forget=True,
            suppressed_events=ECHO_SUPPRESSED_EVENTS,
        ),
        suppress_gateway_emission=True,
    )
    _echo_app = app
    return runner


# --- A2A Deployment ---
_cache_config = ContextCacheConfig(cache_intervals=10, ttl_seconds=1800) if _is_gemini else None
app = App(
    name="ai_echo",
    root_agent=root_agent,
    context_cache_config=_cache_config,
)

echo_a2a_agent, agent_card = create_a2a_deployment(
    name="ai_echo",
    app_or_agent=app,
    agent_getter=get_agent,
)

def _resolve_port() -> int:
    return int(
        config.optional("PORT", "").strip()
        or config.optional("AI_ECHO_PORT", "").strip()
        or "9123"
    )


if __name__ == "__main__":
    from agents.utils.serve import create_agent_app, serve_agent

    config.load_env()
    port = _resolve_port()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger.info("Starting AI Echo (Tier 1) agent server on port %d", port)

    _echo = _get_echo()

    api_app = create_agent_app(
        name="ai_echo",
        agents_dir="agents",
        adk_app=_echo_app,
        agent_card=agent_card,
        simulation_runner=_echo,
    )
    serve_agent(api_app, port=port)
