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

-- migrations/0002_analyses.sql
--
-- Organizational Memory & Learning v1.
-- Spec: concepts/organizational_memory_and_learning.md §5.1
--
-- Three new tables that form the canonical, Postgres-native store for the
-- six-phase memory/learning workflow. Postgres is the system of record in
-- every deployment, with or without the optional Neo4j projection.
--
-- Multi-tenant key: every row hangs off `analysis_id` (a UUID) which itself
-- hangs off `operator_id` (the Firebase UID of the consultant who owns the
-- analysis). No row in this migration is shared across operators. The
-- §7 cross-analysis opt-in is a query-time flag, not a storage difference.
--
-- Append-only / time-travel: nodes and edges carry (valid_from, valid_until)
-- so Phase 6 institutionalization never destroys prior state. Rejected
-- candidates remain in place (soft-rejected via rejected_at) so the operator
-- can revert.
--
-- Idempotency: every statement is CREATE ... IF NOT EXISTS so re-running on
-- every service cold-start is safe. The runner in internal/account/migrate.go
-- applies this file after 0001_users.sql in lexical order.

BEGIN;

-- --- analyses ---------------------------------------------------------------
-- One row = one operator's working model of one organization. The unit of
-- tenancy below users. Many analyses per operator; one organization may
-- have several analyses across different change initiatives.

CREATE TABLE IF NOT EXISTS analyses (
    id                 UUID        PRIMARY KEY,
    operator_id        TEXT        NOT NULL REFERENCES users(id),
    org_name           TEXT,
    title              TEXT        NOT NULL,
    description        TEXT,
    -- §7 cross-analysis learning opt-in. Per LOCKED decision §12.3,
    -- flipping this ON is NON-RETROACTIVE: only learnings produced
    -- after the flip enter the operator's pool. Older analyses must be
    -- toggled individually.
    shared_learnings   BOOLEAN     NOT NULL DEFAULT false,
    -- shared_learnings_set_at carries the timestamp of the most
    -- recent TRUE flip on the shared_learnings column. The Phase 3
    -- pool-query (§7.3) filters joined :Learning rows on
    -- `created_at >= shared_learnings_set_at` to enforce the
    -- non-retroactive contract; clearing the column (on flip to
    -- FALSE) means "this analysis is currently not contributing".
    shared_learnings_set_at TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS analyses_operator_idx
    ON analyses(operator_id)
    WHERE deleted_at IS NULL;

-- Hot path: "give me this operator's analyses that contribute to the
-- shared-learnings pool". Used by Phase 3 retrieval when the current
-- analysis itself has shared_learnings = true.
CREATE INDEX IF NOT EXISTS analyses_shared_pool_idx
    ON analyses(operator_id)
    WHERE shared_learnings = true AND deleted_at IS NULL;

-- --- analysis_documents -----------------------------------------------------
-- Seed documents the operator uploaded for Phase 1 extraction. Every
-- extracted node/edge points back here via source_doc_id (provenance edge
-- in §5.4; never expires). storage_uri is a GCS object path in cloud
-- mode or a local file path in dev.

CREATE TABLE IF NOT EXISTS analysis_documents (
    id           UUID        PRIMARY KEY,
    analysis_id  UUID        NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    kind         TEXT        NOT NULL,        -- 'memo' | 'transcript' | 'org_chart' | 'post_mortem' | 'okr' | 'interview_notes'
    filename     TEXT        NOT NULL,
    sha256       TEXT        NOT NULL,
    storage_uri  TEXT        NOT NULL,
    byte_size    BIGINT,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS analysis_documents_analysis_idx
    ON analysis_documents(analysis_id);

-- Dedup hint: same operator re-uploading the same file should be detected
-- at the application layer via this index. We do NOT enforce uniqueness
-- because re-ingesting a corrected version of the same document is a
-- legitimate workflow.
CREATE INDEX IF NOT EXISTS analysis_documents_sha256_idx
    ON analysis_documents(analysis_id, sha256);

-- --- analysis_nodes ---------------------------------------------------------
-- One row per memory element. `label` is one of the Walsh-Ungson bin
-- labels (see §5.3). `payload` is bin-specific JSON so we don't fragment
-- into 10+ tables in v1 — application-layer validation enforces the shape.
--
-- governing is meaningful only when label = 'Belief'. The LOCKED decision
-- §12.2 forbids the LLM from setting this; it MUST be set by the operator
-- during Phase 2 review. The extractor may write a hint into
-- payload->>'governing_suggested' but governing itself stays false until
-- the operator confirms.

CREATE TABLE IF NOT EXISTS analysis_nodes (
    id              UUID        PRIMARY KEY,
    analysis_id     UUID        NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    label           TEXT        NOT NULL,        -- 'Actor' | 'Belief' | 'Friction' | 'Outcome' | 'Transformation' | 'Structure' | 'Culture' | 'Ecology' | 'External' | 'Document' | 'Learning'
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    governing       BOOLEAN     NOT NULL DEFAULT false,
    confidence      NUMERIC(3,2),
    source_doc_id   UUID        REFERENCES analysis_documents(id),
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ,
    accepted_at     TIMESTAMPTZ,
    accepted_by     TEXT        REFERENCES users(id),
    rejected_at     TIMESTAMPTZ,
    rejected_by     TEXT        REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Hot path A: Phase 3 retrieval for currently-valid nodes of a given bin.
CREATE INDEX IF NOT EXISTS analysis_nodes_live_idx
    ON analysis_nodes(analysis_id, label)
    WHERE valid_until IS NULL AND accepted_at IS NOT NULL AND rejected_at IS NULL;

-- Hot path B: Phase 2 review queue ("show me pending candidates").
CREATE INDEX IF NOT EXISTS analysis_nodes_pending_idx
    ON analysis_nodes(analysis_id, label)
    WHERE accepted_at IS NULL AND rejected_at IS NULL;

-- Hot path C: governing-belief filter for double-loop reflection.
CREATE INDEX IF NOT EXISTS analysis_nodes_governing_idx
    ON analysis_nodes(analysis_id)
    WHERE governing = true AND valid_until IS NULL;

-- --- analysis_edges ---------------------------------------------------------
-- Same temporal model as analysis_nodes. Edge labels come from §5.4.
-- Provenance edges (DERIVED_FROM Document) never get a valid_until.

CREATE TABLE IF NOT EXISTS analysis_edges (
    id              UUID        PRIMARY KEY,
    analysis_id     UUID        NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    label           TEXT        NOT NULL,        -- 'REPORTS_TO' | 'HOLDS_BELIEF' | 'BLOCKED_BY' | 'CONTRADICTS' | 'DERIVED_FROM' | ...
    src_node_id     UUID        NOT NULL REFERENCES analysis_nodes(id) ON DELETE CASCADE,
    dst_node_id     UUID        NOT NULL REFERENCES analysis_nodes(id) ON DELETE CASCADE,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    confidence      NUMERIC(3,2),
    source_doc_id   UUID        REFERENCES analysis_documents(id),
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ,
    accepted_at     TIMESTAMPTZ,
    accepted_by     TEXT        REFERENCES users(id),
    rejected_at     TIMESTAMPTZ,
    rejected_by     TEXT        REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 3 retrieval: "give me all live outbound edges from this node".
CREATE INDEX IF NOT EXISTS analysis_edges_src_live_idx
    ON analysis_edges(analysis_id, src_node_id)
    WHERE valid_until IS NULL AND accepted_at IS NOT NULL AND rejected_at IS NULL;

-- Symmetric: inbound traversal.
CREATE INDEX IF NOT EXISTS analysis_edges_dst_live_idx
    ON analysis_edges(analysis_id, dst_node_id)
    WHERE valid_until IS NULL AND accepted_at IS NOT NULL AND rejected_at IS NULL;

-- Phase 2 review queue for edges.
CREATE INDEX IF NOT EXISTS analysis_edges_pending_idx
    ON analysis_edges(analysis_id, label)
    WHERE accepted_at IS NULL AND rejected_at IS NULL;

-- --- payload shape contract (informational, not enforced) -------------------
--
-- We deliberately do NOT add a CHECK constraint that validates JSONB
-- payload shape per label. Postgres CHECKs on JSONB are clumsy and we
-- want the application layer (internal/memory) to own validation so
-- it can produce structured 400 errors. Below is the contract every
-- writer MUST follow; readers should be defensive.
--
-- Actor          : {"name": text, "role": text, "seniority": text?, "formal_authority": text?}
-- Belief         : {"statement": text, "governing_suggested": bool?}
-- Friction       : {"summary": text, "intensity": text?}
-- Outcome        : {"summary": text, "simulated": bool, "actual": bool}
-- Transformation : {"name": text, "intent": text, "status": text, "started_at": timestamp?, "ended_at": timestamp?}
-- Structure      : {"kind": text, "members": [text]}
-- Culture        : {"norm": text, "intensity": text?, "scope": text?}
-- Ecology        : {"setting": text, "attribute": text?}
-- External       : {"kind": text, "name": text}
-- Document       : {"kind": text, "sha256": text}  -- mirrors analysis_documents row
-- Learning       : {"single_or_double": "A"|"B", "source_run_id": text, "rationale": text}

COMMIT;
