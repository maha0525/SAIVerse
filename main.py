import logging
import threading
import time
import subprocess
import sys
import os
import json
import argparse
import atexit
from typing import Optional, List, Dict
from pathlib import Path

import gradio as gr

from saiverse_manager import SAIVerseManager
from model_configs import get_model_choices
from database.db_manager import create_db_manager_ui

logging.basicConfig(level=logging.INFO)
manager: SAIVerseManager = None
BUILDING_CHOICES = []
BUILDING_NAME_TO_ID_MAP = {}
MODEL_CHOICES = get_model_choices()
AUTONOMOUS_BUILDING_CHOICES = []
AUTONOMOUS_BUILDING_MAP = {}

NOTE_CSS = """
/* --- Flexboxを使った新しいレイアウト --- */

/* メッセージ一行全体をFlexboxコンテナにする */
.message-row {
    display: flex !important;
    align-items: flex-start; /* アイコンとテキストを上揃えに */
    gap: 12px; /* アイコンとテキストの間隔 */
    margin-bottom: 12px;
}

/* アイコンのスタイル */
.message-row .avatar-container,
.message-row .inline-avatar {
    width: 60px;
    height: 60px;
    min-width: 60px; /* 縮まないように */
    border-radius: 20%;
    overflow: hidden;
    margin: 0 !important; /* floatのmarginをリセット */
}

.message-row .avatar-container img,
.message-row .inline-avatar img, /* Gradioが生成するimgタグにも適用 */
.message-row .inline-avatar {
    width: 100%;
    height: 100%;
    object-fit: cover; /* アスペクト比を保ったままコンテナを埋める */
}

/* メッセージテキスト部分のコンテナ */
.message-row .message {
    flex-grow: 1; /* 残りのスペースをすべて使う */
    padding: 10px 14px;
    background-color: #f0f0f0; /* 背景色を少しつける */
    color: #222 !important; /* ★文字色を暗い色に固定 (重要度を上げる) */
    border-radius: 12px;
    min-height: 60px; /* アイコンの高さと合わせる */
    font-size: 1rem !important;
    overflow-wrap: break-word; /* 長い単語でも折り返す */
}

/* ユーザー側のメッセージを右寄せにする */
.user-message {
    flex-direction: row-reverse;
}
.user-message .message {
    background-color: #d1e7ff; /* ユーザーのメッセージ色を変更 */
    color: #222 !important; /* ★ユーザー側の文字色も暗い色に固定 (重要度を上げる) */
}

/* ホストやシステムノートのスタイル */
.note-box {
    background: #fff9db;
    color: #333350 !important; /* ★文字色を暗い色に固定 (重要度を上げる) */
    border-left: 4px solid #ffbf00;
    padding: 8px 12px;
    margin: 0;
    border-radius: 6px;
    font-size: .92rem;
}

/* ダークモード用の文字色上書き */
body.dark .message, body.dark .message p, body.dark .message b {
    color: #222 !important;
}

body.dark .note-box, body.dark .note-box *, body.dark .note-box b {
    color: #333350 !important;
}
"""

def format_history_for_chatbot(raw_history: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """生の会話履歴をGradio Chatbotの表示形式（HTML）に変換する"""
    display: List[Dict[str, str]] = []
    for msg in raw_history:
        role = msg.get("role")
        if role == "assistant":
            pid = msg.get("persona_id")
            avatar = manager.avatar_map.get(pid, manager.default_avatar)
            say = msg.get("content", "")
            if avatar:
                html = f"<div class='message-row'><div class='avatar-container'><img src='{avatar}'></div><div class='message'>{say}</div></div>"
            else:
                html = f"{say}"
            display.append({"role": "assistant", "content": html})
        elif role == "user":
            display.append(msg)
        elif role == "host":
            say = msg.get("content", "")
            if manager.host_avatar:
                html = f"<div class='message-row'><div class='avatar-container'><img src='{manager.host_avatar}'></div><div class='message'>{say}</div></div>"
            else:
                html = f"<b>[HOST]</b> {say}"
            display.append({"role": "assistant", "content": html})
        # "system" role messages are filtered out from the display
    return display


def respond_stream(message: str):
    """Stream AI response for chat and update UI components if needed."""
    # Get history from current location
    current_building_id = manager.user_current_building_id
    if not current_building_id:
        yield [{"role": "assistant", "content": '<div class="note-box">エラー: ユーザーの現在地が不明です。</div>'}], gr.update(), gr.update()
        return

    raw_history = manager.get_building_history(current_building_id)
    history = format_history_for_chatbot(raw_history)
    history.append({"role": "user", "content": message})
    ai_message = ""
    # manager.handle_user_input_stream already uses the user's current location
    for token in manager.handle_user_input_stream(message):
        ai_message += token
        # ストリーミング中はドロップダウンは更新しない
        yield history + [{"role": "assistant", "content": ai_message}], gr.update(), gr.update()
    # After streaming, get the final history again to include system messages etc.
    final_raw = manager.get_building_history(current_building_id)
    final_history_formatted = format_history_for_chatbot(final_raw)

    # Check if the building list has changed
    global BUILDING_CHOICES, BUILDING_NAME_TO_ID_MAP
    summonable_personas = manager.get_summonable_personas()
    new_building_names = sorted([b.name for b in manager.buildings]) # ソートして比較
    if new_building_names != sorted(BUILDING_CHOICES):
        logging.info("Building list has changed. Updating dropdown.")
        BUILDING_CHOICES = new_building_names
        BUILDING_NAME_TO_ID_MAP = {b.name: b.building_id for b in manager.buildings}
        yield final_history_formatted, gr.update(choices=BUILDING_CHOICES), gr.update(choices=summonable_personas, value=None)
    else:
        yield final_history_formatted, gr.update(), gr.update(choices=summonable_personas, value=None)


def select_model(model_name: str):
    manager.set_model(model_name)
    # Get history from current location
    current_building_id = manager.user_current_building_id
    if not current_building_id:
        return []
    raw_history = manager.get_building_history(current_building_id)
    return format_history_for_chatbot(raw_history)

def move_user_ui(building_name: str):
    """UI handler for moving the user."""
    if not building_name:
        # Just return current state if nothing is selected
        current_history = format_history_for_chatbot(manager.get_building_history(manager.user_current_building_id))
        current_location = manager.building_map.get(manager.user_current_building_id).name
        return current_history, current_location, gr.update()

    target_building_id = BUILDING_NAME_TO_ID_MAP.get(building_name)
    if target_building_id:
        manager.move_user(target_building_id)

    new_history = manager.get_building_history(manager.user_current_building_id)
    new_location_name = manager.building_map.get(manager.user_current_building_id).name
    summonable_personas = manager.get_summonable_personas()
    return format_history_for_chatbot(new_history), new_location_name, gr.update(choices=summonable_personas, value=None)

def call_persona_ui(persona_name: str):
    """UI handler for summoning a persona."""
    if not persona_name:
        return format_history_for_chatbot(manager.get_building_history(manager.user_current_building_id)), gr.update()

    persona_id = manager.persona_map.get(persona_name)
    if persona_id:
        manager.summon_persona(persona_id)

    new_history = format_history_for_chatbot(manager.get_building_history(manager.user_current_building_id))
    summonable_personas = manager.get_summonable_personas()
    return new_history, gr.update(choices=summonable_personas, value=None)

def get_autonomous_log(building_name: str):
    """指定されたBuildingの会話ログを取得する"""
    building_id = AUTONOMOUS_BUILDING_MAP.get(building_name)
    if building_id:
        raw_history = manager.get_building_history(building_id)
        return format_history_for_chatbot(raw_history)
    return []

def start_conversations_ui():
    """UI handler to start autonomous conversations and update status."""
    manager.start_autonomous_conversations()
    return "実行中"

def stop_conversations_ui():
    """UI handler to stop autonomous conversations and update status."""
    manager.stop_autonomous_conversations()
    return "停止中"

def login_ui():
    """UI handler for user login."""
    # USERID=1をハードコード
    return manager.set_user_login_status(1, True)

def logout_ui():
    """UI handler for user logout."""
    # USERID=1をハードコード
    return manager.set_user_login_status(1, False)


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
    parser.add_argument("city_name", type=str, help="The name of the city to run (e.g., city_a).")
    parser.add_argument("--db-file", type=str, default="saiverse.db", help="Path to the unified database file.")
    parser.add_argument("--sds-url", type=str, default="http://127.0.0.1:8080", help="URL of the SAIVerse Directory Service.")
    args = parser.parse_args()

    db_path = Path(__file__).parent / "database" / args.db_file

    global manager, AUTONOMOUS_BUILDING_CHOICES, AUTONOMOUS_BUILDING_MAP, BUILDING_CHOICES, BUILDING_NAME_TO_ID_MAP
    manager = SAIVerseManager(
        city_name=args.city_name,
        db_path=str(db_path),
        sds_url=args.sds_url
    )
    # Populate new globals for the move dropdown
    BUILDING_CHOICES = [b.name for b in manager.buildings]
    BUILDING_NAME_TO_ID_MAP = {b.name: b.building_id for b in manager.buildings}
    AUTONOMOUS_BUILDING_CHOICES = [b.name for b in manager.buildings if b.building_id != manager.user_room_id]
    AUTONOMOUS_BUILDING_MAP = {b.name: b.building_id for b in manager.buildings if b.building_id != manager.user_room_id}

    cleanup_and_start_server_with_args(manager.api_port, Path(__file__).parent / "database" / "api_server.py", "API Server", str(db_path))

    # --- アプリケーション終了時にManagerのシャットダウン処理を呼び出す ---
    atexit.register(manager.shutdown)

    # --- FastAPIとGradioの統合 ---
    # 3. Gradio UIを作成
    with gr.Blocks(css=NOTE_CSS, title=f"SAIVerse City: {args.city_name}", theme=gr.themes.Soft()) as demo:
        with gr.Tabs():
            with gr.TabItem("ワールドビュー"):
                with gr.Row():
                    user_location_display = gr.Textbox(
                        # managerから現在地を取得して表示する
                        value=lambda: manager.building_map.get(manager.user_current_building_id).name if manager.user_current_building_id and manager.user_current_building_id in manager.building_map else "不明な場所",
                        label="あなたの現在地",
                        interactive=False,
                        scale=2
                    )
                    move_building_dropdown = gr.Dropdown(
                        choices=BUILDING_CHOICES,
                        label="移動先の建物",
                        interactive=True,
                        scale=2
                    )
                    move_btn = gr.Button("移動", scale=1)

                gr.Markdown("---")

                # --- ここから下は既存のUI ---
                gr.Markdown("### 現在地での対話")

                chatbot = gr.Chatbot(
                    type="messages",
                    value=lambda: format_history_for_chatbot(manager.get_building_history(manager.user_current_building_id)) if manager.user_current_building_id else [],
                    group_consecutive_messages=False,
                    sanitize_html=False,
                    elem_id="my_chat",
                    avatar_images=(
                        "assets/icons/user.png", # ← ユーザー
                        None  # アシスタント側はメッセージ内に表示
                    ),
                    height=800
                )
                with gr.Row():
                    with gr.Column(scale=4):
                        txt = gr.Textbox(placeholder="ここにメッセージを入力...", lines=4)
                    with gr.Column(scale=1):
                        submit = gr.Button("送信")
                
                gr.Markdown("---")
                with gr.Accordion("ペルソナを招待する", open=False):
                    with gr.Row():
                        summon_persona_dropdown = gr.Dropdown(
                            choices=manager.get_summonable_personas(),
                            label="呼ぶペルソナを選択",
                            interactive=True,
                            scale=3
                        )
                        summon_btn = gr.Button("呼ぶ", scale=1)
                
                gr.Markdown("---")

                with gr.Row():
                    login_status_display = gr.Textbox(
                        value="オンライン" if manager.user_is_online else "オフライン",
                        label="ログイン状態",
                        interactive=False,
                        scale=1
                    )
                    login_btn = gr.Button("ログイン", scale=1)
                    logout_btn = gr.Button("ログアウト", scale=1)
                gr.Markdown("---")
                with gr.Row():
                    sds_status_display = gr.Textbox(
                        value=manager.sds_status,
                        label="ネットワークモード",
                        interactive=False,
                        scale=2
                    )
                    online_btn = gr.Button("オンラインモードへ", scale=1)
                    offline_btn = gr.Button("オフラインモードへ", scale=1)


                gr.Markdown("---")

                with gr.Row():
                    model_drop = gr.Dropdown(choices=MODEL_CHOICES, value=manager.model, label="システムデフォルトモデル (一時的な一括上書き)")

                # --- Event Handlers ---
                submit.click(respond_stream, txt, [chatbot, move_building_dropdown, summon_persona_dropdown])
                txt.submit(respond_stream, txt, [chatbot, move_building_dropdown, summon_persona_dropdown]) # Enter key submission
                move_btn.click(fn=move_user_ui, inputs=[move_building_dropdown], outputs=[chatbot, user_location_display, summon_persona_dropdown])
                summon_btn.click(fn=call_persona_ui, inputs=[summon_persona_dropdown], outputs=[chatbot, summon_persona_dropdown])
                login_btn.click(fn=login_ui, inputs=None, outputs=login_status_display)
                logout_btn.click(fn=logout_ui, inputs=None, outputs=login_status_display)
                model_drop.change(select_model, model_drop, chatbot)
                online_btn.click(fn=manager.switch_to_online_mode, inputs=None, outputs=sds_status_display)
                offline_btn.click(fn=manager.switch_to_offline_mode, inputs=None, outputs=sds_status_display)

            with gr.TabItem("自律会話ログ"):
                with gr.Row():
                    status_display = gr.Textbox(
                        value="停止中",
                        label="現在のステータス",
                        interactive=False,
                        scale=1
                    )
                    start_button = gr.Button("自律会話を開始", variant="primary", scale=1)
                    stop_button = gr.Button("自律会話を停止", variant="stop", scale=1)

                gr.Markdown("---")

                with gr.Row():
                    log_building_dropdown = gr.Dropdown(
                        choices=AUTONOMOUS_BUILDING_CHOICES,
                        value=AUTONOMOUS_BUILDING_CHOICES[0] if AUTONOMOUS_BUILDING_CHOICES else None,
                        label="Building選択",
                        interactive=bool(AUTONOMOUS_BUILDING_CHOICES)
                    )
                    log_refresh_btn = gr.Button("手動更新")
                log_chatbot = gr.Chatbot(
                    type="messages",
                    group_consecutive_messages=False,
                    sanitize_html=False,
                    elem_id="log_chat",
                    height=800
                )
                # JavaScriptからクリックされるための、非表示の自動更新ボタン
                auto_refresh_log_btn = gr.Button("Auto-Refresh Trigger", visible=False, elem_id="auto_refresh_log_btn")

                # イベントハンドラ (ON/OFF)
                start_button.click(fn=start_conversations_ui, inputs=None, outputs=status_display)
                stop_button.click(fn=stop_conversations_ui, inputs=None, outputs=status_display)

                # イベントハンドラ
                log_building_dropdown.change(fn=get_autonomous_log, inputs=log_building_dropdown, outputs=log_chatbot, show_progress="hidden")
                log_refresh_btn.click(fn=get_autonomous_log, inputs=log_building_dropdown, outputs=log_chatbot, show_progress="hidden")
                auto_refresh_log_btn.click(fn=get_autonomous_log, inputs=log_building_dropdown, outputs=log_chatbot, show_progress="hidden")

            with gr.TabItem("DB Manager"):
                create_db_manager_ui(manager.SessionLocal)

        # UIロード時にJavaScriptを実行し、5秒ごとの自動更新タイマーを設定する
        js_auto_refresh = """
        () => {
            setInterval(() => {
                const button = document.getElementById('auto_refresh_log_btn');
                if (button) {
                    button.click();
                }
            }, 5000);
        }
        """
        demo.load(None, None, None, js=js_auto_refresh)

    demo.launch(server_port=manager.ui_port, debug=True)


if __name__ == "__main__":
    main()
