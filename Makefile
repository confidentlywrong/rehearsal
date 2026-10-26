# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Makefile for The Rehearsal
# Run `make help` for available targets.

# Load .env if it exists (port assignments, GCP config, etc.)
-include .env
export

# Set a local uv cache to bypass macOS permissions issues on ~/.cache
export UV_CACHE_DIR ?= ./.uv_cache

.PHONY: init check-prereqs infra-init infra-plan infra-apply infra-destroy infra-output deploy deploy-local help test lint lint-go lint-py lint-pyright lint-configs check-license fmt build proto ensure-venv coverage coverage-go coverage-py test-unit-go test-integration-go test-py-integration test-web verify perf-test perf-diagnostic eval-stress test-e2e-simulation docker-build-all docker-build-go docker-build-py eval start status stop restart clean

.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' Makefile | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- Process Management ---
# Lifecycle is managed by scripts/core/sim.py (single source of truth).
# See: uv run start --help, uv run stop --help
start: ## Start all services via Honcho
	@uv run start --skip-tests

status: ## Show status of running services, containers, and ports
	@python3 scripts/core/sim.py status

# --- SaaS shell (Phase 1) ---
# Opt-in multi-tenant mode. Brings up the Firebase Auth emulator
# alongside the usual infra, then runs Honcho with SAAS_MODE=on.
# Single-tenant `make start` is untouched.
saas-local: ## Start with SaaS shell + Firebase Auth emulator (Phase 1)
	@docker compose --profile saas up -d
	@SAAS_MODE=on IDENTITY_PROVIDER=firebase \
	  FIREBASE_PROJECT_ID=$${FIREBASE_PROJECT_ID:-rc-local-dev} \
	  FIREBASE_AUTH_EMULATOR_HOST=localhost:9099 \
	  uv run start --skip-tests

stop: ## Stop all services and free ports
	@uv run stop

restart: ## Restart all services
	@uv run restart --skip-tests

clean: ## Remove compiled Go binaries from repo root and cmd/
	@rm -f gateway admin billing frontend tester
	@rm -f cmd/gateway/gateway cmd/admin/admin cmd/billing/billing cmd/frontend/frontend cmd/tester/tester
	@echo "✅ Stale binaries removed."

# --- Verify (All Automated Layers) ---
# verify: Layers 1-3 (no infrastructure required)
# verify-full: Layers 1-4 (requires Redis/Docker for integration tests)
verify: lint coverage test-web ## Run lint, unit tests with coverage, and web tests (no infra needed)
	@echo "✅ Layers 1-3 passed (lint, tests + coverage)."

verify-full: verify test-integration-go test-py-integration ## Run all verification layers (requires Redis/Docker)
	@echo "✅ All automated verification layers (1-4) passed."

# --- Venv Bootstrap ---
ensure-venv:
	@if [ ! -x ".venv/bin/python3" ]; then echo "📦 Creating venv..." && uv sync; fi

# --- Proto Generation ---
proto: ensure-venv ## Generate protobuf bindings (Go + Python)
	bash scripts/core/generate_proto.sh

# --- Build ---
build: proto ## Build all Go binaries
	go build ./...

# --- Test ---
test: test-go test-py test-web test-deploy-sh ## Run all tests (Go + Python + Web + deploy.sh)

test-go:
	go test ./... -count=1 -short

test-py:
	uv run pytest agents/ -x -q -m "not slow and not integration"

test-web:
	@set -e; for dir in web/admin-dash web/agent-dash web/frontend web/tester; do \
		if [ -f "$$dir/package.json" ]; then \
			echo "🌐 Testing $$dir..."; \
			(cd $$dir && npm test); \
		fi; \
	done
	@echo "✅ All web UI tests passed."

test-deploy-sh: ## Run scripts/deploy.sh unit tests (region parity + argv shape)
	@bash scripts/deploy_test.sh

# --- Agent Evaluations (requires Gemini API) ---
eval: ## Run agent evaluations (requires Gemini API)
	uv run pytest agents/ -x -v -m "slow" -o "addopts="

# --- Performance Testing ---
perf-test:
	uv run python scripts/bench/perf_test.py $(PERF_ARGS)

perf-diagnostic:  ## Run performance diagnostic against GATEWAY_URL
	uv run python scripts/bench/perf_diagnostic.py $(PERF_ARGS)

# --- Eval Stress Testing ---
eval-stress:
	uv run python scripts/bench/eval_stress_test.py $(EVAL_STRESS_ARGS)

# --- Concurrency Benchmarking ---
bench-concurrency:
	uv run python scripts/bench/bench_concurrency.py $(BENCH_ARGS)

bench-test:
	uv run pytest scripts/tests/ -v

# --- Scale Testing ---
scale-test:
	uv run python scripts/e2e/e2e_scale_test.py $(SCALE_ARGS)

# --- Lint ---
lint: lint-go lint-py lint-pyright lint-configs ## Run all linters (Go + Python + configs)

GOLANGCI_LINT := $(shell command -v golangci-lint 2>/dev/null || echo "$$(go env GOPATH)/bin/golangci-lint")
PRE_COMMIT := $(shell command -v pre-commit 2>/dev/null || echo "uvx pre-commit")

lint-go:
	@command -v $(GOLANGCI_LINT) >/dev/null 2>&1 || [ -x "$(GOLANGCI_LINT)" ] || { echo "❌ golangci-lint not found. Install: go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest"; exit 1; }
	$(GOLANGCI_LINT) run ./...

lint-py:
	uv run ruff check agents/

lint-pyright:
	npx --yes pyright@1.1.365 agents/

lint-configs:
	@echo "🔍 Validating YAML, JSON, and Dockerfile syntax..."
	$(PRE_COMMIT) run check-yaml --all-files
	$(PRE_COMMIT) run check-json --all-files
	@echo "🔍 Checking Node version consistency..."
	./scripts/check-node-versions.sh
	@echo "✅ All config files valid."

check-license: ## Verify Apache 2.0 license headers across source files
	go run github.com/google/addlicense@latest -check -y "2024-2030" \
		-ignore 'LICENSE' -ignore 'gen_proto/**' -ignore 'web/**/node_modules/**' -ignore 'web/**/dist/**' \
		-ignore 'web/**/.angular/**' -ignore '.venv/**' -ignore '.uv_cache/**' \
		-ignore '**/.terraform.lock.hcl' .

# --- Format ---
fmt: fmt-go fmt-py ## Format all code (Go + Python)

fmt-go:
	gofmt -w .

fmt-py:
	uv run ruff format agents/

# --- Coverage ---
coverage: coverage-go coverage-py ## Generate coverage reports (Go + Python)

coverage-go:
	go test $$(go list ./... | grep -v gen_proto) -coverprofile=coverage.out -count=1 -short
	go tool cover -func=coverage.out
	go tool cover -html=coverage.out -o coverage.html
	@echo "✅ Go coverage report: coverage.html"

coverage-py:
	uv run pytest agents/ -x -q -m "not slow and not integration" --cov=agents --cov-report=term-missing --cov-report=html:htmlcov --cov-fail-under=60
	@echo "✅ Python coverage report: htmlcov/index.html"

# --- Test Separation ---
test-unit-go:
	go test ./... -count=1 -short

test-integration-go:
	go test ./... -count=1 -run "Integration|Relay"

test-py-integration: ## Run Python integration tests against hermetic compose
	@trap 'docker compose -f docker-compose.test.yml down' EXIT INT TERM; \
		docker compose -f docker-compose.test.yml up -d --wait && \
		uv run pytest agents/ -q -m integration -o "addopts="

# --- E2E Simulation Test ---
test-e2e-simulation:
	uv run python scripts/e2e/e2e_simulation_test.py

# --- Docker Image Builds ---
# Build per-service images using the multi-stage Dockerfile.
# Usage:
#   make docker-build-gateway                    # build single service
#   make docker-build-all                        # build all services
#   make docker-build-gateway TAG=v1.2.3         # custom tag
#   make docker-build-all REGISTRY=my-registry   # custom registry
REGISTRY ?= rehearsal
TAG ?= latest

docker-build-%:
	docker buildx build --target $* -t $(REGISTRY)/$*:$(TAG) --load .

docker-build-all: docker-build-gateway docker-build-admin docker-build-tester docker-build-frontend docker-build-billing docker-build-runner_autopilot docker-build-runner_cloudrun docker-build-leadership_runner_autopilot docker-build-leadership_runner docker-build-ai_echo docker-build-ai_echo_reactive docker-build-ai_echo_autopilot docker-build-dash ## Build all Docker images

docker-build-go: docker-build-gateway docker-build-admin docker-build-tester docker-build-frontend docker-build-billing

docker-build-py: docker-build-runner_autopilot docker-build-runner_cloudrun docker-build-leadership_runner_autopilot docker-build-leadership_runner docker-build-ai_echo docker-build-ai_echo_reactive docker-build-ai_echo_autopilot docker-build-dash


# --- Developer Experience ---
PREREQS := go node uv docker

check-prereqs: ## Verify all prerequisites are installed
	@echo "Checking prerequisites..."
	@fail=0; \
	for cmd in $(PREREQS); do \
		if command -v $$cmd >/dev/null 2>&1; then \
			printf "  ✅ %-10s %s\n" "$$cmd" "$$($$cmd --version 2>/dev/null | head -1)"; \
		else \
			printf "  ❌ %-10s not found\n" "$$cmd"; \
			fail=1; \
		fi; \
	done; \
	if ! docker compose version >/dev/null 2>&1; then \
		echo "  ❌ docker compose (v2) not found"; \
		fail=1; \
	else \
		printf "  ✅ %-10s %s\n" "compose" "$$(docker compose version --short)"; \
	fi; \
	if [ $$fail -eq 1 ]; then echo "\nInstall missing prerequisites and retry."; exit 1; fi
	@echo "All prerequisites found."

init: check-prereqs ## One-time setup: install deps, configure env, start infra, build
	@echo ""
	@echo "=== Initializing The Rehearsal ==="
	@echo ""
	@if [ ! -f .env ]; then \
		echo "📋 Creating .env from .env.example..."; \
		cp .env.example .env; \
		echo "   Edit .env to set your GOOGLE_CLOUD_PROJECT and other config."; \
	else \
		echo "📋 .env already exists, skipping."; \
	fi
	@echo ""
	@echo "📦 Installing Python dependencies..."
	uv sync
	@echo ""
	@echo "📦 Installing and building web UIs..."
	cd web/frontend && npm install
	@set -e; for dir in web/admin-dash web/tester; do \
		if [ -f "$$dir/package.json" ]; then \
			echo "   Building $$dir..."; \
			(cd $$dir && npm install && npm run build); \
		fi; \
	done
	@echo ""
	@echo "🐳 Starting infrastructure (Redis, Pub/Sub, Postgres)..."
	docker compose up -d
	@echo ""
	@echo "🔨 Building Go services..."
	$(MAKE) build
	@echo ""
	@echo "=== Initialization Complete ==="
	@echo ""
	@echo "Next: run 'make start' to start all services."

# --- Infrastructure (Terraform) ---
INFRA_DIR ?= infra

infra-init: ## Initialize Terraform
	terraform -chdir=$(INFRA_DIR) init

infra-plan: ## Plan infrastructure changes
	terraform -chdir=$(INFRA_DIR) plan -out=tfplan

infra-apply: ## Apply infrastructure changes
	terraform -chdir=$(INFRA_DIR) apply tfplan

infra-destroy: ## Destroy infrastructure
	terraform -chdir=$(INFRA_DIR) destroy

infra-output: ## Show Terraform outputs
	terraform -chdir=$(INFRA_DIR) output

# --- Deployment ---
# `make deploy PROJECT_ID=...` runs the one-shot Cloud Build orchestrator
# (cloudbuild-bootstrap.yaml). It does GCS state bootstrap, terraform apply
# (base infra), parallel image builds, parallel Agent Engine deploys, and
# terraform apply (Cloud Run + IAM with image tags). No prior `terraform
# apply` required -- this is the entry point a fresh project uses.
deploy: ## Deploy everything in one shot via Cloud Build (no pre-flight terraform required)
	@if [ -z "$(PROJECT_ID)" ]; then echo "Usage: make deploy PROJECT_ID=your-project-id [REGION=us-central1]"; exit 1; fi
	gcloud builds submit --config cloudbuild-bootstrap.yaml --project $(PROJECT_ID) --region $${REGION:-us-central1} --substitutions _REGION=$${REGION:-us-central1} --machine-type=e2-highcpu-32 --timeout=7200s .
