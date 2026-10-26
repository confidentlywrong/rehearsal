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

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestRateLimitIfEnabled(t *testing.T) {
	gin.SetMode(gin.TestMode)

	tests := []struct {
		name       string
		capacity   int64
		wantStatus int
	}{
		{
			name:       "Disabled (Cap 0)",
			capacity:   0,
			wantStatus: http.StatusOK,
		},
		{
			name:       "Disabled (Cap -1)",
			capacity:   -1,
			wantStatus: http.StatusOK,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r := gin.New()
			opts := RateLimitOptions{
				Client:   nil,
				Capacity: tc.capacity,
			}
			
			r.Use(RateLimitIfEnabled(opts))
			r.GET("/", func(c *gin.Context) {
				c.Status(http.StatusOK)
			})

			w := httptest.NewRecorder()
			req, _ := http.NewRequest("GET", "/", nil)
			r.ServeHTTP(w, req)

			if w.Code != tc.wantStatus {
				t.Errorf("Expected %d, got %d", tc.wantStatus, w.Code)
			}
		})
	}
}

func TestWithSkips(t *testing.T) {
	opts := RateLimitOptions{
		Capacity: 100,
	}
	opts = opts.WithSkips("/health")
	if len(opts.SkipPaths) != 1 || opts.SkipPaths[0] != "/health" {
		t.Errorf("Expected SkipPaths to contain /health")
	}
}
