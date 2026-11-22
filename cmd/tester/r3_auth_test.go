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

package main

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestRequireAuth_CaseInsensitiveAndMalformedBearer(t *testing.T) {
	t.Setenv("TESTER_REQUIRE_AUTH", "true")
	r := setupRouter("")

	for _, hdr := range []string{"bearer ops-token", "BEARER\tops-token", "  Bearer   ops.tok  "} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/tester/health", nil)
		req.Header.Set("Authorization", hdr)
		r.ServeHTTP(w, req)
		require.NotEqual(t, http.StatusUnauthorized, w.Code, "expected %q to pass auth check", hdr)
	}

	for _, bad := range []string{"Bearerfoo", "Bearer tok with space", "Bearer \t "} {
		w := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/tester/health", nil)
		req.Header.Set("Authorization", bad)
		r.ServeHTTP(w, req)
		require.Equal(t, http.StatusUnauthorized, w.Code, "expected %q to be rejected with 401", bad)
	}
}
