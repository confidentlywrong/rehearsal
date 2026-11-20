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
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

type r4FakeProfileStore struct {
	user account.User
}

func (f *r4FakeProfileStore) GetUser(_ context.Context, _ string) (account.User, error) {
	return f.user, nil
}

func (f *r4FakeProfileStore) UpdateUserProfile(_ context.Context, _ string, displayName, timezone *string) error {
	if displayName != nil {
		f.user.DisplayName = *displayName
	}
	if timezone != nil {
		f.user.Timezone = *timezone
	}
	return nil
}

func TestR4_AuthExchange_RequestIsHTTPS_MultiHop(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name    string
		headers []string
		want    bool
	}{
		{name: "single https", headers: []string{"https"}, want: true},
		{name: "multi-hop https", headers: []string{"https, https"}, want: true},
		{name: "multi header https", headers: []string{"https", "HTTPS"}, want: true},
		{name: "spoofed https http", headers: []string{"https, http"}, want: false},
		{name: "empty", headers: nil, want: false},
	}
	for _, tc := range cases {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/exchange", nil)
		for _, v := range tc.headers {
			req.Header.Add("X-Forwarded-Proto", v)
		}
		if got := requestIsHTTPS(req); got != tc.want {
			t.Errorf("%s: requestIsHTTPS = %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestR4_SanitizeAndTruncateRunes_UTF8Safe(t *testing.T) {
	t.Parallel()

	// 79 ASCII bytes + 4-byte emoji + extra runes: byte 80 lands mid-emoji.
	in := strings.Repeat("a", 79) + "🚀extra"
	got := sanitizeAndTruncateRunes(in, maxDisplayNameLen)
	if !utf8.ValidString(got) {
		t.Fatalf("sanitizeAndTruncateRunes emitted invalid UTF-8: %q", got)
	}
	if utf8.RuneCountInString(got) != maxDisplayNameLen {
		t.Fatalf("rune count = %d, want %d", utf8.RuneCountInString(got), maxDisplayNameLen)
	}
	if got != strings.Repeat("a", 79)+"🚀" {
		t.Fatalf("unexpected truncated display name: %q", got)
	}

	// Broken raw UTF-8 bytes must also be repaired before hitting Postgres.
	invalid := "Alice\xff\xfeBob"
	repaired := sanitizeAndTruncateRunes(invalid, maxDisplayNameLen)
	if !utf8.ValidString(repaired) {
		t.Fatalf("repaired string must be valid UTF-8, got %q", repaired)
	}
}

func TestR4_UpdateProfileHandler_RuneLengthAndUTF8Validation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	store := &r4FakeProfileStore{
		user: account.User{ID: "u-r4", Email: "u@example.com"},
	}

	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Request = c.Request.WithContext(account.WithContext(c.Request.Context(), account.UserContext{
			UserID: "u-r4",
			Email:  "u@example.com",
		}))
		c.Next()
	})
	r.PATCH("/api/v1/me/profile", updateProfileHandler(store))

	// 30 CJK runes = 90 UTF-8 bytes (> 80 bytes) but 30 runes <= 80 max_chars -> 200 OK.
	okName := strings.Repeat("中", 30)
	bodyOK, _ := json.Marshal(map[string]string{"display_name": okName})
	reqOK := httptest.NewRequest(http.MethodPatch, "/api/v1/me/profile", bytes.NewReader(bodyOK))
	reqOK.Header.Set("Content-Type", "application/json")
	wOK := httptest.NewRecorder()
	r.ServeHTTP(wOK, reqOK)
	if wOK.Code != http.StatusOK {
		t.Fatalf("30-rune CJK display_name: status = %d, body = %s", wOK.Code, wOK.Body.String())
	}

	// 81 runes must return 400 display_name_too_long.
	longName := strings.Repeat("é", 81)
	bodyLong, _ := json.Marshal(map[string]string{"display_name": longName})
	reqLong := httptest.NewRequest(http.MethodPatch, "/api/v1/me/profile", bytes.NewReader(bodyLong))
	reqLong.Header.Set("Content-Type", "application/json")
	wLong := httptest.NewRecorder()
	r.ServeHTTP(wLong, reqLong)
	if wLong.Code != http.StatusBadRequest || !strings.Contains(wLong.Body.String(), "display_name_too_long") {
		t.Fatalf("81-rune display_name: status = %d, body = %s", wLong.Code, wLong.Body.String())
	}
}
