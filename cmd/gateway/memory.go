// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package main

// HTTP surface for the Organizational Memory & Learning subsystem.
// All routes are mounted under /api/v1/memory and require an approved
// user (they are NOT in the ApprovedPaths allow-list in setupRouter).
//
// Store:   internal/memory
//
// Authorization model: the middleware chokepoint is
// requireOwnedAnalysis below — every analysis-scoped route runs it
// first, which loads the analysis via Store.GetAnalysis(operatorID,
// analysisID) and 404s on miss. Handlers receive the verified
// *memory.Analysis from the gin context and never re-check ownership.

import (
	"log"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"time"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/memory"
)

// memStore is the minimum surface the memory handlers need from
// *memory.Store. Narrow interface so tests can fake it without
// dialing Postgres.
type memStore interface {
	CreateAnalysis(ctx context.Context, in memory.CreateAnalysisInput) (memory.Analysis, error)
	GetAnalysis(ctx context.Context, operatorID string, analysisID uuid.UUID) (memory.Analysis, error)
	ListAnalyses(ctx context.Context, operatorID string) ([]memory.Analysis, error)
	SoftDeleteAnalysis(ctx context.Context, operatorID string, analysisID uuid.UUID) error
	UpdateAnalysis(ctx context.Context, operatorID string, analysisID uuid.UUID, in memory.UpdateAnalysisInput) (memory.Analysis, error)


	AddDocument(ctx context.Context, operatorID string, analysisID uuid.UUID, in memory.AddDocumentInput) (memory.Document, error)
	ListDocuments(ctx context.Context, operatorID string, analysisID uuid.UUID) ([]memory.Document, error)
	GetDocument(ctx context.Context, operatorID string, analysisID, documentID uuid.UUID) (memory.Document, error)
	DeleteDocument(ctx context.Context, operatorID string, analysisID, documentID uuid.UUID) error


	// Stage 2A — document chunks for RAG.
	StoreChunks(ctx context.Context, chunks []memory.StoreChunkInput) error
	DeleteChunksByDocument(ctx context.Context, documentID uuid.UUID) error
	ReplaceChunksForDocument(ctx context.Context, docID uuid.UUID, chunks []memory.StoreChunkInput) error
	RetrieveChunks(ctx context.Context, analysisID uuid.UUID, queryEmbedding []float32, limit int) ([]memory.ChunkResult, error)
}

// docStorage owns the bytes of an uploaded seed document. The Store
// only persists metadata (filename, sha256, storage_uri); the bytes
// themselves live wherever the Storage backend says.
//
// V1 backend: localDiskStorage under MEMORY_STORAGE_DIR.
// V2 backend (drop-in): a GCS-backed implementation reading
// MEMORY_STORAGE_BUCKET. The interface stays the same so handlers
// don't change.
type docStorage interface {
	// Save persists data and returns the opaque storage URI to
	// record in analysis_documents.storage_uri. Implementations
	// must compute their own paths from analysisID + docID;
	// callers do not pick the path.
	Save(ctx context.Context, analysisID, docID uuid.UUID, filename string, data io.Reader) (uri string, byteSize int64, sha256Hex string, err error)

	// Open returns a reader for a previously-saved document by its
	// recorded URI. Caller must Close. Used by the extract handler
	// to ship document text to the extractor agent. Implementations
	// MUST reject URIs that resolve outside their managed root
	// (path traversal defence).
	Open(ctx context.Context, uri string) (io.ReadCloser, error)
	Delete(ctx context.Context, uri string) error
}

// Embedder generates text embeddings for RAG. Implementations call
// a remote model (e.g. Vertex AI gemini-embedding-001); the interface
// lets tests inject a fake.
type Embedder interface {
	EmbedText(ctx context.Context, text string) ([]float32, error)
	EmbedBatch(ctx context.Context, texts []string) ([][]float32, error)
}

// localDiskStorage writes uploads under baseDir, organized as
// `<baseDir>/<analysis_id>/<doc_id>__<filename>`. Suffix-with-filename
// (rather than hash-name-only) keeps the on-disk listing
// human-skimmable during local development. In cloud mode the GCS
// backend will replace this entirely; the suffix is irrelevant there.
type localDiskStorage struct {
	baseDir string
}

func (l *localDiskStorage) Save(_ context.Context, analysisID, docID uuid.UUID, filename string, data io.Reader) (string, int64, string, error) {
	if l.baseDir == "" {
		return "", 0, "", errors.New("memory: localDiskStorage baseDir not configured")
	}
	dir := filepath.Join(l.baseDir, analysisID.String())
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", 0, "", fmt.Errorf("memory: mkdir %s: %w", dir, err)
	}
	// Sanitize filename to a safe leaf — no path separators, no
	// leading dots. We keep the operator's chosen name only for
	// human-readability; the docID is the actual identifier.
	safe := sanitizeFilename(filename)
	path := filepath.Join(dir, docID.String()+"__"+safe)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return "", 0, "", fmt.Errorf("memory: open %s: %w", path, err)
	}
	defer f.Close()
	// Hash on the fly while writing.
	h := sha256.New()
	n, err := io.Copy(f, io.TeeReader(data, h))
	if err != nil {
		// Best-effort cleanup of the partial file so a retry can
		// use the same docID.
		_ = os.Remove(path)
		return "", 0, "", fmt.Errorf("memory: write %s: %w", path, err)
	}
	return "file://" + path, n, hex.EncodeToString(h.Sum(nil)), nil
}

// Open implements docStorage.Open for the local-disk backend. Only
// accepts file:// URIs that resolve INSIDE baseDir — anything else is
// rejected to defeat path-traversal attacks that try to read e.g.
// ../../../etc/passwd by tampering with a stored storage_uri value
// (defence-in-depth: storage_uri is gateway-generated, but a future
// hand-set route or DB-level tampering should still be contained).
func (l *localDiskStorage) Open(_ context.Context, uri string) (io.ReadCloser, error) {
	if l.baseDir == "" {
		return nil, errors.New("memory: localDiskStorage baseDir not configured")
	}
	if !strings.HasPrefix(uri, "file://") {
		return nil, fmt.Errorf("memory: localDiskStorage: unsupported uri scheme %q", uri)
	}
	raw := strings.TrimPrefix(uri, "file://")
	abs, err := filepath.Abs(raw)
	if err != nil {
		return nil, fmt.Errorf("memory: resolve %q: %w", raw, err)
	}
	baseAbs, err := filepath.Abs(l.baseDir)
	if err != nil {
		return nil, fmt.Errorf("memory: resolve base %q: %w", l.baseDir, err)
	}
	// Use filepath.Rel + a "..\\|^\\.\\." check so a path like
	// /var/foo/../../../etc/passwd reduces to a Rel that escapes.
	rel, err := filepath.Rel(baseAbs, abs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return nil, fmt.Errorf("memory: refusing to open %q (outside storage root)", uri)
	}
	f, err := os.Open(abs)
	if err != nil {
		return nil, fmt.Errorf("memory: open %s: %w", abs, err)
	}
	return f, nil
}

func (l *localDiskStorage) Delete(_ context.Context, uri string) error {
	if l.baseDir == "" {
		return errors.New("memory: localDiskStorage baseDir not configured")
	}
	if !strings.HasPrefix(uri, "file://") {
		return fmt.Errorf("memory: localDiskStorage: unsupported uri scheme %q", uri)
	}
	raw := strings.TrimPrefix(uri, "file://")
	abs, err := filepath.Abs(raw)
	if err != nil {
		return fmt.Errorf("memory: resolve %q: %w", raw, err)
	}
	baseAbs, err := filepath.Abs(l.baseDir)
	if err != nil {
		return fmt.Errorf("memory: resolve base %q: %w", l.baseDir, err)
	}
	rel, err := filepath.Rel(baseAbs, abs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return fmt.Errorf("memory: refusing to delete %q (outside storage root)", uri)
	}
	return os.Remove(abs)
}

func sanitizeFilename(name string) string {
	name = filepath.Base(name)
	name = strings.TrimLeft(name, ".") // no hidden files
	if name == "" {
		name = "upload"
	}
	// Replace whitespace and path-unsafe runes with underscore.
	var b strings.Builder
	for _, r := range name {
		switch {
		case r >= 'a' && r <= 'z',
			r >= 'A' && r <= 'Z',
			r >= '0' && r <= '9',
			r == '.', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
	}
	out := b.String()
	if len(out) > 200 {
		out = out[:200]
	}
	return out
}

// memoryDocStorageFromEnv constructs the v1 default storage backend
// from MEMORY_STORAGE_DIR. Returns nil when unset so callers can 503
// the upload route while keeping the rest of /api/v1/memory live.
func memoryDocStorageFromEnv() docStorage {
	dir := strings.TrimSpace(os.Getenv("MEMORY_STORAGE_DIR"))
	if dir == "" {
		return nil
	}
	return &localDiskStorage{baseDir: dir}
}

// memoryMaxUploadBytes caps any single seed-document upload. Default
// 32 MiB — covers real-world strategy memos, town-hall transcripts,
// and post-mortems with margin. Override with MEMORY_MAX_UPLOAD_MB.
func memoryMaxUploadBytes() int64 {
	v := strings.TrimSpace(os.Getenv("MEMORY_MAX_UPLOAD_MB"))
	if v == "" {
		return 32 << 20
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return 32 << 20
	}
	return int64(n) << 20
}

// --- response shapes (snake_case to match the rest of the API) -------

type analysisDTO struct {
	ID                   string  `json:"id"`
	OperatorID           string  `json:"operator_id"`
	OrgName              string  `json:"org_name"`
	Title                string  `json:"title"`
	Description          string  `json:"description,omitempty"`
	CreatedAt            string  `json:"created_at"`
}

func toAnalysisDTO(a memory.Analysis) analysisDTO {
	out := analysisDTO{
		ID:              a.ID.String(),
		OperatorID:      a.OperatorID,
		OrgName:         a.OrgName,
		Title:           a.Title,
		Description:     a.Description,
		CreatedAt:       a.CreatedAt.UTC().Format(time.RFC3339),
	}
	return out
}

type documentDTO struct {
	ID         string `json:"id"`
	AnalysisID string `json:"analysis_id"`
	Kind       string `json:"kind"`
	Filename   string `json:"filename"`
	SHA256     string `json:"sha256"`
	StorageURI string `json:"storage_uri"`
	ByteSize   int64  `json:"byte_size"`
	IngestedAt string `json:"ingested_at"`
}

func toDocumentDTO(d memory.Document) documentDTO {
	return documentDTO{
		ID:         d.ID.String(),
		AnalysisID: d.AnalysisID.String(),
		Kind:       d.Kind,
		Filename:   d.Filename,
		SHA256:     d.SHA256,
		StorageURI: d.StorageURI,
		ByteSize:   d.ByteSize,
		IngestedAt: d.IngestedAt.UTC().Format(time.RFC3339),
	}
}


// --- middleware helpers ----------------------------------------------

// requireOperator pulls the authenticated user id off the request
// context. Returns "" + writes 401 when missing.
func requireOperator(c *gin.Context) string {
	uc, ok := account.FromContextOK(c.Request.Context())
	if !ok || uc.UserID == "" {
		c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
		return ""
	}
	return uc.UserID
}

// parseAnalysisID extracts and validates the :analysis_id URL param.
// Writes 400 on parse failure and returns false.
func parseAnalysisID(c *gin.Context) (uuid.UUID, bool) {
	raw := c.Param("analysis_id")
	id, err := uuid.Parse(raw)
	if err != nil {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "invalid_analysis_id"})
		return uuid.Nil, false
	}
	return id, true
}


// requireOwnedAnalysis is the per-request tenancy chokepoint. It
// loads the analysis owned by the caller and either stashes it on
// the gin context (under ctxKeyAnalysis) or aborts with 404. Every
// :analysis_id-scoped handler calls this first.
//
// "Looks like 404 to outsiders" is intentional: a foreign operator
// probing for someone else's analysis_id sees an indistinguishable
// response from "no such id", which prevents enumeration.
const ctxKeyAnalysis = "memory.analysis"

func requireOwnedAnalysis(store memStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		analysisID, ok := parseAnalysisID(c)
		if !ok {
			return
		}
		a, err := store.GetAnalysis(c.Request.Context(), operatorID, analysisID)
		switch {
		case errors.Is(err, memory.ErrNotFound):
			c.AbortWithStatusJSON(http.StatusNotFound, gin.H{"error": "analysis_not_found"})
			return
		case err != nil:
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "internal_error"})
			return
		}
		c.Set(ctxKeyAnalysis, a)
		c.Next()
	}
}

func analysisFromCtx(c *gin.Context) memory.Analysis {
	v, _ := c.Get(ctxKeyAnalysis)
	a, _ := v.(memory.Analysis)
	return a
}

// --- handlers --------------------------------------------------------

func createAnalysisHandler(store memStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		var req struct {
			OrgName     string `json:"org_name"`
			Title       string `json:"title"`
			Description string `json:"description"`
		}
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_json"})
			return
		}
		a, err := store.CreateAnalysis(c.Request.Context(), memory.CreateAnalysisInput{
			OperatorID:  operatorID,
			OrgName:     req.OrgName,
			Title:       req.Title,
			Description: req.Description,
		})
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_input", "detail": err.Error()})
			return
		}
		recordAuditBestEffort(c, account.AuditEntry{
			Action:   "memory.analysis.created",
			Metadata: map[string]any{"analysis_id": a.ID.String(), "org_name": a.OrgName},
		})
		c.JSON(http.StatusCreated, toAnalysisDTO(a))
	}
}

func listAnalysesHandler(store memStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		analyses, err := store.ListAnalyses(c.Request.Context(), operatorID)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal_error"})
			return
		}
		out := make([]analysisDTO, 0, len(analyses))
		for _, a := range analyses {
			out = append(out, toAnalysisDTO(a))
		}
		c.JSON(http.StatusOK, gin.H{"analyses": out})
	}
}

func getAnalysisHandler() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.JSON(http.StatusOK, toAnalysisDTO(analysisFromCtx(c)))
	}
}

func updateAnalysisHandler(store memStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		a := analysisFromCtx(c)
		var req struct {
			Title       *string `json:"title"`
			Description *string `json:"description"`
			OrgName     *string `json:"org_name"`
		}
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_json"})
			return
		}
		
		if req.Title != nil {
			t := strings.TrimSpace(*req.Title)
			req.Title = &t
			if *req.Title == "" {
				c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_input", "detail": "title cannot be empty"})
				return
			}
		}
		if req.Description != nil {
			d := strings.TrimSpace(*req.Description)
			req.Description = &d
		}
		if req.OrgName != nil {
			o := strings.TrimSpace(*req.OrgName)
			req.OrgName = &o
		}

		updated, err := store.UpdateAnalysis(c.Request.Context(), operatorID, a.ID, memory.UpdateAnalysisInput{
			Title:       req.Title,
			Description: req.Description,
			OrgName:     req.OrgName,
		})
		if err != nil {
			if errors.Is(err, memory.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "analysis_not_found"})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal_error"})
			return
		}
		recordAuditBestEffort(c, account.AuditEntry{
			Action:   "memory.analysis.updated",
			Metadata: map[string]any{"analysis_id": a.ID.String()},
		})
		c.JSON(http.StatusOK, toAnalysisDTO(updated))
	}
}

func deleteAnalysisHandler(store memStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		a := analysisFromCtx(c)
		if err := store.SoftDeleteAnalysis(c.Request.Context(), operatorID, a.ID); err != nil {
			if errors.Is(err, memory.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "analysis_not_found"})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal_error"})
			return
		}
		recordAuditBestEffort(c, account.AuditEntry{
			Action:   "memory.analysis.deleted",
			Metadata: map[string]any{"analysis_id": a.ID.String()},
		})
		c.Status(http.StatusNoContent)
	}
}

func deleteDocumentHandler(store memStore, storage docStorage) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		a := analysisFromCtx(c)
		
		docID, err := uuid.Parse(c.Param("doc_id"))
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_document_id"})
			return
		}

		doc, err := store.GetDocument(c.Request.Context(), operatorID, a.ID, docID)
		if err != nil {
			if errors.Is(err, memory.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "document_not_found"})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal_error"})
			return
		}

		if err := store.DeleteDocument(c.Request.Context(), operatorID, a.ID, docID); err != nil {
			if errors.Is(err, memory.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "document_not_found"})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal_error"})
			return
		}

		if storage != nil {
			if err := storage.Delete(c.Request.Context(), doc.StorageURI); err != nil {
				log.Printf("handler warning: failed to delete file %s: %v", doc.StorageURI, err)
			}
		}

		recordAuditBestEffort(c, account.AuditEntry{
			Action:   "memory.document.deleted",
			Metadata: map[string]any{
				"analysis_id": a.ID.String(),
				"document_id": docID.String(),
			},
		})
		c.Status(http.StatusNoContent)
	}
}


func chunkAndEmbedDocument(ctx context.Context, store memStore, storage docStorage, embedder Embedder, analysisID, docID uuid.UUID, storageURI string, maxChunkBytes int64) (int, error) {
	rc, err := storage.Open(ctx, storageURI)
	if err != nil {
		return 0, err
	}
	defer rc.Close()
	readCap := maxChunkBytes
	if readCap <= 0 {
		readCap = 5 << 20
	}
	raw, err := io.ReadAll(io.LimitReader(rc, readCap))
	if err != nil {
		return 0, err
	}
	text := string(raw)

	// Basic text validation: reject if >10% non-printable/non-UTF8 bytes.
	if !utf8.ValidString(text) {
		return 0, fmt.Errorf("memory: document content is not valid UTF-8 text")
	}

	chunkTexts := memory.ChunkDocument(text, 500)
	if len(chunkTexts) == 0 {
		return 0, nil
	}

	embeddings, err := embedder.EmbedBatch(ctx, chunkTexts)
	if err != nil {
		return 0, err
	}
	if len(embeddings) != len(chunkTexts) {
		return 0, errors.New("embedding count mismatch")
	}

	chunks := make([]memory.StoreChunkInput, len(chunkTexts))
	for i, ct := range chunkTexts {
		chunks[i] = memory.StoreChunkInput{
			DocumentID: docID,
			AnalysisID: analysisID,
			ChunkIndex: i,
			ChunkText:  ct,
			Embedding:  embeddings[i],
		}
	}
	return len(chunks), store.ReplaceChunksForDocument(ctx, docID, chunks)
}

func uploadDocumentHandler(store memStore, storage docStorage, embedder Embedder, maxBytes int64) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		if storage == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "storage_unavailable"})
			return
		}
		a := analysisFromCtx(c)

		// Enforce the upload cap BEFORE allocating any handler
		// memory. multipart.Reader respects the request body
		// LimitReader; we wrap explicitly so a malicious client
		// cannot stream past the cap via chunked encoding.
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxBytes)

		kind := strings.TrimSpace(c.PostForm("kind"))
		if kind == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "missing_kind"})
			return
		}
		file, header, err := c.Request.FormFile("file")
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "missing_file", "detail": err.Error()})
			return
		}
		defer file.Close()
		filename := strings.TrimSpace(header.Filename)
		if filename == "" {
			filename = "upload"
		}

		docID := uuid.New()
		uri, size, sha, err := storage.Save(c.Request.Context(), a.ID, docID, filename, file)
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		doc, err := store.AddDocument(c.Request.Context(), operatorID, a.ID, memory.AddDocumentInput{
			Kind:       kind,
			Filename:   filename,
			SHA256:     sha,
			StorageURI: uri,
			ByteSize:   size,
		})
		if err != nil {
			// Best-effort: storage already wrote the file. We
			// don't attempt to clean it up because a partial
			// write is easier to garbage-collect later than to
			// reconcile a deleted-file/leftover-row mismatch.
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		if embedder != nil {
			if _, err := chunkAndEmbedDocument(c.Request.Context(), store, storage, embedder, a.ID, doc.ID, doc.StorageURI, maxBytes); err != nil {
				log.Printf("handler warning: chunking failed for doc %s: %v", doc.ID, err)
				recordAuditBestEffort(c, account.AuditEntry{
					Action: "memory.document.uploaded",
					Metadata: map[string]any{
						"analysis_id": a.ID.String(),
						"document_id": doc.ID.String(),
						"kind":        kind,
						"byte_size":   size,
						"sha256":      sha,
					},
				})
				c.JSON(http.StatusCreated, gin.H{
					"document": toDocumentDTO(doc),
					"warning":  "Document saved but indexing failed. It will not appear in search results until re-indexed.",
				})
				return
			}
		}

		recordAuditBestEffort(c, account.AuditEntry{
			Action: "memory.document.uploaded",
			Metadata: map[string]any{
				"analysis_id": a.ID.String(),
				"document_id": doc.ID.String(),
				"kind":        kind,
				"byte_size":   size,
				"sha256":      sha,
			},
		})
		c.JSON(http.StatusCreated, gin.H{"document": toDocumentDTO(doc)})
	}
}

func listDocumentsHandler(store memStore) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		a := analysisFromCtx(c)
		docs, err := store.ListDocuments(c.Request.Context(), operatorID, a.ID)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal_error"})
			return
		}
		out := make([]documentDTO, 0, len(docs))
		for _, d := range docs {
			out = append(out, toDocumentDTO(d))
		}
		c.JSON(http.StatusOK, gin.H{"documents": out})
	}
}

func chunkDocumentHandler(store memStore, storage docStorage, embedder Embedder, maxChunkBytes int64) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		if storage == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "storage_unavailable"})
			return
		}
		if embedder == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "embedder_unavailable", "detail": "GOOGLE_CLOUD_PROJECT is not set"})
			return
		}
		a := analysisFromCtx(c)
		docIDStr := strings.TrimSpace(c.Param("doc_id"))
		docID, err := uuid.Parse(docIDStr)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_doc_id"})
			return
		}
		doc, err := store.GetDocument(c.Request.Context(), operatorID, a.ID, docID)
		if err != nil {
			if errors.Is(err, memory.ErrNotFound) {
				c.JSON(http.StatusNotFound, gin.H{"error": "document_not_found"})
				return
			}
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		chunksStored, err := chunkAndEmbedDocument(c.Request.Context(), store, storage, embedder, a.ID, doc.ID, doc.StorageURI, maxChunkBytes)
		if err != nil {
			log.Printf("handler error: chunking failed: %v", err)
			c.JSON(http.StatusBadGateway, gin.H{"error": "chunking_failed", "detail": err.Error()})
			return
		}

		c.JSON(http.StatusOK, gin.H{"status": "ok", "chunks_stored": chunksStored})
	}
}

// retrievePassagesHandler embeds a query string and performs a vector
// similarity search over the analysis's document chunks, returning
// the top-N most relevant passages.
func retrievePassagesHandler(store memStore, embedder Embedder) gin.HandlerFunc {
	return func(c *gin.Context) {
		operatorID := requireOperator(c)
		if operatorID == "" {
			return
		}
		if embedder == nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "embedder_unavailable", "detail": "GOOGLE_CLOUD_PROJECT is not set"})
			return
		}
		a := analysisFromCtx(c)
		var req struct {
			Query string `json:"query"`
			Limit int    `json:"limit,omitempty"`
		}
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_json"})
			return
		}
		query := strings.TrimSpace(req.Query)
		if query == "" {
			c.JSON(http.StatusBadRequest, gin.H{"error": "missing_query"})
			return
		}

		// Embed the query.
		embedding, err := embedder.EmbedText(c.Request.Context(), query)
		if err != nil {
			c.JSON(http.StatusBadGateway, gin.H{"error": "embedding_failed", "detail": err.Error()})
			return
		}

		// Retrieve matching chunks.
		results, err := store.RetrieveChunks(c.Request.Context(), a.ID, embedding, req.Limit)
		if err != nil {
			log.Printf("handler error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		type passageDTO struct {
			Text       string  `json:"text"`
			Score      float64 `json:"score"`
			DocumentID string  `json:"document_id"`
			ChunkIndex int     `json:"chunk_index"`
		}
		passages := make([]passageDTO, 0, len(results))
		for _, r := range results {
			passages = append(passages, passageDTO{
				Text:       r.ChunkText,
				Score:      r.Score,
				DocumentID: r.DocumentID.String(),
				ChunkIndex: r.ChunkIndex,
			})
		}
		c.JSON(http.StatusOK, gin.H{"passages": passages})
	}
}

// --- registration ----------------------------------------------------

// mountMemoryRoutes registers every /api/v1/memory route on r. Wired
// from main() once memoryStoreForGateway is constructed (and only
// then — a nil store causes the whole subsystem to be silently
// skipped, matching the /me handler pattern). Routes that need to
// know about an analysis_id are wrapped in requireOwnedAnalysis so
// every handler can assume the analysis on the context is theirs.
//
// `ext` may be nil — when so, the /extract route returns 503 and
// the batch endpoints still work (operators can still hand-import
// candidates).
//
// `embedder` may be nil — when so, the /chunk and /retrieve-passages
// routes return 503 (local dev without Vertex AI).
func mountMemoryRoutes(r *gin.Engine, store memStore, storage docStorage, embedder Embedder, maxUploadBytes int64) {
	if store == nil {
		return
	}

	g := r.Group("/api/v1/memory")

	g.POST("/analyses", createAnalysisHandler(store))
	g.GET("/analyses", listAnalysesHandler(store))
	g.GET("/config", getConfigHandler())

	owned := g.Group("/analyses/:analysis_id", requireOwnedAnalysis(store))
	owned.GET("", getAnalysisHandler())
	owned.PATCH("", updateAnalysisHandler(store))
	owned.DELETE("", deleteAnalysisHandler(store))

	owned.POST("/documents", uploadDocumentHandler(store, storage, embedder, maxUploadBytes))
	owned.GET("/documents", listDocumentsHandler(store))
	owned.DELETE("/documents/:doc_id", deleteDocumentHandler(store, storage))

	// Stage 2A — document chunking & RAG passage retrieval.
	owned.POST("/documents/:doc_id/chunk", chunkDocumentHandler(store, storage, embedder, maxUploadBytes))
	owned.POST("/retrieve-passages", retrievePassagesHandler(store, embedder))

}

var textReadableDocKinds = map[string]bool{
	"memo":       true,
	"transcript": true,
	"postmortem": true,
	"note":       true,
	"plan":       true,
}

func getConfigHandler() gin.HandlerFunc {
	return func(c *gin.Context) {
		var kinds []string
		for k := range textReadableDocKinds {
			kinds = append(kinds, k)
		}
		sort.Strings(kinds)
		c.JSON(http.StatusOK, gin.H{
			"text_readable_kinds": kinds,
			"max_upload_bytes":    memoryMaxUploadBytes(),
		})
	}
}

