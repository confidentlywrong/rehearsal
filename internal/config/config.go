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
	"errors"
	"fmt"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/joho/godotenv"
)

const (
	// DefaultGatewayPort is the canonical TCP port for cmd/gateway.
	DefaultGatewayPort = "9101"
	// DefaultGatewayURL is the canonical local base URL for cmd/gateway.
	DefaultGatewayURL = "http://127.0.0.1:9101"
	// DefaultAdminPort is the canonical TCP port for cmd/admin.
	DefaultAdminPort = "9121"
	// DefaultFrontendPort is the canonical TCP port for cmd/frontend (BFF).
	DefaultFrontendPort = "9118"
	// DefaultTesterPort is the canonical TCP port for cmd/tester.
	DefaultTesterPort = "9112"
	// DefaultBillingPort is the canonical TCP port for cmd/billing.
	DefaultBillingPort = "9122"
)

// Load attempts to load a .env file.
// It logs a warning if the file is missing and an explicit error for parse/I/O failures.
func Load() {
	if err := godotenv.Load(); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			log.Println("⚠️  Warning: No .env file found. Using system environment variables.")
			return
		}
		log.Printf("❌ Error loading .env file: %v", err)
	}
}

// Require returns the value of an environment variable or panics if it's missing or whitespace-only.
func Require(key string) string {
	val := os.Getenv(key)
	if strings.TrimSpace(val) == "" {
		msg := fmt.Sprintf("❌ CRITICAL ERROR: Required configuration variable '%s' is missing.\n"+
			"Please ensure it is set in your .env file or environment.\n"+
			"Check .env.example for required variables.", key)
		panic(msg)
	}
	return val
}

// Optional returns the value of an environment variable or a default value if missing.
func Optional(key string, defaultValue string) string {
	val := os.Getenv(key)
	if val == "" {
		return defaultValue
	}
	return val
}

// OptionalInt returns the parsed integer value of an environment variable, or def if unset/empty.
// Returns an explicit error if the variable is set to a non-integer or whitespace-only value.
func OptionalInt(key string, def int) (int, error) {
	val := os.Getenv(key)
	if val == "" {
		return def, nil
	}
	trimmed := strings.TrimSpace(val)
	if trimmed == "" {
		return 0, fmt.Errorf("invalid integer for %s: whitespace-only value", key)
	}
	parsed, err := strconv.Atoi(trimmed)
	if err != nil {
		return 0, fmt.Errorf("invalid integer for %s (%q): %w", key, val, err)
	}
	return parsed, nil
}

// OptionalBool returns the parsed boolean value of an environment variable, or def if unset/empty.
// Accepts values supported by strconv.ParseBool. Returns an explicit error if invalid.
func OptionalBool(key string, def bool) (bool, error) {
	val := os.Getenv(key)
	if val == "" {
		return def, nil
	}
	trimmed := strings.TrimSpace(val)
	if trimmed == "" {
		return false, fmt.Errorf("invalid boolean for %s: whitespace-only value", key)
	}
	parsed, err := strconv.ParseBool(trimmed)
	if err != nil {
		return false, fmt.Errorf("invalid boolean for %s (%q): %w", key, val, err)
	}
	return parsed, nil
}

// OptionalDuration returns the parsed duration value of an environment variable, or def if unset/empty.
// Returns an explicit error if the variable is set to an invalid or whitespace-only duration string.
func OptionalDuration(key string, def time.Duration) (time.Duration, error) {
	val := os.Getenv(key)
	if val == "" {
		return def, nil
	}
	trimmed := strings.TrimSpace(val)
	if trimmed == "" {
		return 0, fmt.Errorf("invalid duration for %s: whitespace-only value", key)
	}
	parsed, err := time.ParseDuration(trimmed)
	if err != nil {
		return 0, fmt.Errorf("invalid duration for %s (%q): %w", key, val, err)
	}
	return parsed, nil
}

// ValidatePort ensures a string is an unsigned decimal valid port number (1-65535).
func ValidatePort(val string) error {
	if val == "" {
		return fmt.Errorf("invalid port number: %q", val)
	}
	for i := 0; i < len(val); i++ {
		if val[i] < '0' || val[i] > '9' {
			return fmt.Errorf("invalid port number: %s", val)
		}
	}
	p, err := strconv.Atoi(val)
	if err != nil {
		return fmt.Errorf("invalid port number: %s", val)
	}
	if p < 1 || p > 65535 {
		return fmt.Errorf("port number out of range: %d", p)
	}
	return nil
}
