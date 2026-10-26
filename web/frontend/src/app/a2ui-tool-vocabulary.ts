/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * A2UI tool-vocabulary translation layer.
 *
 * The agent backend exposes tools with names borrowed from the original
 * marathon-simulation infrastructure (e.g. `fire_start_gun`, `spawn_runners`,
 * `start_race_collector`).  In the leadership / innovation-management
 * repurposing those tools carry different semantics — `fire_start_gun`
 * initiates a change cycle, `spawn_runners` instantiates stakeholder personas,
 * `maps_search_places` anchors stakeholder personas to real venues so the DCNS
 * engine can compute engagement friction, etc.
 *
 * This file is the SINGLE source of truth that translates marathon-shaped
 * tool identifiers + result strings into the leadership vocabulary the user
 * sees in the A2UI activity feed.  Edit this file (and only this file) to
 * adjust how those calls are presented.
 *
 * Three translation surfaces:
 *
 *   1. `getToolCallLabel(toolName, argValue)` — returns the SINGLE label
 *      shown as the tool-call card title.  For tools with a salient argument
 *      (e.g. `maps_search_places`'s `textQuery`), the label is the argument
 *      itself, optionally rewritten to a leadership-intent phrase via
 *      `LEADERSHIP_QUERY_REWRITES` (e.g. `"universities in las vegas, nv"`
 *      → `"Academic / talent stakeholders"`).  For tools without a salient
 *      argument, it falls back to the tool's renamed verb (`TOOL_DISPLAY`).
 *
 *   2. `getToolDisplay(toolName)` — legacy lookup, kept for tests and any
 *      caller that wants the renamed verb directly.  The `intent` subtitle
 *      was removed from the UI on 2026-06-25; the field is preserved on the
 *      interface for backward compatibility but is no longer rendered.
 *
 *   3. `applyResultRewrites(text)` — regex-rewrites any stray marathon
 *      vocabulary leaking through the tool-result body string.
 */

export interface ToolDisplay {
  /** Replacement for the bold tool-call card title. */
  title: string;
  /** Optional small grey subtitle that translates the call's INTENT. */
  intent?: string;
}

/**
 * Leadership-intent rewrites for tool-call ARGUMENT strings.
 *
 * Applied by `getToolCallLabel` to the value passed to a tool (typically
 * the `textQuery` of `maps_search_places`).  When a regex matches, the
 * literal argument is replaced with the leadership-intent phrase the user
 * cares about — e.g. `"universities in las vegas, nv"` → `"Academic /
 * talent stakeholders"`.  When nothing matches, the literal argument is
 * shown unchanged.
 *
 * Order matters: longer / more-specific patterns first.
 */
const LEADERSHIP_QUERY_REWRITES: Array<[RegExp, string]> = [
  // Executive leadership & C-suite anchors (apex of the org chart)
  [/\b(corporate headquarters?|c-?suite|executive offices?|board rooms?|executive suites?|corporate towers?|HQ)\b/i, 'Executive / CEO stakeholders'],
  // Innovation / startup ecosystem (must precede the academic regex so "technology center"
  // doesn't get swallowed by the broader "technology park" match).
  [/\b(technology centers?|tech (centers?|hubs?|districts?)|innovation (centers?|hubs?|districts?|labs?)|incubators?|accelerators?|startup hubs?|maker spaces?)\b/i, 'Innovation / startup-ecosystem stakeholders'],
  // Academic + research talent pool (CTO / R&D anchors)
  [/\b(universit(y|ies)|colleges?|campus|research (labs?|park)|r&d|technology park)\b/i, 'Academic / R&D-talent stakeholders'],
  // Healthcare ecosystem
  [/\b(hospitals?|medical centers?|clinics?|healthcare)\b/i, 'Healthcare stakeholders'],
  // Tourism / hospitality (a Vegas-heavy demographic in our scenarios)
  [/\b(casinos?|hotels?|resorts?|entertainment venues?|convention centers?|conference centers?)\b/i, 'Tourism / hospitality stakeholders'],
  // Critical infrastructure
  [/\b(data centers?|switch(es)?|fiber|colocation|cloud regions?|power plants?|substations?)\b/i, 'Infrastructure stakeholders'],
  // Organized labor / workforce representation
  [/\b(unions?|workers? local|labor council|union halls?|workers' council)\b/i, 'Labor / union stakeholders'],
  // Legal-counsel anchors (must precede the general regulatory match)
  [/\b(law firms?|legal offices?|general counsel|in-house legal|corporate counsel)\b/i, 'Legal-counsel stakeholders'],
  // Government / regulatory
  // Note: `\bcourts?\b` would miss "courthouse" because the t/h boundary is not a word break;
  // an explicit "courthouses?" alternative is required.
  [/\b(courts?|courthouses?|justice center|city hall|government|county|federal|regulatory|state agency|municipal)\b/i, 'Regulatory / government stakeholders'],
  // Business community
  [/\b(chamber of commerce|business council|trade associations?|industry groups?)\b/i, 'Business-community stakeholders'],
  // Financial / sponsor stakeholders
  [/\b(banks?|investment|venture capital|private equity|family offices?|hedge funds?)\b/i, 'Capital / sponsor stakeholders'],
  // Education K-12
  [/\b(schools?|school district|k-12|education)\b/i, 'K-12 education stakeholders'],
  // Faith / community
  [/\b(churches?|mosques?|temples?|congregations?|community centers?|neighborhood associations?)\b/i, 'Faith / community stakeholders'],
  // Media
  [/\b(newspapers?|tv stations?|media outlets?|press|broadcasters?)\b/i, 'Media stakeholders'],
];

/**
 * Tools whose first/primary argument carries semantic intent worth showing
 * as the card label.  When the tool is in this set AND the argument is
 * non-empty, the argument (literal or leadership-rewritten) replaces the
 * tool-verb title.  Otherwise we fall back to `TOOL_DISPLAY[toolName].title`.
 */
const TOOLS_WITH_SALIENT_ARG = new Set([
  'maps_search_places',
  'maps_lookup_weather',
]);

/**
 * Tools whose RESULT BODY exposes only the physical world (raw Places API
 * descriptions, weather JSON, route timings) and would defeat the leadership
 * /innovation-management framing.
 *
 * On 2026-06-25 the original "hide the body entirely" behaviour was changed
 * to "replace the body with a leadership-meta one-liner" — the previous fix
 * produced visually-empty cards which the user (rightly) flagged as worse
 * than the original physical-world prose.  See `synthesizeLeadershipBody()`
 * below for the per-tool replacements.
 *
 * Rationale: a card titled "Executive / CEO stakeholders" must not be
 * followed by a paragraph describing "Nevada Corporate Headquarters is a
 * business service provider..." but it ALSO must not be visually empty.
 * The leadership-meta body line satisfies both constraints.
 */
const TOOLS_WITH_SYNTHESIZED_BODY = new Set([
  'maps_search_places',
  'maps_lookup_weather',
  'maps_compute_routes',
]);

/**
 * Tool-name → display-title + leadership-intent subtitle.
 *
 * Keys are the literal `toolName` emitted by the ADK agent.  Add an entry
 * here when you want a tool's card in the agent activity feed to read in
 * leadership vocabulary instead of its raw identifier.
 */
const TOOL_DISPLAY: Record<string, ToolDisplay> = {
  // --- Google Maps Grounding-Lite (anchors stakeholders to real venues) ---
  maps_search_places: {
    title: 'Discover stakeholder venues',
    intent: 'anchoring stakeholder personas to real-world venues for the DCNS influence graph',
  },
  maps_compute_routes: {
    title: 'Measure engagement friction',
    intent: 'computing travel-time edges between stakeholder venues — feeds friction weights into DCNS',
  },
  maps_lookup_weather: {
    title: 'Read environmental pressure',
    intent: 'translating ambient conditions into a cost-vector modifier on stakeholder resources',
  },

  // --- Simulator pre-race (configures and triggers the decision cycle) ---
  prepare_simulation: {
    title: 'Configure decision cycle',
    intent: 'sizing the simulation: cycles, stakeholders, tick cadence',
  },
  spawn_runners: {
    title: 'Instantiate stakeholders',
    intent: 'spawning the stakeholder-persona agents that will react to the change initiative',
  },
  start_race_collector: {
    title: 'Start stakeholder telemetry',
    intent: 'opening the Redis-backed channel that aggregates per-stakeholder updates each cycle',
  },
  fire_start_gun: {
    title: 'Initiate change initiative',
    intent: 'broadcasting the change signal to every stakeholder persona simultaneously',
  },

  // --- Simulator post-race (aggregates outcomes, tears down) ---
  advance_tick: {
    title: 'Advance decision cycle',
    intent: 'running one cycle of stakeholder reaction, ECSI contention and DCNS pathway update',
  },
  check_race_complete: {
    title: 'Check consensus status',
    intent: 'testing whether the population has reached alignment or stalled',
  },
  compile_results: {
    title: 'Aggregate consensus outcomes',
    intent: 'collapsing per-cycle telemetry into alignment, deadlock and disengagement counts',
  },
  stop_race_collector: {
    title: 'Stop stakeholder telemetry',
    intent: 'closing the per-stakeholder update channel and unsubscribing dispatchers',
  },

  // --- Leadership planner (scenario design + persona generation) ---
  design_leadership_scenario: {
    title: 'Design leadership scenario',
    intent: 'composing the change-initiative narrative, stakeholder roster and resource pools',
  },
  report_leadership_scenario: {
    title: 'Report leadership scenario',
    intent: 'emitting the scenario card surface for review',
  },
  plan_consensus_pathway: {
    title: 'Plan consensus pathway',
    intent: 'building the DCNS influence graph anchored to stakeholder venues + engagement friction',
  },
  map_stakeholders: {
    title: 'Map stakeholders',
    intent: 'generating persona cards with affinity vectors and behavioural patterns',
  },
  evaluate_leadership_plan: {
    title: 'Score plan (DCNS / ECSI / Decision Physics)',
    intent: 'running the seven-principle evaluator against the proposed pathway',
  },
  store_scenario: {
    title: 'Save scenario',
    intent: 'writing the scenario + plan to the leadership scenario store',
  },

  // --- Tick-time processing (per-cycle agent invocations) ---
  process_tick: {
    title: 'Process cycle (stakeholder)',
    intent: 'stakeholder persona reacts to the latest cycle event',
  },
  process_leadership_tick: {
    title: 'Process cycle (stakeholder)',
    intent: 'leadership stakeholder persona reacts to the latest cycle event',
  },

  // --- Post-simulation analysis ---
  process_decision_round_tool: {
    title: 'Process stakeholder decision',
    intent: "stakeholder persona evaluates the leader's latest decision and updates trust/resistance",
  },
  emit_sim_results_a2ui: {
    title: 'Emit simulation results',
    intent: 'rendering the simulation outcome dashboard with persona alignment scores',
  },
  emit_action_plan_a2ui: {
    title: 'Emit 90-day action plan',
    intent: 'generating the phased transformation roadmap based on simulation insights',
  },
  emit_translation_diagnostic: {
    title: 'Analyse translation gaps',
    intent: "identifying where leader's System language collides with stakeholder Lifeworld",
  },
  emit_stakeholder_psychology: {
    title: 'Profile stakeholder psychology',
    intent: 'computing Willingness to Transform scores and malicious compliance risk',
  },
  emit_stakeholder_graph: {
    title: 'Map influence network',
    intent: 'constructing the stakeholder influence graph with trust, power and alignment edges between key actors',
  },
  emit_stakeholder_personas: {
    title: 'Generate stakeholder personas',
    intent: 'building detailed stakeholder profiles with motivations, fears, communication styles and influence vectors',
  },
  emit_stakeholder_reactions: {
    title: 'Simulate stakeholder reactions',
    intent: 'projecting how each stakeholder persona responds to the proposed change initiative across decision cycles',
  },
  emit_simulation_timeline: {
    title: 'Build simulation timeline',
    intent: 'sequencing key decision moments, stakeholder inflection points and consensus milestones across the change horizon',
  },
  emit_simulation_report: {
    title: 'Compile simulation report',
    intent: 'aggregating consensus outcomes, alignment scores and risk indicators into the final leadership assessment',
  },
  emit_stakeholder_interview: {
    title: 'Conduct stakeholder interview',
    intent: 'simulating a structured dialogue with a stakeholder persona to surface hidden concerns and alignment blockers',
  },
  start_simulation: {
    title: 'Initialize simulation',
    intent: 'preparing the simulation environment and spawning stakeholder agents',
  },
  submit_plan_to_simulator: {
    title: 'Submit plan to simulator',
    intent: 'sending the leadership scenario and configuration to the simulator agent',
  },

  // --- Memory & retrieval tools ---
  retrieve_document_passages: {
    title: 'Query organizational memory',
    intent: 'retrieving curated beliefs, frictions, and past outcomes from the memory graph',
  },
  get_selected_scenarios: {
    title: 'Load context scenarios',
    intent: 'loading past scenarios selected for context injection',
  },
  emit_scenario_list_a2ui: {
    title: 'Show scenario catalog',
    intent: 'rendering the interactive scenario list surface',
  },
  emit_scenario_detail_a2ui: {
    title: 'Show scenario analysis',
    intent: 'rendering the detailed scenario evaluation surface',
  },
  record_scenario_outcome: {
    title: 'Record simulation outcome',
    intent: 'persisting decision cycle metrics and consensus outcomes to memory',
  },
  store_stakeholder_artifacts: {
    title: 'Persist stakeholder artifacts',
    intent: 'storing stakeholder analysis artifacts in long-term memory',
  },
  recall_stakeholder_artifacts: {
    title: 'Recall stakeholder artifacts',
    intent: 'retrieving stored stakeholder analysis from memory',
  },
  recall_scenarios: {
    title: 'Search past scenarios',
    intent: 'querying the scenario database for relevant precedents',
  },
  get_scenario: {
    title: 'Fetch scenario',
    intent: 'loading a specific scenario from the database',
  },
  get_best_scenario: {
    title: 'Fetch benchmark scenario',
    intent: 'loading the highest-scoring scenario as a benchmark',
  },
  get_planned_scenarios_data: {
    title: 'Fetch planned scenario data',
    intent: 'loading scenario data for simulation planning',
  }
};

/**
 * Result-text safety-net rewrites.
 *
 * Applied to the BODY text of a tool-call card after it completes.  Catches
 * stray marathon vocabulary that either (a) leaks through from Google Maps
 * Grounding-Lite responses we don't control, or (b) was emitted by a Python
 * tool we haven't yet updated to use leadership vocab.
 *
 * Order matters: longer / more-specific patterns first.
 */
const RESULT_REWRITES: Array<[RegExp, string]> = [
  // Class / identifier names
  [/\bRaceCollector\b/g, 'StakeholderTelemetry'],
  [/\bSTART[_ ]GUN\b/g, 'CHANGE_INITIATIVE'],
  [/\brace collector\b/gi, 'stakeholder telemetry'],
  [/\brace results?\b/gi, 'consensus outcomes'],
  [/\bstart gun\b/gi, 'change initiative'],
  [/\bspawn of runners?\b/gi, 'stakeholder instantiation'],

  // Counts ("1000 runners" → "1000 stakeholders") — must come after the
  // class-name rewrites so "RaceCollector" doesn't get parsed as "race ...".
  [/\b(\d+)\s+runners?\b/gi, '$1 stakeholders'],

  // Bare nouns (last, to avoid double-replacing inside "10 runners")
  [/\brunner agents?\b/gi, 'stakeholder personas'],
  [/\brunners?\b/gi, 'stakeholders'],
];

/**
 * Return the display title + leadership-intent subtitle for a tool name.
 *
 * Unknown tool names fall back to the raw identifier (no translation,
 * no subtitle) so newly added tools don't silently disappear.
 */
export function getToolDisplay(toolName: string | undefined | null): ToolDisplay {
  if (!toolName) return { title: '' };

  // Strip any "tool: skill" suffix the chat layer concatenates onto tool names
  // (see agent-chat-display.service.ts TOOL_START handler).
  const base = toolName.split(':')[0].trim();

  const entry = TOOL_DISPLAY[base];
  if (entry) return entry;
  return { title: toolName };
}

/**
 * Rewrite a single tool-call argument string to its leadership intent if a
 * pattern matches; otherwise return the argument unchanged (lowercase
 * forms are gently title-cased for display).
 */
function rewriteQueryToLeadershipIntent(argValue: string): string {
  const trimmed = argValue.trim();
  if (!trimmed) return '';
  for (const [pattern, replacement] of LEADERSHIP_QUERY_REWRITES) {
    if (pattern.test(trimmed)) return replacement;
  }
  // No leadership rewrite — show the literal argument, with the first
  // character upper-cased so it reads as a label rather than a raw query.
  return trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
}

/**
 * Return the SINGLE label shown as the tool-call card title.
 *
 * - For tools in `TOOLS_WITH_SALIENT_ARG` with a non-empty argument
 *   (`argValue`), the label is the argument — leadership-rewritten when
 *   `LEADERSHIP_QUERY_REWRITES` has a matching pattern, otherwise literal.
 * - Otherwise the label falls back to the renamed tool verb
 *   (`TOOL_DISPLAY[toolName].title`) and finally the raw tool name.
 *
 * The italic "intent" subtitle that previously sat below the title was
 * removed on 2026-06-25 in favour of this single-line label.
 */
export function getToolCallLabel(
  toolName: string | undefined | null,
  argValue: string | undefined | null,
): string {
  if (!toolName) return '';
  const base = toolName.split(':')[0].trim();

  const arg = (argValue ?? '').trim();
  if (arg && TOOLS_WITH_SALIENT_ARG.has(base)) {
    return rewriteQueryToLeadershipIntent(arg);
  }

  const entry = TOOL_DISPLAY[base];
  return entry ? entry.title : toolName;
}

/**
 * Per-cohort body prose for `maps_search_places` cards.
 *
 * Keyed by the leadership-rewritten label produced by `LEADERSHIP_QUERY_REWRITES`.
 * Each line is a single flowing sentence (no list bullets, no choppy
 * fragments) that explains how the cohort participates in the DCNS / ECSI
 * influence dynamics for a change-initiative — what makes them move, what
 * stalls them, and where they sit in the decision-cycle pathway.
 *
 * Keep entries short enough to read at-a-glance (≈40 words) but
 * substantive enough that two adjacent cards never read identically.
 */
const STAKEHOLDER_BODY: Record<string, string> = {
  'Executive / CEO stakeholders':
    'Apex decision nodes with high momentum and low willingness to pivot without a quantified ROI narrative. Their alignment unlocks every downstream tier; their dissent stalls the entire change initiative — DCNS weights them as the highest-leverage anchors in the influence graph.',
  'Innovation / startup-ecosystem stakeholders':
    'Velocity-anchored cohort that unlocks experimentation legitimacy and rapid-prototyping channels. They disengage quickly when initiative cadence slows below quarterly milestones, so DCNS routes early-cycle wins through this group to compound momentum across later tiers.',
  'Academic / R&D-talent stakeholders':
    'Long-horizon thinkers who optimize for technical credibility and publication-grade rigor. They lend the initiative R&D legitimacy when framing is research-translation, and quietly disengage when value is positioned in purely commercial language.',
  'Healthcare stakeholders':
    'Risk-averse, regulation-sensitive cohort with strong veto power in late ECSI rounds. They amplify environmental-pressure signals and slow consensus whenever patient-impact framing is absent from the change narrative.',
  'Tourism / hospitality stakeholders':
    'Revenue-sensitive cohort focused on guest experience and operational continuity. They move quickly when ROI is concrete and timelines are visible; ambiguity collapses their willingness-to-change within a single decision cycle.',
  'Infrastructure stakeholders':
    'Reliability-first cohort whose capacity ceilings translate directly into hard constraints on the DCNS pathway. They resist any change that increases operational risk and require quantified resilience guarantees before they grant alignment.',
  'Labor / union stakeholders':
    'Workforce-protection cohort with strong veto power over initiatives that shift roles, hours, or compensation. Their early alignment dramatically lowers downstream ECSI contention; late engagement multiplies friction across every other tier.',
  'Legal-counsel stakeholders':
    'Risk-mitigation cohort that converts ambiguous regulatory signals into concrete contractual constraints. They typically extend the decision-cycle horizon to absorb due-diligence overhead, so DCNS schedules their engagement at the front of the pathway.',
  'Regulatory / government stakeholders':
    'Compliance-anchored cohort that translates ambient regulatory friction into hard constraints on the DCNS pathway. Misalignment here can stretch the decision-cycle horizon by two-to-three times its baseline.',
  'Business-community stakeholders':
    'Network-amplification cohort whose alignment broadcasts the change-initiative narrative across the broader ecosystem. Their endorsement materially lowers acquisition and partnership friction in the later decision cycles.',
  'Capital / sponsor stakeholders':
    'ROI-anchored cohort that gates financing for the initiative. Clear alignment unlocks resource pools; sustained dissent forces re-scoping or descope cycles that ripple back through every dependent stakeholder tier.',
  'K-12 education stakeholders':
    'Community-impact cohort focused on student and family outcomes. They amplify the narrative powerfully when framing emphasizes the long-term workforce pipeline, and disengage from initiatives positioned in purely commercial language.',
  'Faith / community stakeholders':
    'Local-trust cohort whose alignment lends grassroots legitimacy to the initiative. Sustained dissent generates persistent social-friction noise that bleeds into other stakeholder tiers and is hard to dampen mid-cycle.',
  'Media stakeholders':
    'Narrative-amplification cohort that converts internal change signals into external visibility. High leverage on public sentiment, low tolerance for inconsistent messaging — a single off-narrative cycle can invert their amplification effect.',
};

/**
 * Build a leadership-meta one-liner to surface as the tool-call card body.
 *
 * Used in two situations:
 *   1. Tools in `TOOLS_WITH_SYNTHESIZED_BODY` (maps_*) — we always replace
 *      the raw result with a leadership-framed summary, because the raw
 *      result is physical-world prose that breaks the meta framing.
 *      For `maps_search_places` the body is selected from
 *      `STAKEHOLDER_BODY` keyed by the rewritten cohort label so adjacent
 *      cards never read identically.
 *   2. Any tool whose raw result is empty / trivial (just the tool name or
 *      the string "Done") — we substitute the tool's `intent` line from
 *      `TOOL_DISPLAY` so the card never reads as visually empty.
 *
 * Returns `''` only when the tool is unknown AND has no intent — in which
 * case the caller should hide the body div.
 */
export function synthesizeLeadershipBody(
  toolName: string | undefined | null,
  rewrittenLabel: string,
): string {
  if (!toolName) return '';
  const base = toolName.split(':')[0].trim();

  if (base === 'maps_search_places') {
    // Look up the cohort-specific body. The lookup is case-insensitive to
    // tolerate slight casing drift from the planner's prompts.
    const key = (rewrittenLabel || '').trim();
    if (key) {
      const direct = STAKEHOLDER_BODY[key];
      if (direct) return direct;
      const lc = key.toLowerCase();
      for (const [k, v] of Object.entries(STAKEHOLDER_BODY)) {
        if (k.toLowerCase() === lc) return v;
      }
    }
    // Unmatched cohort (literal venue query that no regex caught) — give
    // it a generic but still leadership-framed body rather than going empty.
    const subject = key || 'Stakeholder venues';
    return `**${subject}** anchored in the change-initiative influence graph as an ECSI contention node. The DCNS engine will weight this venue when computing engagement-friction edges between stakeholder groups.`;
  }
  if (base === 'maps_lookup_weather') {
    return 'Ambient environmental signal captured and folded into the cost-vector that governs each stakeholder\u2019s resource availability for the next decision cycle. High-pressure readings tighten budgets and shrink the willingness-to-change window; low-pressure readings relax both.';
  }
  if (base === 'maps_compute_routes') {
    return 'Inter-stakeholder engagement-friction edges measured. The DCNS engine multiplies these distances into the influence graph so groups separated by high travel-time or low-bandwidth channels exert weaker alignment force on each other than co-located peers.';
  }

  // Fallback for any other tool: surface the leadership intent so the card
  // is never visually empty, even when the tool emits no result body.
  const entry = TOOL_DISPLAY[base];
  return entry?.intent ?? '';
}

/**
 * Whether a tool's raw result body should be replaced with the synthesized
 * leadership-meta line.  Currently the maps_* tools.  Kept as a public helper
 * so the chat-display service can detect-and-replace without depending on
 * the internal set.
 */
export function shouldSynthesizeBody(
  toolName: string | undefined | null,
): boolean {
  if (!toolName) return false;
  const base = toolName.split(':')[0].trim();
  return TOOLS_WITH_SYNTHESIZED_BODY.has(base);
}

/**
 * Apply the safety-net regex rewrites to a tool-result body string.
 *
 * Idempotent.  Safe to call on text that has no marathon vocabulary —
 * returns the input unchanged in that case.
 */
export function applyResultRewrites(text: string | undefined | null): string {
  if (!text) return '';
  let out = text;
  for (const [pattern, replacement] of RESULT_REWRITES) {
    out = out.replace(pattern, replacement);
  }
  return out;
}
