// Copyright 2026 The Confidently Wrong Authors
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

// Package logging is the structured-logging seam for Race Condition's
// Go services (G5a). It is a thin wrapper around log/slog that adds:
//
//   - A single Setup() that switches between a human-readable text
//     handler (local dev, default) and a JSON handler (cloud) based
//     on the LOG_FORMAT env var.
//   - GCP Cloud Logging field renaming (`level`→`severity`,
//     `msg`→`message`, `time`→`timestamp`) when LOG_FORMAT=gcp, so
//     logs land correctly in stackdriver without an external agent.
//   - Per-request context plumbing (see context.go) so handlers can
//     emit logs that automatically carry request_id/user_id.
//
// The package deliberately does NOT replace `log.Printf` everywhere —
// instead it gives high-value sites (auth, billing webhooks, quota
// enforcement, agent dispatch errors) a structured emission path that
// the rest of the codebase can adopt incrementally. Old `log.Printf`
// calls still work; they just lack tenant context.
package logging

import (
	"io"
	"log/slog"
	"os"
	"strings"
)

// Format is one of the supported handler formats.
type Format string

const (
	// FormatText is the human-readable slog.TextHandler. Default for
	// local dev. Honors LOG_LEVEL.
	FormatText Format = "text"

	// FormatJSON is plain slog.JSONHandler. Useful for any log
	// aggregator that consumes JSON (Loki, Datadog, etc.).
	FormatJSON Format = "json"

	// FormatGCP is JSON with field names remapped to GCP Cloud
	// Logging conventions (severity / message / timestamp). Use when
	// running on Cloud Run / GKE so logs are categorized by severity
	// in the Cloud Logging UI.
	FormatGCP Format = "gcp"
)

// Setup configures slog.Default() based on env vars:
//
//   - LOG_FORMAT: "text" (default), "json", or "gcp".
//   - LOG_LEVEL:  "debug", "info" (default), "warn", or "error".
//
// Setup is safe to call from main() once. Calling it twice replaces
// the previous default; tests usually skip Setup and use slog directly.
func Setup() {
	slog.SetDefault(NewLogger(formatFromEnv(), levelFromEnv(), os.Stderr))
}

// NewLogger constructs a *slog.Logger with the given format + level
// writing to w. Exposed for tests (which use a bytes.Buffer to assert
// on output) and for binaries that want a non-default writer.
func NewLogger(f Format, lvl slog.Level, w io.Writer) *slog.Logger {
	opts := &slog.HandlerOptions{Level: lvl}
	switch f {
	case FormatJSON:
		return slog.New(slog.NewJSONHandler(w, opts))
	case FormatGCP:
		opts.ReplaceAttr = gcpReplaceAttr
		return slog.New(slog.NewJSONHandler(w, opts))
	case FormatText:
		fallthrough
	default:
		return slog.New(slog.NewTextHandler(w, opts))
	}
}

// gcpReplaceAttr remaps the three slog built-ins to the field names
// Cloud Logging expects on stdout/stderr when no logging agent
// processes the line. See:
// https://cloud.google.com/logging/docs/structured-logging
func gcpReplaceAttr(_ []string, a slog.Attr) slog.Attr {
	switch a.Key {
	case slog.LevelKey:
		// slog levels: DEBUG / INFO / WARN / ERROR.
		// Cloud Logging severities: DEBUG / INFO / WARNING / ERROR.
		lvl, ok := a.Value.Any().(slog.Level)
		sev := "INFO"
		if ok {
			switch {
			case lvl <= slog.LevelDebug:
				sev = "DEBUG"
			case lvl < slog.LevelWarn:
				sev = "INFO"
			case lvl < slog.LevelError:
				sev = "WARNING"
			default:
				sev = "ERROR"
			}
		}
		return slog.String("severity", sev)
	case slog.MessageKey:
		return slog.Attr{Key: "message", Value: a.Value}
	case slog.TimeKey:
		return slog.Attr{Key: "timestamp", Value: a.Value}
	}
	return a
}

// formatFromEnv reads LOG_FORMAT and returns the corresponding Format.
// Unknown values fall back to FormatText so a typo never silently
// breaks logging.
func formatFromEnv() Format {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("LOG_FORMAT"))) {
	case "json":
		return FormatJSON
	case "gcp":
		return FormatGCP
	default:
		return FormatText
	}
}

// levelFromEnv reads LOG_LEVEL and returns the corresponding slog.Level.
// Unknown values fall back to LevelInfo.
func levelFromEnv() slog.Level {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("LOG_LEVEL"))) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
