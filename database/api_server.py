import logging
import os
import argparse
import uvicorn
import uuid
import time
from fastapi import FastAPI, APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from .models import Base, VisitingAI, ThinkingRequest, City as CityModel, Building as BuildingModel

# グローバル変数をプレースホルダーとして定義
engine = None
SessionLocal = None
MY_CITY_ID = None

class VisitingPersonaProfile(BaseModel):
    """Pydantic model for a visiting persona's profile."""
    persona_id: str = Field(..., description="The unique ID of the persona.")
    persona_name: str = Field(..., description="The name of the persona.")
    target_building_id: str = Field(..., description="The ID of the building the persona wants to visit in the destination city.")
    avatar_image: Optional[str] = Field(None, description="A base64 encoded avatar image or a URL.")
    emotion: Optional[Dict[str, Any]] = Field({}, description="The current emotional state of the persona.")
    source_city_id: Optional[str] = Field(None, description="The name of the city the persona is coming from.")

class ThinkingRequestContext(BaseModel):
    """Context required for a remote persona to think."""
    building_id: str
    occupants: List[str]
    recent_history: List[Dict[str, str]]
    user_online: bool

class BuildingInfo(BaseModel):
    """Pydantic model for public building information."""
    building_id: str
    building_name: str
    description: str
    capacity: int

def _queue_visitor_in_db(profile: VisitingPersonaProfile, db_session: sessionmaker):
    """Helper function to be run in the background to queue a new visitor."""
    db = db_session()
    try:
        # Check if this visitor is already queued to avoid duplicates from retries
        existing_visitor = db.query(VisitingAI).filter(
            VisitingAI.city_id == MY_CITY_ID,
            VisitingAI.persona_id == profile.persona_id
        ).first()
        if existing_visitor:
            logging.warning(f"Visitor {profile.persona_name} is already in the arrival queue. Ignoring duplicate request.")
            return

        new_visitor = VisitingAI(
            city_id=MY_CITY_ID,
            persona_id=profile.persona_id,
            profile_json=profile.model_dump_json()
        )
        db.add(new_visitor)
        db.commit()
        logging.info(f"Queued visitor {profile.persona_name} for arrival in city {MY_CITY_ID}.")
    except Exception as e:
        db.rollback()
        logging.error(f"Failed to queue visitor {profile.persona_name} in background: {e}", exc_info=True)
    finally:
        db.close()

def create_inter_city_router() -> APIRouter:
    router = APIRouter(prefix="/inter-city")

    @router.post("/request-move-in", status_code=202)
    def request_move_in(profile: VisitingPersonaProfile, background_tasks: BackgroundTasks):
        if not SessionLocal or not MY_CITY_ID:
            raise HTTPException(status_code=503, detail="Database or City ID not initialized.")
        background_tasks.add_task(_queue_visitor_in_db, profile, SessionLocal)
        return {"message": "Accepted. Visitor arrival is being processed."}
    
    @router.get("/buildings", response_model=List[BuildingInfo])
    def get_buildings_list():
        """Returns a list of all public buildings in this city."""
        if not SessionLocal or not MY_CITY_ID:
            raise HTTPException(status_code=503, detail="Database or City ID not initialized.")
        db = SessionLocal()
        try:
            buildings = db.query(BuildingModel).filter(BuildingModel.CITYID == MY_CITY_ID).all()
            response_data = [
                BuildingInfo(
                    building_id=b.BUILDINGID,
                    building_name=b.BUILDINGNAME,
                    description=b.DESCRIPTION,
                    capacity=b.CAPACITY
                ) for b in buildings
            ]
            return response_data
        finally:
            db.close()
    
    return router

def create_proxy_router() -> APIRouter:
    router = APIRouter(prefix="/persona-proxy")

    @router.post("/{persona_id}/think")
    def think_proxy(persona_id: str, context: ThinkingRequestContext):
        if not SessionLocal:
            raise HTTPException(status_code=503, detail="Database not initialized.")

        request_id = str(uuid.uuid4())
        db = SessionLocal()
        try:
            new_request = ThinkingRequest(
                request_id=request_id,
                persona_id=persona_id,
                city_id=MY_CITY_ID,
                request_context_json=context.model_dump_json(),
                status='pending'
            )
            db.add(new_request)
            db.commit()
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to create thinking request for {persona_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to queue thinking request.")
        finally:
            db.close()

        # ロングポーリングで結果を待つ
        timeout = 30  # 30秒でタイムアウト
        start_time = time.time()
        while time.time() - start_time < timeout:
            db = SessionLocal()
            try:
                req = db.query(ThinkingRequest).filter(ThinkingRequest.request_id == request_id).first()
                if req and req.status == 'processed':
                    return JSONResponse(content={"response_text": req.response_text})
                elif req and req.status == 'error':
                    logging.warning(f"Remote thinking for request {request_id} resulted in an error. Sending error details to proxy.")
                    return JSONResponse(status_code=200, content={"response_text": req.response_text})
            finally:
                db.close()
            time.sleep(0.5) # 0.5秒ごとにポーリング

        raise HTTPException(status_code=408, detail="Request timed out.")

    return router

app = FastAPI(title="SAIVerse Inter-City API")
inter_city_router = create_inter_city_router()
proxy_router = create_proxy_router()
app.include_router(inter_city_router)
app.include_router(proxy_router)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAIVerse DB API Server")
    parser.add_argument("--port", type=int, default=8001, help="Port to run the API server on")
    parser.add_argument("--db", type=str, default="saiverse.db", help="Path to the unified database file.")
    args = parser.parse_args()

    # グローバル変数をコマンドライン引数に基づいて設定
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_FILE_PATH = os.path.join(SCRIPT_DIR, args.db)
    DATABASE_URL = f"sqlite:///{DB_FILE_PATH}"

    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # DBファイルが存在しない場合、またはテーブルが存在しない場合にテーブルを作成する
    if not os.path.exists(DB_FILE_PATH) or not inspect(engine).get_table_names():
        logging.info(f"API Server: Database '{args.db}' not found or empty. Creating tables...")
        Base.metadata.create_all(bind=engine)
        logging.info("API Server: Tables created successfully.")

    # 自身のCity IDをDBから取得してグローバル変数に設定
    db = SessionLocal()
    try:
        city_config = db.query(CityModel).filter(CityModel.API_PORT == args.port).first()
        if not city_config:
            raise RuntimeError(f"Could not find a city configured for API port {args.port} in the database.")
        
        MY_CITY_ID = city_config.CITYID
        logging.info(f"API Server for City ID {MY_CITY_ID} ({city_config.CITYNAME}) is starting up.")
    finally:
        db.close()

    uvicorn.run(app, host="0.0.0.0", port=args.port)