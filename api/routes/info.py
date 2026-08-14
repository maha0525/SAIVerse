import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path
from pydantic import BaseModel
from typing import List, Optional, Any
from api.deps import get_manager, avatar_path_to_url

LOGGER = logging.getLogger(__name__)

router = APIRouter()

class OccupantInfo(BaseModel):
    id: str
    name: str
    avatar: Optional[str] = None
    # ライフビューの常在インジケータ (persona_activity_view.md §4.2)。
    # AI persona のみ設定される。users 配列では常に None。
    autonomy_enabled: Optional[bool] = None
    activity_label: Optional[str] = None  # running 自律 Track 由来の短い活動ラベル
    # 「話しかけやすさ」表示 (life.md §9.1)。AI persona のみ設定される。
    # None = lives 未宣言 (何も出さない。誤情報を出さないため)。
    life_state: Optional[str] = None      # "in_life" (活動中) / "valley" (休憩中)
    life_until: Optional[str] = None      # in_life のときだけ "HH:MM" (現在ライフの終了時刻)

class ItemInfo(BaseModel):
    id: str
    name: str
    description: str
    type: str # 'object', 'document', 'picture', 'bag'
    file_path: Optional[str] = None
    is_open: bool = False  # Whether item content is included in visual context
    contained_items: Optional[List["ItemInfo"]] = None  # For bag type: items inside this bag
    contained_count: int = 0  # Number of items directly inside this bag

class FixtureInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    type: str = "object"
    state_json: Optional[str] = None
    has_observer: bool = False

class BuildingDetailsResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    image_path: Optional[str] = None  # Building interior image for visual context
    occupants: List[OccupantInfo]  # AI personas only
    users: List[OccupantInfo] = []  # Users present in the building (intent §D-3)
    items: List[ItemInfo]
    fixtures: List[FixtureInfo] = []

def _build_item_info(data: dict, manager: Any = None) -> dict:
    """Build an ItemInfo dict from an item data dict."""
    item_id = data.get("item_id", "")
    raw_name = data.get("name", "") or ""
    display_name = raw_name.strip() if raw_name.strip() else "(名前なし)"
    item_type = data.get("type", "object")
    info: dict = {
        "id": item_id,
        "name": display_name,
        "description": data.get("description", ""),
        "type": item_type,
        "file_path": data.get("file_path"),
        "is_open": data.get("state", {}).get("is_open", False) if isinstance(data.get("state"), dict) else False,
    }
    if item_type == "bag" and manager and hasattr(manager, 'get_items_in_bag'):
        contained = manager.get_items_in_bag(item_id)
        info["contained_count"] = len(contained)
        info["contained_items"] = [_build_item_info(c, manager) for c in contained]
    else:
        info["contained_count"] = 0
    return info


@router.get("/details", response_model=BuildingDetailsResponse)
def get_building_details(building_id: Optional[str] = None, manager = Depends(get_manager)):
    """Get detailed info about a building: occupants, items.

    ``building_id`` を必ず引数で渡すこと。省略した場合は server-global の
    ``manager.user_current_building_id`` にフォールバックするが、これは
    マルチデバイス / マルチタブで他クライアントの操作に汚染される
    (2026-04-30 のエリス上書き事故の遠因)。フォールバック時は WARN ログを残し、
    将来的に省略呼び出しを 400 で拒否できるよう移行を進める。
    """
    if not building_id:
        import logging
        logging.warning(
            "[deprecation] /api/info/details called without building_id; "
            "falling back to server-global user_current_building_id (=%s). "
            "This is unsafe for multi-device usage — pass ?building_id=<id> explicitly.",
            manager.user_current_building_id,
        )
        building_id = manager.user_current_building_id
    if not building_id or building_id not in manager.building_map:
        return {
            "id": "unknown", 
            "name": "Unknown", 
            "occupants": [], 
            "items": []
        }

    building = manager.building_map[building_id]
    
    # 1. Occupants — AI と user を分離して返す (intent §D-3)。
    # 既存の occupants 配列は AI personas のみ (= 後方互換)、 user は別配列
    # users で返す。 これで frontend は両方を区別して描画できる。
    occupants_list = []  # AI personas
    users_list = []      # Users (separate, intent §D-3)
    user_id_str = str(getattr(manager.state, "user_id", "1"))
    if building_id in manager.occupancy_manager.occupants:
        occupant_ids = manager.occupancy_manager.occupants[building_id]
        sorted_ids = sorted(occupant_ids) if occupant_ids else []
        for oid in sorted_ids:
            if oid in manager.personas:
                persona = manager.personas[oid]
                avatar = avatar_path_to_url(persona.avatar_image)

                # ライフビューの常在インジケータ: running な自律 Track があれば
                # その title/intent から短い活動ラベルを作る (LLM 不要のテンプレート整形、
                # persona_activity_view.md §4.2)。
                activity_label = None
                try:
                    from saiverse.activity_view import build_activity_label
                    from saiverse.track_manager import STATUS_RUNNING
                    tm = getattr(manager, "track_manager", None)
                    if tm is not None:
                        for track in tm.list_for_persona(oid, statuses=[STATUS_RUNNING]):
                            if track.track_type == "autonomous":
                                activity_label = build_activity_label(track.title, track.intent)
                                break
                except Exception:
                    LOGGER.warning(
                        "[info] Failed to build activity label for %s", oid, exc_info=True,
                    )

                # 「話しかけやすさ」表示 (life.md §9.1): 既存の occupants ポーリング
                # (10 秒) に相乗りし、新しい高頻度ポーリングは作らない。
                life_state = None
                life_until = None
                try:
                    from saiverse.day_plan import get_life_status_now
                    status = get_life_status_now(manager, oid)
                    if status["lives_declared"]:
                        if status["in_life"]:
                            life_state = "in_life"
                            life = status.get("life") or {}
                            life_until = life.get("end")
                        else:
                            life_state = "valley"
                except Exception:
                    LOGGER.warning(
                        "[info] Failed to compute life state for %s", oid, exc_info=True,
                    )

                occupants_list.append({
                    "id": oid,
                    "name": persona.persona_name,
                    "avatar": avatar,
                    "autonomy_enabled": getattr(persona, "autonomy_enabled", None),
                    "activity_label": activity_label,
                    "life_state": life_state,
                    "life_until": life_until,
                })
            elif oid == user_id_str:
                # SAIVerse は現状単一ユーザー (USERID=1) 想定なので
                # manager.state から name/avatar を直接解決する。 将来
                # multi-user 化時は DB クエリに切り替える。
                users_list.append({
                    "id": oid,
                    "name": manager.state.user_display_name or "User",
                    "avatar": manager.state.user_avatar_data or "/api/static/builtin_icons/user.png",
                })

    # 2. Items
    # Collect item IDs that are inside bags (to exclude from top-level)
    bag_contained_ids: set = set()
    if hasattr(manager, 'item_service'):
        bag_contained_ids = manager.item_service.get_items_inside_bags_in_building(building_id)

    items_list = []
    if building_id in manager.items_by_building:
        item_ids = manager.items_by_building[building_id]
        sorted_item_ids = sorted(item_ids) if item_ids else []
        for item_id in sorted_item_ids:
            # Skip items that are inside a bag (they'll appear as contained_items)
            if item_id in bag_contained_ids:
                continue

            if item_id in manager.item_registry:
                item_data = manager.item_registry[item_id]
                data = item_data
                if hasattr(manager, 'items') and item_id in manager.items:
                    data = manager.items[item_id]

                raw_name = data.get("name", "") or ""
                display_name = raw_name.strip() if raw_name.strip() else "(名前なし)"
                item_type = data.get("type", "object")

                item_info: dict = {
                    "id": item_id,
                    "name": display_name,
                    "description": data.get("description", ""),
                    "type": item_type,
                    "file_path": data.get("file_path"),
                    "is_open": data.get("state", {}).get("is_open", False) if isinstance(data.get("state"), dict) else False,
                }

                # For bag items, include contained items
                if item_type == "bag" and hasattr(manager, 'get_items_in_bag'):
                    contained = manager.get_items_in_bag(item_id)
                    item_info["contained_count"] = len(contained)
                    item_info["contained_items"] = [
                        _build_item_info(c, manager) for c in contained
                    ]

                items_list.append(item_info)

    # Get Building image path from database
    building_image_path = None
    try:
        from database.session import SessionLocal
        from database.models import Building as BuildingModel
        session = SessionLocal()
        try:
            db_building = session.query(BuildingModel).filter(BuildingModel.BUILDINGID == building_id).first()
            if db_building and db_building.IMAGE_PATH:
                building_image_path = db_building.IMAGE_PATH
        finally:
            session.close()
    except Exception as e:
        LOGGER.warning("Failed to get building image for %s: %s", building_id, e, exc_info=True)

    # 3. Fixtures
    fixtures_list = []
    obs_mgr = getattr(manager, "observer_manager", None)
    if obs_mgr:
        try:
            fixtures = obs_mgr.get_building_fixtures(building_id)
            for f in fixtures:
                observers = obs_mgr.get_fixture_observers(f.FIXTURE_ID)
                fixtures_list.append({
                    "id": f.FIXTURE_ID,
                    "name": f.NAME,
                    "description": f.DESCRIPTION or "",
                    "type": f.TYPE or "object",
                    "state_json": f.STATE_JSON,
                    "has_observer": len(observers) > 0,
                })
        except Exception:
            LOGGER.warning("Failed to load fixtures for %s", building_id, exc_info=True)

    return {
        "id": building_id,
        "name": building.name,
        "description": building.description or "",
        "image_path": building_image_path,
        "occupants": occupants_list,
        "users": users_list,
        "items": items_list,
        "fixtures": fixtures_list,
    }

class CityMapBuilding(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    image_path: Optional[str] = None
    map_x: Optional[float] = None
    map_y: Optional[float] = None
    occupants: List[OccupantInfo]
    # この Building がいずれかの Region / SubRegion の入口なら、その region_id。
    # マップ上で「子スコープへのノード」として描画・ナビゲートするための印
    entrance_of: Optional[str] = None
    entrance_of_name: Optional[str] = None


class CityMapResponse(BaseModel):
    city_id: Optional[int] = None
    city_name: Optional[str] = None
    user_current_building_id: Optional[str] = None
    map_background_image: Optional[str] = None
    buildings: List[CityMapBuilding]
    # スコープナビゲーション (docs/intent/region.md §2.2):
    # region_id 未指定 = City スコープ (scope_region_id=None)。
    # parent_scope_region_id は ↑ ボタンの遷移先 (None なら City へ)
    scope_region_id: Optional[str] = None
    scope_region_name: Optional[str] = None
    parent_scope_region_id: Optional[str] = None


@router.get("/city-map", response_model=CityMapResponse)
def get_city_map(region_id: Optional[str] = None, manager = Depends(get_manager)):
    """Return one map scope in one shot: its Buildings plus their current occupants.

    展示用のマップビューが、Building ごとに /details を叩かずに 1 回でスコープ全体を
    取得できるよう用意した軽量エンドポイント。アイテム情報は含めない (描画に不要)。

    スコープ規則 (docs/intent/region.md §2.2): 各スコープのマップに載るのは
    「自スコープ直属の Building + 子スコープの入口」のみ。入口は親スコープに
    属するため、REGION_ID によるフィルタだけでこの規則が成立する。
    - region_id 省略 = City スコープ (REGION_ID なしの Building。トップ Region の
      入口はここに自然に含まれる)
    - region_id 指定 = その Region 直属の Building (SubRegion の入口を自然に含む)
    """
    scope_region = None
    if region_id:
        scope_region = manager.get_region(region_id)
        if scope_region is None:
            raise HTTPException(status_code=404, detail="Region not found")
    # Building の IMAGE_PATH/MAP_X/MAP_Y と City の MAP_BACKGROUND_IMAGE を一括取得 (N+1 回避)
    image_map: dict = {}
    pos_map: dict = {}
    map_background: Optional[str] = None
    city_name: Optional[str] = None
    try:
        from database.models import Building as BuildingModel, City as CityModel
        session = manager.SessionLocal()
        try:
            rows = session.query(
                BuildingModel.BUILDINGID,
                BuildingModel.IMAGE_PATH,
                BuildingModel.MAP_X,
                BuildingModel.MAP_Y,
            ).all()
            for bid, img, mx, my in rows:
                if img:
                    image_map[bid] = img
                if mx is not None and my is not None:
                    pos_map[bid] = (mx, my)
            city_id = getattr(manager, "city_id", None)
            if city_id is not None:
                city_row = session.query(
                    CityModel.MAP_BACKGROUND_IMAGE,
                    CityModel.CITYNAME,
                    CityModel.CITY_SLUG,
                ).filter(CityModel.CITYID == city_id).first()
                if city_row:
                    if city_row[0]:
                        map_background = city_row[0]
                    # 見出しは表示名 (CITYNAME)。未設定なら識別子 (CITY_SLUG) を
                    # 代わりに見せる (docs/intent/city_identity.md §4 不変条件 3)。
                    city_name = (city_row[1] or "").strip() or city_row[2]
        finally:
            session.close()
    except Exception as e:
        LOGGER.warning("Failed to fetch building images for city-map: %s", e, exc_info=True)

    buildings_payload: List[dict] = []

    # 入口 Building → 結び付く Region の逆引き (entrance_of マーキング用)
    entrance_index = {
        r.entrance_building_id: r
        for r in getattr(manager, "regions", {}).values()
        if r.entrance_building_id
    }
    scope_id = scope_region.region_id if scope_region else None

    sorted_buildings = sorted(
        (b for b in manager.buildings if getattr(b, "region_id", None) == scope_id),
        key=lambda b: b.name,
    )
    for building in sorted_buildings:
        bid = building.building_id
        occupants_list: List[dict] = []
        occupant_ids = manager.occupancy_manager.occupants.get(bid, []) or []
        for oid in sorted(occupant_ids):
            persona = manager.personas.get(oid)
            if not persona:
                continue
            avatar = avatar_path_to_url(persona.avatar_image)
            occupants_list.append({
                "id": oid,
                "name": persona.persona_name,
                "avatar": avatar,
            })
        pos = pos_map.get(bid)
        entrance_region = entrance_index.get(bid)
        buildings_payload.append({
            "id": bid,
            "name": building.name,
            "description": getattr(building, "description", None) or None,
            "image_path": image_map.get(bid),
            "map_x": pos[0] if pos else None,
            "map_y": pos[1] if pos else None,
            "occupants": occupants_list,
            "entrance_of": entrance_region.region_id if entrance_region else None,
            "entrance_of_name": entrance_region.name if entrance_region else None,
        })

    if scope_region is not None:
        map_background = scope_region.map_background_image

    return {
        "city_id": getattr(manager, "city_id", None),
        "city_name": city_name,
        "user_current_building_id": manager.state.user_current_building_id,
        "map_background_image": map_background,
        "buildings": buildings_payload,
        "scope_region_id": scope_id,
        "scope_region_name": scope_region.name if scope_region else None,
        "parent_scope_region_id": scope_region.parent_region_id if scope_region else None,
    }


def _resolve_item_uuid(manager, key: str) -> str:
    """AI 可視の short_id (``item:N`` の N) を裏方 UUID に解決する。

    frontend は persona 可視の ``saiverse://item/N`` からキー N を取り出して
    ``/api/info/item/N`` を叩く。API 側は short_id を裏方 UUID に解決してから
    従来の UUID 経路に載せる。UUID や未知キーはそのまま返す (呼び出し側が 404)。
    """
    svc = getattr(manager, "item_service", None)
    if svc is not None and hasattr(svc, "item_dict_by_key"):
        d = svc.item_dict_by_key(key)
        if d:
            return d.get("item_id", key)
    return key


@router.get("/item/{item_id}/bag-contents")
def get_bag_contents(item_id: str, manager = Depends(get_manager)):
    """Get the contents of a bag item."""
    item_id = _resolve_item_uuid(manager, item_id)
    items_dict = manager.items if hasattr(manager, 'items') else {}
    item = items_dict.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    if (item.get("type") or "").lower() != "bag":
        raise HTTPException(status_code=400, detail="Item is not a bag")

    contained = manager.get_items_in_bag(item_id) if hasattr(manager, 'get_items_in_bag') else []
    return {
        "bag_id": item_id,
        "bag_name": item.get("name", ""),
        "items": [_build_item_info(c, manager) for c in contained],
    }


@router.get("/item/{item_id}")
def get_item_content(item_id: str, thumb: int = 0, manager = Depends(get_manager)):
    # Use manager.state.items if available, or manager.items
    items_map = {}
    if hasattr(manager, 'state') and hasattr(manager.state, 'items'):
        items_map = manager.state.items
    elif hasattr(manager, 'items'):
        items_map = manager.items
    
    # AI 可視の short_id を裏方 UUID に解決してから UUID 経路に載せる。
    item_id = _resolve_item_uuid(manager, item_id)
    LOGGER.debug("Requesting item_id: %s (items_map size: %d)", item_id, len(items_map))

    if item_id not in items_map:
        # Fallback to registry if needed (for admin/seed items not yet in memory?)
        if hasattr(manager, 'item_registry') and item_id in manager.item_registry:
            items_map = manager.item_registry
            LOGGER.debug("Item %s found in registry", item_id)
        else:
            LOGGER.debug("Item %s not found in any map", item_id)
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")

    item_data = items_map[item_id]

    item_type = item_data.get("type", "object")
    file_path = item_data.get("file_path")
    
    LOGGER.debug("Item type: %s, raw file_path: %s", item_type, file_path)

    if not file_path:
        raise HTTPException(status_code=400, detail="No file path for this item")
        
    path = Path(file_path)
    LOGGER.debug("Checking path: %s", path)
    
    home = getattr(manager, "saiverse_home", None)
    if not path.exists():
        # Attempt recovery for legacy/WSL paths or relative paths
        # The DB might contain:
        # - New format: relative paths like "image/filename.png" or "documents/filename.txt"
        # - Legacy format: /home/maha/.saiverse/... paths from WSL
        # We try to find the file relative to current manager.saiverse_home
        if hasattr(manager, 'saiverse_home'):
            home = manager.saiverse_home
            parts = path.parts
            
            # Strategy 0: Handle relative paths (new format)
            # If path is relative (e.g., "image/filename.png"), join with saiverse_home
            if not path.is_absolute():
                candidate = home / file_path
                if candidate.exists():
                    LOGGER.debug("Recovered path (strategy 0 - relative path): %s", candidate)
                    path = candidate
            
            # Strategy 1a: strict 'documents' match (legacy WSL paths)
            if not path.exists() and 'documents' in parts:
                idx = parts.index('documents')
                rel = Path(*parts[idx:])
                candidate = home / rel
                if candidate.exists():
                    LOGGER.debug("Recovered path (strategy 1a - documents): %s", candidate)
                    path = candidate
            
            # Strategy 1b: strict 'image' match (legacy WSL paths for picture items)
            if not path.exists() and 'image' in parts:
                idx = parts.index('image')
                rel = Path(*parts[idx:])
                candidate = home / rel
                if candidate.exists():
                    LOGGER.debug("Recovered path (strategy 1b - image): %s", candidate)
                    path = candidate

            # Strategy 1c/1d: audio/video subdirs for media items
            for sub in ('audio', 'video'):
                if not path.exists() and sub in parts:
                    idx = parts.index(sub)
                    rel = Path(*parts[idx:])
                    candidate = home / rel
                    if candidate.exists():
                        LOGGER.debug("Recovered path (strategy 1c - %s): %s", sub, candidate)
                        path = candidate

            # Strategy 2a: just filename in documents (fallback)
            if not path.exists():
                candidate = home / "documents" / path.name
                if candidate.exists():
                    LOGGER.debug("Recovered path (strategy 2a - documents filename): %s", candidate)
                    path = candidate

            # Strategy 2b: just filename in image (fallback for picture items)
            if not path.exists():
                candidate = home / "image" / path.name
                if candidate.exists():
                    LOGGER.debug("Recovered path (strategy 2b - image filename): %s", candidate)
                    path = candidate

            # Strategy 2c/2d: just filename in audio/video
            for sub in ('audio', 'video'):
                if not path.exists():
                    candidate = home / sub / path.name
                    if candidate.exists():
                        LOGGER.debug("Recovered path (strategy 2c - %s filename): %s", sub, candidate)
                        path = candidate
    
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found on server: {path}")

    from api.file_safety import ensure_allowed_path

    path = ensure_allowed_path(path, home=home)

    if item_type == "document":
        try:
            content = path.read_text(encoding="utf-8")
            return {"type": "document", "content": content}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    elif item_type == "picture":
        # For pictures, we can serve the file directly OR return the path for a static mount
        # If we return FileResponse, the frontend can display it.
        # But wait, frontend <img src> needs a URL.
        # If we return the file content here, it might be heavy.
        # Better: return a URL that the frontend can use.
        # But we don't have a dynamic route for arbitrary file paths unless we mount them.
        # Previous app used /gradio_api/file=... which Gradio handled.

        # SOLUTION: We can verify the path is within valid areas (assets?) or serve it via a stream endpoint.
        # For now, let's return the content as FileResponse so the browser displays it if visited?
        # NO, frontend needs to Embed it.
        # API: GET /api/info/item/{id}/image -> returns image bytes
        # thumb=1: 一覧表示用の軽量 webp サムネイルを返す (未生成ならその場で
        # 生成してキャッシュ)。生成に失敗したらオリジナルへフォールバック。
        if thumb:
            from saiverse.image_thumbnail import get_or_create_thumbnail
            try:
                thumb_path = get_or_create_thumbnail(
                    path, home=getattr(manager, "saiverse_home", None)
                )
                return FileResponse(thumb_path, media_type="image/webp")
            except Exception as e:
                LOGGER.warning(
                    "Thumbnail generation failed for %s: %s; serving original",
                    path, e,
                )
        return FileResponse(path)

    elif item_type == "audio":
        # Serve the audio file directly. ItemModal uses this URL as <audio src=...>.
        return FileResponse(path, media_type="audio/ogg")

    elif item_type == "video":
        # Serve the video file directly. ItemModal uses this URL as <video src=...>.
        return FileResponse(path, media_type="video/mp4")

    else:
        return {"type": item_type, "message": "No content to display"}

@router.get("/models")
def list_available_models():
    """Get list of available models for persona configuration."""
    from saiverse.model_configs import get_model_choices_with_display_names
    choices = get_model_choices_with_display_names()
    return [{"id": mid, "name": name} for mid, name in choices]


@router.post("/item/{item_id}/toggle-open")
def toggle_item_open(item_id: str, manager = Depends(get_manager)):
    """Toggle the open/close state of an item.

    When an item is open, its content is included in the AI's visual context.
    - Picture items: Added as images
    - Document items: Added as text in prompt
    """
    try:
        item_id = _resolve_item_uuid(manager, item_id)
        new_state = manager.toggle_item_open_state(item_id)
        return {"success": True, "is_open": new_state, "item_id": item_id}
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DocumentContentUpdate(BaseModel):
    content: str


@router.put("/item/{item_id}/content")
def update_item_content(item_id: str, body: DocumentContentUpdate, manager = Depends(get_manager)):
    """Update the content of a document item."""
    # Get item data
    item_id = _resolve_item_uuid(manager, item_id)
    items_map = {}
    if hasattr(manager, 'state') and hasattr(manager.state, 'items'):
        items_map = manager.state.items
    elif hasattr(manager, 'items'):
        items_map = manager.items

    if item_id not in items_map:
        if hasattr(manager, 'item_registry') and item_id in manager.item_registry:
            items_map = manager.item_registry
        else:
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")

    item_data = items_map[item_id]
    item_type = item_data.get("type", "object")

    if item_type != "document":
        raise HTTPException(status_code=400, detail="Only document items can be edited")

    file_path = item_data.get("file_path")
    if not file_path:
        raise HTTPException(status_code=400, detail="No file path for this item")

    path = Path(file_path)

    # Handle relative paths
    if not path.is_absolute() and hasattr(manager, 'saiverse_home'):
        path = manager.saiverse_home / file_path

    # Legacy path recovery (same logic as get_item_content)
    if not path.exists() and hasattr(manager, 'saiverse_home'):
        home = manager.saiverse_home
        parts = Path(file_path).parts

        if 'documents' in parts:
            idx = parts.index('documents')
            rel = Path(*parts[idx:])
            candidate = home / rel
            if candidate.exists():
                path = candidate

        if not path.exists():
            candidate = home / "documents" / Path(file_path).name
            if candidate.exists():
                path = candidate

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    from api.file_safety import ensure_allowed_path

    path = ensure_allowed_path(path, home=getattr(manager, "saiverse_home", None))
    try:
        path.write_text(body.content, encoding="utf-8")
        return {"success": True, "message": "Content updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
