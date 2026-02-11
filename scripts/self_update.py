"""
SAIVerse Self-Updater

Spawned as a detached process by the backend API.
Reads .update_config.json, waits for old processes to exit,
updates code + dependencies, then restarts the application.

This script MUST be self-contained (no imports from saiverse package)
because the package code may be overwritten during update.
"""

import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# --- Logging setup ---
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def setup_logging(project_dir: str) -> None:
    log_path = os.path.join(project_dir, "self_update.log")
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# --- Process management ---

def find_pid_for_port(port: int) -> list:
    """Find PIDs using the given port. Returns list of PIDs."""
    pids = []
    try:
        import psutil
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == "LISTEN":
                if conn.pid and conn.pid not in pids:
                    pids.append(conn.pid)
        return pids
    except ImportError:
        pass

    # Fallback: use netstat/lsof
    if sys.platform == "win32":
        try:
            output = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                text=True, timeout=10
            )
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 5 and f":{port}" in parts[1] and "LISTENING" in parts[3]:
                    pid = int(parts[4])
                    if pid not in pids:
                        pids.append(pid)
        except (subprocess.SubprocessError, ValueError):
            pass
    else:
        try:
            output = subprocess.check_output(
                ["lsof", "-ti", f":{port}"],
                text=True, timeout=10
            ).strip()
            for line in output.splitlines():
                try:
                    pids.append(int(line.strip()))
                except ValueError:
                    pass
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    return pids


def kill_pid(pid: int) -> None:
    """Kill a process by PID."""
    logging.info("Killing PID %d", pid)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                timeout=10, capture_output=True
            )
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except (subprocess.SubprocessError, ProcessLookupError, PermissionError) as e:
        logging.warning("Failed to kill PID %d: %s", pid, e)


def is_port_free(port: int) -> bool:
    """Check if a port is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_for_port_free(port: int, timeout: int = 60) -> bool:
    """Wait until a port is free, killing processes if needed."""
    start = time.time()
    attempt = 0
    while time.time() - start < timeout:
        if is_port_free(port):
            logging.info("Port %d is free", port)
            return True

        attempt += 1
        if attempt == 5:
            # Force kill after 5 attempts
            pids = find_pid_for_port(port)
            for pid in pids:
                kill_pid(pid)

        time.sleep(1)

    logging.error("Timeout waiting for port %d to be free", port)
    return False


# --- Code update ---

def update_via_git(project_dir: str) -> bool:
    """Update code using git pull."""
    logging.info("Updating code via git pull...")
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        logging.info("git pull stdout: %s", result.stdout)
        if result.returncode != 0:
            logging.error("git pull failed: %s", result.stderr)
            return False
        return True
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logging.error("git pull error: %s", e)
        return False


def update_via_zip(project_dir: str) -> bool:
    """Update code by downloading ZIP from GitHub."""
    repo = "maha0525/SAIVerse"
    branch = "main"
    url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"

    logging.info("Downloading from %s ...", url)
    temp_dir = tempfile.mkdtemp(prefix="saiverse_update_")
    zip_path = os.path.join(temp_dir, "saiverse.zip")

    try:
        req = Request(url, headers={"User-Agent": "SAIVerse-Updater"})
        with urlopen(req, timeout=120) as resp:
            with open(zip_path, "wb") as f:
                f.write(resp.read())
        logging.info("Download complete: %s", zip_path)

        # Extract
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)
        logging.info("Extraction complete")

        # Find extracted directory (SAIVerse-main/)
        extracted = None
        for item in os.listdir(temp_dir):
            item_path = os.path.join(temp_dir, item)
            if os.path.isdir(item_path) and item != "__MACOSX":
                extracted = item_path
                break

        if not extracted:
            logging.error("Could not find extracted directory")
            return False

        # Copy files, skipping protected paths
        protected = {".env", ".venv", "node_modules", ".node", "expansion_data",
                     ".update_config.json", "self_update.log"}
        file_count = 0

        for root, dirs, files in os.walk(extracted):
            # Skip protected directories
            dirs[:] = [d for d in dirs if d not in protected]

            rel_root = os.path.relpath(root, extracted)
            dest_root = os.path.join(project_dir, rel_root) if rel_root != "." else project_dir

            os.makedirs(dest_root, exist_ok=True)

            for fname in files:
                if fname in protected:
                    continue
                src = os.path.join(root, fname)
                dst = os.path.join(dest_root, fname)
                shutil.copy2(src, dst)
                file_count += 1

        logging.info("Copied %d files to project directory", file_count)
        return True

    except (URLError, OSError, zipfile.BadZipFile) as e:
        logging.error("ZIP update failed: %s", e)
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# --- Dependency update ---

def update_dependencies(project_dir: str, venv_python: str) -> None:
    """Run pip install, migrate, import playbooks, npm install."""
    def _run(cmd, label, **kwargs):
        logging.info("Running: %s", label)
        try:
            result = subprocess.run(
                cmd,
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=300,
                **kwargs,
            )
            if result.returncode != 0:
                logging.warning("%s failed (exit %d): %s", label, result.returncode, result.stderr[:500])
            else:
                logging.info("%s completed successfully", label)
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            logging.warning("%s error: %s", label, e)

    # pip install
    _run([venv_python, "-m", "pip", "install", "-r", "requirements.txt"],
         "pip install")

    # Database migration
    db_path = os.path.join(os.path.expanduser("~"), ".saiverse", "user_data", "database", "saiverse.db")
    if os.path.exists(db_path):
        _run([venv_python, "database/migrate.py", "--db", db_path],
             "database migrate")

    # Import playbooks
    _run([venv_python, "scripts/import_all_playbooks.py", "--force"],
         "import playbooks")

    # npm install
    # Check for portable node
    portable_node = os.path.join(project_dir, ".node", "node.exe")
    npm_cmd = shutil.which("npm")
    if not npm_cmd and os.path.exists(portable_node):
        npm_cmd = os.path.join(project_dir, ".node", "npm.cmd" if sys.platform == "win32" else "npm")

    if npm_cmd:
        frontend_dir = os.path.join(project_dir, "frontend")
        _run([npm_cmd, "install"], "npm install", cwd=frontend_dir)
    else:
        logging.warning("npm not found, skipping frontend update")


# --- Restart ---

def restart_application(project_dir: str, city_name: str, plat: str) -> None:
    """Restart the application using start.bat or start.sh."""
    logging.info("Restarting application...")

    if plat == "win32":
        start_script = os.path.join(project_dir, "start.bat")
        if os.path.exists(start_script):
            logging.info("Launching start.bat")
            CREATE_NEW_CONSOLE = 0x00000010
            subprocess.Popen(
                ["cmd", "/c", start_script],
                cwd=project_dir,
                creationflags=CREATE_NEW_CONSOLE,
                close_fds=True,
            )
        else:
            logging.error("start.bat not found at %s", start_script)
    else:
        start_script = os.path.join(project_dir, "start.sh")
        if os.path.exists(start_script):
            logging.info("Launching start.sh %s", city_name)
            subprocess.Popen(
                ["./start.sh", city_name],
                cwd=project_dir,
                start_new_session=True,
                close_fds=True,
            )
        else:
            logging.error("start.sh not found at %s", start_script)


# --- Main ---

def main():
    # Read config
    config_path = Path(__file__).resolve().parent.parent / ".update_config.json"
    if not config_path.exists():
        # Try current directory
        config_path = Path.cwd() / ".update_config.json"
    if not config_path.exists():
        print("ERROR: .update_config.json not found")
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    project_dir = config["project_dir"]
    city_name = config.get("city_name", "city_a")
    backend_port = config.get("backend_port", 8000)
    frontend_port = config.get("frontend_port", 3000)
    main_pid = config.get("main_pid")
    venv_python = config.get("venv_python", sys.executable)
    has_git = config.get("has_git", False)
    plat = config.get("platform", sys.platform)

    setup_logging(project_dir)
    logging.info("=" * 60)
    logging.info("SAIVerse Self-Updater started")
    logging.info("Config: %s", json.dumps(config, indent=2))
    logging.info("=" * 60)

    # Step 1: Wait for backend to shut down
    logging.info("Step 1: Waiting for backend (port %d) to shut down...", backend_port)

    # Give the backend a moment to start its shutdown
    time.sleep(3)

    # Kill main process if still alive
    if main_pid:
        try:
            os.kill(main_pid, 0)  # Check if alive
            logging.info("Main process (PID %d) still alive, waiting...", main_pid)
            time.sleep(5)
            try:
                os.kill(main_pid, 0)
                logging.info("Force killing main process PID %d", main_pid)
                kill_pid(main_pid)
            except (ProcessLookupError, PermissionError):
                pass
        except (ProcessLookupError, PermissionError):
            logging.info("Main process (PID %d) already exited", main_pid)

    if not wait_for_port_free(backend_port, timeout=30):
        logging.error("Backend port %d still in use. Forcing kill.", backend_port)
        for pid in find_pid_for_port(backend_port):
            kill_pid(pid)
        time.sleep(2)

    # Kill frontend too
    logging.info("Stopping frontend (port %d)...", frontend_port)
    frontend_pids = find_pid_for_port(frontend_port)
    for pid in frontend_pids:
        kill_pid(pid)
    if frontend_pids:
        time.sleep(2)

    # Step 2: Update code
    logging.info("Step 2: Updating code...")
    if has_git:
        success = update_via_git(project_dir)
    else:
        success = update_via_zip(project_dir)

    if not success:
        logging.error("Code update failed. Restarting with existing code.")
    else:
        logging.info("Code update successful")

    # Step 3: Update dependencies
    logging.info("Step 3: Updating dependencies...")
    update_dependencies(project_dir, venv_python)

    # Step 4: Restart
    logging.info("Step 4: Restarting application...")
    restart_application(project_dir, city_name, plat)

    # Step 5: Cleanup
    logging.info("Step 5: Cleanup...")
    try:
        config_path.unlink(missing_ok=True)
    except OSError as e:
        logging.warning("Could not remove config file: %s", e)

    logging.info("=" * 60)
    logging.info("Self-update complete!")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
