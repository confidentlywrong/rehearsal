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

// Package tracing wires OpenTelemetry distributed tracing for
// Race Condition's Go services (G5b).
//
// Design tenets:
//
//  1. Off by default. With no env vars set, Setup returns a no-op
//     TracerProvider so `make start` is byte-identical to legacy.
//  2. One env knob to opt in: OTEL_EXPORTER=stdout (local debug)
//     or =gcp (Cloud Trace via the operations-go exporter).
//  3. Head sampling at 10% by default (OTEL_SAMPLE_RATIO=0.1) so
//     Cloud Trace stays under the 2.5M-spans/month free tier even
//     on busy days. Set to 1.0 for full sampling, 0.0 to drop all.
//  4. Resource attrs (service.name, service.version,
//     deployment.environment) are populated from env so a single
//     binary running on Cloud Run + a local copy show up as distinct
//     services in Cloud Trace.
//
// The package only wires the SDK + exporters. The actual per-request
// span is created by middleware.RequestTracing in internal/middleware,
// which depends only on otel.Tracer("...") and is therefore safe even
// when Setup was never called (no-op spans).
package tracing

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Exporter is the destination for span data.
type Exporter string

const (
	// ExporterNone is the default: a no-op TracerProvider. Spans
	// created via otel.Tracer("...") are inert, NoopSpan instances.
	// No data leaves the process.
	ExporterNone Exporter = ""

	// ExporterStdout writes spans as JSON-ish to stderr. Local
	// debugging only — high volume in prod.
	ExporterStdout Exporter = "stdout"

	// ExporterGCP exports to Google Cloud Trace via the
	// opentelemetry-operations-go exporter. Uses Application
	// Default Credentials and the GOOGLE_CLOUD_PROJECT env var.
	ExporterGCP Exporter = "gcp"
)

// Config is what Setup uses to build the provider. Public so tests
// can drive it without env vars.
type Config struct {
	// ServiceName is required when an exporter is configured. Maps
	// to the resource attribute service.name; this is what shows up
	// in Cloud Trace's "Service" facet.
	ServiceName string

	// ServiceVersion populates service.version. Optional; defaults
	// to "unknown".
	ServiceVersion string

	// Environment populates deployment.environment, e.g. "prod" /
	// "staging" / "local". Optional.
	Environment string

	// Exporter selects the destination. ExporterNone (default)
	// returns a no-op provider — Setup is still safe to call.
	Exporter Exporter

	// SampleRatio is the head-sampling probability in [0, 1].
	// Out-of-range values are clamped. 0.0 drops everything,
	// 1.0 samples every request, default 0.1.
	SampleRatio float64

	// GCPProject is the project ID for Cloud Trace. Required when
	// Exporter=gcp; falls back to GOOGLE_CLOUD_PROJECT env. Empty
	// + ExporterGCP returns an error.
	GCPProject string
}

// ConfigFromEnv builds a Config from environment variables. It is
// the single source of truth for tracing knobs:
//
//   OTEL_EXPORTER          ""|stdout|gcp     default ""
//   OTEL_SAMPLE_RATIO      float in [0,1]    default 0.1
//   OTEL_SERVICE_NAME      string            default = serviceName arg
//   OTEL_SERVICE_VERSION   string            default "unknown"
//   OTEL_ENVIRONMENT       string            default ""
//   GOOGLE_CLOUD_PROJECT   project id        used when exporter=gcp
func ConfigFromEnv(serviceName string) Config {
	cfg := Config{
		ServiceName:    envOr("OTEL_SERVICE_NAME", serviceName),
		ServiceVersion: envOr("OTEL_SERVICE_VERSION", "unknown"),
		Environment:    os.Getenv("OTEL_ENVIRONMENT"),
		Exporter:       Exporter(strings.ToLower(strings.TrimSpace(os.Getenv("OTEL_EXPORTER")))),
		GCPProject:     os.Getenv("GOOGLE_CLOUD_PROJECT"),
		SampleRatio:    0.1,
	}
	if v := strings.TrimSpace(os.Getenv("OTEL_SAMPLE_RATIO")); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			cfg.SampleRatio = f
		}
	}
	return cfg
}

// Shutdown is the cleanup func returned by Setup. It is safe to
// call multiple times and on a nil receiver. Always invoke it via
// defer in main() so the SDK flushes any buffered spans before exit.
type Shutdown func(context.Context) error

// Setup applies cfg to the global OTel TracerProvider + propagator.
// Returns a Shutdown that flushes pending spans (no-op when the
// exporter is None).
//
// Errors:
//   - ExporterGCP without GCPProject (operator misconfig)
//   - exporter constructor failures (network unreachable etc.)
//
// Setup never panics. Caller is expected to log + decide whether to
// abort startup; tracing failures generally should NOT take down the
// service since logs + structured metrics still work.
func Setup(ctx context.Context, cfg Config) (Shutdown, error) {
	// Always install the W3C tracecontext + baggage propagator so
	// inbound traceparent headers are honored even when the local
	// exporter is None — a downstream service with tracing enabled
	// will still get a continuous trace.
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	if cfg.Exporter == ExporterNone {
		// No-op provider. otel.Tracer("...") returns a NoopTracer.
		return noopShutdown, nil
	}

	exp, err := buildExporter(ctx, cfg)
	if err != nil {
		return noopShutdown, fmt.Errorf("tracing: build exporter: %w", err)
	}

	res, err := buildResource(ctx, cfg)
	if err != nil {
		return noopShutdown, fmt.Errorf("tracing: build resource: %w", err)
	}

	sampler := sdktrace.ParentBased(
		sdktrace.TraceIDRatioBased(clampSampleRatio(cfg.SampleRatio)),
	)

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sampler),
	)
	otel.SetTracerProvider(tp)

	slog.Default().Info("tracing.enabled",
		slog.String("exporter", string(cfg.Exporter)),
		slog.String("service_name", cfg.ServiceName),
		slog.Float64("sample_ratio", clampSampleRatio(cfg.SampleRatio)),
	)

	return func(shutdownCtx context.Context) error {
		// Bound shutdown to keep main()'s defer fast even when the
		// exporter is stuck.
		bounded, cancel := context.WithTimeout(shutdownCtx, 5*time.Second)
		defer cancel()
		return tp.Shutdown(bounded)
	}, nil
}

// SetupFromEnv is the one-line entry-point most binaries use:
//
//	cleanup, err := tracing.SetupFromEnv(ctx, "race-gateway")
//	defer cleanup(context.Background())
func SetupFromEnv(ctx context.Context, serviceName string) (Shutdown, error) {
	return Setup(ctx, ConfigFromEnv(serviceName))
}

// noopShutdown is the cleanup returned when tracing is disabled.
// Always returns nil; safe to call any number of times.
func noopShutdown(context.Context) error { return nil }

func envOr(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func clampSampleRatio(r float64) float64 {
	if r < 0 || r != r { // NaN catch
		return 0
	}
	if r > 1 {
		return 1
	}
	return r
}

// validateGCPConfig is exported indirectly via Setup but kept
// separate so unit tests can assert on the error wording without
// having to stand up an actual exporter.
func validateGCPConfig(cfg Config) error {
	if cfg.GCPProject == "" {
		return errors.New("OTEL_EXPORTER=gcp requires GOOGLE_CLOUD_PROJECT")
	}
	return nil
}
