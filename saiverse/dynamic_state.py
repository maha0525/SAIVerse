"""Dynamic State Sync — A/B/Cの3状態モデルによるBuilding状態管理。

A（ベースライン）は安定したコンテキスト先頭に配置され、Metabolismまで変化しない。
B（最終通知済み状態）はイベントメッセージを注入するたびに更新される。
C（現在状態）はin-memoryキャッシュからリアルタイムで計算される。

B ≠ C のときにイベントメッセージを会話履歴末尾に挿入し、LLMへの通知を行う。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)


@dataclass
class OccupantEntry:
    id: str
    name: str
    kind: str  # "persona" | "user"


@dataclass
class ItemEntry:
    item_id: str
    name: str
    item_type: str
    slot: str  # e.g. "b:3" or "i:2"


@dataclass
class MemopediaPageEntry:
    page_id: str
    title: str
    updated_at: int  # Unix timestamp（更新検出用）


@dataclass
class ChronicleEntryItem:
    entry_id: str
    level: int
    created_at: int  # Unix timestamp


@dataclass
class BuildingStateSnapshot:
    building_id: str
    building_name: str
    items: List[ItemEntry] = field(default_factory=list)
    occupants: List[OccupantEntry] = field(default_factory=list)
    memopedia_pages: List[MemopediaPageEntry] = field(default_factory=list)
    chronicle_entries: List[ChronicleEntryItem] = field(default_factory=list)
    captured_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {
                "building_id": self.building_id,
                "building_name": self.building_name,
                "items": [asdict(i) for i in self.items],
                "occupants": [asdict(o) for o in self.occupants],
                "memopedia_pages": [asdict(p) for p in self.memopedia_pages],
                "chronicle_entries": [asdict(e) for e in self.chronicle_entries],
                "captured_at": self.captured_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, data: str) -> "BuildingStateSnapshot":
        d = json.loads(data)
        return cls(
            building_id=d["building_id"],
            building_name=d["building_name"],
            items=[ItemEntry(**i) for i in d.get("items", [])],
            occupants=[OccupantEntry(**o) for o in d.get("occupants", [])],
            memopedia_pages=[MemopediaPageEntry(**p) for p in d.get("memopedia_pages", [])],
            chronicle_entries=[ChronicleEntryItem(**e) for e in d.get("chronicle_entries", [])],
            captured_at=d.get("captured_at", 0.0),
        )


@dataclass
class StateChange:
    kind: str   # "item_added" | "item_removed" | "item_renamed" | "occupant_entered" | "occupant_left"
    label: str  # 人間が読めるラベル（イベントメッセージに使用）


class DynamicStateManager:
    """Building状態のA/B/C管理とイベントメッセージ注入を行うシングルトン的マネージャー。"""

    # ---- スナップショット構築 ----

    @staticmethod
    def capture_current_state(
        persona: Any,
        building_id: str,
        manager: Any,
    ) -> BuildingStateSnapshot:
        """現在のWorld状態（C）をin-memoryキャッシュから構築する。"""
        building_map = {b.building_id: b for b in manager.buildings}
        building = building_map.get(building_id)
        building_name = building.name if building else building_id

        item_service = getattr(manager, "item_service", None)
        items: List[ItemEntry] = []
        if item_service:
            building_item_ids = list(item_service.items_by_building.get(building_id, []))
            for item_id in building_item_ids:
                item_data = item_service.items.get(item_id)
                if not item_data:
                    continue
                loc = item_service.item_locations.get(item_id, {})
                slot_num = loc.get("slot_number")
                slot = f"b:{slot_num}" if slot_num is not None else "b:?"
                items.append(ItemEntry(
                    item_id=item_id,
                    name=item_data.get("name", ""),
                    item_type=item_data.get("type", "object"),
                    slot=slot,
                ))

            persona_id = getattr(persona, "persona_id", None)
            if persona_id:
                inv_item_ids = list(item_service.items_by_persona.get(persona_id, []))
                for item_id in inv_item_ids:
                    item_data = item_service.items.get(item_id)
                    if not item_data:
                        continue
                    loc = item_service.item_locations.get(item_id, {})
                    slot_num = loc.get("slot_number")
                    slot = f"i:{slot_num}" if slot_num is not None else "i:?"
                    items.append(ItemEntry(
                        item_id=item_id,
                        name=item_data.get("name", ""),
                        item_type=item_data.get("type", "object"),
                        slot=slot,
                    ))

        occupants: List[OccupantEntry] = []
        raw_occupants = list(manager.occupants.get(building_id, []))
        persona_id_self = getattr(persona, "persona_id", None)
        persona_ids = set(getattr(manager, "personas", {}).keys())
        id_to_name = getattr(manager, "id_to_name_map", {})
        for oid in raw_occupants:
            if oid == persona_id_self:
                continue  # 自分自身は除外
            name = id_to_name.get(str(oid), str(oid))
            kind = "persona" if oid in persona_ids else "user"
            occupants.append(OccupantEntry(id=oid, name=name, kind=kind))

        sai_mem = getattr(persona, "sai_memory", None)
        memopedia_pages = DynamicStateManager._capture_memopedia(sai_mem)
        chronicle_entries = DynamicStateManager._capture_chronicle(sai_mem)

        return BuildingStateSnapshot(
            building_id=building_id,
            building_name=building_name,
            items=items,
            occupants=occupants,
            memopedia_pages=memopedia_pages,
            chronicle_entries=chronicle_entries,
        )

    # ---- DB操作 ----

    @staticmethod
    def get_baseline(persona_id: str, building_id: str, db: "Session") -> Optional[BuildingStateSnapshot]:
        from database.models import PersonaBuildingState
        row = db.query(PersonaBuildingState).filter_by(
            PERSONA_ID=persona_id, BUILDING_ID=building_id
        ).first()
        if row and row.BASELINE_JSON:
            try:
                return BuildingStateSnapshot.from_json(row.BASELINE_JSON)
            except Exception as exc:
                LOGGER.warning("Failed to parse baseline JSON for %s/%s: %s", persona_id, building_id, exc)
        return None

    @staticmethod
    def get_last_notified(persona_id: str, building_id: str, db: "Session") -> Optional[BuildingStateSnapshot]:
        from database.models import PersonaBuildingState
        row = db.query(PersonaBuildingState).filter_by(
            PERSONA_ID=persona_id, BUILDING_ID=building_id
        ).first()
        if row and row.LAST_NOTIFIED_JSON:
            try:
                return BuildingStateSnapshot.from_json(row.LAST_NOTIFIED_JSON)
            except Exception as exc:
                LOGGER.warning("Failed to parse last_notified JSON for %s/%s: %s", persona_id, building_id, exc)
        return None

    @staticmethod
    def _upsert_row(persona_id: str, building_id: str, db: "Session") -> Any:
        from database.models import PersonaBuildingState
        from datetime import datetime
        row = db.query(PersonaBuildingState).filter_by(
            PERSONA_ID=persona_id, BUILDING_ID=building_id
        ).first()
        if not row:
            row = PersonaBuildingState(
                PERSONA_ID=persona_id,
                BUILDING_ID=building_id,
                UPDATED_AT=datetime.now(),
            )
            db.add(row)
        return row

    @staticmethod
    def save_baseline(persona_id: str, building_id: str, snapshot: BuildingStateSnapshot, db: "Session") -> None:
        from datetime import datetime
        try:
            row = DynamicStateManager._upsert_row(persona_id, building_id, db)
            row.BASELINE_JSON = snapshot.to_json()
            row.LAST_NOTIFIED_JSON = snapshot.to_json()  # A更新時はBもリセット
            row.UPDATED_AT = datetime.now()
            db.commit()
            LOGGER.debug("[dynamic_state] Saved baseline for %s/%s", persona_id, building_id)
        except Exception as exc:
            db.rollback()
            LOGGER.error("[dynamic_state] Failed to save baseline: %s", exc, exc_info=True)

    @staticmethod
    def save_last_notified(persona_id: str, building_id: str, snapshot: BuildingStateSnapshot, db: "Session") -> None:
        from datetime import datetime
        try:
            row = DynamicStateManager._upsert_row(persona_id, building_id, db)
            row.LAST_NOTIFIED_JSON = snapshot.to_json()
            row.UPDATED_AT = datetime.now()
            db.commit()
            LOGGER.debug("[dynamic_state] Saved last_notified for %s/%s", persona_id, building_id)
        except Exception as exc:
            db.rollback()
            LOGGER.error("[dynamic_state] Failed to save last_notified: %s", exc, exc_info=True)

    # ---- Memopedia / Chronicle スナップショット取得 ----

    @staticmethod
    def _capture_memopedia(sai_mem: Any) -> List[MemopediaPageEntry]:
        """SAIMemoryのSQLiteからMemopediaページ一覧を取得する。"""
        if not sai_mem or not getattr(sai_mem, "conn", None):
            return []
        try:
            cur = sai_mem.conn.execute(
                "SELECT id, title, updated_at FROM memopedia_pages "
                "WHERE COALESCE(is_deleted, 0) = 0 "
                "ORDER BY updated_at DESC LIMIT 200"
            )
            return [
                MemopediaPageEntry(page_id=row[0], title=row[1], updated_at=int(row[2] or 0))
                for row in cur.fetchall()
            ]
        except Exception as exc:
            LOGGER.debug("[dynamic_state] Failed to capture memopedia: %s", exc)
            return []

    @staticmethod
    def _capture_chronicle(sai_mem: Any) -> List[ChronicleEntryItem]:
        """SAIMemoryのSQLiteからChronicleエントリ（最新50件）を取得する。"""
        if not sai_mem or not getattr(sai_mem, "conn", None):
            return []
        try:
            cur = sai_mem.conn.execute(
                "SELECT id, level, created_at FROM arasuji_entries "
                "ORDER BY created_at DESC LIMIT 50"
            )
            return [
                ChronicleEntryItem(entry_id=row[0], level=int(row[1] or 1), created_at=int(row[2] or 0))
                for row in cur.fetchall()
            ]
        except Exception as exc:
            LOGGER.debug("[dynamic_state] Failed to capture chronicle: %s", exc)
            return []

    # ---- 差分計算 ----

    @staticmethod
    def compute_diff(b: BuildingStateSnapshot, c: BuildingStateSnapshot) -> List[StateChange]:
        """B状態とC状態の差分を計算してStateChangeのリストを返す。"""
        changes: List[StateChange] = []

        # --- アイテム差分 ---
        b_items = {e.item_id: e for e in b.items}
        c_items = {e.item_id: e for e in c.items}

        for item_id, c_item in c_items.items():
            if item_id not in b_items:
                changes.append(StateChange(
                    kind="item_added",
                    label=f"アイテム「{c_item.name}」({c_item.slot}) が追加されました",
                ))
            elif b_items[item_id].name != c_item.name:
                changes.append(StateChange(
                    kind="item_renamed",
                    label=f"アイテム「{b_items[item_id].name}」が「{c_item.name}」に名前変更されました",
                ))
            elif b_items[item_id].slot != c_item.slot:
                changes.append(StateChange(
                    kind="item_moved",
                    label=f"アイテム「{c_item.name}」が {b_items[item_id].slot} から {c_item.slot} へ移動されました",
                ))

        for item_id, b_item in b_items.items():
            if item_id not in c_items:
                changes.append(StateChange(
                    kind="item_removed",
                    label=f"アイテム「{b_item.name}」({b_item.slot}) が削除されました",
                ))

        # --- 入退室差分 ---
        b_occ = {e.id: e for e in b.occupants}
        c_occ = {e.id: e for e in c.occupants}

        for oid, c_entry in c_occ.items():
            if oid not in b_occ:
                changes.append(StateChange(
                    kind="occupant_entered",
                    label=f"{c_entry.name} が入室しました",
                ))

        for oid, b_entry in b_occ.items():
            if oid not in c_occ:
                changes.append(StateChange(
                    kind="occupant_left",
                    label=f"{b_entry.name} が退室しました",
                ))

        # --- Memopedia差分 ---
        b_mp = {e.page_id: e for e in b.memopedia_pages}
        c_mp = {e.page_id: e for e in c.memopedia_pages}

        for page_id, c_page in c_mp.items():
            if page_id not in b_mp:
                changes.append(StateChange(
                    kind="memopedia_created",
                    label=f"Memopedia「{c_page.title}」が作成されました",
                ))
            elif b_mp[page_id].updated_at < c_page.updated_at:
                changes.append(StateChange(
                    kind="memopedia_updated",
                    label=f"Memopedia「{c_page.title}」が更新されました",
                ))

        for page_id, b_page in b_mp.items():
            if page_id not in c_mp:
                changes.append(StateChange(
                    kind="memopedia_deleted",
                    label=f"Memopedia「{b_page.title}」が削除されました",
                ))

        # --- Chronicle差分 ---
        b_chr = {e.entry_id for e in b.chronicle_entries}
        new_chr = [e for e in c.chronicle_entries if e.entry_id not in b_chr]
        if new_chr:
            by_level: Dict[int, int] = {}
            for e in new_chr:
                by_level[e.level] = by_level.get(e.level, 0) + 1
            level_str = "、".join(f"Level {lv} × {cnt}件" for lv, cnt in sorted(by_level.items()))
            changes.append(StateChange(
                kind="chronicle_added",
                label=f"Chronicleに新しいエントリが追加されました（{level_str}）",
            ))

        return changes

    @staticmethod
    def format_event_message(changes: List[StateChange]) -> str:
        lines = ["[システム通知]"]
        for ch in changes:
            lines.append(f"- {ch.label}")
        return "\n".join(lines)

    # ---- メインエントリポイント ----

    @staticmethod
    def maybe_inject_event_messages(persona: Any, manager: Any) -> bool:
        """C ≠ B なら会話履歴にイベントメッセージを挿入し、Bを更新する。

        Returns:
            True if an event message was injected.
        """
        persona_id = getattr(persona, "persona_id", None)
        building_id = getattr(persona, "current_building_id", None)
        if not persona_id or not building_id:
            return False

        sai_mem = getattr(persona, "sai_memory", None)
        if not sai_mem or not sai_mem.is_ready():
            return False

        session_factory = getattr(manager, "SessionLocal", None)
        if not session_factory:
            return False

        db = session_factory()
        try:
            c = DynamicStateManager.capture_current_state(persona, building_id, manager)
            b = DynamicStateManager.get_last_notified(persona_id, building_id, db)

            if b is None:
                # 初回: Bが未設定なのでCをBとAとして保存（イベントメッセージは不要）
                DynamicStateManager.save_baseline(persona_id, building_id, c, db)
                LOGGER.debug("[dynamic_state] Initial snapshot saved for %s/%s", persona_id, building_id)
                return False

            changes = DynamicStateManager.compute_diff(b, c)
            if not changes:
                return False

            msg_text = DynamicStateManager.format_event_message(changes)
            LOGGER.info(
                "[dynamic_state] Injecting event message for %s/%s (%d changes)",
                persona_id, building_id, len(changes),
            )

            message = {
                "role": "user",
                "content": f"<system>{msg_text}</system>",
                "metadata": {
                    "tags": ["internal", "event_message"],
                },
            }
            sai_mem.append_persona_message(message)

            DynamicStateManager.save_last_notified(persona_id, building_id, c, db)
            return True

        except Exception as exc:
            LOGGER.error("[dynamic_state] maybe_inject_event_messages failed: %s", exc, exc_info=True)
            return False
        finally:
            db.close()

    @staticmethod
    def on_building_entered(persona: Any, building_id: str, manager: Any) -> None:
        """ペルソナが新しいBuildingに入室したときの処理。

        - 初訪問: 現在状態をAとして保存
        - 再訪問: Aは既存のものを維持（last_known_state）、Bのみ現在状態で更新して到着イベントメッセージを生成
        """
        persona_id = getattr(persona, "persona_id", None)
        if not persona_id:
            return

        session_factory = getattr(manager, "SessionLocal", None)
        if not session_factory:
            return

        sai_mem = getattr(persona, "sai_memory", None)
        if not sai_mem or not sai_mem.is_ready():
            return

        db = session_factory()
        try:
            c = DynamicStateManager.capture_current_state(persona, building_id, manager)
            existing_b = DynamicStateManager.get_last_notified(persona_id, building_id, db)
            existing_a = DynamicStateManager.get_baseline(persona_id, building_id, db)

            if existing_a is None:
                # 初訪問: フルスナップショットとしてAとBを保存
                DynamicStateManager.save_baseline(persona_id, building_id, c, db)
                LOGGER.debug("[dynamic_state] First visit snapshot for %s/%s", persona_id, building_id)
            else:
                # 再訪問: Aは維持、BとCを比較して到着イベントメッセージを生成
                b_for_diff = existing_b or existing_a
                changes = DynamicStateManager.compute_diff(b_for_diff, c)
                if changes:
                    msg_text = DynamicStateManager.format_event_message(changes)
                    message = {
                        "role": "user",
                        "content": f"<system>{msg_text}</system>",
                        "metadata": {
                            "tags": ["internal", "event_message"],
                        },
                    }
                    sai_mem.append_persona_message(message)
                    LOGGER.info(
                        "[dynamic_state] Arrival event message for %s/%s (%d changes)",
                        persona_id, building_id, len(changes),
                    )

                DynamicStateManager.save_last_notified(persona_id, building_id, c, db)

            # ビジュアルコンテキストキャッシュを無効化
            persona._visual_context_cache = None
            persona._visual_context_anchor = None

        except Exception as exc:
            LOGGER.error("[dynamic_state] on_building_entered failed: %s", exc, exc_info=True)
        finally:
            db.close()

    @staticmethod
    def on_metabolism(persona: Any, manager: Any) -> None:
        """Metabolism発火時にAをフルスナップショットで更新し、Bをリセットする。"""
        persona_id = getattr(persona, "persona_id", None)
        building_id = getattr(persona, "current_building_id", None)
        if not persona_id or not building_id:
            return

        session_factory = getattr(manager, "SessionLocal", None)
        if not session_factory:
            return

        db = session_factory()
        try:
            c = DynamicStateManager.capture_current_state(persona, building_id, manager)
            DynamicStateManager.save_baseline(persona_id, building_id, c, db)
            LOGGER.info("[dynamic_state] Metabolism snapshot saved for %s/%s", persona_id, building_id)

            # ビジュアルコンテキストキャッシュを無効化（次回パルスで再生成）
            persona._visual_context_cache = None
            persona._visual_context_anchor = None

        except Exception as exc:
            LOGGER.error("[dynamic_state] on_metabolism failed: %s", exc, exc_info=True)
        finally:
            db.close()
