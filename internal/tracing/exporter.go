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
	"fmt"
	"os"

	cloudtrace "github.com/GoogleCloudPlatform/opentelemetry-operations-go/exporter/trace"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// buildExporter dispatches on cfg.Exporter and returns a configured
// SpanExporter. The two supported destinations:
//
//   - stdouttrace.New: pretty-prints spans to stderr. Useful for
//     verifying instrumentation locally without standing up Cloud
//     Trace; produces verbose output, do not use in prod.
//   - cloudtrace.New: ships spans to Cloud Trace via Application
//     Default Credentials. Requires GOOGLE_CLOUD_PROJECT.
func buildExporter(ctx context.Context, cfg Config) (sdktrace.SpanExporter, error) {
	switch cfg.Exporter {
	case ExporterStdout:
		return stdouttrace.New(stdouttrace.WithWriter(os.Stderr))
	case ExporterGCP:
		if err := validateGCPConfig(cfg); err != nil {
			return nil, err
		}
		return cloudtrace.New(cloudtrace.WithProjectID(cfg.GCPProject))
	default:
		return nil, fmt.Errorf("tracing: unknown exporter %q", cfg.Exporter)
	}
}

// buildResource attaches resource attributes that survive into every
// span. service.name is what categorizes services in Cloud Trace's
// UI; service.version + deployment.environment make it possible to
// correlate a regression with a specific deploy.
func buildResource(ctx context.Context, cfg Config) (*resource.Resource, error) {
	attrs := []attribute.KeyValue{
		semconv.ServiceName(cfg.ServiceName),
		semconv.ServiceVersion(cfg.ServiceVersion),
	}
	if cfg.Environment != "" {
		attrs = append(attrs, semconv.DeploymentEnvironment(cfg.Environment))
	}
	return resource.New(ctx,
		// Note: no WithSchemaURL — the SDK and our semconv pkg can
		// disagree on the schema version, which makes resource.New
		// return an error. Cloud Trace doesn't require a schema URL.
		resource.WithAttributes(attrs...),
		// FromDetectors lets Cloud Run / GKE auto-fill instance ID,
		// region, etc. so traces show their host.
		resource.WithFromEnv(),
		resource.WithProcessRuntimeName(),
		resource.WithProcessRuntimeVersion(),
	)
}
