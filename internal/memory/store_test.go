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

package memory

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type memFakeRow struct {
	scanFn func(dest ...any) error
}

func (r memFakeRow) Scan(dest ...any) error { return r.scanFn(dest...) }

type memFakeRows struct{}

func (memFakeRows) Close()                                       {}
func (memFakeRows) Err() error                                   { return nil }
func (memFakeRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (memFakeRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (memFakeRows) Next() bool                                   { return false }
func (memFakeRows) Scan(...any) error                            { return nil }
func (memFakeRows) Values() ([]any, error)                       { return nil, nil }
func (memFakeRows) RawValues() [][]byte                          { return nil }
func (memFakeRows) Conn() *pgx.Conn                              { return nil }

type memFakeConn struct {
	execFn     func(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	queryFn    func(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	queryRowFn func(ctx context.Context, sql string, args ...any) pgx.Row
	beginFn    func(ctx context.Context) (pgx.Tx, error)
}

func (f *memFakeConn) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	if f.execFn != nil {
		return f.execFn(ctx, sql, args...)
	}
	return pgconn.CommandTag{}, nil
}

func (f *memFakeConn) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	if f.queryFn != nil {
		return f.queryFn(ctx, sql, args...)
	}
	return memFakeRows{}, nil
}

func (f *memFakeConn) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	if f.queryRowFn != nil {
		return f.queryRowFn(ctx, sql, args...)
	}
	return memFakeRow{scanFn: func(...any) error { return pgx.ErrNoRows }}
}

func (f *memFakeConn) Begin(ctx context.Context) (pgx.Tx, error) {
	if f.beginFn != nil {
		return f.beginFn(ctx)
	}
	return nil, errors.New("unexpected Begin")
}

func scanSampleAnalysisRow(a Analysis) memFakeRow {
	return memFakeRow{scanFn: func(dest ...any) error {
		*(dest[0].(*uuid.UUID)) = a.ID
		*(dest[1].(*string)) = a.OperatorID
		*(dest[2].(*string)) = a.OrgName
		*(dest[3].(*string)) = a.Title
		*(dest[4].(*string)) = a.Description
		*(dest[5].(*time.Time)) = a.CreatedAt
		*(dest[6].(**time.Time)) = a.DeletedAt
		return nil
	}}
}

func TestCreateAnalysis_TrimsWhitespaceDescription(t *testing.T) {
	var gotArgs []any
	s := NewStore(&memFakeConn{
		queryRowFn: func(_ context.Context, _ string, args ...any) pgx.Row {
			gotArgs = args
			return scanSampleAnalysisRow(Analysis{
				ID:         uuid.New(),
				OperatorID: "op-1",
				Title:      "T1",
				CreatedAt:  time.Now().UTC(),
			})
		},
	})

	_, err := s.CreateAnalysis(context.Background(), CreateAnalysisInput{
		OperatorID:  " op-1 ",
		OrgName:     "  ",
		Title:       " T1 ",
		Description: "   \t\n  ",
	})
	if err != nil {
		t.Fatalf("CreateAnalysis err: %v", err)
	}
	if len(gotArgs) != 5 || gotArgs[4] != "" {
		t.Fatalf("expected trimmed empty Description for NULLIF, got args=%+v", gotArgs)
	}
}

func TestUpdateAnalysis_UsesNullIfAndValidatesTitle(t *testing.T) {
	ctx := context.Background()
	dbCalls := 0
	var gotSQL string
	var gotArgs []any

	s := NewStore(&memFakeConn{
		queryRowFn: func(_ context.Context, sql string, args ...any) pgx.Row {
			dbCalls++
			gotSQL = sql
			gotArgs = args
			return scanSampleAnalysisRow(Analysis{
				ID:         args[0].(uuid.UUID),
				OperatorID: "op-1",
				Title:      "New Title",
				CreatedAt:  time.Now().UTC(),
			})
		},
	})

	blank := "   "
	if _, err := s.UpdateAnalysis(ctx, "op-1", uuid.New(), UpdateAnalysisInput{Title: &blank}); err == nil || dbCalls != 0 {
		t.Fatalf("expected error without DB call on blank Title, got err=%v dbCalls=%d", err, dbCalls)
	}

	title := "  New Title  "
	desc := "   "
	org := " Acme "
	aid := uuid.New()
	if _, err := s.UpdateAnalysis(ctx, "op-1", aid, UpdateAnalysisInput{
		Title:       &title,
		Description: &desc,
		OrgName:     &org,
	}); err != nil {
		t.Fatalf("UpdateAnalysis full err: %v", err)
	}
	if !strings.Contains(gotSQL, "title = $3") ||
		!strings.Contains(gotSQL, "description = NULLIF($4, '')") ||
		!strings.Contains(gotSQL, "org_name = NULLIF($5, '')") {
		t.Fatalf("unexpected UpdateAnalysis SQL: %s", gotSQL)
	}
	if len(gotArgs) != 5 || gotArgs[2] != "New Title" || gotArgs[3] != "" || gotArgs[4] != "Acme" {
		t.Fatalf("unexpected UpdateAnalysis args: %+v", gotArgs)
	}
}

func TestUpdateAnalysis_EmptyInputDelegatesToGetAnalysis(t *testing.T) {
	var gotSQL string
	aid := uuid.New()
	s := NewStore(&memFakeConn{
		queryRowFn: func(_ context.Context, sql string, _ ...any) pgx.Row {
			gotSQL = sql
			return scanSampleAnalysisRow(Analysis{ID: aid, OperatorID: "op-1", Title: "Existing"})
		},
	})

	got, err := s.UpdateAnalysis(context.Background(), "op-1", aid, UpdateAnalysisInput{})
	if err != nil {
		t.Fatalf("UpdateAnalysis empty input err: %v", err)
	}
	if !strings.Contains(gotSQL, "SELECT id, operator_id") || got.Title != "Existing" {
		t.Fatalf("expected GetAnalysis delegation, got sql=%s analysis=%+v", gotSQL, got)
	}
}

func TestAddDocument_RejectsNegativeByteSize(t *testing.T) {
	aid := uuid.New()
	insertCalled := false
	s := NewStore(&memFakeConn{
		queryRowFn: func(_ context.Context, sql string, _ ...any) pgx.Row {
			if strings.Contains(sql, "INSERT INTO analysis_documents") {
				insertCalled = true
			}
			return scanSampleAnalysisRow(Analysis{ID: aid, OperatorID: "op-1", Title: "T"})
		},
	})

	_, err := s.AddDocument(context.Background(), "op-1", aid, AddDocumentInput{
		Kind:       "memo",
		Filename:   "doc.txt",
		SHA256:     "abc",
		StorageURI: "gs://b/doc.txt",
		ByteSize:   -1,
	})
	if err == nil || insertCalled {
		t.Fatalf("expected error before INSERT for negative ByteSize, got err=%v insertCalled=%v", err, insertCalled)
	}
}

func TestRetrieveChunks_RejectsEmptyZeroNaNInfAndWrongDim(t *testing.T) {
	queryCalled := false
	s := NewStore(&memFakeConn{
		queryFn: func(context.Context, string, ...any) (pgx.Rows, error) {
			queryCalled = true
			return memFakeRows{}, nil
		},
	})

	nanVec := make([]float32, TargetVectorDimension)
	nanVec[0] = float32(math.NaN())
	infVec := make([]float32, TargetVectorDimension)
	infVec[0] = float32(math.Inf(1))
	zeroVec := make([]float32, TargetVectorDimension)
	wrongDim := make([]float32, 768)
	wrongDim[0] = 1.0
	validVec := make([]float32, TargetVectorDimension)
	validVec[0] = 1.0

	cases := []struct {
		name string
		aid  uuid.UUID
		vec  []float32
	}{
		{"nil embedding", uuid.New(), nil},
		{"empty embedding", uuid.New(), []float32{}},
		{"all zeros", uuid.New(), zeroVec},
		{"NaN component", uuid.New(), nanVec},
		{"Inf component", uuid.New(), infVec},
		{"wrong dimension 768", uuid.New(), wrongDim},
		{"nil analysisID", uuid.Nil, validVec},
	}
	for _, tc := range cases {
		if _, err := s.RetrieveChunks(context.Background(), tc.aid, tc.vec, 5); err == nil {
			t.Errorf("%s: expected error, got nil", tc.name)
		}
	}
	if queryCalled {
		t.Fatal("DB Query must NOT be called for invalid RetrieveChunks inputs")
	}
}

func TestRetrieveChunks_SQLIncludesEmbeddingNotNullAndCapsLimit(t *testing.T) {
	var gotSQL string
	var gotLimits []int
	s := NewStore(&memFakeConn{
		queryFn: func(_ context.Context, sql string, args ...any) (pgx.Rows, error) {
			gotSQL = sql
			gotLimits = append(gotLimits, args[2].(int))
			return memFakeRows{}, nil
		},
	})
	vec := make([]float32, TargetVectorDimension)
	vec[0] = 1.0

	if _, err := s.RetrieveChunks(context.Background(), uuid.New(), vec, 0); err != nil {
		t.Fatalf("RetrieveChunks(0): %v", err)
	}
	if _, err := s.RetrieveChunks(context.Background(), uuid.New(), vec, 999); err != nil {
		t.Fatalf("RetrieveChunks(999): %v", err)
	}
	if !strings.Contains(gotSQL, "AND embedding IS NOT NULL") {
		t.Fatalf("expected AND embedding IS NOT NULL in RetrieveChunks SQL, got: %s", gotSQL)
	}
	if len(gotLimits) != 2 || gotLimits[0] != 5 || gotLimits[1] != 20 {
		t.Fatalf("unexpected limits: %v (want [5 20])", gotLimits)
	}
}

func TestStoreChunks_And_ReplaceChunks_RejectInvalidEmbeddings(t *testing.T) {
	execOrBeginCalled := false
	s := NewStore(&memFakeConn{
		execFn: func(context.Context, string, ...any) (pgconn.CommandTag, error) {
			execOrBeginCalled = true
			return pgconn.CommandTag{}, nil
		},
		beginFn: func(context.Context) (pgx.Tx, error) {
			execOrBeginCalled = true
			return nil, errors.New("should not begin")
		},
	})
	ctx := context.Background()
	docID := uuid.New()
	aid := uuid.New()

	badInputs := [][]StoreChunkInput{
		{{DocumentID: uuid.Nil, AnalysisID: aid, ChunkIndex: 0, Embedding: []float32{0.1}}},
		{{DocumentID: docID, AnalysisID: uuid.Nil, ChunkIndex: 0, Embedding: []float32{0.1}}},
		{{DocumentID: docID, AnalysisID: aid, ChunkIndex: -1, Embedding: []float32{0.1}}},
		{{DocumentID: docID, AnalysisID: aid, ChunkIndex: 0, Embedding: nil}},
		{{DocumentID: docID, AnalysisID: aid, ChunkIndex: 0, Embedding: []float32{0, 0, 0}}},
		{{DocumentID: docID, AnalysisID: aid, ChunkIndex: 0, Embedding: []float32{float32(math.NaN()), 1}}},
		{{DocumentID: docID, AnalysisID: aid, ChunkIndex: 0, Embedding: []float32{float32(math.Inf(1)), 1}}},
	}
	for i, in := range badInputs {
		if err := s.StoreChunks(ctx, in); err == nil {
			t.Errorf("StoreChunks[%d]: expected validation error", i)
		}
		if err := s.ReplaceChunksForDocument(ctx, docID, in); err == nil {
			t.Errorf("ReplaceChunksForDocument[%d]: expected validation error", i)
		}
	}
	if err := s.ReplaceChunksForDocument(ctx, uuid.Nil, nil); err == nil {
		t.Error("ReplaceChunksForDocument with uuid.Nil docID: expected error")
	}
	if execOrBeginCalled {
		t.Fatal("DB Exec/Begin must NOT be called when chunk validation fails")
	}
}

func TestAllStoreMethods_NilReceiverReturnErrors(t *testing.T) {
	ctx := context.Background()
	id := uuid.New()

	stores := []struct {
		name  string
		store *Store
	}{
		{"nil pointer", (*Store)(nil)},
		{"zero value", &Store{}},
		{"NewStore(nil)", NewStore(nil)},
		{"NewStore((*pgxpool.Pool)(nil))", NewStore((*pgxpool.Pool)(nil))},
		{"NewStore((*pgx.Conn)(nil))", NewStore((*pgx.Conn)(nil))},
	}

	for _, tc := range stores {
		t.Run(tc.name, func(t *testing.T) {
			s := tc.store
			check := func(method string, err error) {
				t.Helper()
				if err == nil || !strings.Contains(err.Error(), "memory: nil store") {
					t.Fatalf("%s on %s: got %v, want 'memory: nil store'", method, tc.name, err)
				}
			}

			_, err := s.CreateAnalysis(ctx, CreateAnalysisInput{OperatorID: "op", Title: "t"})
			check("CreateAnalysis", err)
			_, err = s.GetAnalysis(ctx, "op", id)
			check("GetAnalysis", err)
			_, err = s.UpdateAnalysis(ctx, "op", id, UpdateAnalysisInput{})
			check("UpdateAnalysis", err)
			_, err = s.ListAnalyses(ctx, "op")
			check("ListAnalyses", err)
			check("SoftDeleteAnalysis", s.SoftDeleteAnalysis(ctx, "op", id))
			_, err = s.AddDocument(ctx, "op", id, AddDocumentInput{})
			check("AddDocument", err)
			_, err = s.ListDocuments(ctx, "op", id)
			check("ListDocuments", err)
			_, err = s.GetDocument(ctx, "op", id, id)
			check("GetDocument", err)
			check("DeleteDocument", s.DeleteDocument(ctx, "op", id, id))
			check("StoreChunks", s.StoreChunks(ctx, nil))
			check("DeleteChunksByDocument", s.DeleteChunksByDocument(ctx, id))
			check("ReplaceChunksForDocument", s.ReplaceChunksForDocument(ctx, id, nil))
			_, err = s.RetrieveChunks(ctx, id, []float32{0.1}, 5)
			check("RetrieveChunks", err)
		})
	}
}
