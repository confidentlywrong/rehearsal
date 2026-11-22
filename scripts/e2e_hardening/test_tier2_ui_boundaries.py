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

"""Tier 2 boundary-value, negative, and rejection tests for UI & compliance (Features #16-#20)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx
import pytest

from scripts.e2e_hardening.compliance_scanners import (
    _is_protected_rel_path,
    check_ollama_11434_listening,
    scan_book_author_refs,
    scan_obsidian_extended,
    scan_r4_license_headers,
    scan_ssot_zero_fake_data,
)
from scripts.e2e_hardening.harness import (
    REPO_ROOT,
    EphemeralGoServer,
    IsolatedRedisNamespace,
    assert_workspace_preserved,
    free_port,
    run_cmd,
)

pytestmark = pytest.mark.tier2

_RAW_OBSIDIAN_CLI = os.environ.get("OBSIDIAN_CLI_PATH", "").strip()
OBSIDIAN_CLI_PATH = Path(_RAW_OBSIDIAN_CLI) if _RAW_OBSIDIAN_CLI else None

_BANNED_COAUTHOR = "Gil" + "martin"
_BOOK_TITLE = "Confidently Wrong"
_AUTHOR_FULL = "Christian " + "Haeussinger"


def _extract_tester_click_listener(main_ts: str, element_id: str) -> str:
    add_ev = re.search(
        rf"""getElementById\(\s*["']{re.escape(element_id)}["']\s*\)[\s\S]{{0,120}}?(addEventListener)\s*\([\s\S]{{0,120}}?=>\s*\{{""",
        main_ts,
    )
    assert add_ev is not None, f"Click listener for #{element_id} not found"
    line_start = main_ts.rfind("\n", 0, add_ev.start(1)) + 1
    indent_match = re.match(r"[ \t]*", main_ts[line_start:])
    indent = indent_match.group(0) if indent_match else ""
    rest = main_ts[add_ev.end():]
    end_match = re.search(rf"""\n{re.escape(indent)}\}}\);""", rest)
    assert end_match is not None, f"Closing }}); for #{element_id} listener not found"
    return rest[: end_match.start()]


# ---------------------------------------------------------------------------
# Feature #16 — Boundary & Negative API/Port Contracts (6 tests)
# ---------------------------------------------------------------------------


def test_f16_bva_no_stale_port_8101_anywhere_in_ui_configs_or_sources(repo_root: Path) -> None:
    skip_dirs = {"node_modules", "dist", ".angular", "coverage", "out-tsc"}
    exts = {".ts", ".js", ".html", ".json"}
    hits: list[str] = []
    web_dir = repo_root / "web"
    for path in sorted(web_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        if any(part in skip_dirs for part in path.relative_to(web_dir).parts):
            continue
        if path.name.endswith((".spec.ts", ".spec.js", ".test.ts", ".test.js")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if re.search(r"(?<![\d.])8101(?!\d)", line):
                hits.append(f"{path.relative_to(repo_root)}:{lineno}: {line.strip()}")
    assert hits == [], "Found stale port 8101 in UI sources/configs:\n" + "\n".join(hits)


@pytest.mark.requires_node
def test_f16_bva_billing_redirect_url_rejects_malicious_and_boundary_urls(repo_root: Path) -> None:
    billing_ts = (repo_root / "web/frontend/src/app/usage/billing.service.ts").read_text(encoding="utf-8")
    ts_lib = (repo_root / "web/frontend/node_modules/typescript/lib/typescript.js").resolve()
    stripped_ts = re.sub(r"import\s+\{[^}]*\}\s+from\s+['\"]@angular/core['\"];?", "", billing_ts)
    stripped_ts = stripped_ts.replace("@Injectable({ providedIn: 'root' })", "")

    node_script = (
        "import { createRequire } from 'node:module';\n"
        "import vm from 'node:vm';\n"
        f"const require = createRequire({json.dumps(str(repo_root / 'web/frontend/package.json'))});\n"
        f"const ts = require({json.dumps(str(ts_lib))});\n"
        "globalThis.window = { location: { origin: 'http://127.0.0.1:9118' } };\n"
        f"const src = {json.dumps(stripped_ts)};\n"
        "const out = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });\n"
        "const mod = { exports: {} };\n"
        "vm.runInNewContext(out.outputText, { exports: mod.exports, module: mod, window: globalThis.window, URL, fetch: globalThis.fetch });\n"
        "const fn = mod.exports.isSafeBillingRedirectUrl;\n"
        "const cases = [\n"
        "  ['empty', fn('')],\n"
        "  ['spaces', fn('   ')],\n"
        "  ['null', fn(null)],\n"
        "  ['number', fn(123)],\n"
        "  ['proto_rel', fn('//evil.example/checkout')],\n"
        "  ['js_scheme', fn('javascript:alert(1)')],\n"
        "  ['ctrl_char', fn('https://evil.com/\\x00')],\n"
        "  ['backslash', fn('https://evil.com\\\\path')],\n"
        "  ['http_cross_origin', fn('http://evil.com/path')],\n"
        "  ['https_stripe', fn('https://checkout.stripe.com/c/pay/cs_test')],\n"
        "  ['same_origin_rel', fn('/billing')],\n"
        "];\n"
        "console.log(JSON.stringify(Object.fromEntries(cases)));\n"
    )
    res = run_cmd(
        ["node", "--input-type=module", "-e", node_script],
        cwd=repo_root,
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        timeout=30,
    )
    assert res.returncode == 0, f"Node TS execution failed: {res.stderr}"
    results = json.loads(res.stdout.strip().splitlines()[-1])
    for bad_key in (
        "empty",
        "spaces",
        "null",
        "number",
        "proto_rel",
        "js_scheme",
        "ctrl_char",
        "backslash",
        "http_cross_origin",
    ):
        assert results[bad_key] is False, f"Expected False for {bad_key}, got {results[bad_key]}"
    assert results["https_stripe"] is True
    assert results["same_origin_rel"] is True


@pytest.mark.requires_go
@pytest.mark.requires_redis
def test_f16_bva_admin_env_reset_unauthenticated_and_invalid_target_rejected(
    admin_binary: Path,
    gateway_binary: Path,
    tmp_path: Path,
) -> None:
    ai_cfg = tmp_path / "ai_settings_f16_bva.json"
    ai_cfg.write_text(
        json.dumps({"provider": "cloud_gemini", "endpoint": f"http://127.0.0.1:{free_port()}"}),
        encoding="utf-8",
    )
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        gw_env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "AI_SETTINGS_PATH": str(ai_cfg),
        }
        with EphemeralGoServer(gateway_binary, env=gw_env) as gw:
            admin_env = {
                "GATEWAY_INTERNAL_URL": gw.base_url,
                "GATEWAY_URL": gw.base_url,
                "REDIS_ADDR": redis_ns.addr,
                "REDIS_PASSWORD": redis_ns.password,
            }
            with EphemeralGoServer(admin_binary, env=admin_env) as admin:
                with httpx.Client(base_url=admin.base_url, timeout=5.0) as admin_client:
                    unauth_resp = admin_client.post(
                        "/api/admin/environment/reset",
                        json={"targets": ["sessions"]},
                    )
                    assert unauth_resp.status_code in (401, 403), (
                        f"Unauthenticated admin reset must return 401/403, got {unauth_resp.status_code}: {unauth_resp.text}"
                    )

                    bad_target_resp = admin_client.post(
                        "/api/admin/environment/reset",
                        headers={"X-Local-Dev-Superadmin": "1"},
                        json={"targets": ["nonexistent"]},
                    )
                    assert bad_target_resp.status_code == 400, (
                        f"Invalid reset target must return 400, got {bad_target_resp.status_code}: {bad_target_resp.text}"
                    )


@pytest.mark.requires_node
def test_f16_bva_admin_api_rejects_boundary_user_ids_and_sweep_days(repo_root: Path) -> None:
    api_js_url = (repo_root / "web/admin-dash/api.js").resolve().as_uri()
    script = (
        "let fetchCount = 0;\n"
        "globalThis.window = { location: { origin: 'http://127.0.0.1:9112', hostname: '127.0.0.1' }, ENV: { LOCAL_DEV: '1' } };\n"
        "globalThis.fetch = async (url, init) => {\n"
        "  fetchCount++;\n"
        "  return { ok: true, status: 200, text: async () => JSON.stringify({ purged: 0, older_than_days: 1 }) };\n"
        "};\n"
        f"const {{ adminApi, AdminApiError }} = await import({json.dumps(api_js_url)});\n"
        "const out = {};\n"
        "for (const [label, fn] of [\n"
        "  ['empty_uid', () => adminApi.setUserPlan('', 'pro')],\n"
        "  ['space_uid', () => adminApi.setUserPlan('   ', 'pro')],\n"
        "  ['null_uid', () => adminApi.exportUser(null)],\n"
        "  ['zero_days', () => adminApi.hardDeleteUsers(0)],\n"
        "  ['neg_days', () => adminApi.hardDeleteUsers(-1)],\n"
        "  ['float_days', () => adminApi.hardDeleteUsers(1.5)],\n"
        "  ['nan_days', () => adminApi.hardDeleteUsers(Number.NaN)],\n"
        "]) {\n"
        "  try { await fn(); out[label] = { threw: false }; } catch (e) {\n"
        "    out[label] = { threw: true, isAdminErr: e instanceof AdminApiError, status: e.status };\n"
        "  }\n"
        "}\n"
        "const countBeforeValid = fetchCount;\n"
        "const valid = await adminApi.hardDeleteUsers(1);\n"
        "console.log(JSON.stringify({ out, countBeforeValid, fetchCount, valid }));\n"
    )
    res = run_cmd(
        ["node", "--input-type=module", "-e", script],
        cwd=repo_root,
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        timeout=30,
    )
    assert res.returncode == 0, f"Node api.js BVA failed: {res.stderr}"
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["countBeforeValid"] == 0, f"Invalid args still called fetch(): {data}"
    assert data["fetchCount"] == 1
    assert data["valid"]["older_than_days"] == 1
    for key, entry in data["out"].items():
        assert entry["threw"] is True and entry["isAdminErr"] is True and entry["status"] == 0, (
            f"Boundary rejection failed for {key}: {entry}"
        )


@pytest.mark.requires_node
def test_f16_bva_tester_ws_to_http_and_gateway_url_guards(repo_root: Path) -> None:
    url_ts = (repo_root / "web/tester/src/url.ts").read_text(encoding="utf-8")
    agents_ts = (repo_root / "web/tester/src/agents.ts").read_text(encoding="utf-8")
    ts_lib = (repo_root / "web/tester/node_modules/typescript/lib/typescript.js").resolve()

    script = (
        "import { createRequire } from 'node:module';\n"
        "import vm from 'node:vm';\n"
        f"const require = createRequire({json.dumps(str(repo_root / 'web/tester/package.json'))});\n"
        f"const ts = require({json.dumps(str(ts_lib))});\n"
        "const runTs = (src) => {\n"
        "  const out = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });\n"
        "  const mod = { exports: {} };\n"
        "  vm.runInNewContext(out.outputText, { exports: mod.exports, module: mod, window: { location: { origin: 'http://127.0.0.1:9112' } }, fetch: globalThis.fetch });\n"
        "  return mod.exports;\n"
        "};\n"
        f"const urlMod = runTs({json.dumps(url_ts)});\n"
        f"const agentsMod = runTs({json.dumps(agents_ts)});\n"
        "let emptyThrew = false;\n"
        "try { agentsMod.resolveGatewayHttpBase('   '); } catch { emptyThrew = true; }\n"
        "let protoRelThrew = false;\n"
        "let protoRel = '';\n"
        "try { protoRel = agentsMod.resolveGatewayHttpBase('//evil.com'); } catch { protoRelThrew = true; }\n"
        "console.log(JSON.stringify({\n"
        "  ws: urlMod.wsToHttp('ws://127.0.0.1:9101/ws'),\n"
        "  wss: urlMod.wsToHttp('wss://example.com/ws?token=x'),\n"
        "  rel: agentsMod.resolveGatewayHttpBase('/ws'),\n"
        "  emptyThrew,\n"
        "  protoRelThrew,\n"
        "  protoRel,\n"
        "}));\n"
    )
    res = run_cmd(
        ["node", "--input-type=module", "-e", script],
        cwd=repo_root,
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        timeout=30,
    )
    assert res.returncode == 0, f"Node tester TS BVA failed: {res.stderr}"
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["ws"] == "http://127.0.0.1:9101"
    assert data["wss"] == "https://example.com"
    assert data["rel"] == "http://127.0.0.1:9112"
    assert data["emptyThrew"] is True
    assert data["protoRelThrew"] is True or (
        isinstance(data["protoRel"], str) and not data["protoRel"].startswith("//")
    )


def test_f16_bva_documents_panel_upload_validation_boundaries(repo_root: Path) -> None:
    panel_src = (
        repo_root / "web/frontend/src/app/memory/components/documents-panel.component.ts"
    ).read_text(encoding="utf-8")
    assert "file.size > this.maxUploadBytes()" in panel_src
    assert "this.hasFileError.set(true)" in panel_src
    assert "lowerName.endsWith('.txt')" in panel_src
    assert "lowerName.endsWith('.md')" in panel_src
    assert "lowerName.endsWith('.csv')" in panel_src

    mem_svc = (repo_root / "web/frontend/src/app/memory/memory.service.ts").read_text(encoding="utf-8")
    assert "encodeURIComponent(analysisId)" in mem_svc
    assert "encodeURIComponent(documentId)" in mem_svc


# ---------------------------------------------------------------------------
# Feature #17 — Boundary Lifecycle, Async Race & Error-Path Tests (6 tests)
# ---------------------------------------------------------------------------


def test_f17_bva_ai_engine_on_destroy_prevents_late_timer_and_async_mutations(repo_root: Path) -> None:
    src = (repo_root / "web/frontend/src/app/settings/ai-engine/ai-engine.page.ts").read_text(
        encoding="utf-8"
    )
    on_destroy = re.search(r"ngOnDestroy\s*\(\)\s*:\s*void\s*\{([\s\S]*?)^\s{2}\}", src, re.MULTILINE)
    assert on_destroy is not None
    destroy_body = on_destroy.group(1)
    assert "this.destroyed = true" in destroy_body
    assert "stopPullPolling" in destroy_body
    called_helpers = re.findall(r"this\.([A-Za-z_]\w*)\s*\(", destroy_body)
    helper_has_clear_timeout = False
    for helper in called_helpers:
        hm = re.search(
            rf"^\s{{2}}(?:(?:private|public|protected)\s+)?{re.escape(helper)}\s*\([^)]*\)[^{{]*\{{([\s\S]*?)^\s{{2}}\}}",
            src,
            re.MULTILINE,
        )
        if hm is not None and "clearTimeout" in hm.group(1):
            helper_has_clear_timeout = True
            break
    assert "clearTimeout" in destroy_body or helper_has_clear_timeout, (
        "AiEnginePageComponent.ngOnDestroy (or a helper called by it) must clear savedSuccess timeout handle"
    )

    on_save = re.search(r"async\s+onSave\s*\(\)\s*:\s*Promise<void>\s*\{([\s\S]*?)^\s{2}\}", src, re.MULTILINE)
    assert on_save is not None
    assert "this.destroyed" in on_save.group(1), (
        "AiEnginePageComponent.onSave setTimeout / post-await path must check this.destroyed"
    )


def test_f17_bva_user_menu_poll_init_me_stops_at_max_attempts_boundary(repo_root: Path) -> None:
    src = (repo_root / "web/frontend/src/app/auth/user-menu.component.ts").read_text(encoding="utf-8")
    poll_match = re.search(
        r"^\s{2}(?:(?:private|public|protected)\s+)?(?:async\s+)?pollInitMe\s*\([^)]*\)[^{]*\{([\s\S]*?)^\s{2}\}",
        src,
        re.MULTILINE,
    )
    assert poll_match is not None
    poll_body = poll_match.group(1)
    assert "while (!this.destroyed)" not in poll_body
    assert re.search(r"(?:maxAttempts|MAX_ATTEMPTS|maxRetries|MAX_RETRIES|attempts?\s*<)", poll_body, re.IGNORECASE)
    assert "clearTimeout(this.retryTimer)" in src


def test_f17_bva_billing_page_unmount_during_inflight_fetch_safe(repo_root: Path) -> None:
    src = (repo_root / "web/frontend/src/app/settings/billing/billing.page.ts").read_text(encoding="utf-8")
    assert "ngOnDestroy()" in src or "ngOnDestroy(" in src
    for method_name in ("ngOnInit", "startCheckout", "openPortal"):
        m = re.search(
            rf"async\s+{method_name}\s*\([^)]*\)\s*:\s*Promise<void>\s*\{{([\s\S]*?)^\s{{2}}\}}",
            src,
            re.MULTILINE,
        )
        assert m is not None, f"{method_name} not found in BillingPageComponent"
        assert "this.destroyed" in m.group(1), (
            f"BillingPageComponent.{method_name} must guard post-await state mutations/navigation with this.destroyed"
        )


def test_f17_bva_organizer_run_simulation_failure_does_not_unhandled_reject(repo_root: Path) -> None:
    src = (
        repo_root / "web/frontend/src/app/components/ChatNavPanel/screens/Organizer/organizer.component.ts"
    ).read_text(encoding="utf-8")
    run_sim = re.search(r"async\s+runSimulation\s*\([^)]*\)[^{]*\{([\s\S]*?)^\s{2}\}", src, re.MULTILINE)
    assert run_sim is not None
    body = run_sim.group(1)
    assert "try" in body and "catch" in body
    assert re.search(r"(?:error|Error|errMsg|spawnError)", body)
    ctor_match = re.search(r"constructor\s*\(\)\s*\{([\s\S]*?)^\s{2}\}", src, re.MULTILINE)
    assert ctor_match is not None
    assert re.search(
        r"\.catch\([\s\S]{0,400}?this\.\w*(?:error|err|msg|status)\w*(?:\s*=|\.set\()",
        ctor_match.group(1),
        re.IGNORECASE,
    ), (
        "OrganizerComponent constructor addAgent().catch() must set explicit UI error state"
    )


def test_f17_bva_tester_reset_non_json_502_and_sessions_500_handled_without_crash(repo_root: Path) -> None:
    main_ts = (repo_root / "web/tester/src/main.ts").read_text(encoding="utf-8")
    flush_body = _extract_tester_click_listener(main_ts, "flush-sessions")
    assert "/api/v1/environment/reset" in flush_body
    tail = flush_body.split("/api/v1/environment/reset", 1)[1]
    assert tail.find("resp.ok") != -1 and (
        tail.find("resp.json()") == -1 or tail.find("resp.ok") < tail.find("resp.json()")
    ), "#flush-sessions crashes on non-JSON 502 if resp.json() runs before resp.ok check"

    stop_body = _extract_tester_click_listener(main_ts, "stop-selected")
    assert "targets.length === 0" in stop_body or "targets.length < 1" in stop_body or "!targets.length" in stop_body, (
        "#stop-selected must handle 0 selected sessions cleanly without backend request"
    )

    ws_open = re.search(r"ws\.onopen\s*=\s*async\s*\(\)\s*=>\s*\{([\s\S]*?)\n\s{2}\};", main_ts)
    assert ws_open is not None
    ws_open_body = ws_open.group(1)
    assert "else" in ws_open_body or "!resp.ok" in ws_open_body, (
        "ws.onopen must log error when GET /api/v1/sessions returns 500"
    )


@pytest.mark.requires_node
@pytest.mark.tier4_slow
def test_f17_bva_frontend_tsconfig_app_and_spec_strict_zero_errors(repo_root: Path) -> None:
    fe_dir = repo_root / "web/frontend"
    for cfg in ("tsconfig.app.json", "tsconfig.spec.json"):
        res = run_cmd(["./node_modules/.bin/tsc", "--noEmit", "-p", cfg], cwd=fe_dir, timeout=300)
        assert res.returncode == 0, f"web/frontend {cfg} failed:\n{res.stdout}\n{res.stderr}"

    tester_res = run_cmd(["./node_modules/.bin/tsc", "--noEmit"], cwd=repo_root / "web/tester", timeout=180)
    assert tester_res.returncode == 0, f"web/tester tsc failed:\n{tester_res.stdout}\n{tester_res.stderr}"


# ---------------------------------------------------------------------------
# Feature #18 — Boundary & Synthetic Negative SSOT Probes (6 tests)
# ---------------------------------------------------------------------------


def test_f18_bva_ssot_scanner_rejects_synthetic_ui_fallbacks(tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "docs.ts").write_text("const kinds = ['memo', 'text'];\n", encoding="utf-8")
    (ui / "health.js").write_text("async function f(resp) { if (!resp.ok) return {}; }\n", encoding="utf-8")
    (ui / "ai.ts").write_text("const endpoint = 'http://localhost:11434';\n", encoding="utf-8")
    (ui / "sample.ts").write_text("const ok = isSampleDecision;\n", encoding="utf-8")
    (ui / "card.ts").write_text("const title = 'EL CLASSICO';\n", encoding="utf-8")

    violations = scan_ssot_zero_fake_data([ui])
    assert len(violations) == 5, f"Expected 5 SSOT violations, got {len(violations)}: {violations}"
    assert {v.rule for v in violations} == {
        "ssot_ui_hardcoded_config_fallback",
        "ssot_ui_silent_health_fallback",
        "ssot_ui_synthetic_payload",
    }


def test_f18_bva_ssot_scanner_allows_html_input_placeholder_attr_but_rejects_js_var(tmp_path: Path) -> None:
    ui = tmp_path / "ui_placeholder"
    ui.mkdir()
    (ui / "ok_tpl.ts").write_text(
        'export const tpl = `<input type="text" placeholder="http://localhost:11434" />`;\n',
        encoding="utf-8",
    )
    assert scan_ssot_zero_fake_data([ui]) == []

    (ui / "bad_var.ts").write_text(
        'const endpoint = "http://localhost:11434";\n'
        'const placeholder = "gemma2:9b";\n',
        encoding="utf-8",
    )
    violations = scan_ssot_zero_fake_data([ui])
    assert len(violations) == 2
    assert all(v.rule == "ssot_ui_hardcoded_config_fallback" for v in violations)


def test_f18_bva_ssot_scanner_excludes_tests_and_seeds_only(tmp_path: Path) -> None:
    ui = tmp_path / "ui_excl"
    ui.mkdir()
    (ui / "card.spec.ts").write_text("const x = 'EL CLASSICO';\n", encoding="utf-8")
    (ui / "main.test.js").write_text("if (!resp.ok) return {};\n", encoding="utf-8")
    (ui / "seed_routes.json").write_text('{"title": "EL CLASSICO"}\n', encoding="utf-8")
    assert scan_ssot_zero_fake_data([ui]) == []

    (ui / "service.ts").write_text("const title = 'EL CLASSICO';\n", encoding="utf-8")
    (ui / "main.js").write_text("if (!resp.ok) return {};\n", encoding="utf-8")
    violations = scan_ssot_zero_fake_data([ui])
    assert len(violations) == 2


@pytest.mark.requires_node
def test_f18_bva_admin_dash_api_error_preserves_http_status_and_non_json_body(repo_root: Path) -> None:
    api_js_url = (repo_root / "web/admin-dash/api.js").resolve().as_uri()
    script = (
        "let mode = '502';\n"
        "globalThis.window = { location: { origin: 'http://127.0.0.1:9112', hostname: '127.0.0.1' }, ENV: { LOCAL_DEV: '1' } };\n"
        "globalThis.fetch = async () => {\n"
        "  if (mode === '502') return { ok: false, status: 502, text: async () => 'Bad Gateway' };\n"
        "  if (mode === '503') return { ok: false, status: 503, text: async () => JSON.stringify({ error: 'stripe disabled' }) };\n"
        "  if (mode === '204') return { ok: true, status: 204, text: async () => '' };\n"
        "  throw new TypeError('ECONNREFUSED');\n"
        "};\n"
        f"const {{ adminApi, AdminApiError }} = await import({json.dumps(api_js_url)});\n"
        "const results = {};\n"
        "try { await adminApi.listUsers(); } catch (e) { results.m502 = { status: e.status, message: e.message, isErr: e instanceof AdminApiError }; }\n"
        "mode = '503';\n"
        "try { await adminApi.listUsers(); } catch (e) { results.m503 = { status: e.status, message: e.message, isErr: e instanceof AdminApiError }; }\n"
        "mode = '204';\n"
        "results.m204 = await adminApi.softDeleteUser('u1');\n"
        "mode = 'net';\n"
        "try { await adminApi.listUsers(); } catch (e) { results.net = { status: e.status, message: e.message, isErr: e instanceof AdminApiError }; }\n"
        "console.log(JSON.stringify(results));\n"
    )
    res = run_cmd(
        ["node", "--input-type=module", "-e", script],
        cwd=repo_root,
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        timeout=30,
    )
    assert res.returncode == 0, f"Node api.js error boundary test failed: {res.stderr}"
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert data["m502"]["status"] == 502 and "Bad Gateway" in data["m502"]["message"]
    assert data["m503"]["status"] == 503 and "stripe disabled" in data["m503"]["message"]
    assert data["m204"] is None
    assert data["net"]["status"] == 0 and "ECONNREFUSED" in data["net"]["message"]


@pytest.mark.requires_node
def test_f18_bva_admin_dash_ops_rejects_non_integer_and_zero_sweep_thresholds(repo_root: Path) -> None:
    ops_js_url = (repo_root / "web/admin-dash/ops.js").resolve().as_uri()
    script = (
        f"const {{ renderSweepResult, renderReconcileResult }} = await import({json.dumps(ops_js_url)});\n"
        "console.log(JSON.stringify({\n"
        "  sweepNull: renderSweepResult(null),\n"
        "  sweepNeg: renderSweepResult({ purged: -5, older_than_days: 'NaN', cutoff: '' }),\n"
        "  sweepZero: renderSweepResult({ purged: 0, older_than_days: 30, cutoff: '2026-01-01' }),\n"
        "  reconcileNull: renderReconcileResult(null),\n"
        "}));\n"
    )
    res = run_cmd(
        ["node", "--input-type=module", "-e", script],
        cwd=repo_root,
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        timeout=30,
    )
    assert res.returncode == 0, f"Node ops.js BVA test failed: {res.stderr}"
    data = json.loads(res.stdout.strip().splitlines()[-1])
    assert "No result" in data["sweepNull"]
    assert "No result" in data["reconcileNull"]
    assert "-5" not in data["sweepNeg"] and "NaN" not in data["sweepNeg"]
    assert "0 user(s)" in data["sweepZero"] and "30 day(s)" in data["sweepZero"]


def test_f18_bva_memory_service_rejects_malformed_2xx_json_and_preserves_api_error(repo_root: Path) -> None:
    mem_svc = (repo_root / "web/frontend/src/app/memory/memory.service.ts").read_text(encoding="utf-8")
    assert "new MemoryApiError(xhr.status, 'invalid_json'" in mem_svc
    assert "new MemoryApiError(resp.status, code, detail)" in mem_svc
    assert "new MemoryApiError(xhr.status, code, detail)" in mem_svc


# ---------------------------------------------------------------------------
# Feature #19 — Boundary & Synthetic Negative Obsidian Probes (6 tests)
# ---------------------------------------------------------------------------


def test_f19_bva_obsidian_extended_catches_all_9_blind_spots_in_tmp(tmp_path: Path) -> None:
    ui = tmp_path / "obsidian_bad"
    ui.mkdir()
    (ui / "bad.scss").write_text(
        ".x { color: #00ffcc; background: #1a1a2e; border-radius: 999px; }\n"
        ".y { border-radius: 100px; }\n"
        ".grid-2 { display: grid; }\n",
        encoding="utf-8",
    )
    (ui / "bad.ts").write_text(
        "export const tpl = `\n"
        '  <div class="bg-slate-950" role="tablist">\n'
        '    <button title="x">SHA-256</button>\n'
        "  </div>`;\n"
        "export function act() { window.confirm('x'); }\n"
        "const toastMessage = 'saved';\n",
        encoding="utf-8",
    )
    violations = scan_obsidian_extended([ui])
    rules = {v.rule for v in violations}
    assert rules == {
        "obsidian_non_canonical_hex",
        "obsidian_prohibited_tailwind",
        "obsidian_pill_radius_or_class",
        "obsidian_native_dialog",
        "obsidian_horizontal_tabs",
        "obsidian_grid_two_columns",
        "obsidian_floating_toast",
        "obsidian_native_title_in_ts_js",
        "obsidian_crypto_plumbing_in_ui",
    }


def test_f19_bva_obsidian_extended_scrollbar_9999px_and_blur_100px_not_flagged(tmp_path: Path) -> None:
    ui = tmp_path / "obsidian_fp"
    ui.mkdir()
    (ui / "clean.scss").write_text(
        "::-webkit-scrollbar-thumb { border-radius: 9999px; }\n"
        ".halo { filter: blur(100px); }\n"
        ".sphere-core { background: #141416; }\n",
        encoding="utf-8",
    )
    assert scan_obsidian_extended([ui]) == []


def test_f19_bva_obsidian_extended_comments_and_viewport_shaders_excluded(tmp_path: Path) -> None:
    ui = tmp_path / "web/frontend/src"
    shader_dir = ui / "app/viewport"
    shader_dir.mkdir(parents=True)
    (shader_dir / "shader.ts").write_text("const c = '#00ffcc';\n", encoding="utf-8")
    (ui / "clean.ts").write_text(
        "// We do NOT use window.confirm('x')\n"
        "/* #00ffcc */\n"
        'const tpl = `<!-- <div title="x"></div> -->`;\n',
        encoding="utf-8",
    )
    assert scan_obsidian_extended([ui]) == []


def test_f19_bva_obsidian_extended_ts_method_declaration_confirm_not_flagged_as_dialog(tmp_path: Path) -> None:
    ui = tmp_path / "ts_confirm"
    ui.mkdir()
    (ui / "modal.ts").write_text(
        "export class Sheet {\n"
        "  public confirm(id: string): void { return; }\n"
        "  confirmArrow = (): boolean => true;\n"
        "  run() { return confirm('Delete?'); }\n"
        "}\n",
        encoding="utf-8",
    )
    hits = [v for v in scan_obsidian_extended([ui]) if v.rule == "obsidian_native_dialog"]
    assert len(hits) == 1
    assert hits[0].line == 4


def test_f19_bva_obsidian_cli_strict_rejects_pill_class_in_html(tmp_path: Path) -> None:
    if OBSIDIAN_CLI_PATH is None or not OBSIDIAN_CLI_PATH.is_file():
        pytest.skip("OBSIDIAN_CLI_PATH is not configured in this environment")
    bad_html = tmp_path / "bad.html"
    bad_html.write_text('<div><span class="status-pill">OK</span></div>\n', encoding="utf-8")
    res = run_cmd(
        [sys.executable, str(OBSIDIAN_CLI_PATH), "audit", "--target", str(tmp_path), "--limit", "50", "--strict"],
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert res.returncode == 1, f"Expected obsidian_cli.py --strict exit 1, got {res.returncode}"


def test_f19_bva_obsidian_extended_multiline_title_attr_caught(tmp_path: Path) -> None:
    ui = tmp_path / "multiline_title"
    ui.mkdir()
    (ui / "btn.ts").write_text(
        "export const html = `\n"
        "  <button\n"
        '    type="button"\n'
        '    title="Delete item">X</button>`;\n',
        encoding="utf-8",
    )
    hits = [v for v in scan_obsidian_extended([ui]) if v.rule == "obsidian_native_title_in_ts_js"]
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# Feature #20 — Boundary R4 License, Book Refs, Protected Files & Ollama (6 tests)
# ---------------------------------------------------------------------------


def test_f20_bva_r4_new_file_rejects_google_llc_and_missing_apache(tmp_path: Path) -> None:
    d = tmp_path / "r4_new"
    d.mkdir()
    (d / "new_with_google.py").write_text("# Copyright 2026 Google LLC\nx = 1\n", encoding="utf-8")
    (d / "new_no_header.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (d / "new_valid.py").write_text(
        "# Copyright 2026 Christian Haeussinger\n"
        "#\n"
        '# Licensed under the Apache License, Version 2.0 (the "License");\n'
        "x = 1\n",
        encoding="utf-8",
    )
    violations = scan_r4_license_headers(d)
    by_name = {Path(v.file).name: v.rule for v in violations}
    assert by_name == {
        "new_with_google.py": "r4_new_file_google_llc",
        "new_no_header.ts": "r4_new_file_missing_copyright",
    }


def test_f20_bva_r4_modified_upstream_requires_all_3_dual_header_lines(tmp_path: Path) -> None:
    d = tmp_path / "r4_mod"
    d.mkdir()
    (d / "missing_upstream.ts").write_text(
        "// Modifications copyright 2026 Christian Haeussinger\n"
        "// Changed for The Rehearsal: fix bug.\n",
        encoding="utf-8",
    )
    (d / "missing_summary.ts").write_text(
        "// Copyright 2026 Google LLC\n"
        "// Modifications copyright 2026 Christian Haeussinger\n"
        "// Changed for The Rehearsal:\n",
        encoding="utf-8",
    )
    (d / "valid_mod.ts").write_text(
        "// Copyright 2026 Google LLC\n"
        "// Modifications copyright 2026 Christian Haeussinger\n"
        "// Changed for The Rehearsal: fix bug.\n",
        encoding="utf-8",
    )
    violations = scan_r4_license_headers(d)
    by_name = {Path(v.file).name: v.rule for v in violations}
    assert by_name == {
        "missing_upstream.ts": "r4_modified_missing_upstream_copyright",
        "missing_summary.ts": "r4_modified_missing_summary",
    }


def test_f20_bva_book_refs_rejects_banned_surname_and_personal_book_author_in_tmp(tmp_path: Path) -> None:
    d = tmp_path / "book_bad"
    d.mkdir()
    (d / "notes.md").write_text(
        f"Co-author {_BANNED_COAUTHOR}.\n"
        f"{_BOOK_TITLE} by Jane Doe.\n"
        f"Author of {_BOOK_TITLE}: {_AUTHOR_FULL}.\n",
        encoding="utf-8",
    )
    (d / "CITATION.cff").write_text(
        "cff-version: 1.2.0\n"
        "references:\n"
        "  - type: book\n"
        f'    title: "{_BOOK_TITLE}"\n'
        "    authors:\n"
        "      - family-names: Doe\n"
        "        given-names: Jane\n",
        encoding="utf-8",
    )
    violations = scan_book_author_refs(d)
    assert len(violations) == 5, f"Expected 5 book attribution violations, got {violations}"
    assert any(v.rule == "book_author_ref_banned_name" for v in violations)
    assert any(v.rule == "book_author_ref_personal_name" for v in violations)


def test_f20_bva_book_refs_allows_collective_book_author_and_software_author(tmp_path: Path) -> None:
    d = tmp_path / "book_good"
    d.mkdir()
    (d / "README.md").write_text(
        f'Companion to "{_BOOK_TITLE}" by The {_BOOK_TITLE} Authors.\n'
        f"Software Copyright 2026 {_AUTHOR_FULL}.\n",
        encoding="utf-8",
    )
    (d / "CITATION.cff").write_text(
        "cff-version: 1.2.0\n"
        "authors:\n"
        "  - family-names: Haeussinger\n"
        "    given-names: Christian\n"
        "references:\n"
        "  - type: book\n"
        f'    title: "{_BOOK_TITLE}"\n'
        "    authors:\n"
        f'      - name: "The {_BOOK_TITLE} Authors"\n',
        encoding="utf-8",
    )
    assert scan_book_author_refs(d) == []


def test_f20_bva_protected_file_path_matcher_and_workspace_preservation_negative(
    baseline_git_snapshot: dict[str, str],
    repo_root: Path,
) -> None:
    for p in ("docs/readme.md", "LICENSE", "CITATION.cff", "deck.pptx", "img.png", "archive.zip", "old.bak"):
        assert _is_protected_rel_path(p) is True, f"Expected {p} to be protected"
    for p in ("scripts/e2e_hardening/test.py", "web/frontend/app.ts"):
        assert _is_protected_rel_path(p) is False, f"Expected {p} not to be protected"

    mutated = dict(baseline_git_snapshot)
    mutated["fake_missing_baseline_file_xyz.txt"] = " M"
    with pytest.raises(AssertionError, match="Workspace preservation violation"):
        assert_workspace_preserved(mutated, repo_root=repo_root)


@pytest.mark.requires_go
def test_f20_bva_free_port_and_ephemeral_server_never_touch_ollama_11434(gateway_binary: Path) -> None:
    ports = [free_port() for _ in range(20)]
    assert all(p != 11434 for p in ports)
    with pytest.raises(ValueError, match="11434"):
        EphemeralGoServer(gateway_binary, port=11434)
    assert check_ollama_11434_listening() is True
