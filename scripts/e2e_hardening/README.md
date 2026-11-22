# E2E Production Hardening & Compliance Suite (`scripts/e2e_hardening/`)

Opaque-box, non-destructive 4-tier verification suite for the Leadership Simulation platform. It exercises compiled Go binaries, isolated PostgreSQL schemas and Redis namespaces, mock Ollama endpoints, UI builds/typechecks/tests, Obsidian Light / HIG compliance, 100% Single Source of Truth (Zero-Fake-Data), R4 Apache-2.0 license headers, book attribution rules, and workspace/host safety.

## Files

- `__init__.py` — Package marker (ruff-clean).
- `harness.py` — Shared execution and isolation primitives:
  - `REPO_ROOT`, `run_cmd()`
  - `build_go_binary()` (always writes to an external temp directory, never into the workspace)
  - `free_port()` (ephemeral loopback port, guaranteed `!= 11434`)
  - `EphemeralGoServer` (sanitizes env to prevent root `.env` backfill via `godotenv.Load()`, polls `/health` or `/healthz`, cleanly `SIGTERM` -> `SIGKILL` on exit)
  - `MockOllamaServer` (ephemeral `ThreadingHTTPServer` serving `/api/tags`, `/v1/models`, `/api/chat`, `/api/embed`, `/api/embeddings`, `/api/pull` NDJSON with fault-injection flags)
  - `IsolatedPostgresSchema` (sync `psycopg2` and async `asyncpg` context manager probing `E2E_DATABASE_URL` -> `9213` -> `9113`, creating/dropping `e2e_<uuid12>`)
  - `IsolatedRedisNamespace` (probing `E2E_REDIS_URL` -> `9202` -> `9102`, cleaning `*{prefix}*` keys on exit)
  - `get_git_status_snapshot()`, `assert_workspace_preserved()`
- `compliance_scanners.py` — Deterministic repo-wide compliance scanners:
  - `scan_obsidian_extended()` (covers all 9 `obsidian_cli.py` blind spots with comment stripping and 3D viewport/scrollbar/blur false-positive guards)
  - `scan_ssot_zero_fake_data()` (catches UI/backend fake fallbacks and hardcoded dev credentials while excluding seed SQL/Python and tests)
  - `scan_r4_license_headers()` (enforces R4 Rule A on new files and Rule B dual headers on modified upstream files)
  - `scan_book_author_refs()` (bans prohibited co-author surnames and personal authorship on `"Confidently Wrong"` while preserving software attribution)
  - `check_ollama_11434_listening()` (non-intrusive TCP probe, never `lsof -i :11434`)
  - `check_protected_files_untouched()`
- `conftest.py` — Registers `tier1`..`tier4`, `tier4_slow`, and capability markers via `pytest_configure`, plus shared session/function fixtures.
- `test_harness_self_check.py` — Meta-tests for `harness.py` and `compliance_scanners.py`.
- `test_tier4_acceptance.py` — Full Tier 4 Python, Go, UI, Obsidian, SSOT, R4 license, config lint, and safety acceptance gates.
- `run_e2e_hardening.py` / `run_e2e_hardening.sh` — CLI runners.

## CLI Usage

```bash
# Fast harness & compliance scanner self-check
uv run python scripts/e2e_hardening/run_e2e_hardening.py --self-check

# Run a specific tier (1, 2, 3, 4, or all)
./scripts/e2e_hardening/run_e2e_hardening.sh --tier 4 --fast

# Emit a structured JSON summary report
uv run python scripts/e2e_hardening/run_e2e_hardening.py --self-check --json-report /tmp/e2e_self_check.json
```

## Workspace & Host Safety Rules

1. **Zero Workspace Mutation**: Compiled Go binaries and UI `ng build` / `vite build` outputs are written only to temporary directories outside the repository.
2. **Pre-existing Uncommitted Files Preserved**: Never executes `git commit`, `git push`, `git reset`, `git checkout`, `git stash`, or `git clean`.
3. **Protected Files Untouched**: Never modifies `docs/**`, `LICENSE`, `NOTICE`, `CITATION.cff`, or `.pptx`/`.docx`/`.pdf`/`.png`/`.jpg`/`.jpeg`/`.zip`/`.bak`.
4. **Host Ollama Safe**: Never binds, stops, or interferes with host Ollama on port `11434`; all tests use `MockOllamaServer` on ephemeral loopback ports.
