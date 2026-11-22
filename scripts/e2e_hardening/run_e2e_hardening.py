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

"""CLI entrypoint for the opaque-box 4-tier E2E hardening and compliance suite."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.e2e_hardening.compliance_scanners import (
    check_ollama_11434_listening,
    check_protected_files_untouched,
    scan_book_author_refs,
    scan_r4_license_headers,
)
from scripts.e2e_hardening.harness import REPO_ROOT, run_cmd

SUITE_DIR = Path(__file__).resolve().parent


def _select_test_files(tier: str, self_check: bool) -> list[Path]:
    if self_check:
        return [SUITE_DIR / "test_harness_self_check.py"]
    if tier in {"1", "2", "3", "4"}:
        files = sorted(SUITE_DIR.glob(f"test_tier{tier}_*.py"))
        if not files:
            raise FileNotFoundError(
                f"No test modules matching test_tier{tier}_*.py found in {SUITE_DIR}"
            )
        return files
    if tier == "all":
        files: list[Path] = []
        self_test = SUITE_DIR / "test_harness_self_check.py"
        if self_test.is_file():
            files.append(self_test)
        for t in ("1", "2", "3", "4"):
            files.extend(sorted(SUITE_DIR.glob(f"test_tier{t}_*.py")))
        if not files:
            raise FileNotFoundError(f"No test modules found in {SUITE_DIR}")
        return files
    raise ValueError(f"Unsupported tier: {tier}")


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the opaque-box E2E hardening and compliance verification suite."
    )
    parser.add_argument(
        "--tier",
        choices=("1", "2", "3", "4", "all"),
        default="all",
        help="Select test tier to run (default: all).",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run harness/scanner self-check meta-tests, ruff check, and R4/book checks on scripts/e2e_hardening/.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip heavy tier4_slow checks (-m 'not tier4_slow').",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        default=None,
        help="Optional path to write a structured JSON summary report.",
    )
    parser.add_argument(
        "--junitxml",
        type=Path,
        default=None,
        help="Optional path for pytest JUnit XML output.",
    )
    args = parser.parse_args(argv)

    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    overall_rc = 0

    try:
        selected_files = _select_test_files(args.tier, args.self_check)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.json_report:
            _write_report(
                args.json_report,
                {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "mode": "self-check" if args.self_check else f"tier-{args.tier}",
                    "exit_code": 2,
                    "error": str(exc),
                    "checks": [],
                },
            )
        return 2

    if args.self_check:
        ruff_res = run_cmd(["uv", "run", "ruff", "check", "scripts/e2e_hardening/"], cwd=REPO_ROOT, timeout=120)
        print(ruff_res.stdout, end="")
        if ruff_res.stderr:
            print(ruff_res.stderr, end="", file=sys.stderr)
        checks.append({"name": "ruff_e2e_hardening", "exit_code": ruff_res.returncode})
        if ruff_res.returncode != 0:
            overall_rc = ruff_res.returncode

        r4_hits = scan_r4_license_headers(SUITE_DIR)
        book_hits = [v for v in scan_book_author_refs(SUITE_DIR)]
        protected_hits = check_protected_files_untouched(REPO_ROOT)
        checks.append({"name": "r4_license_e2e_hardening", "violations": len(r4_hits)})
        checks.append({"name": "book_refs_e2e_hardening", "violations": len(book_hits)})
        checks.append({"name": "protected_files_untouched", "violations": len(protected_hits)})
        if r4_hits or book_hits or protected_hits:
            if r4_hits:
                print(f"R4 license violations in {SUITE_DIR}: {r4_hits}", file=sys.stderr)
            if book_hits:
                print(f"Book author violations in {SUITE_DIR}: {book_hits}", file=sys.stderr)
            if protected_hits:
                print(f"Protected files touched: {protected_hits}", file=sys.stderr)
            overall_rc = 1

    pytest_cmd: list[str] = ["uv", "run", "pytest", "-v", *[str(p) for p in selected_files]]
    if args.fast:
        pytest_cmd.extend(["-m", "not tier4_slow"])
    if args.junitxml is not None:
        pytest_cmd.append(f"--junitxml={args.junitxml}")

    pytest_res = run_cmd(pytest_cmd, cwd=REPO_ROOT, timeout=1800)
    print(pytest_res.stdout, end="")
    if pytest_res.stderr:
        print(pytest_res.stderr, end="", file=sys.stderr)
    checks.append(
        {
            "name": "pytest",
            "files": [p.name for p in selected_files],
            "exit_code": pytest_res.returncode,
        }
    )
    if pytest_res.returncode != 0:
        overall_rc = pytest_res.returncode

    duration_s = round(time.monotonic() - started, 3)
    ollama_up = check_ollama_11434_listening()
    protected_after = check_protected_files_untouched(REPO_ROOT)
    if protected_after:
        print(f"ERROR: Protected files modified during run: {protected_after}", file=sys.stderr)
        overall_rc = 1

    summary = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "self-check" if args.self_check else f"tier-{args.tier}",
        "fast": args.fast,
        "duration_s": duration_s,
        "exit_code": overall_rc,
        "ollama_11434_listening": ollama_up,
        "protected_files_touched": protected_after,
        "checks": checks,
    }
    if args.json_report is not None:
        _write_report(args.json_report, summary)

    print(
        f"\n[e2e_hardening] mode={summary['mode']} exit_code={overall_rc} "
        f"duration={duration_s}s ollama_11434={ollama_up} protected_touched={len(protected_after)}"
    )
    return overall_rc


if __name__ == "__main__":
    raise SystemExit(main())
