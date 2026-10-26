// Copyright 2026 Christian Haeussinger
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Package memory is the per-analysis Organizational Memory & Learning
// data accessor.
//
// Concept: concepts/organizational_memory_and_learning.md §5.1.
// Schema:  migrations/0002_analyses.sql.
//
// Design choices locked in 2026-06-26:
//
//   - **Tenancy is enforced once per request at the middleware layer.**
//     A request handler verifies that the caller (operator) owns the
//     analysis_id on the URL and stashes the verified analysis on the
//     request context. Methods on Store accept analysisID and operatorID
//     and re-check `operator_id = $operator AND deleted_at IS NULL` on
//     every read, but the *authorization decision* is the middleware's.
//     This guarantees that a stolen analysis_id alone cannot leak data
//     even if a handler forgets to call the middleware.
//
//   - **Soft-delete is hide-but-keep.** SetDeleted stamps
//     analyses.deleted_at; child rows (documents, nodes, edges) remain
//     in place. The DB-level ON DELETE CASCADE in 0002_analyses.sql is
//     only relevant for the (currently unused) hard-delete path.
//
//   - **JSONB payload shape is validated at write time** by hand-written
//     per-label validators in validate.go. The DB has no CHECK
//     constraint on shape (see 0002_analyses.sql comment block). Garbage
//     never reaches Postgres.
//
//   - **No transactions** for v1: every write is a single statement and
//     there are no cross-row invariants to preserve atomically. Phase 6
//     institutionalization (which mutates many rows at once) will add a
//     transactional path when it lands.
package memory

import (
	"errors"
	"time"

	"github.com/google/uuid"
)

// --- sentinels --------------------------------------------------------

// ErrNotFound is returned when a lookup finds no row, or when the
// caller's operator_id does not own the requested analysis (the
// store treats unauthorized access as not-found to avoid leaking the
// existence of other operators' data).
var ErrNotFound = errors.New("memory: not found")


// --- domain types -----------------------------------------------------

// Analysis represents one operator's working model or scenario.
// Mirrors the analyses table in migrations/0002_analyses.sql.
type Analysis struct {
	ID                   uuid.UUID
	OperatorID           string
	OrgName              string
	Title                string
	Description          string
	CreatedAt            time.Time
	DeletedAt            *time.Time
}

// Document is one seed document uploaded for Phase 1 extraction.
// Mirrors analysis_documents.
type Document struct {
	ID          uuid.UUID
	AnalysisID  uuid.UUID
	Kind        string
	Filename    string
	SHA256      string
	StorageURI  string
	ByteSize    int64
	IngestedAt  time.Time
}



// --- input types ------------------------------------------------------

// CreateAnalysisInput is the payload for Store.CreateAnalysis. The
// store stamps id and created_at; everything else is the caller's.
type CreateAnalysisInput struct {
	OperatorID  string
	OrgName     string
	Title       string
	Description string
}

// AddDocumentInput is the payload for Store.AddDocument. Storage and
// SHA256 are the caller's responsibility: this layer never touches
// the file itself.
type AddDocumentInput struct {
	Kind       string
	Filename   string
	SHA256     string
	StorageURI string
	ByteSize   int64
}


// UpdateAnalysisInput is the payload for Store.UpdateAnalysis.
type UpdateAnalysisInput struct {
	Title       *string
	Description *string
	OrgName     *string
}


// =====================================================================
// document chunks (Stage 2A — RAG)
// =====================================================================

// ChunkResult is one retrieved document chunk from vector similarity
// search. Returned by Store.RetrieveChunks and surfaced as a passage
// in the retrieve-passages handler.
type ChunkResult struct {
	ChunkID    uuid.UUID
	DocumentID uuid.UUID
	AnalysisID uuid.UUID
	ChunkIndex int
	ChunkText  string
	Score      float64 // cosine similarity score
	CreatedAt  time.Time
}

// StoreChunkInput is one chunk to persist. Produced by the chunk
// handler after splitting a document and embedding each chunk.
type StoreChunkInput struct {
	DocumentID uuid.UUID
	AnalysisID uuid.UUID
	ChunkIndex int
	ChunkText  string
	Embedding  []float32
}
