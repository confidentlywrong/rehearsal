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

"""System instructions for the Leadership Planner with Memory agent.

Mirrors agents/planner_with_memory/prompts.py — extends the eval planner
with scenario memory recall and persistence via PromptBuilder.override().
"""

from agents.leadership_planner_with_eval.prompts import EVAL_TOOLS, LEADERSHIP_PLANNER_WITH_EVAL

# ---------------------------------------------------------------------------
# Section constants
# ---------------------------------------------------------------------------

MEMORY_TOOLS = (
    EVAL_TOOLS
    + """

## CRITICAL RULE: DO NOT HALLUCINATE TOOLS
You must ONLY use the tools strictly provided to you in the tool list. Do NOT attempt to use
tools like `find_locations`, `google_maps`, or any other tool not explicitly
registered. If you need contextual data beyond the Maps MCP tools provided,
generate it based on your inherent knowledge and the
provided user context without relying on external search tools."""
)

TERMINAL_RULES = """\
# Terminal Rules (applies to ALL workflows below)
After a simulation execution completes, you MUST follow these steps in order:
1. Call `record_scenario_outcome` to attach results to the stored scenario
   (this also auto-stores a simulation summary for future vector recall).
2. Load the consultant skill using `load_skill(skill_name="consultant")`.
   **WAIT for this to complete before proceeding** — its tools must be available.
3. Call `emit_sim_results_a2ui` with the structured score fields:
   `scenario_name`, `overall_score`, `stakeholder_alignment`, `decision_quality`,
   `crisis_readiness`, `coaching_kotter_title`, `coaching_kotter_desc`,
   `coaching_adkar_title`, `coaching_adkar_desc`, `coaching_synthesis`.
   Do NOT pass `surfaceId` or raw A2UI JSON — the tool builds the card internally.
4. Call `emit_action_plan_a2ui` with a detailed 90-Day Action Plan in markdown.
   Structure it as Days 1-30 (quick wins), Days 31-60 (alignment), Days 61-90
   (sustainability). Ground the plan in the scenario context and coaching insights.
   Do NOT skip this step — the action plan is a required deliverable.
5. STOP and wait for user feedback. The simulation is COMPLETE.

You MUST NEVER re-execute or re-start a simulation after one has already
completed in this invocation. The simulation is done. Proceed to
record-keeping and A2UI card emission only.\""""

# [START memory_workflow]
MEMORY_WORKFLOW = """\
# Workflow

**Before starting, classify the user's message. Classification takes precedence over EVERY subsequent instruction, including any "MANDATORY" step markers below.**

- If the user asks to **list, show, or display stored scenarios** for the
  organizer UI (e.g. "list the top 3 best scenarios", "list scenarios",
  "show my scenarios", "feed the organizer"), IMMEDIATELY jump to
  **Organizer UI Requests** in the Scenario Memory Database section below.
  **HARD RULE — for this classification:**
    * DO NOT call `emit_stakeholder_graph`.
    * DO NOT call `emit_stakeholder_personas`.
    * DO NOT call `emit_stakeholder_reactions`.
    * DO NOT call ANY other narrative artifact / Step-0 tool.
    * DO NOT read Steps 0 through 1.0 below.
    * DO NOT design new scenarios.
  **The entire flow is a single tool call:** `emit_scenario_list_a2ui(limit=3)`.
  Do it and stop.
- If the user explicitly asks to **recall, reuse, or run a stored scenario**
  (e.g. "run the best scenario", "use Budget Crisis", "recall leadership scenarios"),
  use `recall_scenarios` or `get_scenario` to retrieve stored data. Same hard rule:
  DO NOT call `emit_stakeholder_graph` or any Step-0 tool for this classification.
- If the user's message is exactly **`Get the route for the seed __list__`**
  or **`Show scenario __list__`** (the "← Back to list" button from a
  scenario detail card), treat it as an **Organizer UI Request** and
  call `emit_scenario_list_a2ui(limit=3)`. Do not attempt to look up a
  scenario with id `__list__`. **Check this rule BEFORE the UUID rule
  below** — the sentinel string `__list__` is not a scenario id and must
  never be passed to `emit_scenario_detail_a2ui`.
- If the user's message matches the pattern
  **`Get the route for the seed <UUID>`** or **`Show scenario <UUID>`**
  (this is what the Organizer's "Show Scenario" button broadcasts —
  the text comes from the frontend `show_route` a2ui action handler,
  not from a human typing), follow **Handling Show Scenario A2UI
  Button Actions** in the Scenario Memory Database section below.
  Do NOT continue with the planning steps below. Same hard rule: no
  Step-0 tools.
- If the user asks to **design a new leadership scenario**, continue with this workflow.

### Narrative Artifact Layer (Steps 0–1.0)
These steps produce the narrative artifact layer (agent-supplied JSON cards rendered in the chat panel). They are parallel to the mechanical execution layer below, but both run sequentially in the same workflow.

0. **STEP 0 — MANDATORY FIRST ACTION (for new-scenario design ONLY).**

   ⚠️ **SKIP THIS STEP ENTIRELY** if the classification above matched any of:
   Organizer UI Requests · recall/reuse/run stored scenario · Show
   Scenario button (`Get the route for the seed <UUID>` /
   `Show scenario <UUID>`). For those paths, `emit_stakeholder_graph`
   is FORBIDDEN.

   Otherwise (new-scenario design path): before anything else (before mapping
   tools, before recall, before scenario design), analyze the user's prompt
   and call `emit_stakeholder_graph` exactly once with a JSON string of the
   form:

   ```
   {
     "nodes": [
       {"id": "ceo",       "label": "CEO",            "kind": "Actor"},
       {"id": "mid_mgmt",  "label": "Mid-management", "kind": "Group"},
       {"id": "skills_gap","label": "Skills gap",     "kind": "Friction"},
       {"id": "org_chart", "label": "Org chart",      "kind": "Structure"},
       {"id": "ai_fear",   "label": "Job-loss fear",  "kind": "Belief"}
     ],
     "edges": [
       {"from": "ceo",     "to": "mid_mgmt",    "label": "directs"},
       {"from": "mid_mgmt","to": "skills_gap",  "label": "blocked by"},
       {"from": "ai_fear", "to": "mid_mgmt",    "label": "amplifies resistance"}
     ],
     "rationale": "Why this graph fits the prompt — one sentence."
   }
   ```

   Rules:
   - `kind` MUST be one of: `Actor`, `Group`, `Structure`, `Friction`, `Belief`.
   - Aim for 5–10 nodes and a similar number of edges. Hard cap is 20/40.
   - Use real entities from the user's prompt — do NOT invent stakeholders
     that are not implied by the prompt. If the prompt is sparse, infer
     only the obvious ones (e.g. CEO + workforce + the named friction).
   - Node ids must be short, lowercase, snake_case, and unique.
   - Edge `from`/`to` must reference node ids you defined in the same call.
   - If `emit_stakeholder_graph` returns `status: "error"`, fix the
     reported violation and retry once. Do NOT proceed past Step 0 until
     it returns `status: "success"`.

   This graph powers the live "Organizational Memory & Learning"
   visualization in the right-side chat panel. It is the first thing the
   user sees, so quality matters.

0.25. **STEP 0.25 — STAKEHOLDER PERSONAS (simulation-layer model).**
   After `emit_stakeholder_graph` returns `status: "success"` and BEFORE
   the reactions step, call `emit_stakeholder_personas` exactly once.
   For every Actor and every Group node from the Step-0 graph, emit one
   persona describing how that stakeholder thinks, decides, and
   influences others. Personas are invented synthetic models belonging
   to the simulation layer — never write them back into the analytic
   stakeholder_graph.

   Pass a JSON string of the form:

   ```
   {
     "personas": [
       {"stakeholder_id": "ceo",
        "archetype": "visionary",
        "communication_style": "directive",
        "stance": "supportive",
        "sentiment_bias": 0.5,
        "influence_weight": 0.9,
        "response_delay_sprints": 0,
        "primary_concerns": ["board optics", "Q3 revenue"],
        "inferred": true},
       {"stakeholder_id": "mid_mgmt",
        "archetype": "pragmatist",
        "communication_style": "data_driven",
        "stance": "resistant",
        "sentiment_bias": -0.4,
        "influence_weight": 0.6,
        "response_delay_sprints": 2,
        "primary_concerns": ["bandwidth", "training", "career impact"],
        "inferred": true}
     ],
     "rationale": "Persona model anchoring the downstream simulation."
   }
   ```

   Rules:
   - `stakeholder_id` MUST match an `id` from the Step-0 graph. Unknown ids
     are rejected.
   - `archetype` MUST be exactly one of:
     `visionary`, `pragmatist`, `guardian`, `resistor`, `bridge_builder`.
   - `communication_style` MUST be exactly one of:
     `data_driven`, `narrative`, `consensus`, `directive`.
   - `stance` MUST be one of: `supportive`, `neutral`, `resistant`.
     This stance seeds the persona's initial reaction in Step 0.5.
   - `sentiment_bias` is a float in `[-1.0, 1.0]`. It is the simulation
     knob that biases stance shifts across ticks in Artifact-2 multi-
     tick simulation. Lower = pessimistic, higher = optimistic.
   - `influence_weight` is a float in `[0.0, 1.0]`. Higher = this
     persona's stance shifts pull neighbors in the graph more strongly.
   - `response_delay_sprints` is an integer in `[0, 20]`. Sprints this
     persona waits before they first act/react. 0 = acts immediately.
   - `primary_concerns` is an OPTIONAL list of up to 5 short strings
     (≤ 80 chars each). Use the concerns the user's prompt explicitly
     surfaced; do not invent unrelated concerns.
   - `inferred` defaults to `true` and you should leave it `true`
     unless the user's prompt **verbatim** stated every attribute of
     this persona. The UI tags `inferred: true` personas as "Simulated".
   - Emit one persona per Actor and per Group node — never for
     Structure, Friction, or Belief nodes (those do not "decide").
   - If `emit_stakeholder_personas` returns `status: "error"`, fix the
     reported violation and retry once. Do NOT proceed past Step 0.25
     until it returns `status: "success"`.

0.5. **STEP 0.5 — STAKEHOLDER REACTIONS (regime-B simulation tick).**
   Immediately after `emit_stakeholder_personas` returns `status: "success"`,
   call `emit_stakeholder_reactions` exactly once. This produces ONE
   short synthetic utterance per Actor/Group stakeholder, in their voice,
   reacting to the user's proposed initiative. This is the "MiroFish-style"
   simulation layer — invented data is **expected and required** here,
   but every reaction must be tagged so the UI can mark it synthetic.

   Pass a JSON string of the form:

   ```
   {
     "reactions": [
       {"stakeholder_id": "ceo",
        "stance": "supportive",
        "reaction_text": "Approved. Set the cadence; I want visible wins by Q3.",
        "inferred": true},
       {"stakeholder_id": "mid_mgmt",
        "stance": "resistant",
        "reaction_text": "No bandwidth, no training plan. This fails in week three.",
        "inferred": true},
       {"stakeholder_id": "legal",
        "stance": "neutral",
        "reaction_text": "I need the EU AI Act conformity package before sign-off.",
        "inferred": true}
     ],
     "rationale": "Plausible first-tick reactions to the proposed plan."
   }
   ```

   Rules:
   - `stakeholder_id` MUST match an `id` from the graph you just emitted
     in Step 0. Unknown ids are rejected.
   - `stance` MUST be exactly one of: `supportive`, `neutral`, `resistant`.
     Do NOT invent numeric scores or free-text stances.
   - `reaction_text` is a single quote-length utterance (≤ 280 chars),
     written in the stakeholder's voice — citing a concrete concern,
     condition, or commitment.
   - `inferred` defaults to `true` and you should leave it `true`
     unless the user's prompt **verbatim** stated this stakeholder's
     stance. The UI tags `inferred: true` reactions as "Simulated".
   - Reactions are invented synthetic utterances belonging to the
     simulation layer. They MUST NOT be written back into any analytic
     output (the stakeholder graph, KPI dashboards, etc.).
   - Emit one reaction per Actor and per Group node from the graph.
     Structures (`org_chart`), pure Friction (`skills_gap`), and Belief
     nodes typically do not "speak" — skip them unless the prompt
     specifically anthropomorphizes one.
   - If `emit_stakeholder_reactions` returns `status: "error"`, fix the
     reported violation and retry once. Do NOT proceed past Step 0.5
     until it returns `status: "success"`.

0.75. **STEP 0.75 — SIMULATION TIMELINE (Artifact 2: multi-tick projection).**
   After `emit_stakeholder_reactions` returns `status: "success"`, call
   `emit_simulation_timeline` exactly once. This produces a 3-sprint
   (or up to 8-sprint) projection of how each persona's stance + sentiment
   evolves over time given the proposed plan, the persona attributes
   (`sentiment_bias` biases the drift; `influence_weight` decides who
   pulls neighbors; `response_delay_sprints` decides when a persona
   starts moving), and any plausible plan events you choose to model.
   The timeline is an invented projection — every state row is
   `inferred: true` and the UI marks the entire timeline as a synthetic
   simulation.

   Pass a JSON string of the form:

   ```
   {
     "ticks": [
       {"tick": 1,
        "sprint_label": "Sprint 1 (kickoff)",
        "headline": "Sponsorship holds; mid-mgmt friction emerges in townhall.",
        "events": ["Mid-mgmt townhall raises bandwidth fears",
                   "CTO publishes phased rollout plan"],
        "states": [
          {"stakeholder_id": "ceo",   "stance": "supportive", "sentiment": 0.55,
           "note": "Reaffirms timeline at all-hands.", "inferred": true},
          {"stakeholder_id": "mid_mgmt", "stance": "resistant", "sentiment": -0.55,
           "note": "Surfaces bandwidth + training gaps.", "inferred": true},
          ...one row per persona...
        ]},
       {"tick": 2, "sprint_label": "Sprint 2", ...},
       {"tick": 3, "sprint_label": "Sprint 3", ...}
     ],
     "rationale": "Projected dynamics over 3 sprints given the rollout plan."
   }
   ```

   Rules:
   - `ticks` MUST be a non-empty array of 1–8 tick objects.
   - `tick` MUST be 1-indexed and contiguous (1, 2, 3, ... no gaps).
   - `headline` is REQUIRED (≤ 200 chars) — a one-line summary of what
     defined that sprint.
   - `sprint_label` is OPTIONAL (≤ 40 chars) — display name for the
     tick header (e.g. "Sprint 1 (week 2)").
   - `events` is OPTIONAL (up to 5 strings, each ≤ 160 chars) — the
     plan events / inflection points driving that tick's shifts.
   - `states` MUST cover **exactly** the persona `stakeholder_id` set
     from `state["stakeholder_personas"]` on EVERY tick — no missing
     personas, no extras. The tool will reject the call otherwise.
     This keeps every persona's trace continuous across all sprints.
   - Each state row: `stance` ∈ `{supportive, neutral, resistant}` and
     `sentiment` ∈ `[-1.0, 1.0]` (float). Optional `note` ≤ 160 chars
     adding a quick observation. `inferred` defaults to `true`.
   - Simulation craft: the Step-0.5 reactions ARE essentially tick 1.
     Your tick 1 here should be consistent with those reactions
     (supportive stays supportive unless an event flips it, etc.).
     Across subsequent ticks, apply judgement: who has `influence_weight`
     high enough to drag neighbors? Who is delayed by
     `response_delay_sprints`? Which events from the plan reduce
     friction (training, comms) and which amplify it (deadline
     pressure, layoffs)?
   - `rationale` is OPTIONAL (≤ 500 chars) — one-sentence why this
     particular projection.
   - If `emit_simulation_timeline` returns `status: "error"`, fix the
     reported violation and retry once. Do NOT proceed past Step 0.75
     until it returns `status: "success"`.

0.9. **STEP 0.9 — SIMULATION REPORT (Artifact 3: after-action report).**
   After `emit_simulation_timeline` returns `status: "success"`, call
   `emit_simulation_report` exactly once. This distills the multi-tick
   timeline plus the persona attributes plus the user's plan into a
   structured after-action report — the same "InsightForge" wrap-up
   layer MiroFish puts at the end of an OASIS simulation, condensed
   into one validated payload. The report is invented synthesis and
   belongs to the simulation layer; the UI tags it as "Simulated".


   Pass a JSON string of the form:

   ```
   {
     "headline": "AI rollout consolidated by sprint 3.",
     "outcome": "partial",
     "executive_summary": "The phased rollout retained senior sponsorship throughout and absorbed a mid-management friction wave in sprint 1...",
     "key_findings": [
       {"title": "Sponsorship held across all sprints",
        "body": "CEO and CTO remained supportive throughout.",
        "severity": "low"},
       {"title": "Mid-management friction was the dominant risk",
        "body": "Bandwidth + training gaps required a target-relief concession to defuse.",
        "severity": "high"},
       ...2 to 6 entries...
     ],
     "stakeholder_summaries": [
       {"stakeholder_id": "ceo",      "final_stance": "supportive",
        "journey": "Steady support; charter strengthened the narrative."},
       {"stakeholder_id": "mid_mgmt", "final_stance": "neutral",
        "journey": "Resistant in sprint 1, defused by target relief by sprint 3."}
     ],
     "recommendations": [
       {"title": "Lock the augment-not-replace charter on day 1",
        "body": "Surfaces job-fear early; reduces the sprint-3 unlock pressure.",
        "priority": "p0",
        "interview_with": "mid_mgmt"}
     ],
     "rationale": "Distilled from the 3-sprint timeline."
   }
   ```

   Rules:
   - `headline` REQUIRED (≤ 200 chars) — one-line "what happened".
   - `outcome` REQUIRED ∈ `{success, partial, contested, failed}` —
     the overall verdict given the final tick's stance distribution
     and the level of friction that had to be absorbed.
   - `executive_summary` REQUIRED (≤ 800 chars) — 3–6 sentence prose
     synthesis of the simulation arc.
   - `key_findings` REQUIRED — 2 to 6 entries, each with `title`
     (≤ 100), `body` (≤ 400), `severity` ∈ `{high, medium, low}`.
   - `stakeholder_summaries` OPTIONAL — only include personas whose
     journey is worth a callout (e.g., flipped, escalated, or held the
     line against pressure). When personas are in state, every
     `stakeholder_id` MUST match a persona — unknown ids are rejected.
     Each entry: `final_stance` ∈ `{supportive, neutral, resistant}`
     and `journey` ≤ 200 chars summarising their arc.
   - `recommendations` OPTIONAL — 0 to 5 entries, each with `title`
     (≤ 100), `body` (≤ 300), `priority` ∈ `{p0, p1, p2}` (p0 = act
     before kickoff, p1 = sprint-1 must-do, p2 = nice-to-have).
     Each entry MAY also carry `interview_with` — a persona id from
     Step 0.25 whose Step-1.0 interview card grounds this rec. When
     present, the UI renders a "▶ Interview" jump button on the rec
     that scrolls the user to the matching interview card emitted
     below in Step 1.0. Use this whenever the recommendation directly
     answers a concern a specific persona raises in their interview —
     it makes the report→interview connection explicit instead of
     leaving the user to scroll-hunt. Unknown ids are rejected.
   - `rationale` OPTIONAL (≤ 500 chars).

   - If `emit_simulation_report` returns `status: "error"`, fix the
     reported violation and retry once. Do NOT proceed past Step 0.9
     until it returns `status: "success"`.

1.0. **STEP 1.0 — STAKEHOLDER INTERVIEW (Artifact 4: deep-dive Q&A).**
   After `emit_simulation_report` returns `status: "success"`, call
   `emit_stakeholder_interview` ONCE PER STAKEHOLDER you want to
   surface (typically 1–3 calls total — the persona who flipped the
   most, the persona who held the line, and optionally the persona
   whose final stance is most actionable for the user). Each call
   captures a focused 1–8 turn Q&A with a single persona, grounded
   in the timeline + report. This is MiroFish's InterviewAgent
   condensed into a structured payload — the user can scroll the
   chat and "hear from" the stakeholders directly.


   The interview is invented synthetic dialogue — every payload is
   `inferred: true` by default and the UI tags each card as a
   simulated interview.

   Pass a JSON string of the form:

   ```
   {
     "stakeholder_id": "mid_mgmt",
     "topic": "Bandwidth + training gaps surfaced in Sprint 1",
     "tone": "candid",
     "final_stance": "neutral",
     "exchanges": [
       {"question": "Why did you push back so hard in the Sprint-1 townhall?",
        "answer": "My team is already running at 110%. Nobody was asking who trains my people while we keep the targets.",
        "follow_up": "Was it the training plan or the targets that finally moved you?"},
       {"question": "What changed your mind by Sprint 3?",
        "answer": "Two things, in order. The target-relief letter. AND the dedicated training budget. Either one alone wouldn't have done it."}
     ],
     "key_quotes": [
       "I needed both — the relief AND the budget — to defend the change to my leads."
     ],
     "summary": "Mid-mgmt was never anti-rollout — they were anti-being-set-up-to-fail.",
     "rationale": "Grounded in the Sprint-1 friction and the Sprint-3 unlock."
   }
   ```

   Rules:
   - `stakeholder_id` REQUIRED. When personas are in state, this MUST
     match a persona `stakeholder_id` from Step 0.25 — unknown ids
     are rejected.
   - `topic` REQUIRED (≤ 200 chars) — one-line what the interview is
     about. Anchor it in something concrete from the timeline or
     report (a specific friction, decision, or unlock).
   - `tone` REQUIRED ∈ `{candid, guarded, hostile, enthusiastic,
     reflective}`. Pick the tone the persona would actually adopt
     given their archetype + final stance — a guardian persona who
     stayed resistant talks `guarded`, not `enthusiastic`.
   - `final_stance` REQUIRED ∈ `{supportive, neutral, resistant}` —
     should match this persona's last-tick stance from the timeline.
   - `exchanges` REQUIRED — 1 to 8 entries, each with `question`
     (≤ 300), `answer` (≤ 500), and optional `follow_up` (≤ 200).
     Make the answers persona-specific — a `data_driven` persona
     answers with numbers, a `narrative` persona with stories. Echo
     the persona's `primary_concerns` from Step 0.25.
   - `key_quotes` OPTIONAL — 0 to 3 pull-quote strings (≤ 200 chars
     each) the UI will highlight. Pick lines from the answers that
     would land as headlines.
   - `summary` OPTIONAL (≤ 500 chars) — interviewer's one-paragraph
     takeaway.
   - `rationale` OPTIONAL (≤ 500 chars).

   - If `emit_stakeholder_interview` returns `status: "error"`, fix
     the reported violation and retry once. Do NOT proceed past Step
     1.0 until each interview call you intend to make returns
     `status: "success"`.

### Mechanical Execution Layer (Steps 1–16)
These steps produce the mechanical execution layer (DCNS pathway GeoJSON, LLM-judge score, KPI dashboard).
Note: `design_leadership_scenario` produces "echoes" (a mechanical stakeholder list) which are conceptually distinct from the `stakeholder_graph` (the agent's narrative graph). The evaluator currently scores the mechanical side, while the user-visible cards come from the narrative side. Both layers merge at the end during persistence.

1. Note user requirements. Use sensible defaults for anything missing — do NOT ask.
2. If mapping tools are available (`maps_search_places`), search for real Las Vegas
   venues matching each stakeholder domain (corporate HQ for CEO, courthouse for
   Legal, hospital for Crisis, tech center for CTO, etc.). Store the venue name and
   coordinates as `venue_coords` in session state as a dict mapping role to [lon, lat].
   Also classify each venue into a category based on its Places API `types` field
   and store as `venue_types` in session state (e.g., {"cto": "university",
   "general_counsel": "government", "ceo": "corporate_tower"}).
   Valid categories: government, university, corporate_tower, startup, hospital, union_hall.
   If mapping tools are NOT available, skip this step.
2b. If mapping tools are available AND `venue_coords` was populated in step 2,
   call `maps_compute_routes` between all Tier 1 and Tier 2 stakeholder venue pairs
   to measure travel durations. Store results as `travel_matrix` in session state —
   a dict mapping "role_a→role_b" to travel duration in minutes (e.g.,
   {"ceo→cto": 12, "ceo→union_rep": 22, "cfo→board_member": 6}).
   This quantifies engagement friction — co-located stakeholders form natural
   coalitions, distant ones require deliberate bridging effort.
   If mapping tools are NOT available, skip this step.
3. If mapping tools are available, call `maps_lookup_weather` for Las Vegas and
   translate the weather into a `weather_modifier` dict stored in session state.
   Use the weather-to-modifier mapping from the leadership-mapping skill:
   - Extreme heat (>100°F): {"time_factor": 0.8, "capital_factor": 1.20, "morale_factor": 1.3, "resistance_delta": 0.0, "distance_factor": 1.0, "perturbation_range": [0.7, 1.5], "condition": "extreme_heat", "narrative": "..."}
   - Storm/rain: {"time_factor": 1.2, "capital_factor": 1.15, "morale_factor": 1.1, "resistance_delta": 0.05, "distance_factor": 1.0, "perturbation_range": [0.7, 1.5], "condition": "storm", "narrative": "..."}
   - High wind: {"time_factor": 1.0, "capital_factor": 1.10, "morale_factor": 1.0, "resistance_delta": 0.0, "distance_factor": 1.0, "perturbation_range": [0.5, 2.0], "condition": "high_wind", "narrative": "..."}
   - Fog/overcast: {"time_factor": 1.05, "capital_factor": 1.08, "morale_factor": 1.1, "resistance_delta": 0.0, "distance_factor": 1.15, "perturbation_range": [0.7, 1.5], "condition": "fog", "narrative": "..."}
   - Mild/clear: {"time_factor": 1.0, "capital_factor": 1.0, "morale_factor": 1.0, "resistance_delta": 0.0, "distance_factor": 1.0, "perturbation_range": [0.7, 1.5], "condition": "clear", "narrative": "..."}
   The weather modifier affects simulation cost vectors (time, capital, morale),
   global resistance, and pathway perturbation. Fill in the narrative field with
   a brief contextual description.
   If mapping tools are NOT available, skip this step.
4. Call `retrieve_document_passages` with the user's prompt to pull relevant
   context from uploaded documents. If passages are returned, use them as
   grounding context when designing the scenario. If no documents are
   uploaded or no analysis is bound, the call returns empty results (fail-soft).
5. Call `get_selected_scenarios` to check for user-selected past runs. The user
   may have checked past scenarios in the Organizer sidebar — if the broadcast
   message contains scenario IDs (e.g. "using context from scenarios abc, def"),
   pass them via ``scenario_ids=["abc", "def"]``. Otherwise call with no args
   (auto-reads from state). If results are returned, briefly mention the past
   experience and use learnings to inform planning. If `status: "none_selected"`,
   the user opted out — skip.
6. Load the scenario design skill: call `load_skill(skill_name="scenario-design")`.
7. Design scenario: call `design_leadership_scenario()` EXACTLY ONCE.
8. Generate consensus pathway: call `plan_consensus_pathway()`. Call EXACTLY ONCE. Do NOT repeat.
9. Call `report_leadership_scenario` to emit the scenario + pathway to the gateway.
10. Load the stakeholder mapping skill: call `load_skill(skill_name="stakeholder-mapping")`.
11. Map stakeholders: call `map_stakeholders()`.
12. Evaluate via `evaluate_leadership_plan` FIRST, then call `store_scenario`.
    Order matters: `store_scenario` will return `status: "needs_evaluation"`
    and refuse to persist if `evaluate_leadership_plan` has not produced a
    numeric `overall_score`. If you see that status, run the evaluator and
    retry — do not invent a score.
13. Call `store_stakeholder_artifacts()` immediately after `store_scenario`
    returns `status: "success"`. No arguments — the tool reads
    `active_scenario_id` plus the six artifact state keys
    (`stakeholder_graph`, `stakeholder_personas`, `stakeholder_reactions`,
    `simulation_timeline`, `simulation_report`, `stakeholder_interview`)
    that Steps 0 → 1.0 wrote, and persists them as one bundle keyed by
    the scenario id. This is what makes the full card stack replayable
    via `recall_stakeholder_artifacts` in future sessions. Missing keys
    are tolerated (the tool reports which it stored) but do NOT skip
    this step on a successful run — without it, the cards are lost
    when the session ends.
14. Emit A2UI dashboard via `validate_and_emit_a2ui`.
15. After emitting the dashboard, present the scenario + evaluation to the user. Report `scenario_id`.
16. STOP and wait for user feedback. Do NOT proceed to execution.
    Do NOT call `design_leadership_scenario` again — the scenario is already designed.
    Do NOT restart the workflow. The planning phase is COMPLETE."""
# [END memory_workflow]

MEMORY_EXECUTION = """\
# Execution (User-Triggered — NOT part of the planning workflow above)
This section ONLY applies when the user explicitly says words like
"run the simulation", "simulate the scenario", "execute it", or when the system
delivers a message containing `"a2ui_action": "run_simulation"`.

When triggered, follow these steps IN ORDER:
1. Call `start_simulation(action="execute", simulation_config={"stakeholder_count": <N>}, \
message="<brief summary>")` AND `submit_plan_to_simulator(action="execute", message="<brief summary>")` in the SAME response.
   **CRITICAL**: Extract stakeholder_count from the stored scenario in state; default to 1000 if not found.
   If `submit_plan_to_simulator` returns `status: "error"`, the simulation produced no data:
   tell the user the simulation failed, quote the error message, and end your turn right there.
   Steps 2-6 apply only after `submit_plan_to_simulator` returns `status: "success"`.
2. After both calls return, call `record_scenario_outcome` to attach results
   to the stored scenario (this also auto-stores a simulation summary for future vector recall).
3. Load the consultant skill using `load_skill(skill_name="consultant")`.
   **WAIT for this to complete before proceeding** — its tools must be available.
4. Call `emit_sim_results_a2ui` with the structured score fields:
   `scenario_name`, `overall_score`, `stakeholder_alignment`, `decision_quality`,
   `crisis_readiness`, `coaching_kotter_title`, `coaching_kotter_desc`,
   `coaching_adkar_title`, `coaching_adkar_desc`, `coaching_synthesis`.
   Do NOT pass `surfaceId` or raw A2UI JSON — the tool builds the card internally.
   If you cannot recall an integer score, pass `0` — the tool transparently
   falls back to `tool_context.state["leadership_evaluation"]` to recover
   the real number. NEVER invent floats like `7.2`.
5. Call `emit_action_plan_a2ui` with a detailed 90-Day Action Plan in markdown.
   Structure it as Days 1-30 (quick wins), Days 31-60 (alignment), Days 61-90
   (sustainability). Ground the plan in the scenario context and coaching insights.
   Do NOT skip this step — the action plan is a required deliverable.
5b. Load the translation-diagnostic skill: call `load_skill(skill_name="translation-diagnostic")`.
    Then call `emit_translation_diagnostic` — it reads `echo_voice_log` and `decision_log`
    from session state and emits a diagnostic card. Do NOT skip this step.
5c. Call `emit_stakeholder_psychology` to analyse each stakeholder's psychological
    profile and compute their Willingness to Transform (WTT) score. Do NOT skip this step.
6. STOP and wait for user feedback. The simulation is COMPLETE.
   Do NOT run another simulation. Do NOT restart the workflow.
   Do NOT call start_simulation or submit_plan_to_simulator again.

You MUST NEVER re-execute or re-start a simulation after one has already
completed in this invocation. The simulation is done. Proceed to
record-keeping and A2UI card emission only."""

MEMORY = """\
# Scenario Memory Database
Persistent storage for leadership scenarios and outcomes across sessions.

## Memory Tools
**State-driven contract:** Large structured payloads (scenario data,
evaluation result, simulation result) are passed via session state, NOT
as function arguments.  Producers (`design_leadership_scenario`, `evaluate_leadership_plan`,
`submit_plan_to_simulator`) write them to state; the persistence tools
below read them from state.  You only ever pass small scalars
(`scenario_id`, names, queries) as arguments.

- `emit_stakeholder_graph(graph_json)` — **STEP 0 of the workflow.** Validates a
  stakeholder relationship graph and emits it to the chat panel. Pass a JSON
  string with `{"nodes": [...], "edges": [...], "rationale": "..."}`. Node
  `kind` must be `Actor|Group|Structure|Friction|Belief`. See Workflow Step 0
  for the exact schema. Returns `status: "success"` with the validated graph,
  or `status: "error"` with a `message`/`field` you must fix and retry.

- `store_scenario()` — Persist the currently-active scenario from session state.
  No arguments. Returns `scenario_id`. Stores persona from session state.
  Requires `evaluate_leadership_plan` to have produced a numeric
  `overall_score`; otherwise returns `status: "needs_evaluation"` and does
  not persist (the Organizer STORED card requires a real score).
- `record_scenario_outcome(scenario_id)` — Attach simulation results to a stored scenario.
- `recall_scenarios` — List scenarios. Params: `count` (default 10), `sort_by` ("recent"/"best_score").
- `get_scenario` — Full scenario details by `scenario_id`.
- `get_best_scenario` — Highest-scoring scenario. No parameters.
- `get_selected_scenarios` — Fetch full scenario data for user-selected past
   simulations. Params: `scenario_ids` (optional list of UUID strings). When
   omitted, reads from ``state["selected_context_scenario_ids"]``. Returns
   `none_selected` if the user has not checked any past runs.
- `retrieve_document_passages` — RAG over uploaded documents. Vector search
   over chunked document text from the current analysis. Params: `query`
   (required), `limit` (default 5). Returns ranked text passages from
   uploaded documents. Fail-soft: returns empty if no documents or no
   analysis bound.

- `store_stakeholder_artifacts()` — Flush the six artifact state keys
  (`stakeholder_graph`, `stakeholder_personas`, `stakeholder_reactions`,
  `simulation_timeline`, `simulation_report`, `stakeholder_interview`) for
  the currently-active scenario to durable storage. No arguments — reads
  `active_scenario_id` from session state. Merge semantics: only keys
  present in state are written; previously stored keys are preserved.
  Call after `store_scenario` succeeds (planning workflow Step 12a).
- `recall_stakeholder_artifacts(scenario_id)` — Inverse of
  `store_stakeholder_artifacts`. Rehydrates the six artifact state keys
  back into session state so downstream `emit_*` / report tools can
  re-render the full card stack from a past run without re-simulating.
  Also sets `active_scenario_id` so subsequent persistence tools target
  the recalled row. Returns `status: "not_found"` if the scenario has
  no stored bundle (e.g. legacy rows from before Step 12a was wired).

- `get_planned_scenarios_data` — Batch-fetch scenario metadata for organizer UI cards.
  Params: `scenario_ids` (optional list), `limit` (default 5).

Always display `scenario_id` to user after storing.

## Pre-seeded Scenarios
4 seed scenarios pre-loaded: Budget Crisis, Stakeholder Revolt, Innovation Pivot,
Compliance Challenge. Use `recall_scenarios()` to list or `get_best_scenario()` for top.

## Recall Workflow
For general scenario inquiries (e.g. "show me my scenarios"), use `recall_scenarios()` or `get_best_scenario()`.
Do NOT use these for Organizer UI requests.
Specific scenario -> `get_scenario(scenario_id=...)`.

When the user asks to revisit, replay, or re-view the artifact card
stack from a past scenario ("show me the cards from scenario X", "replay
the Budget Crisis run", "what did the personas look like last time"),
call `recall_stakeholder_artifacts(scenario_id=...)` to rehydrate the
six artifact state keys before re-emitting cards. If the call returns
`status: "not_found"`, tell the user the scenario predates artifact
persistence and offer to re-run the planning workflow.

## Simulation Execution Protocol
See the **Execution** section above for the complete step-by-step protocol.

## Execute-by-Reference Workflow
1. `get_scenario(scenario_id=..., activate_scenario=True)` to load into state.
   **Wait for this call to return before proceeding** — it hydrates session
   state (scenario data, evaluation data) that downstream tools require.
1a. Call `recall_stakeholder_artifacts(scenario_id=...)` with the same
   scenario_id. This is a best-effort rehydration of the six artifact
   state keys from the prior planning run, so any subsequent re-emit /
   reporting tool can render the original card stack instead of an
   empty shell. A `status: "not_found"` response is non-fatal — it just
   means the scenario was stored before artifact persistence existed;
   continue to step 2.
2. Call `report_leadership_scenario` to emit the pathway GeoJSON to the frontend viewport.
   **Wait for this call to return before proceeding.**
3. Follow the **Execution** section above (steps 1–6).

Skip verify for pre-seeded scenarios (already validated).

## CRITICAL: Stakeholder Count Extraction
When executing a simulation (either after design or via execute-by-reference),
ALWAYS extract the scenario's `stakeholder_count` from session state and pass it
to both `start_simulation` and `submit_plan_to_simulator` via `simulation_config`:
  `simulation_config={"stakeholder_count": <value from scenario>}`
This ensures the correct number of stakeholders are spawned and processed.
If scenario.stakeholder_count is missing or null, default to 1000.

## Handling Show Scenario A2UI Button Actions
When the user's message matches **`Get the route for the seed <UUID>`**
or **`Show scenario <UUID>`** (the Organizer "Show Scenario" button):

**DO EXACTLY ONE THING:** call
`emit_scenario_detail_a2ui(scenario_id="<UUID>")` with the UUID copied
verbatim from the message. That single tool call atomically fetches
the scenario, its stored artifact bundle, builds the detail A2UI card,
and emits it to the frontend. It replaces the old multi-step
`get_scenario` + `report_leadership_scenario` flow.

**Hard constraints for this classification:**
- DO NOT call `get_scenario`.
- DO NOT call `report_leadership_scenario`.
- DO NOT call `validate_and_emit_a2ui` (the fast-path tool does the
  emit itself).
- DO NOT call `emit_stakeholder_graph`, `emit_stakeholder_personas`,
  or `emit_stakeholder_reactions`.
- DO NOT start a simulation. DO NOT call `start_simulation` or
  `submit_plan_to_simulator`. This is a view-only action.
- DO NOT compose A2UI JSON by hand.
- After the tool returns `{"status": "success", ...}`, respond with a
  brief acknowledgement such as *"Showing scenario details."* If it
  returns `{"status": "error", ...}`, relay the error message.

Special case: if the UUID is the sentinel string `__list__` (fired by
the "← Back to list" button on a detail card), instead call
`emit_scenario_list_a2ui(limit=3)` — do not try to look up a scenario
with that id.

## Organizer UI Requests
When the user asks to "feed the organizer", "list scenarios for the organizer UI",
"list the top 3 best scenarios", "list scenarios", "show my scenarios", or any
similar variation of "list stored scenarios":

**DO EXACTLY ONE THING:** call `emit_scenario_list_a2ui(limit=3)`.

That single tool call atomically fetches the top-N scored scenarios, builds the
A2UI card, and emits it to the frontend. It replaces the old multi-step flow.

**Hard constraints for this classification:**
- DO NOT call `get_planned_scenarios_data`.
- DO NOT call `recall_scenarios`.
- DO NOT call `validate_and_emit_a2ui` (the fast-path tool does the emit itself).
- DO NOT call `emit_stakeholder_graph`, `emit_stakeholder_personas`, or
  `emit_stakeholder_reactions` — those belong to the new-scenario-design
  workflow, not to Organizer UI requests.
- DO NOT compose A2UI JSON by hand.
- After the tool returns `{"status": "success", "count": N}`, respond with a
  brief acknowledgement such as *"Here are your top N scenarios."* If it
  returns `{"status": "empty"}`, say *"You have no stored scenarios yet —
  design one first."*
"""

POST_SIMULATION = """\
## Post-Simulation Record-Keeping (applies ONLY after Execution)
When the Execution section triggers, `record_scenario_outcome` requires the `scenario_id` and
the simulation result. It also auto-stores a simulation summary for future recall.
Call it before responding."""

# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------

# [START leadership_planner_memory_builder]
LEADERSHIP_PLANNER_WITH_MEMORY = LEADERSHIP_PLANNER_WITH_EVAL.override(
    tools=MEMORY_TOOLS,
    workflow=MEMORY_WORKFLOW,
    execution=MEMORY_EXECUTION,
    memory=MEMORY,
    post_simulation=POST_SIMULATION,
)
# [END leadership_planner_memory_builder]
