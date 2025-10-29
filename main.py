import logging
import threading
import time
import subprocess
import sys
import os
import json
import argparse
import atexit
from dotenv import load_dotenv
from typing import Optional
from pathlib import Path

load_dotenv()

from saiverse_manager import SAIVerseManager
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
    """指定されたポートを使用しているプロセスのPIDを見つける (Windows専用)"""
    if sys.platform != "win32":
        logging.warning("Port cleanup is only supported on Windows.")
        return None
    try:
        result = subprocess.check_output(["netstat", "-ano"], text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        for line in result.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = int(line.split()[-1])
                return pid
    except (subprocess.CalledProcessError, FileNotFoundError):
        logging.error("Could not execute 'netstat' command. Please ensure it is in your PATH.")
    except Exception as e:
        logging.error(f"Error finding PID for port {port}: {e}")
    return None

def kill_process_by_pid(pid: int):
    """PIDを指定してプロセスを終了させる (Windows専用)"""
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True, capture_output=True)
        logging.info(f"Process with PID {pid} has been terminated.")
        time.sleep(1)  # プロセスが完全に終了するのを少し待つ
    except subprocess.CalledProcessError as e:
        if e.returncode == 128: # "No such process"
            logging.warning(f"Process with PID {pid} not found. It might have already been closed.")
        else:
            logging.error(f"Failed to terminate process with PID {pid}. Stderr: {e.stderr.decode(errors='ignore')}")
    except Exception as e:
        logging.error(f"An unexpected error occurred while killing process {pid}: {e}")

def cleanup_and_start_server(port: int, script_path: Path, name: str):
    """ポートをクリーンアップし、指定されたスクリプトをモジュールとしてバックグラウンドで起動する"""
    pid = find_pid_for_port(port)
    if pid:
        logging.warning(f"Port {port} for {name} is already in use by PID {pid}. Attempting to terminate the process.")
        kill_process_by_pid(pid)

    project_root = Path(__file__).parent
    # Convert file path to module path (e.g., database\api_server.py -> database.api_server)
    module_path = str(script_path.relative_to(project_root)).replace(os.sep, '.')[:-3]

    logging.info(f"Starting {name} as module: {module_path}")
    # Run as a module from the project's root directory to handle relative imports correctly
    subprocess.Popen([sys.executable, "-m", module_path], cwd=project_root)

def cleanup_and_start_server_with_args(port: int, script_path: Path, name: str, db_file: str):
    """ポートをクリーンアップし、引数付きでスクリプトをモジュールとして起動する"""
    pid = find_pid_for_port(port)
    if pid:
        logging.warning(f"Port {port} for {name} is already in use by PID {pid}. Attempting to terminate the process.")
        kill_process_by_pid(pid)

    project_root = Path(__file__).parent
    module_path = str(script_path.relative_to(project_root)).replace(os.sep, '.')[:-3]

    logging.info(f"Starting {name} as module: {module_path} with DB: {db_file} on port: {port}")
    subprocess.Popen([sys.executable, "-m", module_path, "--port", str(port), "--db", db_file], cwd=project_root)

def main():
    parser = argparse.ArgumentParser(description="Run a SAIVerse City instance.")
    parser.add_argument("city_name", type=str, nargs='?', default='city_a', help="The name of the city to run (defaults to city_a).")
    parser.add_argument("--db-file", type=str, default="saiverse.db", help="Path to the unified database file.")
    default_sds_url = os.getenv("SDS_URL", "http://127.0.0.1:8080")
    parser.add_argument("--sds-url", type=str, default=default_sds_url, help="URL of the SAIVerse Directory Service (or from .env).")
    args = parser.parse_args()

    db_path = Path(__file__).parent / "database" / args.db_file

    global manager, AUTONOMOUS_BUILDING_CHOICES, AUTONOMOUS_BUILDING_MAP, BUILDING_CHOICES, BUILDING_NAME_TO_ID_MAP
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

    cleanup_and_start_server_with_args(manager.api_port, Path(__file__).parent / "database" / "api_server.py", "API Server", str(db_path))

    # --- アプリケーション終了時にManagerのシャットダウン処理を呼び出す ---
    atexit.register(manager.shutdown)

    # --- FastAPIとGradioの統合 ---
    # 3. Gradio UIを作成
    demo = build_app(args.city_name, NOTE_CSS, HEAD_VIEWPORT)
    demo.launch(server_name="0.0.0.0",server_port=manager.ui_port, debug=True, share = False)


if __name__ == "__main__":
    main()
