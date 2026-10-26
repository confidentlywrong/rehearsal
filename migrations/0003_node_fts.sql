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

-- migrations/0003_node_fts.sql
--
-- Phase 3 retrieval (concept §4-Phase 3, §6.1) needs full-text search
-- over `analysis_nodes.payload` so the memory retrieval layer can locate
-- seed nodes from a free-text scenario question. This migration
-- adds a generated `search_vector` column derived from the small set
-- of well-known JSONB payload fields the bin schemas use (see the
-- contract block at the bottom of 0002_analyses.sql), plus a GIN
-- index that makes `@@ to_tsquery(...)` plans bounded.
--
-- Why a generated column rather than query-time `to_tsvector`:
--   • The bin payload contract is LOCKED (§5.3 + 0002 footer). The
--     fields below are stable.
--   • Retrieval latency must be predictable. A GIN index over a
--     generated tsvector gives O(matching-rows) lookups; query-time
--     `to_tsvector(payload::text)` would re-tokenize on every read.
--   • Generated columns are PG13+; both prod (Cloud SQL Postgres 14+)
--     and the local docker compose service are well past that.
--
-- Why ENGLISH config: every seed document we ingest in v1 is operator-
-- written English business prose. If we ever ingest multi-language
-- transcripts we will add a `payload_lang` field and switch to it via
-- `regconfig`; that day is not today.
--
-- Idempotency: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS,
-- so re-running on every gateway cold-start is safe. The multi-file
-- runner in internal/account/migrate.go applies this file AFTER
-- 0002_analyses.sql in lexical order.

BEGIN;

-- A generated tsvector over the well-known payload fields. We
-- COALESCE every extraction with '' so a row whose payload is missing
-- one of the fields still gets a vector (the missing field just
-- contributes no tokens). Numeric/boolean fields are ignored — they
-- have no business in a text-search index.
--
-- Fields covered (across all 11 labels in §5.3):
--   Actor          → name, role, team
--   Belief         → statement
--   Friction       → summary
--   Outcome        → summary
--   Transformation → name, title, intent
--   Structure      → kind
--   Culture        → norm
--   Ecology        → setting
--   External       → name, kind
--   Document       → kind
--   Learning       → rationale, summary
--
-- The label name itself is included so a scenario question that
-- literally says "show me the frictions" hits friction nodes.

ALTER TABLE analysis_nodes
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english',
            coalesce(payload->>'name', '') || ' ' ||
            coalesce(payload->>'title', '')
        ), 'A') ||
        setweight(to_tsvector('english',
            coalesce(payload->>'statement', '') || ' ' ||
            coalesce(payload->>'summary', '')
        ), 'B') ||
        setweight(to_tsvector('english',
            coalesce(payload->>'role', '') || ' ' ||
            coalesce(payload->>'team', '') || ' ' ||
            coalesce(payload->>'intent', '') || ' ' ||
            coalesce(payload->>'norm', '') || ' ' ||
            coalesce(payload->>'setting', '') || ' ' ||
            coalesce(payload->>'kind', '') || ' ' ||
            coalesce(payload->>'rationale', '')
        ), 'C') ||
        setweight(to_tsvector('english', label), 'D')
    ) STORED;

-- GIN is the right index for tsvector @@ tsquery lookups. Without
-- this index the retriever falls back to a seq scan, which is fine
-- for a 200-node analysis but predictably bad as analyses grow.
--
-- We deliberately do NOT make this a composite (analysis_id,
-- search_vector) index — that would require the btree_gin extension
-- and a superuser-level CREATE EXTENSION. Postgres can bitmap-AND
-- this GIN with the existing `analysis_nodes_live_idx` btree, which
-- is the same plan the planner picks anyway on small analyses.
--
-- The partial-WHERE narrows the index to the same hot path the
-- retriever cares about: live, accepted, not-rejected rows. Pending
-- candidates do not need to be searchable in Phase 3.
CREATE INDEX IF NOT EXISTS analysis_nodes_search_idx
    ON analysis_nodes
    USING GIN (search_vector)
    WHERE valid_until IS NULL
      AND accepted_at IS NOT NULL
      AND rejected_at IS NULL;

COMMIT;
