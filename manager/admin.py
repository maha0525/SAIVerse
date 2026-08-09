import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from zoneinfo import ZoneInfo

from saiverse.buildings import Building
from database.models import (
    AI as AIModel,
    Building as BuildingModel,
    BuildingOccupancyLog,
    BuildingToolLink,
    City as CityModel,
    User as UserModel,
    Item as ItemModel,
    ItemLocation as ItemLocationModel,
    Playbook as PlaybookModel,
    Region as RegionModel,
)
from manager.blueprints import BlueprintMixin
from manager.history import HistoryMixin
from manager.persona import PersonaMixin
from manager.ids import (
    build_identifier,
    charset_error,
    entrance_id_for,
    is_valid_identifier,
)
from manager.state import CoreState
from scripts.import_playbook import infer_scope_from_path
from builtin_data.tools.save_playbook import save_playbook


class AdminService(BlueprintMixin, HistoryMixin, PersonaMixin):
    """Administrative operations for world editing and CRUD."""

    def __init__(self, manager, runtime, state: CoreState):
        self.manager = manager
        self.runtime = runtime
        self.state = state

        self.SessionLocal = manager.SessionLocal
        self.db_path = manager.db_path
        self.saiverse_home = state.saiverse_home
        self.backup_dir = manager.backup_dir
        self.city_id = state.city_id
        self.city_name = state.city_name

        self.buildings = state.buildings
        self.building_map = state.building_map
        self.building_memory_paths = state.building_memory_paths
        self.building_histories = state.building_histories
        self.capacities = state.capacities
        self.items = state.items
        self.item_locations = state.item_locations
        self.items_by_building = state.items_by_building
        self.items_by_persona = state.items_by_persona
        self.world_items = state.world_items
        self.persona_pending_events = state.persona_pending_events

        self.personas = state.personas
        self.visiting_personas = state.visiting_personas
        self.avatar_map = state.avatar_map
        self.persona_map = state.persona_map
        self.occupants = state.occupants
        self.id_to_name_map = state.id_to_name_map

        self.model = state.model
        self._base_model = getattr(manager, '_base_model', None)
        self.provider = state.provider
        self.context_length = state.context_length
        self.default_avatar = state.default_avatar
        self.host_avatar = state.host_avatar
        self.timezone_info = state.timezone_info
        self.timezone_name = state.timezone_name

        self.user_room_id = state.user_room_id

        # Hooks back to runtime methods
        self._move_persona = runtime._move_persona
        self.dispatch_persona = runtime.dispatch_persona
        self.summon_persona = runtime.summon_persona
        self.end_conversation = runtime.end_conversation
        self.get_summonable_personas = runtime.get_summonable_personas
        self.get_conversing_personas = runtime.get_conversing_personas
        self.get_persona_pending_events = manager.get_persona_pending_events
        self.archive_persona_events = manager.archive_persona_events
        # --- ミックスインが self. で読むが、実体は SAIVerseManager 側にあるもの ---
        # AdminService は PersonaMixin / BlueprintMixin / HistoryMixin を継承する
        # 「もう一つの土台」なので、ミックスインのコードが触る状態はここで揃える。
        # 欠けると commit 後に AttributeError になり、DB には作られたのに失敗を
        # 返す形で壊れる (2026-08-09 に _on_persona_registered で実際に発生)。
        # 欠落は tests/test_mixin_host_contract.py が機械的に検査する。
        self._on_persona_registered = manager._on_persona_registered
        self.quarantined_buildings = manager.quarantined_buildings
        self.startup_warnings = manager.startup_warnings
        self.occupancy_manager = manager.occupancy_manager
        self.conversation_managers = manager.conversation_managers
        self._save_building_histories = manager._save_building_histories
        self._save_modified_buildings = manager._save_modified_buildings
        self._update_timezone_cache = manager._update_timezone_cache
        self._load_cities_from_db = manager._load_cities_from_db

    # --- City management ---

    @staticmethod
    def _validate_city_name(name: str) -> Optional[str]:
        """Validate city name is ASCII alphanumeric + underscore only.
        Returns an error message string if invalid, None if valid."""
        if not name or not name.strip():
            return "Error: City name cannot be empty."
        if not re.match(r'^[a-zA-Z0-9_]+$', name):
            return (
                "Error: City name must contain only alphanumeric characters "
                "and underscores (a-z, A-Z, 0-9, _)."
            )
        return None

    def update_city(
        self,
        city_id: int,
        name: str,
        description: str,
        online_mode: bool,
        ui_port: int,
        api_port: int,
        timezone_name: str,
        host_avatar_path: Optional[str] = None,
        host_avatar_upload: Optional[str] = None,
        map_background_image: Optional[str] = None,
    ) -> str:
        name_error = self._validate_city_name(name)
        if name_error:
            return name_error

        db = self.SessionLocal()
        try:
            city = db.query(CityModel).filter(CityModel.CITYID == city_id).first()
            if not city:
                return "Error: City not found."

            tz_candidate = (timezone_name or "UTC").strip() or "UTC"
            try:
                ZoneInfo(tz_candidate)
            except Exception:
                return (
                    f"Error: Invalid timezone '{tz_candidate}'. Please provide an IANA "
                    "timezone name (e.g., Asia/Tokyo)."
                )

            city.CITYNAME = name
            city.DESCRIPTION = description
            city.START_IN_ONLINE_MODE = online_mode
            city.UI_PORT = ui_port
            city.API_PORT = api_port
            city.TIMEZONE = tz_candidate
            avatar_value: Optional[str] = (host_avatar_path or "").strip() or None
            if host_avatar_upload:
                try:
                    upload_path = Path(host_avatar_upload)
                    avatar_value = self._process_avatar_upload(f"host_{city_id}", upload_path)
                except Exception as exc:
                    db.rollback()
                    logging.error("Failed to process host avatar upload: %s", exc, exc_info=True)
                    return f"Error: Failed to process host avatar upload: {exc}"
            city.HOST_AVATAR_IMAGE = avatar_value
            # 街マップ背景画像 (空文字は NULL として保存)
            city.MAP_BACKGROUND_IMAGE = (map_background_image or "").strip() or None
            db.commit()

            if city.CITYID == self.state.city_id:
                self.state.start_in_online_mode = online_mode
                self.manager.start_in_online_mode = online_mode
                self.state.city_name = name
                self.manager.city_name = name
                self.state.ui_port = ui_port
                self.manager.ui_port = ui_port
                self.state.api_port = api_port
                self.manager.api_port = api_port
                self.state.user_room_id = f"user_room_{self.state.city_name}"
                self.manager.user_room_id = self.state.user_room_id
                self.user_room_id = self.state.user_room_id
                self._update_timezone_cache(tz_candidate)
                # _update_timezone_cache updates manager & state; sync admin's
                # own cached copies so _create_persona / _load_single_persona
                # pick up the new timezone immediately.
                self.timezone_name = self.state.timezone_name
                self.timezone_info = self.state.timezone_info
                # Propagate to existing in-memory personas
                for persona in self.state.personas.values():
                    persona.timezone = self.state.timezone_info
                    persona.timezone_name = self.state.timezone_name
                self.manager.reload_host_avatar(avatar_value)

            self._load_cities_from_db()
            logging.info(
                "Updated city settings for City ID %s. A restart may be required.",
                city_id,
            )
            return (
                "City settings updated successfully. "
                "A restart is required for changes to apply."
            )
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to update city settings for ID %s: %s", city_id, exc, exc_info=True
            )
            return f"Error: {exc}"
        finally:
            db.close()

    def create_city(
        self, name: str, description: str, ui_port: int, api_port: int, timezone_name: str
    ) -> str:
        name_error = self._validate_city_name(name)
        if name_error:
            return name_error

        db = self.SessionLocal()
        try:
            if db.query(CityModel).filter_by(CITYNAME=name).first():
                return f"Error: A city named '{name}' already exists."
            if (
                db.query(CityModel)
                .filter(
                    (CityModel.UI_PORT == ui_port) | (CityModel.API_PORT == api_port)
                )
                .first()
            ):
                return (
                    f"Error: UI Port {ui_port} or API Port {api_port} is already in use."
                )

            tz_candidate = (timezone_name or "UTC").strip() or "UTC"
            try:
                ZoneInfo(tz_candidate)
            except Exception:
                return (
                    f"Error: Invalid timezone '{tz_candidate}'. Please provide an IANA "
                    "timezone name (e.g., Asia/Tokyo)."
                )

            new_city = CityModel(
                USERID=self.state.user_id,
                CITYNAME=name,
                DESCRIPTION=description,
                UI_PORT=ui_port,
                API_PORT=api_port,
                TIMEZONE=tz_candidate,
            )
            db.add(new_city)
            db.commit()
            self._load_cities_from_db()
            logging.info("Created new city '%s'.", name)
            return (
                f"City '{name}' created successfully. "
                "Please restart the application to use it."
            )
        except Exception as exc:
            db.rollback()
            return f"Error: {exc}"
        finally:
            db.close()

    def get_user_profile(self) -> Tuple[str, str]:
        db = self.SessionLocal()
        try:
            user = (
                db.query(UserModel)
                .filter(UserModel.USERID == self.state.user_id)
                .first()
            )
            if not user:
                return "ユーザー", ""
            return user.USERNAME or "ユーザー", user.AVATAR_IMAGE or ""
        finally:
            db.close()

    def update_user_profile(
        self,
        name: str,
        avatar_path: Optional[str],
        avatar_upload: Optional[str],
    ) -> str:
        clean_name = (name or "").strip()
        if not clean_name:
            return "Error: ユーザー名を入力してください。"

        db = self.SessionLocal()
        try:
            user = (
                db.query(UserModel)
                .filter(UserModel.USERID == self.state.user_id)
                .first()
            )
            if not user:
                return "Error: User not found."

            user.USERNAME = clean_name
            avatar_value: Optional[str] = (avatar_path or "").strip() or None
            if avatar_upload:
                upload_path = Path(avatar_upload)
                avatar_value = self._process_avatar_upload(f"user_{user.USERID}", upload_path)
            user.AVATAR_IMAGE = avatar_value
            db.commit()

            self.state.user_display_name = clean_name
            self.manager.reload_user_profile()
            logging.info("Updated user profile for USERID=%s", user.USERID)
            return "ユーザープロファイルを更新しました。"
        except Exception as exc:
            db.rollback()
            logging.error("Failed to update user profile: %s", exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

    def delete_city(self, city_id: int) -> str:
        db = self.SessionLocal()
        try:
            city = db.query(CityModel).filter_by(CITYID=city_id).first()
            if not city:
                return "Error: City not found."
            city_count = db.query(CityModel).count()
            if city_count <= 1:
                return "Error: Cannot delete the last remaining city."
            if city.CITYID == self.state.city_id:
                return "Error: Cannot delete the currently running city."

            if db.query(BuildingModel).filter_by(CITYID=city_id).first():
                return (
                    f"Error: Cannot delete city '{city.CITYNAME}' because it still "
                    "contains buildings."
                )

            if db.query(BuildingOccupancyLog).filter_by(CITYID=city_id).first():
                return (
                    f"Error: Cannot delete city '{city.CITYNAME}' due to remaining "
                    "occupancy logs. Please clean up buildings first."
                )

            db.delete(city)
            db.commit()
            logging.info("Deleted city '%s'.", city.CITYNAME)
            return f"City '{city.CITYNAME}' deleted successfully."
        except Exception as exc:
            db.rollback()
            return f"Error: {exc}"
        finally:
            db.close()

    # --- Building management ---

    def create_building(
        self,
        name: str,
        description: str,
        capacity: int,
        system_instruction: str,
        city_id: int,
        building_id: Optional[str] = None,
    ) -> str:
        db = self.SessionLocal()
        try:
            if not db.query(CityModel).filter_by(CITYID=city_id).first():
                return "Error: Target city not found."
            if db.query(BuildingModel).filter_by(CITYID=city_id, BUILDINGNAME=name).first():
                return f"Error: A building named '{name}' already exists in that city."

            city = db.query(CityModel).filter_by(CITYID=city_id).first()

            # Use custom ID if provided, otherwise generate. Either way the ID
            # must satisfy the charset contract (manager/ids.py) — it goes
            # verbatim into log folder paths, saiverse:// URIs and API paths.
            if building_id and building_id.strip():
                building_id = building_id.strip()
                if not is_valid_identifier(building_id):
                    return charset_error("Building ID", building_id)
            else:
                # 日本語名など slug が空になる名前は building_<連番>_<city> へ
                # フォールバック (issue 論点 1: 読み変換は導入せず、まず口を塞ぐ)
                building_id = build_identifier(
                    name,
                    city.CITYNAME,
                    stem="building",
                    exists=lambda cid: db.query(BuildingModel)
                    .filter_by(BUILDINGID=cid)
                    .first()
                    is not None,
                )

            if db.query(BuildingModel).filter_by(BUILDINGID=building_id).first():
                return (
                    f"Error: A building with the ID '{building_id}' "
                    "already exists."
                )

            new_building = BuildingModel(
                CITYID=city_id,
                BUILDINGID=building_id,
                BUILDINGNAME=name,
                DESCRIPTION=description,
                CAPACITY=capacity,
                SYSTEM_INSTRUCTION=system_instruction,
            )
            db.add(new_building)
            db.commit()
            logging.info("Created new building '%s' (ID: %s) in city %s.", name, building_id, city_id)
            return (
                f"Building '{name}' (ID: {building_id}) created successfully. "
                "A restart is required for it to be usable."
            )
        except Exception as exc:
            db.rollback()
            return f"Error: {exc}"
        finally:
            db.close()

    def delete_building(self, building_id: str) -> str:
        if self._is_seeded_entity(building_id):
            return "Error: Seeded buildings cannot be deleted."
        db = self.SessionLocal()
        try:
            building = db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
            if not building:
                return "Error: Building not found."

            # Check AI occupancy
            occupancy = (
                db.query(BuildingOccupancyLog)
                .filter_by(BUILDINGID=building_id, EXIT_TIMESTAMP=None)
                .first()
            )
            if occupancy:
                return (
                    f"Error: Cannot delete '{building.BUILDINGNAME}' because it is "
                    "occupied."
                )

            # Check user occupancy (users are tracked via User.CURRENT_BUILDINGID,
            # not BuildingOccupancyLog)
            user_in_building = (
                db.query(UserModel)
                .filter_by(CURRENT_BUILDINGID=building_id)
                .first()
            )
            if user_in_building:
                return (
                    f"Error: Cannot delete '{building.BUILDINGNAME}' because a "
                    "user is currently in it."
                )

            db.query(BuildingOccupancyLog).filter_by(BUILDINGID=building_id).delete()
            db.delete(building)
            db.commit()
            logging.info("Deleted building '%s'.", building.BUILDINGNAME)
            return (
                f"Building '{building.BUILDINGNAME}' deleted successfully. "
                "A restart is required for changes to apply."
            )
        except Exception as exc:
            db.rollback()
            return f"Error: {exc}"
        finally:
            db.close()

    def update_building(
        self,
        building_id: str,
        name: str,
        capacity: int,
        description: str,
        system_instruction: str,
        city_id: int,
        tool_ids: List[int],
        interval: int,
        image_path: Optional[str] = None,
        extra_prompt_files: Optional[List[str]] = None,
    ) -> str:
        db = self.SessionLocal()
        try:
            building = db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
            if not building:
                return "Error: Building not found."

            # 分離監査 P1-7 (W7 柱5): City は通常更新では immutable。
            # City 変更は User.CURRENT_BUILDINGID / Region 所属 / private room /
            # tool・item link の全参照を検査・一括移送する専用 migration の領分で、
            # multi-city 凍結中は提供しない (凍結解除時に監査の修正方針を正典と
            # して設計する)。
            if building.CITYID != city_id:
                return (
                    f"Error: The city of '{building.BUILDINGNAME}' cannot be "
                    "changed. (City transfer requires a dedicated migration, "
                    "which is out of scope while multi-city is frozen.)"
                )

            building.BUILDINGNAME = name
            building.CAPACITY = capacity
            building.DESCRIPTION = description
            building.SYSTEM_INSTRUCTION = system_instruction
            building.AUTO_INTERVAL_SEC = interval
            building.CITYID = city_id
            # Update image path if provided (allow clearing by passing empty string)
            if image_path is not None:
                building.IMAGE_PATH = image_path.strip() if image_path.strip() else None
            # Update extra prompt files
            if extra_prompt_files is not None:
                import json
                building.EXTRA_PROMPT_FILES = json.dumps(extra_prompt_files) if extra_prompt_files else None

            db.query(BuildingToolLink).filter_by(BUILDINGID=building_id).delete(
                synchronize_session=False
            )
            for tool_id in tool_ids:
                db.add(BuildingToolLink(BUILDINGID=building_id, TOOLID=int(tool_id)))

            db.commit()
            logging.info(
                "Updated building '%s' (%s) and its tool links.", name, building_id
            )
            return f"Building '{name}' updated successfully."
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to update building '%s': %s", building_id, exc, exc_info=True
            )
            return f"Error: {exc}"
        finally:
            db.close()


    # --- Region management ---
    # Region は Building の上位グルーピング (PARENT_REGION_ID 自己参照で 1 段の
    # SubRegion 入れ子)。設計意図: temp/region_rpg_intent.md §A (リポジトリ外管理)

    VALID_REGION_TYPES = ("generic", "game")

    def create_region(
        self,
        name: str,
        description: str,
        region_type: str,
        city_id: int,
        parent_region_id: Optional[str] = None,
        region_id: Optional[str] = None,
        entrance_building_id: Optional[str] = None,
    ) -> str:
        """Region / SubRegion を作成する。

        入口必須の不変条件 (docs/intent/region.md §3) を作成フローで保証する:
        entrance_building_id 指定時は既存 Building を入口として紐づけ、省略時は
        「(名): 入口」Building を自動作成する。入口は親スコープに属する
        (トップ Region の入口は REGION_ID なし、SubRegion の入口は親 Region 所属)。

        例外: game タイプのトップ Region は create_ruler が控室 (= 入口) を
        作成するため、ここでは自動作成しない (Ruler 不在の game Region は
        どのみちゲームを開始できない setup 途中の状態)。
        """
        if region_type not in self.VALID_REGION_TYPES:
            return f"Error: Invalid region_type '{region_type}'. Must be one of {self.VALID_REGION_TYPES}."
        db = self.SessionLocal()
        try:
            city = db.query(CityModel).filter_by(CITYID=city_id).first()
            if not city:
                return "Error: Target city not found."

            if parent_region_id:
                parent = db.query(RegionModel).filter_by(REGION_ID=parent_region_id).first()
                if not parent:
                    return "Error: Parent region not found."
                if parent.CITYID != city_id:
                    return "Error: Parent region belongs to a different city."
                if parent.PARENT_REGION_ID:
                    # 入れ子は 1 段まで (モデル定義のコメント参照)。アプリ層で強制する
                    return "Error: Nesting is limited to one level; the parent is already a SubRegion."

            # 入口を自動作成する分岐に入るか (下の入口決定と同じ条件)。ID 候補を
            # 選ぶ前に確定させる — 自動作成しないのに入口 ID を予約すると、
            # 使える番号を無意味に飛ばす
            will_auto_create_entrance = (
                not (entrance_building_id and entrance_building_id.strip())
                and not (region_type == "game" and not parent_region_id)
            )

            # Region ID も Building ID と同じ文字種契約に従う (manager/ids.py)。
            # 入口 Building の ID は entrance_<region_id> なので、ここが素通しだと
            # Building 側の契約ごと破れる — game_create_subregion (Ruler ペルソナが
            # 自分で SubRegion を作る口) は日本語名をそのまま渡してくる。
            if region_id and region_id.strip():
                region_id = region_id.strip()
                if not is_valid_identifier(region_id):
                    return charset_error("Region ID", region_id)
            else:
                def _region_id_taken(rid: str) -> bool:
                    if db.query(RegionModel).filter_by(REGION_ID=rid).first():
                        return True
                    if not will_auto_create_entrance:
                        return False
                    # 派生する入口 Building の ID も一緒に予約する。Region 側が
                    # 空いていても entrance_<rid> が埋まっていると、下の入口自動
                    # 作成がエラーで止まる — 連番を一つ進めれば避けられる衝突なので
                    # 候補選びの段階で見る (Region を消しても入口 Building が残る
                    # 経路があり、連番の若い番号ほど当たりやすい)。
                    return db.query(BuildingModel).filter_by(
                        BUILDINGID=entrance_id_for(rid)
                    ).first() is not None

                region_id = build_identifier(
                    name,
                    city.CITYNAME,
                    prefix="region",
                    exists=_region_id_taken,
                )

            if db.query(RegionModel).filter_by(REGION_ID=region_id).first():
                return f"Error: A region with the ID '{region_id}' already exists."

            # --- 入口の決定 (region 行と同一トランザクションで原子的に) ---
            entrance_id: Optional[str] = None
            entrance_note = ""
            if entrance_building_id and entrance_building_id.strip():
                entrance_building_id = entrance_building_id.strip()
                # entrance_<region_id> は自動生成入口の予約名。delete_region は
                # 「入口の ID がこの形か」だけで自動生成物かを判定して削除するので、
                # ユーザー所有の Building にこの名前を持たせると Region 削除で
                # 巻き添えに消える。判定へ provenance を持たせるのが本筋だが列の
                # 追加が要るため、まず曖昧さの供給源 (同名を許すこと) を塞ぐ。
                if entrance_building_id == entrance_id_for(region_id):
                    return (
                        f"Error: '{entrance_building_id}' is reserved for the "
                        f"auto-created entrance of region '{region_id}'. Use a "
                        "building with a different ID, or omit entrance_building_id "
                        "to have the entrance created for you."
                    )
                entrance = db.query(BuildingModel).filter_by(
                    BUILDINGID=entrance_building_id
                ).first()
                if not entrance:
                    return "Error: Entrance building not found."
                if entrance.CITYID != city_id:
                    return "Error: Entrance building belongs to a different city."
                # 入口所有は一意 (W7 柱5 / Codex 第二巡): 共有すると片方の
                # 親変更が他方の「入口は親スコープ」不変条件を壊す
                owner = db.query(RegionModel).filter_by(
                    ENTRANCE_BUILDING_ID=entrance_building_id
                ).first()
                if owner is not None:
                    return (
                        f"Error: '{entrance.BUILDINGNAME}' is already the "
                        f"entrance of region '{owner.NAME}'. An entrance "
                        "building cannot be shared between regions."
                    )
                # 入口は親スコープに属する
                entrance.REGION_ID = parent_region_id or None
                entrance_id = entrance_building_id
                entrance_note = f" Entrance: '{entrance.BUILDINGNAME}' (ID: {entrance_id})."
            elif region_type == "game" and not parent_region_id:
                # game トップ Region の入口は create_ruler の控室。ここでは作らない
                pass
            else:
                entrance_name = f"{name}: 入口"
                if db.query(BuildingModel).filter_by(
                    CITYID=city_id, BUILDINGNAME=entrance_name
                ).first():
                    return (
                        f"Error: A building named '{entrance_name}' already exists; "
                        "cannot auto-create the entrance."
                    )
                entrance_id = entrance_id_for(region_id)
                # ここへ来る衝突は「名前から導いた ID」か「カスタム ID」の場合。
                # どちらもユーザーが選んだものなので、連番で黙って別 ID にせず
                # エラーで返す (上の予約は、機械が選ぶ連番候補にだけ効く)。
                if db.query(BuildingModel).filter_by(BUILDINGID=entrance_id).first():
                    return f"Error: A building with the ID '{entrance_id}' already exists."
                db.add(BuildingModel(
                    CITYID=city_id,
                    BUILDINGID=entrance_id,
                    BUILDINGNAME=entrance_name,
                    DESCRIPTION=f"『{name}』への入口。",
                    CAPACITY=10,
                    SYSTEM_INSTRUCTION=(
                        f"『{name}』の入口。ここから先が『{name}』の内部です。"
                    ),
                    REGION_ID=parent_region_id or None,
                ))
                entrance_note = f" Entrance '{entrance_name}' (ID: {entrance_id}) was auto-created."

            db.add(RegionModel(
                REGION_ID=region_id,
                CITYID=city_id,
                PARENT_REGION_ID=parent_region_id or None,
                NAME=name,
                DESCRIPTION=description,
                REGION_TYPE=region_type,
                ENTRANCE_BUILDING_ID=entrance_id,
            ))
            db.commit()
            logging.info(
                "Created new region '%s' (ID: %s) in city %s (entrance: %s).",
                name, region_id, city_id, entrance_id or "(deferred to create_ruler)",
            )
            return f"Region '{name}' (ID: {region_id}) created successfully.{entrance_note}"
        except Exception as exc:
            db.rollback()
            logging.error("Failed to create region '%s': %s", name, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

    def update_region(
        self,
        region_id: str,
        name: str,
        description: str,
        region_type: str,
        parent_region_id: Optional[str] = None,
    ) -> str:
        if region_type not in self.VALID_REGION_TYPES:
            return f"Error: Invalid region_type '{region_type}'. Must be one of {self.VALID_REGION_TYPES}."
        db = self.SessionLocal()
        try:
            region = db.query(RegionModel).filter_by(REGION_ID=region_id).first()
            if not region:
                return "Error: Region not found."

            if parent_region_id:
                if parent_region_id == region_id:
                    return "Error: A region cannot be its own parent."
                parent = db.query(RegionModel).filter_by(REGION_ID=parent_region_id).first()
                if not parent:
                    return "Error: Parent region not found."
                if parent.CITYID != region.CITYID:
                    return "Error: Parent region belongs to a different city."
                if parent.PARENT_REGION_ID:
                    return "Error: Nesting is limited to one level; the parent is already a SubRegion."
                has_children = db.query(RegionModel).filter_by(PARENT_REGION_ID=region_id).first()
                if has_children:
                    return "Error: Cannot make this region a SubRegion while it has SubRegions of its own."

            # 分離監査 P1-6 (W7 柱5): 「入口は親スコープに属する」不変条件。
            # parent 変更時は入口 Building の REGION_ID を同一 tx で新しい親
            # スコープへ同期する (top 化なら City 直下 = None)。取り残すと
            # 入口が旧スコープに残り、Region が通常移動で到達不能になる。
            old_parent = region.PARENT_REGION_ID or None
            new_parent = parent_region_id or None
            if old_parent != new_parent and region.ENTRANCE_BUILDING_ID:
                entrance = db.query(BuildingModel).filter_by(
                    BUILDINGID=region.ENTRANCE_BUILDING_ID
                ).first()
                if not entrance:
                    return (
                        f"Error: Entrance building '{region.ENTRANCE_BUILDING_ID}' "
                        "not found; cannot change the parent of this region."
                    )
                # レガシーデータで入口が共有されている場合、動かすと他 Region の
                # 「入口は親スコープ」不変条件を壊すため拒否 (新規作成時は
                # create_region が共有自体を拒否する)
                other_owner = db.query(RegionModel).filter(
                    RegionModel.ENTRANCE_BUILDING_ID == region.ENTRANCE_BUILDING_ID,
                    RegionModel.REGION_ID != region_id,
                ).first()
                if other_owner is not None:
                    return (
                        f"Error: Entrance building '{entrance.BUILDINGNAME}' is "
                        f"shared with region '{other_owner.NAME}'. Resolve the "
                        "shared entrance before changing parents."
                    )
                entrance.REGION_ID = new_parent

            region.NAME = name
            region.DESCRIPTION = description
            region.REGION_TYPE = region_type
            region.PARENT_REGION_ID = new_parent
            db.commit()
            logging.info("Updated region '%s' (%s).", name, region_id)
            return f"Region '{name}' updated successfully."
        except Exception as exc:
            db.rollback()
            logging.error("Failed to update region '%s': %s", region_id, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

    def delete_region(self, region_id: str) -> str:
        db = self.SessionLocal()
        try:
            region = db.query(RegionModel).filter_by(REGION_ID=region_id).first()
            if not region:
                return "Error: Region not found."

            child = db.query(RegionModel).filter_by(PARENT_REGION_ID=region_id).first()
            if child:
                return (
                    f"Error: Cannot delete '{region.NAME}' because it has SubRegions. "
                    "Delete or detach them first."
                )
            if region.RULER_ID:
                return (
                    f"Error: Cannot delete '{region.NAME}' because it has a Ruler "
                    f"({region.RULER_ID}). Remove the Ruler first."
                )
            assigned = db.query(BuildingModel).filter_by(REGION_ID=region_id).first()
            if assigned:
                return (
                    f"Error: Cannot delete '{region.NAME}' because buildings are assigned to it. "
                    "Detach them first."
                )

            region_name = region.NAME
            entrance_id = region.ENTRANCE_BUILDING_ID
            db.delete(region)
            db.commit()
            logging.info("Deleted region '%s' (%s).", region_name, region_id)

            # 自動生成された入口 (ID 規約 entrance_<region_id>) は Region と運命を
            # 共にする。ユーザー指定の既存 Building が入口の場合は残す。
            note = ""
            if entrance_id == entrance_id_for(region_id):
                entrance_result = self.delete_building(entrance_id)
                if entrance_result.startswith("Error"):
                    note = f" Note: auto-created entrance could not be removed: {entrance_result}"
                else:
                    note = " Auto-created entrance building was also removed."
            return f"Region '{region_name}' deleted successfully.{note}"
        except Exception as exc:
            db.rollback()
            logging.error("Failed to delete region '%s': %s", region_id, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

    def set_building_region(self, building_id: str, region_id: Optional[str]) -> str:
        """Building の Region 所属を設定/解除する (region_id=None で解除)。"""
        db = self.SessionLocal()
        try:
            building = db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
            if not building:
                return "Error: Building not found."

            # 分離監査 P1-6 (W7 柱5): 入口 Building の所属は「親スコープ」という
            # 不変条件ごと Region service (create/update/delete_region) が管理する。
            # ここで自由に付け替えられると、入口を Region 自身の内部へ入れて外から
            # 見えなくしたり、detach で到達不能にできてしまう。
            entrance_owner = db.query(RegionModel).filter_by(
                ENTRANCE_BUILDING_ID=building_id
            ).first()
            if entrance_owner is not None:
                return (
                    f"Error: '{building.BUILDINGNAME}' is the entrance of region "
                    f"'{entrance_owner.NAME}'. Its region assignment is managed by "
                    "the region itself (change the region's parent instead)."
                )

            if region_id:
                region = db.query(RegionModel).filter_by(REGION_ID=region_id).first()
                if not region:
                    return "Error: Region not found."
                if region.CITYID != building.CITYID:
                    return "Error: Region belongs to a different city."
                building.REGION_ID = region_id
            else:
                building.REGION_ID = None

            db.commit()
            logging.info(
                "Set region of building '%s' to %s.", building_id, region_id or "(none)"
            )
            return f"Building '{building.BUILDINGNAME}' region set to {region_id or '(none)'}."
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to set region of building '%s': %s", building_id, exc, exc_info=True
            )
            return f"Error: {exc}"
        finally:
            db.close()

    # --- Item management ---

    def get_item_details(self, item_id: str) -> Optional[Dict[str, Any]]:
        db = self.SessionLocal()
        try:
            item = db.query(ItemModel).filter(ItemModel.ITEM_ID == item_id).first()
            if not item:
                return None
            location = (
                db.query(ItemLocationModel)
                .filter(ItemLocationModel.ITEM_ID == item_id)
                .first()
            )
            return {
                "ITEM_ID": item.ITEM_ID,
                "NAME": item.NAME,
                "TYPE": item.TYPE,
                "DESCRIPTION": item.DESCRIPTION or "",
                "FILE_PATH": item.FILE_PATH or "",
                "STATE_JSON": item.STATE_JSON or "",
                "CREATOR_ID": item.CREATOR_ID,
                "SOURCE_CONTEXT": item.SOURCE_CONTEXT,
                "CREATED_AT": item.CREATED_AT.isoformat() if item.CREATED_AT else None,
                "OWNER_KIND": location.OWNER_KIND if location else "world",
                "OWNER_ID": location.OWNER_ID if location else "",
            }
        finally:
            db.close()

    def create_item(
        self,
        name: str,
        item_type: str,
        description: str,
        owner_kind: str,
        owner_id: Optional[str],
        state_json: Optional[str],
        file_path: Optional[str] = None,
        creator_id: Optional[str] = None,
        source_context: Optional[str] = None,
    ) -> str:
        normalized_kind = (owner_kind or "world").strip().lower()
        owner_id = (owner_id or "").strip()
        if normalized_kind in {"building", "persona", "bag"} and not owner_id:
            return f"Error: owner_id is required for {normalized_kind} ownership."
        if normalized_kind == "building" and owner_id not in self.building_map:
            return f"Error: Building '{owner_id}' not found."
        if normalized_kind == "persona" and owner_id not in self.personas:
            return f"Error: Persona '{owner_id}' not found."
        if normalized_kind == "bag":
            bag_item = self.manager.item_service.items.get(owner_id)
            if not bag_item:
                return f"Error: Bag item '{owner_id}' not found."
            if (bag_item.get("type") or "").lower() != "bag":
                return f"Error: Item '{owner_id}' is not a bag."
        state_payload = (state_json or "").strip()
        if state_payload:
            try:
                json.loads(state_payload)
            except json.JSONDecodeError as exc:
                return f"Error: STATE_JSON must be valid JSON. {exc}"
        else:
            state_payload = None

        item_id = str(uuid.uuid4())
        db = self.SessionLocal()
        try:
            new_item = ItemModel(
                ITEM_ID=item_id,
                NAME=name,
                TYPE=item_type or "object",
                DESCRIPTION=description or "",
                STATE_JSON=state_payload,
                FILE_PATH=(file_path or "").strip() or None,
                CREATOR_ID=creator_id,
                SOURCE_CONTEXT=source_context,
            )
            db.add(new_item)
            if normalized_kind != "world":
                slot_num = self.manager.item_service._assign_slot(
                    db, normalized_kind, owner_id
                )
                db.add(
                    ItemLocationModel(
                        ITEM_ID=item_id,
                        OWNER_KIND=normalized_kind,
                        OWNER_ID=owner_id,
                        SLOT_NUMBER=slot_num,
                    )
                )
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.error("Failed to create item '%s': %s", name, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

        self.manager._load_items_from_db()
        return f"Item '{name}' created successfully."

    def update_item(
        self,
        item_id: str,
        name: str,
        item_type: str,
        description: str,
        owner_kind: str,
        owner_id: Optional[str],
        state_json: Optional[str],
        file_path: Optional[str] = None,
    ) -> str:
        normalized_kind = (owner_kind or "world").strip().lower()
        owner_id = (owner_id or "").strip()
        if normalized_kind in {"building", "persona", "bag"} and not owner_id:
            return f"Error: owner_id is required for {normalized_kind} ownership."
        if normalized_kind == "building" and owner_id not in self.building_map:
            return f"Error: Building '{owner_id}' not found."
        if normalized_kind == "persona" and owner_id not in self.personas:
            return f"Error: Persona '{owner_id}' not found."
        if normalized_kind == "bag":
            bag_item = self.manager.item_service.items.get(owner_id)
            if not bag_item:
                return f"Error: Bag item '{owner_id}' not found."
            if (bag_item.get("type") or "").lower() != "bag":
                return f"Error: Item '{owner_id}' is not a bag."
            if owner_id == item_id:
                return "Error: Cannot place an item inside itself."
        state_payload = (state_json or "").strip()
        if state_payload:
            try:
                json.loads(state_payload)
            except json.JSONDecodeError as exc:
                return f"Error: STATE_JSON must be valid JSON. {exc}"
        else:
            state_payload = None

        db = self.SessionLocal()
        try:
            item = db.query(ItemModel).filter(ItemModel.ITEM_ID == item_id).first()
            if not item:
                return f"Error: Item '{item_id}' not found."
            item.NAME = name
            item.TYPE = item_type or "object"
            item.DESCRIPTION = description or ""
            item.STATE_JSON = state_payload
            item.FILE_PATH = (file_path or "").strip() or None
            location = (
                db.query(ItemLocationModel)
                .filter(ItemLocationModel.ITEM_ID == item_id)
                .first()
            )
            if normalized_kind == "world":
                if location:
                    db.delete(location)
            else:
                if location:
                    owner_changed = (
                        location.OWNER_KIND != normalized_kind
                        or location.OWNER_ID != owner_id
                    )
                    if owner_changed:
                        slot_num = self.manager.item_service._assign_slot(
                            db, normalized_kind, owner_id
                        )
                        location.OWNER_KIND = normalized_kind
                        location.OWNER_ID = owner_id
                        location.SLOT_NUMBER = slot_num
                else:
                    slot_num = self.manager.item_service._assign_slot(
                        db, normalized_kind, owner_id
                    )
                    db.add(
                        ItemLocationModel(
                            ITEM_ID=item_id,
                            OWNER_KIND=normalized_kind,
                            OWNER_ID=owner_id,
                            SLOT_NUMBER=slot_num,
                        )
                    )
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.error("Failed to update item '%s': %s", item_id, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

        self.manager._load_items_from_db()
        return f"Item '{name}' updated successfully."

    def delete_item(self, item_id: str) -> str:
        db = self.SessionLocal()
        try:
            item = db.query(ItemModel).filter(ItemModel.ITEM_ID == item_id).first()
            if not item:
                return f"Error: Item '{item_id}' not found."
            item_name = item.NAME
            db.query(ItemLocationModel).filter(ItemLocationModel.ITEM_ID == item_id).delete(
                synchronize_session=False
            )
            db.delete(item)
            db.commit()
        except Exception as exc:
            db.rollback()
            logging.error("Failed to delete item '%s': %s", item_id, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

        self.manager._load_items_from_db()
        return f"Item '{item_name}' deleted successfully."

    # --- AI management ---

    def get_ai_details(self, ai_id: str) -> Optional[Dict]:
        db = self.SessionLocal()
        try:
            ai = db.query(AIModel).filter(AIModel.AIID == ai_id).first()
            if not ai:
                return None
            return {
                "AIID": ai.AIID,
                "AINAME": ai.AINAME,
                "HOME_CITYID": ai.HOME_CITYID,
                "SYSTEMPROMPT": ai.SYSTEMPROMPT,
                "DESCRIPTION": ai.DESCRIPTION,
                "AVATAR_IMAGE": ai.AVATAR_IMAGE,
                "APPEARANCE_IMAGE_PATH": ai.APPEARANCE_IMAGE_PATH,
                "IS_DISPATCHED": ai.IS_DISPATCHED,
                "DEFAULT_MODEL": ai.DEFAULT_MODEL,
                "LIGHTWEIGHT_MODEL": ai.LIGHTWEIGHT_MODEL,
                "VISION_MODEL": ai.VISION_MODEL,
                "AUDIO_MODEL": ai.AUDIO_MODEL,
                "VIDEO_MODEL": ai.VIDEO_MODEL,
                "MEMORY_WEAVE_MODEL": ai.MEMORY_WEAVE_MODEL,
                "AUTONOMY_ENABLED": ai.AUTONOMY_ENABLED,
                "CHRONICLE_ENABLED": ai.CHRONICLE_ENABLED,
                "AUTONOMOUS_CHRONICLE_ENABLED": ai.AUTONOMOUS_CHRONICLE_ENABLED,
                "AUTO_RECALL_ENABLED": ai.AUTO_RECALL_ENABLED,
                "MEMORY_WEAVE_CONTEXT": ai.MEMORY_WEAVE_CONTEXT,
                "MEMOPEDIA_INDEX_ENABLED": ai.MEMOPEDIA_INDEX_ENABLED,
                "CORE_MEMORY_CHAR_BUDGET": ai.CORE_MEMORY_CHAR_BUDGET,
                "SPELL_ENABLED": ai.SPELL_ENABLED,
                "REALTIME_INFO_ENABLED": ai.REALTIME_INFO_ENABLED,
                "META_JUDGMENT_CONFIG": ai.META_JUDGMENT_CONFIG,
                "USER_CONV_TIMEOUT_MINUTES": ai.USER_CONV_TIMEOUT_MINUTES,
            }
        finally:
            db.close()

    def create_ai(
        self, name: str, system_prompt: str, home_city_id: int, custom_ai_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[str], Optional[str]]:
        if home_city_id != self.state.city_id:
            return (
                False,
                "Creating personas in a different city is not supported. "
                "Use dispatch to move personas between cities.",
                None,
                None,
            )
        success, message, ai_id, room_id = self._create_persona(name, system_prompt, custom_ai_id)
        if success:
            return (
                True,
                f"AI '{name}' and their room created successfully. "
                "A restart is required for the AI to become active.",
                ai_id,
                room_id,
            )
        return False, message, None, None

    def update_ai(
        self,
        ai_id: str,
        name: str,
        description: str,
        system_prompt: str,
        home_city_id: int,
        default_model: Optional[str],
        lightweight_model: Optional[str],
        autonomy_enabled: bool,
        avatar_path: Optional[str],
        avatar_upload: Optional[str],
        appearance_image_path: Optional[str] = None,
        vision_model: Optional[str] = None,
        audio_model: Optional[str] = None,
        video_model: Optional[str] = None,
        memory_weave_model: Optional[str] = None,
        chronicle_enabled: Optional[bool] = None,
        autonomous_chronicle_enabled: Optional[bool] = None,
        auto_recall_enabled: Optional[bool] = None,
        memory_weave_context: Optional[bool] = None,
        memopedia_index_enabled: Optional[bool] = None,
        core_memory_char_budget: Optional[int] = None,
        spell_enabled: Optional[bool] = None,
        realtime_info_enabled: Optional[bool] = None,
        meta_judgment_config: Optional[Dict[str, Any]] = None,
        user_conv_timeout_minutes: Optional[int] = None,
    ) -> str:
        db = self.SessionLocal()
        try:
            ai = db.query(AIModel).filter(AIModel.AIID == ai_id).first()
            if not ai:
                return f"Error: AI with ID '{ai_id}' not found."

            if ai.HOME_CITYID != home_city_id and ai.IS_DISPATCHED:
                return (
                    "Error: Cannot change the home city of a dispatched AI. "
                    f"Please return '{ai.AINAME}' to their home city first."
                )

            avatar_value: Optional[str] = (avatar_path or "").strip() or None
            if avatar_upload:
                try:
                    upload_path = Path(avatar_upload)
                    avatar_value = self._process_avatar_upload(ai_id, upload_path)
                except Exception as exc:
                    logging.error(
                        "Failed to store avatar upload for %s: %s",
                        ai_id,
                        exc,
                        exc_info=True,
                    )
                    return f"Error: Failed to process avatar upload: {exc}"

            original_autonomy = ai.AUTONOMY_ENABLED
            state_changed = original_autonomy != autonomy_enabled

            if state_changed:
                ai.AUTONOMY_ENABLED = autonomy_enabled

            ai.AINAME = name
            ai.DESCRIPTION = description
            ai.SYSTEMPROMPT = system_prompt
            ai.HOME_CITYID = home_city_id
            ai.DEFAULT_MODEL = default_model or None
            ai.LIGHTWEIGHT_MODEL = lightweight_model or None
            ai.VISION_MODEL = vision_model or None
            ai.AUDIO_MODEL = audio_model or None
            ai.VIDEO_MODEL = video_model or None
            ai.MEMORY_WEAVE_MODEL = memory_weave_model or None
            ai.AVATAR_IMAGE = avatar_value
            # Update appearance image path if provided
            if appearance_image_path is not None:
                ai.APPEARANCE_IMAGE_PATH = appearance_image_path.strip() if appearance_image_path.strip() else None
            # Update Chronicle auto-generation toggle
            if chronicle_enabled is not None:
                ai.CHRONICLE_ENABLED = chronicle_enabled
            # Update autonomous-Pulse Chronicle generation toggle (Phase 0, memory_architecture_v2 §6.3)
            if autonomous_chronicle_enabled is not None:
                ai.AUTONOMOUS_CHRONICLE_ENABLED = autonomous_chronicle_enabled
            # Update auto-recall (記憶アーキv2 ゾーン C) per-persona toggle
            if auto_recall_enabled is not None:
                ai.AUTO_RECALL_ENABLED = auto_recall_enabled
            # Update Memory Weave context injection toggle
            if memory_weave_context is not None:
                ai.MEMORY_WEAVE_CONTEXT = memory_weave_context
            # Update Memopedia 索引の head 常時表示 (旧方式) 復活トグル
            if memopedia_index_enabled is not None:
                ai.MEMOPEDIA_INDEX_ENABLED = memopedia_index_enabled
            # Update コア記憶の文字数目安 (記憶アーキv2 ゾーン A, §5)。
            # 0 / 負値が渡されたら NULL に倒して既定値運用 (= 2000 字) に戻す。
            if core_memory_char_budget is not None:
                if core_memory_char_budget > 0:
                    ai.CORE_MEMORY_CHAR_BUDGET = int(core_memory_char_budget)
                else:
                    ai.CORE_MEMORY_CHAR_BUDGET = None
            # Update Spell system toggle
            if spell_enabled is not None:
                ai.SPELL_ENABLED = spell_enabled
            # Update realtime info injection toggle
            if realtime_info_enabled is not None:
                ai.REALTIME_INFO_ENABLED = realtime_info_enabled
            # Update Meta-Judgment Pulse configuration (Phase 4-e)
            if meta_judgment_config is not None:
                if isinstance(meta_judgment_config, dict) and meta_judgment_config:
                    ai.META_JUDGMENT_CONFIG = json.dumps(meta_judgment_config, ensure_ascii=False)
                else:
                    # 空 dict / None / その他は NULL に倒して既定値運用に戻す
                    ai.META_JUDGMENT_CONFIG = None
            # 2026-05-09: wait_response Track の自動 pause タイマー閾値 (分)。
            # 0 / 負値が渡されたら NULL に倒して既定値運用 (= 30 分) に戻す。
            if user_conv_timeout_minutes is not None:
                if user_conv_timeout_minutes > 0:
                    ai.USER_CONV_TIMEOUT_MINUTES = int(user_conv_timeout_minutes)
                else:
                    ai.USER_CONV_TIMEOUT_MINUTES = None
            db.commit()

            llm_warnings = []
            if ai_id in self.personas:
                persona = self.personas[ai_id]
                persona.persona_name = name
                persona.persona_system_instruction = system_prompt
                persona.autonomy_enabled = ai.AUTONOMY_ENABLED
                persona.lightweight_model = lightweight_model
                persona.vision_model = vision_model
                persona.audio_model = audio_model
                persona.video_model = video_model
                persona.memory_weave_model = memory_weave_model

                # Phase C-2: AUTONOMY_ENABLED 変更を AutonomyManager に反映
                # (True なら起動、False なら停止)。
                # ``ensure_autonomy_for`` は SAIVerseManager のメソッドのため、
                # AdminService からは ``self.manager`` 経由で呼び出す。
                if state_changed:
                    try:
                        ensure_autonomy = getattr(
                            self.manager, "ensure_autonomy_for", None
                        )
                        if callable(ensure_autonomy):
                            ensure_autonomy(ai_id)
                    except Exception:
                        logging.warning(
                            "Failed to sync AutonomyManager state for '%s'",
                            ai_id, exc_info=True,
                        )

                # Update default model and recreate LLM client if model changed
                # If a global chat-option override is active, preserve it;
                # only the DB value (ai.DEFAULT_MODEL) was updated above.
                # NOTE: Use self.state.model (live reference) rather than
                # self.model (snapshot from __init__) so chat-option overrides
                # set after AdminService construction are visible.
                global_model_override = getattr(self.state, 'model', None)
                if global_model_override:
                    new_model = global_model_override
                else:
                    new_model = default_model
                if new_model and persona.model != new_model:
                    persona.model = new_model
                    from llm_clients import get_llm_client
                    from saiverse.model_configs import get_context_length, get_model_provider, model_supports_images
                    try:
                        context_len = get_context_length(new_model)
                        provider = get_model_provider(new_model)
                        persona.llm_client = get_llm_client(new_model, provider, context_len)
                        persona.model_supports_images = model_supports_images(new_model)
                        logging.info(
                            "Recreated LLM client for persona '%s' with model '%s'.",
                            name,
                            new_model,
                        )
                    except Exception as exc:
                        logging.error(
                            "Failed to recreate LLM client for '%s': %s",
                            name,
                            exc,
                        )
                        llm_warnings.append(f"モデル '{new_model}' のLLMクライアント作成に失敗: {exc}")

                # Recreate lightweight LLM client if model changed
                if lightweight_model:
                    from llm_clients import get_llm_client
                    from saiverse.model_configs import get_context_length, get_model_provider
                    try:
                        lw_context = get_context_length(lightweight_model)
                        lw_provider = get_model_provider(lightweight_model)
                        persona.lightweight_llm_client = get_llm_client(
                            lightweight_model, lw_provider, lw_context
                        )
                        logging.info(
                            "Recreated lightweight LLM client for persona '%s' with model '%s'.",
                            name,
                            lightweight_model,
                        )
                    except Exception as exc:
                        logging.error(
                            "Failed to recreate lightweight LLM client for '%s': %s",
                            name,
                            exc,
                        )
                        persona.lightweight_llm_client = None
                        llm_warnings.append(f"軽量モデル '{lightweight_model}' のLLMクライアント作成に失敗: {exc}")
                else:
                    persona.lightweight_llm_client = None

                logging.info("Updated in-memory persona '%s' with new settings.", name)
            self._set_persona_avatar(ai_id, avatar_value)

            status_message = f"AI '{name}' updated successfully."
            if llm_warnings:
                status_message += " [WARNING:LLM] " + "; ".join(llm_warnings)
            if state_changed:
                status_message += (
                    f" Autonomy changed from {original_autonomy} to {autonomy_enabled}."
                )
            return status_message
        except Exception as exc:
            db.rollback()
            logging.error("Failed to update AI '%s': %s", ai_id, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()
    def delete_ai(self, ai_id: str) -> str:
        if self._is_seeded_entity(ai_id):
            return "Error: Seeded AIs cannot be deleted."

        db = self.SessionLocal()
        try:
            ai = db.query(AIModel).filter_by(AIID=ai_id).first()
            if not ai:
                return "Error: AI not found."
            if ai.IS_DISPATCHED:
                return (
                    f"Error: Cannot delete a dispatched AI. Please return '{ai.AINAME}' "
                    "to their home city first."
                )

            db.query(BuildingOccupancyLog).filter(
                BuildingOccupancyLog.AIID == ai_id,
                BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None),
            ).update({"EXIT_TIMESTAMP": datetime.now()})

            db.delete(ai)
            db.commit()

            if ai_id in self.personas:
                persona_name = self.personas[ai_id].persona_name
                del self.personas[ai_id]
                self.persona_map.pop(persona_name, None)
                logging.info("Removed local persona instance '%s' from memory.", persona_name)

            self.id_to_name_map.pop(ai_id, None)
            self.avatar_map.pop(ai_id, None)
            for building_id in self.occupants:
                if ai_id in self.occupants[building_id]:
                    self.occupants[building_id].remove(ai_id)

            logging.info("Deleted AI '%s' (%s).", ai.AINAME, ai_id)
            return f"AI '{ai.AINAME}' deleted successfully."
        except Exception as exc:
            db.rollback()
            logging.error("Failed to delete AI '%s': %s", ai_id, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

    def move_ai_from_editor(self, ai_id: str, target_building_id: str) -> str:
        if not ai_id or not target_building_id:
            return "Error: AI ID and Target Building ID are required."

        persona = self.personas.get(ai_id)
        if not persona:
            if ai_id in self.visiting_personas:
                return (
                    "Error: Cannot manage the interaction mode of a visiting persona "
                    "from the editor."
                )
            return f"Error: Persona with ID '{ai_id}' not found in memory."

        if target_building_id not in self.building_map:
            return f"Error: Target building '{target_building_id}' not found."

        from_building_id = persona.current_building_id
        if from_building_id == target_building_id:
            return f"{persona.persona_name} is already in that building."

        if from_building_id == self.user_room_id:
            return (
                "Can't move, because this persona in user room. "
                "Please execute end conversation."
            )

        if target_building_id == self.user_room_id:
            logging.info("[EditorMove] Summoning '%s' to user room.", persona.persona_name)
            success, reason = self.summon_persona(ai_id)
            if success:
                return f"Successfully summoned '{persona.persona_name}' to your room."
            return f"Failed to summon '{persona.persona_name}': {reason}"

        logging.info(
            "[EditorMove] Moving '%s' from '%s' to '%s'.",
            persona.persona_name,
            self.building_map.get(from_building_id, Building(from_building_id, "", 0, "", "")).name,
            self.building_map.get(target_building_id, Building(target_building_id, "", 0, "", "")).name,
        )
        success, reason = self._move_persona(
            ai_id, from_building_id, target_building_id
        )
        if success:
            # 位置属性と cursor 儀式は move_entity が canonical sync 済み (W7 柱5)
            return (
                f"Successfully moved '{persona.persona_name}' to "
                f"'{self.building_map[target_building_id].name}'."
            )
        return f"Failed to move: {reason}"

    def trigger_world_event(self, event_message: str) -> str:
        if not event_message:
            return "Error: Event message cannot be empty."

        try:
            logging.info(
                "Triggering world event for city '%s': %s",
                self.state.city_name,
                event_message,
            )
            formatted_message = (
                "<div class=\"note-box\">🌐 World Event:<br>"
                f"<b>{event_message}</b></div>"
            )
            for building_id in self.building_map.keys():
                # heard_by = current occupants so all present personas perceive
                # the world event in their auto_ingest. add_building_event
                # handles quarantine skip and modified_buildings marking.
                self.add_building_event(
                    building_id,
                    {"role": "host", "content": formatted_message},
                    heard_by=list(self.occupants.get(building_id, [])),
                )
            self._save_modified_buildings()
            logging.info("World event successfully broadcasted to all buildings.")
            return "World event triggered successfully."
        except Exception as exc:
            logging.error("Failed to trigger world event: %s", exc, exc_info=True)
            return f"An internal error occurred: {exc}"

    def get_linked_tool_ids(self, building_id: str) -> List[int]:
        if not building_id:
            return []
        db = self.SessionLocal()
        try:
            links = (
                db.query(BuildingToolLink.TOOLID)
                .filter_by(BUILDINGID=building_id)
                .all()
            )
            return [link[0] for link in links]
        finally:
            db.close()

    # --- Playbook Management ---

    def get_playbook_details(self, playbook_id: int) -> Optional[Dict[str, Any]]:
        """Get detailed information for a specific playbook."""
        db = self.SessionLocal()
        try:
            # Convert numpy.int64 to Python int (DataFrames return numpy types)
            playbook_id = int(playbook_id)
            playbook = db.query(PlaybookModel).filter(PlaybookModel.id == playbook_id).first()
            if not playbook:
                return None
            return {
                "id": playbook.id,
                "name": playbook.name,
                "description": playbook.description,
                "scope": playbook.scope,
                "created_by_persona_id": playbook.created_by_persona_id,
                "building_id": playbook.building_id,
                "schema_json": playbook.schema_json,
                "nodes_json": playbook.nodes_json,
                "router_callable": playbook.router_callable,
                "created_at": str(playbook.created_at) if playbook.created_at else "",
                "updated_at": str(playbook.updated_at) if playbook.updated_at else "",
            }
        finally:
            db.close()

    def update_playbook(
        self,
        playbook_id: int,
        name: str,
        description: str,
        scope: str,
        created_by_persona_id: Optional[str],
        building_id: Optional[str],
        schema_json: str,
        nodes_json: str,
        router_callable: bool,
    ) -> str:
        """Update an existing playbook."""
        db = self.SessionLocal()
        try:
            playbook = db.query(PlaybookModel).filter(PlaybookModel.id == playbook_id).first()
            if not playbook:
                return f"Error: Playbook with id {playbook_id} not found."

            playbook.name = name
            playbook.description = description
            playbook.scope = scope
            playbook.created_by_persona_id = created_by_persona_id
            playbook.building_id = building_id
            playbook.schema_json = schema_json
            playbook.nodes_json = nodes_json
            playbook.router_callable = router_callable

            db.commit()
            return f"Success: Playbook '{name}' updated successfully."
        except Exception as exc:
            db.rollback()
            logging.error("Failed to update playbook: %s", exc, exc_info=True)
            return f"Error: Failed to update playbook. {exc}"
        finally:
            db.close()

    def delete_playbook(self, playbook_id: int) -> str:
        """Delete a playbook by ID."""
        db = self.SessionLocal()
        try:
            playbook = db.query(PlaybookModel).filter(PlaybookModel.id == playbook_id).first()
            if not playbook:
                return f"Error: Playbook with id {playbook_id} not found."

            name = playbook.name
            db.delete(playbook)
            db.commit()
            return f"Success: Playbook '{name}' deleted successfully."
        except Exception as exc:
            db.rollback()
            logging.error("Failed to delete playbook: %s", exc, exc_info=True)
            return f"Error: Failed to delete playbook. {exc}"
        finally:
            db.close()

    def import_playbook_from_file(self, file_path: str) -> str:
        """Import a playbook JSON file and save/update it in the database."""
        try:
            path = Path(file_path)
            if not path.exists():
                return f"Error: File not found: {file_path}"
            if path.is_dir():
                return "Error: Please select a JSON file, not a directory."

            data = json.loads(path.read_text(encoding="utf-8"))
            scope, persona_id, building_id = infer_scope_from_path(path)
            name = data.get("name")
            if not name:
                return f"Error: Playbook name is missing in {path.name}."
            description = data.get("description", "")

            save_playbook(
                name=name,
                description=description,
                scope=scope,
                created_by_persona_id=persona_id,
                building_id=building_id,
                playbook_json=json.dumps(data, ensure_ascii=False),
                router_callable=None,
                user_selectable=None,
            )
            return f"Success: Imported playbook '{name}' (scope={scope})."
        except Exception as exc:
            logging.error("Failed to import playbook from %s: %s", file_path, exc, exc_info=True)
            return f"Error: Failed to import playbook. {exc}"

    def reimport_all_playbooks(self, base_dir: Optional[str] = None) -> str:
        """Re-import all playbooks from builtin_data and user_data directories."""
        try:
            from saiverse.data_paths import get_all_data_paths, PLAYBOOKS_DIR
            
            # Collect all directories to scan
            roots: list[Path] = []
            if base_dir:
                custom_root = Path(base_dir)
                if not custom_root.is_absolute():
                    custom_root = Path(__file__).resolve().parents[1] / custom_root
                if custom_root.exists():
                    roots.append(custom_root)
                else:
                    return f"Error: Directory not found: {custom_root}"
            else:
                # Use both builtin_data and user_data playbooks directories
                for playbook_path in get_all_data_paths(PLAYBOOKS_DIR):
                    if playbook_path.exists():
                        roots.append(playbook_path)
                
                # Fallback to legacy sea/playbooks if no other directories exist
                legacy_path = Path(__file__).resolve().parents[1] / "sea" / "playbooks"
                if not roots and legacy_path.exists():
                    roots.append(legacy_path)
            
            if not roots:
                return "Error: No playbook directories found."

            imported = 0
            failed = 0
            total_scanned = 0

            for root in roots:
                json_files = sorted(p for p in root.rglob("*.json") if p.is_file())
                total_scanned += len(json_files)
                
                for json_path in json_files:
                    try:
                        data = json.loads(json_path.read_text(encoding="utf-8"))
                        name = data.get("name")
                        if not name:
                            logging.warning("Skipping %s: missing 'name' field", json_path)
                            failed += 1
                            continue

                        scope, persona_id, building_id = infer_scope_from_path(json_path)
                        save_playbook(
                            name=name,
                            description=data.get("description", ""),
                            scope=scope,
                            created_by_persona_id=persona_id,
                            building_id=building_id,
                            playbook_json=json.dumps(data, ensure_ascii=False),
                            router_callable=None,
                            user_selectable=None,
                        )
                        imported += 1
                    except Exception as inner_exc:
                        failed += 1
                        logging.error("Failed to import %s: %s", json_path, inner_exc, exc_info=True)

            dirs_scanned = ", ".join(str(r) for r in roots)
            return f"Reimport finished: imported={imported}, failed={failed}, scanned={total_scanned} from [{dirs_scanned}]."
        except Exception as exc:
            logging.error("Failed to reimport playbooks: %s", exc, exc_info=True)
            return f"Error: Failed to reimport playbooks. {exc}"

    # --- Helpers ---

    @staticmethod
    def _is_seeded_entity(entity_id: str) -> bool:
        if not isinstance(entity_id, str):
            return False

        seeded_prefixes = [
            "air_",
            "eris_",
            "genesis_",
            "luna_",
            "sol_",
            "user_room_",
            "deep_think_room_",
            "altar_of_creation_",
        ]
        return any(entity_id.startswith(prefix) for prefix in seeded_prefixes)
