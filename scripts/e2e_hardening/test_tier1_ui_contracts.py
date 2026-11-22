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

"""Tier 1 category-partition functional contract tests for UI & compliance (Features #16-#20)."""

from __future__ import annotations

import json
import os
import re
import sys
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
    assert_workspace_preserved,
    free_port,
    run_cmd,
)

pytestmark = pytest.mark.tier1

_RAW_OBSIDIAN_CLI = os.environ.get("OBSIDIAN_CLI_PATH", "").strip()
OBSIDIAN_CLI_PATH = Path(_RAW_OBSIDIAN_CLI) if _RAW_OBSIDIAN_CLI else None

UI_TARGETS: list[Path] = [
    REPO_ROOT / "web/frontend/src",
    REPO_ROOT / "web/admin-dash",
    REPO_ROOT / "web/tester/src",
    REPO_ROOT / "web/agent-dash",
]


def _extract_ts_interface_block(source: str, interface_name: str) -> str:
    match = re.search(rf"export\s+interface\s+{re.escape(interface_name)}\s*\{{([^}}]*)\}}", source)
    assert match is not None, f"Interface {interface_name} not found"
    return match.group(1)


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
# Feature #16 — UI <-> Go API Contracts & Ports (6 tests)
# ---------------------------------------------------------------------------


def _isolated_ai_settings(tmp_path: Path, name: str = "ai_settings.json") -> str:
    cfg = tmp_path / name
    cfg.write_text(
        json.dumps({"provider": "cloud_gemini", "endpoint": f"http://127.0.0.1:{free_port()}"}),
        encoding="utf-8",
    )
    return str(cfg)


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_f16_billing_plan_contract_matches_go_plans_api(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "SAAS_MODE": "on",
            "AI_SETTINGS_PATH": _isolated_ai_settings(tmp_path, "ai_f16_plans.json"),
        }
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=5.0) as client:
                resp = client.get("/api/v1/plans")
            assert resp.status_code == 200, f"GET /api/v1/plans failed: {resp.status_code} {resp.text}"
            payload = resp.json()
            plans = payload.get("plans")
            assert isinstance(plans, list) and len(plans) >= 5
            expected_keys = {"tier", "monthly_token_cap", "max_concurrent_runs", "description"}
            for plan in plans:
                assert set(plan.keys()) == expected_keys, f"Unexpected plan keys: {plan}"
                assert "max_users" not in plan

    billing_svc = (repo_root / "web/frontend/src/app/usage/billing.service.ts").read_text(encoding="utf-8")
    iface_body = _extract_ts_interface_block(billing_svc, "BillingPlan")
    assert "max_users" not in iface_body, "BillingPlan still declares obsolete max_users field"
    for field in ("tier", "monthly_token_cap", "max_concurrent_runs", "description"):
        assert field in iface_body, f"BillingPlan missing field {field}"

    billing_page = (repo_root / "web/frontend/src/app/settings/billing/billing.page.ts").read_text(
        encoding="utf-8"
    )
    assert "max_users" not in billing_page, "billing.page.ts still interpolates obsolete max_users"


@pytest.mark.requires_go
def test_f16_admin_dash_gateway_port_9101_and_bff_reset_route(
    admin_binary: Path,
    repo_root: Path,
) -> None:
    main_js = (repo_root / "web/admin-dash/main.js").read_text(encoding="utf-8")
    assert "8101" not in main_js, "web/admin-dash/main.js still contains stale port 8101"
    assert "/api/admin/environment/reset" in main_js, (
        "web/admin-dash/main.js must call same-origin BFF POST /api/admin/environment/reset"
    )
    assert "/api/v1/environment/reset" not in main_js, (
        "web/admin-dash/main.js must not call Gateway /api/v1/environment/reset directly"
    )

    unbound_gw_url = f"http://127.0.0.1:{free_port()}"
    with EphemeralGoServer(
        admin_binary,
        env={"GATEWAY_INTERNAL_URL": unbound_gw_url, "GATEWAY_URL": unbound_gw_url},
    ) as admin:
        with httpx.Client(base_url=admin.base_url, timeout=5.0) as client:
            resp = client.post(
                "/api/admin/environment/reset",
                headers={"X-Local-Dev-Superadmin": "1"},
                json={"targets": ["sessions"]},
            )
        assert resp.status_code != 404, (
            f"Admin BFF route POST /api/admin/environment/reset is not registered (got 404): {resp.text}"
        )


@pytest.mark.requires_node
def test_f16_admin_dash_superadmin_header_gated_by_local_dev_env(repo_root: Path) -> None:
    api_js_url = (repo_root / "web/admin-dash/api.js").resolve().as_uri()
    script = (
        "const isLocalCase = process.env.TEST_CASE === 'local';\n"
        "const captured = [];\n"
        "globalThis.window = isLocalCase\n"
        "  ? { location: { origin: 'http://127.0.0.1:9112', hostname: '127.0.0.1' }, ENV: { LOCAL_DEV: '1' } }\n"
        "  : { location: { origin: 'https://admin.example.com', hostname: 'admin.example.com' }, ENV: {} };\n"
        "globalThis.fetch = async (url, init) => {\n"
        "  captured.push({ url, headers: init?.headers || {} });\n"
        "  return { ok: true, status: 200, text: async () => JSON.stringify({ users: [] }) };\n"
        "};\n"
        f"const mod = await import({json.dumps(api_js_url)});\n"
        "await mod.adminApi.listUsers();\n"
        "console.log(JSON.stringify(captured[0].headers));\n"
    )

    local_res = run_cmd(
        ["node", "--input-type=module", "-e", script],
        cwd=repo_root,
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "TEST_CASE": "local"},
        timeout=30,
    )
    assert local_res.returncode == 0, f"Node local execution failed: {local_res.stderr}"
    local_headers = json.loads(local_res.stdout.strip().splitlines()[-1])
    assert local_headers.get("X-Local-Dev-Superadmin") == "1", (
        f"Expected X-Local-Dev-Superadmin: 1 in local dev mode, got {local_headers}"
    )

    cloud_res = run_cmd(
        ["node", "--input-type=module", "-e", script],
        cwd=repo_root,
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "TEST_CASE": "cloud"},
        timeout=30,
    )
    assert cloud_res.returncode == 0, f"Node cloud execution failed: {cloud_res.stderr}"
    cloud_headers = json.loads(cloud_res.stdout.strip().splitlines()[-1])
    assert "X-Local-Dev-Superadmin" not in cloud_headers, (
        f"X-Local-Dev-Superadmin must NOT be attached outside local dev, got {cloud_headers}"
    )


def test_f16_tester_vite_proxy_targets_9101_for_api_and_ws(repo_root: Path) -> None:
    vite_cfg = (repo_root / "web/tester/vite.config.ts").read_text(encoding="utf-8")
    assert "8101" not in vite_cfg, "web/tester/vite.config.ts still targets stale port 8101"
    assert re.search(r"""["']/api["']\s*:\s*\{[^}]*9101""", vite_cfg, re.DOTALL), (
        "web/tester/vite.config.ts must proxy /api to canonical Gateway port 9101"
    )
    ws_block = re.search(r"""["']/ws["']\s*:\s*\{([^}]*)\}""", vite_cfg, re.DOTALL)
    assert ws_block is not None, "web/tester/vite.config.ts must configure a /ws proxy"
    assert "9101" in ws_block.group(1), "/ws proxy in web/tester/vite.config.ts must target port 9101"
    assert re.search(r"\bws\s*:\s*true\b", ws_block.group(1)), (
        "/ws proxy in web/tester/vite.config.ts must enable ws: true"
    )


@pytest.mark.requires_go
@pytest.mark.requires_redis
@pytest.mark.requires_postgres
def test_f16_memory_config_contract_matches_documents_panel(
    gateway_binary: Path,
    isolated_pg_schema: IsolatedPostgresSchema,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    with IsolatedRedisNamespace(require_available=True) as redis_ns:
        env = {
            "REDIS_ADDR": redis_ns.addr,
            "REDIS_PASSWORD": redis_ns.password,
            "REDIS_PREFIX": redis_ns.prefix,
            "DATABASE_URL": isolated_pg_schema.search_path_dsn,
            "AI_SETTINGS_PATH": _isolated_ai_settings(tmp_path, "ai_f16_mem.json"),
        }
        with EphemeralGoServer(gateway_binary, env=env) as gw:
            with httpx.Client(base_url=gw.base_url, timeout=5.0) as client:
                resp = client.get("/api/v1/memory/config")
            assert resp.status_code == 200, f"GET /api/v1/memory/config failed: {resp.status_code}"
            cfg = resp.json()
            assert cfg.get("text_readable_kinds") == ["memo", "note", "plan", "postmortem", "transcript"]
            assert isinstance(cfg.get("max_upload_bytes"), int) and cfg["max_upload_bytes"] > 0

    mem_svc = (repo_root / "web/frontend/src/app/memory/memory.service.ts").read_text(encoding="utf-8")
    assert "getMemoryConfig" in mem_svc
    assert "${BASE}/config" in mem_svc or "/api/v1/memory/config" in mem_svc
    docs_panel = (repo_root / "web/frontend/src/app/memory/components/documents-panel.component.ts").read_text(
        encoding="utf-8"
    )
    assert "text_readable_kinds" in docs_panel
    assert "max_upload_bytes" in docs_panel


@pytest.mark.requires_go
@pytest.mark.requires_node
def test_f16_admin_and_tester_config_js_runtime_contract(
    admin_binary: Path,
    tester_binary: Path,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    env = {"GATEWAY_URL": "http://127.0.0.1:9101", "GATEWAY_INTERNAL_URL": "http://127.0.0.1:9101"}
    with EphemeralGoServer(admin_binary, env=env) as admin, EphemeralGoServer(tester_binary, env=env) as tester:
        with httpx.Client(timeout=5.0) as client:
            admin_resp = client.get(f"{admin.base_url}/config.js")
            tester_resp = client.get(f"{tester.base_url}/config.js")

        assert admin_resp.status_code == 200
        assert "javascript" in admin_resp.headers.get("content-type", "").lower()
        assert "GATEWAY_URL" in admin_resp.text
        assert "8101" not in admin_resp.text

        assert tester_resp.status_code == 200
        assert "javascript" in tester_resp.headers.get("content-type", "").lower()
        assert 'VITE_GATEWAY_URL":"/ws"' in tester_resp.text
        assert "8101" not in tester_resp.text

        for name, body in (("admin_config.js", admin_resp.text), ("tester_config.js", tester_resp.text)):
            js_path = tmp_path / name
            js_path.write_text(body, encoding="utf-8")
            chk = run_cmd(["node", "--check", str(js_path)], cwd=repo_root, timeout=30)
            assert chk.returncode == 0, f"node --check failed on {name}: {chk.stderr}"


# ---------------------------------------------------------------------------
# Feature #17 — UI Lifecycle, Operational Bugs & tsconfig.spec.json (6 tests)
# ---------------------------------------------------------------------------


def test_f17_ai_engine_page_lifecycle_and_no_unrendered_toast(repo_root: Path) -> None:
    src = (repo_root / "web/frontend/src/app/settings/ai-engine/ai-engine.page.ts").read_text(
        encoding="utf-8"
    )
    assert "toastMessage" not in src, "ai-engine.page.ts still carries unrendered toastMessage state"
    assert "ngOnDestroy" in src
    assert "clearTimeout" in src, "ai-engine.page.ts must store and clear its savedSuccess timeout in ngOnDestroy"
    assert re.search(r"if\s*\(\s*this\.destroyed\s*\)\s*return", src), (
        "ai-engine.page.ts callbacks must guard against post-destroy execution"
    )


def test_f17_user_menu_poll_init_me_bounded_and_cleans_timer(repo_root: Path) -> None:
    src = (repo_root / "web/frontend/src/app/auth/user-menu.component.ts").read_text(encoding="utf-8")
    poll_match = re.search(
        r"^\s{2}(?:(?:private|public|protected)\s+)?(?:async\s+)?pollInitMe\s*\([^)]*\)[^{]*\{([\s\S]*?)^\s{2}\}",
        src,
        re.MULTILINE,
    )
    assert poll_match is not None, "pollInitMe() not found in user-menu.component.ts"
    poll_body = poll_match.group(1)
    assert "while (!this.destroyed)" not in poll_body, (
        "pollInitMe() must have an explicit max-attempt bound, not an unbounded while (!this.destroyed) loop"
    )
    assert re.search(r"(?:attempt|maxAttempt|retries|MAX_)", poll_body, re.IGNORECASE), (
        "pollInitMe() must track a bounded retry attempt counter"
    )
    assert "clearTimeout(this.retryTimer)" in src
    assert "agentControlsSub?.unsubscribe()" in src


def test_f17_billing_page_implements_on_destroy_and_guards_async(repo_root: Path) -> None:
    src = (repo_root / "web/frontend/src/app/settings/billing/billing.page.ts").read_text(encoding="utf-8")
    assert re.search(r"class\s+BillingPageComponent\s+implements\s+[^{]*\bOnDestroy\b", src), (
        "BillingPageComponent must implement OnDestroy"
    )
    assert "ngOnDestroy()" in src or "ngOnDestroy(" in src
    assert re.search(r"\bdestroyed\s*=\s*true\b", src), "ngOnDestroy must set destroyed = true"
    assert re.search(r"if\s*\(\s*!?\s*this\.destroyed\s*\)", src), (
        "BillingPageComponent async continuations must check this.destroyed before mutating state or redirecting"
    )


def test_f17_organizer_error_handling_and_clean_empty_copy(repo_root: Path) -> None:
    src = (
        repo_root / "web/frontend/src/app/components/ChatNavPanel/screens/Organizer/organizer.component.ts"
    ).read_text(encoding="utf-8")
    assert "context.." not in src, "organizer.component.ts still contains double-period typo 'context..'"
    run_sim_match = re.search(r"async\s+runSimulation\s*\([^)]*\)[^{]*\{([\s\S]*?)^\s{2}\}", src, re.MULTILINE)
    assert run_sim_match is not None, "runSimulation not found in organizer.component.ts"
    run_sim_body = run_sim_match.group(1)
    assert "try" in run_sim_body and "catch" in run_sim_body, (
        "OrganizerComponent.runSimulation must wrap addAgent in try/catch"
    )
    assert re.search(r"(?:error|Error|errMsg|loadError|spawnError)", run_sim_body), (
        "OrganizerComponent.runSimulation catch must set an explicit UI error state"
    )


def test_f17_tester_reactive_log_filter_stop_sessions_and_reset_error_handling(repo_root: Path) -> None:
    main_ts = (repo_root / "web/tester/src/main.ts").read_text(encoding="utf-8")
    assert re.search(r"""logFilterInput\(\)\s*\?\.\s*addEventListener\(\s*["']input["']""", main_ts), (
        "#log-filter in web/tester/src/main.ts must register an 'input' listener to filter existing logs"
    )
    stop_body = _extract_tester_click_listener(main_ts, "stop-selected")
    assert "fetch(" in stop_body, (
        "#stop-selected must invoke backend fetch() rather than only deleting from local activeSessions"
    )
    flush_body = _extract_tester_click_listener(main_ts, "flush-sessions")
    assert "/api/v1/environment/reset" in flush_body
    flush_tail = flush_body.split("/api/v1/environment/reset", 1)[1]
    assert flush_tail.find("resp.ok") != -1 and (
        flush_tail.find("resp.json()") == -1 or flush_tail.find("resp.ok") < flush_tail.find("resp.json()")
    ), "#flush-sessions must check resp.ok before parsing JSON"

    ws_open_match = re.search(r"ws\.onopen\s*=\s*async\s*\(\)\s*=>\s*\{([\s\S]*?)\n\s{2}\};", main_ts)
    assert ws_open_match is not None
    ws_open_body = ws_open_match.group(1)
    assert "else" in ws_open_body or "!resp.ok" in ws_open_body, (
        "ws.onopen /api/v1/sessions fetch must handle !resp.ok explicitly"
    )

    msg_box = (repo_root / "web/tester/src/a2ui/components/messageBox.ts").read_text(encoding="utf-8")
    assert ".catch(" in msg_box, "messageBox.ts clipboard.writeText must attach .catch(...)"

    img_ts = (repo_root / "web/tester/src/a2ui/components/image.ts").read_text(encoding="utf-8")
    assert "object-${" not in img_ts, "image.ts must use a static object-* class map, not dynamic object-${...}"

    a2ui_index = (repo_root / "web/tester/src/a2ui/index.ts").read_text(encoding="utf-8")
    tester_html = (repo_root / "web/tester/index.html").read_text(encoding="utf-8")
    if "material-icons" in a2ui_index:
        assert "Material+Icons" in tester_html or "material-icons" in tester_html


@pytest.mark.requires_node
@pytest.mark.tier4_slow
def test_f17_frontend_tsconfig_spec_and_admin_dash_vite_build_clean(
    tmp_path: Path,
    repo_root: Path,
) -> None:
    fe_dir = repo_root / "web/frontend"
    tsc_spec = run_cmd(
        ["./node_modules/.bin/tsc", "--noEmit", "-p", "tsconfig.spec.json"],
        cwd=fe_dir,
        timeout=300,
    )
    assert tsc_spec.returncode == 0, (
        f"web/frontend tsconfig.spec.json has TS errors:\n{tsc_spec.stdout}\n{tsc_spec.stderr}"
    )

    admin_out = tmp_path / "admin_dist"
    vite_res = run_cmd(
        ["./node_modules/.bin/vite", "build", "--outDir", str(admin_out), "--emptyOutDir"],
        cwd=repo_root / "web/admin-dash",
        timeout=300,
    )
    assert vite_res.returncode == 0, f"admin-dash vite build failed:\n{vite_res.stdout}\n{vite_res.stderr}"
    combined = f"{vite_res.stdout}\n{vite_res.stderr}"
    assert "can't be bundled without type=\"module\"" not in combined, (
        f"admin-dash vite build emitted config.js bundling warning:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Feature #18 — 100% SSOT & Zero-Fake-Data (6 tests)
# ---------------------------------------------------------------------------


def test_f18_ssot_scanner_zero_violations_across_all_4_ui_apps() -> None:
    violations = scan_ssot_zero_fake_data(UI_TARGETS)
    formatted = "\n".join(f"{v.file}:{v.line} [{v.rule}] {v.match}" for v in violations[:40])
    assert violations == [], f"Found {len(violations)} SSOT violations across UI apps:\n{formatted}"


def test_f18_score_card_and_demo_config_no_synthetic_vegas_or_75_defaults(repo_root: Path) -> None:
    score_card = (
        repo_root / "web/frontend/src/app/components/ScoreCard/score-card.component.ts"
    ).read_text(encoding="utf-8")
    for forbidden in ("'75'", ">75<", "EL CLASSICO", "14:38:11 AM", "25/03/26", "#1234"):
        assert forbidden not in score_card, f"ScoreCardComponent still contains synthetic literal {forbidden!r}"
    assert not re.search(r"onShowScoreCard\s*\(\)\s*:\s*void\s*\{\s*\}", score_card)
    assert not re.search(r"onHistoryClick\s*\(\)\s*:\s*void\s*\{\s*\}", score_card)

    demo_cfg = (repo_root / "web/frontend/src/app/demo-config.ts").read_text(encoding="utf-8")
    demo_svc = (
        repo_root
        / "web/frontend/src/app/components/ChatNavPanel/screens/AgentScreen/services/agent-demo-session.service.ts"
    ).read_text(encoding="utf-8")
    for text in (demo_cfg, demo_svc):
        for forbidden in (
            "Vegas Strip Marathon Plan",
            "Neon Strip Run",
            "placeholderRoutes",
            "placeholderAgentMessage",
            "LEADERSHIP_SIM_RESULTS_COMPONENTS",
        ):
            assert forbidden not in text, f"Found forbidden synthetic payload {forbidden!r}"


def test_f18_documents_panel_explicit_error_on_config_failure(repo_root: Path) -> None:
    src = (
        repo_root / "web/frontend/src/app/memory/components/documents-panel.component.ts"
    ).read_text(encoding="utf-8")
    assert not re.search(r"\[\s*['\"]memo['\"]\s*,\s*['\"]text['\"]\s*\]", src), (
        "documents-panel.component.ts still falls back to ['memo', 'text']"
    )
    load_cfg_match = re.search(r"async\s+loadConfig\s*\(\)\s*:\s*Promise<void>\s*\{([\s\S]*?)^\s{2}\}", src, re.MULTILINE)
    assert load_cfg_match is not None
    assert re.search(r"(?:error|Error|unavailable|Unavailable)", load_cfg_match.group(1)), (
        "documents-panel.component.ts loadConfig() must surface an explicit config error state"
    )


def test_f18_ai_engine_page_no_hardcoded_ollama_model_or_url_state(repo_root: Path) -> None:
    src = (repo_root / "web/frontend/src/app/settings/ai-engine/ai-engine.page.ts").read_text(
        encoding="utf-8"
    )
    violations = scan_ssot_zero_fake_data([repo_root / "web/frontend/src/app/settings/ai-engine/ai-engine.page.ts"])
    assert violations == [], f"ai-engine.page.ts has hardcoded Ollama SSOT violations: {violations}"
    assert "endpoint = 'http://localhost:11434'" not in src
    assert "textModel = 'gemma2:9b'" not in src
    assert "embedModel = 'embeddinggemma:latest'" not in src


@pytest.mark.requires_node
def test_f18_admin_dash_health_surfaces_error_instead_of_empty_object(repo_root: Path) -> None:
    main_js_path = repo_root / "web/admin-dash/main.js"
    main_js = main_js_path.read_text(encoding="utf-8")
    assert not re.search(r"if\s*\(\s*!resp\.ok\s*\)\s*return\s*\{\s*\}", main_js), (
        "web/admin-dash/main.js still silently returns {} on !resp.ok"
    )

    main_js_url = main_js_path.resolve().as_uri()
    script = (
        "const elements = {\n"
        "  'global-status': { textContent: '', className: '' },\n"
        "  'last-update': { textContent: '' },\n"
        "  'categories-container': null,\n"
        "};\n"
        "globalThis.window = { location: { hostname: '127.0.0.1', origin: 'http://127.0.0.1:9112' }, addEventListener: () => {} };\n"
        "globalThis.document = {\n"
        "  getElementById: (id) => elements[id] ?? null,\n"
        "  querySelectorAll: () => [],\n"
        "};\n"
        "globalThis.fetch = async (url) => ({\n"
        "  ok: false,\n"
        "  status: 503,\n"
        "  statusText: 'Service Unavailable',\n"
        "  json: async () => ({ error: 'health probe failed' }),\n"
        "  text: async () => JSON.stringify({ error: 'health probe failed' }),\n"
        "});\n"
        f"const mod = await import({json.dumps(main_js_url)});\n"
        "await mod.refreshHealth();\n"
        "console.log(JSON.stringify(elements['global-status']));\n"
    )
    res = run_cmd(
        ["node", "--input-type=module", "-e", script],
        cwd=repo_root,
        env={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        timeout=30,
    )
    assert res.returncode == 0, f"Node refreshHealth execution failed: {res.stderr}"
    status_el = json.loads(res.stdout.strip().splitlines()[-1])
    txt = status_el.get("textContent", "").lower()
    assert any(tok in txt for tok in ("unavailable", "error", "degraded", "failed", "503")), (
        f"Expected explicit error/unavailable banner on 503 health failure, got: {status_el}"
    )


@pytest.mark.requires_node
def test_f18_tester_and_admin_existing_vitest_unit_suites_pass(repo_root: Path) -> None:
    for app_rel in ("web/admin-dash", "web/tester"):
        app_dir = repo_root / app_rel
        res = run_cmd(["./node_modules/.bin/vitest", "run"], cwd=app_dir, timeout=180)
        assert res.returncode == 0, f"{app_rel} vitest run failed:\n{res.stdout}\n{res.stderr}"


# ---------------------------------------------------------------------------
# Feature #19 — Apple HIG / Obsidian Light Compliance (6 tests)
# ---------------------------------------------------------------------------


def test_f19_obsidian_cli_strict_passes_all_4_ui_targets() -> None:
    if OBSIDIAN_CLI_PATH is None or not OBSIDIAN_CLI_PATH.is_file():
        pytest.skip("OBSIDIAN_CLI_PATH is not configured in this environment")
    for target in UI_TARGETS:
        res = run_cmd(
            [sys.executable, str(OBSIDIAN_CLI_PATH), "audit", "--target", str(target), "--limit", "200", "--strict"],
            cwd=REPO_ROOT,
            timeout=120,
        )
        assert res.returncode == 0, f"obsidian_cli.py --strict failed on {target}:\n{res.stdout}\n{res.stderr}"


def test_f19_obsidian_extended_zero_violations_frontend_and_admin() -> None:
    violations = scan_obsidian_extended([REPO_ROOT / "web/frontend/src", REPO_ROOT / "web/admin-dash"])
    formatted = "\n".join(f"{v.file}:{v.line} [{v.rule}] {v.match}" for v in violations[:40])
    assert violations == [], f"Extended Obsidian violations in frontend/admin-dash:\n{formatted}"


def test_f19_obsidian_extended_zero_violations_tester_and_agent_dash() -> None:
    violations = scan_obsidian_extended([REPO_ROOT / "web/tester/src", REPO_ROOT / "web/agent-dash"])
    formatted = "\n".join(f"{v.file}:{v.line} [{v.rule}] {v.match}" for v in violations[:40])
    assert violations == [], f"Extended Obsidian violations in tester/agent-dash:\n{formatted}"


def test_f19_obsidian_light_canonical_palette_present_in_ui_stylesheets(repo_root: Path) -> None:
    targets = [
        repo_root / "web/admin-dash/style.css",
        repo_root / "web/tester/src/index.css",
        repo_root / "web/agent-dash/index.html",
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        upper = text.upper()
        assert any(tok in upper for tok in ("#F5F5F7", "#FFFFFF", "#1D1D1F", "VAR(--OBSIDIAN-")), (
            f"{path} missing canonical Obsidian Light palette tokens"
        )
        for dark_tok in ("#0d1117", "#1a1a2e", "bg-slate-950", "#00ffcc", "#0fffd1"):
            assert dark_tok.lower() not in text.lower(), f"{path} still contains dark/neon token {dark_tok}"


def test_f19_no_native_browser_dialogs_or_title_tooltips_in_ui_templates() -> None:
    violations = [
        v
        for v in scan_obsidian_extended(UI_TARGETS)
        if v.rule in {"obsidian_native_dialog", "obsidian_native_title_in_ts_js"}
    ]
    formatted = "\n".join(f"{v.file}:{v.line} [{v.rule}] {v.match}" for v in violations[:40])
    assert violations == [], f"Found native browser dialog or title= tooltip violations:\n{formatted}"


def test_f19_no_pill_radii_horizontal_tabs_or_floating_toasts_in_ui() -> None:
    rules = {
        "obsidian_pill_radius_or_class",
        "obsidian_horizontal_tabs",
        "obsidian_grid_two_columns",
        "obsidian_floating_toast",
    }
    violations = [v for v in scan_obsidian_extended(UI_TARGETS) if v.rule in rules]
    formatted = "\n".join(f"{v.file}:{v.line} [{v.rule}] {v.match}" for v in violations[:40])
    assert violations == [], f"Found pill/tab/grid-2/toast Obsidian violations:\n{formatted}"


# ---------------------------------------------------------------------------
# Feature #20 — R4 License, Book Refs, 385-File Preservation & Ollama (6 tests)
# ---------------------------------------------------------------------------


def test_f20_r4_license_headers_clean_across_all_4_ui_dirs(repo_root: Path) -> None:
    violations = scan_r4_license_headers(repo_root / "web")
    formatted = "\n".join(f"{v.file}:{v.line} [{v.rule}] {v.message}" for v in violations[:30])
    assert violations == [], f"R4 license header violations in web/:\n{formatted}"


def test_f20_e2e_hardening_own_files_satisfy_r4_and_ruff(repo_root: Path) -> None:
    e2e_violations = scan_r4_license_headers(repo_root / "scripts/e2e_hardening")
    assert e2e_violations == [], f"R4 license violations in scripts/e2e_hardening/: {e2e_violations}"

    ruff_res = run_cmd(["uv", "run", "ruff", "check", "scripts/e2e_hardening/"], cwd=repo_root, timeout=60)
    assert ruff_res.returncode == 0, f"ruff check failed:\n{ruff_res.stdout}\n{ruff_res.stderr}"


def test_f20_book_refs_clean_across_web_and_citation_cff(repo_root: Path) -> None:
    web_hits = scan_book_author_refs(repo_root / "web")
    assert web_hits == [], f"Book author violations in web/: {web_hits}"

    cff_path = repo_root / "CITATION.cff"
    cff_hits = scan_book_author_refs(cff_path)
    assert cff_hits == [], f"Book author violations in CITATION.cff: {cff_hits}"
    cff_text = cff_path.read_text(encoding="utf-8")
    assert "The Confidently Wrong Authors" in cff_text


def test_f20_baseline_385_uncommitted_files_preserved(
    baseline_git_snapshot: dict[str, str],
    repo_root: Path,
) -> None:
    assert len(baseline_git_snapshot) >= 385, (
        f"Expected >= 385 baseline git entries, found {len(baseline_git_snapshot)}"
    )
    assert_workspace_preserved(baseline_git_snapshot, repo_root=repo_root)


def test_f20_protected_files_completely_untouched(repo_root: Path) -> None:
    touched = check_protected_files_untouched(repo_root)
    assert touched == [], f"Protected files were modified or untracked: {touched}"


def test_f20_host_ollama_11434_listening_and_mock_ollama_isolated(mock_ollama: MockOllamaServer) -> None:
    assert check_ollama_11434_listening() is True, "Host Ollama on port 11434 is not listening"
    assert mock_ollama.port != 11434
    with httpx.Client(base_url=mock_ollama.base_url, timeout=3.0) as client:
        resp = client.get("/api/tags")
    assert resp.status_code == 200
    assert len(resp.json().get("models", [])) >= 1
