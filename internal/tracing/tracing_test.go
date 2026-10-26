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

package tracing

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

func TestConfigFromEnv_Defaults(t *testing.T) {
	t.Setenv("OTEL_EXPORTER", "")
	t.Setenv("OTEL_SAMPLE_RATIO", "")
	t.Setenv("OTEL_SERVICE_NAME", "")
	t.Setenv("OTEL_SERVICE_VERSION", "")
	t.Setenv("OTEL_ENVIRONMENT", "")
	t.Setenv("GOOGLE_CLOUD_PROJECT", "")

	cfg := ConfigFromEnv("svc-fallback")
	if cfg.Exporter != ExporterNone {
		t.Errorf("default exporter must be None, got %q", cfg.Exporter)
	}
	if cfg.SampleRatio != 0.1 {
		t.Errorf("default sample ratio: want 0.1, got %v", cfg.SampleRatio)
	}
	if cfg.ServiceName != "svc-fallback" {
		t.Errorf("ServiceName fell through wrong: got %q", cfg.ServiceName)
	}
	if cfg.ServiceVersion != "unknown" {
		t.Errorf("ServiceVersion fallback: want unknown, got %q", cfg.ServiceVersion)
	}
}

func TestConfigFromEnv_HonorsAllVars(t *testing.T) {
	t.Setenv("OTEL_EXPORTER", "GCP")
	t.Setenv("OTEL_SAMPLE_RATIO", "0.5")
	t.Setenv("OTEL_SERVICE_NAME", "race-gateway")
	t.Setenv("OTEL_SERVICE_VERSION", "v1.2.3")
	t.Setenv("OTEL_ENVIRONMENT", "prod")
	t.Setenv("GOOGLE_CLOUD_PROJECT", "my-project")

	cfg := ConfigFromEnv("ignored-fallback")
	if cfg.Exporter != ExporterGCP {
		t.Errorf("Exporter: want gcp got %q", cfg.Exporter)
	}
	if cfg.SampleRatio != 0.5 {
		t.Errorf("SampleRatio: want 0.5 got %v", cfg.SampleRatio)
	}
	if cfg.ServiceName != "race-gateway" {
		t.Errorf("ServiceName: want race-gateway got %q", cfg.ServiceName)
	}
	if cfg.ServiceVersion != "v1.2.3" {
		t.Errorf("ServiceVersion: want v1.2.3 got %q", cfg.ServiceVersion)
	}
	if cfg.Environment != "prod" {
		t.Errorf("Environment: want prod got %q", cfg.Environment)
	}
	if cfg.GCPProject != "my-project" {
		t.Errorf("GCPProject: want my-project got %q", cfg.GCPProject)
	}
}

func TestConfigFromEnv_BadSampleRatioIgnored(t *testing.T) {
	t.Setenv("OTEL_SAMPLE_RATIO", "not-a-float")
	cfg := ConfigFromEnv("svc")
	if cfg.SampleRatio != 0.1 {
		t.Errorf("bad ratio must keep default 0.1, got %v", cfg.SampleRatio)
	}
}

func TestClampSampleRatio(t *testing.T) {
	cases := []struct {
		in, want float64
	}{
		{0.0, 0.0},
		{0.5, 0.5},
		{1.0, 1.0},
		{-0.1, 0},
		{1.5, 1},
		{math.NaN(), 0},
	}
	for _, tc := range cases {
		if got := clampSampleRatio(tc.in); got != tc.want {
			t.Errorf("clampSampleRatio(%v): want %v got %v", tc.in, tc.want, got)
		}
	}
}

func TestSetup_NoExporterIsNoop(t *testing.T) {
	cleanup, err := Setup(context.Background(), Config{Exporter: ExporterNone})
	if err != nil {
		t.Fatalf("Setup(None) returned error: %v", err)
	}
	if cleanup == nil {
		t.Fatal("cleanup must never be nil")
	}
	// Calling cleanup multiple times must be safe.
	if err := cleanup(context.Background()); err != nil {
		t.Errorf("cleanup #1 returned error: %v", err)
	}
	if err := cleanup(context.Background()); err != nil {
		t.Errorf("cleanup #2 returned error: %v", err)
	}
}

func TestSetup_AlwaysInstallsPropagator(t *testing.T) {
	// Reset propagator to a known-zero state.
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator())

	cleanup, err := Setup(context.Background(), Config{Exporter: ExporterNone})
	if err != nil {
		t.Fatalf("Setup error: %v", err)
	}
	defer func() {
		_ = cleanup(context.Background())
	}()

	// The composite propagator must include W3C tracecontext;
	// inspect Fields() for "traceparent".
	got := otel.GetTextMapPropagator().Fields()
	hasTraceparent := false
	for _, f := range got {
		if f == "traceparent" {
			hasTraceparent = true
		}
	}
	if !hasTraceparent {
		t.Errorf("propagator must include W3C tracecontext, got fields: %v", got)
	}
}

func TestSetup_GCPRequiresProject(t *testing.T) {
	_, err := Setup(context.Background(), Config{
		Exporter:    ExporterGCP,
		ServiceName: "svc",
		// GCPProject deliberately empty
	})
	if err == nil {
		t.Fatal("expected error when ExporterGCP without GCPProject")
	}
	if !strings.Contains(err.Error(), "GOOGLE_CLOUD_PROJECT") {
		t.Errorf("error must mention GOOGLE_CLOUD_PROJECT, got: %v", err)
	}
}

func TestSetup_UnknownExporterErrors(t *testing.T) {
	_, err := Setup(context.Background(), Config{
		Exporter:    Exporter("loki"),
		ServiceName: "svc",
	})
	if err == nil {
		t.Fatal("expected error for unknown exporter")
	}
}

func TestSetup_StdoutBuilds(t *testing.T) {
	cleanup, err := Setup(context.Background(), Config{
		Exporter:       ExporterStdout,
		ServiceName:    "test-svc",
		ServiceVersion: "v0",
		Environment:    "test",
		SampleRatio:    1.0,
	})
	if err != nil {
		t.Fatalf("Setup(Stdout) error: %v", err)
	}
	if cleanup == nil {
		t.Fatal("cleanup must never be nil")
	}
	// Shutdown the provider so subsequent tests get a fresh global.
	if err := cleanup(context.Background()); err != nil {
		t.Errorf("cleanup error: %v", err)
	}
}

func TestValidateGCPConfig(t *testing.T) {
	if err := validateGCPConfig(Config{GCPProject: "x"}); err != nil {
		t.Errorf("non-empty project must validate, got %v", err)
	}
	err := validateGCPConfig(Config{GCPProject: ""})
	if err == nil {
		t.Fatal("empty project must error")
	}
	if !errors.Is(err, err) { // sanity: error implements error
		t.Fatal("validateGCPConfig must return an error")
	}
}

func TestEnvOr(t *testing.T) {
	t.Setenv("MY_KEY", "")
	if got := envOr("MY_KEY", "fb"); got != "fb" {
		t.Errorf("empty env must use fallback, got %q", got)
	}
	t.Setenv("MY_KEY", "  spaced  ")
	if got := envOr("MY_KEY", "fb"); got != "spaced" {
		t.Errorf("env must be trimmed, got %q", got)
	}
}
