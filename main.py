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
import pandas as pd

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

def update_building_ui(b_id: str, name: str, capacity_str: str, desc: str, sys_inst: str, city_id: Optional[int]):
    """UI handler to update building settings."""
    if not b_id: return "Error: Select a building to update.", gr.update()
    if city_id is None:
        return "Error: City must be selected.", gr.update()
    try:
        capacity = int(capacity_str)
    except (ValueError, TypeError):
        return "Error: Capacity must be a valid integer.", gr.update()
    
    result = manager.update_building(b_id, name, capacity, desc, sys_inst, city_id)
    return result, manager.get_buildings_df()

def on_select_building(evt: gr.SelectData):
    """Handler for when a building is selected in the DataFrame."""
    if evt.value is None: return "", "", 1, "", "", None
    row_index = evt.index[0]
    df = manager.get_buildings_df()
    selected_row = df.iloc[row_index]
    return (
        selected_row['BUILDINGID'], selected_row['BUILDINGNAME'], selected_row['CAPACITY'],
        selected_row['DESCRIPTION'], selected_row['SYSTEM_INSTRUCTION'], int(selected_row['CITYID'])
    )

def on_select_ai(evt: gr.SelectData):
    """Handler for when an AI is selected in the DataFrame."""
    if evt.index is None: return "", "", "", "", None, "", False
    row_index = evt.index[0]
    # We need the full DF to get the ID, not just the visible part
    df = manager.get_ais_df()
    ai_id = df.iloc[row_index]['AIID']
    details = manager.get_ai_details(ai_id)
    if not details: return "", "", "", "", None, "", False

    return (
        details['AIID'],
        details['AINAME'],
        details['DESCRIPTION'],
        details['SYSTEMPROMPT'],
        int(details['HOME_CITYID']),
        details['DEFAULT_MODEL'],
        details['IS_DISPATCHED']
    )

def update_ai_ui(ai_id: str, name: str, desc: str, sys_prompt: str, home_city_id: int, model: str):
    """UI handler to update AI settings."""
    if not ai_id:
        return "Error: Select an AI to update.", gr.update()
    if not home_city_id:
        return "Error: Home City must be selected.", gr.update()
    
    result = manager.update_ai(ai_id, name, desc, sys_prompt, home_city_id, model)
    return result, manager.get_ais_df()

def create_world_editor_ui():
    """Creates all UI components for the World Editor tab."""
    # --- ★ UI構築の最初にCity情報を一度だけ取得 ---
    all_cities_df = manager.get_cities_df()
    city_choices = list(zip(all_cities_df['CITYNAME'], all_cities_df['CITYID'].astype(int)))

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
                building_desc_text = gr.Textbox(label="Description", lines=3)
                building_sys_inst_text = gr.Textbox(label="System Instruction", lines=5)
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
                building_df.select(fn=on_select_building, inputs=None, outputs=[building_id_text, building_name_text, building_capacity_num, building_desc_text, building_sys_inst_text, building_city_dropdown])
                save_building_btn.click(fn=update_building_ui, inputs=[building_id_text, building_name_text, building_capacity_num, building_desc_text, building_sys_inst_text, building_city_dropdown], outputs=[building_status_display, building_df])
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
                ai_desc_text = gr.Textbox(label="Description", lines=2)
                ai_sys_prompt_text = gr.Textbox(label="System Prompt", lines=8)
                with gr.Row():
                    is_dispatched_checkbox = gr.Checkbox(label="派遣中 (編集不可)", interactive=False)
                    save_ai_btn = gr.Button("AI設定を保存")
                    delete_ai_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
                    delete_ai_btn = gr.Button("AIを削除", variant="stop", interactive=False, scale=1)
                ai_status_display = gr.Textbox(label="Status", interactive=False)

                # --- AI Event Handlers (Update/Delete) ---
                ai_df.select(fn=on_select_ai, inputs=None, outputs=[ai_id_text, ai_name_text, ai_desc_text, ai_sys_prompt_text, ai_home_city_dropdown, ai_model_dropdown, is_dispatched_checkbox])
                save_ai_btn.click(fn=update_ai_ui, inputs=[ai_id_text, ai_name_text, ai_desc_text, ai_sys_prompt_text, ai_home_city_dropdown, ai_model_dropdown], outputs=[ai_status_display, ai_df])
                delete_ai_confirm_check.change(fn=toggle_delete_button, inputs=delete_ai_confirm_check, outputs=delete_ai_btn)
                delete_ai_btn.click(fn=delete_ai_ui, inputs=[ai_id_text, delete_ai_confirm_check], outputs=[ai_status_display, ai_df])

            with gr.TabItem("新規作成"):
                gr.Markdown("新しいAIを作成します。作成すると、そのAIの個室も自動で生成されます。")
                new_ai_name_text = gr.Textbox(label="New AI Name")
                new_ai_home_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
                new_ai_sys_prompt_text = gr.Textbox(label="System Prompt", lines=6, value="ここにペルソナの基本設定を記述します。")
                create_ai_btn = gr.Button("新規AIを作成", variant="primary")
                create_ai_status = gr.Textbox(label="Status", interactive=False)

                create_ai_btn.click(fn=create_ai_ui, inputs=[new_ai_name_text, new_ai_sys_prompt_text, new_ai_home_city_dropdown], outputs=[create_ai_status, ai_df])

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
        
        backup_choices = backups['Backup Name'].tolist() if not backups.empty else []
        
        return (
            cities,
            buildings,
            ais,
            blueprints,
            backups,
            gr.update(choices=backup_choices, value=None) # ドロップダウンも更新
        )

    # --- ★ Connect Refresh Button to all DataFrames ---
    refresh_editor_btn.click(
        fn=refresh_world_editor_data, inputs=None,
        outputs=[city_df, building_df, ai_df, blueprint_df, backup_df, selected_backup_dropdown])

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

            with gr.TabItem("ワールドエディタ"):
                create_world_editor_ui() # This function now contains all editor sections


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
