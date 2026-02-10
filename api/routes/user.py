import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from api.deps import get_manager

_log = logging.getLogger(__name__)

router = APIRouter()

class UserStatusResponse(BaseModel):
    is_online: bool  # Backward compatibility
    presence_status: str  # "online", "away", "offline"
    current_building_id: Optional[str]
    avatar: Optional[str]
    display_name: str
    email: Optional[str] = None

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
    city_id: Optional[int] = None

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
    except Exception:
        _log.warning("Failed to get user email from database", exc_info=True)

    presence_status = manager.state.user_presence_status
    is_online = presence_status != "offline"

    return {
        "is_online": is_online,
        "presence_status": presence_status,
        "current_building_id": manager.state.user_current_building_id,
        "avatar": manager.state.user_avatar_data,
        "display_name": manager.state.user_display_name,
        "email": email
    }

@router.post("/move", response_model=MoveResponse)
def move_user(req: MoveRequest, manager = Depends(get_manager)):
    import logging
    logging.debug("[USER_MOVE] Request to move to %s", req.target_building_id)

    success, message = manager.move_user(req.target_building_id)
    
    logging.debug("[USER_MOVE] Result success=%s, msg=%s, current_bid=%s", 
                 success, message, manager.user_current_building_id)
        
    return {"success": success, "message": message}

@router.get("/buildings", response_model=BuildingsResponse)
def get_buildings(manager = Depends(get_manager)):
    # Sort buildings by name for better UI experience
    sorted_buildings = sorted(manager.buildings, key=lambda b: b.name)
    return {
        "buildings": [
            {"id": b.building_id, "name": b.name}
            for b in sorted_buildings
        ],
        "city_id": getattr(manager, 'city_id', None)
    }

class _Unset:
    """Sentinel to distinguish 'not provided' from None."""
    pass

_UNSET = _Unset()

class UpdateProfileRequest(BaseModel):
    display_name: str
    avatar: Optional[str] = _UNSET
    email: Optional[str] = _UNSET

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
        if not isinstance(req.avatar, _Unset):
            user.AVATAR_IMAGE = req.avatar
        if not isinstance(req.email, _Unset):
            user.MAILADDRESS = req.email

        # Update user_room building names to match the new username
        from database.models import Building as BuildingModel, City as CityModel
        new_room_name = f"{req.display_name}の部屋"
        all_cities = session.query(CityModel).all()
        for city in all_cities:
            user_room_id = f"user_room_{city.CITYNAME}"
            user_room = session.query(BuildingModel).filter_by(
                BUILDINGID=user_room_id
            ).first()
            if user_room:
                _log.info(
                    "Updating building name: %s -> '%s'",
                    user_room_id, new_room_name,
                )
                user_room.BUILDINGNAME = new_room_name

        session.commit()

        # Update Runtime Manager State so UI reflects it immediately via status polling
        manager.state.user_display_name = req.display_name
        if not isinstance(req.avatar, _Unset):
            manager.state.user_avatar_data = req.avatar

        # Update in-memory Building objects for user_rooms
        for building in manager.buildings:
            if building.building_id.startswith("user_room_"):
                building.name = new_room_name

        return {"success": True}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# --- User Presence Endpoints ---

class HeartbeatRequest(BaseModel):
    last_interaction: Optional[datetime] = None

@router.post("/heartbeat")
def heartbeat(req: HeartbeatRequest, manager = Depends(get_manager)):
    """Update user presence based on frontend activity heartbeat."""
    manager.state.user_presence_status = "online"
    manager.state.user_last_activity_time = req.last_interaction or datetime.now()
    # Sync manager-level cache so SEA runtime sees the updated status
    manager._refresh_user_state_cache()
    return {"status": "ok", "presence_status": "online"}


class VisibilityRequest(BaseModel):
    visible: bool

@router.post("/visibility")
def visibility(req: VisibilityRequest, manager = Depends(get_manager)):
    """Update presence based on browser visibility (tab focus/blur)."""
    if not req.visible:
        manager.state.user_presence_status = "offline"
    else:
        manager.state.user_presence_status = "online"
        manager.state.user_last_activity_time = datetime.now()
    # Sync manager-level cache so SEA runtime sees the updated status
    manager._refresh_user_state_cache()
    return {"status": "ok", "presence_status": manager.state.user_presence_status}


# --- User List Endpoint (for linked user selection) ---

class UserListItem(BaseModel):
    id: int
    name: str

@router.get("/list", response_model=List[UserListItem])
def list_users(manager = Depends(get_manager)):
    """Get list of all users for linked user selection."""
    from database.models import User

    session = manager.SessionLocal()
    try:
        users = session.query(User).all()
        return [
            UserListItem(id=u.USERID, name=u.USERNAME)
            for u in users
        ]
    finally:
        session.close()
