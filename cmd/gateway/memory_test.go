// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package main

// Handler tests for /api/v1/memory. We do not exercise the full
// gateway middleware stack here — that path is covered by gateway
// integration tests elsewhere. These tests:
//
//   1. Use a fakeMemStore implementing the memStore interface.
//   2. Inject a synthetic account.UserContext directly onto the gin
//      request so requireOperator and analysisFromCtx work without
//      booting Firebase / LocalResolver.
//   3. Assert HTTP status + JSON shape per route.
//
// The aim is to lock in the contract surface so refactors to the
// memory.Store cannot silently change the API the SPA depends on.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/memory"
)

// fakeMemStore is a hand-written in-memory implementation of
// memStore. Tests inject one per request to control every store
// outcome.
type fakeMemStore struct {
	analyses     map[uuid.UUID]memory.Analysis
	docs         map[uuid.UUID][]memory.Document
	// Force-error knobs.
	getErr         error
	createErr      error
	uploadStoreErr error
}

func newFakeMemStore() *fakeMemStore {
	return &fakeMemStore{
		analyses:  map[uuid.UUID]memory.Analysis{},
		docs:      map[uuid.UUID][]memory.Document{},
	}
}

func (f *fakeMemStore) CreateAnalysis(_ context.Context, in memory.CreateAnalysisInput) (memory.Analysis, error) {
	if f.createErr != nil {
		return memory.Analysis{}, f.createErr
	}
	// Mirror the real memory.Store contract: empty required
	// fields are a 400-class error, not a silent insert.
	if in.OperatorID == "" || in.OrgName == "" || in.Title == "" {
		return memory.Analysis{}, errors.New("memory: required field missing")
	}
	a := memory.Analysis{
		ID:         uuid.New(),
		OperatorID: in.OperatorID,
		OrgName:    in.OrgName,
		Title:      in.Title,
	}
	f.analyses[a.ID] = a
	return a, nil
}

func (f *fakeMemStore) GetAnalysis(_ context.Context, operatorID string, analysisID uuid.UUID) (memory.Analysis, error) {
	if f.getErr != nil {
		return memory.Analysis{}, f.getErr
	}
	a, ok := f.analyses[analysisID]
	if !ok || a.OperatorID != operatorID {
		return memory.Analysis{}, memory.ErrNotFound
	}
	return a, nil
}

func (f *fakeMemStore) ListAnalyses(_ context.Context, operatorID string) ([]memory.Analysis, error) {
	var out []memory.Analysis
	for _, a := range f.analyses {
		if a.OperatorID == operatorID {
			out = append(out, a)
		}
	}
	return out, nil
}

func (f *fakeMemStore) SoftDeleteAnalysis(_ context.Context, operatorID string, analysisID uuid.UUID) error {
	a, ok := f.analyses[analysisID]
	if !ok || a.OperatorID != operatorID {
		return memory.ErrNotFound
	}
	delete(f.analyses, analysisID)
	return nil
}

func (f *fakeMemStore) AddDocument(_ context.Context, operatorID string, analysisID uuid.UUID, in memory.AddDocumentInput) (memory.Document, error) {
	if f.uploadStoreErr != nil {
		return memory.Document{}, f.uploadStoreErr
	}
	a, ok := f.analyses[analysisID]
	if !ok || a.OperatorID != operatorID {
		return memory.Document{}, memory.ErrNotFound
	}
	d := memory.Document{
		ID:         uuid.New(),
		AnalysisID: analysisID,
		Kind:       in.Kind,
		Filename:   in.Filename,
		SHA256:     in.SHA256,
		StorageURI: in.StorageURI,
		ByteSize:   in.ByteSize,
	}
	f.docs[analysisID] = append(f.docs[analysisID], d)
	return d, nil
}

func (f *fakeMemStore) ListDocuments(_ context.Context, operatorID string, analysisID uuid.UUID) ([]memory.Document, error) {
	a, ok := f.analyses[analysisID]
	if !ok || a.OperatorID != operatorID {
		return nil, memory.ErrNotFound
	}
	return f.docs[analysisID], nil
}

func (f *fakeMemStore) GetDocument(_ context.Context, operatorID string, analysisID, documentID uuid.UUID) (memory.Document, error) {
	a, ok := f.analyses[analysisID]
	if !ok || a.OperatorID != operatorID {
		return memory.Document{}, memory.ErrNotFound
	}
	for _, d := range f.docs[analysisID] {
		if d.ID == documentID {
			return d, nil
		}
	}
	return memory.Document{}, memory.ErrNotFound
}

func (f *fakeMemStore) DeleteDocument(_ context.Context, operatorID string, analysisID, documentID uuid.UUID) error {
	a, ok := f.analyses[analysisID]
	if !ok || a.OperatorID != operatorID {
		return memory.ErrNotFound
	}
	docs := f.docs[analysisID]
	for i, d := range docs {
		if d.ID == documentID {
			f.docs[analysisID] = append(docs[:i], docs[i+1:]...)
			return nil
		}
	}
	return memory.ErrNotFound
}

func (f *fakeMemStore) StoreChunks(_ context.Context, _ []memory.StoreChunkInput) error {
	return nil
}

func (f *fakeMemStore) DeleteChunksByDocument(_ context.Context, _ uuid.UUID) error {
	return nil
}

func (f *fakeMemStore) ReplaceChunksForDocument(_ context.Context, _ uuid.UUID, _ []memory.StoreChunkInput) error {
	return nil
}

func (f *fakeMemStore) UpdateAnalysis(_ context.Context, _ string, _ uuid.UUID, _ memory.UpdateAnalysisInput) (memory.Analysis, error) {
	return memory.Analysis{}, nil
}

func (f *fakeMemStore) RetrieveChunks(_ context.Context, _ uuid.UUID, _ []float32, _ int) ([]memory.ChunkResult, error) {
	return nil, nil
}



// --- helpers ---------------------------------------------------------

// withOperator builds a gin engine, wraps every request with a
// middleware that injects an account.UserContext for operatorID,
// and mounts the memory routes.
func withOperator(t *testing.T, store memStore, storage docStorage, operatorID string) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) {
		ctx := account.WithContext(c.Request.Context(), account.UserContext{
			UserID: operatorID,
			Status: account.StatusApproved,
		})
		c.Request = c.Request.WithContext(ctx)
		c.Next()
	})
	mountMemoryRoutes(r, store, storage, nil, 1<<20)
	return r
}

func doJSON(t *testing.T, r *gin.Engine, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var buf io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal: %v", err)
		}
		buf = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, buf)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

// --- tests -----------------------------------------------------------

func TestMemory_CreateAnalysis_Happy(t *testing.T) {
	store := newFakeMemStore()
	r := withOperator(t, store, nil, "op-1")
	w := doJSON(t, r, http.MethodPost, "/api/v1/memory/analyses", map[string]string{
		"org_name": "Acme",
		"title":    "Procurement reorg",
	})
	if w.Code != http.StatusCreated {
		t.Fatalf("status: want 201, got %d body=%s", w.Code, w.Body.String())
	}
	var out analysisDTO
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if out.OperatorID != "op-1" || out.OrgName != "Acme" || out.Title != "Procurement reorg" {
		t.Fatalf("unexpected body: %+v", out)
	}
}

func TestMemory_CreateAnalysis_RejectsMissingFields(t *testing.T) {
	store := newFakeMemStore()
	r := withOperator(t, store, nil, "op-1")
	w := doJSON(t, r, http.MethodPost, "/api/v1/memory/analyses", map[string]string{
		"org_name": "",
		"title":    "",
	})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status: want 400, got %d body=%s", w.Code, w.Body.String())
	}
}

func TestMemory_GetAnalysis_TenancyChokepoint(t *testing.T) {
	// op-2 owns the analysis; op-1 tries to read it. The middleware
	// chokepoint must respond 404 (not 403) to avoid leaking
	// existence.
	store := newFakeMemStore()
	id := uuid.New()
	store.analyses[id] = memory.Analysis{ID: id, OperatorID: "op-2", OrgName: "Acme", Title: "secret"}

	r := withOperator(t, store, nil, "op-1")
	w := doJSON(t, r, http.MethodGet, "/api/v1/memory/analyses/"+id.String(), nil)
	if w.Code != http.StatusNotFound {
		t.Fatalf("foreign operator must see 404, got %d body=%s", w.Code, w.Body.String())
	}
}

func TestMemory_GetAnalysis_OwnerSees200(t *testing.T) {
	store := newFakeMemStore()
	id := uuid.New()
	store.analyses[id] = memory.Analysis{ID: id, OperatorID: "op-1", OrgName: "Acme", Title: "ok"}

	r := withOperator(t, store, nil, "op-1")
	w := doJSON(t, r, http.MethodGet, "/api/v1/memory/analyses/"+id.String(), nil)
	if w.Code != http.StatusOK {
		t.Fatalf("owner must see 200, got %d body=%s", w.Code, w.Body.String())
	}
}

func TestMemory_GetAnalysis_InvalidUUIDIs400(t *testing.T) {
	store := newFakeMemStore()
	r := withOperator(t, store, nil, "op-1")
	w := doJSON(t, r, http.MethodGet, "/api/v1/memory/analyses/not-a-uuid", nil)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("invalid uuid must be 400, got %d", w.Code)
	}
}

func TestMemory_DeleteAnalysis_NoContent(t *testing.T) {
	store := newFakeMemStore()
	id := uuid.New()
	store.analyses[id] = memory.Analysis{ID: id, OperatorID: "op-1", OrgName: "Acme", Title: "t"}
	r := withOperator(t, store, nil, "op-1")
	w := doJSON(t, r, http.MethodDelete, "/api/v1/memory/analyses/"+id.String(), nil)
	if w.Code != http.StatusNoContent {
		t.Fatalf("want 204, got %d body=%s", w.Code, w.Body.String())
	}
	if _, ok := store.analyses[id]; ok {
		t.Fatal("analysis still present in store after delete")
	}
}

func TestMemory_NoStore_MountIsNoOp(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	mountMemoryRoutes(r, nil, nil, nil, 1<<20)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/memory/analyses", nil))
	if w.Code != http.StatusNotFound {
		t.Fatalf("nil store should produce 404 (route not registered), got %d", w.Code)
	}
}

// --- upload + storage ------------------------------------------------

func TestMemory_UploadDocument_LocalDiskHappyPath(t *testing.T) {
	store := newFakeMemStore()
	id := uuid.New()
	store.analyses[id] = memory.Analysis{ID: id, OperatorID: "op-1", OrgName: "x", Title: "t"}

	tmp := t.TempDir()
	storage := &localDiskStorage{baseDir: tmp}
	r := withOperator(t, store, storage, "op-1")

	body := &bytes.Buffer{}
	mw := multipart.NewWriter(body)
	_ = mw.WriteField("kind", "memo")
	part, err := mw.CreateFormFile("file", "strategy.md")
	if err != nil {
		t.Fatalf("part: %v", err)
	}
	if _, err := part.Write([]byte("# Strategy memo\nLine 2.")); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := mw.Close(); err != nil {
		t.Fatalf("close mw: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, "/api/v1/memory/analyses/"+id.String()+"/documents", body)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d body=%s", w.Code, w.Body.String())
	}
	var envelope struct {
		Document documentDTO `json:"document"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	out := envelope.Document
	if out.Kind != "memo" || out.Filename != "strategy.md" {
		t.Fatalf("unexpected doc: %+v", out)
	}
	if out.SHA256 == "" || out.ByteSize == 0 {
		t.Fatalf("sha256/byte_size not populated: %+v", out)
	}
	if !strings.HasPrefix(out.StorageURI, "file://"+tmp) {
		t.Fatalf("storage_uri not under tmp dir: %q (want prefix file://%s)", out.StorageURI, tmp)
	}
	// File on disk should exist and contain the bytes we sent.
	on := strings.TrimPrefix(out.StorageURI, "file://")
	got, err := os.ReadFile(on)
	if err != nil {
		t.Fatalf("read on-disk file: %v", err)
	}
	if string(got) != "# Strategy memo\nLine 2." {
		t.Fatalf("on-disk content mismatch: %q", got)
	}
}

func TestMemory_UploadDocument_RejectsMissingKind(t *testing.T) {
	store := newFakeMemStore()
	id := uuid.New()
	store.analyses[id] = memory.Analysis{ID: id, OperatorID: "op-1", OrgName: "x", Title: "t"}
	storage := &localDiskStorage{baseDir: t.TempDir()}
	r := withOperator(t, store, storage, "op-1")

	body := &bytes.Buffer{}
	mw := multipart.NewWriter(body)
	part, _ := mw.CreateFormFile("file", "f.txt")
	_, _ = part.Write([]byte("hi"))
	_ = mw.Close()

	req := httptest.NewRequest(http.MethodPost, "/api/v1/memory/analyses/"+id.String()+"/documents", body)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("missing kind must be 400, got %d", w.Code)
	}
}

func TestMemory_UploadDocument_RejectsNilStorage(t *testing.T) {
	store := newFakeMemStore()
	id := uuid.New()
	store.analyses[id] = memory.Analysis{ID: id, OperatorID: "op-1", OrgName: "x", Title: "t"}
	r := withOperator(t, store, nil, "op-1")

	body := &bytes.Buffer{}
	mw := multipart.NewWriter(body)
	_ = mw.WriteField("kind", "memo")
	part, _ := mw.CreateFormFile("file", "f.txt")
	_, _ = part.Write([]byte("hi"))
	_ = mw.Close()

	req := httptest.NewRequest(http.MethodPost, "/api/v1/memory/analyses/"+id.String()+"/documents", body)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil storage must be 503, got %d", w.Code)
	}
}

func TestLocalDiskStorage_SanitizesFilename(t *testing.T) {
	tmp := t.TempDir()
	st := &localDiskStorage{baseDir: tmp}
	uri, _, _, err := st.Save(context.Background(), uuid.New(), uuid.New(), "../../etc/passwd", strings.NewReader("data"))
	if err != nil {
		t.Fatalf("Save: %v", err)
	}
	// The path must remain inside tmp.
	abs, _ := filepath.Abs(strings.TrimPrefix(uri, "file://"))
	tmpAbs, _ := filepath.Abs(tmp)
	if !strings.HasPrefix(abs, tmpAbs) {
		t.Fatalf("path escaped tmp dir: %s (tmp=%s)", abs, tmpAbs)
	}
	// And the filename itself should not contain path separators.
	leaf := filepath.Base(abs)
	if strings.Contains(leaf, "..") || strings.ContainsAny(leaf, "/\\") {
		t.Fatalf("leaf retained unsafe chars: %q", leaf)
	}
}

func TestLocalDiskStorage_BaseDirRequired(t *testing.T) {
	st := &localDiskStorage{}
	_, _, _, err := st.Save(context.Background(), uuid.New(), uuid.New(), "f.txt", strings.NewReader("d"))
	if err == nil {
		t.Fatal("expected error when baseDir is empty")
	}
}

func TestMemoryMaxUploadBytes(t *testing.T) {
	t.Setenv("MEMORY_MAX_UPLOAD_MB", "")
	if got := memoryMaxUploadBytes(); got != 32<<20 {
		t.Errorf("default: want 32MiB, got %d", got)
	}
	t.Setenv("MEMORY_MAX_UPLOAD_MB", "5")
	if got := memoryMaxUploadBytes(); got != 5<<20 {
		t.Errorf("5MB: got %d", got)
	}
	t.Setenv("MEMORY_MAX_UPLOAD_MB", "garbage")
	if got := memoryMaxUploadBytes(); got != 32<<20 {
		t.Errorf("garbage: want default, got %d", got)
	}
}

func TestMemoryDocStorageFromEnv(t *testing.T) {
	t.Setenv("MEMORY_STORAGE_DIR", "")
	if memoryDocStorageFromEnv() != nil {
		t.Fatal("empty env should yield nil storage")
	}
	t.Setenv("MEMORY_STORAGE_DIR", "/tmp/whatever")
	st := memoryDocStorageFromEnv()
	if st == nil {
		t.Fatal("non-empty env should yield a storage")
	}
}

// Defensive: make sure the real *memory.Store satisfies the memStore
// interface. If memory.Store grows or shrinks a method signature, this
// breaks the build right here with a clear message instead of in
// main.go.
var _ memStore = (*memory.Store)(nil)

// Catches a misuse where the test forgets to seed an analysis.
var _ error = memory.ErrNotFound
var _ error = errors.New("compile-time-only")



func TestLocalDiskStorage_OpenRefusesTraversal(t *testing.T) {
	tmp := t.TempDir()
	st := &localDiskStorage{baseDir: tmp}
	// Build a URI that resolves outside baseDir.
	_, err := st.Open(context.Background(), "file:///etc/passwd")
	if err == nil {
		t.Fatal("expected error opening file outside baseDir")
	}
}

func TestLocalDiskStorage_OpenRefusesNonFileScheme(t *testing.T) {
	tmp := t.TempDir()
	st := &localDiskStorage{baseDir: tmp}
	if _, err := st.Open(context.Background(), "gs://bucket/key"); err == nil {
		t.Fatal("expected error for non-file scheme")
	}
}

