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

-- Stage 2C: drop the legacy node/edge/proposal substrate.
-- Keeper tables (analyses, analysis_documents, document_chunks) are untouched.
-- No keeper table has FK references to these tables.
-- Deploy new code FIRST, then run this migration.

DROP TABLE IF EXISTS analysis_proposals CASCADE;
DROP TABLE IF EXISTS analysis_edges    CASCADE;
DROP TABLE IF EXISTS analysis_nodes    CASCADE;
