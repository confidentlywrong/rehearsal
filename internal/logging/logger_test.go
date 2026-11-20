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

package logging

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
)

func TestNewLogger_TextDefault(t *testing.T) {
	var buf bytes.Buffer
	l := NewLogger(FormatText, slog.LevelInfo, &buf)
	l.Info("hello", slog.String("k", "v"))
	out := buf.String()
	if !strings.Contains(out, "msg=hello") || !strings.Contains(out, "k=v") {
		t.Fatalf("text handler missing fields: %q", out)
	}
}

func TestNewLogger_JSON(t *testing.T) {
	var buf bytes.Buffer
	l := NewLogger(FormatJSON, slog.LevelInfo, &buf)
	l.Info("hello", slog.String("k", "v"))
	var got map[string]any
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("json output not valid JSON: %v\n%s", err, buf.String())
	}
	if got["msg"] != "hello" {
		t.Errorf("expected msg=hello, got %v", got["msg"])
	}
	if got["level"] != "INFO" {
		t.Errorf("expected level=INFO, got %v", got["level"])
	}
	if got["k"] != "v" {
		t.Errorf("expected k=v, got %v", got["k"])
	}
}

func TestNewLogger_GCPRenamesFields(t *testing.T) {
	var buf bytes.Buffer
	l := NewLogger(FormatGCP, slog.LevelInfo, &buf)
	l.Warn("uh oh", slog.String("k", "v"))
	var got map[string]any
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("gcp output not valid JSON: %v\n%s", err, buf.String())
	}
	// Renamed fields:
	if got["message"] != "uh oh" {
		t.Errorf("expected message=uh oh, got %v", got["message"])
	}
	if got["severity"] != "WARNING" {
		t.Errorf("expected severity=WARNING, got %v", got["severity"])
	}
	if _, hasTimestamp := got["timestamp"]; !hasTimestamp {
		t.Errorf("expected timestamp field, got %v", got)
	}
	// Original keys must be GONE so Cloud Logging doesn't double-index:
	if _, hasMsg := got["msg"]; hasMsg {
		t.Errorf("gcp format should not emit msg key: %v", got)
	}
	if _, hasLevel := got["level"]; hasLevel {
		t.Errorf("gcp format should not emit level key: %v", got)
	}
}

func TestNewLogger_GCPSeverityMapping(t *testing.T) {
	cases := []struct {
		level slog.Level
		want  string
	}{
		{slog.LevelDebug, "DEBUG"},
		{slog.LevelInfo, "INFO"},
		{slog.LevelWarn, "WARNING"},
		{slog.LevelError, "ERROR"},
	}
	for _, tc := range cases {
		t.Run(tc.want, func(t *testing.T) {
			var buf bytes.Buffer
			l := NewLogger(FormatGCP, slog.LevelDebug, &buf)
			l.Log(context.Background(), tc.level, "x")
			var got map[string]any
			if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
				t.Fatalf("invalid JSON: %v", err)
			}
			if got["severity"] != tc.want {
				t.Errorf("level=%v: expected severity=%q got %v", tc.level, tc.want, got["severity"])
			}
		})
	}
}

func TestSetup_RespectsEnvFormat(t *testing.T) {
	// Snapshot + restore so test order doesn't matter.
	prev := slog.Default()
	defer slog.SetDefault(prev)

	t.Setenv("LOG_FORMAT", "json")
	t.Setenv("LOG_LEVEL", "warn")
	Setup()
	// Setup() can't be observed directly without redirecting stderr,
	// but formatFromEnv / levelFromEnv exposed via NewLogger let us
	// validate the same selection logic:
	if got := formatFromEnv(); got != FormatJSON {
		t.Errorf("formatFromEnv: want %q got %q", FormatJSON, got)
	}
	if got := levelFromEnv(); got != slog.LevelWarn {
		t.Errorf("levelFromEnv: want WARN got %v", got)
	}
}

func TestSetup_UnknownEnvFallsBack(t *testing.T) {
	t.Setenv("LOG_FORMAT", "yaml")
	t.Setenv("LOG_LEVEL", "trace")
	if got := formatFromEnv(); got != FormatText {
		t.Errorf("unknown LOG_FORMAT must fall back to text, got %q", got)
	}
	if got := levelFromEnv(); got != slog.LevelInfo {
		t.Errorf("unknown LOG_LEVEL must fall back to info, got %v", got)
	}
}

func TestRequestID_RoundTrip(t *testing.T) {
	ctx := context.Background()
	if got := RequestID(ctx); got != "" {
		t.Errorf("empty ctx must return empty request id, got %q", got)
	}
	ctx = WithRequestID(ctx, "req-abc")
	if got := RequestID(ctx); got != "req-abc" {
		t.Errorf("expected req-abc, got %q", got)
	}
	// Empty id is a no-op (keeps ctx clean):
	ctx2 := WithRequestID(context.Background(), "")
	if got := RequestID(ctx2); got != "" {
		t.Errorf("WithRequestID(\"\") must leave ctx clean, got %q", got)
	}
	// Nil ctx must not panic in either RequestID or WithRequestID:
	//nolint:staticcheck // SA1012 — exercising nil-ctx guard
	if got := RequestID(nil); got != "" {
		t.Errorf("RequestID(nil) must return empty string, got %q", got)
	}
	//nolint:staticcheck // SA1012 — exercising nil-ctx guard
	ctxFromNil := WithRequestID(nil, "req-from-nil")
	if got := RequestID(ctxFromNil); got != "req-from-nil" {
		t.Errorf("WithRequestID(nil, \"req-from-nil\") -> RequestID = %q, want \"req-from-nil\"", got)
	}
}

func TestFromContext_NeverReturnsNil(t *testing.T) {
	if FromContext(context.Background()) == nil {
		t.Fatal("FromContext returned nil for empty ctx")
	}
	//nolint:staticcheck // SA1012 — exercising nil-ctx guard
	if FromContext(nil) == nil {
		t.Fatal("FromContext returned nil for nil ctx")
	}
}

func TestFromContext_NoTenantNoAttrs(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	defer slog.SetDefault(prev)
	slog.SetDefault(NewLogger(FormatJSON, slog.LevelInfo, &buf))

	FromContext(context.Background()).Info("plain")

	var got map[string]any
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, buf.String())
	}
	for _, k := range []string{"request_id", "user_id", "role", "plan_tier"} {
		if _, has := got[k]; has {
			t.Errorf("expected no %q key on plain log, got %v", k, got)
		}
	}
}

func TestNewLogger_GCPPreservesGroupedAttrs(t *testing.T) {
	var buf bytes.Buffer
	l := NewLogger(FormatGCP, slog.LevelInfo, &buf)
	l.Info("top", slog.Group("payload",
		slog.String("msg", "nested-msg"),
		slog.String("level", "nested-level"),
		slog.String("time", "nested-time"),
	))
	var got map[string]any
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, buf.String())
	}
	if got["message"] != "top" || got["severity"] != "INFO" {
		t.Fatalf("top-level GCP keys missing: %v", got)
	}
	payload, ok := got["payload"].(map[string]any)
	if !ok {
		t.Fatalf("expected payload group object, got %T", got["payload"])
	}
	if payload["msg"] != "nested-msg" || payload["level"] != "nested-level" || payload["time"] != "nested-time" {
		t.Fatalf("grouped attributes were unexpectedly renamed: %v", payload)
	}
}

func TestFromContext_PopulatesUserContextWithoutRawEmail(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	t.Cleanup(func() { slog.SetDefault(prev) })
	slog.SetDefault(NewLogger(FormatJSON, slog.LevelInfo, &buf))

	ctx := account.WithContext(context.Background(), account.UserContext{
		UserID:       "u-123",
		Email:        "secret.user@example.com",
		IsSuperadmin: true,
	})
	FromContext(ctx).Info("authed")

	raw := buf.String()
	if strings.Contains(raw, "secret.user@example.com") {
		t.Fatalf("raw email leaked into structured log: %s", raw)
	}
	var got map[string]any
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if got["user_id"] != "u-123" || got["email_hash"] == "" || got["email_hash"] == nil || got["is_superadmin"] != true {
		t.Fatalf("expected user_id, email_hash, is_superadmin attrs, got %v", got)
	}
	if _, hasEmail := got["email"]; hasEmail {
		t.Fatalf("unexpected raw email key in log: %v", got)
	}
}

