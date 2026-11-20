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

"""Repo-wide compliance scanners for Obsidian Light, SSOT, R4 headers, book refs, and safety."""

from __future__ import annotations

import fnmatch
import re
import socket
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent

# Split literal so self-scanning never triggers a false positive in source.
_BANNED_BOOK_COAUTHOR = "Gil" + "martin"

_OBSIDIAN_SKIP_DIRS = {
    "node_modules",
    "dist",
    "build",
    ".angular",
    ".git",
    ".next",
    "coverage",
    "out-tsc",
    "__tests__",
}
_OBSIDIAN_EXTS = {".ts", ".tsx", ".js", ".jsx", ".css", ".scss", ".html"}
_OBSIDIAN_TEST_SUFFIXES = (
    ".spec.ts",
    ".spec.js",
    ".spec.tsx",
    ".spec.jsx",
    ".test.ts",
    ".test.js",
    ".test.tsx",
    ".test.jsx",
)
_THREED_CANVAS_MODULES = {
    "car-simulator.ts",
    "catmullrom-path-adapter.ts",
    "glb-roads.ts",
    "gradient-path.ts",
    "label-overlay.ts",
    "map.ts",
    "path-generator.ts",
    "path.ts",
    "road-network.ts",
    "runner.ts",
    "simulation.ts",
    "traffic-simulator.ts",
    "water-station.ts",
}
_OBSIDIAN_HEX_WHITELIST = {
    "#F5F5F7",
    "#F2F2F7",
    "#E5E5EA",
    "#FFFFFF",
    "#FFF",
    "#1D1D1F",
    "#2C2C2E",
    "#2D2D2F",
    "#52525B",
    "#515154",
    "#6E6E73",
    "#86868B",
    "#0071E3",
    "#0077ED",
    "#34C759",
    "#FF9500",
    "#FF3B30",
    "#C7C7CC",
    "#000000",
    "#000",
}
_SPHERE_SPECIAL_HEXES = {"#141416", "#050506"}

_RE_PROHIBITED_TAILWIND = re.compile(
    r"\b(?:bg|text|border|ring|from|via|to)-(?:slate|gray|zinc|amber|emerald|indigo|purple|violet|rose|teal|cyan|lime|fuchsia|pink|sky|yellow|orange)-\d{2,3}\b(?:/\d{1,3})?"
    r"|\b(?:bg|text|border)-(?:blue|red|green)-50\b"
)
_RE_HEX_COLOR = re.compile(r"(?<!&)#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{5})?\b")
_RE_PILL_RADIUS = re.compile(r"border-radius\s*:\s*(?:999|100)px\b")
_RE_PILL_CLASS = re.compile(
    r"""\bclass(?:Name)?\s*=\s*["'`][^"'`]*(?:\b[\w-]*pill\b|\bstatus-badge\b|\binferred-tag\b)[^"'`]*["'`]"""
)
_RE_NATIVE_DIALOG = re.compile(
    r"(?<![.\w])(window\.)?(confirm|alert|prompt)\s*\("
)
_RE_TS_MEMBER_MODIFIERS = re.compile(
    r"^(?:\s*(?:public|private|protected|static|async|abstract|override|readonly|declare|function)\b)*\s*$"
)
_RE_NATIVE_TITLE = re.compile(
    r"""<(?:label|th|td|p|h[1-6]|button|span|a)\b[^<>]{0,500}?\btitle\s*=\s*(?:["'][^"']+["']|\{[^}]+\})"""
)
_RE_HORIZONTAL_TABS = re.compile(
    r"""\brole\s*=\s*["']tablist["']|\bclass(?:Name)?\s*=\s*["'`][^"'`]*\b(?:tab-bar|segmented-control|docs__tabs)\b[^"'`]*["'`]"""
)
_RE_GRID_TWO_ATTR = re.compile(r"""\bclass(?:Name)?\s*=\s*["'`][^"'`]*\bgrid-2\b[^"'`]*["'`]""")
_RE_GRID_TWO_CSS = re.compile(r"\.grid-2\b")
_RE_FLOATING_TOAST = re.compile(r"\.apple-glass-toast\.visible\b|showAppleGlassToast\s*\(|\btoastMessage\b")
_RE_CRYPTO_PLUMBING = re.compile(r"\b(?:SHA-256|RFC\s*8785|Row-Merkle|SQLite\s*WAL)\b")

_SSOT_SKIP_DIRS = {
    "node_modules",
    "dist",
    "build",
    ".angular",
    ".git",
    ".venv",
    "__pycache__",
    "coverage",
    "out-tsc",
    "__tests__",
    "tests",
    "evals",
    "e2e",
    "e2e_hardening",
    "seeds",
    "cached_routes",
    "assets",
}
_SSOT_SKIP_FILE_GLOBS = (
    "*.spec.*",
    "*.test.*",
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "conftest.py",
    "*seed*.sql",
    "*seed*.py",
    "*seed*.json",
    "04_leadership_demo_fixtures.sql",
    "generate_local_seeds.py",
)
_SSOT_UI_EXTS = {".ts", ".tsx", ".js", ".jsx", ".html"}
_SSOT_BACKEND_EXTS = {".py", ".go", ".sh"}

_RE_SSOT_UI_SYNTHETIC = re.compile(
    r"\bEL CLASSICO\b|Vegas Strip Marathon Plan|Neon Strip Run"
    r"|\bplaceholderRoutes\b|\bplaceholderAgentMessage\b|\bLEADERSHIP_SIM_RESULTS_COMPONENTS\b"
)
_RE_SSOT_MEMO_TEXT = re.compile(r"\[\s*['\"]memo['\"]\s*,\s*['\"]text['\"]\s*\]")
_RE_SSOT_SAMPLE_FALLBACK = re.compile(
    r"\b(?:isSampleDecision|allowSampleFallback)\b|===\s*['\"]sample-1['\"]|\bSarah Jenkins\b"
)
_RE_SSOT_OLLAMA_UI_DEFAULTS = re.compile(
    r"(['\"])(?:http://localhost:11434|gemma2:9b|embeddinggemma:latest)\1"
)
_RE_SSOT_PLACEHOLDER_PREFIX = re.compile(r"\bplaceholder\s*=\s*$")
_RE_SSOT_SILENT_HEALTH = re.compile(
    r"if\s*\(\s*!res(?:p)?\.ok\s*\)\s*(?:\{\s*)?return\s*\{\s*\}\s*;?"
)
_RE_SSOT_BACKEND_FALLBACK = re.compile(
    r"\b_SAMPLE_RULES\b|\b_samples_response\b|\b_heuristic_fallback\b"
    r"|\b_get_or_create_fallback_stakeholders\b|\b_fallback_inflections\b"
    r"|\b_heuristic_leadership_eval\b"
)
_RE_SSOT_DEV_PASSWORD = re.compile(r"DevPassword123!")
_RE_SSOT_CLAMP_SCORE_75 = re.compile(r"def\s+_clamp_score\([^)]*default\s*=\s*75")

_LICENSE_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    ".uv_cache",
    "dist",
    ".angular",
    "htmlcov",
    "__pycache__",
    ".terraform",
    "gen_proto",
    "data",
    "build",
    "coverage",
    "out-tsc",
}
_LICENSE_SOURCE_EXTS = {
    ".py",
    ".sh",
    ".go",
    ".ts",
    ".js",
    ".css",
    ".scss",
    ".html",
    ".sql",
    ".tf",
    ".hcl",
}

_BOOK_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    ".uv_cache",
    "dist",
    ".angular",
    "htmlcov",
    "__pycache__",
    "logs",
    "data",
    ".terraform",
    "gen_proto",
    "coverage",
    "out-tsc",
}
_BOOK_TEXT_EXTS = {
    ".md",
    ".py",
    ".go",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".html",
    ".css",
    ".scss",
    ".cff",
    ".yaml",
    ".yml",
    ".txt",
    ".sh",
    ".sql",
}
_BOOK_TEXT_NAMES = {"NOTICE", "README", "CONTRIBUTING"}
_RE_BANNED_COAUTHOR = re.compile(rf"\b{_BANNED_BOOK_COAUTHOR}\b", re.IGNORECASE)
_BOOK_NAME_WORD = (
    r"[A-Z](?:[a-z]+(?:[A-Z][a-z]+)?|'[A-Z][a-z]+)"
    r"(?:-[A-Z](?:[a-z]+(?:[A-Z][a-z]+)?|'[A-Z][a-z]+))*"
)
_BOOK_HONORIFIC = r"(?:Dr|Prof|Mr|Ms|Mrs|Sir|Dame)\."
_BOOK_AUTHOR_FIRST_TOKEN = rf"(?:{_BOOK_HONORIFIC}|{_BOOK_NAME_WORD}(?:'s)?|[A-Z]\.(?![A-Za-z]))"
_BOOK_AUTHOR_NEXT_TOKEN = rf"(?:{_BOOK_HONORIFIC}|{_BOOK_NAME_WORD}(?:'s)?|[A-Z]\.?(?![A-Za-z]))"
_BOOK_COLLECTIVE_OF_TITLE = (
    r"\b(?:(?i:The)\s+)?(?i:Authors?|Team|Collective|Editorial(?:\s+Team|\s+Group)?|Group|Press)"
    r"\s+(?i:of)\s+(?:(?i:the)\s+)?(?i:Confidently\s+Wrong|The\s+Rehearsal)"
)
_BOOK_SINGLE_COLLECTIVE = r"(?i:Authors?|Team|Collective|Editorial|Group|Press)"
_BOOK_AUTHOR_ENTITY_CORE = (
    r"(?:"
    rf"{_BOOK_COLLECTIVE_OF_TITLE}(?:\s*\([^)\n]{{1,60}}\))?"
    r"|"
    rf"\b{_BOOK_SINGLE_COLLECTIVE}\s*\([^)\n]{{1,60}}\)"
    r"|"
    rf"\b{_BOOK_AUTHOR_FIRST_TOKEN}(?:\s+{_BOOK_AUTHOR_NEXT_TOKEN}){{1,6}}(?:\s*\([^)\n]{{1,60}}\))?"
    r")"
)
_BOOK_AUTHOR_ENTITY = rf"(?:[\"']?{_BOOK_AUTHOR_ENTITY_CORE}[\"']?)"
_BOOK_AUTHOR_LIST = (
    rf"{_BOOK_AUTHOR_ENTITY}"
    rf"(?:(?:\s*,\s*(?:(?:and|&)\s+)?|\s+(?:and|&)\s+)(?:{_BOOK_AUTHOR_ENTITY}|(?i:Haeussinger)))*"
)
_RE_BOOK_BY_PERSON = re.compile(
    rf"(?<![A-Za-z0-9])Confidently\s+Wrong(?![A-Za-z0-9])"
    rf"(?:(?![;]|(?i:Confidently\s+Wrong))(?:[^\n.]|\.(?!\s|$))){{0,100}}?\bby\s+"
    rf"(?P<by_person>{_BOOK_AUTHOR_LIST}|(?i:Haeussinger))"
)
_RE_BOOK_AUTHOR_OF = re.compile(
    rf"(?P<subject>{_BOOK_AUTHOR_LIST})"
    r"(?:\s+(?i:(?:is|was)\s+(?:the\s+)?author\s+of|(?:co-?)?(?:wrote|authored))\s+"
    r"(?:(?![,;]|(?i:Confidently\s+Wrong))[^\n.]){0,40}?"
    r"(?<![A-Za-z0-9])(?i:Confidently\s+Wrong)(?![A-Za-z0-9])"
    r"|\s*,\s*(?i:(?:(?:the|an)\s+)?(?:co-?)?authors?\s+of)\s+"
    r"(?:(?![,;]|(?i:Confidently\s+Wrong))[^\n.]){0,40}?"
    r"(?<![A-Za-z0-9])(?i:Confidently\s+Wrong)(?![A-Za-z0-9]))"
    r"|"
    r"(?i:\bauthor(?:\(s\)|s)?\s*(?:of|:)\s*)"
    r"(?:(?![,;:]|(?i:Confidently\s+Wrong))[^\n.]){0,60}?"
    r"(?<![A-Za-z0-9])(?i:Confidently\s+Wrong)(?![A-Za-z0-9])"
    r"(?:[*_`\"'\]]|\]\([^)\n]+\)|\s*\([^)\n]{1,40}\)){0,3}"
    r"(?:(?:(?![,;]|(?i:Confidently\s+Wrong))[^\n.]){0,80}?(?i:\b(?:Haeussinger|Christian\s+Haeussinger)\b)"
    r"|\s*(?::|\bis\b|\bare\b|\bwas\b|\bwere\b|,|—|-)\s*"
    rf"(?P<author>{_BOOK_AUTHOR_LIST}))"
)

_PROTECTED_ROOT_FILES = {"LICENSE", "NOTICE", "CITATION.cff"}
_PROTECTED_SUFFIXES = {".pptx", ".docx", ".pdf", ".png", ".jpg", ".jpeg", ".zip", ".bak"}


@dataclass(frozen=True)
class ComplianceViolation:
    """Single deterministic compliance finding."""

    file: str
    line: int
    rule: str
    match: str
    message: str


def _strip_comments_preserve_lines(text: str) -> str:
    text = re.sub(r"<!--[\s\S]*?-->", lambda m: "\n" * m.group(0).count("\n"), text)
    text = re.sub(r"/\*[\s\S]*?\*/", lambda m: "\n" * m.group(0).count("\n"), text)
    text = re.sub(r"(?<!:)//[^\n]*", "", text)
    return text


def _strip_hash_full_line_comments(text: str) -> str:
    return re.sub(r"(?m)^[ \t]*#[^\n]*$", "", text)


def _is_inside_viewport(path: Path) -> bool:
    parts = path.parts
    for idx in range(len(parts) - 1):
        if parts[idx] == "app" and parts[idx + 1] == "viewport":
            return True
    return False


def _iter_files(
    targets: Iterable[Path],
    *,
    skip_dirs: set[str],
    allowed_exts: set[str],
) -> Iterable[Path]:
    for root in targets:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.lower() in allowed_exts:
                yield root
            continue
        for item in sorted(root.rglob("*")):
            if not item.is_file():
                continue
            rel_parts = item.relative_to(root).parts
            if any(part in skip_dirs for part in rel_parts):
                continue
            if item.suffix.lower() in allowed_exts:
                yield item


def _find_balanced_paren_end(text: str, open_idx: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if escaped:
            escaped = False
            continue
        if quote is not None:
            if ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return idx
    return None


def _iter_native_dialog_calls(text: str) -> Iterable[tuple[int, str]]:
    for m in _RE_NATIVE_DIALOG.finditer(text):
        open_idx = m.end() - 1
        close_idx = _find_balanced_paren_end(text, open_idx)
        end_idx = (close_idx + 1) if close_idx is not None else m.end()
        full_match = text[m.start() : end_idx]

        if m.group(1):
            yield (m.start(), full_match)
            continue

        after = text[end_idx:].lstrip()
        if after.startswith("{"):
            continue
        if after.startswith(":"):
            seg_start = max(
                text.rfind("\n", 0, m.start()),
                text.rfind(";", 0, m.start()),
                text.rfind("{", 0, m.start()),
                text.rfind("}", 0, m.start()),
            )
            prefix = text[seg_start + 1 : m.start()]
            if _RE_TS_MEMBER_MODIFIERS.match(prefix):
                if seg_start >= 0 and text[seg_start] == "\n":
                    prev_non_ws = text[: m.start()].rstrip()
                    if prev_non_ws.endswith(("?", "=", ",", "(", "!", "&", "|", "+", "-", "*", "/")):
                        yield (m.start(), full_match)
                        continue
                continue

        yield (m.start(), full_match)


def _is_html_placeholder_attr(stripped: str, line_prefix: str, match_abs_start: int) -> bool:
    if not _RE_SSOT_PLACEHOLDER_PREFIX.search(line_prefix):
        return False
    if re.search(r"(?:\b(?:const|let|var)\s+|\.)placeholder\s*=\s*$", line_prefix):
        return False

    window = stripped[max(0, match_abs_start - 600) : match_abs_start]
    before_attr = _RE_SSOT_PLACEHOLDER_PREFIX.sub("", window)
    tag_matches = list(re.finditer(r"<([A-Za-z][A-Za-z0-9:-]*)(?=[\s/>])", before_attr))
    if not tag_matches:
        return False

    in_tag = before_attr[tag_matches[-1].start() :]
    in_single = False
    in_double = False
    brace_depth = 0
    escaped = False
    for ch in in_tag:
        if escaped:
            escaped = False
            continue
        if in_single:
            if ch == "\\":
                escaped = True
            elif ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            continue
        if ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "{":
            brace_depth += 1
        elif ch == "}" and brace_depth > 0:
            brace_depth -= 1
        elif brace_depth == 0 and ch in {">", ";"}:
            return False

    return not in_single and not in_double and brace_depth == 0


def scan_obsidian_extended(targets: list[Path] | None = None) -> list[ComplianceViolation]:
    """Scan UI sources for the 9 Obsidian Light / HIG blind spots missed by obsidian_cli.py."""
    if targets is None:
        targets = [
            REPO_ROOT / "web/frontend/src",
            REPO_ROOT / "web/admin-dash",
            REPO_ROOT / "web/tester/src",
            REPO_ROOT / "web/agent-dash",
        ]

    violations: list[ComplianceViolation] = []
    for path in _iter_files(targets, skip_dirs=_OBSIDIAN_SKIP_DIRS, allowed_exts=_OBSIDIAN_EXTS):
        name = path.name
        if name.endswith(_OBSIDIAN_TEST_SUFFIXES):
            continue
        if _is_inside_viewport(path):
            continue

        raw_text = path.read_text(encoding="utf-8", errors="replace")
        stripped = _strip_comments_preserve_lines(raw_text)
        lines = stripped.splitlines()
        ext = path.suffix.lower()
        is_threed_canvas = name in _THREED_CANVAS_MODULES
        is_code_dom_file = ext in {".ts", ".tsx", ".js", ".jsx", ".html"}

        for lineno, line_text in enumerate(lines, start=1):
            for m in _RE_PROHIBITED_TAILWIND.finditer(line_text):
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_prohibited_tailwind",
                        match=m.group(0),
                        message="Prohibited non-monochrome or dark-mode Tailwind utility token.",
                    )
                )

            if not is_threed_canvas:
                for m in _RE_HEX_COLOR.finditer(line_text):
                    hex_upper = m.group(0).upper()
                    if hex_upper in _OBSIDIAN_HEX_WHITELIST:
                        continue
                    if hex_upper in _SPHERE_SPECIAL_HEXES:
                        ctx_start = max(0, lineno - 6)
                        ctx_end = min(len(lines), lineno + 5)
                        ctx_block = "\n".join(lines[ctx_start:ctx_end])
                        if ".sphere-core" in ctx_block or ".obsidian-sphere" in ctx_block:
                            continue
                    violations.append(
                        ComplianceViolation(
                            file=str(path),
                            line=lineno,
                            rule="obsidian_non_canonical_hex",
                            match=m.group(0),
                            message=f"Non-canonical hex color {m.group(0)} outside Obsidian Light palette.",
                        )
                    )

            for m in _RE_PILL_RADIUS.finditer(line_text):
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_pill_radius_or_class",
                        match=m.group(0),
                        message="Prohibited pill border-radius (999px/100px).",
                    )
                )
            for m in _RE_PILL_CLASS.finditer(line_text):
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_pill_radius_or_class",
                        match=m.group(0),
                        message="Prohibited pill/status-badge/inferred-tag class.",
                    )
                )

            for m in _RE_HORIZONTAL_TABS.finditer(line_text):
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_horizontal_tabs",
                        match=m.group(0),
                        message="Prohibited horizontal tab bar / segmented control.",
                    )
                )

            grid_match = _RE_GRID_TWO_ATTR.search(line_text)
            if grid_match is None and ext in {".css", ".scss"}:
                grid_match = _RE_GRID_TWO_CSS.search(line_text)
            if grid_match is not None:
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_grid_two_columns",
                        match=grid_match.group(0),
                        message="Prohibited two-column .grid-2 layout class.",
                    )
                )

            for m in _RE_FLOATING_TOAST.finditer(line_text):
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_floating_toast",
                        match=m.group(0),
                        message="Prohibited floating toast activation or state.",
                    )
                )

            for m in _RE_CRYPTO_PLUMBING.finditer(line_text):
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_crypto_plumbing_in_ui",
                        match=m.group(0),
                        message="Internal cryptographic/storage plumbing term exposed in UI.",
                    )
                )

        if is_code_dom_file:
            for start_pos, match_text in _iter_native_dialog_calls(stripped):
                lineno = stripped.count("\n", 0, start_pos) + 1
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_native_dialog",
                        match=match_text,
                        message="Blocking browser dialog (confirm/alert/prompt) violates Apple HIG sheets.",
                    )
                )
            for m in _RE_NATIVE_TITLE.finditer(stripped):
                lineno = stripped.count("\n", 0, m.start()) + 1
                violations.append(
                    ComplianceViolation(
                        file=str(path),
                        line=lineno,
                        rule="obsidian_native_title_in_ts_js",
                        match=m.group(0),
                        message="Native HTML title= attribute in template; use AppleInfoPopover instead.",
                    )
                )

    return violations


def scan_ssot_zero_fake_data(targets: list[Path] | None = None) -> list[ComplianceViolation]:
    """Scan UI and backend code for fake fallback payloads, silent fallbacks, and dev passwords."""
    if targets is None:
        targets = [
            REPO_ROOT / "web/frontend/src",
            REPO_ROOT / "web/admin-dash",
            REPO_ROOT / "web/tester/src",
            REPO_ROOT / "web/agent-dash",
            REPO_ROOT / "agents",
            REPO_ROOT / "cmd",
            REPO_ROOT / "internal",
            REPO_ROOT / "scripts",
        ]

    allowed_exts = _SSOT_UI_EXTS | _SSOT_BACKEND_EXTS
    violations: list[ComplianceViolation] = []

    for path in _iter_files(targets, skip_dirs=_SSOT_SKIP_DIRS, allowed_exts=allowed_exts):
        name = path.name
        if any(fnmatch.fnmatch(name, pat) for pat in _SSOT_SKIP_FILE_GLOBS):
            continue

        raw_text = path.read_text(encoding="utf-8", errors="replace")
        ext = path.suffix.lower()
        stripped = _strip_comments_preserve_lines(raw_text)
        if ext in {".py", ".sh"}:
            stripped = _strip_hash_full_line_comments(stripped)
        raw_lines_with_ends = stripped.splitlines(keepends=True)
        lines = [ln.rstrip("\r\n") for ln in raw_lines_with_ends]

        if ext in _SSOT_UI_EXTS:
            line_offset = 0
            for lineno, (raw_ln, line_text) in enumerate(zip(raw_lines_with_ends, lines, strict=True), start=1):
                for m in _RE_SSOT_UI_SYNTHETIC.finditer(line_text):
                    violations.append(
                        ComplianceViolation(
                            file=str(path),
                            line=lineno,
                            rule="ssot_ui_synthetic_payload",
                            match=m.group(0),
                            message="Hardcoded synthetic demo payload in UI source.",
                        )
                    )
                for pattern in (_RE_SSOT_MEMO_TEXT, _RE_SSOT_SAMPLE_FALLBACK):
                    for m in pattern.finditer(line_text):
                        violations.append(
                            ComplianceViolation(
                                file=str(path),
                                line=lineno,
                                rule="ssot_ui_hardcoded_config_fallback",
                                match=m.group(0),
                                message="Hardcoded UI config/sample fallback violates 100% SSOT.",
                            )
                        )
                for m in _RE_SSOT_OLLAMA_UI_DEFAULTS.finditer(line_text):
                    if _is_html_placeholder_attr(stripped, line_text[: m.start()], line_offset + m.start()):
                        continue
                    violations.append(
                        ComplianceViolation(
                            file=str(path),
                            line=lineno,
                            rule="ssot_ui_hardcoded_config_fallback",
                            match=m.group(0),
                            message="Hardcoded UI config/sample fallback violates 100% SSOT.",
                        )
                    )
                line_offset += len(raw_ln)

            if ext in {".js", ".jsx", ".ts", ".tsx"}:
                for m in _RE_SSOT_SILENT_HEALTH.finditer(stripped):
                    lineno = stripped.count("\n", 0, m.start()) + 1
                    violations.append(
                        ComplianceViolation(
                            file=str(path),
                            line=lineno,
                            rule="ssot_ui_silent_health_fallback",
                            match=m.group(0),
                            message="Silent empty-object fallback on failed HTTP response.",
                        )
                    )

        if ext in _SSOT_BACKEND_EXTS:
            for lineno, line_text in enumerate(lines, start=1):
                for pattern in (_RE_SSOT_BACKEND_FALLBACK, _RE_SSOT_DEV_PASSWORD, _RE_SSOT_CLAMP_SCORE_75):
                    for m in pattern.finditer(line_text):
                        violations.append(
                            ComplianceViolation(
                                file=str(path),
                                line=lineno,
                                rule="ssot_backend_synthetic_fallback",
                                match=m.group(0),
                                message="Synthetic/heuristic fallback or hardcoded credential in backend source.",
                            )
                        )

    return violations


def _git_head_has_google_llc(repo_root: Path, rel_path: str) -> bool:
    proc = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        cwd=str(repo_root),
        capture_output=True,
        check=False,
        timeout=15,
    )
    if proc.returncode != 0:
        return False
    head_lines = proc.stdout.decode("utf-8", errors="replace").splitlines()[:20]
    return any("Copyright 2026 Google LLC" in line for line in head_lines)


def _check_rule_a_new_file(path: Path) -> ComplianceViolation | None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:25]
    header_block = "\n".join(lines)
    if "Google LLC" in header_block:
        return ComplianceViolation(
            file=str(path),
            line=1,
            rule="r4_new_file_google_llc",
            match="Google LLC",
            message="New file must not carry 'Google LLC' copyright.",
        )
    if "Copyright 2026 Christian Haeussinger" not in header_block:
        return ComplianceViolation(
            file=str(path),
            line=1,
            rule="r4_new_file_missing_copyright",
            match="",
            message="New file missing 'Copyright 2026 Christian Haeussinger'.",
        )
    if "Licensed under the Apache License, Version 2.0" not in header_block:
        return ComplianceViolation(
            file=str(path),
            line=1,
            rule="r4_new_file_missing_apache",
            match="",
            message="New file missing full Apache-2.0 license notice.",
        )
    return None


def _check_rule_b_modified_upstream(path: Path) -> ComplianceViolation | None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:25]
    header_block = "\n".join(lines)
    if "Copyright 2026 Google LLC" not in header_block:
        return ComplianceViolation(
            file=str(path),
            line=1,
            rule="r4_modified_missing_upstream_copyright",
            match="",
            message="Modified upstream file missing 'Copyright 2026 Google LLC'.",
        )
    if "Modifications copyright 2026 Christian Haeussinger" not in header_block:
        return ComplianceViolation(
            file=str(path),
            line=1,
            rule="r4_modified_missing_modifications_copyright",
            match="",
            message="Modified upstream file missing 'Modifications copyright 2026 Christian Haeussinger'.",
        )
    has_summary = any(
        "Changed for The Rehearsal:" in line and bool(line.split("Changed for The Rehearsal:", 1)[1].strip())
        for line in lines
    )
    if not has_summary:
        return ComplianceViolation(
            file=str(path),
            line=1,
            rule="r4_modified_missing_summary",
            match="",
            message="Modified upstream file missing non-empty 'Changed for The Rehearsal: <summary>'.",
        )
    return None


def _find_git_root(target: Path) -> Path | None:
    resolved_repo = REPO_ROOT.resolve()
    if target == resolved_repo or resolved_repo in target.parents:
        return resolved_repo
    probe_dir = target if target.is_dir() else target.parent
    if not probe_dir.exists():
        return None
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(probe_dir),
        capture_output=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace").strip()
    return Path(raw).resolve() if raw else None


def _parse_git_porcelain_records(
    repo_root: Path,
    pathspec: str | None = None,
) -> list[tuple[str, str, str | None]]:
    cmd = ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"]
    if pathspec:
        cmd.extend(["--", pathspec])
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        stderr_txt = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"git status failed in {repo_root}: {stderr_txt}")
    tokens = proc.stdout.split(b"\0")
    records: list[tuple[str, str, str | None]] = []
    idx = 0
    while idx < len(tokens):
        raw = tokens[idx]
        idx += 1
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace")
        if len(decoded) < 4:
            continue
        status = decoded[:2]
        rel = decoded[3:]
        orig_rel: str | None = None
        if status[0] in {"R", "C"} and idx < len(tokens):
            orig_raw = tokens[idx].decode("utf-8", errors="replace")
            idx += 1
            orig_rel = orig_raw or None
        records.append((status, rel, orig_rel))
    return records


def scan_r4_license_headers(target_dir: Path) -> list[ComplianceViolation]:
    """Enforce R4 new-file and dual-header Apache-2.0 copyright rules."""
    resolved_target = target_dir.resolve()
    if not resolved_target.exists():
        return []
    violations: list[ComplianceViolation] = []

    git_root = _find_git_root(resolved_target)
    if git_root is not None:
        pathspec = None if resolved_target == git_root else resolved_target.relative_to(git_root).as_posix()
        records = _parse_git_porcelain_records(git_root, pathspec=pathspec)
        seen_paths: set[Path] = set()
        for status, rel, orig_rel in records:
            if "D" in status:
                continue
            candidate = git_root / rel
            if status == "??" and candidate.is_dir():
                files_to_check = list(
                    _iter_files([candidate], skip_dirs=_LICENSE_SKIP_DIRS, allowed_exts=_LICENSE_SOURCE_EXTS)
                )
            elif candidate.is_file():
                rel_parts = Path(rel).parts
                if any(part in _LICENSE_SKIP_DIRS for part in rel_parts):
                    continue
                if candidate.suffix.lower() not in _LICENSE_SOURCE_EXTS:
                    continue
                files_to_check = [candidate]
            else:
                continue

            for fpath in files_to_check:
                if fpath in seen_paths:
                    continue
                seen_paths.add(fpath)
                rel_file = fpath.relative_to(git_root).as_posix()
                head_rel = orig_rel if (orig_rel and status[0] in {"R", "C"}) else rel_file
                if status == "??" or "A" in status:
                    v = _check_rule_a_new_file(fpath)
                    if v is not None:
                        violations.append(v)
                elif _git_head_has_google_llc(git_root, head_rel):
                    v = _check_rule_b_modified_upstream(fpath)
                    if v is not None:
                        violations.append(v)
        return violations

    for fpath in _iter_files([resolved_target], skip_dirs=_LICENSE_SKIP_DIRS, allowed_exts=_LICENSE_SOURCE_EXTS):
        head_block = "\n".join(fpath.read_text(encoding="utf-8", errors="replace").splitlines()[:25])
        if "Modifications copyright 2026 Christian Haeussinger" in head_block:
            v = _check_rule_b_modified_upstream(fpath)
        else:
            v = _check_rule_a_new_file(fpath)
        if v is not None:
            violations.append(v)

    return violations


_RE_COLLECTIVE_AUTHOR_LABEL = re.compile(
    r"\b(?:Authors?|Team|Collective|Editorial|Group|Press)\b",
    re.IGNORECASE,
)
_RE_PERSONAL_BY_PHRASE = re.compile(
    rf"\bby\s+(?:{_BOOK_HONORIFIC}\s+)?(?:[A-Z]\.|{_BOOK_NAME_WORD})(?:\s+(?:[A-Z]\.?|{_BOOK_NAME_WORD}))+"
)
_COLLECTIVE_NON_PERSON_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "of",
        "in",
        "on",
        "to",
        "by",
        "a",
        "an",
        "confidently",
        "wrong",
        "rehearsal",
        "leadership",
        "simulation",
        "author",
        "authors",
        "team",
        "collective",
        "editorial",
        "group",
        "press",
        "book",
        "books",
        "project",
        "companion",
        "anonymous",
        "staff",
        "contributors",
        "maintainers",
        "creators",
        "writers",
        "editors",
        "publishing",
        "publications",
        "edition",
        "editions",
        "executive",
        "management",
        "governance",
        "software",
        "engineering",
        "development",
        "documentation",
        "company",
        "organization",
        "organisation",
        "product",
        "platform",
        "system",
        "systems",
        "core",
        "studio",
        "academy",
        "research",
        "lab",
        "labs",
        "institute",
        "foundation",
        "center",
        "centre",
        "society",
        "association",
        "consortium",
        "community",
        "network",
        "committee",
        "board",
        "working",
        "open",
        "source",
    }
)
_RE_ADJACENT_CAPITALIZED_WORDS = re.compile(
    rf"(?=\b({_BOOK_NAME_WORD}|[A-Z]\.)(?:'s)?\s+(?:[A-Z]\.?\s+)*({_BOOK_NAME_WORD})\b)"
)
_RE_HONORIFIC_PERSON = re.compile(
    rf"\b{_BOOK_HONORIFIC}\s+(?:[A-Z]\.?\s+)*({_BOOK_NAME_WORD})\b"
)
_RE_POSSESSIVE_WORD = re.compile(rf"\b({_BOOK_NAME_WORD})'s\b")


def _has_personal_name_pair(text: str) -> bool:
    for m_hon in _RE_HONORIFIC_PERSON.finditer(text):
        if m_hon.group(1).lower() not in _COLLECTIVE_NON_PERSON_WORDS:
            return True
    for m_poss in _RE_POSSESSIVE_WORD.finditer(text):
        if m_poss.group(1).lower() not in _COLLECTIVE_NON_PERSON_WORDS:
            return True
    for match in _RE_ADJACENT_CAPITALIZED_WORDS.finditer(text):
        first = match.group(1).lower()
        second = match.group(2).lower()
        if first not in _COLLECTIVE_NON_PERSON_WORDS and second not in _COLLECTIVE_NON_PERSON_WORDS:
            return True
    return False


def _strip_yaml_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or value[idx - 1].isspace():
                return value[:idx].rstrip()
    return value.strip()


def _split_yaml_inline_list(inner: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    brace_depth = 0
    for ch in inner:
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif not in_single and not in_double:
            if ch in {"{", "["}:
                brace_depth += 1
                buf.append(ch)
            elif ch in {"}", "]"}:
                brace_depth = max(0, brace_depth - 1)
                buf.append(ch)
            elif ch == "," and brace_depth == 0:
                part = "".join(buf).strip()
                if part:
                    items.append(part)
                buf.clear()
            else:
                buf.append(ch)
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _split_author_list(label: str) -> list[str]:
    """Split a collective/author list on ',', ';', '&', or 'and' outside parens/double quotes."""
    items: list[str] = []
    buf: list[str] = []
    paren_depth = 0
    in_double = False
    n = len(label)
    idx = 0
    while idx < n:
        ch = label[idx]
        if ch == '"':
            in_double = not in_double
            buf.append(ch)
            idx += 1
            continue
        if not in_double:
            if ch == "(":
                paren_depth += 1
                buf.append(ch)
                idx += 1
                continue
            if ch == ")":
                paren_depth = max(0, paren_depth - 1)
                buf.append(ch)
                idx += 1
                continue
            if paren_depth == 0:
                if ch in {",", ";", "&"}:
                    part = "".join(buf).strip()
                    if part:
                        items.append(part)
                    buf.clear()
                    idx += 1
                    continue
                if (
                    label[idx : idx + 3].lower() == "and"
                    and (idx == 0 or not label[idx - 1].isalnum())
                    and (idx + 3 == n or not label[idx + 3].isalnum())
                ):
                    part = "".join(buf).strip()
                    if part:
                        items.append(part)
                    buf.clear()
                    idx += 3
                    continue
        buf.append(ch)
        idx += 1
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _is_approved_collective_author_single(label: str) -> bool:
    cleaned = label.strip().strip("\"'").strip()
    if not cleaned:
        return True
    if re.search(r"Haeussinger|Christian", cleaned, re.IGNORECASE):
        return False
    if _RE_BANNED_COAUTHOR.search(cleaned):
        return False
    if _RE_PERSONAL_BY_PHRASE.search(cleaned):
        return False
    if _has_personal_name_pair(cleaned):
        return False
    return bool(_RE_COLLECTIVE_AUTHOR_LABEL.search(cleaned))


def _is_approved_collective_author(label: str) -> bool:
    cleaned = label.strip().strip("\"'").strip()
    if not cleaned:
        return True
    if re.search(r"Haeussinger|Christian", cleaned, re.IGNORECASE):
        return False
    if _RE_BANNED_COAUTHOR.search(cleaned):
        return False
    if _RE_PERSONAL_BY_PHRASE.search(cleaned):
        return False
    items = _split_author_list(cleaned)
    if not items:
        return True
    if len(items) == 1:
        return _is_approved_collective_author_single(items[0])
    return all(_is_approved_collective_author_single(item) for item in items)


def _is_personal_book_attribution(match: re.Match[str]) -> bool:
    candidate = match.group("subject") or match.group("author")
    if candidate is None:
        return True
    if re.fullmatch(r"(?i)(?:the\s+)?confidently\s+wrong", candidate.strip().strip("\"'").strip()):
        return False
    return not _is_approved_collective_author(candidate)


def _is_flow_mapping_personal_author(flow_item: str) -> bool:
    if re.search(r"\b(?:family-names|given-names)\s*:", flow_item, re.IGNORECASE):
        return True
    m_name = re.search(
        r"""(?:^|[{,\s])name\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^,}]+)""",
        flow_item,
        re.IGNORECASE,
    )
    if not m_name:
        return True
    return not _is_approved_collective_author(m_name.group(1))


def _scan_citation_cff_book_refs(path: Path) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    entries: list[list[tuple[int, str]]] = []
    current_entry: list[tuple[int, str]] | None = None
    in_references = False
    ref_item_indent: int | None = None

    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if indent == 0 and not stripped.startswith("-"):
            if current_entry is not None:
                entries.append(current_entry)
                current_entry = None
            in_references = stripped.startswith("references:")
            ref_item_indent = None
            continue
        if not in_references:
            continue
        if stripped.startswith("- ") and (ref_item_indent is None or indent <= ref_item_indent):
            ref_item_indent = indent
            if current_entry is not None:
                entries.append(current_entry)
            current_entry = [(lineno, line)]
        elif current_entry is not None:
            current_entry.append((lineno, line))

    if current_entry is not None:
        entries.append(current_entry)

    for entry in entries:
        is_book = False
        mentions_book_title = False
        for _, entry_line in entry:
            entry_stripped = _strip_yaml_inline_comment(entry_line.strip())
            if re.match(r"^(?:-\s+)?type:\s*[\"']?book[\"']?$", entry_stripped, re.IGNORECASE):
                is_book = True
            if re.search(r"\bConfidently\s+Wrong\b", entry_stripped, re.IGNORECASE):
                mentions_book_title = True

        if not (is_book and mentions_book_title):
            continue

        in_book_authors = False
        authors_indent = 0
        for lineno, line in entry:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(" \t"))
            m_authors = re.match(r"^(\s*(?:-\s+)?)authors:\s*(.*)$", line)
            if m_authors:
                authors_indent = len(m_authors.group(1))
                inline_val = _strip_yaml_inline_comment(m_authors.group(2))
                if inline_val:
                    has_personal = False
                    if inline_val.startswith("[") and inline_val.endswith("]"):
                        for item in _split_yaml_inline_list(inline_val[1:-1]):
                            if item.startswith("{") and item.endswith("}"):
                                if _is_flow_mapping_personal_author(item):
                                    has_personal = True
                                    break
                            elif not _is_approved_collective_author(item):
                                has_personal = True
                                break
                    elif inline_val.startswith("{") and inline_val.endswith("}"):
                        has_personal = _is_flow_mapping_personal_author(inline_val)
                    elif not _is_approved_collective_author(inline_val):
                        has_personal = True

                    if has_personal:
                        violations.append(
                            ComplianceViolation(
                                file=str(path),
                                line=lineno,
                                rule="book_author_ref_personal_name",
                                match=stripped,
                                message="Personal author name in CITATION.cff book reference.",
                            )
                        )
                    in_book_authors = False
                else:
                    in_book_authors = True
                continue

            if in_book_authors:
                if indent <= authors_indent and not stripped.startswith("-"):
                    in_book_authors = False
                    continue

                is_list_item = stripped.startswith("-")
                item_body = _strip_yaml_inline_comment(stripped[1:].strip() if is_list_item else stripped)
                if not item_body:
                    continue

                if is_list_item and item_body.startswith("{") and item_body.endswith("}"):
                    if _is_flow_mapping_personal_author(item_body):
                        violations.append(
                            ComplianceViolation(
                                file=str(path),
                                line=lineno,
                                rule="book_author_ref_personal_name",
                                match=stripped,
                                message="Personal author name in CITATION.cff book reference.",
                            )
                        )
                elif re.match(r"^(?:family-names|given-names)\s*:", item_body, re.IGNORECASE):
                    violations.append(
                        ComplianceViolation(
                            file=str(path),
                            line=lineno,
                            rule="book_author_ref_personal_name",
                            match=stripped,
                            message="Personal author name in CITATION.cff book reference.",
                        )
                    )
                elif (m_name := re.match(r"^name\s*:\s*(.*)$", item_body, re.IGNORECASE)) is not None:
                    if not _is_approved_collective_author(m_name.group(1)):
                        violations.append(
                            ComplianceViolation(
                                file=str(path),
                                line=lineno,
                                rule="book_author_ref_personal_name",
                                match=stripped,
                                message="Personal author name in CITATION.cff book reference.",
                            )
                        )
                elif is_list_item and not re.match(r"^[a-zA-Z_-]+\s*:", item_body):
                    if not _is_approved_collective_author(item_body):
                        violations.append(
                            ComplianceViolation(
                                file=str(path),
                                line=lineno,
                                rule="book_author_ref_personal_name",
                                match=stripped,
                                message="Personal author name in CITATION.cff book reference.",
                            )
                        )

    return violations


def scan_book_author_refs(repo_root: Path = REPO_ROOT) -> list[ComplianceViolation]:
    """Verify zero banned co-author names and zero personal authorship on Confidently Wrong."""
    violations: list[ComplianceViolation] = []
    is_single_file = repo_root.is_file()
    candidates = [repo_root] if is_single_file else sorted(repo_root.rglob("*"))
    for item in candidates:
        if not item.is_file():
            continue
        if not is_single_file:
            rel_parts = item.relative_to(repo_root).parts
            if any(part in _BOOK_SKIP_DIRS for part in rel_parts):
                continue
        if item.suffix.lower() not in _BOOK_TEXT_EXTS and item.name not in _BOOK_TEXT_NAMES:
            continue

        text = item.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()

        for lineno, line_text in enumerate(lines, start=1):
            m_ban = _RE_BANNED_COAUTHOR.search(line_text)
            if m_ban:
                violations.append(
                    ComplianceViolation(
                        file=str(item),
                        line=lineno,
                        rule="book_author_ref_banned_name",
                        match=m_ban.group(0),
                        message="Prohibited co-author surname in repository file.",
                    )
                )

            for m_by in _RE_BOOK_BY_PERSON.finditer(line_text):
                if not _is_approved_collective_author(m_by.group("by_person")):
                    violations.append(
                        ComplianceViolation(
                            file=str(item),
                            line=lineno,
                            rule="book_author_ref_personal_name",
                            match=m_by.group(0),
                            message="Personal author attribution attached to 'Confidently Wrong'.",
                        )
                    )

            for m_auth in _RE_BOOK_AUTHOR_OF.finditer(line_text):
                if _is_personal_book_attribution(m_auth):
                    violations.append(
                        ComplianceViolation(
                            file=str(item),
                            line=lineno,
                            rule="book_author_ref_personal_name",
                            match=m_auth.group(0),
                            message="Personal author attribution attached to 'Confidently Wrong'.",
                        )
                    )

        if item.name == "CITATION.cff":
            violations.extend(_scan_citation_cff_book_refs(item))

    return violations


def check_ollama_11434_listening() -> bool:
    """Non-intrusively verify host Ollama is listening on TCP port 11434."""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, 11434), timeout=1.0):
                return True
        except OSError:
            continue
    return False


def _is_protected_rel_path(rel_path: str) -> bool:
    norm = rel_path.lstrip("./")
    if norm == "docs" or norm.startswith("docs/"):
        return True
    p = Path(norm)
    if p.name in _PROTECTED_ROOT_FILES and len(p.parts) == 1:
        return True
    if p.suffix.lower() in _PROTECTED_SUFFIXES:
        return True
    return False


def check_protected_files_untouched(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return any modified or untracked protected paths in git status."""
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=str(repo_root),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        stderr_txt = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"git status failed in {repo_root}: {stderr_txt}")

    tokens = proc.stdout.split(b"\0")
    touched: list[str] = []
    idx = 0
    while idx < len(tokens):
        raw = tokens[idx]
        idx += 1
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace")
        if len(decoded) < 4:
            continue
        status = decoded[:2]
        rel = decoded[3:]
        if _is_protected_rel_path(rel):
            touched.append(rel)
        if status[0] in {"R", "C"} and idx < len(tokens):
            orig = tokens[idx].decode("utf-8", errors="replace")
            idx += 1
            if orig and _is_protected_rel_path(orig):
                touched.append(orig)
    return sorted(set(touched))
