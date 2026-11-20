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

package main

import (
	"errors"
	"log"
	"net/http"
	"strings"
	"sync"

	"github.com/GoogleCloudPlatform/race-condition/internal/account"
	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/localai"
	"github.com/gin-gonic/gin"
)

const maxAISettingsBodyBytes = 1 << 20

var (
	aiSettingsMu      sync.Mutex
	aiSettingsManager *localai.Manager
)

func getAiSettingsManager() *localai.Manager {
	aiSettingsMu.Lock()
	defer aiSettingsMu.Unlock()
	if aiSettingsManager == nil {
		settingsPath := config.Optional("AI_SETTINGS_PATH", "data/ai_settings.json")
		aiSettingsManager = localai.NewManager(settingsPath)
	}
	return aiSettingsManager
}

func closeAiSettingsManager() {
	aiSettingsMu.Lock()
	mgr := aiSettingsManager
	aiSettingsManager = nil
	aiSettingsMu.Unlock()
	if mgr != nil {
		if err := mgr.Close(); err != nil {
			log.Printf("GATEWAY: close AI settings manager error: %v", err)
		}
	}
}

type aiTestRequest struct {
	Endpoint  string `json:"endpoint"`
	AutoStart bool   `json:"auto_start"`
}

type aiPullRequest struct {
	Model    string `json:"model"`
	Endpoint string `json:"endpoint"`
}

func canConfigureAISettings(c *gin.Context) bool {
	if !account.SaaSModeEnabled() {
		return true
	}
	uc, ok := account.FromContextOK(c.Request.Context())
	return ok && strings.TrimSpace(uc.UserID) != "" && uc.IsSuperadmin
}

func requireAISettingsMutationAdmin(c *gin.Context) bool {
	if !account.SaaSModeEnabled() {
		return true
	}
	uc, ok := account.FromContextOK(c.Request.Context())
	if !ok || strings.TrimSpace(uc.UserID) == "" {
		c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication_required"})
		return false
	}
	if !uc.IsSuperadmin {
		c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "superadmin_required"})
		return false
	}
	return true
}

// mountAiSettingsRoutes registers the /api/v1/settings/ai/* endpoints unconditionally
// so users can inspect, and local-dev or SaaS superadmins can test, start, pull,
// and switch between Google Cloud Gemini and Local Google Gemma + EmbeddingGemma models.
func mountAiSettingsRoutes(r *gin.Engine, mgr *localai.Manager) {
	if mgr == nil {
		mgr = getAiSettingsManager()
	}

	r.GET("/api/v1/settings/ai", func(c *gin.Context) {
		queryEndpoint := strings.TrimSpace(c.Query("endpoint"))
		var normEndpoint string
		if queryEndpoint != "" {
			validEndpoint, err := localai.ValidateLocalEndpoint(queryEndpoint)
			if err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			resolvedEndpoint, err := localai.ResolveIPv4EndpointValidated(validEndpoint)
			if err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			normEndpoint = resolvedEndpoint
		}
		status, err := mgr.GetStatusForEndpointValidated(c.Request.Context(), normEndpoint)
		if err != nil {
			log.Printf("GET /api/v1/settings/ai config error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
			return
		}
		status.CanConfigure = canConfigureAISettings(c)
		c.JSON(http.StatusOK, status)
	})

	r.PUT("/api/v1/settings/ai", func(c *gin.Context) {
		if !requireAISettingsMutationAdmin(c) {
			return
		}
		var req localai.Config
		if err := decodeStrictJSON(c, &req, maxAISettingsBodyBytes, false); err != nil {
			var maxErr *http.MaxBytesError
			if errors.As(err, &maxErr) {
				c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request_body_too_large"})
				return
			}
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request payload: " + err.Error()})
			return
		}
		if _, err := mgr.SaveConfig(req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		status, err := mgr.GetStatusForEndpointValidated(c.Request.Context(), "")
		if err != nil {
			log.Printf("PUT /api/v1/settings/ai status error: %v", err)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
			return
		}
		status.CanConfigure = canConfigureAISettings(c)
		c.JSON(http.StatusOK, status)
	})

	r.POST("/api/v1/settings/ai/test", func(c *gin.Context) {
		if !requireAISettingsMutationAdmin(c) {
			return
		}
		var req aiTestRequest
		if err := decodeStrictJSON(c, &req, maxAISettingsBodyBytes, true); err != nil {
			var maxErr *http.MaxBytesError
			if errors.As(err, &maxErr) {
				c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request_body_too_large"})
				return
			}
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request payload: " + err.Error()})
			return
		}

		cfg, cfgErr := mgr.LoadConfig()
		if cfgErr != nil {
			log.Printf("POST /api/v1/settings/ai/test config error: %v", cfgErr)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
			return
		}
		endpoint := strings.TrimSpace(req.Endpoint)
		if endpoint == "" {
			endpoint = cfg.Endpoint
		}
		validEndpoint, err := localai.ValidateLocalEndpoint(endpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		normEndpoint, err := localai.ResolveIPv4EndpointValidated(validEndpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}

		probe := mgr.ProbeEndpoint(c.Request.Context(), normEndpoint)
		if !probe.OK && req.AutoStart {
			startedProbe, _, startErr := mgr.EnsureDaemonRunning(c.Request.Context(), normEndpoint)
			if startErr != nil {
				status, statusErr := mgr.GetStatusWithProbeValidated(normEndpoint, startedProbe)
				if statusErr != nil {
					log.Printf("POST /api/v1/settings/ai/test status error: %v", statusErr)
					c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
					return
				}
				status.CanConfigure = canConfigureAISettings(c)
				c.JSON(http.StatusServiceUnavailable, gin.H{
					"reachable":        false,
					"endpoint":         normEndpoint,
					"installed_models": startedProbe.Models,
					"error":            startErr.Error(),
					"status":           status,
				})
				return
			}
			probe = startedProbe
		}

		status, statusErr := mgr.GetStatusWithProbeValidated(normEndpoint, probe)
		if statusErr != nil {
			log.Printf("POST /api/v1/settings/ai/test status error: %v", statusErr)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
			return
		}
		status.CanConfigure = canConfigureAISettings(c)
		c.JSON(http.StatusOK, gin.H{
			"reachable":        probe.OK,
			"endpoint":         normEndpoint,
			"installed_models": probe.Models,
			"error":            probe.Error,
			"status":           status,
		})
	})

	r.POST("/api/v1/settings/ai/start", func(c *gin.Context) {
		if !requireAISettingsMutationAdmin(c) {
			return
		}
		var req aiTestRequest
		if err := decodeStrictJSON(c, &req, maxAISettingsBodyBytes, true); err != nil {
			var maxErr *http.MaxBytesError
			if errors.As(err, &maxErr) {
				c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request_body_too_large"})
				return
			}
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request payload: " + err.Error()})
			return
		}

		cfg, cfgErr := mgr.LoadConfig()
		if cfgErr != nil {
			log.Printf("POST /api/v1/settings/ai/start config error: %v", cfgErr)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
			return
		}
		endpoint := strings.TrimSpace(req.Endpoint)
		if endpoint == "" {
			endpoint = cfg.Endpoint
		}
		validEndpoint, err := localai.ValidateLocalEndpoint(endpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		normEndpoint, err := localai.ResolveIPv4EndpointValidated(validEndpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		probe, _, err := mgr.EnsureDaemonRunning(c.Request.Context(), normEndpoint)
		if err != nil {
			status, statusErr := mgr.GetStatusWithProbeValidated(normEndpoint, probe)
			if statusErr != nil {
				log.Printf("POST /api/v1/settings/ai/start status error: %v", statusErr)
				c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
				return
			}
			status.CanConfigure = canConfigureAISettings(c)
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error":  err.Error(),
				"status": status,
			})
			return
		}
		status, statusErr := mgr.GetStatusWithProbeValidated(normEndpoint, probe)
		if statusErr != nil {
			log.Printf("POST /api/v1/settings/ai/start status error: %v", statusErr)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
			return
		}
		status.CanConfigure = canConfigureAISettings(c)
		c.JSON(http.StatusOK, status)
	})

	r.POST("/api/v1/settings/ai/pull", func(c *gin.Context) {
		if !requireAISettingsMutationAdmin(c) {
			return
		}
		var req aiPullRequest
		if err := decodeStrictJSON(c, &req, maxAISettingsBodyBytes, false); err != nil {
			var maxErr *http.MaxBytesError
			if errors.As(err, &maxErr) {
				c.JSON(http.StatusRequestEntityTooLarge, gin.H{"error": "request_body_too_large"})
				return
			}
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request payload: " + err.Error()})
			return
		}
		model, err := localai.ValidateModelName(req.Model)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		cfg, cfgErr := mgr.LoadConfig()
		if cfgErr != nil {
			log.Printf("POST /api/v1/settings/ai/pull config error: %v", cfgErr)
			c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
			return
		}
		endpoint := strings.TrimSpace(req.Endpoint)
		if endpoint == "" {
			endpoint = cfg.Endpoint
		}
		validEndpoint, err := localai.ValidateLocalEndpoint(endpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		normEndpoint, err := localai.ResolveIPv4EndpointValidated(validEndpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}

		progress, pullErr := mgr.PullModel(c.Request.Context(), normEndpoint, model)
		status, statusErr := mgr.GetStatusForEndpointValidated(c.Request.Context(), normEndpoint)
		if statusErr != nil {
			log.Printf("POST /api/v1/settings/ai/pull status error: %v", statusErr)
			if pullErr != nil {
				c.JSON(http.StatusBadGateway, gin.H{
					"error":         pullErr.Error(),
					"pull_progress": progress,
				})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "ai settings unavailable"})
			return
		}
		status.CanConfigure = canConfigureAISettings(c)
		if pullErr != nil {
			c.JSON(http.StatusBadGateway, gin.H{
				"error":         pullErr.Error(),
				"pull_progress": progress,
				"status":        status,
			})
			return
		}
		c.JSON(http.StatusOK, gin.H{
			"pull_progress": progress,
			"status":        status,
		})
	})
}
