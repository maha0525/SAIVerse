import base64
import logging
import mimetypes
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image
from sqlalchemy import func
from saiverse.buildings import Building
from database.models import (
    AI as AIModel,
    Building as BuildingModel,
    BuildingOccupancyLog,
    BuildingToolLink,
    User,
    UserAiLink,
)
from persona.core import PersonaCore
from saiverse.model_configs import get_context_length, get_model_provider


class PersonaMixin:
    """Persona lifecycle helpers shared across the SAIVerse manager."""

    city_id: int
    city_name: str
    model: str
    provider: str
    context_length: int
    default_avatar: str
    saiverse_home: Path
    user_room_id: str
    timezone_info: object
    timezone_name: str
    building_map: Dict[str, Building]
    buildings: List[Building]
    building_histories: Dict[str, List[Dict[str, str]]]
    building_memory_paths: Dict[str, Path]
    capacities: Dict[str, int]
    occupants: Dict[str, List[str]]
    personas: Dict[str, PersonaCore]
    visiting_personas: Dict[str, PersonaCore]
    avatar_map: Dict[str, str]
    persona_map: Dict[str, str]
    id_to_name_map: Dict[str, str]

    def _set_persona_avatar(self, ai_id: str, avatar_value: Optional[str]) -> None:
        """Update in-memory avatar cache and persona reference."""
        display_value = self.default_avatar
        if avatar_value:
            try:
                avatar_path = Path(avatar_value)
                if avatar_path.exists():
                    mime = mimetypes.guess_type(avatar_path.name)[0] or "image/png"
                    data_b = avatar_path.read_bytes()
                    b64 = base64.b64encode(data_b).decode("ascii")
                    display_value = f"data:{mime};base64,{b64}"
                else:
                    display_value = avatar_value
            except Exception as exc:
                logging.error("Failed to process avatar for %s: %s", ai_id, exc)
                display_value = self.default_avatar
        else:
            avatar_value = None

        self.avatar_map[ai_id] = display_value
        if ai_id in self.personas:
            self.personas[ai_id].avatar_image = avatar_value

    def _process_avatar_upload(self, ai_id: str, upload_path: Path) -> str:
        """Resize & convert avatar uploads to WebP for caching."""
        from saiverse.data_paths import get_user_icons_dir
        avatars_dir = get_user_icons_dir()
        dest_name = f"{ai_id}_{int(time.time())}.webp"
        dest_path = avatars_dir / dest_name
        with Image.open(upload_path) as img:
            img = img.convert("RGBA")
            img.thumbnail((256, 256), Image.LANCZOS)
            width, height = img.size
            img.save(dest_path, "WEBP", quality=85, method=6)
        logging.info(
            "Processed avatar upload for '%s': %s (%dx%d WEBP)",
            ai_id,
            dest_path,
            width,
            height,
        )
        return str(dest_path)

    def _load_personas_from_db(self) -> None:
        """DBからペルソナ情報を読み込み、PersonaCoreインスタンスを生成する"""
        db = self.SessionLocal()
        try:
            db_personas = (
                db.query(AIModel).filter(AIModel.HOME_CITYID == self.city_id).all()
            )
            failed_count = 0
            for db_ai in db_personas:
                pid = db_ai.AIID
                try:
                    self._load_single_persona(db, db_ai)
                except Exception as exc:
                    failed_count += 1
                    msg = f"Failed to load persona '{pid}': {exc}"
                    logging.error(msg, exc_info=True)
                    self.startup_warnings.append({
                        "source": "persona_load",
                        "message": msg,
                    })
            logging.info(
                "Loaded %d personas from database (%d failed).",
                len(self.personas), failed_count,
            )
            # Check for embedding model changes across all loaded personas
            changed_personas = [
                pid
                for pid, p in self.personas.items()
                if getattr(getattr(p, "memory", None), "embed_model_changed", False)
            ]
            if changed_personas:
                names = ", ".join(changed_personas)
                self.startup_warnings.append({
                    "source": "embed_model_mismatch",
                    "message": (
                        f"Embeddingモデルが変更されました。記憶想起を正常に動作させるため、"
                        f"再計算を推奨します。（対象: {names}）"
                    ),
                    "persona_ids": changed_personas,
                })
        except Exception as exc:
            msg = f"Failed to query personas from DB: {exc}"
            logging.error(msg, exc_info=True)
            self.startup_warnings.append({
                "source": "persona_load",
                "message": msg,
            })
        finally:
            db.close()

    def _load_single_persona(self, db, db_ai) -> None:
        """単一のペルソナをDBレコードからロードする"""
        pid = db_ai.AIID
        default_room_id = f"{pid}_room"
        raw_private_room_id = (db_ai.PRIVATE_ROOM_ID or "").strip()
        private_room_id = raw_private_room_id or default_room_id

        self._set_persona_avatar(pid, db_ai.AVATAR_IMAGE)

        persona_model = db_ai.DEFAULT_MODEL or self.model or self._base_model
        persona_context_length = get_context_length(persona_model)
        persona_provider = get_model_provider(persona_model)
        persona_lightweight_model = db_ai.LIGHTWEIGHT_MODEL

        from saiverse.data_paths import find_file, PROMPTS_DIR
        common_prompt_file = find_file(PROMPTS_DIR, "common.txt") or Path("system_prompts/common.txt")

        # Get linked user name (first linked user, or "the user" as fallback)
        linked_user_name = "the user"
        linked_user = (
            db.query(User)
            .join(UserAiLink, User.USERID == UserAiLink.USERID)
            .filter(UserAiLink.AIID == pid)
            .first()
        )
        if linked_user:
            linked_user_name = linked_user.USERNAME

        persona = PersonaCore(
            city_name=self.city_name,
            persona_id=pid,
            persona_name=db_ai.AINAME,
            persona_system_instruction=db_ai.SYSTEMPROMPT or "",
            avatar_image=db_ai.AVATAR_IMAGE,
            buildings=self.buildings,
            common_prompt_path=common_prompt_file,
            action_priority_path=Path("builtin_data/action_priority.json"),
            building_histories=self.building_histories,
            occupants=self.occupants,
            id_to_name_map=self.id_to_name_map,
            move_callback=self._move_persona,
            dispatch_callback=self.dispatch_persona,
            explore_callback=self._explore_city,
            create_persona_callback=self._create_persona,
            session_factory=self.SessionLocal,
            start_building_id=private_room_id,
            model=persona_model,
            lightweight_model=persona_lightweight_model,
            context_length=persona_context_length,
            user_room_id=self.user_room_id,
            provider=persona_provider,
            interaction_mode=(db_ai.INTERACTION_MODE or "auto"),
            is_dispatched=db_ai.IS_DISPATCHED,
            timezone_info=self.timezone_info,
            timezone_name=self.timezone_name,
            item_registry=self.items,
            inventory_item_ids=self.items_by_persona.get(pid, []),
            persona_event_fetcher=self.get_persona_pending_events,
            persona_event_ack=self.archive_persona_events,
            manager_ref=self,
            linked_user_name=linked_user_name,
        )

        persona.private_room_id = private_room_id
        if private_room_id not in self.building_map:
            logging.warning(
                "Persona '%s' private room '%s' is missing from building_map.",
                pid,
                private_room_id,
            )

        self.personas[pid] = persona

    def _load_occupancy_from_db(self) -> None:
        """DBから現在の入室状況を読み込み、PersonaCoreとManagerの状態を更新する"""
        db = self.SessionLocal()
        try:
            current_occupancy = (
                db.query(BuildingOccupancyLog)
                .filter(BuildingOccupancyLog.CITYID == self.city_id)
                .filter(BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None))
                .all()
            )

            self.occupants.clear()
            for building in self.buildings:
                self.occupants[building.building_id] = []

            for log in current_occupancy:
                pid = log.AIID
                bid = log.BUILDINGID
                if pid in self.personas and bid in self.building_map:
                    self.occupants[bid].append(pid)
                    self.personas[pid].current_building_id = bid
                else:
                    msg = f"Invalid occupancy record: AI '{pid}' or Building '{bid}' does not exist."
                    logging.warning(msg)
                    self.startup_warnings.append({
                        "source": "occupancy_load",
                        "message": msg,
                    })
            if hasattr(self, "state"):
                self.state.occupants = self.occupants
            logging.info("Loaded current occupancy from database.")
        except Exception as exc:
            msg = f"Failed to load occupancy from DB: {exc}"
            logging.error(msg, exc_info=True)
            self.startup_warnings.append({
                "source": "occupancy_load",
                "message": msg,
            })
        finally:
            db.close()

    def _create_persona(
        self, name: str, system_prompt: str, custom_ai_id: Optional[str] = None
    ) -> Tuple[bool, str, Optional[str], Optional[str]]:
        """
        Dynamically creates a new persona, their private room, and places them in it.
        This is triggered by an AI action.

        Args:
            name: Display name for the persona
            system_prompt: System prompt for the persona
            custom_ai_id: Optional custom ID (alphanumeric + underscore). If not provided,
                          auto-generated from name.
        """
        db = self.SessionLocal()
        try:
            existing_ai = (
                db.query(AIModel)
                .filter(
                    AIModel.HOME_CITYID == self.city_id,
                    func.lower(AIModel.AINAME) == func.lower(name),
                )
                .first()
            )
            if existing_ai:
                return False, f"A persona named '{name}' already exists in this city.", None, None

            # Use custom ID if provided, otherwise auto-generate from name
            if custom_ai_id:
                new_ai_id = f"{custom_ai_id}_{self.city_name}"
            else:
                new_ai_id = f"{name.lower().replace(' ', '_')}_{self.city_name}"

            if db.query(AIModel).filter_by(AIID=new_ai_id).first():
                return (
                    False,
                    f"A persona with the ID '{new_ai_id}' already exists.",
                    None,
                    None,
                )

            # Building ID based on AI ID
            if custom_ai_id:
                new_building_id = f"{custom_ai_id}_{self.city_name}_room"
            else:
                new_building_id = f"{name.lower().replace(' ', '_')}_{self.city_name}_room"

            new_ai_model = AIModel(
                AIID=new_ai_id,
                HOME_CITYID=self.city_id,
                AINAME=name,
                SYSTEMPROMPT=system_prompt,
                DESCRIPTION=f"A new persona named {name}.",
                AUTO_COUNT=0,
                INTERACTION_MODE="manual",
                IS_DISPATCHED=False,
                DEFAULT_MODEL=self.model,
                CHRONICLE_ENABLED=False,
                PRIVATE_ROOM_ID=new_building_id,
            )
            db.add(new_ai_model)
            logging.info("DB: Added new AI '%s' (%s).", name, new_ai_id)

            new_building_model = BuildingModel(
                CITYID=self.city_id,
                BUILDINGID=new_building_id,
                BUILDINGNAME=f"{name}の部屋",
                CAPACITY=1,
                SYSTEM_INSTRUCTION=f"{name}が待機する個室です。",
                DESCRIPTION=f"{name}のプライベートルーム。",
            )
            db.add(new_building_model)
            logging.info(
                "DB: Added new building '%s' (%s).",
                new_building_model.BUILDINGNAME,
                new_building_id,
            )

            new_occupancy_log = BuildingOccupancyLog(
                CITYID=self.city_id,
                AIID=new_ai_id,
                BUILDINGID=new_building_id,
                ENTRY_TIMESTAMP=datetime.now(),
            )
            db.add(new_occupancy_log)
            logging.info(
                "DB: Added initial occupancy for '%s' in their room.", name
            )

            new_building_obj = Building(
                building_id=new_building_model.BUILDINGID,
                name=new_building_model.BUILDINGNAME,
                capacity=new_building_model.CAPACITY,
                system_instruction=new_building_model.SYSTEM_INSTRUCTION,
                description=new_building_model.DESCRIPTION,
            )
            self.buildings.append(new_building_obj)
            self.building_map[new_building_id] = new_building_obj
            self.capacities[new_building_id] = new_building_obj.capacity
            self.occupants[new_building_id] = [new_ai_id]
            self.building_memory_paths[new_building_id] = (
                self.saiverse_home
                / "cities"
                / self.city_name
                / "buildings"
                / new_building_id
                / "log.json"
            )
            self.building_histories[new_building_id] = []

            new_persona_model = self.model or self._base_model
            new_persona_provider = get_model_provider(new_persona_model)  # Get provider for model
            new_persona_context_length = get_context_length(new_persona_model)

            from saiverse.data_paths import find_file, PROMPTS_DIR
            common_prompt_file = find_file(PROMPTS_DIR, "common.txt") or Path("system_prompts/common.txt")
            new_persona_core = PersonaCore(
                city_name=self.city_name,
                persona_id=new_ai_id,
                persona_name=name,
                persona_system_instruction=system_prompt,
                avatar_image=None,
                buildings=self.buildings,
                common_prompt_path=common_prompt_file,
                action_priority_path=Path("builtin_data/action_priority.json"),
                building_histories=self.building_histories,
                occupants=self.occupants,
                id_to_name_map=self.id_to_name_map,
                move_callback=self._move_persona,
                dispatch_callback=self.dispatch_persona,
                explore_callback=self._explore_city,
                create_persona_callback=self._create_persona,
                session_factory=self.SessionLocal,
                start_building_id=new_building_id,
                model=new_persona_model,
                lightweight_model=None,
                context_length=new_persona_context_length,
                user_room_id=self.user_room_id,
                provider=new_persona_provider,  # Use provider for model
                is_dispatched=False,
                timezone_info=self.timezone_info,
                timezone_name=self.timezone_name,
                item_registry=self.items,
                inventory_item_ids=self.items_by_persona.get(new_ai_id, []),
                persona_event_fetcher=self.get_persona_pending_events,
                persona_event_ack=self.archive_persona_events,
                manager_ref=self,
            )
            new_persona_core.private_room_id = new_building_id
            self.personas[new_ai_id] = new_persona_core
            self.avatar_map[new_ai_id] = self.default_avatar
            self.id_to_name_map[new_ai_id] = name
            self.persona_map[name] = new_ai_id

            db.commit()
            return True, f"Persona '{name}' created successfully.", new_ai_id, new_building_id
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to create new persona '%s': %s", name, exc, exc_info=True
            )
            return False, f"An internal error occurred: {exc}", None, None
        finally:
            db.close()

    def get_ai_details(self, ai_id: str) -> Optional[Dict]:
        """Get full details for a single AI for the edit form."""
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
                "INTERACTION_MODE": ai.INTERACTION_MODE,
            }
        finally:
            db.close()

    def create_ai(self, name: str, system_prompt: str) -> str:
        """Creates a new AI and their private room, similar to _create_persona."""
        success, message, _ai_id, _room_id = self._create_persona(name, system_prompt)
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
        interaction_mode: str,
        avatar_path: Optional[str],
        avatar_upload: Optional[str],
        chronicle_enabled: Optional[bool] = None,
    ) -> str:
        """ワールドエディタからAIの設定を更新する"""
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
                        "Failed to store avatar upload for %s: %s", ai_id, exc, exc_info=True
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
            ai.AVATAR_IMAGE = avatar_value
            db.commit()

            if ai_id in self.personas:
                persona = self.personas[ai_id]
                persona.persona_name = name
                persona.persona_system_instruction = system_prompt
                persona.interaction_mode = ai.INTERACTION_MODE
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
        """Deletes an AI after checking its state."""
        if self._is_seeded_entity(ai_id):
            return "Error: Seeded AIs cannot be deleted."

        db = self.SessionLocal()
        try:
            ai = db.query(AIModel).filter_by(AIID=ai_id).first()
            if not ai:
                return "Error: AI not found."
            if ai.IS_DISPATCHED:
                return (
                    "Error: Cannot delete a dispatched AI. "
                    f"Please return '{ai.AINAME}' to their home city first."
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
                if persona_name in self.persona_map:
                    del self.persona_map[persona_name]
                logging.info(
                    "Removed local persona instance '%s' from memory.", persona_name
                )

            if ai_id in self.id_to_name_map:
                del self.id_to_name_map[ai_id]
            if ai_id in self.avatar_map:
                del self.avatar_map[ai_id]
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

    def get_linked_tool_ids(self, building_id: str) -> List[int]:
        """Gets a list of tool IDs linked to a specific building."""
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
