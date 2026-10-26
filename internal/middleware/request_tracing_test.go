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
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// installTestTracer swaps the global TracerProvider for one that
// records spans into a tracetest.SpanRecorder. Returns the recorder
// + a t.Cleanup that restores the previous provider so test order is
// stable.
func installTestTracer(t *testing.T) *tracetest.SpanRecorder {
	t.Helper()
	prev := otel.GetTracerProvider()
	t.Cleanup(func() { otel.SetTracerProvider(prev) })
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithSpanProcessor(rec),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
	)
	otel.SetTracerProvider(tp)
	return rec
}

func newTracingRouter(serviceName string) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(RequestTracing(serviceName))
	return r
}

func findAttr(attrs []attribute.KeyValue, key string) (attribute.Value, bool) {
	for _, a := range attrs {
		if string(a.Key) == key {
			return a.Value, true
		}
	}
	return attribute.Value{}, false
}

func TestRequestTracing_CreatesSpanPerRequest(t *testing.T) {
	rec := installTestTracer(t)
	r := newTracingRouter("svc-x")
	r.GET("/ping", func(c *gin.Context) { c.Status(http.StatusOK) })

	req := httptest.NewRequest(http.MethodGet, "/ping", nil)
	r.ServeHTTP(httptest.NewRecorder(), req)

	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	got := spans[0]
	if got.Name() != "HTTP GET" {
		t.Errorf("span name: want \"HTTP GET\", got %q", got.Name())
	}
	if got.SpanKind() != trace.SpanKindServer {
		t.Errorf("span kind: want Server, got %v", got.SpanKind())
	}

	if v, ok := findAttr(got.Attributes(), "http.request.method"); !ok || v.AsString() != "GET" {
		t.Errorf("http.request.method: %v", got.Attributes())
	}
	if v, ok := findAttr(got.Attributes(), "url.path"); !ok || v.AsString() != "/ping" {
		t.Errorf("url.path: %v", got.Attributes())
	}
	if v, ok := findAttr(got.Attributes(), "service.name"); !ok || v.AsString() != "svc-x" {
		t.Errorf("service.name: %v", got.Attributes())
	}
	if v, ok := findAttr(got.Attributes(), "http.response.status_code"); !ok || v.AsInt64() != 200 {
		t.Errorf("status_code: %v", got.Attributes())
	}
}

func TestRequestTracing_5xxMarkedError(t *testing.T) {
	rec := installTestTracer(t)
	r := newTracingRouter("svc")
	r.GET("/boom", func(c *gin.Context) { c.Status(http.StatusInternalServerError) })

	req := httptest.NewRequest(http.MethodGet, "/boom", nil)
	r.ServeHTTP(httptest.NewRecorder(), req)

	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	if got := spans[0].Status().Code; got != codes.Error {
		t.Errorf("5xx must set span Error status, got %v", got)
	}
}

func TestRequestTracing_4xxNotMarkedError(t *testing.T) {
	rec := installTestTracer(t)
	r := newTracingRouter("svc")
	r.GET("/missing", func(c *gin.Context) { c.Status(http.StatusNotFound) })

	req := httptest.NewRequest(http.MethodGet, "/missing", nil)
	r.ServeHTTP(httptest.NewRecorder(), req)

	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	// 4xx is the client's fault — span must stay Unset (default), not Error.
	if got := spans[0].Status().Code; got != codes.Unset {
		t.Errorf("4xx must NOT mark span as Error, got %v", got)
	}
}

func TestRequestTracing_HonorsInboundTraceparent(t *testing.T) {
	// Install a propagator + a recording tracer.
	rec := installTestTracer(t)
	prevProp := otel.GetTextMapPropagator()
	t.Cleanup(func() { otel.SetTextMapPropagator(prevProp) })
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))
	r := newTracingRouter("svc")

	var captured trace.SpanContext
	r.GET("/x", func(c *gin.Context) {
		captured = trace.SpanContextFromContext(c.Request.Context())
		c.Status(http.StatusOK)
	})

	// W3C traceparent: version 00, trace-id (16 bytes hex), parent-id (8 bytes hex), sampled flag.
	const traceParent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	req.Header.Set("Traceparent", traceParent)
	r.ServeHTTP(httptest.NewRecorder(), req)

	if !captured.IsValid() {
		t.Fatal("inbound traceparent did not propagate into handler ctx")
	}
	wantTraceID := "0123456789abcdef0123456789abcdef"
	if got := captured.TraceID().String(); got != wantTraceID {
		t.Errorf("trace_id: want %s got %s", wantTraceID, got)
	}

	// And the recorded span should be in the same trace.
	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	if got := spans[0].SpanContext().TraceID().String(); got != wantTraceID {
		t.Errorf("recorded span trace_id: want %s got %s", wantTraceID, got)
	}
}

func TestRequestTracing_NoOpWhenTracingNotSetUp(t *testing.T) {
	// Don't install a TracerProvider — global is the no-op
	// TracerProvider that comes with otel by default. The
	// middleware must be safe to mount (no panic, no error).
	prev := otel.GetTracerProvider()
	t.Cleanup(func() { otel.SetTracerProvider(prev) })

	r := newTracingRouter("svc")
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("status: want 200 got %d", w.Code)
	}
	// The default global provider returns NoopTracer's spans which
	// we can't inspect, but we've proven the middleware doesn't
	// panic and the handler chain still runs.
}

func TestRequestTracing_AddsRawQuery(t *testing.T) {
	rec := installTestTracer(t)
	r := newTracingRouter("svc")
	r.GET("/x", func(c *gin.Context) { c.Status(http.StatusOK) })

	req := httptest.NewRequest(http.MethodGet, "/x?foo=bar&baz=qux", nil)
	r.ServeHTTP(httptest.NewRecorder(), req)

	spans := rec.Ended()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	if v, ok := findAttr(spans[0].Attributes(), "url.query"); !ok || v.AsString() != "foo=bar&baz=qux" {
		t.Errorf("url.query: %v", spans[0].Attributes())
	}
}
