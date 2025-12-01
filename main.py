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
from dotenv import load_dotenv
from typing import Optional
from pathlib import Path

load_dotenv()

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "0")
os.environ.setdefault("GRADIO_TELEMETRY_ENABLED", "0")

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    psutil = None  # type: ignore

from saiverse_manager import SAIVerseManager
from database.paths import default_db_path
from database.backup import run_startup_backup
from model_configs import get_model_choices
from ui import state as ui_state
from ui.app import build_app
try:
    from discord_gateway import ensure_gateway_runtime
except ImportError:  # pragma: no cover - optional dependency
    ensure_gateway_runtime = None

level_name = os.getenv("SAIVERSE_LOG_LEVEL", "INFO").upper()
if level_name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    level_name = "INFO"
logging.basicConfig(level=getattr(logging, level_name))
try:
    _chat_limit_env = int(os.getenv("SAIVERSE_CHAT_HISTORY_LIMIT", "120"))
except ValueError:
    _chat_limit_env = 120
CHAT_HISTORY_LIMIT = max(0, _chat_limit_env)
MODEL_CHOICES = ["None"] + get_model_choices()

VERSION = time.strftime("%Y%m%d%H%M%S")  # 例: 20251008121530

HEAD_VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'


CSS_PATH = Path("assets/css/chat.css")
try:
    NOTE_CSS = CSS_PATH.read_text(encoding="utf-8")
except OSError:
    logging.warning("Failed to load CSS from %s", CSS_PATH)
    NOTE_CSS = ""



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

    ui_state.bind_manager(manager)
    ui_state.set_model_choices(MODEL_CHOICES)
    ui_state.set_chat_history_limit(CHAT_HISTORY_LIMIT)
    ui_state.set_version(VERSION)
    ui_state.refresh_building_caches()

    api_server_process = cleanup_and_start_server_with_args(
        manager.api_port,
        Path(__file__).parent / "database" / "api_server.py",
        "API Server",
        str(db_path),
    )

    # --- アプリケーション終了時のクリーンアップ ---
    def shutdown_everything():
        shutdown_subprocess(api_server_process, "API Server")
        if manager:
            manager.shutdown()

    atexit.register(shutdown_everything)

    # --- FastAPIとGradioの統合 ---
    # 3. Gradio UIを作成
    demo = build_app(args.city_name, NOTE_CSS, HEAD_VIEWPORT)
    demo.launch(
        server_name="0.0.0.0",
        server_port=manager.ui_port,
        debug=False,
        share=False,
    )


if __name__ == "__main__":
    main()
