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
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
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


