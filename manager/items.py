"""Item management service extracted from saiverse_manager.py."""
from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from database.models import (
    Item as ItemModel,
    ItemLocation as ItemLocationModel,
)

if TYPE_CHECKING:
    from manager.state import CoreState

LOGGER = logging.getLogger(__name__)


class ItemService:
    """Manages item operations for personas and buildings."""

    def __init__(self, manager: Any, state: "CoreState") -> None:
        self.manager = manager
        self.state = state
        
        # Item data structures (aliases to manager for compatibility)
        self.items: Dict[str, Dict] = {}
        self.item_locations: Dict[str, Dict] = {}
        self.items_by_building: Dict[str, List[str]] = defaultdict(list)
        self.items_by_persona: Dict[str, List[str]] = defaultdict(list)
        self.items_by_bag: Dict[str, List[str]] = defaultdict(list)
        self.world_items: List[str] = []

    def _resolve_file_path(self, file_path_str: str) -> Path:
        """Resolve file path, handling legacy WSL paths and relative paths.
        
        The DB might contain:
        - New format: relative paths like "image/filename.png" or "documents/filename.txt"
        - Legacy format: /home/maha/.saiverse/... paths from WSL
        
        Returns the resolved Path object.
        """
        path = Path(file_path_str)
        
        if path.exists():
            return path
        
        home = self.manager.saiverse_home
        parts = path.parts
        
        # Strategy 0: Handle relative paths (new format)
        if not path.is_absolute():
            candidate = home / file_path_str
            if candidate.exists():
                return candidate
        
        # Strategy 1a: strict 'documents' match (legacy WSL paths)
        if 'documents' in parts:
            idx = parts.index('documents')
            rel = Path(*parts[idx:])
            candidate = home / rel
            if candidate.exists():
                return candidate
        
        # Strategy 1b: strict 'image' match (legacy WSL paths for picture items)
        if 'image' in parts:
            idx = parts.index('image')
            rel = Path(*parts[idx:])
            candidate = home / rel
            if candidate.exists():
                return candidate
        
        # Strategy 2a: just filename in documents (fallback)
        candidate = home / "documents" / path.name
        if candidate.exists():
            return candidate
        
        # Strategy 2b: just filename in image (fallback for picture items)
        candidate = home / "image" / path.name
        if candidate.exists():
            return candidate
        
        # Return original path if no recovery worked
        return path

    def load_items_from_db(self) -> None:
        """Load items and their locations from the database into memory."""
        db = self.manager.SessionLocal()
        try:
            item_rows = db.query(ItemModel).all()
            location_rows = db.query(ItemLocationModel).all()
        except Exception as exc:
            LOGGER.error("Failed to load items from DB: %s", exc, exc_info=True)
            item_rows = []
            location_rows = []
        finally:
            db.close()

        self.items.clear()
        self.item_locations.clear()
        self.items_by_building.clear()
        self.items_by_persona.clear()
        self.items_by_bag.clear()
        self.world_items.clear()

        for row in item_rows:
            if row.STATE_JSON:
                try:
                    state_payload = json.loads(row.STATE_JSON)
                except json.JSONDecodeError:
                    LOGGER.warning("Invalid STATE_JSON for item %s", row.ITEM_ID)
                    state_payload = {}
            else:
                state_payload = {}
            self.items[row.ITEM_ID] = {
                "item_id": row.ITEM_ID,
                "name": row.NAME,
                "type": row.TYPE,
                "description": row.DESCRIPTION or "",
                "file_path": row.FILE_PATH,
                "state": state_payload,
                "creator_id": row.CREATOR_ID,
                "source_context": row.SOURCE_CONTEXT,
                "created_at": row.CREATED_AT,
                "updated_at": row.UPDATED_AT,
            }

        for loc in location_rows:
            payload = {
                "owner_kind": (loc.OWNER_KIND or "").strip(),
                "owner_id": (loc.OWNER_ID or "").strip(),
                "updated_at": loc.UPDATED_AT,
                "location_id": loc.LOCATION_ID,
            }
            self.item_locations[loc.ITEM_ID] = payload
            owner_kind = payload["owner_kind"]
            owner_id = payload["owner_id"]
            if owner_kind == "building":
                self.items_by_building[owner_id].append(loc.ITEM_ID)
            elif owner_kind == "persona":
                self.items_by_persona[owner_id].append(loc.ITEM_ID)
            elif owner_kind == "bag":
                self.items_by_bag[owner_id].append(loc.ITEM_ID)
            else:
                self.world_items.append(loc.ITEM_ID)

        for item_id in self.items.keys():
            if item_id not in self.item_locations:
                self.world_items.append(item_id)

        # Update buildings
        for building in self.manager.buildings:
            building.item_ids = list(self.items_by_building.get(building.building_id, []))
            self.refresh_building_system_instruction(building.building_id)
        
        # Update personas
        if hasattr(self.manager, "personas") and isinstance(self.manager.personas, dict):
            for persona_id, persona in self.manager.personas.items():
                if hasattr(persona, "set_item_registry"):
                    try:
                        persona.set_item_registry(self.items)
                    except Exception as exc:
                        LOGGER.debug("Failed to update item registry for %s: %s", persona_id, exc)
                inventory_ids = self.items_by_persona.get(persona_id, [])
                persona.set_inventory(list(inventory_ids))
        
        # Sync to state
        self._sync_to_state()

    def _sync_to_state(self) -> None:
        """Sync item data to CoreState."""
        if hasattr(self.state, "items"):
            self.state.items = self.items
            self.state.item_locations = self.item_locations
            self.state.items_by_building = {k: list(v) for k, v in self.items_by_building.items()}
            self.state.items_by_persona = {k: list(v) for k, v in self.items_by_persona.items()}
            self.state.items_by_bag = {k: list(v) for k, v in self.items_by_bag.items()}
            self.state.world_items = list(self.world_items)

    def refresh_building_system_instruction(self, building_id: str) -> None:
        """Refresh building.system_instruction to include current item list."""
        building = self.manager.building_map.get(building_id)
        if not building:
            return
        base_text = building.base_system_instruction or ""
        item_ids = self.items_by_building.get(building_id, [])
        if not item_ids:
            building.system_instruction = base_text
            return
        lines: List[str] = []
        for item_id in item_ids:
            data = self.items.get(item_id)
            if not data:
                continue
            description = (data.get("description") or "").strip() or "(説明なし)"
            if len(description) > 160:
                description = description[:157] + "..."
            display_name = data.get("name", item_id)
            lines.append(f"- {display_name}: {description} [アイテムID:\"{item_id}\"]")
        if not lines:
            building.system_instruction = base_text
            return
        items_block = "\n".join(lines)
        marker = "## 現在地にあるアイテム"
        if marker in base_text:
            before, after = base_text.split(marker, 1)
            after = after.lstrip("\n")
            building.system_instruction = f"{before}{marker}\n{items_block}\n{after}".rstrip()
        else:
            building.system_instruction = f"{base_text.rstrip()}\n\n{marker}\n{items_block}"

    def update_item_cache(
        self, item_id: str, owner_kind: str, owner_id: Optional[str], updated_at: datetime
    ) -> None:
        """Update in-memory cache when item location changes."""
        prev = self.item_locations.get(item_id)
        prev_kind = prev.get("owner_kind") if prev else None
        prev_owner = prev.get("owner_id") if prev else None

        # Remove from previous location
        if prev_kind == "building" and prev_owner:
            listing = self.items_by_building.get(prev_owner, [])
            if listing and item_id in listing:
                listing[:] = [itm for itm in listing if itm != item_id]
            if not listing:
                self.items_by_building.pop(prev_owner, None)
            self.refresh_building_system_instruction(prev_owner)
        elif prev_kind == "persona" and prev_owner:
            inventory = self.items_by_persona.get(prev_owner, [])
            if inventory and item_id in inventory:
                inventory[:] = [itm for itm in inventory if itm != item_id]
            if not inventory:
                self.items_by_persona.pop(prev_owner, None)
            persona_obj = self.manager.personas.get(prev_owner)
            if persona_obj:
                persona_obj.set_inventory(self.items_by_persona.get(prev_owner, []))
        elif prev_kind == "bag" and prev_owner:
            bag_contents = self.items_by_bag.get(prev_owner, [])
            if bag_contents and item_id in bag_contents:
                bag_contents[:] = [itm for itm in bag_contents if itm != item_id]
            if not bag_contents:
                self.items_by_bag.pop(prev_owner, None)
        else:
            if item_id in self.world_items:
                self.world_items[:] = [itm for itm in self.world_items if itm != item_id]

        # Add to new location
        if owner_kind == "building" and owner_id:
            listing = self.items_by_building[owner_id]
            if item_id not in listing:
                listing.append(item_id)
            self.refresh_building_system_instruction(owner_id)
        elif owner_kind == "persona" and owner_id:
            inventory = self.items_by_persona[owner_id]
            if item_id not in inventory:
                inventory.append(item_id)
            persona_obj = self.manager.personas.get(owner_id)
            if persona_obj:
                persona_obj.set_inventory(list(inventory))
        elif owner_kind == "bag" and owner_id:
            bag_contents = self.items_by_bag[owner_id]
            if item_id not in bag_contents:
                bag_contents.append(item_id)
        else:
            if item_id not in self.world_items:
                self.world_items.append(item_id)

        self.item_locations[item_id] = {
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "updated_at": updated_at,
        }

    def broadcast_item_event(self, persona_ids: List[str], message: str) -> None:
        """Record persona events for item operations."""
        deduped = {pid for pid in persona_ids if pid}
        for pid in deduped:
            self.manager.record_persona_event(pid, message)

    def pickup_item(self, persona_id: str, item_id: str) -> str:
        """Pick up an item from the current building."""
        persona = self.manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")
        building_id = persona.current_building_id
        if not building_id:
            raise RuntimeError("現在地が不明なため、アイテムを拾えません。")
        
        item = self.items.get(item_id)
        if not item:
            raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")
        location = self.item_locations.get(item_id)
        if not location or location.get("owner_kind") != "building" or location.get("owner_id") != building_id:
            raise RuntimeError("このアイテムは現在の建物にはありません。")

        timestamp = datetime.utcnow()
        db = self.manager.SessionLocal()
        try:
            row = db.query(ItemLocationModel).filter(ItemLocationModel.ITEM_ID == item_id).one_or_none()
            if row is None:
                raise RuntimeError("アイテムの配置情報が見つかりませんでした。")
            row.OWNER_KIND = "persona"
            row.OWNER_ID = persona_id
            row.UPDATED_AT = timestamp
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()

        self.update_item_cache(item_id, "persona", persona_id, timestamp)
        item_name = item.get("name", item_id)
        actor_msg = f"「{item_name}」を拾った。"
        self.manager.record_persona_event(persona_id, actor_msg)
        
        other_ids = [oid for oid in self.manager.occupants.get(building_id, []) if oid and oid != persona_id]
        if other_ids:
            notice = f"{persona.persona_name}が「{item_name}」を拾った。"
            self.broadcast_item_event(other_ids, notice)
        
        building_name = self.manager.building_map.get(building_id).name if building_id in self.manager.building_map else building_id
        note = (
            '<div class="note-box">📦 Item Pickup:<br>'
            f'<b>{persona.persona_name}が「{item_name}」を拾いました（{building_name}）。</b></div>'
        )
        self.manager._append_building_history_note(building_id, note)
        return actor_msg

    def place_item(self, persona_id: str, item_id: str, building_id: Optional[str] = None) -> str:
        """Place an item from inventory into a building."""
        persona = self.manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")
        building_id = building_id or persona.current_building_id
        if not building_id:
            raise RuntimeError("現在地が不明なため、アイテムを置けません。")
        
        item = self.items.get(item_id)
        if not item:
            raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")
        location = self.item_locations.get(item_id)
        if not location or location.get("owner_kind") != "persona" or location.get("owner_id") != persona_id:
            raise RuntimeError("このアイテムを所持していないため、置けません。")

        timestamp = datetime.utcnow()
        db = self.manager.SessionLocal()
        try:
            row = db.query(ItemLocationModel).filter(ItemLocationModel.ITEM_ID == item_id).one_or_none()
            if row is None:
                row = ItemLocationModel(
                    ITEM_ID=item_id,
                    OWNER_KIND="building",
                    OWNER_ID=building_id,
                    UPDATED_AT=timestamp,
                )
                db.add(row)
            else:
                row.OWNER_KIND = "building"
                row.OWNER_ID = building_id
                row.UPDATED_AT = timestamp
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()

        self.update_item_cache(item_id, "building", building_id, timestamp)
        building_name = self.manager.building_map.get(building_id).name if building_id in self.manager.building_map else building_id
        item_name = item.get("name", item_id)
        actor_msg = f"「{item_name}」を{building_name}に置いた。"
        self.manager.record_persona_event(persona_id, actor_msg)
        
        other_ids = [oid for oid in self.manager.occupants.get(building_id, []) if oid and oid != persona_id]
        if other_ids:
            notice = f"{persona.persona_name}が{building_name}に「{item_name}」を置いた。"
            self.broadcast_item_event(other_ids, notice)
        
        note = (
            '<div class="note-box">📦 Item Placement:<br>'
            f'<b>{persona.persona_name}が「{item_name}」を{building_name}に置きました。</b></div>'
        )
        self.manager._append_building_history_note(building_id, note)
        return actor_msg

    def use_item(self, persona_id: str, item_id: str, action_json: str) -> str:
        """Use an item to apply effects."""
        persona = self.manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")
        
        item = self.items.get(item_id)
        if not item:
            raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")
        location = self.item_locations.get(item_id)
        owner_kind = location.get("owner_kind") if location else None
        owner_id = location.get("owner_id") if location else None
        in_inventory = owner_kind == "persona" and owner_id == persona_id
        in_current_building = owner_kind == "building" and owner_id == persona.current_building_id
        if not location or not (in_inventory or in_current_building):
            raise RuntimeError("このアイテムは現在あなたのインベントリまたは現在いる建物にありません。")

        try:
            action_data = json.loads(action_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"action_jsonのパースに失敗しました: {exc}") from exc

        action_type = action_data.get("action_type")
        item_type = (item.get("type") or "").lower()
        timestamp = datetime.utcnow()

        if action_type == "update_description":
            actor_msg = self._handle_update_description(item_id, item, persona_id, action_data, timestamp)
        elif action_type == "patch_content":
            if item_type != "document":
                raise RuntimeError("patch_contentはdocumentタイプのアイテムにのみ使用できます。")
            actor_msg = self._handle_patch_content(item_id, item, persona_id, action_data, timestamp)
        else:
            raise RuntimeError(f"未対応のaction_type: {action_type}")

        self.manager.record_persona_event(persona_id, actor_msg)
        building_id = persona.current_building_id
        item_name = item.get("name", item_id)
        
        other_ids = [oid for oid in self.manager.occupants.get(building_id or "", []) if oid and oid != persona_id]
        if other_ids:
            notice = f"{persona.persona_name}が「{item_name}」を使った。"
            self.broadcast_item_event(other_ids, notice)
        
        if building_id:
            building_name = self.manager.building_map.get(building_id).name if building_id in self.manager.building_map else building_id
            note = (
                '<div class="note-box">🛠 Item Use:<br>'
                f'<b>{persona.persona_name}が「{item_name}」を使いました（{building_name}）。</b></div>'
            )
            self.manager._append_building_history_note(building_id, note)
        return actor_msg

    def _handle_update_description(
        self, item_id: str, item: Dict, persona_id: str, action_data: Dict, timestamp: datetime
    ) -> str:
        """Handle update_description action."""
        cleaned = (action_data.get("description") or "").strip()
        db = self.manager.SessionLocal()
        try:
            row = db.query(ItemModel).filter(ItemModel.ITEM_ID == item_id).one_or_none()
            if row is None:
                raise RuntimeError("アイテム本体が見つかりません。")
            row.DESCRIPTION = cleaned
            row.UPDATED_AT = timestamp
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()

        item["description"] = cleaned
        item["updated_at"] = timestamp
        location_owner_kind = self.item_locations.get(item_id, {}).get("owner_kind")
        location_owner_id = self.item_locations.get(item_id, {}).get("owner_id")
        if location_owner_kind == "building" and location_owner_id:
            self.refresh_building_system_instruction(location_owner_id)
        
        inventory = self.items_by_persona.get(persona_id, [])
        persona_obj = self.manager.personas.get(persona_id)
        if persona_obj:
            persona_obj.set_inventory(list(inventory))

        preview = cleaned if cleaned else "(内容未設定)"
        if len(preview) > 80:
            preview = preview[:77] + "..."
        item_name = item.get("name", item_id)
        return f"「{item_name}」の説明を更新した。内容: {preview}"

    def _handle_patch_content(
        self, item_id: str, item: Dict, persona_id: str, action_data: Dict, timestamp: datetime
    ) -> str:
        """Handle patch_content action for documents."""
        file_path_str = item.get("file_path")
        if not file_path_str:
            raise RuntimeError("このdocumentにはファイルパスが設定されていません。")

        file_path = self._resolve_file_path(file_path_str)
        if not file_path.exists():
            raise RuntimeError(f"ファイルが見つかりません: {file_path}")

        patch = action_data.get("patch", "")
        try:
            current_content = file_path.read_text(encoding="utf-8")
            new_content = current_content + "\n" + patch
            file_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"ファイルの更新に失敗しました: {exc}") from exc

        from saiverse.media_summary import ensure_document_summary
        new_summary = ensure_document_summary(file_path)

        db = self.manager.SessionLocal()
        try:
            row = db.query(ItemModel).filter(ItemModel.ITEM_ID == item_id).one_or_none()
            if row is None:
                raise RuntimeError("アイテム本体が見つかりません。")
            if new_summary:
                row.DESCRIPTION = new_summary
            row.UPDATED_AT = timestamp
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()

        if new_summary:
            item["description"] = new_summary
        item["updated_at"] = timestamp
        location_owner_kind = self.item_locations.get(item_id, {}).get("owner_kind")
        location_owner_id = self.item_locations.get(item_id, {}).get("owner_id")
        if location_owner_kind == "building" and location_owner_id:
            self.refresh_building_system_instruction(location_owner_id)
        
        inventory = self.items_by_persona.get(persona_id, [])
        persona_obj = self.manager.personas.get(persona_id)
        if persona_obj:
            persona_obj.set_inventory(list(inventory))

        item_name = item.get("name", item_id)
        return f"「{item_name}」の内容を更新した。"

    def view_item(self, persona_id: str, item_id: str) -> str:
        """View the full content of a picture or document item."""
        persona = self.manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")

        item = self.items.get(item_id)
        if not item:
            raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")

        item_type = (item.get("type") or "").lower()

        if item_type == "object":
            raise RuntimeError("objectタイプのアイテムは閲覧できません。")
        elif item_type == "picture":
            file_path_str = item.get("file_path")
            if not file_path_str:
                raise RuntimeError("この画像にはファイルパスが設定されていません。")
            file_path = self._resolve_file_path(file_path_str)
            if not file_path.exists():
                raise RuntimeError(f"ファイルが見つかりません: {file_path}")
            return f"画像ファイル: {file_path}"
        elif item_type == "document":
            file_path_str = item.get("file_path")
            if not file_path_str:
                raise RuntimeError("この文書にはファイルパスが設定されていません。")
            file_path = self._resolve_file_path(file_path_str)
            if not file_path.exists():
                raise RuntimeError(f"ファイルが見つかりません: {file_path}")
            try:
                content = file_path.read_text(encoding="utf-8")
                return f"文書の内容:\n\n{content}"
            except OSError as exc:
                raise RuntimeError(f"ファイルの読み込みに失敗しました: {exc}") from exc
        else:
            raise RuntimeError(f"未対応のアイテムタイプ: {item_type}")

    def toggle_item_open_state(self, item_id: str) -> bool:
        """Toggle the open/close state of an item."""
        item = self.items.get(item_id)
        if not item:
            raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")
        
        state = item.get("state", {})
        if not isinstance(state, dict):
            state = {}
        
        current_is_open = state.get("is_open", False)
        new_is_open = not current_is_open
        state["is_open"] = new_is_open
        item["state"] = state
        
        timestamp = datetime.utcnow()
        db = self.manager.SessionLocal()
        try:
            row = db.query(ItemModel).filter(ItemModel.ITEM_ID == item_id).one_or_none()
            if row:
                row.STATE_JSON = json.dumps(state)
                row.UPDATED_AT = timestamp
                db.commit()
        except Exception as exc:
            db.rollback()
            LOGGER.error(f"Failed to update item state in DB: {exc}")
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()
        
        item["updated_at"] = timestamp
        LOGGER.info(f"Item {item_id} is_open toggled to {new_is_open}")
        return new_is_open

    def get_open_items_in_building(self, building_id: str) -> List[Dict]:
        """Get all items in a building that have is_open = True."""
        open_items = []
        item_ids = self.items_by_building.get(building_id, [])
        for item_id in item_ids:
            item = self.items.get(item_id)
            if item:
                state = item.get("state", {})
                if isinstance(state, dict) and state.get("is_open", False):
                    open_items.append(item)
        return open_items

    def get_open_items_for_persona(self, persona_id: str) -> List[Dict]:
        """Get all items in a persona's inventory that have is_open = True."""
        open_items = []
        item_ids = self.items_by_persona.get(persona_id, [])
        for item_id in item_ids:
            item = self.items.get(item_id)
            if item:
                state = item.get("state", {})
                if isinstance(state, dict) and state.get("is_open", False):
                    open_items.append(item)
        return open_items

    def get_all_items_in_building(self, building_id: str) -> List[Dict]:
        """Get all items in a building (regardless of open state)."""
        all_items = []
        for item_id in self.items_by_building.get(building_id, []):
            item = self.items.get(item_id)
            if item:
                all_items.append(item)
        return all_items

    def get_all_items_for_persona(self, persona_id: str) -> List[Dict]:
        """Get all items in a persona's inventory (regardless of open state)."""
        all_items = []
        for item_id in self.items_by_persona.get(persona_id, []):
            item = self.items.get(item_id)
            if item:
                all_items.append(item)
        return all_items

    def create_document_item(self, persona_id: str, name: str, description: str, content: str, source_context: Optional[str] = None) -> str:
        """Create a new document item and place it in the current building."""
        persona = self.manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナでは文書を作成できません。")

        building_id = persona.current_building_id
        if not building_id:
            raise RuntimeError("現在地が不明なため、文書を作成できません。")

        from saiverse.media_utils import store_document_text
        try:
            metadata, file_path = store_document_text(content, source="tool:document_create")
        except Exception as exc:
            raise RuntimeError(f"ファイルの保存に失敗しました: {exc}") from exc

        from saiverse.media_summary import ensure_document_summary
        summary = ensure_document_summary(file_path)
        if not summary:
            summary = description

        item_id = str(uuid.uuid4())
        timestamp = datetime.utcnow()

        db = self.manager.SessionLocal()
        try:
            relative_path = str(file_path.relative_to(self.manager.saiverse_home))
            initial_state = {"is_open": True}
            item_row = ItemModel(
                ITEM_ID=item_id,
                NAME=name,
                TYPE="document",
                DESCRIPTION=summary,
                FILE_PATH=relative_path,
                STATE_JSON=json.dumps(initial_state),
                CREATOR_ID=persona_id,
                SOURCE_CONTEXT=source_context,
                CREATED_AT=timestamp,
                UPDATED_AT=timestamp,
            )
            db.add(item_row)

            location_row = ItemLocationModel(
                ITEM_ID=item_id,
                OWNER_KIND="building",
                OWNER_ID=building_id,
                UPDATED_AT=timestamp,
            )
            db.add(location_row)
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース登録に失敗しました: {exc}") from exc
        finally:
            db.close()

        self.items[item_id] = {
            "item_id": item_id,
            "name": name,
            "type": "document",
            "description": summary,
            "file_path": relative_path,
            "state": {"is_open": True},
            "creator_id": persona_id,
            "source_context": source_context,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.item_locations[item_id] = {
            "owner_kind": "building",
            "owner_id": building_id,
            "updated_at": timestamp,
            "location_id": None,
        }
        self.items_by_building[building_id].append(item_id)
        self.refresh_building_system_instruction(building_id)

        building_name = self.manager.building_map.get(building_id).name if building_id in self.manager.building_map else building_id
        actor_msg = f"「{name}」という文書を作成し、{building_name}に配置した。"
        self.manager.record_persona_event(persona_id, actor_msg)

        note = (
            '<div class="note-box">📄 Document Created:<br>'
            f'<b>{persona.persona_name}が「{name}」を作成しました（{building_name}）。</b></div>'
        )
        self.manager._append_building_history_note(building_id, note)

        return f"文書「{name}」を作成しました。アイテムID: {item_id}"

    def create_picture_item(
        self, persona_id: str, name: str, description: str, file_path: str,
        building_id: Optional[str] = None, source_context: Optional[str] = None,
    ) -> str:
        """Create a new picture item and place it in the specified building."""
        persona = self.manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナでは画像を作成できません。")

        if not building_id:
            building_id = persona.current_building_id
        if not building_id:
            raise RuntimeError("現在地が不明なため、画像を配置できません。")

        item_id = str(uuid.uuid4())
        timestamp = datetime.utcnow()

        file_path_obj = Path(file_path)
        if file_path_obj.is_absolute():
            try:
                relative_path = str(file_path_obj.relative_to(self.manager.saiverse_home))
            except ValueError:
                relative_path = file_path
        else:
            relative_path = file_path

        db = self.manager.SessionLocal()
        try:
            item_row = ItemModel(
                ITEM_ID=item_id,
                NAME=name,
                TYPE="picture",
                DESCRIPTION=description,
                FILE_PATH=relative_path,
                CREATOR_ID=persona_id,
                SOURCE_CONTEXT=source_context,
                CREATED_AT=timestamp,
                UPDATED_AT=timestamp,
            )
            db.add(item_row)

            location_row = ItemLocationModel(
                ITEM_ID=item_id,
                OWNER_KIND="building",
                OWNER_ID=building_id,
                UPDATED_AT=timestamp,
            )
            db.add(location_row)
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース登録に失敗しました: {exc}") from exc
        finally:
            db.close()

        self.items[item_id] = {
            "item_id": item_id,
            "name": name,
            "type": "picture",
            "description": description,
            "file_path": relative_path,
            "state": {},
            "creator_id": persona_id,
            "source_context": source_context,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.item_locations[item_id] = {
            "owner_kind": "building",
            "owner_id": building_id,
            "updated_at": timestamp,
            "location_id": None,
        }
        self.items_by_building[building_id].append(item_id)
        self.refresh_building_system_instruction(building_id)

        building_name = self.manager.building_map.get(building_id).name if building_id in self.manager.building_map else building_id
        actor_msg = f"「{name}」という画像を生成し、{building_name}に配置した。"
        self.manager.record_persona_event(persona_id, actor_msg)

        note = (
            '<div class="note-box">🖼 Picture Created:<br>'
            f'<b>{persona.persona_name}が「{name}」を生成しました（{building_name}）。</b></div>'
        )
        self.manager._append_building_history_note(building_id, note)

        return item_id

    def create_picture_item_for_user(
        self, name: str, description: str, file_path: str, building_id: str,
        creator_id: Optional[str] = None, source_context: Optional[str] = None,
    ) -> str:
        """Create a picture item from user upload and place it in the specified building.

        Unlike create_picture_item, this does not require a persona and is for user uploads.
        """
        item_id = str(uuid.uuid4())
        timestamp = datetime.utcnow()

        file_path_obj = Path(file_path)
        if file_path_obj.is_absolute():
            try:
                relative_path = str(file_path_obj.relative_to(self.manager.saiverse_home))
            except ValueError:
                relative_path = file_path
        else:
            relative_path = file_path

        db = self.manager.SessionLocal()
        try:
            item_row = ItemModel(
                ITEM_ID=item_id,
                NAME=name,
                TYPE="picture",
                DESCRIPTION=description,
                FILE_PATH=relative_path,
                CREATOR_ID=creator_id,
                SOURCE_CONTEXT=source_context,
                CREATED_AT=timestamp,
                UPDATED_AT=timestamp,
            )
            db.add(item_row)

            location_row = ItemLocationModel(
                ITEM_ID=item_id,
                OWNER_KIND="building",
                OWNER_ID=building_id,
                UPDATED_AT=timestamp,
            )
            db.add(location_row)
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"Failed to create picture item: {exc}") from exc
        finally:
            db.close()

        self.items[item_id] = {
            "item_id": item_id,
            "name": name,
            "type": "picture",
            "description": description,
            "file_path": relative_path,
            "state": {},
            "creator_id": creator_id,
            "source_context": source_context,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.item_locations[item_id] = {
            "owner_kind": "building",
            "owner_id": building_id,
            "updated_at": timestamp,
            "location_id": None,
        }
        self.items_by_building[building_id].append(item_id)
        self.refresh_building_system_instruction(building_id)

        building_name = self.manager.building_map.get(building_id).name if building_id in self.manager.building_map else building_id
        note = (
            '<div class="note-box">🖼 User Upload:<br>'
            f'<b>User uploaded picture "{name}" to {building_name}.</b></div>'
        )
        self.manager._append_building_history_note(building_id, note)

        LOGGER.info(f"Created picture item {item_id} from user upload in {building_id}")
        return item_id

    def create_document_item_for_user(
        self, name: str, description: str, file_path: str, building_id: str,
        is_open: bool = True, creator_id: Optional[str] = None, source_context: Optional[str] = None,
    ) -> str:
        """Create a document item from user upload and place it in the specified building.

        Unlike create_document_item, this does not require a persona and is for user uploads.
        The document is created as is_open=True by default so it will be included in visual context.
        """
        item_id = str(uuid.uuid4())
        timestamp = datetime.utcnow()

        file_path_obj = Path(file_path)
        if file_path_obj.is_absolute():
            try:
                relative_path = str(file_path_obj.relative_to(self.manager.saiverse_home))
            except ValueError:
                relative_path = file_path
        else:
            relative_path = file_path

        initial_state = {"is_open": is_open}

        db = self.manager.SessionLocal()
        try:
            item_row = ItemModel(
                ITEM_ID=item_id,
                NAME=name,
                TYPE="document",
                DESCRIPTION=description,
                FILE_PATH=relative_path,
                STATE_JSON=json.dumps(initial_state),
                CREATOR_ID=creator_id,
                SOURCE_CONTEXT=source_context,
                CREATED_AT=timestamp,
                UPDATED_AT=timestamp,
            )
            db.add(item_row)

            location_row = ItemLocationModel(
                ITEM_ID=item_id,
                OWNER_KIND="building",
                OWNER_ID=building_id,
                UPDATED_AT=timestamp,
            )
            db.add(location_row)
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"Failed to create document item: {exc}") from exc
        finally:
            db.close()

        self.items[item_id] = {
            "item_id": item_id,
            "name": name,
            "type": "document",
            "description": description,
            "file_path": relative_path,
            "state": initial_state,
            "creator_id": creator_id,
            "source_context": source_context,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.item_locations[item_id] = {
            "owner_kind": "building",
            "owner_id": building_id,
            "updated_at": timestamp,
            "location_id": None,
        }
        self.items_by_building[building_id].append(item_id)
        self.refresh_building_system_instruction(building_id)

        building_name = self.manager.building_map.get(building_id).name if building_id in self.manager.building_map else building_id
        note = (
            '<div class="note-box">📄 User Upload:<br>'
            f'<b>User uploaded document "{name}" to {building_name}.</b></div>'
        )
        self.manager._append_building_history_note(building_id, note)

        LOGGER.info(f"Created document item {item_id} from user upload in {building_id}")
        return item_id

    # ========== Bag operations ==========

    def get_items_in_bag(self, bag_item_id: str) -> List[Dict]:
        """Get all items directly contained in a bag."""
        items = []
        for item_id in self.items_by_bag.get(bag_item_id, []):
            item = self.items.get(item_id)
            if item:
                items.append(item)
        return items

    def get_bag_contents_recursive(
        self, bag_item_id: str, max_depth: int = 10, _visited: Optional[set] = None,
    ) -> List[Dict]:
        """Get bag contents recursively, including nested bags.

        Each returned dict includes a '_depth' key indicating nesting level
        and '_children' for nested bag contents.
        Returns a tree structure for display purposes.
        """
        if _visited is None:
            _visited = set()
        if bag_item_id in _visited or max_depth <= 0:
            return []
        _visited.add(bag_item_id)

        result = []
        for item_id in self.items_by_bag.get(bag_item_id, []):
            item = self.items.get(item_id)
            if not item:
                continue
            entry = dict(item)
            item_type = (item.get("type") or "").lower()
            if item_type == "bag":
                entry["_children"] = self.get_bag_contents_recursive(
                    item_id, max_depth - 1, _visited,
                )
            else:
                entry["_children"] = []
            result.append(entry)
        return result

    def _is_ancestor_bag(self, item_id: str, potential_ancestor_id: str, max_depth: int = 50) -> bool:
        """Check if potential_ancestor_id is an ancestor bag of item_id (circular reference check)."""
        current = item_id
        visited = set()
        for _ in range(max_depth):
            loc = self.item_locations.get(current)
            if not loc or loc.get("owner_kind") != "bag":
                return False
            parent_bag_id = loc.get("owner_id")
            if not parent_bag_id or parent_bag_id in visited:
                return False
            if parent_bag_id == potential_ancestor_id:
                return True
            visited.add(parent_bag_id)
            current = parent_bag_id
        return False

    def _find_building_for_bag(self, bag_item_id: str, max_depth: int = 50) -> Optional[str]:
        """Find the building that ultimately contains this bag (traversing parent bags)."""
        current = bag_item_id
        visited = set()
        for _ in range(max_depth):
            loc = self.item_locations.get(current)
            if not loc:
                return None
            kind = loc.get("owner_kind")
            owner = loc.get("owner_id")
            if kind == "building":
                return owner
            if kind == "bag" and owner and owner not in visited:
                visited.add(current)
                current = owner
                continue
            return None
        return None

    def move_item(
        self, persona_id: str, item_ids: List[str],
        destination_kind: str, destination_id: str,
    ) -> str:
        """Move items to a destination (building, persona inventory, or bag).

        Args:
            persona_id: The persona performing the action.
            item_ids: List of item IDs to move (max 100).
            destination_kind: "building", "persona", or "bag".
            destination_id: building_id, "self" (for persona), or bag item_id.
        """
        if len(item_ids) > 100:
            raise RuntimeError("一度に移動できるアイテムは最大100個です。")

        persona = self.manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")

        # Resolve destination
        if destination_kind == "persona":
            destination_id = persona_id
        elif destination_kind == "building":
            if not destination_id:
                destination_id = persona.current_building_id
            if not destination_id:
                raise RuntimeError("移動先のBuildingが不明です。")
            if destination_id not in self.manager.building_map:
                raise RuntimeError(f"Building '{destination_id}' が見つかりません。")
        elif destination_kind == "bag":
            if not destination_id:
                raise RuntimeError("移動先のBagアイテムIDが必要です。")
            bag_item = self.items.get(destination_id)
            if not bag_item:
                raise RuntimeError(f"Bagアイテム '{destination_id}' が見つかりません。")
            if (bag_item.get("type") or "").lower() != "bag":
                raise RuntimeError(f"アイテム '{destination_id}' はBagタイプではありません。")
        else:
            raise RuntimeError(f"未対応の移動先タイプ: {destination_kind}")

        # Validate all items exist and are accessible
        validated_items = []
        for item_id in item_ids:
            item = self.items.get(item_id)
            if not item:
                raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")

            # Cannot move item into itself
            if destination_kind == "bag" and item_id == destination_id:
                raise RuntimeError(f"アイテム '{item_id}' を自分自身の中に入れることはできません。")

            # Circular reference check for bags
            if destination_kind == "bag":
                if (item.get("type") or "").lower() == "bag":
                    if self._is_ancestor_bag(destination_id, item_id):
                        item_name = item.get("name", item_id)
                        raise RuntimeError(
                            f"循環参照: '{item_name}' は移動先Bagの祖先です。"
                        )

            # Check accessibility: item must be in persona's inventory, current building, or a bag in current building
            location = self.item_locations.get(item_id)
            if location:
                loc_kind = location.get("owner_kind")
                loc_owner = location.get("owner_id")
                in_inventory = loc_kind == "persona" and loc_owner == persona_id
                in_current_building = loc_kind == "building" and loc_owner == persona.current_building_id
                in_bag = loc_kind == "bag"
                if not (in_inventory or in_current_building or in_bag):
                    raise RuntimeError(
                        f"アイテム '{item.get('name', item_id)}' にアクセスできません。"
                    )

            validated_items.append((item_id, item))

        # Execute moves
        timestamp = datetime.utcnow()
        moved_names = []
        db = self.manager.SessionLocal()
        try:
            for item_id, item in validated_items:
                row = db.query(ItemLocationModel).filter(
                    ItemLocationModel.ITEM_ID == item_id
                ).one_or_none()
                if row is None:
                    row = ItemLocationModel(
                        ITEM_ID=item_id,
                        OWNER_KIND=destination_kind,
                        OWNER_ID=destination_id,
                        UPDATED_AT=timestamp,
                    )
                    db.add(row)
                else:
                    row.OWNER_KIND = destination_kind
                    row.OWNER_ID = destination_id
                    row.UPDATED_AT = timestamp
                moved_names.append(item.get("name", item_id))
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RuntimeError(f"データベース更新に失敗しました: {exc}") from exc
        finally:
            db.close()

        # Update in-memory cache
        for item_id, _item in validated_items:
            self.update_item_cache(item_id, destination_kind, destination_id, timestamp)

        self._sync_to_state()

        # Build result message
        if destination_kind == "building":
            dest_name = (
                self.manager.building_map[destination_id].name
                if destination_id in self.manager.building_map
                else destination_id
            )
            dest_label = f"Building「{dest_name}」"
        elif destination_kind == "persona":
            dest_label = "自分のインベントリ"
        else:
            bag_item = self.items.get(destination_id)
            bag_name = bag_item.get("name", destination_id) if bag_item else destination_id
            dest_label = f"Bag「{bag_name}」"

        if len(moved_names) == 1:
            actor_msg = f"「{moved_names[0]}」を{dest_label}に移動した。"
        else:
            actor_msg = f"{len(moved_names)}個のアイテムを{dest_label}に移動した。"

        self.manager.record_persona_event(persona_id, actor_msg)

        building_id = persona.current_building_id
        if building_id:
            other_ids = [
                oid for oid in self.manager.occupants.get(building_id, [])
                if oid and oid != persona_id
            ]
            if other_ids:
                notice = f"{persona.persona_name}が{actor_msg}"
                self.broadcast_item_event(other_ids, notice)

            note = (
                '<div class="note-box">📦 Item Move:<br>'
                f'<b>{persona.persona_name}が{actor_msg}</b></div>'
            )
            self.manager._append_building_history_note(building_id, note)

        return actor_msg

    def view_items(self, persona_id: str, item_ids: List[str]) -> str:
        """View multiple items (up to 5). For bags, shows contents list."""
        if len(item_ids) > 5:
            raise RuntimeError("一度に閲覧できるアイテムは最大5個です。")

        persona = self.manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            raise RuntimeError("このペルソナではアイテムを扱えません。")

        results = []
        for item_id in item_ids:
            item = self.items.get(item_id)
            if not item:
                results.append(f"[{item_id}] アイテムが見つかりません。")
                continue

            item_type = (item.get("type") or "").lower()
            item_name = item.get("name", item_id)

            if item_type == "bag":
                contents = self.get_bag_contents_recursive(item_id)
                results.append(self._format_bag_contents(item_name, item_id, item, contents))
            elif item_type == "picture":
                file_path_str = item.get("file_path")
                if not file_path_str:
                    results.append(f"[{item_name}] この画像にはファイルパスが設定されていません。")
                    continue
                file_path = self._resolve_file_path(file_path_str)
                if not file_path.exists():
                    results.append(f"[{item_name}] ファイルが見つかりません: {file_path}")
                    continue
                results.append(f"[{item_name}] 画像ファイル: {file_path}")
            elif item_type == "document":
                file_path_str = item.get("file_path")
                if not file_path_str:
                    results.append(f"[{item_name}] この文書にはファイルパスが設定されていません。")
                    continue
                file_path = self._resolve_file_path(file_path_str)
                if not file_path.exists():
                    results.append(f"[{item_name}] ファイルが見つかりません: {file_path}")
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8")
                    results.append(f"[{item_name}] 文書の内容:\n\n{content}")
                except OSError as exc:
                    results.append(f"[{item_name}] ファイル読み込みエラー: {exc}")
            else:
                description = (item.get("description") or "").strip() or "(説明なし)"
                results.append(f"[{item_name}] {description}")

        return "\n\n---\n\n".join(results)

    def _format_bag_contents(
        self, bag_name: str, bag_id: str, bag_item: Dict, contents: List[Dict], indent: int = 0,
    ) -> str:
        """Format bag contents as readable text."""
        prefix = "  " * indent
        description = (bag_item.get("description") or "").strip() or "(説明なし)"
        lines = [f"{prefix}[Bag] {bag_name} (id: {bag_id})"]
        lines.append(f"{prefix}  {description}")

        if not contents:
            lines.append(f"{prefix}  (空)")
        else:
            lines.append(f"{prefix}  中身 ({len(contents)}個):")
            for entry in contents:
                child_id = entry.get("item_id", "")
                child_name = entry.get("name", "不明")
                child_type = (entry.get("type") or "").lower()
                child_desc = (entry.get("description") or "").strip() or "(説明なし)"
                if len(child_desc) > 100:
                    child_desc = child_desc[:97] + "..."
                type_label = {
                    "picture": "Image", "document": "Document",
                    "object": "Object", "bag": "Bag",
                }.get(child_type, child_type.capitalize() or "Item")

                lines.append(f"{prefix}  - [{type_label}] {child_name} (id: {child_id})")
                lines.append(f"{prefix}    {child_desc}")

                children = entry.get("_children", [])
                if children and child_type == "bag":
                    child_item = self.items.get(child_id, entry)
                    sub_text = self._format_bag_contents(
                        child_name, child_id, child_item, children, indent + 2,
                    )
                    # Skip the header line (already printed above)
                    sub_lines = sub_text.split("\n")
                    # Append only the content lines (skip first 2 that duplicate header)
                    for sl in sub_lines[2:]:
                        lines.append(sl)
        return "\n".join(lines)

    def get_bag_items_in_building(self, building_id: str) -> List[Dict]:
        """Get all bag-type items in a building."""
        bags = []
        for item_id in self.items_by_building.get(building_id, []):
            item = self.items.get(item_id)
            if item and (item.get("type") or "").lower() == "bag":
                bags.append(item)
        return bags

    def get_items_inside_bags_in_building(self, building_id: str) -> set:
        """Get set of item IDs that are inside bags that are in a building.

        Used to exclude bag-contained items from top-level building item lists.
        """
        bag_contained_ids: set = set()
        bag_ids = [
            item_id for item_id in self.items_by_building.get(building_id, [])
            if self.items.get(item_id, {}).get("type", "").lower() == "bag"
        ]
        for bag_id in bag_ids:
            self._collect_bag_contents_recursive(bag_id, bag_contained_ids)
        return bag_contained_ids

    def _collect_bag_contents_recursive(self, bag_id: str, collected: set, max_depth: int = 10) -> None:
        """Recursively collect all item IDs inside a bag."""
        if max_depth <= 0 or bag_id in collected:
            return
        for item_id in self.items_by_bag.get(bag_id, []):
            collected.add(item_id)
            item = self.items.get(item_id)
            if item and (item.get("type") or "").lower() == "bag":
                self._collect_bag_contents_recursive(item_id, collected, max_depth - 1)


__all__ = ["ItemService"]
