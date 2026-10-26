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

"""Leadership Planner with Eval Agent — designs and evaluates leadership scenarios.

Mirrors agents/planner_with_eval/agent.py with leadership-domain tools and
LLM-as-Judge evaluation on 6 enterprise criteria.
"""

from agents.utils.env import configure_project_env

configure_project_env("leadership_planner_eval")

import logging
import os

from a2a.types import AgentSkill
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.genai import types

from agents.leadership_planner_with_eval.adk_tools import get_tools
from agents.leadership_planner_with_eval.prompts import LEADERSHIP_PLANNER_WITH_EVAL
from agents.leadership_planner.callbacks import leadership_guardrail_callback
from agents.utils import config
from agents.utils.communication_plugin import SimulationCommunicationPlugin
from agents.utils.retry import resilient_model
from agents.utils.deployment import create_a2a_deployment
from agents.utils.factory import create_simulation_runner

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AGENT_NAME = "leadership_planner_with_eval"
MODEL = os.getenv("PLANNER_MODEL", "gemini-3-flash-preview")

# [START leadership_planner_eval_agent]
planner_config = types.GenerateContentConfig(
    max_output_tokens=8192,
    temperature=0.2,
    thinking_config=types.ThinkingConfig(thinking_budget=1024),
)


def get_agent():
    """Entry point for the ADK framework."""
    return LlmAgent(
        name=AGENT_NAME,
        model=resilient_model(MODEL),
        description=(
            "An autonomous AI agent responsible for designing enterprise "
            "leadership simulation scenarios. It generates stakeholder topologies, "
            "designs decision rounds, and evaluates scenario quality using a "
            "specialized LLM-as-Judge evaluator."
        ),
        static_instruction=LEADERSHIP_PLANNER_WITH_EVAL.build(),
        tools=get_tools(),
        generate_content_config=planner_config,
        before_model_callback=leadership_guardrail_callback,
    )


# [END leadership_planner_eval_agent]


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
app = App(name=AGENT_NAME, root_agent=root_agent)

leadership_eval_skills = [
    AgentSkill(
        id="design_and_evaluate_leadership_scenario",
        name="Design and Evaluate Leadership Scenario",
        description=(
            "Designs an enterprise leadership simulation scenario and "
            "automatically evaluates its quality across 6 dimensions "
            "(stakeholder alignment, decision quality, resource stewardship, "
            "crisis readiness, strategic coherence, ethical alignment)."
        ),
        tags=["planning", "evaluation", "leadership"],
        examples=[
            "Design and evaluate a leadership simulation for an AI transformation with 10 stakeholders",
        ],
    ),
]

leadership_eval_a2a_agent, agent_card = create_a2a_deployment(
    name=AGENT_NAME,
    app_or_agent=app,
    agent_getter=get_agent,
    skills=leadership_eval_skills,
)

if __name__ == "__main__":
    from agents.utils.serve import create_agent_app, serve_agent

    config.load_env()
    port = int(config.optional("PORT", config.optional("LEADERSHIP_PLANNER_WITH_EVAL_PORT", "9115")))
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
