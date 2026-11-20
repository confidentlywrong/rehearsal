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

"""Tier 4 repo-wide acceptance gates across Python, Go, UI, Obsidian, SSOT, R4, and safety."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from scripts.e2e_hardening.compliance_scanners import (
    check_ollama_11434_listening,
    check_protected_files_untouched,
    scan_book_author_refs,
    scan_obsidian_extended,
    scan_r4_license_headers,
    scan_ssot_zero_fake_data,
)
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    get_git_status_snapshot,
    run_cmd,
)

_RAW_OBSIDIAN_CLI = os.environ.get("OBSIDIAN_CLI_PATH", "").strip()
OBSIDIAN_CLI_PATH = Path(_RAW_OBSIDIAN_CLI) if _RAW_OBSIDIAN_CLI else None

UI_TARGETS: list[Path] = [
    REPO_ROOT / "web/frontend/src",
    REPO_ROOT / "web/admin-dash",
    REPO_ROOT / "web/tester/src",
    REPO_ROOT / "web/agent-dash",
]


def _resolve_golangci_lint() -> str:
    found = shutil.which("golangci-lint")
    if found:
        return found
    gopath_res = run_cmd(["go", "env", "GOPATH"], cwd=REPO_ROOT, timeout=30)
    assert gopath_res.returncode == 0, f"go env GOPATH failed: {gopath_res.stderr}"
    gopath = gopath_res.stdout.strip()
    candidate = Path(gopath) / "bin" / "golangci-lint"
    assert candidate.is_file(), f"golangci-lint binary not found on PATH or at {candidate}"
    return str(candidate)


@pytest.mark.tier4
@pytest.mark.tier4_slow
def test_python_unit_tests() -> None:
    res = run_cmd(
        ["uv", "run", "pytest", "agents/", "-x", "-q", "-m", "not slow and not integration"],
        cwd=REPO_ROOT,
        timeout=600,
    )
    assert res.returncode == 0, f"Python unit tests failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
def test_python_scripts_tests() -> None:
    res = run_cmd(["uv", "run", "pytest", "scripts/tests/", "-q"], cwd=REPO_ROOT, timeout=300)
    assert res.returncode == 0, f"scripts/tests failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
def test_python_ruff() -> None:
    res = run_cmd(
        ["uv", "run", "ruff", "check", "agents/", "scripts/e2e_hardening/"],
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert res.returncode == 0, f"ruff check failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
@pytest.mark.tier4_slow
def test_python_pyright() -> None:
    res = run_cmd(["npx", "--yes", "pyright@1.1.365", "agents/"], cwd=REPO_ROOT, timeout=300)
    assert res.returncode == 0, f"pyright failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
@pytest.mark.requires_go
def test_go_vet() -> None:
    res = run_cmd(["go", "vet", "./..."], cwd=REPO_ROOT, timeout=300)
    assert res.returncode == 0, f"go vet failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
@pytest.mark.tier4_slow
@pytest.mark.requires_go
def test_go_test_race() -> None:
    res = run_cmd(["go", "test", "-race", "-short", "-count=1", "./..."], cwd=REPO_ROOT, timeout=600)
    assert res.returncode == 0, f"go test -race failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
@pytest.mark.requires_go
def test_go_golangci_lint() -> None:
    lint_bin = _resolve_golangci_lint()
    res = run_cmd([lint_bin, "run", "./..."], cwd=REPO_ROOT, timeout=300)
    assert res.returncode == 0, f"golangci-lint failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
@pytest.mark.tier4_slow
@pytest.mark.requires_node
def test_ui_unit_tests() -> None:
    for app_rel in ("web/frontend", "web/admin-dash", "web/tester"):
        app_dir = REPO_ROOT / app_rel
        res = run_cmd(["./node_modules/.bin/vitest", "run"], cwd=app_dir, timeout=300)
        assert res.returncode == 0, f"{app_rel} vitest failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
@pytest.mark.tier4_slow
@pytest.mark.requires_node
def test_ui_typecheck() -> None:
    fe_dir = REPO_ROOT / "web/frontend"
    res_app = run_cmd(
        ["./node_modules/.bin/tsc", "--noEmit", "-p", "tsconfig.app.json"],
        cwd=fe_dir,
        timeout=300,
    )
    assert res_app.returncode == 0, f"web/frontend tsconfig.app.json failed:\n{res_app.stdout}\n{res_app.stderr}"

    res_spec = run_cmd(
        ["./node_modules/.bin/tsc", "--noEmit", "-p", "tsconfig.spec.json"],
        cwd=fe_dir,
        timeout=300,
    )
    assert res_spec.returncode == 0, f"web/frontend tsconfig.spec.json failed:\n{res_spec.stdout}\n{res_spec.stderr}"

    tester_dir = REPO_ROOT / "web/tester"
    res_tester = run_cmd(["./node_modules/.bin/tsc", "--noEmit"], cwd=tester_dir, timeout=300)
    assert res_tester.returncode == 0, f"web/tester tsc failed:\n{res_tester.stdout}\n{res_tester.stderr}"

    admin_dir = REPO_ROOT / "web/admin-dash"
    for js_file in sorted(admin_dir.glob("*.js")):
        if js_file.name.endswith(".test.js") or js_file.name == "vitest.config.js":
            continue
        chk = run_cmd(["node", "--check", str(js_file)], cwd=admin_dir, timeout=60)
        assert chk.returncode == 0, f"node --check failed on {js_file}:\n{chk.stdout}\n{chk.stderr}"
    admin_tsc = admin_dir / "node_modules/.bin/tsc"
    if admin_tsc.exists():
        res_admin_tsc = run_cmd([str(admin_tsc), "--noEmit"], cwd=admin_dir, timeout=300)
        assert res_admin_tsc.returncode == 0, (
            f"web/admin-dash tsc failed:\n{res_admin_tsc.stdout}\n{res_admin_tsc.stderr}"
        )


@pytest.mark.tier4
@pytest.mark.tier4_slow
@pytest.mark.requires_node
def test_ui_builds_non_destructive(tmp_path: Path) -> None:
    ng_out = tmp_path / "frontend_dist"
    res_ng = run_cmd(
        ["./node_modules/.bin/ng", "build", "--output-path", str(ng_out), "--delete-output-path"],
        cwd=REPO_ROOT / "web/frontend",
        timeout=600,
    )
    assert res_ng.returncode == 0, f"ng build failed:\n{res_ng.stdout}\n{res_ng.stderr}"
    assert ng_out.exists()

    admin_out = tmp_path / "admin_dist"
    res_admin = run_cmd(
        ["./node_modules/.bin/vite", "build", "--outDir", str(admin_out), "--emptyOutDir"],
        cwd=REPO_ROOT / "web/admin-dash",
        timeout=300,
    )
    assert res_admin.returncode == 0, f"admin-dash vite build failed:\n{res_admin.stdout}\n{res_admin.stderr}"
    assert (admin_out / "index.html").is_file()

    tester_out = tmp_path / "tester_dist"
    res_tester = run_cmd(
        ["./node_modules/.bin/vite", "build", "--outDir", str(tester_out), "--emptyOutDir"],
        cwd=REPO_ROOT / "web/tester",
        timeout=300,
    )
    assert res_tester.returncode == 0, f"tester vite build failed:\n{res_tester.stdout}\n{res_tester.stderr}"
    assert (tester_out / "index.html").is_file()


@pytest.mark.tier4
def test_obsidian_cli_strict() -> None:
    if OBSIDIAN_CLI_PATH is None or not OBSIDIAN_CLI_PATH.is_file():
        pytest.skip("OBSIDIAN_CLI_PATH is not configured in this environment")
    for target in UI_TARGETS:
        res = run_cmd(
            [sys.executable, str(OBSIDIAN_CLI_PATH), "audit", "--target", str(target), "--limit", "200", "--strict"],
            cwd=REPO_ROOT,
            timeout=120,
        )
        assert res.returncode == 0, (
            f"obsidian_cli.py --strict failed on {target}:\n{res.stdout}\n{res.stderr}"
        )


@pytest.mark.tier4
def test_obsidian_extended_zero_violations() -> None:
    violations = scan_obsidian_extended(UI_TARGETS)
    formatted = "\n".join(f"{v.file}:{v.line} [{v.rule}] {v.match} — {v.message}" for v in violations[:50])
    assert violations == [], f"Found {len(violations)} extended Obsidian violations:\n{formatted}"


@pytest.mark.tier4
def test_ssot_zero_fake_data() -> None:
    violations = scan_ssot_zero_fake_data()
    formatted = "\n".join(f"{v.file}:{v.line} [{v.rule}] {v.match} — {v.message}" for v in violations[:50])
    assert violations == [], f"Found {len(violations)} SSOT/Zero-Fake-Data violations:\n{formatted}"


@pytest.mark.tier4
def test_make_check_license() -> None:
    res = run_cmd(["make", "check-license"], cwd=REPO_ROOT, timeout=300)
    assert res.returncode == 0, f"make check-license failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
def test_r4_license_headers_repo_and_e2e_hardening() -> None:
    repo_violations = scan_r4_license_headers(REPO_ROOT)
    assert repo_violations == [], f"Repo R4 license violations: {repo_violations[:20]}"

    e2e_violations = scan_r4_license_headers(REPO_ROOT / "scripts" / "e2e_hardening")
    assert e2e_violations == [], f"scripts/e2e_hardening R4 license violations: {e2e_violations}"


@pytest.mark.tier4
def test_book_author_refs() -> None:
    violations = scan_book_author_refs(REPO_ROOT)
    assert violations == [], f"Book author attribution violations: {violations[:20]}"


@pytest.mark.tier4
def test_make_lint_configs() -> None:
    res = run_cmd(["make", "lint-configs"], cwd=REPO_ROOT, timeout=300)
    assert res.returncode == 0, f"make lint-configs failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.tier4
def test_workspace_preservation_and_host_ollama_safety() -> None:
    snapshot = get_git_status_snapshot(REPO_ROOT)
    assert len(snapshot) >= 385, f"Expected >= 385 baseline entries, found {len(snapshot)}"
    assert check_protected_files_untouched(REPO_ROOT) == []
    assert check_ollama_11434_listening() is True
