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

"""Meta-tests verifying harness.py and compliance_scanners.py (true positives & false-positive guards)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
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
    EphemeralGoServer,
    IsolatedPostgresSchema,
    IsolatedRedisNamespace,
    MockOllamaServer,
    PostgresUnavailableError,
    RedisUnavailableError,
    assert_workspace_preserved,
    free_port,
    get_git_status_snapshot,
    run_cmd,
)

_BANNED_NAME = "Gil" + "martin"
_BOOK_TITLE = "Confidently Wrong"
_AUTHOR_SURNAME = "Christian " + "Haeussinger"

_VALID_R4_NEW_HEADER = (
    "# Copyright 2026 Christian Haeussinger\n"
    "#\n"
    '# Licensed under the Apache License, Version 2.0 (the "License");\n'
    "# you may not use this file except in compliance with the License.\n"
    "# You may obtain a copy of the License at\n"
    "#\n"
    "#     http://www.apache.org/licenses/LICENSE-2.0\n"
    "#\n"
    "# Unless required by applicable law or agreed to in writing, software\n"
    "# distributed under the License is distributed on an \"AS IS\" BASIS,\n"
    "# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n"
    "# See the License for the specific language governing permissions and\n"
    "# limitations under the License.\n"
)


class TestObsidianExtendedScanner:
    """Verify positive detection and false-positive guards for scan_obsidian_extended."""

    def test_detects_all_nine_blind_spot_categories(self, tmp_path: Path) -> None:
        ui_dir = tmp_path / "ui"
        ui_dir.mkdir()

        (ui_dir / "theme.scss").write_text(
            ".dark-card { background: #1a1a2e; border-radius: 999px; }\n"
            ".grid-2 { display: grid; }\n",
            encoding="utf-8",
        )
        (ui_dir / "panel.ts").write_text(
            "export const tpl = `\n"
            '  <div class="bg-slate-900/50 status-badge" role="tablist">\n'
            '    <span title="Hover tip">SHA-256</span>\n'
            "  </div>`;\n"
            "export function act() { if (window.confirm('Sure?')) { return 1; } return 0; }\n"
            "const toastMessage = 'saved';\n",
            encoding="utf-8",
        )
        (ui_dir / "legacy.html").write_text(
            '<div class="docs__tabs" style="color: #00ffcc; border-radius: 100px;"></div>\n',
            encoding="utf-8",
        )

        violations = scan_obsidian_extended([ui_dir])
        rules_hit = {v.rule for v in violations}
        expected_rules = {
            "obsidian_prohibited_tailwind",
            "obsidian_non_canonical_hex",
            "obsidian_pill_radius_or_class",
            "obsidian_native_dialog",
            "obsidian_native_title_in_ts_js",
            "obsidian_horizontal_tabs",
            "obsidian_grid_two_columns",
            "obsidian_floating_toast",
            "obsidian_crypto_plumbing_in_ui",
        }
        assert expected_rules.issubset(rules_hit), f"Missing rules: {expected_rules - rules_hit}"

    def test_ignores_comments_tests_viewport_and_compliant_css(self, tmp_path: Path) -> None:
        ui_dir = tmp_path / "web" / "frontend" / "src"
        viewport_dir = ui_dir / "app" / "viewport" / "shaders"
        viewport_dir.mkdir(parents=True)

        (viewport_dir / "shader.ts").write_text(
            "const neon = '#00ffcc';\n",
            encoding="utf-8",
        )
        (ui_dir / "sample.spec.ts").write_text(
            "const fake = '#1a1a2e'; window.confirm('x');\n",
            encoding="utf-8",
        )
        (ui_dir / "clean.ts").write_text(
            "// https://example.com and window.confirm('ignored')\n"
            "/* block comment alert('ignored') #00ffcc */\n"
            "const tpl = `<!-- We do NOT use window.confirm() -->\n"
            '  <div class="status-label-gutter">&#039;</div>`;\n',
            encoding="utf-8",
        )
        (ui_dir / "clean.scss").write_text(
            ".halo { filter: blur(100px); background: #FFFFFF; color: #1D1D1F; }\n"
            "::-webkit-scrollbar-thumb { border-radius: 9999px; }\n"
            ".sphere-core { background: radial-gradient(#141416, #050506); }\n",
            encoding="utf-8",
        )

        violations = scan_obsidian_extended([ui_dir])
        assert violations == [], f"Unexpected false positives: {violations}"

    def test_repo_false_positive_regression_guards(self) -> None:
        profile_ts = REPO_ROOT / "web/frontend/src/app/settings/profile/profile.page.ts"
        detail_ts = REPO_ROOT / "web/frontend/src/app/memory/pages/analysis-detail.page.ts"
        organizer_scss = (
            REPO_ROOT / "web/frontend/src/app/components/ChatNavPanel/screens/Organizer/Organizer.scss"
        )
        sphere_shader = REPO_ROOT / "web/frontend/src/app/viewport/shaders/sphere-shader.ts"
        raw_obsidian_css = os.environ.get("OBSIDIAN_CSS_PATH", "").strip()
        obsidian_css = Path(raw_obsidian_css) if raw_obsidian_css else None

        profile_hits = [
            v for v in scan_obsidian_extended([profile_ts]) if v.rule == "obsidian_native_dialog"
        ]
        assert profile_hits == []

        detail_hits = [
            v
            for v in scan_obsidian_extended([detail_ts])
            if v.rule == "obsidian_native_dialog" and v.line <= 30
        ]
        assert detail_hits == []

        org_pill_hits = [
            v
            for v in scan_obsidian_extended([organizer_scss])
            if v.rule == "obsidian_pill_radius_or_class"
        ]
        assert org_pill_hits == []

        assert scan_obsidian_extended([sphere_shader]) == []

        if obsidian_css is not None and obsidian_css.exists():
            css_hits = [
                v
                for v in scan_obsidian_extended([obsidian_css])
                if v.rule in {"obsidian_pill_radius_or_class", "obsidian_non_canonical_hex"}
            ]
            assert css_hits == [], f"Unexpected hits on obsidian_liquid_glass.css: {css_hits}"

    def test_multiline_title_and_ts_method_definition_edge_cases(self, tmp_path: Path) -> None:
        ui_dir = tmp_path / "edge"
        ui_dir.mkdir()
        (ui_dir / "sheet.ts").write_text(
            "export class Sheet {\n"
            "  confirm(): void {}\n"
            "  confirmWithArg(id: string): Promise<void> { return Promise.resolve(); }\n"
            "  run(x: string) { if (confirm(x)) { return true; } return false; }\n"
            "}\n"
            "export const html = `\n"
            "  <button\n"
            '    type="button"\n'
            '    title="Multiline Delete">\n'
            "    Delete\n"
            "  </button>`;\n",
            encoding="utf-8",
        )
        violations = scan_obsidian_extended([ui_dir])
        dialog_hits = [v for v in violations if v.rule == "obsidian_native_dialog"]
        title_hits = [v for v in violations if v.rule == "obsidian_native_title_in_ts_js"]
        assert len(dialog_hits) == 1, f"Expected only confirm(x), got: {dialog_hits}"
        assert dialog_hits[0].line == 4
        assert len(title_hits) == 1, f"Expected multiline title= hit, got: {title_hits}"
        assert title_hits[0].line == 7

    def test_ts_method_declarations_with_callbacks_and_object_return_types(self, tmp_path: Path) -> None:
        ui_dir = tmp_path / "ts_sigs"
        ui_dir.mkdir()
        (ui_dir / "dialog.ts").write_text(
            "export interface Confirmable {\n"
            "  confirm(cb: () => void): void;\n"
            "  confirm(): { ok: boolean };\n"
            "}\n"
            "export class DialogService implements Confirmable {\n"
            "  confirm(cb?: () => void): any { return { ok: true }; }\n"
            "  trigger(ready: boolean) {\n"
            "    if (window.confirm('Are you sure?')) { return true; }\n"
            "    return ready ? confirm('Proceed?') : false;\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        violations = scan_obsidian_extended([ui_dir])
        dialog_hits = [v for v in violations if v.rule == "obsidian_native_dialog"]
        assert [v.line for v in dialog_hits] == [8, 9], f"Expected only lines 8 and 9, got: {dialog_hits}"


class TestSsotZeroFakeDataScanner:
    """Verify SSOT/Zero-Fake-Data positive detections and seed/test exclusions."""

    def test_detects_ui_and_backend_synthetic_fallbacks(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()

        (src / "score.ts").write_text("const title = 'EL CLASSICO';\n", encoding="utf-8")
        (src / "docs.ts").write_text("const kinds = ['memo', 'text'];\n", encoding="utf-8")
        (src / "ai.ts").write_text("const endpoint = 'http://localhost:11434';\n", encoding="utf-8")
        (src / "health.js").write_text("if (!resp.ok) return {};\n", encoding="utf-8")
        (src / "tools.py").write_text(
            "_SAMPLE_RULES = []\n"
            "def _clamp_score(val, default=75):\n"
            "    return default\n",
            encoding="utf-8",
        )
        (src / "deploy.sh").write_text('ALLOYDB_PASSWORD="DevPassword123!"\n', encoding="utf-8")

        violations = scan_ssot_zero_fake_data([src])
        rules_hit = {v.rule for v in violations}
        assert rules_hit == {
            "ssot_ui_synthetic_payload",
            "ssot_ui_hardcoded_config_fallback",
            "ssot_ui_silent_health_fallback",
            "ssot_backend_synthetic_fallback",
        }

    def test_ignores_seeds_tests_and_placeholder_attributes(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        seeds = src / "seeds"
        tests = src / "tests"
        seeds.mkdir(parents=True)
        tests.mkdir(parents=True)

        (seeds / "seed_data.py").write_text("_SAMPLE_RULES = ['x']\n", encoding="utf-8")
        (src / "04_leadership_demo_fixtures.sql").write_text("EL CLASSICO\n", encoding="utf-8")
        (tests / "test_mock.py").write_text("DevPassword123!\n", encoding="utf-8")
        (src / "page.spec.ts").write_text("const m = 'gemma2:9b';\n", encoding="utf-8")
        (src / "form.html").write_text('<input placeholder="http://localhost:11434" />\n', encoding="utf-8")

        assert scan_ssot_zero_fake_data([src]) == []

    def test_repo_seed_files_produce_zero_violations(self) -> None:
        fixtures_sql = REPO_ROOT / "scripts/postgres-init/04_leadership_demo_fixtures.sql"
        seed_sql = REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/seed_local.sql"
        assert scan_ssot_zero_fake_data([fixtures_sql, seed_sql]) == []

    def test_ssot_edge_cases_res_ok_multiline_and_spaced_placeholder(self, tmp_path: Path) -> None:
        src = tmp_path / "ssot_edge"
        src.mkdir()
        (src / "client.ts").write_text(
            "async function a(res: Response) {\n"
            "  if (!res.ok) return {}\n"
            "}\n"
            "async function b(resp: Response) {\n"
            "  if (!resp.ok) {\n"
            "    return {};\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        (src / "form.html").write_text(
            '<input placeholder = "http://localhost:11434" />\n',
            encoding="utf-8",
        )
        violations = scan_ssot_zero_fake_data([src])
        assert len(violations) == 2, f"Expected 2 silent health violations, got: {violations}"
        assert all(v.rule == "ssot_ui_silent_health_fallback" for v in violations)

    def test_ssot_distinguishes_js_placeholder_var_from_html_attr(self, tmp_path: Path) -> None:
        src = tmp_path / "ssot_placeholder"
        src.mkdir()
        (src / "api.ts").write_text(
            "const placeholder = 'http://localhost:11434';\n"
            'export const html = `<input placeholder = "http://localhost:11434" />`;\n',
            encoding="utf-8",
        )
        (src / "form.html").write_text(
            '<input\n  type="text"\n  placeholder = "http://localhost:11434"\n/>\n',
            encoding="utf-8",
        )
        violations = scan_ssot_zero_fake_data([src])
        assert len(violations) == 1, f"Expected only JS const placeholder violation, got: {violations}"
        assert violations[0].rule == "ssot_ui_hardcoded_config_fallback"
        assert violations[0].line == 1


class TestR4LicenseHeaderScanner:
    """Verify R4 Rule A (new files) and Rule B (modified upstream dual headers)."""

    def test_synthetic_rule_a_and_rule_b_cases(self, tmp_path: Path) -> None:
        good_dir = tmp_path / "good"
        good_dir.mkdir()
        (good_dir / "new_mod.py").write_text(_VALID_R4_NEW_HEADER + "x = 1\n", encoding="utf-8")
        (good_dir / "modified_upstream.py").write_text(
            "# Copyright 2026 Google LLC\n"
            "# Modifications copyright 2026 Christian Haeussinger\n"
            "# Changed for The Rehearsal: hardened error path.\n"
            "#\n"
            '# Licensed under the Apache License, Version 2.0 (the "License");\n',
            encoding="utf-8",
        )
        assert scan_r4_license_headers(good_dir) == []

        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "missing_header.py").write_text("x = 1\n", encoding="utf-8")
        (bad_dir / "google_new.py").write_text(
            "# Copyright 2026 Google LLC\n"
            '# Licensed under the Apache License, Version 2.0 (the "License");\n',
            encoding="utf-8",
        )
        (bad_dir / "empty_summary.py").write_text(
            "# Copyright 2026 Google LLC\n"
            "# Modifications copyright 2026 Christian Haeussinger\n"
            "# Changed for The Rehearsal:\n",
            encoding="utf-8",
        )
        violations = scan_r4_license_headers(bad_dir)
        rules = {v.rule for v in violations}
        assert rules == {
            "r4_new_file_missing_copyright",
            "r4_new_file_google_llc",
            "r4_modified_missing_summary",
        }

    def test_e2e_hardening_passes_r4_license_headers(self) -> None:
        e2e_dir = REPO_ROOT / "scripts" / "e2e_hardening"
        violations = scan_r4_license_headers(e2e_dir)
        assert violations == [], f"License violations in scripts/e2e_hardening/: {violations}"

    def test_repo_subdirectory_ignores_unmodified_tracked_and_handles_renames(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "r4_repo"
        sub_clean = repo / "sub_clean"
        sub_dirty = repo / "sub_dirty"
        sub_clean.mkdir(parents=True)
        sub_dirty.mkdir(parents=True)

        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)

        upstream_hdr = (
            "// Copyright 2026 Google LLC\n"
            "// Licensed under the Apache License, Version 2.0 (the \"License\");\n"
        )
        (sub_clean / "vite.config.ts").write_text(upstream_hdr + "export default {};\n", encoding="utf-8")
        (sub_dirty / "tracked_unmodified.ts").write_text(upstream_hdr + "export const a = 1;\n", encoding="utf-8")
        (sub_dirty / "orig_upstream.ts").write_text(upstream_hdr + "export const b = 2;\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        assert scan_r4_license_headers(sub_clean) == []

        subprocess.run(
            ["git", "mv", "sub_dirty/orig_upstream.ts", "sub_dirty/renamed_upstream.ts"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        (sub_dirty / "renamed_upstream.ts").write_text(
            upstream_hdr + "export const b = 3;\n",
            encoding="utf-8",
        )
        (sub_dirty / "new_untracked.py").write_text("x = 1\n", encoding="utf-8")

        dirty_hits = scan_r4_license_headers(sub_dirty)
        flagged_names = {Path(v.file).name: v.rule for v in dirty_hits}
        assert "tracked_unmodified.ts" not in flagged_names
        assert flagged_names == {
            "renamed_upstream.ts": "r4_modified_missing_modifications_copyright",
            "new_untracked.py": "r4_new_file_missing_copyright",
        }

        admin_hits = scan_r4_license_headers(REPO_ROOT / "web" / "admin-dash")
        admin_names = {Path(v.file).name for v in admin_hits}
        assert "vite.config.ts" not in admin_names
        assert "vitest.config.js" not in admin_names


class TestBookAuthorRefsScanner:
    """Verify book attribution scanner rules and software-attribution allowance."""

    def test_synthetic_positive_and_negative_cases(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "doc.md").write_text(
            f"Co-author {_BANNED_NAME} mentioned.\n"
            f"Read {_BOOK_TITLE} by {_AUTHOR_SURNAME} today.\n",
            encoding="utf-8",
        )
        (bad / "CITATION.cff").write_text(
            "cff-version: 1.2.0\n"
            "references:\n"
            "  - type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            "    authors:\n"
            f'      - name: "{_AUTHOR_SURNAME}"\n',
            encoding="utf-8",
        )
        violations = scan_book_author_refs(bad)
        rules = {v.rule for v in violations}
        assert "book_author_ref_banned_name" in rules
        assert "book_author_ref_personal_name" in rules

        good = tmp_path / "good"
        good.mkdir()
        (good / "README.md").write_text(
            "The software is maintained by Christian Haeussinger as the companion codebase "
            "to Confidently Wrong, ISBN 979-8178685112.\n",
            encoding="utf-8",
        )
        (good / "CITATION.cff").write_text(
            "cff-version: 1.2.0\n"
            "authors:\n"
            "  - family-names: Haeussinger\n"
            "    given-names: Christian\n"
            "references:\n"
            "  - type: book\n"
            '    title: "Confidently Wrong"\n'
            "    authors:\n"
            '      - name: "The Confidently Wrong Authors"\n',
            encoding="utf-8",
        )
        assert scan_book_author_refs(good) == []

    def test_repo_core_docs_and_e2e_hardening_have_no_false_positives(self) -> None:
        violations = scan_book_author_refs(REPO_ROOT)
        assert violations == [], f"Book author attribution violations found: {violations}"

    def test_markdown_link_url_and_inline_citation_authors_edge_cases(self, tmp_path: Path) -> None:
        bad = tmp_path / "book_bad"
        bad.mkdir()
        (bad / "link.md").write_text(
            f"See [{_BOOK_TITLE}](https://example.com/book.html) by {_AUTHOR_SURNAME}.\n",
            encoding="utf-8",
        )
        (bad / "CITATION.cff").write_text(
            "cff-version: 1.2.0\n"
            "references:\n"
            "  - type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            f'    authors: ["{_AUTHOR_SURNAME}"]\n',
            encoding="utf-8",
        )
        bad_hits = scan_book_author_refs(bad)
        assert len(bad_hits) == 2, f"Expected 2 violations, got: {bad_hits}"
        assert all(v.rule == "book_author_ref_personal_name" for v in bad_hits)

        good = tmp_path / "book_good"
        good.mkdir()
        (good / "link.md").write_text(
            f"Companion to [{_BOOK_TITLE}](https://example.com/book.html). "
            f"Software maintained by {_AUTHOR_SURNAME}.\n",
            encoding="utf-8",
        )
        (good / "CITATION.cff").write_text(
            "cff-version: 1.2.0\n"
            "references:\n"
            "  - type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            '    authors: ["The Confidently Wrong Authors"]\n',
            encoding="utf-8",
        )
        assert scan_book_author_refs(good) == []

    def test_general_personal_names_in_citation_cff_and_prose(self, tmp_path: Path) -> None:
        inline_bad = tmp_path / "inline_bad"
        inline_bad.mkdir()
        (inline_bad / "CITATION.cff").write_text(
            "cff-version: 1.2.0\n"
            "references:\n"
            "  - type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            '    authors: ["John Doe"]\n',
            encoding="utf-8",
        )
        inline_hits = scan_book_author_refs(inline_bad)
        assert len(inline_hits) == 1, f"Expected 1 violation for inline John Doe, got: {inline_hits}"
        assert inline_hits[0].rule == "book_author_ref_personal_name"

        block_bad = tmp_path / "block_bad"
        block_bad.mkdir()
        (block_bad / "CITATION.cff").write_text(
            "cff-version: 1.2.0\n"
            "references:\n"
            "  - type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            "    authors:\n"
            "      - family-names: Doe\n"
            "        given-names: John\n"
            '      - name: "Jane Smith"\n',
            encoding="utf-8",
        )
        block_hits = scan_book_author_refs(block_bad)
        assert len(block_hits) == 3, f"Expected 3 block author violations, got: {block_hits}"
        assert all(v.rule == "book_author_ref_personal_name" for v in block_hits)

        prose_bad = tmp_path / "prose_bad"
        prose_bad.mkdir()
        (prose_bad / "notes.md").write_text(
            f"John Doe wrote {_BOOK_TITLE}.\n"
            f"Author of {_BOOK_TITLE}: Jane Smith.\n"
            f"Jane Smith authored {_BOOK_TITLE}.\n"
            f"Authors of {_BOOK_TITLE}: Bob Jones.\n"
            f"The author of {_BOOK_TITLE} is Alice Walker.\n"
            f"John Doe's Team authored {_BOOK_TITLE}.\n"
            f"The Authors (John Doe) wrote {_BOOK_TITLE}.\n",
            encoding="utf-8",
        )
        prose_hits = scan_book_author_refs(prose_bad)
        assert len(prose_hits) == 7, f"Expected 7 prose violations, got: {prose_hits}"
        assert all(v.rule == "book_author_ref_personal_name" for v in prose_hits)

        collective_good = tmp_path / "collective_good"
        collective_good.mkdir()
        (collective_good / "CITATION.cff").write_text(
            "cff-version: 1.2.0\n"
            "authors:\n"
            "  - family-names: Haeussinger\n"
            "    given-names: Christian\n"
            "references:\n"
            "  - type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            '    authors: ["The Confidently Wrong Authors"]\n'
            "  - authors:\n"
            '      - name: "The Confidently Wrong Authors"\n'
            '      - name: "The Leadership Simulation Editorial Team"\n'
            "    type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            "  - type: software\n"
            '    title: "race-condition"\n'
            "    authors:\n"
            '      - name: "Google LLC"\n',
            encoding="utf-8",
        )
        (collective_good / "README.md").write_text(
            f"Software by {_AUTHOR_SURNAME} as companion to {_BOOK_TITLE}.\n"
            f"The authors of {_BOOK_TITLE} published The Rehearsal.\n"
            f"The {_BOOK_TITLE} Authors wrote {_BOOK_TITLE}.\n"
            f"The Rehearsal Team authored {_BOOK_TITLE}.\n"
            f"The Leadership Simulation Editorial Team authored {_BOOK_TITLE}.\n"
            f"Author of {_BOOK_TITLE}: The Leadership Simulation Editorial Team.\n"
            f"The {_BOOK_TITLE} Collective wrote {_BOOK_TITLE}.\n"
            f"The {_BOOK_TITLE} Authors and The Rehearsal Team wrote {_BOOK_TITLE}.\n"
            f"Authors of {_BOOK_TITLE}: The {_BOOK_TITLE} Authors.\n"
            f"Author of {_BOOK_TITLE}: {_BOOK_TITLE} Authors.\n",
            encoding="utf-8",
        )
        assert scan_book_author_refs(collective_good) == []

        collective_spoof = tmp_path / "collective_spoof"
        collective_spoof.mkdir()
        (collective_spoof / "CITATION.cff").write_text(
            "cff-version: 1.2.0\n"
            "references:\n"
            "  - type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            "    authors:\n"
            '      - name: "John Doe\'s Team"\n'
            '      - name: "The Authors (John Doe)"\n'
            "  - authors:\n"
            '      - name: "Jane Smith"\n'
            "    type: book\n"
            f'    title: "{_BOOK_TITLE}"\n',
            encoding="utf-8",
        )
        spoof_hits = scan_book_author_refs(collective_spoof)
        assert len(spoof_hits) == 3, f"Expected 3 spoofed collective violations, got: {spoof_hits}"
        assert all(v.rule == "book_author_ref_personal_name" for v in spoof_hits)

    def test_same_line_masking_initials_markdown_mixed_lists_and_flow_mappings(
        self,
        tmp_path: Path,
    ) -> None:
        bad_lines = [
            f"The {_BOOK_TITLE} Authors wrote {_BOOK_TITLE}, not John Doe wrote {_BOOK_TITLE}.",
            f"The Rehearsal Team authored {_BOOK_TITLE}; Author of {_BOOK_TITLE}: Jane Smith.",
            f"Authors of {_BOOK_TITLE}: Jane Smith; Author of {_BOOK_TITLE}: The {_BOOK_TITLE} Authors.",
            f"The {_BOOK_TITLE} Authors wrote {_BOOK_TITLE}, and John Doe wrote {_BOOK_TITLE}.",
            f"Author of {_BOOK_TITLE}: John Doe, see {_BOOK_TITLE}: The Rehearsal Team.",
            f"John A. Doe wrote {_BOOK_TITLE}.",
            f"J. Doe wrote {_BOOK_TITLE}.",
            f"Dr. Jane Smith wrote {_BOOK_TITLE}.",
            f"Author of {_BOOK_TITLE}: Dr. Jane Smith.",
            f"Author of {_BOOK_TITLE}: J. Doe.",
            f"John Doe wrote *{_BOOK_TITLE}*.",
            f"John Doe wrote `{_BOOK_TITLE}`.",
            f"Authors of {_BOOK_TITLE}: The {_BOOK_TITLE} Authors and John Doe.",
            f"John Doe and The {_BOOK_TITLE} Authors wrote {_BOOK_TITLE}.",
            f"Acme Corp wrote {_BOOK_TITLE}.",
        ]
        prose_file = tmp_path / "adversarial.md"
        prose_file.write_text("\n".join(bad_lines) + "\n", encoding="utf-8")
        prose_hits = scan_book_author_refs(prose_file)
        assert len(prose_hits) == len(bad_lines), (
            f"Expected {len(bad_lines)} violations, got {len(prose_hits)}: {prose_hits}"
        )
        assert {v.line for v in prose_hits} == set(range(1, len(bad_lines) + 1))
        assert all(v.rule == "book_author_ref_personal_name" for v in prose_hits)

        cff_file = tmp_path / "CITATION.cff"
        cff_file.write_text(
            "cff-version: 1.2.0\n"
            "references:\n"
            "  - type: book\n"
            f'    title: "{_BOOK_TITLE}"\n'
            "    authors:\n"
            '      - {name: "The Leadership Simulation Editorial Team"}\n'
            '      - {name: "The Confidently Wrong Authors", family-names: Doe}\n'
            '      - {name: "Acme Corp"}\n',
            encoding="utf-8",
        )
        cff_hits = scan_book_author_refs(cff_file)
        assert [v.line for v in cff_hits] == [7, 8], f"Unexpected CITATION.cff hits: {cff_hits}"
        assert all(v.rule == "book_author_ref_personal_name" for v in cff_hits)

    def test_failures_a_b_c_mixed_clauses_by_collectives_and_name_shapes(
        self,
        tmp_path: Path,
    ) -> None:
        bad_cases = [
            # Failure A: greedy regex masking across clause boundaries
            f"The {_BOOK_TITLE} Authors wrote {_BOOK_TITLE}; John Doe wrote {_BOOK_TITLE}.",
            f"Author of {_BOOK_TITLE}: Jane Smith; Authors of {_BOOK_TITLE}: The {_BOOK_TITLE} Authors.",
            f"The {_BOOK_TITLE} Authors wrote {_BOOK_TITLE} and John Doe wrote {_BOOK_TITLE}.",
            # Failure B: _RE_BOOK_BY_PERSON with initials, apostrophes, hyphens, spoofs
            f"{_BOOK_TITLE} by Jane Smith.",
            f"{_BOOK_TITLE} by John F. Kennedy.",
            f"{_BOOK_TITLE} by Patrick O'Brien.",
            f"{_BOOK_TITLE} by Mary-Jane Watson.",
            f"{_BOOK_TITLE} by John Doe's Team.",
            f"{_BOOK_TITLE} by The Authors (John Doe).",
            # Failure C & reviewer caveats: initials, hyphens, apostrophes, quotes, year, appositive, mixed lists, spoofs
            f"John F. Kennedy wrote {_BOOK_TITLE}.",
            f"Author of {_BOOK_TITLE}: John Q. Public.",
            f"Sarah Connor-Smith wrote {_BOOK_TITLE}.",
            f"Patrick O'Brien wrote {_BOOK_TITLE}.",
            f"Mary-Jane Watson wrote {_BOOK_TITLE}.",
            f'Author of {_BOOK_TITLE}: "Jane Smith".',
            f"Authors of {_BOOK_TITLE} (2025): Jane Smith.",
            f"John Doe, author of {_BOOK_TITLE}, spoke today.",
            f"Authors of {_BOOK_TITLE}: The {_BOOK_TITLE} Authors and Jane Smith.",
            f"Authors of {_BOOK_TITLE}: Editorial Group, Jane Smith.",
            f"John's Team authored {_BOOK_TITLE}.",
            f"Authors (John Doe) wrote {_BOOK_TITLE}.",
            f"Authors (John Q. Public) wrote {_BOOK_TITLE}.",
        ]
        bad_file = tmp_path / "r4_failures_bad.md"
        bad_file.write_text("\n".join(bad_cases) + "\n", encoding="utf-8")
        bad_hits = scan_book_author_refs(bad_file)
        assert len(bad_hits) == len(bad_cases), (
            f"Expected {len(bad_cases)} violations, got {len(bad_hits)}: {bad_hits}"
        )
        assert {v.line for v in bad_hits} == set(range(1, len(bad_cases) + 1))
        assert all(v.rule == "book_author_ref_personal_name" for v in bad_hits)

        good_cases = [
            f"{_BOOK_TITLE} by The {_BOOK_TITLE} Authors.",
            f"{_BOOK_TITLE}, published by The Rehearsal Team.",
            f"{_BOOK_TITLE} by {_BOOK_TITLE} Authors.",
            f"{_BOOK_TITLE} by The Authors of {_BOOK_TITLE}.",
            f"The Authors of {_BOOK_TITLE} wrote {_BOOK_TITLE}.",
            f"{_BOOK_TITLE} Authors wrote {_BOOK_TITLE}.",
            f"The Rehearsal Team authored {_BOOK_TITLE}.",
            f"The {_BOOK_TITLE} Authors, authors of {_BOOK_TITLE}, spoke today.",
            f'Authors of {_BOOK_TITLE}: "The {_BOOK_TITLE} Authors".',
            f"Authors of {_BOOK_TITLE} (2025): The {_BOOK_TITLE} Authors.",
        ]
        good_file = tmp_path / "r4_failures_good.md"
        good_file.write_text("\n".join(good_cases) + "\n", encoding="utf-8")
        assert scan_book_author_refs(good_file) == []
        assert scan_book_author_refs(REPO_ROOT) == []


class TestHarnessCoreUtilities:
    """Verify MockOllamaServer, port allocation, run_cmd, safety probes, and git preservation."""

    def test_free_port_never_returns_11434(self) -> None:
        ports = {free_port() for _ in range(10)}
        assert len(ports) >= 1
        assert all(1024 <= p <= 65535 and p != 11434 for p in ports)

    def test_run_cmd_captures_output_and_exit_code(self) -> None:
        ok = run_cmd(["python3", "-c", "print('hello-harness')"])
        assert ok.returncode == 0
        assert "hello-harness" in ok.stdout

        fail = run_cmd(["python3", "-c", "import sys; sys.stderr.write('err'); sys.exit(3)"])
        assert fail.returncode == 3
        assert "err" in fail.stderr

    def test_mock_ollama_server_endpoints_and_fault_injection(self) -> None:
        with MockOllamaServer(chat_response_content='{"decision": "proceed"}') as mock:
            assert mock.port != 11434
            with httpx.Client(base_url=mock.base_url, timeout=3.0) as client:
                tags = client.get("/api/tags")
                assert tags.status_code == 200
                assert any(m["name"] == "gemma2:9b" for m in tags.json()["models"])

                v1_models = client.get("/v1/models")
                assert v1_models.status_code == 200
                assert v1_models.json()["data"][0]["id"] == "gemma2:9b"

                chat = client.post("/api/chat", json={"model": "gemma2:9b", "messages": []})
                assert chat.status_code == 200
                assert chat.json()["message"]["content"] == '{"decision": "proceed"}'

                embed = client.post("/api/embed", json={"model": "embeddinggemma:latest", "input": ["a", "b"]})
                assert embed.status_code == 200
                vecs = embed.json()["embeddings"]
                assert len(vecs) == 2
                assert len(vecs[0]) == 768

                legacy_embed = client.post("/api/embeddings", json={"model": "embeddinggemma:latest", "prompt": "hi"})
                assert legacy_embed.status_code == 200
                assert len(legacy_embed.json()["embedding"]) == 768

                pull = client.post("/api/pull", json={"name": "gemma2:9b", "stream": True})
                assert pull.status_code == 200
                assert "application/x-ndjson" in pull.headers.get("content-type", "")
                frames = [json.loads(line) for line in pull.text.strip().splitlines()]
                assert len(frames) == 2
                assert frames[-1]["status"] == "success"

                mock.fail_chat = True
                assert client.post("/api/chat", json={"model": "gemma2:9b"}).status_code == 500
                mock.fail_chat = False

                mock.empty_embed = True
                assert client.post("/api/embed", json={"input": "x"}).json() == {"embeddings": []}
                mock.empty_embed = False

                mock.fail_embed = True
                assert client.post("/api/embed", json={"input": "x"}).status_code == 500
                mock.fail_embed = False

                mock.status_override["/api/embed"] = 404
                assert client.post("/api/embed", json={"input": "x"}).status_code == 404

            assert len(mock.requests) == 10

    def test_mock_ollama_server_concurrent_burst(self) -> None:
        num_threads = 16
        trials = 5
        with MockOllamaServer(chat_response_content='{"status": "concurrent-ok"}') as mock:
            for _ in range(trials):
                barrier = threading.Barrier(num_threads)
                errors: list[str] = []
                err_lock = threading.Lock()

                def worker(b: threading.Barrier = barrier) -> None:
                    try:
                        b.wait(timeout=5.0)
                        with httpx.Client(base_url=mock.base_url, timeout=5.0) as client:
                            resp = client.post(
                                "/api/chat",
                                json={"model": "gemma2:9b", "messages": [{"role": "user", "content": "hi"}]},
                            )
                            if resp.status_code != 200:
                                with err_lock:
                                    errors.append(f"HTTP {resp.status_code}")
                    except Exception as exc:  # noqa: BLE001
                        with err_lock:
                            errors.append(f"{type(exc).__name__}: {exc}")

                threads = [threading.Thread(target=worker) for _ in range(num_threads)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10.0)
                assert errors == [], f"Concurrent MockOllamaServer burst failed: {errors}"

            assert len(mock.requests) == num_threads * trials

    def test_ephemeral_go_server_rejects_port_env_overrides(self) -> None:
        for key in ("PORT", "GATEWAY_PORT", "ADMIN_PORT"):
            with pytest.raises(ValueError, match="Pass port=\\.\\.\\. to EphemeralGoServer instead of env\\["):
                EphemeralGoServer(Path("/nonexistent/gateway"), env={key: "9999"})

    def test_workspace_preservation_positive_and_negative(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)

        tracked = repo / "app.py"
        tracked.write_text("v1\n", encoding="utf-8")
        docs_file = repo / "docs" / "guide.md"
        docs_file.parent.mkdir()
        docs_file.write_text("guide\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        tracked.write_text("v2\n", encoding="utf-8")
        baseline = get_git_status_snapshot(repo)
        assert baseline.get("app.py") == " M"

        allowed_new = repo / "scripts" / "e2e_hardening" / "x.py"
        allowed_new.parent.mkdir(parents=True)
        allowed_new.write_text("# ok\n", encoding="utf-8")
        assert_workspace_preserved(baseline, allow_new_prefixes=("scripts/e2e_hardening/",), repo_root=repo)

        unexpected = repo / "rogue.txt"
        unexpected.write_text("bad\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="Unexpected modified/untracked"):
            assert_workspace_preserved(baseline, allow_new_prefixes=("scripts/e2e_hardening/",), repo_root=repo)
        unexpected.unlink()

        docs_file.write_text("tampered\n", encoding="utf-8")
        assert check_protected_files_untouched(repo) == ["docs/guide.md"]
        with pytest.raises(AssertionError, match="Protected files"):
            assert_workspace_preserved(baseline, allow_new_prefixes=("scripts/e2e_hardening/",), repo_root=repo)
        docs_file.write_text("guide\n", encoding="utf-8")

        tracked.write_text("v1\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="Baseline paths disappeared"):
            assert_workspace_preserved(baseline, allow_new_prefixes=("scripts/e2e_hardening/",), repo_root=repo)

    def test_repo_safety_checks_execute_quickly(self) -> None:
        assert check_protected_files_untouched(REPO_ROOT) == []
        snapshot = get_git_status_snapshot(REPO_ROOT)
        assert len(snapshot) >= 385

        start = time.monotonic()
        listening = check_ollama_11434_listening()
        elapsed = time.monotonic() - start
        assert isinstance(listening, bool)
        assert elapsed < 2.5, f"Ollama TCP probe took unexpectedly long: {elapsed:.2f}s"

    def test_isolated_postgres_schema_sync_and_cleanup(self) -> None:
        import psycopg2

        try:
            with IsolatedPostgresSchema(require_available=True) as pg:
                assert pg.available is True
                assert pg.schema_name.startswith("e2e_")
                assert "search_path" in pg.search_path_dsn
                with psycopg2.connect(pg.search_path_dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("CREATE TABLE probe_tbl (id INT PRIMARY KEY)")
                        cur.execute("INSERT INTO probe_tbl VALUES (42)")
                        cur.execute("SELECT id FROM probe_tbl")
                        assert cur.fetchone() == (42,)
                base_dsn = pg.dsn
                dropped_schema = pg.schema_name
        except PostgresUnavailableError as exc:
            pytest.skip(f"Postgres unreachable: {exc}")

        with psycopg2.connect(base_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
                    (dropped_schema,),
                )
                assert cur.fetchone() is None

    async def test_isolated_postgres_schema_async_and_cleanup(self) -> None:
        import asyncpg

        try:
            async with IsolatedPostgresSchema(require_available=True) as pg:
                assert pg.available is True
                conn = await asyncpg.connect(pg.dsn)
                try:
                    await conn.execute(f'SET search_path TO "{pg.schema_name}", public')
                    await conn.execute("CREATE TABLE async_probe (id INT PRIMARY KEY)")
                    val = await conn.fetchval("SELECT 99")
                    assert val == 99
                finally:
                    await conn.close()
                base_dsn = pg.dsn
                dropped_schema = pg.schema_name
        except PostgresUnavailableError as exc:
            pytest.skip(f"Postgres unreachable: {exc}")

        conn = await asyncpg.connect(base_dsn)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1",
                dropped_schema,
            )
            assert exists is None
        finally:
            await conn.close()

    def test_isolated_redis_namespace_and_cleanup(self) -> None:
        import redis

        try:
            with IsolatedRedisNamespace(require_available=True) as rns:
                assert rns.available is True
                client = redis.Redis(
                    host=rns.host,
                    port=rns.port,
                    password=rns.password or None,
                    decode_responses=True,
                )
                key = f"{rns.prefix}:probe"
                client.set(key, "ok")
                assert client.get(key) == "ok"
                client.close()
                host, port, pw, prefix = rns.host, rns.port, rns.password, rns.prefix
        except RedisUnavailableError as exc:
            pytest.skip(f"Redis unreachable: {exc}")

        verify = redis.Redis(host=host, port=port, password=pw or None, decode_responses=True)
        try:
            assert verify.keys(f"*{prefix}*") == []
        finally:
            verify.close()
