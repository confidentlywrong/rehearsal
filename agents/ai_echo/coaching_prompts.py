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

"""Coaching prompts library for the Executive Coach Echo.

Socratic prompts keyed by leadership principle, used by the Tier 1
Executive Coach when a contradiction or domain-relevant decision is
detected.  These prompts guide the coach's LLM response toward
reflection-oriented questions rather than directive advice.

No LLM dependency — pure Python data.
"""

# Socratic coaching prompts keyed by principle name.
# Each list contains 3-4 prompts of increasing depth:
#   [0] = awareness probe
#   [1] = perspective shift
#   [2] = commitment challenge
#   [3] = synthesis question (optional)
COACHING_PROMPTS: dict[str, list[str]] = {
    "Strategic Foresight & Vision Cascading": [
        "I notice this decision addresses the immediate need. What's your 3-year view on how this plays out?",
        "How does this align with the vision you articulated earlier to the team?",
        "If you communicated this decision to the board today, what story would it tell about our direction?",
        "What would need to be true for this to be the right strategic choice 5 years from now?",
    ],
    "Adaptive Decision-Making & Agility": [
        "What signal would tell you it's time to reverse or adjust this decision?",
        "Is this a one-way door or a two-way door? How easily can we course-correct?",
        "What's the smallest experiment you could run before committing fully?",
    ],
    "Stakeholder Engagement & Inclusive Communication": [
        "Who hasn't been heard from on this decision yet?",
        "What would happen if you brought {affected_echo} into this conversation before proceeding?",
        "How will the people most affected by this learn about it? What will their first reaction be?",
        "If you were in {affected_echo}'s position, what would you need to hear right now?",
    ],
    "Ethical Leadership & Governance": [
        "Would you be comfortable if this decision were reported on the front page tomorrow?",
        "What precedent does this set for how we handle similar situations in the future?",
        "Who bears the cost of this decision, and did they have a voice in making it?",
    ],
    "Empowerment & Cultivating Distributed Leadership": [
        "Could someone closer to the problem make this decision better than you?",
        "What would it look like to give your team ownership of the implementation?",
        "Are you solving this problem, or building the capability for others to solve problems like it?",
        "How does this decision affect your team's autonomy and growth?",
    ],
    "Organizational Resilience & Change Agility": [
        "What happens if this doesn't work? What's your Plan B?",
        "How much organizational capacity are you spending on this versus keeping for future surprises?",
        "Is this building long-term resilience or creating a short-term fix that adds fragility?",
    ],
    "Resource Optimization & Strategic Allocation": [
        "What are you choosing NOT to fund by allocating resources here?",
        "If you had half the budget, what would you cut — and what would you keep?",
        "How are you measuring whether this investment is paying off?",
        "What's the cost of doing nothing for one more decision round?",
    ],
}

# Contradiction-specific prompts (used when Truth Engine detects a Say-Do gap)
CONTRADICTION_PROMPTS: list[str] = [
    "I notice you committed to {commitment} earlier, but this decision seems to move in a different direction. Can you help me understand your reasoning?",
    "Your team heard you say {commitment}. This decision may send a different signal. How do you reconcile that?",
    "There may be a gap between what was communicated and what this decision enacts. Is that intentional, or an opportunity to realign?",
]

# Resource crisis prompts (used when resources hit critical thresholds)
RESOURCE_CRISIS_PROMPTS: dict[str, list[str]] = {
    "lau": [
        "Your leadership attention is stretched thin. What can you delegate to create space for what matters most?",
        "With limited bandwidth, which stakeholder relationship is most critical to maintain right now?",
    ],
    "bt": [
        "Budget is approaching critical levels. What's the highest-leverage investment you can make with what remains?",
        "Have you considered which initiatives could be paused rather than canceled?",
    ],
    "tbu": [
        "Your team's bandwidth is dangerously low. What's the one thing they should focus on this round?",
        "When bandwidth is this constrained, saying 'no' is as important as saying 'yes.' What will you say no to?",
    ],
    "owb": [
        "Organizational well-being is in the red zone. What would a visible act of care look like right now?",
        "Your team is watching how you respond under pressure. What message do you want to send?",
    ],
}


def get_coaching_prompt(
    principle: str,
    tick: int,
    affected_echo: str = "",
) -> str:
    """Select a coaching prompt for a given principle and tick.

    Uses tick number for deterministic cycling through available prompts.

    Args:
        principle: The leadership principle being coached on.
        tick: Current simulation tick (for deterministic selection).
        affected_echo: Optional role name to substitute into prompts.

    Returns:
        A coaching prompt string.
    """
    prompts = COACHING_PROMPTS.get(principle, [])
    if not prompts:
        return f"How does this decision connect to {principle}?"

    prompt = prompts[tick % len(prompts)]
    if affected_echo and "{affected_echo}" in prompt:
        prompt = prompt.replace("{affected_echo}", affected_echo.replace("_", " ").title())
    return prompt


def get_contradiction_prompt(
    commitment: str,
    tick: int,
) -> str:
    """Select a contradiction coaching prompt.

    Args:
        commitment: The stated commitment that was violated.
        tick: Current simulation tick (for deterministic selection).

    Returns:
        A contradiction-specific coaching prompt.
    """
    prompt = CONTRADICTION_PROMPTS[tick % len(CONTRADICTION_PROMPTS)]
    return prompt.replace("{commitment}", commitment)


def get_resource_crisis_prompt(
    resource: str,
    tick: int,
) -> str:
    """Select a resource crisis coaching prompt.

    Args:
        resource: Resource key (lau, bt, tbu, owb).
        tick: Current simulation tick (for deterministic selection).

    Returns:
        A resource-crisis coaching prompt.
    """
    prompts = RESOURCE_CRISIS_PROMPTS.get(resource.lower(), [])
    if not prompts:
        return f"{resource.upper()} is at a critical level. How will you address this?"
    return prompts[tick % len(prompts)]
