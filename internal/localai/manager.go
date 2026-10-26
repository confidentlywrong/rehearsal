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

// Package localai manages local Google LLM (Gemma) and vector embedding
// (EmbeddingGemma) execution via Ollama / llama-server. It provides hardware
// RAM profiling, local manifest discovery (~/.ollama/models/manifests),
// live endpoint probing, daemon auto-start, streaming model pulls with
// progress tracking, and atomic persistence of the active AI configuration.
package localai

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// ProviderCloudGemini routes LLM and embedding calls to Google Cloud Vertex AI / Gemini API.
	ProviderCloudGemini = "cloud_gemini"
	// ProviderLocalGemma routes LLM and embedding calls to local Gemma & EmbeddingGemma via Ollama.
	ProviderLocalGemma = "local_gemma"

	// DefaultEndpoint is the default local Ollama HTTP endpoint.
	DefaultEndpoint = "http://localhost:11434"
	// DefaultTextModel is the default local Gemma text/agent model.
	DefaultTextModel = "gemma2:9b"
	// DefaultEmbedModel is the default local Google vector embedding model.
	DefaultEmbedModel = "embeddinggemma:latest"
)

var validModelNameRe = regexp.MustCompile(`^[a-zA-Z0-9._:/-]{1,128}$`)

// Config holds the user-selected AI Engine settings persisted in data/ai_settings.json.
type Config struct {
	Provider   string `json:"provider"`
	Endpoint   string `json:"endpoint"`
	TextModel  string `json:"text_model"`
	EmbedModel string `json:"embed_model"`
	UpdatedAt  string `json:"updated_at,omitempty"`
}

// ModelTier describes one RAM-calibrated Google Gemma model tier.
type ModelTier struct {
	ID        string  `json:"id"`
	Model     string  `json:"model"`
	Label     string  `json:"label"`
	Subtitle  string  `json:"subtitle"`
	SizeGB    float64 `json:"size_gb"`
	MinRAMGB  int     `json:"min_ram_gb"`
	FitsRAM   bool    `json:"fits_ram"`
	Installed bool    `json:"installed"`
}

// EmbedEngineStatus describes the installation and readiness state of the local
// Google vector embeddings model (embeddinggemma:latest).
type EmbedEngineStatus struct {
	Model           string  `json:"model"`
	ConfiguredModel string  `json:"configured_model"`
	Label           string  `json:"label"`
	Subtitle        string  `json:"subtitle"`
	SizeGB          float64 `json:"size_gb"`
	NativeDim       int     `json:"native_dim"`
	PgVectorDim     int     `json:"pgvector_dim"`
	Installed       bool    `json:"installed"`
}

// HardwareProfile reports the host CPU/chip and Unified Memory / RAM budget.
type HardwareProfile struct {
	Platform         string `json:"platform"`
	Arch             string `json:"arch"`
	Chip             string `json:"chip"`
	TotalRAMGB       int    `json:"total_ram_gb"`
	RecommendedTier  string `json:"recommended_tier"`
	RecommendedModel string `json:"recommended_model"`
}

// PullProgress tracks an active or recently completed background model download.
type PullProgress struct {
	Active         bool    `json:"active"`
	Model          string  `json:"model,omitempty"`
	Status         string  `json:"status,omitempty"`
	Percent        float64 `json:"percent"`
	CompletedBytes int64   `json:"completed_bytes"`
	TotalBytes     int64   `json:"total_bytes"`
	Done           bool    `json:"done"`
	Error          string  `json:"error,omitempty"`
	UpdatedAt      string  `json:"updated_at,omitempty"`
}

// EngineStatus summarizes the full AI Engine state returned by GET /api/v1/settings/ai.
type EngineStatus struct {
	Provider               string            `json:"provider"`
	Endpoint               string            `json:"endpoint"`
	TextModel              string            `json:"text_model"`
	EmbedModel             string            `json:"embed_model"`
	CloudProjectConfigured bool              `json:"cloud_project_configured"`
	Connected              bool              `json:"connected"`
	Protocol               string            `json:"protocol,omitempty"`
	LatencyMs              *int64            `json:"latency_ms,omitempty"`
	BinaryInstalled        bool              `json:"binary_installed"`
	BinaryPath             string            `json:"binary_path,omitempty"`
	TextModelReady         bool              `json:"text_model_ready"`
	EmbedModelReady        bool              `json:"embed_model_ready"`
	InstalledModels        []string          `json:"installed_models"`
	Hardware               HardwareProfile   `json:"hardware"`
	ModelTiers             []ModelTier       `json:"model_tiers"`
	EmbedEngine            EmbedEngineStatus `json:"embed_engine"`
	PullProgress           PullProgress      `json:"pull_progress"`
}

// ProbeResult holds the result of probing a local Ollama / llama-server endpoint.
type ProbeResult struct {
	OK        bool
	Protocol  string
	Models    []string
	LatencyMs int64
	Error     string
}

var baseModelTiers = []ModelTier{
	{
		ID:       "compact",
		Model:    "gemma2:2b",
		Label:    "Gemma 2B · Compact",
		Subtitle: "Fastest response · 8 GB+ RAM",
		SizeGB:   1.6,
		MinRAMGB: 8,
	},
	{
		ID:       "balanced",
		Model:    "gemma2:9b",
		Label:    "Gemma 9B · Balanced",
		Subtitle: "Recommended for leadership rehearsal · 16 GB+ RAM",
		SizeGB:   5.4,
		MinRAMGB: 16,
	},
	{
		ID:       "expert",
		Model:    "gemma2:27b",
		Label:    "Gemma 27B · Expert",
		Subtitle: "Maximum analytical depth · 32 GB+ RAM",
		SizeGB:   16.0,
		MinRAMGB: 32,
	},
}

// Manager coordinates AI settings persistence, hardware detection, model discovery,
// daemon auto-start, and background model pulls.
type Manager struct {
	mu           sync.RWMutex
	settingsPath string
	manifestRoot string
	pullMu       sync.RWMutex
	pullState    PullProgress
	startMu      sync.Mutex
	httpClient   *http.Client
}

// NewManager creates a Local AI Manager backed by the given settings JSON file path.
// When settingsPath is empty, it defaults to AI_SETTINGS_PATH or "data/ai_settings.json".
func NewManager(settingsPath string) *Manager {
	if strings.TrimSpace(settingsPath) == "" {
		settingsPath = strings.TrimSpace(os.Getenv("AI_SETTINGS_PATH"))
	}
	if settingsPath == "" {
		settingsPath = filepath.Join("data", "ai_settings.json")
	}
	modelsDir := strings.TrimSpace(os.Getenv("OLLAMA_MODELS"))
	if modelsDir == "" {
		if home, _ := os.UserHomeDir(); home != "" {
			modelsDir = filepath.Join(home, ".ollama", "models")
		}
	}
	manifestRoot := ""
	if modelsDir != "" {
		manifestRoot = filepath.Join(modelsDir, "manifests", "registry.ollama.ai", "library")
	}
	return &Manager{
		settingsPath: settingsPath,
		manifestRoot: manifestRoot,
		httpClient: &http.Client{
			Timeout: 5 * time.Second,
		},
	}
}

// SetManifestRootForTest overrides the Ollama manifest directory for unit tests.
func (m *Manager) SetManifestRootForTest(dir string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.manifestRoot = dir
}

// ValidateLocalEndpoint enforces that the endpoint is a valid http/https URL
// targeting a loopback address (localhost, 127.0.0.1, ::1, or host.docker.internal)
// to prevent SSRF against internal or cloud metadata services.
func ValidateLocalEndpoint(raw string) (string, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return DefaultEndpoint, nil
	}
	trimmed = strings.TrimRight(trimmed, "/")
	u, err := url.Parse(trimmed)
	if err != nil {
		return "", fmt.Errorf("invalid endpoint URL: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", errors.New("endpoint must use http:// or https:// scheme")
	}
	if u.User != nil {
		return "", errors.New("endpoint must not contain user credentials")
	}
	if u.RawQuery != "" || u.Fragment != "" {
		return "", errors.New("endpoint must not contain query parameters or fragments")
	}
	cleanPath := strings.TrimRight(u.Path, "/")
	if strings.EqualFold(cleanPath, "/v1") || strings.EqualFold(cleanPath, "/api") {
		cleanPath = ""
	}
	if cleanPath != "" {
		return "", fmt.Errorf("endpoint must not contain a URL path, got %q", u.Path)
	}
	host := strings.ToLower(u.Hostname())
	if host == "" {
		return "", errors.New("endpoint hostname is required")
	}
	if portStr := u.Port(); portStr != "" {
		portNum, perr := strconv.Atoi(portStr)
		if perr != nil || portNum < 1 || portNum > 65535 {
			return "", fmt.Errorf("invalid endpoint port %q", portStr)
		}
	}
	allowedHost := host == "localhost" ||
		host == "127.0.0.1" ||
		host == "::1" ||
		host == "host.docker.internal"
	if !allowedHost {
		ip := net.ParseIP(host)
		if ip == nil || !ip.IsLoopback() {
			return "", fmt.Errorf("local AI endpoint must target loopback (localhost / 127.0.0.1), got %q", host)
		}
	}
	return fmt.Sprintf("%s://%s", u.Scheme, u.Host), nil
}

// StripModelPrefix removes optional LiteLLM provider prefixes ("ollama_chat/" or "ollama/")
// so raw Ollama API endpoints receive the canonical model tag (e.g. "gemma2:9b").
func StripModelPrefix(name string) string {
	trimmed := strings.TrimSpace(name)
	lower := strings.ToLower(trimmed)
	if strings.HasPrefix(lower, "ollama_chat/") {
		return strings.TrimSpace(trimmed[len("ollama_chat/"):])
	}
	if strings.HasPrefix(lower, "ollama/") {
		return strings.TrimSpace(trimmed[len("ollama/"):])
	}
	return trimmed
}

// ValidateModelName checks that a model identifier contains only safe characters
// and no path traversal sequences, stripping any optional LiteLLM ollama prefix.
func ValidateModelName(name string) (string, error) {
	rawTrimmed := strings.TrimSpace(name)
	if strings.HasPrefix(strings.ToLower(rawTrimmed), "bundle:") {
		sub := strings.TrimSpace(rawTrimmed[len("bundle:"):])
		if strings.HasPrefix(strings.ToLower(sub), "bundle") || strings.Contains(strings.ToLower(sub), "embed") {
			return "", fmt.Errorf("invalid bundle text model target %q", rawTrimmed)
		}
		normSub, err := ValidateModelName(sub)
		if err != nil {
			return "", err
		}
		return "bundle:" + normSub, nil
	}
	trimmed := StripModelPrefix(rawTrimmed)
	if trimmed == "" {
		return "", errors.New("model name cannot be empty")
	}
	if strings.Contains(trimmed, "..") || !validModelNameRe.MatchString(trimmed) {
		return "", fmt.Errorf("invalid model name %q", trimmed)
	}
	return trimmed, nil
}

func isValidConfiguredTextModel(m string) bool {
	lower := strings.ToLower(strings.TrimSpace(m))
	return lower != "" &&
		lower != "bundle" &&
		!strings.HasPrefix(lower, "bundle:") &&
		!strings.Contains(lower, "embed")
}

func isValidConfiguredEmbedModel(m string) bool {
	lower := strings.ToLower(strings.TrimSpace(m))
	return lower != "" &&
		lower != "bundle" &&
		!strings.HasPrefix(lower, "bundle:")
}

// ValidateProvider normalizes and validates the provider identifier.
func ValidateProvider(p string) (string, error) {
	clean := strings.ToLower(strings.TrimSpace(p))
	switch clean {
	case "", ProviderCloudGemini:
		return ProviderCloudGemini, nil
	case ProviderLocalGemma:
		return ProviderLocalGemma, nil
	default:
		return "", fmt.Errorf("unsupported AI provider %q (must be %q or %q)", p, ProviderCloudGemini, ProviderLocalGemma)
	}
}

// defaultConfigFromEnv builds the initial Config using environment variables or defaults.
func defaultConfigFromEnv() Config {
	provider := strings.ToLower(strings.TrimSpace(os.Getenv("AI_PROVIDER")))
	if provider != ProviderLocalGemma && provider != ProviderCloudGemini {
		provider = ProviderCloudGemini
	}
	endpoint := strings.TrimSpace(os.Getenv("OLLAMA_API_BASE"))
	if endpoint == "" {
		endpoint = strings.TrimSpace(os.Getenv("OLLAMA_ENDPOINT"))
	}
	if validated, err := ValidateLocalEndpoint(endpoint); err == nil {
		endpoint = validated
	} else {
		endpoint = DefaultEndpoint
	}
	textModel := strings.TrimSpace(os.Getenv("GEMMA_TEXT_MODEL"))
	if validated, err := ValidateModelName(textModel); err == nil && isValidConfiguredTextModel(validated) {
		textModel = validated
	} else {
		textModel = DefaultTextModel
	}
	embedModel := strings.TrimSpace(os.Getenv("GEMMA_EMBED_MODEL"))
	if validated, err := ValidateModelName(embedModel); err == nil && isValidConfiguredEmbedModel(validated) {
		embedModel = validated
	} else {
		embedModel = DefaultEmbedModel
	}
	return Config{
		Provider:   provider,
		Endpoint:   endpoint,
		TextModel:  textModel,
		EmbedModel: embedModel,
	}
}

// GetConfig returns the current AI configuration, reading from disk if present
// or falling back to environment defaults.
func (m *Manager) GetConfig() Config {
	m.mu.RLock()
	path := m.settingsPath
	m.mu.RUnlock()

	data, err := os.ReadFile(path)
	if err == nil && len(data) > 0 {
		var cfg Config
		if jerr := json.Unmarshal(data, &cfg); jerr == nil {
			if p, err := ValidateProvider(cfg.Provider); err == nil {
				cfg.Provider = p
			} else {
				cfg.Provider = ProviderCloudGemini
			}
			if ep, err := ValidateLocalEndpoint(cfg.Endpoint); err == nil {
				cfg.Endpoint = ep
			} else {
				cfg.Endpoint = DefaultEndpoint
			}
			if tm, err := ValidateModelName(cfg.TextModel); err == nil && isValidConfiguredTextModel(tm) {
				cfg.TextModel = tm
			} else {
				cfg.TextModel = DefaultTextModel
			}
			if em, err := ValidateModelName(cfg.EmbedModel); err == nil && isValidConfiguredEmbedModel(em) {
				cfg.EmbedModel = em
			} else {
				cfg.EmbedModel = DefaultEmbedModel
			}
			return cfg
		}
	}
	return defaultConfigFromEnv()
}

// SaveConfig validates and atomically persists the updated AI configuration to disk.
func (m *Manager) SaveConfig(in Config) (Config, error) {
	provider, err := ValidateProvider(in.Provider)
	if err != nil {
		return Config{}, err
	}
	endpoint := in.Endpoint
	if strings.TrimSpace(endpoint) == "" {
		endpoint = DefaultEndpoint
	}
	endpoint, err = ValidateLocalEndpoint(endpoint)
	if err != nil {
		return Config{}, err
	}
	textModel := in.TextModel
	if strings.TrimSpace(textModel) == "" {
		textModel = DefaultTextModel
	}
	textModel, err = ValidateModelName(textModel)
	if err != nil {
		return Config{}, err
	}
	if !isValidConfiguredTextModel(textModel) {
		return Config{}, fmt.Errorf("invalid text model %q (must be a Gemma text model, not an embedding model or bundle target)", textModel)
	}
	embedModel := in.EmbedModel
	if strings.TrimSpace(embedModel) == "" {
		embedModel = DefaultEmbedModel
	}
	embedModel, err = ValidateModelName(embedModel)
	if err != nil {
		return Config{}, err
	}
	if !isValidConfiguredEmbedModel(embedModel) {
		return Config{}, fmt.Errorf("invalid embedding model %q", embedModel)
	}

	cfg := Config{
		Provider:   provider,
		Endpoint:   endpoint,
		TextModel:  textModel,
		EmbedModel: embedModel,
		UpdatedAt:  time.Now().UTC().Format(time.RFC3339),
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	dir := filepath.Dir(m.settingsPath)
	if dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return Config{}, fmt.Errorf("localai: mkdir %s: %w", dir, err)
		}
	}
	payload, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return Config{}, fmt.Errorf("localai: marshal config: %w", err)
	}
	tmpPath := m.settingsPath + ".tmp"
	if err := os.WriteFile(tmpPath, payload, 0o644); err != nil {
		return Config{}, fmt.Errorf("localai: write temp config: %w", err)
	}
	if err := os.Rename(tmpPath, m.settingsPath); err != nil {
		_ = os.Remove(tmpPath)
		return Config{}, fmt.Errorf("localai: rename config: %w", err)
	}
	return cfg, nil
}

// GetHardwareProfile detects the host CPU/chip and Unified Memory / RAM budget.
func GetHardwareProfile() HardwareProfile {
	totalRAMGB := detectTotalRAMGB()
	chip := detectChipName()

	recommendedTier := "compact"
	recommendedModel := "gemma2:2b"
	if totalRAMGB >= 32 {
		recommendedTier = "expert"
		recommendedModel = "gemma2:27b"
	} else if totalRAMGB >= 16 {
		recommendedTier = "balanced"
		recommendedModel = "gemma2:9b"
	}

	return HardwareProfile{
		Platform:         runtime.GOOS,
		Arch:             runtime.GOARCH,
		Chip:             chip,
		TotalRAMGB:       totalRAMGB,
		RecommendedTier:  recommendedTier,
		RecommendedModel: recommendedModel,
	}
}

func detectTotalRAMGB() int {
	if runtime.GOOS == "darwin" {
		out, err := exec.Command("sysctl", "-n", "hw.memsize").Output()
		if err == nil {
			if bytesVal, perr := strconv.ParseUint(strings.TrimSpace(string(out)), 10, 64); perr == nil && bytesVal > 0 {
				return int(math.Round(float64(bytesVal) / (1024 * 1024 * 1024)))
			}
		}
	} else if runtime.GOOS == "linux" {
		data, err := os.ReadFile("/proc/meminfo")
		if err == nil {
			for _, line := range strings.Split(string(data), "\n") {
				if strings.HasPrefix(line, "MemTotal:") {
					fields := strings.Fields(line)
					if len(fields) >= 2 {
						if kb, perr := strconv.ParseUint(fields[1], 10, 64); perr == nil && kb > 0 {
							return int(math.Round(float64(kb) / (1024 * 1024)))
						}
					}
				}
			}
		}
	}
	return 16
}

func detectChipName() string {
	if runtime.GOOS == "darwin" {
		out, err := exec.Command("sysctl", "-n", "machdep.cpu.brand_string").Output()
		if err == nil {
			brand := strings.TrimSpace(string(out))
			if brand != "" {
				return brand
			}
		}
		if runtime.GOARCH == "arm64" {
			return "Apple Silicon"
		}
	}
	return fmt.Sprintf("%s/%s", runtime.GOOS, runtime.GOARCH)
}

// DiscoverLocalManifestModels inspects ~/.ollama/models/manifests/registry.ollama.ai/library
// on disk so already-installed models (e.g. gemma2:9b, embeddinggemma:latest) are recognized
// even when the Ollama daemon is currently stopped.
func (m *Manager) DiscoverLocalManifestModels() []string {
	m.mu.RLock()
	root := m.manifestRoot
	m.mu.RUnlock()

	if root == "" {
		return nil
	}
	families, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	var discovered []string
	seen := make(map[string]struct{})
	for _, fam := range families {
		if !fam.IsDir() || strings.HasPrefix(fam.Name(), ".") {
			continue
		}
		// Skip internal runner directories like "llamacpp"
		if strings.EqualFold(fam.Name(), "llamacpp") {
			continue
		}
		famDir := filepath.Join(root, fam.Name())
		tags, err := os.ReadDir(famDir)
		if err != nil {
			continue
		}
		for _, tagEntry := range tags {
			if tagEntry.IsDir() || strings.HasPrefix(tagEntry.Name(), ".") {
				continue
			}
			fullTag := fmt.Sprintf("%s:%s", fam.Name(), tagEntry.Name())
			lower := strings.ToLower(fullTag)
			if _, exists := seen[lower]; !exists {
				seen[lower] = struct{}{}
				discovered = append(discovered, fullTag)
			}
		}
	}
	sort.Strings(discovered)
	return discovered
}

// ResolveOllamaBinary locates the local `ollama` CLI/daemon binary across PATH
// and standard macOS / Linux installation locations (including /Applications/Ollama.app).
// Prioritizes the full `ollama` binary over `llama-server` so both Gemma text models
// and EmbeddingGemma can be served concurrently and `/api/pull` is supported.
func ResolveOllamaBinary() string {
	// In Cloud Run multi-tenant deployments, local process spawning is disabled.
	if os.Getenv("K_SERVICE") != "" {
		return ""
	}
	if p, err := exec.LookPath("ollama"); err == nil && p != "" {
		return p
	}
	home, _ := os.UserHomeDir()
	candidates := []string{
		"/Applications/Ollama.app/Contents/Resources/ollama",
		"/opt/homebrew/bin/ollama",
		"/usr/local/bin/ollama",
		"/usr/bin/ollama",
	}
	if home != "" {
		candidates = append([]string{
			filepath.Join(home, "Applications", "Ollama.app", "Contents", "Resources", "ollama"),
		}, candidates...)
	}
	for _, c := range candidates {
		if info, err := os.Stat(c); err == nil && !info.IsDir() {
			return c
		}
	}
	return ""
}

// IsTextModelInstalled checks whether a requested text model (e.g. "gemma2:9b" or "gemma3:4b")
// is present in the list of installed models, using tag-aware matching so
// "gemma2:2b" never false-matches "gemma2:9b".
func IsTextModelInstalled(installed []string, requested string) bool {
	req := strings.ToLower(StripModelPrefix(requested))
	if !isValidConfiguredTextModel(req) {
		return false
	}
	reqHasExplicitTag := strings.Contains(req, ":")
	reqFamily, reqTag := splitModelTag(req)

	for _, raw := range installed {
		cand := strings.ToLower(StripModelPrefix(raw))
		if cand == "" || strings.Contains(cand, "embed") {
			continue
		}
		if cand == req {
			return true
		}
		candFamily, candTag := splitModelTag(cand)
		if candFamily != reqFamily {
			continue
		}
		if !reqHasExplicitTag {
			return true
		}
		// In Ollama, gemma2:latest aliases gemma2:9b and gemma3:latest aliases gemma3:4b
		if (req == "gemma2:9b" && cand == "gemma2:latest") ||
			(req == "gemma2:latest" && cand == "gemma2:9b") ||
			(req == "gemma3:4b" && cand == "gemma3:latest") ||
			(req == "gemma3:latest" && cand == "gemma3:4b") {
			return true
		}
		if reqTag == candTag {
			return true
		}
	}
	return false
}

// IsEmbedModelInstalled checks whether the requested embedding model
// (e.g. "embeddinggemma:latest") is present in the list of installed models.
func IsEmbedModelInstalled(installed []string, requested string) bool {
	req := strings.ToLower(StripModelPrefix(requested))
	if req == "" {
		req = DefaultEmbedModel
	}
	reqFamily, reqTag := splitModelTag(req)
	for _, raw := range installed {
		cand := strings.ToLower(StripModelPrefix(raw))
		if cand == "" {
			continue
		}
		if cand == req {
			return true
		}
		candFamily, candTag := splitModelTag(cand)
		if candFamily == reqFamily && (reqTag == "latest" || candTag == "latest" || reqTag == candTag) {
			return true
		}
	}
	return false
}

func splitModelTag(model string) (string, string) {
	parts := strings.SplitN(model, ":", 2)
	if len(parts) == 1 {
		return parts[0], "latest"
	}
	return parts[0], parts[1]
}

// ResolveIPv4Endpoint validates the local endpoint and rewrites "localhost" to "127.0.0.1"
// for outgoing HTTP calls to avoid macOS IPv6 ::1 connection-refused delays when Ollama binds 127.0.0.1.
func ResolveIPv4Endpoint(endpoint string) string {
	clean, err := ValidateLocalEndpoint(endpoint)
	if err != nil {
		clean = DefaultEndpoint
	}
	return strings.Replace(clean, "://localhost", "://127.0.0.1", 1)
}

func resolveIPv4Endpoint(endpoint string) string {
	return ResolveIPv4Endpoint(endpoint)
}

// ProbeEndpoint probes a local Ollama (/api/tags) or llama-server (/v1/models)
// endpoint and merges any live models with models discovered in ~/.ollama/models/manifests.
func (m *Manager) ProbeEndpoint(ctx context.Context, endpoint string) ProbeResult {
	manifestModels := m.DiscoverLocalManifestModels()
	cleanEndpoint, err := ValidateLocalEndpoint(endpoint)
	if err != nil {
		return ProbeResult{OK: false, Models: manifestModels, Error: err.Error()}
	}
	fetchBase := resolveIPv4Endpoint(cleanEndpoint)

	tagsCtx, cancelTags := context.WithTimeout(ctx, 1800*time.Millisecond)
	defer cancelTags()

	start := time.Now()
	req, err := http.NewRequestWithContext(tagsCtx, http.MethodGet, fetchBase+"/api/tags", nil)
	if err == nil {
		if resp, doErr := m.httpClient.Do(req); doErr == nil {
			defer resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				var payload struct {
					Models []struct {
						Name  string `json:"name"`
						Model string `json:"model"`
					} `json:"models"`
				}
				if decErr := json.NewDecoder(resp.Body).Decode(&payload); decErr == nil {
					var live []string
					for _, item := range payload.Models {
						name := strings.TrimSpace(item.Name)
						if name == "" {
							name = strings.TrimSpace(item.Model)
						}
						if name != "" {
							live = append(live, name)
						}
					}
					return ProbeResult{
						OK:        true,
						Protocol:  "ollama",
						Models:    mergeModelLists(live, manifestModels),
						LatencyMs: time.Since(start).Milliseconds(),
					}
				}
			}
		}
	}

	// Fallback probe: llama-server OpenAI-compatible /v1/models with its own timeout budget
	if ctx.Err() == nil {
		v1Ctx, cancelV1 := context.WithTimeout(ctx, 1800*time.Millisecond)
		defer cancelV1()

		startV1 := time.Now()
		reqV1, err := http.NewRequestWithContext(v1Ctx, http.MethodGet, fetchBase+"/v1/models", nil)
		if err == nil {
			if respV1, doErr := m.httpClient.Do(reqV1); doErr == nil {
				defer respV1.Body.Close()
				if respV1.StatusCode == http.StatusOK {
					var payload struct {
						Data []struct {
							ID string `json:"id"`
						} `json:"data"`
					}
					if decErr := json.NewDecoder(respV1.Body).Decode(&payload); decErr == nil {
						var live []string
						for _, item := range payload.Data {
							id := strings.TrimSpace(item.ID)
							if id != "" && !strings.HasPrefix(id, "/") && !strings.Contains(id, "sha256-") {
								live = append(live, id)
							}
						}
						return ProbeResult{
							OK:        true,
							Protocol:  "llama_server",
							Models:    mergeModelLists(live, manifestModels),
							LatencyMs: time.Since(startV1).Milliseconds(),
						}
					}
				}
			}
		}
	}

	return ProbeResult{
		OK:     false,
		Models: manifestModels,
		Error:  fmt.Sprintf("Local AI engine at %s is not reachable", cleanEndpoint),
	}
}

func mergeModelLists(a, b []string) []string {
	seen := make(map[string]struct{})
	var out []string
	for _, list := range [][]string{a, b} {
		for _, item := range list {
			trimmed := strings.TrimSpace(item)
			if trimmed == "" {
				continue
			}
			key := strings.ToLower(trimmed)
			if _, ok := seen[key]; !ok {
				seen[key] = struct{}{}
				out = append(out, trimmed)
			}
		}
	}
	return out
}

func isDefaultLocalPort(endpoint string) bool {
	u, err := url.Parse(endpoint)
	if err != nil {
		return false
	}
	port := u.Port()
	return port == "" || port == "11434"
}

// EnsureDaemonRunning checks if the local Ollama endpoint is reachable; if not,
// and the `ollama` binary is installed locally, it spawns `ollama serve` in the
// background and waits up to 7.5 seconds for it to accept connections.
func (m *Manager) EnsureDaemonRunning(ctx context.Context, endpoint string) (ProbeResult, bool, error) {
	cleanEndpoint, err := ValidateLocalEndpoint(endpoint)
	if err != nil {
		return ProbeResult{}, false, err
	}
	initial := m.ProbeEndpoint(ctx, cleanEndpoint)
	if initial.OK {
		return initial, false, nil
	}
	if !isDefaultLocalPort(cleanEndpoint) {
		return initial, false, fmt.Errorf("endpoint %s is not running (auto-start only targets port 11434)", cleanEndpoint)
	}
	binPath := ResolveOllamaBinary()
	if binPath == "" {
		return initial, false, errors.New("Ollama binary not found on this machine. Install Ollama from https://ollama.com/download")
	}

	m.startMu.Lock()
	defer m.startMu.Unlock()

	// Re-probe after acquiring lock in case another goroutine just started it.
	if second := m.ProbeEndpoint(ctx, cleanEndpoint); second.OK {
		return second, false, nil
	}

	cmd := exec.Command(binPath, "serve")
	cmd.Env = append(os.Environ(), "OLLAMA_HOST=127.0.0.1:11434")
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		return initial, false, fmt.Errorf("failed to start %s serve: %w", binPath, err)
	}
	go func() {
		_ = cmd.Wait()
	}()

	// Poll up to 25 * 300ms (7.5 seconds) for Ollama to accept HTTP connections.
	for i := 0; i < 25; i++ {
		select {
		case <-ctx.Done():
			return initial, false, ctx.Err()
		case <-time.After(300 * time.Millisecond):
		}
		probe := m.ProbeEndpoint(ctx, cleanEndpoint)
		if probe.OK {
			return probe, true, nil
		}
	}
	return m.ProbeEndpoint(ctx, cleanEndpoint), false, errors.New("timed out waiting for local Ollama daemon to start on port 11434")
}

// GetStatus compiles the complete EngineStatus for the Settings UI using the saved endpoint.
func (m *Manager) GetStatus(ctx context.Context) EngineStatus {
	return m.GetStatusForEndpoint(ctx, "")
}

// GetStatusForEndpoint compiles the complete EngineStatus probing endpointOverride when non-empty.
func (m *Manager) GetStatusForEndpoint(ctx context.Context, endpointOverride string) EngineStatus {
	cfg := m.GetConfig()
	targetEndpoint := cfg.Endpoint
	if strings.TrimSpace(endpointOverride) != "" {
		if valid, err := ValidateLocalEndpoint(endpointOverride); err == nil {
			targetEndpoint = valid
		}
	}
	probe := m.ProbeEndpoint(ctx, targetEndpoint)
	return m.GetStatusWithProbe(targetEndpoint, probe)
}

// GetStatusWithProbe compiles the complete EngineStatus reusing an already-executed ProbeResult.
func (m *Manager) GetStatusWithProbe(endpointOverride string, probe ProbeResult) EngineStatus {
	cfg := m.GetConfig()
	if strings.TrimSpace(endpointOverride) != "" {
		if valid, err := ValidateLocalEndpoint(endpointOverride); err == nil {
			cfg.Endpoint = valid
		}
	}
	hw := GetHardwareProfile()
	binPath := ResolveOllamaBinary()

	tiers := make([]ModelTier, len(baseModelTiers))
	for i, t := range baseModelTiers {
		t.FitsRAM = hw.TotalRAMGB >= t.MinRAMGB
		t.Installed = IsTextModelInstalled(probe.Models, t.Model)
		tiers[i] = t
	}

	textReady := IsTextModelInstalled(probe.Models, cfg.TextModel)
	embedReady := IsEmbedModelInstalled(probe.Models, cfg.EmbedModel)

	var latencyPtr *int64
	if probe.OK {
		lat := probe.LatencyMs
		latencyPtr = &lat
	}

	cloudConfigured := strings.TrimSpace(os.Getenv("GOOGLE_CLOUD_PROJECT")) != "" ||
		strings.TrimSpace(os.Getenv("PROJECT_ID")) != "" ||
		strings.TrimSpace(os.Getenv("GEMINI_API_KEY")) != ""

	embedModel := cfg.EmbedModel
	if embedModel == "" {
		embedModel = DefaultEmbedModel
	}

	return EngineStatus{
		Provider:               cfg.Provider,
		Endpoint:               cfg.Endpoint,
		TextModel:              cfg.TextModel,
		EmbedModel:             cfg.EmbedModel,
		CloudProjectConfigured: cloudConfigured,
		Connected:              probe.OK,
		Protocol:               probe.Protocol,
		LatencyMs:              latencyPtr,
		BinaryInstalled:        binPath != "",
		BinaryPath:             binPath,
		TextModelReady:         textReady,
		EmbedModelReady:        embedReady,
		InstalledModels:        probe.Models,
		Hardware:               hw,
		ModelTiers:             tiers,
		EmbedEngine: EmbedEngineStatus{
			Model:           embedModel,
			ConfiguredModel: embedModel,
			Label:           "EmbeddingGemma · Local Vector Engine",
			Subtitle:        "Google local embeddings for document RAG & passage retrieval",
			SizeGB:          0.6,
			NativeDim:       768,
			PgVectorDim:     3072,
			Installed:       embedReady,
		},
		PullProgress: m.GetPullProgress(),
	}
}

// GetPullProgress returns a snapshot of the current or most recent model pull.
func (m *Manager) GetPullProgress() PullProgress {
	m.pullMu.RLock()
	defer m.pullMu.RUnlock()
	return m.pullState
}

// PullModel downloads one or more models ("bundle" pulls both the recommended/configured
// Gemma text model and embeddinggemma:latest). If the model is already installed, it returns
// immediately. For fast mock/local servers that complete within 2 seconds, it returns
// synchronously; for large multi-GB downloads, it continues streaming in the background
// while updating PullProgress so the 90-second BFF proxy never times out.
func (m *Manager) PullModel(ctx context.Context, endpoint, requestedModel string) (PullProgress, error) {
	cfg := m.GetConfig()
	if strings.TrimSpace(endpoint) == "" {
		endpoint = cfg.Endpoint
	}
	cleanEndpoint, err := ValidateLocalEndpoint(endpoint)
	if err != nil {
		return PullProgress{}, err
	}

	var targets []string
	trimmedReq := strings.TrimSpace(requestedModel)
	if strings.EqualFold(trimmedReq, "bundle") || strings.HasPrefix(strings.ToLower(trimmedReq), "bundle:") {
		hw := GetHardwareProfile()
		textTarget := cfg.TextModel
		if strings.HasPrefix(strings.ToLower(trimmedReq), "bundle:") {
			customText := strings.TrimSpace(trimmedReq[len("bundle:"):])
			if customText != "" {
				validCustom, verr := ValidateModelName(customText)
				if verr != nil {
					return PullProgress{}, verr
				}
				textTarget = validCustom
			}
		}
		if textTarget == "" {
			textTarget = hw.RecommendedModel
		}
		embedTarget := cfg.EmbedModel
		if embedTarget == "" {
			embedTarget = DefaultEmbedModel
		}
		targets = []string{textTarget, embedTarget}
	} else {
		validModel, verr := ValidateModelName(trimmedReq)
		if verr != nil {
			return PullProgress{}, verr
		}
		targets = []string{validModel}
	}

	if current := m.GetPullProgress(); current.Active {
		return current, nil
	}

	// Ensure daemon is running if targeting default port and binary is present.
	probe := m.ProbeEndpoint(ctx, cleanEndpoint)
	if !probe.OK && isDefaultLocalPort(cleanEndpoint) && ResolveOllamaBinary() != "" {
		if startedProbe, _, startErr := m.EnsureDaemonRunning(ctx, cleanEndpoint); startErr == nil {
			probe = startedProbe
		}
	}

	// Filter to only missing models so "bundle" skips models that are already installed
	// on the reachable Ollama endpoint.
	var missingTargets []string
	if probe.OK {
		for _, t := range targets {
			if strings.Contains(strings.ToLower(t), "embed") {
				if !IsEmbedModelInstalled(probe.Models, t) {
					missingTargets = append(missingTargets, t)
				}
			} else if !IsTextModelInstalled(probe.Models, t) {
				missingTargets = append(missingTargets, t)
			}
		}
	} else {
		missingTargets = append(missingTargets, targets...)
	}
	if len(missingTargets) == 0 && len(targets) > 0 {
		doneState := PullProgress{
			Active:    false,
			Model:     strings.Join(targets, ", "),
			Status:    "already_installed",
			Percent:   100,
			Done:      true,
			UpdatedAt: time.Now().UTC().Format(time.RFC3339),
		}
		m.setPullProgress(doneState)
		return doneState, nil
	}

	m.pullMu.Lock()
	if m.pullState.Active {
		current := m.pullState
		m.pullMu.Unlock()
		return current, nil
	}
	initialState := PullProgress{
		Active:    true,
		Model:     missingTargets[0],
		Status:    "starting",
		Percent:   0,
		Done:      false,
		UpdatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	m.pullState = initialState
	m.pullMu.Unlock()

	doneCh := make(chan struct{})
	go func() {
		defer close(doneCh)
		m.runPullSequence(cleanEndpoint, missingTargets)
	}()

	// Wait up to 2.5 seconds: fast tests or cached layer pulls finish synchronously,
	// while multi-GB downloads return the active PullProgress immediately so the
	// frontend can poll GET /api/v1/settings/ai without hitting the 90s BFF timeout.
	select {
	case <-doneCh:
		final := m.GetPullProgress()
		if final.Error != "" {
			return final, errors.New(final.Error)
		}
		return final, nil
	case <-time.After(2500 * time.Millisecond):
		return m.GetPullProgress(), nil
	}
}

func (m *Manager) setPullProgress(p PullProgress) {
	m.pullMu.Lock()
	defer m.pullMu.Unlock()
	m.pullState = p
}

func (m *Manager) runPullSequence(endpoint string, targets []string) {
	fetchBase := resolveIPv4Endpoint(endpoint)
	client := &http.Client{Timeout: 30 * time.Minute}

	for idx, modelName := range targets {
		m.setPullProgress(PullProgress{
			Active:    true,
			Model:     modelName,
			Status:    fmt.Sprintf("pulling (%d/%d): %s", idx+1, len(targets), modelName),
			Percent:   0,
			Done:      false,
			UpdatedAt: time.Now().UTC().Format(time.RFC3339),
		})

		reqBody, _ := json.Marshal(map[string]any{
			"name":   modelName,
			"stream": true,
		})
		req, err := http.NewRequest(http.MethodPost, fetchBase+"/api/pull", bytes.NewReader(reqBody))
		if err != nil {
			m.setPullProgress(PullProgress{
				Active:    false,
				Model:     modelName,
				Status:    "error",
				Error:     err.Error(),
				UpdatedAt: time.Now().UTC().Format(time.RFC3339),
			})
			return
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := client.Do(req)
		if err != nil {
			m.setPullProgress(PullProgress{
				Active:    false,
				Model:     modelName,
				Status:    "error",
				Error:     fmt.Sprintf("Failed to contact Ollama at %s: %v", endpoint, err),
				UpdatedAt: time.Now().UTC().Format(time.RFC3339),
			})
			return
		}

		if resp.StatusCode != http.StatusOK {
			errBytes, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
			resp.Body.Close()
			m.setPullProgress(PullProgress{
				Active:    false,
				Model:     modelName,
				Status:    "error",
				Error:     fmt.Sprintf("Ollama pull %s failed (HTTP %d): %s", modelName, resp.StatusCode, strings.TrimSpace(string(errBytes))),
				UpdatedAt: time.Now().UTC().Format(time.RFC3339),
			})
			return
		}

		scanner := bufio.NewScanner(resp.Body)
		scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		framesSeen := 0
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if line == "" {
				continue
			}
			var frame struct {
				Status    string `json:"status"`
				Completed int64  `json:"completed"`
				Total     int64  `json:"total"`
				Error     string `json:"error"`
			}
			if jerr := json.Unmarshal([]byte(line), &frame); jerr == nil {
				framesSeen++
				if frame.Error != "" {
					resp.Body.Close()
					m.setPullProgress(PullProgress{
						Active:    false,
						Model:     modelName,
						Status:    "error",
						Error:     frame.Error,
						UpdatedAt: time.Now().UTC().Format(time.RFC3339),
					})
					return
				}
				pct := 0.0
				if frame.Total > 0 {
					pct = math.Min(99.9, math.Round((float64(frame.Completed)/float64(frame.Total))*1000)/10)
				}
				statusText := frame.Status
				if statusText == "" {
					statusText = "downloading"
				}
				m.setPullProgress(PullProgress{
					Active:         true,
					Model:          modelName,
					Status:         statusText,
					Percent:        pct,
					CompletedBytes: frame.Completed,
					TotalBytes:     frame.Total,
					Done:           false,
					UpdatedAt:      time.Now().UTC().Format(time.RFC3339),
				})
			}
		}
		scanErr := scanner.Err()
		resp.Body.Close()
		if scanErr != nil {
			m.setPullProgress(PullProgress{
				Active:    false,
				Model:     modelName,
				Status:    "error",
				Error:     fmt.Sprintf("Stream interrupted while pulling %s: %v", modelName, scanErr),
				UpdatedAt: time.Now().UTC().Format(time.RFC3339),
			})
			return
		}
		if framesSeen == 0 {
			m.setPullProgress(PullProgress{
				Active:    false,
				Model:     modelName,
				Status:    "error",
				Error:     fmt.Sprintf("Ollama pull %s returned an empty stream", modelName),
				UpdatedAt: time.Now().UTC().Format(time.RFC3339),
			})
			return
		}
	}

	m.setPullProgress(PullProgress{
		Active:    false,
		Model:     strings.Join(targets, ", "),
		Status:    "completed",
		Percent:   100,
		Done:      true,
		UpdatedAt: time.Now().UTC().Format(time.RFC3339),
	})
}
