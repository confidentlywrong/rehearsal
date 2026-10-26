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

package middleware

import (
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
)

// requestIDHeader is the HTTP header used both for inbound trust
// (clients/load balancers can supply their own) and the response
// echo. Matches the de-facto standard most observability stacks
// already understand.
const requestIDHeader = "X-Request-ID"

// RequestLogger returns a Gin middleware that:
//
//  1. Attaches a request_id to the request context (preferring an
//     inbound X-Request-ID header, otherwise generating a fresh one).
//  2. Echoes the request_id in the response header so clients can
//     correlate.
//  3. Logs ONE structured "http.request" entry per request after
//     the handler chain completes, with method, path, status,
//     latency_ms, client_ip, plus any tenant attributes the tenant
//     middleware has already attached to ctx by then.
//
// Mount it BEFORE the tenant middleware so the request_id is
// available even on auth failures. The end-of-request log is fired
// in a deferred call, so it picks up tenant attrs that downstream
// middleware adds.
func RequestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()

		id := strings.TrimSpace(c.GetHeader(requestIDHeader))
		if id == "" || !isSafeRequestID(id) {
			id = newRequestID()
		}

		// Attach to ctx for any downstream code that calls
		// logging.FromContext, and stash on the gin.Context for
		// handlers that prefer c.GetString("request_id").
		c.Request = c.Request.WithContext(logging.WithRequestID(c.Request.Context(), id))
		c.Set("request_id", id)
		c.Header(requestIDHeader, id)

		c.Next()

		latencyMs := time.Since(start).Milliseconds()
		status := c.Writer.Status()

		// Pull the logger AFTER c.Next() so we capture tenant attrs
		// that the tenant middleware injected during the handler.
		logger := logging.FromContext(c.Request.Context())

		level := slog.LevelInfo
		switch {
		case status >= http.StatusInternalServerError:
			level = slog.LevelError
		case status >= http.StatusBadRequest:
			level = slog.LevelWarn
		}

		// gin collects per-request errors via c.Error(); surface them
		// as a single comma-joined string when present.
		var errStr string
		if len(c.Errors) > 0 {
			errStr = c.Errors.String()
		}

		attrs := []any{
			slog.String("method", c.Request.Method),
			slog.String("path", c.Request.URL.Path),
			slog.Int("status", status),
			slog.Int64("latency_ms", latencyMs),
			slog.String("client_ip", c.ClientIP()),
		}
		if c.Request.URL.RawQuery != "" {
			attrs = append(attrs, slog.String("query", c.Request.URL.RawQuery))
		}
		if errStr != "" {
			attrs = append(attrs, slog.String("error", errStr))
		}
		logger.Log(c.Request.Context(), level, "http.request", attrs...)
	}
}

// newRequestID returns a 16-byte hex string. crypto/rand failure
// (extremely rare) falls back to a timestamp so we never panic in
// the request path.
func newRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// Best-effort fallback. The collision risk on a single
		// process even at 10k req/s is acceptable for log correlation.
		return "ts-" + time.Now().UTC().Format("20060102T150405.000000000")
	}
	return hex.EncodeToString(b[:])
}

// isSafeRequestID accepts inbound IDs only when they're short and
// look like an opaque token. Rejects anything with whitespace, CR/LF,
// or that's longer than 128 chars — defense-in-depth against header-
// injection attempts where the request_id ends up echoed in our log.
func isSafeRequestID(s string) bool {
	if len(s) == 0 || len(s) > 128 {
		return false
	}
	for _, r := range s {
		if r < 0x20 || r == 0x7f {
			return false
		}
	}
	return true
}
