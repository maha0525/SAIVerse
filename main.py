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
from typing import Optional, List, Dict
from pathlib import Path
import pandas as pd

import gradio as gr

load_dotenv()

from saiverse_manager import SAIVerseManager
from model_configs import get_model_choices
from database.db_manager import create_db_manager_ui

level_name = os.getenv("SAIVERSE_LOG_LEVEL", "INFO").upper()
if level_name not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    level_name = "INFO"
logging.basicConfig(level=getattr(logging, level_name))
manager: SAIVerseManager = None
BUILDING_CHOICES = []
BUILDING_NAME_TO_ID_MAP = {}
MODEL_CHOICES = ["None"] + get_model_choices()
AUTONOMOUS_BUILDING_CHOICES = []
AUTONOMOUS_BUILDING_MAP = {}

VERSION = time.strftime("%Y%m%d%H%M%S")  # 例: 20251008121530

HEAD_VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'


NOTE_CSS = """
/* === モバイルで Chatbot 親ブロックの左右パディングを完全に殺す === */
@media (max-width: 680px) {
  /* 親ブロック特定（:has はそのまま・@supports は外す） */
  :is(.gr-block, .gr-column, .gr-row, .group, .tabitem):has(> #chat_wrap):not(.sidebar-parent),
  :is(.gr-block, .gr-column, .gr-row, .group, .tabitem):has(#my_chat):not(.sidebar-parent) {
    padding-left: 0 !important;
    padding-right: 0 !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    max-width: 100% !important;
    width: 100% !important;
  }

  /* フォールバック：親の内側余白を“子から”物理的に打ち消す */
  #chat_wrap {
    /* ビューポートいっぱいに拡げて親のpaddingを無効化 */
    width: 100vw !important;
    max-width: 100vw !important;
    margin-left: calc(50% - 50vw) !important;
    margin-right: calc(50% - 50vw) !important;
  }

  /* Chatbot 本体も全幅に */
  #my_chat {
    max-width: 100% !important;
    width: 100% !important;
  }

  /* Markdown クランプ解除（保険） */
  #my_chat .prose, #my_chat [class*="prose"] { max-width: none !important; }

  /* 長文の折返し */
  #my_chat pre { white-space: pre-wrap !important; word-break: break-word !important; }
}

/* iOS セーフエリア（必要なら併用可：viewport-fit=cover を head に入れている前提） */
@supports (padding: env(safe-area-inset-left)) {
  body {
    padding-left: max(0px, env(safe-area-inset-left));
    padding-right: max(0px, env(safe-area-inset-right));
  }
}

html[data-theme='light'] {
  --msg-bg: #f3f4f6;
  --msg-fg: #111827;
  --user-bg: #dbeafe;  /* light blue */
  --user-fg: #111827;
  --note-bg: #fff9db;
  --note-fg: #1f2937;
}
html[data-theme='dark'] {
  --msg-bg: #333333;
  --msg-fg: #f9fafb;
  --user-bg: #1f3b57; /* darker blue for contrast */
  --user-fg: #e5e7eb;
  --note-bg: #3b3a2a;
  --note-fg: #f3f4f6;
}
/* Fallback to system preference if theme attr not present */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) {
    --msg-bg: #333333;
    --msg-fg: #f9fafb;
    --user-bg: #1f3b57;
    --user-fg: #e5e7eb;
    --note-bg: #3b3a2a;
    --note-fg: #f3f4f6;
  }
}

/* --- Flexboxレイアウト --- */
.message-row { display: flex !important; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.message-row .avatar-container { width: 60px; height: 60px; min-width: 60px; border-radius: 12px !important; overflow: hidden; margin: 0 !important; display: inline-block; }
.message-row .avatar-container img { width: 100%; height: 100%; object-fit: cover; border-radius: inherit !important; display: block; }
.message-row .message { flex-grow: 1; padding: 10px 14px; background-color: var(--msg-bg); color: var(--msg-fg) !important; border-radius: 12px; min-height: 60px; font-size: 1rem !important; overflow-wrap: break-word; }
.user-message { flex-direction: row-reverse; }
.user-message .message { background-color: var(--user-bg); color: var(--user-fg) !important; }

/* Notes */
.note-box { background: var(--note-bg); color: var(--note-fg) !important; border-left: 4px solid #ffbf00; padding: 8px 12px; margin: 0; border-radius: 6px; font-size: .92rem; }
.note-box b { color: var(--note-fg) !important; }

/* Reasoning (Thinking) blocks */
details.saiv-thinking { margin-top: 10px; border: 1px solid rgba(128,128,128,0.25); border-radius: 8px; padding: 8px 12px; background: rgba(0,0,0,0.02); }
html[data-theme='dark'] details.saiv-thinking { background: rgba(255,255,255,0.04); border-color: rgba(255,255,255,0.12); }
details.saiv-thinking summary { cursor: pointer; font-weight: 600; outline: none; }
details.saiv-thinking summary:focus { outline: none; }
.saiv-thinking-body { margin-top: 6px; display: flex; flex-direction: column; gap: 8px; font-size: 0.96rem; color: inherit; }
.saiv-thinking-item { border-top: 1px solid rgba(128,128,128,0.2); padding-top: 6px; }
.saiv-thinking-item:first-child { border-top: none; padding-top: 0; }
.saiv-thinking-title { font-weight: 600; margin-bottom: 4px; }
.saiv-thinking-text { line-height: 1.45; word-break: break-word; }

/* Strongly scoped avatar rounding for Chatbot area */
#my_chat .saiv-avatar { border-radius: 12px !important; overflow: hidden !important; display: inline-block; width: 60px; height: 60px; min-width: 60px; }
#my_chat .saiv-avatar > img { border-radius: 12px !important; margin: 0 !important; width: 100% !important; height: 100% !important; object-fit: cover !important; display: block !important; clip-path: inset(0 round 12px) !important; }
#my_chat .saiv-avatar > picture > img { border-radius: 12px !important; margin: 0 !important; clip-path: inset(0 round 12px) !important; }

:global(.saiverse-sidebar.sidebar) {
  width: 20vw !important;
}
:global(.saiverse-sidebar.sidebar):not(.right) {
  left: calc(-1 * 20vw) !important;
}
:global(.saiverse-sidebar.sidebar).right {
  right: calc(-1 * 20vw) !important;
}

/* Sidebar toggle を押しやすく */
:global(.saiverse-sidebar.sidebar) .toggle-button {
  width: 56px !important;
  height: 56px !important;
  padding: 0 !important;
}
:global(.saiverse-sidebar.sidebar):not(.right) .toggle-button,
:global(.saiverse-sidebar.sidebar).open:not(.right) .toggle-button {
  border-radius: 0 28px 28px 0 !important;
}
:global(.saiverse-sidebar.sidebar).right .toggle-button,
:global(.saiverse-sidebar.sidebar).open.right .toggle-button {
  border-radius: 28px 0 0 28px !important;
}
:global(.saiverse-sidebar.sidebar) .chevron {
  padding-right: 0 !important;
}
:global(.saiverse-sidebar.sidebar) .chevron-left {
  width: 18px !important;
  height: 18px !important;
  border-top-width: 3px !important;
  border-right-width: 3px !important;
}
@media (max-width: 768px) {
  :global(.saiverse-sidebar.sidebar) {
    width: 80vw !important;
  }
  :global(.saiverse-sidebar.sidebar):not(.right) {
    left: -80vw !important;
  }
  :global(.saiverse-sidebar.sidebar).right {
    right: -80vw !important;
  }
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
                avatar_box = (
                    "width:60px;height:60px;min-width:60px;"
                    "border-radius:12px;overflow:hidden;display:inline-block;margin:0;"
                )
                avatar_img = (
                    "width:100%;height:100%;object-fit:cover;display:block;"
                    "margin:0;border-radius:inherit;clip-path: inset(0 round 12px);"
                )
                html = (
                    f"<div class='message-row'>"
                    f"<div class='avatar-container saiv-avatar' style=\"{avatar_box}\">"
                    f"<img class='saiv-avatar-img' src='{avatar}' style=\"{avatar_img}\"></div>"
                    f"<div class='message'>{say}</div></div>"
                )
            else:
                html = f"{say}"
            display.append({"role": "assistant", "content": html})
        elif role == "user":
            # Let Gradio Chatbot handle user-side alignment and avatar rendering
            # Keep the role as 'user' and pass through the plain content
            display.append({"role": "user", "content": msg.get("content", "")})
        elif role == "host":
            say = msg.get("content", "")
            if manager.host_avatar:
                avatar_box = (
                    "width:60px;height:60px;min-width:60px;"
                    "border-radius:12px;overflow:hidden;display:inline-block;margin:0;"
                )
                avatar_img = (
                    "width:100%;height:100%;object-fit:cover;display:block;"
                    "margin:0;border-radius:inherit;clip-path: inset(0 round 12px);"
                )
                html = (
                    f"<div class='message-row'>"
                    f"<div class='avatar-container saiv-avatar' style=\"{avatar_box}\">"
                    f"<img class='saiv-avatar-img' src='{manager.host_avatar}' style=\"{avatar_img}\"></div>"
                    f"<div class='message'>{say}</div></div>"
                )
            else:
                html = f"<b>[HOST]</b> {say}"
            display.append({"role": "assistant", "content": html})
        # "system" role messages are filtered out from the display
    return display


def respond_stream(message: str):
    """Stream AI response for chat and update UI components if needed."""
    # Get history from current location
    print(manager.occupants[manager.user_current_building_id])
    current_building_id = manager.user_current_building_id
    if not current_building_id:
        yield [{"role": "assistant", "content": '<div class="note-box">エラー: ユーザーの現在地が不明です。</div>'}], gr.update(), gr.update(), gr.update()
        return

    raw_history = manager.get_building_history(current_building_id)
    history = format_history_for_chatbot(raw_history)
    history.append({"role": "user", "content": message})
    ai_message = ""
    # manager.handle_user_input_stream already uses the user's current location
    for token in manager.handle_user_input_stream(message):
        ai_message += token
        # ストリーミング中はドロップダウンは更新しない
        yield history + [{"role": "assistant", "content": ai_message}], gr.update(), gr.update(), gr.update()
    # After streaming, get the final history again to include system messages etc.
    final_raw = manager.get_building_history(current_building_id)
    final_history_formatted = format_history_for_chatbot(final_raw)

    # Check if the building list has changed
    global BUILDING_CHOICES, BUILDING_NAME_TO_ID_MAP
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    new_building_names = sorted([b.name for b in manager.buildings]) # ソートして比較
    if new_building_names != sorted(BUILDING_CHOICES):
        logging.info("Building list has changed. Updating dropdown.")
        BUILDING_CHOICES = new_building_names
        BUILDING_NAME_TO_ID_MAP = {b.name: b.building_id for b in manager.buildings}
        yield final_history_formatted, gr.update(choices=BUILDING_CHOICES), gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)
    else:
        yield final_history_formatted, gr.update(), gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)


def select_model(model_name: str):
    # "None" means clear override and use each persona's DB default
    manager.set_model(model_name or "None")
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
        current_location = manager.building_map.get(manager.user_current_building_id).name if manager.user_current_building_id in manager.building_map else "不明"
        return current_history, current_location, gr.update(), gr.update()

    target_building_id = BUILDING_NAME_TO_ID_MAP.get(building_name)
    if target_building_id:
        manager.move_user(target_building_id)

    new_history = manager.get_building_history(manager.user_current_building_id)
    new_location_name = manager.building_map.get(manager.user_current_building_id).name
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    return format_history_for_chatbot(new_history), new_location_name, gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)

def call_persona_ui(persona_name: str):
    """UI handler for summoning a persona."""
    if not persona_name:
        return format_history_for_chatbot(manager.get_building_history(manager.user_current_building_id)), gr.update(), gr.update()

    persona_id = manager.persona_map.get(persona_name)
    if persona_id:
        manager.summon_persona(persona_id)
        manager._load_occupancy_from_db()

    new_history = format_history_for_chatbot(manager.get_building_history(manager.user_current_building_id))
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    return new_history, gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)

def end_conversation_ui(persona_id: str):
    """UI handler to end a conversation with a persona."""
    if not persona_id:
        # This can happen on initial load, just return current state
        current_history = format_history_for_chatbot(manager.get_building_history(manager.user_current_building_id))
        conversing_personas = manager.get_conversing_personas()
        manager._load_occupancy_from_db()
        return current_history, gr.update(), gr.update(choices=conversing_personas, value=None)

    manager.end_conversation(persona_id)
    manager._load_occupancy_from_db()

    new_history = format_history_for_chatbot(manager.get_building_history(manager.user_current_building_id))
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    return new_history, gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)


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
    # OccupantsリストをDBから最新化
    manager._load_occupancy_from_db()
    # USERID=1をハードコード
    summonable_personas = manager.get_summonable_personas()
    conversing_personas = manager.get_conversing_personas()
    status = manager.set_user_login_status(1, True)
    return status, gr.update(choices=summonable_personas, value=None), gr.update(choices=conversing_personas, value=None)

def logout_ui():
    """UI handler for user logout."""
    # USERID=1をハードコード
    return manager.set_user_login_status(1, False)

# --- World Editor UI Handlers ---

def update_city_ui(city_id_str: str, name: str, desc: str, online_mode: bool, ui_port_str: str, api_port_str: str):
    """UI handler to update city settings."""
    if not city_id_str: return "Error: Select a city to update.", gr.update()
    try:
        city_id = int(city_id_str)
        ui_port = int(ui_port_str)
        api_port = int(api_port_str)
    except (ValueError, TypeError):
        return "Error: Port numbers must be valid integers.", gr.update()
    
    result = manager.update_city(city_id, name, desc, online_mode, ui_port, api_port)
    return result, manager.get_cities_df()

def on_select_city(evt: gr.SelectData):
    """Handler for when a city is selected in the DataFrame."""
    if evt.value is None: return "", "", "", False, "", ""
    row_index = evt.index[0]
    df = manager.get_cities_df()
    selected_row = df.iloc[row_index]
    return (
        selected_row['CITYID'], selected_row['CITYNAME'], selected_row['DESCRIPTION'],
        selected_row['START_IN_ONLINE_MODE'], selected_row['UI_PORT'], selected_row['API_PORT']
    )

def update_building_ui(b_id: str, name: str, capacity_str: str, desc: str, sys_inst: str, city_id: Optional[int], tool_ids: List[int], interval_str: str):
    """UI handler to update building settings."""
    if not b_id: return "Error: Select a building to update.", gr.update()
    if city_id is None:
        return "Error: City must be selected.", gr.update()
    try:
        capacity = int(capacity_str)
        interval = int(interval_str)
    except (ValueError, TypeError):
        return "Error: Capacity and Interval must be valid integers.", gr.update()
    
    result = manager.update_building(b_id, name, capacity, desc, sys_inst, city_id, tool_ids, interval)
    return result, manager.get_buildings_df()

def on_select_building(evt: gr.SelectData):
    """Handler for when a building is selected in the DataFrame."""
    if evt.value is None: return "", "", 1, "", "", None, None, 10
    row_index = evt.index[0]
    df = manager.get_buildings_df()
    selected_row = df.iloc[row_index]
    linked_tool_ids = manager.get_linked_tool_ids(selected_row['BUILDINGID'])
    return (
        selected_row['BUILDINGID'], selected_row['BUILDINGNAME'], selected_row['CAPACITY'],
        selected_row['DESCRIPTION'], selected_row['SYSTEM_INSTRUCTION'], int(selected_row['CITYID']),
        linked_tool_ids, selected_row.get('AUTO_INTERVAL_SEC', 10) # Fallback for old DB
    )

def on_select_ai(evt: gr.SelectData):
    """Handler for when an AI is selected in the DataFrame."""
    if evt.index is None: return "", "", "", "", None, "", False, "auto", ""
    row_index = evt.index[0]
    # We need the full DF to get the ID, not just the visible part
    df = manager.get_ais_df()
    ai_id = df.iloc[row_index]['AIID']
    details = manager.get_ai_details(ai_id)
    if not details: return "", "", "", "", None, "", False, "auto", ""

    # --- 現在地を取得 ---
    current_location_name = "不明"
    if ai_id in manager.personas:
        current_building_id = manager.personas[ai_id].current_building_id
        if current_building_id in manager.building_map:
            current_location_name = manager.building_map[current_building_id].name

    return (
        details['AIID'],
        details['AINAME'],
        details['DESCRIPTION'],
        details['SYSTEMPROMPT'],
        int(details['HOME_CITYID']),
        details['DEFAULT_MODEL'],
        details['IS_DISPATCHED'],
        details['INTERACTION_MODE'],
        current_location_name
    )

def update_ai_ui(ai_id: str, name: str, desc: str, sys_prompt: str, home_city_id: int, model: str, interaction_mode: str):
    """UI handler to update AI settings."""
    if not ai_id:
        return "Error: Select an AI to update.", gr.update()
    if not home_city_id:
        return "Error: Home City must be selected.", gr.update()
    
    result = manager.update_ai(ai_id, name, desc, sys_prompt, home_city_id, model, interaction_mode)
    return result, manager.get_ais_df()

def move_ai_ui(ai_id: str, target_building_name: str):
    """UI handler to move an AI from the world editor."""
    if not ai_id or not target_building_name:
        return "Error: AIと移動先を選択してください。", gr.update()
    
    target_building_id = BUILDING_NAME_TO_ID_MAP.get(target_building_name)
    if not target_building_id:
        return f"Error: 建物 '{target_building_name}' のIDが見つかりません。", gr.update()
        
    result = manager.move_ai_from_editor(ai_id, target_building_id)
    
    # 移動後の現在地を再取得してUIに反映
    new_location_name = "不明"
    if ai_id in manager.personas:
        current_building_id = manager.personas[ai_id].current_building_id
        if current_building_id in manager.building_map:
            new_location_name = manager.building_map[current_building_id].name
            
    return result, new_location_name

def on_select_tool(evt: gr.SelectData):
    """Handler for when a tool is selected in the DataFrame."""
    if evt.index is None: return "", "", "", "", ""
    row_index = evt.index[0]
    df = manager.get_tools_df()
    selected_row = df.iloc[row_index]
    details = manager.get_tool_details(int(selected_row['TOOLID']))
    if not details: return "", "", "", "", ""
    return (
        details['TOOLID'], details['TOOLNAME'], details['DESCRIPTION'],
        details['MODULE_PATH'], details['FUNCTION_NAME']
    )

def create_world_editor_ui():
    """Creates all UI components for the World Editor tab."""
    # --- ★ UI構築の最初にCity情報を一度だけ取得 ---
    all_cities_df = manager.get_cities_df()
    city_choices = list(zip(all_cities_df['CITYNAME'], all_cities_df['CITYID'].astype(int)))
    # --- ★ UI構築の最初にTool情報を一度だけ取得 ---
    all_tools_df = manager.get_tools_df()
    tool_choices = list(zip(all_tools_df['TOOLNAME'], all_tools_df['TOOLID'].astype(int))) if not all_tools_df.empty else []


    # --- ★ Refresh Button ---
    with gr.Row():
        refresh_editor_btn = gr.Button("🔄 ワールドエディタ全体を更新", variant="secondary")

    # --- Handlers for Create/Delete ---
    def create_city_ui(name, desc, ui_port, api_port):
        if not all([name, ui_port, api_port]): return "Error: Name, UI Port, and API Port are required.", gr.update()
        result = manager.create_city(name, desc, int(ui_port), int(api_port))
        return result, manager.get_cities_df()

    def delete_city_ui(city_id_str, confirmed):
        if not confirmed: return "Error: Please check the confirmation box to delete.", gr.update()
        if not city_id_str: return "Error: Select a city to delete.", gr.update()
        result = manager.delete_city(int(city_id_str))
        return result, manager.get_cities_df()

    def create_building_ui(name, desc, capacity, sys_inst, city_id):
        if not all([name, capacity, city_id]): return "Error: Name, Capacity, and City are required.", gr.update()
        result = manager.create_building(name, desc, int(capacity), sys_inst, city_id)
        return result, manager.get_buildings_df()

    def delete_building_ui(b_id, confirmed):
        if not confirmed: return "Error: Please check the confirmation box to delete.", gr.update()
        if not b_id: return "Error: Select a building to delete.", gr.update()
        result = manager.delete_building(b_id)
        return result, manager.get_buildings_df()

    def create_ai_ui(name, sys_prompt, home_city_id):
        if not all([name, sys_prompt, home_city_id]): return "Error: Name, System Prompt, and Home City are required.", gr.update()
        result = manager.create_ai(name, sys_prompt, home_city_id)
        return result, manager.get_ais_df()

    def delete_ai_ui(ai_id, confirmed):
        if not confirmed: return "Error: Please check the confirmation box to delete.", gr.update()
        if not ai_id: return "Error: Select an AI to delete.", gr.update()
        result = manager.delete_ai(ai_id)
        return result, manager.get_ais_df()
    
    def create_tool_ui(name, desc, module_path, func_name):
        if not all([name, module_path, func_name]): return "Error: Name, Module Path, and Function Name are required.", gr.update()
        result = manager.create_tool(name, desc, module_path, func_name)
        return result, manager.get_tools_df()

    def update_tool_ui(tool_id, name, desc, module_path, func_name):
        if not tool_id: return "Error: Select a tool to update.", gr.update()
        result = manager.update_tool(int(tool_id), name, desc, module_path, func_name)
        return result, manager.get_tools_df()

    def delete_tool_ui(tool_id, confirmed):
        if not confirmed: return "Error: Please check the confirmation box to delete.", gr.update()
        if not tool_id: return "Error: Select a tool to delete.", gr.update()
        result = manager.delete_tool(int(tool_id))
        return result, manager.get_tools_df()

    # --- UI Layout with Create/Delete ---
    with gr.Accordion("City管理", open=True):
        with gr.Tabs():
            with gr.TabItem("編集/削除"):
                city_df = gr.DataFrame(value=manager.get_cities_df, interactive=False, label="Cities in this World")
                with gr.Row():
                    city_id_text = gr.Textbox(label="City ID", interactive=False)
                    city_name_textbox = gr.Textbox(label="City Name")
                    city_ui_port_num = gr.Number(label="UI Port", precision=0)
                    city_api_port_num = gr.Number(label="API Port", precision=0)
                city_desc_textbox = gr.Textbox(label="Description", lines=3)
                online_mode_checkbox = gr.Checkbox(label="次回起動時にオンラインモードで起動する")
                with gr.Row():
                    save_city_btn = gr.Button("City設定を保存")
                    delete_city_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
                    delete_city_btn = gr.Button("Cityを削除", variant="stop", interactive=False, scale=1)
                city_status_display = gr.Textbox(label="Status", interactive=False)
            with gr.TabItem("新規作成"):
                gr.Markdown("新しいCityを作成します。作成後、アプリケーションの再起動が必要です。")
                new_city_name_text = gr.Textbox(label="New City Name")
                new_city_desc_text = gr.Textbox(label="Description", lines=2)
                with gr.Row():
                    new_city_ui_port = gr.Number(label="UI Port", precision=0)
                    new_city_api_port = gr.Number(label="API Port", precision=0)
                create_city_btn = gr.Button("新規Cityを作成", variant="primary")
                create_city_status = gr.Textbox(label="Status", interactive=False)

    with gr.Accordion("Building管理", open=False):
        with gr.Tabs():
            with gr.TabItem("編集/削除"):
                building_df = gr.DataFrame(value=manager.get_buildings_df, interactive=False, label="Buildings in this World")
                with gr.Row():
                    building_id_text = gr.Textbox(label="Building ID", interactive=False)
                    building_name_text = gr.Textbox(label="Building Name")
                    building_capacity_num = gr.Number(label="Capacity", precision=0)
                    building_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
                    building_interval_num = gr.Number(label="自律会話周期(秒)", precision=0)
                building_desc_text = gr.Textbox(label="Description", lines=3)
                building_sys_inst_text = gr.Textbox(label="System Instruction", lines=5)
                building_tools_checkbox = gr.CheckboxGroup(choices=tool_choices, label="利用可能なツール", type="value")
                with gr.Row():
                    save_building_btn = gr.Button("Building設定を保存")
                    delete_bldg_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
                    delete_bldg_btn = gr.Button("Buildingを削除", variant="stop", interactive=False, scale=1)
                building_status_display = gr.Textbox(label="Status", interactive=False)
                
                # --- City Event Handlers ---
                def toggle_delete_button(is_checked):
                    return gr.update(interactive=is_checked)

                city_df.select(fn=on_select_city, inputs=None, outputs=[city_id_text, city_name_textbox, city_desc_textbox, online_mode_checkbox, city_ui_port_num, city_api_port_num])
                save_city_btn.click(fn=update_city_ui, inputs=[city_id_text, city_name_textbox, city_desc_textbox, online_mode_checkbox, city_ui_port_num, city_api_port_num], outputs=[city_status_display, city_df])
                delete_city_confirm_check.change(fn=toggle_delete_button, inputs=delete_city_confirm_check, outputs=delete_city_btn)
                delete_city_btn.click(fn=delete_city_ui, inputs=[city_id_text, delete_city_confirm_check], outputs=[city_status_display, city_df])
                create_city_btn.click(fn=create_city_ui, inputs=[new_city_name_text, new_city_desc_text, new_city_ui_port, new_city_api_port], outputs=[create_city_status, city_df])

                # --- Building Event Handlers ---
                building_df.select(fn=on_select_building, inputs=None, outputs=[building_id_text, building_name_text, building_capacity_num, building_desc_text, building_sys_inst_text, building_city_dropdown, building_tools_checkbox, building_interval_num])
                save_building_btn.click(fn=update_building_ui, inputs=[building_id_text, building_name_text, building_capacity_num, building_desc_text, building_sys_inst_text, building_city_dropdown, building_tools_checkbox, building_interval_num], outputs=[building_status_display, building_df])
                delete_bldg_confirm_check.change(fn=toggle_delete_button, inputs=delete_bldg_confirm_check, outputs=delete_bldg_btn)
                delete_bldg_btn.click(fn=delete_building_ui, inputs=[building_id_text, delete_bldg_confirm_check], outputs=[building_status_display, building_df])

            with gr.TabItem("新規作成"):
                gr.Markdown("新しいBuildingを作成します。作成後、アプリケーションの再起動が必要です。")
                new_bldg_name_text = gr.Textbox(label="New Building Name")
                new_bldg_desc_text = gr.Textbox(label="Description", lines=2)
                with gr.Row():
                    new_bldg_capacity_num = gr.Number(label="Capacity", precision=0, value=1)
                    new_bldg_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
                new_bldg_sys_inst_text = gr.Textbox(label="System Instruction", lines=4)
                create_bldg_btn = gr.Button("新規Buildingを作成", variant="primary")
                create_bldg_status = gr.Textbox(label="Status", interactive=False)

                create_bldg_btn.click(fn=create_building_ui, inputs=[new_bldg_name_text, new_bldg_desc_text, new_bldg_capacity_num, new_bldg_sys_inst_text, new_bldg_city_dropdown], outputs=[create_bldg_status, building_df])

    with gr.Accordion("AI管理", open=False):
        with gr.Tabs():
            with gr.TabItem("編集/削除"):
                ai_df = gr.DataFrame(value=manager.get_ais_df, interactive=False, label="AIs in this World")
                with gr.Row():
                    ai_id_text = gr.Textbox(label="AI ID", interactive=False)
                    ai_name_text = gr.Textbox(label="AI Name")
                    ai_home_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
                    ai_model_dropdown = gr.Dropdown(choices=MODEL_CHOICES, label="Default Model", allow_custom_value=True)
                    ai_interaction_mode_dropdown = gr.Dropdown(choices=["auto", "manual", "sleep"], label="対話モード", value="auto")
                ai_desc_text = gr.Textbox(label="Description", lines=2)
                ai_sys_prompt_text = gr.Textbox(label="System Prompt", lines=8)
                with gr.Row():
                    is_dispatched_checkbox = gr.Checkbox(label="派遣中 (編集不可)", interactive=False)
                    save_ai_btn = gr.Button("AI設定を保存")
                    delete_ai_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
                    delete_ai_btn = gr.Button("AIを削除", variant="stop", interactive=False, scale=1)
                ai_status_display = gr.Textbox(label="Status", interactive=False)

                gr.Markdown("---")
                gr.Markdown("### AIを移動させる")
                with gr.Row():
                    ai_current_location_text = gr.Textbox(label="現在地", interactive=False, scale=2)
                    ai_move_target_dropdown = gr.Dropdown(
                        choices=BUILDING_CHOICES,
                        label="移動先",
                        scale=2
                    )
                    move_ai_btn = gr.Button("移動実行", scale=1)
                move_ai_status_display = gr.Textbox(label="Status", interactive=False)


                # --- AI Event Handlers (Update/Delete) ---
                ai_df.select(fn=on_select_ai, inputs=None, outputs=[ai_id_text, ai_name_text, ai_desc_text, ai_sys_prompt_text, ai_home_city_dropdown, ai_model_dropdown, is_dispatched_checkbox, ai_interaction_mode_dropdown, ai_current_location_text])
                save_ai_btn.click(fn=update_ai_ui, inputs=[ai_id_text, ai_name_text, ai_desc_text, ai_sys_prompt_text, ai_home_city_dropdown, ai_model_dropdown, ai_interaction_mode_dropdown], outputs=[ai_status_display, ai_df])
                delete_ai_confirm_check.change(fn=toggle_delete_button, inputs=delete_ai_confirm_check, outputs=delete_ai_btn)
                delete_ai_btn.click(fn=delete_ai_ui, inputs=[ai_id_text, delete_ai_confirm_check], outputs=[ai_status_display, ai_df])
                move_ai_btn.click(fn=move_ai_ui, inputs=[ai_id_text, ai_move_target_dropdown], outputs=[move_ai_status_display, ai_current_location_text])

            with gr.TabItem("新規作成"):
                gr.Markdown("新しいAIを作成します。作成すると、そのAIの個室も自動で生成されます。")
                new_ai_name_text = gr.Textbox(label="New AI Name")
                new_ai_home_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
                new_ai_sys_prompt_text = gr.Textbox(label="System Prompt", lines=6, value="ここにペルソナの基本設定を記述します。")
                create_ai_btn = gr.Button("新規AIを作成", variant="primary")
                create_ai_status = gr.Textbox(label="Status", interactive=False)

                create_ai_btn.click(fn=create_ai_ui, inputs=[new_ai_name_text, new_ai_sys_prompt_text, new_ai_home_city_dropdown], outputs=[create_ai_status, ai_df])

    with gr.Accordion("ツール管理", open=False):
        gr.Markdown("AIが利用可能なツールを定義します。")
        with gr.Tabs():
            with gr.TabItem("編集/削除"):
                tool_df = gr.DataFrame(value=manager.get_tools_df, interactive=False, label="Available Tools")
                with gr.Row():
                    tool_id_text = gr.Textbox(label="Tool ID", interactive=False)
                    tool_name_text = gr.Textbox(label="Tool Name")
                tool_desc_text = gr.Textbox(label="Description", lines=2)
                with gr.Row():
                    tool_module_path_text = gr.Textbox(label="Module Path", placeholder="e.g., tools.defs.calculator")
                    tool_function_name_text = gr.Textbox(label="Function Name", placeholder="e.g., calculate_expression")
                with gr.Row():
                    save_tool_btn = gr.Button("ツール設定を保存")
                    delete_tool_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
                    delete_tool_btn = gr.Button("ツールを削除", variant="stop", interactive=False, scale=1)
                tool_status_display = gr.Textbox(label="Status", interactive=False)
            with gr.TabItem("新規作成"):
                gr.Markdown("新しいツールを登録します。")
                new_tool_name_text = gr.Textbox(label="New Tool Name")
                new_tool_desc_text = gr.Textbox(label="Description", lines=2)
                with gr.Row():
                    new_tool_module_path_text = gr.Textbox(label="Module Path", placeholder="e.g., tools.defs.new_tool")
                    new_tool_function_name_text = gr.Textbox(label="Function Name", placeholder="e.g., run_new_tool")
                create_tool_btn = gr.Button("新規ツールを作成", variant="primary")
                create_tool_status = gr.Textbox(label="Status", interactive=False)

    with gr.Accordion("ブループリント管理", open=False):
        gr.Markdown("エンティティの設計図を作成・管理し、ワールドに配置します。\n行を選択する際はBLUEPRINT_ID列をクリックしてください。")
        with gr.Tabs():
            with gr.TabItem("編集"):
                blueprint_df = gr.DataFrame(value=manager.get_blueprints_df, interactive=False, label="Blueprints in this World")
                with gr.Row():
                    bp_id_text = gr.Textbox(label="Blueprint ID", interactive=False)
                    bp_name_text = gr.Textbox(label="Blueprint Name")
                    bp_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
                    bp_entity_type_text = gr.Textbox(label="Entity Type", value="ai")
                bp_desc_text = gr.Textbox(label="Description", lines=2)
                bp_sys_prompt_text = gr.Textbox(label="Base System Prompt", lines=6)
                with gr.Row():
                    bp_create_btn = gr.Button("新規作成")
                    bp_update_btn = gr.Button("更新")
                    bp_delete_btn = gr.Button("削除", variant="stop")
                bp_status_display = gr.Textbox(label="Status", interactive=False)

            with gr.TabItem("スポーン"):
                gr.Markdown("リストからブループリントを選択し、新しいエンティティをワールドに配置します。")
                all_blueprints_df = manager.get_blueprints_df()
                blueprint_choices = list(zip(all_blueprints_df['NAME'], all_blueprints_df['BLUEPRINT_ID']))
                spawn_bp_dropdown = gr.Dropdown(choices=blueprint_choices, label="使用するブループリント", type="value")
                spawn_entity_name_text = gr.Textbox(label="新しいエンティティ名")
                spawn_building_dropdown = gr.Dropdown(choices=BUILDING_CHOICES, label="配置先の建物")
                spawn_btn = gr.Button("スポーン実行", variant="primary")
                spawn_status_display = gr.Textbox(label="Status", interactive=False)

        # --- Blueprint Handlers ---
        def on_select_blueprint(evt: gr.SelectData):
            # evt.indexの代わりにevt.valueを使用する
            # evt.valueには選択された行の最初の列の値（BLUEPRINT_ID）が入る
            if evt.value is None:
                # 選択が解除されたらフォームをクリア
                return "", "", "", None, "", "ai"

            try:
                # valueは文字列として渡されることがあるのでintに変換
                blueprint_id = int(evt.value)
            except (ValueError, TypeError):
                # ヘッダーをクリックした場合など、intに変換できない場合はフォームをクリア
                return "", "", "", None, "", "ai"
            details = manager.get_blueprint_details(blueprint_id)
            if not details: return "", "", "", None, "", "ai"
            return details['BLUEPRINT_ID'], details['NAME'], details['DESCRIPTION'], int(details['CITYID']), details['BASE_SYSTEM_PROMPT'], details['ENTITY_TYPE']

        def create_blueprint_ui(name, desc, city_id, sys_prompt, entity_type):
            if not all([name, city_id, sys_prompt, entity_type]): return "Error: Name, City, System Prompt, and Entity Type are required.", gr.update()
            result = manager.create_blueprint(name, desc, city_id, sys_prompt, entity_type)
            return result, manager.get_blueprints_df()

        def update_blueprint_ui(bp_id, name, desc, city_id, sys_prompt, entity_type):
            if not bp_id: return "Error: Select a blueprint to update.", gr.update()
            result = manager.update_blueprint(int(bp_id), name, desc, city_id, sys_prompt, entity_type)
            return result, manager.get_blueprints_df()

        def delete_blueprint_ui(bp_id):
            if not bp_id: return "Error: Select a blueprint to delete.", gr.update()
            result = manager.delete_blueprint(int(bp_id))
            return result, manager.get_blueprints_df()

        def spawn_entity_ui(blueprint_id, entity_name, building_name):
            if not all([blueprint_id, entity_name, building_name]): return "Error: Blueprint, Entity Name, and Target Building are required.", gr.update()
            building_id = BUILDING_NAME_TO_ID_MAP.get(building_name)
            if not building_id: return f"Error: Building '{building_name}' not found.", gr.update()
            success, message = manager.spawn_entity_from_blueprint(blueprint_id, entity_name, building_id)
            return message, manager.get_ais_df()

        blueprint_df.select(fn=on_select_blueprint, inputs=None, outputs=[bp_id_text, bp_name_text, bp_desc_text, bp_city_dropdown, bp_sys_prompt_text, bp_entity_type_text])
        bp_create_btn.click(fn=create_blueprint_ui, inputs=[bp_name_text, bp_desc_text, bp_city_dropdown, bp_sys_prompt_text, bp_entity_type_text], outputs=[bp_status_display, blueprint_df])
        bp_update_btn.click(fn=update_blueprint_ui, inputs=[bp_id_text, bp_name_text, bp_desc_text, bp_city_dropdown, bp_sys_prompt_text, bp_entity_type_text], outputs=[bp_status_display, blueprint_df])
        bp_delete_btn.click(fn=delete_blueprint_ui, inputs=[bp_id_text], outputs=[bp_status_display, blueprint_df])
        spawn_btn.click(fn=spawn_entity_ui, inputs=[spawn_bp_dropdown, spawn_entity_name_text, spawn_building_dropdown], outputs=[spawn_status_display, ai_df])

        # --- Tool Handlers ---
        tool_df.select(fn=on_select_tool, inputs=None, outputs=[tool_id_text, tool_name_text, tool_desc_text, tool_module_path_text, tool_function_name_text])
        save_tool_btn.click(fn=update_tool_ui, inputs=[tool_id_text, tool_name_text, tool_desc_text, tool_module_path_text, tool_function_name_text], outputs=[tool_status_display, tool_df])
        delete_tool_confirm_check.change(fn=toggle_delete_button, inputs=delete_tool_confirm_check, outputs=delete_tool_btn)
        delete_tool_btn.click(fn=delete_tool_ui, inputs=[tool_id_text, delete_tool_confirm_check], outputs=[tool_status_display, tool_df])
        create_tool_btn.click(fn=create_tool_ui, inputs=[new_tool_name_text, new_tool_desc_text, new_tool_module_path_text, new_tool_function_name_text], outputs=[create_tool_status, tool_df])

    with gr.Accordion("バックアップ/リストア管理", open=False):
        gr.Markdown("現在のワールドの状態をバックアップしたり、過去のバックアップから復元します。**リストア後はアプリケーションの再起動が必須です。**")
        
        backup_df = gr.DataFrame(value=manager.get_backups, interactive=False, label="利用可能なバックアップ")
        
        with gr.Row():
            selected_backup_dropdown = gr.Dropdown(label="操作対象のバックアップ", choices=manager.get_backups()['Backup Name'].tolist() if not manager.get_backups().empty else [], scale=2)
            restore_confirm_check = gr.Checkbox(label="リストアを確認", value=False, scale=1)
            restore_btn = gr.Button("リストア実行", variant="primary", interactive=False, scale=1)
            delete_backup_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
            delete_backup_btn = gr.Button("削除", variant="stop", interactive=False, scale=1)

        with gr.Row():
            new_backup_name_text = gr.Textbox(label="新しいバックアップ名 (英数字のみ)", scale=3)
            create_backup_btn = gr.Button("現在のワールドをバックアップ", scale=1)
        
        backup_status_display = gr.Textbox(label="Status", interactive=False)

        # --- Backup/Restore Handlers ---
        def update_backup_components():
            df = manager.get_backups()
            choices = df['Backup Name'].tolist() if not df.empty else []
            return gr.update(value=df), gr.update(choices=choices, value=None)

        def create_backup_ui(name):
            if not name: return "Error: Backup name is required.", gr.update(), gr.update()
            result = manager.backup_world(name)
            return result, *update_backup_components()

        def restore_backup_ui(name, confirmed):
            if not confirmed: return "Error: Please check the confirmation box to restore."
            if not name: return "Error: Select a backup to restore."
            return manager.restore_world(name)

        def delete_backup_ui(name, confirmed):
            if not confirmed: return "Error: Please check the confirmation box to delete.", gr.update(), gr.update()
            if not name: return "Error: Select a backup to delete.", gr.update(), gr.update()
            result = manager.delete_backup(name)
            return result, *update_backup_components()

        create_backup_btn.click(fn=create_backup_ui, inputs=[new_backup_name_text], outputs=[backup_status_display, backup_df, selected_backup_dropdown])
        restore_confirm_check.change(fn=toggle_delete_button, inputs=restore_confirm_check, outputs=restore_btn)
        restore_btn.click(fn=restore_backup_ui, inputs=[selected_backup_dropdown, restore_confirm_check], outputs=[backup_status_display])
        delete_backup_confirm_check.change(fn=toggle_delete_button, inputs=delete_backup_confirm_check, outputs=delete_backup_btn)
        delete_backup_btn.click(fn=delete_backup_ui, inputs=[selected_backup_dropdown, delete_backup_confirm_check], outputs=[backup_status_display, backup_df, selected_backup_dropdown])

    with gr.Accordion("ワールド・イベント", open=False):
        gr.Markdown("ワールド全体に影響を与えるイベントを発生させます。イベントはシステムメッセージとして各Buildingのログに記録され、AIたちの新たな行動のきっかけとなります。")
        with gr.Row():
            world_event_text = gr.Textbox(label="イベントメッセージ", placeholder="例: 空にオーロラが現れた。", scale=3)
            trigger_event_btn = gr.Button("イベントを発生させる", variant="primary", scale=1)
        world_event_status_display = gr.Textbox(label="Status", interactive=False)

        trigger_event_btn.click(fn=manager.trigger_world_event, inputs=[world_event_text], outputs=[world_event_status_display])


    # --- ★ Refresh Handler Definition ---
    def refresh_world_editor_data():
        """Refreshes all DataFrames and related components in the world editor."""
        logging.info("Refreshing all world editor DataFrames.")
        
        cities = manager.get_cities_df()
        buildings = manager.get_buildings_df()
        ais = manager.get_ais_df()
        blueprints = manager.get_blueprints_df()
        backups = manager.get_backups()
        tools = manager.get_tools_df()
        
        backup_choices = backups['Backup Name'].tolist() if not backups.empty else []
        
        return (
            cities,
            buildings,
            ais,
            blueprints,
            backups,
            tools,
            gr.update(choices=backup_choices, value=None) # ドロップダウンも更新
        )

    # --- ★ Connect Refresh Button to all DataFrames ---
    refresh_editor_btn.click(
        fn=refresh_world_editor_data, inputs=None,
        outputs=[city_df, building_df, ai_df, blueprint_df, backup_df, tool_df, selected_backup_dropdown]
    )

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
    with gr.Blocks(fill_width=True, head=HEAD_VIEWPORT, css=NOTE_CSS, title=f"SAIVerse City: {args.city_name}", theme=gr.themes.Soft()) as demo:
        with gr.Sidebar(open=False, width=340, elem_id="sample_sidebar", elem_classes=["saiverse-sidebar"]):
            gr.Markdown("サンプルのサイドバーです")
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
                with gr.Group(elem_id="chat_wrap"):
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

                with gr.Accordion("会話を終える", open=False):
                    with gr.Row():
                        end_conv_persona_dropdown = gr.Dropdown(
                            choices=manager.get_conversing_personas(),
                            label="会話を終えるペルソナを選択",
                            interactive=True,
                            scale=3
                        )
                        end_conv_btn = gr.Button("会話を終了", scale=1)

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
                    model_drop = gr.Dropdown(choices=MODEL_CHOICES, value="None", label="システムデフォルトモデル (一時的な一括上書き)")

                # --- Event Handlers ---
                submit.click(respond_stream, txt, [chatbot, move_building_dropdown, summon_persona_dropdown, end_conv_persona_dropdown])
                txt.submit(respond_stream, txt, [chatbot, move_building_dropdown, summon_persona_dropdown, end_conv_persona_dropdown]) # Enter key submission
                move_btn.click(fn=move_user_ui, inputs=[move_building_dropdown], outputs=[chatbot, user_location_display, summon_persona_dropdown, end_conv_persona_dropdown])
                summon_btn.click(fn=call_persona_ui, inputs=[summon_persona_dropdown], outputs=[chatbot, summon_persona_dropdown, end_conv_persona_dropdown])
                login_btn.click(
                    fn=login_ui,
                    inputs=None,
                    outputs=[login_status_display, summon_persona_dropdown, end_conv_persona_dropdown]
                )
                logout_btn.click(fn=logout_ui, inputs=None, outputs=login_status_display)
                model_drop.change(select_model, model_drop, chatbot)
                online_btn.click(fn=manager.switch_to_online_mode, inputs=None, outputs=sds_status_display)
                offline_btn.click(fn=manager.switch_to_offline_mode, inputs=None, outputs=sds_status_display)
                end_conv_btn.click(
                    fn=end_conversation_ui,
                    inputs=[end_conv_persona_dropdown],
                    outputs=[chatbot, summon_persona_dropdown, end_conv_persona_dropdown]
                )

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

            with gr.TabItem("ワールドエディタ"):
                create_world_editor_ui() # This function now contains all editor sections


        # UIロード時にJavaScriptを実行し、5秒ごとの自動更新タイマーを設定する
        js_auto_refresh = """
        () => {
            const markSidebars = () => {
                let found = false;
                document.querySelectorAll('.sidebar').forEach((el) => {
                    if (!el.classList.contains('saiverse-sidebar')) {
                        el.classList.add('saiverse-sidebar');
                    }
                    const isMobile = window.matchMedia('(max-width: 768px)').matches;
                    const widthValue = isMobile ? '80vw' : '20vw';
                    const offsetValue = `calc(-1 * ${widthValue})`;
                    el.style.setProperty('width', widthValue, 'important');
                    if (el.classList.contains('right')) {
                        el.style.removeProperty('left');
                        el.style.setProperty('right', offsetValue, 'important');
                    } else {
                        el.style.removeProperty('right');
                        el.style.setProperty('left', offsetValue, 'important');
                    }
                    found = true;
                });
                return found;
            };

            if (!markSidebars()) {
                let attempts = 0;
                const watcher = setInterval(() => {
                    attempts += 1;
                    if (markSidebars() || attempts > 20) {
                        clearInterval(watcher);
                    }
                }, 250);
            }

            setInterval(() => {
                const button = document.getElementById('auto_refresh_log_btn');
                if (button) {
                    button.click();
                }
                markSidebars();
            }, 5000);
        }
        """
        demo.load(None, None, None, js=js_auto_refresh)

    demo.launch(server_port=manager.ui_port, debug=True, share = True)


if __name__ == "__main__":
    main()
