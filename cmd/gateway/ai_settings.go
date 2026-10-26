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
	"net/http"
	"strings"
	"sync"

	"github.com/GoogleCloudPlatform/race-condition/internal/config"
	"github.com/GoogleCloudPlatform/race-condition/internal/localai"
	"github.com/gin-gonic/gin"
)

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

type aiTestRequest struct {
	Endpoint  string `json:"endpoint"`
	AutoStart bool   `json:"auto_start"`
}

type aiPullRequest struct {
	Model    string `json:"model"`
	Endpoint string `json:"endpoint"`
}

// mountAiSettingsRoutes registers the /api/v1/settings/ai/* endpoints unconditionally
// so users can inspect, test, start, pull, and switch between Google Cloud Gemini and
// Local Google Gemma + EmbeddingGemma models even without Postgres or cloud credentials.
func mountAiSettingsRoutes(r *gin.Engine, mgr *localai.Manager) {
	if mgr == nil {
		mgr = getAiSettingsManager()
	}

	r.GET("/api/v1/settings/ai", func(c *gin.Context) {
		queryEndpoint := strings.TrimSpace(c.Query("endpoint"))
		if queryEndpoint != "" {
			normEndpoint, err := localai.ValidateLocalEndpoint(queryEndpoint)
			if err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			status := mgr.GetStatusForEndpoint(c.Request.Context(), normEndpoint)
			c.JSON(http.StatusOK, status)
			return
		}
		status := mgr.GetStatus(c.Request.Context())
		c.JSON(http.StatusOK, status)
	})

	r.PUT("/api/v1/settings/ai", func(c *gin.Context) {
		var req localai.Config
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request payload: " + err.Error()})
			return
		}
		if _, err := mgr.SaveConfig(req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		status := mgr.GetStatus(c.Request.Context())
		c.JSON(http.StatusOK, status)
	})

	r.POST("/api/v1/settings/ai/test", func(c *gin.Context) {
		var req aiTestRequest
		_ = c.ShouldBindJSON(&req)

		endpoint := strings.TrimSpace(req.Endpoint)
		if endpoint == "" {
			endpoint = mgr.GetConfig().Endpoint
		}
		normEndpoint, err := localai.ValidateLocalEndpoint(endpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}

		probe := mgr.ProbeEndpoint(c.Request.Context(), normEndpoint)
		if !probe.OK && req.AutoStart {
			if startedProbe, _, startErr := mgr.EnsureDaemonRunning(c.Request.Context(), normEndpoint); startErr == nil {
				probe = startedProbe
			}
		}

		status := mgr.GetStatusWithProbe(normEndpoint, probe)
		c.JSON(http.StatusOK, gin.H{
			"reachable":        probe.OK,
			"endpoint":         normEndpoint,
			"installed_models": probe.Models,
			"error":            probe.Error,
			"status":           status,
		})
	})

	r.POST("/api/v1/settings/ai/start", func(c *gin.Context) {
		var req aiTestRequest
		_ = c.ShouldBindJSON(&req)

		endpoint := strings.TrimSpace(req.Endpoint)
		if endpoint == "" {
			endpoint = mgr.GetConfig().Endpoint
		}
		normEndpoint, err := localai.ValidateLocalEndpoint(endpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		probe, _, err := mgr.EnsureDaemonRunning(c.Request.Context(), normEndpoint)
		if err != nil {
			status := mgr.GetStatusWithProbe(normEndpoint, probe)
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"error":  err.Error(),
				"status": status,
			})
			return
		}
		status := mgr.GetStatusWithProbe(normEndpoint, probe)
		c.JSON(http.StatusOK, status)
	})

	r.POST("/api/v1/settings/ai/pull", func(c *gin.Context) {
		var req aiPullRequest
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request payload: " + err.Error()})
			return
		}
		model, err := localai.ValidateModelName(req.Model)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		endpoint := strings.TrimSpace(req.Endpoint)
		if endpoint == "" {
			endpoint = mgr.GetConfig().Endpoint
		}
		normEndpoint, err := localai.ValidateLocalEndpoint(endpoint)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}

		progress, err := mgr.PullModel(c.Request.Context(), normEndpoint, model)
		if err != nil {
			status := mgr.GetStatusForEndpoint(c.Request.Context(), normEndpoint)
			c.JSON(http.StatusBadGateway, gin.H{
				"error":         err.Error(),
				"pull_progress": progress,
				"status":        status,
			})
			return
		}
		status := mgr.GetStatusForEndpoint(c.Request.Context(), normEndpoint)
		c.JSON(http.StatusOK, gin.H{
			"pull_progress": progress,
			"status":        status,
		})
	})
}
