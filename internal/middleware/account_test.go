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
	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

func TestRequireSuperadmin(t *testing.T) {
	gin.SetMode(gin.TestMode)

	tests := []struct {
		name       string
		user       *account.UserContext
		wantStatus int
	}{
		{
			name:       "No User Context",
			user:       nil,
			wantStatus: http.StatusUnauthorized,
		},
		{
			name: "Not Superadmin",
			user: &account.UserContext{
				UserID:       "user-1",
				IsSuperadmin: false,
			},
			wantStatus: http.StatusForbidden,
		},
		{
			name: "Is Superadmin",
			user: &account.UserContext{
				UserID:       "user-2",
				IsSuperadmin: true,
			},
			wantStatus: http.StatusOK,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r := gin.New()
			
			if tc.user != nil {
				r.Use(func(c *gin.Context) {
					c.Set("account", *tc.user)
					c.Next()
				})
			}

			r.GET("/admin", RequireSuperadmin(), func(c *gin.Context) {
				c.Status(http.StatusOK)
			})

			w := httptest.NewRecorder()
			req, _ := http.NewRequest("GET", "/admin", nil)
			r.ServeHTTP(w, req)

			if w.Code != tc.wantStatus {
				t.Errorf("Expected %d, got %d", tc.wantStatus, w.Code)
			}
		})
	}
}

func TestRequireUser(t *testing.T) {
	gin.SetMode(gin.TestMode)

	tests := []struct {
		name       string
		user       *account.UserContext
		wantStatus int
	}{
		{
			name:       "No User Context",
			user:       nil,
			wantStatus: http.StatusUnauthorized,
		},
		{
			name: "Valid User",
			user: &account.UserContext{
				UserID: "user-1",
			},
			wantStatus: http.StatusOK,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			r := gin.New()
			
			if tc.user != nil {
				r.Use(func(c *gin.Context) {
					c.Set("account", *tc.user)
					c.Next()
				})
			}

			r.GET("/user", RequireUser(), func(c *gin.Context) {
				c.Status(http.StatusOK)
			})

			w := httptest.NewRecorder()
			req, _ := http.NewRequest("GET", "/user", nil)
			r.ServeHTTP(w, req)

			if w.Code != tc.wantStatus {
				t.Errorf("Expected %d, got %d", tc.wantStatus, w.Code)
			}
		})
	}
}
