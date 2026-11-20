// Copyright 2026 Google LLC
// Modifications copyright 2026 Christian Haeussinger
// Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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

package config

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRequire(t *testing.T) {
	// Set a test variable
	t.Setenv("TEST_REQUIRED_VAR", "value")

	val := Require("TEST_REQUIRED_VAR")
	if val != "value" {
		t.Errorf("Expected 'value', got '%s'", val)
	}

	// Test panic on missing variable
	t.Run("panics on missing", func(t *testing.T) {
		defer func() {
			if r := recover(); r == nil {
				t.Errorf("Require did not panic on missing variable")
			}
		}()
		Require("NON_EXISTENT_VAR")
	})

	// Test panic on whitespace-only variable
	t.Run("panics on whitespace only", func(t *testing.T) {
		t.Setenv("TEST_WHITESPACE_VAR", "  \t\n ")
		defer func() {
			if r := recover(); r == nil {
				t.Errorf("Require did not panic on whitespace-only variable")
			}
		}()
		Require("TEST_WHITESPACE_VAR")
	})
}

func TestOptional(t *testing.T) {
	// Set a test variable
	t.Setenv("TEST_OPTIONAL_VAR", "value")

	val := Optional("TEST_OPTIONAL_VAR", "default")
	if val != "value" {
		t.Errorf("Expected 'value', got '%s'", val)
	}

	// Test default value on missing variable
	valMissing := Optional("MISSING_OPTIONAL_VAR", "default_val")
	if valMissing != "default_val" {
		t.Errorf("Expected 'default_val', got '%s'", valMissing)
	}
}

func TestValidatePort(t *testing.T) {
	if err := ValidatePort("8080"); err != nil {
		t.Errorf("Expected valid port '8080', got error: %v", err)
	}
	if err := ValidatePort(DefaultGatewayPort); err != nil {
		t.Errorf("Expected valid DefaultGatewayPort %q, got error: %v", DefaultGatewayPort, err)
	}
	invalid := []string{
		"",
		"0",
		"-1",
		"+9101",
		"+8080",
		" 9101",
		"9101 ",
		"not-a-port",
		"65536",
		"70000",
	}
	for _, p := range invalid {
		if err := ValidatePort(p); err == nil {
			t.Errorf("Expected error for %q, got nil", p)
		}
	}
}

func TestCanonicalPortConstants(t *testing.T) {
	if DefaultGatewayPort != "9101" {
		t.Errorf("DefaultGatewayPort = %q, want 9101", DefaultGatewayPort)
	}
	if DefaultGatewayURL != "http://127.0.0.1:9101" {
		t.Errorf("DefaultGatewayURL = %q, want http://127.0.0.1:9101", DefaultGatewayURL)
	}
	if DefaultAdminPort != "9121" {
		t.Errorf("DefaultAdminPort = %q, want 9121", DefaultAdminPort)
	}
	if DefaultFrontendPort != "9118" {
		t.Errorf("DefaultFrontendPort = %q, want 9118", DefaultFrontendPort)
	}
	if DefaultTesterPort != "9112" {
		t.Errorf("DefaultTesterPort = %q, want 9112", DefaultTesterPort)
	}
	if DefaultBillingPort != "9122" {
		t.Errorf("DefaultBillingPort = %q, want 9122", DefaultBillingPort)
	}
}

func TestLoadDistinguishesMissingVsError(t *testing.T) {
	origOut := log.Writer()
	defer log.SetOutput(origOut)

	t.Run("missing .env logs warning", func(t *testing.T) {
		dir := t.TempDir()
		t.Chdir(dir)

		var buf bytes.Buffer
		log.SetOutput(&buf)
		Load()

		out := buf.String()
		if !strings.Contains(out, "No .env file found") {
			t.Fatalf("expected missing .env warning, got %q", out)
		}
		if strings.Contains(out, "Error loading .env file") {
			t.Fatalf("unexpected error log for missing .env: %q", out)
		}
	})

	t.Run("malformed or unreadable .env logs explicit error", func(t *testing.T) {
		dir := t.TempDir()
		t.Chdir(dir)
		// Create .env as a directory so godotenv.Load fails with a non-ErrNotExist error
		// regardless of OS permissions or root user.
		if err := os.Mkdir(filepath.Join(dir, ".env"), 0o755); err != nil {
			t.Fatalf("mkdir .env: %v", err)
		}

		var buf bytes.Buffer
		log.SetOutput(&buf)
		Load()

		out := buf.String()
		if !strings.Contains(out, "Error loading .env file") {
			t.Fatalf("expected explicit error log for unreadable .env, got %q", out)
		}
		if strings.Contains(out, "No .env file found") {
			t.Fatalf("should not report 'No .env file found' when .env exists and fails: %q", out)
		}
	})
}

func TestOptionalInt(t *testing.T) {
	t.Run("unset returns default", func(t *testing.T) {
		v, err := OptionalInt("TEST_OPT_INT_UNSET", 42)
		if err != nil || v != 42 {
			t.Fatalf("got (%d, %v), want (42, nil)", v, err)
		}
	})
	t.Run("valid value", func(t *testing.T) {
		t.Setenv("TEST_OPT_INT", " 128 ")
		v, err := OptionalInt("TEST_OPT_INT", 42)
		if err != nil || v != 128 {
			t.Fatalf("got (%d, %v), want (128, nil)", v, err)
		}
	})
	t.Run("invalid value returns error", func(t *testing.T) {
		t.Setenv("TEST_OPT_INT_BAD", "not-an-int")
		if _, err := OptionalInt("TEST_OPT_INT_BAD", 42); err == nil {
			t.Fatal("expected error for invalid integer")
		}
	})
	t.Run("whitespace-only returns error", func(t *testing.T) {
		t.Setenv("TEST_OPT_INT_WS", "   ")
		if _, err := OptionalInt("TEST_OPT_INT_WS", 42); err == nil {
			t.Fatal("expected error for whitespace-only integer")
		}
	})
}

func TestOptionalBool(t *testing.T) {
	t.Run("unset returns default", func(t *testing.T) {
		v, err := OptionalBool("TEST_OPT_BOOL_UNSET", true)
		if err != nil || !v {
			t.Fatalf("got (%v, %v), want (true, nil)", v, err)
		}
	})
	t.Run("valid values", func(t *testing.T) {
		t.Setenv("TEST_OPT_BOOL", "true")
		v, err := OptionalBool("TEST_OPT_BOOL", false)
		if err != nil || !v {
			t.Fatalf("got (%v, %v), want (true, nil)", v, err)
		}
		t.Setenv("TEST_OPT_BOOL", " 0 ")
		v, err = OptionalBool("TEST_OPT_BOOL", true)
		if err != nil || v {
			t.Fatalf("got (%v, %v), want (false, nil)", v, err)
		}
	})
	t.Run("invalid value returns error", func(t *testing.T) {
		t.Setenv("TEST_OPT_BOOL_BAD", "maybe")
		if _, err := OptionalBool("TEST_OPT_BOOL_BAD", false); err == nil {
			t.Fatal("expected error for invalid boolean")
		}
	})
	t.Run("whitespace-only returns error", func(t *testing.T) {
		t.Setenv("TEST_OPT_BOOL_WS", "\t ")
		if _, err := OptionalBool("TEST_OPT_BOOL_WS", false); err == nil {
			t.Fatal("expected error for whitespace-only boolean")
		}
	})
}

func TestOptionalDuration(t *testing.T) {
	t.Run("unset returns default", func(t *testing.T) {
		v, err := OptionalDuration("TEST_OPT_DUR_UNSET", 5*time.Second)
		if err != nil || v != 5*time.Second {
			t.Fatalf("got (%v, %v), want (5s, nil)", v, err)
		}
	})
	t.Run("valid value", func(t *testing.T) {
		t.Setenv("TEST_OPT_DUR", " 2m30s ")
		v, err := OptionalDuration("TEST_OPT_DUR", time.Second)
		if err != nil || v != 150*time.Second {
			t.Fatalf("got (%v, %v), want (2m30s, nil)", v, err)
		}
	})
	t.Run("invalid value returns error", func(t *testing.T) {
		t.Setenv("TEST_OPT_DUR_BAD", "five-seconds")
		if _, err := OptionalDuration("TEST_OPT_DUR_BAD", time.Second); err == nil {
			t.Fatal("expected error for invalid duration")
		}
	})
	t.Run("whitespace-only returns error", func(t *testing.T) {
		t.Setenv("TEST_OPT_DUR_WS", "  ")
		if _, err := OptionalDuration("TEST_OPT_DUR_WS", time.Second); err == nil {
			t.Fatal("expected error for whitespace-only duration")
		}
	})
}
