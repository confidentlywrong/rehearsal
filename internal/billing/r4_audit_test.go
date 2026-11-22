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

package billing

import (
	"bytes"
	"strings"
	"testing"
	"unicode/utf8"
)

func TestR4_TruncateForLog_DoesNotSplitUTF8Rune(t *testing.T) {
	t.Parallel()

	// 510 ASCII bytes + 4-byte emoji (514 bytes total; index 512 is inside the emoji).
	payload := append(bytes.Repeat([]byte("a"), 510), []byte("🚀tail")...)
	got := truncateForLog(payload)

	if !utf8.ValidString(got) {
		t.Fatalf("truncateForLog produced invalid UTF-8: %q", got)
	}
	wantPrefix := strings.Repeat("a", 510) + "...(truncated)"
	if got != wantPrefix {
		t.Fatalf("truncateForLog = %q, want %q", got, wantPrefix)
	}
}
