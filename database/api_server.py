import logging
import os
import argparse
import uvicorn
from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from .models import Base, VisitingAI

# グローバル変数をプレースホルダーとして定義
engine = None
SessionLocal = None

class VisitingPersonaProfile(BaseModel):
    """Pydantic model for a visiting persona's profile."""
    persona_id: str = Field(..., description="The unique ID of the persona.")
    persona_name: str = Field(..., description="The name of the persona.")
    target_building_id: str = Field(..., description="The ID of the building the persona wants to visit in the destination city.")
    system_prompt: Optional[str] = Field("", description="The persona's system prompt.")
    avatar_image: Optional[str] = Field(None, description="A base64 encoded avatar image or a URL.")
    emotion: Optional[Dict[str, Any]] = Field({}, description="The current emotional state of the persona.")

def create_inter_city_router() -> APIRouter:
    """
    Creates a FastAPI router for inter-city communication.
    This router's endpoint will be used by other cities to send visiting personas.
    """
    router = APIRouter(prefix="/inter-city")

    @router.post("/move-in")
    def move_in(profile: VisitingPersonaProfile):
        """Endpoint to receive a visiting persona and queue them in the DB."""
        if not SessionLocal:
            raise HTTPException(status_code=503, detail="Database not initialized.")

        db = SessionLocal()
        try:
            existing_visitor = db.query(VisitingAI).filter(VisitingAI.persona_id == profile.persona_id).first()
            if existing_visitor:
                raise HTTPException(status_code=409, detail=f"Persona {profile.persona_name} is already pending arrival.")

            new_visitor = VisitingAI(
                persona_id=profile.persona_id,
                profile_json=profile.model_dump_json()
            )
            db.add(new_visitor)
            db.commit()
            return JSONResponse(content={"status": "success", "message": f"Welcome, {profile.persona_name}! Your arrival is being processed."})
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to process move-in for {profile.persona_name}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error while processing arrival.")
        finally:
            db.close()
    
    return router

app = FastAPI(title="SAIVerse Inter-City API")
api_router = create_inter_city_router()
app.include_router(api_router)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAIVerse DB API Server")
    parser.add_argument("--port", type=int, default=8001, help="Port to run the API server on")
    parser.add_argument("--db", type=str, default="city_A.db", help="Database file name")
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

    uvicorn.run(app, host="0.0.0.0", port=args.port)