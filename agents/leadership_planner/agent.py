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

"""Leadership Planner Agent — enterprise leadership scenario architect.

Mirrors agents/planner/agent.py with leadership-domain tools, skills,
and instructions. Uses the same ADK agent patterns and deployment wiring.
"""

from agents.utils.env import configure_project_env

configure_project_env("leadership_planner")

import logging
import os

from a2a.types import AgentSkill
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.genai import types

from agents.leadership_planner.adk_tools import get_tools
from agents.leadership_planner.prompts import LEADERSHIP_PLANNER
from agents.utils import config
from agents.utils.retry import resilient_model
from agents.utils.communication_plugin import SimulationCommunicationPlugin
from agents.utils.deployment import create_a2a_deployment
from agents.utils.factory import create_simulation_runner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AGENT_DIR = os.path.dirname(__file__)


# [START leadership_planner_agent]
def get_agent():
    """Entry point for the ADK framework."""
    return LlmAgent(
        name="leadership_planner",
        model=resilient_model("gemini-3-flash-preview"),
        description="Leadership Scenario Architect for enterprise simulation design.",
        static_instruction=LEADERSHIP_PLANNER.build(),
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=8192,
            temperature=0.1,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
        ),
        tools=get_tools(),
    )


# [END leadership_planner_agent]


root_agent = get_agent()

_runner_app: "App | None" = None


def _get_runner():
    global _runner_app
    runner, app, _ = create_simulation_runner(
        name="leadership_planner",
        root_agent=root_agent,
        extra_plugins=[SimulationCommunicationPlugin()],
    )
    _runner_app = app
    return runner


# --- A2A Deployment ---
app = App(name="leadership_planner", root_agent=root_agent)

leadership_planner_skills = [
    AgentSkill(
        id="design_leadership_scenario",
        name="Design Leadership Scenario",
        description=(
            "Designs a comprehensive enterprise leadership simulation scenario, "
            "including stakeholder topology, decision rounds, resource allocation, "
            "and alignment thresholds."
        ),
        tags=["planning", "simulation", "leadership"],
        examples=[
            "Design a leadership simulation for an AI-powered enterprise transformation with 10 stakeholders",
            "Create a crisis management scenario with 5 executives",
        ],
    ),
]

leadership_planner_a2a_agent, agent_card = create_a2a_deployment(
    name="leadership_planner",
    app_or_agent=app,
    agent_getter=get_agent,
    skills=leadership_planner_skills,
)

if __name__ == "__main__":
    from agents.utils.serve import create_agent_app, serve_agent

    config.load_env()
    port = int(config.optional("PORT", config.optional("LEADERSHIP_PLANNER_PORT", "9114")))
    logger.info(f"Starting leadership_planner agent server on port {port}")

    _runner = _get_runner()

    api_app = create_agent_app(
        name="leadership_planner",
        agents_dir="agents",
        adk_app=_runner_app,
        agent_card=agent_card,
        simulation_runner=_runner,
    )
    serve_agent(api_app, port=port)
