import gradio as gr
import pandas as pd
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    inspect,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError
from fastapi import Request
from fastapi.responses import JSONResponse
import os

# --- 1. データベース設定 ---

# スクリプトファイルがあるディレクトリを基準にDBファイルの絶対パスを生成
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE_PATH = os.path.join(SCRIPT_DIR, "saiverse_main.db")
DATABASE_URL = f"sqlite:///{DB_FILE_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# --- 2. テーブルモデル定義 ---

class User(Base):
    __tablename__ = "user"
    USERID = Column(Integer, primary_key=True)
    PASSWORD = Column(String(32))
    USERNAME = Column(String(32))
    MAILADDRESS = Column(String(64))

class AI(Base):
    __tablename__ = "ai"
    AIID = Column(Integer, primary_key=True)
    AINAME = Column(String(32))
    SYSTEMPROMPT = Column(String(1024))
    DESCRIPTION = Column(String(1024))

class Building(Base):
    __tablename__ = "building"
    BUILDINGID = Column(Integer, primary_key=True)
    BUILDINGNAME = Column(String(32))
    ASSISTANTPROMPT = Column(String(1024))
    DESCRIPTION = Column(String(1024))

class City(Base):
    __tablename__ = "city"
    CITYID = Column(Integer, primary_key=True)
    CITYNAME = Column(String(32))
    DESCRIPTION = Column(String(1024))

class Tool(Base):
    __tablename__ = "tool"
    TOOLID = Column(Integer, primary_key=True)
    TOOLNAME = Column(String(32))
    DESCRIPTION = Column(String(1024))

class UserAiLink(Base):
    __tablename__ = "user_ai_link"
    USERID = Column(Integer, ForeignKey("user.USERID"), primary_key=True)
    AIID = Column(Integer, ForeignKey("ai.AIID"), primary_key=True)

class AiToolLink(Base):
    __tablename__ = "ai_tool_link"
    AIID = Column(Integer, ForeignKey("ai.AIID"), primary_key=True)
    TOOLID = Column(Integer, ForeignKey("tool.TOOLID"), primary_key=True)

class BuildingToolLink(Base):
    __tablename__ = "building_tool_link"
    BUILDINGID = Column(Integer, ForeignKey("building.BUILDINGID"), primary_key=True)
    TOOLID = Column(Integer, ForeignKey("tool.TOOLID"), primary_key=True)

class CityBuildingLink(Base):
    __tablename__ = "city_building_link"
    CITYID = Column(Integer, ForeignKey("city.CITYID"), primary_key=True)
    BUILDINGID = Column(Integer, ForeignKey("building.BUILDINGID"), primary_key=True)

class BuildingAiLink(Base):
    __tablename__ = "building_ai_link"
    BUILDINGID = Column(Integer, ForeignKey("building.BUILDINGID"), primary_key=True)
    AIID = Column(Integer, ForeignKey("ai.AIID"), primary_key=True)
    ENTERDT = Column(DateTime)
    EXITDT = Column(DateTime)


def init_db():
    """データベースファイルが存在しない場合にテーブルを作成する"""
    if not os.path.exists(DB_FILE_PATH):
        print(f"Database file '{DB_FILE_PATH}' not found. Creating tables...")
        Base.metadata.create_all(bind=engine)
        print("Tables created successfully.")
    else:
        print(f"Database file '{DB_FILE_PATH}' already exists.")


# テーブル名とモデルクラスのマッピング
TABLE_MODEL_MAP = {
    "user": User,
    "ai": AI,
    "building": Building,
    "city": City,
    "tool": Tool,
    "user_ai_link": UserAiLink,
    "ai_tool_link": AiToolLink,
    "building_tool_link": BuildingToolLink,
    "city_building_link": CityBuildingLink,
    "building_ai_link": BuildingAiLink,
}

# --- 3. CRUD (Create, Read, Update, Delete) 操作関数 ---

def get_dataframe(model_class):
    """テーブルからデータをPandas DataFrameとして取得"""
    db = SessionLocal()
    try:
        query = db.query(model_class)
        return pd.read_sql(query.statement, db.bind)
    finally:
        db.close()

def add_or_update_record(model_class, data_dict):
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

    db = SessionLocal()
    try:
        # 空の文字列をNoneに変換
        for key, value in data_dict.items():
            if value == "":
                data_dict[key] = None

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

def delete_record(model_class, pks_dict):
    """主キーに基づいてレコードを削除"""
    db = SessionLocal()
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

def create_management_tab(model_class):
    """指定されたモデルの管理用UIタブを生成する"""
    mapper = inspect(model_class)
    pk_cols = [c.name for c in mapper.primary_key]

    with gr.Blocks() as tab_interface:
        with gr.Row():
            with gr.Column(scale=3):
                dataframe = gr.DataFrame(
                    value=lambda: get_dataframe(model_class),
                    label=f"{model_class.__tablename__} Table",
                    interactive=False,
                )
            with gr.Column(scale=2):
                gr.Markdown("### Add / Update / Delete Record")
                inputs = {}
                for c in mapper.columns:
                    if isinstance(c.type, (Integer,)):
                        inputs[c.name] = gr.Number(label=c.name)
                    else:
                        inputs[c.name] = gr.Textbox(label=c.name)

                with gr.Row():
                    add_update_btn = gr.Button("Add / Update")
                    delete_btn = gr.Button("Delete", variant="stop")
                
                refresh_btn = gr.Button("Refresh Table", variant="primary")
                status_output = gr.Textbox(label="Status", interactive=False)

        # --- イベントハンドラ ---

        def on_select(evt: gr.SelectData):
            """DataFrameで行が選択されたとき、フォームに値をセットする"""
            row = evt.value
            updates = []
            for c in mapper.columns:
                # GradioのNumberはNoneを扱えないため、0にフォールバック
                value = row.get(c.name)
                if isinstance(c.type, (Integer,)) and value is None:
                    value = 0
                updates.append(inputs[c.name].update(value=value))
            return updates

        def on_add_update_click(*args):
            data_dict = {c.name: val for c, val in zip(mapper.columns, args)}
            status = add_or_update_record(model_class, data_dict)
            return status, get_dataframe(model_class)

        def on_delete_click(*args):
            data_dict = {c.name: val for c, val in zip(mapper.columns, args)}
            pks_dict = {pk: data_dict[pk] for pk in pk_cols}
            
            # 複合主キーでない場合は辞書ではなく値を渡す
            pks_to_pass = pks_dict if len(pks_dict) > 1 else list(pks_dict.values())[0]
            
            status = delete_record(model_class, pks_to_pass)
            return status, get_dataframe(model_class)

        dataframe.select(
            fn=on_select,
            inputs=None,
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
            fn=lambda: get_dataframe(model_class),
            inputs=None,
            outputs=dataframe,
        )

    return tab_interface


def main():
    # アプリケーション起動時にDBを初期化
    init_db()

    with gr.Blocks(title="SAIVerse DB Manager", theme=gr.themes.Soft()) as demo:
        gr.Markdown("# SAIVerse Database Manager")
        with gr.Tabs():
            with gr.TabItem("User"):
                create_management_tab(User)
            with gr.TabItem("AI"):
                create_management_tab(AI)
            with gr.TabItem("Building"):
                create_management_tab(Building)
            with gr.TabItem("City"):
                create_management_tab(City)
            with gr.TabItem("Tool"):
                create_management_tab(Tool)
            with gr.TabItem("User-AI Link"):
                create_management_tab(UserAiLink)
            with gr.TabItem("AI-Tool Link"):
                create_management_tab(AiToolLink)
            with gr.TabItem("Building-Tool Link"):
                create_management_tab(BuildingToolLink)
            with gr.TabItem("City-Building Link"):
                create_management_tab(CityBuildingLink)
            with gr.TabItem("Building-AI Link"):
                create_management_tab(BuildingAiLink)

        # --- API Endpoints ---
        @demo.app.get("/db-api/{table_name}")
        def api_get_table(table_name: str):
            """テーブルの全データを取得するAPI"""
            model_class = TABLE_MODEL_MAP.get(table_name.lower())
            if not model_class:
                return JSONResponse(status_code=404, content={"error": "Table not found"})
            df = get_dataframe(model_class)
            # DataFrameをJSONシリアライズ可能な形式に変換
            result = df.to_dict(orient="records")
            return JSONResponse(content=result)

        @demo.app.post("/db-api/{table_name}")
        async def api_add_or_update(table_name: str, request: Request):
            """レコードを追加または更新するAPI"""
            model_class = TABLE_MODEL_MAP.get(table_name.lower())
            if not model_class:
                return JSONResponse(status_code=404, content={"error": "Table not found"})
            try:
                data_dict = await request.json()
                status = add_or_update_record(model_class, data_dict)
                if "Error" in status:
                    return JSONResponse(status_code=400, content={"error": status})
                return JSONResponse(content={"status": status})
            except Exception as e:
                return JSONResponse(status_code=500, content={"error": str(e)})

        @demo.app.delete("/db-api/{table_name}")
        async def api_delete(table_name: str, request: Request):
            """主キーに基づいてレコードを削除するAPI"""
            model_class = TABLE_MODEL_MAP.get(table_name.lower())
            if not model_class:
                return JSONResponse(status_code=404, content={"error": "Table not found"})

            mapper = inspect(model_class)
            pk_cols = [c.name for c in mapper.primary_key]

            # クエリパラメータから主キーを取得
            pks_dict = {pk: request.query_params.get(pk) for pk in pk_cols}

            if any(v is None for v in pks_dict.values()):
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Primary key(s) required in query params: {', '.join(pk_cols)}"}
                )

            # 主キーの値を適切な型に変換
            try:
                for pk_col in mapper.primary_key:
                    if isinstance(pk_col.type, Integer):
                        pks_dict[pk_col.name] = int(pks_dict[pk_col.name])
            except (ValueError, TypeError):
                 return JSONResponse(status_code=400, content={"error": "Invalid primary key type"})

            # delete_recordに渡す形式を調整
            pks_to_pass = pks_dict if len(pks_dict) > 1 else list(pks_dict.values())[0]
            status = delete_record(model_class, pks_to_pass)

            if "Error" in status:
                return JSONResponse(status_code=400, content={"error": status})
            return JSONResponse(content={"status": status})


    demo.launch(server_port=7960)


if __name__ == "__main__":
    main()
