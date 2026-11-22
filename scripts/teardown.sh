#!/usr/bin/env bash
# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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

# scripts/teardown.sh -- interactive one-click teardown for The Rehearsal (OSS).
# Symmetric counterpart to scripts/deploy.sh. Removes everything the deploy
# created: Vertex AI Agent Engines, all Terraform-managed infra, and (with
# confirmation) the Terraform state bucket itself.
#
# Pure helpers below are sourceable via TEARDOWN_SH_NO_MAIN=1 for unit testing.

set -euo pipefail

# Curated region allowlist. MUST stay in sync with scripts/deploy.sh's
# allowed_regions() and variables.tf's variable "region" validation.
allowed_regions() {
  printf '%s\n' \
    us-central1 us-east1 us-east4 us-west1 \
    europe-west1 europe-west4 \
    asia-southeast1 asia-northeast1
}

# REST endpoint for the Vertex AI Reasoning Engines API in a given region.
# us-central1 uses the global aiplatform.googleapis.com host; every other
# region uses the regional alias. Mirrors the resolution in deploy.py.
ae_api_base() {
  local region="$1"
  if [[ "$region" == "us-central1" ]]; then
    echo "https://aiplatform.googleapis.com"
  else
    echo "https://${region}-aiplatform.googleapis.com"
  fi
}

# Lists every Agent Engine resource_name in the given project + region.
# Returns nothing on empty list (NOT a fatal condition for teardown).
# Fails cleanly (exit 1 + message to stderr) if ADC token lookup, curl,
# HTTP status (non-2xx), JSON parsing, or an API "error" payload fails.
list_agent_engines() {
  local project="${1:-}" region="${2:-}"
  if [[ -z "$project" || -z "$region" ]]; then
    echo "ERROR: list_agent_engines requires project and region." >&2
    return 1
  fi
  local base url token page_token=""
  base=$(ae_api_base "$region")
  url="${base}/v1beta1/projects/${project}/locations/${region}/reasoningEngines"
  # ADC token (NOT `gcloud auth print-access-token`): the latter returns a
  # CBA-bound token on some workstations that aiplatform.googleapis.com
  # rejects. ADC works from both Cloud Shell (auto-configured) and local
  # dev (after `gcloud auth application-default login`).
  if ! token=$(gcloud auth application-default print-access-token) || [[ -z "$token" ]]; then
    echo "ERROR: failed to acquire ADC access token; run 'gcloud auth application-default login'." >&2
    return 1
  fi

  while :; do
    local req_url="$url"
    if [[ -n "$page_token" ]]; then
      local encoded_tok
      encoded_tok=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$page_token")
      req_url="${url}?pageToken=${encoded_tok}"
    fi

    local raw http_code body
    if ! raw=$(curl -sS -w $'\n%{http_code}' -H "Authorization: Bearer $token" "$req_url"); then
      echo "ERROR: Agent Engine list request failed for ${req_url}" >&2
      return 1
    fi
    http_code="${raw##*$'\n'}"
    body="${raw%$'\n'*}"
    if [[ "$raw" == "$http_code" ]]; then
      body=""
    fi

    if [[ ! "$http_code" =~ ^[0-9]+$ ]] || (( http_code < 200 || http_code >= 300 )); then
      echo "ERROR: Agent Engine list API returned HTTP ${http_code} for ${req_url}: ${body}" >&2
      return 1
    fi

    local parsed
    if ! parsed=$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print(f"ERROR: invalid JSON from Agent Engine list API: {exc}", file=sys.stderr)
    sys.exit(1)
if not isinstance(d, dict):
    print(f"ERROR: unexpected JSON payload from Agent Engine list API: {type(d).__name__}", file=sys.stderr)
    sys.exit(1)
if "error" in d and d["error"] is not None:
    err = d["error"]
    print(f"ERROR: Agent Engine list API error: {err}", file=sys.stderr)
    sys.exit(1)
print(d.get("nextPageToken") or "")
for e in d.get("reasoningEngines") or []:
    if isinstance(e, dict) and e.get("name"):
        print(e["name"])
'); then
      return 1
    fi

    page_token=""
    local first_line=1 line
    while IFS= read -r line; do
      if (( first_line )); then
        page_token="$line"
        first_line=0
      elif [[ -n "$line" ]]; then
        printf '%s\n' "$line"
      fi
    done <<EOF
${parsed}
EOF
    [[ -n "$page_token" ]] || break
  done
}

# Deletes one Agent Engine by its resource_name (force=true so child
# deployments are dropped along with the engine).
delete_agent_engine() {
  local region="${1:-}" resource_name="${2:-}"
  if [[ -z "$region" || -z "$resource_name" ]]; then
    echo "ERROR: delete_agent_engine requires region and resource_name." >&2
    return 1
  fi
  local base token raw http_code body
  base=$(ae_api_base "$region")
  if ! token=$(gcloud auth application-default print-access-token) || [[ -z "$token" ]]; then
    echo "ERROR: failed to acquire ADC access token for deleting ${resource_name}." >&2
    return 1
  fi
  if ! raw=$(curl -sS -w $'\n%{http_code}' -X DELETE -H "Authorization: Bearer $token" \
      "${base}/v1beta1/${resource_name}?force=true"); then
    echo "ERROR: Agent Engine delete request failed for ${resource_name}" >&2
    return 1
  fi
  http_code="${raw##*$'\n'}"
  body="${raw%$'\n'*}"
  if [[ "$raw" == "$http_code" ]]; then
    body=""
  fi
  if [[ ! "$http_code" =~ ^[0-9]+$ ]] || (( http_code < 200 || http_code >= 300 )); then
    echo "ERROR: Agent Engine delete API returned HTTP ${http_code} for ${resource_name}: ${body}" >&2
    return 1
  fi
  if [[ -n "${body//[[:space:]]/}" ]]; then
    if ! printf '%s' "$body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if isinstance(d, dict) and "error" in d and d["error"] is not None:
    err = d["error"]
    print(f"ERROR: Agent Engine delete API error: {err}", file=sys.stderr)
    sys.exit(1)
'; then
      return 1
    fi
  fi
}

# Deletes Agent Engines for the given project + region. Idempotent (no-op
# when nothing is deployed). Non-fatal on individual delete failures so the
# Terraform destroy that follows still gets a chance to run.
teardown_agent_engines() {
  local project="$1" region="$2"
  local engines_out=""
  if ! engines_out=$(list_agent_engines "$project" "$region"); then
    echo "⚠️  Could not list Agent Engines in ${region} (check ADC credentials / permissions); skipping Agent Engine cleanup." >&2
    return 1
  fi
  local engines=()
  local line
  while IFS= read -r line; do
    if [[ -n "$line" ]]; then
      engines+=("$line")
    fi
  done <<EOF
${engines_out}
EOF
  if [[ ${#engines[@]} -eq 0 ]]; then
    echo "✅ No Agent Engines to remove."
    return 0
  fi
  echo "🤖 Removing ${#engines[@]} Agent Engine(s)..."
  for engine in "${engines[@]}"; do
    [[ -n "$engine" ]] || continue
    echo "   - $engine"
    delete_agent_engine "$region" "$engine" || \
      echo "     ⚠️  delete failed (continuing): $engine"
  done
}

# Test harness opt-out: don't run interactive flow when sourced.
[[ "${TEARDOWN_SH_NO_MAIN:-0}" == "1" ]] && return 0

# Refuse to run non-interactively (select prompts misbehave on piped stdin).
if [[ ! -t 0 ]]; then
  echo "ERROR: teardown.sh requires an interactive TTY (Cloud Shell or local terminal)." >&2
  exit 1
fi

# --- Phase 1: project selection ------------------------------------------
PROJECT_ID=""
OPT=""
echo "📁 Project Selection:"
PS3="Choose: "
select OPT in "Enter project ID" "Pick from list (gcloud projects list)"; do
  case "$OPT" in
    "Enter project ID")
      read -r -p "Project ID: " PROJECT_ID || { echo "Cancelled."; exit 1; }
      break
      ;;
    "Pick from list (gcloud projects list)")
      PROJS=()
      projs_out=""
      if projs_out=$(gcloud projects list --limit=20 --format="value(projectId)" 2>/dev/null); then
        while IFS= read -r line; do
          if [[ -n "$line" ]]; then
            PROJS+=("$line")
          fi
        done <<EOF
${projs_out}
EOF
      fi
      if [[ ${#PROJS[@]} -eq 0 ]]; then
        echo "No projects accessible to current gcloud auth. Falling back to manual entry."
        read -r -p "Project ID: " PROJECT_ID || { echo "Cancelled."; exit 1; }
      else
        select PROJECT_ID in "${PROJS[@]}"; do
          [[ -n "${PROJECT_ID:-}" ]] && break
        done
      fi
      break
      ;;
    *) echo "Invalid choice." ;;
  esac
done
[[ -n "$PROJECT_ID" ]] || { echo "ERROR: project ID is required" >&2; exit 1; }

# --- Phase 2: region selection -------------------------------------------
echo ""
echo "🌐 Region (must match what you deployed to):"
REGIONS=()
regions_out=$(allowed_regions)
while IFS= read -r line; do
  if [[ -n "$line" ]]; then
    REGIONS+=("$line")
  fi
done <<EOF
${regions_out}
EOF
[[ ${#REGIONS[@]} -gt 0 ]] || { echo "ERROR: no allowed regions configured" >&2; exit 1; }
REGION=""
PS3="Choose: "
select REGION in "${REGIONS[@]}"; do
  [[ -n "${REGION:-}" ]] && break
done
[[ -n "$REGION" ]] || { echo "ERROR: region is required" >&2; exit 1; }

# --- Phase 3: confirmation -----------------------------------------------
STATE_BUCKET="${PROJECT_ID}-tf-state"
cat <<EOF

====================================================
🧨 About to PERMANENTLY DELETE The Rehearsal from:
   Project: $PROJECT_ID
   Region:  $REGION

This removes:
  • All Vertex AI Agent Engines (planner, simulator, etc.)
  • All Terraform-managed infra (Cloud Run, Memorystore,
    Cloud SQL, Pub/Sub, IAM, networking, secrets)
  • Optionally, the Terraform state bucket
    gs://${STATE_BUCKET}

This does NOT remove:
  • The GCP project itself (delete it from the Console
    if you want a totally clean account)
  • Artifact Registry images (kept so a redeploy is
    fast; ~pennies per month at this scale)
  • Cloud Build history and logs

Resources cannot be recovered after destroy completes.
====================================================
EOF
REPLY=""
read -r -p "Type 'destroy' to proceed: " REPLY || { echo "Cancelled."; exit 1; }
[[ "$REPLY" == "destroy" ]] || { echo "Cancelled."; exit 1; }

# --- Phase 4: Agent Engines ----------------------------------------------
echo ""
teardown_agent_engines "$PROJECT_ID" "$REGION" || \
  echo "⚠️  Agent Engine cleanup did not complete cleanly (see error above); continuing to Terraform destroy." >&2

# --- Phase 5: Terraform destroy ------------------------------------------
echo ""
echo "🏗️  Initialising Terraform against gs://${STATE_BUCKET}/oss/state ..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}/infra"
terraform init -input=false -reconfigure \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="prefix=oss/state" >/dev/null

# Cloud Run direct VPC egress allocates SERVERLESS-purpose addresses that
# GCP releases asynchronously after the consuming service is deleted.
# Until they release, the VPC subnet + service-networking peering can't be
# torn down. Retry a few times with a wait between attempts; if it still
# fails after that, surface a clear message that re-running later will
# pick up where this left off (terraform state has the remaining handful
# of resources already; nothing else needs to be done).
echo "💥 Running terraform destroy (≈3-5 min) ..."
# Ensure Cloud SQL deletion_protection is lifted in GCP before destroy runs.
SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-$(terraform output -raw database_instance_name 2>/dev/null || true)}"
if [[ -z "${SQL_INSTANCE}" ]]; then
  SQL_INSTANCE="$(gcloud sql instances list --project="${PROJECT_ID}" --format='value(name)' --filter='name~postgres' --limit=1 2>/dev/null || true)"
fi
if [[ -n "${SQL_INSTANCE}" ]]; then
  gcloud sql instances patch "${SQL_INSTANCE}" \
    --no-deletion-protection \
    --project="${PROJECT_ID}" \
    --quiet >/dev/null 2>&1 || true
fi

attempt=1
max_attempts=4
while (( attempt <= max_attempts )); do
  if terraform destroy -auto-approve -input=false \
       -var "project_id=${PROJECT_ID}" \
       -var "region=${REGION}" \
       -var "database_deletion_protection=false"; then
    echo "✅ Terraform destroy complete."
    break
  fi
  if (( attempt == max_attempts )); then
    cat <<MSG

⚠️  Terraform destroy could not finish on attempt ${attempt}.

This is almost always GCP's Service Networking API holding a SERVERLESS-purpose IP
allocation (Cloud Run direct VPC egress) that takes anywhere from a few minutes
to a few hours to release after the consuming services are deleted. The Agent
Engines and most infrastructure ARE already gone; only a few networking
resources remain.

What to do: wait an hour or so, then re-run ./scripts/teardown.sh. The script
is idempotent and picks up where it left off — terraform state already knows
about the remaining resources, no other manual cleanup is needed.

If you want to check what's blocking, look for SERVERLESS-purpose addresses:
  gcloud compute addresses list --project=${PROJECT_ID} --filter="purpose:SERVERLESS"
MSG
    exit 1
  fi
  echo ""
  echo "⏳ Destroy attempt ${attempt} blocked (likely a Cloud Run direct VPC egress IP"
  echo "   still releasing in the background). Waiting 60s and retrying..."
  sleep 60
  attempt=$(( attempt + 1 ))
done
cd "${REPO_ROOT}"

# --- Phase 6: optional state bucket removal ------------------------------
echo ""
REPLY=""
read -p "Also delete the Terraform state bucket gs://${STATE_BUCKET}? (y/N) " -n 1 -r REPLY || true
echo
if [[ "${REPLY}" =~ ^[Yy]$ ]]; then
  echo "🗑️  Deleting state bucket ..."
  gcloud storage rm --recursive "gs://${STATE_BUCKET}" --project="$PROJECT_ID" --quiet || \
    echo "⚠️  Bucket delete failed (it may not exist or you may lack permission)."
else
  echo "ℹ️  State bucket kept. Re-running deploy.sh will reuse it."
fi

echo ""
echo "===================================================="
echo "🎉 The Rehearsal removed from $PROJECT_ID."
echo "===================================================="
