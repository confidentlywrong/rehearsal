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

package tracing

import (
	"context"
	"sync"
	"testing"

	"go.opentelemetry.io/otel"
)

func TestR4_Setup_ShutdownSafeWithNilCancelledAndConcurrentCalls(t *testing.T) {
	prev := otel.GetTracerProvider()
	prevProp := otel.GetTextMapPropagator()
	t.Cleanup(func() {
		otel.SetTracerProvider(prev)
		otel.SetTextMapPropagator(prevProp)
	})

	// Test Setup with nil context in none mode.
	var nilCtx context.Context
	noopClean, err := Setup(nilCtx, Config{Exporter: ExporterNone, ServiceName: "svc-r4"})
	if err != nil {
		t.Fatalf("Setup(nil, none): %v", err)
	}
	if err := noopClean(nilCtx); err != nil {
		t.Fatalf("noopShutdown(nil): %v", err)
	}

	// Test Setup with stdout exporter and concurrent/cancelled/nil shutdown calls.
	clean, err := Setup(context.Background(), Config{
		Exporter:    ExporterStdout,
		ServiceName: "svc-r4",
		SampleRatio: 1.0,
	})
	if err != nil {
		t.Fatalf("Setup(stdout): %v", err)
	}

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()

	var wg sync.WaitGroup
	errs := make([]error, 6)
	for i := range errs {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			if idx%2 == 0 {
				errs[idx] = clean(nilCtx)
			} else {
				errs[idx] = clean(cancelled)
			}
		}(i)
	}
	wg.Wait()

	for i, serr := range errs {
		if serr != errs[0] {
			t.Fatalf("concurrent shutdown[%d] = %v, want %v", i, serr, errs[0])
		}
	}
}
