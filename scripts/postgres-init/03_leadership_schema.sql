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

-- Add owner_id column for per-user tenancy (SaaS Phase 3).
ALTER TABLE leadership_scenarios ADD COLUMN IF NOT EXISTS owner_id VARCHAR(64);

-- Covering index for "list my scenarios, newest first" scans.
CREATE INDEX IF NOT EXISTS idx_leadership_scenarios_owner_created
    ON leadership_scenarios (owner_id, created_at DESC);


