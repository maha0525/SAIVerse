import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import func

from database.models import (
    AI as AIModel,
    Building as BuildingModel,
    BuildingOccupancyLog,
    Blueprint,
    BuildingToolLink,
    City as CityModel,
    Tool as ToolModel,
)
from persona_core import PersonaCore
from buildings import Building


class BlueprintMixin:
    """Blueprint and tool management helpers for SAIVerseManager."""

    def get_blueprint_details(self, blueprint_id: int) -> Optional[Dict]:
        """Get full details for a single blueprint for the edit form."""
        db = self.SessionLocal()
        try:
            blueprint = (
                db.query(Blueprint)
                .filter(Blueprint.BLUEPRINT_ID == blueprint_id)
                .first()
            )
            if not blueprint:
                return None
            return {
                "BLUEPRINT_ID": blueprint.BLUEPRINT_ID,
                "NAME": blueprint.NAME,
                "DESCRIPTION": blueprint.DESCRIPTION,
                "CITYID": blueprint.CITYID,
                "BASE_SYSTEM_PROMPT": blueprint.BASE_SYSTEM_PROMPT,
                "ENTITY_TYPE": blueprint.ENTITY_TYPE,
            }
        finally:
            db.close()

    def get_blueprint_choices(self) -> List[str]:
        """Return blueprint IDs as string choices for UI dropdowns."""
        db = self.SessionLocal()
        try:
            results = db.query(Blueprint.BLUEPRINT_ID).order_by(Blueprint.NAME.asc()).all()
            return [str(row.BLUEPRINT_ID) for row in results]
        finally:
            db.close()

    def get_blueprints_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのBlueprint一覧をDataFrameとして取得する"""
        db = self.SessionLocal()
        try:
            query = db.query(Blueprint)
            df = pd.read_sql(query.statement, query.session.bind)
            return df[["BLUEPRINT_ID", "NAME", "DESCRIPTION", "ENTITY_TYPE", "CITYID"]]
        finally:
            db.close()

    def create_blueprint(
        self,
        name: str,
        description: str,
        city_id: int,
        system_prompt: str,
        entity_type: str,
    ) -> str:
        """ワールドエディタから新しいBlueprintを作成する"""
        db = self.SessionLocal()
        try:
            existing = db.query(Blueprint).filter_by(CITYID=city_id, NAME=name).first()
            if existing:
                return f"Error: A blueprint named '{name}' already exists in this city."

            new_blueprint = Blueprint(
                CITYID=city_id,
                NAME=name,
                DESCRIPTION=description,
                BASE_SYSTEM_PROMPT=system_prompt,
                ENTITY_TYPE=entity_type,
            )
            db.add(new_blueprint)
            db.commit()
            logging.info("Created new blueprint '%s' in City ID %s.", name, city_id)
            return f"Blueprint '{name}' created successfully."
        except Exception as exc:
            db.rollback()
            logging.error("Failed to create blueprint '%s': %s", name, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

    def update_blueprint(
        self,
        blueprint_id: int,
        name: str,
        description: str,
        city_id: int,
        system_prompt: str,
        entity_type: str,
    ) -> str:
        """ワールドエディタからBlueprintの設定を更新する"""
        db = self.SessionLocal()
        try:
            blueprint = db.query(Blueprint).filter_by(BLUEPRINT_ID=blueprint_id).first()
            if not blueprint:
                return "Error: Blueprint not found."
            if not city_id:
                return "Error: City must be selected."

            if blueprint.NAME != name or blueprint.CITYID != city_id:
                existing = db.query(Blueprint).filter_by(CITYID=city_id, NAME=name).first()
                if existing:
                    target_city = (
                        db.query(CityModel).filter_by(CITYID=city_id).first()
                    )
                    city_name_for_error = (
                        target_city.CITYNAME if target_city else f"ID {city_id}"
                    )
                    return (
                        "Error: A blueprint named "
                        f"'{name}' already exists in city '{city_name_for_error}'."
                    )

            blueprint.NAME = name
            blueprint.DESCRIPTION = description
            blueprint.CITYID = city_id
            blueprint.BASE_SYSTEM_PROMPT = system_prompt
            blueprint.ENTITY_TYPE = entity_type
            db.commit()
            logging.info("Updated blueprint '%s' (ID: %s).", name, blueprint_id)
            return f"Blueprint '{name}' updated successfully."
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to update blueprint ID %s: %s", blueprint_id, exc, exc_info=True
            )
            return f"Error: {exc}"
        finally:
            db.close()

    def delete_blueprint(self, blueprint_id: int) -> str:
        """ワールドエディタからBlueprintを削除する"""
        db = self.SessionLocal()
        try:
            blueprint = db.query(Blueprint).filter_by(BLUEPRINT_ID=blueprint_id).first()
            if not blueprint:
                return "Error: Blueprint not found."

            db.delete(blueprint)
            db.commit()
            logging.info("Deleted blueprint ID %s.", blueprint_id)
            return "Blueprint deleted successfully."
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to delete blueprint ID %s: %s", blueprint_id, exc, exc_info=True
            )
            return f"Error: {exc}"
        finally:
            db.close()

    def spawn_entity_from_blueprint(
        self, blueprint_id: int, entity_name: str, target_building_id: str
    ) -> Tuple[bool, str]:
        """ブループリントから新しいエンティティを生成し、指定された建物に配置する"""
        db = self.SessionLocal()
        try:
            blueprint = db.query(Blueprint).filter_by(BLUEPRINT_ID=blueprint_id).first()
            if not blueprint:
                return False, "Blueprint not found."
            if target_building_id not in self.building_map:
                return False, f"Target building '{target_building_id}' not found."
            if len(self.occupants.get(target_building_id, [])) >= self.capacities.get(
                target_building_id, 1
            ):
                return (
                    False,
                    f"Target building '{self.building_map[target_building_id].name}' is at full capacity.",
                )
            if (
                db.query(AIModel)
                .filter(func.lower(AIModel.AINAME) == func.lower(entity_name))
                .first()
            ):
                return (
                    False,
                    f"An entity named '{entity_name}' already exists.",
                )

            home_city = db.query(CityModel).filter_by(CITYID=blueprint.CITYID).first()
            new_ai_id = f"{entity_name.lower().replace(' ', '_')}_{home_city.CITYNAME}"
            if db.query(AIModel).filter_by(AIID=new_ai_id).first():
                return (
                    False,
                    f"An entity with the generated ID '{new_ai_id}' already exists.",
                )

            private_room_id = f"{new_ai_id}_room"
            private_room_model = BuildingModel(
                CITYID=blueprint.CITYID,
                BUILDINGID=private_room_id,
                BUILDINGNAME=f"{entity_name}の部屋",
                CAPACITY=1,
                SYSTEM_INSTRUCTION=f"{entity_name}が待機する個室です。",
                DESCRIPTION=f"{entity_name}のプライベートルーム。",
            )
            db.add(private_room_model)
            logging.info(
                "DB: Added new private room '%s' (%s) for spawned AI.",
                private_room_model.BUILDINGNAME,
                private_room_id,
            )

            new_ai_model = AIModel(
                AIID=new_ai_id,
                HOME_CITYID=blueprint.CITYID,
                AINAME=entity_name,
                SYSTEMPROMPT=blueprint.BASE_SYSTEM_PROMPT,
                DESCRIPTION=blueprint.DESCRIPTION,
                AVATAR_IMAGE=blueprint.BASE_AVATAR,
                DEFAULT_MODEL=self.model,
                PRIVATE_ROOM_ID=private_room_id,
            )
            db.add(new_ai_model)

            target_building_db = (
                db.query(BuildingModel).filter_by(BUILDINGID=target_building_id).first()
            )
            new_occupancy_log = BuildingOccupancyLog(
                CITYID=target_building_db.CITYID,
                AIID=new_ai_id,
                BUILDINGID=target_building_id,
                ENTRY_TIMESTAMP=datetime.now(),
            )
            db.add(new_occupancy_log)

            if blueprint.CITYID == self.city_id:
                new_building_obj = Building(
                    building_id=private_room_model.BUILDINGID,
                    name=private_room_model.BUILDINGNAME,
                    capacity=private_room_model.CAPACITY,
                    system_instruction=private_room_model.SYSTEM_INSTRUCTION,
                    description=private_room_model.DESCRIPTION,
                )
                self.buildings.append(new_building_obj)
                self.building_map[private_room_id] = new_building_obj
                self.capacities[private_room_id] = new_building_obj.capacity
                self.occupants[private_room_id] = []
                self.building_memory_paths[private_room_id] = (
                    self.saiverse_home
                    / "cities"
                    / self.city_name
                    / "buildings"
                    / private_room_id
                    / "log.json"
                )
                self.building_histories[private_room_id] = []

            if blueprint.CITYID == self.city_id:
                new_persona_core = PersonaCore(
                    city_name=self.city_name,
                    persona_id=new_ai_id,
                    persona_name=entity_name,
                    persona_system_instruction=blueprint.BASE_SYSTEM_PROMPT,
                    avatar_image=blueprint.BASE_AVATAR,
                    buildings=self.buildings,
                    common_prompt_path=Path("system_prompts/common.txt"),
                    action_priority_path=Path("action_priority.json"),
                    building_histories=self.building_histories,
                    occupants=self.occupants,
                    id_to_name_map=self.id_to_name_map,
                    move_callback=self._move_persona,
                    dispatch_callback=self.dispatch_persona,
                    explore_callback=self._explore_city,
                    create_persona_callback=self._create_persona,
                session_factory=self.SessionLocal,
                start_building_id=target_building_id,
                model=self.model,
                context_length=self.context_length,
                user_room_id=self.user_room_id,
                provider=self.provider,
                is_dispatched=False,
                timezone_info=self.timezone_info,
                timezone_name=self.timezone_name,
                item_registry=self.items,
                inventory_item_ids=self.items_by_persona.get(new_ai_id, []),
                persona_event_fetcher=self.get_persona_pending_events,
                persona_event_ack=self.archive_persona_events,
                manager_ref=self,
            )
                self.personas[new_ai_id] = new_persona_core
                self.avatar_map[new_ai_id] = self.default_avatar
                self.id_to_name_map[new_ai_id] = entity_name
                self.persona_map[entity_name] = new_ai_id

            self.occupants.setdefault(target_building_id, []).append(new_ai_id)
            arrival_message = (
                "<div class=\"note-box\">✨ Blueprint Spawn:<br>"
                f"<b>{entity_name}がこの世界に現れました</b></div>"
            )
            self.building_histories.setdefault(target_building_id, []).append(
                {"role": "host", "content": arrival_message}
            )
            self._save_building_histories()

            db.commit()
            return (
                True,
                f"Entity '{entity_name}' spawned successfully in "
                f"'{self.building_map[target_building_id].name}'.",
            )
        except Exception as exc:
            db.rollback()
            logging.error(
                "Failed to spawn entity from blueprint: %s", exc, exc_info=True
            )
            return False, f"An internal error occurred: {exc}"
        finally:
            db.close()

    # --- Tool Management ---

    def get_tools_df(self) -> pd.DataFrame:
        """ワールドエディタ用にすべてのTool一覧をDataFrameとして取得する"""
        db = self.SessionLocal()
        try:
            query = db.query(ToolModel)
            df = pd.read_sql(query.statement, query.session.bind)
            return df
        finally:
            db.close()

    def get_tool_details(self, tool_id: int) -> Optional[Dict]:
        """Get full details for a single tool for the edit form."""
        db = self.SessionLocal()
        try:
            tool = db.query(ToolModel).filter(ToolModel.TOOLID == tool_id).first()
            if not tool:
                return None
            return {
                "TOOLID": tool.TOOLID,
                "TOOLNAME": tool.TOOLNAME,
                "DESCRIPTION": tool.DESCRIPTION,
                "MODULE_PATH": tool.MODULE_PATH,
                "FUNCTION_NAME": tool.FUNCTION_NAME,
            }
        finally:
            db.close()

    def create_tool(
        self, name: str, description: str, module_path: str, function_name: str
    ) -> str:
        """ワールドエディタから新しいToolを作成する"""
        db = self.SessionLocal()
        try:
            if db.query(ToolModel).filter_by(TOOLNAME=name).first():
                return f"Error: A tool named '{name}' already exists."
            if db.query(ToolModel).filter_by(
                MODULE_PATH=module_path, FUNCTION_NAME=function_name
            ).first():
                return (
                    "Error: A tool with the same module and function name already exists."
                )

            new_tool = ToolModel(
                TOOLNAME=name,
                DESCRIPTION=description,
                MODULE_PATH=module_path,
                FUNCTION_NAME=function_name,
            )
            db.add(new_tool)
            db.commit()
            logging.info("Created new tool '%s'.", name)
            return f"Tool '{name}' created successfully."
        except Exception as exc:
            db.rollback()
            logging.error("Failed to create tool '%s': %s", name, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

    def update_tool(
        self, tool_id: int, name: str, description: str, module_path: str, function_name: str
    ) -> str:
        """ワールドエディタからToolの設定を更新する"""
        db = self.SessionLocal()
        try:
            tool = db.query(ToolModel).filter_by(TOOLID=tool_id).first()
            if not tool:
                return "Error: Tool not found."

            tool.TOOLNAME = name
            tool.DESCRIPTION = description
            tool.MODULE_PATH = module_path
            tool.FUNCTION_NAME = function_name
            db.commit()
            logging.info("Updated tool '%s' (ID: %s).", name, tool_id)
            return f"Tool '{name}' updated successfully."
        except Exception as exc:
            db.rollback()
            logging.error("Failed to update tool ID %s: %s", tool_id, exc, exc_info=True)
            return f"Error: {exc}"
        finally:
            db.close()

    def delete_tool(self, tool_id: int) -> str:
        """ワールドエディタからToolを削除する"""
        db = self.SessionLocal()
        try:
            tool = db.query(ToolModel).filter_by(TOOLID=tool_id).first()
            if not tool:
                return "Error: Tool not found."
            if db.query(BuildingToolLink).filter_by(TOOLID=tool_id).first():
                return (
                    f"Error: Cannot delete tool '{tool.TOOLNAME}' because it is linked to "
                    "one or more buildings."
                )
            db.delete(tool)
            db.commit()
            logging.info("Deleted tool ID %s.", tool_id)
            return f"Tool '{tool.TOOLNAME}' deleted successfully."
        except Exception as exc:
            db.rollback()
            logging.error("Failed to delete tool ID %s: %s", tool_id, exc, exc_info=True)
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
