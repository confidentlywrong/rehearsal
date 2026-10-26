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

-- migrations/0006_document_chunks.sql
--
-- Document chunking for RAG over uploaded seed documents.
-- Each uploaded document is split into overlapping text chunks (~500
-- tokens each) and embedded using gemini-embedding-001 (3072 dim).
--
-- The planner's retrieve_document_passages tool queries this table
-- via vector similarity, scoped to the current analysis.
--
-- Design notes:
--   - analysis_id is intentionally denormalised from analysis_documents.
--     RetrieveChunks queries by analysis_id without JOINing
--     analysis_documents, so the extra column saves a join on every
--     retrieval call. See implementation_plan_memory_phase2.md R3.
--   - ON DELETE CASCADE from analysis_documents means deleting a
--     document auto-cleans its chunks. No orphan management needed.
--   - No vector index: pgvector ≤0.8.x caps both ivfflat and hnsw at
--     2000 dimensions, but gemini-embedding-001 produces 3072-dim
--     vectors. At the expected scale (hundreds of chunks per analysis)
--     a sequential scan is fast enough. Add an hnsw index after
--     upgrading pgvector to ≥0.9.0 (which raises the limit to 4000).

BEGIN;

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID        NOT NULL REFERENCES analysis_documents(id) ON DELETE CASCADE,
    analysis_id UUID        NOT NULL,
    chunk_index INT         NOT NULL,
    chunk_text  TEXT        NOT NULL,
    embedding   VECTOR(3072),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Vector similarity index intentionally omitted — see header comment.

CREATE INDEX IF NOT EXISTS idx_document_chunks_analysis
    ON document_chunks (analysis_id);

CREATE INDEX IF NOT EXISTS idx_document_chunks_document
    ON document_chunks (document_id);

COMMIT;
