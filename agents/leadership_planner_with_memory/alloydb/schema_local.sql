-- Copyright 2026 The Confidently Wrong Authors
--
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- Local development schema for leadership_planner_with_memory agent.
-- Used by the local Postgres container (docker-compose.yml postgres service).
--
-- Mirrors the marathon planner_with_memory schema but with leadership-domain
-- table names and columns (scenarios instead of routes).

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- 1. LEADERSHIP SCENARIOS TABLE  (mirrors planned_routes)
-- ============================================================

CREATE TABLE IF NOT EXISTS leadership_scenarios (
    scenario_id       VARCHAR(64)  PRIMARY KEY,
    scenario_data     JSONB        NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    eval_score        FLOAT,
    eval_result       JSONB
);

-- ============================================================
-- 2. LEADERSHIP SIMULATION RECORDS TABLE  (mirrors simulation_records)
-- ============================================================

CREATE TABLE IF NOT EXISTS leadership_simulation_records (
    simulation_id  VARCHAR(64)  PRIMARY KEY,
    scenario_id    VARCHAR(64)  NOT NULL REFERENCES leadership_scenarios(scenario_id) ON DELETE CASCADE,
    sim_result     JSONB        NOT NULL,
    simulated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- FK lookup index: get_scenario joins leadership_simulation_records by scenario_id.
CREATE INDEX IF NOT EXISTS idx_leadership_sim_records_scenario_id
    ON leadership_simulation_records (scenario_id);

-- Add persona column to leadership_scenarios for persona-aware filtering.
ALTER TABLE leadership_scenarios ADD COLUMN IF NOT EXISTS persona VARCHAR(50);

-- Per-user tenancy (SaaS Phase 3). Nullable: existing rows stay unowned
-- and remain visible to single-tenant callers (owner_id=None reads).
-- Scoped reads filter by this column. The covering index keeps the
-- common "list my scenarios, newest first" query cheap even as a single
-- Postgres instance accumulates rows from many users.
ALTER TABLE leadership_scenarios ADD COLUMN IF NOT EXISTS owner_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_leadership_scenarios_owner_created
    ON leadership_scenarios (owner_id, created_at DESC);


-- ============================================================
-- 5. LEADERSHIP STAKEHOLDER ARTIFACTS TABLE
--    (Artifacts 1-4 from leadership_planner_with_memory)
-- ============================================================
-- Persists the six state-key payloads produced by the emit_* tools
-- (stakeholder_graph, stakeholder_personas, stakeholder_reactions,
-- simulation_timeline, simulation_report, stakeholder_interview)
-- so the user can revisit a past scenario and the agent can rehydrate
-- the full card stack without re-running the simulation.
--
-- One row per scenario (1:1 with leadership_scenarios). Each artifact
-- column is nullable so the agent can call store_stakeholder_artifacts
-- before all six artifacts have fired (partial captures are preserved).

CREATE TABLE IF NOT EXISTS leadership_stakeholder_artifacts (
    scenario_id           VARCHAR(64)  PRIMARY KEY
        REFERENCES leadership_scenarios(scenario_id) ON DELETE CASCADE,
    stakeholder_graph     JSONB,
    stakeholder_personas  JSONB,
    stakeholder_reactions JSONB,
    simulation_timeline   JSONB,
    simulation_report     JSONB,
    stakeholder_interview JSONB,
    owner_id              VARCHAR(64),
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_leadership_artifacts_owner_updated
    ON leadership_stakeholder_artifacts (owner_id, updated_at DESC);
