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

package localai

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func TestValidateLocalEndpoint_SSRFProtection(t *testing.T) {
	valid := map[string]string{
		"":                                  "http://localhost:11434",
		"http://localhost:11434":            "http://localhost:11434",
		"http://localhost:11434/v1":         "http://localhost:11434",
		"http://localhost:11434/api/":       "http://localhost:11434",
		"http://127.0.0.1:11434/":           "http://127.0.0.1:11434",
		"http://[::1]:11434":                "http://[::1]:11434",
		"http://host.docker.internal:11434": "http://host.docker.internal:11434",
	}
	for ep, want := range valid {
		got, err := ValidateLocalEndpoint(ep)
		if err != nil {
			t.Errorf("expected %q to be valid, got err: %v", ep, err)
		} else if got != want {
			t.Errorf("expected %q to canonicalize to %q, got %q", ep, want, got)
		}
	}

	invalid := []string{
		"http://169.254.169.254/latest/meta-data",
		"http://metadata.google.internal/computeMetadata/v1/",
		"https://evil.example.com:11434",
		"ftp://localhost:11434",
		"http://192.168.1.50:11434",
		"http://user:pass@localhost:11434",
		"http://localhost:11434?x=1",
		"http://localhost:11434#frag",
		"http://localhost:11434/custom/subpath",
		"http://localhost:0",
		"http://localhost:99999",
	}
	for _, ep := range invalid {
		if _, err := ValidateLocalEndpoint(ep); err == nil {
			t.Errorf("expected %q to be rejected by SSRF guard, but it succeeded", ep)
		}
	}
}

func TestValidateModelName(t *testing.T) {
	if _, err := ValidateModelName("gemma2:9b"); err != nil {
		t.Fatalf("unexpected error for gemma2:9b: %v", err)
	}
	if _, err := ValidateModelName("embeddinggemma:latest"); err != nil {
		t.Fatalf("unexpected error for embeddinggemma:latest: %v", err)
	}
	if got, err := ValidateModelName("bundle:ollama_chat/gemma3:4b"); err != nil || got != "bundle:gemma3:4b" {
		t.Fatalf("expected bundle:ollama_chat/gemma3:4b -> bundle:gemma3:4b, got %q (err=%v)", got, err)
	}
	for _, bad := range []string{"", "bundle:", "bundle:../../etc/passwd", "../../etc/passwd", "gemma2:9b; rm -rf /", "model with spaces"} {
		if _, err := ValidateModelName(bad); err == nil {
			t.Errorf("expected %q to fail model validation", bad)
		}
	}
}

func TestDiscoverLocalManifestModels_BothInstalledAndMissingStates(t *testing.T) {
	tmpDir := t.TempDir()
	settingsFile := filepath.Join(tmpDir, "ai_settings.json")
	manifestRoot := filepath.Join(tmpDir, "manifests")

	// Simulate user who already has gemma2:9b and embeddinggemma:latest installed on disk,
	// but NOT gemma2:2b or gemma2:27b.
	if err := os.MkdirAll(filepath.Join(manifestRoot, "gemma2"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(manifestRoot, "gemma2", "9b"), []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(manifestRoot, "embeddinggemma"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(manifestRoot, "embeddinggemma", "latest"), []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}

	mgr := NewManager(settingsFile)
	mgr.SetManifestRootForTest(manifestRoot)

	models := mgr.DiscoverLocalManifestModels()
	if !IsTextModelInstalled(models, "gemma2:9b") {
		t.Errorf("expected gemma2:9b to be detected as installed, got %v", models)
	}
	if IsTextModelInstalled(models, "gemma2:2b") {
		t.Errorf("expected gemma2:2b to NOT be installed, got %v", models)
	}
	if IsTextModelInstalled(models, "gemma2:27b") {
		t.Errorf("expected gemma2:27b to NOT be installed, got %v", models)
	}
	if !IsEmbedModelInstalled(models, "embeddinggemma:latest") {
		t.Errorf("expected embeddinggemma:latest to be detected as installed, got %v", models)
	}
}

func TestSaveAndGetConfig(t *testing.T) {
	tmpDir := t.TempDir()
	mgr := NewManager(filepath.Join(tmpDir, "ai_settings.json"))

	saved, err := mgr.SaveConfig(Config{
		Provider:   ProviderLocalGemma,
		Endpoint:   "http://localhost:11434/",
		TextModel:  "gemma2:9b",
		EmbedModel: "embeddinggemma:latest",
	})
	if err != nil {
		t.Fatalf("SaveConfig failed: %v", err)
	}
	if saved.Provider != ProviderLocalGemma || saved.Endpoint != "http://localhost:11434" {
		t.Fatalf("unexpected saved config: %+v", saved)
	}

	loaded := mgr.GetConfig()
	if loaded.Provider != ProviderLocalGemma || loaded.TextModel != "gemma2:9b" || loaded.EmbedModel != "embeddinggemma:latest" {
		t.Fatalf("unexpected loaded config: %+v", loaded)
	}
}

func TestProbeAndPullModel(t *testing.T) {
	pulled := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/tags":
			if pulled {
				_, _ = fmt.Fprintln(w, `{"models":[{"name":"gemma2:2b"},{"name":"embeddinggemma:latest"}]}`)
			} else {
				_, _ = fmt.Fprintln(w, `{"models":[]}`)
			}
		case "/api/pull":
			pulled = true
			w.Header().Set("Content-Type", "application/x-ndjson")
			_, _ = fmt.Fprintln(w, `{"status":"pulling","completed":50,"total":100}`)
			_, _ = fmt.Fprintln(w, `{"status":"success","completed":100,"total":100}`)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	tmpDir := t.TempDir()
	mgr := NewManager(filepath.Join(tmpDir, "ai_settings.json"))
	mgr.SetManifestRootForTest(filepath.Join(tmpDir, "empty-manifests"))

	// Initially not installed
	probe1 := mgr.ProbeEndpoint(context.Background(), srv.URL)
	if !probe1.OK {
		t.Fatalf("expected probe OK, got %+v", probe1)
	}
	if IsTextModelInstalled(probe1.Models, "gemma2:2b") {
		t.Fatal("expected gemma2:2b not installed before pull")
	}

	// Pull gemma2:2b
	prog, err := mgr.PullModel(context.Background(), srv.URL, "gemma2:2b")
	if err != nil {
		t.Fatalf("PullModel failed: %v", err)
	}
	if !prog.Done || prog.Percent != 100 {
		t.Fatalf("expected pull completed 100%%, got %+v", prog)
	}

	// Now installed
	probe2 := mgr.ProbeEndpoint(context.Background(), srv.URL)
	if !IsTextModelInstalled(probe2.Models, "gemma2:2b") {
		t.Fatalf("expected gemma2:2b installed after pull, got %v", probe2.Models)
	}
}

func TestGemma3AliasesAndPrefixStripping(t *testing.T) {
	norm, err := ValidateModelName("ollama_chat/gemma3:12b")
	if err != nil || norm != "gemma3:12b" {
		t.Fatalf("expected ollama_chat/gemma3:12b -> gemma3:12b, got %q (err=%v)", norm, err)
	}
	installed := []string{"gemma3:latest", "embeddinggemma:latest"}
	if !IsTextModelInstalled(installed, "gemma3:4b") {
		t.Errorf("expected gemma3:latest to satisfy gemma3:4b")
	}
	if !IsTextModelInstalled(installed, "ollama_chat/gemma3:4b") {
		t.Errorf("expected prefixed ollama_chat/gemma3:4b to match gemma3:latest")
	}
	if IsTextModelInstalled(installed, "gemma3:27b") {
		t.Errorf("expected gemma3:27b not to match gemma3:latest")
	}
	if !IsEmbedModelInstalled(installed, "ollama/embeddinggemma:latest") {
		t.Errorf("expected prefixed ollama/embeddinggemma:latest to match")
	}
}

func TestPullBundleSkipsInstalledAndDetectsEmptyStreamError(t *testing.T) {
	var pulledModels []string
	failStream := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/tags":
			// gemma2:9b is already installed; embeddinggemma:latest is missing
			_, _ = fmt.Fprintln(w, `{"models":[{"name":"gemma2:9b"}]}`)
		case "/api/pull":
			var body struct {
				Name string `json:"name"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			pulledModels = append(pulledModels, body.Name)
			w.Header().Set("Content-Type", "application/x-ndjson")
			if failStream {
				// Simulate dropped/empty stream
				return
			}
			_, _ = fmt.Fprintln(w, `{"status":"success","completed":100,"total":100}`)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	tmpDir := t.TempDir()
	mgr := NewManager(filepath.Join(tmpDir, "ai_settings.json"))
	mgr.SetManifestRootForTest(filepath.Join(tmpDir, "empty-manifests"))
	_, _ = mgr.SaveConfig(Config{
		Provider:   ProviderLocalGemma,
		Endpoint:   srv.URL,
		TextModel:  "gemma2:9b",
		EmbedModel: "embeddinggemma:latest",
	})

	prog, err := mgr.PullModel(context.Background(), srv.URL, "bundle")
	if err != nil {
		t.Fatalf("PullModel(bundle) failed: %v", err)
	}
	if !prog.Done || len(pulledModels) != 1 || pulledModels[0] != "embeddinggemma:latest" {
		t.Fatalf("expected bundle to only pull missing embeddinggemma:latest, pulled=%v prog=%+v", pulledModels, prog)
	}

	// Now test empty stream failure detection
	failStream = true
	progErr, err := mgr.PullModel(context.Background(), srv.URL, "gemma3:12b")
	if err == nil || progErr.Done || progErr.Status != "error" {
		t.Fatalf("expected empty stream to fail with status=error and Done=false, got err=%v prog=%+v", err, progErr)
	}

	// Test bundle:<customModel> pulls the unsaved custom text model + embed model
	failStream = false
	pulledModels = nil
	progCustom, err := mgr.PullModel(context.Background(), srv.URL, "bundle:gemma3:4b")
	if err != nil || !progCustom.Done {
		t.Fatalf("expected bundle:gemma3:4b to succeed, got err=%v prog=%+v", err, progCustom)
	}
	if len(pulledModels) != 2 || pulledModels[0] != "gemma3:4b" || pulledModels[1] != "embeddinggemma:latest" {
		t.Fatalf("expected [gemma3:4b embeddinggemma:latest], got %v", pulledModels)
	}
}

func TestDefaultConfigFromEnvNormalizesModels(t *testing.T) {
	t.Setenv("GEMMA_TEXT_MODEL", "ollama_chat/gemma3:4b")
	t.Setenv("GEMMA_EMBED_MODEL", "ollama/embeddinggemma:latest")

	tmpDir := t.TempDir()
	mgr := NewManager(filepath.Join(tmpDir, "nonexistent_settings.json"))
	cfg := mgr.GetConfig()
	if cfg.TextModel != "gemma3:4b" {
		t.Fatalf("expected GEMMA_TEXT_MODEL stripped to gemma3:4b, got %q", cfg.TextModel)
	}
	if cfg.EmbedModel != "embeddinggemma:latest" {
		t.Fatalf("expected GEMMA_EMBED_MODEL stripped to embeddinggemma:latest, got %q", cfg.EmbedModel)
	}
}

func TestRejectsBundleAndEmbeddingAsConfiguredTextModel(t *testing.T) {
	if _, err := ValidateModelName("bundle:bundle:gemma2:9b"); err == nil {
		t.Fatalf("expected nested bundle:bundle:gemma2:9b to be rejected")
	}
	if _, err := ValidateModelName("bundle:embeddinggemma:latest"); err == nil {
		t.Fatalf("expected bundle:embeddinggemma:latest to be rejected")
	}
	if IsTextModelInstalled([]string{"gemma2:9b", "embeddinggemma:latest"}, "bundle:gemma2:9b") {
		t.Fatalf("expected IsTextModelInstalled to return false for bundle:gemma2:9b")
	}

	tmpDir := t.TempDir()
	mgr := NewManager(filepath.Join(tmpDir, "ai_settings.json"))
	if _, err := mgr.SaveConfig(Config{
		Provider:   ProviderLocalGemma,
		Endpoint:   "http://localhost:11434",
		TextModel:  "bundle:gemma2:9b",
		EmbedModel: "embeddinggemma:latest",
	}); err == nil {
		t.Fatalf("expected SaveConfig to reject bundle:gemma2:9b as TextModel")
	}
	if _, err := mgr.SaveConfig(Config{
		Provider:   ProviderLocalGemma,
		Endpoint:   "http://localhost:11434",
		TextModel:  "embeddinggemma:latest",
		EmbedModel: "embeddinggemma:latest",
	}); err == nil {
		t.Fatalf("expected SaveConfig to reject embeddinggemma:latest as TextModel")
	}
}

func TestLoadConfig_ErrorsOnInvalidEnvVars(t *testing.T) {
	tmpDir := t.TempDir()
	mgr := NewManager(filepath.Join(tmpDir, "missing.json"))

	t.Run("invalid_AI_PROVIDER", func(t *testing.T) {
		t.Setenv("AI_PROVIDER", "typo_provider")
		if _, err := mgr.LoadConfig(); err == nil || !strings.Contains(err.Error(), "AI_PROVIDER") {
			t.Fatalf("expected LoadConfig error for invalid AI_PROVIDER, got %v", err)
		}
		var buf bytes.Buffer
		prevOut := log.Writer()
		log.SetOutput(&buf)
		defer log.SetOutput(prevOut)

		cfg := mgr.GetConfig()
		if cfg.Provider != ProviderCloudGemini {
			t.Fatalf("expected fallback provider %s, got %s", ProviderCloudGemini, cfg.Provider)
		}
		if !strings.Contains(buf.String(), "localai: WARNING") {
			t.Fatalf("expected GetConfig to log warning, got %q", buf.String())
		}
	})

	t.Run("invalid_OLLAMA_API_BASE", func(t *testing.T) {
		t.Setenv("OLLAMA_API_BASE", "http://169.254.169.254")
		if _, err := mgr.LoadConfig(); err == nil || !strings.Contains(err.Error(), "OLLAMA_API_BASE") {
			t.Fatalf("expected LoadConfig error for SSRF OLLAMA_API_BASE, got %v", err)
		}
	})

	t.Run("invalid_GEMMA_TEXT_MODEL", func(t *testing.T) {
		t.Setenv("GEMMA_TEXT_MODEL", "embeddinggemma:latest")
		if _, err := mgr.LoadConfig(); err == nil || !strings.Contains(err.Error(), "GEMMA_TEXT_MODEL") {
			t.Fatalf("expected LoadConfig error for embedding model in GEMMA_TEXT_MODEL, got %v", err)
		}
	})

	t.Run("invalid_GEMMA_EMBED_MODEL", func(t *testing.T) {
		t.Setenv("GEMMA_EMBED_MODEL", "bundle:gemma2:9b")
		if _, err := mgr.LoadConfig(); err == nil || !strings.Contains(err.Error(), "GEMMA_EMBED_MODEL") {
			t.Fatalf("expected LoadConfig error for bundle in GEMMA_EMBED_MODEL, got %v", err)
		}
	})
}

func TestLoadConfig_ErrorsOnCorruptOrUnreadableSettingsFile(t *testing.T) {
	t.Run("corrupt_json", func(t *testing.T) {
		tmpDir := t.TempDir()
		path := filepath.Join(tmpDir, "ai_settings.json")
		if err := os.WriteFile(path, []byte("{corrupt json"), 0o644); err != nil {
			t.Fatal(err)
		}
		mgr := NewManager(path)
		if _, err := mgr.LoadConfig(); err == nil {
			t.Fatal("expected LoadConfig error on corrupt JSON")
		}
		var buf bytes.Buffer
		prevOut := log.Writer()
		log.SetOutput(&buf)
		defer log.SetOutput(prevOut)

		cfg := mgr.GetConfig()
		if cfg.Provider != ProviderCloudGemini {
			t.Fatalf("expected fallback provider, got %s", cfg.Provider)
		}
		if !strings.Contains(buf.String(), "localai: WARNING") {
			t.Fatalf("expected warning log on corrupt JSON, got %q", buf.String())
		}
	})

	t.Run("invalid_field_in_json", func(t *testing.T) {
		tmpDir := t.TempDir()
		path := filepath.Join(tmpDir, "ai_settings.json")
		if err := os.WriteFile(path, []byte(`{"provider":"unknown_vendor"}`), 0o644); err != nil {
			t.Fatal(err)
		}
		mgr := NewManager(path)
		if _, err := mgr.LoadConfig(); err == nil {
			t.Fatal("expected LoadConfig error on invalid provider in JSON")
		}
	})

	t.Run("non_ErrNotExist_read_error", func(t *testing.T) {
		tmpDir := t.TempDir() // directory cannot be read as a file
		mgr := NewManager(tmpDir)
		if _, err := mgr.LoadConfig(); err == nil {
			t.Fatal("expected LoadConfig error when settings path is unreadable directory")
		}
	})
}

func TestResolveIPv4EndpointValidated_And_GetStatusForEndpointValidated(t *testing.T) {
	ep, err := ResolveIPv4EndpointValidated("http://localhost:11434")
	if err != nil || ep != "http://127.0.0.1:11434" {
		t.Fatalf("expected http://127.0.0.1:11434, got %q (err=%v)", ep, err)
	}
	if _, err := ResolveIPv4EndpointValidated("http://169.254.169.254"); err == nil {
		t.Fatal("expected ResolveIPv4EndpointValidated to reject metadata endpoint")
	}

	mgr := NewManager(filepath.Join(t.TempDir(), "ai_settings.json"))
	if _, err := mgr.GetStatusForEndpointValidated(context.Background(), "http://169.254.169.254"); err == nil {
		t.Fatal("expected GetStatusForEndpointValidated to reject invalid endpoint override")
	}
}

func TestIsDefaultLocalPort_RequiresExplicit11434(t *testing.T) {
	if isDefaultLocalPort("http://localhost") {
		t.Fatal("port-less URL http://localhost must NOT match default local Ollama port 11434")
	}
	if isDefaultLocalPort("http://localhost:80") {
		t.Fatal("port 80 must NOT match 11434")
	}
	if !isDefaultLocalPort("http://localhost:11434") {
		t.Fatal("http://localhost:11434 must match 11434")
	}
	if !isDefaultLocalPort("http://127.0.0.1:11434") {
		t.Fatal("http://127.0.0.1:11434 must match 11434")
	}
}

func TestBuildDaemonEnv_DeduplicatesOllamaHost(t *testing.T) {
	env := buildDaemonEnv([]string{"PATH=/usr/bin", "OLLAMA_HOST=0.0.0.0:9999", "FOO=bar"}, "127.0.0.1:11434")
	hostCount := 0
	var gotHost string
	for _, entry := range env {
		if strings.HasPrefix(entry, "OLLAMA_HOST=") {
			hostCount++
			gotHost = entry
		}
	}
	if hostCount != 1 || gotHost != "OLLAMA_HOST=127.0.0.1:11434" {
		t.Fatalf("expected single OLLAMA_HOST=127.0.0.1:11434, got count=%d val=%q env=%v", hostCount, gotHost, env)
	}
}

func reserveClosedLoopbackPort(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen ephemeral port: %v", err)
	}
	_, port, err := net.SplitHostPort(ln.Addr().String())
	_ = ln.Close()
	if err != nil {
		t.Fatalf("split host port: %v", err)
	}
	return port
}

func TestEnsureDaemonRunning_FailsFastOnImmediateExitAndKillsOnCancel(t *testing.T) {
	t.Run("immediate_exit_fails_fast", func(t *testing.T) {
		tmpDir := t.TempDir()
		script := filepath.Join(tmpDir, "ollama")
		if err := os.WriteFile(script, []byte("#!/bin/sh\nexit 1\n"), 0o755); err != nil {
			t.Fatal(err)
		}

		port := reserveClosedLoopbackPort(t)
		mgr := NewManager(filepath.Join(tmpDir, "ai_settings.json"))
		mgr.SetManifestRootForTest(filepath.Join(tmpDir, "empty-manifests"))
		mgr.autoStartPort = port
		mgr.daemonPollWait = 200 * time.Millisecond
		mgr.resolveBinaryFn = func() string { return script }

		start := time.Now()
		_, started, err := mgr.EnsureDaemonRunning(context.Background(), "http://127.0.0.1:"+port)
		elapsed := time.Since(start)
		if err == nil || started {
			t.Fatalf("expected error and started=false on failing daemon, got started=%v err=%v", started, err)
		}
		if elapsed > 2*time.Second {
			t.Fatalf("expected EnsureDaemonRunning to fail fast on immediate exit, took %v", elapsed)
		}
	})

	t.Run("ctx_cancel_kills_spawned_process", func(t *testing.T) {
		tmpDir := t.TempDir()
		pidFile := filepath.Join(tmpDir, "daemon.pid")
		script := filepath.Join(tmpDir, "ollama")
		body := fmt.Sprintf("#!/bin/sh\necho $$ > %q\nsleep 30\n", pidFile)
		if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}

		port := reserveClosedLoopbackPort(t)
		mgr := NewManager(filepath.Join(tmpDir, "ai_settings.json"))
		mgr.SetManifestRootForTest(filepath.Join(tmpDir, "empty-manifests"))
		mgr.autoStartPort = port
		mgr.daemonPollWait = 500 * time.Millisecond
		mgr.resolveBinaryFn = func() string { return script }

		ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
		defer cancel()

		_, started, err := mgr.EnsureDaemonRunning(ctx, "http://127.0.0.1:"+port)
		if !errors.Is(err, context.DeadlineExceeded) || started {
			t.Fatalf("expected context.DeadlineExceeded, got started=%v err=%v", started, err)
		}

		if pidBytes, rerr := os.ReadFile(pidFile); rerr == nil {
			if pid, perr := strconv.Atoi(strings.TrimSpace(string(pidBytes))); perr == nil && pid > 0 {
				if sigErr := syscall.Kill(pid, 0); sigErr == nil {
					_ = syscall.Kill(pid, syscall.SIGKILL)
					t.Fatalf("spawned daemon process %d survived context cancellation", pid)
				}
			}
		}
	})
}

func TestPullModel_PropagatesEnsureDaemonRunningErrorAndRespectsCtxDone(t *testing.T) {
	t.Run("propagates_startErr", func(t *testing.T) {
		tmpDir := t.TempDir()
		script := filepath.Join(tmpDir, "ollama")
		if err := os.WriteFile(script, []byte("#!/bin/sh\nexit 1\n"), 0o755); err != nil {
			t.Fatal(err)
		}
		port := reserveClosedLoopbackPort(t)
		mgr := NewManager(filepath.Join(tmpDir, "ai_settings.json"))
		mgr.SetManifestRootForTest(filepath.Join(tmpDir, "empty-manifests"))
		mgr.autoStartPort = port
		mgr.daemonPollWait = 200 * time.Millisecond
		mgr.resolveBinaryFn = func() string { return script }

		_, err := mgr.PullModel(context.Background(), "http://127.0.0.1:"+port, "gemma2:2b")
		if err == nil || !strings.Contains(err.Error(), "exited prematurely") {
			t.Fatalf("expected EnsureDaemonRunning premature-exit error, got %v", err)
		}
	})

	t.Run("respects_ctx_done_during_wait", func(t *testing.T) {
		releasePull := make(chan struct{})
		defer close(releasePull)
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case "/api/tags":
				_, _ = fmt.Fprintln(w, `{"models":[]}`)
			case "/api/pull":
				w.Header().Set("Content-Type", "application/x-ndjson")
				w.WriteHeader(http.StatusOK)
				if f, ok := w.(http.Flusher); ok {
					f.Flush()
				}
				select {
				case <-releasePull:
				case <-r.Context().Done():
				}
			}
		}))
		defer srv.Close()

		mgr := NewManager(filepath.Join(t.TempDir(), "ai_settings.json"))
		defer mgr.Shutdown()

		ctx, cancel := context.WithTimeout(context.Background(), 120*time.Millisecond)
		defer cancel()

		start := time.Now()
		_, err := mgr.PullModel(ctx, srv.URL, "gemma2:2b")
		elapsed := time.Since(start)
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("expected context.DeadlineExceeded, got %v", err)
		}
		if elapsed > 1*time.Second {
			t.Fatalf("expected PullModel to abort promptly on ctx.Done(), took %v", elapsed)
		}
	})
}

func TestProbeEndpoint_PreservesHTTPErrorDetails(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/tags":
			http.Error(w, "upstream overloaded", http.StatusServiceUnavailable)
		case "/v1/models":
			w.WriteHeader(http.StatusOK)
			_, _ = fmt.Fprint(w, "{not-valid-json")
		}
	}))
	defer srv.Close()

	mgr := NewManager(filepath.Join(t.TempDir(), "ai_settings.json"))
	mgr.SetManifestRootForTest(filepath.Join(t.TempDir(), "empty-manifests"))

	probe := mgr.ProbeEndpoint(context.Background(), srv.URL)
	if probe.OK {
		t.Fatalf("expected probe.OK=false, got %+v", probe)
	}
	if !strings.Contains(probe.Error, "HTTP 503") || !strings.Contains(probe.Error, "decode error") {
		t.Fatalf("expected ProbeResult.Error to preserve HTTP 503 and decode error, got %q", probe.Error)
	}
}

func TestSaveConfig_LeavesNoTempFiles(t *testing.T) {
	dir := t.TempDir()
	mgr := NewManager(filepath.Join(dir, "ai_settings.json"))
	if _, err := mgr.SaveConfig(Config{
		Provider:   ProviderLocalGemma,
		Endpoint:   DefaultEndpoint,
		TextModel:  DefaultTextModel,
		EmbedModel: DefaultEmbedModel,
	}); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "ai_settings.json" {
		t.Fatalf("expected only ai_settings.json in %s, found %v", dir, entries)
	}
}

func TestProbeEndpoint_ZeroValueManagerDoesNotPanic(t *testing.T) {
	t.Run("ollama_tags", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/api/tags" {
				_, _ = fmt.Fprintln(w, `{"models":[{"name":"gemma2:9b"}]}`)
				return
			}
			w.WriteHeader(http.StatusNotFound)
		}))
		defer srv.Close()

		probe := (&Manager{}).ProbeEndpoint(context.Background(), srv.URL)
		if !probe.OK || probe.Protocol != "ollama" {
			t.Fatalf("expected zero-value Manager ProbeEndpoint to succeed with protocol=ollama, got %+v", probe)
		}
		if !IsTextModelInstalled(probe.Models, "gemma2:9b") {
			t.Fatalf("expected gemma2:9b in probed models, got %v", probe.Models)
		}
	})

	t.Run("llama_server_fallback", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case "/api/tags":
				w.WriteHeader(http.StatusNotFound)
			case "/v1/models":
				_, _ = fmt.Fprintln(w, `{"data":[{"id":"gemma2:2b"}]}`)
			default:
				w.WriteHeader(http.StatusNotFound)
			}
		}))
		defer srv.Close()

		probe := (&Manager{}).ProbeEndpoint(context.Background(), srv.URL)
		if !probe.OK || probe.Protocol != "llama_server" {
			t.Fatalf("expected zero-value Manager ProbeEndpoint fallback to succeed with protocol=llama_server, got %+v", probe)
		}
		if !IsTextModelInstalled(probe.Models, "gemma2:2b") {
			t.Fatalf("expected gemma2:2b in probed models, got %v", probe.Models)
		}
	})
}

func TestCorruptAISettings_GetStatusWithProbeValidatedAndPullModelFailClosed(t *testing.T) {
	var tagsHits atomic.Int32
	var pullHits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/tags":
			tagsHits.Add(1)
			_, _ = fmt.Fprintln(w, `{"models":[{"name":"gemma2:9b"}]}`)
		case "/api/pull":
			pullHits.Add(1)
			w.Header().Set("Content-Type", "application/x-ndjson")
			_, _ = fmt.Fprintln(w, `{"status":"success","completed":100,"total":100}`)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	cfgPath := filepath.Join(t.TempDir(), "ai_settings.json")
	mgr := NewManager(cfgPath)
	if _, err := mgr.SaveConfig(Config{
		Provider:   ProviderLocalGemma,
		Endpoint:   srv.URL,
		TextModel:  "gemma2:9b",
		EmbedModel: DefaultEmbedModel,
	}); err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}

	// Valid config succeeds and rejects SSRF override.
	st, err := mgr.GetStatusWithProbeValidated(srv.URL, ProbeResult{
		OK:       true,
		Protocol: "ollama",
		Models:   []string{"gemma2:9b"},
	})
	if err != nil {
		t.Fatalf("expected GetStatusWithProbeValidated to succeed on valid config, got %v", err)
	}
	if st.Provider != ProviderLocalGemma || st.Endpoint != srv.URL || !st.Connected {
		t.Fatalf("unexpected EngineStatus: %+v", st)
	}
	if _, err := mgr.GetStatusWithProbeValidated("http://169.254.169.254", ProbeResult{}); err == nil {
		t.Fatal("expected GetStatusWithProbeValidated to reject SSRF endpoint override")
	}

	// Corrupt ai_settings.json on disk.
	if err := os.WriteFile(cfgPath, []byte("{corrupt"), 0o600); err != nil {
		t.Fatalf("write corrupt config: %v", err)
	}

	if _, err := mgr.GetStatusWithProbeValidated(srv.URL, ProbeResult{OK: true, Protocol: "ollama"}); err == nil || !strings.Contains(err.Error(), "parse settings") {
		t.Fatalf("expected GetStatusWithProbeValidated error on corrupt config, got %v", err)
	}
	if _, err := mgr.PullModel(context.Background(), srv.URL, "bundle"); err == nil || !strings.Contains(err.Error(), "parse settings") {
		t.Fatalf("expected PullModel(bundle) error on corrupt config, got %v", err)
	}
	if _, err := mgr.PullModel(context.Background(), srv.URL, "gemma2:2b"); err == nil || !strings.Contains(err.Error(), "parse settings") {
		t.Fatalf("expected PullModel(gemma2:2b) error on corrupt config, got %v", err)
	}
	if got := tagsHits.Load(); got != 0 {
		t.Fatalf("expected 0 /api/tags calls on corrupt config, got %d", got)
	}
	if got := pullHits.Load(); got != 0 {
		t.Fatalf("expected 0 /api/pull calls on corrupt config, got %d", got)
	}
}


