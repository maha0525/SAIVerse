import logging
import threading
import time
import subprocess
import sys
import os
import json
import argparse
import atexit
import signal
import asyncio
from dotenv import load_dotenv
from typing import Optional
from pathlib import Path

load_dotenv()

# Migrate legacy user_data/ to ~/.saiverse/user_data/ if needed
from saiverse.data_paths import migrate_legacy_user_data
migrate_legacy_user_data()

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    psutil = None  # type: ignore

from saiverse.saiverse_manager import SAIVerseManager
from database.paths import default_db_path
from database.backup import run_startup_backup
from saiverse.model_configs import get_model_choices, get_model_choices_with_display_names
from saiverse import app_state
try:
    from discord_gateway import ensure_gateway_runtime
except ImportError:  # pragma: no cover - optional dependency
    ensure_gateway_runtime = None

# Unity Gateway (optional)
try:
    from unity_gateway import UnityGatewayServer
    UNITY_GATEWAY_AVAILABLE = True
except ImportError:
    UnityGatewayServer = None
    UNITY_GATEWAY_AVAILABLE = False

level_name = os.getenv("SAIVERSE_LOG_LEVEL", "INFO").upper()
if level_name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    level_name = "INFO"
# Configure logging with terminal mirroring and per-startup log files
from saiverse.logging_config import configure_logging
SESSION_LOG_DIR = configure_logging(level_name)
try:
    _chat_limit_env = int(os.getenv("SAIVERSE_CHAT_HISTORY_LIMIT", "120"))
except ValueError:
    _chat_limit_env = 120
CHAT_HISTORY_LIMIT = max(0, _chat_limit_env)
# Build model choices with display names for UI dropdowns
# Format: [(display_name, model_id), ...] - Gradio uses (label, value) order
_model_choices_raw = get_model_choices_with_display_names()
MODEL_CHOICES = [("None", "None")] + [(display_name, model_id) for model_id, display_name in _model_choices_raw]
logging.info("Loaded %d model choices with display names", len(MODEL_CHOICES))
logging.debug("Sample model choices: %s", MODEL_CHOICES[:5])

def _read_version() -> str:
    """Read semantic version from VERSION file, fallback to timestamp."""
    version_file = Path(__file__).parent / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return time.strftime("%Y%m%d%H%M%S")

VERSION = _read_version()



def find_pid_for_port(port: int) -> Optional[int]:
    """指定されたポートを使用しているプロセスのPIDを見つける。"""
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                laddr = getattr(conn, "laddr", None)
                if not laddr:
                    continue
                if laddr.port == port and conn.pid:
                    return conn.pid
        except psutil.AccessDenied:
            logging.debug("psutil could not access connection information (permission denied).")
        except psutil.Error as exc:
            logging.debug("psutil failed while enumerating processes: %s", exc)

    if sys.platform == "win32":
        try:
            result = subprocess.check_output(
                ["netstat", "-ano"],
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for line in result.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = int(line.split()[-1])
                    return pid
        except (subprocess.CalledProcessError, FileNotFoundError):
            logging.error("Could not execute 'netstat' command. Please ensure it is in your PATH.")
        except Exception as exc:
            logging.error("Error finding PID for port %s: %s", port, exc)
        return None

    for cmd in (["lsof", "-ti", f":{port}"], ["fuser", "-n", "tcp", str(port)]):
        try:
            result = subprocess.check_output(cmd, text=True)
            for line in result.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    return int(line.split()[0])
                except ValueError:
                    continue
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError:
            continue
        except Exception as exc:
            logging.debug("Command %s failed while searching for port %s: %s", cmd[0], port, exc)
    return None


def kill_process_by_pid(pid: int):
    """PIDを指定してプロセスを終了させる。"""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                check=True,
                capture_output=True,
            )
            logging.info("Process with PID %s has been terminated.", pid)
            time.sleep(1)
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 128:
                logging.warning("Process with PID %s not found. It might have already been closed.", pid)
            else:
                stderr = exc.stderr.decode(errors="ignore") if exc.stderr else ""
                logging.error("Failed to terminate process with PID %s. Stderr: %s", pid, stderr)
        except Exception as exc:
            logging.error("An unexpected error occurred while killing process %s: %s", pid, exc)
        return

    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)
        os.kill(pid, signal.SIGKILL)
        logging.info("Process with PID %s forcefully terminated.", pid)
    except ProcessLookupError:
        logging.warning("Process with PID %s not found. It might have already been closed.", pid)
    except PermissionError:
        logging.error("Permission denied when trying to terminate PID %s.", pid)
    except Exception as exc:
        logging.error("Failed to terminate process %s: %s", pid, exc)

def cleanup_and_start_server(port: int, script_path: Path, name: str):
    """ポートをクリーンアップし、指定されたスクリプトをモジュールとしてバックグラウンドで起動する"""
    pid = find_pid_for_port(port)
    if pid:
        logging.warning("Port %s for %s is already in use by PID %s. Attempting to terminate the process.", port, name, pid)
        kill_process_by_pid(pid)

    project_root = Path(__file__).parent
    # Convert file path to module path (e.g., database\api_server.py -> database.api_server)
    module_path = str(script_path.relative_to(project_root)).replace(os.sep, '.')[:-3]

    logging.info("Starting %s as module: %s", name, module_path)
    # Run as a module from the project's root directory to handle relative imports correctly
    return subprocess.Popen([sys.executable, "-m", module_path], cwd=project_root)

def cleanup_and_start_server_with_args(port: int, script_path: Path, name: str, db_file: str):
    """ポートをクリーンアップし、引数付きでスクリプトをモジュールとして起動する"""
    pid = find_pid_for_port(port)
    if pid:
        logging.warning("Port %s for %s is already in use by PID %s. Attempting to terminate the process.", port, name, pid)
        kill_process_by_pid(pid)

    project_root = Path(__file__).parent
    module_path = str(script_path.relative_to(project_root)).replace(os.sep, '.')[:-3]

    logging.info("Starting %s as module: %s with DB: %s on port: %s", name, module_path, db_file, port)
    return subprocess.Popen(
        [sys.executable, "-m", module_path, "--port", str(port), "--db", db_file],
        cwd=project_root,
    )


def shutdown_subprocess(process: Optional[subprocess.Popen], name: str) -> None:
    if not process:
        return
    if process.poll() is not None:
        return
    logging.info("Shutting down %s...", name)
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logging.warning("%s did not exit in time; forcing kill.", name)
            process.kill()
    except Exception as exc:
        logging.error("Failed to shut down %s cleanly: %s", name, exc)

api_server_process: Optional[subprocess.Popen] = None
manager: Optional[SAIVerseManager] = None
unity_gateway_task: Optional[asyncio.Task] = None


def main():
    parser = argparse.ArgumentParser(description="Run a SAIVerse City instance.")
    parser.add_argument("city_name", type=str, nargs='?', default='city_a', help="The name of the city to run (defaults to city_a).")
    parser.add_argument(
        "--db-file",
        type=str,
        default=None,
        help="Path to the unified database file. Defaults to the managed database/data directory.",
    )
    default_sds_url = os.getenv("SDS_URL", "http://127.0.0.1:8080")
    parser.add_argument("--sds-url", type=str, default=default_sds_url, help="URL of the SAIVerse Directory Service (or from .env).")
    args = parser.parse_args()

    if args.db_file:
        provided_path = Path(args.db_file)
        if provided_path.is_absolute():
            db_path = provided_path
        else:
            root_dir = Path(__file__).parent
            if ("/" not in args.db_file and "\\" not in args.db_file):
                db_path = (root_dir / "database" / provided_path).resolve()
            else:
                db_path = (root_dir / provided_path).resolve()
    else:
        db_path = default_db_path()

    # Start database backup in background thread
    threading.Thread(target=run_startup_backup, args=(db_path,), daemon=True).start()

    global manager, AUTONOMOUS_BUILDING_CHOICES, AUTONOMOUS_BUILDING_MAP, BUILDING_CHOICES, BUILDING_NAME_TO_ID_MAP, api_server_process
    manager = SAIVerseManager(
        city_name=args.city_name,
        db_path=str(db_path),
        sds_url=args.sds_url
    )
    if ensure_gateway_runtime:
        ensure_gateway_runtime(manager)

    app_state.bind_manager(manager)
    app_state.set_model_choices(MODEL_CHOICES)
    app_state.set_chat_history_limit(CHAT_HISTORY_LIMIT)
    app_state.set_version(VERSION)
    app_state.set_city_name(args.city_name)
    app_state.set_project_dir(str(Path(__file__).parent.resolve()))

    # Unity Gateway の起動（オプション）
    unity_gateway_port = int(os.getenv("UNITY_GATEWAY_PORT", "8765"))
    if UNITY_GATEWAY_AVAILABLE and os.getenv("UNITY_GATEWAY_ENABLED", "true").lower() == "true":
        manager.unity_gateway = UnityGatewayServer(manager)
        if manager.unity_gateway.is_available:
            import asyncio
            def run_unity_gateway():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(manager.unity_gateway.start(port=unity_gateway_port))
            unity_gateway_thread = threading.Thread(target=run_unity_gateway, daemon=True)
            unity_gateway_thread.start()
            logging.info(f"Unity Gateway starting on ws://0.0.0.0:{unity_gateway_port}")
        else:
            logging.warning("Unity Gateway: websockets package not installed")
    else:
        manager.unity_gateway = None

    api_server_process = cleanup_and_start_server_with_args(
        manager.api_port,
        Path(__file__).parent / "database" / "api_server.py",
        "API Server",
        str(db_path),
    )

    # --- アプリケーション終了時のクリーンアップ ---
    shutdown_called = False

    def shutdown_everything():
        nonlocal shutdown_called
        if shutdown_called:
            return
        shutdown_called = True
        # Unity Gatewayの停止
        if manager and manager.unity_gateway:
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(manager.unity_gateway.stop())
            except Exception as e:
                logging.debug(f"Error stopping Unity Gateway: {e}")
        shutdown_subprocess(api_server_process, "API Server")
        if manager:
            manager.shutdown()

    def handle_sigterm(signum, frame):
        """SIGTERMを受け取ったときにクリーンアップを実行してから終了"""
        logging.info("Received SIGTERM, initiating graceful shutdown...")
        shutdown_everything()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    atexit.register(shutdown_everything)

    # --- FastAPI Server Setup ---
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    import uvicorn

    app = FastAPI()

    # CORS settings (Allow frontend access)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Development only: allow all
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount uploads directory for user-attached images FIRST (more specific path)
    # Access via /api/static/uploads/filename.png
    uploads_dir = Path.home() / ".saiverse" / "image"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/api/static/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

    # Mount user icons for user-uploaded avatars
    # Access via /api/static/user_icons/filename.webp
    from saiverse.data_paths import get_user_icons_dir
    user_icons_dir = get_user_icons_dir()
    app.mount("/api/static/user_icons", StaticFiles(directory=str(user_icons_dir)), name="user_icons")

    # Mount builtin_data/icons for default icons (host.png, user.png)
    # Access via /api/static/builtin_icons/host.png
    builtin_icons_dir = Path(__file__).parent / "builtin_data" / "icons"
    builtin_icons_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/api/static/builtin_icons", StaticFiles(directory=str(builtin_icons_dir)), name="builtin_icons")

    # Mount assets directory for static files (legacy fallback)
    # Access via /api/static/icons/user.png
    app.mount("/api/static", StaticFiles(directory="assets"), name="static")

    # Mount API Routes
    from api.main import api_router
    app.include_router(api_router, prefix="/api")

    logging.info(f"Starting SAIVerse backend on http://0.0.0.0:{manager.ui_port}")
    logging.info(f"API endpoints available at http://0.0.0.0:{manager.ui_port}/api")
    logging.info("")
    logging.info("To use the UI, start the Next.js frontend:")
    logging.info("  cd frontend && npm run dev")
    logging.info("  Then open http://localhost:3000 in your browser")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=manager.ui_port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
