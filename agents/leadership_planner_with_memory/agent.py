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

"""Leadership Planner with Memory Agent — designs, evaluates, and remembers scenarios.

Mirrors agents/planner_with_memory/agent.py with leadership-domain tools
and AlloyDB-backed scenario memory for cross-session recall.
"""

from agents.utils.env import configure_project_env

configure_project_env("leadership_planner_memory")

import logging
import os

from a2a.types import AgentSkill
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.genai import types

from agents.leadership_planner_with_memory.adk_tools import get_tools
from agents.leadership_planner_with_memory.prompts import LEADERSHIP_PLANNER_WITH_MEMORY
from agents.utils import config
from agents.utils.communication_plugin import SimulationCommunicationPlugin
from agents.utils.retry import resilient_model
from agents.utils.deployment import create_a2a_deployment
from agents.utils.factory import create_simulation_runner

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AGENT_NAME = "leadership_planner_with_memory"
MODEL = os.getenv("PLANNER_MODEL", "gemini-3-flash-preview")

planner_config = types.GenerateContentConfig(
    max_output_tokens=8192,
    temperature=0.2,
    thinking_config=types.ThinkingConfig(thinking_budget=1024),
)


# [START leadership_planner_memory_agent]
def get_agent():
    """Entry point for the ADK framework."""
    return LlmAgent(
        name=AGENT_NAME,
        model=resilient_model(MODEL),
        description=(
            "An autonomous AI agent responsible for designing enterprise "
            "leadership simulation scenarios. It generates stakeholder topologies, "
            "designs decision rounds, evaluates scenario quality using an "
            "LLM-as-Judge evaluator, and persists scenarios in a memory database "
            "for cross-session recall and comparison."
        ),
        static_instruction=LEADERSHIP_PLANNER_WITH_MEMORY.build(),
        tools=get_tools(),
        generate_content_config=planner_config,
        # No before_model_callback: the secure leadership-modeling skill
        # handles refusals via validate_and_emit_a2ui (A2UI card).
        # A programmatic guardrail would short-circuit the LLM before
        # it can emit the A2UI refusal card.
    )


# [END leadership_planner_memory_agent]


root_agent = get_agent()

_runner_app: "App | None" = None


def _get_runner():
    global _runner_app
    runner, app, _ = create_simulation_runner(
        name=AGENT_NAME,
        root_agent=root_agent,
        extra_plugins=[SimulationCommunicationPlugin()],
    )
    _runner_app = app
    return runner


# --- A2A Deployment ---
# NOTE: Two App instances exist by design:
#   - `app` (below): Used for A2A deployment via create_a2a_deployment().
#     No plugins — serves the agent card and handles A2A protocol requests.
#   - `_runner_app` (in _get_runner): Used for the SSE/runner endpoint.
#     Includes SimulationNetworkPlugin + SimulationCommunicationPlugin
#     for real-time gateway dispatch and Redis orchestration.
app = App(name=AGENT_NAME, root_agent=root_agent)


leadership_memory_skills = [
    AgentSkill(
        id="design_evaluate_and_remember_leadership_scenario",
        name="Design, Evaluate, and Remember Leadership Scenario",
        description=(
            "Designs an enterprise leadership simulation scenario, evaluates "
            "its quality, and persists the scenario in a memory database for "
            "future recall and comparison across sessions."
        ),
        tags=["planning", "evaluation", "memory", "leadership"],
        examples=[
            "Design a leadership simulation for an AI transformation with 10 stakeholders",
            "Recall the best leadership scenario we designed",
        ],
    ),
]

leadership_memory_a2a_agent, agent_card = create_a2a_deployment(
    name=AGENT_NAME,
    app_or_agent=app,
    agent_getter=get_agent,
    skills=leadership_memory_skills,
)

if __name__ == "__main__":
    from agents.utils.serve import create_agent_app, serve_agent

    config.load_env()
    port = int(config.optional("PORT", config.optional("LEADERSHIP_PLANNER_WITH_MEMORY_PORT", "9116")))
    logger.info(f"Starting {AGENT_NAME} agent server on port {port}")

    _runner = _get_runner()

    api_app = create_agent_app(
        name=AGENT_NAME,
        agents_dir="agents",
        adk_app=_runner_app,
        agent_card=agent_card,
        simulation_runner=_runner,
    )
    serve_agent(api_app, port=port)
