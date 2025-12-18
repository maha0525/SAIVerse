from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from api.deps import get_manager

router = APIRouter()

class UserStatusResponse(BaseModel):
    is_online: bool
    current_building_id: Optional[str]
    avatar: Optional[str]
    display_name: str

class MoveRequest(BaseModel):
    target_building_id: str

class MoveResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    
class BuildingInfo(BaseModel):
    id: str
    name: str

class BuildingsResponse(BaseModel):
    buildings: List[BuildingInfo]

@router.get("/status", response_model=UserStatusResponse)
def get_user_status(manager = Depends(get_manager)):
    # Fetch email from DB for completeness (User ID 1)
    email = None
    try:
        from database.models import User
        session = manager.SessionLocal()
        user_db = session.query(User).filter(User.USERID == 1).first()
        if user_db:
            email = user_db.MAILADDRESS
        session.close()
    except:
        pass

    return {
        "is_online": manager.state.user_is_online,
        "current_building_id": manager.state.user_current_building_id,
        "avatar": manager.state.user_avatar_data,
        "display_name": manager.state.user_display_name,
        "email": email
    }

@router.post("/move", response_model=MoveResponse)
def move_user(req: MoveRequest, manager = Depends(get_manager)):
    # DEBUG LOGGING
    debug_log_path = r"c:\Users\shuhe\workspace\SAIVerse\debug_chat.log"
    from datetime import datetime
    with open(debug_log_path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()}: [USER_MOVE] Request to move to {req.target_building_id}\n")

    success, message = manager.move_user(req.target_building_id)
    
    with open(debug_log_path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()}: [USER_MOVE] Result success={success}, msg={message}, current_bid={manager.user_current_building_id}\n")
        
    return {"success": success, "message": message}

@router.get("/buildings", response_model=BuildingsResponse)
def get_buildings(manager = Depends(get_manager)):
    # Sort buildings by name for better UI experience
    sorted_buildings = sorted(manager.buildings, key=lambda b: b.name)
    return {
        "buildings": [
            {"id": b.building_id, "name": b.name} 
            for b in sorted_buildings
        ]
    }

class UpdateProfileRequest(BaseModel):
    display_name: str
    avatar: Optional[str] = None
    email: Optional[str] = None

@router.patch("/me")
def update_user_profile(req: UpdateProfileRequest, manager = Depends(get_manager)):
    """Update current user profile (Hardcoded to User ID 1 for now)."""
    from database.models import User
    
    session = manager.SessionLocal()
    try:
        # Assuming User ID 1 as per instruction
        user = session.query(User).filter(User.USERID == 1).first()
        if not user:
            # Create if missing? Or error? Error likely safer but user said "fixed to 1".
            # For robustness, let's just error if not found.
            raise HTTPException(status_code=404, detail="User not found")
        
        user.USERNAME = req.display_name
        user.AVATAR_IMAGE = req.avatar
        user.MAILADDRESS = req.email
        session.commit()
        
        # Update Runtime Manager State so UI reflects it immediately via status polling
        manager.state.user_display_name = req.display_name
        manager.state.user_avatar_data = req.avatar
        
        return {"success": True}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
