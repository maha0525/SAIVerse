import gradio as gr
import pandas as pd
from sqlalchemy import inspect, DateTime, Integer, Boolean
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from datetime import datetime

from .models import (
    User, AI, Building, City, Tool, Blueprint, Playbook,
    UserAiLink, AiToolLink, BuildingToolLink, BuildingOccupancyLog,
    ThinkingRequest, VisitingAI
)
from model_configs import get_model_choices


# テーブル名とモデルクラスのマッピング
TABLE_MODEL_MAP = {
    "user": User,
    "ai": AI,
    "building": Building,
    "city": City,
    "tool": Tool,
    "playbooks": Playbook,
    "user_ai_link": UserAiLink,
    "ai_tool_link": AiToolLink,
    "building_tool_link": BuildingToolLink,
    "building_occupancy_log": BuildingOccupancyLog,
    "thinking_request": ThinkingRequest,
    "visiting_ai": VisitingAI
}

# --- 3. CRUD (Create, Read, Update, Delete) 操作関数 ---

def get_dataframe(model_class, session_factory: sessionmaker):
    """テーブルからデータをPandas DataFrameとして取得"""
    db = session_factory()
    try:
        query = db.query(model_class)
        return pd.read_sql(query.statement, db.bind)
    finally:
        db.close()

def add_or_update_record(model_class, data_dict, session_factory: sessionmaker):
    """レコードを追加または更新"""
    mapper = inspect(model_class)
    pk_cols = [c.name for c in mapper.primary_key]

    # 主キーが未入力（None）の場合、新規レコードと判断
    # GradioのNumberコンポーネントは空の場合Noneを返す
    is_new = all(data_dict.get(pk) is None for pk in pk_cols)

    if is_new:
        excluded_cols = {"DESCRIPTION", "EXITDT"}
        # チェック対象のカラム（主キーと除外対象以外）
        validation_targets = [
            c.name for c in mapper.columns
            if not c.primary_key and c.name not in excluded_cols
        ]

        # チェック対象のカラムがすべて空かチェック
        is_all_empty = all(
            data_dict.get(col_name) is None or data_dict.get(col_name) == ""
            for col_name in validation_targets
        )
        if is_all_empty and validation_targets:
            return "Error: To add a new record, at least one required field (other than DESCRIPTION or EXITDT) must be filled."

    db = session_factory()
    try:
        # 空の文字列をNoneに変換 & 日付文字列をdatetimeオブジェクトに変換
        for key, value in data_dict.items():
            if value == "":
                data_dict[key] = None
                continue

            column = mapper.columns.get(key)
            if column is None:
                continue

            # --- Data Type Conversion ---
            if isinstance(column.type, Boolean) and isinstance(value, str):
                # Convert string 'True', '1', etc. to boolean True
                data_dict[key] = value.lower() in ('true', '1', 't', 'yes')
            elif isinstance(column.type, DateTime) and isinstance(value, str):
                try:
                    if '.' in value:
                        data_dict[key] = datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f')
                    else:
                        data_dict[key] = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError):
                    return f"Error: Invalid datetime format for {key}. Please use YYYY-MM-DD HH:MM:SS."

        instance = model_class(**data_dict)
        db.merge(instance)
        db.commit()
        return f"Success: Record added/updated in {model_class.__tablename__}."
    except IntegrityError as e:
        db.rollback()
        return f"Error: Integrity constraint failed. {e.orig}"
    except Exception as e:
        db.rollback()
        return f"Error: {e}"
    finally:
        db.close()

def delete_record(model_class, pks_dict, session_factory: sessionmaker):
    """主キーに基づいてレコードを削除"""
    db = session_factory()
    try:
        instance = db.get(model_class, pks_dict)
        if instance:
            db.delete(instance)
            db.commit()
            return f"Success: Record deleted from {model_class.__tablename__}."
        return "Error: Record not found."
    except Exception as e:
        db.rollback()
        return f"Error: {e}"
    finally:
        db.close()


# --- 4. Gradio UI ---

def create_management_tab(model_class, session_factory: sessionmaker):
    """指定されたモデルの管理用UIタブを生成する"""
    mapper = inspect(model_class)
    pk_cols = [c.name for c in mapper.primary_key]

    # --- 外部キー用の選択肢を生成するヘルパー ---
    def get_fk_choices(session_factory: sessionmaker):
        fk_choices = {}
        db = session_factory()
        try:
            for c in mapper.columns:
                if c.foreign_keys:
                    fk = next(iter(c.foreign_keys))
                    target_model = TABLE_MODEL_MAP.get(fk.column.table.name)
                    if target_model:
                        # --- Find a user-friendly display column ---
                        display_col = None
                        # List of preferred column names for display
                        preferred_names = ['USERNAME', 'AINAME', 'CITYNAME', 'BUILDINGNAME']
                        for name in preferred_names:
                            if hasattr(target_model, name):
                                display_col = getattr(target_model, name)
                                break
                        
                        # If no preferred name is found, fallback to the primary key of the target table
                        if display_col is None:
                            target_mapper = inspect(target_model)
                            # Assuming single-column primary key for simplicity in display
                            if target_mapper.primary_key:
                                display_col = target_mapper.primary_key[0]

                        # If we still don't have a column, we can't create a dropdown
                        if display_col is None: continue

                        value_col = fk.column
                        choices = [(getattr(row, display_col.name), getattr(row, value_col.name)) for row in db.query(target_model).all()]
                        fk_choices[c.name] = gr.Dropdown(choices=choices, label=c.name)
        finally:
            db.close()
        return fk_choices

    with gr.Blocks() as tab_interface:
        with gr.Row():
            with gr.Column(scale=3):
                dataframe = gr.DataFrame(
                    value=lambda: get_dataframe(model_class, session_factory),
                    label=f"{model_class.__tablename__} Table",
                    interactive=False,
                )
            with gr.Column(scale=2):
                gr.Markdown("### Add / Update / Delete Record")
                inputs = {}
                long_text_fields = {
                    "SYSTEMPROMPT",
                    "DESCRIPTION",
                    "SYSTEM_INSTRUCTION",
                    "ENTRY_PROMPT",
                    "AUTO_PROMPT",
                    "schema_json",
                    "nodes_json",
                }
                
                fk_dropdowns = get_fk_choices(session_factory)
                model_choices = get_model_choices()

                for c in mapper.columns:
                    if c.name in fk_dropdowns:
                        inputs[c.name] = fk_dropdowns[c.name]
                    elif isinstance(c.type, Boolean):
                        inputs[c.name] = gr.Checkbox(label=c.name)
                    elif isinstance(c.type, (Integer,)):
                        inputs[c.name] = gr.Number(label=c.name, precision=0)
                    elif c.name == 'DEFAULT_MODEL' and model_class is AI:
                        inputs[c.name] = gr.Dropdown(choices=model_choices, label=c.name, allow_custom_value=True)
                    elif c.name in long_text_fields:
                        inputs[c.name] = gr.Textbox(
                            label=c.name, lines=5, max_lines=20
                        )
                    else:
                        inputs[c.name] = gr.Textbox(label=c.name)

                with gr.Row():
                    add_update_btn = gr.Button("Add / Update")
                    delete_btn = gr.Button("Delete", variant="stop")
                
                refresh_btn = gr.Button("Refresh Table", variant="primary")
                status_output = gr.Textbox(label="Status", interactive=False)

        # --- イベントハンドラ ---

        def on_select(df_data: pd.DataFrame, evt: gr.SelectData):
            """DataFrameで行が選択されたとき、フォームに値をセットする"""
            # evt.indexは(行, 列)のタプルなので、行インデックスを取得
            row_index = evt.index[0]
            selected_row = df_data.iloc[row_index]

            updates = []
            for c in mapper.columns:
                value = selected_row.get(c.name)
                # pandasの欠損値(nan)をNoneに変換
                if pd.isna(value):
                    value = None
                # --- 型変換の追加 ---
                # DataFrameから取得した値がfloatになることがあるため、Integer型カラムはintに変換
                elif isinstance(c.type, Integer) and value is not None:
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        # 変換できない場合はそのまま（エラーはGradio側で発生するかもしれないが、ここでクラッシュするよりは良い）
                        pass
                updates.append(gr.update(value=value))
            return updates

        def on_add_update_click(*args):
            data_dict = {c.name: val for c, val in zip(mapper.columns, args)}
            status = add_or_update_record(model_class, data_dict, session_factory)
            return status, get_dataframe(model_class, session_factory)

        def on_delete_click(*args):
            data_dict = {c.name: val for c, val in zip(mapper.columns, args)}
            pks_dict = {pk: data_dict[pk] for pk in pk_cols}
            
            # 複合主キーでない場合は辞書ではなく値を渡す
            pks_to_pass = pks_dict if len(pks_dict) > 1 else list(pks_dict.values())[0]
            
            status = delete_record(model_class, pks_to_pass, session_factory)
            return status, get_dataframe(model_class, session_factory)

        dataframe.select(
            fn=on_select,
            inputs=[dataframe],
            outputs=list(inputs.values()),
        )
        add_update_btn.click(
            fn=on_add_update_click,
            inputs=list(inputs.values()),
            outputs=[status_output, dataframe],
        )
        delete_btn.click(
            fn=on_delete_click,
            inputs=list(inputs.values()),
            outputs=[status_output, dataframe],
        )
        refresh_btn.click(
            fn=lambda: get_dataframe(model_class, session_factory),
            inputs=None,
            outputs=dataframe,
        )

    return tab_interface

def create_db_manager_ui(session_factory: sessionmaker):
    """Creates the complete DB Manager UI component."""
    with gr.Blocks(theme=gr.themes.Soft(), analytics_enabled=False) as db_manager_interface:
        gr.Markdown("# SAIVerse Database Manager")
        with gr.Tabs():
            with gr.TabItem("User"):
                create_management_tab(User, session_factory)
            with gr.TabItem("AI"):
                create_management_tab(AI, session_factory)
            with gr.TabItem("Building"):
                create_management_tab(Building, session_factory)
            with gr.TabItem("City"):
                create_management_tab(City, session_factory)
            with gr.TabItem("Blueprint"):
                create_management_tab(Blueprint, session_factory)
            with gr.TabItem("Tool"):
                create_management_tab(Tool, session_factory)
            with gr.TabItem("Playbook"):
                create_management_tab(Playbook, session_factory)
            with gr.TabItem("User-AI Link"):
                create_management_tab(UserAiLink, session_factory)
            with gr.TabItem("AI-Tool Link"):
                create_management_tab(AiToolLink, session_factory)
            with gr.TabItem("Building-Tool Link"):
                create_management_tab(BuildingToolLink, session_factory)
            with gr.TabItem("building_occupancy_log"):
                create_management_tab(BuildingOccupancyLog, session_factory)
            with gr.TabItem("Thinking Request"):
                create_management_tab(ThinkingRequest, session_factory)
            with gr.TabItem("Visiting AI"):
                create_management_tab(VisitingAI, session_factory)
    return db_manager_interface
