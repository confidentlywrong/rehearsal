# Stakeholder Personas

A persona in The Rehearsal describes a position in a company and the daily work done there.

Build each persona from conversations with people who hold that role, stripped of anything that identifies a real person or company. An account manager who has priced the same lanes for years hears *"faster quotes, fewer errors, tighter margins"* differently from someone hired last month. That difference is what a persona should capture.

## Rules for Writing Personas

1. **Describe the position and daily work.** Keep personas free of assumptions about age, gender, nationality or temperament. Describe the daily work, the stakes and the language that carries weight on that side of the table.
2. **Never include confidential or identifying details.** Never include real names, company names or confidential text in a persona, an example or an issue.
3. **Name the file after the industry and role**, for example `logistics-account-manager.md` or `manufacturing-works-council.md`. Translations of existing personas are welcome as separate files with a language suffix, for example `logistics-account-manager.de.md`.

## Canonical Persona Template

Copy the template below when creating a new persona file in `personas/`.

```markdown
# [Role] in [industry]

## Daily work
What this role does on an ordinary day, in concrete terms.

## What a change would cost
Time, standing, skills that lose value, control over their own work.

## What a change could offer
Less repetitive work, better information, recognition, new skills.

## Words that carry weight
Terms this group uses with a meaning outsiders may miss.

## What they ask first
The first question this group raises when a change is announced.

## Source
How this persona was built, for example "four interviews with account
managers at two logistics companies, 2026". No names, no company names.
```

## Included Examples

- [`logistics-account-manager.md`](https://github.com/confidentlywrong/rehearsal/blob/main/personas/logistics-account-manager.md) covers an account manager in logistics, based on the composite case in [Confidently Wrong](https://www.amazon.com/dp/B0HP61JD82).

## Connecting Personas to Simulation Scenarios

- **Interactive Rehearsal on port `9119`** lets you paste a persona description alongside your rollout draft directly in the chat panel or upload role notes into the scenario workspace so `agents/leadership_planner_with_eval` and `agents/leadership_planner_with_memory` ground the rehearsal around your groups.
- **Multi-Stakeholder Simulation Scenarios in `configs/quests/*.yaml`** run tick-by-tick rehearsals across three organizational tiers using the `agents/ai_echo`, `agents/ai_echo_reactive` and `agents/ai_echo_autopilot` persona agents, where each stakeholder entry defines `tier`, `persona`, `domain_principles`, `initial_sentiment`, `initial_resistance` and `initial_influence`.
