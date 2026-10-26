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

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/logging"
)

// tracerName is the instrumentation library name reported on
// every span this middleware creates. Cloud Trace surfaces it in
// the span list so operators can tell library spans apart from
// in-app spans.
const tracerName = "github.com/GoogleCloudPlatform/race-condition/internal/middleware"

// RequestTracing returns a Gin middleware that creates one span per
// request named `HTTP <METHOD>` with semconv-compatible attributes:
//
//   - http.request.method, url.path, url.query, http.client_ip
//   - http.response.status_code (set after handlers)
//   - request.id (request_id from logging.WithRequestID)
//   - org.id, user.id, role (when tenant middleware has run)
//
// Inbound traceparent / tracestate / baggage headers are honored via
// the global TextMapPropagator, so a trace started by an upstream
// service (LB → gateway → admin) stays one continuous trace.
//
// Mount AFTER RequestLogger so spans inherit the request_id that
// middleware generates, and BEFORE the tenant middleware so the
// span captures the tenant attrs in the deferred SetAttributes call
// at end-of-request.
//
// Setup-free: when tracing.Setup has not been called (or
// OTEL_EXPORTER is empty), otel.Tracer returns the no-op tracer
// and this middleware adds zero overhead beyond a context lookup.
func RequestTracing(serviceName string) gin.HandlerFunc {
	tracer := otel.Tracer(tracerName)
	propagator := otel.GetTextMapPropagator()

	return func(c *gin.Context) {
		// Extract upstream context from inbound headers (W3C
		// tracecontext + baggage). When no headers are present this
		// returns the unchanged ctx, so a fresh trace is started.
		parentCtx := propagator.Extract(c.Request.Context(),
			propagation.HeaderCarrier(c.Request.Header))

		spanName := "HTTP " + c.Request.Method
		ctx, span := tracer.Start(parentCtx, spanName, trace.WithSpanKind(trace.SpanKindServer))
		// Always end the span — even on panic, deferred runs.
		defer span.End()

		// Initial attrs (request-shape data we know up front).
		attrs := []attribute.KeyValue{
			attribute.String("http.request.method", c.Request.Method),
			attribute.String("url.path", c.Request.URL.Path),
			attribute.String("http.client_ip", c.ClientIP()),
			attribute.String("service.name", serviceName),
		}
		if q := c.Request.URL.RawQuery; q != "" {
			attrs = append(attrs, attribute.String("url.query", q))
		}
		if rid := logging.RequestID(c.Request.Context()); rid != "" {
			attrs = append(attrs, attribute.String("request.id", rid))
		}
		span.SetAttributes(attrs...)

		// Propagate the new ctx (containing the active span) so
		// downstream handlers' OTel calls + logging.FromContext
		// see the same span context.
		c.Request = c.Request.WithContext(ctx)

		c.Next()

		// Post-handler attrs: status + tenant fields that other
		// middleware injected during the request.
		status := c.Writer.Status()
		span.SetAttributes(attribute.Int("http.response.status_code", status))

		if uc := account.FromContext(c.Request.Context()); uc.UserID != "" {
			span.SetAttributes(attribute.String("user.id", uc.UserID))
			if uc.Email != "" {
				span.SetAttributes(attribute.String("user.email", uc.Email))
			}
			if uc.IsSuperadmin {
				span.SetAttributes(attribute.Bool("user.superadmin", true))
			}
		}

		// Map HTTP status to span status. Per OTel HTTP semconv:
		//   - server-side spans only mark Error for 5xx (4xx is the
		//     client's fault, not the server's). Cloud Trace surfaces
		//     codes.Error spans in red on the timeline, so this avoids
		//     turning the whole UI red on every 401.
		if status >= http.StatusInternalServerError {
			span.SetStatus(codes.Error, http.StatusText(status))
		}

		// Surface gin-aggregated errors as a span event.
		if len(c.Errors) > 0 {
			span.SetAttributes(attribute.String("error.message", c.Errors.String()))
		}
	}
}
