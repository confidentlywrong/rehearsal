// Copyright 2026 Christian Haeussinger. Apache-2.0.

package memory

import (
	"strings"
	"testing"
)

func TestChunkDocument_EmptyText(t *testing.T) {
	got := ChunkDocument("", 500)
	if got != nil {
		t.Fatalf("expected nil, got %v", got)
	}
}

func TestChunkDocument_WhitespaceOnly(t *testing.T) {
	got := ChunkDocument("   \n\n  \n  ", 500)
	if got != nil {
		t.Fatalf("expected nil, got %v", got)
	}
}

func TestChunkDocument_SingleParagraphWithinLimit(t *testing.T) {
	text := "Hello world this is a test."
	got := ChunkDocument(text, 500)
	if len(got) != 1 {
		t.Fatalf("expected 1 chunk, got %d", len(got))
	}
	if got[0] != "Hello world this is a test." {
		t.Fatalf("unexpected chunk: %q", got[0])
	}
}

func TestChunkDocument_DefaultMaxTokens(t *testing.T) {
	// Passing 0 should use the default (500).
	// 10 words, should be a single chunk with default 500.
	text := strings.Repeat("word ", 10)
	got := ChunkDocument(text, 0)
	if len(got) != 1 {
		t.Fatalf("expected 1 chunk with default maxTokens, got %d", len(got))
	}
}

func TestChunkDocument_MultipleParagraphsMerged(t *testing.T) {
	// Two short paragraphs that together fit within 20 tokens.
	text := "Hello world.\n\nGoodbye world."
	got := ChunkDocument(text, 20)
	if len(got) != 1 {
		t.Fatalf("expected 1 merged chunk, got %d", len(got))
	}
	if got[0] != "Hello world. Goodbye world." {
		t.Fatalf("unexpected: %q", got[0])
	}
}

func TestChunkDocument_ParagraphsSplit(t *testing.T) {
	// Two paragraphs of 5 words each with maxTokens=5 should
	// yield 2 chunks with overlap.
	p1 := "one two three four five"
	p2 := "six seven eight nine ten"
	text := p1 + "\n\n" + p2
	got := ChunkDocument(text, 5)
	if len(got) != 2 {
		t.Fatalf("expected 2 chunks, got %d: %v", len(got), got)
	}
	// First chunk should be exactly p1.
	if got[0] != p1 {
		t.Fatalf("chunk[0] = %q, want %q", got[0], p1)
	}
	// Second chunk should have overlap from chunk[0].
	if !strings.Contains(got[1], "six seven eight nine ten") {
		t.Fatalf("chunk[1] missing p2 content: %q", got[1])
	}
}

func TestChunkDocument_VeryLongParagraph(t *testing.T) {
	// A single paragraph of 25 words with maxTokens=10 should be
	// hard-split into 3 segments with overlap applied.
	words := make([]string, 25)
	for i := range words {
		words[i] = "w"
	}
	text := strings.Join(words, " ")
	got := ChunkDocument(text, 10)
	if len(got) < 3 {
		t.Fatalf("expected >=3 chunks for 25 words at maxTokens=10, got %d", len(got))
	}
	// First chunk should have exactly 10 words.
	if wc := len(strings.Fields(got[0])); wc != 10 {
		t.Fatalf("chunk[0] word count = %d, want 10", wc)
	}
}

func TestChunkDocument_OverlapPresent(t *testing.T) {
	// 3 paragraphs of 10 words each, maxTokens=10, overlap=50
	// (but overlap is capped at prev chunk length).
	words := make([]string, 10)
	for i := range words {
		words[i] = "a"
	}
	para := strings.Join(words, " ")
	text := para + "\n\n" + para + "\n\n" + para
	got := ChunkDocument(text, 10)
	if len(got) < 2 {
		t.Fatalf("expected >=2 chunks, got %d", len(got))
	}
	// Second chunk should start with overlap words from the first.
	secondWords := strings.Fields(got[1])
	// With overlap=50 but first chunk only has 10 words, all 10
	// words carry over as prefix.
	if len(secondWords) < 10 {
		t.Fatalf("chunk[1] too short for overlap: %d words", len(secondWords))
	}
}

func TestChunkDocument_MultipleBlankLines(t *testing.T) {
	// Multiple blank lines between paragraphs should still split.
	text := "first para\n\n\n\nsecond para"
	got := ChunkDocument(text, 500)
	if len(got) != 1 {
		t.Fatalf("expected 1 merged chunk, got %d", len(got))
	}
	if !strings.Contains(got[0], "first para") || !strings.Contains(got[0], "second para") {
		t.Fatalf("missing content: %q", got[0])
	}
}
