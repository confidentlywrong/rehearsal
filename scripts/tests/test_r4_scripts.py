# Copyright 2026 Christian Haeussinger
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

"""Round-4 regression tests for shell deployment, teardown, and ops scripts."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from scripts.ops import discover_maps_tools, generate_local_seeds

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _setup_mock_bin(tmp_path: Path, *, gcloud_script: str, curl_script: str) -> dict[str, str]:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir(parents=True, exist_ok=True)
    _write_exe(mock_bin / "gcloud", gcloud_script)
    _write_exe(mock_bin / "curl", curl_script)
    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
    return env


def _run_teardown_fn(bash_exe: str, env: dict[str, str], snippet: str) -> subprocess.CompletedProcess[str]:
    script = f'set -euo pipefail; TEARDOWN_SH_NO_MAIN=1 source "{_REPO_ROOT / "scripts" / "teardown.sh"}"; {snippet}'
    return subprocess.run(
        [bash_exe, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestBash32Compatibility:
    @pytest.mark.parametrize("rel_path", ["scripts/deploy.sh", "scripts/teardown.sh"])
    def test_no_bash4_mapfile_or_readarray(self, rel_path: str) -> None:
        text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert "mapfile" not in text, f"{rel_path} must not use Bash 4+ mapfile"
        assert "readarray" not in text, f"{rel_path} must not use Bash 4+ readarray"

    def test_deploy_test_passes_under_bin_bash(self) -> None:
        bash_exe = "/bin/bash" if Path("/bin/bash").exists() else "bash"
        res = subprocess.run(
            [bash_exe, str(_REPO_ROOT / "scripts" / "deploy_test.sh")],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"

    def test_shell_scripts_syntax_valid_under_bin_bash(self) -> None:
        bash_exe = "/bin/bash" if Path("/bin/bash").exists() else "bash"
        for rel in (
            "scripts/deploy.sh",
            "scripts/teardown.sh",
            "scripts/deploy_test.sh",
            "scripts/core/generate_proto.sh",
        ):
            res = subprocess.run(
                [bash_exe, "-n", str(_REPO_ROOT / rel)],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0, f"{rel} failed bash -n:\n{res.stderr}"


class TestTeardownAgentEnginesErrorHandling:
    @pytest.mark.parametrize("status_code", [401, 403, 404])
    def test_http_json_error_does_not_claim_zero_engines(self, tmp_path: Path, status_code: int) -> None:
        bash_exe = "/bin/bash" if Path("/bin/bash").exists() else "bash"
        payload = json.dumps(
            {
                "error": {
                    "code": status_code,
                    "message": "Request had insufficient authentication scopes.",
                    "status": "PERMISSION_DENIED",
                }
            }
        )
        env = _setup_mock_bin(
            tmp_path,
            gcloud_script="#!/bin/sh\necho fake-adc-token\n",
            curl_script=f"#!/bin/sh\nprintf '%s\\n{status_code}' '{payload}'\n",
        )

        res = _run_teardown_fn(bash_exe, env, 'teardown_agent_engines "proj-1" "us-central1"')
        assert res.returncode != 0
        assert "No Agent Engines to remove" not in res.stdout
        assert f"HTTP {status_code}" in res.stderr

    def test_http_200_json_error_key_fails_loudly(self, tmp_path: Path) -> None:
        bash_exe = "/bin/bash" if Path("/bin/bash").exists() else "bash"
        env = _setup_mock_bin(
            tmp_path,
            gcloud_script="#!/bin/sh\necho fake-adc-token\n",
            curl_script='#!/bin/sh\nprintf \'{"error":{"message":"permission denied"}}\\n200\'\n',
        )

        res = _run_teardown_fn(bash_exe, env, 'teardown_agent_engines "proj-1" "us-central1"')
        assert res.returncode != 0
        assert "No Agent Engines to remove" not in res.stdout
        assert "permission denied" in res.stderr

    def test_adc_failure_does_not_claim_zero_engines(self, tmp_path: Path) -> None:
        bash_exe = "/bin/bash" if Path("/bin/bash").exists() else "bash"
        env = _setup_mock_bin(
            tmp_path,
            gcloud_script="#!/bin/sh\necho 'Reauthentication required' >&2\nexit 1\n",
            curl_script="#!/bin/sh\necho 'unexpected curl call' >&2\nexit 99\n",
        )

        res = _run_teardown_fn(bash_exe, env, 'teardown_agent_engines "proj-1" "us-central1"')
        assert res.returncode != 0
        assert "No Agent Engines to remove" not in res.stdout
        assert "ADC access token" in res.stderr

    def test_empty_200_reports_no_engines_and_succeeds(self, tmp_path: Path) -> None:
        bash_exe = "/bin/bash" if Path("/bin/bash").exists() else "bash"
        env = _setup_mock_bin(
            tmp_path,
            gcloud_script="#!/bin/sh\necho fake-adc-token\n",
            curl_script="#!/bin/sh\nprintf '{}\\n200'\n",
        )

        res = _run_teardown_fn(bash_exe, env, 'teardown_agent_engines "proj-1" "us-central1"')
        assert res.returncode == 0, res.stderr
        assert "No Agent Engines to remove." in res.stdout

    def test_paginated_list_and_delete_handles_individual_delete_failure(self, tmp_path: Path) -> None:
        bash_exe = "/bin/bash" if Path("/bin/bash").exists() else "bash"
        calls_log = tmp_path / "curl_calls.log"
        curl_script = f"""#!/usr/bin/env python3
import json, sys

args = sys.argv[1:]
method = "GET"
if "-X" in args:
    method = args[args.index("-X") + 1]
url = args[-1]
with open({str(calls_log)!r}, "a", encoding="utf-8") as fh:
    fh.write(f"{{method}} {{url}}\\n")

if method == "GET":
    if "pageToken=p2" in url:
        print(json.dumps({{"reasoningEngines": [{{"name": "projects/p/locations/us-central1/reasoningEngines/e2"}}]}}))
    else:
        print(json.dumps({{"reasoningEngines": [{{"name": "projects/p/locations/us-central1/reasoningEngines/e1"}}], "nextPageToken": "p2"}}))
    print("200", end="")
elif method == "DELETE":
    if "/e1?" in url:
        print(json.dumps({{"error": {{"code": 403, "message": "forbidden"}}}}))
        print("403", end="")
    else:
        print("{{}}")
        print("200", end="")
"""
        env = _setup_mock_bin(
            tmp_path,
            gcloud_script="#!/bin/sh\necho fake-adc-token\n",
            curl_script=curl_script,
        )

        res = _run_teardown_fn(bash_exe, env, 'teardown_agent_engines "proj-1" "us-central1"')
        assert res.returncode == 0, res.stderr
        assert "Removing 2 Agent Engine(s)..." in res.stdout
        assert "delete failed (continuing)" in res.stdout
        logged = calls_log.read_text(encoding="utf-8")
        assert "GET " in logged and "pageToken=p2" in logged
        assert "DELETE " in logged and "/e1?force=true" in logged and "/e2?force=true" in logged


class TestOpsScriptsHardening:
    def test_discover_maps_tools_handles_missing_heading_and_section_bounds(self) -> None:
        assert discover_maps_tools.extract_documented_tools("# Title\nNo section here `not_a_tool`.") == set()
        md = (
            "# Header `ignored_before`\n\n"
            "## Available Tools\n\n"
            "- `maps_search_places`: search\n"
            "- `maps_compute_routes`: route\n\n"
            "## Other Section\n\n"
            "- `ignored_after`\n"
        )
        assert discover_maps_tools.extract_documented_tools(md) == {
            "maps_search_places",
            "maps_compute_routes",
        }

    def test_generate_local_seeds_rejects_non_finite_scores_and_vectors(self) -> None:
        assert generate_local_seeds._sql_float_or_null(None) == "NULL"
        assert generate_local_seeds._sql_float_or_null(True) == "NULL"
        assert generate_local_seeds._sql_float_or_null(float("nan")) == "NULL"
        assert generate_local_seeds._sql_float_or_null(float("inf")) == "NULL"
        assert generate_local_seeds._sql_float_or_null("not-a-number") == "NULL"
        assert generate_local_seeds._sql_float_or_null(0.875) == "0.875"

        assert generate_local_seeds._format_vector([0.1, -2.5]) == "'[0.1,-2.5]'::vector"
        with pytest.raises(ValueError):
            generate_local_seeds._format_vector([0.1, float("nan")])
        with pytest.raises(ValueError):
            generate_local_seeds._format_vector([float("inf")])
