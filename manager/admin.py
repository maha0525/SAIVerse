import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from zoneinfo import ZoneInfo

from buildings import Building
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
)
from manager.blueprints import BlueprintMixin
from manager.history import HistoryMixin
from manager.persona import PersonaMixin
from manager.state import CoreState
from scripts.import_playbook import infer_scope_from_path
from tools.defs.save_playbook import save_playbook

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
        self.provider = state.provider
        self.context_length = state.context_length
        self.default_avatar = state.default_avatar
        self.host_avatar = state.host_avatar
        self.timezone_info = state.timezone_info
        self.timezone_name = state.timezone_name

        self.user_room_id = state.user_room_id

        # Hooks back to runtime methods
        self._move_persona = runtime._move_persona
        self._explore_city = runtime.explore_city
        self.dispatch_persona = runtime.dispatch_persona
        self.summon_persona = runtime.summon_persona
        self.end_conversation = runtime.end_conversation
        self.get_summonable_personas = runtime.get_summonable_personas
        self.get_conversing_personas = runtime.get_conversing_personas
        self.get_persona_pending_events = manager.get_persona_pending_events
        self.archive_persona_events = manager.archive_persona_events
        self.occupancy_manager = manager.occupancy_manager
        self.conversation_managers = manager.conversation_managers
        self._save_building_histories = manager._save_building_histories
        self._update_timezone_cache = manager._update_timezone_cache
        self._load_cities_from_db = manager._load_cities_from_db

    # --- City management ---

    def get_cities_df(self) -> pd.DataFrame:
        db = self.SessionLocal()
        try:
            query = db.query(CityModel)
            df = pd.read_sql(query.statement, query.session.bind)
            cols = [
                "CITYID",
                "CITYNAME",
                "DESCRIPTION",
                "TIMEZONE",
                "START_IN_ONLINE_MODE",
                "UI_PORT",
                "API_PORT",
            ]
            existing_cols = [c for c in cols if c in df.columns]
            return df[existing_cols]
        finally:
            db.close()

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
    ) -> str:
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
            if city.CITYNAME in ["city_a", "city_b"]:
                return "Error: Seeded cities (city_a, city_b) cannot be deleted."
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

    def get_buildings_df(self) -> pd.DataFrame:
        db = self.SessionLocal()
        try:
            query = db.query(BuildingModel)
            df = pd.read_sql(query.statement, query.session.bind)
            return df[
                [
                    "BUILDINGID",
                    "BUILDINGNAME",
                    "CAPACITY",
                    "DESCRIPTION",
                    "SYSTEM_INSTRUCTION",
                    "CITYID",
                    "AUTO_INTERVAL_SEC",
                ]
            ]
        finally:
            db.close()

    def create_building(
        self,
        name: str,
        description: str,
        capacity: int,
        system_instruction: str,
        city_id: int,
    ) -> str:
        db = self.SessionLocal()
        try:
            if not db.query(CityModel).filter_by(CITYID=city_id).first():
                return "Error: Target city not found."
            if db.query(BuildingModel).filter_by(CITYID=city_id, BUILDINGNAME=name).first():
                return f"Error: A building named '{name}' already exists in that city."

            city = db.query(CityModel).filter_by(CITYID=city_id).first()
            building_id = f"{name.lower().replace(' ', '_')}_{city.CITYNAME}"
            if db.query(BuildingModel).filter_by(BUILDINGID=building_id).first():
                return (
                    f"Error: A building with the generated ID '{building_id}' "
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
            logging.info("Created new building '%s' in city %s.", name, city_id)
            return (
                f"Building '{name}' created successfully. "
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
    ) -> str:
        db = self.SessionLocal()
        try:
            building = db.query(BuildingModel).filter_by(BUILDINGID=building_id).first()
            if not building:
                return "Error: Building not found."

            occupancy = (
                db.query(BuildingOccupancyLog)
                .filter_by(BUILDINGID=building_id, EXIT_TIMESTAMP=None)
                .first()
            )
            if occupancy and building.CITYID != city_id:
                return (
                    f"Error: Cannot change the city of '{building.BUILDINGNAME}' "
                    "while it is occupied."
                )

            building.BUILDINGNAME = name
            building.CAPACITY = capacity
            building.DESCRIPTION = description
            building.SYSTEM_INSTRUCTION = system_instruction
            building.AUTO_INTERVAL_SEC = interval
            building.CITYID = city_id

            db.query(BuildingToolLink).filter_by(BUILDINGID=building_id).delete(
                synchronize_session=False
            )
            for tool_id in tool_ids:
                db.add(BuildingToolLink(BUILDINGID=building_id, TOOLID=int(tool_id)))

            db.commit()
            logging.info(
                "Updated building '%s' (%s) and its tool links.", name, building_id
            )
            return (
                f"Building '{name}' and its tool links updated successfully. "
                "A restart is required for the changes to take full effect."
            )
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to update building '%s': %s", building_id, exc, exc_info=True
            )
            return f"Error: {exc}"
        finally:
            db.close()

    # --- Item management ---

    def get_items_df(self) -> pd.DataFrame:
        db = self.SessionLocal()
        try:
            query = (
                db.query(ItemModel, ItemLocationModel)
                .outerjoin(ItemLocationModel, ItemModel.ITEM_ID == ItemLocationModel.ITEM_ID)
            )
            rows: List[Dict[str, Any]] = []
            for item, location in query:
                rows.append(
                    {
                        "ITEM_ID": item.ITEM_ID,
                        "NAME": item.NAME,
                        "TYPE": item.TYPE,
                        "DESCRIPTION": item.DESCRIPTION,
                        "OWNER_KIND": getattr(location, "OWNER_KIND", "world"),
                        "OWNER_ID": getattr(location, "OWNER_ID", ""),
                        "UPDATED_AT": str(getattr(item, "UPDATED_AT", "")),
                    }
                )
            columns = [
                "ITEM_ID",
                "NAME",
                "TYPE",
                "DESCRIPTION",
                "OWNER_KIND",
                "OWNER_ID",
                "UPDATED_AT",
            ]
            if not rows:
                return pd.DataFrame(columns=columns)
            return pd.DataFrame(rows, columns=columns)
        finally:
            db.close()

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
    ) -> str:
        normalized_kind = (owner_kind or "world").strip().lower()
        owner_id = (owner_id or "").strip()
        if normalized_kind in {"building", "persona"} and not owner_id:
            return "Error: owner_id is required for building or persona ownership."
        if normalized_kind == "building" and owner_id not in self.building_map:
            return f"Error: Building '{owner_id}' not found."
        if normalized_kind == "persona" and owner_id not in self.personas:
            return f"Error: Persona '{owner_id}' not found."
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
            )
            db.add(new_item)
            if normalized_kind != "world":
                db.add(
                    ItemLocationModel(
                        ITEM_ID=item_id,
                        OWNER_KIND=normalized_kind,
                        OWNER_ID=owner_id,
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
        if normalized_kind in {"building", "persona"} and not owner_id:
            return "Error: owner_id is required for building or persona ownership."
        if normalized_kind == "building" and owner_id not in self.building_map:
            return f"Error: Building '{owner_id}' not found."
        if normalized_kind == "persona" and owner_id not in self.personas:
            return f"Error: Persona '{owner_id}' not found."
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
                    location.OWNER_KIND = normalized_kind
                    location.OWNER_ID = owner_id
                else:
                    db.add(
                        ItemLocationModel(
                            ITEM_ID=item_id,
                            OWNER_KIND=normalized_kind,
                            OWNER_ID=owner_id,
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

    def get_ais_df(self) -> pd.DataFrame:
        db = self.SessionLocal()
        try:
            query = db.query(AIModel)
            df = pd.read_sql(query.statement, query.session.bind)
            df["SYSTEMPROMPT_SNIPPET"] = df["SYSTEMPROMPT"].str.slice(0, 40) + "..."
            return df[
                [
                    "AIID",
                    "AINAME",
                    "HOME_CITYID",
                    "DEFAULT_MODEL",
                    "IS_DISPATCHED",
                    "DESCRIPTION",
                    "SYSTEMPROMPT_SNIPPET",
                ]
            ]
        finally:
            db.close()

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
                "IS_DISPATCHED": ai.IS_DISPATCHED,
                "DEFAULT_MODEL": ai.DEFAULT_MODEL,
                "LIGHTWEIGHT_MODEL": ai.LIGHTWEIGHT_MODEL,
                "INTERACTION_MODE": ai.INTERACTION_MODE,
            }
        finally:
            db.close()

    def create_ai(self, name: str, system_prompt: str, home_city_id: int) -> str:
        if home_city_id != self.state.city_id:
            return (
                "Error: Creating personas in a different city is not supported. "
                "Use dispatch to move personas between cities."
            )
        success, message = self._create_persona(name, system_prompt)
        if success:
            return (
                f"AI '{name}' and their room created successfully. "
                "A restart is required for the AI to become active."
            )
        return f"Error: {message}"

    def update_ai(
        self,
        ai_id: str,
        name: str,
        description: str,
        system_prompt: str,
        home_city_id: int,
        default_model: Optional[str],
        lightweight_model: Optional[str],
        interaction_mode: str,
        avatar_path: Optional[str],
        avatar_upload: Optional[str],
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

            original_mode = ai.INTERACTION_MODE
            mode_changed = original_mode != interaction_mode
            move_feedback = ""

            if mode_changed:
                if interaction_mode == "sleep":
                    ai.INTERACTION_MODE = "sleep"
                    logging.info(
                        "AI '%s' mode changed to 'sleep'. Attempting to move to private room.",
                        name,
                    )

                    private_room_id = ai.PRIVATE_ROOM_ID
                    if not private_room_id or private_room_id not in self.building_map:
                        move_feedback = (
                            " Note: Could not move to private room because it is not "
                            "configured or invalid."
                        )
                        logging.warning(
                            "Cannot move AI '%s' to sleep. Private room ID '%s' is invalid.",
                            name,
                            private_room_id,
                        )
                    else:
                        current_building_id = self.personas[ai_id].current_building_id
                        if current_building_id != private_room_id:
                            success, reason = self._move_persona(
                                ai_id,
                                current_building_id,
                                private_room_id,
                                db_session=db,
                            )
                            if success:
                                self.personas[ai_id].current_building_id = private_room_id
                                move_feedback = (
                                    " Moved to private room "
                                    f"'{self.building_map[private_room_id].name}'."
                                )
                                logging.info(
                                    "Successfully moved AI '%s' to their private room '%s'.",
                                    name,
                                    private_room_id,
                                )
                            else:
                                move_feedback = (
                                    f" Note: Failed to move to private room: {reason}."
                                )
                                logging.error(
                                    "Failed to move AI '%s' to private room: %s",
                                    name,
                                    reason,
                                )
                elif interaction_mode in ("auto", "manual"):
                    ai.INTERACTION_MODE = interaction_mode
                else:
                    logging.warning(
                        "Invalid interaction mode '%s' requested for AI '%s'. No change made.",
                        interaction_mode,
                        name,
                    )

            ai.AINAME = name
            ai.DESCRIPTION = description
            ai.SYSTEMPROMPT = system_prompt
            ai.HOME_CITYID = home_city_id
            ai.DEFAULT_MODEL = default_model or None
            ai.LIGHTWEIGHT_MODEL = lightweight_model or None
            ai.AVATAR_IMAGE = avatar_value
            db.commit()

            if ai_id in self.personas:
                persona = self.personas[ai_id]
                persona.persona_name = name
                persona.persona_system_instruction = system_prompt
                persona.interaction_mode = ai.INTERACTION_MODE
                persona.lightweight_model = lightweight_model

                # Recreate lightweight LLM client if model changed
                if lightweight_model:
                    from llm_clients import get_llm_client
                    from model_configs import get_context_length
                    try:
                        lw_context = get_context_length(lightweight_model)
                        persona.lightweight_llm_client = get_llm_client(
                            lightweight_model, self.provider, lw_context
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
                else:
                    persona.lightweight_llm_client = None

                logging.info("Updated in-memory persona '%s' with new settings.", name)
            self._set_persona_avatar(ai_id, avatar_value)

            status_message = f"AI '{name}' updated successfully."
            if mode_changed:
                status_message += (
                    f" Mode changed from '{original_mode}' to '{interaction_mode}'."
                )
            return status_message + move_feedback
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
            persona.current_building_id = target_building_id
            persona.register_entry(target_building_id)
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
                self.building_histories.setdefault(building_id, []).append(
                    {"role": "host", "content": formatted_message}
                )
            self._save_building_histories()
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

    def get_playbooks_df(self) -> pd.DataFrame:
        """Get all playbooks as a DataFrame."""
        db = self.SessionLocal()
        try:
            query = db.query(PlaybookModel)
            df = pd.read_sql(query.statement, query.session.bind)
            # Add snippet columns for long text fields
            if not df.empty:
                df["description_snippet"] = df["description"].str.slice(0, 50) + "..."
                if "schema_json" in df.columns:
                    df["schema_snippet"] = df["schema_json"].str.slice(0, 30) + "..."
                if "nodes_json" in df.columns:
                    df["nodes_snippet"] = df["nodes_json"].str.slice(0, 30) + "..."
            return df
        finally:
            db.close()

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
        """Re-import all playbooks under sea/playbooks (or a custom directory)."""
        try:
            root = Path(base_dir) if base_dir else Path(__file__).resolve().parents[1] / "sea" / "playbooks"
            if not root.is_absolute():
                root = Path(__file__).resolve().parents[1] / root
            if not root.exists():
                return f"Error: Directory not found: {root}"

            json_files = sorted(p for p in root.rglob("*.json") if p.is_file())
            if not json_files:
                return f"Warning: No JSON files found under {root}."

            imported = 0
            failed = 0

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

            total = len(json_files)
            return f"Reimport finished: imported={imported}, failed={failed}, scanned={total} under {root}."
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
