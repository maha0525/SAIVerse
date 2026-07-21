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
    # Region RPG: ユーザーが参加中 (playing/paused) のゲームがあれば現在地に
    # 関わらず返るゲームモード情報 {region_id, region_name, phase, scene,
    # party_location, inside, at_entrance}。それ以外は None。
    # inside=False のとき UI はセッションログ閲覧トグル + 復帰ボタンを出す。
    active_game: Optional[dict] = None

class MoveRequest(BaseModel):
    target_building_id: str
    # B-1 CAS: クライアントが知っている現在地。 サーバ側の current_building_id
    # と一致しない場合は他クライアントが先に move 済みなので 409 を返す。
    # 後方互換のため Optional (= 未指定なら CAS チェックなし、 旧挙動)。
    # See: docs/intent/building_memory_unified.md §B-1
    expected_from_building_id: Optional[str] = None

class MoveResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    current_building_id: Optional[str] = None  # CAS 失敗時にサーバの真の現在地を返す
    
class BuildingInfo(BaseModel):
    id: str
    name: str
    # 所属スコープ (Region / SubRegion の ID)。None なら City 直属
    region_id: Optional[str] = None
    # この Building がいずれかの Region / SubRegion の入口なら、その region_id
    entrance_of: Optional[str] = None

class RegionInfo(BaseModel):
    region_id: str
    name: str
    parent_region_id: Optional[str] = None
    region_type: str = "generic"
    entrance_building_id: Optional[str] = None

class BuildingsResponse(BaseModel):
    buildings: List[BuildingInfo]
    city_id: Optional[int] = None
    # サイドバーの Region 折り畳み用 (docs/intent/region.md §2.2)
    regions: List[RegionInfo] = []

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

    active_game = None
    lifecycle = getattr(manager, "game_lifecycle", None)
    if lifecycle is not None:
        try:
            active_game = lifecycle.active_game_for_user()
        except Exception:
            _log.warning("Failed to resolve active game for user", exc_info=True)

    return {
        "is_online": is_online,
        "presence_status": presence_status,
        "current_building_id": manager.state.user_current_building_id,
        "avatar": manager.state.user_avatar_data,
        "display_name": manager.state.user_display_name,
        "email": email,
        "active_game": active_game,
    }

@router.post("/move", response_model=MoveResponse)
def move_user(req: MoveRequest, manager = Depends(get_manager)):
    import logging
    logging.debug(
        "[USER_MOVE] Request to move to %s (expected_from=%s)",
        req.target_building_id, req.expected_from_building_id,
    )

    # B-1 CAS: クライアントが思っている現在地とサーバの現在地が異なれば
    # 他クライアントが先に move 済み。 409 で現在地を返して再同期させる。
    current_bid = manager.state.user_current_building_id
    if (
        req.expected_from_building_id is not None
        and req.expected_from_building_id != current_bid
    ):
        logging.info(
            "[USER_MOVE] CAS conflict: expected_from=%s server_current=%s — refusing move",
            req.expected_from_building_id, current_bid,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "cas_conflict",
                "message": "他のクライアントが先に移動したため、 この移動は受け付けませんでした。 最新状態に同期します。",
                "current_building_id": current_bid,
            },
        )

    success, message = manager.move_user(req.target_building_id)

    logging.debug("[USER_MOVE] Result success=%s, msg=%s, current_bid=%s",
                 success, message, manager.user_current_building_id)

    # サーバ側 CAS (move_entity の条件付き UPDATE) の競合も、クライアント CAS と
    # 同じ 409 形式で返して再同期を起動する (W7 柱5 / 2026-07-21 Codex P2)
    if not success and getattr(message, "code", None) == "cas_conflict":
        # 仲裁負け直後は in-memory が勝者の移動をまだ映していないことがある。
        # 拒否メッセージが運ぶ DB 確定現在地を優先する (Codex 第三巡 P2)
        confirmed_bid = (
            getattr(message, "current_building_id", None)
            or manager.state.user_current_building_id
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "cas_conflict",
                "message": str(message),
                "current_building_id": confirmed_bid,
            },
        )

    return {
        "success": success,
        "message": message,
        "current_building_id": manager.state.user_current_building_id,
    }

@router.get("/buildings", response_model=BuildingsResponse)
def get_buildings(manager = Depends(get_manager)):
    # Sort buildings by name for better UI experience
    sorted_buildings = sorted(manager.buildings, key=lambda b: b.name)
    regions = list(getattr(manager, "regions", {}).values())
    entrance_index = {
        r.entrance_building_id: r.region_id
        for r in regions if r.entrance_building_id
    }
    return {
        "buildings": [
            {
                "id": b.building_id,
                "name": b.name,
                "region_id": getattr(b, "region_id", None),
                "entrance_of": entrance_index.get(b.building_id),
            }
            for b in sorted_buildings
        ],
        "city_id": getattr(manager, 'city_id', None),
        "regions": [
            {
                "region_id": r.region_id,
                "name": r.name,
                "parent_region_id": r.parent_region_id,
                "region_type": r.region_type,
                "entrance_building_id": r.entrance_building_id,
            }
            for r in regions
        ],
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
            avatar_value = req.avatar
            # If avatar is a media URL, resolve to actual file and process as avatar
            if avatar_value and avatar_value.startswith("/api/media/images/"):
                from pathlib import Path
                from saiverse.media_utils import _ensure_image_dir
                filename = avatar_value.rsplit("/", 1)[-1]
                source_path = _ensure_image_dir() / filename
                if source_path.exists():
                    avatar_value = manager._process_avatar_upload(
                        f"user_{user.USERID}", source_path
                    )
                else:
                    _log.warning(
                        "Avatar media file not found: %s", source_path
                    )
            user.AVATAR_IMAGE = avatar_value
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
            if user.AVATAR_IMAGE:
                from pathlib import Path
                from manager.user_state import UserStateMixin
                avatar_path = UserStateMixin._resolve_avatar_to_path(
                    user.AVATAR_IMAGE
                )
                avatar_data = None
                if avatar_path:
                    avatar_data = manager._load_avatar_data(avatar_path)
                manager.state.user_avatar_data = avatar_data or manager.default_avatar
            else:
                manager.state.user_avatar_data = manager.default_avatar

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
