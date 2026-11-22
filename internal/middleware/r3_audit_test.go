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

package middleware

import "testing"

func TestIsSafeRequestID_RejectsWhitespaceAndNonASCII(t *testing.T) {
	for _, valid := range []string{"req-123", "abc_DEF.01:xyz", "a"} {
		if !isSafeRequestID(valid) {
			t.Errorf("isSafeRequestID(%q) = false, want true", valid)
		}
	}
	for _, bad := range []string{
		"",
		"req with space",
		"req\ttab",
		"req\nnewline",
		"req\rreturn",
		"req\x7fdel",
		"req-é",
		"req-\u00a0nbsp",
	} {
		if isSafeRequestID(bad) {
			t.Errorf("isSafeRequestID(%q) = true, want false", bad)
		}
	}
}
