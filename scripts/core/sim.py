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

import argparse
import json
import os
import socket
import subprocess
import sys
import time

# Ensure we are always running from the project root (backend/)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT_DIR)

LOG_DIR = os.path.join(ROOT_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "simulation.log")


def _configure_docker_host():
    """Auto-configure DOCKER_HOST for Colima users if not already set.

    Colima's Docker socket is at ~/.colima/default/docker.sock. When a user
    runs `uv run start` without a DOCKER_HOST prefix this function discovers
    and exports the socket so every subsequent docker / docker-compose
    subprocess call works correctly — no manual prefix needed.
    """
    if "DOCKER_HOST" in os.environ:
        return  # Already set, respect it.
    default_socket = os.path.expanduser("~/.colima/default/docker.sock")
    if os.path.exists(default_socket):
        os.environ["DOCKER_HOST"] = f"unix://{default_socket}"
        print(f"🐳 Docker: Auto-configured Colima socket → {default_socket}")


# Configure Docker host at module level so ALL entry points benefit —
# both `python scripts/core/sim.py <cmd>` (via main()) and `uv run <cmd>`
# (console scripts that call functions directly, bypassing main()).
_configure_docker_host()


def _read_port_slot():
    """Read the current worktree port slot from .port-slot marker file.

    Returns 0 (default) if the marker file does not exist.
    """
    slot_path = os.path.join(ROOT_DIR, ".port-slot")
    try:
        with open(slot_path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _docker_compose_cmd():
    """Build the docker compose command for the current slot.

    For non-zero slots, the override file is a STANDALONE compose file
    (not a merge override) to avoid Docker Compose's list-merging behavior
    with port bindings. It is used INSTEAD OF the base docker-compose.yml.
    """
    override_path = os.path.join(ROOT_DIR, "docker-compose.override.yml")
    slot = _read_port_slot()
    if slot > 0 and os.path.exists(override_path):
        # Standalone compose file for worktree slots
        return ["docker", "compose", "-f", "docker-compose.override.yml"]
    return ["docker", "compose", "-f", "docker-compose.yml"]


def _read_ports_from_env():
    """Read simulation *_PORT values from .env.

    All configured service ports are included so stop() cleans them up properly.
    """
    _EXCLUDED_KEYS = set()
    env_path = os.path.join(ROOT_DIR, ".env")
    ports = []
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.endswith("_PORT") and key not in _EXCLUDED_KEYS:
                    try:
                        ports.append(int(value))
                    except ValueError:
                        pass
    except FileNotFoundError:
        ports = list(range(9100, 9126))
    return ports


def _read_redis_port_from_env():
    """Read the Redis port from the local .env file.

    Returns the port number (default 9102 if not found).
    """
    env_path = os.path.join(ROOT_DIR, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("REDIS_ADDR="):
                    _, addr = line.split("=", 1)
                    if ":" in addr:
                        return int(addr.rsplit(":", 1)[1])
    except (FileNotFoundError, ValueError):
        pass
    return 9102




def _read_env_file_value(key, default):
    """Read a single value from the local .env file, falling back to default."""
    env_path = os.path.join(ROOT_DIR, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1]
    except FileNotFoundError:
        pass
    return default


def wait_for_infra():
    """Blocks until Redis and PubSub are truly accepting connections."""
    import socket

    # Read from .env file (not process env) so worktree slot ports are used
    redis_env = _read_env_file_value("REDIS_ADDR", "localhost:9102")
    pubsub_env = _read_env_file_value("PUBSUB_EMULATOR_HOST", "localhost:9103")
    pg_port = _read_env_file_value("ALLOYDB_PORT", "9113")

    # Parse addresses
    def parse_addr(env_val, default_port):
        if ":" in env_val:
            host, port = env_val.split(":")
            return (host, int(port))
        return (env_val, default_port)

    redis_addr = parse_addr(redis_env, 9102)
    pubsub_addr = parse_addr(pubsub_env, 9103)
    pg_addr = parse_addr(f"localhost:{pg_port}", 9113)

    print("⏳ Waiting for infrastructure readiness...")
    for addr in [redis_addr, pubsub_addr, pg_addr]:
        start_time = time.time()
        timeout = 30
        connected = False
        while time.time() - start_time < timeout:
            try:
                with socket.create_connection(addr, timeout=1):
                    connected = True
                    break
            except (socket.timeout, ConnectionRefusedError):
                time.sleep(1)

        if not connected:
            print(f"❌ Timeout waiting for {addr[0]}:{addr[1]}")
            sys.exit(1)

        if addr == pg_addr:
            import subprocess
            for _ in range(30):
                try:
                    result = subprocess.run(
                        ["docker", "exec", "leadership-sim-postgres", "pg_isready", "-U", "postgres"],
                        capture_output=True, timeout=5
                    )
                    if result.returncode == 0:
                        break
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass
                time.sleep(1)

        print(f"  ✅ {addr[0]}:{addr[1]} is online.")


def preflight_infra():
    """Ensures Docker infrastructure is ready before starting simulation."""
    # DOCKER_HOST is already configured by _configure_docker_host() at import.
    # Retry docker info because the daemon can be temporarily unresponsive
    # after docker-compose down (especially on Colima which runs in a VM).
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            subprocess.run(["docker", "info"], capture_output=True, check=True)
            print("🐳 Docker: Daemon is reachable and responsive.")
            break
        except (subprocess.CalledProcessError, FileNotFoundError):
            if attempt < max_attempts:
                print(f"  ⏳ Docker daemon not ready (attempt {attempt}/{max_attempts}), retrying in 2s...")
                time.sleep(2)
            else:
                print("❌ Error: Docker daemon is not responding.")
                print("   If using Colima, run: colima start")
                print("   Otherwise ensure Docker Desktop is running.")
                sys.exit(1)

    # 1. Ensure images are present (pull from host side where DNS is reliable)
    images = ["redis:7-alpine", "gcr.io/google.com/cloudsdktool/cloud-sdk:568.0.0"]
    print("📥 Checking/Pulling infrastructure images...")
    for img in images:
        res = subprocess.run(["docker", "image", "inspect", img], capture_output=True)
        if res.returncode != 0:
            print(f"  ☁️ Pulling {img} from host context...")
            subprocess.run(["docker", "pull", img])

    # 2. Prune stopped containers and dangling networks from previous sessions.
    # This prevents name conflicts (containers) and network limit exhaustion.
    # Note: container prune removes ALL stopped containers system-wide.
    # Network prune is scoped to this compose project via label filter.
    print("🧹 Pruning stopped containers and dangling networks...")
    result = subprocess.run(
        ["docker", "container", "prune", "-f"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ⚠️  Container prune failed: {result.stderr.strip()}")
    result = subprocess.run(
        ["docker", "network", "prune", "-f", "--filter", "label=com.docker.compose.project"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ⚠️  Network prune failed: {result.stderr.strip()}")

    # 3. Start infrastructure independently to avoid honcho cascade
    print("🏗️ Starting infrastructure (Redis/PubSub)...")

    # Pre-remove stale containers to prevent name conflicts.
    # docker-compose --force-recreate fails if a container with the same name
    # exists from a different compose project (e.g., a previous worktree run
    # or manual docker-compose invocation). Silently removing them first
    # ensures a clean start.
    slot = _read_port_slot()
    stale_containers = [f"redis-slot-{slot}", f"pubsub-slot-{slot}"]
    for name in stale_containers:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    # Pre-build the local Postgres image to avoid timeout during compose up
    print("🔨 Pre-building local Postgres image...")
    try:
        subprocess.run(
            ["docker", "compose", "build", "postgres"],
            capture_output=True, check=True, timeout=120
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"   ⚠️ Postgres image pre-build failed: {e}")

    compose_cmd = _docker_compose_cmd()
    result = subprocess.run(
        compose_cmd + ["up", "-d", "--force-recreate", "--remove-orphans"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"⚠️  docker-compose up failed: {result.stderr.strip()}")
        print("   Retrying after container cleanup...")
        for name in stale_containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(
            compose_cmd + ["up", "-d", "--remove-orphans"],
            check=True,
        )

    # 4. WAIT for it to be ready
    wait_for_infra()


def preflight_ui(force_install=False):
    """Ensures all frontend components are built before starting simulation."""
    # NOTE: "frontend" hosts the Angular app launched by honcho as
    # frontend-app.1 (`ng serve`). It needs its node_modules installed so the
    # locally-resolved `ng` binary is present; we do NOT build it (ng serve
    # builds in-process). For the static dashboards we both install and build.
    projects = ["admin-dash", "tester", "agent-dash", "frontend"]
    no_build_projects = {"frontend"}
    print("🎨 [UI] Checking and building frontend components...")

    for project in projects:
        project_path = os.path.join("web", project)
        if not os.path.exists(project_path):
            continue

        pkg_json = os.path.join(project_path, "package.json")
        if not os.path.exists(pkg_json):
            continue

        # 1. npm install if node_modules missing or forced
        node_modules = os.path.join(project_path, "node_modules")
        if not os.path.exists(node_modules) or force_install:
            print(f"  📦 Installing dependencies for {project}...")
            subprocess.run(["npm", "install"], cwd=project_path, check=True)

        # 2. Build if package.json has a build script and dist is missing


        with open(pkg_json, "r") as f:
            pkg_data = json.load(f)

        if "scripts" in pkg_data and "build" in pkg_data["scripts"] and project not in no_build_projects:
            dist_path = os.path.join(project_path, "dist")
            if not os.path.exists(dist_path) or force_install:
                print(f"  🏗️ Building {project}...")
                subprocess.run(["npm", "run", "build"], cwd=project_path, check=True)

    print("  ✅ All UI components are ready.")


def check_gcp_credentials():
    """Verifies that Google Cloud Application Default Credentials are valid.

    If ADC is unavailable, falls back to GEMINI_API_KEY or GOOGLE_API_KEY
    if present in the environment or in the local .env file.
    """
    print("☁️  Checking Google Cloud credentials...")
    try:
        # We try to print the access token; if it succeeds, ADC is valid
        res = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            check=True,
        )
        if res.returncode == 0:
            print("  ✅ GCP credentials are valid.")
            return
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Fallback: check for GEMINI_API_KEY or GOOGLE_API_KEY in env or .env
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or _read_env_file_value("GEMINI_API_KEY", "")
        or _read_env_file_value("GOOGLE_API_KEY", "")
    )
    if api_key and api_key.strip():
        print("  ℹ️  GCP ADC missing, using GEMINI_API_KEY fallback for local execution.")
        return

    print("\n❌ Error: Google Cloud Application Default Credentials (ADC) are missing or expired.")
    print("👉 Please run: gcloud auth application-default login")
    print("   Or provide GEMINI_API_KEY in your environment or .env file.")
    sys.exit(1)


def init():
    """Initializes the repository and development environment."""
    print("🛠️ Initializing development environment...")

    # 0. Check credentials
    check_gcp_credentials()

    # 1. System Dependency Checks
    deps = ["docker", "npm", "go", "uv"]
    for dep in deps:
        if subprocess.run(["which", dep], capture_output=True).returncode != 0:
            print(f"❌ Error: {dep} is not installed or not in PATH.")
            sys.exit(1)
    print("  ✅ System dependencies found.")

    # 2. Environment Setup
    if not os.path.exists(".env"):
        print("  📝 Creating .env from .env.example...")
        import shutil

        shutil.copy(".env.example", ".env")
    else:
        print("  ✅ .env already exists.")

    # 3. Python Dependencies
    print("  🐍 Syncing Python dependencies...")
    subprocess.run(["uv", "sync"], check=True)

    # 4. UI Dependencies (Force install)
    preflight_ui(force_install=True)

    # 5. Go Dependencies
    print("  🐹 Tidying Go modules...")
    subprocess.run(["go", "mod", "tidy"], check=True)

    # 6. Protobuf Generation
    generate_protos()

    # 7. Infrastructure Preflight (Pull images)
    preflight_infra()

    print("\n✨ Repository initialization complete!")


def _run_honcho_with_logging(cmd, log_path=LOG_FILE):
    """Run a subprocess, teeing its output to both stdout and a logfile.

    Args:
        cmd: Command list to execute via Popen.
        log_path: Path to the logfile. Parent directory is created if needed.
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    with open(log_path, "w") as logfile:
        print(f"📝 Logging to {os.path.relpath(log_path, ROOT_DIR)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            for line in proc.stdout:
                decoded = line.decode("utf-8", errors="replace")
                sys.stdout.write(decoded)
                sys.stdout.flush()
                logfile.write(decoded)
                logfile.flush()
        except KeyboardInterrupt:
            print("\n🛑 Simulation stopped by user.")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return

    rc = proc.wait()
    if rc != 0:
        print(f"❌ Process exited with code {rc}")
        sys.exit(rc)


def start(skip_tests=False, include_slow=False):
    """Starts the simulation using honcho."""
    # 0. Check credentials
    check_gcp_credentials()

    # 1. Infrastructure must be UP
    preflight_infra()

    # 2. UI Components must be BUILT
    preflight_ui(force_install=True)

    if not skip_tests:
        test(include_slow=include_slow)
    slot = _read_port_slot()
    print(f"🚀 Starting simulation via honcho (slot {slot})...")

    _run_honcho_with_logging(["honcho", "start", "-f", "Procfile", "-e", ".env"])


def kill_process_on_port(port: int):
    """Find and terminate processes listening on the given port.

    Skips Colima/Docker infrastructure processes. Handles PermissionError gracefully.
    Escalates termination signal from SIGTERM to SIGKILL if process persists.
    """
    _INFRA_KEYWORDS = {"colima", "lima", "docker", "containerd", "vpnkit"}
    try:
        cmd = f"lsof -ti :{port} -sTCP:LISTEN"
        pids = subprocess.check_output(cmd, shell=True).decode().split()
        for pid in pids:
            try:
                # Use lsof instead of ps to avoid macOS SIP blocks on /bin/ps
                proc_info = subprocess.check_output(
                    ["lsof", "-a", "-p", pid, "-d", "txt", "-Fn"],
                    text=True,
                    timeout=5,
                )
                proc_info = (
                    proc_info.decode("utf-8", errors="replace")
                    if isinstance(proc_info, bytes)
                    else str(proc_info)
                ).strip().lower()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, PermissionError):
                proc_info = ""
            if any(kw in proc_info for kw in _INFRA_KEYWORDS):
                print(f"  Skipping infrastructure process {pid} on port {port}")
                continue
            print(f"  Killing process {pid} on port {port}...")
            try:
                res = subprocess.run(["kill", "-9", pid], capture_output=True)
                if res.returncode != 0:
                    err_msg = (
                        res.stderr.decode(errors="replace").strip()
                        if isinstance(res.stderr, bytes)
                        else str(res.stderr).strip()
                    )
                    print(
                        f"  ⚠️ Warning: Could not terminate process {pid} on port {port}: {err_msg}"
                    )
            except PermissionError:
                print(f"  ⚠️ Permission denied killing process {pid} on port {port}")
    except (subprocess.CalledProcessError, PermissionError):
        pass


def stop():
    """Stops the simulation processes and infrastructure.

    Port list is read dynamically from .env so this works correctly for any
    worktree slot. Infrastructure teardown is skipped for non-zero slots to
    avoid killing shared containers -- each slot manages its own containers
    via docker-compose override.
    """
    print("🛑 Stopping simulation...")

    slot = _read_port_slot()

    # 0. Purge Python bytecode caches (prevents stale .pyc from being served)
    print("🧹 Purging __pycache__ directories...")
    for dirpath, dirnames, _filenames in os.walk(os.path.join(ROOT_DIR, "agents")):
        if "__pycache__" in dirnames:
            cache_dir = os.path.join(dirpath, "__pycache__")
            import shutil

            shutil.rmtree(cache_dir, ignore_errors=True)
    print("  ✅ Bytecode caches cleared.")

    # 1a. Kill agent and honcho processes whose cwd is this worktree (catches rogues on wrong ports)
    my_root = ROOT_DIR
    print(f"Killing agent and honcho processes with cwd {my_root}...")
    for pattern, require_in_cmd in [("agent.py", "agents/"), ("honcho", "")]:
        try:
            ps_out = subprocess.check_output(["pgrep", "-fl", pattern], text=True)
            for line in ps_out.splitlines():
                if require_in_cmd and require_in_cmd not in line:
                    continue
                parts = line.split()
                pid = parts[0]
                # Check if process cwd matches this worktree
                try:
                    cwd = subprocess.check_output(
                        ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                        text=True,
                        timeout=5,
                    )
                    if my_root in cwd:
                        print(f"  Killing {pattern} process {pid} (cwd matches worktree)")
                        subprocess.run(["kill", "-9", pid], capture_output=True)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    # If we can't check cwd and the full path is in the command, kill it
                    if my_root in line:
                        print(f"  Killing {pattern} process {pid} (path in command)")
                        subprocess.run(["kill", "-9", pid], capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    # 1b. Surgical Port Cleanup (catches non-agent processes on our ports)
    # Skip Colima/Lima/Docker infrastructure processes -- killing them
    # destroys the Docker daemon connection and breaks subsequent starts.
    ports = _read_ports_from_env()
    print(f"Cleaning up simulation ports (slot {slot}): {ports}")
    for port in ports:
        kill_process_on_port(port)

    # 2. Flush Redis (sessions + pub/sub backlog)
    flush_redis()

    print("Stopping infrastructure (Redis/PubSub)...")
    compose_cmd = _docker_compose_cmd()
    subprocess.run(compose_cmd + ["down", "--remove-orphans"], capture_output=True)
    # Force-remove containers in case docker-compose down left orphans
    # (can happen when a container was created by a different compose project)
    for name in [f"redis-slot-{slot}", f"pubsub-slot-{slot}"]:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    print("✅ Simulation stopped and infrastructure cleared.")


def flush_redis():
    """Flush all Redis data (sessions, pub/sub backlog, spawn queues).

    Tries redis-cli first, falls back to Python redis client.
    """
    redis_port = _read_redis_port_from_env()
    redis_password = _read_env_file_value("REDIS_PASSWORD", "")
    print(f"🗑️  Flushing Redis on port {redis_port}...")

    auth_arg = f"-a '{redis_password}' " if redis_password else ""
    # Try redis-cli first (fastest)
    result = subprocess.run(
        f"redis-cli -p {redis_port} {auth_arg}flushall",
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and "OK" in result.stdout:
        print(f"  ✅ Redis flushed via redis-cli (port {redis_port})")
        return

    # Fallback: use Python redis client
    try:
        import redis

        r = redis.Redis(host="127.0.0.1", port=redis_port, password=redis_password or None)
        r.flushall()
        r.close()
        print(f"  ✅ Redis flushed via Python client (port {redis_port})")
    except Exception as e:
        print(f"  ⚠️  Could not flush Redis on port {redis_port}: {e}")
        print("     (Redis may not be running — this is OK if you're stopping.)")


def restart(skip_tests=False, include_slow=False):
    """Restarts the simulation."""
    stop()
    start(skip_tests=skip_tests, include_slow=include_slow)


def generate_protos():
    """Generates Protobuf bindings if missing or needed."""
    proto_script = "scripts/core/generate_proto.sh"
    if not os.path.exists(proto_script):
        print(f"⚠️  Warning: {proto_script} not found. Skipping proto generation.")
        return

    print("🔨 Ensuring Protobuf bindings are up to date...")
    try:
        subprocess.run(["bash", proto_script], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Error: Protobuf generation failed: {e}")
        # We don't exit here because older bindings might still work,
        # but Go tests will likely fail later if they are missing.


def test(include_slow=False):
    """Runs all unit tests (Python and Go)."""
    # 0. Check credentials
    check_gcp_credentials()

    # Ensure protos are generated before testing
    generate_protos()

    print("🧪 Running all unit tests...")

    print("\n🐍 [Python] Testing agents and utils...")
    # Always exclude integration tests -- they require the hermetic
    # docker-compose.test.yml stack (see Makefile:test-py-integration).
    pytest_cmd = [".venv/bin/pytest", "agents"]
    marker = "not integration" if include_slow else "not slow and not integration"
    pytest_cmd.extend(["-m", marker])
    py_res = subprocess.run(pytest_cmd, check=False)

    print("\n🐹 [Go] Testing internal packages and gateway...")
    go_res = subprocess.run(["go", "test", "./..."], check=False)

    if py_res.returncode != 0 or go_res.returncode != 0:
        print("\n❌ Unit testing failed.")
        sys.exit(1)
    print("\n✅ All unit tests passed.")


def _start_cli_parser():
    """Build argparse parser for start/restart CLI entry points."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tests", action="store_true", help="Skip pre-launch tests")
    parser.add_argument("--include-slow", action="store_true", help="Include slow-marked tests")
    return parser


def start_cli():
    """CLI entry point for ``uv run start``."""
    args = _start_cli_parser().parse_args()
    start(skip_tests=args.skip_tests, include_slow=args.include_slow)


def stop_cli():
    """CLI entry point for ``uv run stop``."""
    stop()


def status_cli():
    """CLI entry point for ``uv run status``."""
    status()


def restart_cli():
    """CLI entry point for ``uv run restart``."""
    args = _start_cli_parser().parse_args()
    restart(skip_tests=args.skip_tests, include_slow=args.include_slow)


def is_port_in_use(port: int) -> bool:
    """Check if a TCP port is currently accepting connections."""
    if not isinstance(port, int) or not (0 <= port <= 65535):
        return False
    for host, family in [("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)]:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((host, port)) == 0:
                    return True
        except Exception:
            pass
    return False


def status():
    """Reports running state of Docker infrastructure and stack service ports."""
    slot = _read_port_slot()
    print(f"\n📊 Simulation Stack Status (slot {slot})")
    print("=" * 60)

    # 1. Docker infrastructure status (Postgres, Redis, Pub/Sub emulator)
    print("\n🐳 Docker Infrastructure Services:")
    compose_cmd = _docker_compose_cmd()
    try:
        res = subprocess.run(compose_cmd + ["ps"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            print(res.stdout.strip())
        else:
            print("  No containers running or docker compose ps produced no output.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  ⚠️ Could not query docker compose: {e}")

    # 2. Port and Process inspection across configured simulation ports
    print("\n🔌 Simulation Service Ports:")
    ports = _read_ports_from_env()

    # Friendly service name mapping
    default_service_names = {
        9101: "gateway",
        9102: "redis",
        9103: "pubsub",
        9104: "simulator",
        9107: "simulator-with-failure",
        9111: "dash",
        9112: "tester",
        9113: "postgres",
        9114: "leadership_planner",
        9115: "leadership_planner_with_eval",
        9116: "leadership_planner_with_memory",
        9117: "leadership_runner",
        9118: "frontend",
        9119: "frontend-app",
        9120: "leadership_runner_autopilot",
        9121: "admin",
        9122: "billing",
        9123: "ai_echo",
        9124: "ai_echo_reactive",
        9125: "ai_echo_autopilot",
    }

    env_path = os.path.join(ROOT_DIR, ".env")
    port_to_service = dict(default_service_names)
    if os.path.exists(env_path):
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        if k.endswith("_PORT"):
                            try:
                                port_to_service[int(v)] = k.replace("_PORT", "").lower()
                            except ValueError:
                                pass
        except Exception:
            pass

    status_records = {}
    for port in sorted(set(ports)):
        svc_name = port_to_service.get(port, f"port-{port}")
        in_use = is_port_in_use(port)
        pids = []
        if in_use:
            try:
                cmd = f"lsof -ti :{port} -sTCP:LISTEN"
                out = subprocess.check_output(cmd, shell=True, timeout=2).decode().split()
                pids = out
            except Exception:
                pids = []
            status_str = f"ACTIVE (PID: {', '.join(pids)})" if pids else "ACTIVE"
            print(f"  ✅ Port {port:5d} [{svc_name:30s}]: {status_str}")
        else:
            print(f"  ⚪ Port {port:5d} [{svc_name:30s}]: IDLE")
        status_records[port] = {"service": svc_name, "in_use": in_use, "pids": pids}

    print("=" * 60)
    return status_records


def main():
    # No-op if already configured at module level, but kept as a safety net
    # for direct `python scripts/core/sim.py` invocations.
    _configure_docker_host()

    parser = argparse.ArgumentParser(description="Simulation Lifecycle Manager")
    parser.add_argument(
        "command",
        choices=[
            "init",
            "start",
            "stop",
            "restart",
            "status",
            "flush",
            "test",
        ],
        help="Command to execute",
    )
    parser.add_argument("--skip-tests", action="store_true", help="Skip pre-launch tests")
    parser.add_argument("--include-slow", action="store_true", help="Include slow-marked tests")
    args = parser.parse_args()

    if args.command == "init":
        init()
    elif args.command == "start":
        start(skip_tests=args.skip_tests, include_slow=args.include_slow)
    elif args.command == "stop":
        stop()
    elif args.command == "restart":
        restart(skip_tests=args.skip_tests, include_slow=args.include_slow)
    elif args.command == "status":
        status()
    elif args.command == "flush":
        flush_redis()
    elif args.command == "test":
        test(include_slow=args.include_slow)


if __name__ == "__main__":
    main()
