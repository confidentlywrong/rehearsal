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
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

func TestR2_PgvectorStringSanitizesNonFiniteFloats(t *testing.T) {
	vec := []float32{
		1.5,
		float32(math.NaN()),
		float32(math.Inf(1)),
		float32(math.Inf(-1)),
		-0.25,
	}
	got := pgvectorString(vec)
	want := "[1.5,0,0,0,-0.25]"
	if got != want {
		t.Fatalf("pgvectorString(%v) = %q, want %q", vec, got, want)
	}
}

type fakeTxR2 struct {
	pgx.Tx
	cancelCaller context.CancelFunc
	rollbackCalled bool
	rollbackCtxErr error
}

func (tx *fakeTxR2) Exec(_ context.Context, _ string, _ ...any) (pgconn.CommandTag, error) {
	// Simulate caller context cancelling mid-transaction right as Exec fails.
	if tx.cancelCaller != nil {
		tx.cancelCaller()
	}
	return pgconn.NewCommandTag(""), errors.New("simulated mid-tx failure")
}

func (tx *fakeTxR2) Rollback(ctx context.Context) error {
	tx.rollbackCalled = true
	tx.rollbackCtxErr = ctx.Err()
	return nil
}

type fakeTxConnR2 struct {
	tx *fakeTxR2
}

func (c fakeTxConnR2) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	return pgconn.NewCommandTag(""), nil
}
func (c fakeTxConnR2) Query(context.Context, string, ...any) (pgx.Rows, error) { return nil, nil }
func (c fakeTxConnR2) QueryRow(context.Context, string, ...any) pgx.Row        { return nil }
func (c fakeTxConnR2) Begin(context.Context) (pgx.Tx, error)                   { return c.tx, nil }

func TestR2_ReplaceChunksForDocumentRollsBackWithHealthyFallbackContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	tx := &fakeTxR2{cancelCaller: cancel}
	store := NewStore(fakeTxConnR2{tx: tx})

	err := store.ReplaceChunksForDocument(ctx, uuid.New(), []StoreChunkInput{
		{DocumentID: uuid.New(), AnalysisID: uuid.New(), ChunkIndex: 0, ChunkText: "hi", Embedding: []float32{0.1}},
	})
	if err == nil {
		t.Fatal("expected ReplaceChunksForDocument to fail on simulated Exec error")
	}
	if !tx.rollbackCalled {
		t.Fatal("expected tx.Rollback to be called")
	}
	if tx.rollbackCtxErr != nil {
		t.Fatalf("expected tx.Rollback to receive a non-cancelled fallback context, got ctx.Err()=%v", tx.rollbackCtxErr)
	}
}
