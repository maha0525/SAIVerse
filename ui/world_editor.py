from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr

from ui import state as ui_state


def _require_manager():
    manager = ui_state.manager
    if manager is None:
        raise RuntimeError("Manager not initialised")
    return manager


def update_city_ui(city_id_str: str, name: str, desc: str, online_mode: bool, ui_port_str: str, api_port_str: str, timezone_name: str, host_avatar_path: str, host_avatar_upload):
    manager = _require_manager()
    if not city_id_str:
        return "Error: Select a city to update.", gr.update()
    try:
        city_id = int(city_id_str)
        ui_port = int(ui_port_str)
        api_port = int(api_port_str)
    except (ValueError, TypeError):
        return "Error: Port numbers must be valid integers.", gr.update()

    result = manager.update_city(city_id, name, desc, online_mode, ui_port, api_port, timezone_name, host_avatar_path, host_avatar_upload)
    return result, manager.get_cities_df()


def on_select_city(evt: gr.SelectData):
    manager = _require_manager()
    if evt.value is None:
        return "", "", "", False, "", "", "UTC", ""
    row_index = evt.index[0]
    df = manager.get_cities_df()
    selected_row = df.iloc[row_index]
    return (
        selected_row["CITYID"],
        selected_row["CITYNAME"],
        selected_row["DESCRIPTION"],
        selected_row["START_IN_ONLINE_MODE"],
        selected_row["UI_PORT"],
        selected_row["API_PORT"],
        selected_row.get("TIMEZONE", "UTC"),
        selected_row.get("HOST_AVATAR_IMAGE", ""),
    )


def load_user_profile_ui():
    manager = _require_manager()
    name, avatar_path = manager.get_user_profile()
    return name, avatar_path


def update_user_profile_ui(name: str, avatar_path: str, avatar_upload):
    manager = _require_manager()
    result = manager.update_user_profile(name, avatar_path, avatar_upload)
    fresh_name, fresh_path = manager.get_user_profile()
    return result, fresh_name, fresh_path, gr.update(value=None)


def update_building_ui(b_id: str, name: str, capacity_str: str, desc: str, sys_inst: str, city_id: Optional[int], tool_ids: List[int], interval_str: str):
    manager = _require_manager()
    if not b_id:
        return "Error: Select a building to update.", gr.update()
    if city_id is None:
        return "Error: City must be selected.", gr.update()
    try:
        capacity = int(capacity_str)
        interval = int(interval_str)
    except (ValueError, TypeError):
        return "Error: Capacity and Interval must be valid integers.", gr.update()

    result = manager.update_building(b_id, name, capacity, desc, sys_inst, city_id, tool_ids, interval)
    ui_state.refresh_building_caches()
    return result, manager.get_buildings_df()


def on_select_building(evt: gr.SelectData):
    manager = _require_manager()
    if evt.value is None:
        return "", "", 1, "", "", None, None, 10
    row_index = evt.index[0]
    df = manager.get_buildings_df()
    selected_row = df.iloc[row_index]
    linked_tool_ids = manager.get_linked_tool_ids(selected_row["BUILDINGID"])
    return (
        selected_row["BUILDINGID"],
        selected_row["BUILDINGNAME"],
        selected_row["CAPACITY"],
        selected_row["DESCRIPTION"],
        selected_row["SYSTEM_INSTRUCTION"],
        int(selected_row["CITYID"]),
        linked_tool_ids,
        selected_row.get("AUTO_INTERVAL_SEC", 10),
    )


def on_select_ai(evt: gr.SelectData):
    manager = _require_manager()
    if evt.index is None:
        return "", "", "", "", None, "", False, "auto", "", "", gr.update(value=None)
    row_index = evt.index[0]
    df = manager.get_ais_df()
    ai_id = df.iloc[row_index]["AIID"]
    details = manager.get_ai_details(ai_id)
    if not details:
        return "", "", "", "", None, "", False, "auto", "", "", gr.update(value=None)

    current_location_name = "不明"
    if ai_id in manager.personas:
        current_building_id = manager.personas[ai_id].current_building_id
        if current_building_id in manager.building_map:
            current_location_name = manager.building_map[current_building_id].name

    return (
        details["AIID"],
        details["AINAME"],
        details["DESCRIPTION"],
        details["SYSTEMPROMPT"],
        int(details["HOME_CITYID"]),
        details["DEFAULT_MODEL"],
        details["IS_DISPATCHED"],
        details["INTERACTION_MODE"],
        current_location_name,
        details.get("AVATAR_IMAGE") or "",
        gr.update(value=None),
    )


def update_ai_ui(ai_id: str, name: str, desc: str, sys_prompt: str, home_city_id, model: str, interaction_mode: str, avatar_path: str, avatar_file):
    manager = _require_manager()
    if not ai_id:
        return "Error: Select an AI to update.", gr.update()
    if home_city_id is None or home_city_id == "":
        return "Error: Home City must be selected.", gr.update()

    if isinstance(home_city_id, str):
        try:
            home_city_id = int(home_city_id)
        except ValueError:
            return "Error: Home City must be an integer.", gr.update()

    upload_path = None
    if isinstance(avatar_file, list):
        upload_path = avatar_file[0] if avatar_file else None
    elif isinstance(avatar_file, dict):
        upload_path = avatar_file.get("name") or avatar_file.get("path")
    else:
        upload_path = avatar_file

    result = manager.update_ai(ai_id, name, desc, sys_prompt, home_city_id, model, interaction_mode, avatar_path, upload_path)
    return result, manager.get_ais_df()


def move_ai_ui(ai_id: str, target_building_name: str):
    manager = _require_manager()
    if not ai_id or not target_building_name:
        return "Error: AIと移動先を選択してください。", gr.update()

    target_building_id = ui_state.building_name_to_id.get(target_building_name)
    if not target_building_id:
        return f"Error: 建物 '{target_building_name}' のIDが見つかりません。", gr.update()

    result = manager.move_ai_from_editor(ai_id, target_building_id)
    new_location_name = "不明"
    if ai_id in manager.personas:
        current_building_id = manager.personas[ai_id].current_building_id
        if current_building_id in manager.building_map:
            new_location_name = manager.building_map[current_building_id].name
    return result, new_location_name


def on_select_tool(evt: gr.SelectData):
    manager = _require_manager()
    if evt.index is None:
        return "", "", "", "", ""
    row_index = evt.index[0]
    df = manager.get_tools_df()
    selected_row = df.iloc[row_index]
    details = manager.get_tool_details(int(selected_row["TOOLID"]))
    if not details:
        return "", "", "", "", ""
    return (
        details["TOOLID"],
        details["TOOLNAME"],
        details["DESCRIPTION"],
        details["MODULE_PATH"],
        details["FUNCTION_NAME"],
    )


def create_world_editor_ui():
    manager = _require_manager()
    all_cities_df = manager.get_cities_df()
    city_choices: List[Tuple[str, int]] = list(zip(all_cities_df["CITYNAME"], all_cities_df["CITYID"].astype(int)))

    all_tools_df = manager.get_tools_df()
    tool_choices = list(zip(all_tools_df["TOOLNAME"], all_tools_df["TOOLID"].astype(int))) if not all_tools_df.empty else []

    with gr.Row():
        refresh_editor_btn = gr.Button("🔄 ワールドエディタ全体を更新", variant="secondary", elem_id="world_editor_refresh_btn")

    def create_city_ui(name, desc, ui_port, api_port, timezone_name):
        if not all([name, ui_port, api_port, timezone_name]):
            return "Error: Name, UI Port, API Port, and Timezone are required.", gr.update()
        result = manager.create_city(name, desc, int(ui_port), int(api_port), timezone_name)
        return result, manager.get_cities_df()

    def delete_city_ui(city_id_str, confirmed):
        if not confirmed:
            return "Error: Please check the confirmation box to delete.", gr.update()
        if not city_id_str:
            return "Error: Select a city to delete.", gr.update()
        result = manager.delete_city(int(city_id_str))
        return result, manager.get_cities_df()

    def create_building_ui(name, desc, capacity, sys_inst, city_id):
        if not all([name, capacity, city_id]):
            return "Error: Name, Capacity, and City are required.", gr.update()
        result = manager.create_building(name, desc, int(capacity), sys_inst, city_id)
        ui_state.refresh_building_caches()
        return result, manager.get_buildings_df()

    def delete_building_ui(b_id, confirmed):
        if not confirmed:
            return "Error: Please check the confirmation box to delete.", gr.update()
        if not b_id:
            return "Error: Select a building to delete.", gr.update()
        result = manager.delete_building(b_id)
        ui_state.refresh_building_caches()
        return result, manager.get_buildings_df()

    def create_ai_ui(name, sys_prompt, home_city_id):
        if not all([name, sys_prompt, home_city_id]):
            return "Error: Name, System Prompt, and Home City are required.", gr.update()
        result = manager.create_ai(name, sys_prompt, home_city_id)
        return result, manager.get_ais_df()

    def delete_ai_ui(ai_id, confirmed):
        if not confirmed:
            return "Error: Please check the confirmation box to delete.", gr.update()
        if not ai_id:
            return "Error: Select an AI to delete.", gr.update()
        result = manager.delete_ai(ai_id)
        return result, manager.get_ais_df()

    def create_tool_ui(name, desc, module_path, func_name):
        if not all([name, module_path, func_name]):
            return "Error: Name, Module Path, and Function Name are required.", gr.update()
        result = manager.create_tool(name, desc, module_path, func_name)
        return result, manager.get_tools_df()

    def update_tool_ui(tool_id, name, desc, module_path, func_name):
        if not tool_id:
            return "Error: Select a tool to update.", gr.update()
        result = manager.update_tool(int(tool_id), name, desc, module_path, func_name)
        return result, manager.get_tools_df()

    def delete_tool_ui(tool_id, confirmed):
        if not confirmed:
            return "Error: Please check the confirmation box to delete.", gr.update()
        if not tool_id:
            return "Error: Select a tool to delete.", gr.update()
        result = manager.delete_tool(int(tool_id))
        return result, manager.get_tools_df()

    def on_select_item(evt: gr.SelectData):
        manager = _require_manager()
        if evt.index is None:
            return "", "", "object", "", "world", "", ""
        row_index = evt.index[0]
        df = manager.get_items_df()
        if df.empty or row_index >= len(df):
            return "", "", "object", "", "world", "", ""
        item_id = df.iloc[row_index]["ITEM_ID"]
        details = manager.get_item_details(item_id)
        if not details:
            return "", "", "object", "", "world", "", ""
        owner_kind = (details.get("OWNER_KIND") or "world").strip() or "world"
        owner_id = details.get("OWNER_ID") or ""
        state_json = details.get("STATE_JSON") or ""
        return (
            details["ITEM_ID"],
            details["NAME"],
            details["TYPE"],
            details["DESCRIPTION"],
            owner_kind,
            owner_id,
            state_json,
        )

    def update_item_ui(item_id, name, item_type, description, owner_kind, owner_id, state_json):
        if not item_id:
            return "Error: Select an item to update.", gr.update()
        manager = _require_manager()
        normalized_kind = (owner_kind or "world").strip() or "world"
        owner_value = owner_id if normalized_kind != "world" else None
        result = manager.update_item(
            item_id,
            name or "",
            item_type or "object",
            description or "",
            normalized_kind,
            owner_value,
            state_json or "",
        )
        return result, manager.get_items_df()

    def delete_item_ui(item_id, confirmed):
        if not confirmed:
            return "Error: Please check the confirmation box to delete.", gr.update()
        if not item_id:
            return "Error: Select an item to delete.", gr.update()
        manager = _require_manager()
        result = manager.delete_item(item_id)
        return result, manager.get_items_df()

    def create_item_ui(name, item_type, description, owner_kind, owner_id, state_json):
        if not name:
            return "Error: Item name is required.", gr.update()
        manager = _require_manager()
        normalized_kind = (owner_kind or "world").strip() or "world"
        owner_value = owner_id if normalized_kind != "world" else None
        result = manager.create_item(
            name,
            item_type or "object",
            description or "",
            normalized_kind,
            owner_value,
            state_json or "",
        )
        return result, manager.get_items_df()

    with gr.Accordion("City管理", open=True):
        with gr.Tabs():
            with gr.TabItem("編集/削除"):
                city_df = gr.DataFrame(value=None, interactive=False, label="Cities in this World")
                with gr.Row():
                    city_id_text = gr.Textbox(label="City ID", interactive=False)
                    city_name_textbox = gr.Textbox(label="City Name")
                    city_ui_port_num = gr.Number(label="UI Port", precision=0)
                    city_api_port_num = gr.Number(label="API Port", precision=0)
                city_desc_textbox = gr.Textbox(label="Description", lines=3)
                city_timezone_textbox = gr.Textbox(label="Timezone (IANA形式)", value=lambda: manager.timezone_name, placeholder="例: Asia/Tokyo")
                city_host_avatar_path = gr.Textbox(label="Host Avatar Path", interactive=False)
                city_host_avatar_upload = gr.File(label="Host Avatar Upload", file_types=["image"], type="filepath")
                online_mode_checkbox = gr.Checkbox(label="次回起動時にオンラインモードで起動する")
                with gr.Row():
                    save_city_btn = gr.Button("City設定を保存")
                    delete_city_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
                    delete_city_btn = gr.Button("Cityを削除", variant="stop", interactive=False, scale=1)
                city_status_display = gr.Textbox(label="Status", interactive=False)

                def toggle_delete_button(is_checked):
                    return gr.update(interactive=is_checked)

                city_df.select(fn=on_select_city, inputs=None, outputs=[city_id_text, city_name_textbox, city_desc_textbox, online_mode_checkbox, city_ui_port_num, city_api_port_num, city_timezone_textbox, city_host_avatar_path])
                save_city_btn.click(fn=update_city_ui, inputs=[city_id_text, city_name_textbox, city_desc_textbox, online_mode_checkbox, city_ui_port_num, city_api_port_num, city_timezone_textbox, city_host_avatar_path, city_host_avatar_upload], outputs=[city_status_display, city_df])
                delete_city_confirm_check.change(fn=toggle_delete_button, inputs=delete_city_confirm_check, outputs=delete_city_btn)
                delete_city_btn.click(fn=delete_city_ui, inputs=[city_id_text, delete_city_confirm_check], outputs=[city_status_display, city_df])
            with gr.TabItem("新規作成"):
                gr.Markdown("新しいCityを作成します。作成後、アプリケーションの再起動が必要です。")
                with gr.Row():
                    new_city_name_text = gr.Textbox(label="City Name")
                    new_city_ui_port = gr.Number(label="UI Port", precision=0)
                    new_city_api_port = gr.Number(label="API Port", precision=0)
                new_city_desc_text = gr.Textbox(label="Description", lines=3)
                new_city_timezone_text = gr.Textbox(label="Timezone (IANA形式)", value="UTC", placeholder="例: Asia/Tokyo")
                create_city_btn = gr.Button("新規Cityを作成", variant="primary")
                create_city_status = gr.Textbox(label="Status", interactive=False)

                create_city_btn.click(fn=create_city_ui, inputs=[new_city_name_text, new_city_desc_text, new_city_ui_port, new_city_api_port, new_city_timezone_text], outputs=[create_city_status, city_df])

    with gr.Accordion("Building管理", open=False):
        with gr.Tabs():
            with gr.TabItem("編集/削除"):
                building_df = gr.DataFrame(value=None, interactive=False, label="Buildings in this World")
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
                ai_df = gr.DataFrame(value=None, interactive=False, label="AIs in this World")
                with gr.Row():
                    ai_id_text = gr.Textbox(label="AI ID", interactive=False)
                    ai_name_text = gr.Textbox(label="AI Name")
                    ai_home_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
                    ai_model_dropdown = gr.Dropdown(choices=ui_state.model_choices, label="Default Model", allow_custom_value=True)
                    ai_interaction_mode_dropdown = gr.Dropdown(choices=["auto", "manual", "sleep"], label="対話モード", value="auto")
                ai_desc_text = gr.Textbox(label="Description", lines=2)
                ai_sys_prompt_text = gr.Textbox(label="System Prompt", lines=8)
                with gr.Row():
                    ai_avatar_path_text = gr.Textbox(label="Avatar Image Path/URL", placeholder="例: assets/avatars/air.png")
                    ai_avatar_upload = gr.File(label="Upload New Avatar", file_types=["image"], type="filepath")
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
                    ai_move_target_dropdown = gr.Dropdown(choices=ui_state.building_choices, label="移動先", scale=2)
                    move_ai_btn = gr.Button("移動実行", scale=1)
                move_ai_status_display = gr.Textbox(label="Status", interactive=False)

                ai_df.select(fn=on_select_ai, inputs=None, outputs=[ai_id_text, ai_name_text, ai_desc_text, ai_sys_prompt_text, ai_home_city_dropdown, ai_model_dropdown, is_dispatched_checkbox, ai_interaction_mode_dropdown, ai_current_location_text, ai_avatar_path_text, ai_avatar_upload])
                save_ai_btn.click(fn=update_ai_ui, inputs=[ai_id_text, ai_name_text, ai_desc_text, ai_sys_prompt_text, ai_home_city_dropdown, ai_model_dropdown, ai_interaction_mode_dropdown, ai_avatar_path_text, ai_avatar_upload], outputs=[ai_status_display, ai_df])
                delete_ai_confirm_check.change(fn=toggle_delete_button, inputs=delete_ai_confirm_check, outputs=delete_ai_btn)
                delete_ai_btn.click(fn=delete_ai_ui, inputs=[ai_id_text, delete_ai_confirm_check], outputs=[ai_status_display, ai_df])
                move_ai_btn.click(fn=move_ai_ui, inputs=[ai_id_text, ai_move_target_dropdown], outputs=[move_ai_status_display, ai_current_location_text])

            with gr.TabItem("新規作成"):
                gr.Markdown("新しいAIを作成します。")
                new_ai_name_text = gr.Textbox(label="AI Name")
                new_ai_sys_prompt_text = gr.Textbox(label="System Prompt", lines=8)
                new_ai_home_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
                create_ai_btn = gr.Button("新規AIを作成", variant="primary")
                create_ai_status = gr.Textbox(label="Status", interactive=False)

                create_ai_btn.click(fn=create_ai_ui, inputs=[new_ai_name_text, new_ai_sys_prompt_text, new_ai_home_city_dropdown], outputs=[create_ai_status, ai_df])

    with gr.Accordion("ユーザー管理", open=False):
        gr.Markdown("ログインユーザーの表示名とアイコンを変更できます。")
        user_name_text = gr.Textbox(label="ユーザー名", value=lambda: manager.user_display_name)
        user_avatar_path = gr.Textbox(label="ユーザーアイコンの保存パス", interactive=False, value=lambda: manager.get_user_profile()[1])
        user_avatar_upload = gr.File(label="ユーザーアイコンをアップロード", file_types=["image"], type="filepath")
        with gr.Row():
            load_user_btn = gr.Button("最新の情報を読み込む")
            save_user_btn = gr.Button("ユーザー情報を保存", variant="primary")
        user_status = gr.Textbox(label="Status", interactive=False)

        load_user_btn.click(fn=load_user_profile_ui, inputs=None, outputs=[user_name_text, user_avatar_path])
        save_user_btn.click(
            fn=update_user_profile_ui,
            inputs=[user_name_text, user_avatar_path, user_avatar_upload],
            outputs=[user_status, user_name_text, user_avatar_path, user_avatar_upload],
        )

    with gr.Accordion("Blueprint管理", open=False):
        blueprint_df = gr.DataFrame(value=None, interactive=False, label="Blueprints")
        with gr.Row():
            bp_id_text = gr.Textbox(label="Blueprint ID", interactive=False)
            bp_name_text = gr.Textbox(label="Blueprint Name")
            bp_city_dropdown = gr.Dropdown(choices=city_choices, label="所属City", type="value")
        bp_desc_text = gr.Textbox(label="Description", lines=2)
        bp_sys_prompt_text = gr.Textbox(label="System Prompt", lines=5)
        bp_entity_type_text = gr.Textbox(label="Entity Type", placeholder="persona / building / tool")
        with gr.Row():
            bp_create_btn = gr.Button("Blueprint作成", variant="primary")
            bp_update_btn = gr.Button("Blueprint更新")
            bp_delete_btn = gr.Button("Blueprint削除", variant="stop")
        bp_status_display = gr.Textbox(label="Status", interactive=False)

        def on_select_blueprint(evt: gr.SelectData):
            if evt.index is None:
                return "", "", "", "", ""
            row_index = evt.index[0]
            df = manager.get_blueprints_df()
            selected_row = df.iloc[row_index]
            return (
                selected_row["BLUEPRINT_ID"],
                selected_row["NAME"],
                selected_row["DESCRIPTION"],
                int(selected_row["HOME_CITYID"]),
                selected_row.get("SYSTEM_PROMPT", ""),
            )

        def create_blueprint_ui(name, desc, city_id, sys_prompt, entity_type):
            if not all([name, city_id, entity_type]):
                return "Error: Name, City, and Entity Type are required.", gr.update()
            result = manager.create_blueprint(name, desc, city_id, sys_prompt, entity_type)
            return result, manager.get_blueprints_df()

        def update_blueprint_ui(bp_id, name, desc, city_id, sys_prompt, entity_type):
            if not bp_id:
                return "Error: Select a blueprint to update.", gr.update()
            result = manager.update_blueprint(bp_id, name, desc, city_id, sys_prompt, entity_type)
            return result, manager.get_blueprints_df()

        def delete_blueprint_ui(bp_id):
            if not bp_id:
                return "Error: Select a blueprint to delete.", gr.update()
            result = manager.delete_blueprint(bp_id)
            return result, manager.get_blueprints_df()

        def spawn_entity_ui(blueprint_id_value, entity_name, building_name):
            if not all([blueprint_id_value, entity_name, building_name]):
                return "Error: Blueprint, Entity Name, and Building are required.", gr.update()
            try:
                blueprint_id = int(blueprint_id_value)
            except (TypeError, ValueError):
                return "Error: Invalid blueprint selection.", gr.update()
            success, message = manager.spawn_entity_from_blueprint(blueprint_id, entity_name, building_name)
            updated_ai_df = manager.get_ais_df()
            return message, updated_ai_df

        blueprint_df.select(fn=on_select_blueprint, inputs=None, outputs=[bp_id_text, bp_name_text, bp_desc_text, bp_city_dropdown, bp_sys_prompt_text])
        bp_create_btn.click(fn=create_blueprint_ui, inputs=[bp_name_text, bp_desc_text, bp_city_dropdown, bp_sys_prompt_text, bp_entity_type_text], outputs=[bp_status_display, blueprint_df])
        bp_update_btn.click(fn=update_blueprint_ui, inputs=[bp_id_text, bp_name_text, bp_desc_text, bp_city_dropdown, bp_sys_prompt_text, bp_entity_type_text], outputs=[bp_status_display, blueprint_df])
        bp_delete_btn.click(fn=delete_blueprint_ui, inputs=[bp_id_text], outputs=[bp_status_display, blueprint_df])

        gr.Markdown("### BlueprintからAIをスポーン")
        spawn_bp_dropdown = gr.Dropdown(choices=manager.get_blueprint_choices(), label="Blueprint", type="value")
        spawn_entity_name_text = gr.Textbox(label="Entity Name")
        spawn_building_dropdown = gr.Dropdown(choices=ui_state.building_choices, label="Building", type="value")
        spawn_btn = gr.Button("スポーン実行", variant="primary")
        spawn_status_display = gr.Textbox(label="Status", interactive=False)

        spawn_btn.click(fn=spawn_entity_ui, inputs=[spawn_bp_dropdown, spawn_entity_name_text, spawn_building_dropdown], outputs=[spawn_status_display, ai_df])

    with gr.Accordion("ツール管理", open=False):
        tool_df = gr.DataFrame(value=None, interactive=False, label="Tools")
        with gr.Row():
            tool_id_text = gr.Textbox(label="Tool ID", interactive=False)
            tool_name_text = gr.Textbox(label="Tool Name")
            tool_module_path_text = gr.Textbox(label="Module Path")
            tool_function_name_text = gr.Textbox(label="Function Name")
        tool_desc_text = gr.Textbox(label="Description", lines=2)
        with gr.Row():
            save_tool_btn = gr.Button("Tool設定を保存")
            delete_tool_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
            delete_tool_btn = gr.Button("Toolを削除", variant="stop", interactive=False, scale=1)
        tool_status_display = gr.Textbox(label="Status", interactive=False)
        new_tool_name_text = gr.Textbox(label="新しいTool Name")
        new_tool_desc_text = gr.Textbox(label="Description", lines=2)
        new_tool_module_path_text = gr.Textbox(label="Module Path")
        new_tool_function_name_text = gr.Textbox(label="Function Name")
        create_tool_btn = gr.Button("新規Toolを作成", variant="primary")
        create_tool_status = gr.Textbox(label="Status", interactive=False)

        tool_df.select(fn=on_select_tool, inputs=None, outputs=[tool_id_text, tool_name_text, tool_desc_text, tool_module_path_text, tool_function_name_text])
        save_tool_btn.click(fn=update_tool_ui, inputs=[tool_id_text, tool_name_text, tool_desc_text, tool_module_path_text, tool_function_name_text], outputs=[tool_status_display, tool_df])
        delete_tool_confirm_check.change(fn=toggle_delete_button, inputs=delete_tool_confirm_check, outputs=delete_tool_btn)
        delete_tool_btn.click(fn=delete_tool_ui, inputs=[tool_id_text, delete_tool_confirm_check], outputs=[tool_status_display, tool_df])
        create_tool_btn.click(fn=create_tool_ui, inputs=[new_tool_name_text, new_tool_desc_text, new_tool_module_path_text, new_tool_function_name_text], outputs=[create_tool_status, tool_df])

    with gr.Accordion("アイテム管理", open=False):
        building_examples = ", ".join(
            f"{b.building_id}" for b in manager.buildings[:6]
        )
        persona_examples = ", ".join(
            f"{pid}" for pid in list(manager.personas.keys())[:6]
        )
        gr.Markdown(
            "Owner Kind に応じて Owner ID を設定してください。<br>"
            f"- `building`: Building ID を入力（例: {building_examples or '該当なし'}）<br>"
            f"- `persona`: Persona ID を入力（例: {persona_examples or '該当なし'}）<br>"
            "- `world`: Owner ID は空欄のままで構いません。"
        )
        with gr.Tabs():
            with gr.TabItem("編集/削除"):
                item_df = gr.DataFrame(value=None, interactive=False, label="Items")
                item_id_text = gr.Textbox(label="Item ID", interactive=False)
                item_name_text = gr.Textbox(label="Name")
                item_type_text = gr.Textbox(label="Type", value="object")
                item_desc_text = gr.Textbox(label="Description", lines=3)
                item_state_text = gr.Textbox(label="State JSON", lines=3, placeholder="任意。追加情報をJSONで記述。")
                with gr.Row():
                    owner_kind_dropdown = gr.Dropdown(
                        label="Owner Kind",
                        choices=["world", "building", "persona"],
                        value="world",
                    )
                    owner_id_text = gr.Textbox(
                        label="Owner ID",
                        placeholder="BuildingID または PersonaID（worldの場合は空欄）",
                    )
                with gr.Row():
                    save_item_btn = gr.Button("Item設定を保存")
                    delete_item_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
                    delete_item_btn = gr.Button("Itemを削除", variant="stop", interactive=False, scale=1)
                item_status_display = gr.Textbox(label="Status", interactive=False)

                item_df.select(
                    fn=on_select_item,
                    inputs=None,
                    outputs=[
                        item_id_text,
                        item_name_text,
                        item_type_text,
                        item_desc_text,
                        owner_kind_dropdown,
                        owner_id_text,
                        item_state_text,
                    ],
                )
                save_item_btn.click(
                    fn=update_item_ui,
                    inputs=[
                        item_id_text,
                        item_name_text,
                        item_type_text,
                        item_desc_text,
                        owner_kind_dropdown,
                        owner_id_text,
                        item_state_text,
                    ],
                    outputs=[item_status_display, item_df],
                )
                delete_item_confirm_check.change(
                    fn=toggle_delete_button,
                    inputs=delete_item_confirm_check,
                    outputs=delete_item_btn,
                )
                delete_item_btn.click(
                    fn=delete_item_ui,
                    inputs=[item_id_text, delete_item_confirm_check],
                    outputs=[item_status_display, item_df],
                )

            with gr.TabItem("新規作成"):
                new_item_name_text = gr.Textbox(label="Name")
                new_item_type_text = gr.Textbox(label="Type", value="object")
                new_item_desc_text = gr.Textbox(label="Description", lines=3)
                new_item_state_text = gr.Textbox(label="State JSON", lines=3, placeholder="任意。追加情報をJSONで記述。")
                with gr.Row():
                    new_owner_kind_dropdown = gr.Dropdown(
                        label="Owner Kind",
                        choices=["world", "building", "persona"],
                        value="world",
                    )
                    new_owner_id_text = gr.Textbox(
                        label="Owner ID",
                        placeholder="BuildingID または PersonaID（worldの場合は空欄）",
                    )
                create_item_btn = gr.Button("新規Itemを作成", variant="primary")
                create_item_status_display = gr.Textbox(label="Status", interactive=False)

                create_item_btn.click(
                    fn=create_item_ui,
                    inputs=[
                        new_item_name_text,
                        new_item_type_text,
                        new_item_desc_text,
                        new_owner_kind_dropdown,
                        new_owner_id_text,
                        new_item_state_text,
                    ],
                    outputs=[create_item_status_display, item_df],
                )

    with gr.Accordion("バックアップ/リストア管理", open=False):
        gr.Markdown("現在のワールドの状態をバックアップしたり、過去のバックアップから復元します。**リストア後はアプリケーションの再起動が必須です。**")

        backup_df = gr.DataFrame(value=None, interactive=False, label="利用可能なバックアップ")

        with gr.Row():
            selected_backup_dropdown = gr.Dropdown(label="操作対象のバックアップ", choices=[], scale=2)
            restore_confirm_check = gr.Checkbox(label="リストアを確認", value=False, scale=1)
            restore_btn = gr.Button("リストア実行", variant="primary", interactive=False, scale=1)
            delete_backup_confirm_check = gr.Checkbox(label="削除を確認", value=False, scale=1)
            delete_backup_btn = gr.Button("削除", variant="stop", interactive=False, scale=1)

        with gr.Row():
            new_backup_name_text = gr.Textbox(label="新しいバックアップ名 (英数字のみ)", scale=3)
            create_backup_btn = gr.Button("現在のワールドをバックアップ", scale=1)

        backup_status_display = gr.Textbox(label="Status", interactive=False)

        def update_backup_components():
            df = manager.get_backups()
            choices = df["Backup Name"].tolist() if not df.empty else []
            return gr.update(value=df), gr.update(choices=choices, value=None)

        def create_backup_ui(name):
            if not name:
                return "Error: Backup name is required.", gr.update(), gr.update()
            result = manager.backup_world(name)
            return result, *update_backup_components()

        def restore_backup_ui(name, confirmed):
            if not confirmed:
                return "Error: Please check the confirmation box to restore."
            if not name:
                return "Error: Select a backup to restore."
            return manager.restore_world(name)

        def delete_backup_ui(name, confirmed):
            if not confirmed:
                return "Error: Please check the confirmation box to delete.", gr.update(), gr.update()
            if not name:
                return "Error: Select a backup to delete.", gr.update(), gr.update()
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

    def refresh_world_editor_data():
        logging.info("Refreshing all world editor DataFrames.")

        cities = manager.get_cities_df()
        buildings = manager.get_buildings_df()
        ais = manager.get_ais_df()
        blueprints = manager.get_blueprints_df()
        backups = manager.get_backups()
        tools = manager.get_tools_df()
        items = manager.get_items_df()

        backup_choices = backups["Backup Name"].tolist() if not backups.empty else []

        return (
            cities,
            buildings,
            ais,
            blueprints,
            backups,
            tools,
            items,
            gr.update(choices=backup_choices, value=None),
        )

    refresh_editor_btn.click(
        fn=refresh_world_editor_data,
        inputs=None,
        outputs=[
            city_df,
            building_df,
            ai_df,
            blueprint_df,
            backup_df,
            tool_df,
            item_df,
            selected_backup_dropdown,
        ],
    )
