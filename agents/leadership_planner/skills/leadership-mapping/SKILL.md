---
name: leadership-mapping
description: >
  Use when designing leadership scenarios that should reference real-world
  Las Vegas geography: looking up corporate offices, government buildings,
  hospitals, and other venues that match stakeholder domains. Also computes
  engagement friction between venues and checks weather conditions for
  environmental pressure modifiers. Triggered when the agent needs
  real-world venue grounding for stakeholder placement.
license: Apache-2.0
---

# Leadership Mapping

You are an expert at utilizing Google Maps Grounding Lite MCP tools to ensure
leadership scenarios are grounded in precise, real-world Las Vegas data.

## Available Tools

- `maps_search_places`: Identify real-world Las Vegas venues matching
  stakeholder domains — corporate headquarters, courthouses, hospitals,
  tech centers, community centers, government buildings. Use this to anchor
  each stakeholder persona to a semantically meaningful location.
- `maps_compute_routes`: Calculate actual travel durations between stakeholder
  venues. Travel time quantifies **engagement friction** — how difficult it
  is to get two stakeholders aligned. Co-located stakeholders (≤5 min) form
  natural coalitions; distant ones (20+ min) require deliberate bridging.
- `maps_lookup_weather`: Check Las Vegas weather to determine **environmental
  pressure modifiers** that affect simulation cost vectors. Extreme heat
  creates a "burning platform" (faster but costlier decisions), storms
  increase global resistance, high wind makes outcomes unpredictable.

## Stakeholder-to-Venue Mapping

Use these queries to find appropriate venues for each stakeholder domain.

> **IMPORTANT**: Las Vegas is dominated by gaming/hospitality. When searching
> for non-hospitality stakeholders, use `excludedPrimaryTypes: ["hotel",
> "casino", "lodging"]` to avoid casino results. The real corporate power
> in Las Vegas sits at Hughes Center, Downtown Summerlin, and the I-215
> corridor — NOT on the Strip (unless the stakeholder is in hospitality).

| Stakeholder Domain | Suggested Search Query | Real LV Fallback |
|---|---|---|
| CEO / Board / Executive | "corporate office Las Vegas" or "Hughes Center Las Vegas" | Hughes Center, 3800 Howard Hughes Pkwy |
| CFO / Finance | "financial office Las Vegas" or "bank headquarters Las Vegas" | Bank of America Plaza, Hughes Center |
| CTO / Engineering | "technology center Las Vegas" or "UNLV engineering" | Switch SuperNAP or UNLV Research Park |
| HR / People Operations | "community center Las Vegas" or "training facility Las Vegas" | — |
| Legal / Compliance | "courthouse Las Vegas" or "Clark County government" | Clark County Regional Justice Center, 200 Lewis Ave |
| External Stakeholders | "city hall Las Vegas" or "public park Las Vegas" | Las Vegas City Hall, 495 S Main St |
| Crisis Response | "hospital Las Vegas" or "emergency services Las Vegas" | UMC (University Medical Center), 1800 W Charleston |
| Union / Labor | "culinary union Las Vegas" or "labor union Las Vegas" | Culinary Workers Union Local 226, 5241 Spring Mountain Rd |
| Hospitality Executive | "MGM Resorts headquarters" or "Wynn corporate office" | MGM Resorts International, 6385 S Rainbow Blvd |

## Venue Type Classification

After discovering venues, classify each into one of these categories based on
the Places API `types` field:

| Category | Places API types | Simulation meaning |
|---|---|---|
| `government` | government_office, courthouse, city_hall | Formal processes, high institutional weight |
| `university` | university, school, research_institute | Research-oriented, open to innovation |
| `corporate_tower` | corporate_office, office_building | Standard corporate engagement (baseline) |
| `resort_corporate` | hotel, casino, lodging (corporate offices) | Hospitality deal-making culture, slightly faster, entertainment-world energy |
| `startup` | coworking_space, technology_park | Fast-moving, risk-tolerant culture |
| `hospital` | hospital, fire_station, emergency | Crisis urgency, high emotional cost |
| `union_hall` | community_center, labor_union | Collective process, grassroots morale |

> **Classifying Strip venues**: If the Places API returns a venue with types
> `hotel` or `casino` but the stakeholder is a corporate executive (CEO, CFO),
> classify it as `resort_corporate` — NOT as a generic hotel. The gaming
> industry's corporate offices are legitimate power centers in Las Vegas.

## Weather-to-Modifier Mapping

Translate weather conditions into a `weather_modifier` dict:

| Condition | time_factor | capital_factor | morale_factor | resistance_delta | perturbation_range |
|---|---|---|---|---|---|
| Extreme heat (>100°F) | 0.8 | 1.20 | 1.3 | 0.0 | [0.7, 1.5] |
| Storm / heavy rain | 1.2 | 1.15 | 1.1 | 0.05 | [0.7, 1.5] |
| High wind | 1.0 | 1.10 | 1.0 | 0.0 | [0.5, 2.0] |
| Fog / overcast | 1.05 | 1.08 | 1.1 | 0.0 | [0.7, 1.5] |
| Mild / clear | 1.0 | 1.0 | 1.0 | 0.0 | [0.7, 1.5] |

Also set `distance_factor`: 1.15 for fog, 1.0 for all others.
Set `condition` to one of: extreme_heat, storm, high_wind, fog, clear.
Set `narrative` to a brief description for the scenario text.
