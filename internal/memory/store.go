// Copyright 2026 Christian Haeussinger. Apache-2.0.

package memory

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// PgxConn is the minimal pgx-style surface the Store needs. Both
// *pgx.Conn and *pgxpool.Pool satisfy it (locked in by the compile
// time assertion in pgxpool_iface_test.go). Tests can pass a fake.
type PgxConn interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
	Begin(ctx context.Context) (pgx.Tx, error)
}

// Store is the per-analysis data accessor for the Organizational
// Memory & Learning model.
//
// Authorization model (concept §6.1, locked 2026-06-26): tenancy is
// the *middleware's* responsibility — every analyses-scoped HTTP
// route verifies that the caller's operator_id owns the analysis_id
// on the URL once per request. The Store nonetheless re-filters
// every read by operator_id (when the caller hands one in) so a
// handler that forgets the middleware cannot leak data; the worst
// case is "looks like ErrNotFound" rather than "leaks other
// operators' memory".
type Store struct {
	db PgxConn
}

// NewStore constructs a Store over db. db may be nil; in that case
// every method returns an error and the surrounding handler should
// surface a 503. (We never silently no-op writes.)
func NewStore(db PgxConn) *Store {
	return &Store{db: db}
}

func (s *Store) ok() error {
	if s == nil || s.db == nil {
		return errors.New("memory: nil store")
	}
	return nil
}

// =====================================================================
// analyses
// =====================================================================

// CreateAnalysis inserts a new row owned by in.OperatorID. The store
// stamps id, created_at, and leaves shared_learnings = false.
func (s *Store) CreateAnalysis(ctx context.Context, in CreateAnalysisInput) (Analysis, error) {
	if err := s.ok(); err != nil {
		return Analysis{}, err
	}
	in.OperatorID = strings.TrimSpace(in.OperatorID)
	in.OrgName = strings.TrimSpace(in.OrgName)
	in.Title = strings.TrimSpace(in.Title)
	if in.OperatorID == "" {
		return Analysis{}, errors.New("memory: operator_id required")
	}
	if in.Title == "" {
		return Analysis{}, errors.New("memory: title required")
	}
	id := uuid.New()
	row := s.db.QueryRow(ctx, `
		INSERT INTO analyses (id, operator_id, org_name, title, description)
		VALUES ($1, $2, $3, $4, NULLIF($5, ''))
		RETURNING id, operator_id, org_name, title, COALESCE(description,''),
		          created_at, deleted_at`,
		id, in.OperatorID, in.OrgName, in.Title, in.Description)
	return scanAnalysis(row)
}

// GetAnalysis returns the analysis if and only if it is owned by
// operatorID and not soft-deleted. Returns ErrNotFound otherwise.
func (s *Store) GetAnalysis(ctx context.Context, operatorID string, analysisID uuid.UUID) (Analysis, error) {
	if err := s.ok(); err != nil {
		return Analysis{}, err
	}
	row := s.db.QueryRow(ctx, `
		SELECT id, operator_id, org_name, title, COALESCE(description,''),
		       created_at, deleted_at
		  FROM analyses
		 WHERE id = $1 AND operator_id = $2 AND deleted_at IS NULL`,
		analysisID, operatorID)
	return scanAnalysis(row)
}

// UpdateAnalysis updates the title, description, and/or org_name of an analysis.
func (s *Store) UpdateAnalysis(ctx context.Context, operatorID string, analysisID uuid.UUID, in UpdateAnalysisInput) (Analysis, error) {
	if err := s.ok(); err != nil {
		return Analysis{}, err
	}
	
	var sets []string
	var args []any
	args = append(args, analysisID, operatorID)
	argId := 3
	
	if in.Title != nil {
		sets = append(sets, fmt.Sprintf("title = $%d", argId))
		args = append(args, *in.Title)
		argId++
	}
	if in.Description != nil {
		sets = append(sets, fmt.Sprintf("description = $%d", argId))
		args = append(args, *in.Description)
		argId++
	}
	if in.OrgName != nil {
		sets = append(sets, fmt.Sprintf("org_name = $%d", argId))
		args = append(args, *in.OrgName)
	}
	
	if len(sets) == 0 {
		return s.GetAnalysis(ctx, operatorID, analysisID)
	}
	
	query := fmt.Sprintf(`
		UPDATE analyses SET %s
		WHERE id = $1 AND operator_id = $2 AND deleted_at IS NULL
		RETURNING id, operator_id, org_name, title, COALESCE(description,''), created_at, deleted_at`, strings.Join(sets, ", "))
	return scanAnalysis(s.db.QueryRow(ctx, query, args...))
}

// ListAnalyses returns every non-deleted analysis owned by
// operatorID, newest first. No paging in v1 — handlers should cap.
func (s *Store) ListAnalyses(ctx context.Context, operatorID string) ([]Analysis, error) {
	if err := s.ok(); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(ctx, `
		SELECT id, operator_id, org_name, title, COALESCE(description,''),
		       created_at, deleted_at
		  FROM analyses
		 WHERE operator_id = $1 AND deleted_at IS NULL
		 ORDER BY created_at DESC`, operatorID)
	if err != nil {
		return nil, fmt.Errorf("memory: list analyses: %w", err)
	}
	defer rows.Close()
	var out []Analysis
	for rows.Next() {
		a, err := scanAnalysis(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// SoftDeleteAnalysis stamps deleted_at on an analysis owned by
// operatorID. Child rows (documents, nodes, edges) remain in place
// per the hide-but-keep contract (locked 2026-06-26). Calling on an
// already-deleted analysis is idempotent.
func (s *Store) SoftDeleteAnalysis(ctx context.Context, operatorID string, analysisID uuid.UUID) error {
	if err := s.ok(); err != nil {
		return err
	}
	tag, err := s.db.Exec(ctx, `
		UPDATE analyses
		   SET deleted_at = now()
		 WHERE id = $1 AND operator_id = $2 AND deleted_at IS NULL`,
		analysisID, operatorID)
	if err != nil {
		return fmt.Errorf("memory: soft-delete analysis: %w", err)
	}
	if tag.RowsAffected() == 0 {
		// Either missing, not owned, or already deleted. Probe
		// the deleted-or-foreign case so the handler can return
		// 404 cleanly.
		var exists bool
		probe := s.db.QueryRow(ctx, `
			SELECT EXISTS(
				SELECT 1 FROM analyses
				 WHERE id = $1 AND operator_id = $2)`,
			analysisID, operatorID)
		if perr := probe.Scan(&exists); perr != nil {
			return fmt.Errorf("memory: probe analysis: %w", perr)
		}
		if !exists {
			return ErrNotFound
		}
		// Row exists but is already deleted — idempotent success.
	}
	return nil
}


// =====================================================================
// documents
// =====================================================================

// AddDocument records a seed document for an analysis. The caller
// owns storage_uri (GCS object or local path) and the sha256; this
// layer never touches the file.
func (s *Store) AddDocument(ctx context.Context, operatorID string, analysisID uuid.UUID, in AddDocumentInput) (Document, error) {
	if err := s.ok(); err != nil {
		return Document{}, err
	}
	// Defence-in-depth tenancy check: refuse to attach a doc to an
	// analysis the operator does not own. Middleware should have
	// caught this already.
	if _, err := s.GetAnalysis(ctx, operatorID, analysisID); err != nil {
		return Document{}, err
	}
	in.Kind = strings.TrimSpace(in.Kind)
	in.Filename = strings.TrimSpace(in.Filename)
	in.SHA256 = strings.TrimSpace(in.SHA256)
	in.StorageURI = strings.TrimSpace(in.StorageURI)
	if in.Kind == "" || in.Filename == "" || in.SHA256 == "" || in.StorageURI == "" {
		return Document{}, errors.New("memory: kind, filename, sha256, storage_uri all required")
	}
	id := uuid.New()
	row := s.db.QueryRow(ctx, `
		INSERT INTO analysis_documents (id, analysis_id, kind, filename, sha256, storage_uri, byte_size)
		VALUES ($1, $2, $3, $4, $5, $6, NULLIF($7, 0)::bigint)
		RETURNING id, analysis_id, kind, filename, sha256, storage_uri,
		          COALESCE(byte_size, 0), ingested_at`,
		id, analysisID, in.Kind, in.Filename, in.SHA256, in.StorageURI, in.ByteSize)
	return scanDocument(row)
}

// ListDocuments returns every document attached to an analysis the
// operator owns. Oldest first (ingestion order matters for citation
// timeline in the Interpret UI).
func (s *Store) ListDocuments(ctx context.Context, operatorID string, analysisID uuid.UUID) ([]Document, error) {
	if err := s.ok(); err != nil {
		return nil, err
	}
	if _, err := s.GetAnalysis(ctx, operatorID, analysisID); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(ctx, `
		SELECT id, analysis_id, kind, filename, sha256, storage_uri,
		       COALESCE(byte_size, 0), ingested_at
		  FROM analysis_documents
		 WHERE analysis_id = $1
		 ORDER BY ingested_at ASC`, analysisID)
	if err != nil {
		return nil, fmt.Errorf("memory: list documents: %w", err)
	}
	defer rows.Close()
	var out []Document
	for rows.Next() {
		d, err := scanDocument(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// GetDocument fetches one document by id, scoped to an analysis the
// operator owns. ErrNotFound for both "wrong operator" and "no such
// document" — prevents existence enumeration across tenants.
func (s *Store) GetDocument(ctx context.Context, operatorID string, analysisID, documentID uuid.UUID) (Document, error) {
	if err := s.ok(); err != nil {
		return Document{}, err
	}
	if _, err := s.GetAnalysis(ctx, operatorID, analysisID); err != nil {
		return Document{}, err
	}
	row := s.db.QueryRow(ctx, `
		SELECT id, analysis_id, kind, filename, sha256, storage_uri,
		       COALESCE(byte_size, 0), ingested_at
		  FROM analysis_documents
		 WHERE id = $1 AND analysis_id = $2`, documentID, analysisID)
	d, err := scanDocument(row)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Document{}, ErrNotFound
		}
		return Document{}, fmt.Errorf("memory: get document: %w", err)
	}
	return d, nil
}

// DeleteDocument permanently deletes a document from an analysis.
// Documents do not use soft-delete. It returns ErrNotFound if the
// document does not exist, does not belong to the analysis, or the
// analysis does not belong to the operator.
func (s *Store) DeleteDocument(ctx context.Context, operatorID string, analysisID, documentID uuid.UUID) error {
	tag, err := s.db.Exec(ctx, `
		DELETE FROM analysis_documents
		 WHERE id = $1
		   AND analysis_id = $2
		   AND EXISTS (
		       SELECT 1 FROM analyses
		        WHERE id = $2 AND operator_id = $3 AND deleted_at IS NULL
		   )
	`, documentID, analysisID, operatorID)
	if err != nil {
		return fmt.Errorf("memory: delete document: %w", err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// --- scanners ---------------------------------------------------------

// scannable is the subset of pgx.Row/Rows we need.
type scannable interface {
	Scan(dest ...any) error
}

func scanAnalysis(r scannable) (Analysis, error) {
	var a Analysis
	err := r.Scan(
		&a.ID, &a.OperatorID, &a.OrgName, &a.Title, &a.Description,
		&a.CreatedAt, &a.DeletedAt,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Analysis{}, ErrNotFound
		}
		return Analysis{}, fmt.Errorf("memory: scan analysis: %w", err)
	}
	return a, nil
}

func scanDocument(r scannable) (Document, error) {
	var d Document
	err := r.Scan(
		&d.ID, &d.AnalysisID, &d.Kind, &d.Filename, &d.SHA256, &d.StorageURI,
		&d.ByteSize, &d.IngestedAt,
	)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Document{}, ErrNotFound
		}
		return Document{}, fmt.Errorf("memory: scan document: %w", err)
	}
	return d, nil
}


// =====================================================================
// document chunks
// =====================================================================

// StoreChunks batch-inserts document chunks with their embeddings.
// Uses a single multi-row INSERT for efficiency. No tenancy check
// here — the caller (handler) already verified ownership via
// requireOwnedAnalysis.
func (s *Store) StoreChunks(ctx context.Context, chunks []StoreChunkInput) error {
	if err := s.ok(); err != nil {
		return err
	}
	if len(chunks) == 0 {
		return nil
	}

	// Build a multi-row INSERT.  Each row has 5 placeholders:
	// (id, document_id, analysis_id, chunk_index, chunk_text, embedding).
	var b strings.Builder
	b.WriteString(`INSERT INTO document_chunks (chunk_id, document_id, analysis_id, chunk_index, chunk_text, embedding) VALUES `)
	args := make([]any, 0, len(chunks)*6)
	for i, c := range chunks {
		if i > 0 {
			b.WriteString(", ")
		}
		base := i * 6
		fmt.Fprintf(&b, "($%d, $%d, $%d, $%d, $%d, $%d::vector)",
			base+1, base+2, base+3, base+4, base+5, base+6)
		id := uuid.New()
		args = append(args, id, c.DocumentID, c.AnalysisID, c.ChunkIndex, c.ChunkText, pgvectorString(c.Embedding))
	}
	_, err := s.db.Exec(ctx, b.String(), args...)
	if err != nil {
		return fmt.Errorf("memory: store chunks: %w", err)
	}
	return nil
}

// DeleteChunksByDocument removes all chunks for a document. Used
// before re-chunking to avoid stale data.
func (s *Store) DeleteChunksByDocument(ctx context.Context, documentID uuid.UUID) error {
	if err := s.ok(); err != nil {
		return err
	}
	_, err := s.db.Exec(ctx, `DELETE FROM document_chunks WHERE document_id = $1`, documentID)
	if err != nil {
		return fmt.Errorf("memory: delete chunks by document: %w", err)
	}
	return nil
}

// ReplaceChunksForDocument does delete + insert atomically.
func (s *Store) ReplaceChunksForDocument(ctx context.Context, docID uuid.UUID, chunks []StoreChunkInput) error {
	if err := s.ok(); err != nil {
		return err
	}
	
	tx, err := s.db.Begin(ctx)
	if err != nil {
		return fmt.Errorf("memory: begin tx: %w", err)
	}
	defer func() {
		_ = tx.Rollback(ctx)
	}()
	
	_, err = tx.Exec(ctx, `DELETE FROM document_chunks WHERE document_id = $1`, docID)
	if err != nil {
		return fmt.Errorf("memory: delete chunks tx: %w", err)
	}
	
	if len(chunks) > 0 {
		var b strings.Builder
		b.WriteString(`INSERT INTO document_chunks (chunk_id, document_id, analysis_id, chunk_index, chunk_text, embedding) VALUES `)
		args := make([]any, 0, len(chunks)*6)
		for i, c := range chunks {
			if i > 0 {
				b.WriteString(", ")
			}
			base := i * 6
			fmt.Fprintf(&b, "($%d, $%d, $%d, $%d, $%d, $%d::vector)",
				base+1, base+2, base+3, base+4, base+5, base+6)
			id := uuid.New()
			args = append(args, id, c.DocumentID, c.AnalysisID, c.ChunkIndex, c.ChunkText, pgvectorString(c.Embedding))
		}
		_, err = tx.Exec(ctx, b.String(), args...)
		if err != nil {
			return fmt.Errorf("memory: insert chunks tx: %w", err)
		}
	}
	
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("memory: commit tx: %w", err)
	}
	return nil
}

// RetrieveChunks performs a vector similarity search over all chunks
// belonging to an analysis, ranked by cosine similarity to
// queryEmbedding. Limit is capped to 20; defaults to 5 when <= 0.
func (s *Store) RetrieveChunks(ctx context.Context, analysisID uuid.UUID, queryEmbedding []float32, limit int) ([]ChunkResult, error) {
	if err := s.ok(); err != nil {
		return nil, err
	}
	if limit <= 0 {
		limit = 5
	}
	if limit > 20 {
		limit = 20
	}
	embStr := pgvectorString(queryEmbedding)
	rows, err := s.db.Query(ctx, `
		SELECT chunk_id, document_id, analysis_id, chunk_index, chunk_text,
		       1 - (embedding <=> $1::vector) AS score, created_at
		  FROM document_chunks
		 WHERE analysis_id = $2
		 ORDER BY embedding <=> $1::vector
		 LIMIT $3`,
		embStr, analysisID, limit)
	if err != nil {
		return nil, fmt.Errorf("memory: retrieve chunks: %w", err)
	}
	defer rows.Close()
	var out []ChunkResult
	for rows.Next() {
		var cr ChunkResult
		if err := rows.Scan(
			&cr.ChunkID, &cr.DocumentID, &cr.AnalysisID,
			&cr.ChunkIndex, &cr.ChunkText, &cr.Score, &cr.CreatedAt,
		); err != nil {
			return nil, fmt.Errorf("memory: scan chunk result: %w", err)
		}
		out = append(out, cr)
	}
	return out, rows.Err()
}

// pgvectorString serialises a float32 slice into the pgvector text
// format "[0.1,0.2,0.3]" for use with $N::vector casts.
func pgvectorString(v []float32) string {
	var b strings.Builder
	b.WriteByte('[')
	for i, f := range v {
		if i > 0 {
			b.WriteByte(',')
		}
		fmt.Fprintf(&b, "%g", f)
	}
	b.WriteByte(']')
	return b.String()
}
