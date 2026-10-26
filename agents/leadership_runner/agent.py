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

"""Leadership Runner Agent -- LLM-powered organizational stakeholder simulation.

Mirrors the Marathon runner agent (agents/runner/agent.py) but with a
stakeholder persona and leadership-specific physics.  The LLM interprets
simulation events and decides how to respond as an organizational
stakeholder navigating a transformation initiative.

The model backend is configurable via the ``RUNNER_MODEL`` environment variable
(same as the Marathon runner).
"""

from agents.utils.env import configure_project_env

configure_project_env("leadership_runner")

import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
from typing import cast  # noqa: E402

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.agents.context_cache_config import ContextCacheConfig  # noqa: E402
from google.adk.apps import App  # noqa: E402
from google.adk.models.lite_llm import LiteLlm  # noqa: E402
from google.genai import types  # noqa: E402

from agents.utils import config, load_agent_skills  # noqa: E402
from agents.utils.communication_plugin import SimulationCommunicationPlugin  # noqa: E402
from agents.utils.retry import resilient_model  # noqa: E402
from agents.utils.deployment import create_a2a_deployment  # noqa: E402
from agents.utils.factory import create_simulation_runner  # noqa: E402
from agents.utils.plugins import RedisDashLogPlugin  # noqa: E402
from agents.utils.sim_defaults import DEFAULT_RACE_DISTANCE_MI  # noqa: E402
from agents.leadership_runner.initialization import initialize_leadership_stakeholder  # noqa: E402
from agents.leadership_runner.constants import get_persona_profile  # noqa: E402
from agents.leadership_runner.capital import deplete_capital, restore_capital, executive_intervention  # noqa: E402

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Events suppressed from the dashboard for all stakeholder agents.
RUNNER_SUPPRESSED_EVENTS: frozenset[str] = frozenset(
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
DEFAULT_MODEL = "gemini-3.5-flash-lite"
RUNNER_MODEL = (
    (os.environ.get("LEADERSHIP_RUNNER_MODEL") or "").strip()
    or (os.environ.get("RUNNER_MODEL") or "").strip()
    or DEFAULT_MODEL
)

_is_gemini = RUNNER_MODEL.startswith("gemini")

VLLM_API_URL = os.environ.get("VLLM_API_URL", "")
if VLLM_API_URL:
    os.environ.setdefault("OPENAI_API_BASE", VLLM_API_URL)
    os.environ.setdefault("OPENAI_API_KEY", "not-needed")

if not _is_gemini:
    logger.info("Leadership runner using non-Gemini model: %s", RUNNER_MODEL)

# ---------------------------------------------------------------------------
# Agent name configuration
# ---------------------------------------------------------------------------
AGENT_NAME = os.environ.get("AGENT_NAME", "leadership_runner")

# Load tool functions from the agent's skills/ directory
_skills, skill_tools = load_agent_skills(str(pathlib.Path(__file__).parent))

# Stakeholder Identity & Instructions
# [START stakeholder_instruction]
STAKEHOLDER_STATIC_INSTRUCTION = """You are a stakeholder in an organizational AI transformation.
Each user message is a JSON event from the simulator. The most common event is a tick:

  {"event":"tick","tick":<n>,"max_ticks":<n>,"minutes_per_tick":<f>,
   "elapsed_minutes":<f>,"race_distance_mi":<f>,"collector_buffer_key":<s>}

## Your job on every tick

On every tick event you MUST call `process_leadership_tick`. Always. No exceptions.
Use the tool-calling mechanism -- do NOT emit the call as text. The call
must be a structured function_call part.

`process_leadership_tick` requires ALL of these arguments. Never omit any of them:
  - `tick` (from the event)
  - `minutes_per_tick` (from the event)
  - `elapsed_minutes` (from the event)
  - `race_distance_mi` (from the event)
  - `collector_buffer_key` (from the event)
  - `inner_thought` (you write this -- a short <=5 word thought the stakeholder
    is thinking right now, e.g. "Budget concerns unresolved.",
    "This could work.", "Need more data.", "Politics blocking progress.",
    "Coalition gaining strength.", "Timeline too aggressive.")

`inner_thought` is required. Never pass empty. Never omit it. Always write
something fresh, ideally referencing your current alignment, capital, or
status from the stakeholder-state context message.

Do NOT call any other tool unless explicitly instructed by a non-tick
event. Do NOT chain multiple tool calls. Do NOT respond with narrative
text -- only the single structured tool call.

Exception: When the stakeholder is STALLED (exhausted) or DEADLOCKED (collapsed),
you may call `restore_capital` or `executive_intervention` AFTER calling
`process_leadership_tick` if the tick result indicates a critical state.
`executive_intervention` is a one-time emergency recovery from deadlock.
`restore_capital` restores political capital at coalition checkpoints.
`deplete_capital` spends capital to push through resistance.
Never call these tools when the stakeholder is running normally.

## Non-tick events

If the event is `{"event":"start_gun"}`: respond with the text "Ready."
If the event is anything else: respond with the text "ack".
"""
# [END stakeholder_instruction]

STAKEHOLDER_DYNAMIC_INSTRUCTION = (
    "Stakeholder state right now -- "
    "alignment: {distance} units, "
    "political capital: {water}%, "
    "momentum: {velocity}, "
    "status: {runner_status}, "
    "target alignment: {target_finish_minutes} min."
)


# [START stakeholder_model_config]
def _build_generate_content_config() -> types.GenerateContentConfig:
    """Build model generation config, adapting for the selected backend."""
    kwargs: dict = {
        "temperature": 0.2,
        "max_output_tokens": 256,
    }
    if _is_gemini:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    return types.GenerateContentConfig(**kwargs)


async def _stakeholder_before_agent_callback(callback_context):
    """Initialize stakeholder profile and extract tick params from incoming message.

    Order matches Marathon gold standard: init first, then extract tick params.
    Each runner self-assigns a persona via seeded random in
    initialize_leadership_stakeholder(), ensuring diverse persona distribution.
    """
    state = callback_context.state
    content = getattr(callback_context, "user_content", None)

    # --- Initialize stakeholder on first tick (Marathon ordering: init FIRST) ---
    if state.get("velocity") is None:
        session = getattr(callback_context, "session", None)
        sid = getattr(session, "id", "unknown") if session else "unknown"
        runner_count = state.get("runner_count")
        if runner_count is None:
            logger.warning("CRITICAL: runner_count is missing from state. Defaulting to 1 to avoid stall.")
            runner_count = 1
        initialize_leadership_stakeholder(state, sid, runner_count)
        persona_name = state.get("persona", "pragmatist")
        logger.info(
            "Stakeholder initialized: persona=%s momentum=%.4f target_finish=%.1f min",
            persona_name,
            state["velocity"],
            state.get("target_finish_minutes", 0),
        )
        # Tier 0 debug: dump the physics profile so developers can correlate
        # a stakeholder's observed behaviour with its persona parameters.
        profile = get_persona_profile(persona_name)
        logger.info(
            "Stakeholder profile [%s] session=%s: momentum_scale=%.2f "
            "capital_depletion=%.3f friction_growth=%.4f entropy_rate=%.4f "
            "min_entropy=%.2f stall<%.0f deadlock<%.0f checkpoint_refill=%.1f "
            "init_capital=%.0f wall_mi=%.1f wall_severity=%.2f",
            persona_name,
            sid,
            profile.get("momentum_scale", 0.0),
            profile.get("capital_depletion_rate", 0.0),
            profile.get("friction_growth", 0.0),
            profile.get("entropy_rate", 0.0),
            profile.get("min_entropy_factor", 0.0),
            profile.get("stall_threshold", 0.0),
            profile.get("deadlock_threshold", 0.0),
            profile.get("checkpoint_refill", 0.0),
            state.get("water", 0.0),
            state.get("wall_mi", 0.0),
            state.get("wall_severity", 0.0),
        )

    # --- Extract tick params from user message into state (Marathon ordering: SECOND) ---
    if content and hasattr(content, "parts") and content.parts:
        for part in content.parts:
            text = getattr(part, "text", None)
            if not text:
                continue
            try:
                msg = json.loads(text)
                if isinstance(msg, dict) and msg.get("event") == "tick":
                    state["_tick_params"] = {
                        "tick": msg.get("tick", 0),
                        "minutes_per_tick": msg.get("minutes_per_tick", 0.0),
                        "elapsed_minutes": msg.get("elapsed_minutes", 0.0),
                        "race_distance_mi": msg.get("race_distance_mi", DEFAULT_RACE_DISTANCE_MI),
                        "collector_buffer_key": msg.get("collector_buffer_key", ""),
                        "station_index": msg.get("station_index", {}),
                        "route_friction_map": msg.get("route_friction_map", {}),
                        "gatekeeper_route_map": msg.get("gatekeeper_route_map", {}),
                    }
                    break
            except (json.JSONDecodeError, TypeError):
                pass

    return None  # Continue to LLM


def get_agent():
    """Entry point for the ADK framework."""
    return LlmAgent(
        name=AGENT_NAME,
        model=resilient_model(RUNNER_MODEL) if _is_gemini else LiteLlm(model=RUNNER_MODEL),
        description="A stakeholder in an organizational transformation, powered by an LLM.",
        static_instruction=STAKEHOLDER_STATIC_INSTRUCTION,
        instruction=STAKEHOLDER_DYNAMIC_INSTRUCTION,
        include_contents="none",
        generate_content_config=_build_generate_content_config(),
        tools=cast(list, skill_tools) + [deplete_capital, restore_capital, executive_intervention],
        before_agent_callback=_stakeholder_before_agent_callback,
    )


# [END stakeholder_model_config]


root_agent = get_agent()

_runner_app: "App | None" = None


def _get_runner():
    global _runner_app
    runner, app, _ = create_simulation_runner(
        name=AGENT_NAME,
        root_agent=root_agent,
        extra_plugins=[SimulationCommunicationPlugin()],
        dash_log_plugin=RedisDashLogPlugin(
            fire_and_forget=True,
            suppressed_events=RUNNER_SUPPRESSED_EVENTS,
        ),
        suppress_gateway_emission=True,
    )
    _runner_app = app
    return runner


# --- A2A Deployment ---
_cache_config = ContextCacheConfig(cache_intervals=10, ttl_seconds=1800) if _is_gemini else None
app = App(
    name=AGENT_NAME,
    root_agent=root_agent,
    context_cache_config=_cache_config,
)

runner_a2a_agent, agent_card = create_a2a_deployment(
    name=AGENT_NAME,
    app_or_agent=app,
    agent_getter=get_agent,
)

if __name__ == "__main__":
    from agents.utils.serve import create_agent_app, serve_agent

    config.load_env()
    port = int(config.optional("PORT", config.optional("LEADERSHIP_RUNNER_PORT", "9117")))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger.info("Starting leadership runner agent server on port %d", port)

    _runner = _get_runner()

    api_app = create_agent_app(
        name=AGENT_NAME,
        agents_dir="agents",
        adk_app=_runner_app,
        agent_card=agent_card,
        simulation_runner=_runner,
    )
    serve_agent(api_app, port=port)
