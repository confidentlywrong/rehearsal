// Copyright 2026 The Confidently Wrong Authors. Apache-2.0.

package memory

import "strings"

// defaultMaxTokens is the target chunk size in approximate tokens
// (whitespace-delimited words). 500 tokens ≈ 350–400 words ≈ one
// dense paragraph — a good trade-off between retrieval precision
// and context breadth.
const defaultMaxTokens = 500

// chunkOverlap is the number of tokens duplicated between consecutive
// chunks so that queries landing near a chunk boundary still surface
// the surrounding context.
const chunkOverlap = 50

// ChunkDocument splits text into overlapping chunks of approximately
// maxTokens tokens. It splits on paragraph boundaries (\n\n), merges
// short paragraphs into the same chunk, and splits very long
// paragraphs that exceed maxTokens on their own.
//
// Tokens are approximated as whitespace-delimited words — good enough
// for chunking decisions; the exact token count is irrelevant because
// the downstream embedder handles tokenization independently.
//
// If maxTokens <= 0 the default (500) is used.
func ChunkDocument(text string, maxTokens int) []string {
	if maxTokens <= 0 {
		maxTokens = defaultMaxTokens
	}
	text = strings.TrimSpace(text)
	if text == "" {
		return nil
	}

	// Split into paragraphs on blank-line boundaries.
	paragraphs := splitParagraphs(text)

	// Merge short paragraphs / split long ones into word-counted
	// segments of roughly maxTokens words each.
	segments := mergeAndSplit(paragraphs, maxTokens)

	if len(segments) <= 1 {
		return segments
	}

	// Apply overlap: prepend the last `chunkOverlap` words of the
	// previous segment to the current segment.
	return applyOverlap(segments, chunkOverlap, maxTokens)
}

// splitParagraphs splits text on one-or-more blank lines (\n\n+),
// returning only non-empty trimmed paragraphs.
func splitParagraphs(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.ReplaceAll(text, "\r", "\n")
	raw := strings.Split(text, "\n\n")
	out := make([]string, 0, len(raw))
	for _, p := range raw {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

// mergeAndSplit walks paragraphs left-to-right, accumulating words
// into the current segment. When adding the next paragraph would
// exceed maxTokens, the current segment is flushed. Paragraphs
// longer than maxTokens are hard-split at word boundaries.
func mergeAndSplit(paragraphs []string, maxTokens int) []string {
	var (
		segments []string
		curWords []string
	)
	for _, p := range paragraphs {
		words := strings.Fields(p)
		if len(words) == 0 {
			continue
		}

		// If the paragraph alone exceeds maxTokens, hard-split it.
		if len(words) > maxTokens {
			// Flush whatever we have accumulated so far.
			if len(curWords) > 0 {
				segments = append(segments, strings.Join(curWords, " "))
				curWords = curWords[:0]
			}
			for len(words) > 0 {
				end := maxTokens
				if end > len(words) {
					end = len(words)
				}
				segments = append(segments, strings.Join(words[:end], " "))
				words = words[end:]
			}
			continue
		}

		// Would adding this paragraph overflow the current segment?
		if len(curWords)+len(words) > maxTokens && len(curWords) > 0 {
			segments = append(segments, strings.Join(curWords, " "))
			curWords = curWords[:0]
		}
		curWords = append(curWords, words...)
	}

	// Flush remainder.
	if len(curWords) > 0 {
		segments = append(segments, strings.Join(curWords, " "))
	}

	return segments
}

// applyOverlap prepends the trailing `overlap` words of segment[i-1]
// to segment[i] for i >= 1. This ensures that a query whose answer
// spans a chunk boundary hits both chunks.
func applyOverlap(segments []string, overlap, maxTokens int) []string {
	out := make([]string, len(segments))
	out[0] = segments[0]
	for i := 1; i < len(segments); i++ {
		prevWords := strings.Fields(segments[i-1])
		start := len(prevWords) - overlap
		if start < 0 {
			start = 0
		}

		overlapWords := prevWords[start:]
		currentWords := strings.Fields(segments[i])

		// Trim overlap to fit within maxTokens while preserving all current content.
		maxOverlap := maxTokens - len(currentWords)
		if maxOverlap < 0 {
			maxOverlap = 0
		}
		if len(overlapWords) > maxOverlap {
			overlapWords = overlapWords[len(overlapWords)-maxOverlap:]
		}

		combined := append(overlapWords, currentWords...)
		out[i] = strings.Join(combined, " ")
	}
	return out
}
