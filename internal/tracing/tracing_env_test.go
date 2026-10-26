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
	"os"
	"testing"
)

func TestSetupFromEnv(t *testing.T) {
	os.Setenv("TRACING_ENABLED", "false")
	defer os.Unsetenv("TRACING_ENABLED")

	shutdown, err := SetupFromEnv(context.Background(), "test-service")
	if err != nil {
		t.Fatalf("SetupFromEnv failed: %v", err)
	}
	if shutdown == nil {
		t.Fatal("Expected non-nil shutdown function")
	}
	
	// Test the shutdown function
	if err := shutdown(context.Background()); err != nil {
		t.Errorf("Shutdown failed: %v", err)
	}
}
