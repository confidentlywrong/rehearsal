# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
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

# ============================================================
# Per-service multi-stage Dockerfile
#
# Build individual service images with:
#   docker build --target <service> -t <tag> .
#
# Available targets:
#   Go:     gateway, admin, tester, frontend, billing
#   Python: runner_autopilot, runner_cloudrun, leadership_runner_autopilot,
#           leadership_runner, ai_echo, ai_echo_reactive, ai_echo_autopilot, dash
# ============================================================


# ============================================================
# BASE STAGES
# ============================================================

# --- Go: build base with deps and shared source ---
# Use $BUILDPLATFORM so the Go compiler runs natively (no QEMU emulation).
# Cross-compilation is handled via GOOS/GOARCH env vars.
FROM --platform=$BUILDPLATFORM golang:1.25 AS go-base
ARG TARGETOS TARGETARCH
ENV GOOS=${TARGETOS} GOARCH=${TARGETARCH}
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY internal/ internal/
COPY gen_proto/ gen_proto/

# --- Web: build Vite/React frontends (admin-dash + tester) ---
# NOTE: Node version must match the project's local dev environment to ensure
# npm lockfile compatibility (npm lockfile v3 format varies across major versions).
FROM node:26-slim AS web-builder

# admin-dash — deps first for layer caching
WORKDIR /app/web/admin-dash
COPY web/admin-dash/package.json web/admin-dash/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/admin-dash/ ./
RUN npm run build

# tester — deps first for layer caching
WORKDIR /app/web/tester
COPY web/tester/package.json web/tester/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/tester/ ./
RUN npm run build

WORKDIR /app

# --- Python: dependency cache (rebuilt only when pyproject.toml/uv.lock change) ---
FROM python:3.13-slim AS python-deps
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv==0.7.12
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev \
    && groupadd -r appuser && useradd -r -g appuser -d /app appuser \
    && chown -R appuser:appuser /app
USER appuser


# ============================================================
# GO BUILD STAGES — one per service, parallelized by BuildKit
# ============================================================

FROM go-base AS build-gateway
COPY cmd/gateway/ cmd/gateway/
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/service ./cmd/gateway

FROM go-base AS build-admin
COPY cmd/admin/ cmd/admin/
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/service ./cmd/admin

FROM go-base AS build-tester
COPY cmd/tester/ cmd/tester/
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/service ./cmd/tester

FROM go-base AS build-frontend
COPY cmd/frontend/ cmd/frontend/
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/service ./cmd/frontend

# billing: Stripe webhook receiver micro-service. Separate from
# gateway because it needs to be publicly reachable on its own URL
# (Stripe Dashboard webhook endpoint) AND has a different scaling
# profile (idle most of the time; bursty on subscription renewals).
FROM go-base AS build-billing
COPY cmd/billing/ cmd/billing/
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/service ./cmd/billing


# ============================================================
# FINAL IMAGES — Go services (distroless, ~10-25MB each)
# ============================================================

# --- gateway: API entry point, WebSocket hub, session routing ---
FROM gcr.io/distroless/static-debian12:nonroot AS gateway
WORKDIR /app
COPY --from=build-gateway /bin/service /bin/service
# account.ApplyMigration reads migrations/0001_users.sql from CWD on
# cold start (creates the per-user schema with pg_advisory_lock
# serialization). Without this COPY, the gateway logs "account
# migration failed" and every /api/v1/me request returns the
# Postgres error `relation "users" does not exist`.
COPY migrations/ migrations/
ENTRYPOINT ["/bin/service"]

# --- admin: Admin dashboard BFF + React SPA ---
FROM gcr.io/distroless/static-debian12:nonroot AS admin
WORKDIR /app
COPY --from=build-admin /bin/service /bin/service
COPY --from=web-builder /app/web/admin-dash/dist/ ./web/admin-dash/dist/
# See gateway stage above for rationale.
COPY migrations/ migrations/
ENTRYPOINT ["/bin/service"]

# --- tester: Tester UI BFF + React SPA ---
FROM gcr.io/distroless/static-debian12:nonroot AS tester
WORKDIR /app
COPY --from=build-tester /bin/service /bin/service
COPY --from=web-builder /app/web/tester/dist/ ./web/tester/dist/
ENTRYPOINT ["/bin/service"]

# --- frontend: Consumer frontend BFF + Angular dist ---
# NOTE: web/frontend/dist/ must be pre-built externally (Angular from ../frontend/)
FROM gcr.io/distroless/static-debian12:nonroot AS frontend
WORKDIR /app
COPY --from=build-frontend /bin/service /bin/service
COPY web/frontend/dist/ ./web/frontend/dist/
# See gateway stage above for rationale.
COPY migrations/ migrations/
ENTRYPOINT ["/bin/service"]

# --- billing: Stripe webhook receiver (cmd/billing) ---
# No static assets; pure HTTP handler. Listens on $PORT (default 9122
# locally; Cloud Run sets PORT=8080). Refuses to start when
# BILLING_PROVIDER=stripe AND STRIPE_WEBHOOK_SECRET is empty, so a
# misconfigured deploy fails fast rather than silently rejecting
# every event.
FROM gcr.io/distroless/static-debian12:nonroot AS billing
WORKDIR /app
COPY --from=build-billing /bin/service /bin/service
# See gateway stage above for rationale.
COPY migrations/ migrations/
ENTRYPOINT ["/bin/service"]


# ============================================================
# FINAL IMAGES — Python services (~200MB each)
# ============================================================

# --- runner_autopilot: Deterministic runner (Cloud Run, subscriber dispatch) ---
FROM python-deps AS runner_autopilot
COPY agents/__init__.py ./agents/__init__.py
COPY agents/utils/ ./agents/utils/
COPY agents/runner/ ./agents/runner/
COPY agents/runner_autopilot/ ./agents/runner_autopilot/
COPY gen_proto/ ./gen_proto/
ENV PYTHONPATH=.
CMD [".venv/bin/python", "agents/runner_autopilot/agent.py"]

# --- runner_cloudrun: LLM-powered runner (Cloud Run, subscriber dispatch) ---
FROM python-deps AS runner_cloudrun
COPY agents/__init__.py ./agents/__init__.py
COPY agents/utils/ ./agents/utils/
COPY agents/runner/ ./agents/runner/
COPY gen_proto/ ./gen_proto/
ENV PYTHONPATH=.
CMD [".venv/bin/python", "agents/runner/agent.py"]

# --- leadership_runner_autopilot: Deterministic leadership stakeholder (Cloud Run, subscriber dispatch) ---
FROM python-deps AS leadership_runner_autopilot
COPY agents/__init__.py ./agents/__init__.py
COPY agents/utils/ ./agents/utils/
COPY agents/runner/ ./agents/runner/
COPY agents/leadership_runner/ ./agents/leadership_runner/
COPY agents/leadership_runner_autopilot/ ./agents/leadership_runner_autopilot/
COPY gen_proto/ ./gen_proto/
ENV PYTHONPATH=.
CMD [".venv/bin/python", "agents/leadership_runner_autopilot/agent.py"]

# --- leadership_runner: LLM-powered leadership stakeholder (Cloud Run, subscriber dispatch) ---
FROM python-deps AS leadership_runner
COPY agents/__init__.py ./agents/__init__.py
COPY agents/utils/ ./agents/utils/
COPY agents/runner/ ./agents/runner/
COPY agents/leadership_runner/ ./agents/leadership_runner/
COPY gen_proto/ ./gen_proto/
ENV PYTHONPATH=.
CMD [".venv/bin/python", "agents/leadership_runner/agent.py"]

# --- ai_echo: Autonomous Tier-1 AI Echo stakeholder (Cloud Run, subscriber dispatch) ---
FROM python-deps AS ai_echo
COPY agents/__init__.py ./agents/__init__.py
COPY agents/utils/ ./agents/utils/
COPY agents/ai_echo/ ./agents/ai_echo/
COPY gen_proto/ ./gen_proto/
ENV PYTHONPATH=.
CMD [".venv/bin/python", "agents/ai_echo/agent.py"]

# --- ai_echo_reactive: Reactive Tier-2 AI Echo stakeholder (Cloud Run, subscriber dispatch) ---
FROM python-deps AS ai_echo_reactive
COPY agents/__init__.py ./agents/__init__.py
COPY agents/utils/ ./agents/utils/
COPY agents/ai_echo/ ./agents/ai_echo/
COPY agents/ai_echo_autopilot/ ./agents/ai_echo_autopilot/
COPY agents/ai_echo_reactive/ ./agents/ai_echo_reactive/
COPY gen_proto/ ./gen_proto/
ENV PYTHONPATH=.
CMD [".venv/bin/python", "agents/ai_echo_reactive/agent.py"]

# --- ai_echo_autopilot: Deterministic Tier-3 AI Echo stakeholder (Cloud Run, subscriber dispatch) ---
FROM python-deps AS ai_echo_autopilot
COPY agents/__init__.py ./agents/__init__.py
COPY agents/utils/ ./agents/utils/
COPY agents/ai_echo/ ./agents/ai_echo/
COPY agents/ai_echo_autopilot/ ./agents/ai_echo_autopilot/
COPY gen_proto/ ./gen_proto/
ENV PYTHONPATH=.
CMD [".venv/bin/python", "agents/ai_echo_autopilot/agent.py"]

# --- dash: Agent telemetry dashboard (FastAPI + WebSocket) ---
FROM python-deps AS dash
COPY agents/__init__.py ./agents/__init__.py
COPY agents/utils/ ./agents/utils/
COPY gen_proto/ ./gen_proto/
COPY scripts/ ./scripts/
COPY web/agent-dash/ ./web/agent-dash/
ENV PYTHONPATH=.
CMD [".venv/bin/python", "scripts/core/agent_dash.py"]
