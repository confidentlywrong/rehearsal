-- Copyright 2026 Google LLC
-- Modifications copyright 2026 Christian Haeussinger
-- Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

-- Local development schema for planner_with_memory agent.
-- Used by the local Postgres container (docker-compose.yml postgres service)
-- AND by the production Cloud Build `schema-migration` step (which applies
-- this file via cloud-sql-proxy + psql against the `agent_memory` Cloud SQL
-- database on every deploy).
--
-- All statements MUST be idempotent (CREATE ... IF NOT EXISTS, ADD COLUMN
-- IF NOT EXISTS) so re-applying on every deploy is a no-op when the schema
-- is already in place.
--
-- Differences from schema.sql (AlloyDB production):
--   - Skips CREATE EXTENSION google_ml_integration (AlloyDB-only)
--   - Skips CALL ai.initialize_embeddings(...) (AlloyDB-only)
--   - Uses standard pgvector extension for the embedding VECTOR column
--
-- The get_local_and_traffic_rules tool will fail gracefully locally
-- (returns {"status": "error", "message": "..."}) since ai.embedding()
-- is not available. All route persistence tools work identically.

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- 1. RULES TABLE  (vector column present; search disabled locally)
-- ============================================================

CREATE TABLE IF NOT EXISTS rules (
    source_file VARCHAR(255),
    chunk_id    INT,
    city        VARCHAR(100),
    text        TEXT,
    embedding   VECTOR(3072)
);

-- ============================================================
-- 2. PLANNED ROUTES TABLE  (full fidelity locally)
-- ============================================================

CREATE TABLE IF NOT EXISTS planned_routes (
    route_id    VARCHAR(64)  PRIMARY KEY,
    route_data  JSONB        NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    eval_score  FLOAT,
    eval_result JSONB
);

-- Per-user tenancy (SaaS Phase 3). Nullable for backwards compat with
-- pre-tenancy rows (owner_id=NULL is the "unowned legacy" bucket).
-- Scoped reads filter by this column; the covering index keeps the
-- common "list my routes, newest first" query cheap as one Postgres
-- instance accumulates rows from many users.
ALTER TABLE planned_routes
    ADD COLUMN IF NOT EXISTS owner_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_planned_routes_owner_created
    ON planned_routes (owner_id, created_at DESC);

-- ============================================================
-- 3. SIMULATION RECORDS TABLE  (full fidelity locally)
-- ============================================================

CREATE TABLE IF NOT EXISTS simulation_records (
    simulation_id VARCHAR(64)  PRIMARY KEY,
    route_id      VARCHAR(64)  NOT NULL REFERENCES planned_routes(route_id) ON DELETE CASCADE,
    sim_result    JSONB        NOT NULL,
    simulated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_simulation_records_route_id
    ON simulation_records (route_id);

-- ============================================================
-- 4. SIMULATION SUMMARIES TABLE  (semantic recall)
-- ============================================================

CREATE TABLE IF NOT EXISTS simulation_summaries (
    summary_id  VARCHAR(64)  PRIMARY KEY,
    city        VARCHAR(100),
    prompt      TEXT         NOT NULL,
    summary     TEXT         NOT NULL,
    route_id    VARCHAR(64),
    sim_result  JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    embedding   VECTOR(3072)
);

-- Per-user tenancy. Same semantics as on planned_routes above: nullable
-- for backwards compat, covering index for scoped semantic recall.
ALTER TABLE simulation_summaries
    ADD COLUMN IF NOT EXISTS owner_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_simulation_summaries_owner_created
    ON simulation_summaries (owner_id, created_at DESC);

-- ============================================================
-- 5. LEADERSHIP TABLES  (leadership_planner_with_memory agent)
-- ============================================================
-- Mirrors the marathon tables above but with leadership-domain names
-- (scenarios instead of routes) AND includes per-user `owner_id`
-- tenancy columns + covering indexes from the start.
--
-- Added 2026-06-19 to fix a latent prod drift bug: the leadership
-- agent's Python store_alloydb.py expects these tables + columns to
-- exist, but earlier deploys only created the marathon tables. The
-- first leadership-agent query in prod would otherwise fail with
--   ERROR: relation "leadership_scenarios" does not exist
-- (or, after partial migration:
--   ERROR: column "owner_id" of relation ... does not exist).
--
-- Every statement is idempotent and safe to re-apply on every deploy.

CREATE TABLE IF NOT EXISTS leadership_scenarios (
    scenario_id    VARCHAR(64)  PRIMARY KEY,
    scenario_data  JSONB        NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    eval_score     FLOAT,
    eval_result    JSONB
);

-- persona-aware filtering (added later via ALTER so existing
-- pre-persona deploys converge to the same shape).
ALTER TABLE leadership_scenarios
    ADD COLUMN IF NOT EXISTS persona VARCHAR(50);

-- Per-user tenancy (SaaS Phase 3). Nullable for backwards-compat
-- with single-tenant local dev (owner_id=NULL reads). Scoped reads
-- filter by this column; the covering index keeps the common
-- "list my scenarios, newest first" query cheap as one Postgres
-- instance accumulates rows from many users.
ALTER TABLE leadership_scenarios
    ADD COLUMN IF NOT EXISTS owner_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_leadership_scenarios_owner_created
    ON leadership_scenarios (owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS leadership_simulation_records (
    simulation_id  VARCHAR(64)  PRIMARY KEY,
    scenario_id    VARCHAR(64)  NOT NULL
        REFERENCES leadership_scenarios(scenario_id) ON DELETE CASCADE,
    sim_result     JSONB        NOT NULL,
    simulated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- FK lookup index: get_scenario joins by scenario_id.
CREATE INDEX IF NOT EXISTS idx_leadership_sim_records_scenario_id
    ON leadership_simulation_records (scenario_id);


