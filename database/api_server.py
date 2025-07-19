import pandas as pd
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    inspect,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError
from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse
import uvicorn
import os

# --- 1. データベース設定 ---

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE_PATH = os.path.join(SCRIPT_DIR, "saiverse_main.db")
DATABASE_URL = f"sqlite:///{DB_FILE_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# --- 2. テーブルモデル定義 (db_manager.pyと共通) ---

class User(Base): __tablename__ = "user"; USERID = Column(Integer, primary_key=True); PASSWORD = Column(String(32)); USERNAME = Column(String(32)); MAILADDRESS = Column(String(64))
class AI(Base): __tablename__ = "ai"; AIID = Column(Integer, primary_key=True); AINAME = Column(String(32)); SYSTEMPROMPT = Column(String(1024)); DESCRIPTION = Column(String(1024))
class Building(Base): __tablename__ = "building"; BUILDINGID = Column(Integer, primary_key=True); BUILDINGNAME = Column(String(32)); ASSISTANTPROMPT = Column(String(1024)); DESCRIPTION = Column(String(1024))
class City(Base): __tablename__ = "city"; CITYID = Column(Integer, primary_key=True); CITYNAME = Column(String(32)); DESCRIPTION = Column(String(1024))
class Tool(Base): __tablename__ = "tool"; TOOLID = Column(Integer, primary_key=True); TOOLNAME = Column(String(32)); DESCRIPTION = Column(String(1024))
class UserAiLink(Base): __tablename__ = "user_ai_link"; USERID = Column(Integer, ForeignKey("user.USERID"), primary_key=True); AIID = Column(Integer, ForeignKey("ai.AIID"), primary_key=True)
class AiToolLink(Base): __tablename__ = "ai_tool_link"; AIID = Column(Integer, ForeignKey("ai.AIID"), primary_key=True); TOOLID = Column(Integer, ForeignKey("tool.TOOLID"), primary_key=True)
class BuildingToolLink(Base): __tablename__ = "building_tool_link"; BUILDINGID = Column(Integer, ForeignKey("building.BUILDINGID"), primary_key=True); TOOLID = Column(Integer, ForeignKey("tool.TOOLID"), primary_key=True)
class CityBuildingLink(Base): __tablename__ = "city_building_link"; CITYID = Column(Integer, ForeignKey("city.CITYID"), primary_key=True); BUILDINGID = Column(Integer, ForeignKey("building.BUILDINGID"), primary_key=True)
class BuildingAiLink(Base): __tablename__ = "building_ai_link"; BUILDINGID = Column(Integer, ForeignKey("building.BUILDINGID"), primary_key=True); AIID = Column(Integer, ForeignKey("ai.AIID"), primary_key=True); ENTERDT = Column(DateTime); EXITDT = Column(DateTime)

def init_db():
    if not os.path.exists(DB_FILE_PATH):
        print(f"API Server: Database file '{DB_FILE_PATH}' not found. Creating tables...")
        Base.metadata.create_all(bind=engine)
        print("API Server: Tables created successfully.")
    else:
        print(f"API Server: Database file '{DB_FILE_PATH}' already exists.")

TABLE_MODEL_MAP = { "user": User, "ai": AI, "building": Building, "city": City, "tool": Tool, "user_ai_link": UserAiLink, "ai_tool_link": AiToolLink, "building_tool_link": BuildingToolLink, "city_building_link": CityBuildingLink, "building_ai_link": BuildingAiLink }

# --- 3. CRUD 操作関数 (db_manager.pyから移動) ---

def get_dataframe(model_class):
    db = SessionLocal(); query = db.query(model_class); return pd.read_sql(query.statement, db.bind)

def add_or_update_record(model_class, data_dict):
    mapper = inspect(model_class); pk_cols = [c.name for c in mapper.primary_key]; is_new = all(data_dict.get(pk) is None for pk in pk_cols)
    if is_new:
        excluded_cols = {"DESCRIPTION", "EXITDT"}; validation_targets = [c.name for c in mapper.columns if not c.primary_key and c.name not in excluded_cols]
        is_all_empty = all(data_dict.get(col_name) is None or data_dict.get(col_name) == "" for col_name in validation_targets)
        if is_all_empty and validation_targets: return "Error: To add a new record, at least one required field (other than DESCRIPTION or EXITDT) must be filled."
    db = SessionLocal()
    try:
        for key, value in data_dict.items():
            if value == "": data_dict[key] = None
        instance = model_class(**data_dict); db.merge(instance); db.commit()
        return f"Success: Record added/updated in {model_class.__tablename__}."
    except IntegrityError as e: db.rollback(); return f"Error: Integrity constraint failed. {e.orig}"
    except Exception as e: db.rollback(); return f"Error: {e}"
    finally: db.close()

def delete_record(model_class, pks_dict):
    db = SessionLocal()
    try:
        instance = db.get(model_class, pks_dict)
        if instance: db.delete(instance); db.commit(); return f"Success: Record deleted from {model_class.__tablename__}."
        return "Error: Record not found."
    except Exception as e: db.rollback(); return f"Error: {e}"
    finally: db.close()

# --- 4. APIルーター (db_manager.pyから移動) ---

def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/db-api")

    @router.get("/{table_name}")
    def api_get_table(table_name: str):
        model_class = TABLE_MODEL_MAP.get(table_name.lower())
        if not model_class: return JSONResponse(status_code=404, content={"error": "Table not found"})
        df = get_dataframe(model_class)
        return JSONResponse(content=df.to_dict(orient="records"))

    @router.post("/{table_name}")
    async def api_add_or_update(table_name: str, request: Request):
        model_class = TABLE_MODEL_MAP.get(table_name.lower())
        if not model_class: return JSONResponse(status_code=404, content={"error": "Table not found"})
        try:
            data_dict = await request.json()
            status = add_or_update_record(model_class, data_dict)
            if "Error" in status: return JSONResponse(status_code=400, content={"error": status})
            return JSONResponse(content={"status": status})
        except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

    @router.delete("/{table_name}")
    async def api_delete(table_name: str, request: Request):
        model_class = TABLE_MODEL_MAP.get(table_name.lower())
        if not model_class: return JSONResponse(status_code=404, content={"error": "Table not found"})
        mapper = inspect(model_class); pk_cols = [c.name for c in mapper.primary_key]
        pks_dict = {pk: request.query_params.get(pk) for pk in pk_cols}
        if any(v is None for v in pks_dict.values()): return JSONResponse(status_code=400, content={"error": f"Primary key(s) required: {', '.join(pk_cols)}"})
        try:
            for pk_col in mapper.primary_key:
                if isinstance(pk_col.type, Integer): pks_dict[pk_col.name] = int(pks_dict[pk_col.name])
        except (ValueError, TypeError): return JSONResponse(status_code=400, content={"error": "Invalid primary key type"})
        pks_to_pass = pks_dict if len(pks_dict) > 1 else list(pks_dict.values())[0]
        status = delete_record(model_class, pks_to_pass)
        if "Error" in status: return JSONResponse(status_code=400, content={"error": status})
        return JSONResponse(content={"status": status})

    return router

# --- 5. サーバー起動 ---

app = FastAPI(title="SAIVerse DB API")

@app.on_event("startup")
def on_startup():
    init_db()

api_router = create_api_router()
app.include_router(api_router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7920)